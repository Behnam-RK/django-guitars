"""Audit a live database's tenant RLS enforcement against the models -- the gate to run
after a deploy, since ``makeguitarmigrations --check`` only proves migrations exist, not
that they ran or still bind. See ``docs/tenancy.md``'s "Auditing" for the five findings."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections

from guitars.gucs import GUC_PREFIX
from guitars.management import _generator
from guitars.sql.policy import TENANT_POLICY
from guitars.tenancy import TenantEnforcement
from guitars.tenancy.discovery import autofill_function_name, expected_coverage


if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.db.backends.base.base import BaseDatabaseWrapper

    from guitars.tenancy.discovery import TableCoverage


class TableState(NamedTuple):
    """What the database says about one table."""

    schema: str
    has_policy: bool
    rls_enabled: bool
    rls_forced: bool
    #: ``tenant.*`` settings and columns the live USING predicate references. Both empty
    #: when there's no policy.
    policy_gucs: frozenset[str]
    policy_columns: frozenset[tuple[str, str]]
    #: Same, off the WITH CHECK half (governs writes) -- see docs/tenancy.md's "USING
    #: (<tenant match>) WITH CHECK (true)" hazard. Falls back to USING when NULL.
    policy_check_gucs: frozenset[str]
    #: Names of the functions this table's row-level INSERT triggers call (ADR 0005). A
    #: table that should autofill but calls none is the "looks fine, is not" state.
    autofill_functions: frozenset[str]


#: Live enforcement state for every table in the search path. Ordered search-path-descending
#: so the earliest schema wins when folded into a dict by bare name -- else a
#: schema-per-tenant collision could report on a table Django never writes to.
_STATE_SQL = """
SELECT
    c.relname,
    n.nspname,
    c.relrowsecurity,
    c.relforcerowsecurity,
    p.oid,
    pg_get_expr(p.polqual, p.polrelid),
    pg_get_expr(p.polwithcheck, p.polrelid)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_policy p ON p.polrelid = c.oid AND p.polname = %s
WHERE c.relkind = 'r' AND n.nspname = ANY(current_schemas(false))
ORDER BY array_position(current_schemas(false), n.nspname) DESC
"""

#: Which functions each table's own row triggers call (ADR 0005). ``tgisinternal`` excludes
#: the ones Postgres creates for foreign keys and constraints.
_AUTOFILL_TRIGGER_SQL = """
SELECT c.relname, p.proname
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_proc p ON p.oid = t.tgfoid
WHERE NOT t.tgisinternal
  AND c.relkind = 'r'
  AND n.nspname = ANY(current_schemas(false))
"""

#: Columns each policy references, from pg_depend rather than a regex over pg_get_expr --
#: the stored expression parenthesises/casts columns, and a renamed column is exactly what
#: this needs to notice. DISTINCT: USING and WITH CHECK each record their own dependency row.
_POLICY_COLUMNS_SQL = """
SELECT DISTINCT d.objid, c.relname, a.attname
FROM pg_depend d
JOIN pg_class c ON c.oid = d.refobjid
JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
WHERE d.classid = 'pg_policy'::regclass
  AND d.objid = ANY(%s)
  AND d.refclassid = 'pg_class'::regclass
  AND d.refobjsubid > 0
"""

#: The two role attributes that bypass RLS *unconditionally* -- policy, ENABLE and FORCE
#: alike. ``current_user`` is always a row in ``pg_roles``, so exactly one row returns.
_ROLE_SQL = """
SELECT current_user, r.rolsuper, r.rolbypassrls
FROM pg_roles r
WHERE r.rolname = current_user
"""

#: ``current_setting('tenant.x', true)`` keeps its literal argument verbatim through
#: PostgreSQL's rewrite of a stored policy expression, so this one extraction is reliable
#: where matching the whole predicate as text is impossible.
_RE_GUC = re.compile(r"current_setting\('([^']+)'")


def _tenant_gucs(expression: str | None) -> frozenset[str]:
    """The kit's own session settings a stored policy expression reads -- restricted to
    :data:`~guitars.gucs.GUC_PREFIX`, so a policy also consulting ``statement_timeout``
    isn't reported as drift for it."""
    return frozenset(
        guc for guc in _RE_GUC.findall(expression or '') if guc.startswith(GUC_PREFIX)
    )


