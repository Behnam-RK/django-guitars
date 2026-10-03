"""A cascading child whose model was deleted (#63). ``DROP TABLE ... CASCADE`` takes the cascade
rule, which names the child in its action, but not the revive trigger on the owner: a plpgsql
body records no dependency. The trigger then fails every ``UPDATE`` on the owner."""

import pytest
from django.apps import apps
from django.db import ProgrammingError, connection, migrations, transaction
from django.db.migrations.graph import MigrationGraph
from django.db.migrations.operations import (
    CreateModel,
    DeleteModel,
    RenameModel,
    SeparateDatabaseAndState,
)
from django.db import models

from guitars.management.enforcement import graph
from guitars.management.enforcement.command import Command
from guitars.operations import RetireEnforcement
from tests.conftest import clear_cascade_coverage, execute, scalar

CHILD, OWNER = 'testapp_setlistentry', 'testapp_setlist'
KEY = (CHILD, OWNER, None)
REVIVE = 'soft_delete_revive_15_testapp_setlist_20_testapp_setlistentry'


def _revive_is_live() -> bool:
    return bool(scalar('SELECT count(*) FROM pg_trigger WHERE tgname = %s', [REVIVE]))


def _update_owner():
    execute(f'UPDATE {OWNER} SET title = title')


@pytest.mark.django_db
class TestTheLeak:
    def test_dropping_the_child_leaves_a_trigger_that_breaks_the_owner(self):
        with pytest.raises(ProgrammingError, match=f'relation "{CHILD}" does not exist'):
            with transaction.atomic():
                execute(f'DROP TABLE {CHILD} CASCADE')
                assert _revive_is_live()
                _update_owner()

    def test_retire_enforcement_on_the_child_does_not_reach_it(self):
        """It drops what *depends* on the child and the child's own triggers. The owner's revive
        trigger is neither, so it is no workaround for this."""
        with transaction.atomic():
            with connection.schema_editor() as editor:
                RetireEnforcement(CHILD).database_forwards('testapp', editor, None, None)
            assert _revive_is_live()
            transaction.set_rollback(True)


def _command(monkeypatch, *, dropped: set[str]):
    """The child's model deleted: its table maps to nothing and no key calls for it."""
    command = Command()
    clear_cascade_coverage(command)
    hosting, key_maps = command._table_app_labels, command._cascade_key_maps

    def without_child():
        return {table: app for table, app in hosting().items() if table != CHILD}

    def maps_without_child():
        required, by_table = key_maps()
        return (
            {key: value for key, value in required.items() if key[0] != CHILD},
            {table: model for table, model in by_table.items() if table != CHILD},
        )

    monkeypatch.setattr(command, '_table_app_labels', without_child)
    monkeypatch.setattr(command, '_cascade_key_maps', maps_without_child)
    monkeypatch.setattr(command, '_dropped_tables', lambda: dropped)
    command.existing.soft_delete_related[KEY] = 'abc'
    command.existing.soft_delete_revive[KEY] = 'def'
    return command


def _retirements(command) -> list[str]:
    return command._retired_cascade_operations(apps.get_app_config('testapp'))


def _forward_sql(operation: str) -> str:
    source = operation[operation.index('migrations.RunSQL') :].rstrip().rstrip(',')
    return eval(source, {'migrations': migrations}).sql  # noqa: S307 - our own output


