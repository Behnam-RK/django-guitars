"""Retirement of cascade soft-delete rules (2.9.0). Nothing in this kit dropped a rule when the
models stopped calling for it, so relaxing a ``CASCADE`` key left the rule live with ``--check``
green. These pin what is retired, what is only *named*, and why the difference is not a guess."""

import types
from io import StringIO

import pytest
from django.apps import apps
from django.core.management import CommandError, call_command
from django.db import models
from django.db.migrations.loader import MigrationLoader
from django.db.models import CASCADE, SET_NULL
from django.test import override_settings
from django.test.utils import isolate_apps

from guitars.models import OwningForeignKey, SetarModel

from guitars.management import _generator
from guitars.management.enforcement import graph, scanning
from guitars.management.enforcement.command import Command
from guitars.management.enforcement.headers import (
    HEADER_SOFT_DELETE_RELATED,
    HEADER_SOFT_DELETE_RELATED_RETIRED,
)
from guitars.management.enforcement.operations import OperationsMixin
from guitars.management.enforcement.scanning import (
    CascadeRetirementSite,
    scan_existing_operations,
)


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


def test_a_proxy_bound_to_the_table_still_recovers_the_retired_column(command):
    """``_cascade_key_maps`` binds one model per table and a proxy of an earlier-registered app
    can reach it first. Its ``local_fields`` are empty, so the column scan finds nothing and
    the reverse refuses -- an irreversible migration where the rule could be rebuilt."""

    @isolate_apps('tests.testapp')
    def _build():
        class ReviewProxy(apps.get_model('testapp', 'Review')):
            class Meta:
                app_label = 'testapp'
                proxy = True

        return ReviewProxy

    key = ('testapp_review', 'testapp_release', None)

    assert command._retired_cascade_column(key, {'testapp_review': _build()}) == 'release_id'


# --- Which migration created the rule a retirement drops (2.10.0, issue #49) -------------------


def _scan_with(monkeypatch, **by_app: tuple[str, ...]):
    """Feed the real scanner synthetic migration text, per app. On disk these files would go in
    an app's own migrations directory, where ``-n auto`` lets another worker read one
    half-written -- and the shape under test is precisely two apps disagreeing."""

    def _iter(app):
        for index, content in enumerate(by_app.get(app.label, ())):
            yield types.SimpleNamespace(stem=f'{index:04d}_auto_enforcement'), content

    monkeypatch.setattr(_generator, 'iter_migration_files', _iter)
    return scan_existing_operations()


def _created(table='testapp_album', owner='testapp_genre') -> str:
    return (
        HEADER_SOFT_DELETE_RELATED.format(related_table=table, table=owner)
        + ' [SQL:abc123def456]\n'
    )


def _retired(table='testapp_album', owner='testapp_genre') -> str:
    return (
        HEADER_SOFT_DELETE_RELATED_RETIRED.format(related_table=table, table=owner)
        + ' [SQL:abc123def456]\n'
    )


_KEY = ('testapp_album', 'testapp_genre', None)


def test_every_recorded_cascade_key_carries_the_migration_that_created_it():
    """The invariant, over the committed corpus rather than a fixture: no key is recorded whose
    creating migration is unknown, so no retirement can silently go unordered."""
    existing = scan_existing_operations()

    assert set(existing.soft_delete_related) <= set(existing.soft_delete_related_dependencies)


def test_a_rename_carries_provenance_with_the_coverage_it_mirrors(monkeypatch):
    """Provenance is keyed on the table, so it has to move when the table does. Left out of the
    walk's family list it stays under the freed name, the coverage moves without it, and the
    retirement that follows loses its edge -- the original bug with the warning suppressed."""
    real = graph.renames_by_migration
    monkeypatch.setattr(
        scanning,
        'renames_by_migration',
        lambda ldr, app: {'0001_auto_enforcement': [('testapp_gone', 'testapp_renamed')]}
        if app == 'testapp'
        else real(ldr, app),
    )

    # A source no model owns: ``_move_renamed`` declines to carry coverage off a live name.
    existing = _scan_with(monkeypatch, testapp=(_created(table='testapp_gone'), ''))

    moved = ('testapp_renamed', 'testapp_genre', None)
    assert moved in existing.soft_delete_related
    assert existing.soft_delete_related_dependencies[moved] == (
        'testapp',
        '0000_auto_enforcement',
    )
    assert ('testapp_gone', 'testapp_genre', None) not in existing.soft_delete_related_dependencies


def test_a_create_records_the_migration_that_wrote_it(monkeypatch):
    """The node an edge points at, and the whole reason the scan grew a second dict: a rule is
    a ``RunSQL``, so no walk of migration *state* can answer which file made it."""
    existing = _scan_with(monkeypatch, testapp=(_created(),))

    assert existing.soft_delete_related_dependencies[_KEY] == ('testapp', '0000_auto_enforcement')


