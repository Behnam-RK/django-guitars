"""Tests for the ``audittenancy`` management command -- the gate that runs after a deploy,
asking the database directly. Every test here breaks something real and watches the
command notice; the happy path alone would only prove the command runs, not that it audits."""

from __future__ import annotations

import types

import pglast
import pytest
from django.core.management import CommandError, call_command
from pglast import ast, visitors
from pglast.enums import AlterTableType, SubLinkType

from guitars import sql
from guitars.management.commands.audittenancy import Command as AuditCommand
from guitars.sql import triggers
from guitars.tenancy import tenant
from tests.conftest import execute
from guitars.tenancy.discovery import autofill_function_name, autofill_trigger_name
from tests.testapp.models import Booking, Release


def _audit(*args, **options) -> str:
    """Run the command, returning its stdout. Raises ``CommandError`` on a finding."""
    from io import StringIO

    out, err = StringIO(), StringIO()
    call_command('audittenancy', *args, stdout=out, stderr=err, **options)
    return out.getvalue() + err.getvalue()


def _audit_failure(*args, **options) -> str:
    """Run the command expecting a failure, returning stdout+stderr plus the error text."""
    from io import StringIO

    out, err = StringIO(), StringIO()
    with pytest.raises(CommandError) as caught:
        call_command('audittenancy', *args, stdout=out, stderr=err, **options)
    return out.getvalue() + err.getvalue() + str(caught.value)


class TestAGoodDatabasePasses:
    def test_a_migrated_database_is_clean(self, db):
        output = _audit()

        assert 'audit passed' in output
        # The count is part of the assertion: "passed" over zero tables is the failure mode
        # a deploy gate has to be incapable of.
        assert '12 table(s) expected, 12 enforced' in output

    def test_force_is_already_on_everywhere(self, db):
        """``GUITARS_RLS_FORCE`` defaults to True, so the strict gate passes out of the box
        -- the whole reason: shipping policies the owning role bypasses is inert."""
        assert 'audit passed' in _audit(require_force=True)

    def test_it_reports_the_uncoverable_table(self, db):
        """The multi-hop model. A named gap, on every run, not a one-off build-time note."""
        assert 'testapp_review' in _audit()

    def test_the_suite_role_is_not_a_bypassing_one(self, db):
        """Load-bearing, not hygiene: SUPERUSER/BYPASSRLS would make every RLS assertion
        pass vacuously. ``scripts/postgres-init.sql`` prevents it; this notices if it stops."""
        assert 'bypasses every tenant_scope policy' not in _audit()


class TestABypassingRoleIsReported:
    """The two conditions the catalog can't show, attributes of the *connection*. Stubbed,
    not real: granting SUPERUSER/BYPASSRLS mid-run would disarm every RLS assertion."""

    @staticmethod
    def _notes(role: str, superuser: bool, bypassrls: bool) -> list[str]:
        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, *_):
                return None

            def fetchone(self):
                return (role, superuser, bypassrls)

        return AuditCommand._bypassing_role_notes(types.SimpleNamespace(cursor=_Cursor))

    def test_an_ordinary_role_says_nothing(self):
        assert self._notes('app', False, False) == []

    @pytest.mark.parametrize(
        ('superuser', 'bypassrls', 'expected'),
        [
            (True, False, 'SUPERUSER'),
            (False, True, 'BYPASSRLS'),
            (True, True, 'SUPERUSER and BYPASSRLS'),
        ],
    )
    def test_it_names_the_attribute_that_bypasses(self, superuser, bypassrls, expected):
        (note,) = self._notes('admin', superuser, bypassrls)

        assert f"Audited as 'admin', which holds {expected} --" in note
        assert 'prove nothing about whether enforcement binds' in note

    def test_it_is_printed_ahead_of_the_heading_and_does_not_fail_the_run(self, db, monkeypatch):
        """Qualifies every line that follows, so it comes first -- a role the pipeline
        chose is not a reason to fail a correctly-configured database."""
        monkeypatch.setattr(
            AuditCommand, '_bypassing_role_notes', lambda self, connection: ['BYPASS WARNING']
        )

        output = _audit(require_force=True, require_match=True)

        assert 'audit passed' in output
        assert output.index('BYPASS WARNING') < output.index('table(s) expected')


