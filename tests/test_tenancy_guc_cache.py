"""The GUC publisher's cache must never skip a publish a rollback undid (fail-open: the
previous frame's tenant stays live), and a savepoint reverting nothing must cost nothing.
Probes read ``current_setting`` through an ordinary cursor, so they pass the publisher first."""

from __future__ import annotations

import subprocess
import sys

import pytest
from django.db import DatabaseError, connection, transaction
from django.test.utils import CaptureQueriesContext

from guitars.gucs import guc_name
from guitars.tenancy import guc
from guitars.tenancy import tenant
from guitars.tenancy import tenancy_bypassed
from tests.conftest import execute, scalar
from tests.testapp.models import Label


_LABEL_GUC = guc_name('label')


def _published() -> str | None:
    return scalar('SELECT current_setting(%s, true)', [_LABEL_GUC])


class _Interleaved:
    """Context managers entered and exited by hand, since the rollback must land while a tenant
    scope entered inside the block is still active. Leftovers close last-in-first-out on exit,
    so a red assertion cannot leak a tenant frame into the next test."""

    def __init__(self) -> None:
        self._open: list = []

    def enter(self, manager):
        manager.__enter__()
        self._open.append(manager)
        return manager

    def release(self, manager) -> None:
        self._open.remove(manager)
        manager.__exit__(None, None, None)

    def roll_back(self, block) -> None:
        """Exit an ``atomic()`` *as if* its body raised."""
        self._open.remove(block)
        block.__exit__(RuntimeError, RuntimeError(), None)

    def __enter__(self) -> _Interleaved:
        return self

    def __exit__(self, *exc_info) -> None:
        while self._open:
            self._open.pop().__exit__(None, None, None)


# The publisher runs on the SAVEPOINT statement itself, before the savepoint exists, so only a
# publish made *inside* the block can be reverted by it. These hold through the marker (via
# atomic()) *and* through _distrust, so each mechanism alone satisfies them -- see below.
@pytest.mark.django_db
class TestARevertedPublishIsRepublished:
    def test_rollback_of_the_savepoint_it_was_published_in(self, tenants):
        with tenant(label=tenants.a), _Interleaved() as interleaved:
            block = interleaved.enter(transaction.atomic())
            interleaved.enter(tenant(label=tenants.b))
            assert _published() == str(tenants.b.pk)  # published inside the savepoint
            interleaved.roll_back(block)  # reverts that SET LOCAL to A's

            assert _published() == str(tenants.b.pk)

    def test_rollback_of_an_enclosing_savepoint_after_the_inner_one_was_released(
        self, tenants
    ):
        with tenant(label=tenants.a), _Interleaved() as interleaved:
            outer = interleaved.enter(transaction.atomic())
            inner = interleaved.enter(transaction.atomic())
            interleaved.enter(tenant(label=tenants.b))
            assert _published() == str(tenants.b.pk)
            interleaved.release(inner)  # released: the SET LOCAL survives it
            assert _published() == str(tenants.b.pk)
            interleaved.roll_back(outer)  # ...but not the enclosing rollback

            assert _published() == str(tenants.b.pk)

    def test_rollback_past_a_block_without_a_savepoint(self, tenants):
        with tenant(label=tenants.a), _Interleaved() as interleaved:
            outer = interleaved.enter(transaction.atomic())
            inner = interleaved.enter(transaction.atomic(savepoint=False))
            interleaved.enter(tenant(label=tenants.b))
            assert _published() == str(tenants.b.pk)
            interleaved.release(inner)
            interleaved.roll_back(outer)

            assert _published() == str(tenants.b.pk)



@pytest.mark.django_db
class TestLexicalNestingStaysCorrect:
    """Lexical nesting cannot reach a stale publish, so these do not pin the fingerprint. They pin
    the state compare and the clearing of a dimension no longer in scope, which they alone catch."""

    def test_a_tenant_switched_inside_a_rolled_back_savepoint(self, tenants):
        with tenant(label=tenants.a):
            scalar('SELECT 1')
            with pytest.raises(RuntimeError), transaction.atomic():
                with tenant(label=tenants.b):
                    assert _published() == str(tenants.b.pk)
                assert _published() == str(tenants.a.pk)
                raise RuntimeError

            assert _published() == str(tenants.a.pk)

    def test_a_scope_exited_after_its_savepoint_rolled_back_clears_the_value(self, tenants):
        with tenant(label=tenants.a):
            scalar('SELECT 1')
            with pytest.raises(RuntimeError), transaction.atomic():
                with tenant(label=tenants.b):
                    scalar('SELECT 1')
                raise RuntimeError

        assert _published() in ('', None)


