"""What ``hard_delete()`` costs in statements (PR 4 of #55): one switch for the whole walk; a
self-referential cascade by one recursive query; an owned fixpoint reading each owner once.
Counted, never timed, and always to the end state the per-table version reached."""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from tests.testapp.models import (
    Clause,
    Offer,
    QuantityCondition,
    Setlist,
    SetlistEntry,
    Tier,
)


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


def _chain(depth: int) -> Setlist:
    """A tree *depth* levels deep, each level with one entry; returns the root."""
    root = node = Setlist.objects.create(title='root')
    SetlistEntry.objects.create(song='root-song', setlist=node)
    for level in range(1, depth):
        node = Setlist.objects.create(title=f'level-{level}', parent=node)
        SetlistEntry.objects.create(song=f'song-{level}', setlist=node)
    return root


@pytest.mark.django_db
class TestASelfReferentialCascade:
    """One level a query, until the walk asked for the whole subtree at once."""

    def test_the_cost_does_not_grow_with_the_depth(self):
        counts = {}
        for depth in (2, 8, 20):
            root = _chain(depth)
            counts[depth] = len(statements(root.hard_delete))

        assert len(set(counts.values())) == 1, counts

    def test_the_whole_subtree_and_its_entries_are_removed(self):
        root = _chain(8)
        other = Setlist.objects.create(title='untouched')

        root.hard_delete()

        assert Setlist._all_objects.filter(title__startswith='level-').count() == 0
        assert not Setlist._all_objects.filter(title='root').exists()
        assert SetlistEntry._all_objects.count() == 0
        assert Setlist._all_objects.filter(pk=other.pk).exists()

    def test_the_cost_does_not_grow_with_the_breadth_either(self):
        counts = []
        for width in (3, 30):
            root = Setlist.objects.create(title=f'root-{width}')
            for number in range(width):
                Setlist.objects.create(title=f'child-{width}-{number}', parent=root)
            counts.append(len(statements(root.hard_delete)))

        assert counts[0] == counts[1]

    def test_a_cycle_in_the_data_terminates(self):
        """A parent cycle cannot be written through the ORM's own guards, but a raw ``UPDATE``
        can; ``UNION`` rather than ``UNION ALL`` is what stops the recursion."""
        first = Setlist.objects.create(title='a')
        second = Setlist.objects.create(title='b', parent=first)
        Setlist._all_objects.filter(pk=first.pk).update(parent=second)
        with connection.cursor() as cursor:  # a ``UNION ALL`` would recurse for ever: fail, not hang
            cursor.execute("SET LOCAL statement_timeout = '5s'")

        first.hard_delete()

        assert Setlist._all_objects.count() == 0

    def test_a_mid_tree_node_takes_only_its_own_subtree(self):
        root = _chain(6)
        middle = Setlist.objects.get(title='level-2')

        middle.hard_delete()

        remaining = set(Setlist._all_objects.values_list('title', flat=True))
        assert remaining == {'root', 'level-1'}


@pytest.mark.django_db
class TestSeveralSelfReferentialKeys:
    def test_a_subtree_reached_through_either_key_goes(self):
        from tests.testapp.models import Ledger  # noqa: PLC0415

        root = Ledger.objects.create(name='root')
        under = Ledger.objects.create(name='under', parent=root)
        mirrored = Ledger.objects.create(name='mirrored', mirror=under)
        deep = Ledger.objects.create(name='deep', parent=mirrored)
        other = Ledger.objects.create(name='other')

        root.hard_delete()

        assert set(Ledger._all_objects.values_list('name', flat=True)) == {'other'}
        assert deep and other

    def test_the_cost_does_not_grow_with_the_depth(self):
        from tests.testapp.models import Ledger  # noqa: PLC0415

        counts = []
        for depth in (3, 12):
            root = node = Ledger.objects.create(name=f'root-{depth}')
            for level in range(depth):
                node = Ledger.objects.create(
                    name=f'n-{depth}-{level}', **{('parent' if level % 2 else 'mirror'): node}
                )
            counts.append(len(statements(root.hard_delete)))

        assert counts[0] == counts[1]


def _ledger(depth: int):
    """A ledger tree *depth* levels deep through ``parent``, root first."""
    from tests.testapp.models import Ledger  # noqa: PLC0415

    nodes = [Ledger.objects.create(name='l-0')]
    for level in range(1, depth):
        nodes.append(Ledger.objects.create(name=f'l-{level}', parent=nodes[-1]))
    return nodes


@pytest.mark.django_db
class TestAnOwnedTree:
    """The owned root is removed only if nothing outside still points at it, which reads the
    cascade closure of a self-referential model."""

    def test_the_tree_goes_with_its_last_owner(self):
        from tests.testapp.models import Ledger, Stagecraft  # noqa: PLC0415

        nodes = _ledger(5)
        craft = Stagecraft.objects.create(name='c', ledger=nodes[0])

        craft.hard_delete()

        assert not Ledger._all_objects.exists()
        assert not Stagecraft._all_objects.exists()

    def test_it_is_spared_while_another_owner_remains(self):
        from tests.testapp.models import Ledger, Stagecraft  # noqa: PLC0415

        nodes = _ledger(3)
        first = Stagecraft.objects.create(name='one', ledger=nodes[0])
        Stagecraft.objects.create(name='two', ledger=nodes[0])

        first.hard_delete()

        assert Ledger._all_objects.filter(pk=nodes[0].pk).exists()
        assert Stagecraft._all_objects.count() == 1

    def test_a_row_that_goes_with_the_tree_does_not_hold_the_root_back(self):
        """A cue deep in the tree anchors the owned root with a plain key. It is removed with the
        tree, so it is no reason to spare the root -- which needs the closure to reach it."""
        from tests.testapp.models import Cue, Ledger, Stagecraft  # noqa: PLC0415

        nodes = _ledger(5)
        Cue.objects.create(label='deep', ledger=nodes[3], anchor=nodes[0])
        craft = Stagecraft.objects.create(name='c', ledger=nodes[0])

        craft.hard_delete()

        assert not Ledger._all_objects.exists()
        assert not Cue._all_objects.exists()

    def test_a_cue_outside_the_tree_does_hold_it_back(self):
        from tests.testapp.models import Cue, Ledger, Stagecraft  # noqa: PLC0415

        nodes = _ledger(3)
        outside = Ledger.objects.create(name='outside')
        Cue.objects.create(label='outside', ledger=outside, anchor=nodes[0])
        craft = Stagecraft.objects.create(name='c', ledger=nodes[0])

        craft.hard_delete()

        assert Ledger._all_objects.filter(pk=nodes[0].pk).exists()
