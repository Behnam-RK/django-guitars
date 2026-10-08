"""``graph.vacated_tables`` (#61): the tables a migration renames away or drops, which an older
enforcement migration of another app may still name -- so a fresh ``migrate`` must not reach the
rename or the delete before it."""

from __future__ import annotations

from django.db import migrations, models
from django.db.migrations.graph import MigrationGraph
from django.db.migrations.operations import (
    AlterModelTable,
    CreateModel,
    DeleteModel,
    RenameModel,
    SeparateDatabaseAndState,
)

from guitars.management.enforcement import graph


class _Loader:
    """What ``vacated_tables`` reads off a ``MigrationLoader``: the graph, nothing on disk."""

    def __init__(self, *chains):
        self.graph = MigrationGraph()
        self.unmigrated_apps: set[str] = set()
        self.disk_migrations: dict = {}
        for app, operations_in_order in chains:
            previous = None
            for number, operations in enumerate(operations_in_order, start=1):
                key = (app, f'{number:04d}')
                migration = migrations.Migration(key[1], app)
                migration.operations = operations
                self.graph.add_node(key, migration)
                self.disk_migrations[key] = migration
                if previous:
                    self.graph.add_dependency(migration, key, previous)
                previous = key


def _create(name='root', **options):
    return CreateModel(name, [('id', models.AutoField(primary_key=True))], options=options)


def test_a_renamed_model_vacates_the_table_it_had():
    loader = _Loader(('anc', [[_create()], [RenameModel('root', 'trunk')]]))

    assert graph.vacated_tables(loader) == {('anc', '0002'): ['anc_root']}


def test_a_retabled_model_vacates_the_old_table():
    loader = _Loader(('anc', [[_create()], [AlterModelTable('root', 'anc_trunk')]]))

    assert graph.vacated_tables(loader) == {('anc', '0002'): ['anc_root']}


def test_a_table_put_back_to_its_default_vacates_the_explicit_one():
    loader = _Loader(
        ('anc', [[_create(db_table='custom')], [AlterModelTable('root', None)]]),
    )

    assert graph.vacated_tables(loader) == {('anc', '0002'): ['custom']}


def test_a_deleted_model_vacates_its_table():
    loader = _Loader(('anc', [[_create()], [DeleteModel('root')]]))

    assert graph.vacated_tables(loader) == {('anc', '0002'): ['anc_root']}


def test_an_explicit_table_survives_a_model_rename():
    """``RenameModel`` moves no table the model named itself: nothing is vacated."""
    loader = _Loader(('anc', [[_create(db_table='custom')], [RenameModel('root', 'trunk')]]))

    assert graph.vacated_tables(loader) == {}


def test_a_table_something_holds_again_is_not_vacated():
    """A new model took the old name: the enforcement migration naming it names *that* table."""
    loader = _Loader(('anc', [[_create()], [RenameModel('root', 'trunk')], [_create('root')]]))

    assert graph.vacated_tables(loader) == {}


def test_a_database_side_retable_that_a_state_side_move_hides_is_seen():
    """The cross-app move: the state operations delete the model, the database ones rename its
    table. Reading state alone saw no rename at all."""
    moved = SeparateDatabaseAndState(
        database_operations=[AlterModelTable('root', 'new_root')],
        state_operations=[DeleteModel('root')],
    )
    loader = _Loader(
        ('anc', [[_create()], [moved]]), ('new', [[_create('root', db_table='new_root')]])
    )

    assert graph.vacated_tables(loader) == {('anc', '0002'): ['anc_root']}


def test_a_state_only_move_vacates_nothing():
    moved = SeparateDatabaseAndState(state_operations=[DeleteModel('root')])
    loader = _Loader(('anc', [[_create()], [moved]]))

    assert graph.vacated_tables(loader) == {}


def test_a_model_owning_no_table_vacates_none():
    loader = _Loader(
        ('anc', [[_create(proxy=True)], [DeleteModel('root')]]),
        ('mgd', [[_create('thing', managed=False)], [RenameModel('thing', 'other')]]),
    )

    assert graph.vacated_tables(loader) == {}


def test_a_database_side_rename_of_a_model_the_state_never_had_is_ignored():
    stray = SeparateDatabaseAndState(database_operations=[AlterModelTable('ghost', 'x')])
    loader = _Loader(('anc', [[_create()], [stray]]))

    assert graph.vacated_tables(loader) == {}


def test_a_database_side_delete_vacates_the_table_the_state_still_has():
    """The database half is read whole: a ``DeleteModel`` there drops the table whatever the
    state half says."""
    dropped = SeparateDatabaseAndState(database_operations=[DeleteModel('root')])
    loader = _Loader(('anc', [[_create()], [dropped]]))

    assert graph.vacated_tables(loader) == {}

    forgotten = SeparateDatabaseAndState(
        database_operations=[DeleteModel('root')], state_operations=[DeleteModel('root')]
    )
    loader = _Loader(('anc', [[_create()], [forgotten]]))

    assert graph.vacated_tables(loader) == {('anc', '0002'): ['anc_root']}


