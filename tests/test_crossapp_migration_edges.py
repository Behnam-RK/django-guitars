"""Cross-app dependency edges (2.5.0). A rule's action is parsed by PostgreSQL when the rule
is created, so a table or column it names in another app must already exist -- and across apps
only an explicit dependency says so. See ADR 0013 and ``docs/migrations.md``."""

from __future__ import annotations

import importlib
import re
import sys
from contextlib import contextmanager
from io import StringIO
from pathlib import Path

import pytest
from django.apps import apps
from django.core.management import CommandError, call_command
from django.db.migrations.loader import MigrationLoader

from guitars.management.enforcement.graph import ObjectRef
from django.test import override_settings


#: Two tests here mutate the crossapp enforcement migrations in place, and the rest read them.
#: ``xdist_group`` pins the module to one worker; under ``-n auto`` a reader on another worker
#: otherwise sees a file mid-mutation. Honoured only because ``--dist loadgroup`` is in addopts.
pytestmark = pytest.mark.xdist_group(name='crossapp_migration_files')


def _enforcement_migration(app_label: str) -> Path:
    return Path(apps.get_app_config(app_label).path) / 'migrations' / '0002_auto_enforcement.py'


def _dependencies(app_label: str) -> set[tuple[str, str]]:
    """The ``dependencies`` list of *app_label*'s enforcement migration, read off the file --
    the migration objects Django loads normalise them, and the point is what was *written*."""
    block = re.search(
        r'dependencies = \[(.*?)\]', _enforcement_migration(app_label).read_text(), re.DOTALL
    )
    assert block is not None
    return set(re.findall(r"\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)", block.group(1)))


def _plan(app_label: str) -> list[tuple[str, str]]:
    """The forwards plan for *app_label*'s leaf, as a fresh ``migrate <app>`` would run it."""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    leaf = sorted(loader.graph.leaf_nodes(app_label))[-1]
    return list(loader.graph.forwards_plan(leaf))


# ─── the edges themselves ───


def test_the_edge_points_at_the_migration_that_creates_the_table_not_the_app_leaf():
    """Depending on the leaf over-constrains the graph and drags unrelated migrations forward
    -- the consumer's hand-written edge did exactly that, trading one ordering failure for
    another. ``0001_initial`` creates the table; it is not the app's leaf."""
    edges = _dependencies('crossapp_owner')
    loader = MigrationLoader(None, ignore_no_migrations=True)

    assert ('crossapp_dependent', '0001_initial') in edges
    assert ('crossapp_dependent', '0001_initial') not in loader.graph.leaf_nodes(
        'crossapp_dependent'
    )


def test_an_edge_is_emitted_in_the_direction_no_foreign_key_orders():
    """The failing direction, and the reason "safe by accident" is not safe. ``crossapp_owner``
    holds the foreign key, so *its* migration is ordered after the dependent's anyway. Nothing
    orders the dependent's rule against the owner's table -- its guard reads a co-owner there."""
    assert ('crossapp_owner', '0001_initial') in _dependencies('crossapp_dependent')


def test_a_single_app_rule_gains_no_cross_app_edge():
    """``testapp`` owns every table its rules name, so 2.5.0 must leave its migrations alone --
    a new edge there would re-digest files every consuming project already has."""
    testapp_migrations = Path(apps.get_app_config('testapp').path) / 'migrations'
    foreign = {
        path.name
        for path in testapp_migrations.glob('*_auto_enforcement*.py')
        for app, _ in re.findall(r"\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)", path.read_text())
        if app not in {'testapp'}
    }

    assert foreign == set()


# ─── what the graph does with them ───


def test_the_rule_is_planned_after_the_migration_creating_the_table_it_names():
    """The whole point, at the level the failure happens. Without the edge the plan for
    ``crossapp_dependent`` contains no ``crossapp_owner`` migration at all, and the rule is
    created against a table that does not exist yet."""
    plan = _plan('crossapp_dependent')
    rule = plan.index(('crossapp_dependent', '0002_auto_enforcement'))

    assert ('crossapp_owner', '0001_initial') in plan[:rule]


@pytest.mark.parametrize('app_label', ['crossapp_owner', 'crossapp_dependent', 'crossapp_third'])
def test_the_graph_stays_acyclic_from_either_app(app_label):
    """Two apps each naming the other's table is the shape that could close a cycle. It cannot,
    because an edge points at the migration that *creates* an object and that is always older
    than the rule naming it -- asserted from both ends rather than argued."""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    loader.graph.ensure_not_cyclic()

    assert _plan(app_label)


