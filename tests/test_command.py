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