class TestItCatchesRealDamage:
    def test_a_dropped_policy_fails_the_audit(self, _execute):
        _execute(sql.drop_tenant_policy(table=Release._meta.db_table))

        output = _audit_failure()

        assert 'testapp_release' in output
        assert 'no tenant_scope policy' in output
        assert 'audit failed' in output

    def test_disabled_rls_fails_the_audit(self, _execute):
        """A policy that exists but isn't enabled enforces nothing, and looks fine in
        ``pg_policies``."""
        _execute(sql.disable_rls(table=Release._meta.db_table))

        output = _audit_failure()

        assert 'RLS not enabled' in output

    def test_a_missing_force_is_a_warning_by_default(self, _execute):
        """Off by default so a project mid-retrofit can still audit coverage -- a warning,
        not silence, since the table *looks* protected while the owning role sails past."""
        _execute(sql.no_force_rls(table=Release._meta.db_table))

        output = _audit()

        assert 'audit passed' in output
        assert 'without FORCE' in output
        assert 'bypasses it silently' in output

    def test_a_missing_force_fails_under_require_force(self, _execute):
        _execute(sql.no_force_rls(table=Release._meta.db_table))

        output = _audit_failure(require_force=True)

        assert 'testapp_release' in output
        assert 'audit failed' in output

    def test_force_without_a_policy_is_still_caught(self, _execute):
        """FORCE on its own is not protection -- and RLS enabled with *no* policy is
        default-deny, so this state is loud in production but silent in an audit that only
        looked at FORCE."""
        _execute(sql.drop_tenant_policy(table=Release._meta.db_table))

        assert 'no tenant_scope policy' in _audit_failure(require_force=True)

    def test_a_policy_the_models_no_longer_expect_is_reported(self, _execute):
        """The other direction. Harmless to reads, but the database and the models disagree,
        and the next person to trust a green audit deserves to know."""
        _execute(*sql.create_table_rls(table='testapp_band', columns={'label': 'id'}))
        try:
            output = _audit()

            assert 'testapp_band' in output
            assert 'no longer expect one' in output
        finally:
            _execute(*sql.drop_table_rls(table='testapp_band'))


#: What ``makeguitarmigrations`` named ``Release``'s autofill trigger -- derived rather than
#: spelled out, so renaming the scheme moves these tests with it instead of past them.
_RELEASE_AUTOFILL_FUNCTION = autofill_function_name('label', 'label_id')
_RELEASE_AUTOFILL_TRIGGER = f'"{autofill_trigger_name(_RELEASE_AUTOFILL_FUNCTION)}"'


