"""``graph.replay_plan`` (ADR 0043): every migration in the order ``migrate`` runs it, with the
table events its operations make -- the order the scan replays headers in, so a later file wins by
being later rather than by sorting after."""

from __future__ import annotations

import pytest
from django.db import migrations, models
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.graph import MigrationGraph
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import (
    AlterModelTable,
    CreateModel,
    DeleteModel,
    RenameModel,
    SeparateDatabaseAndState,
)

from guitars.management.enforcement.graph import TableEvent, replay_plan
from guitars.operations import RetireEnforcement


class _Loader:
    """What ``replay_plan`` reads off a ``MigrationLoader``: the graph and the migrations on disk.
    A chain is ``(app, [operations per migration])``; ``squash`` replaces a run of one's files."""

    def __init__(self, *chains, squash=None):
        self.graph = MigrationGraph()
        self.unmigrated_apps: set[str] = set()
        self.disk_migrations: dict = {}
        self.replaced: list = []
        for app, per_migration in chains:
            previous = None
            for number, operations in enumerate(per_migration, start=1):
                key = (app, f'{number:04d}')
                migration = migrations.Migration(key[1], app)
                migration.operations = operations
                self.disk_migrations[key] = migration
                if previous:
                    migration.dependencies = [previous]
                previous = key
        for key, migration in self.disk_migrations.items():
            if squash and key[0] == squash[0] and key[1] in squash[1]:
                self.replaced.append(key)
                continue
            self.graph.add_node(key, migration)
        if squash:
            app, names, operations = squash
            key = (app, f'{names[0]}_squashed_{names[-1]}')
            migration = migrations.Migration(key[1], app)
            migration.operations = operations
            migration.replaces = [(app, name) for name in names]
            self.disk_migrations[key] = migration
            self.graph.add_node(key, migration)
        for key, migration in self.graph.nodes.items():
            for parent in migration.dependencies:
                if parent in self.graph.nodes:
                    self.graph.add_dependency(migration, key, parent)
                elif squash and parent in self.replaced:
                    self.graph.add_dependency(
                        migration, key, (squash[0], f'{squash[1][0]}_squashed_{squash[1][-1]}')
                    )


def _create(name='root', **options):
    return CreateModel(name, [('id', models.AutoField(primary_key=True))], options=options)


def _events(loader):
    return {(unit.app_label, unit.name): unit.events for unit in replay_plan(loader)}


def test_create_rename_and_drop_are_events_in_operation_order():
    loader = _Loader(
        ('anc', [[_create()], [RenameModel('root', 'trunk')], [DeleteModel('trunk')]])
    )

    assert _events(loader) == {
        ('anc', '0001'): (TableEvent('create', 'anc_root'),),
        ('anc', '0002'): (TableEvent('rename', 'anc_root', 'anc_trunk'),),
        ('anc', '0003'): (TableEvent('drop', 'anc_trunk'),),
    }


def test_a_drop_and_a_recreate_are_two_events_in_their_own_migrations():
    loader = _Loader(('anc', [[_create()], [DeleteModel('root')], [_create()]]))

    kinds = [event.kind for unit in replay_plan(loader) for event in unit.events]

    assert kinds == ['create', 'drop', 'create']


def test_several_operations_of_one_migration_keep_their_order():
    loader = _Loader(('anc', [[_create(), _create('other'), RenameModel('root', 'trunk')]]))

    assert _events(loader)[('anc', '0001')] == (
        TableEvent('create', 'anc_root'),
        TableEvent('create', 'anc_other'),
        TableEvent('rename', 'anc_root', 'anc_trunk'),
    )


def test_a_retable_is_a_rename_and_one_to_the_name_it_has_is_nothing():
    loader = _Loader(
        (
            'anc',
            [
                [_create()],
                [AlterModelTable('root', 'anc_trunk')],
                [AlterModelTable('root', 'anc_trunk')],
            ],
        )
    )

    events = _events(loader)

    assert events[('anc', '0002')] == (TableEvent('rename', 'anc_root', 'anc_trunk'),)
    assert events[('anc', '0003')] == ()


def test_an_explicit_table_survives_a_model_rename():
    loader = _Loader(('anc', [[_create(db_table='custom')], [RenameModel('root', 'trunk')]]))

    assert _events(loader)[('anc', '0002')] == ()


def test_a_proxy_and_an_unmanaged_model_own_no_table():
    loader = _Loader(
        ('anc', [[_create(), _create('shadow', proxy=True), _create('legacy', managed=False)]])
    )

    assert _events(loader)[('anc', '0001')] == (TableEvent('create', 'anc_root'),)


class TestSeparateDatabaseAndState:
    """The database half is what happens to a table; the state half only what Django believes."""

    def test_a_database_half_retable_is_the_rename_of_a_model_the_state_half_deletes(self):
        move = SeparateDatabaseAndState(
            database_operations=[AlterModelTable('root', 'new_root')],
            state_operations=[DeleteModel('root')],
        )
        adopt = SeparateDatabaseAndState(state_operations=[_create('root', db_table='new_root')])
        loader = _Loader(('anc', [[_create()], [move]]), ('new', [[adopt]]))

        events = _events(loader)

        assert events[('anc', '0002')] == (TableEvent('rename', 'anc_root', 'new_root'),)
        assert events[('new', '0001')] == ()

    def test_a_state_only_delete_drops_nothing(self):
        forget = SeparateDatabaseAndState(state_operations=[DeleteModel('root')])
        loader = _Loader(('anc', [[_create()], [forget]]))

        assert _events(loader)[('anc', '0002')] == ()

    def test_a_database_half_delete_is_a_drop(self):
        drop = SeparateDatabaseAndState(database_operations=[DeleteModel('root')])
        loader = _Loader(('anc', [[_create()], [drop]]))

        assert _events(loader)[('anc', '0002')] == (TableEvent('drop', 'anc_root'),)

    def test_a_retirement_is_read_from_either_half(self):
        wrapped = SeparateDatabaseAndState(
            database_operations=[RetireEnforcement('anc_root', 'owner_id')],
            state_operations=[RetireEnforcement('anc_other')],
        )
        loader = _Loader(('anc', [[_create()], [wrapped]]))

        assert _events(loader)[('anc', '0002')] == (
            TableEvent('retire', 'anc_root', column='owner_id'),
            TableEvent('retire', 'anc_other'),
        )


