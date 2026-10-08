"""Where archiving in SQL and Django's collector part ways, read off the registry. Each shape is
asserted both ways (covered, and a gap), since a walk reporting nothing would read as "every
model is eligible"."""

from __future__ import annotations

import types

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
    """Since 2.17.0 (#59) the rule matches on the ``to_field`` column, so a key onto it is a
    covered edge like any other -- except where the rule cannot read that column."""

    @staticmethod
    @isolate_apps('tests.testapp')
    def _gaps(*, below_the_holder: bool = False):
        class Parent(SetarModel):
            code = models.CharField(max_length=10, unique=True)

            class Meta:
                app_label = 'testapp'

        class Kid(Parent):
            slug = models.CharField(max_length=10, unique=True)

            class Meta:
                app_label = 'testapp'

        class Child(SetarModel):
            parent = models.ForeignKey(
                Kid if below_the_holder else Parent,
                to_field='slug' if below_the_holder else 'code',
                on_delete=models.CASCADE,
                related_name='children',
            )

            class Meta:
                app_label = 'testapp'

        clear_cascade_plan_cache()
        return cascade_plan(Kid if below_the_holder else Parent)[0]

    def test_it_is_covered(self):
        assert [g for g in self._gaps() if g.blocking] == []

    def test_a_column_the_rule_cannot_read_is_a_blocking_gap(self):
        """Declared on a descendant, below the table the rule fires on: no rule is written."""
        assert [g for g in self._gaps(below_the_holder=True) if g.blocking]


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
        monkeypatch.setattr(coverage, '_needs_rules_from_its_app', lambda owner: True)

        gaps = coverage._enforcement_gaps(TenantedChild)

        assert [g.reason for g in gaps if g.blocking] == [f"'{child_app}' is not in LOCAL_APPS"]

    def test_a_child_routed_off_postgresql(self, monkeypatch):
        monkeypatch.setattr(
            coverage, 'migrates_to_postgresql', lambda model: model is not QuantityCondition
        )
        monkeypatch.setattr(coverage, '_needs_rules_from_its_app', lambda owner: True)

        gaps = coverage._enforcement_gaps(QuantityCondition)

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
            coverage._enforcement_gaps(LitPylon),
            coverage._enforcement_gaps(NeonPylon),
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

        (gap,) = coverage._enforcement_gaps(TenantedChild)

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
        monkeypatch.setattr(coverage, '_needs_rules_from_its_app', lambda owner: True)

        gaps = coverage._enforcement_gaps(kid)

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

    def test_a_primary_key_that_is_not_the_parent_link_declines_the_fast_path(self):
        """The redirect rule joins on the child's own key (#64), so ``.delete()`` archives another
        row; ``soft_delete()`` stamps through the holder's key and is right, so this only declines
        the fast path."""
        kid = self._explicit_pk()

        gaps = coverage._enforcement_gaps(kid)

        assert [g.blocking for g in gaps] == [False]
        assert 'parent link' in gaps[0].reason

    @staticmethod
    @isolate_apps('tests.testapp')
    def _explicit_pk_with_a_child():
        from django.db.models import AutoField, OneToOneField  # noqa: PLC0415

        class Root(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Kid(Root):
            code = AutoField(primary_key=True)
            root_link = OneToOneField(Root, on_delete=models.CASCADE, parent_link=True)

            class Meta:
                app_label = 'testapp'

        class Child(SetarModel):
            kid = models.ForeignKey(Kid, on_delete=models.CASCADE, related_name='children')

            class Meta:
                app_label = 'testapp'

        return Kid

    def test_a_key_cascading_into_it_makes_the_gap_blocking(self):
        """The key stores ``Kid.code`` and the rule on the root's table compares it with the
        root's ``id`` (#64): the stamp is right, the cascade out of it archives the wrong
        children. ``soft_delete()`` there silently hid a sibling's rows, so it must raise."""
        kid = self._explicit_pk_with_a_child()

        gaps = coverage._enforcement_gaps(kid)

        assert [g.blocking for g in gaps] == [True]

    @staticmethod
    @isolate_apps('tests.testapp')
    def _grandchild_over_an_intermediate_with_its_own_key():
        from django.db.models import AutoField, OneToOneField  # noqa: PLC0415

        class Root(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Mid(Root):
            code = AutoField(primary_key=True)
            root_link = OneToOneField(Root, on_delete=models.CASCADE, parent_link=True)

            class Meta:
                app_label = 'testapp'

        class Kid(Mid):
            class Meta:
                app_label = 'testapp'

        return Kid

    def test_an_intermediates_own_key_is_seen_from_the_grandchild(self):
        """``Kid``'s own key is its link to ``Mid``, so only walking the chain sees it."""
        kid = self._grandchild_over_an_intermediate_with_its_own_key()

        gaps = coverage._enforcement_gaps(kid)

        assert [g.reason for g in gaps] == ['its primary key is not its parent link (#64, guitars.E005)']

    def test_every_gap_is_reported_not_the_first(self, monkeypatch):
        """An early return let a non-blocking locality gap hide a blocking one behind it."""
        kid = self._explicit_pk()
        monkeypatch.setattr(coverage, 'migrates_to_postgresql', lambda model: False)
        monkeypatch.setattr(coverage, '_needs_rules_from_its_app', lambda owner: False)

        gaps = coverage._enforcement_gaps(kid)

        assert {g.reason for g in gaps} == {
            'is routed off PostgreSQL',
            'its primary key is not its parent link (#64, guitars.E005)',
        }


class TestWhichKeysCascadeIntoAModel:
    """A cascade rule is written from the pass over the key's *target*, so only a CASCADE key
    pointing at the model itself makes that model's own app matter."""

    def test_a_cascade_key_counts(self):
        assert coverage._needs_rules_from_its_app(Offer)

    def test_a_key_that_does_not_cascade_does_not(self):
        """``Stagehand``'s only inbound key is ``DO_NOTHING``."""
        from tests.testapp.models import Stagehand  # noqa: PLC0415

        assert not coverage._needs_rules_from_its_app(Stagehand)

    def test_set_null_does_not(self):
        from tests.crossapp_retire_child.models import Heir  # noqa: PLC0415

        assert not coverage._needs_rules_from_its_app(Heir)

    def test_a_parent_link_does_not(self):
        """``Condition`` is pointed at by its descendants' parent links alone; the one ordinary
        key (``ConditionNote``) aims at ``QuantityCondition``, which is the one that counts."""
        from tests.testapp.models import Condition  # noqa: PLC0415

        assert not coverage._needs_rules_from_its_app(Condition)
        assert coverage._needs_rules_from_its_app(QuantityCondition)

    @staticmethod
    @isolate_apps('tests.testapp')
    def _an_inherited_key():
        class Root(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Booking(SetarModel):
            root = models.ForeignKey(Root, on_delete=models.CASCADE, related_name='bookings')

            class Meta:
                app_label = 'testapp'

        class Kid(Root):
            class Meta:
                app_label = 'testapp'

        return Root, Kid

    def test_a_key_into_an_ancestor_is_the_ancestors_not_the_descendants(self):
        """``Booking -> Root``'s rule is written from ``Root``'s app, whatever ``Kid`` is."""
        root, kid = self._an_inherited_key()

        assert coverage._needs_rules_from_its_app(root)
        assert not coverage._needs_rules_from_its_app(kid)

    def test_a_set_null_childless_child_does_not_refuse_soft_delete(self, monkeypatch):
        """The real shape: a non-local descendant whose only inbound key is ``SET_NULL``."""
        from tests.crossapp_retire_child.models import Heir  # noqa: PLC0415

        child_app = Heir._meta.app_label
        monkeypatch.setattr(coverage, 'is_local', lambda config: config.label != child_app)

        gaps = coverage._enforcement_gaps(Heir)

        assert gaps and not [g for g in gaps if g.blocking]


class TestAnOwnedRuleIsWrittenFromItsOwnersApp:
    """An ``OwningForeignKey`` rule fires on the declaring model's table, from that model's own
    app pass, though no ``CASCADE`` key points at the declarer."""

    @staticmethod
    @isolate_apps('tests.testapp')
    def _an_owner():
        from guitars.models import OwningForeignKey  # noqa: PLC0415

        class Target(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Owner(SetarModel):
            target = OwningForeignKey(Target, on_delete=models.DO_NOTHING, null=True)

            class Meta:
                app_label = 'testapp'

        return Owner

    def test_it_counts_as_needing_a_rule_from_its_app(self):
        assert coverage._needs_rules_from_its_app(self._an_owner())

    def test_a_non_local_owner_leaves_its_target_live_so_it_blocks(self, monkeypatch):
        owner = self._an_owner()
        monkeypatch.setattr(coverage, 'is_local', lambda config: False)

        gaps = coverage._enforcement_gaps(owner)

        assert [g.blocking for g in gaps] == [True]


class TestTheInboundTestsRemainingExclusions:
    def test_a_referrer_with_no_deleted_at_carries_no_rule(self):
        """``Band``'s many-to-many through row has no ``_deleted_at``; it is removed in Python."""
        from tests.testapp.models import Genre  # noqa: PLC0415

        assert not coverage._needs_rules_from_its_app(Genre)

    @staticmethod
    @isolate_apps('tests.testapp')
    def _a_key_to_a_proxy():
        class Root(SetarModel):
            class Meta:
                app_label = 'testapp'

        class RootProxy(Root):
            class Meta:
                app_label = 'testapp'
                proxy = True

        class Booking(SetarModel):
            root = models.ForeignKey(RootProxy, on_delete=models.CASCADE, related_name='+')

            class Meta:
                app_label = 'testapp'

        return Root

    def test_a_key_aimed_at_a_proxy_counts_for_its_concrete_model(self):
        assert coverage._needs_rules_from_its_app(self._a_key_to_a_proxy())

    def test_each_chain_owner_is_asked_for_itself_not_the_model(self, monkeypatch):
        """Only ``Mid`` has rules written from its app; asking about ``Kid`` instead missed it."""
        _root, mid, kid = TestWhatTheModelsOwnAppHasToDoWithIt._an_intermediate()
        monkeypatch.setattr(coverage, 'migrates_to_postgresql', lambda model: model is not mid)
        monkeypatch.setattr(coverage, '_needs_rules_from_its_app', lambda owner: owner is mid)

        gaps = coverage._enforcement_gaps(kid)

        assert [g.blocking for g in gaps] == [True]


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


class TestWhichKeysAreSafeBelowAnOwnKey:
    """A key into a model stores that model's primary key, which is the root's id only while it
    and every model between it and the holder use their parent link as their key (#64)."""

    @staticmethod
    @isolate_apps('tests.testapp')
    def _shapes():
        from django.db.models import AutoField, OneToOneField  # noqa: PLC0415

        from guitars.models import OwningForeignKey  # noqa: PLC0415

        class Root(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Normal(Root):
            class Meta:
                app_label = 'testapp'

        class Own(Normal):
            code = AutoField(primary_key=True)
            normal_link = OneToOneField(Normal, on_delete=models.CASCADE, parent_link=True)

            class Meta:
                app_label = 'testapp'

        class Below(Own):
            class Meta:
                app_label = 'testapp'

        class IntoRoot(SetarModel):
            root = models.ForeignKey(Root, on_delete=models.CASCADE, related_name='+')

            class Meta:
                app_label = 'testapp'

        class IntoNormal(SetarModel):
            normal = models.ForeignKey(Normal, on_delete=models.CASCADE, related_name='+')

            class Meta:
                app_label = 'testapp'

        class IntoBelow(SetarModel):
            below = models.ForeignKey(Below, on_delete=models.CASCADE, related_name='+')

            class Meta:
                app_label = 'testapp'

        class Owner(SetarModel):
            own = OwningForeignKey(Own, on_delete=models.DO_NOTHING, null=True, related_name='+')

            class Meta:
                app_label = 'testapp'

        return types.SimpleNamespace(Own=Own, Below=Below, Normal=Normal, Owner=Owner)

    def test_the_own_key_model_is_blocked_only_by_keys_that_can_store_it(self):
        """``IntoNormal`` stores ``Normal``'s key (the root's id); ``IntoRoot`` likewise; only a key
        into ``Own`` or ``Below`` stores ``Own.code``. Here ``Own`` has none, ``Below`` has one."""
        shapes = self._shapes()

        assert [g.blocking for g in coverage._enforcement_gaps(shapes.Own)] == [False]
        assert [g.blocking for g in coverage._enforcement_gaps(shapes.Below)] == [True]

    def test_an_owned_key_into_an_own_key_model_blocks_the_owner(self):
        """The owned rule stamps ``WHERE id = old.<fk>`` and the key stores ``Own.code``: it
        archives another row. No ``CASCADE`` edge shows it, so ``cascade_plan`` has to."""
        shapes = self._shapes()
        clear_cascade_plan_cache()

        gaps, _reached = cascade_plan(shapes.Owner)

        assert [g.blocking for g in gaps if '#64' in g.reason] == [True]

    def test_every_gap_is_reported_once(self, monkeypatch):
        shapes = self._shapes()
        monkeypatch.setattr(coverage, 'is_local', lambda config: False)

        gaps = coverage._enforcement_gaps(shapes.Below)

        assert len({(g.edge, g.reason) for g in gaps}) == len(gaps)

    def test_one_reason_from_levels_that_differ_is_blocking_if_any_is(self, monkeypatch):
        """The leaf needs a rule from its app and the levels above do not: the one reported gap
        must be the blocking one whichever order they are read in."""
        shapes = self._shapes()
        monkeypatch.setattr(coverage, 'is_local', lambda config: False)
        monkeypatch.setattr(coverage, '_needs_rules_from_its_app', lambda owner: owner is shapes.Below)

        gaps = coverage._enforcement_gaps(shapes.Below)

        assert [g.blocking for g in gaps if 'LOCAL_APPS' in g.reason] == [True]

    def test_the_refusal_does_not_send_a_64_model_to_dot_delete(self):
        """``.delete()`` archives another row for that shape, so the advice it gets is not that."""
        from guitars.models.soft_deletion import (  # noqa: PLC0415
            SoftDeleteUnsupportedError,
            _require_covered,
        )

        shapes = self._shapes()
        clear_cascade_plan_cache()

        with pytest.raises(SoftDeleteUnsupportedError) as raised:
            _require_covered(shapes.Below, 'default')

        assert '#64' in str(raised.value)
        assert 'Use .delete()' not in str(raised.value)