@pytest.mark.django_db
class TestARollbackWithTheScopeStillOpen:
    """The publish for a scope entered inside a savepoint can land on the ``ROLLBACK TO
    SAVEPOINT`` statement itself, which then reverts it -- and the cache recorded it. Found by a
    reviewer; present before this branch, on every path ending in that statement."""

    def test_a_scope_entered_in_the_savepoint_with_no_query_before_the_rollback(
        self, tenants
    ):
        with tenant(label=tenants.a), _Interleaved() as interleaved:
            scalar('SELECT 1')
            block = interleaved.enter(transaction.atomic())
            interleaved.enter(tenant(label=tenants.b))
            interleaved.roll_back(block)

            assert _published() == str(tenants.b.pk)

    def test_the_public_savepoint_api_which_never_reaches_savepoint_ids(self, tenants):
        with tenant(label=tenants.a):
            scalar('SELECT 1')
            sid = transaction.savepoint()
            with tenant(label=tenants.b):
                scalar('SELECT 1')
                transaction.savepoint_rollback(sid)

                assert _published() == str(tenants.b.pk)
            transaction.savepoint_commit(sid)

    def test_a_savepoint_rolled_back_in_raw_sql(self, tenants):
        with tenant(label=tenants.a):
            scalar('SELECT 1')
            execute('SAVEPOINT guitars_probe')
            with tenant(label=tenants.b):
                scalar('SELECT 1')
                execute('ROLLBACK TO SAVEPOINT guitars_probe')

                assert _published() == str(tenants.b.pk)
            execute('RELEASE SAVEPOINT guitars_probe')


@pytest.mark.django_db
class TestEverySpellingOfARollback:
    """Django writes ``ROLLBACK TO SAVEPOINT x``, but a hand-written statement, or a wrapper that
    prefixes a comment to every statement, spells it otherwise -- and PostgreSQL accepts all of it."""

    @pytest.mark.parametrize(
        'statement',
        [
            'ROLLBACK TO SAVEPOINT guitars_probe',
            'rollback to savepoint guitars_probe',
            '  ROLLBACK TO SAVEPOINT guitars_probe',
            '\nROLLBACK TO SAVEPOINT guitars_probe',
            '/* request-id */ ROLLBACK TO SAVEPOINT guitars_probe',
            '-- request-id\nROLLBACK TO SAVEPOINT guitars_probe',
            'ROLLBACK  TO  SAVEPOINT guitars_probe',
            'ROLLBACK\nTO SAVEPOINT guitars_probe',
            'ROLLBACK TO guitars_probe',
            'ROLLBACK WORK TO SAVEPOINT guitars_probe',
            'ROLLBACK TRANSACTION TO SAVEPOINT guitars_probe',
            b'ROLLBACK TO SAVEPOINT guitars_probe',
            'ROLLBACK /* x */ TO SAVEPOINT guitars_probe',
            '/* a /* nested */ still a comment */ ROLLBACK TO SAVEPOINT guitars_probe',
            '/* one */ -- two\n/* three */ ROLLBACK WORK /* four */ TO guitars_probe',
        ],
        ids=lambda value: repr(value)[:40],
    )
    def test_the_next_statement_republishes(self, tenants, statement):
        with tenant(label=tenants.a):
            scalar('SELECT 1')
            execute('SAVEPOINT guitars_probe')
            with tenant(label=tenants.b):
                scalar('SELECT 1')
                execute(statement)

                assert _published() == str(tenants.b.pk)
            execute('RELEASE SAVEPOINT guitars_probe')

    def test_a_statement_that_merely_mentions_one_costs_nothing(self, tenants):
        with tenant(label=tenants.a):
            scalar('SELECT 1')
            with CaptureQueriesContext(connection) as captured:
                scalar("SELECT 'ROLLBACK TO SAVEPOINT x'")

        assert _set_configs(captured.captured_queries) == 0


class TestTheMatcherNeverBacktracks:
    """It runs on every statement the wrapper sees, so a pattern that backtracks hangs the
    connection. A catastrophic one holds the GIL, so it is timed in a subprocess."""

    @pytest.mark.parametrize('shape', ['leading', 'later', 'unclosed', 'nested'])
    def test_many_comments_cost_next_to_nothing(self, shape):
        code = (
            'from guitars.tenancy.guc import _reverts_a_set\n'
            "sql = {'leading': '/* c */ ' * 2000 + 'SELECT 1',"
            " 'later': '/* a */ SELECT 1 ' + '/* c */ ' * 2000,"
            " 'unclosed': '/* ' + 'x ' * 20000,"
            " 'nested': '/* ' * 2000 + '*/ ' * 2000 + 'SELECT 1'}[" + repr(shape) + "]\n"
            'print(_reverts_a_set(sql))'
        )
        done = subprocess.run(
            [sys.executable, '-c', code], capture_output=True, text=True, timeout=20, check=False
        )

        assert done.stdout.strip() == 'False', done.stderr[-300:]


