"""Where archiving in SQL and Django's collector part ways, read off the registry. Each shape is
asserted both ways (covered, and a gap), since a walk reporting nothing would read as "every
model is eligible"."""

from __future__ import annotations

import pytest
from django.db import models
from django.db.models.signals import class_prepared
from django.test.utils import isolate_apps

from guitars.models import SetarModel
from guitars.models import _cascade_coverage as coverage
from guitars.models._cascade_coverage import cascade_plan, clear_cascade_plan_cache
from tests.testapp.models import (
    Album,
    Band,
    Genre,
    ChamberOrchestra,
    Offer,
    QuantityCondition,
    Setlist,
    Signboard,
    Tier,
)


@pytest.fixture(autouse=True)
def _fresh_plans():
    clear_cascade_plan_cache()
    yield
    clear_cascade_plan_cache()


def reasons(model, *, blocking: bool | None = None) -> list[str]:
    gaps, _ = cascade_plan(model)
    return [f'{g.edge}: {g.reason}' for g in gaps if blocking is None or g.blocking is blocking]


class TestACoveredTree:
    def test_the_issue_55_tree_has_no_gap_at_any_depth(self):
        assert cascade_plan(Offer)[0] == ()
        assert cascade_plan(Tier)[0] == ()

    def test_an_mti_child_inherits_its_roots_coverage(self):
        assert cascade_plan(QuantityCondition)[0] == ()
        assert cascade_plan(ChamberOrchestra)[0] == ()

    def test_a_self_referential_key_is_covered_but_not_transparently(self):
        """Its trigger archives the tree, so ``soft_delete()`` is fine; but a child below level
        one keeps a stale ``_updated_at`` that the collector would have moved, so ``delete()``
        must not take the shortcut."""
        assert reasons(Setlist, blocking=True) == []
        assert any('self-referential' in line for line in reasons(Setlist, blocking=False))

    def test_every_model_the_collector_would_touch_is_reached(self):
        _, reached = cascade_plan(Offer)

        names = {model.__name__ for model in reached}
        assert {'Offer', 'Tier', 'Clause', 'Condition', 'QuantityCondition', 'Reward'} <= names
        assert {'DiscountReward', 'ShippingReward', 'GiftReward'} <= names

    def test_ancestors_are_reached_for_their_signals(self):
        _, reached = cascade_plan(QuantityCondition)

        assert 'Condition' in {model.__name__ for model in reached}


class TestWhereDjangoActsInPython:
    """Defined behaviour, but not the rules' -- so an explicit ``soft_delete()`` proceeds and the
    ``delete()`` fast path declines."""

    def test_set_null_is_applied_by_the_collector(self):
        found = reasons(Band, blocking=False)

        assert any('Album.producer' in line and 'SET_NULL' in line for line in found)

    def test_a_plain_many_to_many_through_row_is_removed_by_the_collector(self):
        found = reasons(Band, blocking=False)

        assert any('not soft-deletable' in line and 'genres' in line for line in found)

    def test_none_of_that_blocks(self):
        assert reasons(Band, blocking=True) == []


class TestWhereARulesOnlyArchiveLeavesRowsLive:
    def test_a_generic_relation_is_blocking(self):
        found = reasons(Signboard, blocking=True)

        assert any('Signboard.scribbles' in line and 'generic relation' in line for line in found)

    def test_a_cycle_refused_edge_is_blocking(self, monkeypatch):
        offer, tier = Offer._meta.db_table, Tier._meta.db_table
        monkeypatch.setattr(coverage, 'rule_update_cycle_edges', lambda models: {(offer, tier)})
        clear_cascade_plan_cache()

        found = reasons(Offer, blocking=True)

        assert any('Tier.offer -> testapp.Offer' in line and 'cycle' in line for line in found)

    def test_an_app_outside_local_apps_is_blocking(self, monkeypatch):
        monkeypatch.setattr(coverage, 'is_local', lambda app: False)

        found = reasons(Offer, blocking=True)

        assert any('testapp.Offer' in line and 'LOCAL_APPS' in line for line in found)

    def test_a_model_routed_off_postgresql_is_blocking(self, monkeypatch):
        monkeypatch.setattr(coverage, 'migrates_to_postgresql', lambda model: model is not Tier)

        found = reasons(Offer, blocking=True)

        assert any('testapp.Tier' in line and 'routed off PostgreSQL' in line for line in found)

    def test_a_generic_child_is_blocking_through_the_relation_not_a_missing_column(self):
        """``Scribble`` is soft-deletable and no key column ties it to ``Signboard``: what holds
        the fast path back is the relation itself, and nothing says it is not soft-deletable."""
        assert any(
            'testapp.Signboard.scribbles' in line and 'generic relation' in line
            for line in reasons(Signboard, blocking=True)
        )
        assert all('not soft-deletable' not in line for line in reasons(Signboard))


