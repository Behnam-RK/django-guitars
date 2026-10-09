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


@isolate_apps('tests.testapp')
def _a_parent_keyed_to_its_own_child():
    class Parent(SetarModel):
        kid = ForeignKey(
            'testapp.Child', on_delete=CASCADE, to_field='slug', null=True, related_name='+'
        )

        class Meta:
            app_label = 'testapp'

    class Child(Parent):
        slug = CharField(max_length=20, unique=True)

        class Meta:
            app_label = 'testapp'

    return Parent, Child


def _command(owner, *relations) -> Command:
    command = Command()
    clear_cascade_coverage(command)
    command._skipped_rule_notes.clear()
    command.reverse_relations_mapping[owner] = {
        (model, model._meta.get_field(name), CASCADE) for model, name in relations
    }
    return command


class TestTheArchiveArm:
    """The flat cascade, an arm of the owner's trigger since 2.19.0 (#80, ADR 0039); it was a rule."""

    def test_a_to_field_key_matches_on_the_column_it_holds(self):
        owner, by_code, by_pk, *_ = _shapes()
        command = _command(owner, (by_code, 'owner'), (by_pk, 'owner'))

        by_column = command._archive_arm(('t', 'o', None), by_code, 'owner_id', 'id')
        by_key = command._archive_arm(('t', 'o', None), by_pk, 'owner_id', 'id')

        assert 'SELECT guitars_before."code" AS guitars_key' in by_column
        assert 'guitars_child."owner_id" = guitars_archived.guitars_key' in by_column
        # The pk-targeting sibling reads the pk, as the rule always did.
        assert 'SELECT guitars_before."id" AS guitars_key' in by_key

    def test_a_to_field_column_is_not_read_through_the_primary_key(self):
        owner, by_code, *_ = _shapes()
        command = _command(owner, (by_code, 'owner'))

        arm = command._archive_arm(('t', 'o', None), by_code, 'owner_id', 'id')

        assert 'guitars_before."id" AS guitars_key' not in arm


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
            classify_cascade(by_slug, field, CASCADE, owner_table) is CascadeKind.REFUSED
        )

    def test_a_parent_keyed_to_its_own_child_is_refused_not_routed_to_the_self_trigger(self):
        """Same table, so ``SELF`` -- but the trigger reads ``guitars_after."slug"`` off a table
        that has no such column, and every ``UPDATE`` of it would fail."""
        parent, _child = _a_parent_keyed_to_its_own_child()
        field = parent._meta.get_field('kid')

        kind = classify_cascade(parent, field, CASCADE, parent._meta.db_table)

        assert kind is CascadeKind.REFUSED

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
            classify_cascade(by_code, field, CASCADE, owner._meta.db_table)
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


