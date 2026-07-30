"""Row-level-security policy SQL for tenanted tables.

The database half of the fail-closed guarantee: a ``tenant_scope`` policy on a table
enforces the active frame on every statement -- joins, cascades, ``_base_manager``,
``instance.save()`` and raw SQL included -- none of which ever consult a Django manager.
Policies read the ``tenant.*`` session settings published by
:mod:`guitars.tenancy.guc`.

These are functions rather than format-string constants because the predicate is composed
from a variable-length ``{dimension: column}`` mapping. ``maketenantmigrations`` writes
migrations that call them at *migrate* time.

**Nothing here reads Django settings, and that is deliberate.** Whether to emit ``FORCE``,
and which roles are exempt, are decided by the generator and written into the migration as
literal arguments. A migration whose SQL depended on the settings in force when it ran
would produce different databases from the same migration history -- and would silently
change an already-reviewed migration's meaning when someone edited a setting.

The tenant predicate is deliberately the **list-tolerant** form::

    <column>::text = ANY(string_to_array(
        (SELECT current_setting('tenant.<dim>', true)), ','))

because ``guc.encode_value`` always encodes a dimension as a separated list -- one policy
form handles a scalar scope and a collection scope alike, with no cast to trip over. Every
NULL path denies: an unset GUC yields NULL (-> ``ANY(NULL)`` -> NULL -> deny) and an empty
string yields an empty array.

Each ``current_setting`` sits inside a **scalar subquery** on purpose. A bare call in a
qual is re-evaluated for every candidate row; wrapping it lets the planner hoist it to an
InitPlan evaluated once per statement (the documented PostgreSQL RLS pattern).
Predicate-equivalent -- a scalar subquery over a zero-table SELECT returns exactly the
same value, NULL included -- so the fail-closed reasoning above is untouched.
"""

from __future__ import annotations

from guitars.gucs import BYPASS_GUC, VALUE_SEPARATOR, guc_name


__all__ = [
    'EXEMPT_POLICY_PREFIX',
    'TENANT_POLICY',
    'create_exempt_policy',
    'create_table_rls',
    'create_tenant_policy',
    'disable_rls',
    'drop_exempt_policy',
    'drop_table_rls',
    'drop_tenant_policy',
    'enable_rls',
    'force_rls',
    'no_force_rls',
]

TENANT_POLICY = 'tenant_scope'
"""Policy enforcing the tenant frame. One name everywhere, greppable in pg_policies."""

EXEMPT_POLICY_PREFIX = 'rls_exempt_'
"""Prefix for a per-role read-only exemption policy. Explicit and auditable."""

#: Alias for the ancestor table in an MTI owner-join policy. Prefixed so it cannot collide
#: with a real table or alias in the statement being filtered.
_OWNER_ALIAS = '_guitars_owner'


def _setting(name: str) -> str:
    """``current_setting`` as a scalar subquery, so the planner caches it.

    See the module docstring: bare in a qual it runs per row; hoisted to an InitPlan it
    runs once per statement.
    """
    return f"(SELECT current_setting('{name}', true))"


def _match(qualified_column: str, dimension: str) -> str:
    """One dimension's match test. Membership, not equality -- the GUC carries a list."""
    return (
        f'{qualified_column}::text = ANY(string_to_array('
        f"{_setting(guc_name(dimension))}, '{VALUE_SEPARATOR}'))"
    )


def _owner_exists(
    table: str,
    owner_table: str,
    owner_pk: str,
    child_pk: str,
    owner_columns: dict[str, str],
) -> str:
    """The MTI form: reach the ancestor that physically holds the tenant column.

    A multi-table-inheritance child has its own table, but the tenant column lives on the
    ancestor that declared it. An ancestor-only policy is *not* sufficient: a child-only
    statement -- ``queryset.update()`` on child-local fields, a ``DELETE`` against the
    child table, ``.values()`` of child-only columns -- never touches the ancestor, so the
    ancestor's policy never applies. This is the same problem ``set_parent_updated_at``
    exists to solve for timestamps, reached from the other direction.

    Correlated by the shared-PK invariant every MTI chain satisfies (see the package
    docstring): ``owner_pk = child_pk``.

    Two naming hazards, both handled. The ancestor is aliased, so an unqualified column
    inside the subquery cannot silently resolve to it. And the child's key is written
    table-qualified, so it cannot be shadowed by a same-named column on the ancestor --
    PostgreSQL resolves that name once, at ``CREATE POLICY`` time, into an attribute
    reference on the policy's own relation, so the stored policy still works when a query
    aliases the table.

    Note that the ancestor's *own* RLS policy also applies to this subquery. That is
    correct rather than merely tolerable: it compares the same GUC, so it is satisfied for
    the same tenant and denies for any other -- the two layers agree instead of one
    quietly widening the other.
    """
    matches = ' AND '.join(
        _match(f'{_OWNER_ALIAS}.{column}', dimension)
        for dimension, column in sorted(owner_columns.items())
    )
    # nosec B608 - DDL, not a query: every identifier here is resolved from Django's
    # model _meta by the generator, and the result is written into a migration file for
    # review. There are no runtime values and nothing to parameterise -- PostgreSQL does
    # not accept bind parameters in a policy definition.
    return (  # noqa: S608
        f'EXISTS (\n'  # nosec B608
        f'        SELECT 1 FROM {owner_table} AS {_OWNER_ALIAS}\n'
        f'        WHERE {_OWNER_ALIAS}.{owner_pk} = {table}.{child_pk}\n'
        f'          AND {matches}\n'
        f'    )'
    )


