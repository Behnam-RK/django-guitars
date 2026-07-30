"""The last uncovered branches: defensive guards, async twins, and vendor checks.

Most of these are reached only by calling an internal directly. That is deliberate and
narrow: each one is a guard whose *absence* would be silent, and a guard nothing exercises is
indistinguishable from a guard that has stopped working. Where a public path can reach the
branch, it is used; where it genuinely cannot -- because an earlier guard fires first -- the
internal is called and the docstring says why.

Async methods get their own tests rather than being assumed equivalent to the sync ones. They
are separate code in ``manager.py``, and a guard added to one and not the other is exactly the
kind of asymmetry that ships.
"""

from __future__ import annotations

import pytest
from django.apps import apps as django_apps
from django.core.management import CommandError
from django.db import connection, connections
from django.test import override_settings

from guitars.management import _generator
from guitars.models import GuitarModel
from guitars.sql import policy
from guitars.tenancy import TenantScopeError, guc, manager, reporting, tenancy_bypassed, tenant
from guitars.tenancy.checks import TENANT_MODEL_ID, check_guitar_models_have_a_tenant
from guitars.tenancy.discovery import _classify
from tests.testapp.models import Booking, Label, Release, Track


# ─────────────────────────── defensive SQL guards ──────────────────────────── #


def test_quote_literal_rejects_a_nul_byte():
    """Unreachable through the public API -- ``create_exempt_policy`` quotes the role as an
    *identifier* first, so that guard fires before this one can. Called directly because a
    guard on a quoting helper is worth keeping honest even when the current callers cannot
    trip it: the next caller might pass a value that never went through ``_quote_ident``.
    """
    with pytest.raises(ValueError, match='string literals cannot contain a NUL byte'):
        policy._quote_literal('bad\x00value')


# ──────────────────────────── the system check ─────────────────────────────── #


class TestTheTenantModelCheckShortCircuits:
    def test_it_is_silent_when_tenancy_is_wired(self):
        """This suite's own configuration. The cheap path, taken on every ``manage.py check``."""
        assert check_guitar_models_have_a_tenant(None) == []

    def test_it_is_silent_for_apps_with_no_guitar_models(self, monkeypatch):
        """Scoped to an app that never used the rung, there is nothing to report -- even with
        the setting missing. Keyed on subclasses, not on the setting alone, so an untenanted
        project is never nagged about a feature it declined."""
        monkeypatch.setattr(GuitarModel, '_guitars_tenancy_installed', False)

        errors = check_guitar_models_have_a_tenant([django_apps.get_app_config('guitars')])

        assert errors == []

    def test_it_reports_when_the_app_does_use_the_rung(self, monkeypatch):
        """The same call, scoped to the app that did -- so the silence above is a decision and
        not a broken check."""
        monkeypatch.setattr(GuitarModel, '_guitars_tenancy_installed', False)

        errors = check_guitar_models_have_a_tenant([django_apps.get_app_config('testapp')])

        assert [error.id for error in errors] == [TENANT_MODEL_ID]


# ───────────────────────── partial policy coverage ─────────────────────────── #


class TestPartiallyCoveredModel:
    """One dimension on this table, one through a relation.

    The policy enforces what it can and the note says what it cannot. Only the *spec* is
    stubbed -- see ``TestDimensionsOnTwoAncestors`` in ``test_tenancy_edges.py`` for why
    declaring a throwaway concrete model is not an option.
    """

    @pytest.fixture
    def mixed_spec(self, monkeypatch):
        spec = {'label': 'label', 'via_release': 'release__label'}
        for module in ('guitars.tenancy.discovery', 'guitars.tenancy.manager'):
            monkeypatch.setattr(f'{module}.tenant_spec', lambda model, _spec=spec: _spec)

    def test_the_covered_dimension_still_gets_a_policy(self, mixed_spec):
        coverage, _ = _classify(Track)

        assert coverage is not None
        assert coverage.columns == {'label': 'label_id'}

    def test_the_note_names_both_halves(self, mixed_spec):
        """ "Skipped" alone would read as "no protection here" on a table that has some."""
        _, notes = _classify(Track)

        note = next(note for note in notes if 'traverse a relation' in note)
        assert 'via_release (release__label)' in note
        assert "enforces ['label']" in note


