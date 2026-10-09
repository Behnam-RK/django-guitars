"""Two sessions, stepped by hand (#91, ADR 0044): the cascade arms and the guard when a child and
its parent are written at once. A statement that must wait runs in a thread, and the test goes on
once ``pg_stat_activity`` shows it waiting on a lock, so nothing sleeps for a result."""

import threading
import time

import psycopg
import pytest
from django.db import OperationalError, connection, transaction

from tests.testapp.models import Offer, Setlist, Tier


ARCHIVE_OFFER = (
    'UPDATE testapp_offer SET _deleted_at = NOW() WHERE id = %s AND _deleted_at IS NULL'
)
RESTORE_OFFER = 'UPDATE testapp_offer SET _deleted_at = NULL WHERE id = %s'
INSERT_TIER = (
    'INSERT INTO testapp_tier (offer_id, _created_at, _updated_at) VALUES (%s, NOW(), NOW())'
)

pytestmark = pytest.mark.django_db(transaction=True)


class Session:
    """A raw connection with its own transaction, and a way to run one statement off-thread."""

    def __init__(self, isolation: str = 'READ COMMITTED') -> None:
        settings = connection.settings_dict
        self.conn = psycopg.connect(
            dbname=settings['NAME'],
            user=settings['USER'],
            password=settings['PASSWORD'],
            host=settings['HOST'],
            port=settings['PORT'],
        )
        self.conn.isolation_level = getattr(
            psycopg.IsolationLevel, isolation.replace(' ', '_').upper()
        )
        self.pid = self.conn.info.backend_pid
        self._thread: threading.Thread | None = None
        self.error: BaseException | None = None

    def run(self, sql: str, *params):
        with self.conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall() if cursor.description else None

    def start(self, sql: str, *params) -> None:
        """Run *sql* in a thread, for a statement expected to wait."""

        def work():
            try:
                self.run(sql, *params)
            except psycopg.Error as exc:
                self.error = exc

        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()

    def waiting(self) -> bool:
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s', [self.pid]
            )
            row = cursor.fetchone()
        return bool(row) and row[0] == 'Lock'

    def wait_until_blocked(self) -> None:
        deadline = time.monotonic() + 10
        while not self.waiting():
            assert time.monotonic() < deadline, 'the statement never waited on a lock'
            time.sleep(0.01)

    def finish(self) -> None:
        assert self._thread is not None
        self._thread.join(timeout=10)
        assert not self._thread.is_alive(), 'the statement never returned'

    def close(self) -> None:
        self.conn.rollback()
        self.conn.close()


@pytest.fixture
def session():
    opened: list[Session] = []

    def make(isolation: str = 'READ COMMITTED') -> Session:
        opened.append(Session(isolation))
        return opened[-1]

    yield make
    for each in opened:
        each.close()


def _stamp(model, pk):
    return model._all_objects.values_list('_deleted_at', flat=True).get(pk=pk)


def _tiers(offer) -> list:
    return list(Tier._all_objects.filter(offer=offer).values_list('_deleted_at', flat=True))


