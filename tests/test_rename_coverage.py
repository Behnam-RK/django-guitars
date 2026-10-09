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

from tests.conftest import clear_cascade_coverage

from guitars.management.enforcement import graph, scanning
from guitars.management.enforcement.command import Command
from guitars.management.enforcement.scanning import scan_existing_operations


@pytest.fixture
def loader():
    return MigrationLoader(None, ignore_no_migrations=True)


def test_a_rename_model_is_read_off_the_migration_state(loader):
    """Chained: 0051 renamed the model, 0053 gave it a ``db_table``, and 0055 renamed it again
    without moving the table. The answer is the name coverage was first recorded under."""
    assert graph.renamed_tables(loader, 'testapp') == {
        'testapp_callbacks': ['testapp_encore', 'testapp_callback']
    }


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
    assert existing.renamed_tables['testapp_callbacks'] == [
        'testapp_encore',
        'testapp_callback',
    ]


def test_the_renamed_table_gets_the_replace_form_not_a_plain_create():
    """The whole point. A plain ``CREATE TRIGGER`` fails with *already exists* against the one
    PostgreSQL carried over; the replace form replaces it."""
    # The state 0052 was generated *from*: coverage translated onto the new name, carrying the
    # digest the old name's operation recorded, which the new table's SQL cannot match.
    command = Command()
    command.existing.triggers['testapp_callbacks'] = 'stale00000000'

    (trigger,) = [
        operation
        for operation in command._build_operations(apps.get_app_config('testapp'))
        if operation.startswith('# Updated at Trigger on "testapp_callbacks"')
    ]

    assert 'CREATE OR REPLACE TRIGGER updated_at_trigger' in trigger
    assert 'CREATE TRIGGER updated_at_trigger' not in trigger


def test_a_second_run_emits_nothing_for_the_renamed_table():
    """Idempotent: once the replace form is recorded under the new name, the digests agree."""
    command = Command()

    assert [
        operation
        for operation in command._build_operations(apps.get_app_config('testapp'))
        if 'testapp_callbacks' in operation
    ] == []


# --- The families whose object name embeds a table ------------------------------------------


def _self_cascade_retirement_for(renamed_from: list[str] | None) -> str:
    """The retirement of ``testapp_setlist``'s self-cascade trigger (superseded by an arm of the
    owner's, ADR 0042), optionally pretending its table was renamed and its coverage translated
    -- which is what the scan leaves behind."""
    command = Command()
    if renamed_from is not None:
        command.existing.renamed_tables['testapp_setlist'] = renamed_from
    command.existing.soft_delete_self_cascade[('testapp_setlist', 'parent_id')] = 'stale00000000'
    (operation,) = [
        candidate
        for candidate in command._retired_trigger_operations(apps.get_app_config('testapp'))
        if candidate.startswith('# Soft Delete Self Cascade Trigger retired on "testapp_setlist"')
    ]
    return operation


def test_a_renamed_table_retires_the_trigger_under_its_old_name():
    """The name folds the table in, so the carried-over trigger answers to the *old* one.
    Dropping the new name alone would leave it live beside the arm that supersedes it."""
    operation = _self_cascade_retirement_for(['testapp_oldtree'])

    old = 'soft_delete_self_cascade_15_testapp_oldtree_9_parent_id'
    assert f'DROP TRIGGER IF EXISTS "{old}"' in operation
    assert f'DROP FUNCTION IF EXISTS "{old}"' in operation
    # And the current name too: which spelling is live depends on when a generation last ran.
    assert 'DROP TRIGGER IF EXISTS "soft_delete_self_cascade_15_testapp_setlist' in operation
    # Its reverse rebuilds it under the current name, as the owner's arm is what supersedes it.
    assert 'CREATE TRIGGER "soft_delete_self_cascade_15_testapp_setlist_9_parent_id"' in operation


