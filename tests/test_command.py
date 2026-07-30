"""Tests for the makeguitarmigrations management command.

``_create_empty_migration_file`` (which shells out to ``makemigrations --empty``) is
exercised in practice by the test app's committed enforcement migrations applying against
Postgres, since running it for real would scaffold a new migration file on disk. Here we
cover the scanning, idempotency, and SQL-operation-building logic directly, including
``_write_migration_file`` against a throwaway ``tmp_path`` migrations directory.
"""

import types
from io import StringIO

import pytest
from django.apps import apps
from django.apps import apps as django_apps
from django.core.management import CommandError, call_command
from django.db.models import CASCADE
from django.test import override_settings

from guitars.management import _generator
from guitars.management.commands import makeguitarmigrations as makeguitarmigrations_module
from guitars.management.commands.makeguitarmigrations import Command
from tests.testapp.models import Album, Band, Ensemble, Orchestra


def test_check_passes_when_enforcement_migrations_exist():
    out, err = StringIO(), StringIO()

    call_command('makeguitarmigrations', '--check', stdout=out, stderr=err)

    assert 'Missing enforcement migrations' not in err.getvalue()


def test_run_is_idempotent_when_nothing_changed():
    out = StringIO()

    call_command('makeguitarmigrations', stdout=out)

    assert 'No changes detected' in out.getvalue()


def test_build_operations_emits_trigger_rule_and_cascade_ops():
    command = Command()
    # Pretend nothing has been generated yet so every operation is produced.
    command.existing.triggers.clear()
    command.existing.soft_deletes.clear()
    command.existing.soft_delete_related.clear()

    ops = '\n'.join(command._build_operations(apps.get_app_config('testapp')))

    assert 'Updated at Trigger' in ops  # Genre/Band/Album have _updated_at
    assert 'Soft Delete Rule' in ops  # Band/Album have _deleted_at
    assert 'Soft Delete Related Rule' in ops  # Album -> Band cascade


def test_build_operations_emits_mti_ops_for_child_models():
    command = Command()
    command.existing.triggers.clear()
    command.existing.soft_deletes.clear()
    command.existing.soft_delete_related.clear()
    command.existing.mti_triggers.clear()
    command.existing.mti_soft_deletes.clear()

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
    command.existing.soft_delete_related.clear()

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
    command.existing.soft_delete_related.clear()

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
    assert 'multi-table-inheritance ancestor' in warning


def test_migration_with_digest_returns_false_for_unknown_digest():
    app = apps.get_app_config('testapp')

    assert _generator.migration_with_digest_exists(app, 'nonexistentdigest') is False


def test_iter_migration_files_empty_when_no_migrations_dir(tmp_path):
    app = types.SimpleNamespace(path=str(tmp_path))

    assert list(_generator.iter_migration_files(app)) == []


def test_migration_with_digest_exists_false_when_no_migrations_dir(tmp_path):
    app = types.SimpleNamespace(path=str(tmp_path))

    assert _generator.migration_with_digest_exists(app, 'anydigest') is False


def test_migration_with_digest_exists_true_when_a_file_matches(tmp_path):
    migrations_dir = tmp_path / 'migrations'
    migrations_dir.mkdir()
    (migrations_dir / '0001_initial.py').write_text(
        '# Generated by makeguitarmigrations command! [DIGEST:abc123]\n'
    )
    app = types.SimpleNamespace(path=str(tmp_path))

    assert _generator.migration_with_digest_exists(app, 'abc123') is True


# Must match Django's real ``makemigrations --empty`` output, in particular that
# ``operations = [`` and its closing ``]`` are on SEPARATE lines. Operations are
# inserted before the file's last line, so a single-line ``operations = []`` scaffold
# would place them OUTSIDE the list -- producing a structurally broken migration that
# a substring assertion still passes. See MIGRATION_TEMPLATE in
# django/db/migrations/writer.py.
_EMPTY_MIGRATION_SCAFFOLD = """# Generated by Django 5.2 on 2026-01-01

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("testapp", "0001_initial"),
    ]

    operations = [
    ]
"""


