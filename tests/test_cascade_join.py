"""A CASCADE key declared on an MTI descendant's table while ``_deleted_at`` lives on an ancestor:
the rule updates the ancestor, reaching it through the descendant's table. Before 2.12.0 the
generator skipped these, so only Django's Python ``Collector`` archived such children."""

from __future__ import annotations

import re
import types
from datetime import timedelta

import pytest
from django.apps import apps
from django.db.models import CASCADE, ForeignKey
from django.utils import timezone
from django.test.utils import isolate_apps

from guitars import introspection
from guitars.introspection import _rule_update_edges, rule_update_cycle_edges
from guitars.management.enforcement.command import Command
from guitars.models import SetarModel
from guitars.tenancy import tenancy_bypassed
from tests.conftest import clear_cascade_coverage, execute, scalar
from tests.testapp.models import Festival, HeadlineFestival, Label, TouringFestival


def _label_command() -> Command:
    command = Command()
    command._skipped_rule_notes.clear()
    clear_cascade_coverage(command)
    # Files the refusals and notes a run reports, which the arms are then rendered under.
    command._cascade_operations(Label)
    return command


def _label_arms(command: Command | None = None) -> dict[str, str]:
    """``related table -> archive arm`` for every cascade key into ``Label``: the cascade rules
    became arms of the owner's trigger in 2.19.0 (#80, ADR 0039), the joined form included."""
    command = command or _label_command()
    candidates, _selfs = command._cascade_candidates(Label, Label._meta.db_table, report=False)
    return {
        related._meta.db_table: command._archive_arm(
            (related._meta.db_table, Label._meta.db_table, field.column),
            related,
            field.column,
            'id',
        )
        for related, field, _primary in candidates
    }


class TestTheJoinedArm:
    def test_a_descendant_key_updates_the_ancestor_through_the_descendants_table(self):
        arm = _label_arms()['testapp_touringfestival']

        assert 'UPDATE "testapp_festival" AS guitars_child' in arm
        assert 'SET _deleted_at = guitars_archived._deleted_at' in arm
        assert 'SELECT guitars_link."festival_ptr_id" FROM "testapp_touringfestival"' in arm
        assert 'guitars_link."promoter_id" = guitars_archived.guitars_key' in arm
        # The guard on the ancestor's row, not the trigger condition's ``before IS NULL``.
        assert re.search(r'\)\s*AND guitars_child\._deleted_at IS NULL', arm)

    def test_the_pointer_is_the_descendants_own_not_the_roots(self):
        """In a default chain every key is its parent link, so the descendant's link column
        matches the ancestor's pk directly -- no multi-hop join, whatever the depth."""
        arm = _label_arms()['testapp_headlinefestival']

        assert 'UPDATE "testapp_festival" AS guitars_child' in arm
        assert 'SELECT guitars_link."touringfestival_ptr_id" FROM "testapp_headlinefestival"' in arm
        assert 'guitars_link."sponsor_id" = guitars_archived.guitars_key' in arm

    def test_the_flat_arm_for_the_roots_own_key_is_not_joined(self):
        arm = _label_arms()['testapp_festival']

        assert 'UPDATE "testapp_festival" AS guitars_child' in arm
        assert 'guitars_child."market_id" = guitars_archived.guitars_key' in arm
        assert 'guitars_link' not in arm

    def test_no_cascade_rule_is_written_for_any_of_them(self):
        command = Command()
        command._skipped_rule_notes.clear()
        clear_cascade_coverage(command)

        assert command._cascade_operations(Label) == []

    def test_each_joined_rule_has_a_revive_arm_against_the_ancestor(self):
        """In the owner's one revive trigger since 2.16.0 (#70), beside the flat arms."""
        command = Command()
        clear_cascade_coverage(command)
        (revive,) = [
            op
            for op in command._revive_operations(apps.get_app_config('testapp'))
            if op.startswith('# Soft Delete Revive Trigger on "testapp_label" table!')
        ]
        # Past the archive half and the refusal, which name the same links: the revive's own.
        revive = revive[revive.index('guitars_before._deleted_at IS NOT NULL') :]

        for related, link in (
            ('testapp_touringfestival', 'festival_ptr_id'),
            ('testapp_headlinefestival', 'touringfestival_ptr_id'),
        ):
            arm = revive.split(f'SELECT guitars_link."{link}" FROM "{related}"')[0].rsplit(
                'UPDATE ', 1
            )[1]
            assert arm.startswith('"testapp_festival" AS guitars_child'), related
            assert f'guitars_link."{link}"' in revive
        assert 'guitars_child._deleted_at = guitars_revived._deleted_at' in revive

    def test_nothing_is_reported_skipped_any_more(self):
        command = _label_command()

        assert not [n for n in command._skipped_rule_notes if 'testapp_touringfestival' in n]
        assert not [n for n in command._skipped_rule_notes if 'testapp_headlinefestival' in n]


