"""The ``delete()`` fast path and ``soft_delete()``: that they engage, what they cost, where they
stand aside. What ``delete()`` returns and leaves behind is pinned in
``tests/test_delete_characterization.py``."""

from __future__ import annotations

import pytest
from asgiref.sync import async_to_sync
from django.db import connection
from django.db.models.signals import pre_delete
from django.test.utils import CaptureQueriesContext

from guitars.models import SoftDeleteUnsupportedError
from guitars.tenancy import tenancy_bypassed, tenant
from tests.testapp.models import (
    Band,
    Clause,
    Condition,
    Genre,
    Offer,
    QuantityCondition,
    Release,
    Signboard,
    Tier,
)


def build(conditions: int):
    offer = Offer.objects.create(name='o')
    tier = Tier.objects.create(offer=offer)
    clause = Clause.objects.create(tier=tier)
    made = [QuantityCondition.objects.create(clause=clause) for _ in range(conditions)]
    return offer, tier, clause, made


def statements(action) -> int:
    """Statements issued, not counting the tenancy layer's ``set_config`` publish."""
    with CaptureQueriesContext(connection) as captured:
        action()
    return sum('set_config' not in query['sql'] for query in captured.captured_queries)


def single_row_reads(action) -> int:
    """The collector's per-row parent read: ``... WHERE id = N LIMIT 21``."""
    with CaptureQueriesContext(connection) as captured:
        action()
    return sum('LIMIT 21' in query['sql'] for query in captured.captured_queries)


def archived(instance) -> bool:
    return type(instance)._all_objects.get(pk=instance.pk)._deleted_at is not None


@pytest.mark.django_db
class TestTheFastPathEngages:
    def test_a_covered_tree_is_one_statement_however_many_children(self, settings):
        settings.GUITARS_DELETE_FAST_PATH = True
        counts = []
        for conditions in (1, 8):
            offer, *_ = build(conditions)
            counts.append(statements(lambda offer=offer: Offer.objects.filter(pk=offer.pk).delete()))

        assert counts == [1, 1]

    def test_the_collector_reads_each_mti_childs_parent_separately(self, settings):
        """Issue #55. Needs ``ConditionNote``: an incoming ``CASCADE`` key onto the child is what
        stops Django fast-deleting it, so the rows load and each parent is fetched one by one."""
        settings.GUITARS_DELETE_FAST_PATH = False
        reads = []
        for conditions in (1, 8):
            offer, *_ = build(conditions)
            reads.append(
                single_row_reads(lambda offer=offer: Offer.objects.filter(pk=offer.pk).delete())
            )

        assert reads == [1, 8]

    def test_the_fast_path_issues_none_of_them(self, settings):
        settings.GUITARS_DELETE_FAST_PATH = True
        offer, *_ = build(8)

        assert single_row_reads(lambda: Offer.objects.filter(pk=offer.pk).delete()) == 0

    def test_an_mti_child_queryset_is_one_statement_too(self, settings):
        settings.GUITARS_DELETE_FAST_PATH = True
        _, _, _, made = build(8)
        pks = [condition.pk for condition in made]

        assert statements(lambda: QuantityCondition.objects.filter(pk__in=pks).delete()) == 1
        assert all(archived(condition) for condition in made)

    def test_an_instance_is_one_statement(self, settings):
        settings.GUITARS_DELETE_FAST_PATH = True
        offer, *_ = build(8)

        assert statements(offer.delete) == 1

    def test_the_setting_turns_it_off(self, settings):
        settings.GUITARS_DELETE_FAST_PATH = False
        offer, *_ = build(2)

        assert statements(lambda: Offer.objects.filter(pk=offer.pk).delete()) > 1


