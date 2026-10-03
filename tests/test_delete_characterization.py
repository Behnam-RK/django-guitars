"""What ``.delete()`` does on a soft-deletable model, pinned so the fast path can't change it.
Every test runs with ``GUITARS_DELETE_FAST_PATH`` on and off: a transparent optimisation must
leave the return value, the instance, the signals, the guards and the stamps exactly as they were."""

from __future__ import annotations

import pytest
from django.apps import apps
from django.db.models.signals import post_delete, pre_delete
from zeal import zeal_ignore

from tests.testapp.models import (
    Album,
    Band,
    Clause,
    Condition,
    DiscountReward,
    GiftReward,
    Genre,
    Offer,
    QuantityCondition,
    Setlist,
    SetlistEntry,
    Reward,
    ShippingReward,
    Tier,
)


@pytest.fixture(autouse=True, params=[True, False], ids=['fastpath', 'collector'])
def fast_path(request, settings):
    settings.GUITARS_DELETE_FAST_PATH = request.param
    if request.param:
        yield request.param  # still guarded: this is what proves it issues no per-row read
    else:
        with zeal_ignore():  # the collector's per-row parent read is the N+1 #55 is about
            yield request.param


def build_tree(conditions: int = 2, rewards: int = 3):
    offer = Offer.objects.create(name='o')
    tier = Tier.objects.create(offer=offer)
    clause = Clause.objects.create(tier=tier)
    made = [QuantityCondition.objects.create(clause=clause) for _ in range(conditions)]
    kinds = (DiscountReward, ShippingReward, GiftReward)
    prizes = [kinds[number % 3].objects.create(tier=tier) for number in range(rewards)]
    return offer, tier, clause, made, prizes


def archived(instance) -> bool:
    return type(instance)._all_objects.get(pk=instance.pk)._deleted_at is not None


def everything(tree):
    offer, tier, clause, conditions, rewards = tree
    return [offer, tier, clause, *conditions, *rewards]


@pytest.mark.django_db
class TestQuerysetDelete:
    def test_it_returns_no_counts_and_archives_the_whole_tree(self):
        tree = build_tree()

        result = Offer.objects.filter(pk=tree[0].pk).delete()

        assert result == (0, {})
        assert all(archived(row) for row in everything(tree))

    def test_a_child_queryset_archives_the_root_row(self):
        tree = build_tree()
        condition = tree[3][0]

        result = QuantityCondition.objects.filter(pk=condition.pk).delete()

        assert result == (0, {})
        assert archived(condition)
        assert Condition._all_objects.get(pk=condition.pk)._deleted_at is not None
        assert not archived(tree[3][1])  # the sibling is untouched

    def test_a_root_queryset_leaves_the_children_readable_through_all_objects(self):
        tree = build_tree()

        Reward.objects.filter(tier=tree[1]).delete()

        assert all(archived(reward) for reward in tree[4])
        assert DiscountReward._all_objects.filter(pk__in=[r.pk for r in tree[4]]).exists()

    def test_an_empty_queryset_is_a_no_op(self):
        tree = build_tree()

        assert Offer.objects.filter(pk=-1).delete() == (0, {})
        assert Offer.objects.none().delete() == (0, {})
        assert not archived(tree[0])

    def test_an_already_archived_row_keeps_its_stamp(self):
        tree = build_tree()
        Offer.objects.filter(pk=tree[0].pk).delete()
        first = Offer._all_objects.get(pk=tree[0].pk)._deleted_at

        Offer._all_objects.filter(pk=tree[0].pk).delete()

        assert Offer._all_objects.get(pk=tree[0].pk)._deleted_at == first

    def test_a_child_carries_its_parents_stamp(self):
        tree = build_tree()

        Offer.objects.filter(pk=tree[0].pk).delete()

        stamp = Offer._all_objects.get(pk=tree[0].pk)._deleted_at
        for row in everything(tree)[1:]:
            assert type(row)._all_objects.get(pk=row.pk)._deleted_at == stamp

    def test_it_clears_the_result_cache(self):
        tree = build_tree()
        queryset = Offer.objects.filter(pk=tree[0].pk)
        list(queryset)

        queryset.delete()

        assert queryset._result_cache is None


@pytest.mark.django_db
class TestInstanceDelete:
    def test_it_returns_no_counts_and_clears_the_pk(self):
        tree = build_tree()
        offer = tree[0]
        pk = offer.pk

        result = offer.delete()

        assert result == (0, {})
        assert offer.pk is None
        assert Offer._all_objects.get(pk=pk)._deleted_at is not None
        assert all(archived(row) for row in everything(tree)[1:])

    def test_an_mti_child_clears_only_its_own_pk_attname(self):
        tree = build_tree()
        condition = tree[3][0]
        parent_id = condition.id

        result = condition.delete()

        assert result == (0, {})
        assert condition.pk is None
        assert condition.id == parent_id  # Django leaves the inherited attribute alone
        assert Condition._all_objects.get(pk=parent_id)._deleted_at is not None

    def test_an_unsaved_instance_is_refused(self):
        with pytest.raises(ValueError, match="can't be deleted because its id attribute"):
            Offer(name='x').delete()

    def test_keep_parents_is_honoured(self):
        tree = build_tree()
        condition = tree[3][0]

        condition.delete(keep_parents=True)

        assert Condition._all_objects.get(pk=condition.id)._deleted_at is not None