# ─────────────────────────── manager internals ─────────────────────────────── #


class TestLocalTenantFields:
    def test_a_lookup_naming_no_field_is_not_local(self, monkeypatch):
        """A typo'd lookup is not a crash and not a silent policy either.

        It simply is not a local column, so there is nothing to autofill and nothing to
        predicate on -- and ``FieldDoesNotExist`` is caught by type rather than as a bare
        ``Exception``, so a genuine bug in the surrounding code still surfaces.
        """
        monkeypatch.setattr(
            'guitars.tenancy.manager.tenant_spec', lambda model: {'label': 'no_such_field'}
        )

        assert manager.local_tenant_fields(Release) == {}

    def test_an_untenanted_model_never_autofills(self):
        """``_autofills`` walks the managers looking for a tenanted one and finds none."""
        assert manager._autofills(Label) is False


class TestAWriteWithNoTenantAndNoScope:
    def test_it_names_both_problems(self, db):
        """Neither an explicit tenant nor a scope to take one from.

        Distinct from "missing, but there is a scope" (autofill's case) and from "explicit, but
        no scope" (allowed, the database still checks it) -- so it gets its own message.
        """
        with pytest.raises(TenantScopeError, match='no active tenant scope to take one from'):
            Booking(venue='Nowhere').save()


class TestAsyncTwins:
    """``acreate`` / ``abulk_create`` are separate code, so they get separate proof."""

    @pytest.fixture(autouse=True)
    async def _close_executor_connections(self):
        """Close the connection Django's async ORM opened in its executor thread.

        ``sync_to_async(thread_sensitive=True)`` -- which every ``a*`` ORM method uses -- runs
        the query in a dedicated thread, and ``connections`` is thread-local, so that thread's
        connection is invisible to the main thread's teardown. Left open it holds the test
        database and pytest-django cannot drop it, which surfaces as a teardown warning
        attributed to whichever test happened to run last.

        Closed *through* ``sync_to_async`` so the call lands in the same thread that opened it.
        """
        yield
        from asgiref.sync import sync_to_async

        await sync_to_async(connections.close_all)()

    @pytest.fixture
    def _audit_mode(self):
        reporting.reset_reported()
        with override_settings(GUITARS_TENANT_ENFORCE='audit'):
            yield
        reporting.reset_reported()

    @pytest.mark.django_db(transaction=True)
    async def test_abulk_create_is_guarded_on_the_scoped_path(self):
        """The scoped queryset's ``abulk_create`` must autofill exactly like the sync one."""
        from asgiref.sync import sync_to_async

        label = await sync_to_async(Label.objects.create)(name='Async Records')
        try:
            with tenant(label=label):
                await Release.objects.abulk_create([Release(title='async-filled')])

                created = await sync_to_async(lambda: Release.objects.get(title='async-filled'))()
                assert created.label_id == label.pk
        finally:
            with tenancy_bypassed():
                await sync_to_async(Release._all_objects.all().hard_delete)()
                await sync_to_async(Label._all_objects.all().hard_delete)()

    async def test_acreate_is_denied_without_a_scope(self):
        """No database mark: strict mode refuses before a statement is ever compiled, which is
        itself the guarantee -- a denial that had to reach the database would already have
        leaked the fact that the row exists."""
        with pytest.raises(TenantScopeError, match='needs an active tenant scope'):
            await Release.objects.acreate(title='nowhere')

    async def test_abulk_create_is_denied_without_a_scope(self):
        with pytest.raises(TenantScopeError, match='needs an active tenant scope'):
            await Release.objects.abulk_create([Release(title='nowhere')])

    @pytest.mark.django_db(transaction=True)
    async def test_acreate_reports_before_the_database_refuses(self, _audit_mode, caplog):
        """Audit mode softens the Python guard on the async path exactly as on the sync one --
        and the policy refuses anyway, because no setting makes a policy lenient.

        So the value of audit mode here is the *log line*: it names the call site, which the
        database error cannot. Asserted in that order.
        """
        with caplog.at_level('WARNING', logger='guitars.tenancy'):
            with pytest.raises(TenantScopeError, match='rejected by a tenant policy'):
                await Release.objects.acreate(title='async-audit')

        assert 'needs an active tenant scope' in caplog.text

    @pytest.mark.django_db(transaction=True)
    async def test_abulk_create_reports_before_the_database_refuses(self, _audit_mode, caplog):
        with caplog.at_level('WARNING', logger='guitars.tenancy'):
            with pytest.raises(TenantScopeError, match='rejected by a tenant policy'):
                await Release.objects.abulk_create([Release(title='async-audit-bulk')])

        assert 'needs an active tenant scope' in caplog.text