@pytest.mark.django_db
class TestItStandsAside:
    def test_a_model_with_a_many_to_many_is_left_to_the_collector(self, settings):
        settings.GUITARS_DELETE_FAST_PATH = True
        band = Band.objects.create(name='b')
        band.genres.add(Genre.objects.create(name='g'))

        assert statements(lambda: Band.objects.filter(pk=band.pk).delete()) > 1

    def test_a_delete_signal_receiver_connected_at_runtime_is_honoured(self, settings):
        settings.GUITARS_DELETE_FAST_PATH = True
        offer, *_ = build(1)
        assert statements(lambda: Offer.objects.filter(pk=offer.pk).delete()) == 1
        second, *_ = build(1)
        seen = []

        def receiver(sender, instance, **kwargs):
            seen.append(instance)

        pre_delete.connect(receiver, sender=Condition)
        try:
            Offer.objects.filter(pk=second.pk).delete()
        finally:
            pre_delete.disconnect(receiver, sender=Condition)

        assert len(seen) == 1

    def test_an_ancestors_receiver_counts_for_its_descendant(self, settings):
        """The collector sends the signal for the ancestor row too, so a receiver on it is a
        reason to decline for a queryset of the descendant."""
        settings.GUITARS_DELETE_FAST_PATH = True
        _, _, _, made = build(1)
        seen = []

        def receiver(sender, instance, **kwargs):
            seen.append(instance)

        pre_delete.connect(receiver, sender=Condition)
        try:
            QuantityCondition.objects.filter(pk=made[0].pk).delete()
        finally:
            pre_delete.disconnect(receiver, sender=Condition)

        assert len(seen) == 1


def test_a_non_postgresql_alias_never_takes_the_fast_path(settings):
    """The rules are PostgreSQL DDL, so on another backend a ``DELETE`` really deletes."""
    from guitars.models.soft_deletion import _fast_delete_applies  # noqa: PLC0415

    settings.GUITARS_DELETE_FAST_PATH = True

    assert _fast_delete_applies(Offer, 'default') is True
    assert _fast_delete_applies(Offer, 'nonpg') is False


@pytest.mark.django_db
class TestSoftDelete:
    def test_it_returns_the_number_stamped_and_archives_the_tree(self):
        offer, tier, clause, made = build(3)

        stamped = Offer.objects.filter(pk=offer.pk).soft_delete()

        assert stamped == 1
        assert all(archived(row) for row in (offer, tier, clause, *made))

    def test_its_end_state_equals_delete(self, settings):
        settings.GUITARS_DELETE_FAST_PATH = False
        first = build(3)
        second = build(3)

        Offer.objects.filter(pk=first[0].pk).delete()
        Offer.objects.filter(pk=second[0].pk).soft_delete()

        for expected, got in zip((first[0], first[1], first[2], *first[3]), (second[0], second[1], second[2], *second[3]), strict=True):
            assert archived(expected) and archived(got)
        for rows in (first, second):
            stamp = Offer._all_objects.get(pk=rows[0].pk)._deleted_at
            for row in (rows[1], rows[2], *rows[3]):
                assert type(row)._all_objects.get(pk=row.pk)._deleted_at == stamp

    @pytest.mark.parametrize('conditions', [1, 8])
    def test_the_statement_count_does_not_grow_with_the_tree(self, conditions):
        offer, *_ = build(conditions)

        assert statements(lambda: Offer.objects.filter(pk=offer.pk).soft_delete()) == 1

    def test_an_mti_child_queryset_is_a_constant_number_of_statements(self):
        counts = []
        for conditions in (1, 8):
            _, _, _, made = build(conditions)
            pks = [condition.pk for condition in made]
            counts.append(statements(lambda pks=pks: QuantityCondition.objects.filter(pk__in=pks).soft_delete()))

        assert counts[0] == counts[1]

    def test_an_mti_child_queryset_archives_the_root_row(self):
        _, _, _, made = build(2)

        stamped = QuantityCondition.objects.filter(pk=made[0].pk).soft_delete()

        assert stamped == 1
        assert archived(made[0])
        assert not archived(made[1])

    def test_an_empty_queryset_issues_no_statement(self):
        build(1)

        assert statements(lambda: Offer.objects.none().soft_delete()) == 0

    def test_an_already_archived_row_is_not_stamped_again(self):
        offer, *_ = build(1)
        Offer.objects.filter(pk=offer.pk).soft_delete()
        first = Offer._all_objects.get(pk=offer.pk)._deleted_at

        assert Offer._all_objects.filter(pk=offer.pk).soft_delete() == 0
        assert Offer._all_objects.get(pk=offer.pk)._deleted_at == first

    def test_it_never_hard_deletes(self):
        offer, *_ = build(2)

        Offer.objects.filter(pk=offer.pk).soft_delete()

        assert Offer._all_objects.filter(pk=offer.pk).exists()
        assert QuantityCondition._all_objects.count() == 2

    def test_it_is_not_reachable_from_a_manager(self):
        with pytest.raises(AttributeError):
            Offer.objects.soft_delete()

    def test_it_skips_on_delete_and_leaves_plain_children_alone(self):
        band = Band.objects.create(name='b')
        genre = Genre.objects.create(name='g')
        band.genres.add(genre)

        Band.objects.filter(pk=band.pk).soft_delete()

        assert Band.genres.through.objects.count() == 1  # delete() would have removed it
        assert archived(band)

    def test_it_refuses_where_a_rules_only_archive_would_leave_rows_live(self):
        signboard = Signboard.objects.create(caption='c')

        with pytest.raises(SoftDeleteUnsupportedError, match=r'Signboard\.scribbles'):
            Signboard.objects.filter(pk=signboard.pk).soft_delete()

        assert not archived(signboard)

    def test_the_async_twin_does_the_same(self):
        offer, tier, *_ = build(2)

        stamped = async_to_sync(Offer.objects.filter(pk=offer.pk).asoft_delete)()

        assert stamped == 1
        assert archived(tier)