class TestAMissingAutofillTrigger:
    """The state no other check sees (ADR 0005): policy present and correct, reads and
    cross-tenant writes refused, and only an INSERT omitting the tenant fails -- with a bare
    NOT NULL instead of taking the active scope. Exactly "looks fine, is not"."""

    def test_a_missing_trigger_is_a_warning_by_default(self, _execute):
        """Warned, not fatal, for the same reason drift is: a run before the deploy's own
        ``migrate`` legitimately has the migration written but not yet applied."""
        table = Release._meta.db_table
        _execute(f'DROP TRIGGER {_RELEASE_AUTOFILL_TRIGGER} ON {table}')

        output = _audit()

        assert 'no tenant autofill trigger' in output
        assert table in output

    def test_it_names_the_function_and_the_fix(self, _execute):
        """A finding that does not say what to run makes the operator go and read the ADR."""
        _execute(f'DROP TRIGGER {_RELEASE_AUTOFILL_TRIGGER} ON {Release._meta.db_table}')

        output = _audit()

        assert _RELEASE_AUTOFILL_FUNCTION in output
        assert 'makeguitarmigrations' in output

    def test_it_is_fatal_under_require_match(self, _execute):
        _execute(f'DROP TRIGGER {_RELEASE_AUTOFILL_TRIGGER} ON {Release._meta.db_table}')

        output = _audit_failure(require_match=True)

        assert 'no tenant autofill trigger' in output
        assert 'not enforcing what the models describe' in output

    def test_a_disabled_trigger_is_reported_as_missing(self, _execute):
        """``ALTER TABLE ... DISABLE TRIGGER`` leaves the row in ``pg_trigger`` while nothing
        fires -- the sharpest form of "looks fine, is not", and exactly what a bulk load left
        under ``session_replication_role = 'replica'`` leaves behind."""
        table = Release._meta.db_table
        _execute(f'ALTER TABLE {table} DISABLE TRIGGER {_RELEASE_AUTOFILL_TRIGGER}')

        output = _audit()

        assert 'no tenant autofill trigger' in output
        assert table in output

    def test_a_table_that_does_not_autofill_is_not_expected_to_have_one(self, db):
        """``Booking``'s hand-declared manager passes no ``autofill=``, so it falls back to
        ``GUITARS_TENANT_AUTOFILL`` (False here) and correctly carries no trigger. Reporting
        that as a finding would make the opt-out unusable."""
        output = _audit()

        assert 'no tenant autofill trigger' not in output
        assert Booking._meta.db_table not in output.split('Tenant RLS audit on')[0]


class TestAStrayAutofillTrigger:
    """The other direction, and the live half of issue #27: a rename mints a new function
    and leaves the old trigger dereferencing a dropped column, so every INSERT on the table
    fails. Presence-only checks call that healthy, which is what made it worth catching."""

    def _stray(self, _execute, table: str) -> str:
        """Create a guitars-shaped trigger the models do not expect, on *table*."""
        function = 'guitars_fill_9_gone_gone_id'
        _execute(
            triggers._CREATE_TENANT_AUTOFILL_FUNCTION.format(
                function=f'"{function}"',
                column='label_id',
                bypass_guc='tenant.bypass',
                guc='tenant.gone',
                separator=',',
            ),
            triggers._CREATE_TENANT_AUTOFILL_TRIGGER.format(
                trigger=f'"{autofill_trigger_name(function)}"',
                table=f'"{table}"',
                function=f'"{function}"',
            ),
        )
        return function

    def test_a_trigger_the_models_no_longer_expect_is_reported(self, _execute):
        table = Release._meta.db_table
        function = self._stray(_execute, table)
        try:
            output = _audit()

            assert 'no longer expect' in output
            assert function in output
        finally:
            _execute(
                f'DROP TRIGGER "{autofill_trigger_name(function)}" ON "{table}"',
                f'DROP FUNCTION "{function}"()',
            )

    def test_an_applications_own_insert_trigger_is_never_reported(self, _execute):
        """Only this library's own functions are its business. Reporting every BEFORE INSERT
        trigger in the database would make the finding useless on any real project."""
        table = Release._meta.db_table
        _execute(
            """
            CREATE FUNCTION app_own_trigger_fn() RETURNS TRIGGER LANGUAGE PLPGSQL
            AS $$ BEGIN RETURN NEW; END; $$
            """,
            f'CREATE TRIGGER app_own_trigger BEFORE INSERT ON "{table}" '
            f'FOR EACH ROW EXECUTE FUNCTION app_own_trigger_fn()',
        )
        try:
            output = _audit()

            assert 'app_own_trigger_fn' not in output
        finally:
            _execute(
                f'DROP TRIGGER app_own_trigger ON "{table}"',
                'DROP FUNCTION app_own_trigger_fn()',
            )

    def test_a_scoped_audit_stays_quiet(self, _execute):
        """A scoped run cannot tell "not mine" from "gone" -- the same reason the policy
        side gates its own unexpected check on a full-repo run."""
        table = Release._meta.db_table
        function = self._stray(_execute, table)
        try:
            output = _audit('testapp')

            assert 'no longer expect' not in output
        finally:
            _execute(
                f'DROP TRIGGER "{autofill_trigger_name(function)}" ON "{table}"',
                f'DROP FUNCTION "{function}"()',
            )


