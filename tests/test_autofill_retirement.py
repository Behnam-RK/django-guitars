"""Tests for retiring tenant autofill triggers the models no longer require (issue #27).
A rename names a new function, orphaning the old trigger -- which still dereferences the
dropped column and fails every INSERT on the table until it is dropped."""

from __future__ import annotations

import types

import pytest

from guitars.management import _generator
from guitars.management.commands.makeguitarmigrations import Command
from guitars.management.enforcement.headers import (
    HEADER_TENANT_AUTOFILL,
    HEADER_TENANT_AUTOFILL_RETIRED,
)
from guitars.management.enforcement.scanning import scan_existing_operations
from guitars.sql import triggers as _triggers
from guitars.tenancy.discovery import autofill_trigger_name

from .conftest import rows


_TABLE = 'testapp_release'
_STALE = 'guitars_fill_9_venue_venue_id'
_KEY = (_TABLE, _STALE)


def _header(template, table=_TABLE, function=_STALE) -> str:
    return template.format(table=table, function=function) + ' [SQL:abc123def456]\n'


def _scan_with(monkeypatch, *file_contents: str):
    """Run the real scanner over synthetic migration files, in the order given."""

    def _iter(app):
        for index, content in enumerate(file_contents):
            yield types.SimpleNamespace(stem=f'{index:04d}_auto_enforcement'), content

    monkeypatch.setattr(_generator, 'iter_migration_files', _iter)
    return scan_existing_operations()


class TestScanningSubtracts:
    """The retirement header is the one thing this scan removes rather than records."""

    def test_a_create_is_recorded(self, monkeypatch):
        existing = _scan_with(monkeypatch, _header(HEADER_TENANT_AUTOFILL))

        assert _KEY in existing.tenant_autofill

    def test_a_retirement_pops_the_key(self, monkeypatch):
        existing = _scan_with(
            monkeypatch,
            _header(HEADER_TENANT_AUTOFILL),
            _header(HEADER_TENANT_AUTOFILL_RETIRED),
        )

        assert _KEY not in existing.tenant_autofill

    def test_a_readopted_column_reads_as_uncovered_again(self, monkeypatch):
        """The case that breaks a sentinel design: after retire-then-create-again the key
        must carry the *create* digest, so the next run emits a plain CREATE rather than an
        unguarded DROP+CREATE against a trigger the retirement really did remove."""
        existing = _scan_with(
            monkeypatch,
            _header(HEADER_TENANT_AUTOFILL),
            _header(HEADER_TENANT_AUTOFILL_RETIRED),
            _header(HEADER_TENANT_AUTOFILL),
        )

        assert existing.tenant_autofill[_KEY] == 'abc123def456'

    def test_retiring_a_second_time_leaves_it_absent(self, monkeypatch):
        existing = _scan_with(
            monkeypatch,
            _header(HEADER_TENANT_AUTOFILL),
            _header(HEADER_TENANT_AUTOFILL_RETIRED),
            _header(HEADER_TENANT_AUTOFILL),
            _header(HEADER_TENANT_AUTOFILL_RETIRED),
        )

        assert _KEY not in existing.tenant_autofill

    def test_a_retirement_marks_its_app(self, monkeypatch):
        """The file-level ``[DIGEST:...]`` guard assumes an app's operation set never
        recurs. Retirement breaks that, so the scan has to say which apps it broke it for."""
        existing = _scan_with(
            monkeypatch,
            _header(HEADER_TENANT_AUTOFILL),
            _header(HEADER_TENANT_AUTOFILL_RETIRED),
        )

        assert 'testapp' in existing.autofill_retirement_apps

    def test_an_app_that_never_retired_is_not_marked(self, monkeypatch):
        existing = _scan_with(monkeypatch, _header(HEADER_TENANT_AUTOFILL))

        assert existing.autofill_retirement_apps == set()

    def test_a_retirement_does_not_pop_a_different_function_on_the_same_table(
        self, monkeypatch
    ):
        """The key is the pair. Popping by table alone would retire the live trigger of a
        table tenanted on two dimensions the moment the other one was renamed."""
        live = (_TABLE, 'guitars_fill_5_label_label_id')
        existing = _scan_with(
            monkeypatch,
            _header(HEADER_TENANT_AUTOFILL, function=live[1]),
            _header(HEADER_TENANT_AUTOFILL),
            _header(HEADER_TENANT_AUTOFILL_RETIRED),
        )

        assert live in existing.tenant_autofill
        assert _KEY not in existing.tenant_autofill


@pytest.fixture
def _command():
    """A command whose recorded autofill coverage carries one stale key nothing requires."""
    command = Command()
    command.existing.tenant_autofill[_KEY] = 'stale00digest'
    return command