class TestTheCycleGraphFollowsTheAncestor:
    @staticmethod
    @isolate_apps('tests.testapp')
    def _models():
        class Owner(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Root(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Child(Root):
            owner = ForeignKey(Owner, on_delete=CASCADE, related_name='children')

            class Meta:
                app_label = 'testapp'

        return Owner, Root, Child

    def test_the_edge_is_filed_against_the_table_the_rule_updates(self):
        """Only the joined form can produce it: ``Child`` owns no ``_deleted_at``, so the
        rule fires on ``Owner`` and updates ``Root`` -- never ``Child``'s own table."""
        owner, root, child = self._models()

        edges = _rule_update_edges([owner, root, child])

        assert (owner._meta.db_table, root._meta.db_table) in edges
        assert (owner._meta.db_table, child._meta.db_table) not in edges


class TestAnAncestorRoutedOffPostgresqlIsLeftAlone:
    """The rule names the ancestor's table, so a router sending it elsewhere is a table this DDL
    cannot name -- the gate the descendant already gets, asked of the second end too."""

    def test_the_generator_writes_no_joined_arm(self, monkeypatch):
        monkeypatch.setattr(introspection, 'migrates_to_postgresql', lambda m: m is not Festival)

        arms = _label_arms()

        assert 'testapp_touringfestival' not in arms
        assert 'testapp_headlinefestival' not in arms

    def test_the_cycle_graph_files_no_edge(self, monkeypatch):
        owner, root, child = TestTheCycleGraphFollowsTheAncestor._models()
        monkeypatch.setattr(introspection, 'migrates_to_postgresql', lambda m: m is not root)

        assert (owner._meta.db_table, root._meta.db_table) not in _rule_update_edges(
            [owner, root, child]
        )


class TestASelfReferencingDescendantIsRefused:
    """``Child`` cascades to its own ``Root``: the rule would sit on, and update, the root's
    table -- a rule rewritten into itself, which PostgreSQL refuses on *every* UPDATE there."""

    @staticmethod
    @isolate_apps('tests.testapp')
    def _models():
        class Root(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Child(Root):
            parent = ForeignKey(Root, on_delete=CASCADE, related_name='children')

            class Meta:
                app_label = 'testapp'

        return Root, Child

    def test_the_edge_closes_a_cycle(self):
        root, child = self._models()

        assert (root._meta.db_table, root._meta.db_table) in rule_update_cycle_edges([root, child])

    def test_the_generator_refuses_it_and_says_why(self):
        command = Command()
        command._skipped_rule_notes.clear()
        clear_cascade_coverage(command)
        root, child = self._models()
        command.reverse_relations_mapping[root] = {
            (child, child._meta.get_field('parent'), CASCADE)
        }

        assert command._cascade_operations(root) == []
        assert len(command._skipped_rule_notes) == 1
        assert 'infinite rule recursion' in command._skipped_rule_notes[0]


@pytest.fixture
def festivals(db):
    """``target`` is the label under test; every festival's ``market`` is ``other`` -- the flat
    rule on ``Festival.market`` would otherwise archive the row and hide whether the joined one
    ran. ``control`` rows point their joined key at ``other`` too, so they must stay live."""
    with tenancy_bypassed():
        target = Label.objects.create(name='Target Records')
        other = Label.objects.create(name='Other Records')
        touring = TouringFestival.objects.create(name='t', market=other, promoter=target)
        headline = HeadlineFestival.objects.create(
            name='h', market=other, promoter=other, sponsor=target
        )
        control = TouringFestival.objects.create(name='c', market=other, promoter=other)
    return types.SimpleNamespace(
        target=target, other=other, touring=touring, headline=headline, control=control
    )


def _raw_delete_label(pk):
    """Bypassed, since a rule's subselect runs under the invoker's row-level security: an
    unscoped session cannot see ``HeadlineFestival`` rows, whose table carries a tenant policy."""
    with tenancy_bypassed():
        execute('DELETE FROM testapp_label WHERE id = %s', params=[pk])


def _deleted_at(model, pk):
    with tenancy_bypassed():
        return model._all_objects.get(pk=pk)._deleted_at


@pytest.mark.django_db
class TestArchive:
    def test_a_raw_delete_archives_the_descendant_through_the_ancestor(self, festivals):
        _raw_delete_label(festivals.target.pk)

        stamp = _deleted_at(Label, festivals.target.pk)
        assert stamp is not None
        assert _deleted_at(Festival, festivals.touring.pk) == stamp
        assert _deleted_at(Festival, festivals.headline.pk) == stamp

    def test_a_row_whose_key_points_elsewhere_stays_live(self, festivals):
        _raw_delete_label(festivals.target.pk)

        assert _deleted_at(Festival, festivals.touring.pk) is not None  # the rule did run
        assert _deleted_at(Festival, festivals.control.pk) is None

    def test_the_orm_delete_ends_in_the_same_state(self, festivals):
        """Parity, not proof of the rule: Django's collector archives these in Python."""
        with tenancy_bypassed():
            Label.objects.filter(pk=festivals.target.pk).delete()

        assert _deleted_at(Festival, festivals.touring.pk) is not None
        assert _deleted_at(Festival, festivals.headline.pk) is not None
        assert _deleted_at(Festival, festivals.control.pk) is None

    def test_a_festival_archived_earlier_keeps_its_own_stamp(self, festivals):
        earlier = timezone.now() - timedelta(days=30)
        with tenancy_bypassed():
            Festival._all_objects.filter(pk=festivals.touring.pk).update(_deleted_at=earlier)

        _raw_delete_label(festivals.target.pk)

        assert _deleted_at(Festival, festivals.headline.pk) is not None  # the rule did run
        assert _deleted_at(Festival, festivals.touring.pk) == earlier


@pytest.mark.django_db
class TestRevive:
    def test_reviving_the_label_revives_the_descendant_it_archived(self, festivals):
        _raw_delete_label(festivals.target.pk)
        assert _deleted_at(Festival, festivals.touring.pk) is not None  # or this proves nothing
        assert _deleted_at(Festival, festivals.headline.pk) is not None

        with tenancy_bypassed():
            Label._all_objects.filter(pk=festivals.target.pk).update(_deleted_at=None)

        assert _deleted_at(Festival, festivals.touring.pk) is None
        assert _deleted_at(Festival, festivals.headline.pk) is None

    def test_a_festival_archived_before_the_label_stays_archived(self, festivals):
        earlier = timezone.now() - timedelta(days=30)
        with tenancy_bypassed():
            Festival._all_objects.filter(pk=festivals.touring.pk).update(_deleted_at=earlier)
        _raw_delete_label(festivals.target.pk)

        assert _deleted_at(Festival, festivals.headline.pk) is not None  # the rule did run

        with tenancy_bypassed():
            Label._all_objects.filter(pk=festivals.target.pk).update(_deleted_at=None)

        assert _deleted_at(Festival, festivals.headline.pk) is None  # and the revive did
        assert _deleted_at(Festival, festivals.touring.pk) == earlier


@pytest.mark.django_db
def test_the_archived_ancestors_updated_at_is_stamped_by_the_trigger(festivals):
    """Compared with ``now()`` rather than the earlier value: a test runs in one transaction, so
    the trigger's ``NOW()`` is its start, which precedes the row's creation timestamp."""
    _raw_delete_label(festivals.target.pk)

    with tenancy_bypassed():
        stamped = Festival._all_objects.get(pk=festivals.touring.pk)._updated_at
    assert stamped == scalar('SELECT now()')


class TestTwoJoinedKeysFromOneDescendant:
    """One child with two keys to the same owner takes the primary and the ``_via`` form, as two
    flat keys do -- each rule joined, each with its own revive twin, and no shared name."""

    @staticmethod
    @isolate_apps('tests.testapp')
    def _operations():
        class Owner(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Root(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Child(Root):
            first = ForeignKey(Owner, on_delete=CASCADE, related_name='firsts')
            second = ForeignKey(Owner, on_delete=CASCADE, related_name='seconds')

            class Meta:
                app_label = 'testapp'

        command = Command()
        command._skipped_rule_notes.clear()
        clear_cascade_coverage(command)
        command.reverse_relations_mapping[Owner] = {
            (Child, Child._meta.get_field(name), CASCADE) for name in ('first', 'second')
        }
        return command, command._cascade_operations(Owner), Child

    def test_two_keys_two_arms_each_way_and_no_rule(self):
        """No rule at all since 2.19.0 (#80): each key is an archive arm and a revive arm of the
        owner's one trigger (2.16.0), so there are no names to clash over."""
        command, operations, child = self._operations()

        assert operations == []
        assert command._rule_name_clashes == []
        assert command._skipped_rule_notes == []
        owner_table = child._meta.get_field('first').related_model._meta.db_table
        for column in ('first_id', 'second_id'):
            key = (child._meta.db_table, owner_table, column)
            arm = command._revive_arm(key, child, column, 'id')
            assert f'guitars_link."{column}" = guitars_revived."id"' in arm
            assert 'guitars_link."root_ptr_id"' in arm
            archive = command._archive_arm(key, child, column, 'id')
            assert f'guitars_link."{column}" = guitars_archived.guitars_key' in archive

    def test_each_archive_arm_is_joined_and_carries_its_own_column(self):
        command, _, child = self._operations()
        owner_table = child._meta.get_field('first').related_model._meta.db_table

        arms = [
            command._archive_arm((child._meta.db_table, owner_table, column), child, column, 'id')
            for column in ('first_id', 'second_id')
        ]

        assert all('SELECT guitars_link."root_ptr_id"' in arm and 'IN (' in arm for arm in arms)
        assert sorted(re.findall(r'guitars_link\."(\w+)" = guitars_archived', ' '.join(arms))) == [
            'first_id',
            'second_id',
        ]

    def test_the_via_key_recovers_no_column_on_retirement(self):
        """The ``_via`` spelling names its column in the key, which is not enough: the flat
        template would still be built against a table with no ``_deleted_at``."""
        command, _, child = self._operations()
        key = (child._meta.db_table, 'testapp_owner', 'second_id')

        assert command._retired_cascade_column(key, {child._meta.db_table: child}) is None


class TestRetiringAJoinedKey:
    """Its reverse refuses, for both halves: the key names no ancestor, and rebuilding it with the
    flat template would fail against a table that has no ``_deleted_at``."""

    @staticmethod
    def _command_without(model):
        command = Command()
        clear_cascade_coverage(command)
        command.reverse_relations_mapping[Label] = {
            relation
            for relation in command.reverse_relations_mapping[Label]
            if relation[0] is not model
        }
        return command

    @staticmethod
    def _retired(command, kind):
        app = apps.get_app_config('testapp')
        return [
            op
            for op in command._retired_cascade_operations(app)
            if op.startswith(f'# Soft Delete {kind} retired')
        ]

    def test_both_halves_refuse_to_reverse(self):
        command = self._command_without(TouringFestival)
        key = (TouringFestival._meta.db_table, Label._meta.db_table, None)
        command.existing.soft_delete_related[key] = 'abc'
        command.existing.soft_delete_revive[key] = 'def'

        (cascade,) = self._retired(command, 'Related Rule')
        (revive,) = self._retired(command, 'Revive Trigger')

        assert 'RAISE EXCEPTION' in cascade
        assert 'RAISE EXCEPTION' in revive

    def test_the_refusal_names_the_ancestor_not_a_column_it_failed_to_record(self):
        """The column is known for a joined key; what cannot be rebuilt is the ancestor the
        rule updated, so a message about an unrecorded column sends the reader the wrong way."""
        command = self._command_without(TouringFestival)
        key = (TouringFestival._meta.db_table, Label._meta.db_table, None)
        command.existing.soft_delete_related[key] = 'abc'
        command.existing.soft_delete_revive[key] = 'def'

        for kind in ('Related Rule', 'Revive Trigger'):
            (refusal,) = self._retired(command, kind)
            assert 'could not record which column' not in refusal
            assert 'ancestor' in refusal

    def test_a_model_that_lost_its_column_is_not_joined(self):
        """No ``_deleted_at`` at all is not "the ancestor holds it": such a key was flat, so its
        column is recoverable and its reverse rebuilds the rule, as it did before 2.12.0."""
        from tests.testapp.models import Genre  # noqa: PLC0415

        command = Command()
        by_table = {Genre._meta.db_table: Genre}

        assert (
            command._retired_cascade_column((Genre._meta.db_table, 'x', 'genre_id'), by_table)
            == 'genre_id'
        )
        assert not command._retired_key_is_joined(Genre._meta.db_table, by_table)

    def test_a_flat_key_is_still_reversible_so_the_refusal_is_not_universal(self):
        """The control: the same retirement for a key whose child owns the column rebuilds it."""
        command = self._command_without(Festival)
        key = (Festival._meta.db_table, Label._meta.db_table, None)
        command.existing.soft_delete_related[key] = 'abc'

        (cascade,) = self._retired(command, 'Related Rule')

        assert 'RAISE EXCEPTION' not in cascade
        assert 'CREATE OR REPLACE RULE' in cascade


class TestAToFieldKeyIsLeftToTheCollector:
    """The rule compares the key with the owner's primary key, so a ``to_field`` column fails
    ``migrate`` or archives the wrong rows. Skipped before 2.12.0, so it must not become the
    shape that breaks ``migrate``; the flat form's identical flaw is #59."""

    @staticmethod
    @isolate_apps('tests.testapp')
    def _models():
        from django.db.models import CharField  # noqa: PLC0415

        class Owner(SetarModel):
            slug = CharField(max_length=20, unique=True)

            class Meta:
                app_label = 'testapp'

        class Root(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Kid(Root):
            owner = ForeignKey(Owner, on_delete=CASCADE, to_field='slug', related_name='kids')

            class Meta:
                app_label = 'testapp'

        return Owner, Root, Kid

    def test_the_generator_writes_no_rule_and_says_why(self):
        command = Command()
        command._skipped_rule_notes.clear()
        clear_cascade_coverage(command)
        owner, _, kid = self._models()
        command.reverse_relations_mapping[owner] = {(kid, kid._meta.get_field('owner'), CASCADE)}

        assert command._cascade_operations(owner) == []
        assert len(command._skipped_rule_notes) == 1
        assert 'to_field' in command._skipped_rule_notes[0]

    def test_it_files_no_edge_in_the_cycle_graph(self):
        owner, root, kid = self._models()

        assert _rule_update_edges([owner, root, kid]) == set()

    def test_a_silent_run_still_skips_it(self):
        command = Command()
        clear_cascade_coverage(command)
        owner, _, kid = self._models()
        command.reverse_relations_mapping[owner] = {(kid, kid._meta.get_field('owner'), CASCADE)}

        candidates, selfs = command._cascade_candidates(owner, owner._meta.db_table, report=False)

        assert (candidates, selfs, command._skipped_rule_notes) == ([], [], [])


class TestARetirementForACycleSaysSo:
    """A key that closes a rule cycle is refused with every other edge on it, so a rule already
    live there is dropped; the note must say so, since "skipped" reads as "left alone"."""

    @staticmethod
    def _retire(monkeypatch, edges, *, relaxed=False):
        """*relaxed* removes the relation from the command's view, as a key made ``SET_NULL``."""
        command = Command()
        command._skipped_rule_notes.clear()
        clear_cascade_coverage(command)
        if relaxed:
            command.reverse_relations_mapping[Label] = {
                relation
                for relation in command.reverse_relations_mapping[Label]
                if relation[0] is not Festival
            }
        key = (Festival._meta.db_table, Label._meta.db_table, None)
        command.existing.soft_delete_related[key] = 'abc'
        monkeypatch.setattr(command, '_rule_cycle_edges', lambda: edges)
        for _ in range(2):  # a check run and a generation both ask; one note, not two
            command._retired_cascade_operations(apps.get_app_config('testapp'))
        return command._skipped_rule_notes

    def test_a_rule_dropped_because_of_a_cycle_is_named(self, monkeypatch):
        edges = {(Label._meta.db_table, Festival._meta.db_table)}

        (note,) = self._retire(monkeypatch, edges)

        assert 'dropped' in note
        assert 'testapp_festival' in note
        assert 'testapp_label' in note

    def test_a_rule_dropped_for_another_reason_adds_no_note(self, monkeypatch):
        assert self._retire(monkeypatch, set(), relaxed=True) == []

    def test_a_key_whose_on_delete_changed_on_a_shared_edge_adds_no_note(self, monkeypatch):
        from django.db.models import SET_NULL  # noqa: PLC0415

        command = Command()
        command._skipped_rule_notes.clear()
        clear_cascade_coverage(command)
        command.reverse_relations_mapping[Label] = {
            (model, field, SET_NULL if model is Festival else on_delete)
            for model, field, on_delete in command.reverse_relations_mapping[Label]
        }
        command.existing.soft_delete_related[
            (Festival._meta.db_table, Label._meta.db_table, None)
        ] = 'abc'
        edges = {(Label._meta.db_table, Festival._meta.db_table)}
        monkeypatch.setattr(command, '_rule_cycle_edges', lambda: edges)

        command._retired_cascade_operations(apps.get_app_config('testapp'))

        assert command._skipped_rule_notes == []

    def test_a_key_relaxed_on_a_shared_edge_adds_no_note(self, monkeypatch):
        """The edge belongs to every descendant of one ancestor, so it being on a cycle says
        nothing about a key that was relaxed: that one is retired for its own reason."""
        edges = {(Label._meta.db_table, Festival._meta.db_table)}

        assert self._retire(monkeypatch, edges, relaxed=True) == []


class TestTheParentLinkIsTheAncestorsNotTheDescendantsPk:
    """A descendant's own primary key need not be its link to the ancestor: it can declare an
    explicit one beside ``parent_link=True``, and a second concrete parent has a link of its own."""

    @staticmethod
    @isolate_apps('tests.testapp')
    def _explicit_pk():
        from django.db.models import AutoField, OneToOneField  # noqa: PLC0415

        class Owner(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Root(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Kid(Root):
            code = AutoField(primary_key=True)
            root_link = OneToOneField(Root, on_delete=CASCADE, parent_link=True)
            owner = ForeignKey(Owner, on_delete=CASCADE, related_name='kids')

            class Meta:
                app_label = 'testapp'

        return Owner, Kid

    @staticmethod
    @isolate_apps('tests.testapp')
    def _second_parent():
        from django.db import models  # noqa: PLC0415

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
            owner = ForeignKey(Owner, on_delete=CASCADE, related_name='boths')

            class Meta:
                app_label = 'testapp'

        return Owner, Both

    @staticmethod
    def _operations(owner, kid):
        command = Command()
        command._skipped_rule_notes.clear()
        clear_cascade_coverage(command)
        command.reverse_relations_mapping[owner] = {(kid, kid._meta.get_field('owner'), CASCADE)}
        # Files the refusals a run reports; the arms are then rendered for what it did not refuse.
        command._cascade_operations(owner)
        candidates, _selfs = command._cascade_candidates(
            owner, owner._meta.db_table, report=False
        )
        return command, '\n'.join(
            command._archive_arm((rel._meta.db_table, owner._meta.db_table, None), rel, f.column, 'id')
            for rel, f, _primary in candidates
        )

    def test_the_archive_arm_reads_the_link_not_the_descendants_own_key(self):
        owner, kid = self._explicit_pk()

        _, arm = self._operations(owner, kid)

        assert 'SELECT guitars_link."root_link_id" FROM "testapp_kid"' in arm
        assert '"code"' not in arm

    def test_the_revive_arm_reads_it_too(self):
        owner, kid = self._explicit_pk()
        command, _ = self._operations(owner, kid)

        arm = command._revive_arm(
            (kid._meta.db_table, owner._meta.db_table, None), kid, 'owner_id', 'id'
        )

        assert 'guitars_link."root_link_id"' in arm

    @staticmethod
    @isolate_apps('tests.testapp')
    def _explicit_pk_in_the_middle():
        from django.db.models import AutoField, OneToOneField  # noqa: PLC0415

        class Owner(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Root(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Mid(Root):
            code = AutoField(primary_key=True)
            root_link = OneToOneField(Root, on_delete=CASCADE, parent_link=True)

            class Meta:
                app_label = 'testapp'

        class Kid(Mid):
            owner = ForeignKey(Owner, on_delete=CASCADE, related_name='kids')

            class Meta:
                app_label = 'testapp'

        return Owner, Kid

    def test_an_indirect_link_through_a_mid_level_own_pk_gets_no_rule(self):
        """``Kid``'s link holds ``Mid``'s own key, not ``Root``'s, and one subselect cannot hop
        it. A ``Mid`` whose key *is* its link (the default) keeps the rule, as the festival tree shows."""
        owner, kid = self._explicit_pk_in_the_middle()

        command, blob = self._operations(owner, kid)

        assert blob == ''
        assert any('mid' in note.lower() for note in command._skipped_rule_notes)

    def test_the_indirect_link_files_no_edge_and_a_silent_run_says_nothing(self):
        owner, kid = self._explicit_pk_in_the_middle()
        command, _ = self._operations(owner, kid)
        command._skipped_rule_notes.clear()

        candidates, _ = command._cascade_candidates(owner, owner._meta.db_table, report=False)

        assert _rule_update_edges([owner, kid]) == set()
        assert (candidates, command._skipped_rule_notes) == ([], [])

    def test_a_descendant_the_check_refuses_files_no_edge(self):
        """An invented edge closes a cycle that cannot form and takes a legitimate rule with it."""
        owner, both = self._second_parent()

        assert _rule_update_edges([owner, both]) == set()

    def test_a_descendant_the_check_refuses_gets_no_rule(self):
        """``guitars.E003``'s shape: the generator re-asks it for the model's own rule, so it
        must for a key declared on the model too, or ``--skip-checks`` writes a rule through a
        link that is the wrong parent's."""
        owner, both = self._second_parent()

        _, blob = self._operations(owner, both)

        assert blob == ''