def test_a_renamed_owned_sweep_drops_both_of_its_tables_old_names():
    """The sweep's name folds in **two** tables, either of which a rename can have moved."""
    command = Command()
    command.existing.renamed_tables['testapp_rack'] = ['testapp_oldrack']
    key = ('testapp_riser', 'testapp_rack', 'riser_id')
    command.existing.soft_delete_owned_sweep[key] = 'stale00000000'

    (operation,) = [
        candidate
        for candidate in command._build_operations(apps.get_app_config('testapp'))
        if candidate.startswith('# Soft Delete Owned Sweep on "testapp_riser"')
    ]

    assert 'DROP TRIGGER IF EXISTS "soft_delete_owned_sweep_15_testapp_oldrack' in operation
    assert 'CREATE TRIGGER "soft_delete_owned_sweep_12_testapp_rack' in operation


def test_a_renamed_owned_target_drops_the_rule_it_left_behind():
    """The owned rule's name embeds the *dependent's* table, so a rename there leaves the
    carried-over rule live beside the new one -- both cascading, neither retired, and the stale
    one frozen at the old predicate. Claimed by `docs/migrations.md` and the changelog."""
    command = Command()
    command.existing.renamed_tables['testapp_riser'] = ['testapp_oldriser']
    key = ('testapp_riser', 'testapp_rack', 'riser_id')
    command.existing.soft_delete_owned[key] = 'stale00000000'

    (operation,) = [
        candidate
        for candidate in command._build_operations(apps.get_app_config('testapp'))
        if candidate.startswith('# Soft Delete Owned Rule retired on "testapp_riser"')
    ]

    # Retired since 2.19.0 (#80): over every name the dependent's table held, and the reverse
    # rebuilds the rule under the current one.
    assert 'DROP RULE IF EXISTS "soft_delete_owned_16_testapp_oldriser_8_riser_id"' in operation
    assert 'DROP RULE IF EXISTS "soft_delete_owned_13_testapp_riser_8_riser_id"' in operation
    assert 'CREATE OR REPLACE RULE "soft_delete_owned_13_testapp_riser_8_riser_id"' in operation


def test_a_renamed_cascade_child_drops_the_rule_it_left_behind():
    """A cascade rule is named after the child's table, so a rename leaves the carried-over
    rule live beside the new one -- both cascading, and nothing later retires either."""
    command = Command()
    command.existing.renamed_tables['testapp_album'] = ['testapp_oldalbum']
    command.existing.soft_delete_related[('testapp_album', 'testapp_band', None)] = 'stale00000'

    (operation,) = [
        candidate
        for candidate in command._build_operations(apps.get_app_config('testapp'))
        if candidate.startswith('# Soft Delete Related Rule retired on "testapp_album"')
    ]

    # Retired since 2.19.0 (#80), over both names; the reverse rebuilds it under the current one.
    assert 'DROP RULE IF EXISTS "soft_delete_related_testapp_oldalbum" ON "testapp_band"' in (
        operation
    )
    assert 'DROP RULE IF EXISTS "soft_delete_related_testapp_album" ON "testapp_band"' in (
        operation
    )
    assert 'CREATE OR REPLACE RULE "soft_delete_related_testapp_album"' in operation


def _per_key_revive_retirement(command):
    (operation,) = [
        candidate
        for candidate in command._build_operations(apps.get_app_config('testapp'))
        if candidate.startswith(
            '# Soft Delete Revive Trigger retired on "testapp_album" that is related to '
            '"testapp_band"!'
        )
    ]
    return operation.split('reverse_sql')[0]