class TestAPolicyThatNoLongerMatchesTheModels:
    """The finding existence checks can't make: a healthy policy scoping on the wrong
    thing. Compared by the two facts a *stored* policy preserves (Postgres rewrites the
    expression on save): the ``tenant.*`` settings it reads and the ``pg_depend`` columns."""

    def test_drift_is_a_warning_by_default(self, _execute):
        """Reported, but not fatal without ``--require-match`` -- a run before the deploy's
        own ``migrate`` is legitimately in this state, same reason ``--require-force`` is opt-in."""
        table = Release._meta.db_table
        _execute(
            sql.drop_tenant_policy(table=table),
            sql.create_tenant_policy(table=table, columns={'somethingelse': 'label_id'}),
        )
        try:
            output = _audit()

            assert 'audit passed' in output
            assert 'not by the scope the models describe' in output
            # Still not counted as enforced: it is protected by the wrong scope.
            assert '11 enforced' in output
        finally:
            _execute(
                sql.drop_tenant_policy(table=table),
                *sql.create_table_rls(table=table, columns={'label': 'label_id'}),
            )

    def test_a_policy_scoping_on_the_wrong_dimension_is_caught(self, _execute):
        """The model scopes on ``label``; this policy reads ``tenant.somethingelse``."""
        table = Release._meta.db_table
        _execute(
            sql.drop_tenant_policy(table=table),
            sql.create_tenant_policy(table=table, columns={'somethingelse': 'label_id'}),
        )
        try:
            output = _audit_failure(require_match=True)

            assert 'tenant.somethingelse' in output
            assert 'not by the scope the models describe' in output
            assert 'audit failed' in output
        finally:
            _execute(
                sql.drop_tenant_policy(table=table),
                *sql.create_table_rls(table=table, columns={'label': 'label_id'}),
            )

    def test_a_policy_on_the_wrong_column_is_caught(self, _execute):
        """Same dimension name, different column -- so the GUC set matches and only the
        ``pg_depend`` column set gives it away. This is the half a regex over the predicate
        text could not do reliably, since PostgreSQL stores columns as ``(label_id)::text``."""
        table = Release._meta.db_table
        _execute(
            sql.drop_tenant_policy(table=table),
            # `id` is a real column on the table, so the policy is valid SQL -- just wrong.
            sql.create_tenant_policy(table=table, columns={'label': 'id'}),
        )
        try:
            output = _audit_failure(require_match=True)

            assert 'references columns' in output
            assert 'label_id' in output
            assert 'audit failed' in output
        finally:
            _execute(
                sql.drop_tenant_policy(table=table),
                *sql.create_table_rls(table=table, columns={'label': 'label_id'}),
            )

    def test_an_mti_child_policy_is_compared_through_its_owner_join(self, db):
        """The owner-join form references four columns across two tables, and the audit has to
        expect all of them or every MTI child would read as drifted on a healthy database."""
        assert 'audit passed' in _audit()

    def test_drift_and_a_missing_force_are_both_reported(self, _execute):
        """A table can be both, and counting the summary by subtracting each list would
        double-subtract it -- so "enforced" is counted from the set of unhealthy tables."""
        table = Release._meta.db_table
        _execute(
            sql.drop_tenant_policy(table=table),
            sql.create_tenant_policy(table=table, columns={'somethingelse': 'label_id'}),
            sql.no_force_rls(table=table),
        )
        try:
            output = _audit_failure(require_force=True, require_match=True)

            assert 'not by the scope the models describe' in output
            assert 'bypasses it' in output
            # Seven expected tables, one of them unhealthy for two reasons -> six enforced.
            assert '11 enforced' in output
            assert '1 enabled without FORCE' in output
            assert '1 not matching the models' in output
        finally:
            _execute(
                sql.drop_tenant_policy(table=table),
                *sql.create_table_rls(table=table, columns={'label': 'label_id'}),
            )

    def test_a_with_check_that_does_not_scope_is_caught(self, _execute, tenants):
        """The write half, checked in its own right: ``USING (<tenant match>) WITH CHECK
        (true)`` scopes reads while accepting every cross-tenant write -- invisible to the
        other two comparisons. Raw SQL insert, since the Python guard would refuse it first."""
        table = Release._meta.db_table
        predicate = (
            f"(SELECT current_setting('tenant.bypass', true)) = 'on' OR "
            f'({table}.label_id::text = ANY(string_to_array('
            f"(SELECT current_setting('tenant.label', true)), ',')))"
        )
        _execute(
            sql.drop_tenant_policy(table=table),
            f'CREATE POLICY {sql.TENANT_POLICY} ON {table} FOR ALL TO PUBLIC '
            f'USING ({predicate}) WITH CHECK (true)',
        )
        try:
            output = _audit_failure(require_match=True)

            assert 'scopes writes on' in output
            assert 'a cross-tenant write is accepted' in output
            assert 'audit failed' in output
            # Not merely reported: the write really does land, so the finding is not academic.
            with tenant(label=tenants.a):
                execute(
                    f'INSERT INTO {table} (title, label_id, _created_at, _updated_at) '
                    f'VALUES (%s, %s, NOW(), NOW())',
                    params=['smuggled', tenants.b.pk],
                )
        finally:
            _execute(
                # Django declares foreign keys DEFERRABLE INITIALLY DEFERRED, and PostgreSQL
                # refuses ALTER TABLE while a row inserted in this transaction still has
                # pending trigger events. Firing them now is what lets the restore run.
                'SET CONSTRAINTS ALL IMMEDIATE',
                sql.drop_tenant_policy(table=table),
                *sql.create_table_rls(table=table, columns={'label': 'label_id'}),
            )

    def test_a_policy_reading_an_unrelated_setting_is_not_reported_as_drift(self, _execute):
        """Only ``tenant.*`` settings are compared -- a policy also consulting
        ``statement_timeout`` reported here would train operators to ignore the check."""
        table = Release._meta.db_table
        _execute(
            sql.drop_tenant_policy(table=table),
            f'CREATE POLICY {sql.TENANT_POLICY} ON {table} FOR ALL TO PUBLIC USING ('
            f"(SELECT current_setting('tenant.bypass', true)) = 'on' OR ("
            f"current_setting('statement_timeout', true) IS NOT NULL AND "
            f'{table}.label_id::text = ANY(string_to_array('
            f"(SELECT current_setting('tenant.label', true)), ','))))",
        )
        try:
            assert 'audit passed' in _audit()
        finally:
            _execute(
                sql.drop_tenant_policy(table=table),
                *sql.create_table_rls(table=table, columns={'label': 'label_id'}),
            )


