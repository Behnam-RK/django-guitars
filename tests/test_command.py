"""Tests for the makeguitarmigrations management command.

``_create_empty_migration_file`` (which shells out to ``makemigrations --empty``) is
exercised in practice by the test app's committed advanced migrations applying against
Postgres, since running it for real would scaffold a new migration file on disk. Here we
cover the scanning, idempotency, and SQL-operation-building logic directly, including
``_write_migration_file`` against a throwaway ``tmp_path`` migrations directory.
"""

import types
from io import StringIO

import pytest
from django.apps import apps
from django.core.management import CommandError, call_command
from django.db.models import CASCADE
from django.test import override_settings

from guitars.management.commands import makeguitarmigrations as makeguitarmigrations_module
from guitars.management.commands.makeguitarmigrations import Command
from tests.testapp.models import Album, Band, Ensemble, Orchestra


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


def test_build_operations_emits_mti_ops_for_child_models():
    command = Command()
    command.existing_triggers.clear()
    command.existing_soft_deletes.clear()
    command.existing_soft_delete_related.clear()
    command.existing_mti_triggers.clear()
    command.existing_mti_soft_deletes.clear()

    ops = '\n'.join(command._build_operations(apps.get_app_config('testapp')))

    # MTI children get a parent-propagation updated-at trigger + a redirect soft-delete rule,
    # both on the child table but naming the owning ancestor.
    assert 'MTI Updated at Trigger on "testapp_orchestra" table (parent "testapp_ensemble")' in ops
    assert 'MTI Soft Delete Rule on "testapp_orchestra" table (parent "testapp_ensemble")' in ops
    # Cascade INTO an MTI child (Section -> Orchestra) lands on the owner (ensemble) table.
    assert (
        'Soft Delete Related Rule on "testapp_section" that is related to "testapp_ensemble"'
        in ops
    )
    # The MTI parent-link is structural, not a user cascade FK: no cascade rule for it.
    assert 'related to "testapp_orchestra"' not in ops


def test_cascade_operations_skip_non_cascade_and_non_deletable_relations():
    """Band is the FK target of: Album.band (CASCADE, generates a rule), Album.producer
    (SET_NULL -- skipped, wrong on_delete) and Riff.band (CASCADE, but Riff has no
    _deleted_at -- skipped, nothing to cascade to)."""
    command = Command()
    command.existing_soft_delete_related.clear()

    ops = '\n'.join(command._cascade_operations(Band))

    assert 'testapp_album" that is related to "testapp_band"' in ops  # Album.band survives
    assert 'testapp_riff' not in ops  # Riff has no _deleted_at to cascade into


def test_cascade_operation_warns_when_related_model_is_mti_child_without_own_deleted_at(
    monkeypatch,
):
    """Cascading into an MTI child through a FK declared on the child's own table, while
    its ``_deleted_at`` lives on a farther ancestor, isn't supported (needs a join form) --
    it must be surfaced as a warning instead of emitting a broken rule. Modeled here via a
    synthetic reverse-relation entry (Orchestra doesn't actually have a FK to Band) rather
    than a new schema field, since this is purely about the command's own logic.
    """
    command = Command()
    command._mti_cascade_warnings.clear()
    command.existing_soft_delete_related.clear()

    class _FakeFKField:
        column = 'sponsor_id'
        model = Orchestra
        remote_field = types.SimpleNamespace(parent_link=False)

    command.reverse_relations_mapping[Band] = {(Orchestra, _FakeFKField(), CASCADE)}

    ops = command._cascade_operations(Band)

    assert ops == []
    assert len(command._mti_cascade_warnings) == 1
    warning = command._mti_cascade_warnings[0]
    assert 'testapp_orchestra' in warning
    assert 'multi-table inheritance' in warning


def test_migration_with_digest_returns_false_for_unknown_digest():
    app = apps.get_app_config('testapp')

    assert Command._migration_with_digest_exists(app, 'nonexistentdigest') is False


