"""Retirement of the owned rule, its sweep and the self-cascade trigger (#66). None of the three
was retired before, so relaxing or removing the key behind one left it live: the plpgsql bodies
name the column, and after ``DROP COLUMN ... CASCADE`` every UPDATE on the table failed."""

import types

import pytest
from django.apps import apps
from django.db import ProgrammingError, connection, migrations, transaction

from guitars.management import _generator
from guitars.management.enforcement.command import Command
from guitars.management.enforcement.headers import (
    HEADER_SOFT_DELETE_OWNED,
    HEADER_SOFT_DELETE_OWNED_RETIRED,
    HEADER_SOFT_DELETE_OWNED_SWEEP,
    HEADER_SOFT_DELETE_OWNED_SWEEP_RETIRED,
    HEADER_SOFT_DELETE_SELF_CASCADE,
    HEADER_SOFT_DELETE_SELF_CASCADE_RETIRED,
)
from guitars.management.enforcement.scanning import scan_existing_operations
from tests.conftest import clear_cascade_coverage, execute, scalar


OWNED = ('testapp_gone_target', 'testapp_setlist', 'gone_id')
SELF = ('testapp_setlist', 'gone_parent_id')


def _command() -> Command:
    command = Command()
    clear_cascade_coverage(command)
    return command


def _non_revive(notes: list[str]) -> list[str]:
    """*notes* less the revive family's: ``_command`` clears every recorded owner trigger, so a
    scoped run names each as missing beside the family a test is about."""
    return [note for note in notes if not note.startswith('Revive trigger')]


def _retired(command: Command) -> list[str]:
    return command._retired_trigger_operations(apps.get_app_config('testapp'))


def _forward_sql(operation: str) -> str:
    source = operation[operation.index('migrations.RunSQL') :].rstrip().rstrip(',')
    return eval(source, {'migrations': migrations}).sql  # noqa: S307 - our own output


def _headers(operations: list[str]) -> list[str]:
    return [operation.split('!')[0] + '!' for operation in operations]


class TestAnOwnedKeyNoLongerDeclared:
    def test_both_halves_are_retired(self):
        command = _command()
        command.existing.soft_delete_owned[OWNED] = 'abc'
        command.existing.soft_delete_owned_sweep[OWNED] = 'def'

        retired = _retired(command)

        assert _headers(retired) == [
            HEADER_SOFT_DELETE_OWNED_RETIRED.format(
                dependent_table=OWNED[0], table=OWNED[1], foreign_key=OWNED[2]
            ),
            HEADER_SOFT_DELETE_OWNED_SWEEP_RETIRED.format(
                dependent_table=OWNED[0], table=OWNED[1], foreign_key=OWNED[2]
            ),
        ]
        rule, sweep = (_forward_sql(operation) for operation in retired)
        assert 'DROP RULE IF EXISTS' in rule
        assert 'DROP TRIGGER IF EXISTS' in sweep
        assert 'DROP FUNCTION IF EXISTS' in sweep

    def test_only_the_recorded_half_is_retired(self):
        """A project generated before 2.6.0 has a rule and no sweep."""
        command = _command()
        command.existing.soft_delete_owned[OWNED] = 'abc'

        assert len(_retired(command)) == 1

    def test_the_reverse_refuses_and_points_at_adopt(self):
        command = _command()
        command.existing.soft_delete_owned_sweep[OWNED] = 'def'

        (operation,) = _retired(command)

        assert 'RAISE EXCEPTION' in operation
        assert 'makeguitarmigrations --adopt' in operation

    def test_a_declared_key_is_left_alone_even_when_refused(self):
        """A refusal (a cycle, a tenancy mismatch) already escalates a live rule to a failing
        ``--check`` naming the hand-drop; retiring it here would answer a different question."""
        command = _command()
        live = next(
            key
            for key in scan_existing_operations().soft_delete_owned_sweep
            if command._table_app_labels().get(key[1]) == 'testapp'
        )
        command.existing.soft_delete_owned_sweep[live] = 'def'

        assert _retired(command) == []

    def test_an_owner_mapping_to_no_model_is_left_alone(self):
        """Its table went with every trigger on it, or its app is out of scope: either way
        there is nothing this app can drop."""
        command = _command()
        command.existing.soft_delete_owned_sweep[('t', 'shop_gone', 'x_id')] = 'def'

        assert _retired(command) == []


