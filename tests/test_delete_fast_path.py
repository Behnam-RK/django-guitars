"""The ``delete()`` fast path and ``soft_delete()``: that they engage, what they cost, where they
stand aside. What ``delete()`` returns and leaves behind is pinned in
``tests/test_delete_characterization.py``."""

from __future__ import annotations

import pytest
from asgiref.sync import async_to_sync
from django.db import connection
from django.db.models import Count, Exists, F, OuterRef, Window
from django.db.models.functions import RowNumber
from django.db.models.signals import post_delete, pre_delete
from django.test.utils import CaptureQueriesContext, isolate_apps

from zeal import zeal_ignore

from guitars.models import SoftDeleteUnsupportedError
from guitars.tenancy import tenancy_bypassed, tenant
from tests.conftest import scalar
from tests.testapp.models import (
    Band,
    Clause,
    Condition,
    ConditionNote,
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
    def test_a_covered_tree_costs_the_same_however_many_children(self, settings):
        settings.GUITARS_DELETE_FAST_PATH = True
        counts = []
        for conditions in (1, 8):
            offer, *_ = build(conditions)
            counts.append(statements(lambda offer=offer: Offer.objects.filter(pk=offer.pk).delete()))

        assert counts == [2, 2]  # the keys, then the delete

    def test_the_collector_reads_each_mti_childs_parent_separately(self, settings):
        """Issue #55. Needs ``ConditionNote``: an incoming ``CASCADE`` key onto the child is what
        stops Django fast-deleting it, so the rows load and each parent is fetched one by one."""
        settings.GUITARS_DELETE_FAST_PATH = False
        reads = []
        for conditions in (1, 8):
            offer, *_ = build(conditions)
            with zeal_ignore():  # the guard flags this very read, which is what is measured
                reads.append(
                    single_row_reads(lambda offer=offer: Offer.objects.filter(pk=offer.pk).delete())
                )

        assert reads == [1, 8]

    def test_the_fast_path_issues_none_of_them(self, settings):
        settings.GUITARS_DELETE_FAST_PATH = True
        offer, *_ = build(8)

        assert single_row_reads(lambda: Offer.objects.filter(pk=offer.pk).delete()) == 0

    def test_an_mti_child_queryset_costs_the_same_too(self, settings):
        settings.GUITARS_DELETE_FAST_PATH = True
        _, _, _, made = build(8)
        pks = [condition.pk for condition in made]

        assert statements(lambda: QuantityCondition.objects.filter(pk__in=pks).delete()) == 2
        assert all(archived(condition) for condition in made)

    def test_an_instance_is_one_statement(self, settings):
        settings.GUITARS_DELETE_FAST_PATH = True
        offer, *_ = build(8)

        assert statements(offer.delete) == 1

    def test_the_setting_turns_it_off(self, settings):
        settings.GUITARS_DELETE_FAST_PATH = False
        offer, *_ = build(2)

        with zeal_ignore():
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
        assert statements(lambda: Offer.objects.filter(pk=offer.pk).delete()) == 2
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

    def test_a_post_delete_only_receiver_is_honoured(self, settings):
        """Only ``post_delete`` is connected: the check must ask both signals, not just the first."""
        settings.GUITARS_DELETE_FAST_PATH = True
        offer, *_ = build(1)
        seen = []

        def receiver(sender, instance, **kwargs):
            seen.append(sender.__name__)

        post_delete.connect(receiver, sender=Tier)
        try:
            Offer.objects.filter(pk=offer.pk).delete()
        finally:
            post_delete.disconnect(receiver, sender=Tier)

        assert seen == ['Tier']

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


@pytest.mark.django_db
class TestAReceiverOnAProxy:
    """The collector sends a signal with the class of the instances it loaded, so a receiver
    connected for a proxy of the model being deleted is as much a reason to decline as one on
    the model itself -- and the registry walk resolves a proxy to its concrete model."""

    @staticmethod
    @isolate_apps('tests.testapp')
    def _delete_through_a_proxy(how, settings):
        settings.GUITARS_DELETE_FAST_PATH = True

        class OfferProxy(Offer):
            class Meta:
                proxy = True
                app_label = 'testapp'

        offer, *_ = build(1)
        seen: list[str] = []

        def receiver(sender, instance, **kwargs):
            seen.append(sender.__name__)

        pre_delete.connect(receiver, sender=OfferProxy)
        try:
            if how == 'queryset':
                OfferProxy.objects.filter(pk=offer.pk).delete()
            else:
                OfferProxy.objects.get(pk=offer.pk).delete()
        finally:
            pre_delete.disconnect(receiver, sender=OfferProxy)
        return seen

    @pytest.mark.parametrize('how', ['queryset', 'instance'])
    def test_it_is_called(self, settings, how):
        assert self._delete_through_a_proxy(how, settings) == ['OfferProxy']


class _ReadElsewhere:
    """Reads go to the non-PostgreSQL alias, writes to ``default``: the backend that matters to
    ``soft_delete()`` and ``delete()`` is the one they write to."""

    def db_for_read(self, model, **hints):
        return 'nonpg'

    def db_for_write(self, model, **hints):
        return 'default'


@pytest.mark.django_db
class TestTheBackendIsTheOneWrittenTo:
    @pytest.fixture(autouse=True)
    def _route_reads_elsewhere(self, settings):
        settings.DATABASE_ROUTERS = ['tests.test_delete_fast_path._ReadElsewhere']

    def test_soft_delete_is_allowed(self):
        offer, *_ = build(1)

        assert Offer.objects.filter(pk=offer.pk).soft_delete() == 1

    def test_the_delete_fast_path_still_engages(self, settings):
        settings.GUITARS_DELETE_FAST_PATH = True
        offer, *_ = build(1)

        assert statements(lambda: Offer.objects.filter(pk=offer.pk).delete()) == 2


@pytest.mark.django_db(databases=['default', 'secondary'])
@pytest.mark.parametrize('how', ['from_state', 'explicit'])
def test_an_instance_is_deleted_on_the_alias_it_is_on(settings, how):
    settings.GUITARS_DELETE_FAST_PATH = True
    offer = Offer.objects.using('secondary').create(name='elsewhere')
    pk = offer.pk  # delete() clears it on the instance

    if how == 'from_state':
        offer.delete()
    else:
        Offer.objects.using('secondary').get(pk=pk).delete(using='secondary')

    assert Offer._all_objects.using('secondary').get(pk=pk)._deleted_at is not None


def test_soft_delete_refuses_a_non_postgresql_alias():
    """The rules are PostgreSQL DDL: elsewhere ``soft_delete()`` would stamp the matched rows and
    cascade nothing. Raised before any query, so the alias needs no tables."""
    with pytest.raises(SoftDeleteUnsupportedError, match='PostgreSQL'):
        Offer.objects.using('nonpg').all().soft_delete()


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

        with zeal_ignore():
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

        assert statements(lambda: Offer.objects.filter(pk=offer.pk).soft_delete()) == 2

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


def _shape_exists(offer):
    return Offer.objects.filter(Exists(Tier.objects.filter(offer=OuterRef('pk'))), pk=offer.pk)


def _shape_pk_in(offer):
    return Offer.objects.filter(pk__in=Tier.objects.values('offer')).filter(pk=offer.pk)


def _shape_joined_live(offer):
    return Offer.objects.filter(tiers___deleted_at__isnull=True, pk=offer.pk)


@pytest.mark.django_db
class TestAFilterThatReadsWhatTheRulesChange:
    """A rule's cascade runs BEFORE the statement that fired it, so a ``WHERE`` reading a table the
    cascade modifies is re-evaluated against rows already archived, and the parent is skipped."""

    @pytest.mark.parametrize('shape', [_shape_exists, _shape_pk_in, _shape_joined_live])
    def test_delete_archives_the_parent_as_well(self, settings, shape):
        settings.GUITARS_DELETE_FAST_PATH = True
        offer, tier, *_ = build(1)

        shape(offer).delete()

        assert archived(offer)
        assert archived(tier)

    @pytest.mark.parametrize('shape', [_shape_exists, _shape_pk_in, _shape_joined_live])
    def test_soft_delete_archives_the_parent_as_well(self, shape):
        offer, tier, *_ = build(1)

        assert shape(offer).soft_delete() == 1

        assert archived(offer)
        assert archived(tier)


@pytest.mark.django_db
class TestAFilterPostgreSQLCannotEvaluateInAWhere:
    """An aggregate or window in the ``WHERE`` makes a bare ``DELETE`` fail. The collector selects
    the keys first, so it never meets the statement; the fast path has to do the same."""

    @staticmethod
    def _aggregate(offer):
        return Offer.objects.annotate(n=Count('id')).filter(n__gte=1, pk=offer.pk)

    @staticmethod
    def _window(offer):
        return Offer.objects.annotate(rn=Window(RowNumber(), order_by=F('pk').asc())).filter(
            rn=1, pk=offer.pk
        )

    @staticmethod
    def _extra_tables(offer):
        return Offer.objects.extra(
            tables=['testapp_tier'], where=['testapp_tier.offer_id = testapp_offer.id']
        ).filter(pk=offer.pk)

    @pytest.mark.parametrize('shape', ['_aggregate', '_window', '_extra_tables'])
    def test_delete_does_not_raise_and_archives(self, settings, shape):
        settings.GUITARS_DELETE_FAST_PATH = True
        offer, *_ = build(1)

        getattr(self, shape)(offer).delete()

        assert archived(offer)

    @pytest.mark.parametrize('shape', ['_aggregate', '_window', '_extra_tables'])
    def test_soft_delete_does_not_raise_and_archives(self, shape):
        offer, *_ = build(1)

        assert getattr(self, shape)(offer).soft_delete() == 1

        assert archived(offer)


@pytest.mark.django_db
class TestTheStampIsTheTransactionsClock:
    """Every rule and trigger writes ``NOW()``, the transaction's start. Django's ``Now()`` renders
    ``STATEMENT_TIMESTAMP()``, so a descendant stamped by a self-cascade or an owned rule would
    not carry the parent's value, and a revive keys on exactly that equality."""

    def test_soft_delete_matches_now_after_time_has_passed(self):
        offer, *_ = build(1)
        scalar('SELECT pg_sleep(0.05)')  # statement_timestamp() moves on; now() does not

        Offer.objects.filter(pk=offer.pk).soft_delete()

        assert Offer._all_objects.get(pk=offer.pk)._deleted_at == scalar('SELECT now()')

    def test_the_instance_form_does_too(self):
        offer, *_ = build(1)
        scalar('SELECT pg_sleep(0.05)')

        offer.soft_delete()

        assert offer._deleted_at == scalar('SELECT now()')


@pytest.mark.django_db
class TestSoftDeleteKeepsDeletesGuards:
    """``soft_delete()`` ignored ``DISTINCT ON`` and stamped every match, where ``.delete()`` raises."""

    def test_distinct_on_fields(self):
        with pytest.raises(TypeError, match=r'soft_delete\(\) after .distinct'):
            Offer.objects.order_by('pk').distinct('name').soft_delete()

    def test_sliced(self):
        with pytest.raises(TypeError, match="'limit' or 'offset' with soft_delete"):
            Offer.objects.all()[:1].soft_delete()

    def test_values(self):
        with pytest.raises(TypeError, match=r'soft_delete\(\) after .values'):
            Offer.objects.values('name').soft_delete()

    def test_combined(self):
        from django.db import NotSupportedError  # noqa: PLC0415

        with pytest.raises(NotSupportedError):
            Offer.objects.all().union(Offer.objects.all()).soft_delete()


@pytest.mark.django_db
class TestALeafInstanceReturnsItsOwnLabel:
    """Django's single-instance shortcut returns ``(count, {label: count})``; the empty dict is
    only what a model with dependents gives."""

    @pytest.mark.parametrize('fast', [True, False])
    def test_a_model_nothing_depends_on(self, settings, fast):
        settings.GUITARS_DELETE_FAST_PATH = fast
        *_, made = build(1)
        note = ConditionNote.objects.create(condition=made[0])

        with zeal_ignore():
            result = note.delete()

        assert result == (0, {'testapp.ConditionNote': 0})


@pytest.mark.django_db
def test_a_hidden_row_is_not_an_error_for_the_instance_form(tenants):
    """Row-level security hides another tenant's row: nothing is stamped, as ``.delete()`` does
    nothing there, rather than raising from the refresh that follows."""
    with tenant(label=tenants.b):
        stamped = tenants.release_a.soft_delete()

    assert stamped == 0
    assert not archived_anywhere(tenants.release_a)


@pytest.mark.django_db
class TestTheConsistentTreeAssumption:
    """Documented, and pinned here so it stays a deliberate fact: the rules cascade only through
    rows that *flip* to archived, so a live row under an already-archived ancestor is reached by the
    collector and left live by the fast path. See ``docs/soft-delete-api.md``, "What both assume"."""

    @staticmethod
    def _live_clause_under_an_archived_tier():
        offer, tier, *_ = build(0)
        tier.soft_delete()
        clause = Clause._all_objects.create(tier=tier)
        return offer, clause

    def test_the_collector_reaches_it(self, settings):
        settings.GUITARS_DELETE_FAST_PATH = False
        offer, clause = self._live_clause_under_an_archived_tier()

        with zeal_ignore():
            Offer._all_objects.filter(pk=offer.pk).delete()

        assert archived(clause)

    def test_the_fast_path_leaves_it_live(self, settings):
        settings.GUITARS_DELETE_FAST_PATH = True
        offer, clause = self._live_clause_under_an_archived_tier()

        Offer._all_objects.filter(pk=offer.pk).delete()

        assert not archived(clause)