def test_a_renamed_cascade_childs_per_key_revive_is_retired_under_its_old_name_too():
    """2.16.0 retires every per-key revive (#70). Its name embeds the child's table, sized, so a
    rename stranded it under the old spelling, which the retirement must drop as well."""
    command = Command()
    command.existing.renamed_tables['testapp_album'] = ['testapp_oldalbum']
    command.existing.soft_delete_revive[('testapp_album', 'testapp_band', None)] = 'stale00000'

    forward = _per_key_revive_retirement(command)

    for name in (
        'soft_delete_revive_12_testapp_band_16_testapp_oldalbum',
        'soft_delete_revive_12_testapp_band_13_testapp_album',
    ):
        assert f'DROP TRIGGER IF EXISTS "{name}" ON "testapp_band"' in forward
        # The function goes with it: a trigger and its function share one name here.
        assert f'DROP FUNCTION IF EXISTS "{name}"()' in forward


def test_a_renamed_owners_per_key_revive_is_retired_under_its_old_name_too():
    """The per-key name folds in the owner as well, and a renamed owner carries the trigger with
    it under the old spelling: asking about the related table alone left that pair live."""
    command = Command()
    command.existing.renamed_tables['testapp_band'] = ['testapp_oldband']
    command.existing.soft_delete_revive[('testapp_album', 'testapp_band', None)] = 'stale00000'

    forward = _per_key_revive_retirement(command)

    assert 'DROP TRIGGER IF EXISTS "soft_delete_revive_15_testapp_oldband_13_testapp_album"' in (
        forward
    )
    assert 'DROP FUNCTION IF EXISTS "soft_delete_revive_15_testapp_oldband_13_testapp_album"()' in (
        forward
    )


def _owner_revive(command):
    (operation,) = [
        candidate
        for candidate in command._build_operations(apps.get_app_config('testapp'))
        if candidate.startswith('# Soft Delete Cascade Trigger on "testapp_band" table!')
    ]
    return operation.split('reverse_sql')[0]


def test_a_renamed_owners_revive_drops_its_old_name():
    """The per-owner name spells the owner, so its rename strands the old trigger and function,
    both running the same arms on every update of that table."""
    command = Command()
    command.existing.renamed_tables['testapp_band'] = ['testapp_oldband']
    command.existing.soft_delete_cascade_owner[('testapp_band',)] = 'stale00000'

    forward = _owner_revive(command)

    assert 'DROP TRIGGER IF EXISTS "soft_delete_cascade_on_15_testapp_oldband"' in forward
    assert 'DROP FUNCTION IF EXISTS "soft_delete_cascade_on_15_testapp_oldband"()' in forward
    assert 'CREATE OR REPLACE TRIGGER "soft_delete_cascade_on_12_testapp_band"' in forward


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

    scanning._move_renamed('testapp_old', 'testapp_new', policies)

    assert policies == {'testapp_new', 'testapp_untouched'}


def test_the_move_runs_at_the_rename_so_the_later_record_wins():
    """Moving where the rename happens is what makes ordering right without a special case: a
    record written afterwards is simply recorded later. See the cycle test below."""
    recorded = {'testapp_old': 'written_before'}

    scanning._move_renamed('testapp_old', 'testapp_new', recorded)

    assert recorded == {'testapp_new': 'written_before'}


def test_retiring_a_rule_on_a_renamed_table_drops_both_names():
    """A retirement names the rule after the child's table, but a rename left the live one
    under the old name -- and ``DROP RULE`` has no ``IF EXISTS``, so the wrong name fails
    ``migrate``. Which is live depends on ordering, so both are dropped, both ``IF EXISTS``."""
    command = Command()
    clear_cascade_coverage(command)
    command.existing.renamed_tables['testapp_callbacks'] = ['testapp_encore']
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


def test_a_prior_name_back_in_use_as_a_live_table_is_never_dropped():
    """A freed name retaken by a later ``CreateModel`` must not be dropped -- but the chain
    keeps it, because the scan needs every spelling to translate. Filter the chain instead and
    the renamed table reads as uncovered, so the plain ``CREATE`` collides after all."""
    command = Command()
    command.existing.renamed_tables['testapp_setlist'] = ['testapp_gone', 'testapp_genre']

    # ``testapp_genre`` is a live table, so no drop names it; ``testapp_gone`` is not.
    assert command._prior_names('testapp_setlist') == ['testapp_gone']
    # And the chain itself is untouched, so the translation still has every spelling.
    assert command.existing.renamed_tables['testapp_setlist'] == ['testapp_gone', 'testapp_genre']


