"""A CASCADE key declared on an MTI descendant's table while ``_deleted_at`` lives on an ancestor:
the rule updates the ancestor, reaching it through the descendant's table. Before 2.12.0 the
generator skipped these, so only Django's Python ``Collector`` archived such children."""

from __future__ import annotations

import types
from datetime import timedelta

import pytest
from django.db.models import CASCADE, ForeignKey
from django.utils import timezone
from django.test.utils import isolate_apps

from guitars import introspection
from guitars.introspection import _rule_update_edges, rule_update_cycle_edges
from guitars.management.enforcement import operations as operations_module
from guitars.management.enforcement.command import Command
from guitars.models import SetarModel
from guitars.tenancy import tenancy_bypassed
from tests.conftest import clear_cascade_coverage, execute, scalar
from tests.testapp.models import Festival, HeadlineFestival, Label, TouringFestival


def _label_operations() -> tuple[Command, str]:
    command = Command()
    command._skipped_rule_notes.clear()
    clear_cascade_coverage(command)
    return command, '\n'.join(command._cascade_operations(Label))


class TestTheJoinedRule:
    def test_a_descendant_key_updates_the_ancestor_through_the_descendants_table(self):
        _, blob = _label_operations()

        assert 'RULE "soft_delete_related_testapp_touringfestival"' in blob
        rule = blob.split('RULE "soft_delete_related_testapp_touringfestival"')[1].split(');')[0]
        assert 'UPDATE "testapp_festival"' in rule
        assert 'SET _deleted_at = new._deleted_at' in rule
        assert 'SELECT "festival_ptr_id" FROM "testapp_touringfestival"' in rule
        assert '"promoter_id" = old."id"' in rule
        assert '_deleted_at IS NULL' in rule

    def test_the_pointer_is_the_descendants_own_not_the_roots(self):
        """Every table in a chain stores one pk value, so the descendant's parent-link column
        matches the ancestor's pk directly -- no multi-hop join, whatever the depth."""
        _, blob = _label_operations()

        rule = blob.split('RULE "soft_delete_related_testapp_headlinefestival"')[1].split(');')[0]
        assert 'UPDATE "testapp_festival"' in rule
        assert 'SELECT "touringfestival_ptr_id" FROM "testapp_headlinefestival"' in rule
        assert '"sponsor_id" = old."id"' in rule

    def test_the_flat_rule_for_the_roots_own_key_is_unchanged(self):
        _, blob = _label_operations()

        rule = blob.split('RULE "soft_delete_related_testapp_festival"')[1].split(');')[0]
        assert 'UPDATE "testapp_festival"' in rule
        assert '"market_id" = old."id"' in rule
        assert 'SELECT' not in rule

    def test_each_joined_rule_has_a_revive_twin_against_the_ancestor(self):
        _, blob = _label_operations()

        for related in ('testapp_touringfestival', 'testapp_headlinefestival'):
            assert f'_{related}"\n' in blob, related
        assert 'UPDATE "testapp_festival" AS guitars_child' in blob
        assert 'guitars_child._deleted_at = guitars_revived._deleted_at' in blob

    def test_nothing_is_reported_skipped_any_more(self):
        command, _ = _label_operations()

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

    def test_the_generator_writes_no_joined_rule(self, monkeypatch):
        monkeypatch.setattr(
            operations_module, 'migrates_to_postgresql', lambda m: m is not Festival
        )

        _, blob = _label_operations()

        assert 'soft_delete_related_testapp_touringfestival' not in blob
        assert 'soft_delete_related_testapp_headlinefestival' not in blob

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
        command.reverse_relations_mapping[root] = {(child, child._meta.get_field('parent'), CASCADE)}

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
