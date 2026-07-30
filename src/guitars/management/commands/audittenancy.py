"""Audit a live database's tenant RLS enforcement against the models.

``makeguitarmigrations --check`` is a *build* gate: it proves the migrations exist. It
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

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections

from guitars.management import _generator
from guitars.sql.policy import TENANT_POLICY
from guitars.tenancy import TenantEnforcement
from guitars.tenancy.discovery import expected_coverage


if TYPE_CHECKING:
    from collections.abc import Mapping

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

    @staticmethod
    def _live_state(connection: BaseDatabaseWrapper) -> dict[str, TableState]:
        with connection.cursor() as cursor:
            cursor.execute(_STATE_SQL, [TENANT_POLICY])
            return {
                name: TableState(has_policy=has_policy, rls_enabled=enabled, rls_forced=forced)
                for name, enabled, forced, has_policy in cursor.fetchall()
            }

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
        requested = set(app_labels)

        # A typo'd label would otherwise match no app, audit zero tables and report
        # "passed" -- a green deploy gate that verified nothing, which is precisely the
        # outcome this command exists to prevent. Same validation, and the same error, as
        # `makeguitarmigrations` and Django's own `makemigrations`.
        _generator.validate_app_labels(requested)

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

        for note in self._enforcement_mode_notes(live, expected.tables):
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