class TestAChildAndItsParentArchivedAtOnce:
    """Before 2.22.0 each of these left a live child under an archived parent: the archive's
    row lock (``FOR NO KEY UPDATE``) does not conflict with a foreign-key check's
    (``FOR KEY SHARE``), and the arm's snapshot predates the child's commit."""

    def test_an_insert_waits_for_an_uncommitted_archive_and_is_archived_with_it(self, session):
        offer = Offer.objects.create(name='p')
        a, b = session(), session()
        a.run(ARCHIVE_OFFER, offer.pk)

        b.start(INSERT_TIER, offer.pk)
        b.wait_until_blocked()
        a.conn.commit()
        b.finish()
        b.conn.commit()

        assert b.error is None
        assert _tiers(offer) == [_stamp(Offer, offer.pk)]
        assert _tiers(offer)[0] is not None

    def test_an_archive_waits_for_an_uncommitted_insert_and_takes_its_child(self, session):
        offer = Offer.objects.create(name='p')
        a, b = session(), session()
        b.run(INSERT_TIER, offer.pk)

        a.start(ARCHIVE_OFFER, offer.pk)
        a.wait_until_blocked()
        b.conn.commit()
        a.finish()
        a.conn.commit()

        assert a.error is None
        assert _tiers(offer) == [_stamp(Offer, offer.pk)]
        assert _tiers(offer)[0] is not None

    def test_a_re_pointed_row_waits_and_is_archived_with_the_new_parent(self, session):
        gone, live = Offer.objects.create(name='gone'), Offer.objects.create(name='live')
        tier = Tier.objects.create(offer=live)
        a, b = session(), session()
        a.run(ARCHIVE_OFFER, gone.pk)

        b.start('UPDATE testapp_tier SET offer_id = %s WHERE id = %s', gone.pk, tier.pk)
        b.wait_until_blocked()
        a.conn.commit()
        b.finish()
        b.conn.commit()

        assert b.error is None
        assert _stamp(Tier, tier.pk) == _stamp(Offer, gone.pk) is not None

    def test_a_leaf_added_to_a_tree_being_archived_is_archived_with_it(self, session):
        """Depth five, through the self key's arm: the leaf's own guard waits on its parent."""
        chain = [Setlist.objects.create(title='0')]
        for depth in range(1, 5):
            chain.append(Setlist.objects.create(title=str(depth), parent=chain[-1]))
        a, b = session(), session()
        a.run(
            'UPDATE testapp_setlist SET _deleted_at = NOW() WHERE id = %s AND _deleted_at IS NULL',
            chain[0].pk,
        )

        b.start(
            'INSERT INTO testapp_setlist (title, parent_id, _created_at, _updated_at) '
            "VALUES ('leaf', %s, NOW(), NOW())",
            chain[-1].pk,
        )
        b.wait_until_blocked()
        a.conn.commit()
        b.finish()
        b.conn.commit()

        assert b.error is None
        assert Setlist._all_objects.filter(_deleted_at__isnull=True).count() == 0


class TestTwoWritersOfOneParent:
    def test_a_second_archive_waits_then_changes_nothing(self, session):
        offer = Offer.objects.create(name='p')
        Tier.objects.create(offer=offer)
        a, b = session(), session()
        a.run(ARCHIVE_OFFER, offer.pk)
        first = a.run('SELECT _deleted_at FROM testapp_offer WHERE id = %s', offer.pk)[0][0]

        b.start(ARCHIVE_OFFER, offer.pk)
        b.wait_until_blocked()
        a.conn.commit()
        b.finish()
        b.conn.commit()

        assert b.error is None
        assert _stamp(Offer, offer.pk) == first
        assert _tiers(offer) == [first]

    def test_a_restore_waits_for_an_uncommitted_archive_and_revives_the_whole_tree(self, session):
        offer = Offer.objects.create(name='p')
        Tier.objects.create(offer=offer)
        a, b = session(), session()
        a.run(ARCHIVE_OFFER, offer.pk)

        b.start(RESTORE_OFFER, offer.pk)
        b.wait_until_blocked()
        a.conn.commit()
        b.finish()
        b.conn.commit()

        assert b.error is None
        assert _stamp(Offer, offer.pk) is None
        assert _tiers(offer) == [None]

    def test_opposite_orders_deadlock_and_one_survives(self, session):
        """PostgreSQL's own answer, and the kit adds no second one: the sessions hold a parent
        each and reach for the other's. ``40P01`` aborts one; the other commits."""
        first, second = Offer.objects.create(name='1'), Offer.objects.create(name='2')
        a, b = session(), session()
        a.run(ARCHIVE_OFFER, first.pk)
        b.run(ARCHIVE_OFFER, second.pk)

        a.start(ARCHIVE_OFFER, second.pk)
        a.wait_until_blocked()
        try:
            b.run(ARCHIVE_OFFER, first.pk)
        except psycopg.Error as exc:
            b.error = exc
        a.finish()

        losers = [each for each in (a, b) if each.error is not None]
        assert len(losers) == 1
        assert losers[0].error.sqlstate == '40P01'