def test_iter_migration_files_empty_when_no_migrations_dir(tmp_path):
    app = types.SimpleNamespace(path=str(tmp_path))

    assert list(Command._iter_migration_files(app)) == []


def test_migration_with_digest_exists_false_when_no_migrations_dir(tmp_path):
    app = types.SimpleNamespace(path=str(tmp_path))

    assert Command._migration_with_digest_exists(app, 'anydigest') is False


def test_migration_with_digest_exists_true_when_a_file_matches(tmp_path):
    migrations_dir = tmp_path / 'migrations'
    migrations_dir.mkdir()
    (migrations_dir / '0001_initial.py').write_text(
        '# Generated by makeguitarmigrations command! [DIGEST:abc123]\n'
    )
    app = types.SimpleNamespace(path=str(tmp_path))

    assert Command._migration_with_digest_exists(app, 'abc123') is True


_EMPTY_MIGRATION_SCAFFOLD = '''# Generated by Django 5.2 on 2026-01-01

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("testapp", "0001_initial"),
    ]

    operations = []
'''


def _write_empty_migration(tmp_path, filename='0002_auto_advanced.py'):
    migrations_dir = tmp_path / 'migrations'
    migrations_dir.mkdir()
    (migrations_dir / filename).write_text(_EMPTY_MIGRATION_SCAFFOLD)
    return types.SimpleNamespace(path=str(tmp_path)), filename


def test_write_migration_file_inserts_digest_import_operations_and_dependency(tmp_path):
    app, migration_file = _write_empty_migration(tmp_path)

    Command._write_migration_file(
        app=app,
        migration_file=migration_file,
        operations=['# op-marker\nmigrations.RunSQL(sql="SELECT 1;"),\n'],
        operations_digest='digest123',
        dependencies=[('testapp', '0001_trigger_function')],
    )

    content = (tmp_path / 'migrations' / migration_file).read_text()
    assert content.startswith(
        '# Generated by makeguitarmigrations command! [DIGEST:digest123]\n'
    )
    assert 'from guitars import sql' in content
    assert '# op-marker' in content
    assert '("testapp", "0001_trigger_function"),' in content


def test_write_migration_file_skips_self_referential_dependency(tmp_path):
    app, migration_file = _write_empty_migration(tmp_path)

    Command._write_migration_file(
        app=app,
        migration_file=migration_file,
        operations=[],
        operations_digest='digest123',
        # The migration's own stem -- must not depend on itself.
        dependencies=[('testapp', '0002_auto_advanced')],
    )

    content = (tmp_path / 'migrations' / migration_file).read_text()
    assert content.count('0002_auto_advanced') == 0


def test_write_migration_file_skips_dependency_already_present(tmp_path):
    app, migration_file = _write_empty_migration(tmp_path)
    # The scaffold already depends on ("testapp", "0001_initial").

    Command._write_migration_file(
        app=app,
        migration_file=migration_file,
        operations=[],
        operations_digest='digest123',
        dependencies=[('testapp', '0001_initial')],
    )

    content = (tmp_path / 'migrations' / migration_file).read_text()
    assert content.count('0001_initial') == 1


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


@override_settings(LOCAL_APPS=['fake.ensemblea', 'fake.orchestrab'])
def test_scoped_cascade_gap_skips_mti_parent_link(monkeypatch):
    """The MTI parent-link (Orchestra -> Ensemble) is structural, not a user cascade FK --
    it must never be reported as a skipped cascade rule, even when scoped out."""
    command = Command()
    command.existing_soft_delete_related.clear()

    fake_ensemble_app = _fake_app_config('fake.ensemblea', 'ensemblea', [Ensemble])
    fake_orchestra_app = _fake_app_config('fake.orchestrab', 'orchestrab', [Orchestra])
    monkeypatch.setattr(
        makeguitarmigrations_module.django_apps,
        'get_app_configs',
        lambda: [fake_ensemble_app, fake_orchestra_app],
    )

    # Ensemble's app ('ensemblea') is scoped out; Orchestra's ('orchestrab') is in scope --
    # the only relation between them is the structural parent-link, so no gap is reported.
    assert command._scoped_cascade_gap_notes({'orchestrab'}) == []


