"""A ``CASCADE`` key with ``to_field`` (#59): the rule, its revive arm and the self-cascade trigger
match children on the target's column, pairing a row across a statement on the pk. A key into
the primary key renders exactly as it always has (ADR 0035)."""

from __future__ import annotations

import pytest
from django.db.models import CASCADE, CharField, ForeignKey
from django.test.utils import isolate_apps

from guitars.introspection import CascadeKind, classify_cascade, to_field_refusal
from guitars.management.enforcement.command import Command
from guitars.models import SetarModel
from tests.conftest import clear_cascade_coverage


@isolate_apps('tests.testapp')
def _shapes():
    class Owner(SetarModel):
        code = CharField(max_length=20, unique=True)

        class Meta:
            app_label = 'testapp'

    class ByCode(SetarModel):
        owner = ForeignKey(Owner, on_delete=CASCADE, to_field='code', related_name='by_code')

        class Meta:
            app_label = 'testapp'

    class ByPk(SetarModel):
        owner = ForeignKey(Owner, on_delete=CASCADE, related_name='by_pk')

        class Meta:
            app_label = 'testapp'

    class Folder(SetarModel):
        code = CharField(max_length=20, unique=True)
        parent = ForeignKey(
            'self', on_delete=CASCADE, to_field='code', null=True, related_name='children'
        )

        class Meta:
            app_label = 'testapp'

    class Root(SetarModel):
        class Meta:
            app_label = 'testapp'

    class Kid(Root):
        slug = CharField(max_length=20, unique=True)

        class Meta:
            app_label = 'testapp'

    class BySlug(SetarModel):
        kid = ForeignKey(Kid, on_delete=CASCADE, to_field='slug', related_name='by_slug')

        class Meta:
            app_label = 'testapp'

    return Owner, ByCode, ByPk, Folder, Root, Kid, BySlug


def _command(owner, *relations) -> Command:
    command = Command()
    clear_cascade_coverage(command)
    command._skipped_rule_notes.clear()
    command.reverse_relations_mapping[owner] = {
        (model, model._meta.get_field(name), CASCADE) for model, name in relations
    }
    return command


class TestTheFlatRule:
    def test_a_to_field_key_matches_on_the_column_it_holds(self):
        owner, by_code, by_pk, *_ = _shapes()
        command = _command(owner, (by_code, 'owner'), (by_pk, 'owner'))

        rules = '\n'.join(command._cascade_operations(owner))

        assert 'WHERE "owner_id" = old."code"' in rules
        # The pk-targeting sibling reads the pk, as it always has -- the same text, so its
        # recorded ``[SQL:]`` digest does not move and nothing is re-emitted for it.
        assert 'WHERE "owner_id" = old."id"' in rules

    def test_a_to_field_column_is_not_read_through_the_primary_key(self):
        owner, by_code, *_ = _shapes()
        command = _command(owner, (by_code, 'owner'))

        (rule, *_rest) = command._cascade_operations(owner)

        assert 'old."id"' not in rule


class TestTheRevivePairsOnThePrimaryKeyAndMatchesOnTheColumn:
    def test_the_arm(self):
        owner, by_code, by_pk, *_ = _shapes()
        command = _command(owner, (by_code, 'owner'), (by_pk, 'owner'))
        command._cascade_key_maps()

        arm = command._revive_arm(('t', 'o', None), by_code, 'owner_id', 'id')
        flat = command._revive_arm(('t', 'o', None), by_pk, 'owner_id', 'id')

        assert 'ON guitars_after."id" = guitars_before."id"' in arm
        assert 'guitars_child."owner_id" = guitars_revived."code"' in arm
        assert 'guitars_child."owner_id" = guitars_revived."id"' in flat