class TestTheCheckNamesAnUnorderedPair:
    """``--check`` reads the enforcement files on disk against the history's renames and drops."""

    @staticmethod
    def _command(monkeypatch, loader, content):
        from pathlib import Path  # noqa: PLC0415

        from guitars.management import _generator  # noqa: PLC0415
        from guitars.management.enforcement.command import Command  # noqa: PLC0415

        command = Command()
        command._loader_cache = loader
        monkeypatch.setattr(
            _generator,
            'iter_migration_files',
            lambda app: (
                iter([(Path('0004_auto_enforcement.py'), content)])
                if app.label == 'testapp'
                else iter(())
            ),
        )
        return command

    @staticmethod
    def _enforcement(table='anc_root'):
        return f'# [DIGEST:abc]\nmigrations.RunSQL(sql="""UPDATE "{table}" SET x = 1""")\n'

    @staticmethod
    def _history():
        from django.db.migrations import Migration  # noqa: PLC0415

        loader = _Loader(
            ('anc', [[_create()], [RenameModel('root', 'trunk')]]),
            ('testapp', [[_create('other')]]),
        )
        enforcement = Migration('0004_auto_enforcement', 'testapp')
        loader.graph.add_node(('testapp', '0004_auto_enforcement'), enforcement)
        loader.graph.add_dependency(
            enforcement, ('testapp', '0004_auto_enforcement'), ('testapp', '0001')
        )
        return loader

    def test_a_file_naming_the_old_table_with_no_order_is_named_with_the_line_to_paste(
        self, monkeypatch
    ):
        command = self._command(monkeypatch, self._history(), self._enforcement())

        (note,) = command._missing_rename_edge_notes(set())

        assert "'testapp.0004_auto_enforcement' names 'anc_root'" in note
        assert "'anc.0002' renames away or drops" in note
        assert "('testapp', '0004_auto_enforcement')," in note

    def test_a_file_naming_the_new_table_is_not(self, monkeypatch):
        command = self._command(monkeypatch, self._history(), self._enforcement('anc_trunk'))

        assert command._missing_rename_edge_notes(set()) == []

    def test_a_file_the_rename_already_depends_on_is_not(self, monkeypatch):
        loader = self._history()
        mover = loader.graph.nodes['anc', '0002']
        loader.graph.add_dependency(mover, ('anc', '0002'), ('testapp', '0004_auto_enforcement'))
        command = self._command(monkeypatch, loader, self._enforcement())

        assert command._missing_rename_edge_notes(set()) == []

    def test_a_file_that_already_depends_on_the_rename_is_not_named(self, monkeypatch):
        """Django rejects that graph outright, so pasting the edge would be red with no way out."""
        loader = self._history()
        enforcement = loader.graph.nodes['testapp', '0004_auto_enforcement']
        loader.graph.add_dependency(
            enforcement, ('testapp', '0004_auto_enforcement'), ('anc', '0002')
        )
        command = self._command(monkeypatch, loader, self._enforcement())

        assert command._missing_rename_edge_notes(set()) == []

    def test_a_file_without_a_digest_is_not_one_of_ours(self, monkeypatch):
        command = self._command(monkeypatch, self._history(), 'UPDATE "anc_root" SET x = 1')

        assert command._missing_rename_edge_notes(set()) == []

    def test_a_file_the_graph_does_not_know_is_skipped(self, monkeypatch):
        """A squash's replaced file stays on disk after the graph drops it."""
        loader = _Loader(
            ('anc', [[_create()], [RenameModel('root', 'trunk')]]), ('testapp', [[_create('x')]])
        )
        command = self._command(monkeypatch, loader, self._enforcement())

        assert command._missing_rename_edge_notes(set()) == []

    def test_an_app_with_no_migrations_in_the_graph_is_not_read(self, monkeypatch):
        from guitars.management import _generator  # noqa: PLC0415

        loader = _Loader(('anc', [[_create()], [RenameModel('root', 'trunk')]]))
        command = self._command(monkeypatch, loader, self._enforcement())

        def _unread(app):
            raise AssertionError('read a migration file')

        monkeypatch.setattr(_generator, 'iter_migration_files', _unread)

        assert command._missing_rename_edge_notes(set()) == []

    def test_a_run_scoped_to_another_app_does_not_read_it(self, monkeypatch):
        command = self._command(monkeypatch, self._history(), self._enforcement())

        assert command._missing_rename_edge_notes({'anc'}) == []

    def test_a_history_with_no_rename_reads_no_files(self, monkeypatch):
        from guitars.management import _generator  # noqa: PLC0415

        loader = _Loader(('anc', [[_create()]]))
        command = self._command(monkeypatch, loader, self._enforcement())

        def _unread(app):
            raise AssertionError('read a migration file')

        monkeypatch.setattr(_generator, 'iter_migration_files', _unread)

        assert command._missing_rename_edge_notes(set()) == []

    def test_the_same_app_is_ordered_by_its_own_chain(self, monkeypatch):
        """Enforcement and rename in one app: its migrations form one dependency chain."""
        loader = _Loader(('testapp', [[_create()], [RenameModel('root', 'trunk')]]))
        command = self._command(monkeypatch, loader, self._enforcement('testapp_root'))
        loader.graph.add_node(
            ('testapp', '0004_auto_enforcement'),
            migrations.Migration('0004_auto_enforcement', 'testapp'),
        )

        assert command._missing_rename_edge_notes(set()) == []