# ──────────────────────────── the GUC publisher ────────────────────────────── #


class TestPublisherGuards:
    def test_a_non_postgresql_connection_is_left_alone(self):
        """The policies are PostgreSQL-only, so there is nothing to publish elsewhere -- and
        issuing ``set_config`` at a backend that has never heard of it would break the
        connection rather than degrade."""

        class _Sqlite:
            vendor = 'sqlite'

        # Returns without touching the object: no cursor, no attribute set.
        guc._ensure(_Sqlite())

        assert not hasattr(_Sqlite(), guc._CACHE)

    def test_an_unrelated_error_is_not_swallowed(self):
        """Only "the transaction is already aborted" is skipped.

        Swallowing anything else would mean a failed publish looked like a successful one, and
        the *previous* tenant's value would stay live -- which fails open.
        """
        assert guc._aborted_transaction(ValueError('nothing to do with SQL')) is False

    def test_a_failed_publish_for_another_reason_propagates(self, db):
        def explode(connection, state):
            raise RuntimeError('publish is broken')

        original = guc._publish
        guc._publish = explode
        guc.install_on(connection)  # forget the cache, so _ensure really tries to publish
        try:
            with pytest.raises(RuntimeError, match='publish is broken'):
                guc._ensure(connection)
        finally:
            # Restored by hand rather than by monkeypatch: teardown rolls the test back, which
            # opens a cursor, which re-enters the wrapper -- and would hit the stub.
            guc._publish = original

    def test_uninstall_forgets_the_cached_state(self, db):
        """A reinstall must not inherit a cache describing a session it no longer owns."""
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        assert hasattr(connection, guc._CACHE)

        guc.uninstall()
        try:
            assert not hasattr(connection, guc._CACHE)
            assert guc._wrapper not in connection.execute_wrappers
        finally:
            guc.install()

        assert guc._wrapper in connections['default'].execute_wrappers


# ──────────────────────────── generator internals ──────────────────────────── #


class TestScaffolding:
    def test_it_returns_the_filename_django_printed(self, db):
        """Django prints the path it wrote rather than returning it, so it is parsed back out.

        Exercised for real -- including the ``makemigrations --empty`` round trip and the
        re-entry into guitars' own ``makemigrations`` override -- then cleaned up. A test that
        mocked ``call_command`` would only prove the regex, not that the output still looks the
        way the regex expects.
        """
        app = django_apps.get_app_config('testapp')

        filename = _generator.create_empty_migration_file(app, 'coverage_probe')
        try:
            assert filename.endswith('_coverage_probe.py')
            assert (__import__('pathlib').Path(app.path) / 'migrations' / filename).exists()
        finally:
            (__import__('pathlib').Path(app.path) / 'migrations' / filename).unlink()

    def test_unparseable_output_is_an_error(self, monkeypatch):
        """Guessing -- rewriting whichever file a glob found first -- would corrupt an
        unrelated migration."""
        monkeypatch.setattr(_generator, 'call_command', lambda *args, **kwargs: None)

        with pytest.raises(CommandError, match='Could not find the created migration file'):
            _generator.create_empty_migration_file(
                django_apps.get_app_config('testapp'), 'never_written'
            )


# ────────────────────────── audittenancy internals ─────────────────────────── #


class TestEnforcementModeNote:
    def test_it_says_nothing_when_no_policy_binds(self):
        """Audit mode is exactly right at this stage of a rollout, so the command stays quiet.

        Called directly with an empty live state, because a database with the policies applied
        is what every other test in the suite has.
        """
        from guitars.management.commands.audittenancy import Command

        with override_settings(GUITARS_TENANT_ENFORCE='audit'):
            assert Command._enforcement_mode_notes({}, {'testapp_release': object()}) == []

    def test_it_says_nothing_in_strict_mode(self):
        from guitars.management.commands.audittenancy import Command

        assert Command._enforcement_mode_notes({}, {}) == []
