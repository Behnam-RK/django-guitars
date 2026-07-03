"""Tests for the makeguitarmigrations management command.

The command's file-writing internals (``_create_empty_migration_file``,
``_write_migration_file`` orchestration) are exercised in practice by the test
app's committed advanced migrations applying against Postgres. Here we cover the
scanning, idempotency, and SQL-operation-building logic directly.
"""

from io import StringIO

from django.apps import apps
from django.core.management import call_command

from guitars.management.commands.makeguitarmigrations import Command


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

    # Scoped to a different label: testapp is skipped, nothing is generated.
    created.clear()
    build_command().handle('not_testapp', check_only=False)
    assert created == []
