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


def _retirements(built: Command) -> list[str]:
    app = apps.get_app_config('testapp')
    return [
        operation
        for operation in built._retired_cascade_operations(app)
        if operation.startswith('# Soft Delete Related Rule retired')
    ]


def test_a_relaxed_key_is_retired_with_a_reverse_that_recreates_it(command):
    """``Encore.band`` is ``SET_NULL`` in the models and so calls for no rule, while the key
    stays recorded. The column is still on the model, so the reverse can rebuild the rule."""
    command.existing.soft_delete_related[('testapp_encore', 'testapp_band', None)] = 'abc'

    (operation,) = _retirements(command)

    assert 'retired on "testapp_encore" that is related to "testapp_band"!' in operation
    assert 'DROP RULE "soft_delete_related_testapp_encore" ON "testapp_band"' in operation
    # The reverse rebuilds the rule, column and all -- recovered from the relaxed field.
    assert 'CREATE OR REPLACE RULE "soft_delete_related_testapp_encore"' in operation
    assert '"band_id" = old."id"' in operation
    # Retired, so not also *named*: the two paths are exclusive on whether the tables map.
    assert command._unmapped_cascade_notes() == []


def test_the_via_form_keeps_its_own_header_and_column(command):
    """A second key between the same pair took the ``_via`` name, so its retirement has to
    drop that name -- and its column is in the key rather than recovered."""
    command.existing.soft_delete_related[('testapp_encore', 'testapp_band', 'band_id')] = 'abc'

    (operation,) = _retirements(command)

    assert 'via "band_id"!' in operation
    assert 'DROP RULE "soft_delete_related_testapp_encore_band_id" ON "testapp_band"' in operation


def test_a_key_whose_column_cannot_be_recovered_refuses_to_be_reversed(command):
    """The primary form's key never spelled its column, so a key whose field is gone entirely
    leaves nothing to rebuild. Refused loudly rather than reversed into a silent no-op."""
    command.existing.soft_delete_related[('testapp_genre', 'testapp_band', None)] = 'abc'

    (operation,) = _retirements(command)

    assert 'DROP RULE "soft_delete_related_testapp_genre" ON "testapp_band"' in operation
    assert 'RAISE EXCEPTION' in operation
    assert 'could not' in operation


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
    command.existing.soft_delete_related[('testapp_encore', 'testapp_band', None)] = 'abc'

    other = [
        operation
        for operation in command._retired_cascade_operations(apps.get_app_config('crossapp_owner'))
        if operation.startswith('# Soft Delete Related Rule retired')
    ]

    assert other == []
    assert len(_retirements(command)) == 1


def test_the_committed_history_records_and_then_forgets_the_retired_key():
    """The real corpus, end to end: 0048 wrote the rule while ``Encore.band`` was ``CASCADE``
    and 0050 retired it, so the scan reads the key as absent and the next run emits nothing."""
    existing = scan_existing_operations()

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
