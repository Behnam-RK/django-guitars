"""What ``hard_delete()`` costs in statements (PR 4 of #55): one switch for the whole walk; a
self-referential cascade by one recursive query; an owned fixpoint reading each owner once.
Counted, never timed, and always to the end state the per-table version reached."""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from tests.testapp.models import Clause, Offer, QuantityCondition, Tier


def statements(action) -> list[str]:
    with CaptureQueriesContext(connection) as captured:
        action()
    return [query['sql'] for query in captured.captured_queries]


def _switches(sql: list[str]) -> int:
    return sum('set_config' in statement for statement in sql)


def _tree(conditions: int = 2):
    offer = Offer.objects.create(name='o')
    tier = Tier.objects.create(offer=offer)
    clause = Clause.objects.create(tier=tier)
    made = [QuantityCondition.objects.create(clause=clause) for _ in range(conditions)]
    return offer, tier, clause, made


@pytest.mark.django_db
class TestTheSwitch:
    def test_one_on_and_one_off_for_the_whole_walk(self):
        offer, *_ = _tree()

        sql = statements(offer.hard_delete)

        assert _switches(sql) == 2

    def test_the_order_is_on_every_delete_then_off(self):
        offer, *_ = _tree()

        sql = statements(offer.hard_delete)
        on, off = (
            next(i for i, s in enumerate(sql) if "'on'" in s),
            next(i for i, s in enumerate(sql) if "'off'" in s),
        )
        hard = [i for i, s in enumerate(sql) if s.startswith('DELETE') and 'IN (' in s]

        assert hard and all(on < i < off for i in hard)

    def test_it_is_not_left_on_after_the_walk(self):
        offer, *_ = _tree()

        offer.hard_delete()

        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('rules.hard_deletion', true)")
            assert cursor.fetchone()[0] in (None, '', 'off')


@pytest.mark.django_db(transaction=True)
def test_a_failure_part_way_leaves_nothing_removed_and_the_switch_off(monkeypatch):
    """The second table's delete fails, after the first has run under the switch: the whole walk
    rolls back, and a switch left on would let the next rule-bypassing delete through."""
    from django.db.backends.utils import CursorWrapper  # noqa: PLC0415

    offer, *_ = _tree()
    pk = offer.pk  # Phase 1 clears it on the instance, rolled back or not
    deletes = []
    real = CursorWrapper.execute

    def failing(self, sql, params=None):
        if isinstance(sql, str) and sql.strip().upper().startswith('DELETE FROM'):
            deletes.append(sql)
            if len(deletes) == 2:
                raise RuntimeError('simulated failure on the second table')
        return real(self, sql, params)

    monkeypatch.setattr(CursorWrapper, 'execute', failing)

    with pytest.raises(RuntimeError, match='second table'):
        offer.hard_delete()

    monkeypatch.undo()
    assert Offer._all_objects.filter(pk=pk).exists()
    assert Tier._all_objects.filter(offer_id=pk).exists()
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('rules.hard_deletion', true)")
        assert cursor.fetchone()[0] in (None, '', 'off')


@pytest.mark.django_db
def test_the_walk_costs_a_delete_a_table_not_five_statements():
    """Was 34 for this tree, five a table; the one savepoint left is the walk's own atomic."""
    offer, *_ = _tree()

    sql = statements(offer.hard_delete)

    assert sum(statement.startswith('SAVEPOINT') for statement in sql) == 1
    assert len(sql) <= 16