class TestARetirementIsOrderedAfterItsCreate:
    """An MTI descendant's pass writes a self-cascade trigger into the descendant's app while the
    retirement lands in the table's: with ``IF EXISTS`` a drop that ran first would be a no-op,
    and the create would bring the trigger back on a fresh ``migrate`` (#66)."""

    def test_a_self_cascade_created_in_another_app(self):
        command = _command()
        command.existing.soft_delete_self_cascade[SELF] = 'abc'
        command.existing.soft_delete_self_cascade_dependencies[SELF] = [('otherapp', '0002_x')]

        _retired(command)

        assert ('otherapp', '0002_x') in command._retirement_edges['testapp']

    def test_an_owned_sweep_created_in_another_app(self):
        command = _command()
        command.existing.soft_delete_owned_sweep[OWNED] = 'def'
        command.existing.soft_delete_owned_sweep_dependencies[OWNED] = [('otherapp', '0003_y')]

        _retired(command)

        assert ('otherapp', '0003_y') in command._retirement_edges['testapp']


class TestASelfCascadeKeyNoLongerRequired:
    def test_it_is_retired(self):
        command = _command()
        command.existing.soft_delete_self_cascade[SELF] = 'abc'

        (operation,) = _retired(command)

        assert operation.startswith(
            HEADER_SOFT_DELETE_SELF_CASCADE_RETIRED.format(table=SELF[0], foreign_key=SELF[1])
        )
        assert 'DROP TRIGGER IF EXISTS' in _forward_sql(operation)
        assert 'DROP FUNCTION IF EXISTS' in _forward_sql(operation)
        assert 'RAISE EXCEPTION' in operation

    def test_the_live_one_is_left_alone(self):
        command = _command()
        command.existing.soft_delete_self_cascade[('testapp_setlist', 'parent_id')] = 'abc'

        assert _retired(command) == []


@pytest.mark.django_db
class TestTheLeakAndTheRepair:
    """Against the database: the self-cascade trigger names its key column, so dropping that
    column with ``CASCADE`` leaves a trigger failing every UPDATE, and the retirement repairs it."""

    TABLE, COLUMN = 'testapp_setlist', 'parent_id'

    def _retirement(self):
        command = _command()
        key = (self.TABLE, self.COLUMN)
        command.existing.soft_delete_self_cascade[key] = 'abc'
        required = command._required_self_cascades
        command._required_self_cascades = lambda: required() - {key}  # the key relaxed
        (operation,) = _retired(command)
        return _forward_sql(operation)

    def test_dropping_the_column_breaks_every_update(self):
        with pytest.raises(ProgrammingError, match='parent_id'):
            with transaction.atomic():
                execute(f'ALTER TABLE {self.TABLE} DROP COLUMN {self.COLUMN} CASCADE')
                execute(f'UPDATE {self.TABLE} SET title = title')

    def test_the_retirement_repairs_it(self):
        drop = self._retirement()
        with transaction.atomic():
            execute(f'ALTER TABLE {self.TABLE} DROP COLUMN {self.COLUMN} CASCADE')
            execute(drop)
            execute(f'UPDATE {self.TABLE} SET title = title')
            assert not scalar(
                "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'soft_delete_self_cascade%%'"
                ' AND tgrelid = %s::regclass',
                [self.TABLE],
            )
            transaction.set_rollback(True)


def _scan_with(monkeypatch, *file_contents: str):
    def _iter(app):
        for index, content in enumerate(file_contents):
            yield types.SimpleNamespace(stem=f'{index:04d}_auto_enforcement'), content

    monkeypatch.setattr(_generator, 'iter_migration_files', _iter)
    return scan_existing_operations()


def _owned_header(template) -> str:
    return (
        template.format(dependent_table=OWNED[0], table=OWNED[1], foreign_key=OWNED[2])
        + ' [SQL:abc123def456]\n'
    )


def _self_header(template) -> str:
    return template.format(table=SELF[0], foreign_key=SELF[1]) + ' [SQL:abc123def456]\n'


