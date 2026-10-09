"""A cascading child whose model was deleted (#63). ``DROP TABLE ... CASCADE`` takes the cascade
rule, which names the child in its action, but not the revive trigger on the owner: a plpgsql
body records no dependency. The trigger then fails every ``UPDATE`` on the owner."""

import types
from io import StringIO
from pathlib import Path

import pytest
from django.apps import apps
from django.core.management import CommandError, call_command
from django.db import ProgrammingError, connection, migrations, models, transaction
from django.db.backends.utils import truncate_name
from django.db.migrations.graph import MigrationGraph
from django.db.migrations.operations import (
    CreateModel,
    DeleteModel,
    RenameModel,
    SeparateDatabaseAndState,
)

from guitars.management import _generator
from guitars.management.enforcement import graph
from guitars.management.enforcement import operations as operations_module
from guitars.management.enforcement.command import Command
from guitars.management.enforcement.headers import (
    HEADER_MTI_UPDATED_AT,
    HEADER_SOFT_DELETE,
    HEADER_SOFT_DELETE_OWNED_SWEEP,
    HEADER_SOFT_DELETE_RELATED,
    HEADER_SOFT_DELETE_REVIVE,
    HEADER_SOFT_DELETE_SELF_CASCADE,
    HEADER_TENANT_AUTOFILL,
    HEADER_TENANT_POLICY,
    HEADER_UPDATED_AT,
)
from guitars.operations import RetireEnforcement
from tests.conftest import clear_cascade_coverage, execute, scalar


CHILD, OWNER = 'testapp_setlistentry', 'testapp_setlist'
KEY = (CHILD, OWNER, None)
# The owner's one revive trigger since 2.16.0 (#70), carrying the child's arm.
REVIVE = 'soft_delete_cascade_on_15_testapp_setlist'
# The per-key trigger it superseded, which a retirement still drops ``IF EXISTS``.
PER_KEY_REVIVE = 'soft_delete_revive_15_testapp_setlist_20_testapp_setlistentry'


def _revive_is_live() -> bool:
    return bool(scalar('SELECT count(*) FROM pg_trigger WHERE tgname = %s', [REVIVE]))


def _update_owner():
    execute(f'UPDATE {OWNER} SET title = title')


@pytest.mark.django_db
class TestTheLeak:
    def test_dropping_the_child_leaves_a_trigger_that_breaks_a_revive_of_the_owner(self):
        """The arm naming the dropped child runs only once the trigger's ``EXISTS`` finds a
        row revived, so the failure is the revive's -- every plain UPDATE through 2.15."""
        from tests.testapp.models import Setlist  # noqa: PLC0415

        Setlist.objects.create(title='s')
        execute(f'UPDATE {OWNER} SET _deleted_at = NOW()')
        with pytest.raises(ProgrammingError, match=f'relation "{CHILD}" does not exist'):
            with transaction.atomic():
                execute(f'DROP TABLE {CHILD} CASCADE')
                assert _revive_is_live()
                execute(f'UPDATE {OWNER} SET _deleted_at = NULL')

    def test_a_plain_update_of_the_owner_survives_the_dropped_child(self):
        """The early exit's side effect (#70): nothing revived, no arm runs, nothing fails."""
        with transaction.atomic():
            execute(f'DROP TABLE {CHILD} CASCADE')
            _update_owner()
            transaction.set_rollback(True)

    def test_retire_enforcement_on_the_child_does_not_reach_it(self):
        """It drops what *depends* on the child and the child's own triggers. The owner's revive
        trigger is neither, so it is no workaround for this."""
        with transaction.atomic():
            with connection.schema_editor() as editor:
                RetireEnforcement(CHILD).database_forwards('testapp', editor, None, None)
            assert _revive_is_live()
            transaction.set_rollback(True)


