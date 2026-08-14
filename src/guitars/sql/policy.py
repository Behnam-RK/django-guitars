"""Row-level-security policy SQL for tenanted tables -- the database half of the
fail-closed guarantee, predicate shape and all, documented in ``docs/tenancy.md``.
Functions, not constants, for the variable-length ``{dimension: column}`` mapping."""

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
    """A possibly schema-qualified table name. Self-quoted passes through unchanged (a dot
    inside it must not be mistaken for a schema separator). Otherwise
    :func:`_bare_or_qualified`: Django's pre-quoted form is trusted, not re-validated."""
    if _QUOTED_UNQUALIFIED.match(table) is not None:
        return table
    pre_quoted = _QUOTED_QUALIFIED.match(table) is not None
    schema, name = _bare_or_qualified('table', table)
    if pre_quoted:
        return _quote_qualified(schema, name)
    return f'{schema}.{name}' if schema else name


def _setting(name: str) -> str:
    """``current_setting`` as a scalar subquery so the planner hoists it to an InitPlan,
    evaluated once per statement rather than once per row -- see ``docs/tenancy.md``."""
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
    """The MTI owner-join form: reach the ancestor holding the tenant column -- an
    ancestor-only policy misses a child-only statement. See ADR-0003. Correlated on
    ``owner_pk = child_pk``; the ancestor is aliased, the child key table-qualified."""
    owner_table = _qualified_table(owner_table)
    owner_pk = _bare('owner primary key', owner_pk)
    child_pk = _bare('child primary key', child_pk)
    matches = ' AND '.join(
        _match(f'{_OWNER_ALIAS}.{_bare("owner tenant column", column)}', dimension)
        for dimension, column in sorted(owner_columns.items())
    )
    # Suppressed below as DDL, not a query: every identifier is resolved from Django's model
    # _meta and written into a migration file for review; Postgres accepts no bind
    # parameters in a policy definition. Marker on the first fragment: bandit reports there.
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
    """The tenant-match predicate. Bypass short-circuits everything; otherwise every
    dimension must match -- those held on this table directly, and those held on an MTI
    ancestor through the correlated subquery. A model may legitimately have both."""
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
    """``CREATE POLICY`` granting *role* unfiltered reads, guarded on the role existing --
    roles are cluster-level, and one provisioned only in prod would fail ``migrate``
    everywhere else. One policy per role, so a cluster missing some still gets the rest."""
    table = _qualified_table(table)
    # Role-derived name quoted rather than trusted bare; the inner statement escaped as a
    # whole since EXECUTE takes a string literal -- two nesting levels, each escaped once.
    statement = (
        f'CREATE POLICY {_exempt_policy_name(role)} ON {table} '
        f'FOR SELECT TO {_quote_ident(role)} USING (true)'
    )
    # Suppressed below as DDL: the role name comes from GUITARS_RLS_EXEMPT_ROLES (author-
    # controlled) and lands in a migration file for review; Postgres accepts no bind
    # parameters here, so quoting above is the available defence.
    return (  # noqa: S608
        'DO $$\n'  # nosec B608
        'BEGIN\n'
        f'    IF EXISTS (SELECT FROM pg_roles WHERE rolname = {_quote_literal(role)}) THEN\n'
        f'        EXECUTE {_quote_literal(statement)};\n'
        '    END IF;\n'
        'END $$'
    )


def _exempt_policy_name(role: str) -> str:
    """The exemption policy's identifier, quoted -- one function so CREATE and DROP can
    never disagree about the spelling. Truncated via :func:`_safe_ident` before quoting:
    two long role names could otherwise collide onto the same truncated policy name."""
    return _safe_ident(f'{EXEMPT_POLICY_PREFIX}{role}')


def drop_exempt_policy(table: str, role: str) -> str:
    return f'DROP POLICY IF EXISTS {_exempt_policy_name(role)} ON {_qualified_table(table)}'


def drop_all_exempt_policies(table: str) -> str:
    """Drop every exemption policy on *table*, whatever roles it was created for --
    discovered from ``pg_policy``, not a list, so a role removed from
    ``GUITARS_RLS_EXEMPT_ROLES`` doesn't keep a stale cross-tenant exemption forever."""
    table = _qualified_table(table)
    # Suppressed below as DDL: *table* is proved bare or quoted by _qualified_table above,
    # spliced as a %s *argument* to format() and quoted via _quote_literal for the regclass
    # cast -- so neither an embedded quote nor a literal '%' can corrupt the statement.
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
    """Make the policy bind the table's owner too -- separate from :func:`enable_rls` on
    purpose, since an owner bypasses non-FORCE RLS silently (no error, no log). See
    ADR-0002. Emitted by default; rollback is ``NO FORCE``, seconds not a migration rewrite."""
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
    """Everything one table needs: the policy, any exemptions, ENABLE, and FORCE. *force*
    and *exempt_roles* arrive as literals the generator resolved, never settings read here."""
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
    """Redefine an existing table's policies in place: Postgres has no ``CREATE OR REPLACE
    POLICY``, so a changed coverage shape can't simply re-``create_table_rls``. ``ENABLE``/
    ``FORCE`` are left alone rather than cycled -- only the policies are ever unprotected."""
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
    """Reverse of :func:`create_table_rls`, in teardown order: ``NO FORCE`` before
    ``DISABLE`` so the table is never forced-but-disabled; policies dropped last."""
    return [
        no_force_rls(table),
        disable_rls(table),
        *[drop_exempt_policy(table, role) for role in exempt_roles or []],
        drop_tenant_policy(table),
    ]