@pytest.mark.django_db
class TestInstanceSoftDelete:
    def test_it_stamps_the_instance_and_keeps_the_pk(self):
        offer, tier, *_ = build(2)

        stamped = offer.soft_delete()

        assert stamped == 1
        assert offer.pk is not None
        assert offer._deleted_at is not None
        assert offer._deleted_at == Offer._all_objects.get(pk=offer.pk)._deleted_at
        assert archived(tier)

    def test_it_sets_updated_at_from_the_database(self):
        offer, *_ = build(1)
        before = offer._updated_at

        offer.soft_delete()

        assert offer._updated_at == Offer._all_objects.get(pk=offer.pk)._updated_at
        assert offer._updated_at is not None
        assert before is not None

    def test_a_second_call_stamps_nothing(self):
        offer, *_ = build(1)
        offer.soft_delete()

        assert offer.soft_delete() == 0

    def test_an_mti_child_instance_archives_the_root_row(self):
        _, _, _, made = build(2)

        assert made[0].soft_delete() == 1
        assert made[0]._deleted_at is not None
        assert not archived(made[1])

    def test_an_unsaved_instance_is_refused(self):
        with pytest.raises(ValueError, match="can't be soft-deleted"):
            Offer(name='x').soft_delete()

    def test_it_refuses_where_a_rules_only_archive_would_leave_rows_live(self):
        signboard = Signboard.objects.create(caption='c')

        with pytest.raises(SoftDeleteUnsupportedError):
            signboard.soft_delete()

    def test_the_async_twin_does_the_same(self):
        offer, tier, *_ = build(1)

        assert async_to_sync(offer.asoft_delete)() == 1
        assert archived(tier)


def archived_anywhere(instance) -> bool:
    """``archived`` for a tenanted row: reading it back unscoped would be denied."""
    with tenancy_bypassed():
        return archived(instance)


@pytest.mark.django_db
class TestTenancy:
    def test_an_unscoped_queryset_is_denied(self, tenants):
        from guitars.tenancy import TenantScopeMissing  # noqa: PLC0415

        with pytest.raises(TenantScopeMissing, match='write needs an active tenant scope'):
            Release.objects.filter(pk=tenants.release_a.pk).soft_delete()

        assert not archived_anywhere(tenants.release_a)

    def test_a_scoped_queryset_archives_only_its_own_tenants_rows(self, tenants):
        with tenant(label=tenants.a):
            stamped = Release.objects.all().soft_delete()

        assert stamped == 1
        assert archived_anywhere(tenants.release_a)
        assert not archived_anywhere(tenants.release_b)

    def test_the_delete_fast_path_follows_the_same_scope(self, tenants, settings):
        settings.GUITARS_DELETE_FAST_PATH = True
        with tenant(label=tenants.a):
            Release.objects.all().delete()

        assert archived_anywhere(tenants.release_a)
        assert not archived_anywhere(tenants.release_b)
