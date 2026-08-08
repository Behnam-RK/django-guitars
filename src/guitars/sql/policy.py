"""Row-level-security policy SQL for tenanted tables.

The database half of the fail-closed guarantee: a ``tenant_scope`` policy on a table
enforces the active frame on every statement -- joins, cascades, ``_base_manager``,
``instance.save()`` and raw SQL included -- none of which ever consult a Django manager.
Policies read the ``tenant.*`` session settings published by
:mod:`guitars.tenancy.guc`.

These are functions rather than format-string constants because the predicate is composed
from a variable-length ``{dimension: column}`` mapping. ``makeguitarmigrations`` writes
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
from guitars.sql._identifiers import (
    _QUOTED_QUALIFIED,
    _QUOTED_UNQUALIFIED,
    _bare,
    _bare_or_qualified,
    _quote_ident,
    _quote_literal,
    _quote_qualified,
    _safe_ident,
)


__all__ = [
    'EXEMPT_POLICY_PREFIX',
    'TENANT_POLICY',
    'create_exempt_policy',
    'create_table_rls',
    'create_tenant_policy',
    'disable_rls',
    'drop_all_exempt_policies',
    'drop_exempt_policy',
    'drop_table_rls',
    'drop_tenant_policy',
    'enable_rls',
    'force_rls',
    'no_force_rls',
    'replace_table_rls',
]

TENANT_POLICY = 'tenant_scope'
"""Policy enforcing the tenant frame. One name everywhere, greppable in pg_policies."""

EXEMPT_POLICY_PREFIX = 'rls_exempt_'
"""Prefix for a per-role read-only exemption policy. Explicit and auditable."""

#: Alias for the ancestor table in an MTI owner-join policy. Prefixed so it cannot collide
#: with a real table or alias in the statement being filtered.
_OWNER_ALIAS = '_guitars_owner'


def _qualified_table(table: str) -> str:
    """A possibly schema-qualified table name.

    Checked first, ahead of either shape :func:`_bare_or_qualified` distinguishes: *table*
    is one well-formed, self-quoted segment (:data:`~guitars.sql._identifiers._QUOTED_UNQUALIFIED`)
    -- an unqualified table pre-wrapped by the caller, e.g. ``'"my.table"'`` with a literal
    ``.`` embedded in its own name. Returned unchanged for the same reason
    :func:`guitars.sql._identifiers._quote_table` checks this first: a dot inside an
    already-self-quoted, schema-less table must not be mistaken for a schema separator and
    split on, which is what happens if this table is instead routed into
    :func:`_bare_or_qualified` below. Deliberately the stricter ``_QUOTED_UNQUALIFIED``, not
    the looser :func:`~guitars.sql._identifiers._is_self_quoted` -- a malformed or
    three-or-more-part string like ``'"a"."b"."c"'`` also starts and ends with ``"`` without
    being one legitimate quoted identifier, and must still reach ``_bare_or_qualified`` below
    to be rejected there.

    Otherwise, two input shapes, two different outputs, because :func:`_bare_or_qualified`
    treats them differently (see its docstring):

    * A bare ``'schema.table'`` (or unqualified ``'table'``) comes back with each part
      already proved bare via :func:`_bare`, so it is joined -- or returned -- unquoted, the
      same as this module has always interpolated a table/column position. Quoting an
      already-lowercase, already-validated identifier would change the SQL text every
      already-generated migration contains for no behavioural gain.
    * Django's own pre-quoted ``'"schema"."table"'`` convention is explicitly *not*
      re-validated as bare by :func:`_bare_or_qualified` -- quoting is what already made a
      hostile part safe, so re-checking it against ``_BARE_IDENTIFIER`` would reject
      legitimate mixed-case/reserved-word content this form exists to carry. That trust has
      to be repaid by re-quoting here: joining the raw, unescaped parts with a bare ``.``
      would render exactly the unquoted, case-folding bug this milestone exists to fix,
      just relocated from the trigger/rule path to the tenant-policy one. A model meant to
      be read and written through the Django ORM *must* use this form for its ``db_table``
      (see :func:`guitars.sql._identifiers._quote_table`'s docstring for why), so this is
      not a corner this module could decline to cover.
    """
    if _QUOTED_UNQUALIFIED.match(table) is not None:
        return table
    pre_quoted = _QUOTED_QUALIFIED.match(table) is not None
    schema, name = _bare_or_qualified('table', table)
    if pre_quoted:
        return _quote_qualified(schema, name)
    return f'{schema}.{name}' if schema else name


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
    owner_table = _qualified_table(owner_table)
    owner_pk = _bare('owner primary key', owner_pk)
    child_pk = _bare('child primary key', child_pk)
    matches = ' AND '.join(
        _match(f'{_OWNER_ALIAS}.{_bare("owner tenant column", column)}', dimension)
        for dimension, column in sorted(owner_columns.items())
    )
    # Suppressed below as DDL, not a query: every identifier here is resolved from Django's
    # model _meta by the generator, and the result is written into a migration file for
    # review. There are no runtime values and nothing to parameterise -- PostgreSQL does
    # not accept bind parameters in a policy definition.
    #
    # The marker sits on the first fragment because that is the line the finding is
    # reported on, and it carries no trailing prose: bandit parses whatever follows the
    # test id as further test ids and warns once per word.
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
    table = _qualified_table(table)
    terms = [
        _match(f'{table}.{_bare("tenant column", column)}', dimension)
        for dimension, column in sorted(columns.items())
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
        f'CREATE POLICY {TENANT_POLICY} ON {_qualified_table(table)} FOR ALL TO PUBLIC\n'
        f'    USING ({predicate})\n'
        f'    WITH CHECK ({predicate})'
    )


def drop_tenant_policy(table: str) -> str:
    return f'DROP POLICY IF EXISTS {TENANT_POLICY} ON {_qualified_table(table)}'


def create_exempt_policy(table: str, role: str) -> str:
    """``CREATE POLICY`` granting *role* unfiltered reads -- guarded on the role existing.

    For a reporting or BI account that must see across tenants. One policy per role rather
    than one naming several, so a cluster where only some of them exist still gets the
    rest.

    Roles are cluster-level, and a role provisioned only in staging or production would
    make ``CREATE POLICY ... TO <missing role>`` fail migrate everywhere else -- so local
    and CI databases skip the exemption instead of erroring.
    """
    table = _qualified_table(table)
    # Every role-derived name is quoted rather than trusted to be bare, and the inner
    # statement is escaped as a whole because EXECUTE takes a string literal -- so the two
    # nesting levels are each escaped once, by the same rules PostgreSQL's own
    # quote_ident/quote_literal use.
    statement = (
        f'CREATE POLICY {_exempt_policy_name(role)} ON {table} '
        f'FOR SELECT TO {_quote_ident(role)} USING (true)'
    )
    # Suppressed below as DDL, not a query, and not an injection boundary: the role name
    # comes from GUITARS_RLS_EXEMPT_ROLES, a settings value the project author controls, and
    # the result lands in a migration file for review. PostgreSQL accepts no bind parameters
    # in a policy definition, so quoting above is the available defence and it is applied.
    return (  # noqa: S608
        'DO $$\n'  # nosec B608
        'BEGIN\n'
        f'    IF EXISTS (SELECT FROM pg_roles WHERE rolname = {_quote_literal(role)}) THEN\n'
        f'        EXECUTE {_quote_literal(statement)};\n'
        '    END IF;\n'
        'END $$'
    )


def _exempt_policy_name(role: str) -> str:
    """The exemption policy's identifier, quoted.

    One function so the ``CREATE`` and the ``DROP`` can never disagree about how a role
    name was spelled -- a mismatch would leave an exemption policy behind that nothing
    knows how to remove.

    Truncated through :func:`_safe_ident` before quoting: ``role`` is free-form ``settings``
    text with no length limit of its own, and a name over PostgreSQL's 63-byte NAMEDATALEN
    would otherwise be silently truncated by PostgreSQL itself -- two distinct long role
    names could collide onto the same truncated policy name with no error either at
    generation or at ``migrate`` time. Shared with
    :func:`guitars.management.enforcement.operations._related_rule_name`, which needs the
    identical truncate-then-quote discipline for a different derived name.
    """
    return _safe_ident(f'{EXEMPT_POLICY_PREFIX}{role}')


def drop_exempt_policy(table: str, role: str) -> str:
    return f'DROP POLICY IF EXISTS {_exempt_policy_name(role)} ON {_qualified_table(table)}'


def drop_all_exempt_policies(table: str) -> str:
    """Drop every exemption policy on *table*, whatever roles it was created for.

    :func:`drop_exempt_policy` can only drop a role it is told about, and the caller
    replacing a policy knows the roles configured *now* -- not the ones configured when the
    policy was written. A role removed from ``GUITARS_RLS_EXEMPT_ROLES`` would therefore
    keep its ``USING (true)`` exemption forever, which is a cross-tenant read the settings
    say was revoked.

    Discovering them from ``pg_policy`` instead of a list is what makes
    :func:`replace_table_rls` converge on the configured set rather than accumulate.
    ``starts_with`` rather than ``LIKE``: the prefix ends in ``_``, which ``LIKE`` would
    read as a single-character wildcard.
    """
    table = _qualified_table(table)
    # Suppressed below as DDL, not a query: *table* is proved bare per part above (the
    # unqualified/bare-qualified shapes) or quoted by _qualified_table itself (Django's
    # pre-quoted shape) -- either way it is not interpolated unescaped. It is spliced as a
    # %s *argument* to format(), not into the format string itself, and quoted as a literal
    # via _quote_literal for the regclass cast -- so neither an embedded single quote (which
    # would otherwise terminate the surrounding SQL string literal early) nor an embedded
    # '%' (which format() would otherwise try to read as its own directive) can corrupt the
    # statement it builds. The policy names are read from the catalog and quoted by
    # format(%I).
    return (  # noqa: S608
        'DO $$\n'  # nosec B608
        'DECLARE\n'
        '    exempt_policy text;\n'
        'BEGIN\n'
        '    FOR exempt_policy IN\n'
        f'        SELECT polname FROM pg_policy WHERE polrelid = {_quote_literal(table)}::regclass\n'
        f'          AND starts_with(polname, {_quote_literal(EXEMPT_POLICY_PREFIX)})\n'
        '    LOOP\n'
        f"        EXECUTE format('DROP POLICY %I ON %s', exempt_policy, {_quote_literal(table)});\n"
        '    END LOOP;\n'
        'END $$'
    )


def enable_rls(table: str) -> str:
    return f'ALTER TABLE {_qualified_table(table)} ENABLE ROW LEVEL SECURITY'


def disable_rls(table: str) -> str:
    return f'ALTER TABLE {_qualified_table(table)} DISABLE ROW LEVEL SECURITY'


def force_rls(table: str) -> str:
    """Make the policy bind the table's owner too.

    Not folded into :func:`enable_rls`, because the two are separable on purpose: the
    application role owns its tables (it runs migrations), and **an owner bypasses
    non-FORCE RLS silently** -- no error, no log, rows simply come back unfiltered. So
    ``ENABLE`` alone is inert against the application, which is useful exactly once: as
    the first stage of retrofitting policies onto a populated database.

    Emitted by default. Rollback is ``NO FORCE`` -- seconds, not a migration rewrite.
    """
    return f'ALTER TABLE {_qualified_table(table)} FORCE ROW LEVEL SECURITY'


def no_force_rls(table: str) -> str:
    return f'ALTER TABLE {_qualified_table(table)} NO FORCE ROW LEVEL SECURITY'


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


def replace_table_rls(
    table: str,
    columns: dict[str, str],
    owner_table: str | None = None,
    owner_pk: str | None = None,
    child_pk: str | None = None,
    owner_columns: dict[str, str] | None = None,
    force: bool = True,
    exempt_roles: list[str] | None = None,
) -> list[str]:
    """Redefine an existing table's policies in place, for a coverage shape that changed.

    PostgreSQL has no ``CREATE OR REPLACE POLICY`` and no ``CREATE POLICY IF NOT EXISTS``, so
    a table whose tenant dimensions, tenant column or exempt roles changed cannot simply be
    re-``create_table_rls``'d -- that fails with "policy tenant_scope already exists". The
    generator emits this instead once it sees a policy whose recorded shape no longer matches
    the models.

    ``ENABLE`` and ``FORCE`` are deliberately left alone rather than cycled: both are
    idempotent ``ALTER TABLE``s that :func:`create_table_rls` re-issues below, and dropping
    them would leave the table briefly unprotected for no gain. Only the policies are
    replaced, so at no point in the transaction is the table enabled-but-unpolicied (which is
    default-DENY) or policied-but-disabled (which is no protection at all).
    """
    return [
        drop_tenant_policy(table),
        drop_all_exempt_policies(table),
        *create_table_rls(
            table,
            columns,
            owner_table,
            owner_pk,
            child_pk,
            owner_columns,
            force=force,
            exempt_roles=exempt_roles,
        ),
    ]


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