class Command(BaseCommand):
    """Compares live RLS state against what the models expect."""

    help = 'Audits tenant row-level security on a live database (see docs/tenancy.md).'

    def add_arguments(self, parser):
        parser.add_argument(
            'args',
            metavar='app_label',
            nargs='*',
            help='Optional app labels to scope the audit to (default: all LOCAL_APPS).',
        )
        parser.add_argument(
            '--database',
            default=DEFAULT_DB_ALIAS,
            help='Database alias to audit (default: "default").',
        )
        parser.add_argument(
            '--require-force',
            action='store_true',
            dest='require_force',
            help=(
                'Treat a table without FORCE ROW LEVEL SECURITY as a failure. Off by '
                'default so a staged retrofit (GUITARS_RLS_FORCE = False) can still audit '
                'its policy coverage before FORCE lands.'
            ),
        )
        parser.add_argument(
            '--require-match',
            action='store_true',
            dest='require_match',
            help=(
                'Treat a policy whose predicate does not match the models as a failure. Off '
                'by default: a deploy that applies migrations after this runs is legitimately '
                'in this state for a moment, and only the operator knows the ordering. Turn '
                'it on in the step that runs after migrate.'
            ),
        )

    @staticmethod
    def _live_state(connection: BaseDatabaseWrapper) -> dict[str, TableState]:
        with connection.cursor() as cursor:
            cursor.execute(_STATE_SQL, [TENANT_POLICY])
            rows = cursor.fetchall()

            policy_oids = [row[4] for row in rows if row[4] is not None]
            columns: dict[int, set[tuple[str, str]]] = {}
            if policy_oids:
                # Guarded: ``= ANY('{}')`` is valid but a needless round trip before the
                # first migrate, when there are no policies at all.
                cursor.execute(_POLICY_COLUMNS_SQL, [policy_oids])
                for oid, table, column in cursor.fetchall():
                    columns.setdefault(oid, set()).add((table, column))

            cursor.execute(_AUTOFILL_TRIGGER_SQL)
            triggers: dict[str, set[str]] = {}
            for table, function in cursor.fetchall():
                triggers.setdefault(table, set()).add(function)

            state: dict[str, TableState] = {}
            for name, schema, enabled, forced, oid, qual, with_check in rows:
                state[name] = TableState(
                    schema=schema,
                    has_policy=oid is not None,
                    rls_enabled=enabled,
                    rls_forced=forced,
                    policy_gucs=_tenant_gucs(qual),
                    policy_columns=frozenset(columns.get(oid, ())),
                    # NULL polwithcheck means "use USING for writes too" (Postgres's own
                    # rule for a FOR ALL policy with no explicit WITH CHECK).
                    policy_check_gucs=_tenant_gucs(qual if with_check is None else with_check),
                    autofill_functions=frozenset(triggers.get(name, ())),
                )
            return state

    @staticmethod
    def _bypassing_role_notes(connection: BaseDatabaseWrapper) -> list[str]:
        """Warn when the connecting role bypasses RLS whatever the catalog says -- every
        other check here is bypass-blind, so without this the success message misleads.
        A warning, not a failure: it describes who's connecting, not the database."""
        with connection.cursor() as cursor:
            cursor.execute(_ROLE_SQL)
            role, superuser, bypassrls = cursor.fetchone()

        held = [name for name, has in (('SUPERUSER', superuser), ('BYPASSRLS', bypassrls)) if has]
        if not held:
            return []
        return [
            f"Audited as '{role}', which holds {' and '.join(held)} -- that role bypasses "
            f'every {TENANT_POLICY} policy unconditionally, FORCE included. The findings '
            f'below describe the catalog correctly and prove nothing about whether '
            f'enforcement binds for this connection. Re-run as the application role.'
        ]

    @staticmethod
    def _autofill_drift(table: str, coverage: TableCoverage, state: TableState) -> str | None:
        """Report a table whose manager autofills but whose ADR 0005 trigger is absent. No
        other check sees it: the policy is present and correct, reads and cross-tenant writes
        are refused, and only an insert omitting the tenant fails -- with a bare NOT NULL."""
        expected_functions = {
            autofill_function_name(dimension, column)
            for dimension, column in (coverage.autofill_columns or {}).items()
        }
        if missing := sorted(expected_functions - state.autofill_functions):
            return (
                f"'{table}': no tenant autofill trigger calling {missing} -- its manager "
                f'autofills, so an INSERT omitting the tenant will fail on NOT NULL instead '
                f'of taking the active scope. Run `manage.py makeguitarmigrations` + migrate.'
            )
        return None

    @staticmethod
    def _predicate_drift(table: str, coverage: TableCoverage, state: TableState) -> str | None:
        """Describe how the live policy differs from the models', if it does -- compared as
        sets, not SQL text (Postgres rewrites stored expressions). USING/WITH CHECK checked
        separately for the "scoped reads, open writes" hazard docs/tenancy.md describes."""
        expected_gucs = coverage.policy_gucs()
        expected_columns = coverage.policy_columns(table)
        differences = []
        if state.policy_gucs != expected_gucs:
            differences.append(
                f'scopes reads on {sorted(state.policy_gucs)} where the models imply '
                f'{sorted(expected_gucs)}'
            )
        if state.policy_check_gucs != expected_gucs:
            differences.append(
                f'scopes writes on {sorted(state.policy_check_gucs)} where the models imply '
                f'{sorted(expected_gucs)} -- its WITH CHECK half does not constrain the '
                f'tenant, so a cross-tenant write is accepted'
            )
        if state.policy_columns != expected_columns:
            differences.append(
                f'references columns {sorted(state.policy_columns)} where the models imply '
                f'{sorted(expected_columns)}'
            )
        if not differences:
            return None
        return (
            f"'{table}': the live {TENANT_POLICY} policy {' and '.join(differences)} -- the "
            f'table is protected, but not by the scope the models describe. Run '
            f'`makemigrations` to generate the replacement, then `migrate`.'
        )

    @staticmethod
    def _enforcement_mode_notes(
        live: Mapping[str, TableState], expected: Mapping[str, object]
    ) -> list[str]:
        """Warn when ``GUITARS_TENANT_ENFORCE = 'audit'`` on a database whose policies
        already bind -- ``'audit'`` only softens the *Python* guard, so a team using it to
        avoid 500s during rollout gets them from the database layer instead."""
        mode = str(getattr(settings, 'GUITARS_TENANT_ENFORCE', TenantEnforcement.STRICT))
        if mode != TenantEnforcement.AUDIT.value:
            return []
        binding = sorted(
            table
            for table in expected
            if (state := live.get(table)) and state.has_policy and state.rls_enabled
        )
        if not binding:
            return []
        return [
            f"GUITARS_TENANT_ENFORCE is 'audit', but {len(binding)} table(s) already enforce a "
            f'{TENANT_POLICY} policy. Audit mode only softens the Python write guard -- the '
            f'policy still rejects a cross-tenant write, so those writes are reported *and* '
            f'refused. Audit mode belongs before the policies land (GUITARS_TENANT_POLICIES = '
            f"False); once they bind, 'strict' is the honest setting."
        ]

    def handle(self, *app_labels, **options):
        connection = connections[options['database']]
        require_force = options['require_force']
        require_match = options['require_match']
        requested = set(app_labels)

        # A typo'd label would otherwise match no app and report "passed" -- a green gate
        # that verified nothing. Same validation as `makeguitarmigrations`.
        _generator.validate_app_labels(requested)

        expected = expected_coverage(requested)
        live = self._live_state(connection)

        missing: list[str] = []
        drifted: list[str] = []
        unforced: list[str] = []
        # Tables with any finding, so "enforced" below counts only the actually-clean ones.
        # A table can be both drifted and unforced, so a set avoids double-subtracting it.
        unhealthy: set[str] = set()
        for table in sorted(expected.tables):
            state = live.get(table)
            if state is None:
                missing.append(f"'{table}': table not found in the database.")
                unhealthy.add(table)
                continue
            gaps = []
            if not state.has_policy:
                gaps.append(f'no {TENANT_POLICY} policy')
            if not state.rls_enabled:
                gaps.append('RLS not enabled')
            if gaps:
                missing.append(f"'{table}': {', '.join(gaps)}.")
                unhealthy.add(table)
                continue
            if drift := self._predicate_drift(table, expected.tables[table], state):
                drifted.append(drift)
                unhealthy.add(table)
            if autofill_drift := self._autofill_drift(table, expected.tables[table], state):
                drifted.append(autofill_drift)
                unhealthy.add(table)
            if not state.rls_forced:
                unforced.append(table)
                unhealthy.add(table)

        # The other direction: a scoped run can't tell "not mine" from "gone", so only a
        # full-repo audit may claim a policy is unexpected.
        unexpected = (
            sorted(
                table
                for table, state in live.items()
                if state.has_policy and table not in expected.tables
            )
            if not requested
            else []
        )

        # First, and before the heading: it qualifies every line that follows.
        for note in self._bypassing_role_notes(connection):
            self.stdout.write(self.style.WARNING(note))

        for note in expected.notes:
            self.stdout.write(self.style.WARNING(note))

        for note in self._enforcement_mode_notes(live, expected.tables):
            self.stdout.write(self.style.WARNING(note))

        # "enforced" counts only clean tables -- one merely having a policy isn't enforcement
        # if FORCE is missing or the predicate is stale.
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f'Tenant RLS audit on {connection.alias}: '
                f'{len(expected.tables)} table(s) expected, '
                f'{len(expected.tables) - len(unhealthy)} enforced, '
                f'{len(unforced)} enabled without FORCE, '
                f'{len(drifted)} not matching the models.'
            )
        )

        for line in missing:
            self.stderr.write(self.style.ERROR(line))
        for line in drifted:
            if require_match:
                self.stderr.write(self.style.ERROR(line))
            else:
                self.stdout.write(self.style.WARNING(line))
        for table in unexpected:
            # Schema-qualified: with more than one schema on the search path, "which
            # table?" is a real question, and the bare name does not answer it.
            self.stdout.write(
                self.style.WARNING(
                    f"'{live[table].schema}.{table}': has a {TENANT_POLICY} policy but the "
                    f'models no longer expect one -- database and models disagree.'
                )
            )
        for table in unforced:
            message = (
                f"'{table}': RLS enabled without FORCE -- the owning app role bypasses it "
                f'silently, so the policy does not constrain this service.'
            )
            if require_force:
                self.stderr.write(self.style.ERROR(message))
            else:
                self.stdout.write(self.style.WARNING(message))

        failures = (
            len(missing)
            + (len(drifted) if require_match else 0)
            + (len(unforced) if require_force else 0)
        )
        if failures:
            # Findings, not tables: a table both drifted and unforced would overstate the
            # blast radius counted as two. The heading above reports table counts instead.
            raise CommandError(
                f'Tenant RLS audit failed: {failures} finding(s) at or above the requested '
                f'severity -- the database is not enforcing what the models describe.'
            )

        self.stdout.write(self.style.SUCCESS('Tenant RLS audit passed.'))