def _command(monkeypatch, *, dropped: set[str], keep_self_arm: bool = False):
    """The child's model deleted: its table maps to nothing and no key calls for it. The owner's
    own self key is dropped too unless *keep_self_arm*: the scenario is the child being its only
    arm, which the real registry no longer shows for ``Setlist`` (ADR 0042)."""
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

    def arms_without_child():
        arms = command._revive_arm_sources if command._cascade_key_maps() else {}
        return {
            owner: kept
            for owner, keyed in arms.items()
            # The owner's self key is no arm of this scenario either: the child was its only one,
            # and a self key is an arm of the owner's trigger since 2.20.0 (ADR 0042).
            if (
                kept := {
                    key: arm
                    for key, arm in keyed.items()
                    if key[0] != CHILD and (keep_self_arm or not key[0] == key[1] == OWNER)
                }
            )
        }

    monkeypatch.setattr(command, '_table_app_labels', without_child)
    monkeypatch.setattr(command, '_cascade_key_maps', maps_without_child)
    monkeypatch.setattr(command, '_revive_arms_by_owner', arms_without_child)
    monkeypatch.setattr(
        command, '_dropped_tables', lambda: {table: ('otherapp', '0009_gone') for table in dropped}
    )
    command.existing.soft_delete_related[KEY] = 'abc'
    command.existing.soft_delete_revive[KEY] = 'def'
    command.existing.soft_delete_cascade_owner[(OWNER,)] = 'ghi'
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
        assert PER_KEY_REVIVE in _forward_sql(revive)

    def test_the_retirement_is_ordered_after_the_deletion(self, monkeypatch):
        """Run before the ``DeleteModel``, it dropped both objects while the child was live, and
        an owner archived in between left its children live."""
        command = _command(monkeypatch, dropped={CHILD})

        _retirements(command)

        assert ('otherapp', '0009_gone') in command._retirement_edges['testapp']

    def test_a_deletion_in_the_owners_app_needs_no_edge(self, monkeypatch):
        """The app's own history already orders the two."""
        command = _command(monkeypatch, dropped={CHILD})
        monkeypatch.setattr(command, '_dropped_tables', lambda: {CHILD: ('testapp', '0009_gone')})

        _retirements(command)

        assert ('testapp', '0009_gone') not in command._retirement_edges.get('testapp', [])

    def test_the_reverse_points_at_adopt(self, monkeypatch):
        """``--fake`` then a plain regeneration left neither object and ``--check`` green: the
        scan still believed the retired key covered. Only ``--adopt`` rebuilds them."""
        for operation in _retirements(_command(monkeypatch, dropped={CHILD})):
            assert 'makeguitarmigrations --adopt' in operation

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

    def test_a_project_with_no_revive_recorded_gets_no_scoped_note(self, monkeypatch):
        """Before 2.11.0 there was no revive trigger, so nothing is broken to warn about."""
        command = _command(monkeypatch, dropped={CHILD})
        command.existing.soft_delete_revive.clear()
        command.existing.soft_delete_cascade_owner.clear()

        assert command._scoped_cascade_retirement_notes({'crossapp_owner'}) == []

    def test_the_owners_one_trigger_is_named_once_its_per_key_trigger_is_gone(self, monkeypatch):
        """Since 2.16.0 the per-key trigger is gone and the owner's carries the arm (#70). It
        fails only a revive, the early exit sparing every other UPDATE, until re-emitted."""
        command = _command(monkeypatch, dropped={CHILD})
        command.existing.soft_delete_revive.clear()

        notes = [
            note
            for note in (
                *command._scoped_cascade_retirement_notes({'crossapp_owner'}),
                *command._scoped_trigger_retirement_notes({'crossapp_owner'}),
            )
            if REVIVE in note
        ]

        # Once, by the note comparing digests: the arm went with its last key, so retired.
        (note,) = notes
        assert 'no longer called for' in note

    def test_a_per_key_trigger_whose_owner_is_in_scope_is_not_named(self, monkeypatch):
        """The run in scope writes its retirement itself."""
        command = _command(monkeypatch, dropped={CHILD})

        assert command._scoped_cascade_retirement_notes({'testapp'}) == []

    def test_the_owners_trigger_is_named_with_its_own_host(self, monkeypatch, settings):
        """Kept by the app that created it (ADR 0033), which need not host the table: the note
        sends the reader to that app, and an in-scope one is not told to wait for itself."""
        settings.LOCAL_APPS = [*settings.LOCAL_APPS, 'tests.crossapp_owner']
        command = _command(monkeypatch, dropped={CHILD})
        command.existing.soft_delete_revive.clear()
        command.existing.soft_delete_cascade_owner_dependencies[(OWNER,)] = [
            ('crossapp_owner', '0003_auto_enforcement')
        ]

        def revive_notes(requested):
            return [
                note
                for note in command._scoped_trigger_retirement_notes(requested)
                if REVIVE in note
            ]

        assert revive_notes({'crossapp_owner'}) == []
        (note,) = revive_notes({'other'})
        assert "'crossapp_owner'" in note

    def test_two_keys_to_one_owner_name_two_triggers(self, monkeypatch):
        command = _command(monkeypatch, dropped={CHILD})
        for family in (command.existing.soft_delete_related, command.existing.soft_delete_revive):
            family[(CHILD, OWNER, 'other_id')] = 'ghi'

        notes = command._scoped_cascade_retirement_notes({'crossapp_owner'})

        assert len(notes) == len(set(notes)) == 2

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

    def test_an_owner_keeping_its_self_arm_is_re_emitted_not_retired(self, monkeypatch):
        """The real registry's shape: the dropped child was one arm of two, so the owner's trigger
        stays and is re-emitted without it, by its digest moving, rather than retired."""
        command = _command(monkeypatch, dropped={CHILD}, keep_self_arm=True)

        retired = [
            op
            for op in command._retired_trigger_operations(apps.get_app_config('testapp'))
            if f'Cascade Trigger retired on "{OWNER}"' in op
        ]

        assert retired == []

    @pytest.mark.django_db
    def test_running_it_repairs_the_owner(self, monkeypatch):
        """The child was the owner's only arm, so its one trigger goes with it (2.16.0)."""
        from tests.testapp.models import Setlist  # noqa: PLC0415

        command = _command(monkeypatch, dropped={CHILD})
        app = apps.get_app_config('testapp')
        retired = [
            *_retirements(command),
            *[op for op in command._retired_trigger_operations(app) if f'"{OWNER}"' in op],
            *[op for op in command._revive_operations(app) if f'"{OWNER}"' in op],
        ]
        Setlist.objects.create(title='s')

        with transaction.atomic():
            execute(f'UPDATE {OWNER} SET _deleted_at = NOW()')
            execute(f'DROP TABLE {CHILD} CASCADE')
            for operation in retired:
                execute(_forward_sql(operation))
            assert not _revive_is_live()
            execute(f'UPDATE {OWNER} SET _deleted_at = NULL')
            transaction.set_rollback(True)


