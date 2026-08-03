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

from guitars import sql
from guitars.management import _generator
from guitars.management.commands import makeguitarmigrations as makeguitarmigrations_module
from guitars.management.commands.makeguitarmigrations import Command, unforced_policy_tables
from guitars.tenancy.discovery import app_coverage
from tests.testapp.models import Album, Band, Ensemble, Orchestra


def _pretend_function_migrations_are_current(command):
    """Mark both singleton trigger-function migrations as existing *and* up to date.

    Setting only the dependency is no longer enough to mean "already done": a singleton is
    skipped only when the migration that defines it also carries the digest of the SQL the
    kit emits today. That is the whole point of the digest -- before it, an edited function
    body shipped no migration at all, because the first thing the ensure methods did was
    return early on existence.
    """
    command.trigger_function_dependency = ('albumb', '0001_pretend')
    command.parent_trigger_function_dependency = ('albumb', '0001_pretend_parent')
    command.trigger_function_sql = makeguitarmigrations_module._sql_digest(
        sql.CREATE_UPDATED_AT_TRIGGER_FUNCTION, sql.DROP_UPDATED_AT_TRIGGER_FUNCTION
    )
    command.parent_trigger_function_sql = makeguitarmigrations_module._sql_digest(
        sql.CREATE_PARENT_UPDATED_AT_TRIGGER_FUNCTION,
        sql.DROP_PARENT_UPDATED_AT_TRIGGER_FUNCTION,
    )


def test_check_passes_when_enforcement_migrations_exist():
    out, err = StringIO(), StringIO()

    call_command('makeguitarmigrations', '--check', stdout=out, stderr=err)

    assert 'Missing or outdated enforcement migrations' not in err.getvalue()


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


def test_cascade_operations_disambiguates_two_fks_to_the_same_related_table():
    """Merch has two independent CASCADE FKs to Album -- ``album`` and ``bonus_album`` --
    which is the exact shape the ``_via`` naming scheme exists for: a PostgreSQL rule is
    namespaced by name alone, not by what it references, so without disambiguation the
    second FK's ``CREATE OR REPLACE RULE soft_delete_related_testapp_merch`` would silently
    replace the first FK's rule, leaving one of the two relations uncascaded with no error
    anywhere (see ``sql.CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE_VIA``'s comment).
    """
    command = Command()
    command.existing.soft_delete_related.clear()

    ops = command._cascade_operations(Album)
    merch_ops = [op for op in ops if 'testapp_merch' in op]

    assert len(merch_ops) == 2
    headers = [op.splitlines()[0] for op in merch_ops]
    assert any(
        '# Soft Delete Related Rule on "testapp_merch" that is related to "testapp_album"!' in h
        for h in headers
    )
    assert any('via "bonus_album_id"!' in h for h in headers)
    # Two distinct rule names -- neither op's CREATE OR REPLACE can silently clobber the
    # other's.
    blob = '\n'.join(merch_ops)
    assert 'RULE soft_delete_related_testapp_merch\n' in blob
    assert 'RULE soft_delete_related_testapp_merch_bonus_album_id' in blob


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


