"""The GUC publisher's cache must never skip a publish a rollback undid (fail-open: the
previous frame's tenant stays live), and a savepoint reverting nothing must cost nothing.
Probes read ``current_setting`` through an ordinary cursor, so they pass the publisher first."""

from __future__ import annotations

import pytest
from django.db import DatabaseError, connection, transaction
from django.test.utils import CaptureQueriesContext

from guitars.gucs import guc_name
from guitars.tenancy import tenant
from tests.conftest import scalar


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
# publish made *inside* the block can be reverted by it. Entered via atomic(): a bare
# transaction.savepoint() never reaches connection.savepoint_ids, so nothing could see it.
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


def _set_configs(queries) -> int:
    return sum('set_config' in query['sql'] for query in queries)


@pytest.mark.django_db
class TestASavepointThatRevertsNothingCostsNothing:
    @pytest.mark.parametrize('blocks', [1, 8])
    def test_pushing_and_releasing_savepoints_does_not_republish(self, tenants, blocks):
        with tenant(label=tenants.a):
            scalar('SELECT 1')
            with CaptureQueriesContext(connection) as captured:
                for _ in range(blocks):
                    with transaction.atomic():
                        scalar('SELECT 1')

        assert _set_configs(captured.captured_queries) == 0

    def test_rolling_back_a_savepoint_that_published_nothing_republishes_nothing(self, tenants):
        with tenant(label=tenants.a):
            scalar('SELECT 1')
            with CaptureQueriesContext(connection) as captured:
                with pytest.raises(RuntimeError), transaction.atomic():
                    scalar('SELECT 1')
                    raise RuntimeError
                scalar('SELECT 1')

        assert _set_configs(captured.captured_queries) == 0


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
