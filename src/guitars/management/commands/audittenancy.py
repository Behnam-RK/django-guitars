"""Audit a live database's tenant RLS enforcement against the models.

``maketenantmigrations --check`` is a *build* gate: it proves the migrations exist. It
cannot prove they ran, that nobody dropped a policy by hand, or that enforcement actually
binds -- so this command asks the database directly, and is the gate to run after a deploy.

Three findings it exists to catch, in descending order of danger:

* **ENABLE without FORCE.** The app role owns its tables (it runs migrations), and an owner
  bypasses non-``FORCE`` RLS *silently* -- no error, no log, rows simply come back
  unfiltered. A table in this state looks protected in ``pg_policies`` and constrains
  nothing. Since guitars emits ``FORCE`` by default this should never appear; it is a
  release blocker where it does, hence ``--require-force``. It is opt-in only because a
  project mid-way through a staged retrofit is legitimately in this state.
* **Missing policy or missing ENABLE** -- a migration that never ran, or drift.
* **Unexpected coverage** -- a ``tenant_scope`` policy on a table the models no longer
  consider tenanted. Harmless to reads, but it means the database and the models disagree,
  and the next person to trust this audit deserves to know.

Exits non-zero on any finding at or above the requested severity, so it drops straight into
a deploy step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections

from guitars.sql.policy import TENANT_POLICY
from guitars.tenancy.discovery import expected_coverage


if TYPE_CHECKING:
    from django.db.backends.base.base import BaseDatabaseWrapper


class TableState(NamedTuple):
    """What the database says about one table."""

    has_policy: bool
    rls_enabled: bool
    rls_forced: bool


#: Live enforcement state for every regular table in the search path. ``relrowsecurity`` is
#: ENABLE; ``relforcerowsecurity`` is FORCE -- the owner-bypass switch.
_STATE_SQL = """
SELECT
    c.relname,
    c.relrowsecurity,
    c.relforcerowsecurity,
    EXISTS (
        SELECT FROM pg_policy p WHERE p.polrelid = c.oid AND p.polname = %s
    )
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r' AND n.nspname = ANY(current_schemas(false))
"""


class Command(BaseCommand):
    """Compares live RLS state against what the models expect."""

    help = 'Audits tenant row-level security on a live database (see docs/tenancy.md).'

    def add_arguments(self, parser):  # pragma: no cover
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

    @staticmethod
    def _live_state(connection: BaseDatabaseWrapper) -> dict[str, TableState]:
        with connection.cursor() as cursor:
            cursor.execute(_STATE_SQL, [TENANT_POLICY])
            return {
                name: TableState(has_policy=has_policy, rls_enabled=enabled, rls_forced=forced)
                for name, enabled, forced, has_policy in cursor.fetchall()
            }

    def handle(self, *app_labels, **options):
        connection = connections[options['database']]
        require_force = options['require_force']
        requested = set(app_labels)

        expected = expected_coverage(requested)
        live = self._live_state(connection)

        missing: list[str] = []
        unforced: list[str] = []
        for table in sorted(expected.tables):
            state = live.get(table)
            if state is None:
                missing.append(f"'{table}': table not found in the database.")
                continue
            gaps = []
            if not state.has_policy:
                gaps.append(f'no {TENANT_POLICY} policy')
            if not state.rls_enabled:
                gaps.append('RLS not enabled')
            if gaps:
                missing.append(f"'{table}': {', '.join(gaps)}.")
            elif not state.rls_forced:
                unforced.append(table)

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

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f'Tenant RLS audit on {connection.alias}: '
                f'{len(expected.tables)} table(s) expected, '
                f'{len(expected.tables) - len(missing)} enforced, '
                f'{len(unforced)} without FORCE.'
            )
        )

        for line in missing:
            self.stderr.write(self.style.ERROR(line))
        for table in unexpected:
            self.stdout.write(
                self.style.WARNING(
                    f"'{table}': has a {TENANT_POLICY} policy but the models no longer "
                    f'expect one -- database and models disagree.'
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

        failures = len(missing) + (len(unforced) if require_force else 0)
        if failures:
            raise CommandError(f'Tenant RLS audit failed: {failures} table(s) unprotected.')

        self.stdout.write(self.style.SUCCESS('Tenant RLS audit passed.'))