def test_a_later_create_supersedes_the_earlier_one(monkeypatch):
    """Last write wins, as the digest beside it already does -- so a re-adoption's create is the
    one a retirement after it is ordered against, not the one it replaced."""
    existing = _scan_with(monkeypatch, testapp=(_created(), _created()))

    assert existing.soft_delete_related_dependencies[_KEY] == ('testapp', '0001_auto_enforcement')


def test_a_retirement_leaves_the_provenance_it_popped(monkeypatch):
    """Coverage is subtracted; provenance is not. Nothing reads it for an unrecorded key -- the
    emitter iterates ``recorded - required`` -- and the ``--check`` half needs it after the pop."""
    existing = _scan_with(monkeypatch, testapp=(_created(), _retired()))

    assert _KEY not in existing.soft_delete_related
    assert existing.soft_delete_related_dependencies[_KEY] == ('testapp', '0000_auto_enforcement')
    (site,) = existing.cascade_retirement_sites
    assert site == ('testapp', '0001_auto_enforcement', _KEY, ('testapp', '0000_auto_enforcement'))


def test_a_create_scanned_after_the_retirement_leaves_the_snapshot_empty(monkeypatch):
    """Apps walk in registry order, which is not chronological, so the create can be read after
    the retirement that dropped it. The snapshot is then ``None`` and the reader falls back to
    the finished map -- the shape #49 is actually made of, and why one alone will not do."""
    with override_settings(LOCAL_APPS=['tests.testapp', 'tests.crossapp_owner']):
        existing = _scan_with(monkeypatch, testapp=(_retired(),), crossapp_owner=(_created(),))

    (site,) = existing.cascade_retirement_sites
    assert site.created is None
    assert existing.soft_delete_related_dependencies[_KEY] == (
        'crossapp_owner',
        '0000_auto_enforcement',
    )


# --- The edge the retirement carries -----------------------------------------------------------


def _seed(built: Command, created: tuple[str, str] | None) -> tuple[str, str, str | None]:
    built.existing.soft_delete_related[_KEY] = 'abc'
    if created is not None:
        built.existing.soft_delete_related_dependencies[_KEY] = created
    return _KEY


def test_the_retirement_declares_an_edge_to_the_migration_that_created_the_rule(command):
    """The fix. The drop is hosted by the owner table's app while the create was hosted by the
    app that walked the owner model, and only an explicit edge orders one after the other."""
    _seed(command, ('crossapp_owner', '0001_initial'))
    app = apps.get_app_config('testapp')

    assert _retirements(command)

    assert command._retired_cascade_dependencies_for(app) == [('crossapp_owner', '0001_initial')]


def test_a_create_in_the_same_app_needs_no_edge(command):
    """The overwhelmingly common case, and the one every project before this had: the app's own
    linear history orders the drop after the create, and an edge into it would be a self-edge."""
    _seed(command, ('testapp', '0048_auto_enforcement'))

    assert _retirements(command)

    assert command._retired_cascade_dependencies_for(apps.get_app_config('testapp')) == []


def test_a_creating_migration_the_graph_never_saw_warns_instead_of_emitting_an_edge(command):
    """Warned, not refused, as an unresolvable object reference is. The node was read off a
    header, and a squash since then can have replaced the file that carried it."""
    _seed(command, ('crossapp_owner', '9999_squashed_away'))

    assert _retirements(command)

    assert command._retired_cascade_dependencies_for(apps.get_app_config('testapp')) == []
    (note,) = command._unresolved_reference_notes
    assert 'retires a cascade rule created by' in note
    assert 'crossapp_owner.9999_squashed_away' in note


def test_the_dependency_list_carries_the_retirement_edge_after_the_others(command):
    """Composed, not replacing: an old file's list reads as it did, with the new edge appended.
    Once, and only where the graph does not already reach it."""
    _seed(command, ('crossapp_owner', '0001_initial'))
    app = apps.get_app_config('testapp')
    operations = _retirements(command)

    dependencies = command._dependencies_for(app, '\n'.join(operations))

    assert dependencies.count(('crossapp_owner', '0001_initial')) == 1
    assert dependencies[-1] == ('crossapp_owner', '0001_initial')


# --- The retirement already on disk, which the emitter can never reach --------------------------


def _site(app_label, migration, created) -> CascadeRetirementSite:
    return CascadeRetirementSite(app_label, migration, _KEY, created)


def test_check_names_a_retirement_nothing_orders_against_its_create(command):
    """The installed base. Regenerating never rewrites that file -- the digest guard skips it --
    so the note is the only way a history broken this way is ever told."""
    command.existing.cascade_retirement_sites.append(
        _site('crossapp_owner', '0001_initial', ('crossapp_third', '0001_initial'))
    )

    (note,) = command._missing_retirement_edge_notes()

    assert "'crossapp_owner.0001_initial' drops the cascade rule on 'testapp_genre'" in note
    assert "nothing orders it after 'crossapp_third.0001_initial'" in note
    assert 'does not exist' in note
    assert "('crossapp_third', '0001_initial')," in note