@override_settings(LOCAL_APPS=['fake.banda', 'fake.albumb'])
def test_scoped_cascade_gap_skipped_when_rule_already_exists(monkeypatch):
    command = Command()

    fake_band_app = _fake_app_config('fake.banda', 'banda', [Band])
    fake_album_app = _fake_app_config('fake.albumb', 'albumb', [Album])
    monkeypatch.setattr(
        makeguitarmigrations_module.django_apps,
        'get_app_configs',
        lambda: [fake_band_app, fake_album_app],
    )
    command.existing_soft_delete_related.add((Album._meta.db_table, Band._meta.db_table))

    assert command._scoped_cascade_gap_notes({'albumb'}) == []


def test_handle_skips_app_when_digest_already_exists(monkeypatch):
    """If the freshly-computed operations digest for an app already matches a committed
    migration (however that happened to be detected), handle() must skip it silently --
    no new migration file gets created."""
    command = Command()
    command.stdout = StringIO()
    command.existing_triggers.clear()
    command.existing_soft_deletes.clear()
    command.existing_soft_delete_related.clear()
    command.trigger_function_dependency = ('testapp', '0001_pretend')
    monkeypatch.setattr(command, '_migration_with_digest_exists', lambda *a, **k: True)
    created: list[str] = []
    monkeypatch.setattr(
        command, '_create_empty_migration_file', lambda *a, **k: created.append(1)
    )

    command.handle('testapp', check_only=False)

    assert created == []


def test_handle_check_only_reports_missing_migrations_and_mti_warnings(monkeypatch):
    command = Command()
    command.stdout = StringIO()
    command.stderr = StringIO()
    command.existing_triggers.clear()
    command.existing_soft_deletes.clear()
    command.existing_soft_delete_related.clear()
    command.existing_mti_triggers.clear()
    command.existing_mti_soft_deletes.clear()
    command.trigger_function_dependency = ('testapp', '0001_pretend')
    command.parent_trigger_function_dependency = ('testapp', '0001_pretend_parent')
    monkeypatch.setattr(command, '_migration_with_digest_exists', lambda *a, **k: False)
    # Surfaced regardless of check_only -- seeded directly rather than relying on a real
    # MTI-cascade-limitation model, since that's covered at the unit level above.
    command._mti_cascade_warnings.append('some skipped MTI cascade rule')

    with pytest.raises(CommandError, match='Run `manage.py makeguitarmigrations`'):
        command.handle('testapp', check_only=True)

    assert 'Missing advanced migrations' in command.stderr.getvalue()
    assert 'some skipped MTI cascade rule' in command.stderr.getvalue()


@override_settings(LOCAL_APPS=['fake.banda', 'fake.albumb'])
def test_handle_writes_scoped_cascade_gap_warning_to_stdout(monkeypatch):
    command = Command()
    command.stdout = StringIO()
    command.stderr = StringIO()
    command.existing_soft_delete_related.clear()
    # Both singleton function migrations already exist, so the per-app loop is the only
    # thing left to exercise.
    command.trigger_function_dependency = ('albumb', '0001_pretend')
    command.parent_trigger_function_dependency = ('albumb', '0001_pretend_parent')

    fake_band_app = _fake_app_config('fake.banda', 'banda', [Band])
    fake_album_app = _fake_app_config('fake.albumb', 'albumb', [Album])
    fake_apps_by_label = {'banda': fake_band_app, 'albumb': fake_album_app}
    monkeypatch.setattr(
        makeguitarmigrations_module.django_apps,
        'get_app_configs',
        lambda: [fake_band_app, fake_album_app],
    )
    monkeypatch.setattr(
        makeguitarmigrations_module.django_apps,
        'get_app_config',
        lambda label: fake_apps_by_label[label],
    )
    monkeypatch.setattr(command, '_migration_with_digest_exists', lambda *a, **k: True)

    command.handle('albumb', check_only=False)

    assert "parent app 'banda' is not in this scoped run" in command.stdout.getvalue()
