"""Concurrency and connection-lifecycle tests.

The rest of the suite proves the enforcement layers correct for one request on one
connection. This file proves the parts of the design that only matter once more than one
of those exists at a time: two threads, an already-running event loop, a connection
Django keeps alive across requests, and -- where the local Postgres offers a pooler -- a
connection pool operating underneath both Django and PostgreSQL.

Negative control first, matching ``test_tenancy_rls.py``'s discipline: if the connecting
role could bypass RLS, none of what follows would prove anything.
"""

from __future__ import annotations

import socket
import threading

import pytest
from django.db import close_old_connections, connection, connections

from guitars.tenancy import tenant
from tests.conftest import scalar as _scalar
from tests.testapp.models import Release


PGBOUNCER_HOST, PGBOUNCER_PORT = 'localhost', 6432


def _pgbouncer_is_up() -> bool:
    """Whether the opt-in ``pgbouncer`` compose profile is running.

    ``docker compose --profile pooling up -d --wait`` starts it; plain ``docker compose up
    -d`` does not, so most local runs and CI cells skip these rather than fail on a
    service nobody asked for.
    """
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
    """Close whatever this module's threading/asyncio tests left open, once, at the end.

    The threading test closes its own worker connections explicitly. The asyncio test
    cannot: ``aupdate()``'s write runs via ``sync_to_async`` on a worker thread whose
    Django connection wrapper refuses to be closed from any other thread (by design --
    see ``validate_thread_sharing``), and which independent ``sync_to_async`` call lands
    on which OS thread is not something this suite controls or can predict.

    Doing this per-test instead would be actively wrong: a raw ``pg_terminate_backend``
    sweep run *between* tests can just as easily kill the main test connection another
    test's own teardown still needs, which is worse than the leak it was meant to fix.
    Run once, after every test in this module is done and nothing needs a connection
    anymore, there is nothing left to protect against.

    The main thread's own Django connection is exactly such a survivor: pytest-django
    keeps it open and idle across test *modules* within one process, so later files
    (``test_management_audittenancy.py`` et al.) reuse it rather than reconnecting. A
    sweep that excludes only its own pid -- not that one -- terminates it too, and the
    next query any later test runs on it fails with ``OperationalError: terminating
    connection due to administrator command``. Its pid must be excluded explicitly.
    """
    yield
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
    """The tenant frame is a ``ContextVar`` (see tenancy/scope.py), one per OS thread by
    default. This is the test that would fail if that were ever a shared/global instead.

    ``transaction=True``, not the plain ``db`` fixture: the default fixture wraps the
    whole test in one rollback-only transaction on the *main* thread's connection, so a
    worker thread's separate connection would not see ``tenants``' rows at all -- every
    query below would truthfully return nothing, for a reason unrelated to tenancy.
    """

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


# ──────────────────────────────────── asyncio ──────────────────────────────────── #


@pytest.mark.django_db(transaction=True)
async def test_aupdate_inside_an_already_running_event_loop(tenants):
    """``test_base.py::test_aupdate_persists_changes`` only ever calls ``aupdate`` via
    ``async_to_sync``, which builds and tears down a fresh event loop for the one call --
    it never runs with a loop that was already running before the scope opened, which is
    the shape every real caller (an async view, a Channels consumer) actually has.

    ``asyncio_mode = "auto"`` (pyproject.toml) makes this an ``async def`` test running on
    a real, already-running loop with no ``async_to_sync`` involved.

    ``transaction=True``: ``aupdate`` runs the actual write via ``sync_to_async``, which
    executes on a worker thread and therefore a separate connection -- the same reason
    the threading test above needs it.
    """
    with tenant(label=tenants.a):
        await tenants.release_a.aupdate(title='renamed-inside-a-running-loop')

    with tenant(label=tenants.a):
        refreshed = await Release.objects.aget(pk=tenants.release_a.pk)

    assert refreshed.title == 'renamed-inside-a-running-loop'
    # aupdate()'s write runs via sync_to_async, on a worker thread whose own (separate,
    # thread-local) Django connection nothing in this test closes -- and, confirmed
    # empirically rather than assumed, successive independent sync_to_async calls in this
    # pytest-asyncio environment do not reliably land on the same OS thread, so there is
    # no single thread to reach back into and close it from here. See
    # ``_reap_worker_thread_connections`` below, which sweeps it up once for the whole
    # module instead of trying to identify and close it mid-test.


# ─────────────────────────── connection reuse (CONN_MAX_AGE) ─────────────────────────── #