def test_a_tuple_key_is_re_keyed_across_a_rename():
    """The cascade, owned, sweep, self-cascade and autofill families are all keyed on tuples,
    and the corpus cannot prove they translate -- its one renamed table had its cascade key
    retired before the rename."""
    recorded = {
        ('testapp_old', 'testapp_owner', None): 'a',
        ('testapp_dep', 'testapp_old', 'fk_id'): 'b',
        ('testapp_untouched', 'testapp_owner', None): 'c',
    }

    scanning._move_renamed('testapp_old', 'testapp_new', recorded)

    assert recorded == {
        ('testapp_new', 'testapp_owner', None): 'a',
        ('testapp_dep', 'testapp_new', 'fk_id'): 'b',
        ('testapp_untouched', 'testapp_owner', None): 'c',
    }


def test_the_sweep_drops_the_dependents_prior_names_as_well_as_the_owners():
    """The sweep's name folds in two tables, so a rename of *either* leaves an object behind.
    The owner side was covered; this is the dependent side."""
    command = Command()
    command.existing.renamed_tables['testapp_riser'] = ['testapp_oldriser']
    command.existing.soft_delete_owned_sweep[('testapp_riser', 'testapp_rack', 'riser_id')] = 'x'

    (operation,) = [
        candidate
        for candidate in command._build_operations(apps.get_app_config('testapp'))
        if candidate.startswith('# Soft Delete Owned Sweep on "testapp_riser"')
    ]

    assert 'DROP TRIGGER IF EXISTS "soft_delete_owned_sweep_12_testapp_rack_16_testapp_o' in (
        operation
    )


def test_a_retirement_subtracts_under_every_spelling_of_a_renamed_table(monkeypatch):
    """A retirement names the table as spelled *now*, while the key it must subtract may still
    be filed under a name a rename left behind. Without the prior spellings the post-pass moves
    the old key back over the hole and a dropped object reads as covered, ``--check`` green."""
    real = graph.retired_enforcement
    monkeypatch.setattr(
        scanning,
        'retired_enforcement',
        lambda ldr, app: {'0056_rename_encore_deleted_at_refrain_deleted_at_and_more': [
            ('testapp_callbacks', None)
        ]}
        if app == 'testapp'
        else real(ldr, app),
    )

    existing = scan_existing_operations()

    # Recorded in 0048 under `testapp_encore`; the retirement names `testapp_callbacks`.
    assert 'testapp_callbacks' not in existing.soft_deletes
    assert 'testapp_encore' not in existing.soft_deletes


def test_the_required_key_sweep_runs_without_reporting(monkeypatch):
    """``_cascade_key_maps`` walks every local model, including apps a scoped run was never
    asked about. It passes ``report=False`` so their misconfigurations are not reported here --
    asserted at the call site, since testing the parameter alone leaves the wiring free."""
    command = Command()
    seen: list[bool] = []
    real = Command._cascade_candidates

    def _spy(self, model, owner_table, *, report=True):
        seen.append(report)
        return real(self, model, owner_table, report=report)

    monkeypatch.setattr(Command, '_cascade_candidates', _spy)
    command._cascade_key_maps()

    assert seen and not any(seen)


def test_liveness_is_asked_of_the_whole_registry_not_just_local_apps():
    """A name retaken by a model outside ``LOCAL_APPS`` is no less live, and dropping its
    objects no less wrong -- ``_table_app_labels`` would have called it dead."""
    command = Command()
    command.existing.renamed_tables['testapp_setlist'] = ['django_content_type', 'testapp_gone']

    assert command._prior_names('testapp_setlist') == ['testapp_gone']