# ─── against a real database ───


@pytest.mark.django_db(transaction=True)
def test_the_rules_are_created_on_a_database_migrated_from_either_app():
    """The handoff's first test: a virgin-database ``migrate`` in both app orders. Django
    resolves the edge either way round, so naming one app pulls in whatever its rules read.
    Before 2.5.0 this raised ``relation "crossapp_owner_owner" does not exist``."""
    for app_label in ('crossapp_dependent', 'crossapp_owner'):
        call_command('migrate', app_label, stdout=StringIO())

    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tablename, rulename FROM pg_rules WHERE tablename LIKE 'crossapp%%' "
            "AND rulename <> '_RETURN' ORDER BY tablename, rulename"
        )
        found = cursor.fetchall()

    # Each app's owner table carries its own soft-delete rule and its owned rule over the
    # shared dependent -- the owned one being what names the other app's table.
    owned = {table for table, rule in found if rule.startswith('soft_delete_owned')}
    assert owned == {
        'crossapp_dependent_localowner',
        'crossapp_owner_owner',
        'crossapp_third_thirdowner',
    }


@pytest.mark.django_db(transaction=True)
def test_the_cross_app_arm_actually_spares_a_row_its_other_owner_still_holds():
    """The edge is only worth having if the rule it orders is the one 2.4.0 shipped. Both
    owners live in different apps, so this exercises the co-owner arm *through* the boundary:
    soft-deleting one leaves the dependent alive while the other still points at it."""
    call_command('migrate', 'crossapp_owner', stdout=StringIO())

    from tests.crossapp_dependent.models import LocalOwner, Shared
    from tests.crossapp_owner.models import Owner

    shared = Shared.objects.create()
    across = Owner.objects.create(target=shared)
    LocalOwner.objects.create(target=shared)

    across.delete()  # the only owner of its kind -- but the local one is still live

    assert Shared.objects.filter(pk=shared.pk).exists()

    LocalOwner.objects.get(target=shared).delete()  # now the last owner of any kind

    assert not Shared.objects.filter(pk=shared.pk).exists()


def test_three_apps_owning_one_dependent_stay_acyclic():
    """The handoff's cycle case. Three apps own ``Shared``, so every one of the three rules
    reads arms in the other two and each app's migration depends on both peers -- the shape
    that would loop if edges pointed at app leaves rather than at what creates the object."""
    for app_label in ('crossapp_owner', 'crossapp_dependent', 'crossapp_third'):
        peers = {app for app, _ in _dependencies(app_label)} - {app_label, 'testapp'}
        assert len(peers) == 2, (app_label, peers)

    MigrationLoader(None, ignore_no_migrations=True).graph.ensure_not_cyclic()


def test_every_arm_of_a_three_way_dependent_is_ordered_against_its_own_app():
    """Each rule carries one arm per owning column -- three owners, so the rule's own column
    plus two co-owner arms. Both co-owners live elsewhere, so both need an edge, and the count
    is asserted rather than assumed: a dropped arm would silently pass the edge check."""
    content = _enforcement_migration('crossapp_third').read_text()
    arms = set(re.findall(r'(guitars_owner(?:_\d+)?)\."target_id"', content))

    assert arms == {'guitars_owner', 'guitars_owner_1', 'guitars_owner_2'}


# ─── --check ───


@contextmanager
def _without_dependency(app_label: str, dependency: str):
    """Drop *dependency* from *app_label*'s enforcement migration for the duration. The module
    is evicted from ``sys.modules`` on the way in *and* out: ``MigrationLoader`` imports
    migrations, so a restored file that is still cached leaves the mutated graph live."""
    path = _enforcement_migration(app_label)
    original = path.read_text()
    module = f'tests.{app_label}.migrations.{path.stem}'

    def _reload() -> None:
        sys.modules.pop(module, None)
        importlib.invalidate_caches()

    try:
        path.write_text(original.replace(f"        ('{dependency}', '0001_initial'),\n", '', 1))
        _reload()
        yield
    finally:
        path.write_text(original)
        _reload()


def _check(*app_labels: str) -> None:
    call_command(
        'makeguitarmigrations', *app_labels, '--check', stdout=StringIO(), stderr=StringIO()
    )


