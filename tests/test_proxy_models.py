"""A proxy is not a multi-table-inheritance child (2.9.1, issue #45). Django fills its
``_meta.parents`` and it declares no field, so it read as one -- and sharing its concrete
model's table, the operations it earned named that table as their own parent."""

from io import StringIO
from pathlib import Path
from unittest import mock

import pytest
from django.db import models
from django.core.management import call_command
from django.test.utils import isolate_apps

from guitars.introspection import is_mti_child
from guitars.management import _generator
from guitars.management.enforcement.command import Command
from guitars.models import SetarModel
from tests.testapp.models import Ensemble, Orchestra


class _Config:
    """The minimal ``AppConfig`` face ``_build_operations`` reads, so a test can hand it an
    exact model list rather than the whole registry."""

    label = 'testapp'

    def __init__(self, *models):
        self._models = models

    def get_models(self):
        return list(self._models)


@pytest.fixture
def plain_proxy():
    """A proxy over a plain soft-deletable model, registered for one test."""

    @isolate_apps('tests.testapp')
    def _build():
        class Plain(SetarModel):
            class Meta(SetarModel.Meta):
                app_label = 'testapp'

        class PlainProxy(Plain):
            class Meta:
                app_label = 'testapp'
                proxy = True

        return Plain, PlainProxy

    return _build()


def test_a_proxy_is_not_an_mti_child(plain_proxy):
    """The predicate, and with it every caller -- ``needs_parent_function`` included, which
    walks its own model list and so is out of the operations loop's reach. ``_meta.parents`` is
    truthy for a proxy and ``owns_column`` false, which is the whole trap."""
    _plain, proxy = plain_proxy

    assert proxy._meta.parents  # the trap: Django fills this for a proxy too
    assert not proxy._meta.local_fields
    assert is_mti_child(proxy, '_updated_at') is False
    assert is_mti_child(proxy, '_deleted_at') is False


def test_a_proxy_adds_no_operation_of_its_own(plain_proxy):
    """Two operations, both the concrete model's. Before the fix there were four: the proxy
    earned an MTI trigger and an MTI rule, each naming ``testapp_plain`` as its own parent."""
    plain, proxy = plain_proxy

    headers = [
        line
        for operation in Command()._build_operations(_Config(plain, proxy))
        for line in operation.splitlines()
        if line.startswith('#')
    ]

    assert len(headers) == 2
    assert not [header for header in headers if header.startswith('# MTI')]


@pytest.fixture
def mti_child_proxy():
    """A proxy over a *real* MTI child, the shape where the concrete model legitimately does
    earn MTI operations and the proxy must add none."""

    @isolate_apps('tests.testapp')
    def _build():
        class OrchestraProxy(Orchestra):
            class Meta:
                app_label = 'testapp'
                proxy = True

        return OrchestraProxy

    return _build()


def test_a_proxy_of_an_mti_child_adds_nothing_and_leaves_the_child_alone(mti_child_proxy):
    """The child keeps every operation its own inheritance earns; the proxy contributes none.
    Both would otherwise key on ``testapp_orchestra`` and collide rather than add."""
    def _emit(*models):
        # Recorded coverage cleared, or the corpus's own MTI keys make this emit nothing and
        # the comparison would hold for the wrong reason.
        command = Command()
        command.existing.mti_triggers.clear()
        command.existing.mti_soft_deletes.clear()
        return command._build_operations(_Config(*models))

    with_proxy = _emit(Ensemble, Orchestra, mti_child_proxy)
    without = _emit(Ensemble, Orchestra)

    assert with_proxy == without
    assert [line for operation in without for line in operation.splitlines() if '# MTI' in line]


# --- The note for a migration written before the fix -----------------------------------------


def test_a_recorded_mti_key_nothing_requires_is_named():
    """The generator cannot repair a file, so it says which file and what to do to it. Both
    readings are named -- a proxy and a model flattened out of inheritance leave an identical
    record -- and the note sends the reader to the database rather than guessing which."""
    command = Command()
    # ``testapp_band`` is a plain model's table: hosted, and no model reaches a column through
    # an ancestor there. That is exactly the shape a proxy left behind.
    command.existing.mti_triggers['testapp_band'] = 'abc'

    (note,) = command._orphaned_mti_notes()

    assert "MTI Updated at Trigger on 'testapp_band' is recorded" in note
    assert 'a proxy model earned the operation before 2.9.1' in note
    assert 'Delete the operation from the migration' in note
    assert 'flattened out of inheritance' in note
    assert 'look in the database rather than assuming' in note