class TestTheSelfCascadeTrigger:
    def test_it_matches_children_on_the_column_and_pairs_on_the_primary_key(self):
        folder = _shapes()[3]
        command = Command()
        command._skipped_rule_notes.clear()
        clear_cascade_coverage(command)
        ops: list[str] = []

        command._self_cascade_operation(
            ops,
            owner=folder,
            owner_table='testapp_folder',
            header_owner_table='testapp_folder',
            ident_owner_table='"testapp_folder"',
            ident_owner_pk='id',
            foreign_key='parent_id',
            adopt=False,
            app_label='testapp',
        )

        (trigger,) = ops
        assert 'guitars_child."parent_id" = guitars_after."code"' in trigger
        assert 'SELECT guitars_before."code" AS guitars_key' in trigger
        assert 'guitars_vanished."code"' in trigger
        # Pairing one row across the two images is by identity, whatever the child holds.
        assert 'ON guitars_after."id" = guitars_before."id"' in trigger
        assert 'guitars_after."code" = guitars_before."code"' not in trigger


class TestWhatTheRuleCannotRead:
    def test_a_column_on_another_table_than_the_rule_fires_on_is_refused(self):
        """The rule fires on the ``_deleted_at`` holder and reads ``old.<column>``: a column
        declared on a descendant is not on that table."""
        _owner, _by_code, _by_pk, _folder, root, kid, by_slug = _shapes()
        owner_table = root._meta.db_table
        field = by_slug._meta.get_field('kid')

        reason = to_field_refusal(field, owner_table)

        assert reason is not None
        assert "'slug'" in reason
        assert (
            classify_cascade(by_slug, field, CASCADE, owner_table, set()) is CascadeKind.REFUSED
        )

    def test_it_is_named_and_writes_nothing(self):
        _owner, _by_code, _by_pk, _folder, root, kid, by_slug = _shapes()
        command = _command(root, (by_slug, 'kid'))

        ops = command._cascade_operations(root)

        assert ops == []
        assert any("'slug'" in note for note in command._skipped_rule_notes)

    def test_a_column_on_the_holders_own_table_is_fine(self):
        owner, by_code, *_ = _shapes()
        field = by_code._meta.get_field('owner')

        assert to_field_refusal(field, owner._meta.db_table) is None
        assert (
            classify_cascade(by_code, field, CASCADE, owner._meta.db_table, set())
            is CascadeKind.RULE
        )

    def test_a_key_into_the_primary_key_is_never_refused_for_it(self):
        owner, _by_code, by_pk, *_ = _shapes()

        assert to_field_refusal(by_pk._meta.get_field('owner'), owner._meta.db_table) is None