@override_settings(
    LOCAL_APPS=[
        'tests.testapp',
        'tests.crossapp_dependent',
        'tests.crossapp_owner',
        'tests.crossapp_third',
    ]
)
def test_check_fails_when_a_required_edge_is_absent_and_says_what_to_paste():
    """New in 2.5.0 and the reason it is a minor release: a graph that passed before now fails.
    The message has to be actionable on its own -- the migration is already recorded, so
    re-running the generator will not add the edge, and the operator pastes it by hand."""
    with _without_dependency('crossapp_dependent', 'crossapp_owner'):
        with pytest.raises(CommandError) as raised:
            _check('crossapp_dependent')

        message = str(raised.value)
        assert 'crossapp_owner_owner' in message
        assert "('crossapp_owner', '0001_initial')," in message
        assert 'does not exist' in message

    _check('crossapp_dependent')  # and green again once restored


@override_settings(
    LOCAL_APPS=[
        'tests.testapp',
        'tests.crossapp_dependent',
        'tests.crossapp_owner',
        'tests.crossapp_third',
    ]
)
def test_check_accepts_an_ordering_guaranteed_through_another_path():
    """Reachability, not a literal edge. ``crossapp_owner`` holds the foreign key, so its own
    ``0001_initial`` already depends on the dependent's -- dropping the explicit edge leaves the
    ordering guaranteed, and flagging it would fail a build over a graph that works."""
    with _without_dependency('crossapp_owner', 'crossapp_dependent'):
        _check('crossapp_owner')  # still reachable via crossapp_owner.0001_initial


# ─── the paths that should never be taken ───


def _command_over(app_label: str, refs: list) -> object:
    """A command whose recorded references are *refs*, bypassing the build. The paths below are
    all "should never happen" branches, and constructing the model shapes that reach them would
    test the shapes rather than the branches."""
    from guitars.management.enforcement.command import Command

    command = Command()
    command._object_refs[app_label] = refs
    return command


def test_a_reference_nothing_creates_warns_instead_of_emitting_an_edge():
    """An app with no migration creating the object is legitimate -- a third-party model its own
    package migrates -- so the rule stands and the operator is told. Refusing it would withdraw
    a rule that works today; emitting silence is what 2.4.2 did."""
    command = _command_over('testapp', [ObjectRef('crossapp_owner', 'Ghost', 'nope')])

    edges = command._object_dependencies_for(apps.get_app_config('testapp'))

    assert edges == []
    assert any('crossapp_owner.Ghost.nope' in note for note in command._mti_cascade_warnings)
    assert any('by hand' in note for note in command._mti_cascade_warnings)


def test_a_reference_to_a_model_that_no_longer_exists_is_not_checked():
    """A migration older than a deleted model still mentions its table, and that is not this
    check's business -- ``_ref_table`` answers ``None`` and the reference is passed over rather
    than reported against a model the registry cannot resolve."""
    command = _command_over('testapp', [ObjectRef('crossapp_owner', 'Deleted', 'gone')])

    assert command._missing_edge_notes(apps.get_app_config('testapp')) == []


def test_an_edge_that_would_close_a_cycle_is_dropped_with_a_warning():
    """It should be unreachable: an edge points at the migration that *creates* an object, which
    is older than any rule naming it. Asserted anyway, because the failure it guards against --
    a graph Django rejects outright -- is worse than the ordering failure the edge prevents."""
    from guitars.management.enforcement import operations as operations_module

    command = _command_over('testapp', [ObjectRef('crossapp_owner', 'Owner', 'target')])
    original = operations_module.would_close_a_cycle
    operations_module.would_close_a_cycle = lambda *_: True
    try:
        edges = command._object_dependencies_for(apps.get_app_config('testapp'))
    finally:
        operations_module.would_close_a_cycle = original

    assert edges == []
    assert any('cyclic' in note for note in command._mti_cascade_warnings)
    assert any('migrate' in note for note in command._mti_cascade_warnings)


def test_an_edge_that_closes_no_cycle_is_emitted():
    """The ordinary path, and the one the cycle check must not stand in the way of. Same shape as
    the refusal test above without the patched verdict, so the two differ only in the answer."""
    command = _command_over('testapp', [ObjectRef('crossapp_owner', 'Owner', 'target')])

    assert command._object_dependencies_for(apps.get_app_config('testapp')) == [
        ('crossapp_owner', '0001_initial')
    ]
    assert command._mti_cascade_warnings == []