def _testapp():
    from django.apps import apps

    return apps.get_app_config('testapp')


class TestRetirementEmission:
    def test_a_key_the_models_no_longer_require_is_dropped(self, _command):
        operations = _command._retired_autofill_operations(_testapp())

        blob = '\n'.join(operations)
        assert HEADER_TENANT_AUTOFILL_RETIRED.format(table=_TABLE, function=_STALE) in blob
        assert f'DROP TRIGGER "{autofill_trigger_name(_STALE)}" ON "{_TABLE}";' in blob
        assert 'IF EXISTS' not in blob

    def test_the_reverse_recreates_the_trigger(self, _command):
        """A genuine inverse, not a noop -- the migration has to be reversible like every
        other operation this command writes."""
        blob = '\n'.join(_command._retired_autofill_operations(_testapp()))

        assert f'CREATE TRIGGER "{autofill_trigger_name(_STALE)}"' in blob
        assert f'EXECUTE FUNCTION "{_STALE}"();' in blob

    def test_a_required_key_is_never_retired(self, _command):
        """The live trigger testapp really needs must survive a run that retires its
        neighbour, or the generator would drop and re-create it on every invocation."""
        blob = '\n'.join(_command._retired_autofill_operations(_testapp()))

        assert 'guitars_fill_5_label_label_id' not in blob

    def test_a_second_run_emits_nothing(self, _command):
        """Idempotency is the set difference alone: scanning pops the key on reading the
        retirement header, so the next run finds nothing to retire."""
        assert _command._retired_autofill_operations(_testapp())

        del _command.existing.tenant_autofill[_KEY]

        assert _command._retired_autofill_operations(_testapp()) == []

    def test_another_apps_table_is_left_alone(self, _command):
        """A scoped run must not drop a trigger it is not writing migrations for."""
        other = types.SimpleNamespace(label='not_testapp')

        assert _command._retired_autofill_operations(other) == []

    def test_adopt_drops_if_exists(self, _command):
        """--adopt's premise is that nobody knows what the database holds, so the drop may
        legitimately find nothing there."""
        blob = '\n'.join(_command._retired_autofill_operations(_testapp(), adopt=True))

        assert f'DROP TRIGGER IF EXISTS "{autofill_trigger_name(_STALE)}"' in blob

    def test_adopt_does_not_re_retire_an_already_retired_key(self, _command):
        """Unlike every other adopt operation: re-asserting an *absence* would append a
        growing tail of no-op drops to history on every invocation, and a recorded
        retirement is already a positive statement that the drop is in a migration."""
        del _command.existing.tenant_autofill[_KEY]

        assert _command._retired_autofill_operations(_testapp(), adopt=True) == []

    def test_retirement_sorts_before_the_creates(self, _command):
        """"Retire, then create" is how a rename reads. Legibility, not correctness -- the
        two names never collide."""
        # Clear the live key too, so this run has both a create and a retirement to order --
        # which is exactly the shape a rename produces.
        _command.existing.tenant_autofill.pop((_TABLE, 'guitars_fill_5_label_label_id'), None)
        operations = _command._build_operations(_testapp())

        blob = '\n'.join(operations)
        assert blob.index('Tenant autofill Trigger retired') < blob.index(
            'Tenant autofill Trigger on'
        )

    def test_the_retirement_declares_the_function_migration_dependency(self, _command):
        """Its reverse_sql recreates the trigger, so the edge to the function migration is
        what makes reversing the retirement possible at all."""
        _command.tenant_autofill_dependencies[_STALE] = ('testapp', '0019_auto_enforcement')
        blob = '\n'.join(_command._retired_autofill_operations(_testapp()))

        assert ('testapp', '0019_auto_enforcement') in _command._function_dependencies_for(blob)


