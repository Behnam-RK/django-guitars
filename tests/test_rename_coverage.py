"""Rename-aware coverage (2.9.0). PostgreSQL carries a trigger, rule or policy with its table,
so after a rename the object is still there while the coverage asserting it is filed under the
old table name -- the new name then reads as uncovered and a plain ``CREATE`` collides."""

import pytest
from django.apps import apps
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import (
    AlterModelTable,
    RenameModel,
    SeparateDatabaseAndState,
)

from guitars.management.enforcement import graph, scanning
from guitars.management.enforcement.command import Command
from guitars.management.enforcement.scanning import scan_existing_operations


@pytest.fixture
def loader():
    return MigrationLoader(None, ignore_no_migrations=True)


def test_a_rename_model_is_read_off_the_migration_state(loader):
    """Chained: 0051 renamed the model, 0053 gave it a ``db_table``, and 0055 renamed it again
    without moving the table. The answer is the name coverage was first recorded under."""
    assert graph.renamed_tables(loader, 'testapp') == {'testapp_callbacks': 'testapp_encore'}


def test_an_app_that_never_renamed_anything_answers_empty(loader):
    """And cheaply: the state replay only runs for apps whose history has a renaming
    operation, which is almost none of them."""
    assert graph.renamed_tables(loader, 'crossapp_owner') == {}


def test_coverage_recorded_under_the_old_name_is_read_under_the_new_one():
    """The translation itself. Both objects were recorded against ``testapp_encore`` in 0048,
    and the scan has to answer for the name in use now."""
    existing = scan_existing_operations()

    assert 'testapp_callbacks' in existing.triggers
    assert 'testapp_callbacks' in existing.soft_deletes
    assert 'testapp_encore' not in existing.triggers
    assert existing.renamed_tables['testapp_callbacks'] == 'testapp_encore'


def test_the_renamed_table_gets_the_replace_form_not_a_plain_create():
    """The whole point. A plain ``CREATE TRIGGER`` fails with *already exists* against the one
    PostgreSQL carried over; the replace form drops it first."""
    # The state 0052 was generated *from*: coverage translated onto the new name, carrying the
    # digest the old name's operation recorded, which the new table's SQL cannot match.
    command = Command()
    command.existing.triggers['testapp_callbacks'] = 'stale00000000'

    (trigger,) = [
        operation
        for operation in command._build_operations(apps.get_app_config('testapp'))
        if operation.startswith('# Updated at Trigger on "testapp_callbacks"')
    ]

    assert 'DROP TRIGGER updated_at_trigger ON "testapp_callbacks"' in trigger
    assert trigger.index('DROP TRIGGER') < trigger.index('CREATE TRIGGER')


def test_a_second_run_emits_nothing_for_the_renamed_table():
    """Idempotent: once the replace form is recorded under the new name, the digests agree."""
    command = Command()

    assert [
        operation
        for operation in command._build_operations(apps.get_app_config('testapp'))
        if 'testapp_callbacks' in operation
    ] == []


# --- The families whose object name embeds a table ------------------------------------------


def _self_cascade_for(renamed_from: str | None) -> str:
    """The self-cascade operation for ``testapp_setlist``, optionally pretending its table was
    renamed and its coverage translated -- which is what the scan leaves behind."""
    command = Command()
    if renamed_from is not None:
        command.existing.renamed_tables['testapp_setlist'] = renamed_from
    command.existing.soft_delete_self_cascade[('testapp_setlist', 'parent_id')] = 'stale00000000'
    (operation,) = [
        candidate
        for candidate in command._build_operations(apps.get_app_config('testapp'))
        if candidate.startswith('# Soft Delete Self Cascade Trigger on "testapp_setlist"')
    ]
    return operation


def test_a_renamed_table_drops_the_trigger_under_its_old_name():
    """The name folds the table in, so the carried-over trigger answers to the *old* one.
    Dropping the new name would fail on a name nothing has, which is a broken ``migrate``."""
    operation = _self_cascade_for('testapp_oldtree')

    assert 'DROP TRIGGER IF EXISTS "soft_delete_self_cascade_15_testapp_oldtree_9_parent_id"' in (
        operation
    )
    assert 'DROP FUNCTION IF EXISTS "soft_delete_self_cascade_15_testapp_oldtree_9_parent_id"' in (
        operation
    )
    assert 'CREATE TRIGGER "soft_delete_self_cascade_15_testapp_setlist_9_parent_id"' in operation


def test_an_unrenamed_table_keeps_the_plain_replace_form():
    """The other side of the branch: no rename, no ``IF EXISTS``, and the drop names the
    object this run is about to replace."""
    operation = _self_cascade_for(None)

    assert 'DROP TRIGGER IF EXISTS' not in operation
    assert 'DROP TRIGGER "soft_delete_self_cascade_15_testapp_setlist_9_parent_id"' in operation


