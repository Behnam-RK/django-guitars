"""The last uncovered branches: defensive guards, async twins, vendor checks -- most
reached only by calling an internal directly. Async methods are separate code, so they
get their own tests rather than assumed sync-equivalent."""

from __future__ import annotations

from pathlib import Path

import pytest
from django.apps import apps as django_apps
from django.conf import settings as django_settings
from django.core.management import CommandError
from django.db import connection, connections, transaction
from django.test import override_settings

from guitars.management import _generator
from guitars.models import GuitarModel
from guitars.sql import policy
from guitars.tenancy import (
    TenantScopeError,
    TenantValueError,
    get_tenant,
    guc,
    reporting,
    spec,
    tenancy_bypassed,
    tenant,
)
from guitars.tenancy.checks import TENANT_MODEL_ID, check_guitar_models_have_a_tenant
from guitars.tenancy.discovery import _classify
from tests.conftest import execute as _execute
from tests.testapp.models import Band, Booking, Label, Release, Track


# ─────────────────────────── defensive SQL guards ──────────────────────────── #


def test_quote_literal_rejects_a_nul_byte():
    """Unreachable through the public API -- ``create_exempt_policy`` quotes the role as
    an identifier first. Called directly to keep the guard honest for a future caller."""
    with pytest.raises(ValueError, match='string literals cannot contain a NUL byte'):
        policy._quote_literal('bad\x00value')


# ──────────────────────────── the system check ─────────────────────────────── #


class TestTheTenantModelCheckShortCircuits:
    def test_it_is_silent_when_tenancy_is_wired(self):
        """This suite's own config -- the cheap path, taken on every ``manage.py check``."""
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
    """One dimension on this table, one through a relation -- the policy enforces what it
    can, the note says what it can't. Only the *spec* is stubbed, not a throwaway model."""

    @pytest.fixture
    def mixed_spec(self, monkeypatch):
        stub_spec = {'label': 'label', 'via_release': 'release__label'}
        for module in ('guitars.tenancy.discovery', 'guitars.tenancy.spec'):
            monkeypatch.setattr(f'{module}.tenant_spec', lambda model, _spec=stub_spec: _spec)

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
        """A typo'd lookup is simply not a local column -- ``FieldDoesNotExist`` is caught
        by type, not a bare ``Exception``, so a genuine bug still surfaces."""
        monkeypatch.setattr(
            'guitars.tenancy.spec.tenant_spec', lambda model: {'label': 'no_such_field'}
        )

        assert spec.local_tenant_fields(Release) == {}

    def test_a_lookup_naming_a_non_concrete_field_is_not_local(self, monkeypatch):
        """A lookup with no ``__`` is not automatically a column -- ``albums`` resolves as
        a reverse relation, which has no column of its own, and must be dropped too."""
        monkeypatch.setattr(
            'guitars.tenancy.spec.tenant_spec', lambda model: {'label': 'albums'}
        )

        assert spec.local_tenant_fields(Band) == {}

    def test_an_untenanted_model_never_autofills(self):
        """``_autofills`` walks the managers looking for a tenanted one and finds none."""
        assert spec._autofills(Label) is False


class TestAWriteWithNoTenantAndNoScope:
    def test_it_names_both_problems(self, db):
        """Neither an explicit tenant nor a scope to take one from -- distinct from
        autofill's case and from "explicit, no scope" (allowed), so its own message."""
        with pytest.raises(TenantScopeError, match='no active tenant scope to take one from'):
            Booking(venue='Nowhere').save()


class TestAsyncTwins:
    """``acreate`` / ``abulk_create`` are separate code, so they get separate proof."""

    @pytest.fixture(autouse=True)
    async def _close_executor_connections(self):
        """Close the connection Django's async ORM opened in its executor thread --
        ``connections`` is thread-local, invisible to the main thread's teardown. Closed
        *through* sync_to_async so the call lands in the same thread that opened it."""
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
        """Audit softens the Python guard, but the policy refuses anyway -- so its value
        here is the *log line*, naming the call site the database error cannot."""
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


class TestTheEncodingRefusesWhatItCannotCarry:
    """A tenant value containing the separator would make the *database* layer wider: a pk
    of ``'acme,globex'`` reads as the two-tenant scope ``['acme', 'globex']``. Refused
    twice: at ``tenant()`` entry and at publish, the boundary the policy actually reads."""

    def test_a_scalar_containing_the_separator_is_refused(self):
        with pytest.raises(TenantValueError, match='contains'):
            guc.encode_value('acme,globex')

    def test_a_collection_member_containing_the_separator_is_refused(self):
        """The collection path encodes each member through the same helper, so it cannot be
        the loophole -- ``['a', 'b,c']`` would otherwise publish as three values."""
        with pytest.raises(TenantValueError, match='contains'):
            guc.encode_value(['acme', 'globex,initech'])

    def test_the_scope_refuses_it_at_entry_and_names_the_dimension(self):
        """The eager half: the traceback points at the ``with`` the caller wrote, not a
        cursor several frames away inside whichever query happened to publish first."""
        with pytest.raises(TenantValueError, match=r'tenant\(shop=\.\.\.\) value'):
            with tenant(shop='acme,globex'):
                pytest.fail('the scope should not have opened')

        # And it did not leave a frame behind on the way out.
        assert get_tenant() == {}

    def test_a_collection_is_checked_at_entry_too(self):
        with pytest.raises(TenantValueError, match='contains'):
            with tenant(shop=['acme', 'globex,initech']):
                pytest.fail('the scope should not have opened')

    def test_a_pk_acquired_after_entry_is_still_refused(self):
        """Why the publish-time guard stays: an unsaved instance's ``pk=None`` passes the
        eager check honestly, but can acquire a separator before the frame publishes."""

        class _Unsaved:
            pk = None

        instance = _Unsaved()
        with tenant(shop=instance):  # passes: pk is None, which publishes as empty and denies
            instance.pk = 'acme,globex'
            with pytest.raises(TenantValueError, match='contains'):
                guc.desired_state()

    def test_an_ordinary_value_still_encodes(self):
        """The guard must not have made every scope unusable."""
        assert guc.encode_value('acme') == 'acme'
        assert guc.encode_value(['b', 'a']) == 'a,b'

    def test_a_none_pk_still_encodes_as_empty(self):
        """Unchanged: an empty GUC yields an empty array, which denies."""
        assert guc.encode_value(None) == ''