@pytest.mark.django_db
class TestRollbackThroughOtherDoors:
    def test_a_rollback_later_in_a_multi_statement_string(self, tenants):
        with tenant(label=tenants.a):
            scalar('SELECT 1')
            execute('SAVEPOINT guitars_probe')
            with tenant(label=tenants.b):
                scalar('SELECT 1')
                execute('SELECT 1; ROLLBACK TO SAVEPOINT guitars_probe')

                assert _published() == str(tenants.b.pk)
            execute('RELEASE SAVEPOINT guitars_probe')

    def test_rollback_and_chain_which_keeps_the_transaction_open(self, tenants):
        """It undoes every ``SET LOCAL`` while Django still sees the same transaction. Run in the
        test's own wrapping transaction: inside an atomic() it would destroy that block's
        savepoint and fail its RELEASE, which is a different problem."""
        with tenant(label=tenants.a):
            scalar('SELECT 1')
            execute('ROLLBACK AND CHAIN')

            assert _published() == str(tenants.a.pk)


@pytest.mark.django_db
class TestWhatIsNotARollback:
    @pytest.mark.parametrize(
        'statement',
        ['', '   ', '-- only a comment', '/* only a comment */', '/* unterminated', 'SELECT 1',
         'BEGIN', "SELECT 'ROLLBACK TO x'", 'ROLLBACKTO x', 'ROLLBACK PREPARED \'gid\'',
         'COMMIT PREPARED \'gid\'', 'SELECT 1; SELECT 2', 'SELECT 1;'],
    )
    def test_it_is_left_alone(self, statement):
        """Only the exact statement shape counts: a name that merely starts with ROLLBACK, or one
        quoted inside another statement, must not cost a republish."""
        assert guc._reverts_a_set(statement) is False

    @pytest.mark.parametrize(
        'statement',
        ['ROLLBACK', 'ROLLBACK;', 'ABORT', 'ROLLBACK AND CHAIN', 'abort and chain', 'COMMIT',
         'COMMIT AND CHAIN', 'END', 'ROLLBACK TO SAVEPOINT p', 'SELECT 1; ROLLBACK TO SAVEPOINT p',
         '; ROLLBACK TO SAVEPOINT p', "SET LOCAL x.y = 1; ROLLBACK TO SAVEPOINT p"],
    )
    def test_anything_that_ends_the_transaction_or_its_savepoint_counts(self, statement):
        """Each reverts every ``SET LOCAL``, ``AND CHAIN`` and a bare ``ROLLBACK`` included."""
        assert guc._reverts_a_set(statement) is True

    def test_a_driver_object_is_not_inspected(self):
        """A ``psycopg.sql.Composed`` cannot be read without a connection; it is documented, not
        guessed at, and must not raise from the wrapper's ``finally``."""
        assert guc._reverts_a_set(object()) is False
        assert guc._reverts_a_set(None) is False


def _set_configs(queries) -> int:
    return sum('set_config' in query['sql'] for query in queries)


@pytest.mark.django_db
class TestWhatASavepointCosts:
    @pytest.mark.parametrize('blocks', [1, 8])
    def test_pushing_and_releasing_savepoints_does_not_republish(self, tenants, blocks):
        with tenant(label=tenants.a):
            scalar('SELECT 1')
            with CaptureQueriesContext(connection) as captured:
                for _ in range(blocks):
                    with transaction.atomic():
                        scalar('SELECT 1')

        assert _set_configs(captured.captured_queries) == 0

    def test_a_rolled_back_savepoint_costs_exactly_one_republish(self, tenants):
        """The price of distrusting the cache after ``ROLLBACK TO SAVEPOINT``, which can revert a
        publish it recorded. Rollbacks are the exception; push and release stay free."""
        with tenant(label=tenants.a):
            scalar('SELECT 1')
            with CaptureQueriesContext(connection) as captured:
                with pytest.raises(RuntimeError), transaction.atomic():
                    scalar('SELECT 1')
                    raise RuntimeError
                scalar('SELECT 1')

        assert _set_configs(captured.captured_queries) == 1