def _predicate(
    table: str,
    columns: dict[str, str],
    owner_table: str | None = None,
    owner_pk: str | None = None,
    child_pk: str | None = None,
    owner_columns: dict[str, str] | None = None,
) -> str:
    """The tenant-match predicate.

    Bypass short-circuits everything; otherwise every dimension must match its published
    value -- those held on this table directly, and those held on an MTI ancestor through
    the correlated subquery. A model may legitimately have both.
    """
    terms = [
        _match(f'{table}.{column}', dimension) for dimension, column in sorted(columns.items())
    ]
    if owner_columns:
        if not (owner_table and owner_pk and child_pk):
            raise ValueError('owner_columns needs owner_table, owner_pk and child_pk to join on.')
        terms.append(_owner_exists(table, owner_table, owner_pk, child_pk, owner_columns))

    if not terms:
        # Emitting a policy with nothing to predicate on would be worse than emitting
        # none: `USING (bypass)` denies every scoped read on the table, which reads as a
        # broken deployment rather than as the configuration mistake it is.
        raise ValueError(
            f'no tenant columns to predicate on for {table!r} -- refusing to emit a '
            f'policy that would deny every scoped statement.'
        )

    return f"{_setting(BYPASS_GUC)} = 'on' OR ({' AND '.join(terms)})"


def create_tenant_policy(
    table: str,
    columns: dict[str, str],
    owner_table: str | None = None,
    owner_pk: str | None = None,
    child_pk: str | None = None,
    owner_columns: dict[str, str] | None = None,
) -> str:
    """``CREATE POLICY tenant_scope`` -- both reads (USING) and writes (WITH CHECK)."""
    predicate = _predicate(table, columns, owner_table, owner_pk, child_pk, owner_columns)
    return (
        f'CREATE POLICY {TENANT_POLICY} ON {table} FOR ALL TO PUBLIC\n'
        f'    USING ({predicate})\n'
        f'    WITH CHECK ({predicate})'
    )


def drop_tenant_policy(table: str) -> str:
    return f'DROP POLICY IF EXISTS {TENANT_POLICY} ON {table}'


def create_exempt_policy(table: str, role: str) -> str:
    """``CREATE POLICY`` granting *role* unfiltered reads -- guarded on the role existing.

    For a reporting or BI account that must see across tenants. One policy per role rather
    than one naming several, so a cluster where only some of them exist still gets the
    rest.

    Roles are cluster-level, and a role provisioned only in staging or production would
    make ``CREATE POLICY ... TO <missing role>`` fail migrate everywhere else -- so local
    and CI databases skip the exemption instead of erroring.
    """
    policy = f'{EXEMPT_POLICY_PREFIX}{role}'
    # nosec B608 - DDL, not a query. The role name comes from GUITARS_RLS_EXEMPT_ROLES, a
    # settings value the project author controls, and lands in a migration file for review.
    return (  # noqa: S608
        'DO $$\n'  # nosec B608
        'BEGIN\n'
        f"    IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN\n"
        f"        EXECUTE 'CREATE POLICY {policy} ON {table} "
        f"FOR SELECT TO {role} USING (true)';\n"
        '    END IF;\n'
        'END $$'
    )


def drop_exempt_policy(table: str, role: str) -> str:
    return f'DROP POLICY IF EXISTS {EXEMPT_POLICY_PREFIX}{role} ON {table}'


def enable_rls(table: str) -> str:
    return f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY'


def disable_rls(table: str) -> str:
    return f'ALTER TABLE {table} DISABLE ROW LEVEL SECURITY'


def force_rls(table: str) -> str:
    """Make the policy bind the table's owner too.

    Not folded into :func:`enable_rls`, because the two are separable on purpose: the
    application role owns its tables (it runs migrations), and **an owner bypasses
    non-FORCE RLS silently** -- no error, no log, rows simply come back unfiltered. So
    ``ENABLE`` alone is inert against the application, which is useful exactly once: as
    the first stage of retrofitting policies onto a populated database.

    Emitted by default. Rollback is ``NO FORCE`` -- seconds, not a migration rewrite.
    """
    return f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY'


def no_force_rls(table: str) -> str:
    return f'ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY'


def create_table_rls(
    table: str,
    columns: dict[str, str],
    owner_table: str | None = None,
    owner_pk: str | None = None,
    child_pk: str | None = None,
    owner_columns: dict[str, str] | None = None,
    force: bool = True,
    exempt_roles: list[str] | None = None,
) -> list[str]:
    """Everything one table needs: the policy, any exemptions, ENABLE, and FORCE.

    *force* and *exempt_roles* arrive as literals written into the migration by the
    generator, never read from settings here -- see the module docstring.
    """
    statements = [
        create_tenant_policy(table, columns, owner_table, owner_pk, child_pk, owner_columns)
    ]
    statements += [create_exempt_policy(table, role) for role in exempt_roles or []]
    statements.append(enable_rls(table))
    if force:
        statements.append(force_rls(table))
    return statements


def drop_table_rls(table: str, exempt_roles: list[str] | None = None) -> list[str]:
    """Reverse of :func:`create_table_rls`, in teardown order.

    ``NO FORCE`` before ``DISABLE`` so the table is never left forced-but-disabled, and the
    policies are dropped last, once nothing is enforcing them.
    """
    return [
        no_force_rls(table),
        disable_rls(table),
        *[drop_exempt_policy(table, role) for role in exempt_roles or []],
        drop_tenant_policy(table),
    ]