class TestAKeyIntoAModelGuitarsE005Refuses:
    """Its column stores the refused child's own key where the ancestor's rule compares the
    ancestor's id (#64): the rule would archive another row, so none is written."""

    @staticmethod
    @isolate_apps('tests.testapp')
    def _shapes():
        from django.db.models import AutoField, OneToOneField  # noqa: PLC0415

        class Root(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Kid(Root):
            code = AutoField(primary_key=True)
            link = OneToOneField(Root, on_delete=CASCADE, parent_link=True)

            class Meta:
                app_label = 'testapp'

        class Pointer(SetarModel):
            kid = ForeignKey(Kid, on_delete=CASCADE, related_name='pointers')

            class Meta:
                app_label = 'testapp'

        return Root, Kid, Pointer

    def test_it_is_refused_and_named(self):
        root, _kid, pointer = self._shapes()
        field = pointer._meta.get_field('kid')

        assert classify_cascade(pointer, field, CASCADE, root._meta.db_table) is (
            CascadeKind.REFUSED
        )
        command = _command(root, (pointer, 'kid'))
        assert command._cascade_operations(root) == []
        assert any('guitars.E005' in note for note in command._skipped_rule_notes)


class TestRetiringAKeyThatBecameUnreadable:
    def test_the_reverse_refuses_rather_than_naming_a_column_the_table_lacks(self):
        root, _kid, by_slug = _shapes()[4:]
        command = Command()
        key = (by_slug._meta.db_table, root._meta.db_table, None)
        models_by_table = {by_slug._meta.db_table: by_slug}

        assert command._retired_cascade_column(key, models_by_table) is None

    def test_a_readable_key_keeps_its_column(self):
        owner, by_code, *_ = _shapes()
        command = Command()
        key = (by_code._meta.db_table, owner._meta.db_table, None)

        assert command._retired_cascade_column(key, {by_code._meta.db_table: by_code}) == 'owner_id'


class TestTheRetirementsRebuildWhatTheyDrop:
    """A retired key is rebuilt on unapply as it was written: the same ``to_field`` column."""

    @staticmethod
    def _retire(table: str, owner: str, command: Command | None = None):
        from django.apps import apps  # noqa: PLC0415

        command = command or Command()
        clear_cascade_coverage(command)
        key = (table, owner, None)
        command.existing.soft_delete_related[key] = 'abc'
        command.existing.soft_delete_revive[key] = 'def'
        required, models_by_table = command._cascade_key_maps()
        command._cascade_key_maps_cache = (
            {k: v for k, v in required.items() if k != key},
            models_by_table,
        )
        return command._retired_cascade_operations(apps.get_app_config('testapp'))

    def test_a_relaxed_key_rebuilds_its_rule_and_revive_on_the_column(self):
        retired = '\n'.join(self._retire('testapp_seat', 'testapp_ticket'))

        assert 'WHERE "ticket_id" = old."number"' in retired
        assert 'guitars_revived."number"' in retired
        assert 'old."id"' not in retired.split('reverse_sql')[1]

    def test_a_superseded_per_key_revive_rebuilds_on_the_column(self):
        """The 2.16.0 upgrade retires every per-key revive; unapplying rebuilds this one."""
        from django.apps import apps  # noqa: PLC0415

        command = Command()
        clear_cascade_coverage(command)
        command.existing.soft_delete_revive[('testapp_listing', 'testapp_catalog', None)] = 'x'

        (retirement,) = command._retired_cascade_operations(apps.get_app_config('testapp'))

        assert 'guitars_child."catalog_id" = guitars_revived."code"' in retirement

    @staticmethod
    def _with_dollars_in_the_column(monkeypatch):
        from guitars.management.enforcement import operations  # noqa: PLC0415

        monkeypatch.setattr(operations, '_referenced_key', lambda *args: '"a$$b"')

    def test_a_retired_revive_naming_dollars_is_refused_not_rebuilt(self, monkeypatch):
        """The body is dollar-quoted, so a name holding ``$$`` closes it early: the forward path
        skips such a key, and the reverse says so rather than run a broken function."""
        self._with_dollars_in_the_column(monkeypatch)

        retired = '\n'.join(self._retire('testapp_seat', 'testapp_ticket'))

        assert 'cannot be recreated' in retired and 'dollar signs' in retired
        assert 'CREATE OR REPLACE FUNCTION' not in retired.split('reverse_sql')[-1]

    def test_the_cascade_rule_beside_it_is_still_rebuilt(self, monkeypatch):
        """Plain SQL, not dollar-quoted: only the revive's reverse is refused."""
        self._with_dollars_in_the_column(monkeypatch)

        retired = '\n'.join(self._retire('testapp_seat', 'testapp_ticket'))

        assert 'CREATE OR REPLACE RULE' in retired

    def test_a_superseded_revive_naming_dollars_is_refused_not_rebuilt(self, monkeypatch):
        from django.apps import apps  # noqa: PLC0415

        self._with_dollars_in_the_column(monkeypatch)
        command = Command()
        clear_cascade_coverage(command)
        command.existing.soft_delete_revive[('testapp_listing', 'testapp_catalog', None)] = 'x'

        (retirement,) = command._retired_cascade_operations(apps.get_app_config('testapp'))

        assert 'cannot be recreated' in retirement and 'dollar signs' in retirement


@pytest.mark.django_db
def test_a_statement_that_archives_and_rewrites_the_column_archives_by_the_before_image():
    """Pairing a row across a statement is on the pk, so rewriting its ``to_field`` value in the
    same statement is no obstacle; the children hold the *old* value, which is what is matched."""
    from django.db import connection  # noqa: PLC0415

    from tests.testapp.models import Catalog, Listing  # noqa: PLC0415

    owner = Catalog.objects.create(code='old')
    child = Listing.objects.create(catalog=owner, name='x')

    with connection.cursor() as cursor:
        cursor.execute(
            'UPDATE testapp_catalog SET _deleted_at = NOW(), code = %s WHERE id = %s',
            ['new', owner.pk],
        )
        archived = Listing._all_objects.get(pk=child.pk)._deleted_at is not None
        # The deferred key would fail at teardown otherwise: the child follows its parent's value.
        cursor.execute('UPDATE testapp_listing SET catalog_id = %s WHERE id = %s', ['new', child.pk])

    assert archived