@pytest.mark.django_db
class TestARecoveryStatementOnAnAbortedTransaction:
    def test_a_changed_frame_does_not_wedge_the_savepoint_rollback(self, tenants):
        """The publisher runs before ``ROLLBACK TO SAVEPOINT``, which an aborted transaction
        refuses too. Reached only when the frame changed since the last publish (B published,
        statement failed, scope exited): the publish must be skipped, not raised."""
        with tenant(label=tenants.a):
            scalar('SELECT 1')
            with pytest.raises(DatabaseError), transaction.atomic():
                with tenant(label=tenants.b):
                    scalar('SELECT 1 / 0')

            assert _published() == str(tenants.a.pk)  # and the next block republished A


def _setting(name: str) -> str | None:
    return scalar('SELECT current_setting(%s, true)', [name])


@pytest.mark.django_db
class TestADistrustedCacheStillClearsWhatItForgot:
    """A rollback can restore a dimension the cache has since forgotten, so the republish after one
    has to clear every dimension ever published, not only the last frame's."""

    def test_a_scope_closed_inside_the_savepoint_which_then_rolls_back(self, tenants):
        with tenant(label=tenants.a), _Interleaved() as interleaved:
            scope = interleaved.enter(tenant(other='X'))
            scalar('SELECT 1')  # `other` published outside the savepoint
            block = interleaved.enter(transaction.atomic())
            interleaved.release(scope)
            scalar('SELECT 1')  # republished with `other` cleared, inside the savepoint
            interleaved.roll_back(block)  # ...which the rollback undoes

            assert _setting('tenant.other') in ('', None)

    def test_with_no_statement_between_the_rollback_and_the_scope_exit(self, tenants):
        with tenant(label=tenants.a):
            with tenant(other='X'):
                scalar('SELECT 1')
                sid = transaction.savepoint()
                scalar('SELECT 1')
                transaction.savepoint_rollback(sid)

            assert _setting('tenant.other') in ('', None)
            transaction.savepoint_commit(sid)

    @pytest.mark.parametrize('cache', ['absent', 'none'])
    def test_a_connection_that_never_published_is_left_alone(self, db, cache):
        """It runs from a ``finally``, so raising would replace the statement's own error. A
        connection that never published has no attribute at all (``install_on`` deletes it)."""
        if cache == 'none':
            setattr(connection, guc._CACHE, None)
        elif hasattr(connection, guc._CACHE):
            delattr(connection, guc._CACHE)

        guc._distrust(connection)

        assert getattr(connection, guc._CACHE, None) is None


class TestASessionLevelPublishOutlivesATransactionLocalClear:
    """Outside a transaction a publish is session-level. Inside one, clearing it is only
    transaction-local, so the old value returns when the transaction ends -- and the cache, which
    stores the desired state, had forgotten the dimension it cleared."""

    @pytest.mark.parametrize('outcome', ['commit', 'rollback'])
    def test_a_dimension_cleared_inside_a_block_stays_cleared_after_it(
        self, transactional_db, outcome
    ):
        with tenancy_bypassed():
            label = Label.objects.create(name='Session Records')
        with tenant(label=label):
            scalar('SELECT 1')  # autocommit: published at session level

        try:
            with transaction.atomic():
                assert _published() in ('', None)  # the block clears it, locally
                if outcome == 'rollback':
                    raise RuntimeError
        except RuntimeError:
            pass

        assert _published() in ('', None)

    def test_a_publish_inside_a_transaction_does_not_outlive_it(self, transactional_db):
        """Read through the driver, past the publisher, which would otherwise republish."""
        with tenancy_bypassed():
            label = Label.objects.create(name='Local Records')
        with tenant(label=label), transaction.atomic():
            scalar('SELECT 1')

        with connection.connection.cursor() as raw:
            raw.execute("SELECT current_setting('tenant.label', true)")
            assert raw.fetchone()[0] in ('', None)

    def test_a_long_transaction_keeps_one_marker(self, tenants):
        before = len(connection.run_on_commit)
        for label in (tenants.a, tenants.b) * 5:
            with tenant(label=label):
                scalar('SELECT 1')

        assert len(connection.run_on_commit) - before <= 1


def test_a_later_transaction_republishes(transactional_db):
    """The marker's own job: a commit reverts every ``SET LOCAL``, and nothing in the fingerprint
    differs between two sibling blocks."""
    with tenancy_bypassed():
        label = Label.objects.create(name='Sibling Records')

    with tenant(label=label):
        with transaction.atomic():
            assert _published() == str(label.pk)
        with transaction.atomic():
            assert _published() == str(label.pk)
