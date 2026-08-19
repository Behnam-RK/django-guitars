"""Concurrency and connection-lifecycle tests -- the parts of the design that only matter
once more than one connection exists at a time: two threads, an already-running event
loop, a connection Django keeps alive, and a pooler underneath both Django and Postgres."""

from __future__ import annotations

import socket
import threading

import pytest
from asgiref.sync import SyncToAsync, async_to_sync
from django.db import close_old_connections, connection, connections
from django.db.models.signals import pre_save

from guitars.tenancy import tenant
from tests.conftest import scalar as _scalar
from tests.testapp.models import Band, Release


PGBOUNCER_HOST, PGBOUNCER_PORT = 'localhost', 6432


def _pgbouncer_is_up() -> bool:
    """Whether the opt-in ``pgbouncer`` compose profile is running -- plain ``docker
    compose up -d`` doesn't start it, so most runs skip these rather than fail."""
    try:
        with socket.create_connection((PGBOUNCER_HOST, PGBOUNCER_PORT), timeout=0.5):
            return True
    except OSError:
        return False


requires_pgbouncer = pytest.mark.skipif(
    not _pgbouncer_is_up(),
    reason='pgbouncer is not listening on :6432 -- run `docker compose --profile pooling up -d --wait`',
)


@pytest.fixture(scope='module', autouse=True)
def _reap_worker_thread_connections():
    """Close whatever this module's tests left open, once, at the end -- a per-test sweep
    could kill another test's still-needed connection. The main thread's own connection
    (kept alive across modules by pytest-django) must be excluded by pid."""
    yield
    try:
        # asgiref's one persistent thread holds the async ORM's connection past this module:
        # terminate that backend from outside and a later async test reusing the thread finds
        # a live connection object on a dead server. Close it from inside the thread instead.
        SyncToAsync.single_thread_executor.submit(connections.close_all).result(timeout=10)
    finally:
        # ``finally``, because the sweep is this fixture's whole point: if the close above
        # raises or times out, skipping it would leak every worker-thread connection this
        # module opened into the next one. The close's own error still propagates after.
        main = connections['default'].settings_dict
        exclude_pids = []
        if connection.connection is not None:
            exclude_pids.append(connection.connection.info.backend_pid)
        import psycopg

        with psycopg.connect(
            dbname=main['NAME'],
            user=main['USER'],
            password=main['PASSWORD'],
            host=main['HOST'] or 'localhost',
            port=main['PORT'] or None,
            autocommit=True,
        ) as reaper:
            exclude_pids.append(reaper.info.backend_pid)
            with reaper.cursor() as cursor:
                cursor.execute(
                    'SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                    'WHERE datname = current_database() AND pid <> ALL(%s)',
                    [exclude_pids],
                )


def test_the_connecting_role_cannot_bypass_rls(db):
    """Precondition for every other test in this module -- see test_tenancy_rls.py."""
    can_bypass = _scalar(
        'SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user'
    )
    assert can_bypass is False, (
        'the test role can bypass RLS, so these tests would pass without enforcing '
        'anything -- see scripts/postgres-init.sql'
    )


# ──────────────────────────────────── threads ──────────────────────────────────── #