def test_each_family_says_what_its_own_shape_did_to_the_migration():
    """The two halves differ in whether the migration applied at all, and one shared sentence
    was wrong for one of them: a duplicate *rule* is deduped and applies, only a duplicate
    *trigger* aborts. Saying the trigger's reason over the rule misreports what is live."""
    command = Command()
    command.existing.mti_triggers['testapp_band'] = 'abc'
    command.existing.mti_soft_deletes['testapp_band'] = 'abc'

    trigger, rule = command._orphaned_mti_notes()

    assert 'MTI Soft Delete Rule' in rule
    assert 'dedupes a rule on its name per table' in rule
    # And not a flat "so that migration applied": on every rung carrying both columns the
    # trigger rides the same atomic migration and takes the rule down with it.
    assert 'may still have aborted the pair' in rule
    assert '_deleted_at' in rule
    assert 'MTI Updated at Trigger' in trigger
    assert 'refusing a second of one name on a table' in trigger
    # The --adopt form drops before it creates, so that one applied and the blanket
    # "nothing is live" this note used to carry was false for it.
    assert 'the --adopt form drops before it creates' in trigger
    assert '_updated_at' in trigger


def test_a_name_a_rename_freed_and_another_model_retook_stays_silent():
    """The third shape, and the one where both repairs are wrong. The scan declines to re-key
    coverage onto the new spelling while the old name is live, so the record stays under the
    freed name -- while the object itself went with the table under its new one."""
    command = Command()
    # ``testapp_callbacks`` was really renamed from ``testapp_encore`` (0051, 0053), so the
    # chain is the corpus's own rather than a fixture's.
    command.existing.mti_triggers['testapp_encore'] = 'abc'
    # And the freed name is live again, which is the whole condition.
    command._table_app_labels_cache = {**command._table_app_labels(), 'testapp_encore': 'testapp'}

    assert command._orphaned_mti_notes() == []


def test_a_recorded_mti_key_on_an_unmapped_table_stays_silent():
    """Positive evidence only. A table mapping to no local model is a deleted model on one
    reading and a scoped run on another, and following the wrong one is how a live object gets
    dropped -- so that case is left alone, as the cascade and autofill families leave it."""
    command = Command()
    command.existing.mti_triggers['shop_gone'] = 'abc'

    assert command._orphaned_mti_notes() == []


def test_the_real_corpus_produces_no_note():
    """Every MTI key the committed migrations record is still required by a concrete child, so
    a project with no proxy sees nothing."""
    assert Command()._orphaned_mti_notes() == []


# --- A relation pointing *at* a proxy ----------------------------------------------------------


@pytest.fixture
def fk_to_proxy():
    """A cascade key aimed at a proxy, plus a self-referential one. Django keeps the proxy as
    ``Field.related_model``, so both arms were filed under a model that owns no table."""

    @isolate_apps('tests.testapp')
    def _build():
        class Owner(SetarModel):
            parent = models.ForeignKey(
                'testapp.OwnerProxy', on_delete=models.CASCADE, null=True, related_name='kids'
            )

            class Meta(SetarModel.Meta):
                app_label = 'testapp'

        class OwnerProxy(Owner):
            class Meta:
                app_label = 'testapp'
                proxy = True

        class Held(SetarModel):
            ref = models.ForeignKey(OwnerProxy, on_delete=models.CASCADE)

            class Meta(SetarModel.Meta):
                app_label = 'testapp'

        return Owner, OwnerProxy, Held

    return _build()


def _headers(*emitted: type[models.Model]) -> list[str]:
    command = Command()
    command.all_models = list(emitted)
    command._index_reverse_relations(command.all_models)
    return [
        line
        for operation in command._build_operations(_Config(*emitted))
        for line in operation.splitlines()
        if line.startswith('#') and 'retired' not in line
    ]


def test_a_cascade_key_aimed_at_a_proxy_still_gets_its_rule(fk_to_proxy):
    """The regression the proxy skip opened. ``related_model`` is the proxy, so the arm was
    filed under it, and skipping proxies then left no model reaching it: the rule vanished with
    ``--check`` green, and a raw ``DELETE`` on the owner archived it and left the child live."""
    owner, proxy, held = fk_to_proxy

    headers = _headers(owner, proxy, held)

    assert [
        header
        for header in headers
        if 'Soft Delete Related Rule on "testapp_held" that is related to "testapp_owner"'
        in header
    ]


def test_a_self_referential_cascade_key_aimed_at_a_proxy_still_gets_its_trigger(fk_to_proxy):
    """The same loss in the family that takes a trigger instead of a rule (ADR 0018). Its key
    names the proxy, so it too was filed under the model no walk reaches."""
    owner, proxy, held = fk_to_proxy

    headers = _headers(owner, proxy, held)

    assert [header for header in headers if 'Self Cascade' in header and 'testapp_owner' in header]