class TestTheCostOfTheGuard:
    def test_two_inserts_each_followed_by_an_update_of_the_parent_deadlock(self, session):
        """What ``FOR SHARE`` costs (ADR 0044): each insert holds a share lock on the parent
        until commit, and a non-key update of that parent waits for the other's. Without the
        guard the two serialise with no error. ``GUITARS_CASCADE_GUARD = False`` removes it."""
        offer = Offer.objects.create(name='p')
        a, b = session(), session()
        a.run(INSERT_TIER, offer.pk)
        b.run(INSERT_TIER, offer.pk)
        update = "UPDATE testapp_offer SET name = 'x' WHERE id = %s"

        a.start(update, offer.pk)
        a.wait_until_blocked()
        try:
            b.run(update, offer.pk)
        except psycopg.Error as exc:
            b.error = exc
        a.finish()

        losers = [each for each in (a, b) if each.error is not None]
        assert len(losers) == 1
        assert losers[0].error.sqlstate == '40P01'

    def test_a_role_that_may_not_update_the_parent_reads_it_without_waiting(self, session):
        """The fallback branch, taken: another session's uncommitted update of the parent would
        block a ``FOR SHARE``, and the privilege check is shadowed to read ``false``."""
        offer = Offer.objects.create(name='p')
        a = session()
        a.run('SELECT 1 FROM testapp_offer WHERE id = %s FOR NO KEY UPDATE', offer.pk)

        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                'CREATE FUNCTION public.has_any_column_privilege(text, text) '
                'RETURNS boolean LANGUAGE sql AS $$ SELECT false $$'
            )
            cursor.execute(
                "SET LOCAL search_path = public, pg_catalog; SET LOCAL lock_timeout = '1s'"
            )
            cursor.execute(INSERT_TIER, [offer.pk])
            transaction.set_rollback(True)

    def test_without_the_shadow_the_same_insert_waits(self, session):
        offer = Offer.objects.create(name='p')
        a = session()
        a.run('SELECT 1 FROM testapp_offer WHERE id = %s FOR NO KEY UPDATE', offer.pk)

        with pytest.raises(OperationalError, match='lock timeout'), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '300ms'")
                cursor.execute(INSERT_TIER, [offer.pk])


class TestWhatIsolationChanges:
    def test_a_repeatable_read_archive_misses_a_child_committed_after_its_snapshot(self, session):
        """The limit, pinned as a limit: the arm reads the snapshot its transaction took before
        the child existed. A guard cannot help -- the child was inserted under a live parent.
        Two ``SERIALIZABLE`` sessions are the remedy, in the next test."""
        offer = Offer.objects.create(name='p')
        a = session('REPEATABLE READ')
        a.run('SELECT 1')
        Tier.objects.create(offer=offer)

        a.run(ARCHIVE_OFFER, offer.pk)
        a.conn.commit()

        assert _stamp(Offer, offer.pk) is not None
        assert _tiers(offer) == [None]

    def test_serializable_sessions_fail_rather_than_miss_it(self, session):
        """The insert's guard reads the parent the archive writes, and the arm reads the children
        the insert wrote: two edges between the sessions, a cycle, so one is refused."""
        offer = Offer.objects.create(name='p')
        a, b = session('SERIALIZABLE'), session('SERIALIZABLE')
        a.run('SELECT 1')
        b.run(INSERT_TIER, offer.pk)
        b.conn.commit()

        with pytest.raises(psycopg.errors.SerializationFailure):
            a.run(ARCHIVE_OFFER, offer.pk)
            a.conn.commit()

    def test_a_repeatable_read_insert_under_an_archive_fails(self, session):
        offer = Offer.objects.create(name='p')
        a, b = session(), session('REPEATABLE READ')
        b.run('SELECT 1')
        a.run(ARCHIVE_OFFER, offer.pk)
        a.conn.commit()

        with pytest.raises(psycopg.errors.SerializationFailure):
            b.run(INSERT_TIER, offer.pk)