def test_a_freed_name_retaken_by_a_live_table_keeps_its_own_coverage():
    """Round 2 filtered the *drop* side. The translation moved the old key unconditionally and
    deleted it, so a header written for the retaken name **after** the rename was destroyed --
    which never converges, the generator re-emitting a colliding CREATE on every run."""
    existing = scan_existing_operations()

    # ``testapp_setlist`` is live, so the walk skipped any move that would have taken its
    # coverage -- the guard sits on the move as well as on the drop.
    assert 'testapp_setlist' in existing.soft_deletes
    assert 'testapp_setlist' in existing.triggers


def test_an_ambiguous_retired_column_refuses_rather_than_guessing():
    """Which of two foreign keys to one owner the rule read is unknowable once the key is
    relaxed, so the reverse refuses. Guessing rebuilds the rule on a column it never read --
    ``Album`` holds both ``band_id`` and ``producer_id`` to ``testapp_band``."""
    from tests.testapp.models import Album  # noqa: PLC0415 - a fixture, not a dependency

    command = Command()
    key = ('testapp_album', 'testapp_band', None)

    assert command._retired_cascade_column(key, {'testapp_album': Album}) is None
    # One key to the owner is unambiguous, and is the relaxed-field case the reverse exists for.
    assert command._retired_cascade_column(
        ('testapp_refrain', 'testapp_band', None), {'testapp_refrain': _refrain()}
    ) == 'band_id'


def _refrain():
    from tests.testapp.models import Refrain  # noqa: PLC0415 - a fixture, not a dependency

    return Refrain


def test_a_rename_cycle_keeps_the_coverage_written_inside_it():
    """``A -> B`` and back leaves two entries filed under ``A``, one from before the cycle and
    one from inside it. A post-pass could not tell which was newer, so the stale one won and a
    database still holding ``B``-named objects read as covered, ``--check`` green."""
    recorded = {'a': 'pre_cycle'}

    scanning._move_renamed('a', 'b', recorded)
    recorded['b'] = 'written_while_it_was_b'
    scanning._move_renamed('b', 'a', recorded)

    assert recorded == {'a': 'written_while_it_was_b'}


def test_a_record_written_after_a_rename_still_wins():
    """The other direction, which the cycle fix must not break: moving at the rename means a
    record written afterwards is simply recorded later, and needs no special case."""
    recorded = {'old': 'stale'}

    scanning._move_renamed('old', 'new', recorded)
    recorded['new'] = 'written_after_the_rename'

    assert recorded == {'new': 'written_after_the_rename'}


def test_the_move_handles_tuple_keys_without_shredding_string_ones():
    """A string is iterable, so the tuple branch would turn a table-keyed entry into a tuple of
    characters -- which silently emptied every policy family the first time."""
    recorded = {('old', 'owner', 'fk'): 'x', 'old': 'y', 'untouched': 'z'}

    scanning._move_renamed('old', 'new', recorded)

    assert recorded == {('new', 'owner', 'fk'): 'x', 'new': 'y', 'untouched': 'z'}


def test_a_freed_name_already_retaken_is_not_moved_in_the_walk():
    """The liveness guard applies to the move as well as the drop: a name another model now
    owns keeps its own coverage."""
    command = Command()
    existing = scan_existing_operations()

    # ``testapp_setlist`` is live, so nothing in the corpus could have moved its coverage away.
    assert 'testapp_setlist' in existing.soft_deletes
    assert command._prior_names('testapp_setlist') == []


def test_the_walk_skips_a_move_whose_source_is_a_live_table(monkeypatch):
    """The liveness guard sits on the move as well as the drop: a freed name another model now
    owns keeps its own coverage, rather than having it carried onto the renamed table."""
    real = graph.renames_by_migration
    monkeypatch.setattr(
        scanning,
        'renames_by_migration',
        lambda ldr, app: {'0051_rename_encore_to_callback': [('testapp_setlist', 'testapp_zzz')]}
        if app == 'testapp'
        else real(ldr, app),
    )

    existing = scan_existing_operations()

    assert 'testapp_setlist' in existing.soft_deletes
    assert 'testapp_zzz' not in existing.soft_deletes