class TestTheFileDigestGuardYieldsToRetirement:
    """Retire a trigger, then re-adopt it, and the plain CREATE the second run builds is
    byte-identical to the one the original migration carries -- so is its file digest. The
    guard would then skip writing it, leaving the trigger dropped with ``--check`` green."""

    _OPERATIONS = ['# pretend!\nmigrations.RunSQL(\n    sql="",\n    reverse_sql="",\n),\n']

    def _missing(self, command, *, adopt: bool = False) -> list[tuple[str, list[str]]]:
        _, missing = command._generate_stage(
            {'testapp'},
            migration_name='auto_enforcement',
            build_ops=lambda app: list(self._OPERATIONS),
            check_only=True,
            adopt=adopt,
        )
        return missing

    def test_a_recurring_digest_is_skipped_without_a_retirement(self, _command):
        """The guard itself, unchanged for an app whose history only ever added."""
        _command.existing.existing_digests['testapp'] = {_generator.digest_of(self._OPERATIONS)}

        assert self._missing(_command) == []

    def test_a_recurring_digest_is_written_once_the_app_has_retired_something(self, _command):
        _command.existing.existing_digests['testapp'] = {_generator.digest_of(self._OPERATIONS)}
        _command.existing.autofill_retirement_apps.add('testapp')

        assert self._missing(_command) == [('testapp', self._OPERATIONS)]

    def test_adopt_keeps_the_guard_because_it_re_emits_everything(self, _command):
        """``--adopt`` re-emits everything by design, so this guard is its only idempotency:
        waiving it appends a byte-identical migration every run. No waiver is needed there
        anyway -- the adopt form's ``DROP ... IF EXISTS`` digests differently from history."""
        _command.existing.existing_digests['testapp'] = {_generator.digest_of(self._OPERATIONS)}
        _command.existing.autofill_retirement_apps.add('testapp')

        assert self._missing(_command, adopt=True) == []


class TestNotesRatherThanOperations:
    def test_a_table_no_local_model_maps_to_is_named_not_retired(self, _command):
        """There is no app to write the migration into, and this generator has no
        migration-state graph to ask which app used to own the table."""
        _command.existing.tenant_autofill[('gone_table', _STALE)] = 'stale00digest'

        assert _command._retired_autofill_operations(_testapp()) != []
        notes = _command._unmapped_autofill_notes()

        assert len(notes) == 1
        assert 'gone_table' in notes[0]
        assert f'DROP TRIGGER "{autofill_trigger_name(_STALE)}" ON "gone_table";' in notes[0]

    def test_a_scoped_run_names_the_retirement_it_could_not_write(self, _command):
        """The dangerous half to leave silent: the orphan dereferences a dropped column and
        fails every INSERT on its table until some later, wider run retires it."""
        notes = _command._scoped_autofill_gap_notes({'other_app'})

        assert [note for note in notes if _TABLE in note and 'not retired' in note]

    def test_an_unscoped_run_has_no_retirement_gap(self, _command):
        """It writes the retirement itself, so there is nothing to warn about."""
        assert _command._scoped_autofill_gap_notes(set()) == []

    def test_an_orphaned_function_is_named_not_dropped(self, _command):
        """Inert: DROP FUNCTION must follow every trigger depending on it, and a scoped run
        cannot prove an out-of-scope app has no trigger left calling it."""
        _command.existing.tenant_autofill_function_dependencies['guitars_fill_7_gone_gone_id'] = (
            'testapp',
            '0019_auto_enforcement',
        )
        del _command.existing.tenant_autofill[_KEY]

        notes = _command._orphaned_autofill_function_notes()

        assert len(notes) == 1
        assert 'DROP FUNCTION "guitars_fill_7_gone_gone_id"();' in notes[0]

    def test_a_function_a_retirement_candidate_still_names_is_not_orphaned(self, _command):
        """It is still called until the retirement migration is applied, so naming it now
        would tell the operator to drop a function a pending migration depends on."""
        _command.existing.tenant_autofill_function_dependencies[_STALE] = (
            'testapp',
            '0019_auto_enforcement',
        )

        assert _command._orphaned_autofill_function_notes() == []


class TestAgainstTheRealDatabase:
    """The emitted SQL has to work, not merely read correctly."""

    def test_the_forward_drops_and_the_reverse_restores(self, _execute):
        function = 'guitars_fill_test_retirement'
        trigger = autofill_trigger_name(function)
        slots = {
            'table': f'"{_TABLE}"',
            'function': f'"{function}"',
            'trigger': f'"{trigger}"',
        }
        _execute(
            _triggers._CREATE_TENANT_AUTOFILL_FUNCTION.format(
                function=f'"{function}"',
                column='label_id',
                bypass_guc='tenant.bypass',
                guc='tenant.label',
                separator=',',
            )
        )
        _execute(_triggers._CREATE_TENANT_AUTOFILL_TRIGGER.format(**slots))

        def _present() -> bool:
            return bool(
                rows(
                    'SELECT 1 FROM pg_trigger WHERE tgname = %s AND NOT tgisinternal',
                    [trigger],
                )
            )

        assert _present()

        _execute(_triggers._DROP_TENANT_AUTOFILL_TRIGGER.format(**slots))
        assert not _present()

        _execute(_triggers._CREATE_TENANT_AUTOFILL_TRIGGER.format(**slots))
        assert _present()

        _execute(_triggers._DROP_TENANT_AUTOFILL_TRIGGER.format(**slots))
        _execute(_triggers._DROP_TENANT_AUTOFILL_FUNCTION.format(function=f'"{function}"'))