@pytest.mark.parametrize(
    ('create', 'retire', 'header', 'family', 'key'),
    [
        (
            HEADER_SOFT_DELETE_OWNED,
            HEADER_SOFT_DELETE_OWNED_RETIRED,
            _owned_header,
            'soft_delete_owned',
            OWNED,
        ),
        (
            HEADER_SOFT_DELETE_OWNED_SWEEP,
            HEADER_SOFT_DELETE_OWNED_SWEEP_RETIRED,
            _owned_header,
            'soft_delete_owned_sweep',
            OWNED,
        ),
        (
            HEADER_SOFT_DELETE_SELF_CASCADE,
            HEADER_SOFT_DELETE_SELF_CASCADE_RETIRED,
            _self_header,
            'soft_delete_self_cascade',
            SELF,
        ),
    ],
    ids=['owned', 'sweep', 'self'],
)
class TestTheScan:
    def test_a_retirement_pops_the_key_and_marks_no_app(
        self, monkeypatch, create, retire, header, family, key
    ):
        """Marked where a create or drop is *written* (ADR 0021 point 9), never at scan time: a
        one-time retirement -- every owned rule's, in 2.19.0 (#80) -- must not waive the digest
        guard for the app for ever."""
        existing = _scan_with(monkeypatch, header(create), header(retire))

        assert key not in getattr(existing, family)
        assert 'testapp' not in existing.retirement_apps

    def test_a_create_after_it_records_the_key_again(
        self, monkeypatch, create, retire, header, family, key
    ):
        existing = _scan_with(monkeypatch, header(create), header(retire), header(create))

        assert key in getattr(existing, family)


def test_a_generation_writes_the_retirement():
    """The wiring: the retirement reaches the operations a run writes for the app."""
    command = _command()
    command.existing.soft_delete_self_cascade[SELF] = 'abc'

    operations = command._build_operations(apps.get_app_config('testapp'))

    assert any(
        operation.startswith(
            HEADER_SOFT_DELETE_SELF_CASCADE_RETIRED.format(table=SELF[0], foreign_key=SELF[1])
        )
        for operation in operations
    )


def _retired_for(command: Command) -> list[str]:
    app = apps.get_app_config('testapp')
    # The owner's revive re-emitted without the arm, or retired with its last, since 2.16.0.
    return (
        command._retired_trigger_operations(app)
        + command._retired_cascade_operations(app)
        + command._revive_operations(app)
    )


def _without(command: Command, method: str, drop):
    """*command* with *method*'s answer less *drop*: the key the models stopped calling for."""
    original = getattr(command, method)

    def narrowed():
        answer = original()
        if isinstance(answer, tuple):  # ``_cascade_key_maps``
            required, by_table = answer
            return {k: v for k, v in required.items() if k != drop}, by_table
        return answer - {drop}

    setattr(command, method, narrowed)
    if method == '_cascade_key_maps':
        # The key's revive arm goes with it: the owner's one trigger is built off the same sweep.
        dropped_arm = (drop[0], drop[1], original()[0][drop])
        def arms():
            narrowed()
            return {
                owner: kept
                for owner, keyed in command._revive_arm_sources.items()
                # Arms are keyed on the real column, which a ``None`` key form leaves out: the
                # relation it stands for is the one ``required`` mapped it to.
                if (kept := {key: arm for key, arm in keyed.items() if key != dropped_arm})
            }

        command._revive_arms_by_owner = arms
    return command


def _update(table: str) -> None:
    execute(f'UPDATE {table} SET _deleted_at = _deleted_at')