@pytest.mark.django_db(transaction=True)
class TestTwoThreadsInDifferentScopes:
    """The tenant frame is a per-OS-thread ``ContextVar`` -- this fails if that were ever
    shared/global. ``transaction=True``: plain ``db`` wraps the test in a rollback-only
    transaction on the *main* thread, so a worker thread's connection would see nothing."""

    def test_no_cross_tenant_leakage_under_concurrent_scopes(self, tenants):
        barrier = threading.Barrier(2)
        seen: dict[str, list[str]] = {}
        errors: list[BaseException] = []

        def worker(key: str, label) -> None:
            try:
                with tenant(label=label):
                    # Both threads reach the query at (approximately) the same instant,
                    # so any shared mutable state behind `tenant()` or the GUC cache has
                    # its best chance to show up as the other thread's rows.
                    barrier.wait(timeout=5)
                    seen[key] = list(Release.objects.values_list('title', flat=True))
            except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread below
                errors.append(exc)
            finally:
                connection.close()

        threads = [
            threading.Thread(target=worker, args=('a', tenants.a)),
            threading.Thread(target=worker, args=('b', tenants.b)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, errors
        assert seen == {'a': ['release-a'], 'b': ['release-b']}


@pytest.mark.django_db(transaction=True)
class TestConcurrentAupdateDisableSignals:
    """Proves ``aupdate()``'s claim: overlapping ``_disable_signals=True`` blocks nest
    safely. Real threads, not ``asyncio.gather`` -- asgiref pins one loop's calls to a
    single thread, so gather could never actually overlap."""

    def test_two_threads_both_persist_and_the_write_guard_is_restored(self):
        before = len(pre_save.receivers)
        band_a = Band.objects.create(name='A', nickname='a-nick')
        band_b = Band.objects.create(name='B', nickname='b-nick')

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def worker(band, new_name: str) -> None:
            try:
                barrier.wait(timeout=5)
                async_to_sync(band.aupdate)(name=new_name, _disable_signals=True)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread below
                errors.append(exc)
            finally:
                connection.close()

        threads = [
            threading.Thread(target=worker, args=(band_a, 'A2')),
            threading.Thread(target=worker, args=(band_b, 'B2')),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, errors
        assert Band.objects.get(pk=band_a.pk).name == 'A2'
        assert Band.objects.get(pk=band_b.pk).name == 'B2'
        # Not under- or over-connected after both blocks close -- proof the reference
        # count survived two overlapping entries/exits across real threads, not just one.
        assert len(pre_save.receivers) == before


# ──────────────────────────────────── asyncio ──────────────────────────────────── #


@pytest.mark.django_db(transaction=True)
async def test_aupdate_inside_an_already_running_event_loop(tenants):
    """``test_base.py``'s version only calls ``aupdate`` via ``async_to_sync``, which
    builds a fresh loop per call -- never the already-running loop every real caller (an
    async view) has. ``transaction=True``: the write runs on a worker thread's own connection."""
    with tenant(label=tenants.a):
        await tenants.release_a.aupdate(title='renamed-inside-a-running-loop')

    with tenant(label=tenants.a):
        refreshed = await Release.objects.aget(pk=tenants.release_a.pk)

    assert refreshed.title == 'renamed-inside-a-running-loop'
    # The worker thread's own connection is left open: successive sync_to_async calls
    # don't reliably land on the same OS thread, so nothing here can close it. Swept by
    # ``_reap_worker_thread_connections`` once for the whole module instead.


# ─────────────────────────── connection reuse (CONN_MAX_AGE) ─────────────────────────── #


@pytest.mark.django_db(transaction=True)
def test_a_persistent_connection_tracks_a_new_tenant_across_logical_requests(tenants):
    """``CONN_MAX_AGE`` is unset elsewhere, so nothing else proves the GUC cache survives
    one connection serving a second logical request. ``settings_dict`` mutated directly,
    not via ``override_settings``: the wrapper caches it once and never rebuilds."""
    original_max_age = connection.settings_dict['CONN_MAX_AGE']
    connection.close()  # so the next connect() picks up CONN_MAX_AGE=60 from the start
    connection.settings_dict['CONN_MAX_AGE'] = 60
    try:
        with tenant(label=tenants.a):
            assert list(Release.objects.values_list('title', flat=True)) == ['release-a']
        reused = connection.connection
        assert reused is not None

        close_old_connections()
        assert connection.connection is reused, (
            'the connection was closed despite CONN_MAX_AGE=60 -- the scenario below '
            'proves nothing if each "request" secretly gets a fresh connection'
        )

        with tenant(label=tenants.b):
            assert list(Release.objects.values_list('title', flat=True)) == ['release-b']
        assert connection.connection is reused, 'still the same connection as the first request'
    finally:
        # Not just close_old_connections(): a not-yet-"old" connection would survive
        # past this test and block the session-end DROP DATABASE.
        connection.close()
        connection.settings_dict['CONN_MAX_AGE'] = original_max_age


@pytest.mark.django_db(transaction=True)
def test_conn_max_age_zero_is_the_control_that_makes_the_above_meaningful(tenants):
    """Without this, the previous test's reuse assertion could pass merely because
    pytest never closes anything, not because CONN_MAX_AGE caused it."""
    with tenant(label=tenants.a):
        list(Release.objects.values_list('title', flat=True))
    live = connection.connection
    assert live is not None

    close_old_connections()

    assert connection.connection is not live


# ───────────────────────────── Django's psycopg connection pool ───────────────────────────── #


def _pool_alias_available() -> bool:
    try:
        import psycopg_pool  # noqa: F401
    except ImportError:
        return False
    import django

    return django.VERSION >= (5, 1)


requires_connection_pool = pytest.mark.skipif(
    not _pool_alias_available(),
    reason='needs Django>=5.1 and psycopg_pool installed',
)


@requires_connection_pool
@pytest.mark.django_db(transaction=True, databases=['default', 'pooled'])
def test_tenant_scope_is_correct_under_djangos_psycopg_pool(tenants):
    """Django 5.1+'s pool hands out a different connection per checkout, firing a fresh
    ``connection_created`` signal each time, so guitars' GUC cache starts empty. The
    'pooled' alias must be a real settings.DATABASES entry -- Django validates it early."""
    pooled = connections['pooled']
    try:
        for key, label, expected in (
            ('a', tenants.a, 'release-a'),
            ('b', tenants.b, 'release-b'),
        ):
            with tenant(label=label):
                names = list(Release.objects.using('pooled').values_list('title', flat=True))
            assert names == [expected], key
    finally:
        pooled.close_pool()


# ──────────────────────────────────── pgbouncer ──────────────────────────────────── #


@requires_pgbouncer
@pytest.mark.xdist_group(name='pgbouncer')
class TestPgbouncerTransactionPooling:
    """pgbouncer runs one shared backend (``DEFAULT_POOL_SIZE: 1``), deliberately, turning
    the GUC-leak race into a two-line reproduction -- neither test goes through Django.
    ``xdist_group`` pins both to one worker, or a second worker could read the wrong leak."""

    @staticmethod
    def _connect():
        import psycopg

        return psycopg.connect(
            f'postgresql://guitars:guitars@{PGBOUNCER_HOST}:{PGBOUNCER_PORT}/guitars',
            autocommit=True,
        )

    def test_session_set_is_not_naturally_isolated_between_clients(self):
        """The risk ``docs/tenancy.md`` warns about, shown directly: client A sets a
        session GUC and disconnects; client B connects and reads A's value back, since
        both share the one pgbouncer backend under transaction pooling."""
        with self._connect() as client_a, client_a.cursor() as cursor:
            cursor.execute("SET tenant.label = 'leaked-from-client-a'")

        with self._connect() as client_b, client_b.cursor() as cursor:
            cursor.execute("SELECT current_setting('tenant.label', true)")
            (value,) = cursor.fetchone()

        assert value == 'leaked-from-client-a', (
            'expected the documented leak -- if this starts failing, either pgbouncer '
            'stopped being configured for transaction pooling with one backend, or a '
            'newer pgbouncer discards session state between clients by default, and '
            'the compose service / this test need to be revisited together'
        )

    def test_reset_all_is_what_a_pooler_should_be_configured_to_run_between_clients(self):
        """The mitigation, proven not assumed: ``DISCARD ALL`` (a properly configured
        pgbouncer's ``server_reset_query``) clears exactly the state the previous test
        showed leaking, on the same one-backend setup."""
        with self._connect() as client_a, client_a.cursor() as cursor:
            cursor.execute("SET tenant.label = 'should-not-survive-a-reset'")
            cursor.execute('DISCARD ALL')

        with self._connect() as client_b, client_b.cursor() as cursor:
            cursor.execute("SELECT current_setting('tenant.label', true)")
            (value,) = cursor.fetchone()

        assert value in (None, ''), f'expected DISCARD ALL to have cleared the GUC, got {value!r}'
