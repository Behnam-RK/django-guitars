"""Audit a live database's tenant RLS enforcement against the models.

``makeguitarmigrations --check`` is a *build* gate: it proves the migrations exist. It
cannot prove they ran, that nobody dropped a policy by hand, or that enforcement actually
binds -- so this command asks the database directly, and is the gate to run after a deploy.

Four findings it exists to catch, in descending order of danger:

* **ENABLE without FORCE.** The app role owns its tables (it runs migrations), and an owner
  bypasses non-``FORCE`` RLS *silently* -- no error, no log, rows simply come back
  unfiltered. A table in this state looks protected in ``pg_policies`` and constrains
  nothing. Since guitars emits ``FORCE`` by default this should never appear; it is a
  release blocker where it does, hence ``--require-force``. It is opt-in only because a
  project mid-way through a staged retrofit is legitimately in this state.
* **Missing policy or missing ENABLE** -- a migration that never ran, or drift.
* **A policy that no longer says what the models say.** A table can carry a perfectly
  healthy ``tenant_scope`` policy that scopes on the *wrong* dimensions -- a model gained a
  tenant dimension, or its tenant column was renamed, and the replacement migration was
  generated but never applied (or was applied and then hand-edited). Existence checks pass
  and the table looks protected, while every statement is filtered by a strictly weaker
  predicate than the Python layer believes. Compared by the two facts a stored policy
  preserves: the ``tenant.*`` settings it reads, and the columns ``pg_depend`` records it
  referencing. See ``TableCoverage.policy_gucs`` / ``policy_columns``.

  Reported but not fatal unless ``--require-match``, for the same reason ``--require-force``
  is opt-in: a run that happens before the deploy's ``migrate`` step is legitimately in this
  state, and only the operator knows the ordering.
* **Unexpected coverage** -- a ``tenant_scope`` policy on a table the models no longer
  consider tenanted. Harmless to reads, but it means the database and the models disagree,
  and the next person to trust this audit deserves to know.

Exits non-zero on any finding at or above the requested severity, so it drops straight into
a deploy step.
"""

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
from guitars.tenancy.discovery import expected_coverage


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
    #: ``tenant.*`` settings the live policy predicate reads, and the columns it references.
    #: Both empty when there is no policy. Compared against what the models imply, which is
    #: the only way to catch a policy that exists but enforces the wrong scope.
    policy_gucs: frozenset[str]
    policy_columns: frozenset[tuple[str, str]]


#: Live enforcement state for every regular table in the search path. ``relrowsecurity`` is
#: ENABLE; ``relforcerowsecurity`` is FORCE -- the owner-bypass switch.
#:
#: Ordered by search-path position **descending** so that, as rows are folded into a dict
#: keyed on the bare table name, the earliest schema in the path is written last and wins.
#: That is the table an unqualified ``db_table`` actually resolves to. Without the ordering,
#: two same-named tables in two schemas on the path (an ordinary shape for a
#: schema-per-tenant deployment) collide and whichever the catalog happened to return last
#: is reported -- so the audit could pass on a table Django never writes to.
_STATE_SQL = """
SELECT
    c.relname,
    n.nspname,
    c.relrowsecurity,
    c.relforcerowsecurity,
    p.oid,
    pg_get_expr(p.polqual, p.polrelid)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_policy p ON p.polrelid = c.oid AND p.polname = %s
WHERE c.relkind = 'r' AND n.nspname = ANY(current_schemas(false))
ORDER BY array_position(current_schemas(false), n.nspname) DESC
"""

#: Columns each policy references, from the dependencies PostgreSQL records when the policy
#: is created. Authoritative where a regex over ``pg_get_expr`` would be guesswork: the
#: stored expression parenthesises and casts columns (``(label_id)::text``), and a renamed
#: column is exactly what we are trying to notice. DISTINCT because USING and WITH CHECK are
#: the same expression here and each records its own dependency row.
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