class _Loader:
    """What ``dropped_tables`` reads off a ``MigrationLoader``: the graph, and nothing on disk."""

    def __init__(self, *migrations_in_order, app='shop'):
        self.graph = MigrationGraph()
        self.unmigrated_apps: set[str] = set()
        previous = None
        for number, operations in enumerate(migrations_in_order, start=1):
            key = (app, f'{number:04d}')
            migration = migrations.Migration(key[1], app)
            migration.operations = operations
            self.graph.add_node(key, migration)
            if previous:
                self.graph.add_dependency(migration, key, previous)
            previous = key


def _create(name='item', **options):
    return CreateModel(name, [('id', models.AutoField(primary_key=True))], options=options)


class TestDroppedTables:
    def test_a_deleted_model_is_dropped_by_the_migration_that_deleted_it(self):
        loader = _Loader([_create()], [DeleteModel('item')])

        assert graph.dropped_tables(loader) == {'shop_item': ('shop', '0002')}

    def test_a_default_name_is_truncated_as_django_truncates_it(self):
        """Past 63 bytes ``Options`` shortens the name with a hash; spelling it whole never
        matched the recorded key, and the leaked trigger stayed."""
        app = 'a' * 60
        loader = _Loader([_create()], [DeleteModel('item')], app=app)

        (table,) = graph.dropped_tables(loader)

        assert table == truncate_name(f'{app}_item', connection.ops.max_name_length())
        assert len(table) <= 63

    def test_a_table_recreated_later_is_not(self):
        loader = _Loader([_create()], [DeleteModel('item')], [_create()])

        assert not graph.dropped_tables(loader)

    def test_a_state_only_move_is_not(self):
        """Moving a model between apps deletes it from one app's *state* alone; the table lives."""
        moved = SeparateDatabaseAndState(state_operations=[DeleteModel('item')])
        loader = _Loader([_create()], [moved])

        assert not graph.dropped_tables(loader)

    def test_a_create_and_delete_in_one_migration_is(self):
        """A squash's shape once its replaced files are gone: the state before the *operation*,
        not before the migration, holds the model."""
        loader = _Loader([_create(), DeleteModel('item')])

        assert set(graph.dropped_tables(loader)) == {'shop_item'}

    def test_a_rename_before_the_delete_names_the_table_it_had(self):
        loader = _Loader([_create()], [RenameModel('item', 'kit'), DeleteModel('kit')])

        assert set(graph.dropped_tables(loader)) == {'shop_kit'}

    def test_it_reads_the_graph_not_the_files(self):
        """A pending squash leaves its replaced migrations on disk but out of the graph; reading
        them asked the graph for nodes it does not have and crashed the generator."""
        loader = _Loader([_create()], [DeleteModel('item')])
        loader.disk_migrations = {('shop', '0003_replaced'): migrations.Migration('x', 'shop')}

        assert set(graph.dropped_tables(loader)) == {'shop_item'}

    @pytest.mark.parametrize(
        'options', [{'proxy': True}, {'managed': False}], ids=['proxy', 'unmanaged']
    )
    def test_a_model_owning_no_table_drops_none(self, options):
        """Django drops no table for either, so a live one would lose its trigger."""
        loader = _Loader([_create(**options)], [DeleteModel('item')])

        assert not graph.dropped_tables(loader)


def test_an_owner_whose_table_was_dropped_is_not_named(monkeypatch):
    """Its rules and triggers went with its table: there is nothing left to drop by hand."""
    command = Command()
    clear_cascade_coverage(command)
    command.existing.soft_delete_related[('shop_child', 'shop_gone', None)] = 'abc'
    monkeypatch.setattr(command, '_dropped_tables', lambda: {'shop_gone'})

    assert command._unmapped_cascade_notes() == []