class TestWalkChain:
    """M5 (#12): the exception-chain walk used to be duplicated verbatim in
    ``_aborted_transaction`` and ``_rls_violation``; both now delegate to this."""

    def test_yields_the_exception_itself_first(self):
        exc = ValueError('leaf')

        assert list(guc._walk_chain(exc)) == [exc]

    def test_follows_cause_then_context(self):
        root = ValueError('root')
        middle = ValueError('middle')
        middle.__cause__ = root
        leaf = ValueError('leaf')
        leaf.__context__ = middle  # not __cause__ -- proves __cause__ is preferred first

        # leaf has no __cause__, so __context__ is followed; middle has __cause__, so
        # that wins over any __context__ it might also carry.
        assert list(guc._walk_chain(leaf)) == [leaf, middle, root]

    def test_a_cycle_terminates_instead_of_looping_forever(self):
        a = ValueError('a')
        b = ValueError('b')
        a.__cause__ = b
        b.__cause__ = a  # cycle

        assert list(guc._walk_chain(a)) == [a, b]


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
        """Only "the transaction is already aborted" is skipped -- swallowing anything
        else would leave the *previous* tenant's value looking live, failing open."""
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

    @pytest.mark.django_db(transaction=True)
    def test_a_transaction_marker_is_found_past_an_unrelated_commit_hook(self):
        """The replace-in-place loop must not assume the marker is the *first* entry --
        something could register a hook earlier. ``transaction=True`` is load-bearing:
        under plain ``db`` the fixture's own publish would land at index 0."""
        with tenancy_bypassed():
            # Outside any atomic() block: in_atomic_block is False, so this publish is
            # session-level (no run_on_commit entry at all) -- run_on_commit starts empty.
            label_a = Label.objects.create(name='Aardvark')
            label_b = Label.objects.create(name='Basilisk')

        with transaction.atomic():
            # Registered before any tenant switch, so it sits ahead of the first marker.
            transaction.on_commit(lambda: None)

            with tenant(label=label_a):
                Release.objects.exists()  # first publish: superseded=None, marker appended last
            first_marker = getattr(connection, guc._CACHE)[2]

            with tenant(label=label_b):
                # Different state -> _ensure republishes, this time with superseded=first_marker,
                # which now sits behind the decoy and makes the search skip past it.
                Release.objects.exists()
            second_marker = getattr(connection, guc._CACHE)[2]

            hooks = [hook for _, hook, *_ in connection.run_on_commit]
            # Replaced in place, not appended: still one decoy plus one marker.
            assert len(hooks) == 2
            assert first_marker not in hooks
            assert second_marker in hooks

    def test_uninstall_forgets_the_cached_state(self, db):
        """A reinstall must not inherit a cache describing a session it no longer owns."""
        _execute('SELECT 1')
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
    @pytest.mark.xdist_group(name='makemigrations_override_dir')
    def test_it_returns_the_filename_django_printed(self, db):
        """Django prints the path rather than returning it, parsed back out -- exercised
        for real, not mocked. Scaffolds into ``tests.makemigrations_override``, not
        ``tests.testapp``, whose migrations dir is scanned concurrently under xdist."""
        with override_settings(
            INSTALLED_APPS=[*django_settings.INSTALLED_APPS, 'tests.makemigrations_override']
        ):
            app = django_apps.get_app_config('makemigrations_override')

            filename = _generator.create_empty_migration_file(app, 'coverage_probe')
            try:
                assert filename.endswith('_coverage_probe.py')
                assert (Path(app.path) / 'migrations' / filename).exists()
            finally:
                (Path(app.path) / 'migrations' / filename).unlink()

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
        """Audit mode is right at this rollout stage. Called with an empty live state,
        since a database with policies applied is what every other test has."""
        from guitars.management.commands.audittenancy import Command

        with override_settings(GUITARS_TENANT_ENFORCE='audit'):
            assert Command._enforcement_mode_notes({}, {'testapp_release': object()}) == []

    def test_it_says_nothing_in_strict_mode(self):
        from guitars.management.commands.audittenancy import Command

        assert Command._enforcement_mode_notes({}, {}) == []