def test_write_migration_file_output_is_exact(tmp_path, snapshot):
    """Pin the generated file byte for byte.

    Substring assertions cannot catch the failure that actually matters here: an
    operation landing outside ``operations = [...]``. That produces a file which still
    contains every expected fragment and still fails at ``migrate`` time in someone
    else's project.

    Note the absence of any ``from guitars import sql``. Operations carry their SQL
    literally, so a generated migration imports nothing from the kit and cannot change
    meaning when the kit is upgraded.

    Snapshotted rather than a hand-written triple-quoted literal: the generator's own
    output is the source of truth, and a future refactor (M3) updates one snapshot file
    instead of re-typing whitespace here by hand.
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
    assert content == snapshot


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


def test_write_migration_file_raises_command_error_when_scaffold_has_no_dependencies_list(
    tmp_path,
):
    """A missing ``dependencies = [`` line must fail loudly, not as a bare StopIteration.

    Only reachable if makemigrations' own ``--empty`` template ever drops or reformats
    that line -- but if it does, the previous behaviour was an unhandled StopIteration
    deep inside the dependency-insertion loop rather than a diagnosable CommandError.
    """
    migrations_dir = tmp_path / 'migrations'
    migrations_dir.mkdir()
    filename = '0002_auto_enforcement.py'
    (migrations_dir / filename).write_text(
        '# Generated by Django 5.2 on 2026-01-01\n\n'
        'from django.db import migrations\n\n\n'
        'class Migration(migrations.Migration):\n\n'
        '    operations = [\n'
        '    ]\n'
    )
    app = types.SimpleNamespace(path=str(tmp_path))

    with pytest.raises(CommandError, match='dependencies = \\['):
        Command._write_migration_file(
            app=app,
            migration_file=filename,
            operations=[],
            operations_digest='digest123',
            dependencies=[('testapp', '0001_initial')],
        )


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

    assert 'Missing or outdated enforcement migrations' not in err.getvalue()


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
        command.existing.existing_digests.clear()
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


def test_handle_skips_an_in_scope_app_with_no_operations(monkeypatch):
    """An in-scope app that needs no enforcement contributes nothing to generate.

    Every real LOCAL_APP in this suite (just testapp) always has models needing
    enforcement, so nothing exercises this otherwise -- ``_build_operations`` is
    mocked directly rather than reaching for a fake app with no models, which
    ``test_scoped_cascade_gap_*`` already does for a different method.
    """
    created: list[str] = []

    command = Command()
    command.stdout = StringIO()
    command.existing.triggers.clear()
    command.existing.soft_deletes.clear()
    command.existing.soft_delete_related.clear()
    command.trigger_function_dependency = ('testapp', '0001_pretend')
    monkeypatch.setattr(command, '_build_operations', lambda app: [])
    monkeypatch.setattr(
        _generator,
        'create_empty_migration_file',
        lambda app, name='auto_enforcement': created.append(app.label) or f'0002_{name}.py',
    )

    command.handle(check_only=False)

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


def _sponsor_fk_reverse_relation():
    """Synthetic shape for the "generator would refuse this rule anyway" case: an FK on
    an MTI child's own table while its ``_deleted_at`` lives on an ancestor -- same as
    ``test_cascade_operation_warns_when_related_model_is_mti_child_without_own_deleted_at``.
    """

    class _FakeFKField:
        column = 'sponsor_id'
        model = Orchestra
        remote_field = types.SimpleNamespace(parent_link=False)

    return {(Orchestra, _FakeFKField(), CASCADE)}


@pytest.mark.parametrize(
    (
        'local_apps',
        'app_configs',
        'setup',
        'requested',
        'expected_note_substrings',
    ),
    [
        pytest.param(
            ['fake.banda', 'fake.albumb'],
            lambda: [
                _fake_app_config('fake.banda', 'banda', [Band]),
                _fake_app_config('fake.albumb', 'albumb', [Album]),
            ],
            None,
            {'albumb'},
            ['banda'],
            id='reported_when_parent_app_out_of_scope',
        ),
        pytest.param(
            ['fake.banda', 'fake.albumb'],
            lambda: [
                _fake_app_config('fake.banda', 'banda', [Band]),
                _fake_app_config('fake.albumb', 'albumb', [Album]),
            ],
            None,
            {'banda', 'albumb'},
            [],
            id='empty_when_both_apps_in_scope',
        ),
        pytest.param(
            ['fake.banda', 'fake.albumb'],
            lambda: [
                _fake_app_config('fake.banda', 'banda', [Band]),
                _fake_app_config('fake.albumb', 'albumb', [Album]),
            ],
            None,
            set(),
            [],
            id='empty_when_entirely_unscoped',
        ),
        pytest.param(
            ['fake.banda', 'fake.albumb', 'fake.otherc'],
            lambda: [
                _fake_app_config('fake.banda', 'banda', [Band]),
                _fake_app_config('fake.albumb', 'albumb', [Album]),
                _fake_app_config('fake.otherc', 'otherc', []),
            ],
            None,
            {'otherc'},
            [],
            id='silent_when_child_app_also_out_of_scope',
        ),
        pytest.param(
            ['fake.banda', 'fake.orchestrab'],
            lambda: [
                _fake_app_config('fake.banda', 'banda', [Band]),
                _fake_app_config('fake.orchestrab', 'orchestrab', [Orchestra]),
            ],
            lambda command: command.reverse_relations_mapping.__setitem__(
                Band, _sponsor_fk_reverse_relation()
            ),
            {'orchestrab'},
            [],
            id='silent_for_a_rule_the_generator_would_refuse',
        ),
        pytest.param(
            ['fake.ensemblea', 'fake.orchestrab'],
            lambda: [
                _fake_app_config('fake.ensemblea', 'ensemblea', [Ensemble]),
                _fake_app_config('fake.orchestrab', 'orchestrab', [Orchestra]),
            ],
            None,
            {'orchestrab'},
            [],
            id='skips_mti_parent_link',
        ),
        pytest.param(
            ['fake.banda', 'fake.albumb'],
            lambda: [
                _fake_app_config('fake.banda', 'banda', [Band]),
                _fake_app_config('fake.albumb', 'albumb', [Album]),
            ],
            lambda command: command.existing.soft_delete_related.__setitem__(
                (Album._meta.db_table, Band._meta.db_table, None), None
            ),
            {'albumb'},
            [],
            id='skipped_when_rule_already_exists',
        ),
    ],
)
def test_scoped_cascade_gap_notes(
    local_apps, app_configs, setup, requested, expected_note_substrings, monkeypatch
):
    with override_settings(LOCAL_APPS=local_apps):
        command = Command()
        command.existing.soft_delete_related.clear()
        if setup is not None:
            setup(command)
        monkeypatch.setattr(
            makeguitarmigrations_module.django_apps, 'get_app_configs', app_configs
        )

        notes = command._scoped_cascade_gap_notes(requested)

    assert len(notes) == len(expected_note_substrings)
    for note, substring in zip(notes, expected_note_substrings, strict=True):
        assert substring in note


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
    # The exact digest handle() will compute for this app's operations, given the state
    # above -- recorded ahead of time rather than faked, so _sql_digest (which the
    # trigger-function-migration check also goes through) is untouched.
    operations = command._build_operations(apps.get_app_config('testapp'))
    command.existing.existing_digests['testapp'] = {_generator.digest_of(operations)}
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
    command.existing.existing_digests.clear()
    # Surfaced regardless of check_only -- seeded directly rather than relying on a real
    # MTI-cascade-limitation model, since that's covered at the unit level above.
    command._mti_cascade_warnings.append('some skipped MTI cascade rule')

    with pytest.raises(CommandError, match='Run `manage.py makeguitarmigrations`'):
        command.handle('testapp', check_only=True)

    assert 'Missing or outdated enforcement migrations' in command.stderr.getvalue()
    assert 'some skipped MTI cascade rule' in command.stderr.getvalue()


@override_settings(LOCAL_APPS=['fake.banda', 'fake.albumb'])
def test_handle_writes_scoped_cascade_gap_warning_to_stdout(monkeypatch):
    command = Command()
    command.stdout = StringIO()
    command.stderr = StringIO()
    command.existing.soft_delete_related.clear()
    # Both singleton function migrations already exist and are current, so the per-app loop
    # is the only thing left to exercise.
    _pretend_function_migrations_are_current(command)

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
    # The exact digest handle() will compute for 'albumb', recorded ahead of time rather
    # than faked, so _sql_digest (which the trigger-function-migration check also goes
    # through) is untouched.
    operations = command._build_operations(fake_album_app)
    command.existing.existing_digests['albumb'] = {_generator.digest_of(operations)}

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


@pytest.mark.parametrize(
    (
        'filename',
        'dependency_attr',
        'method_name',
        'pre_setup',
        'function_signature',
        'extra_content_substring',
    ),
    [
        pytest.param(
            '0002_auto_enforcement.py',
            'trigger_function_dependency',
            '_ensure_trigger_function_migration',
            None,
            'CREATE FUNCTION set_updated_at()',
            None,
            id='base_trigger_function',
        ),
        pytest.param(
            '0003_auto_enforcement_parent_trigger_function.py',
            'parent_trigger_function_dependency',
            '_ensure_parent_trigger_function_migration',
            ('trigger_function_dependency', ('testapp', '0002_auto_enforcement_trigger_function')),
            'CREATE FUNCTION set_parent_updated_at()',
            '0002_auto_enforcement_trigger_function',
            id='parent_trigger_function_depends_on_the_base_one',
        ),
    ],
)
def test_ensure_trigger_function_migration_writes_and_records_the_dependency(
    monkeypatch,
    tmp_path,
    filename,
    dependency_attr,
    method_name,
    pre_setup,
    function_signature,
    extra_content_substring,
):
    """Every other enforcement migration depends on one of these two by name, so the
    recorded ``(app_label, stem)`` is what makes the dependency resolvable rather than a
    guess. The parent one is kept as its own migration so adding MTI support never
    re-digests -- and therefore never regenerates -- the base function migration."""
    command, app, filename = _command_with_scaffold(monkeypatch, tmp_path, filename)
    if pre_setup is not None:
        attr, value = pre_setup
        setattr(command, attr, value)
    setattr(command, dependency_attr, None)

    assert getattr(command, method_name)() is True

    content = (tmp_path / 'migrations' / filename).read_text()
    # The SQL itself, not a reference to the constant that holds it: a generated migration
    # that reads ``sql.CREATE_UPDATED_AT_TRIGGER_FUNCTION`` at migrate time means something
    # different on a later version of the kit than it did when it was written.
    assert function_signature in content
    assert 'from guitars import sql' not in content
    # A first definition, so the plain CREATE -- a collision on this unqualified public-schema
    # name must fail migrate rather than silently replace something that is not ours.
    assert 'CREATE OR REPLACE FUNCTION' not in content
    if extra_content_substring is not None:
        assert extra_content_substring in content
    assert getattr(command, dependency_attr) == ('testapp', filename.removesuffix('.py'))
    # Second call is a no-op: the singleton is a singleton, and it is now current.
    assert getattr(command, method_name)() is False


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
    command.existing.existing_digests.clear()
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
    command.existing.existing_digests.clear()

    with pytest.raises(CommandError, match='makeguitarmigrations'):
        command.handle(check_only=True, force_rls=True)

    assert 'testapp' in command.stderr.getvalue()


@pytest.mark.parametrize(
    ('dependency_attr', 'method_name', 'error_match'),
    [
        pytest.param(
            'trigger_function_dependency',
            '_ensure_trigger_function_migration',
            'trigger function migration',
            id='base_trigger_function',
        ),
        pytest.param(
            'parent_trigger_function_dependency',
            '_ensure_parent_trigger_function_migration',
            'MTI parent trigger function migration',
            id='parent_trigger_function',
        ),
    ],
)
def test_check_reports_a_missing_trigger_function_migration(
    dependency_attr, method_name, error_match
):
    """``--check`` on a project that never generated the shared function migration.

    It is a hard prerequisite -- every per-app enforcement migration declares a dependency on
    it -- so ``--check`` has to fail rather than report the per-app migrations as fine.
    """
    command = Command()
    command.stdout = StringIO()
    setattr(command, dependency_attr, None)

    with pytest.raises(CommandError, match=error_match):
        getattr(command, method_name)(check_only=True)


def test_tenant_policy_operations_are_emitted_for_uncovered_tables():
    """The ordinary first run: a tenanted table with no policy operation yet gets one."""
    app = django_apps.get_app_config('testapp')

    command = Command()
    command.stdout = StringIO()
    command.existing.tenant_policies.clear()
    command.existing.tenant_policy_identities.clear()

    operations = command._tenant_operations(app, force_rls=False)
    headers = [line for op in operations for line in op.splitlines() if line.startswith('#')]

    assert any(header.startswith('# Tenant RLS on "testapp_release" table!') for header in headers)
    # One per policy-eligible table, and none for the multi-hop model.
    assert len(headers) == 7
    # The CREATE form, not the replacement: there was no policy to replace.
    assert not any('replaced' in header for header in headers)


def test_a_policy_whose_shape_is_unchanged_emits_nothing():
    """The steady state. Re-running the generator on untouched models must be a no-op.

    Pinned as its own test because it is the precondition for the two below meaning anything:
    an identity that were unstable across runs would emit a replacement every time, and the
    drift detection would be indistinguishable from a bug.
    """
    app = django_apps.get_app_config('testapp')
    command = Command()
    command.stdout = StringIO()

    assert command._tenant_operations(app, force_rls=False) == []


def test_a_changed_coverage_shape_emits_a_replacement_not_nothing():
    """A model gaining a tenant dimension must produce a migration.

    The regression this guards is silent under-enforcement, and it is worth stating plainly:
    the header used to record only the table name, so a table that had *any* policy was
    treated as covered forever. Adding a dimension changed the predicate the models describe
    while the database kept the old, weaker one -- and because the generator emitted nothing,
    `makemigrations --check` reported nothing to do and CI stayed green.
    """
    app = django_apps.get_app_config('testapp')
    command = Command()
    command.stdout = StringIO()

    # Pretend the recorded policy was written for a different shape.
    command.existing.tenant_policy_identities['testapp_release'] = 'stale00000000'

    operations = command._tenant_operations(app, force_rls=False)
    headers = [line for op in operations for line in op.splitlines() if line.startswith('#')]

    assert len(headers) == 1
    assert headers[0].startswith('# Tenant RLS replaced on "testapp_release" table!')
    # The replacement form, because PostgreSQL has no CREATE OR REPLACE POLICY -- re-emitting
    # the CREATE form would fail migrate with "policy tenant_scope already exists". Asserted
    # on the emitted SQL rather than on a `sql.replace_table_rls(` call, because the operation
    # now carries the statements literally.
    assert 'DROP POLICY IF EXISTS tenant_scope ON testapp_release' in operations[0]
    assert operations[0].index('DROP POLICY IF EXISTS tenant_scope') < operations[0].index(
        'CREATE POLICY tenant_scope'
    )


def test_check_fails_when_a_policy_shape_changed(monkeypatch):
    """The gate, end to end -- this is what the whole identity mechanism is for.

    `makemigrations --check` used to exit 0 on a model that had gained a tenant dimension,
    because the generator emitted nothing and so there was nothing to report. CI stayed green
    while the database enforced a strictly weaker predicate than every call site implied.
    """
    app = django_apps.get_app_config('testapp')
    real = app_coverage(app)
    widened = real.tables['testapp_release']._replace(
        columns={**real.tables['testapp_release'].columns, 'region': 'region_id'}
    )
    monkeypatch.setattr(
        makeguitarmigrations_module,
        'app_coverage',
        lambda _app: type(real)(
            tables={**real.tables, 'testapp_release': widened}, notes=real.notes
        ),
    )

    command = Command()
    command.stdout, command.stderr = StringIO(), StringIO()

    with pytest.raises(CommandError, match='Run `manage.py makeguitarmigrations`'):
        command.handle(check_only=True)

    assert 'Missing or outdated enforcement migrations' in command.stderr.getvalue()
    assert 'Tenant RLS replaced on "testapp_release"' in command.stderr.getvalue()


def test_the_policy_identity_covers_the_predicate_and_the_exempt_roles():
    """What must change the identity, and what must not.

    ``force`` is excluded deliberately: it is an ALTER TABLE with its own staged mechanism
    (``--force-rls``), so folding it in would make flipping GUITARS_RLS_FORCE replace every
    policy and defeat the retrofit that setting exists for.
    """
    app = django_apps.get_app_config('testapp')
    command = Command()
    coverage = app_coverage(app).tables['testapp_release']
    baseline = command._policy_identity('testapp_release', coverage)

    # A second dimension on the same table -- the case that was silently missed.
    widened = coverage._replace(columns={**coverage.columns, 'region': 'region_id'})
    assert command._policy_identity('testapp_release', widened) != baseline

    # A renamed tenant column, same dimension name.
    renamed = coverage._replace(columns={dim: 'other_id' for dim in coverage.columns})
    assert command._policy_identity('testapp_release', renamed) != baseline

    with override_settings(GUITARS_RLS_EXEMPT_ROLES=['metabase_ro']):
        assert command._policy_identity('testapp_release', coverage) != baseline

    with override_settings(GUITARS_RLS_FORCE=False):
        assert command._policy_identity('testapp_release', coverage) == baseline


def test_force_rls_stage_skips_an_operation_set_already_written(monkeypatch):
    """Digest dedupe applies to the FORCE stage too, or re-running it would stack migrations."""
    command = Command()
    command.stdout = StringIO()
    command.existing.unforced_policies.add('testapp_release')
    # The exact digest the FORCE stage will compute for 'testapp', recorded ahead of time.
    force_operations = command._tenant_operations(apps.get_app_config('testapp'), force_rls=True)
    command.existing.existing_digests['testapp'] = {_generator.digest_of(force_operations)}

    command.handle(check_only=False, force_rls=True)

    assert 'No changes detected' in command.stdout.getvalue()


# --- Which policies shipped inert -----------------------------------------------


def _unforced_policy_tables(content: str) -> set[str]:
    """Find the ``_RE_TENANT_POLICY`` matches *content* needs, then defer to the real thing.

    ``unforced_policy_tables`` takes them as an argument rather than finding them itself --
    its caller (``Command._scan_existing_operations``) already scans for the same pattern
    and passes its own matches in, so a second scan here would be exactly the double work
    that changed.
    """
    matches = list(makeguitarmigrations_module._RE_TENANT_POLICY.finditer(content))
    return unforced_policy_tables(content, matches)


_TWO_POLICY_OPERATIONS = """
        # Tenant RLS on "table_a" table! [POLICY:aaaaaaaaaaaa]
        migrations.RunSQL(
            sql=sql.create_table_rls(table='table_a', columns={'l': 'l_id'}, force=True),
            reverse_sql=sql.drop_table_rls(table='table_a'),
        ),
        # Tenant RLS on "table_b" table! [POLICY:bbbbbbbbbbbb]
        migrations.RunSQL(
            sql=sql.create_table_rls(table='table_b', columns={'l': 'l_id'}, force=False),
            reverse_sql=sql.drop_table_rls(table='table_b'),
        ),
"""


def test_unforced_policy_tables_reads_each_operation_in_isolation():
    """Two adjacent operations, one forced and one not -- which is every real file.

    A regex spanning a few lines after the header cannot do this: operations are about that
    long, so a lazy match from the ``force=True`` operation reaches into the next one, claims
    *its* ``force=False``, and consumes the header on the way. The result is exactly backwards
    -- the already-forced table gets flagged and the inert one is missed -- so `--force-rls`
    would force what needs nothing and leave the unprotected table unprotected.
    """
    assert _unforced_policy_tables(_TWO_POLICY_OPERATIONS) == {'table_b'}


def test_unforced_policy_tables_is_empty_when_everything_shipped_forced():
    forced_only = _TWO_POLICY_OPERATIONS.replace('force=False', 'force=True')

    assert _unforced_policy_tables(forced_only) == set()


def test_unforced_policy_tables_handles_the_last_operation_in_a_file():
    """The final operation has no following header to bound it, so it is bounded by EOF."""
    only_unforced = _TWO_POLICY_OPERATIONS.replace('force=True', 'force=False')

    assert _unforced_policy_tables(only_unforced) == {'table_a', 'table_b'}


def test_unforced_policy_tables_bounds_a_replacement_operation_too():
    """A replacement operation is a policy operation, so it must bound the one before it.

    ``--force-rls`` reads ``force=`` out of each operation's own text. If the replacement
    header were not one of the bounds, a preceding ``force=True`` operation would run on into
    the replacement, read *its* ``force=False``, and flag the wrong table -- the same
    off-by-one-operation bug the isolation test above pins for the CREATE form.
    """
    with_replacement = _TWO_POLICY_OPERATIONS.replace(
        '# Tenant RLS on "table_b"', '# Tenant RLS replaced on "table_b"'
    )

    assert _unforced_policy_tables(with_replacement) == {'table_b'}


def test_unforced_policy_tables_is_last_write_wins_within_one_file():
    """Two operations for the SAME table: the later one is the state the database ends in.

    Accumulating into a set instead answers "was this table ever shipped inert", which is a
    different and useless question -- a table replaced with ``force=True`` would stay on the
    FORCE backlog forever and ``--force-rls`` would keep writing a migration that changes
    nothing. Across files the caller already applies this rule; within one it has to hold
    here, because the caller cannot see the operation boundaries.
    """
    inert_then_forced = (
        _TWO_POLICY_OPERATIONS.replace('table_b', 'table_a')
        + """
        # Tenant RLS replaced on "table_a" table! [POLICY:cccccccccccc]
        migrations.RunSQL(
            sql=sql.replace_table_rls(table='table_a', columns={'l': 'l_id'}, force=True),
            reverse_sql=sql.drop_table_rls(table='table_a'),
        ),
"""
    )

    assert _unforced_policy_tables(inert_then_forced) == set()


def test_a_replacement_carrying_force_takes_a_table_off_the_backlog(tmp_path, monkeypatch):
    """The scan is last-write-wins per table, not a union across every migration file.

    The shape that matters is a finished retrofit: ``table_b``'s policy shipped inert under
    ``GUITARS_RLS_FORCE = False``, then the model changed and the replacement was generated
    with the setting back on. That replacement inlines FORCE, so it writes no
    ``# Tenant FORCE RLS`` header for ``tenant_forces`` to find -- and if the scan merely
    unioned every ``force=False`` it ever saw, ``table_b`` would stay on the backlog forever
    and ``--force-rls`` would keep emitting a migration for a table that is already forced.
    That is the same redundant-migration bug the flag was fixed for once already, one file
    further along.
    """
    migrations_dir = tmp_path / 'migrations'
    migrations_dir.mkdir()
    # Both tables ship inert first, so the assertion below distinguishes "came off the
    # backlog" from "the scan found nothing at all".
    (migrations_dir / '0001_initial.py').write_text(
        _TWO_POLICY_OPERATIONS.replace('force=True', 'force=False')
    )
    (migrations_dir / '0002_replacement.py').write_text(
        '# Tenant RLS replaced on "table_b" table! [POLICY:cccccccccccc]\n'
        "sql.replace_table_rls(table='table_b', columns={'l': 'l_id'}, force=True),\n"
    )

    app = django_apps.get_app_config('testapp')
    monkeypatch.setattr(app, 'path', str(tmp_path))

    command = Command()

    # Both are recorded as policied; only the one whose latest operation is still inert
    # remains something --force-rls has to act on.
    assert {'table_a', 'table_b'} <= command.existing.tenant_policies
    assert command.existing.unforced_policies == {'table_a'}


def test_the_real_migrations_ship_every_policy_forced():
    """GUITARS_RLS_FORCE defaults to True, so this repo's own migrations carry FORCE inline.

    Pinned because it is the precondition for `--force-rls` correctly finding nothing to do.
    """
    command = Command()

    assert command.existing.tenant_policies
    assert command.existing.unforced_policies == set()
