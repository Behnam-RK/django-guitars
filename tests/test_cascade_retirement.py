"""Retirement of cascade soft-delete rules (2.9.0). Nothing in this kit dropped a rule when the
models stopped calling for it, so relaxing a ``CASCADE`` key left the rule live with ``--check``
green. These pin what is retired, what is only *named*, and why the difference is not a guess."""

import pytest
from django.apps import apps
from django.db import models
from django.db.models import CASCADE, SET_NULL
from django.test.utils import isolate_apps

from guitars.models import OwningForeignKey, SetarModel

from guitars.management.enforcement.command import Command
from guitars.management.enforcement.scanning import scan_existing_operations


@pytest.fixture
def command():
    """A command whose recorded cascade rules the test sets by hand, so a retirement can be
    arranged without a migration that would really drop one."""
    built = Command()
    built.existing.soft_delete_related.clear()
    return built


def _retirements(built: Command, app: str = 'testapp') -> list[str]:
    app = apps.get_app_config(app)
    return [
        operation
        for operation in built._retired_cascade_operations(app)
        if operation.startswith('# Soft Delete Related Rule retired')
    ]


def test_a_relaxed_key_is_retired_with_a_reverse_that_recreates_it(command):
    """``Refrain.band`` is ``SET_NULL`` in the models and so calls for no rule, while the key
    stays recorded. The column is still on the model, so the reverse can rebuild the rule."""
    command.existing.soft_delete_related[('testapp_callbacks', 'testapp_band', None)] = 'abc'

    (operation,) = _retirements(command)

    assert 'retired on "testapp_callbacks" that is related to "testapp_band"!' in operation
    # ``testapp_callbacks`` really was renamed (0051, 0053), so the drop takes every spelling
    # the rule may carry -- the unrenamed, bare-``DROP RULE`` branch is covered below.
    assert 'DROP RULE IF EXISTS "soft_delete_related_testapp_encore" ON "testapp_band"' in (
        operation
    )
    assert 'DROP RULE IF EXISTS "soft_delete_related_testapp_callbacks" ON "testapp_band"' in (
        operation
    )
    # The reverse rebuilds the rule, column and all -- recovered from the relaxed field.
    assert 'CREATE OR REPLACE RULE "soft_delete_related_testapp_callbacks"' in operation
    assert '"band_id" = old."id"' in operation
    # Retired, so not also *named*: the two paths are exclusive on whether the tables map.
    assert command._unmapped_cascade_notes() == []


def test_the_via_form_keeps_its_own_header_and_column(command):
    """A second key between the same pair took the ``_via`` name, so its retirement has to
    drop that name -- and its column is in the key rather than recovered."""
    command.existing.soft_delete_related[('testapp_callbacks', 'testapp_band', 'band_id')] = 'abc'

    (operation,) = _retirements(command)

    assert 'via "band_id"!' in operation
    assert 'DROP RULE IF EXISTS "soft_delete_related_testapp_callbacks_band_id" ON ' in operation


def test_an_unrenamed_key_is_dropped_by_name_without_if_exists(command):
    """The other branch. Nothing renamed ``testapp_album``, so the recorded key is evidence the
    rule is there under exactly that name and the bare form is right -- ``IF EXISTS`` would
    hide a database that had already diverged."""
    command.existing.soft_delete_related[('testapp_album', 'testapp_genre', None)] = 'abc'

    (operation,) = _retirements(command, app='testapp')

    assert 'DROP RULE "soft_delete_related_testapp_album" ON "testapp_genre"' in operation
    assert 'IF EXISTS' not in operation


def test_a_key_whose_column_cannot_be_recovered_refuses_to_be_reversed(command):
    """The primary form's key never spelled its column, so a key whose field is gone entirely
    leaves nothing to rebuild. Refused loudly rather than reversed into a silent no-op."""
    command.existing.soft_delete_related[('testapp_genre', 'testapp_band', None)] = 'abc'

    (operation,) = _retirements(command)

    assert 'DROP RULE "soft_delete_related_testapp_genre" ON "testapp_band"' in operation
    assert 'RAISE EXCEPTION' in operation
    # The rule and its table are named, so a consumer hitting this on a rollback can trace it
    # -- passed as RAISE arguments rather than interpolated, which a quote would break.
    assert 'soft_delete_related_testapp_genre' in operation.split('RAISE EXCEPTION')[1]
    assert "'testapp_band'" in operation


def test_a_key_naming_an_unmapped_table_is_named_rather_than_retired(command):
    """Positive evidence only. A table mapping to no local model is a deleted model on one
    reading and an app outside ``LOCAL_APPS`` on another, and following the wrong one destroys
    a live cascade -- so it is reported with the statement to run by hand."""
    command.existing.soft_delete_related[('shop_gone', 'testapp_band', None)] = 'abc'

    assert _retirements(command) == []

    (note,) = command._unmapped_cascade_notes()
    assert "maps to no local model" in note
    assert 'DROP RULE "soft_delete_related_shop_gone" ON "testapp_band"' in note


def test_a_key_the_models_still_call_for_is_left_alone(command):
    """The set difference is the whole mechanism: a live cascade is never retired."""
    command.existing.soft_delete_related[('testapp_album', 'testapp_band', None)] = 'abc'

    assert _retirements(command) == []
    assert command._unmapped_cascade_notes() == []


