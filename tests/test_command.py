"""Tests for the makeguitarmigrations management command. ``_create_empty_migration_file``
is exercised via the test app's committed migrations instead, since running it for real
would scaffold a new file on disk; here it's scanning, idempotency, and SQL-building."""

import types
from io import StringIO

import pytest
from django.apps import apps
from django.apps import apps as django_apps
from django.conf import settings as django_settings
from django.core.management import CommandError, call_command
from django.db import models
from django.db.models import CASCADE, DO_NOTHING, SET_NULL
from django.test import override_settings
from django.test.utils import isolate_apps

from guitars import sql
from guitars.models import OwningForeignKey, SetarModel
from guitars.management import _generator
from guitars.management.enforcement import command as command_module
from guitars.management.enforcement import headers as headers_module
from guitars.management.enforcement import identity as identity_module
from guitars.management.enforcement import operations as operations_module
from guitars.management.enforcement.command import Command
from guitars.sql import _identifiers
from guitars.tenancy.discovery import app_coverage, autofill_function_name
from tests.testapp.models import Album, Band, Ensemble, Foyer, Kiosk, Merch, Orchestra


def _pretend_function_migrations_are_current(command):
    """Mark both singleton trigger-function migrations as existing *and* up to date --
    setting only the dependency isn't enough: a singleton is skipped only when its
    migration also carries today's SQL digest, not merely on existence."""
    command.trigger_function_dependency = ('albumb', '0001_pretend')
    command.parent_trigger_function_dependency = ('albumb', '0001_pretend_parent')
    command.trigger_function_sql = identity_module._sql_digest(
        sql.CREATE_UPDATED_AT_TRIGGER_FUNCTION, sql.DROP_UPDATED_AT_TRIGGER_FUNCTION
    )
    command.parent_trigger_function_sql = identity_module._sql_digest(
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


def test_schema_qualified_table_headers_round_trip_and_stay_idempotent():
    """Regression: a table with a literal ``"`` (Django's pre-quoted form) used to embed
    it raw in the header, so the scanner's own capture group never matched it back."""
    with override_settings(
        INSTALLED_APPS=[*django_settings.INSTALLED_APPS, 'tests.schema_qualified']
    ):
        from tests.schema_qualified.models import Event  # noqa: F401 -- registers the app's model

        app = apps.get_app_config('schema_qualified')
        command = Command()
        command.existing.triggers.clear()
        command.existing.soft_deletes.clear()
        command.existing.tenant_autofill.clear()

        first_run = command._build_operations(app)
        blob = '\n'.join(first_run)
        assert blob, (
            'Event (GuitarModel, db_table="\\"analytics\\".\\"events\\"") should '
            'produce at least a trigger, a soft-delete rule and a tenant policy'
        )

        trigger_match = headers_module._RE_UPDATED_AT.search(blob)
        assert trigger_match, f'schema-qualified header not matched by its own scanner: {blob!r}'
        # The captured, still-escaped text must undo back to the real, unescaped db_table --
        # the same value a later run recomputes fresh as its dict key.
        assert _identifiers._unescape_ident(trigger_match.group(1)) == '"analytics"."events"'

        # Same round trip for the autofill trigger, whose scanner is hand-written and has to
        # walk past the escaped quotes to reach "(function ..." rather than stopping at the
        # first quote it meets -- a schema-qualified name is where that goes wrong.
        autofill_match = headers_module._RE_TENANT_AUTOFILL.search(blob)
        assert autofill_match, f'schema-qualified autofill header not matched: {blob!r}'
        assert (
            _identifiers._unescape_ident(
                autofill_match.group(headers_module.RE_TENANT_AUTOFILL_TABLE)
            )
            == '"analytics"."events"'
        )
        assert autofill_match.group(headers_module.RE_TENANT_AUTOFILL_FUNCTION), (
            'autofill header carried no readable function name'
        )

        # Simulate a second run reading this run's own output back, the same way
        # scanning.py really does: extract every header, unescape its captured table name,
        # and record its [SQL:...] digest.
        for match in headers_module._RE_UPDATED_AT.finditer(blob):
            key = _identifiers._unescape_ident(match.group(1))
            command.existing.triggers[key] = identity_module._recorded_sql_identity(blob, match)
        for match in headers_module._RE_SOFT_DELETE.finditer(blob):
            key = _identifiers._unescape_ident(match.group(1))
            command.existing.soft_deletes[key] = identity_module._recorded_sql_identity(
                blob, match
            )
        for match in headers_module._RE_TENANT_AUTOFILL.finditer(blob):
            key = (
                _identifiers._unescape_ident(match.group(headers_module.RE_TENANT_AUTOFILL_TABLE)),
                _identifiers._unescape_ident(
                    match.group(headers_module.RE_TENANT_AUTOFILL_FUNCTION)
                ),
            )
            command.existing.tenant_autofill[key] = identity_module._recorded_sql_identity(
                blob, match
            )
        policy_matches = list(headers_module._RE_TENANT_POLICY.finditer(blob))
        for match in policy_matches:
            table = _identifiers._unescape_ident(match.group(1))
            command.existing.tenant_policies.add(table)
            command.existing.tenant_policy_identities[table] = (
                identity_module._recorded_policy_identity(blob, match)
            )
            command.existing.tenant_policy_sql[table] = identity_module._recorded_sql_identity(
                blob, match
            )

        second_run = command._build_operations(app)
        assert second_run == [], (
            f'a table already covered was re-emitted as a duplicate operation: {second_run!r}'
        )


def test_cascade_operations_skip_non_cascade_and_non_deletable_relations():
    """Band is the FK target of: Album.band (CASCADE, generates a rule), Album.producer
    (SET_NULL -- skipped, wrong on_delete) and Riff.band (CASCADE, but Riff has no
    _deleted_at -- skipped, nothing to cascade to)."""
    command = Command()
    command.existing.soft_delete_related.clear()

    ops = '\n'.join(command._cascade_operations(Band))

    assert 'testapp_album" that is related to "testapp_band"' in ops  # Album.band survives
    assert 'testapp_riff' not in ops  # Riff has no _deleted_at to cascade into


def test_related_rule_name_does_not_reject_a_hostile_unqualified_table():
    """Regression: a legal-but-hostile, unqualified ``db_table`` like ``'Order Items'``
    (mixed case, embedded space) must render safely quoted, not raise at build time."""
    name = operations_module._related_rule_name('Order Items')
    assert name == '"soft_delete_related_Order Items"'


def test_related_rule_name_folds_in_a_hostile_schema_qualified_table():
    name = operations_module._related_rule_name('analytics.Weird Table')
    assert name == '"soft_delete_related_9_analytics_Weird Table"'


def test_related_rule_name_does_not_collide_across_the_schema_table_boundary():
    """Regression: a plain ``f'{schema}_{table}'`` join would fold ``('tenant_a', 'events')``
    and ``('tenant', 'a_events')`` to the same name, colliding two cascade rules."""
    first = operations_module._related_rule_name('tenant_a.events')
    second = operations_module._related_rule_name('tenant.a_events')
    assert first != second


def test_cascade_operations_disambiguates_two_fks_to_the_same_related_table():
    """Merch has two CASCADE FKs to Album -- the exact shape ``_via`` naming exists for:
    without disambiguation, the second FK's rule would silently replace the first's."""
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
    assert 'RULE "soft_delete_related_testapp_merch"\n' in blob
    assert 'RULE "soft_delete_related_testapp_merch_bonus_album_id"' in blob


def test_cascade_operation_warns_when_related_model_is_mti_child_without_own_deleted_at(
    monkeypatch,
):
    """Cascading into an MTI child whose ``_deleted_at`` lives on a farther ancestor isn't
    supported (needs a join form) -- must warn, not emit a broken rule. Synthetic
    reverse-relation, not a new schema field: purely about the command's own logic."""
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


def test_owned_rule_name_folds_a_hostile_schema_qualified_table_like_its_cascade_twin():
    """Same length-prefixed folding as ``_related_rule_name``'s stem, under its own prefix -- a
    rule is namespaced by name alone, so the two families must not be able to meet. The FK is
    length-prefixed as well, which the frozen cascade spelling cannot be."""
    assert (
        operations_module._owned_rule_name('analytics.Weird Table', 'kit_id')
        == '"soft_delete_owned_9_analytics_11_Weird Table_6_kit_id"'
    )
    assert operations_module._owned_rule_name(
        'tenant_a.events', 'kit_id'
    ) != operations_module._owned_rule_name('tenant.a_events', 'kit_id')


@pytest.mark.parametrize(
    ('first', 'second'),
    [
        # The adjacent split: one string, two ways to cut it into (table, column).
        (('shop_press_kit', 'kit_id'), ('shop_press', 'kit_kit_id')),
        # And the one a length prefix at *one* end still admits, which is why both are sized:
        # `soft_delete_owned_a_5_b_1_c` would have been either of these.
        (('a_5_b', 'c'), ('a', 'b_1_c')),
        # A sized schema does not save an unsized table from the same trick.
        (('s.a_5_b', 'c'), ('s.a', 'b_1_c')),
    ],
)
def test_owned_rule_names_cannot_be_split_two_ways(first, second):
    """Every variable segment carries its length, so reading left to right leaves no boundary
    to guess at -- no two ``(schema, table, column)`` triples reach one name. The claim has to
    hold outright: `_claim_rule_name` reports a clash but nothing stops the rule shipping."""
    assert operations_module._owned_rule_name(*first) != operations_module._owned_rule_name(
        *second
    )


def test_owned_operations_emit_only_for_owning_foreign_keys():
    """Album declares two ``OwningForeignKey``s to PressKit and two plain FKs to Band. Only
    the owning pair gets a rule -- ``on_delete`` never decides this."""
    command = Command()
    command.existing.soft_delete_owned.clear()

    ops = command._owned_operations(Album)
    blob = '\n'.join(ops)

    assert len(ops) == 2
    assert 'that is owned by "testapp_album"' in blob
    assert 'testapp_presskit' in blob
    # `band` (CASCADE) and `producer` (SET_NULL) are plain ForeignKeys: no ownership either way.
    assert 'testapp_band' not in blob


def test_owned_operations_name_one_rule_per_foreign_key_column():
    """Album's two owned FKs point at the same table, so the FK column is the only thing
    keeping their rule names apart -- a collision would silently replace, not fail."""
    command = Command()
    command.existing.soft_delete_owned.clear()

    blob = '\n'.join(command._owned_operations(Album))

    assert 'RULE "soft_delete_owned_16_testapp_presskit_12_press_kit_id"' in blob
    assert 'RULE "soft_delete_owned_16_testapp_presskit_16_alt_press_kit_id"' in blob
    assert 'via "press_kit_id"!' in blob
    assert 'via "alt_press_kit_id"!' in blob


def test_owned_rule_carries_the_last_owner_guard():
    """Unconditional, not derived from a UniqueConstraint, which would go silently wrong the
    day one was dropped. The self-exclusion is load-bearing, not tidiness: a rule action runs
    before the original update, so without it the NOT EXISTS never holds and nothing fires."""
    command = Command()
    command.existing.soft_delete_owned.clear()

    blob = '\n'.join(command._owned_operations(Album))

    assert 'NOT EXISTS' in blob
    assert 'guitars_owner."press_kit_id" = old."press_kit_id"' in blob
    assert 'guitars_owner."id" <> old."id"' in blob
    assert 'guitars_owner._deleted_at IS NULL' in blob


def test_owned_operation_correlates_an_mti_dependent_against_its_owner_table():
    """``Merch.featured_orchestra`` points at an MTI child whose ``_deleted_at`` lives on
    Ensemble. A chain shares one primary-key value, which is what the FK holds, so the
    ancestor's table is both correct and the only one carrying a column to stamp."""
    command = Command()
    command.existing.soft_delete_owned.clear()

    blob = '\n'.join(command._owned_operations(Merch))

    assert 'that is owned by "testapp_merch" via "featured_orchestra_id"!' in blob
    assert 'UPDATE "testapp_ensemble"' in blob
    assert 'UPDATE "testapp_orchestra"' not in blob


def test_owned_operation_warns_when_the_owner_inherits_deleted_at_from_an_ancestor():
    """``Orchestra.programme`` sits on the child's own table while the rule must fire on
    Ensemble, where ``old."programme_id"`` names nothing. Warn, emit nothing."""
    command = Command()
    command._mti_cascade_warnings.clear()
    command.existing.soft_delete_owned.clear()

    ops = command._owned_operations(Orchestra)

    assert ops == []
    assert len(command._mti_cascade_warnings) == 1
    warning = command._mti_cascade_warnings[0]
    assert 'testapp_orchestra.programme_id' in warning
    assert 'multi-table-inheritance' in warning


def test_owned_operation_warns_when_the_owner_owns_its_own_table():
    """A rule whose action updates the table it fires on is rewritten into itself, and
    PostgreSQL then rejects *every* UPDATE on that table -- a plain ``save()`` included --
    with "infinite recursion detected in rules for relation". Warn, emit nothing."""
    command = Command()
    command._mti_cascade_warnings.clear()
    command.existing.soft_delete_owned.clear()

    @isolate_apps('tests.testapp')
    def _build():
        class SelfOwner(SetarModel):
            previous = OwningForeignKey('self', on_delete=SET_NULL, null=True)

            class Meta:
                app_label = 'testapp'

        return command._owned_operations(SelfOwner)

    assert _build() == []
    assert len(command._mti_cascade_warnings) == 1
    assert 'infinite rule recursion' in command._mti_cascade_warnings[0]


def test_owned_operation_warns_when_the_target_is_not_soft_deletable():
    """An ``OwningForeignKey`` has no purpose other than the rule, so a target with no
    ``_deleted_at`` to stamp is a misconfiguration, not a relation to pass over quietly."""
    command = Command()
    command._mti_cascade_warnings.clear()
    command.existing.soft_delete_owned.clear()

    @isolate_apps('tests.testapp')
    def _build():
        class Plain(models.Model):
            class Meta:
                app_label = 'testapp'

        class Owner(SetarModel):
            plain = OwningForeignKey(Plain, on_delete=SET_NULL, null=True)

            class Meta:
                app_label = 'testapp'

        return command._owned_operations(Owner)

    assert _build() == []
    assert len(command._mti_cascade_warnings) == 1
    assert 'no _deleted_at column' in command._mti_cascade_warnings[0]


def test_owned_operation_warns_when_the_owner_is_not_soft_deletable():
    """The rule fires on the owner's own ``_deleted_at`` transition, so an owner that has no
    such column can never fire it. Silence here is the failure mode ADR 0011 chose a
    checkable field subclass to avoid: a declaration that quietly generates nothing."""
    command = Command()
    command._mti_cascade_warnings.clear()
    command.existing.soft_delete_owned.clear()

    @isolate_apps('tests.testapp')
    def _build():
        class Kit(SetarModel):
            class Meta:
                app_label = 'testapp'

        class PlainOwner(models.Model):
            kit = OwningForeignKey(Kit, on_delete=SET_NULL, null=True)

            class Meta:
                app_label = 'testapp'

        return command._owned_operations(PlainOwner)

    assert _build() == []
    assert len(command._mti_cascade_warnings) == 1
    assert 'has no _deleted_at column' in command._mti_cascade_warnings[0]
    assert 'never soft-deleted' in command._mti_cascade_warnings[0]


def test_owned_operation_warns_when_the_key_is_redirected_off_the_primary_key():
    """``guitars.E002``'s twin, as the cycle refusal is ``E001``'s: ``--skip-checks`` reaches
    the generator, and the rule would correlate the key against a primary key it never held --
    stamping whichever row happens to carry that value as its pk."""
    command = Command()
    command._mti_cascade_warnings.clear()
    command.existing.soft_delete_owned.clear()

    @isolate_apps('tests.testapp')
    def _build():
        class Kit(SetarModel):
            legacy_id = models.IntegerField(unique=True)

            class Meta:
                app_label = 'testapp'

        class Owner(SetarModel):
            kit = OwningForeignKey(
                Kit, on_delete=DO_NOTHING, to_field='legacy_id', null=True, blank=True
            )

            class Meta:
                app_label = 'testapp'

        return command._owned_operations(Owner)

    assert _build() == []
    assert len(command._mti_cascade_warnings) == 1
    assert "to_field='legacy_id'" in command._mti_cascade_warnings[0]
    assert 'would stamp the wrong row' in command._mti_cascade_warnings[0]


def test_owned_operations_are_a_no_op_for_a_model_declaring_none():
    """Called for every model now, not only soft-deletable ones, so the common case has to
    cost nothing and warn about nothing."""
    command = Command()
    command._mti_cascade_warnings.clear()

    assert command._owned_operations(Band) == []
    assert command._mti_cascade_warnings == []


def test_owned_operations_are_idempotent_across_two_runs():
    """A run reading its own output back must emit nothing -- the header, its ``[SQL:...]``
    identity and the dedupe key all have to agree on the same three-part key."""
    command = Command()
    command.existing.soft_delete_owned.clear()

    blob = '\n'.join(command._owned_operations(Album))

    for match in headers_module._RE_SOFT_DELETE_OWNED.finditer(blob):
        key = (
            _identifiers._unescape_ident(match.group(1)),
            _identifiers._unescape_ident(match.group(2)),
            _identifiers._unescape_ident(match.group(3)),
        )
        command.existing.soft_delete_owned[key] = identity_module._recorded_sql_identity(
            blob, match
        )

    assert command._owned_operations(Album) == []


def test_owned_operations_under_adopt_stay_a_plain_create_or_replace():
    """Rules carry no adopt form on purpose -- ``CREATE OR REPLACE RULE`` is already correct
    whether or not the object exists, and a ``DROP ... IF EXISTS`` first would open an
    instant where a DELETE on that table destroys rows."""
    command = Command()
    command.existing.soft_delete_owned.clear()

    blob = '\n'.join(command._owned_operations(Album, adopt=True))

    assert 'CREATE OR REPLACE RULE "soft_delete_owned_16_testapp_presskit_12_press_kit_id"' in blob
    assert 'DROP RULE IF EXISTS' not in blob


def test_cascade_operation_refuses_a_self_referential_cascade_foreign_key():
    """A tree (`parent = ForeignKey('self', CASCADE)`). The rule would read ON UPDATE TO t
    DO ALSO UPDATE t, which PostgreSQL rejects at rewrite time -- bricking *every* UPDATE on
    the table, a plain ``save()`` included, with `migrate` having reported success."""
    command = Command()
    command._mti_cascade_warnings.clear()
    command.existing.soft_delete_related.clear()

    class _SelfReferentialFKField:
        column = 'parent_id'
        model = Band
        remote_field = types.SimpleNamespace(parent_link=False)

    command.reverse_relations_mapping[Band] = {(Band, _SelfReferentialFKField(), CASCADE)}

    ops = command._cascade_operations(Band)

    assert ops == []
    assert len(command._mti_cascade_warnings) == 1
    assert 'infinite rule recursion' in command._mti_cascade_warnings[0]


def test_constructing_the_command_does_not_touch_the_filesystem(monkeypatch):
    """Django constructs a Command() for --help and the command registry -- neither needs
    a filesystem scan of every local app's migrations, so __init__ must not trigger one."""

    def _fail(*args, **kwargs):
        raise AssertionError('Command() must not scan migration files eagerly')

    monkeypatch.setattr(_generator, 'iter_migration_files', _fail)

    Command()  # must not raise


def test_iter_migration_files_empty_when_no_migrations_dir(tmp_path):
    app = types.SimpleNamespace(path=str(tmp_path))

    assert list(_generator.iter_migration_files(app)) == []


# Must match Django's real --empty output: operations = [ and ] on SEPARATE lines, since
# operations insert before the file's last line -- a single-line scaffold would place
# them OUTSIDE the list, a broken migration a substring assertion still passes.
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
    """Pin the generated file byte for byte -- a substring assertion can't catch an
    operation landing outside ``operations = [...]``, which still contains every expected
    fragment and still fails at ``migrate`` time. No ``from guitars import sql`` import."""
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
    """The structural guarantee, independent of exact formatting -- parsed, not
    pattern-matched, so an operation landing outside the list makes this fail."""
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
    """A missing ``dependencies = [`` line must fail loudly, not as a bare StopIteration
    deep inside the dependency-insertion loop."""
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
    """An in-scope app that needs no enforcement contributes nothing to generate --
    ``_build_operations`` mocked directly since every real LOCAL_APP here needs some."""
    created: list[str] = []

    command = Command()
    command.stdout = StringIO()
    command.existing.triggers.clear()
    command.existing.soft_deletes.clear()
    command.existing.soft_delete_related.clear()
    command.trigger_function_dependency = ('testapp', '0001_pretend')
    monkeypatch.setattr(command, '_build_operations', lambda app, **kwargs: [])
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
    """Synthetic shape: an FK on an MTI child's own table while ``_deleted_at`` lives on
    an ancestor -- the generator-refuses-this-rule case."""

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
        monkeypatch.setattr(command_module.django_apps, 'get_app_configs', app_configs)

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


def test_check_reports_both_function_and_app_level_gaps_in_one_run():
    """Function-migration staleness used to raise immediately, before the per-app loop ever
    ran -- so a project with both problems only ever heard about whichever one this method
    reached first. Both must be visible in the same ``--check`` run."""
    command = Command()
    command.stdout = StringIO()
    command.stderr = StringIO()
    command.existing.triggers.clear()
    command.existing.soft_deletes.clear()
    command.existing.soft_delete_related.clear()
    # Overridden after touching .existing above, which is what populates these from the
    # real scan -- setting them first would just be clobbered by that scan.
    command.trigger_function_dependency = None
    command.trigger_function_sql = None

    with pytest.raises(CommandError, match='Run `manage.py makeguitarmigrations`'):
        command.handle('testapp', check_only=True)

    stderr = command.stderr.getvalue()
    assert 'Run `manage.py makeguitarmigrations` to create the trigger function migration' in (
        stderr
    )
    assert 'Missing or outdated enforcement migrations' in stderr


def test_check_reports_a_missing_parent_trigger_function_migration_alongside_app_gaps():
    """The MTI parent function migration goes through its own try/except in handle() --
    covered independently of the base function migration's, since the two are separate
    call sites collecting into the same function_check_messages list."""
    command = Command()
    command.stdout = StringIO()
    command.stderr = StringIO()
    command.existing.triggers.clear()
    command.existing.soft_deletes.clear()
    command.existing.soft_delete_related.clear()
    command.existing.mti_triggers.clear()
    command.existing.mti_soft_deletes.clear()
    # The base function migration is current, so only the parent one is missing.
    _pretend_function_migrations_are_current(command)
    command.parent_trigger_function_dependency = None
    command.parent_trigger_function_sql = None

    with pytest.raises(CommandError, match='Run `manage.py makeguitarmigrations`'):
        command.handle('testapp', check_only=True)

    stderr = command.stderr.getvalue()
    assert 'MTI parent trigger function migration' in stderr


def test_check_reports_a_missing_autofill_function_alongside_the_other_gaps():
    """Collected into the same report rather than raising straight out, so a project missing
    both a function migration and per-app operations hears about both in one run instead of
    fixing one, re-running, and discovering the next."""
    command = Command()
    command.stdout = StringIO()
    command.stderr = StringIO()
    # Force the lazy scan before overriding: `handle` touches `self.existing`, which
    # repopulates these from disk and would undo the setup below.
    _ = command.existing
    _pretend_function_migrations_are_current(command)
    # Everything else current; only the autofill function migration is unrecorded.
    command.tenant_autofill_dependencies = {}
    command.tenant_autofill_sql = {}

    with pytest.raises(CommandError, match='Run `manage.py makeguitarmigrations`'):
        command.handle('testapp', check_only=True)

    assert 'tenant autofill function migration' in command.stderr.getvalue()


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
        command_module.django_apps,
        'get_app_configs',
        lambda: [fake_band_app, fake_album_app],
    )
    monkeypatch.setattr(
        command_module.django_apps,
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
    """A per-app migration depends on a function migration only when its operations
    actually call it -- soft-delete/cascade rules call none, so those apps depend on neither."""
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


# --- The singleton function migrations and the staged FORCE run ---
# Neither state exists in this repo (no project pre-function-migration, none mid-retrofit),
# so each is driven directly with the throwaway-app pattern above.


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
    recorded ``(app_label, stem)`` must be real, not guessed."""
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


def test_ensure_tenant_autofill_function_migration_writes_and_records_the_dependency(
    monkeypatch, tmp_path
):
    """Keyed by function name rather than a singleton, since autofill is one function per
    ``(column, GUC)`` pair -- so the recorded dependency has to be per function too, or a
    second pair would silently reuse the first's migration."""
    command, app, filename = _command_with_scaffold(
        monkeypatch, tmp_path, '0001_auto_enforcement_guitars_fill_5_label_label_id.py'
    )
    function = autofill_function_name('label', 'label_id')

    assert command._ensure_tenant_autofill_function_migration(function, 'label', 'label_id')

    content = (tmp_path / 'migrations' / filename).read_text()
    assert f'CREATE FUNCTION "{function}"()' in content
    # The GUC and column are baked in, not read from a constant at migrate time (ADR 0006).
    assert "current_setting('tenant.label', true)" in content
    assert 'NEW."label_id"' in content
    assert 'from guitars import sql' not in content
    assert 'CREATE OR REPLACE FUNCTION' not in content
    assert command.tenant_autofill_dependencies[function] == (
        'testapp',
        filename.removesuffix('.py'),
    )
    # Second call is a no-op: this function is now recorded and current.
    assert not command._ensure_tenant_autofill_function_migration(function, 'label', 'label_id')


def test_a_changed_tenant_field_renames_the_function_and_regenerates(monkeypatch, tmp_path):
    """ADR 0005 says to confirm rather than assume this. A different ``GUITARS_TENANT_FIELD``
    means a different column, which renames the function, which is a different header -- so
    the header scan reads it as a new operation instead of covered-forever."""
    command, app, filename = _command_with_scaffold(monkeypatch, tmp_path, '0001_auto.py')
    first = autofill_function_name('label', 'label_id')
    second = autofill_function_name('org', 'org_id')

    assert first != second
    assert command._ensure_tenant_autofill_function_migration(first, 'label', 'label_id')
    # A recorded first function must not satisfy the second: different name, different header.
    assert command._ensure_tenant_autofill_function_migration(second, 'org', 'org_id')
    assert set(command.tenant_autofill_dependencies) == {first, second}


def test_a_missing_autofill_function_migration_is_reported_under_check(monkeypatch, tmp_path):
    """``--check`` has to name this gap rather than raising past the per-app report, or a
    project with both a missing function and a missing trigger only hears about one."""
    command, app, _ = _command_with_scaffold(monkeypatch, tmp_path, '0001_auto.py')

    with pytest.raises(CommandError, match='tenant autofill function migration'):
        command._ensure_tenant_autofill_function_migration(
            autofill_function_name('label', 'label_id'), 'label', 'label_id', check_only=True
        )


def test_function_dependencies_for_keys_autofill_on_the_function_the_trigger_names():
    """An app depends on the autofill functions its own triggers call and no others, which is
    why the trigger header carries the function name at all."""
    command = Command()
    command.trigger_function_dependency = None
    command.parent_trigger_function_dependency = None
    command.tenant_autofill_dependencies = {
        'guitars_fill_5_label_label_id': ('testapp', '0019_fn'),
        'guitars_fill_3_org_org_id': ('testapp', '0021_other_fn'),
    }
    blob = headers_module.HEADER_TENANT_AUTOFILL.format(
        table='shop_order', function='guitars_fill_5_label_label_id'
    )

    assert command._function_dependencies_for(blob) == [('testapp', '0019_fn')]


def test_function_dependencies_for_ignores_an_autofill_function_it_has_not_written():
    """A header naming a function with no recorded migration must not fabricate a dependency
    on a migration that does not exist -- Django would refuse to load the graph."""
    command = Command()
    command.trigger_function_dependency = None
    command.parent_trigger_function_dependency = None
    command.tenant_autofill_dependencies = {}
    blob = headers_module.HEADER_TENANT_AUTOFILL.format(
        table='shop_order', function='guitars_fill_5_label_label_id'
    )

    assert command._function_dependencies_for(blob) == []


def test_tenant_autofill_operations_is_empty_when_policies_are_disabled():
    """``GUITARS_TENANT_POLICIES = False`` is documented as leaving the database untouched,
    and a trigger is a database object -- so the Python-only rollout stage must not receive
    one, the same gate ``_tenant_force_operations`` applies."""
    with override_settings(GUITARS_TENANT_POLICIES=False):
        command = Command()
        app = django_apps.get_app_config('testapp')
        assert command._tenant_autofill_operations(app) == []
        assert command._required_autofill_functions(set()) == {}


def test_tenant_force_operations_is_empty_when_policies_are_disabled():
    """``_handle_force_rls_stage`` already gates on this before ever calling here -- covered
    directly since a future caller reaching this method any other way must see the same
    guard, not assume tenant policies are enabled."""
    with override_settings(GUITARS_TENANT_POLICIES=False):
        assert Command()._tenant_force_operations(django_apps.get_app_config('testapp')) == []


def test_tenant_operations_force_stage_only_touches_policies_that_shipped_unforced():
    """Keying on "a policy exists and has no separate FORCE operation" would match every
    policy generated with FORCE inline -- the default -- emitting a redundant migration."""
    app = django_apps.get_app_config('testapp')

    command = Command()
    command.stdout = StringIO()
    # As shipped: policies carry FORCE inline, so the stage has nothing to do.
    assert command._tenant_force_operations(app) == []

    # Now pretend one of them shipped inert.
    command.existing.unforced_policies.add('testapp_release')
    operations = command._tenant_force_operations(app)

    assert len(operations) == 1
    assert 'Tenant FORCE RLS on "testapp_release"' in operations[0]

    # And once its FORCE operation exists, it is done.
    command.existing.tenant_forces.add('testapp_release')
    assert command._tenant_force_operations(app) == []


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
    """``--check`` on a project that never generated the shared function migration must
    fail -- it's a hard prerequisite every per-app migration depends on."""
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

    operations = command._tenant_policy_operations(app)
    headers = [line for op in operations for line in op.splitlines() if line.startswith('#')]

    assert any(header.startswith('# Tenant RLS on "testapp_release" table!') for header in headers)
    # One per policy-eligible table, and none for the multi-hop model -- asserted directly
    # rather than left implied by the count, which every new tenanted test model shifts.
    assert not any('testapp_review' in header for header in headers)
    assert len(headers) == 12
    # The CREATE form, not the replacement: there was no policy to replace.
    assert not any('replaced' in header for header in headers)


def test_a_policy_whose_shape_is_unchanged_emits_nothing():
    """The steady state: re-running on untouched models must be a no-op. Precondition for
    the two tests below meaning anything -- an unstable identity would emit every time."""
    app = django_apps.get_app_config('testapp')
    command = Command()
    command.stdout = StringIO()

    assert command._tenant_policy_operations(app) == []


def test_a_changed_coverage_shape_emits_a_replacement_not_nothing():
    """A model gaining a tenant dimension must produce a migration -- the header used to
    record only the table name, so any policy read as covered forever, silently
    under-enforcing while `--check` reported nothing and CI stayed green."""
    app = django_apps.get_app_config('testapp')
    command = Command()
    command.stdout = StringIO()

    # Pretend the recorded policy was written for a different shape.
    command.existing.tenant_policy_identities['testapp_release'] = 'stale00000000'

    operations = command._tenant_policy_operations(app)
    headers = [line for op in operations for line in op.splitlines() if line.startswith('#')]

    assert len(headers) == 1
    assert headers[0].startswith('# Tenant RLS replaced on "testapp_release" table!')
    # The replacement form, since Postgres has no CREATE OR REPLACE POLICY. Asserted on the
    # emitted SQL, not a `sql.replace_table_rls(` call, since operations carry it literally.
    assert 'DROP POLICY IF EXISTS tenant_scope ON testapp_release' in operations[0]
    assert operations[0].index('DROP POLICY IF EXISTS tenant_scope') < operations[0].index(
        'CREATE POLICY tenant_scope'
    )


def test_check_fails_when_a_policy_shape_changed(monkeypatch):
    """The gate, end to end: `--check` used to exit 0 on a model that gained a tenant
    dimension, because the generator emitted nothing to report. CI stayed green regardless."""
    app = django_apps.get_app_config('testapp')
    real = app_coverage(app)
    widened = real.tables['testapp_release']._replace(
        columns={**real.tables['testapp_release'].columns, 'region': 'region_id'}
    )
    monkeypatch.setattr(
        operations_module,
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
    """What must change the identity, and what must not -- ``force`` is excluded, since
    folding it in would make flipping GUITARS_RLS_FORCE replace every policy."""
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
    force_operations = command._tenant_force_operations(apps.get_app_config('testapp'))
    command.existing.existing_digests['testapp'] = {_generator.digest_of(force_operations)}

    command.handle(check_only=False, force_rls=True)

    assert 'No changes detected' in command.stdout.getvalue()


# --- Which policies shipped inert -----------------------------------------------


def _unforced_policy_tables(content: str) -> set[str]:
    """Find the matches ``unforced_policy_tables`` needs, then defer to the real thing --
    it takes them as an argument since its real caller already scanned for them once."""
    matches = list(headers_module._RE_TENANT_POLICY.finditer(content))
    return identity_module.unforced_policy_tables(content, matches)


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
    """Two adjacent operations, one forced and one not -- an unbounded regex would let a
    lazy match from the first reach into the second and read backwards, exactly wrong."""
    assert _unforced_policy_tables(_TWO_POLICY_OPERATIONS) == {'table_b'}


def test_unforced_policy_tables_is_empty_when_everything_shipped_forced():
    forced_only = _TWO_POLICY_OPERATIONS.replace('force=False', 'force=True')

    assert _unforced_policy_tables(forced_only) == set()


def test_unforced_policy_tables_handles_the_last_operation_in_a_file():
    """The final operation has no following header to bound it, so it is bounded by EOF."""
    only_unforced = _TWO_POLICY_OPERATIONS.replace('force=True', 'force=False')

    assert _unforced_policy_tables(only_unforced) == {'table_a', 'table_b'}


def test_unforced_policy_tables_bounds_a_replacement_operation_too():
    """A replacement operation is a policy operation too, so it must bound the one before
    it -- else a preceding force=True would run into it and read the wrong force= value."""
    with_replacement = _TWO_POLICY_OPERATIONS.replace(
        '# Tenant RLS on "table_b"', '# Tenant RLS replaced on "table_b"'
    )

    assert _unforced_policy_tables(with_replacement) == {'table_b'}


def test_unforced_policy_tables_is_last_write_wins_within_one_file():
    """Two operations for the SAME table: the later is the state the database ends in --
    accumulating into a set instead would leave an already-forced table on the backlog."""
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
    """The scan is last-write-wins per table, across files -- a finished retrofit
    (shipped inert, later replaced with force=True inlined) must come off the backlog."""
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
    """GUITARS_RLS_FORCE defaults to True, so this repo's own migrations carry FORCE
    inline -- the precondition for `--force-rls` correctly finding nothing to do."""
    command = Command()

    assert command.existing.tenant_policies
    assert command.existing.unforced_policies == set()


def test_owned_operation_warns_when_two_models_own_each_other():
    """The self-owning case one hop out: A owns B and B owns A, so each rule updates the
    table the other fires on. PostgreSQL rewrites the pair into each other and refuses every
    UPDATE to *both* tables, so neither rule may be written."""

    @isolate_apps('tests.testapp')
    def _build():
        class OwnerA(SetarModel):
            partner = OwningForeignKey('OwnerB', on_delete=SET_NULL, null=True)

            class Meta:
                app_label = 'testapp'

        class OwnerB(SetarModel):
            partner = OwningForeignKey('OwnerA', on_delete=SET_NULL, null=True)

            class Meta:
                app_label = 'testapp'

        # `all_models` by hand: ``isolate_apps`` swaps ``Options.apps``, not the global
        # registry ``_setup_models_and_reverse_relations`` reads, so a Command built here
        # sees neither model. The self-referential cascade test injects the mapping likewise.
        command = Command()
        command._mti_cascade_warnings.clear()
        command.existing.soft_delete_owned.clear()
        command.all_models = [OwnerA, OwnerB]
        return command, command._owned_operations(OwnerA) + command._owned_operations(OwnerB)

    command, ops = _build()

    assert ops == []
    assert len(command._mti_cascade_warnings) == 2
    for warning in command._mti_cascade_warnings:
        assert 'infinite rule recursion' in warning
        assert 'cycle of ON UPDATE rules' in warning


def test_owned_operation_still_emits_when_ownership_is_one_way():
    """The control for the cycle test above: A owns B and B owns nothing back, so there is
    no cycle and the rule is written. Guards against the graph over-refusing any pair."""

    @isolate_apps('tests.testapp')
    def _build():
        class OneWayOwner(SetarModel):
            owned = OwningForeignKey('OneWayOwned', on_delete=SET_NULL, null=True)

            class Meta:
                app_label = 'testapp'

        class OneWayOwned(SetarModel):
            class Meta:
                app_label = 'testapp'

        command = Command()
        command._mti_cascade_warnings.clear()
        command.existing.soft_delete_owned.clear()
        command.all_models = [OneWayOwner, OneWayOwned]
        return command, command._owned_operations(OneWayOwner)

    command, ops = _build()

    assert command._mti_cascade_warnings == []
    assert len(ops) == 1
    assert 'testapp_onewayowned' in ops[0]


def test_cascade_operation_warns_when_an_owned_rule_closes_the_cycle():
    """The mixed cycle, and the one a project reaches by accident: ``Holder`` owns ``Held``
    through one foreign key and CASCADEs from it through another, so the owned rule updates
    ``Held`` while the cascade rule updates ``Holder``. Both edges are refused."""

    @isolate_apps('tests.testapp')
    def _build():
        class Held(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Holder(SetarModel):
            owned = OwningForeignKey(Held, on_delete=SET_NULL, null=True, related_name='owners')
            parent = models.ForeignKey(Held, on_delete=CASCADE, related_name='children')

            class Meta:
                app_label = 'testapp'

        command = Command()
        command._mti_cascade_warnings.clear()
        command.existing.soft_delete_owned.clear()
        command.existing.soft_delete_related.clear()
        command.all_models = [Held, Holder]
        command.reverse_relations_mapping[Held] = {
            (Holder, Holder._meta.get_field('parent'), CASCADE)
        }
        # Held's cascade rule (fires on Held, updates Holder) and Holder's owned rule (fires
        # on Holder, updates Held) are the two halves of the same cycle.
        return command, command._cascade_operations(Held) + command._owned_operations(Holder)

    command, ops = _build()

    assert ops == []
    kinds = sorted(warning.split(' rule for ')[0] for warning in command._mti_cascade_warnings)
    assert kinds == ['Cascade', 'Owned']
    for warning in command._mti_cascade_warnings:
        assert 'cycle of ON UPDATE rules' in warning


def test_cascade_operations_report_two_relations_that_would_share_a_rule_name():
    """The frozen cascade spelling joins its FK suffix plainly, so a child table named exactly
    another child's ``<table>_<column>`` names one rule twice. It cannot be renamed -- 0.x
    shipped it and no command retires a rule -- so the clash is reported instead of silent."""

    @isolate_apps('tests.testapp')
    def _build():
        class Parent(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Child(SetarModel):
            # `a_id` sorts first, so it is the primary FK and keeps the bare form; `b_id`
            # is the one that gets the suffixed spelling this test is about.
            a = models.ForeignKey(Parent, on_delete=CASCADE, related_name='firsts')
            b = models.ForeignKey(Parent, on_delete=CASCADE, related_name='bs')

            class Meta:
                app_label = 'testapp'
                db_table = 'c_a'

        class Namesake(SetarModel):
            parent = models.ForeignKey(Parent, on_delete=CASCADE, related_name='namesakes')

            class Meta:
                app_label = 'testapp'
                # `soft_delete_related_c_a` + `_b_id` is this table's own bare name.
                db_table = 'c_a_b_id'

        command = Command()
        command._rule_name_clashes.clear()
        command.existing.soft_delete_related.clear()
        command.all_models = [Parent, Child, Namesake]
        command.reverse_relations_mapping[Parent] = {
            (Child, Child._meta.get_field('a'), CASCADE),
            (Child, Child._meta.get_field('b'), CASCADE),
            (Namesake, Namesake._meta.get_field('parent'), CASCADE),
        }
        return command, command._cascade_operations(Parent)

    command, ops = _build()

    # Emitted anyway: what ships works for one of the two, which is the whole problem.
    assert len(ops) == 3
    assert len(command._rule_name_clashes) == 1
    clash = command._rule_name_clashes[0]
    assert 'soft_delete_related_c_a_b_id' in clash
    assert "'c_a' via 'b_id'" in clash and "'c_a_b_id'" in clash
    assert 'the second replaces the first' in clash


def test_cascade_operations_report_an_mti_parent_and_child_sharing_a_rule_name():
    """Regression: an MTI parent and its child share one ``owner_table`` and
    ``seen_related_tables`` is per call, so both bare names -- and both operation *keys* --
    matched. Claiming on the key read the two as one and reported nothing."""

    @isolate_apps('tests.testapp')
    def _build():
        class Parent(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Child(Parent):
            class Meta:
                app_label = 'testapp'

        class Referrer(SetarModel):
            p = models.ForeignKey(Parent, on_delete=CASCADE, related_name='ps')
            c = models.ForeignKey(Child, on_delete=CASCADE, related_name='cs')

            class Meta:
                app_label = 'testapp'

        command = Command()
        command._rule_name_clashes.clear()
        command.existing.soft_delete_related.clear()
        command.all_models = [Parent, Child, Referrer]
        command.reverse_relations_mapping[Parent] = {
            (Referrer, Referrer._meta.get_field('p'), CASCADE),
        }
        command.reverse_relations_mapping[Child] = {
            (Referrer, Referrer._meta.get_field('c'), CASCADE),
        }
        # Both land on the parent's table: it is where ``_deleted_at`` actually flips.
        return command, [*command._cascade_operations(Parent), *command._cascade_operations(Child)]

    command, ops = _build()

    assert len(ops) == 2
    assert len(command._rule_name_clashes) == 1
    clash = command._rule_name_clashes[0]
    assert "via 'p_id'" in clash and "via 'c_id'" in clash
    assert 'the second replaces the first' in clash
    # The bare name carries no column, so the plain "rename a column" remedy cannot apply.
    assert 'renaming cannot help' in clash


def test_a_rule_name_clash_fails_a_check_run_but_only_reports_on_a_generating_one():
    """A clash means one of the two rules does not exist in the database the migration ships
    to, which is exactly what ``--check`` is for. A generating run has already written the
    files by the time it is known, so there it stays the report printed just above."""
    command = Command()
    command._rule_name_clashes = ['two relations name one rule']

    with pytest.raises(CommandError, match='two relations name one rule'):
        command._refuse_a_rule_name_clash(check_only=True)

    command._refuse_a_rule_name_clash(check_only=False)


# ---- Cross-owner last-owner guard (ADR 0012) ------------------------------------------


def _owned_blob(*models_to_register, subject=None):
    """Build a Command over exactly *models_to_register* and return its owned operations for
    *subject* (the first model by default). ``all_models`` by hand for the reason the cycle
    tests above give: ``isolate_apps`` swaps ``Options.apps``, not the global registry."""
    command = Command()
    command._mti_cascade_warnings.clear()
    command._refusals_over_live_rules.clear()
    command.existing.soft_delete_owned.clear()
    command.all_models = list(models_to_register)
    ops = command._owned_operations(subject or models_to_register[0])
    return command, '\n'.join(ops), ops


def test_owned_guard_carries_an_arm_for_a_co_owner_on_another_table():
    """The bug 2.4.0 exists for: two owner tables, each rule blind to the other, so the last
    owner *of one kind* archived a dependent a live owner of the other kind still held."""

    @isolate_apps('tests.testapp')
    def _build():
        class Shared(SetarModel):
            class Meta:
                app_label = 'testapp'

        class OwnerLeft(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)

            class Meta:
                app_label = 'testapp'

        class OwnerRight(SetarModel):
            other = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)

            class Meta:
                app_label = 'testapp'

        registry = (Shared, OwnerLeft, OwnerRight)
        return (
            _owned_blob(*registry, subject=OwnerLeft)[1],
            _owned_blob(*registry, subject=OwnerRight)[1],
        )

    left, right = _build()

    # Each rule reads its own table self-excluded, and the other's unqualified: the other
    # table holds no row being soft-deleted by this statement, so there is none to exclude.
    assert 'FROM "testapp_ownerleft" AS guitars_owner\n' in left
    assert 'guitars_owner."target_id" = old."target_id"' in left
    assert 'FROM "testapp_ownerright" AS guitars_owner_1' in left
    assert 'guitars_owner_1."other_id" = old."target_id"' in left
    assert 'guitars_owner_1."id" <> old."id"' not in left

    assert 'FROM "testapp_ownerright" AS guitars_owner\n' in right
    assert 'FROM "testapp_ownerleft" AS guitars_owner_1' in right
    assert 'guitars_owner_1."target_id" = old."other_id"' in right
    assert 'guitars_owner_1."id" <> old."id"' not in right


def test_owned_guard_self_excludes_every_arm_on_the_declaring_table():
    """Two owning columns on one table. Self-exclusion is per *row*, not per column: without
    it on the second arm, a row owning the target through both columns reads as its own last
    live owner and holds the target alive forever."""

    @isolate_apps('tests.testapp')
    def _build():
        class Shared(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Twice(SetarModel):
            first = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True, related_name='+')
            second = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True, related_name='+')

            class Meta:
                app_label = 'testapp'

        return _owned_blob(Shared, Twice, subject=Twice)[1]

    blob = _build()

    assert blob.count('AS guitars_owner_1') == 2  # one arm in each of the two rules
    assert 'guitars_owner_1."second_id" = old."first_id"' in blob
    assert 'guitars_owner_1."first_id" = old."second_id"' in blob
    # Both arms are on the table the rule fires on, so both exclude the row going away.
    assert blob.count('guitars_owner_1."id" <> old."id"') == 2


def test_owned_guard_does_not_count_a_self_owning_target_as_its_own_owner():
    """A target owning *itself* gets no rule of its own -- that is the 1-cycle -- but still
    contributes an arm to every other owner's rule. Excluded by the key rather than by pk: the
    row that must not count is the one the rule stamps, not the one going away."""

    @isolate_apps('tests.testapp')
    def _build():
        class Shared(SetarModel):
            parent = OwningForeignKey('self', on_delete=DO_NOTHING, null=True, related_name='+')

            class Meta:
                app_label = 'testapp'

        class Owner(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)

            class Meta:
                app_label = 'testapp'

        return _owned_blob(Shared, Owner, subject=Owner)

    command, blob, ops = _build()

    assert len(ops) == 1  # Shared's own relation is refused; Owner's still carries its arm
    assert 'FROM "testapp_shared" AS guitars_owner_1' in blob
    assert 'guitars_owner_1."id" <> old."target_id"' in blob


def test_owned_guard_scales_to_three_owners():
    """N owning columns produce N rules of N arms. Guards the composition, not one shape."""

    @isolate_apps('tests.testapp')
    def _build():
        class Shared(SetarModel):
            class Meta:
                app_label = 'testapp'

        owners = []
        for name in ('OwnerOne', 'OwnerTwo', 'OwnerThree'):
            owners.append(
                type(
                    name,
                    (SetarModel,),
                    {
                        'target': OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True),
                        'Meta': type('Meta', (), {'app_label': 'testapp'}),
                        '__module__': 'tests.testapp.models',
                    },
                )
            )
        return [_owned_blob(Shared, *owners, subject=owner)[1] for owner in owners]

    blobs = _build()

    assert len(blobs) == 3
    for blob in blobs:
        assert blob.count('NOT EXISTS') == 3
        assert 'AS guitars_owner\n' in blob
        assert 'AS guitars_owner_1' in blob
        assert 'AS guitars_owner_2' in blob


def test_owned_guard_arm_order_does_not_follow_registry_order():
    """The arms are sorted, so the rendered guard -- and the ``[SQL:...]`` identity deciding
    whether a migration is emitted at all -- cannot move with the order models happen to be
    registered in. An order-dependent digest would make ``--check`` flap."""

    @isolate_apps('tests.testapp')
    def _build():
        class Shared(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Zulu(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)

            class Meta:
                app_label = 'testapp'

        class Alpha(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)

            class Meta:
                app_label = 'testapp'

        forward = _owned_blob(Shared, Zulu, Alpha, subject=Zulu)[1]
        reversed_ = _owned_blob(Alpha, Zulu, Shared, subject=Zulu)[1]
        return forward, reversed_

    forward, reversed_ = _build()

    assert forward == reversed_


def test_owned_guard_skips_a_co_owner_it_cannot_express():
    """``Orchestra.programme`` owns PressKit but keeps ``_deleted_at`` on its MTI ancestor, so
    an arm against it would need a join the template has no shape for. It contributes none --
    and is already reported by the refusal that denies it a rule of its own."""
    command = Command()
    command.existing.soft_delete_owned.clear()

    blob = '\n'.join(command._owned_operations(Album))

    assert 'testapp_orchestra' not in blob
    # The two arms it does carry are Album's own pair, not three.
    assert blob.count('NOT EXISTS') == 4  # two rules, two arms each


def test_owned_guard_reads_owners_the_kit_does_not_generate_for():
    """A live owner is live whether or not its app is in ``LOCAL_APPS``. Excluding a non-local
    co-owner would re-create the very bug this guard closes, for third-party models."""

    # 'legacy_migrations' is installed but deliberately outside LOCAL_APPS, so the kit
    # generates no enforcement for it -- exactly the asymmetry CHANGELOG 2.3.0 records.
    @isolate_apps('tests.testapp', 'tests.legacy_migrations')
    def _build():
        class Shared(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Local(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)

            class Meta:
                app_label = 'testapp'

        class Vendor(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)

            class Meta:
                app_label = 'legacy_migrations'

        assert 'tests.legacy_migrations' not in django_settings.LOCAL_APPS
        return _owned_blob(Shared, Local, Vendor, subject=Local)[1]

    blob = _build()

    assert 'FROM "legacy_migrations_vendor" AS guitars_owner_1' in blob


def test_owned_rule_is_refused_when_a_co_owner_is_tenanted_and_the_dependent_is_not():
    """The guard's NOT EXISTS is an ordinary SELECT, so a policy on the co-owner's table hides
    an out-of-tenant live owner and the rule stamps a still-owned row. 2.3.0 could only reach
    that through the table you declared the key on; an arm reaches tables you never named."""

    @isolate_apps('tests.testapp')
    def _build():
        from guitars.tenancy import tenanted_manager

        class Shared(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Plain(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)

            class Meta:
                app_label = 'testapp'

        class Scoped(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)
            label = models.ForeignKey('testapp.Label', on_delete=CASCADE, null=True)
            objects = tenanted_manager(label='label')

            class Meta:
                app_label = 'testapp'

        return _owned_blob(Shared, Plain, Scoped, subject=Plain)

    command, blob, ops = _build()

    assert ops == []
    assert len(command._mti_cascade_warnings) == 1
    warning = command._mti_cascade_warnings[0]
    assert "'testapp_scoped'" in warning
    assert 'tenanted' in warning
    assert 'another tenant' in warning


def test_owned_rule_is_refused_when_a_co_owner_carries_a_dimension_the_dependent_does_not():
    """A tenanted *dependent* is not enough. The co-owner's policy filters on `tenant.market`,
    a dimension reaching the dependent's row never constrained, so a live owner in another
    market is still invisible -- the same stamp-a-still-owned-row hole, one shape further in."""

    @isolate_apps('tests.testapp')
    def _build():
        from guitars.tenancy import tenanted_manager

        class Shared(SetarModel):
            label = models.ForeignKey('testapp.Label', on_delete=CASCADE, null=True)
            objects = tenanted_manager(label='label')

            class Meta:
                app_label = 'testapp'

        class Plain(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)

            class Meta:
                app_label = 'testapp'

        class Scoped(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)
            label = models.ForeignKey('testapp.Label', on_delete=CASCADE, null=True)
            objects = tenanted_manager(market='label')

            class Meta:
                app_label = 'testapp'

        return _owned_blob(Shared, Plain, Scoped, subject=Plain)

    command, blob, ops = _build()

    assert ops == []
    assert len(command._mti_cascade_warnings) == 1
    assert "'testapp_scoped'" in command._mti_cascade_warnings[0]


def test_owned_rule_survives_a_co_owner_tenanted_on_the_dependent_s_own_dimension():
    """The control, and why the refusal is per dimension rather than per tenanted-ness:
    reaching the dependent's row already put the session inside `tenant.label`, so the
    co-owner's policy on that same dimension cannot hide one of its owners."""

    @isolate_apps('tests.testapp')
    def _build():
        from guitars.tenancy import tenanted_manager

        class Shared(SetarModel):
            label = models.ForeignKey('testapp.Label', on_delete=CASCADE, null=True)
            objects = tenanted_manager(label='label')

            class Meta:
                app_label = 'testapp'

        class Plain(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)

            class Meta:
                app_label = 'testapp'

        class Scoped(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)
            label = models.ForeignKey('testapp.Label', on_delete=CASCADE, null=True)
            objects = tenanted_manager(label='label')

            class Meta:
                app_label = 'testapp'

        return _owned_blob(Shared, Plain, Scoped, subject=Plain)

    command, blob, ops = _build()

    assert command._mti_cascade_warnings == []
    assert 'FROM "testapp_scoped" AS guitars_owner_1' in blob


def test_owned_rule_survives_a_co_owner_whose_dimension_no_policy_can_filter_on():
    """A dimension traversing a relation is left to Python scoping, so no policy filters the
    co-owner's table and no read of it hides anything. Refusing on the *manager* withheld a
    correct rule -- and failed ``--check`` over one recorded, told to drop what was working."""

    @isolate_apps('tests.testapp')
    def _build():
        from guitars.tenancy import tenanted_manager

        class Shared(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Plain(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)

            class Meta:
                app_label = 'testapp'

        class Hop(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)
            release = models.ForeignKey('testapp.Release', on_delete=CASCADE, null=True)
            # Two hops from this table, so ``_classify`` emits no policy for it at all.
            objects = tenanted_manager(label='release__label')

            class Meta:
                app_label = 'testapp'

        return _owned_blob(Shared, Plain, Hop, subject=Plain)

    command, blob, ops = _build()

    assert command._mti_cascade_warnings == []
    assert 'FROM "testapp_hop" AS guitars_owner_1' in blob


def test_owned_rule_is_refused_when_the_dependent_s_own_dimension_predicates_nothing():
    """The mirror: the dependent *declares* the co-owner's dimension but its policy cannot
    filter on it, so reaching its row put the session inside nothing and the co-owner's policy
    hides a live owner anyway. Read off the spec, the two dimension sets cancelled."""

    @isolate_apps('tests.testapp')
    def _build():
        from guitars.tenancy import tenanted_manager

        class Shared(SetarModel):
            release = models.ForeignKey('testapp.Release', on_delete=CASCADE, null=True)
            objects = tenanted_manager(label='release__label')

            class Meta:
                app_label = 'testapp'

        class Plain(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)

            class Meta:
                app_label = 'testapp'

        class Scoped(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)
            label = models.ForeignKey('testapp.Label', on_delete=CASCADE, null=True)
            objects = tenanted_manager(label='label')

            class Meta:
                app_label = 'testapp'

        return _owned_blob(Shared, Plain, Scoped, subject=Plain)

    command, blob, ops = _build()

    assert ops == []
    assert len(command._mti_cascade_warnings) == 1
    assert "'testapp_scoped'" in command._mti_cascade_warnings[0]


def test_owned_rule_is_refused_when_the_dependent_is_one_the_kit_writes_no_policy_for():
    """The other side of that fallback, and why it cannot be the same one: the dependent's
    dimensions are *subtracted*, so reading an unknown table's manager as enforced would cancel
    the co-owner's and suppress the refusal. Unknown means unfiltered on this side."""

    @isolate_apps('tests.testapp', 'tests.legacy_migrations')
    def _build():
        from guitars.tenancy import tenanted_manager

        class Shared(SetarModel):
            release = models.ForeignKey('testapp.Release', on_delete=CASCADE, null=True)
            objects = tenanted_manager(label='release__label')

            class Meta:
                app_label = 'legacy_migrations'

        class Plain(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)

            class Meta:
                app_label = 'testapp'

        class Scoped(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)
            label = models.ForeignKey('testapp.Label', on_delete=CASCADE, null=True)
            objects = tenanted_manager(label='label')

            class Meta:
                app_label = 'testapp'

        return _owned_blob(Shared, Plain, Scoped, subject=Plain)

    command, blob, ops = _build()

    assert ops == []
    assert len(command._mti_cascade_warnings) == 1
    assert "'testapp_scoped'" in command._mti_cascade_warnings[0]


def test_policy_dimensions_are_asked_once_per_model_per_run():
    """Two rules over one dependent, and the answer reaches ``_classify``, which sweeps the
    registry for an MTI model that autofills. Memoised for the run rather than the process: it
    moves with ``LOCAL_APPS``, and ``isolate_apps`` replaces the registry between runs."""

    @isolate_apps('tests.testapp')
    def _build():
        from guitars.tenancy import tenanted_manager

        class Shared(SetarModel):
            label = models.ForeignKey('testapp.Label', on_delete=CASCADE, null=True)
            objects = tenanted_manager(label='label')

            class Meta:
                app_label = 'testapp'

        class Twice(SetarModel):
            first = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True, related_name='+')
            second = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True, related_name='+')

            class Meta:
                app_label = 'testapp'

        return _owned_blob(Shared, Twice, subject=Twice)

    command, blob, ops = _build()

    # Both rules emitted: the owner carries none of the dependent's dimensions, so nothing its
    # arms read is filtered by one the dependent's own policy does not apply.
    assert len(ops) == 2
    assert list(command._policy_dimension_memo.values()) == [frozenset({'label'})]


def test_owned_rule_is_refused_for_a_tenanted_co_owner_the_kit_generates_no_policy_for():
    """A model outside ``LOCAL_APPS`` gets no policy from this kit, but its own package may
    carry one -- unknowable from here, so its manager is read as enforced. The fail-safe half
    of asking what a policy filters on rather than what a manager declares."""

    @isolate_apps('tests.testapp', 'tests.legacy_migrations')
    def _build():
        from guitars.tenancy import tenanted_manager

        class Shared(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Plain(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)

            class Meta:
                app_label = 'testapp'

        class Vendor(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)
            market = models.ForeignKey('testapp.Market', on_delete=CASCADE, null=True)
            objects = tenanted_manager(market='market')

            class Meta:
                app_label = 'legacy_migrations'

        assert 'tests.legacy_migrations' not in django_settings.LOCAL_APPS
        return _owned_blob(Shared, Plain, Vendor, subject=Plain)

    command, blob, ops = _build()

    assert ops == []
    assert len(command._mti_cascade_warnings) == 1
    assert "'legacy_migrations_vendor'" in command._mti_cascade_warnings[0]


@override_settings(LOCAL_APPS=['fake.kioska', 'fake.loose'])
def test_a_scoped_run_does_not_report_an_out_of_scope_app_s_own_misconfiguration(monkeypatch):
    """The gap-note pass re-runs the owned candidate test over apps this run was never asked
    about, taking its verdict without its reporting: otherwise a scoped run prints another app's
    misconfiguration, and where that app recorded the rule, raises over it."""
    command = Command()
    command._mti_cascade_warnings.clear()
    command._refusals_over_live_rules.clear()
    command.existing.soft_delete_owned.clear()

    @isolate_apps('tests.testapp')
    def _build():
        class Kit(SetarModel):
            legacy_id = models.IntegerField(unique=True)

            class Meta:
                app_label = 'testapp'

        class Loose(SetarModel):
            # Refused for its redirected key, the one refusal reachable here: the pass skips a
            # model that does not own ``_deleted_at`` before it ever asks about its fields.
            kit = OwningForeignKey(
                Kit, on_delete=DO_NOTHING, to_field='legacy_id', null=True, blank=True
            )

            class Meta:
                app_label = 'testapp'

        # Recorded, so reporting the refusal would escalate rather than merely warn.
        command.existing.soft_delete_owned[
            (Kit._meta.db_table, Loose._meta.db_table, 'kit_id')
        ] = None
        monkeypatch.setattr(
            operations_module.django_apps,
            'get_app_configs',
            lambda: [
                _fake_app_config('fake.kioska', 'kioska', [Kiosk]),
                _fake_app_config('fake.loose', 'loose', [Loose]),
            ],
        )
        return command._scoped_owned_gap_notes({'kioska'})

    assert _build() == []
    assert command._mti_cascade_warnings == []
    assert command._refusals_over_live_rules == []


def test_a_refused_owned_rule_that_already_exists_fails_check():
    """Refusing emits nothing, so the rule 2.3.0 recorded stays live and wrong with nothing
    else to notice -- unlike every other refusal, which only ever fires where no rule was
    written. The escalation is the only thing between that and a green ``--check``."""

    @isolate_apps('tests.testapp')
    def _build():
        class Shared(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Cyclic(SetarModel):
            target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True)

            class Meta:
                app_label = 'testapp'

        # Shared owns Cyclic back, so both rules close an ON UPDATE cycle and are refused.
        Shared.add_to_class(
            'back', OwningForeignKey(Cyclic, on_delete=DO_NOTHING, null=True, related_name='+')
        )

        command = Command()
        command._mti_cascade_warnings.clear()
        command._refusals_over_live_rules.clear()
        command.all_models = [Shared, Cyclic]
        # Pretend the project already migrated this rule, which 2.3.0 would have written.
        command.existing.soft_delete_owned.clear()
        command.existing.soft_delete_owned[('testapp_shared', 'testapp_cyclic', 'target_id')] = (
            'deadbeefcafe'
        )
        return command, command._owned_operations(Cyclic)

    command, ops = _build()

    assert ops == []
    assert len(command._refusals_over_live_rules) == 1
    assert 'DROP RULE' in command._refusals_over_live_rules[0]
    with pytest.raises(CommandError, match='already exists'):
        command._refuse_a_stale_owned_rule(check_only=True)
    # A generating run reports it and carries on -- there is nothing it could emit instead.
    command._refuse_a_stale_owned_rule(check_only=False)


def test_a_single_owner_rule_is_byte_identical_to_2_3_0(snapshot):
    """A dependent owned from one place renders exactly as 2.3.0 rendered it, so its
    ``[SQL:...]`` identity does not move. Pinned byte for byte: a substring assertion cannot
    catch a whitespace change, and whitespace is what the digest hashes."""

    @isolate_apps('tests.testapp')
    def _build():
        class Alone(SetarModel):
            class Meta:
                app_label = 'testapp'

        class OnlyOwner(SetarModel):
            target = OwningForeignKey(Alone, on_delete=DO_NOTHING, null=True)

            class Meta:
                app_label = 'testapp'

        return _owned_blob(Alone, OnlyOwner, subject=OnlyOwner)[1]

    assert _build() == snapshot


@override_settings(LOCAL_APPS=['fake.kioska', 'fake.foyerb'])
def test_scoped_run_warns_that_an_out_of_scope_owned_rule_may_be_stale(monkeypatch):
    """Arms are registry-wide, so generating for one app moves the rule text of an owner in an
    app the same run never re-derives. Warned rather than escalated, per ADR 0012: an unscoped
    run -- what CI runs -- re-derives every rule."""
    command = Command()
    kiosk_app = _fake_app_config('fake.kioska', 'kioska', [Kiosk])
    foyer_app = _fake_app_config('fake.foyerb', 'foyerb', [Foyer])
    monkeypatch.setattr(
        operations_module.django_apps,
        'get_app_configs',
        lambda: [kiosk_app, foyer_app],
    )

    notes = command._scoped_owned_gap_notes({'kioska'})

    assert len(notes) == 1
    assert (
        "Owned rule on 'testapp_placard' owned by 'testapp_foyer' via 'placard_id' may be stale"
        in notes[0]
    )
    # The in-scope table whose arm moved the out-of-scope rule's text, and the app that holds
    # the rule this run leaves alone.
    assert "reads 'testapp_kiosk', in this run's scope, but app 'foyerb' is not" in notes[0]