def test_a_refused_to_field_key_is_not_a_rule_update_edge():
    """An edge for a rule never written closes a cycle that cannot form (see
    ``_rule_update_edges``) and takes the legitimate rule pointing back down with it. Here the
    unreadable key ``Spoke.kid`` would be the edge back to ``Kid.spoke``'s."""
    from guitars.introspection import rule_update_cycle_edges  # noqa: PLC0415

    @isolate_apps('tests.testapp')
    def _build():
        class Spoke(SetarModel):
            kid = ForeignKey('Kid', on_delete=CASCADE, to_field='slug', related_name='spokes')

            class Meta:
                app_label = 'testapp'

        class Root(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Kid(Root):
            slug = CharField(max_length=20, unique=True)
            spoke = ForeignKey(Spoke, on_delete=CASCADE, related_name='kids', null=True)

            class Meta:
                app_label = 'testapp'

        return rule_update_cycle_edges([Spoke, Root, Kid])

    assert _build() == set()


def test_a_to_field_naming_no_field_is_refused_not_raised():
    """``fields.E312`` reports it; resolving it must not take the generator down first."""
    from guitars.management.enforcement.operations import _referenced_key  # noqa: PLC0415

    @isolate_apps('tests.testapp')
    def _build():
        class Owner(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Child(SetarModel):
            owner = ForeignKey(Owner, on_delete=CASCADE, to_field='nope', related_name='kids')

            class Meta:
                app_label = 'testapp'

        field = Child._meta.get_field('owner')
        return (
            to_field_refusal(field, Owner._meta.db_table),
            _referenced_key(Child, 'owner_id', '"id"'),
        )

    reason, key = _build()

    assert reason is not None and 'naming no field' in reason
    assert key == '"id"'


@pytest.mark.django_db
class TestTheDatabase:
    """Raw SQL, no ORM: the path the rules and triggers exist for."""

    @staticmethod
    def _archive(table: str, pk: int) -> None:
        from django.db import connection  # noqa: PLC0415

        with connection.cursor() as cursor:
            cursor.execute(f'UPDATE {table} SET _deleted_at = NOW() WHERE id = %s', [pk])

    @staticmethod
    def _revive(table: str, pk: int) -> None:
        from django.db import connection  # noqa: PLC0415

        with connection.cursor() as cursor:
            cursor.execute(f'UPDATE {table} SET _deleted_at = NULL WHERE id = %s', [pk])

    def test_a_char_column_archives_and_revives_its_children(self):
        from tests.testapp.models import Catalog, Listing  # noqa: PLC0415

        mine, other = Catalog.objects.create(code='north'), Catalog.objects.create(code='south')
        ours = Listing.objects.create(catalog=mine, name='a')
        theirs = Listing.objects.create(catalog=other, name='b')

        self._archive('testapp_catalog', mine.pk)

        assert Listing._all_objects.get(pk=ours.pk)._deleted_at is not None
        assert Listing._all_objects.get(pk=theirs.pk)._deleted_at is None

        self._revive('testapp_catalog', mine.pk)

        assert Listing._all_objects.get(pk=ours.pk)._deleted_at is None

    def test_an_integer_column_archives_the_rows_holding_it_not_the_ones_whose_pk_matches(self):
        """Ticket 1 is numbered 2 and ticket 2 is numbered 1: the old rule, correlating on the
        pk, archived the *other* ticket's seats."""
        from tests.testapp.models import Seat, Ticket  # noqa: PLC0415

        first, second = Ticket.objects.create(number=2), Ticket.objects.create(number=1)
        on_first = Seat.objects.create(ticket=first)
        on_second = Seat.objects.create(ticket=second)

        self._archive('testapp_ticket', first.pk)

        assert Seat._all_objects.get(pk=on_first.pk)._deleted_at is not None
        assert Seat._all_objects.get(pk=on_second.pk)._deleted_at is None

    def test_a_tree_through_a_to_field_archives_whole_and_leaves_its_neighbour(self):
        from tests.testapp.models import Folio  # noqa: PLC0415

        root = Folio.objects.create(code='r')
        child = Folio.objects.create(code='c', parent=root)
        grandchild = Folio.objects.create(code='g', parent=child)
        neighbour = Folio.objects.create(code='n')
        neighbours_child = Folio.objects.create(code='nc', parent=neighbour)

        self._archive('testapp_folio', root.pk)

        archived = {
            folio.code
            for folio in Folio._all_objects.filter(pk__in=[root.pk, child.pk, grandchild.pk])
            if folio._deleted_at is not None
        }
        assert archived == {'r', 'c', 'g'}
        assert Folio._all_objects.get(pk=neighbour.pk)._deleted_at is None
        assert Folio._all_objects.get(pk=neighbours_child.pk)._deleted_at is None

    def test_soft_delete_and_the_fast_path_take_it(self):
        """No longer a gap that refuses or declines them."""
        from guitars.models.soft_deletion import _fast_delete_applies  # noqa: PLC0415
        from tests.testapp.models import Catalog, Listing  # noqa: PLC0415

        owner = Catalog.objects.create(code='east')
        child = Listing.objects.create(catalog=owner, name='x')

        assert _fast_delete_applies(Catalog, 'default') is True
        assert Catalog.objects.filter(pk=owner.pk).soft_delete() == 1
        assert Listing._all_objects.get(pk=child.pk)._deleted_at is not None
