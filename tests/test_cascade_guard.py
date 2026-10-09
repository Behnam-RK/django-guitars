"""The cascade guard (2.22.0, ADR 0044): a child inserted, or re-pointed, under a parent that is
archived takes the parent's stamp, where it used to stay live under it. What it does at runtime,
and how the generator writes, scans and retires it."""

import types
from datetime import datetime, timezone

import pytest
from django.apps import apps
from django.db import IntegrityError, connection, transaction

from guitars.management import _generator
from guitars.management.enforcement.command import Command
from guitars.management.enforcement.headers import (
    HEADER_SOFT_DELETE_GUARD,
    HEADER_SOFT_DELETE_GUARD_RETIRED,
)
from guitars.management.enforcement.scanning import scan_existing_operations
from tests.conftest import scalar
from tests.testapp.models import Lineage, Offer, Offshoot, Setlist, SetlistEntry, Tier


T_EARLY = datetime(2021, 6, 15, 12, 30, tzinfo=timezone.utc)


def _stamp(instance):
    return type(instance)._all_objects.values_list('_deleted_at', flat=True).get(pk=instance.pk)


def _restore(instance) -> None:
    type(instance)._all_objects.filter(pk=instance.pk).update(_deleted_at=None)


@pytest.fixture
def archived_offer(db):
    offer = Offer.objects.create(name='gone')
    offer.soft_delete()
    return offer


@pytest.mark.django_db
class TestAChildArrivingUnderAnArchivedParent:
    def test_an_insert_takes_the_parents_stamp(self, archived_offer):
        tier = Tier._all_objects.create(offer=archived_offer)

        assert _stamp(tier) == _stamp(archived_offer) is not None

    def test_a_row_re_pointed_at_it_takes_the_parents_stamp(self, archived_offer):
        tier = Tier.objects.create(offer=Offer.objects.create(name='live'))

        Tier._all_objects.filter(pk=tier.pk).update(offer=archived_offer)

        assert _stamp(tier) == _stamp(archived_offer)

    def test_a_save_of_the_loaded_instance_takes_it_too(self, archived_offer):
        tier = Tier.objects.create(offer=Offer.objects.create(name='live'))

        tier.offer = archived_offer
        tier.save()

        assert _stamp(tier) == _stamp(archived_offer)

    def test_the_restore_of_the_parent_brings_it_back(self, archived_offer):
        """The stamp is the parent's own, so the revive arm (ADR 0024) matches it."""
        tier = Tier._all_objects.create(offer=archived_offer)
        assert _stamp(tier) is not None

        _restore(archived_offer)

        assert _stamp(tier) is None

    def test_a_child_archived_on_its_own_keeps_its_stamp(self, archived_offer):
        tier = Tier._all_objects.create(offer=archived_offer, _deleted_at=T_EARLY)

        assert _stamp(tier) == T_EARLY

    def test_a_live_parent_leaves_the_child_live(self, db):
        tier = Tier.objects.create(offer=Offer.objects.create(name='live'))

        assert _stamp(tier) is None

    def test_a_save_that_leaves_the_key_alone_is_not_looked_at(self, archived_offer):
        """A child restored by hand under an archived parent stays restored: the guard reads a
        key at the moment it arrives, not on every write (ADR 0044)."""
        tier = Tier._all_objects.create(offer=archived_offer)
        _restore(tier)

        tier = Tier._all_objects.get(pk=tier.pk)
        tier.save()

        assert _stamp(tier) is None


