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
    assert existing.soft_delete_related_dependencies[moved] == [('testapp', '0000_auto_enforcement')]
    assert ('testapp_gone', 'testapp_genre', None) not in existing.soft_delete_related_dependencies


def test_a_create_records_the_migration_that_wrote_it(monkeypatch):
    """The node an edge points at, and the whole reason the scan grew a second dict: a rule is
    a ``RunSQL``, so no walk of migration *state* can answer which file made it."""
    existing = _scan_with(monkeypatch, testapp=(_created(),))

    assert existing.soft_delete_related_dependencies[_KEY] == [('testapp', '0000_auto_enforcement')]


def test_a_later_create_supersedes_the_earlier_one(monkeypatch):
    """Last write wins, as the digest beside it already does -- so a re-adoption's create is the
    one a retirement after it is ordered against, not the one it replaced."""
    existing = _scan_with(monkeypatch, testapp=(_created(), _created()))

    assert existing.soft_delete_related_dependencies[_KEY] == [
        ('testapp', '0000_auto_enforcement'),
        ('testapp', '0001_auto_enforcement'),
    ]


def test_a_retirement_leaves_the_provenance_it_popped(monkeypatch):
    """Coverage is subtracted; provenance is not. Nothing reads it for an unrecorded key -- the
    emitter iterates ``recorded - required`` -- and the ``--check`` half needs it after the pop."""
    existing = _scan_with(monkeypatch, testapp=(_created(), _retired()))

    assert _KEY not in existing.soft_delete_related
    assert existing.soft_delete_related_dependencies[_KEY] == [('testapp', '0000_auto_enforcement')]
    (site,) = existing.cascade_retirement_sites
    assert site == ('testapp', '0001_auto_enforcement', _KEY, ('testapp', '0000_auto_enforcement'))


def test_a_create_scanned_after_the_retirement_is_still_attributed_to_it(monkeypatch):
    """Apps walk in registry order, which is not chronological, so the create can be read after
    the retirement that dropped it. Nothing is in scope at the pop, so the site is filled from
    the finished map afterwards -- the shape #49 is made of, and why the snapshot alone fails."""
    with override_settings(LOCAL_APPS=['tests.testapp', 'tests.crossapp_owner']):
        existing = _scan_with(monkeypatch, testapp=(_retired(),), crossapp_owner=(_created(),))

    (site,) = existing.cascade_retirement_sites
    assert site.created == ('crossapp_owner', '0000_auto_enforcement')
    assert existing.soft_delete_related_dependencies[_KEY] == [site.created]


# --- The edge the retirement carries -----------------------------------------------------------


def _seed(built: Command, created: tuple[str, str] | None) -> tuple[str, str, str | None]:
    built.existing.soft_delete_related[_KEY] = 'abc'
    if created is not None:
        built.existing.soft_delete_related_dependencies[_KEY] = [created]
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

    (note,) = command._missing_retirement_edge_notes(set())

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

    assert command._missing_retirement_edge_notes(set()) == []


def test_a_create_that_already_depends_on_the_retirement_is_not_named(command):
    """The one shape Django rejects outright, so printing the tuple would be red with no move
    that clears it -- the guard ``_missing_edge_notes`` states in the same words."""
    command.existing.cascade_retirement_sites.append(
        _site('crossapp_owner', '0001_initial', ('crossapp_dependent', '0002_auto_enforcement'))
    )

    assert command._missing_retirement_edge_notes(set()) == []


def test_the_committed_corpus_is_named_by_neither_half():
    """Its one retirement (0050) drops a rule 0048 created, both in testapp, so the own-app
    filter takes it. A note here would fail every consumer's CI over a file this release
    did not touch."""
    assert Command()._missing_retirement_edge_notes(set()) == []