@pytest.mark.django_db(transaction=True)
def test_a_persistent_connection_tracks_a_new_tenant_across_logical_requests(tenants):
    """``CONN_MAX_AGE`` is confirmed unset everywhere in this harness (see CLAUDE.md), so
    nothing before this test ever proved the GUC cache survives the one thing
    ``CONN_MAX_AGE`` exists to cause: the *same* physical connection serving a second,
    unrelated logical request.

    ``connection.settings_dict`` is mutated directly rather than through
    ``override_settings(DATABASES=...)``: a wrapper object is cached the first time
    ``connections[alias]`` is accessed (``django.utils.connection.BaseConnectionHandler``),
    and nothing rebuilds that cache when the ``DATABASES`` setting changes later --
    ``connect()`` reads ``max_age`` from *this* wrapper's own ``self.settings_dict``, so
    only mutating that dict in place actually reaches it.

    ``close_old_connections()`` is Django's own hook, normally wired to the
    request-started/request-finished signals -- nothing calls it under pytest, so without
    calling it explicitly here ``CONN_MAX_AGE`` would be decorative and this test would
    pass whether or not connection reuse actually works.
    """
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
        # Not just close_old_connections(): a connection that is not yet "old" by
        # CONN_MAX_AGE's clock would survive past this test and block the session-end
        # DROP DATABASE with "database is being accessed by other users".
        connection.close()
        connection.settings_dict['CONN_MAX_AGE'] = original_max_age


@pytest.mark.django_db(transaction=True)
def test_conn_max_age_zero_is_the_control_that_makes_the_above_meaningful(tenants):
    """Without this, the previous test's ``assert connection.connection is reused`` could
    pass merely because pytest never closes anything -- not because ``CONN_MAX_AGE``
    caused reuse. ``CONN_MAX_AGE`` defaults to 0 (unset, see core/settings.py), under
    which ``close_old_connections()`` must actually close an idle connection.
    """
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
    """Django 5.1+'s ``OPTIONS: {"pool": True}`` hands out a *different* psycopg connection
    object per checkout from the same underlying pool -- closer to what a pgbouncer-style
    pooler does than a single long-lived connection is. Each checkout is a fresh
    ``connection_created`` signal, so guitars re-installs its wrapper and starts the GUC
    cache empty every time, which is exactly what should make this safe.

    The 'pooled' alias lives in tests/settings.py (``TEST: {"MIRROR": "default"}``, so it
    shares the migrated test database rather than getting its own) -- it has to be a real
    settings.DATABASES entry because Django validates ``django_db(databases=[...])``
    against settings at test-class setup, before this test body ever runs.
    """
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
class TestPgbouncerTransactionPooling:
    """``compose.yaml``'s opt-in ``pooling`` profile runs pgbouncer with ``POOL_MODE:
    transaction`` and ``DEFAULT_POOL_SIZE: 1`` -- one PostgreSQL backend total, shared by
    every client. That size is deliberate, not incidental: with exactly one backend, two
    *different* client connections are guaranteed to share it, which is what turns "a
    session GUC can leak between logically unrelated clients under transaction pooling"
    from a probabilistic race into a two-line reproduction.

    Neither test below goes through Django or ``guitars.tenancy`` -- see the module
    docstring on ``tenancy/guc.py``: the whole reason its cache is keyed on more than the
    GUC values is that PostgreSQL, not guitars, is what silently reverts a ``SET``. This
    demonstrates the PostgreSQL-and-pooler-level mechanism that guards against; there is
    no Python-level cache to fool because nothing here asks Python to cache anything.
    """

    @staticmethod
    def _connect():
        import psycopg

        return psycopg.connect(
            f'postgresql://guitars:guitars@{PGBOUNCER_HOST}:{PGBOUNCER_PORT}/guitars',
            autocommit=True,
        )

    def test_session_set_is_not_naturally_isolated_between_clients(self):
        """The risk ``docs/tenancy.md`` and ``tenancy/guc.py`` warn about, shown directly.

        Client A sets a session GUC and disconnects (releasing pgbouncer's one backend).
        Client B then connects and, without ever setting it, reads client A's value back --
        because with ``DEFAULT_POOL_SIZE: 1`` and transaction pooling, B's transaction was
        handed the exact same PostgreSQL session A just left it in. A direct ``SET`` (what
        a plain, unpooled connection would use) does not survive a client disconnecting in
        session-pooling mode, but it does here.
        """
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
        """The mitigation, proven rather than assumed: ``DISCARD ALL`` (what a properly
        configured transaction-mode pgbouncer runs as its ``server_reset_query``) clears
        exactly the state the previous test showed leaking. This does not assert anything
        about this repo's pgbouncer configuration -- see the docstring above, and the M5
        follow-up this test family exists to inform -- it proves the fix these tests
        recommend actually closes the gap, on the same one-backend setup.
        """
        with self._connect() as client_a, client_a.cursor() as cursor:
            cursor.execute("SET tenant.label = 'should-not-survive-a-reset'")
            cursor.execute('DISCARD ALL')

        with self._connect() as client_b, client_b.cursor() as cursor:
            cursor.execute("SELECT current_setting('tenant.label', true)")
            (value,) = cursor.fetchone()

        assert value in (None, ''), f'expected DISCARD ALL to have cleared the GUC, got {value!r}'