def _write_empty_migration(tmp_path, filename='0002_auto_enforcement.py'):
    migrations_dir = tmp_path / 'migrations'
    migrations_dir.mkdir()
    (migrations_dir / filename).write_text(_EMPTY_MIGRATION_SCAFFOLD)
    return types.SimpleNamespace(path=str(tmp_path)), filename


def test_write_migration_file_output_is_exact(tmp_path):
    """Pin the generated file byte for byte.

    Substring assertions cannot catch the failure that actually matters here: an
    operation landing outside ``operations = [...]``, or the ``sql`` import landing
    inside the class body. Both produce a file that still contains every expected
    fragment and still fails at ``migrate`` time in someone else's project.
    """
    app, migration_file = _write_empty_migration(tmp_path)

    Command._write_migration_file(
        app=app,
        migration_file=migration_file,
        operations=['# op-marker\nmigrations.RunSQL(sql="SELECT 1;"),\n'],
        operations_digest='digest123',
        dependencies=[('testapp', '0001_trigger_function')],
    )

    content = (tmp_path / 'migrations' / migration_file).read_text()
    assert (
        content
        == """# Generated by makeguitarmigrations command! [DIGEST:digest123]

from django.db import migrations

from guitars import sql


class Migration(migrations.Migration):

    dependencies = [
        ("testapp", "0001_trigger_function"),
        ("testapp", "0001_initial"),
    ]

    operations = [
        # op-marker
        migrations.RunSQL(sql="SELECT 1;"),

    ]
"""
    )


def test_write_migration_file_puts_operations_inside_the_operations_list(tmp_path):
    """The structural guarantee, asserted independently of exact formatting.

    Parsed rather than pattern-matched: if the generated module does not import, or
    the operation ends up outside the list, ``operations`` is empty and this fails --
    which is the regression a substring check silently allows through.
    """
    app, migration_file = _write_empty_migration(tmp_path)

    Command._write_migration_file(
        app=app,
        migration_file=migration_file,
        operations=['# op-marker\nmigrations.RunSQL(sql="SELECT 1;"),\n'],
        operations_digest='digest123',
        dependencies=None,
    )

    path = tmp_path / 'migrations' / migration_file
    namespace: dict = {}
    exec(compile(path.read_text(), str(path), 'exec'), namespace)  # noqa: S102 - generated file

    operations = namespace['Migration'].operations
    assert len(operations) == 1, 'the operation did not land inside operations = [...]'
    assert operations[0].sql == 'SELECT 1;'


def test_write_migration_file_skips_self_referential_dependency(tmp_path):
    app, migration_file = _write_empty_migration(tmp_path)

    Command._write_migration_file(
        app=app,
        migration_file=migration_file,
        operations=[],
        operations_digest='digest123',
        # The migration's own stem -- must not depend on itself.
        dependencies=[('testapp', '0002_auto_enforcement')],
    )

    content = (tmp_path / 'migrations' / migration_file).read_text()
    assert content.count('0002_auto_enforcement') == 0


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
    assert _generator.is_in_scope(testapp, set()) is True
    assert _generator.is_in_scope(guitars_app, set()) is False

    # Scoped: only requested labels among the local apps are in scope.
    assert _generator.is_in_scope(testapp, {'testapp'}) is True
    assert _generator.is_in_scope(testapp, {'other'}) is False


def test_check_passes_when_scoped_to_named_app():
    out, err = StringIO(), StringIO()

    call_command('makeguitarmigrations', 'testapp', '--check', stdout=out, stderr=err)

    assert 'Missing enforcement migrations' not in err.getvalue()


