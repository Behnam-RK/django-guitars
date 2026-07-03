"""Tests for the makeguitarmigrations management command.

The command's file-writing internals (``_create_empty_migration_file``,
``_write_migration_file`` orchestration) are exercised in practice by the test
app's committed advanced migrations applying against Postgres. Here we cover the
scanning, idempotency, and SQL-operation-building logic directly.
"""

import types
from io import StringIO

import pytest
from django.apps import apps
from django.core.management import CommandError, call_command
from django.test import override_settings

from guitars.management.commands import makeguitarmigrations as makeguitarmigrations_module
from guitars.management.commands.makeguitarmigrations import Command
from tests.testapp.models import Album, Band


def test_check_passes_when_advanced_migrations_exist():
    out, err = StringIO(), StringIO()

    call_command('makeguitarmigrations', '--check', stdout=out, stderr=err)

    assert 'Missing advanced migrations' not in err.getvalue()


def test_run_is_idempotent_when_nothing_changed():
    out = StringIO()

    call_command('makeguitarmigrations', stdout=out)

    assert 'No changes detected' in out.getvalue()


def test_build_operations_emits_trigger_rule_and_cascade_ops():
    command = Command()
    # Pretend nothing has been generated yet so every operation is produced.
    command.existing_triggers.clear()
    command.existing_soft_deletes.clear()
    command.existing_soft_delete_related.clear()

    ops = '\n'.join(command._build_operations(apps.get_app_config('testapp')))

    assert 'Updated at Trigger' in ops  # Genre/Band/Album have _updated_at
    assert 'Soft Delete Rule' in ops  # Band/Album have _deleted_at
    assert 'Soft Delete Related Rule' in ops  # Album -> Band cascade


def test_migration_with_digest_returns_false_for_unknown_digest():
    app = apps.get_app_config('testapp')

    assert Command._migration_with_digest_exists(app, 'nonexistentdigest') is False


def test_trigger_function_host_app_resolves_from_settings():
    host = Command()._get_trigger_function_host_app()

    assert host.label == 'testapp'


def test_is_in_scope_matches_local_apps_and_requested_labels():
    testapp = apps.get_app_config('testapp')
    guitars_app = apps.get_app_config('guitars')  # installed but not in LOCAL_APPS

    # Unscoped (empty request): local apps are in scope, non-local apps are not.
    assert Command._is_in_scope(testapp, set()) is True
    assert Command._is_in_scope(guitars_app, set()) is False

    # Scoped: only requested labels among the local apps are in scope.
    assert Command._is_in_scope(testapp, {'testapp'}) is True
    assert Command._is_in_scope(testapp, {'other'}) is False


def test_check_passes_when_scoped_to_named_app():
    out, err = StringIO(), StringIO()

    call_command('makeguitarmigrations', 'testapp', '--check', stdout=out, stderr=err)

    assert 'Missing advanced migrations' not in err.getvalue()


def test_handle_generates_only_for_named_apps(monkeypatch):
    """The per-app loop must scaffold migrations only for the named app(s)."""
    created: list[str] = []

    def build_command():
        command = Command()
        command.stdout = StringIO()
        # Pretend nothing exists yet so generation would otherwise fire...
        command.existing_triggers.clear()
        command.existing_soft_deletes.clear()
        command.existing_soft_delete_related.clear()
        # ...and the shared trigger-function migration is already in place.
        command.trigger_function_dependency = ('testapp', '0001_pretend')
        monkeypatch.setattr(command, '_migration_with_digest_exists', lambda *a, **k: False)
        monkeypatch.setattr(command, '_write_migration_file', lambda **k: None)
        monkeypatch.setattr(
            command,
            '_create_empty_migration_file',
            lambda app, name='auto_advanced': created.append(app.label) or f'0002_{name}.py',
        )
        return command

    # Unscoped: testapp (the only local app with guitar models) is generated.
    build_command().handle(check_only=False)
    assert created == ['testapp']

    # Scoped to a different, real app: testapp is skipped, nothing is generated.
    created.clear()
    build_command().handle('guitars', check_only=False)
    assert created == []


def test_unknown_app_label_raises_command_error():
    with pytest.raises(CommandError):
        call_command('makeguitarmigrations', 'not_a_real_app')


def test_unknown_app_label_raises_command_error_with_check():
    # A typo must not let `--check` silently pass having validated nothing.
    with pytest.raises(CommandError):
        call_command('makeguitarmigrations', 'not_a_real_app', '--check')


def _fake_app_config(name: str, label: str, model_list: list) -> types.SimpleNamespace:
    return types.SimpleNamespace(name=name, label=label, get_models=lambda: model_list)


@override_settings(LOCAL_APPS=['fake.banda', 'fake.albumb'])
def test_scoped_cascade_gap_reported_when_parent_app_out_of_scope(monkeypatch):
    # Real, already-related models (Album -> Band, CASCADE), reassigned to two
    # fake apps so we can scope to one without the other.
    command = Command()
    command.existing_soft_delete_related.clear()

    fake_band_app = _fake_app_config('fake.banda', 'banda', [Band])
    fake_album_app = _fake_app_config('fake.albumb', 'albumb', [Album])
    monkeypatch.setattr(
        makeguitarmigrations_module.django_apps,
        'get_app_configs',
        lambda: [fake_band_app, fake_album_app],
    )

    # Album's app ('albumb') is in scope; Band's app ('banda') -- the cascade
    # rule's parent -- is not, so the Band -> Album cascade rule is skipped.
    notes = command._scoped_cascade_gap_notes({'albumb'})

    assert len(notes) == 1
    assert 'banda' in notes[0]


@override_settings(LOCAL_APPS=['fake.banda', 'fake.albumb'])
def test_scoped_cascade_gap_empty_when_parent_app_in_scope(monkeypatch):
    command = Command()
    command.existing_soft_delete_related.clear()

    fake_band_app = _fake_app_config('fake.banda', 'banda', [Band])
    fake_album_app = _fake_app_config('fake.albumb', 'albumb', [Album])
    monkeypatch.setattr(
        makeguitarmigrations_module.django_apps,
        'get_app_configs',
        lambda: [fake_band_app, fake_album_app],
    )

    # Both apps in scope, or unscoped entirely: no gap to report.
    assert command._scoped_cascade_gap_notes({'banda', 'albumb'}) == []
    assert command._scoped_cascade_gap_notes(set()) == []


@override_settings(LOCAL_APPS=['fake.banda', 'fake.albumb', 'fake.otherc'])
def test_scoped_cascade_gap_silent_when_child_app_also_out_of_scope(monkeypatch):
    """A cascade rule between two apps neither of which is in the requested
    scope is not this run's business -- reporting it would just be noise
    about apps the caller isn't touching right now.
    """
    command = Command()
    command.existing_soft_delete_related.clear()

    fake_band_app = _fake_app_config('fake.banda', 'banda', [Band])
    fake_album_app = _fake_app_config('fake.albumb', 'albumb', [Album])
    fake_other_app = _fake_app_config('fake.otherc', 'otherc', [])
    monkeypatch.setattr(
        makeguitarmigrations_module.django_apps,
        'get_app_configs',
        lambda: [fake_band_app, fake_album_app, fake_other_app],
    )

    # Requested scope is a third, unrelated app -- neither the cascade's
    # parent ('banda') nor its child ('albumb') is part of this run.
    assert command._scoped_cascade_gap_notes({'otherc'}) == []