def test_a_graph_node_with_no_disk_migration_is_skipped_by_the_rename_walk(loader):
    """A squash replaces its nodes, so the graph can name a migration no file backs."""
    missing = next(name for (app, name) in loader.disk_migrations if app == 'testapp')
    del loader.disk_migrations[('testapp', missing)]

    assert missing not in graph.renames_by_migration(loader, 'testapp')


def test_a_revive_re_emission_replaces_its_trigger_without_dropping_it():
    """A bare ``CREATE TRIGGER`` aborts over a live trigger, and ``DROP`` first holds ACCESS
    EXCLUSIVE on every owner table of the app until the migration commits;
    ``CREATE OR REPLACE TRIGGER`` (PG 14) does neither (#80, ADR 0039)."""
    command = Command()
    command.existing.soft_delete_cascade_owner[('testapp_band',)] = 'stale00000'

    forward = _owner_revive(command)

    assert 'CREATE OR REPLACE TRIGGER "soft_delete_cascade_on_12_testapp_band"' in forward
    assert 'DROP TRIGGER' not in forward


class TestAModelHandedToAnotherApp:
    """A scan walks one app's files at a time, so another app's files may land under the new
    name before or after the move (#66). Read through the cross-app route, the coverage ends
    where the same-app rename put it."""

    @staticmethod
    def _as_a_move(monkeypatch, *, at=None):
        """Read ``testapp``'s own renames as cross-app moves, optionally all at migration *at*."""
        loader = MigrationLoader(None, ignore_no_migrations=True)
        real = graph.renames_by_migration
        moves = real(loader, 'testapp')
        monkeypatch.setattr(
            scanning,
            'renames_by_migration',
            lambda loader, label: {} if label == 'testapp' else real(loader, label),
        )
        handed = {at: [pair for pairs in moves.values() for pair in pairs]} if at else moves
        monkeypatch.setattr(
            scanning,
            'moves_between_apps_by_migration',
            lambda loader, label: handed if label == 'testapp' else {},
        )

    def test_moved_where_it_happened_it_lands_as_a_rename_does(self, monkeypatch):
        baseline = scan_existing_operations()
        self._as_a_move(monkeypatch)

        existing = scan_existing_operations()

        assert existing.triggers == baseline.triggers
        assert existing.soft_deletes == baseline.soft_deletes
        assert set(existing.soft_delete_related_dependencies) == set(
            baseline.soft_delete_related_dependencies
        )

    @pytest.mark.parametrize(
        'at', ['0001_initial', '0078_retirement_host_after_catalog'], ids=['before', 'after']
    )
    def test_a_name_another_model_holds_keeps_its_own_coverage(self, monkeypatch, at):
        """A freed name a live model retook is not the moved table's to take with it: neither
        at the move, once its coverage is recorded, nor when the walk is done."""
        baseline = scan_existing_operations()
        monkeypatch.setattr(
            scanning,
            'moves_between_apps_by_migration',
            lambda loader, label: {at: [('testapp_catalog', 'testapp_elsewhere')]}
            if label == 'testapp'
            else {},
        )

        existing = scan_existing_operations()

        assert 'testapp_catalog' in baseline.triggers
        assert 'testapp_catalog' in existing.triggers
        assert 'testapp_elsewhere' not in existing.triggers

    def test_a_move_never_overwrites_and_a_rename_always_does(self, monkeypatch):
        """Chronology holds inside one app's walk and not across two, so only the cross-app
        route keeps what the destination already has."""
        flags = []
        real = scanning._move_renamed
        monkeypatch.setattr(
            scanning,
            '_move_renamed',
            lambda old, new, recorded, **kwargs: (
                flags.append(kwargs.get('keep_existing', False)) or real(old, new, recorded, **kwargs)
            ),
        )
        scan_existing_operations()
        as_renames = set(flags)
        flags.clear()
        self._as_a_move(monkeypatch)

        scan_existing_operations()

        assert as_renames == {False}
        assert set(flags) == {True}

    def test_what_another_app_filed_after_the_move_is_carried_to_the_end_of_the_chain(
        self, monkeypatch
    ):
        """The move is read before ``testapp``'s own files, which then record the old name: the
        cross-app hop is followed through the renames the app made after it."""
        baseline = scan_existing_operations()
        self._as_a_move(monkeypatch, at='0001_initial')

        existing = scan_existing_operations()

        assert 'testapp_callbacks' in existing.triggers
        assert 'testapp_encore' not in existing.triggers
        assert existing.triggers == baseline.triggers
        assert existing.soft_deletes == baseline.soft_deletes
        key = ('testapp_callbacks', 'testapp_band', None)
        assert key in baseline.soft_delete_related_dependencies
        assert key in existing.soft_delete_related_dependencies
        assert ('testapp_encore', 'testapp_band', None) not in existing.soft_delete_related_dependencies

