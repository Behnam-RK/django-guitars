"""What ``hard_delete()`` costs in statements (PR 4 of #55): one switch for the whole walk; a
self-referential cascade by one recursive query; an owned fixpoint reading each owner once.
Counted, never timed, and always to the end state the per-table version reached."""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext, isolate_apps

from tests.testapp.models import (
    Clause,
    Condition,
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


@pytest.mark.django_db
def test_a_failure_part_way_leaves_the_switch_off_inside_the_callers_transaction(monkeypatch):
    """The walk's third ``DELETE`` fails (the first is Phase 1's), after the switch went on. The
    caller catches it and goes on in the same transaction -- the case where a leaked switch is
    live -- and the very next ``.delete()`` must still archive rather than remove."""
    from django.db import transaction  # noqa: PLC0415
    from django.db.backends.utils import CursorWrapper  # noqa: PLC0415

    offer, tier, *_ = _tree()
    other = Offer.objects.create(name='bystander')
    other_pk = other.pk  # ``delete()`` clears it on the instance
    deletes = []
    real = CursorWrapper.execute

    def failing(self, sql, params=None):
        if isinstance(sql, str) and sql.strip().upper().startswith('DELETE FROM'):
            deletes.append(sql)
            if len(deletes) == 3:
                raise RuntimeError('simulated failure on the walk\'s second table')
        return real(self, sql, params)

    with transaction.atomic():
        monkeypatch.setattr(CursorWrapper, 'execute', failing)
        with pytest.raises(RuntimeError, match='second table'):
            offer.hard_delete()
        monkeypatch.undo()

        other.delete()

        assert Offer._all_objects.filter(pk=other_pk).exists()  # archived, not removed
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('rules.hard_deletion', true)")
            assert cursor.fetchone()[0] in (None, '', 'off')


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

    def test_the_walk_adds_no_statement_a_level_beyond_phase_1s_collector(self):
        """Phase 1 is still ``self.delete()``: Django's collector reads a self-referential tree a
        level at a time (two statements), while the walk after it, four more, is now constant."""
        counts = {}
        for depth in (2, 8, 20):
            root = _chain(depth)
            counts[depth] = len(statements(root.hard_delete))

        assert counts[20] - counts[2] <= 2 * (20 - 2), counts

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

    def test_the_walk_after_phase_1_does_not_grow_with_the_depth(self):
        from tests.testapp.models import Ledger  # noqa: PLC0415

        counts = []
        for depth in (3, 12):
            root = node = Ledger.objects.create(name=f'root-{depth}')
            for level in range(depth):
                node = Ledger.objects.create(
                    name=f'n-{depth}-{level}', **{('parent' if level % 2 else 'mirror'): node}
                )
            counts.append(len(statements(root.hard_delete)))

        # Phase 1's collector reads a key a level; the walk after it is constant.
        assert counts[1] - counts[0] <= 3 * (12 - 3), counts


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

    @pytest.mark.parametrize('depth', [1, 2], ids=['child', 'grandchild'])
    def test_a_cue_outside_the_tree_anchoring_a_descendant_holds_the_root_back(self, depth):
        """Removing the root removes its subtree, so a plain key into any node of it dangles
        just as hard (#71): the walk aborted at ``COMMIT`` instead of sparing the tree."""
        from tests.testapp.models import Cue, Ledger, Stagecraft  # noqa: PLC0415

        nodes = _ledger(3)
        outside = Ledger.objects.create(name='outside')
        Cue.objects.create(label='outside', ledger=outside, anchor=nodes[depth])
        craft = Stagecraft.objects.create(name='c', ledger=nodes[0])

        craft.hard_delete()
        with connection.cursor() as cursor:
            cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')

        assert not Stagecraft._all_objects.exists()
        assert Ledger._all_objects.filter(pk__in=[n.pk for n in nodes]).count() == 3


@pytest.mark.django_db
class TestWhichOwnedTargetsAnOutsideKeySpares:
    """A hit on a closure row spares exactly the targets whose closure holds it."""

    def test_only_the_target_whose_subtree_is_anchored(self):
        from guitars.models.soft_deletion import _still_referenced  # noqa: PLC0415
        from tests.testapp.models import Cue, Ledger  # noqa: PLC0415

        first, second = _ledger(3), _ledger(3)
        outside = Ledger.objects.create(name='outside')
        Cue.objects.create(label='outside', ledger=outside, anchor=first[2])

        spared = _still_referenced(Ledger, {first[0].pk, second[0].pk}, {}, 'default')

        assert spared == {first[0].pk}

    def test_a_row_both_subtrees_reach_spares_both(self):
        from guitars.models.soft_deletion import _still_referenced  # noqa: PLC0415
        from tests.testapp.models import Cue, Ledger  # noqa: PLC0415

        first, second = _ledger(2), _ledger(2)
        shared = Ledger.objects.create(name='shared', parent=first[1], mirror=second[1])
        outside = Ledger.objects.create(name='outside')
        Cue.objects.create(label='outside', ledger=outside, anchor=shared)

        spared = _still_referenced(Ledger, {first[0].pk, second[0].pk}, {}, 'default')

        assert spared == {first[0].pk, second[0].pk}

    def test_a_key_into_a_row_of_another_model_the_tree_takes_counts(self):
        """The cue goes with the tree; the note pointing at it does not, so the tree must stay."""
        from guitars.models.soft_deletion import _still_referenced  # noqa: PLC0415
        from tests.testapp.models import Cue, CueNote, Ledger, Stagecraft  # noqa: PLC0415

        nodes = _ledger(2)
        CueNote.objects.create(cue=Cue.objects.create(label='in-tree', ledger=nodes[1]))

        assert _still_referenced(Ledger, {nodes[0].pk}, {}, 'default') == {nodes[0].pk}

        Stagecraft.objects.create(name='c', ledger=nodes[0]).hard_delete()
        with connection.cursor() as cursor:
            cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
        assert Ledger._all_objects.count() == 2

    def test_a_row_reached_along_two_relations_carries_both_targets(self):
        """``Merch`` reaches ``Album`` twice, so one row arrives once per relation; the second
        arrival must still add its target, or a key into it would spare only the first."""
        from guitars.models.soft_deletion import _cascade_closure  # noqa: PLC0415
        from tests.testapp.models import Album, Band, Merch  # noqa: PLC0415

        first, second = Band.objects.create(name='a'), Band.objects.create(name='b')
        shared = Merch.objects.create(
            description='m',
            album=Album.objects.create(title='a', band=first),
            bonus_album=Album.objects.create(title='b', band=second),
        )

        _taken, origins = _cascade_closure(Band, {first.pk, second.pk}, 'default')

        assert origins[Merch][shared.pk] == {first.pk, second.pk}

    def test_a_row_going_anyway_spares_nothing(self):
        """The anchoring cue is claimed by the walk, so its key goes with it."""
        from guitars.models.soft_deletion import _still_referenced  # noqa: PLC0415
        from tests.testapp.models import Cue, Ledger  # noqa: PLC0415

        nodes = _ledger(3)
        outside = Ledger.objects.create(name='outside')
        cue = Cue.objects.create(label='outside', ledger=outside, anchor=nodes[2])

        spared = _still_referenced(Ledger, {nodes[0].pk}, {Cue: {cue.pk}}, 'default')

        assert spared == set()


@pytest.mark.django_db
class TestTheOwnedFixpointReadsWhatIsNew:
    def test_a_chain_reads_each_owner_once(self):
        """Ten reads for this three-deep chain when every round rescanned every owner."""
        from tests.testapp.models import Residency, Rider, Stagehand  # noqa: PLC0415

        stagehand = Stagehand.objects.create(name='s')
        rider = Rider.objects.create(stagehand=stagehand)
        residency = Residency.objects.create(rider=rider)

        sql = statements(residency.hard_delete)

        reads = [s for s in sql if s.startswith('SELECT') and 'set_config' not in s]
        assert len(reads) <= 4

    def test_the_rule_graph_is_read_once_for_the_walk(self, monkeypatch):
        from guitars.models import soft_deletion  # noqa: PLC0415
        from tests.testapp.models import Residency, Rider, Stagehand  # noqa: PLC0415

        calls = []
        real = soft_deletion.rule_update_cycle_edges
        monkeypatch.setattr(
            soft_deletion,
            'rule_update_cycle_edges',
            lambda models: calls.append(1) or real(models),
        )
        stagehand = Stagehand.objects.create(name='s')
        residency = Residency.objects.create(rider=Rider.objects.create(stagehand=stagehand))

        residency.hard_delete()

        assert len(calls) == 1


def _end_state():
    """The row count of every testapp table, live and archived: what a walk leaves behind."""
    from django.apps import apps  # noqa: PLC0415

    from guitars.tenancy import tenancy_bypassed  # noqa: PLC0415

    state = {}
    with tenancy_bypassed():
        for model in apps.get_app_config('testapp').get_models():
            manager = model._all_objects if hasattr(model, '_all_objects') else model._base_manager
            state[model._meta.db_table] = manager.count()
    return state


def _scenarios():
    """Each builds rows and returns the instance to ``hard_delete``; chosen for the shapes a
    delta read could get wrong: a chain, a co-owner that spares, a target freed in a later round."""
    from tests.testapp.models import (  # noqa: PLC0415
        Album,
        Band,
        Merch,
        Orchestra,
        PressKit,
        Residency,
        Rider,
        Stagehand,
    )

    def chain():
        return Residency.objects.create(
            rider=Rider.objects.create(stagehand=Stagehand.objects.create(name='s'))
        )

    def co_owner():
        band = Band.objects.create(name='b')
        kit = PressKit.objects.create(headline='shared')
        Album.objects.create(title='keeps', band=band, press_kit=kit)
        return Album.objects.create(title='goes', band=band, press_kit=kit)

    def freed_in_a_later_round():
        band = Band.objects.create(name='b')
        kit = PressKit.objects.create(headline='Shared')
        album = Album.objects.create(title='Hemispheres', band=band, press_kit=kit)
        orchestra = Orchestra.objects.create(name='LSO', conductor='Davis', programme=kit)
        Merch.objects.create(description='Tour shirt', album=album, featured_orchestra=orchestra)
        return album

    return {'chain': chain, 'co_owner': co_owner, 'freed_later': freed_in_a_later_round}


@pytest.mark.django_db
@pytest.mark.parametrize('name', ['chain', 'co_owner', 'freed_later'])
def test_the_delta_read_ends_where_a_full_rescan_did(name, monkeypatch):
    """A fresh ``_OwnedScan`` each round *is* the full rescan, so the end states must agree."""
    from django.db import transaction  # noqa: PLC0415

    from guitars.models import soft_deletion  # noqa: PLC0415

    def walk():
        with transaction.atomic():
            _scenarios()[name]().hard_delete()
            counts = _end_state()
            transaction.set_rollback(True)
        return counts

    delta = walk()
    real = soft_deletion._owned_targets
    monkeypatch.setattr(
        soft_deletion, '_owned_targets', lambda claimed, using, scan=None: real(claimed, using)
    )
    full = walk()

    assert delta == full


class TestWhichSelfKeysAreFollowedInOneQuery:
    """The eligibility guards of ``_self_cascade_fields``, each of which keeps the level-by-level
    walk where one recursion would be wrong: an MTI chain collected from its root, a key to a
    column that is not the primary key, a pk the ORM converts."""

    @staticmethod
    @isolate_apps('tests.testapp')
    def _models():
        from django.db import models as dj_models  # noqa: PLC0415

        from guitars.models import SetarModel  # noqa: PLC0415

        class Plain(SetarModel):
            parent = dj_models.ForeignKey('self', on_delete=dj_models.CASCADE, null=True)

            class Meta:
                app_label = 'testapp'

        class Root(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Chapter(Root):
            parent = dj_models.ForeignKey('self', on_delete=dj_models.CASCADE, null=True)

            class Meta:
                app_label = 'testapp'

        class Folder(SetarModel):
            code = dj_models.CharField(max_length=10, unique=True)
            parent = dj_models.ForeignKey(
                'self', to_field='code', on_delete=dj_models.CASCADE, null=True
            )

            class Meta:
                app_label = 'testapp'

        return Plain, Chapter, Folder

    def test_a_plain_model_with_a_key_to_the_primary_key_is_followed(self):
        from guitars.models.soft_deletion import _self_cascade_fields  # noqa: PLC0415

        plain, _chapter, _folder = self._models()

        assert [field.name for field in _self_cascade_fields(plain, None)] == ['parent']

    def test_an_mti_chain_keeps_the_level_walk(self):
        from guitars.models.soft_deletion import _self_cascade_fields  # noqa: PLC0415

        _plain, chapter, _folder = self._models()

        assert _self_cascade_fields(chapter, None) == []

    def test_a_key_to_another_column_keeps_the_level_walk(self):
        from guitars.models.soft_deletion import _self_cascade_fields  # noqa: PLC0415

        _plain, _chapter, folder = self._models()

        assert _self_cascade_fields(folder, None) == []

    def test_a_pk_the_orm_converts_keeps_the_level_walk(self, monkeypatch):
        from guitars.models.soft_deletion import _self_cascade_fields  # noqa: PLC0415

        plain, _chapter, _folder = self._models()
        monkeypatch.setattr(
            type(plain._meta.pk), 'get_db_converters', lambda self, connection: [str]
        )

        assert _self_cascade_fields(plain, None) == []

    @staticmethod
    @isolate_apps('tests.testapp')
    def _other_shapes():
        from django.db import models as dj_models  # noqa: PLC0415

        from guitars.models import SetarModel  # noqa: PLC0415

        class Pinned(SetarModel):
            parent = dj_models.ForeignKey('self', on_delete=dj_models.DO_NOTHING, null=True)

            class Meta:
                app_label = 'testapp'

        class Account(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Profile(SetarModel):
            account = dj_models.OneToOneField(
                Account, on_delete=dj_models.CASCADE, primary_key=True
            )
            parent = dj_models.ForeignKey('self', on_delete=dj_models.CASCADE, null=True)

            class Meta:
                app_label = 'testapp'

        return Pinned, Profile

    def test_a_key_that_does_not_cascade_is_not_followed(self):
        """Followed, its referrers would be collected and removed with the row they point at."""
        from guitars.models.soft_deletion import _self_cascade_fields  # noqa: PLC0415

        pinned, _profile = self._other_shapes()

        assert _self_cascade_fields(pinned, None) == []

    def test_a_pk_that_is_a_key_keeps_the_level_walk(self):
        """The ORM reads such a pk through its target's converters too, which the pk's own
        ``get_db_converters`` does not list; raw SQL would return it unconverted."""
        from guitars.models.soft_deletion import _self_cascade_fields  # noqa: PLC0415

        _pinned, profile = self._other_shapes()

        assert _self_cascade_fields(profile, None) == []


class _ToSecondary:
    def db_for_read(self, model, **hints):
        return 'secondary'

    def db_for_write(self, model, **hints):
        return 'secondary'


@pytest.mark.django_db(databases=['default', 'secondary'])
class TestAnInstanceNoDatabaseHasLoaded:
    """``Model(pk=...)`` has no ``_state.db``: every read, the switch and every ``DELETE`` must
    land on the one alias the router writes to, as ``delete()`` resolves it."""

    @pytest.fixture(autouse=True)
    def _route_to_secondary(self, settings):
        settings.DATABASE_ROUTERS = ['tests.test_hard_delete_depth._ToSecondary']

    def test_its_tree_is_removed_where_it_lives(self):
        offer = Offer.objects.using('secondary').create(name='o')
        Tier.objects.using('secondary').create(offer=offer)

        Offer(pk=offer.pk).hard_delete()

        assert not Offer._all_objects.using('secondary').exists()
        assert not Tier._all_objects.using('secondary').exists()

    def test_a_subtree_on_another_database_is_not_read(self):
        Setlist.objects.using('default').create(pk=9001, title='root')
        Setlist.objects.using('default').create(pk=9002, title='child', parent_id=9001)
        Setlist.objects.using('secondary').create(pk=9001, title='root')
        Setlist.objects.using('secondary').create(pk=9002, title='unrelated')

        Setlist(pk=9001).hard_delete()

        left = dict(Setlist._all_objects.using('secondary').values_list('title', '_deleted_at'))
        assert left == {'unrelated': None}


class _WritesToSecondary:
    def db_for_write(self, model, **hints):
        return 'secondary'


@pytest.mark.django_db(databases=['default', 'secondary'])
def test_a_router_outranks_the_alias_an_instance_was_read_from(settings):
    """``delete()`` asks the router before ``_state.db``, so Phase 1 archives where the router
    writes; the walk after it must remove there too, not where the instance was read."""
    for alias in ('default', 'secondary'):
        Setlist.objects.using(alias).create(pk=9001, title='root')
        Setlist.objects.using(alias).create(pk=9002, title='child', parent_id=9001)
    loaded = Setlist._all_objects.using('default').get(pk=9001)
    settings.DATABASE_ROUTERS = ['tests.test_hard_delete_depth._WritesToSecondary']

    loaded.hard_delete()

    assert not Setlist._all_objects.using('secondary').exists()
    assert dict(Setlist._all_objects.using('default').values_list('title', '_deleted_at')) == {
        'root': None,
        'child': None,
    }


class _ReadsFromSecondary:
    def db_for_read(self, model, **hints):
        return 'secondary'

    def db_for_write(self, model, **hints):
        return 'default'


@pytest.mark.django_db(databases=['default', 'secondary'])
class TestAQuerysetHardDeleteUnderASplitRouter:
    """A queryset's ``db`` is its read alias until it is written through. ``hard_delete()`` is a
    write: it removes where the router writes, and takes the switch there, as ``delete()`` does."""

    @pytest.fixture(autouse=True)
    def _split(self, settings):
        settings.DATABASE_ROUTERS = ['tests.test_hard_delete_depth._ReadsFromSecondary']

    def test_a_plain_model_is_removed_where_it_is_written(self):
        offer = Offer.objects.using('default').create(name='o')

        Offer._all_objects.filter(pk=offer.pk).hard_delete()

        assert not Offer._all_objects.using('default').filter(pk=offer.pk).exists()

    def test_an_mti_chain_is_removed_where_it_is_written(self):
        offer, _tier, _clause, (condition, *_) = _tree(conditions=1)

        QuantityCondition._all_objects.filter(pk=condition.pk).hard_delete()

        assert not QuantityCondition._all_objects.using('default').filter(pk=condition.pk).exists()
        assert not Condition._all_objects.using('default').filter(pk=condition.pk).exists()


def test_the_owned_rule_graph_is_redone_only_for_a_model_outside_the_registry(monkeypatch):
    """Computed once for the walk; a claimed model the registry does not hold was not in that
    sweep, so its first appearance must redo it."""
    from guitars.models import soft_deletion  # noqa: PLC0415

    calls = []
    monkeypatch.setattr(
        soft_deletion, 'rule_update_cycle_edges', lambda swept: calls.append(swept) or set()
    )
    monkeypatch.setattr(soft_deletion, 'owned_tenancy_refusals', lambda swept: {})

    @isolate_apps('tests.testapp')
    def unregistered():
        from guitars.models import SetarModel  # noqa: PLC0415

        class Stray(SetarModel):
            class Meta:
                app_label = 'testapp'

        return Stray

    stray = unregistered()
    scan = soft_deletion._OwnedScan()
    scan.graph({Offer: {1}})
    scan.graph({Offer: {1}, Tier: {2}})
    assert len(calls) == 1
    scan.graph({Offer: {1}, stray: {3}})
    assert len(calls) == 2
    assert stray in calls[-1]
    scan.graph({Offer: {1}, stray: {3}, Tier: {4}})
    assert len(calls) == 2


@pytest.mark.django_db
def test_the_walks_subtree_read_returns_each_row_once(monkeypatch):
    """The collection walk needs the rows, not which seed each descends from: reading pairs
    returned a row once per seed above it, quadratic in depth when every node is a seed."""
    from django.db.backends.postgresql.base import Cursor  # noqa: PLC0415

    from guitars.models.soft_deletion import _with_self_descendants  # noqa: PLC0415

    nodes = _ledger(20)
    fetched = []
    real = Cursor.fetchall

    def fetchall(self):
        fetched.append(real(self))
        return fetched[-1]

    monkeypatch.setattr(Cursor, 'fetchall', fetchall)

    below = _with_self_descendants(type(nodes[0]), {n.pk for n in nodes}, 'default')

    assert below == {n.pk for n in nodes}
    assert [len(rows) for rows in fetched] == [len(nodes) - 1]


@pytest.mark.django_db
def test_the_collection_walk_does_not_read_origins(monkeypatch):
    """Origins are the sparing closure's; a tree with no owner never asks for them."""
    from guitars.models import soft_deletion  # noqa: PLC0415

    calls = []
    real = soft_deletion._self_descendant_origins
    monkeypatch.setattr(
        soft_deletion,
        '_self_descendant_origins',
        lambda *args: calls.append(args) or real(*args),
    )
    nodes = _ledger(4)

    nodes[0].hard_delete()

    assert calls == []
    assert not type(nodes[0])._all_objects.exists()