@pytest.mark.django_db
class TestEveryLeakIsRepairedByItsRetirement:
    """For each family: the column its trigger names dropped with ``CASCADE`` (what Django 5.x's
    ``RemoveField`` does), every UPDATE then failing, and the generated retirement repairing it."""

    def _repairs(self, table, column, owner, retirement, update=_update):
        with transaction.atomic():
            execute(f'ALTER TABLE {table} DROP COLUMN {column} CASCADE')
            with pytest.raises(ProgrammingError), transaction.atomic():
                update(owner)
            for operation in retirement:
                execute(_forward_sql(operation))
            update(owner)
            transaction.set_rollback(True)

    @staticmethod
    def _revive(table: str) -> None:
        """The UPDATE a revive trigger acts on: since 2.16.0 one that revives nothing leaves at
        the trigger's first test, so only a revive reaches the arm naming the dropped column."""
        execute(f'UPDATE {table} SET _deleted_at = NULL WHERE _deleted_at IS NOT NULL')

    @staticmethod
    def _archived_setlist() -> None:
        from tests.testapp.models import Setlist  # noqa: PLC0415

        Setlist.objects.create(title='s')
        execute('UPDATE testapp_setlist SET _deleted_at = NOW()')

    def test_the_owned_sweep(self):
        key = ('testapp_stagehand', 'testapp_rider', 'stagehand_id')
        command = _without(Command(), '_declared_owned_keys', key)

        self._repairs('testapp_rider', 'stagehand_id', 'testapp_rider', _retired_for(command))

    def test_the_revive_after_the_childs_key_is_removed(self):
        key = ('testapp_setlistentry', 'testapp_setlist', None)
        command = _without(Command(), '_cascade_key_maps', key)
        self._archived_setlist()

        self._repairs(
            'testapp_setlistentry',
            'setlist_id',
            'testapp_setlist',
            _retired_for(command),
            update=self._revive,
        )

    def test_retire_enforcement_first_is_the_path_django_6_needs(self):
        """``RetireEnforcement`` before the ``RemoveField``: the rule goes, the owner's revive
        does not, and the generated retirement takes it."""
        from guitars.operations import RetireEnforcement  # noqa: PLC0415

        key = ('testapp_setlistentry', 'testapp_setlist', None)
        retirement = _retired_for(_without(Command(), '_cascade_key_maps', key))
        self._archived_setlist()
        with transaction.atomic():
            with connection.schema_editor() as editor:
                RetireEnforcement('testapp_setlistentry', 'setlist_id').database_forwards(
                    'testapp', editor, None, None
                )
            execute('ALTER TABLE testapp_setlistentry DROP COLUMN setlist_id')
            with pytest.raises(ProgrammingError), transaction.atomic():
                self._revive('testapp_setlist')
            for operation in retirement:
                execute(_forward_sql(operation))
            self._revive('testapp_setlist')
            transaction.set_rollback(True)


def test_each_retirement_lands_in_the_app_that_wrote_its_create():
    """In a single-app project the create and the retirement share an app, whose own history
    orders them; where they do not (an MTI descendant's self cascade), the retirement depends on
    the create -- ``TestARetirementIsOrderedAfterItsCreate``."""
    command = Command()
    existing = command.existing
    hosting = command._table_app_labels()
    files = {
        app.label: [content for _path, content in _generator.iter_migration_files(app)]
        for app in apps.get_app_configs()
        if _generator.is_local(app)
    }
    for header, fires_on, keys in (
        (
            HEADER_SOFT_DELETE_OWNED_SWEEP,
            lambda key: key[1],
            existing.soft_delete_owned_sweep,
        ),
        (HEADER_SOFT_DELETE_SELF_CASCADE, lambda key: key[0], existing.soft_delete_self_cascade),
    ):
        for key in keys:
            text = (
                header.format(dependent_table=key[0], table=key[1], foreign_key=key[2])
                if len(key) == 3
                else header.format(table=key[0], foreign_key=key[1])
            )
            writers = {
                label for label, contents in files.items() if any(text in c for c in contents)
            }
            assert writers == {hosting[fires_on(key)]}, key


class TestAScopedRunNamesWhatItLeaves:
    def test_an_owned_sweep_hosted_out_of_scope_is_named(self):
        command = _command()
        command.existing.soft_delete_owned_sweep[OWNED] = 'def'

        (note,) = _non_revive(command._scoped_trigger_retirement_notes({'crossapp_owner'}))

        assert OWNED[1] in note
        assert 'testapp' in note

    def test_a_self_cascade_hosted_out_of_scope_is_named(self):
        command = _command()
        command.existing.soft_delete_self_cascade[SELF] = 'abc'

        (note,) = _non_revive(command._scoped_trigger_retirement_notes({'crossapp_owner'}))

        assert SELF[0] in note

    def test_nothing_is_named_when_the_host_is_in_scope(self):
        command = _command()
        command.existing.soft_delete_self_cascade[SELF] = 'abc'

        assert command._scoped_trigger_retirement_notes({'testapp'}) == []

    def test_a_scoped_run_prints_it(self, monkeypatch):
        from io import StringIO  # noqa: PLC0415

        from django.core.management import call_command  # noqa: PLC0415

        monkeypatch.setattr(
            Command, '_scoped_trigger_retirement_notes', lambda self, requested: ['LEFT-BEHIND']
        )
        out = StringIO()
        call_command(
            'makeguitarmigrations', 'crossapp_owner', '--check', stdout=out, stderr=StringIO()
        )

        assert 'LEFT-BEHIND' in out.getvalue()