@pytest.mark.django_db
class TestASelfKeyAndAJoinedKey:
    def test_a_self_key(self, db):
        root = Setlist.objects.create(title='root')
        root.soft_delete()

        child = Setlist._all_objects.create(title='child', parent=root)
        grandchild = Setlist._all_objects.create(title='grandchild', parent=child)

        assert _stamp(child) == _stamp(root) == _stamp(grandchild) is not None

    def test_a_flat_key_beside_a_self_key(self, db):
        """One function on the table, a block for each key: ``SetlistEntry`` has the one, its
        owner ``Setlist`` has the other."""
        root = Setlist.objects.create(title='root')
        root.soft_delete()

        entry = SetlistEntry._all_objects.create(song='s', setlist=root)

        assert _stamp(entry) == _stamp(root)

    def test_a_joined_key_archives_the_ancestors_row(self, db):
        """``Offshoot``'s ``_deleted_at`` lives on ``Lineage``, so the guard is an ``AFTER``
        trigger archiving that row, not a ``BEFORE`` one setting ``NEW``."""
        parent = Lineage.objects.create(name='parent')
        parent.soft_delete()

        offshoot = Offshoot._all_objects.create(name='late', parent=parent)

        assert _stamp(Lineage._all_objects.get(pk=offshoot.pk)) == _stamp(parent)
        assert _stamp(offshoot) == _stamp(parent)

    def test_a_joined_key_under_a_live_parent_changes_nothing(self, db):
        offshoot = Offshoot.objects.create(name='fine', parent=Lineage.objects.create(name='p'))

        assert _stamp(offshoot) is None

    def test_a_joined_key_with_no_parent_changes_nothing(self, db):
        assert _stamp(Offshoot.objects.create(name='root')) is None


GUARDED = 'soft_delete_guard_on_%'


def _guards_on(table: str) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT tgname FROM pg_trigger WHERE tgrelid = %s::regclass AND tgname LIKE %s',
            [table, GUARDED],
        )
        return [row[0] for row in cursor.fetchall()]


def test_the_guard_is_a_row_trigger_on_the_child_alone(db):
    assert len(_guards_on('testapp_tier')) == 1
    assert _guards_on('testapp_offer') == []


def test_the_guard_function_is_one_name_per_child_table(db):
    assert (
        scalar(
            'SELECT count(*) FROM pg_proc WHERE proname = ANY(%s)',
            [_guards_on('testapp_tier') + _guards_on('testapp_setlist')],
        )
        == 2
    )


def _command() -> Command:
    return Command()


def _ops(command: Command) -> list[str]:
    return command._guard_operations(apps.get_app_config('testapp'))


class TestTheGenerator:
    def test_a_recorded_guard_writes_nothing(self):
        assert _ops(_command()) == []

    def test_an_unrecorded_guard_is_created(self):
        command = _command()
        del command.existing.soft_delete_guard[('testapp_tier',)]

        [operation] = _ops(command)

        assert operation.startswith(HEADER_SOFT_DELETE_GUARD.format(table='testapp_tier'))
        assert 'FOR SHARE OF guitars_parent' in operation
        assert 'CREATE TRIGGER "soft_delete_guard_on_12_testapp_tier"' in operation
        assert 'BEFORE INSERT OR UPDATE ON "testapp_tier"' in operation

    def test_a_stale_digest_is_replaced_in_place(self):
        command = _command()
        command.existing.soft_delete_guard[('testapp_tier',)] = 'stale'

        [operation] = _ops(command)

        assert 'CREATE OR REPLACE TRIGGER' in operation

    def test_adopt_rewrites_every_guard_in_place(self):
        """Idempotent by construction: a ``CREATE OR REPLACE`` over what may or may not be there."""
        operations = _command()._guard_operations(apps.get_app_config('testapp'), adopt=True)

        assert operations
        assert all('CREATE OR REPLACE TRIGGER' in operation for operation in operations)

    def test_a_joined_guard_is_an_after_trigger_naming_the_ancestors_table(self):
        command = _command()
        del command.existing.soft_delete_guard[('testapp_offshoot',)]

        [operation] = _ops(command)

        assert 'AFTER INSERT OR UPDATE ON "testapp_offshoot"' in operation
        assert 'guitars_stamp "testapp_lineage"._deleted_at%TYPE' in operation
        assert 'WHERE "id" = NEW."lineage_ptr_id" AND _deleted_at IS NULL' in operation

    def test_a_routed_away_table_has_no_host(self, monkeypatch):
        command = _command()
        monkeypatch.setattr(command, '_routed_away_tables', lambda: {'testapp_tier'})

        assert command._guard_host('testapp_tier') is None

    def test_a_table_is_hosted_by_the_app_that_created_its_guard(self, monkeypatch):
        command = _command()
        command.existing.soft_delete_guard_dependencies[('testapp_tier',)] = [
            ('testapp', '0001_initial')
        ]
        monkeypatch.setattr(command, '_table_app_labels', lambda: {'testapp_tier': 'elsewhere'})

        assert command._guard_host('testapp_tier') == 'testapp'

    def test_without_a_recorded_create_the_tables_own_host_writes_it(self, monkeypatch):
        command = _command()
        command.existing.soft_delete_guard_dependencies.pop(('testapp_tier',), None)
        monkeypatch.setattr(command, '_table_app_labels', lambda: {'testapp_tier': 'elsewhere'})

        assert command._guard_host('testapp_tier') == 'elsewhere'

    def test_a_joined_guard_orders_itself_after_the_ancestors_column(self):
        """``%TYPE`` is read as the function is created, so the guard needs the edges the
        redirect rule does where the ancestor sits in another app."""
        command = _command()
        del command.existing.soft_delete_guard[('testapp_offshoot',)]

        _ops(command)

        refs = {(ref.model, ref.field) for ref in command._object_refs['testapp']}
        assert ('Lineage', None) in refs
        assert ('Lineage', '_deleted_at') in refs