class TestMovingEntriesWithoutOverwriting:
    def test_an_entry_already_under_the_new_name_is_kept(self):
        recorded = {('old', 'owner', None): 'older', ('new', 'owner', None): 'newer'}

        scanning._move_renamed('old', 'new', recorded, keep_existing=True)

        assert recorded == {('new', 'owner', None): 'newer'}

    def test_dependency_lists_are_merged_not_dropped(self):
        recorded = {'old': [('a', '1')], 'new': [('b', '2')]}

        scanning._move_renamed('old', 'new', recorded, keep_existing=True)

        assert recorded == {'new': [('a', '1'), ('b', '2')]}

    def test_a_gap_is_filled(self):
        recorded = {('old', 'owner', None): 'older'}

        scanning._move_renamed('old', 'new', recorded, keep_existing=True)

        assert recorded == {('new', 'owner', None): 'older'}

    def test_a_rename_still_overwrites(self):
        """Same app, one chronological walk: whatever is under the destination predates it."""
        recorded = {('old', 'owner', None): 'newer', ('new', 'owner', None): 'older'}

        scanning._move_renamed('old', 'new', recorded)

        assert recorded == {('new', 'owner', None): 'newer'}


class TestFollowingAChainOfMoves:
    def test_two_moves_end_at_the_last(self):
        ends = scanning._chain_ends({'a': 'b', 'b': 'c'}, {})

        assert ends == {'a': 'c', 'b': 'c'}

    def test_a_move_then_a_rename_ends_at_the_rename(self):
        ends = scanning._chain_ends({'a': 'b'}, {'b': 'c'})

        assert ends == {'a': 'c', 'b': 'c'}

    def test_a_cycle_ends_the_walk_rather_than_looping(self):
        assert scanning._chain_ends({'a': 'b', 'b': 'a'}, {}) == {'a': 'b', 'b': 'a'}

    def test_a_rename_alone_is_not_this_chain(self):
        assert scanning._chain_ends({}, {'a': 'b'}) == {}


class TestChainsJoinedAcrossApps:
    def test_a_chain_is_extended_by_the_chains_of_the_names_in_it(self):
        joined = scanning._join_chains({'c': ['b'], 'b': ['a']})

        assert joined == {'c': ['a', 'b']}

    def test_a_chain_already_whole_is_left_out(self):
        assert scanning._join_chains({'b': ['a'], 'c': ['a', 'b']}) == {}

    def test_a_cycle_does_not_loop(self):
        assert scanning._join_chains({'a': ['b'], 'b': ['a']}) == {}

    def test_the_scan_answers_with_the_whole_history(self, monkeypatch):
        """``testapp_zzz`` was a rename of the table ``testapp_callbacks`` came to be, in
        another app's history: its prior names include everything that table held."""
        real = graph.renamed_tables
        monkeypatch.setattr(
            scanning,
            'renamed_tables',
            lambda loader, label: (
                {**real(loader, label), 'testapp_zzz': ['testapp_callbacks']}
                if label == 'testapp'
                else real(loader, label)
            ),
        )

        existing = scan_existing_operations()

        assert existing.renamed_tables['testapp_zzz'] == [
            'testapp_encore',
            'testapp_callback',
            'testapp_callbacks',
        ]