@pytest.mark.django_db
class TestTheGuardsAreDjangos:
    """``.update()`` raises differently, or not at all, on each of these."""

    def test_sliced(self):
        with pytest.raises(TypeError, match="Cannot use 'limit' or 'offset' with delete"):
            Offer.objects.all()[:1].delete()

    def test_distinct_on_fields(self):
        with pytest.raises(TypeError, match=r'Cannot call delete\(\) after .distinct'):
            Offer.objects.distinct('name').delete()

    def test_values(self):
        with pytest.raises(TypeError, match=r'Cannot call delete\(\) after .values'):
            Offer.objects.values('name').delete()

    def test_values_list(self):
        with pytest.raises(TypeError, match=r'Cannot call delete\(\) after .values'):
            Offer.objects.values_list('name').delete()

    def test_combined(self):
        from django.db import NotSupportedError  # noqa: PLC0415

        with pytest.raises(NotSupportedError):
            Offer.objects.all().union(Offer.objects.all()).delete()

    def test_plain_distinct_and_select_related_are_allowed(self):
        tree = build_tree()

        assert Offer.objects.distinct().filter(pk=tree[0].pk).delete() == (0, {})
        assert Tier.objects.select_related('offer').filter(pk=tree[1].pk).delete() == (0, {})


@pytest.mark.django_db
class TestWhatOnlyTheCollectorDoes:
    """Edges no rule covers, so the fast path must decline and these keep happening."""

    def test_set_null_is_applied(self):
        band = Band.objects.create(name='producer')
        album = Album.objects.create(title='x', band=Band.objects.create(name='b'), producer=band)

        band.delete()

        assert Album.objects.get(pk=album.pk).producer_id is None

    def test_a_plain_many_to_many_through_row_is_removed(self):
        band = Band.objects.create(name='b')
        genre = Genre.objects.create(name='g')
        band.genres.add(genre)
        through = Band.genres.through

        band.delete()

        assert through.objects.filter(band_id=band.pk if band.pk else 0).count() == 0
        assert through.objects.count() == 0

    def test_signals_are_sent_for_every_collected_row(self):
        tree = build_tree(conditions=1, rewards=0)
        seen: list[str] = []

        def receiver(sender, instance, **kwargs):
            seen.append(sender.__name__)

        pre_delete.connect(receiver, sender=Tier)
        post_delete.connect(receiver, sender=Tier)
        try:
            Offer.objects.filter(pk=tree[0].pk).delete()
        finally:
            pre_delete.disconnect(receiver, sender=Tier)
            post_delete.disconnect(receiver, sender=Tier)

        assert seen == ['Tier', 'Tier']


@pytest.mark.django_db
class TestEveryRowsUpdatedAtMovesAsItDid:
    """A trigger's stamp is observable and a shortcut can lose it (a self-referential tree's
    lower levels run at trigger depth 1). Compared between settings, so any shape that differs fails."""

    @staticmethod
    def _moved(settings, fast: bool, build_rows, act):
        settings.GUITARS_DELETE_FAST_PATH = fast
        rows = build_rows()
        before = {(type(r).__name__, r.pk): type(r)._all_objects.get(pk=r.pk)._updated_at for r in rows}
        act(rows)
        return {
            key: _row(key)._updated_at != stamp
            for key, stamp in before.items()
        }

    def test_the_offer_tree(self, settings):
        def rows():
            return everything(build_tree())

        def act(made):
            Offer.objects.filter(pk=made[0].pk).delete()

        assert list(self._moved(settings, True, rows, act).values()) == list(
            self._moved(settings, False, rows, act).values()
        )

    def test_a_self_referential_tree_with_children(self, settings):
        def rows():
            root = Setlist.objects.create(title='root')
            middle = Setlist.objects.create(title='middle', parent=root)
            leaf = Setlist.objects.create(title='leaf', parent=middle)
            entries = [
                SetlistEntry.objects.create(song=node.title, setlist=node)
                for node in (root, middle, leaf)
            ]
            return [root, middle, leaf, *entries]

        def act(made):
            Setlist.objects.filter(pk=made[0].pk).delete()

        assert list(self._moved(settings, True, rows, act).values()) == list(
            self._moved(settings, False, rows, act).values()
        )


def _row(key):
    name, pk = key
    model = {m.__name__: m for m in apps.get_models()}[name]
    return model._all_objects.get(pk=pk)