def test_a_retirement_already_ordered_against_its_create_is_not_named(command):
    """Reachability, not a literal edge: ``crossapp_dependent.0002`` really does declare that
    dependency, so the ordering is guaranteed and naming it would be a false alarm."""
    command.existing.cascade_retirement_sites.append(
        _site('crossapp_dependent', '0002_auto_enforcement', ('crossapp_owner', '0001_initial'))
    )

    assert command._missing_retirement_edge_notes() == []


def test_a_create_that_already_depends_on_the_retirement_is_not_named(command):
    """The one shape Django rejects outright, so printing the tuple would be red with no move
    that clears it -- the guard ``_missing_edge_notes`` states in the same words."""
    command.existing.cascade_retirement_sites.append(
        _site('crossapp_owner', '0001_initial', ('crossapp_dependent', '0002_auto_enforcement'))
    )

    assert command._missing_retirement_edge_notes() == []


def test_the_committed_corpus_is_named_by_neither_half():
    """Its one retirement (0050) drops a rule 0048 created, both in testapp, so the own-app
    filter takes it. A note here would fail every consumer's CI over a file this release
    did not touch."""
    assert Command()._missing_retirement_edge_notes() == []


def test_the_note_fails_a_check_run(monkeypatch):
    """Wired, and to the existing refusal rather than a new one: the note joins ``_missing_edges``
    and ``_refuse_a_missing_edge`` raises it, so ``--check`` fails as it does for a missing
    object edge."""
    monkeypatch.setattr(
        OperationsMixin,
        '_missing_retirement_edge_notes',
        lambda self: ['A retirement nothing orders.'],
    )

    with pytest.raises(CommandError, match='A retirement nothing orders.'):
        call_command('makeguitarmigrations', 'testapp', '--check', stdout=StringIO())


def test_two_rules_from_one_migration_share_a_single_edge(command):
    """One migration routinely creates several cascade rules, and retiring two of them wants one
    dependency, not the same tuple twice."""
    created = ('crossapp_owner', '0001_initial')
    second = ('testapp_merch', 'testapp_genre', None)
    _seed(command, created)
    command.existing.soft_delete_related[second] = 'abc'
    command.existing.soft_delete_related_dependencies[second] = created

    assert len(_retirements(command)) == 2

    assert command._retired_cascade_dependencies_for(apps.get_app_config('testapp')) == [created]


def test_one_retirement_reported_twice_is_named_once(command):
    """A key retired again in a later migration leaves two sites naming one repair, and the
    reader has one file to open either way."""
    site = _site('crossapp_owner', '0001_initial', ('crossapp_third', '0001_initial'))
    command.existing.cascade_retirement_sites.extend([site, site])

    assert len(command._missing_retirement_edge_notes()) == 1


def test_a_retirement_scanned_before_its_create_still_reads_as_retired():
    """Apps walk in registry order and this question is graph order. The owner app is scanned
    first, so its retirement popped a key the child app then re-recorded -- leaving the rule
    reading as live and the retirement re-emitted on every run, ``--check`` never green."""
    with override_settings(
        LOCAL_APPS=['tests.crossapp_retire_owner', 'tests.crossapp_retire_child']
    ):
        existing = scan_existing_operations()

    key = ('crossapp_retire_child_dependant', 'crossapp_retire_owner_retiree', None)
    assert key in existing.soft_delete_related_dependencies
    assert key not in existing.soft_delete_related


def test_a_key_created_again_after_its_retirement_stays_recorded(monkeypatch):
    """The other side of that verdict, and the reason it is not a blanket pop: a create the
    graph puts *after* the retirement is a re-adoption, and the rule is live again."""
    existing = _scan_with(monkeypatch, testapp=(_created(), _retired(), _created()))

    assert _KEY in existing.soft_delete_related


@pytest.mark.parametrize(
    ('retirement', 'create', 'survives'),
    [
        ('0050_auto_enforcement', '0048_auto_enforcement', False),
        ('0048_auto_enforcement', '0050_auto_enforcement', True),
    ],
)
def test_the_later_of_the_two_migrations_wins(retirement, create, survives):
    """Asked of the graph directly, over two real nodes: the create wins only where it is
    provably after the retirement. The corpus's own 0048 creates the rule 0050 retires, so
    reversing the pair is the re-adoption shape without inventing a history for it."""
    recorded = {_KEY: 'abc'}

    scanning._settle_retired_key(
        CascadeRetirementSite('testapp', retirement, _KEY, None),
        recorded,
        {_KEY: ('testapp', create)},
        lambda: MigrationLoader(None, ignore_no_migrations=True),
    )

    assert (_KEY in recorded) is survives