#: ``current_setting('tenant.x', true)`` keeps its literal argument verbatim through
#: PostgreSQL's rewrite of a stored policy expression, so this one extraction is reliable
#: where matching the whole predicate as text is impossible.
_RE_GUC = re.compile(r"current_setting\('([^']+)'")


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
                # Guarded: ``= ANY('{}')`` is valid but a needless round trip on a database
                # with no policies at all, which is the ordinary state before the first
                # migrate.
                cursor.execute(_POLICY_COLUMNS_SQL, [policy_oids])
                for oid, table, column in cursor.fetchall():
                    columns.setdefault(oid, set()).add((table, column))

            state: dict[str, TableState] = {}
            for name, schema, enabled, forced, oid, qual in rows:
                state[name] = TableState(
                    schema=schema,
                    has_policy=oid is not None,
                    rls_enabled=enabled,
                    rls_forced=forced,
                    # Restricted to the kit's own namespace, so a hand-tuned policy also
                    # reading, say, ``statement_timeout`` is not reported as drift for it.
                    policy_gucs=frozenset(
                        guc for guc in _RE_GUC.findall(qual or '') if guc.startswith(GUC_PREFIX)
                    ),
                    policy_columns=frozenset(columns.get(oid, ())),
                )
            return state

    @staticmethod
    def _predicate_drift(table: str, coverage: TableCoverage, state: TableState) -> str | None:
        """Describe how the live policy differs from the one the models describe, if it does.

        Compared as two sets rather than as SQL text because PostgreSQL rewrites a policy
        expression when it stores it -- casts made explicit, columns parenthesised,
        ``current_setting(...) AS current_setting`` -- so the text it hands back never equals
        the text ``sql.policy`` emitted, however correct the policy is.
        """
        expected_gucs = coverage.policy_gucs()
        expected_columns = coverage.policy_columns(table)
        differences = []
        if state.policy_gucs != expected_gucs:
            differences.append(
                f'scopes on {sorted(state.policy_gucs)} where the models imply '
                f'{sorted(expected_gucs)}'
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
        """Warn when ``GUITARS_TENANT_ENFORCE = 'audit'`` on a database whose policies bind.

        This command is the only place that can see both halves at once: it knows the setting
        and it has just asked the database whether the policies are enforced. The combination
        is worth naming because it does not do what it looks like -- ``'audit'`` softens the
        *Python* guard so a bad write is reported and proceeds, and there is no session
        variable that makes a policy lenient. So the write is reported and then rejected
        anyway, and a team that set ``'audit'`` specifically to avoid 500s during a rollout
        gets them from one layer lower.

        A warning rather than an error: it is a legitimate end state (strict is what you want
        eventually, and the reverse order is merely awkward), and only the operator knows
        which stage of a rollout they are in.
        """
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

        # A typo'd label would otherwise match no app, audit zero tables and report
        # "passed" -- a green deploy gate that verified nothing, which is precisely the
        # outcome this command exists to prevent. Same validation, and the same error, as
        # `makeguitarmigrations` and Django's own `makemigrations`.
        _generator.validate_app_labels(requested)

        expected = expected_coverage(requested)
        live = self._live_state(connection)

        missing: list[str] = []
        drifted: list[str] = []
        unforced: list[str] = []
        # Tables with any finding, so the summary's "enforced" count is the number of tables
        # that are actually clean. A table can be both drifted and unforced, so counting by
        # subtracting each list would double-subtract it.
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
            if not state.rls_forced:
                unforced.append(table)
                unhealthy.add(table)

        # The other direction: the database enforces something the models stopped expecting.
        # A scoped run cannot tell "not mine" from "gone", so only a full-repo audit may
        # claim a policy is unexpected.
        unexpected = (
            sorted(
                table
                for table, state in live.items()
                if state.has_policy and table not in expected.tables
            )
            if not requested
            else []
        )

        for note in expected.notes:
            self.stdout.write(self.style.WARNING(note))

        for note in self._enforcement_mode_notes(live, expected.tables):
            self.stdout.write(self.style.WARNING(note))

        # "enforced" counts only the clean tables. Counting every table that merely has a
        # policy would include the ones this command exists to flag: without FORCE the owning
        # role bypasses the policy entirely, and with a stale predicate it scopes on the wrong
        # thing -- neither is enforcement, and reporting them as such is how an audit gets
        # trusted for something it did not check.
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
            raise CommandError(
                f'Tenant RLS audit failed: {failures} table(s) not enforcing what the '
                f'models describe.'
            )

        self.stdout.write(self.style.SUCCESS('Tenant RLS audit passed.'))