def test_a_model_that_is_not_soft_deletable_is_blocking_and_never_raises():
    """``Genre`` carries no ``_deleted_at``: nothing archives it, and the walk must say so
    rather than ask for a column it has not got."""
    assert reasons(Genre, blocking=True) == ['testapp.Genre: is not soft-deletable']


def test_a_plan_is_computed_once_per_model():
    clear_cascade_plan_cache()

    assert cascade_plan(Offer) is cascade_plan(Offer)
    assert cascade_plan(Album) is not cascade_plan(Band)


class TestAKeyToAColumnOtherThanThePrimaryKey:
    """The cascade rule correlates ``fk = old.<pk>``, so a ``to_field`` key archives nothing: it
    is a gap the walk must report, not a covered edge."""

    @staticmethod
    @isolate_apps('tests.testapp')
    def _gaps():
        class Parent(SetarModel):
            code = models.CharField(max_length=10, unique=True)

            class Meta:
                app_label = 'testapp'

        class Child(SetarModel):
            parent = models.ForeignKey(
                Parent, to_field='code', on_delete=models.CASCADE, related_name='children'
            )

            class Meta:
                app_label = 'testapp'

        clear_cascade_plan_cache()
        return cascade_plan(Parent)[0]

    def test_it_is_a_blocking_gap(self):
        gaps = self._gaps()

        assert [g.reason for g in gaps if g.blocking and 'to_field' in g.reason]


def _both_caches_are_warm():
    cascade_plan(Offer)
    coverage._registry_cycle_edges()
    assert cascade_plan.cache_info().currsize >= 1
    assert coverage._registry_cycle_edges.cache_info().currsize >= 1


def _both_caches_are_empty():
    return (
        cascade_plan.cache_info().currsize == 0
        and coverage._registry_cycle_edges.cache_info().currsize == 0
    )


def test_a_new_model_invalidates_every_cache():
    _both_caches_are_warm()

    class_prepared.send(sender=Offer)

    assert _both_caches_are_empty()


def test_a_changed_setting_invalidates_every_cache(settings):
    """What an edge is depends on ``LOCAL_APPS`` and the router, so a plan outlives neither."""
    _both_caches_are_warm()

    settings.LOCAL_APPS = [*settings.LOCAL_APPS]

    assert _both_caches_are_empty()


