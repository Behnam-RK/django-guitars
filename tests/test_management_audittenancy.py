"""Tests for the ``audittenancy`` management command.

``makeguitarmigrations --check`` is a *build* gate: it proves the migrations exist. It cannot
prove they ran, that nobody dropped a policy by hand, or that enforcement actually binds.
This command asks the database, so it is the gate that runs after a deploy -- and a deploy
gate that can pass while unprotected is worse than no gate at all.

Every test here therefore has to break something real and watch the command notice. Asserting
only the happy path would prove the command runs, not that it audits.
"""

from __future__ import annotations

import pytest
from django.core.management import CommandError, call_command
from django.db import connection

from guitars import sql
from guitars.tenancy import tenant
from tests.testapp.models import Release


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


@pytest.fixture
def _execute(db):
    def run(*statements: str) -> None:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)

    return run


class TestAGoodDatabasePasses:
    def test_a_migrated_database_is_clean(self, db):
        output = _audit()

        assert 'audit passed' in output
        # The count is part of the assertion: "passed" over zero tables is the failure mode
        # a deploy gate has to be incapable of.
        assert '6 table(s) expected, 6 enforced' in output

    def test_force_is_already_on_everywhere(self, db):
        """``GUITARS_RLS_FORCE`` defaults to True, so the strict gate passes out of the box.

        This is the whole reason the default is True: a library that shipped policies which
        the owning application role bypasses would ship an inert security feature.
        """
        assert 'audit passed' in _audit(require_force=True)

    def test_it_reports_the_uncoverable_table(self, db):
        """The multi-hop model. A named gap, on every run, not a one-off build-time note."""
        assert 'testapp_review' in _audit()


class TestItCatchesRealDamage:
    def test_a_dropped_policy_fails_the_audit(self, _execute):
        _execute(sql.drop_tenant_policy(table=Release._meta.db_table))

        output = _audit_failure()

        assert 'testapp_release' in output
        assert 'no tenant_scope policy' in output
        assert 'audit failed' in output

    def test_disabled_rls_fails_the_audit(self, _execute):
        """A policy that exists but is not enabled enforces nothing, and looks fine in
        ``pg_policies``."""
        _execute(sql.disable_rls(table=Release._meta.db_table))

        output = _audit_failure()

        assert 'RLS not enabled' in output

    def test_a_missing_force_is_a_warning_by_default(self, _execute):
        """Off by default so a project mid-retrofit can still audit its policy coverage.

        It is a warning rather than silence because the table *looks* protected: the policy
        is there, RLS is on, and the owning role sails straight past it.
        """
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


class TestAPolicyThatNoLongerMatchesTheModels:
    """The finding existence checks cannot make: a healthy policy scoping on the wrong thing.

    Every other check here asks "is there a ``tenant_scope`` policy, is RLS on, is it FORCE'd".
    All three pass for a table whose policy predicates on a dimension the model dropped, or on
    a column it renamed -- the table looks protected while every statement is filtered by a
    weaker predicate than the Python layer believes. Nothing else in the kit sees it: the
    generator now emits a replacement, but a replacement that was generated and never applied
    leaves exactly this state.

    Compared by the two facts a *stored* policy preserves, because PostgreSQL rewrites the
    expression when it saves it and the text never matches what was emitted: the ``tenant.*``
    settings it reads, and the columns ``pg_depend`` records it referencing.
    """

    def test_drift_is_a_warning_by_default(self, _execute):
        """Reported, but not fatal without ``--require-match``.

        A run that happens *before* the deploy's own ``migrate`` step is legitimately in this
        state, and only the operator knows their ordering -- the same reason ``--require-force``
        is opt-in. Warned rather than silent, because the table looks protected.
        """
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
            assert '5 enforced' in output
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
            # Six expected tables, one of them unhealthy for two reasons -> five enforced.
            assert '5 enforced' in output
            assert '1 enabled without FORCE' in output
            assert '1 not matching the models' in output
        finally:
            _execute(
                sql.drop_tenant_policy(table=table),
                *sql.create_table_rls(table=table, columns={'label': 'label_id'}),
            )

    def test_a_with_check_that_does_not_scope_is_caught(self, _execute, tenants):
        """The write half, checked in its own right -- and the reason it has to be.

        ``USING`` governs reads and ``WITH CHECK`` governs writes, and the two are
        independently editable. A policy left as ``USING (<tenant match>) WITH CHECK (true)``
        therefore scopes every read while accepting every cross-tenant *write*, which is the
        more dangerous direction. Neither of the other two comparisons sees it: the GUC set of
        the ``USING`` half is exactly right, and ``true`` records no ``pg_depend`` rows, so the
        column set is exactly right too.

        The insert below is raw SQL on purpose. The Python guard would refuse it long before
        the database saw it, which is precisely why an audit of the database layer cannot lean
        on Python having been in the call path.
        """
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
            with tenant(label=tenants.a), connection.cursor() as cursor:
                cursor.execute(
                    f'INSERT INTO {table} (title, label_id, _created_at, _updated_at) '
                    f'VALUES (%s, %s, NOW(), NOW())',
                    ['smuggled', tenants.b.pk],
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
        """Only ``tenant.*`` settings are compared.

        A policy hand-tuned to also consult, say, ``statement_timeout`` is not what this check
        is about, and reporting it would make the check something operators learn to ignore.
        """
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


class TestScoping:
    def test_a_scoped_run_audits_only_that_app(self, db):
        assert 'audit passed' in _audit('testapp')

    def test_an_unknown_app_label_is_rejected(self, db):
        """Not "0 tables, passed".

        A typo'd label in a deploy step would otherwise audit nothing and exit 0 -- a green
        gate that verified nothing, which is the exact outcome this command exists to
        prevent.
        """
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