def test_check_fails_on_a_note_it_names(monkeypatch):
    """The note is an error under ``--check``: the file it names is never rewritten, so nothing
    else would ever ask for the edge."""
    from io import StringIO  # noqa: PLC0415

    import pytest  # noqa: PLC0415
    from django.core.management import CommandError, call_command  # noqa: PLC0415

    from guitars.management.enforcement.command import Command  # noqa: PLC0415

    monkeypatch.setattr(Command, '_missing_rename_edge_notes', lambda self, requested: ['paste me'])

    with pytest.raises(CommandError, match='paste me'):
        call_command('makeguitarmigrations', check_only=True, stdout=StringIO(), stderr=StringIO())


class _StateLoader(_Loader):
    """The state half of a ``MigrationLoader`` as well, which ``renamed_tables`` reads."""

    def project_state(self, nodes=None, at_end=True):
        return self.graph.make_state(
            nodes=nodes, at_end=at_end, real_apps=self.unmigrated_apps
        )


def _moved_between_apps():
    """``anc`` hands ``Root`` to ``new``: the database half renames the table, the state half
    deletes the model there and creates it here."""
    move_out = SeparateDatabaseAndState(
        database_operations=[AlterModelTable('root', 'new_root')],
        state_operations=[DeleteModel('root')],
    )
    move_in = SeparateDatabaseAndState(state_operations=[_create('root', db_table='new_root')])
    return _StateLoader(('anc', [[_create()], [move_out]]), ('new', [[move_in]]))


class TestAModelMovedBetweenApps:
    """The table renames, but the model it renames is gone from the source app's state once the
    migration has run, so reading the state alone saw no rename (#66)."""

    def test_the_source_app_records_the_rename(self):
        assert graph.renamed_tables(_moved_between_apps(), 'anc') == {'new_root': ['anc_root']}

    def test_and_the_migration_that_made_it(self):
        assert graph.renames_by_migration(_moved_between_apps(), 'anc') == {
            '0002': [('anc_root', 'new_root')]
        }

    def test_the_destination_app_records_none(self):
        assert graph.renamed_tables(_moved_between_apps(), 'new') == {}

    def test_a_retable_that_keeps_its_model_is_unchanged(self):
        """The model survives the state half, so the ordinary path reads it."""
        kept = SeparateDatabaseAndState(
            database_operations=[AlterModelTable('root', 'anc_trunk')],
            state_operations=[AlterModelTable('root', 'anc_trunk')],
        )
        loader = _StateLoader(('anc', [[_create()], [kept]]))

        assert graph.renamed_tables(loader, 'anc') == {'anc_trunk': ['anc_root']}
        (moves,) = graph.renames_by_migration(loader, 'anc').values()
        assert set(moves) == {('anc_root', 'anc_trunk')}

    def test_a_database_retable_to_the_default_is_not_a_move(self):
        """``table=None`` resets to the default name; nothing names where it went, and the state
        half has the model to read it from anyway."""
        reset = SeparateDatabaseAndState(
            database_operations=[AlterModelTable('root', None)],
            state_operations=[DeleteModel('root')],
        )
        loader = _StateLoader(('anc', [[_create(db_table='custom')], [reset]]))

        assert graph.renamed_tables(loader, 'anc') == {}


    def test_a_database_rename_of_a_model_that_was_never_there_is_ignored(self):
        stray = SeparateDatabaseAndState(
            database_operations=[AlterModelTable('ghost', 'x')],
            state_operations=[DeleteModel('root')],
        )
        loader = _StateLoader(('anc', [[_create()], [stray]]))

        assert graph.renamed_tables(loader, 'anc') == {}

    def test_a_database_retable_to_the_name_it_has_is_not_a_move(self):
        same = SeparateDatabaseAndState(
            database_operations=[AlterModelTable('root', 'anc_root')],
            state_operations=[DeleteModel('root')],
        )
        loader = _StateLoader(('anc', [[_create()], [same]]))

        assert graph.renamed_tables(loader, 'anc') == {}

    def test_another_database_operation_is_no_move(self):
        from django.db.migrations.operations import RunSQL  # noqa: PLC0415

        other = SeparateDatabaseAndState(database_operations=[RunSQL('SELECT 1')])

        assert graph._moved_out(other, {'root': 'anc_root'}, {}) == []

    def test_the_state_keeping_its_model_is_no_move(self):
        """A model the state half keeps is the ordinary path's, not this one's."""
        retable = AlterModelTable('root', 'anc_trunk')
        moved = SeparateDatabaseAndState(database_operations=[retable])

        assert graph._moved_out(moved, {'root': 'anc_root'}, {'root': 'anc_trunk'}) == []