class TestAJoinedKeyTheGeneratorRefusesIsAGap:
    """Every key the generator writes no rule for must read as uncovered here, or ``.delete()``
    takes the fast path and the rows only a rule would have archived stay live."""

    @staticmethod
    @isolate_apps('tests.testapp')
    def _through_an_intermediates_own_key():
        class Owner(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Root(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Mid(Root):
            code = models.AutoField(primary_key=True)
            root_link = models.OneToOneField(Root, on_delete=models.CASCADE, parent_link=True)

            class Meta:
                app_label = 'testapp'

        class Kid(Mid):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE, related_name='kids')

            class Meta:
                app_label = 'testapp'

        clear_cascade_plan_cache()
        return cascade_plan(Owner)[0]

    @staticmethod
    @isolate_apps('tests.testapp')
    def _over_a_refused_chain():
        class Owner(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Plain(models.Model):
            class Meta:
                app_label = 'testapp'

        class Soft(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Both(Plain, Soft):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE, related_name='boths')

            class Meta:
                app_label = 'testapp'

        clear_cascade_plan_cache()
        return cascade_plan(Owner)[0]

    def test_a_link_through_an_intermediates_own_key(self):
        assert [g for g in self._through_an_intermediates_own_key() if g.blocking]

    def test_a_key_over_a_chain_guitars_e003_refuses(self):
        assert [g for g in self._over_a_refused_chain() if g.blocking]


class TestAReachedModelTheGeneratorWritesNoRuleFor:
    """The generator writes a child's own rule from the pass over the child's app, so a child
    outside ``LOCAL_APPS`` or routed away has none even under a covered ancestor; and a chain
    ``guitars.E003`` refuses has none at all. Each is a blocking gap, or the fast path deletes."""

    def test_a_child_whose_own_app_is_not_local(self, monkeypatch):
        from tests.crossapp_tenant_child.models import TenantedChild  # noqa: PLC0415

        child_app = TenantedChild._meta.app_label
        monkeypatch.setattr(coverage, 'is_local', lambda config: config.label != child_app)

        gaps = coverage._enforcement_gaps(TenantedChild, has_inbound_keys=True)

        assert [g.reason for g in gaps if g.blocking] == [f"'{child_app}' is not in LOCAL_APPS"]

    def test_a_child_routed_off_postgresql(self, monkeypatch):
        monkeypatch.setattr(
            coverage, 'migrates_to_postgresql', lambda model: model is not QuantityCondition
        )

        gaps = coverage._enforcement_gaps(QuantityCondition, has_inbound_keys=True)

        assert [g.reason for g in gaps if g.blocking] == ['is routed off PostgreSQL']

    @staticmethod
    @isolate_apps('tests.testapp')
    def _refused():
        from guitars.models import DutarModel, SoftDeletableModel  # noqa: PLC0415

        class Pylon(DutarModel):
            class Meta:
                app_label = 'testapp'

        class LitPylon(Pylon, SoftDeletableModel):
            class Meta(SoftDeletableModel.Meta):
                app_label = 'testapp'

        class NeonPylon(LitPylon):
            class Meta(SoftDeletableModel.Meta):
                app_label = 'testapp'

        return (
            coverage._enforcement_gaps(LitPylon, has_inbound_keys=False),
            coverage._enforcement_gaps(NeonPylon, has_inbound_keys=False),
        )

    def test_a_chain_guitars_e003_refuses(self):
        for gaps in self._refused():
            assert [g.reason for g in gaps if g.blocking] == [
                'its chain is refused a soft-delete rule (guitars.E003)'
            ]


class TestWhatTheModelsOwnAppHasToDoWithIt:
    def test_a_child_with_no_inbound_keys_only_declines_the_fast_path(self, monkeypatch):
        """Nothing cascades into it, so ``soft_delete()`` leaves nothing live; but the collector
        would physically delete its row, so the fast path must still decline (#58)."""
        from tests.crossapp_tenant_child.models import TenantedChild  # noqa: PLC0415

        child_app = TenantedChild._meta.app_label
        monkeypatch.setattr(coverage, 'is_local', lambda config: config.label != child_app)

        (gap,) = coverage._enforcement_gaps(TenantedChild, has_inbound_keys=False)

        assert (gap.blocking, gap.reason) == (False, f"'{child_app}' is not in LOCAL_APPS")

    def test_a_root_is_not_refused_because_a_childless_child_is_unenforced(self, monkeypatch):
        from tests.crossapp_tenant_ancestor.models import TenantedAncestor  # noqa: PLC0415
        from tests.crossapp_tenant_child.models import TenantedChild  # noqa: PLC0415

        child_app = TenantedChild._meta.app_label
        monkeypatch.setattr(coverage, 'is_local', lambda config: config.label != child_app)
        clear_cascade_plan_cache()

        gaps, _ = cascade_plan(TenantedAncestor)

        assert gaps and not [g for g in gaps if g.blocking]

    @staticmethod
    @isolate_apps('tests.testapp')
    def _an_intermediate():
        class Root(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Mid(Root):
            class Meta:
                app_label = 'testapp'

        class Kid(Mid):
            class Meta:
                app_label = 'testapp'

        return Root, Mid, Kid

    def test_an_intermediate_ancestors_app_is_asked_too(self, monkeypatch):
        """``Mid``'s own rule is written from ``Mid``'s app pass, between ``Kid`` and ``Root``."""
        _root, mid, kid = self._an_intermediate()
        monkeypatch.setattr(coverage, 'migrates_to_postgresql', lambda model: model is not mid)

        gaps = coverage._enforcement_gaps(kid, has_inbound_keys=True)

        assert [g.reason for g in gaps if g.blocking] == ['is routed off PostgreSQL']

    @staticmethod
    @isolate_apps('tests.testapp')
    def _explicit_pk():
        from django.db.models import AutoField, OneToOneField  # noqa: PLC0415

        class Root(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Kid(Root):
            code = AutoField(primary_key=True)
            root_link = OneToOneField(Root, on_delete=models.CASCADE, parent_link=True)

            class Meta:
                app_label = 'testapp'

        return Kid

    def test_a_primary_key_that_is_not_the_parent_link_is_a_blocking_gap(self):
        """The redirect rule joins on the child's own key (#64), so it archives another row."""
        kid = self._explicit_pk()

        gaps = coverage._enforcement_gaps(kid, has_inbound_keys=False)

        assert [g.blocking for g in gaps] == [True]
        assert 'parent link' in gaps[0].reason


class TestTheJoinedRefusalsAreOneAnswer:
    """``joined_refusal`` is what the generator, the cycle graph and ``classify_cascade`` all
    read; each arm needs its own test, since the model-level gap above masks the E003 one."""

    @staticmethod
    @isolate_apps('tests.testapp')
    def _over_a_refused_chain():
        class Owner(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Plain(models.Model):
            class Meta:
                app_label = 'testapp'

        class Soft(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Both(Plain, Soft):
            owner = models.ForeignKey(Owner, on_delete=models.CASCADE, related_name='boths')

            class Meta:
                app_label = 'testapp'

        return Both, Both._meta.get_field('owner')

    def test_a_chain_guitars_e003_refuses(self):
        from guitars.introspection import joined_refusal  # noqa: PLC0415

        both, field = self._over_a_refused_chain()

        assert joined_refusal(both, field) == 'its chain is refused a soft-delete rule (guitars.E003)'