class TestGeneratedPolicySQLIsStructurallySound:
    """Parse ``sql.create_tenant_policy``'s own output rather than pattern-match it -- a
    substring assertion can pass on subtly broken SQL (extra paren, misplaced clause)
    since the substring survives while the statement around it stops parsing."""

    @staticmethod
    def _parse(statement: str):
        return pglast.parse_sql(statement)[0].stmt

    @staticmethod
    def _sublink_types(node) -> list:
        found = []

        class _Visitor(visitors.Visitor):
            def visit_SubLink(self, ancestors, sublink):  # noqa: N802 -- pglast dispatches by this name
                found.append(sublink.subLinkType)

        _Visitor()(node)
        return found

    def test_an_own_table_policy_has_both_halves_and_no_owner_join(self):
        stmt = self._parse(
            sql.create_tenant_policy(table='testapp_release', columns={'label': 'label_id'})
        )

        assert isinstance(stmt, ast.CreatePolicyStmt)
        assert stmt.policy_name == sql.TENANT_POLICY
        assert stmt.table.relname == 'testapp_release'
        assert stmt.cmd_name == 'all'
        assert stmt.qual is not None, 'USING half is missing -- every read would be denied'
        assert stmt.with_check is not None, (
            'WITH CHECK half is missing -- see _predicate_drift: this is the half whose '
            'absence lets a cross-tenant write through while reads still look scoped'
        )
        # No correlated owner subquery for a table that owns its own tenant column.
        assert SubLinkType.EXISTS_SUBLINK not in self._sublink_types(stmt.qual)
        assert SubLinkType.EXISTS_SUBLINK not in self._sublink_types(stmt.with_check)

    def test_an_mti_owner_policy_correlates_through_an_exists_subquery(self):
        """The MTI shape's one genuinely intricate piece of SQL, checked by structure --
        asserting the correlated ``EXISTS`` subquery exists, not matching its exact text,
        lets this survive a rewording of the join."""
        stmt = self._parse(
            sql.create_tenant_policy(
                table='testapp_orchestra',
                columns={},
                owner_table='testapp_ensemble',
                owner_pk='id',
                child_pk='ensemble_ptr_id',
                owner_columns={'label': 'label_id'},
            )
        )

        assert isinstance(stmt, ast.CreatePolicyStmt)
        for half in (stmt.qual, stmt.with_check):
            sublink_types = self._sublink_types(half)
            assert SubLinkType.EXISTS_SUBLINK in sublink_types, (
                'no correlated EXISTS subquery -- an MTI child policy without one cannot '
                'reach the tenant column, which lives on the owner table'
            )

    def test_force_rls_statements_are_two_alter_table_commands_in_order(self):
        """``create_table_rls(force=True)`` must enable RLS before forcing it -- reversed
        is still valid SQL and parses, so a substring check wouldn't catch the swap."""
        statements = sql.create_table_rls(
            table='testapp_release', columns={'label': 'label_id'}, force=True
        )
        subtypes = [
            cmd.subtype
            for statement in statements
            for stmt in [self._parse(statement)]
            if isinstance(stmt, ast.AlterTableStmt)
            for cmd in stmt.cmds
        ]

        assert AlterTableType.AT_EnableRowSecurity in subtypes
        assert AlterTableType.AT_ForceRowSecurity in subtypes
        assert subtypes.index(AlterTableType.AT_EnableRowSecurity) < subtypes.index(
            AlterTableType.AT_ForceRowSecurity
        )


