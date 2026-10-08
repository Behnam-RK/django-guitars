"""``ordering.order_after_enforcement`` (#61): a fresh rename or drop of a table an enforcement
migration of another app names is written already ordered after it."""

from __future__ import annotations

from pathlib import Path

import pytest
from django.db import migrations, models
from django.db.migrations.graph import MigrationGraph
from django.db.migrations.operations import (
    AlterModelTable,
    CreateModel,
    DeleteModel,
    RenameModel,
    SeparateDatabaseAndState,
)
from django.db.migrations.state import ProjectState

from guitars.management import _generator
from guitars.management.enforcement.ordering import order_after_enforcement


ENFORCEMENT = ('testapp', '0004_auto_enforcement')


class _Loader:
    """The two things the ordering reads off a ``MigrationLoader``: a graph and a state."""

    def __init__(self, *chains):
        self.graph = MigrationGraph()
        self.unmigrated_apps: set[str] = set()
        for app, operations_in_order in chains:
            previous = None
            for number, operations in enumerate(operations_in_order, start=1):
                key = (app, f'{number:04d}')
                self._add(key, operations, previous)
                previous = key

    def _add(self, key, operations, previous=None):
        migration = migrations.Migration(key[1], key[0])
        migration.operations = operations
        self.graph.add_node(key, migration)
        if previous:
            self.graph.add_dependency(migration, key, previous)
        return migration

    def project_state(self):
        state = ProjectState(real_apps=set())
        for leaf in self.graph.leaf_nodes():
            for app_label, name in self.graph.forwards_plan(leaf):
                for operation in self.graph.nodes[app_label, name].operations:
                    operation.state_forwards(app_label, state)
        return state


def _create(name='root', **options):
    return CreateModel(name, [('id', models.AutoField(primary_key=True))], options=options)


def _loader():
    loader = _Loader(('anc', [[_create()]]))
    loader._add(ENFORCEMENT, [])
    return loader


def _fresh(*operations, dependencies=(('anc', '0001'),)):
    migration = migrations.Migration('0002_fresh', 'anc')
    migration.operations = list(operations)
    migration.dependencies = list(dependencies)
    return migration


@pytest.fixture
def enforcement_file(monkeypatch):
    """The ``testapp`` enforcement migrations on disk, settable per test: one file by default,
    more by naming each ``stem``."""
    files: dict[str, str] = {}

    def _iter(app):
        if app.label == 'testapp':
            for stem, text in files.items():
                yield Path(f'{stem}.py'), text

    monkeypatch.setattr(_generator, 'iter_migration_files', _iter)

    def _set(table, digest=True, stem='0004_auto_enforcement'):
        files[stem] = f'{"# [DIGEST:abc]" if digest else ""}\nUPDATE "{table}" SET x = 1\n'

    return _set


def test_a_rename_is_ordered_after_the_file_naming_the_old_table(enforcement_file):
    enforcement_file('anc_root')
    migration = _fresh(RenameModel('root', 'trunk'))

    order_after_enforcement({'anc': [migration]}, _loader())

    assert migration.dependencies == [('anc', '0001'), ENFORCEMENT]


def test_a_retable_is_too(enforcement_file):
    enforcement_file('anc_root')
    migration = _fresh(AlterModelTable('root', 'anc_trunk'))

    order_after_enforcement({'anc': [migration]}, _loader())

    assert ENFORCEMENT in migration.dependencies


def test_a_delete_is_too(enforcement_file):
    enforcement_file('anc_root')
    migration = _fresh(DeleteModel('root'))

    order_after_enforcement({'anc': [migration]}, _loader())

    assert ENFORCEMENT in migration.dependencies


def test_a_move_between_apps_is_too(enforcement_file):
    """The database half renames the table while the state half deletes the model."""
    enforcement_file('anc_root')
    migration = _fresh(
        SeparateDatabaseAndState(
            database_operations=[AlterModelTable('root', 'new_root')],
            state_operations=[DeleteModel('root')],
        )
    )

    order_after_enforcement({'anc': [migration]}, _loader())

    assert ENFORCEMENT in migration.dependencies


def test_a_file_naming_the_new_table_gets_no_edge(enforcement_file):
    enforcement_file('anc_trunk')
    migration = _fresh(RenameModel('root', 'trunk'))

    order_after_enforcement({'anc': [migration]}, _loader())

    assert migration.dependencies == [('anc', '0001')]


def test_a_file_without_a_digest_is_not_ours(enforcement_file):
    enforcement_file('anc_root', digest=False)
    migration = _fresh(RenameModel('root', 'trunk'))

    order_after_enforcement({'anc': [migration]}, _loader())

    assert migration.dependencies == [('anc', '0001')]