def test_handle_generates_only_for_named_apps(monkeypatch):
    """The per-app loop must scaffold migrations only for the named app(s)."""
    created: list[str] = []

    def build_command():
        command = Command()
        command.stdout = StringIO()
        # Pretend nothing exists yet so generation would otherwise fire...
        command.existing.triggers.clear()
        command.existing.soft_deletes.clear()
        command.existing.soft_delete_related.clear()
        # ...and the shared trigger-function migration is already in place.
        command.trigger_function_dependency = ('testapp', '0001_pretend')
        monkeypatch.setattr(_generator, 'migration_with_digest_exists', lambda *a, **k: False)
        monkeypatch.setattr(command, '_write_migration_file', lambda **k: None)
        monkeypatch.setattr(
            _generator,
            'create_empty_migration_file',
            lambda app, name='auto_enforcement': created.append(app.label) or f'0002_{name}.py',
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
    command.existing.soft_delete_related.clear()

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
    command.existing.soft_delete_related.clear()

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
    command.existing.soft_delete_related.clear()

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
    command.existing.soft_delete_related.clear()

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
    command.existing.soft_delete_related.add((Album._meta.db_table, Band._meta.db_table))

    assert command._scoped_cascade_gap_notes({'albumb'}) == []


def test_handle_skips_app_when_digest_already_exists(monkeypatch):
    """If the freshly-computed operations digest for an app already matches a committed
    migration (however that happened to be detected), handle() must skip it silently --
    no new migration file gets created."""
    command = Command()
    command.stdout = StringIO()
    command.existing.triggers.clear()
    command.existing.soft_deletes.clear()
    command.existing.soft_delete_related.clear()
    command.trigger_function_dependency = ('testapp', '0001_pretend')
    monkeypatch.setattr(_generator, 'migration_with_digest_exists', lambda *a, **k: True)
    created: list[str] = []
    monkeypatch.setattr(
        _generator, 'create_empty_migration_file', lambda *a, **k: created.append(1)
    )

    command.handle('testapp', check_only=False)

    assert created == []


def test_handle_check_only_reports_missing_migrations_and_mti_warnings(monkeypatch):
    command = Command()
    command.stdout = StringIO()
    command.stderr = StringIO()
    command.existing.triggers.clear()
    command.existing.soft_deletes.clear()
    command.existing.soft_delete_related.clear()
    command.existing.mti_triggers.clear()
    command.existing.mti_soft_deletes.clear()
    command.trigger_function_dependency = ('testapp', '0001_pretend')
    command.parent_trigger_function_dependency = ('testapp', '0001_pretend_parent')
    monkeypatch.setattr(_generator, 'migration_with_digest_exists', lambda *a, **k: False)
    # Surfaced regardless of check_only -- seeded directly rather than relying on a real
    # MTI-cascade-limitation model, since that's covered at the unit level above.
    command._mti_cascade_warnings.append('some skipped MTI cascade rule')

    with pytest.raises(CommandError, match='Run `manage.py makeguitarmigrations`'):
        command.handle('testapp', check_only=True)

    assert 'Missing enforcement migrations' in command.stderr.getvalue()
    assert 'some skipped MTI cascade rule' in command.stderr.getvalue()


@override_settings(LOCAL_APPS=['fake.banda', 'fake.albumb'])
def test_handle_writes_scoped_cascade_gap_warning_to_stdout(monkeypatch):
    command = Command()
    command.stdout = StringIO()
    command.stderr = StringIO()
    command.existing.soft_delete_related.clear()
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
    monkeypatch.setattr(_generator, 'migration_with_digest_exists', lambda *a, **k: True)

    command.handle('albumb', check_only=False)

    assert "parent app 'banda' is not in this scoped run" in command.stdout.getvalue()


def test_function_dependencies_for_only_includes_deps_the_operations_use():
    """The per-app migration must depend on a function migration only when its operations
    actually call that function: own-table ``updated_at`` triggers need ``set_updated_at``;
    MTI parent-propagation triggers need ``set_parent_updated_at``. Soft-delete and cascade
    rules call no function, so an app emitting only those depends on neither -- avoiding a
    spurious edge to the (MTI) parent-function migration and its host app's ordering.
    """
    command = Command()
    command.trigger_function_dependency = ('testapp', '0002_trigger_function')
    command.parent_trigger_function_dependency = ('testapp', '0006_parent_trigger_function')

    own_only = '# Updated at Trigger on "testapp_band" table!\nmigrations.RunSQL(...)'
    mti_only = (
        '# MTI Updated at Trigger on "testapp_orchestra" table (parent "testapp_ensemble")!\n'
        'migrations.RunSQL(...)'
    )
    rules_only = (
        '# Soft Delete Rule on "testapp_band" table!\n'
        '# MTI Soft Delete Rule on "testapp_orchestra" table (parent "testapp_ensemble")!'
    )

    assert command._function_dependencies_for(own_only) == [('testapp', '0002_trigger_function')]
    assert command._function_dependencies_for(mti_only) == [
        ('testapp', '0006_parent_trigger_function')
    ]
    assert command._function_dependencies_for(own_only + '\n' + mti_only) == [
        ('testapp', '0002_trigger_function'),
        ('testapp', '0006_parent_trigger_function'),
    ]
    # Only soft-delete / cascade rules -> no function migration dependency at all.
    assert command._function_dependencies_for(rules_only) == []


# --- The singleton function migrations and the staged FORCE run ------------------
#
# These branches only fire on a project that has not generated them yet, or one part-way
# through a staged RLS retrofit. Neither state exists in this repo, so each is driven
# directly with the same throwaway-app pattern used above. They are worth driving rather
# than leaving uncovered: the trigger-function migration is a hard prerequisite of every
# other enforcement migration, and the FORCE stage is the one that makes an inert policy
# bind.


def _command_with_scaffold(monkeypatch, tmp_path, filename='0002_auto_enforcement.py'):
    """A command whose scaffolding writes into *tmp_path* instead of a real app."""
    app, _ = _write_empty_migration(tmp_path, filename)
    app.label = 'testapp'
    command = Command()
    command.stdout = StringIO()
    monkeypatch.setattr(command, '_get_trigger_function_host_app', lambda: app)
    monkeypatch.setattr(_generator, 'create_empty_migration_file', lambda *a, **k: filename)
    return command, app, filename


def test_ensure_trigger_function_migration_writes_and_records_the_dependency(
    monkeypatch, tmp_path
):
    """Every other enforcement migration depends on this one by name, so the recorded
    ``(app_label, stem)`` is what makes the dependency resolvable rather than a guess."""
    command, app, filename = _command_with_scaffold(monkeypatch, tmp_path)
    command.trigger_function_dependency = None

    assert command._ensure_trigger_function_migration() is True

    content = (tmp_path / 'migrations' / filename).read_text()
    assert 'CREATE_UPDATED_AT_TRIGGER_FUNCTION' in content
    assert command.trigger_function_dependency == ('testapp', '0002_auto_enforcement')
    # Second call is a no-op: the singleton is a singleton.
    assert command._ensure_trigger_function_migration() is False


def test_ensure_parent_trigger_function_migration_depends_on_the_base_one(monkeypatch, tmp_path):
    """Kept as a separate migration so adding MTI support never re-digests -- and therefore
    never regenerates -- the existing single-table function migration."""
    command, app, filename = _command_with_scaffold(
        monkeypatch, tmp_path, '0003_auto_enforcement_parent_trigger_function.py'
    )
    command.trigger_function_dependency = ('testapp', '0002_auto_enforcement_trigger_function')
    command.parent_trigger_function_dependency = None

    assert command._ensure_parent_trigger_function_migration() is True

    content = (tmp_path / 'migrations' / filename).read_text()
    assert 'CREATE_PARENT_UPDATED_AT_TRIGGER_FUNCTION' in content
    assert '0002_auto_enforcement_trigger_function' in content
    assert command._ensure_parent_trigger_function_migration() is False


def test_tenant_operations_force_stage_only_touches_policies_that_shipped_unforced():
    """The flag exists for one situation: policies shipped inert under
    ``GUITARS_RLS_FORCE = False``, then the setting was turned on.

    Keying on "a policy exists and has no separate FORCE operation" would match every policy
    generated with FORCE inline -- which is the default -- and emit a redundant migration per
    tenanted table.
    """
    app = django_apps.get_app_config('testapp')

    command = Command()
    command.stdout = StringIO()
    # As shipped: policies carry FORCE inline, so the stage has nothing to do.
    assert command._tenant_operations(app, force_rls=True) == []

    # Now pretend one of them shipped inert.
    command.existing.unforced_policies.add('testapp_release')
    operations = command._tenant_operations(app, force_rls=True)

    assert len(operations) == 1
    assert 'Tenant FORCE RLS on "testapp_release"' in operations[0]

    # And once its FORCE operation exists, it is done.
    command.existing.tenant_forces.add('testapp_release')
    assert command._tenant_operations(app, force_rls=True) == []


def test_force_rls_stage_writes_a_migration_for_an_inert_policy(monkeypatch):
    """The staged retrofit's second half, end to end through ``handle``."""
    written: list[dict] = []

    command = Command()
    command.stdout = StringIO()
    command.existing.unforced_policies.add('testapp_release')
    monkeypatch.setattr(_generator, 'migration_with_digest_exists', lambda *a, **k: False)
    monkeypatch.setattr(command, '_write_migration_file', lambda **k: written.append(k))
    monkeypatch.setattr(
        _generator, 'create_empty_migration_file', lambda app, name: f'0011_{name}.py'
    )

    command.handle(check_only=False, force_rls=True)

    assert len(written) == 1
    assert written[0]['migration_file'] == '0011_auto_tenant_force.py'
    assert 'Tenant FORCE RLS on "testapp_release"' in '\n'.join(written[0]['operations'])
    assert 'No changes detected' not in command.stdout.getvalue()


def test_force_rls_stage_check_only_reports_and_exits_non_zero(monkeypatch):
    """So a deploy pipeline can gate on "the FORCE stage still has work to do"."""
    command = Command()
    command.stdout = StringIO()
    command.stderr = StringIO()
    command.existing.unforced_policies.add('testapp_release')
    monkeypatch.setattr(_generator, 'migration_with_digest_exists', lambda *a, **k: False)

    with pytest.raises(CommandError, match='makeguitarmigrations'):
        command.handle(check_only=True, force_rls=True)

    assert 'testapp' in command.stderr.getvalue()


def test_check_reports_a_missing_trigger_function_migration():
    """``--check`` on a project that never generated the shared function migration.

    It is a hard prerequisite -- every per-app enforcement migration declares a dependency on
    it -- so ``--check`` has to fail rather than report the per-app migrations as fine.
    """
    command = Command()
    command.stdout = StringIO()
    command.trigger_function_dependency = None

    with pytest.raises(CommandError, match='trigger function migration'):
        command._ensure_trigger_function_migration(check_only=True)


def test_check_reports_a_missing_parent_trigger_function_migration():
    command = Command()
    command.stdout = StringIO()
    command.parent_trigger_function_dependency = None

    with pytest.raises(CommandError, match='MTI parent trigger function migration'):
        command._ensure_parent_trigger_function_migration(check_only=True)


def test_tenant_policy_operations_are_emitted_for_uncovered_tables():
    """The ordinary first run: a tenanted table with no policy operation yet gets one."""
    app = django_apps.get_app_config('testapp')

    command = Command()
    command.stdout = StringIO()
    command.existing.tenant_policies.clear()

    operations = command._tenant_operations(app, force_rls=False)
    headers = [line for op in operations for line in op.splitlines() if line.startswith('#')]

    assert '# Tenant RLS on "testapp_release" table!' in headers
    # One per policy-eligible table, and none for the multi-hop model.
    assert len(headers) == 6


def test_force_rls_stage_skips_an_operation_set_already_written(monkeypatch):
    """Digest dedupe applies to the FORCE stage too, or re-running it would stack migrations."""
    command = Command()
    command.stdout = StringIO()
    command.existing.unforced_policies.add('testapp_release')
    monkeypatch.setattr(_generator, 'migration_with_digest_exists', lambda *a, **k: True)

    command.handle(check_only=False, force_rls=True)

    assert 'No changes detected' in command.stdout.getvalue()