def test_a_renamed_owned_sweep_drops_both_of_its_tables_old_names():
    """The sweep's name folds in **two** tables, either of which a rename can have moved."""
    command = Command()
    command.existing.renamed_tables['testapp_rack'] = 'testapp_oldrack'
    key = ('testapp_riser', 'testapp_rack', 'riser_id')
    command.existing.soft_delete_owned_sweep[key] = 'stale00000000'

    (operation,) = [
        candidate
        for candidate in command._build_operations(apps.get_app_config('testapp'))
        if candidate.startswith('# Soft Delete Owned Sweep on "testapp_riser"')
    ]

    assert 'DROP TRIGGER IF EXISTS "soft_delete_owned_sweep_15_testapp_oldrack' in operation
    assert 'CREATE TRIGGER "soft_delete_owned_sweep_12_testapp_rack' in operation


def test_a_renamed_cascade_child_drops_the_rule_it_left_behind():
    """A cascade rule is named after the child's table, so a rename leaves the carried-over
    rule live beside the new one -- both cascading, and nothing later retires either."""
    command = Command()
    command.existing.renamed_tables['testapp_album'] = 'testapp_oldalbum'
    command.existing.soft_delete_related[('testapp_album', 'testapp_band', None)] = 'stale00000'

    (operation,) = [
        candidate
        for candidate in command._build_operations(apps.get_app_config('testapp'))
        if candidate.startswith('# Soft Delete Related Rule on "testapp_album"')
    ]

    assert 'DROP RULE IF EXISTS "soft_delete_related_testapp_oldalbum" ON "testapp_band"' in (
        operation
    )
    assert 'CREATE OR REPLACE RULE "soft_delete_related_testapp_album"' in operation


def test_a_rename_wrapped_in_separate_database_and_state_is_still_seen():
    """The standard idiom for a change the database already has, and what a hand-tuned squash
    carries -- read past exactly as the object-reference resolver reads past it."""
    wrapped = SeparateDatabaseAndState(
        database_operations=[], state_operations=[RenameModel(old_name='Old', new_name='New')]
    )

    assert graph._renaming(wrapped) == [('old', 'new')]
    assert graph._renaming(AlterModelTable(name='Thing', table='t')) == [('thing', 'thing')]
    assert graph._renaming(RenameModel(old_name='A', new_name='B')) == [('a', 'b')]


def test_the_translation_handles_a_set_as_well_as_a_mapping():
    """Tenant policies are recorded as a bare set of tables rather than a keyed mapping, so
    the translation has to move a member rather than re-key an entry."""
    policies = {'testapp_old', 'testapp_untouched'}

    scanning._translate_renamed({'testapp_new': 'testapp_old'}, policies)

    assert policies == {'testapp_new', 'testapp_untouched'}


def test_a_key_already_recorded_under_the_new_name_wins():
    """A record written *after* the rename is the current answer, so a stale one filed under
    the old name never displaces it."""
    recorded = {'testapp_new': 'current', 'testapp_old': 'stale'}

    scanning._translate_renamed({'testapp_new': 'testapp_old'}, recorded)

    assert recorded == {'testapp_new': 'current'}


def test_retiring_a_rule_on_a_renamed_table_drops_both_names():
    """A retirement names the rule after the child's table, but a rename left the live one
    under the old name -- and ``DROP RULE`` has no ``IF EXISTS``, so the wrong name fails
    ``migrate``. Which is live depends on ordering, so both are dropped, both ``IF EXISTS``."""
    command = Command()
    command.existing.soft_delete_related.clear()
    command.existing.renamed_tables['testapp_callbacks'] = 'testapp_encore'
    command.existing.soft_delete_related[('testapp_callbacks', 'testapp_band', None)] = 'abc'

    (operation,) = [
        candidate
        for candidate in command._retired_cascade_operations(apps.get_app_config('testapp'))
        if candidate.startswith('# Soft Delete Related Rule retired')
    ]

    assert 'DROP RULE IF EXISTS "soft_delete_related_testapp_encore" ON "testapp_band"' in (
        operation
    )
    assert 'DROP RULE IF EXISTS "soft_delete_related_testapp_callbacks" ON "testapp_band"' in (
        operation
    )
    # And never the bare form, which is what fails on a name nothing has.
    assert 'DROP RULE "soft_delete_related' not in operation
    # Retired, so not also *named*: both its tables map.
    assert command._unmapped_cascade_notes() == []


def test_adopt_after_a_rename_drops_the_old_name_too():
    """``--adopt`` is the path where the database's state is unknown, so it drops the current
    name ``IF EXISTS``. After a rename the object may equally be under the old one, and
    dropping only the new leaves the carried-over trigger live beside the new one."""
    command = Command()
    command.existing.renamed_tables['testapp_setlist'] = 'testapp_oldtree'
    command.existing.soft_delete_self_cascade[('testapp_setlist', 'parent_id')] = 'stale00000000'

    (operation,) = [
        candidate
        for candidate in command._build_operations(apps.get_app_config('testapp'), adopt=True)
        if candidate.startswith('# Soft Delete Self Cascade Trigger on "testapp_setlist"')
    ]

    assert 'DROP TRIGGER IF EXISTS "soft_delete_self_cascade_15_testapp_oldtree_9_parent_id"' in (
        operation
    )
    assert 'DROP TRIGGER IF EXISTS "soft_delete_self_cascade_15_testapp_setlist_9_parent_id"' in (
        operation
    )