class TestWhichNameAChainEndsAt:
    def test_the_longest_chain_says_where_a_name_ended(self):
        """Dict order is not time: the intermediate chain only says where it stopped."""
        joined = {'zed_child': ['anc_child', 'shop_child'], 'shop_child': ['anc_child']}

        assert scanning._latest_names(joined)['anc_child'] == 'zed_child'
        assert scanning._latest_names(dict(reversed(joined.items())))['anc_child'] == 'zed_child'

    def test_the_name_nearest_the_end_is_carried_first(self):
        """So where two names of one chain both hold an entry, the newer one is the one kept."""
        ends = scanning._chain_ends({'a': 'b', 'b': 'c'}, {})

        assert list(ends) == ['b', 'a']

    def test_where_the_newer_entry_is_it_wins_whatever_the_hash_seed(self):
        recorded = {'a': 'old', 'b': 'newer'}

        for old, new in scanning._chain_ends({'a': 'b', 'b': 'c'}, {}).items():
            scanning._move_renamed(old, new, recorded, keep_existing=True)

        assert recorded == {'c': 'newer'}


class TestTheWalkAloneAndTheEndAlone:
    def test_a_move_where_it_happened_is_carried_by_the_walk_alone(self, monkeypatch):
        """What is recorded after the move is moved there; the post-walk only sweeps the rest."""
        baseline = scan_existing_operations()
        TestAModelHandedToAnotherApp._as_a_move(monkeypatch)
        monkeypatch.setattr(scanning, '_chain_ends', lambda moves, renames: {})

        existing = scan_existing_operations()

        assert existing.triggers == baseline.triggers
        assert existing.soft_deletes == baseline.soft_deletes

    def test_the_renames_after_a_move_carry_it_on(self, monkeypatch):
        """Only the first hop is a move: ``testapp``'s own rename, read as no walk step, is what
        takes the entries on to the name in use."""
        baseline = scan_existing_operations()
        loader = MigrationLoader(None, ignore_no_migrations=True)
        real = graph.renames_by_migration
        first = real(loader, 'testapp')['0051_rename_encore_to_callback']
        monkeypatch.setattr(
            scanning,
            'renames_by_migration',
            lambda loader, label: {} if label == 'testapp' else real(loader, label),
        )
        monkeypatch.setattr(
            scanning,
            'moves_between_apps_by_migration',
            lambda loader, label: {'0001_initial': first} if label == 'testapp' else {},
        )

        existing = scan_existing_operations()

        assert existing.triggers == baseline.triggers
        assert 'testapp_encore' not in existing.triggers


class TestSettlingAKeyAfterAChain:
    def test_a_retirement_names_the_key_by_where_its_table_ended(self):
        """``zed_child`` held ``shop_child`` and ``anc_child``; the shorter chain, written last,
        would stop the key at ``shop_child``, which no model has any more."""
        from types import SimpleNamespace  # noqa: PLC0415

        site = scanning.CascadeRetirementSite('shop', '0006', ('anc_child', 'shop_parent', None), None)
        renames = {'zed_child': ['anc_child', 'shop_child'], 'shop_child': ['anc_child']}

        for ordered in (renames, dict(reversed(renames.items()))):
            (settled,) = scanning._settle_retirement_sites(
                [site],
                {},
                {},
                ordered,
                {'zed_child', 'shop_parent'},
                lambda: SimpleNamespace(graph=None),
            )

            assert settled.key == ('zed_child', 'shop_parent', None)