def test_the_retirement_is_hosted_by_the_app_owning_the_table_it_fires_on(command):
    """One table, one host. The rule lives on the parent's table, so the parent's app writes
    the drop -- two apps each emitting it would fail the second at ``migrate``."""
    command.existing.soft_delete_related[('testapp_callbacks', 'testapp_band', None)] = 'abc'

    other = [
        operation
        for operation in command._retired_cascade_operations(apps.get_app_config('crossapp_owner'))
        if operation.startswith('# Soft Delete Related Rule retired')
    ]

    assert other == []
    assert len(_retirements(command)) == 1


def test_the_committed_history_records_and_then_forgets_the_retired_key():
    """The real corpus, end to end: 0048 wrote the rule while the model was still ``Encore``
    with a ``CASCADE`` key, and 0050 retired it -- so the scan reads the key as absent under
    either spelling and the next run emits nothing. 0051 then renamed the model."""
    existing = scan_existing_operations()

    assert ('testapp_callbacks', 'testapp_band', None) not in existing.soft_delete_related
    assert ('testapp_encore', 'testapp_band', None) not in existing.soft_delete_related
    # And the app is flagged, so the file-level digest guard yields -- retirement makes an
    # operation set recur, which that guard otherwise assumes never happens.
    assert 'testapp' in existing.retirement_apps


def test_the_silent_sweep_meets_a_cycle_and_says_nothing():
    """``_cascade_key_maps`` walks every local model, including apps a scoped run was never
    asked about, so it sweeps with ``report=False``: their misconfigurations are not its to
    report, still less to fail ``--check`` over. The same shape with ``report=True`` warns."""

    @isolate_apps('tests.testapp')
    def _build(*, report):
        class Held(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Holder(SetarModel):
            owned = OwningForeignKey(Held, on_delete=SET_NULL, null=True, related_name='owners')
            parent = models.ForeignKey(Held, on_delete=CASCADE, related_name='children')

            class Meta:
                app_label = 'testapp'

        built = Command()
        built._skipped_rule_notes.clear()
        built.all_models = [Held, Holder]
        built.reverse_relations_mapping[Held] = {
            (Holder, Holder._meta.get_field('parent'), CASCADE)
        }
        built._cascade_candidates(Held, Held._meta.db_table, report=report)
        return built._skipped_rule_notes

    assert _build(report=False) == []
    assert 'cycle' in _build(report=True)[0]


def test_the_retirement_reaches_a_real_generation(command):
    """Wired, not merely written: every other test here calls ``_retired_cascade_operations``
    directly, so the branch's headline feature could be unhooked from ``_build_operations``
    without a single failure."""
    command.existing.soft_delete_related[('testapp_album', 'testapp_genre', None)] = 'abc'

    retired = [
        operation
        for operation in command._build_operations(apps.get_app_config('testapp'))
        if operation.startswith('# Soft Delete Related Rule retired')
    ]

    assert len(retired) == 1
    assert 'DROP RULE "soft_delete_related_testapp_album" ON "testapp_genre"' in retired[0]


def test_the_adopt_form_says_if_exists(command):
    """``--adopt`` is honest about not knowing what the database holds, so it is the one path
    that may assert ``IF EXISTS`` -- the swap the autofill retirement beside it already makes.
    Without it a rule already dropped by hand fails ``migrate``."""
    command.existing.soft_delete_related[('testapp_album', 'testapp_genre', None)] = 'abc'
    app = apps.get_app_config('testapp')

    plain = command._retired_cascade_operations(app)[0]
    adopted = command._retired_cascade_operations(app, adopt=True)[0]

    assert 'DROP RULE "soft_delete_related_testapp_album"' in plain
    assert 'DROP RULE IF EXISTS "soft_delete_related_testapp_album"' in adopted


def test_adopt_keeps_the_prior_name_drops_a_rename_added(command):
    """``--adopt`` swaps in an ``IF EXISTS`` drop of the current name, which would be strictly
    weaker than the plain path where a rename already made that path all-``IF EXISTS`` over
    every spelling. Where the old name is the live one, adopt has to keep them."""
    command.existing.renamed_tables['testapp_callbacks'] = ['testapp_encore']
    command.existing.soft_delete_related[('testapp_callbacks', 'testapp_band', None)] = 'abc'

    (operation,) = [
        candidate
        for candidate in command._retired_cascade_operations(
            apps.get_app_config('testapp'), adopt=True
        )
        if candidate.startswith('# Soft Delete Related Rule retired')
    ]

    assert 'DROP RULE IF EXISTS "soft_delete_related_testapp_encore"' in operation
    assert 'DROP RULE IF EXISTS "soft_delete_related_testapp_callbacks"' in operation


def test_a_scoped_run_names_the_retirement_it_cannot_write(command):
    """The dangerous half to leave silent. A creation gap merely delays a rule; a retirement
    gap leaves one live and still archiving rows, with ``--check`` green -- so it is named, the
    way the autofill family already names its own."""
    command.existing.soft_delete_related[('testapp_album', 'testapp_genre', None)] = 'abc'

    # The rule lives on ``testapp_genre``, hosted by testapp, and testapp is out of scope.
    (note,) = command._scoped_cascade_retirement_notes({'crossapp_owner'})

    assert "its app 'testapp' is not in this scoped run" in note
    assert 'goes on archiving rows' in note


def test_a_scoped_retirement_note_is_silent_where_the_owner_is_in_scope(command):
    """The note is for the gap only: with the owner's app in the run the retirement is written,
    so there is nothing to report."""
    command.existing.soft_delete_related[('testapp_album', 'testapp_genre', None)] = 'abc'

    assert command._scoped_cascade_retirement_notes({'testapp'}) == []