class TestScoping:
    def test_a_scoped_run_audits_only_that_app(self, db):
        assert 'audit passed' in _audit('testapp')

    def test_an_unknown_app_label_is_rejected(self, db):
        """Not "0 tables, passed" -- a typo'd label would otherwise audit nothing and exit
        0, a green gate that verified nothing."""
        with pytest.raises(CommandError, match="No installed app with label 'nosuchapp'"):
            call_command('audittenancy', 'nosuchapp')

    def test_a_scoped_run_will_not_claim_a_policy_is_unexpected(self, _execute):
        """It cannot tell "not mine" from "gone", so only a full-repo run may say so."""
        _execute(*sql.create_table_rls(table='testapp_band', columns={'label': 'id'}))
        try:
            assert 'no longer expect one' not in _audit('testapp')
        finally:
            _execute(*sql.drop_table_rls(table='testapp_band'))


class TestOptions:
    def test_it_audits_the_named_database(self, db):
        assert 'audit on default' in _audit(database='default')

    def test_a_table_missing_entirely_is_a_finding(self, _execute):
        """Not a crash. An unmigrated database is the most likely reason to run this."""
        _execute('DROP TABLE testapp_track CASCADE')

        output = _audit_failure()

        assert 'testapp_track' in output
        assert 'not found in the database' in output

    def test_a_database_with_no_policies_at_all_is_reported_table_by_table(self, _execute, db):
        """The state before the very first ``migrate`` -- the column lookup is skipped
        entirely with no policy oids to ask about, so this must not sail through empty."""
        from guitars.tenancy.discovery import expected_coverage

        tables = sorted(expected_coverage().tables)
        _execute(*[sql.drop_tenant_policy(table=table) for table in tables])

        output = _audit_failure()

        assert f'{len(tables)} table(s) expected, 0 enforced' in output
        for table in tables:
            assert f"'{table}': no {sql.TENANT_POLICY} policy" in output