def test_the_note_fails_a_check_run(monkeypatch):
    """Wired, and to the existing refusal rather than a new one: the note joins ``_missing_edges``
    and ``_refuse_a_missing_edge`` raises it, so ``--check`` fails as it does for a missing
    object edge."""
    monkeypatch.setattr(
        OperationsMixin,
        '_missing_retirement_edge_notes',
        lambda self, requested: ['A retirement nothing orders.'],
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
    command.existing.soft_delete_related_dependencies[second] = [created]

    assert len(_retirements(command)) == 2

    assert command._retired_cascade_dependencies_for(apps.get_app_config('testapp')) == [created]


def test_a_key_retired_twice_names_the_create_each_drop_dropped(command):
    """One note per retirement, each naming its *own* create rather than the newest of the
    key. The scan matched them; this asserts the note carries that through instead of
    collapsing to one answer, which after a re-adoption is the wrong one."""
    command.existing.cascade_retirement_sites.extend(
        [
            _site('crossapp_owner', '0001_initial', ('crossapp_third', '0001_initial')),
            _site(
                'crossapp_owner', '0002_auto_enforcement', ('crossapp_third', '0002_auto_enforcement')
            ),
        ]
    )

    first, second = command._missing_retirement_edge_notes(set())

    assert "('crossapp_third', '0001_initial')," in first
    assert "('crossapp_third', '0002_auto_enforcement')," in second


def test_a_note_is_confined_to_the_apps_a_scoped_run_asked_about(command):
    """Scoped like every other refusal. The scan reads all of LOCAL_APPS, so an unscoped note
    turns every per-app CI job red over one app's history, with no fix available from that job."""
    command.existing.cascade_retirement_sites.append(
        _site('crossapp_owner', '0001_initial', ('crossapp_third', '0001_initial'))
    )

    assert command._missing_retirement_edge_notes({'testapp'}) == []
    assert len(command._missing_retirement_edge_notes({'crossapp_owner'})) == 1


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


def _loader() -> MigrationLoader:
    return MigrationLoader(None, ignore_no_migrations=True)


def _settled(retirement, creates):
    """Run the post-pass over one site with *creates* recorded for its key, and report both
    halves of its verdict: whether the key survives, and which create the site was matched to."""
    recorded = {_KEY: 'abc'}
    (site,) = scanning._settle_retirement_sites(
        [CascadeRetirementSite('testapp', retirement, _KEY, None)],
        recorded,
        {_KEY: creates},
        {},
        set(),
        set(),
        lambda: MigrationLoader(None, ignore_no_migrations=True),
    )
    return _KEY in recorded, site.created


@pytest.mark.parametrize(
    ('retirement', 'create', 'survives'),
    [
        ('0050_auto_enforcement', '0048_auto_enforcement', False),
        ('0048_auto_enforcement', '0050_auto_enforcement', True),
    ],
)
def test_the_retirement_wins_only_where_the_create_is_provably_older(retirement, create, survives):
    """Asked of the graph over two real nodes. The corpus's own 0048 creates the rule 0050
    retires, so reversing the pair is the re-adoption shape without inventing a history."""
    assert _settled(retirement, [('testapp', create)])[0] is survives


def test_an_unordered_create_is_not_read_as_older_than_the_retirement():
    """Unordered is not "the create is older". A re-adopted create in another app is ordered
    against nothing, and reading it as ordered pops a live rule's coverage."""
    recorded = {_KEY: 'abc'}

    scanning._settle_retirement_sites(
        # Two real nodes in different apps with no path between them either way.
        [CascadeRetirementSite('crossapp_owner', '0001_initial', _KEY, None)],
        recorded,
        {_KEY: [('crossapp_third', '0001_initial'), ('crossapp_owner', '0002_auto_enforcement')]},
        {},
        set(),
        set(),
        lambda: MigrationLoader(None, ignore_no_migrations=True),
    )

    assert _KEY in recorded


def test_a_site_is_matched_to_the_newest_create_before_it_not_the_newest_of_all():
    """The destructive one. After retire/re-adopt the newest create is the *re-adoption*, and
    naming it would print a tuple ordering this drop after the rule it revives -- dropping it
    on every database, fresh and incremental, from advice the tool printed."""
    creates = [('testapp', '0048_auto_enforcement'), ('testapp', '0055_auto_enforcement')]

    assert _settled('0050_auto_enforcement', creates)[1] == ('testapp', '0048_auto_enforcement')


def test_a_readopted_create_declares_the_retirement_it_revives(command):
    """The mirror of the drop's own edge. Without it a fresh ``migrate`` can run the ``CREATE``
    before the ``DROP`` that retired the key, ending with no rule where an incremental database
    has one -- the same ADR 0006 divergence, reached from the other side."""
    command.existing.cascade_retirement_sites.append(
        _site('crossapp_owner', '0001_initial', ('crossapp_third', '0001_initial'))
    )

    command._record_readoption_edge('testapp', _KEY)

    assert command._retired_cascade_dependencies_for(apps.get_app_config('testapp')) == [
        ('crossapp_owner', '0001_initial')
    ]


def test_a_readopted_create_in_the_retiring_app_needs_no_edge(command):
    """Its own linear history orders it after the drop already, and an edge into the file being
    written is what the writer drops as a self-reference."""
    command.existing.cascade_retirement_sites.append(
        _site('testapp', '0050_auto_enforcement', ('testapp', '0048_auto_enforcement'))
    )

    command._record_readoption_edge('testapp', _KEY)

    assert command._retired_cascade_dependencies_for(apps.get_app_config('testapp')) == []


def test_the_create_path_records_that_edge_itself(command):
    """Wired, not merely written: the recorder is called from ``_cascade_operations``, so the
    one line joining them could go without a failure. Seeds a retirement for a key the models
    still require, which is exactly a re-adoption."""
    live = next(iter(command._cascade_key_maps()[0]))
    command.existing.cascade_retirement_sites.append(
        _site('crossapp_owner', '0001_initial', ('crossapp_third', '0001_initial'))._replace(
            key=live
        )
    )

    command._build_operations(apps.get_app_config('testapp'))

    assert ('crossapp_owner', '0001_initial') in command._retirement_edges.get('testapp', [])


def test_one_migration_naming_a_key_twice_is_recorded_once(monkeypatch):
    """The provenance list is per migration, not per header. A file naming one key twice would
    otherwise offer the same node as two candidate creates, and "the newest before this drop"
    would then depend on how many times a header happened to appear."""
    existing = _scan_with(monkeypatch, testapp=(_created() + _created(),))

    assert existing.soft_delete_related_dependencies[_KEY] == [('testapp', '0000_auto_enforcement')]


def test_a_legacy_history_with_two_unordered_cycles_is_still_named(command):
    """The population this release exists for, at its worst: create, drop, re-create, drop,
    with nothing ordering any of it. Attributing only where the graph proves it left these
    silent while a *less* broken single-cycle history got a note."""
    # Real nodes with no path either way, which is what a pre-2.10.0 cross-app history is.
    creates = [
        ('crossapp_tenant_ancestor', '0001_initial'),
        ('crossapp_tenant_ancestor', '0003_auto_enforcement'),
    ]
    drops = [
        _site('crossapp_owner', '0001_initial', None),
        _site('crossapp_owner', '0003_auto_enforcement', None),
    ]
    settled = scanning._settle_retirement_sites(
        drops, {}, {_KEY: creates}, {}, set(), set(), _loader
    )

    # Paired by rank, the two alternating: the nth drop dropped the nth create.
    assert [site.created for site in settled] == creates
    command.existing.cascade_retirement_sites.extend(settled)
    assert len(command._missing_retirement_edge_notes(set())) == 2


def test_an_unordered_history_ending_in_a_drop_reads_as_retired(command):
    """And the pop that goes with it. Nothing orders the halves, so counting is the only
    signal: as many drops as creates means the last event was a drop, and re-emitting a third
    would fail on a rule already gone."""
    creates = [('shop', '0002_auto_enforcement')]
    recorded = {_KEY: 'abc'}

    scanning._settle_retirement_sites(
        [_site('stock', '0003_auto_enforcement', None)],
        recorded,
        {_KEY: creates},
        {},
        set(),
        set(),
        _loader,
    )

    assert _KEY not in recorded


def test_a_retirement_whose_create_was_never_scanned_leaves_the_key_alone():
    """No create recorded at all -- a hand-written drop, or one whose create lives in an app
    outside ``LOCAL_APPS``. Nothing to compare it against, so the walk's verdict stands rather
    than a guess popping coverage the models may still require."""
    recorded = {_KEY: 'abc'}

    (site,) = scanning._settle_retirement_sites(
        [_site('crossapp_owner', '0001_initial', None)], recorded, {}, {}, set(), set(), _loader
    )

    assert site.created is None
    assert _KEY in recorded


def test_a_renamed_key_is_told_the_quieter_symptom(command):
    """A rename makes that drop ``IF EXISTS`` over every prior spelling, so an unordered one
    does not abort -- it no-ops, and the create after it leaves the rule live. Promising the
    abort would have a reader wait for a failure that never comes."""
    key = ('testapp_callbacks', 'testapp_band', None)
    command.existing.renamed_tables['testapp_callbacks'] = ['testapp_encore']
    command.existing.cascade_retirement_sites.append(
        _site('crossapp_owner', '0001_initial', ('crossapp_third', '0001_initial'))._replace(
            key=key
        )
    )

    (note,) = command._missing_retirement_edge_notes(set())

    assert 'silently does nothing and leaves the rule live' in note
    assert 'does not exist' not in note


def test_a_re_adopted_key_reads_as_live_regardless_of_which_apps_scan_last(monkeypatch):
    """The bug the raw walk's own pop used to hide: whichever app scanned last decided the
    answer, so a re-adopted key -- more creates than drops, unordered -- read as retired
    whenever its drop's app scanned after its creates'. ``testapp`` scans first."""
    with override_settings(LOCAL_APPS=['tests.testapp', 'tests.crossapp_owner']):
        existing = _scan_with(
            monkeypatch,
            testapp=(_created(), _created()),
            crossapp_owner=(_retired(),),
        )

    assert _KEY in existing.soft_delete_related


def test_a_renamed_owner_table_alone_still_promises_the_abort(command):
    """The owner table's own rename plays no part in ``_retired_cascade_operations``' choice
    of ``IF EXISTS`` -- only the related table's does -- so a rename here must not switch the
    note to the quieter symptom, which promises a silence this drop cannot produce."""
    key = ('testapp_album', 'testapp_genre', None)
    command.existing.renamed_tables['testapp_genre'] = ['testapp_old_genre']
    command.existing.cascade_retirement_sites.append(
        _site('crossapp_owner', '0001_initial', ('crossapp_third', '0001_initial'))._replace(
            key=key
        )
    )

    (note,) = command._missing_retirement_edge_notes(set())

    assert 'does not exist' in note
    assert 'silently does nothing' not in note


def test_a_freed_name_retaken_by_a_live_model_is_not_translated():
    """The mirror of ``_move_renamed``'s own guard: a freed name another model retook keeps
    its own coverage under that name, so the site's key must stay untranslated too, or the
    lookup misses the provenance the walk deliberately left where it was."""
    key = ('shop_old_child', 'testapp_genre', None)
    create = ('crossapp_third', '0001_initial')
    drop = _site('crossapp_owner', '0001_initial', None)._replace(key=key)

    settled = scanning._settle_retirement_sites(
        [drop],
        {},
        {key: [create]},
        {'shop_new_child': ['shop_old_child']},
        {'shop_old_child'},
        set(),
        _loader,
    )

    (site,) = settled
    assert site.key == key
    assert site.created == create