def test_a_file_the_graph_does_not_hold_gets_no_edge(enforcement_file):
    """A squash's replaced file stays on disk; depending on its name would break the graph."""
    enforcement_file('anc_root')
    migration = _fresh(RenameModel('root', 'trunk'))

    order_after_enforcement({'anc': [migration]}, _Loader(('anc', [[_create()]])))

    assert migration.dependencies == [('anc', '0001')]


def test_the_apps_own_files_are_ordered_by_its_chain(enforcement_file):
    enforcement_file('testapp_root')
    migration = migrations.Migration('0002_fresh', 'testapp')
    migration.operations = [RenameModel('root', 'trunk')]
    migration.dependencies = [('testapp', '0001')]
    loader = _Loader(('testapp', [[_create()]]))
    loader._add(ENFORCEMENT, [])

    order_after_enforcement({'testapp': [migration]}, loader)

    assert migration.dependencies == [('testapp', '0001')]


def test_a_table_a_live_model_holds_is_ordered_all_the_same(enforcement_file):
    """The older file names the table as it was; whoever holds the name now is the later one's."""
    enforcement_file('testapp_catalog')
    loader = _Loader(('anc', [[_create(db_table='testapp_catalog')]]))
    loader._add(ENFORCEMENT, [])
    migration = _fresh(DeleteModel('root'))

    order_after_enforcement({'anc': [migration]}, loader)

    assert ENFORCEMENT in migration.dependencies


def test_a_rewritten_leaf_is_left_alone(enforcement_file):
    """``makemigrations --update`` rewrites the leaf under a *new name* and its operations carry
    the old ones too: the state already holds what they did, so replaying them fails -- and a
    file on disk is not this function's to edit."""
    enforcement_file('anc_root')
    loader = _Loader(('anc', [[_create()], [RenameModel('root', 'trunk')]]))
    loader._add(ENFORCEMENT, [])
    rewritten = migrations.Migration('0002_renamed_updated', 'anc')
    rewritten.operations = [*loader.graph.nodes['anc', '0002'].operations]
    rewritten.dependencies = [('anc', '0001')]

    order_after_enforcement({'anc': [rewritten]}, loader, rewritten=frozenset({'anc'}))

    assert rewritten.dependencies == [('anc', '0001')]


def test_other_apps_in_the_same_run_are_still_ordered(enforcement_file):
    enforcement_file('anc_root')
    fresh = _fresh(RenameModel('root', 'trunk'))

    order_after_enforcement({'anc': [fresh], 'other': []}, _loader(), rewritten=frozenset({'other'}))

    assert ENFORCEMENT in fresh.dependencies


def test_an_edge_the_dependencies_already_reach_is_not_repeated(enforcement_file):
    enforcement_file('anc_root')
    loader = _loader()
    loader.graph.add_dependency(None, ('anc', '0001'), ENFORCEMENT)
    migration = _fresh(RenameModel('root', 'trunk'))

    order_after_enforcement({'anc': [migration]}, loader)

    assert migration.dependencies == [('anc', '0001')]


def test_an_edge_already_listed_is_not_repeated(enforcement_file):
    enforcement_file('anc_root')
    migration = _fresh(RenameModel('root', 'trunk'), dependencies=[('anc', '0001'), ENFORCEMENT])

    order_after_enforcement({'anc': [migration]}, _loader())

    assert migration.dependencies == [('anc', '0001'), ENFORCEMENT]


def test_a_migration_vacating_nothing_is_left_alone(enforcement_file):
    enforcement_file('anc_root')
    migration = _fresh(_create('other'))

    order_after_enforcement({'anc': [migration]}, _loader())

    assert migration.dependencies == [('anc', '0001')]


def test_of_two_files_naming_it_only_the_later_is_written(enforcement_file):
    """The earlier is implied by the later, as it is by any edge the graph already carries."""
    enforcement_file('anc_root')
    enforcement_file('anc_root', stem='0005_auto_enforcement')
    loader = _loader()
    later = ('testapp', '0005_auto_enforcement')
    loader._add(later, [])
    loader.graph.add_dependency(None, later, ENFORCEMENT)
    migration = _fresh(RenameModel('root', 'trunk'))

    order_after_enforcement({'anc': [migration]}, loader)

    assert migration.dependencies == [('anc', '0001'), later]


def test_a_later_operation_reads_the_state_the_earlier_one_left(enforcement_file):
    """Renamed then deleted in one migration: the delete names the table the rename gave."""
    enforcement_file('anc_trunk')
    migration = _fresh(RenameModel('root', 'trunk'), DeleteModel('trunk'))

    order_after_enforcement({'anc': [migration]}, _loader())

    assert ENFORCEMENT in migration.dependencies