def test_a_proxy_of_the_child_does_not_double_the_arm(plain_proxy):
    """The other direction, on the mapping rather than the rules it feeds: a proxy's
    ``get_fields()`` is its concrete model's, so indexing one files every key twice under one
    table. ``_is_cascade_candidate`` rejects the copy anyway; this never files it."""
    plain, proxy = plain_proxy

    @isolate_apps('tests.testapp')
    def _build():
        class Holder(SetarModel):
            ref = models.ForeignKey(plain, on_delete=models.CASCADE)

            class Meta(SetarModel.Meta):
                app_label = 'testapp'

        class HolderProxy(Holder):
            class Meta:
                app_label = 'testapp'
                proxy = True

        return Holder, HolderProxy

    holder, _holder_proxy = _build()
    command = Command()
    command._index_reverse_relations([plain, proxy, holder, _holder_proxy])

    (arm,) = command.reverse_relations_mapping[plain]
    assert arm[0] is holder


def test_a_migration_carrying_one_mti_header_twice_is_named_with_its_file():
    """The shape the set difference is blind to: a proxy over a *real* MTI child recorded the
    key its concrete child still requires. The repeat inside one file is the only evidence of
    it, and unlike the orphan note this one can name the migration to open."""
    command = Command()
    command.existing.duplicate_mti_operations.append(
        ('shop', '0004_auto_enforcement', 'MTI Updated at Trigger', 'shop_descendant')
    )

    command.existing.duplicate_mti_operations.append(
        ('shop', '0004_auto_enforcement', 'MTI Soft Delete Rule', 'shop_descendant')
    )

    trigger, rule = command._duplicated_mti_notes()

    assert "MTI Updated at Trigger on 'shop_descendant' is written twice" in trigger
    assert "migration '0004_auto_enforcement' of app 'shop'" in trigger
    assert 'Delete the repeated operation' in trigger
    # Both readings, as every sibling note gives: a hand-edited file and two models sharing one
    # ``db_table`` reach this too, and the note tells a consumer to delete something.
    assert 'a migration was edited by hand' in trigger
    assert 'two models share that ``db_table``' in trigger
    # And the applicability hedged per kind. Only the plain trigger form collides: the rule form
    # is CREATE OR REPLACE and the --adopt trigger form drops first, so a flat "cannot apply"
    # would send a consumer with a working database to edit applied history.
    assert 'the --adopt form drops before it creates' in trigger
    assert 'That operation applies either way' in rule
    assert 'CREATE OR REPLACE' in rule


def test_the_scan_reads_a_repeat_out_of_a_migration_file(monkeypatch):
    """The scan half, through the real walk and the real regex, the file handed in rather than
    written: on disk it goes in the app's own migrations directory, where ``-n auto`` lets
    another worker read it half-written. The clean-corpus assertion comes first, before that."""
    assert Command().existing.duplicate_mti_operations == []

    header = (
        '        # MTI Updated at Trigger on "testapp_orchestra" table '
        '(parent "testapp_ensemble")! [SQL:abc123abc123]\n'
        "        migrations.RunSQL(sql='SELECT 1;', reverse_sql='SELECT 1;'),\n"
    )
    walk = _generator.iter_migration_files
    monkeypatch.setattr(
        _generator,
        'iter_migration_files',
        lambda app: (
            [(Path('0099_a_repeated_mti_header.py'), header + header)]
            if app.label == 'testapp'
            else walk(app)
        ),
    )

    (found,) = Command().existing.duplicate_mti_operations

    assert found == (
        'testapp',
        '0099_a_repeated_mti_header',
        'MTI Updated at Trigger',
        'testapp_orchestra',
    )


def test_a_third_copy_does_not_repeat_the_sentence(monkeypatch):
    """Recorded per file rather than per extra copy. Three copies are one file to open, and
    the same sentence printed twice reads as two problems."""
    header = (
        '        # MTI Updated at Trigger on "testapp_orchestra" table '
        '(parent "testapp_ensemble")! [SQL:abc123abc123]\n'
    )
    walk = _generator.iter_migration_files
    monkeypatch.setattr(
        _generator,
        'iter_migration_files',
        lambda app: (
            [(Path('0099_thrice.py'), header * 3)] if app.label == 'testapp' else walk(app)
        ),
    )

    assert len(Command().existing.duplicate_mti_operations) == 1


@pytest.mark.parametrize(
    'method',
    ['_orphaned_mti_notes', '_duplicated_mti_notes'],
)
def test_each_note_reaches_stdout_and_does_not_fail_the_check(method):
    """Wiring, and the deliberate half of it. Every other test here calls the method, so the
    one line joining each to the report could go without a failure -- and both are advisory:
    a run that fails ``--check`` over a file the command cannot repair helps nobody."""
    out, err = StringIO(), StringIO()
    with mock.patch.object(Command, method, return_value=[f'Reported by {method}.']):
        call_command('makeguitarmigrations', '--check', stdout=out, stderr=err)

    assert f'Reported by {method}.' in out.getvalue()