def test_a_database_half_of_two_operations_reads_the_second_on_the_state_the_first_left():
    both = SeparateDatabaseAndState(
        database_operations=[RenameModel('root', 'trunk'), AlterModelTable('trunk', 'anc_stump')]
    )
    loader = _Loader(('anc', [[_create()], [both]]))

    assert _events(loader)[('anc', '0002')] == (
        TableEvent('rename', 'anc_root', 'anc_trunk'),
        TableEvent('rename', 'anc_trunk', 'anc_stump'),
    )


def test_a_cross_app_dependency_orders_the_other_apps_migration_after_it():
    loader = _Loader(
        ('anc', [[_create()], [RenameModel('root', 'trunk')]]), ('shop', [[_create('kid')]])
    )
    loader.disk_migrations['shop', '0001'].dependencies = [('anc', '0002')]
    loader.graph.add_dependency(
        loader.disk_migrations['shop', '0001'], ('shop', '0001'), ('anc', '0002')
    )

    order = [(unit.app_label, unit.name) for unit in replay_plan(loader)]

    assert order.index(('anc', '0002')) < order.index(('shop', '0001'))


class TestASquash:
    def test_replaced_files_on_disk_are_walked_as_themselves_in_order(self):
        loader = _Loader(
            ('anc', [[_create()], [RenameModel('root', 'trunk')], [DeleteModel('trunk')]]),
            squash=('anc', ['0001', '0002'], [_create('trunk')]),
        )

        units = replay_plan(loader)

        assert [(u.name, u.graph_node[1]) for u in units] == [
            ('0001', '0001_squashed_0002'),
            ('0002', '0001_squashed_0002'),
            ('0001_squashed_0002', '0001_squashed_0002'),
            ('0003', '0003'),
        ]
        assert units[0].events == (TableEvent('create', 'anc_root'),)
        assert units[1].events == (TableEvent('rename', 'anc_root', 'anc_trunk'),)
        assert units[2].events == ()
        assert units[3].events == (TableEvent('drop', 'anc_trunk'),)

    def test_a_squash_whose_replaced_files_are_gone_is_used_as_itself(self):
        loader = _Loader(
            ('anc', [[_create()], [RenameModel('root', 'trunk')], [DeleteModel('trunk')]]),
            squash=('anc', ['0001', '0002'], [_create('trunk')]),
        )
        for key in loader.replaced:
            del loader.disk_migrations[key]

        units = replay_plan(loader)

        assert [u.name for u in units] == ['0001_squashed_0002', '0003']
        assert units[0].events == (TableEvent('create', 'anc_trunk'),)

    def test_a_squash_naming_files_that_are_partly_gone_is_used_as_itself(self):
        loader = _Loader(
            ('anc', [[_create()], [RenameModel('root', 'trunk')]]),
            squash=('anc', ['0001', '0002'], [_create('trunk')]),
        )
        del loader.disk_migrations['anc', '0002']

        assert [u.name for u in replay_plan(loader)] == ['0001_squashed_0002']


def test_the_order_is_the_one_a_fresh_migrate_runs():
    """Off the real project, not a fake: the executor's own plan over every leaf."""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    executor = MigrationExecutor(None)
    executor.loader = loader
    expected = [
        (migration.app_label, migration.name)
        for migration, _backwards in executor.migration_plan(loader.graph.leaf_nodes())
    ]

    ordered = [
        (u.app_label, u.name)
        for u in replay_plan(loader)
        if (u.app_label, u.name) in set(expected)
    ]

    assert ordered == expected


@pytest.mark.parametrize('app', ['issue66_recreated', 'issue66_retaken'])
def test_a_real_history_reads_its_own_drops_and_renames(app):
    kinds = {
        event.kind
        for unit in replay_plan(MigrationLoader(None, ignore_no_migrations=True))
        if unit.app_label == app
        for event in unit.events
    }

    assert 'create' in kinds and kinds & {'drop', 'rename'}


def test_a_history_with_no_operations_drops_and_vacates_nothing():
    from guitars.management.enforcement.graph import dropped_tables, vacated_tables  # noqa: PLC0415

    loader = _Loader(('anc', [[]]))

    assert (dropped_tables(loader), vacated_tables(loader)) == ({}, {})


class TestDroppedTables:
    def test_a_database_half_delete_drops_the_table(self):
        drop = SeparateDatabaseAndState(
            database_operations=[DeleteModel('root')], state_operations=[DeleteModel('root')]
        )
        loader = _Loader(('anc', [[_create()], [drop]]))

        from guitars.management.enforcement.graph import dropped_tables  # noqa: PLC0415

        assert dropped_tables(loader) == {'anc_root': ('anc', '0002')}

    def test_a_state_only_delete_leaves_the_table(self):
        forget = SeparateDatabaseAndState(state_operations=[DeleteModel('root')])
        loader = _Loader(('anc', [[_create()], [forget]]))

        from guitars.management.enforcement.graph import dropped_tables  # noqa: PLC0415

        assert dropped_tables(loader) == {}