class TestADroppedChildIsRetired:
    def test_both_halves_are_retired(self, monkeypatch):
        command = _command(monkeypatch, dropped={CHILD})

        retired = _retirements(command)

        assert [op.split('\n')[0].split('!')[0] for op in retired] == [
            f'# Soft Delete Related Rule retired on "{CHILD}" that is related to "{OWNER}"',
            f'# Soft Delete Revive Trigger retired on "{CHILD}" that is related to "{OWNER}"',
        ]

    def test_the_rule_drop_tolerates_the_rule_being_gone(self, monkeypatch):
        """``DROP TABLE ... CASCADE`` already took it on a database that ran the deletion."""
        rule, _revive = _retirements(_command(monkeypatch, dropped={CHILD}))

        assert 'DROP RULE IF EXISTS' in _forward_sql(rule)

    def test_the_trigger_drop_tolerates_a_hand_drop(self, monkeypatch):
        """#63's stopgap drops the trigger by hand, and a plain ``DROP`` would then fail
        ``migrate`` on the project that followed it."""
        _rule, revive = _retirements(_command(monkeypatch, dropped={CHILD}))

        assert 'DROP TRIGGER IF EXISTS' in _forward_sql(revive)
        assert 'DROP FUNCTION IF EXISTS' in _forward_sql(revive)

    def test_a_renamed_then_deleted_child_drops_every_spelling(self, monkeypatch):
        """Objects named before the rename are the ones a project generated earliest; dropping
        only the current spelling left the live trigger, ``IF EXISTS`` hiding that it missed."""
        command = _command(monkeypatch, dropped={CHILD})
        command.existing.renamed_tables[CHILD] = ['testapp_oldentry']

        _rule, revive = _retirements(command)

        assert 'soft_delete_revive_15_testapp_setlist_16_testapp_oldentry' in _forward_sql(revive)
        assert REVIVE in _forward_sql(revive)

    def test_the_reverse_refuses(self, monkeypatch):
        for operation in _retirements(_command(monkeypatch, dropped={CHILD})):
            assert 'RAISE EXCEPTION' in operation

    def test_it_is_no_longer_named_as_unretirable(self, monkeypatch):
        assert _command(monkeypatch, dropped={CHILD})._unmapped_cascade_notes() == []

    def test_a_run_scoped_away_from_the_owner_still_says_so(self, monkeypatch):
        """The retirement belongs to the owner's app; a run without it writes the deletion and
        nothing else, so it has to name what is left broken."""
        (note,) = _command(monkeypatch, dropped={CHILD})._scoped_cascade_retirement_notes(
            {'crossapp_owner'}
        )

        assert OWNER in note
        assert 'every UPDATE' in note

    def test_a_scoped_run_without_evidence_adds_no_scoped_note(self, monkeypatch):
        """The unscoped note already names it; the scoped one is for a retirement left unwritten."""
        command = _command(monkeypatch, dropped=set())

        assert command._scoped_cascade_retirement_notes({'crossapp_owner'}) == []

    def test_without_evidence_of_a_deletion_it_is_still_only_named(self, monkeypatch):
        """An app dropped from ``LOCAL_APPS`` looks the same from the registry: no evidence, no
        retirement."""
        command = _command(monkeypatch, dropped=set())

        assert _retirements(command) == []
        assert len(command._unmapped_cascade_notes()) == 1

    @pytest.mark.django_db
    def test_running_it_repairs_the_owner(self, monkeypatch):
        retired = _retirements(_command(monkeypatch, dropped={CHILD}))

        with transaction.atomic():
            execute(f'DROP TABLE {CHILD} CASCADE')
            for operation in retired:
                execute(_forward_sql(operation))
            assert not _revive_is_live()
            _update_owner()
            transaction.set_rollback(True)


class _Loader:
    """What ``dropped_tables`` reads off a ``MigrationLoader``: the graph, and nothing on disk."""

    def __init__(self, *migrations_in_order):
        self.graph = MigrationGraph()
        self.unmigrated_apps: set[str] = set()
        previous = None
        for number, operations in enumerate(migrations_in_order, start=1):
            key = ('shop', f'{number:04d}')
            migration = migrations.Migration(key[1], 'shop')
            migration.operations = operations
            self.graph.add_node(key, migration)
            if previous:
                self.graph.add_dependency(migration, key, previous)
            previous = key


def _create(name='item', **options):
    return CreateModel(name, [('id', models.AutoField(primary_key=True))], options=options)


class TestDroppedTables:
    def test_a_deleted_model_is_dropped(self):
        loader = _Loader([_create()], [DeleteModel('item')])

        assert graph.dropped_tables(loader) == {'shop_item'}

    def test_a_table_recreated_later_is_not(self):
        loader = _Loader([_create()], [DeleteModel('item')], [_create()])

        assert graph.dropped_tables(loader) == set()

    def test_a_state_only_move_is_not(self):
        """Moving a model between apps deletes it from one app's *state* alone; the table lives."""
        moved = SeparateDatabaseAndState(state_operations=[DeleteModel('item')])
        loader = _Loader([_create()], [moved])

        assert graph.dropped_tables(loader) == set()

    def test_a_create_and_delete_in_one_migration_is(self):
        """A squash's shape once its replaced files are gone: the state before the *operation*,
        not before the migration, holds the model."""
        loader = _Loader([_create(), DeleteModel('item')])

        assert graph.dropped_tables(loader) == {'shop_item'}

    def test_a_rename_before_the_delete_names_the_table_it_had(self):
        loader = _Loader([_create()], [RenameModel('item', 'kit'), DeleteModel('kit')])

        assert graph.dropped_tables(loader) == {'shop_kit'}

    def test_it_reads_the_graph_not_the_files(self):
        """A pending squash leaves its replaced migrations on disk but out of the graph; reading
        them asked the graph for nodes it does not have and crashed the generator."""
        loader = _Loader([_create()], [DeleteModel('item')])
        loader.disk_migrations = {('shop', '0003_replaced'): migrations.Migration('x', 'shop')}

        assert graph.dropped_tables(loader) == {'shop_item'}

    @pytest.mark.parametrize(
        'options', [{'proxy': True}, {'managed': False}], ids=['proxy', 'unmanaged']
    )
    def test_a_model_owning_no_table_drops_none(self, options):
        """Django drops no table for either, so a live one would lose its trigger."""
        loader = _Loader([_create(**options)], [DeleteModel('item')])

        assert graph.dropped_tables(loader) == set()


def test_an_owner_whose_table_was_dropped_is_not_named(monkeypatch):
    """Its rules and triggers went with its table: there is nothing left to drop by hand."""
    command = Command()
    clear_cascade_coverage(command)
    command.existing.soft_delete_related[('shop_child', 'shop_gone', None)] = 'abc'
    monkeypatch.setattr(command, '_dropped_tables', lambda: {'shop_gone'})

    assert command._unmapped_cascade_notes() == []