class TestARenameInAnotherAppReachesTheseKeys:
    """An app walked after the one that renamed a table records its keys under the old name and
    the walk never moves them, so the owned key read as undeclared and was retired on every run."""

    @staticmethod
    def _scan(monkeypatch, *contents):
        from guitars.management.enforcement import scanning  # noqa: PLC0415

        monkeypatch.setattr(
            scanning, 'renamed_tables', lambda loader, label: {'tgt_prize': ['tgt_target']}
        )
        monkeypatch.setattr(scanning, 'renames_by_migration', lambda loader, label: {})
        return _scan_with(monkeypatch, *contents)

    @staticmethod
    def _header(template, table='tgt_target') -> str:
        return (
            template.format(dependent_table=table, table='own_owner', foreign_key='target_id')
            + ' [SQL:abc123def456]\n'
        )

    def test_a_create_under_the_old_name_is_filed_under_the_new(self, monkeypatch):
        existing = self._scan(monkeypatch, self._header(HEADER_SOFT_DELETE_OWNED_SWEEP))

        assert ('tgt_prize', 'own_owner', 'target_id') in existing.soft_delete_owned_sweep
        assert ('tgt_target', 'own_owner', 'target_id') not in existing.soft_delete_owned_sweep
        assert (
            'tgt_prize',
            'own_owner',
            'target_id',
        ) in existing.soft_delete_owned_sweep_dependencies

    def test_its_retirement_under_the_new_name_takes_it(self, monkeypatch):
        existing = self._scan(
            monkeypatch,
            self._header(HEADER_SOFT_DELETE_OWNED_SWEEP),
            self._header(HEADER_SOFT_DELETE_OWNED_SWEEP_RETIRED, table='tgt_prize'),
        )

        assert not existing.soft_delete_owned_sweep


def test_an_unscoped_run_names_nothing_it_is_about_to_write():
    command = _command()
    command.existing.soft_delete_self_cascade[SELF] = 'abc'

    assert command._scoped_trigger_retirement_notes(set()) == []


class TestARedeclaredKeyIsOrderedAfterItsRetirement:
    """Retired, then declared again: unordered against that retirement, a fresh ``migrate``
    could run the create first, and the drop then took what the create made (ADR 0021)."""

    @staticmethod
    def _edges(command) -> list:
        command._build_operations(apps.get_app_config('testapp'))
        return command._retirement_edges.get('testapp', [])

    def test_a_self_cascade(self):
        from guitars.management.enforcement.scanning import CascadeRetirementSite  # noqa: PLC0415

        command = _command()
        key = ('testapp_setlist', 'parent_id')
        command.existing.soft_delete_self_cascade.pop(key)
        command.existing.self_cascade_retirement_sites.append(
            CascadeRetirementSite('otherapp', '0007_x', key, None)
        )

        assert ('otherapp', '0007_x') in self._edges(command)

    def test_an_owned_sweep(self):
        """The rule no longer has a create to order (#80, ADR 0039): only the sweep does."""
        from guitars.management.enforcement.scanning import CascadeRetirementSite  # noqa: PLC0415

        command = _command()
        key = ('testapp_stagehand', 'testapp_rider', 'stagehand_id')
        command.existing.soft_delete_owned_sweep.pop(key)
        command.existing.owned_sweep_retirement_sites.append(
            CascadeRetirementSite('otherapp', '0009_z', key, None)
        )

        assert ('otherapp', '0009_z') in self._edges(command)


def test_a_rename_walked_late_does_not_overwrite_a_newer_record(monkeypatch):
    """Across apps the walk is registry order, not time: an app walked first can have filed a
    key under the new name *after* the rename happened, and moving the old entry onto it then
    replaced the newer record with the older one -- the owned rule re-emitted every run."""
    from guitars.management.enforcement import scanning  # noqa: PLC0415

    monkeypatch.setattr(
        scanning, 'renamed_tables', lambda loader, label: {'tgt_prize': ['tgt_target']}
    )
    monkeypatch.setattr(
        scanning,
        'renames_by_migration',
        lambda loader, label: {'0001_auto_enforcement': [('tgt_target', 'tgt_prize')]},
    )

    def header(table, digest):
        return (
            HEADER_SOFT_DELETE_OWNED.format(
                dependent_table=table, table='own_owner', foreign_key='target_id'
            )
            + f' [SQL:{digest}]\n'
        )

    existing = _scan_with(
        monkeypatch, header('tgt_target', 'old000000000') + header('tgt_prize', 'new000000000'), ''
    )

    assert existing.soft_delete_owned[('tgt_prize', 'own_owner', 'target_id')] == 'new000000000'