class TestTheSettingOff:
    @pytest.fixture(autouse=True)
    def _off(self, settings):
        settings.GUITARS_CASCADE_GUARD = False

    def test_no_guard_is_called_for(self):
        assert _command()._guards_by_child() == {}
        assert _ops(_command()) == []

    def test_every_recorded_guard_is_retired(self):
        command = _command()
        recorded = {table for (table,) in command.existing.soft_delete_guard}

        retired = command._retired_guard_operations(apps.get_app_config('testapp'))

        assert recorded
        assert len(retired) == len(recorded)
        assert all(
            operation.startswith(HEADER_SOFT_DELETE_GUARD_RETIRED.format(table=table))
            for operation, table in zip(retired, sorted(recorded))
        )
        assert all('DROP TRIGGER IF EXISTS' in operation for operation in retired)

    def test_a_retirement_is_not_written_twice(self):
        command = _command()
        command.existing.soft_delete_guard.clear()

        assert command._retired_guard_operations(apps.get_app_config('testapp')) == []


def _scan_with(monkeypatch, *file_contents: str):
    def _iter(app):
        for index, content in enumerate(file_contents):
            yield types.SimpleNamespace(stem=f'{index:04d}_auto_enforcement'), content

    monkeypatch.setattr(_generator, 'iter_migration_files', _iter)
    return scan_existing_operations()


def _header(template) -> str:
    return template.format(table='testapp_tier') + ' [SQL:abc123def456]\n'


class TestTheScan:
    def test_a_create_is_recorded_by_table_and_digest(self, monkeypatch):
        existing = _scan_with(monkeypatch, _header(HEADER_SOFT_DELETE_GUARD))

        assert existing.soft_delete_guard[('testapp_tier',)] == 'abc123def456'

    def test_a_retirement_forgets_it(self, monkeypatch):
        existing = _scan_with(
            monkeypatch,
            _header(HEADER_SOFT_DELETE_GUARD),
            _header(HEADER_SOFT_DELETE_GUARD_RETIRED),
        )

        assert ('testapp_tier',) not in existing.soft_delete_guard

    def test_a_create_after_a_retirement_records_it_again(self, monkeypatch):
        existing = _scan_with(
            monkeypatch,
            _header(HEADER_SOFT_DELETE_GUARD),
            _header(HEADER_SOFT_DELETE_GUARD_RETIRED),
            _header(HEADER_SOFT_DELETE_GUARD),
        )

        assert ('testapp_tier',) in existing.soft_delete_guard
