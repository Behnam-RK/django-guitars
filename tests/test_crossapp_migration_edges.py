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
from django.test import override_settings

from guitars import sql
from guitars.management.enforcement.graph import ObjectRef
from guitars.management.enforcement.operations import OperationsMixin


#: Two tests here mutate the crossapp enforcement migrations in place, and the rest read them.
#: ``xdist_group`` pins the module to one worker; under ``-n auto`` a reader on another worker
#: otherwise sees a file mid-mutation. Honoured only because ``--dist loadgroup`` is in addopts.
pytestmark = pytest.mark.xdist_group(name='crossapp_migration_files')


def _enforcement_migration(app_label: str, name: str = '0002_auto_enforcement') -> Path:
    return Path(apps.get_app_config(app_label).path) / 'migrations' / f'{name}.py'


def _declared_dependencies(content: str) -> set[tuple[str, str]]:
    """The ``dependencies`` list written into *content*. Scoped to that block, never the whole
    file: an emitted policy's ``IN ('a', 'b')`` reads as a dependency tuple to the same regex."""
    block = re.search(r'dependencies = \[(.*?)\]', content, re.DOTALL)
    assert block is not None
    return set(re.findall(r"\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)", block.group(1)))


def _dependencies(app_label: str) -> set[tuple[str, str]]:
    """The ``dependencies`` list of *app_label*'s enforcement migration, read off the file --
    the migration objects Django loads normalise them, and the point is what was *written*."""
    return _declared_dependencies(_enforcement_migration(app_label).read_text())


def _plan(app_label: str) -> list[tuple[str, str]]:
    """The forwards plan for *app_label*'s leaf, as a fresh ``migrate <app>`` would run it."""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    leaf = sorted(loader.graph.leaf_nodes(app_label))[-1]
    return list(loader.graph.forwards_plan(leaf))


def _refs_recorded_for(app_label: str) -> list[ObjectRef]:
    """The references a real build of *app_label*'s operations records. Asserted rather than the
    generated file, for objects whose creating migration happens to be the one creating the
    table: the edge would be identical either way, and the ref is what a later history splits."""
    from guitars.management.enforcement.command import Command

    command = Command()
    command._build_operations(apps.get_app_config(app_label))
    return command._object_refs.get(app_label, [])


# ─── what a rule names, as the rules are built ───


@pytest.mark.parametrize(
    'ref',
    [
        # The table the rule updates, and the column it writes -- promoting a model to
        # ``SetarModel`` gains that column in a migration later than the one creating its
        # table, so an edge to the table alone would not order the column.
        ObjectRef('crossapp_dependent', 'Shared', None),
        ObjectRef('crossapp_dependent', 'Shared', '_deleted_at'),
        # A co-owner arm: the column its ``NOT EXISTS`` reads, and the liveness column beside it.
        ObjectRef('crossapp_dependent', 'LocalOwner', 'target'),
        ObjectRef('crossapp_dependent', 'LocalOwner', '_deleted_at'),
        ObjectRef('crossapp_third', 'ThirdOwner', 'target'),
        ObjectRef('crossapp_third', 'ThirdOwner', '_deleted_at'),
    ],
)
def test_an_owned_rule_records_every_object_its_action_names(ref):
    """Structurally, as the rule is built -- a co-owner arm's table appears only in the rule
    body, never in its header, so nothing could read these back off the rendered SQL."""
    assert ref in _refs_recorded_for('crossapp_owner')


def test_an_mti_redirect_rule_records_the_ancestor_table_and_its_deleted_at():
    """``CREATE_MTI_SOFT_DELETE_RULE``'s action ``UPDATE``s the ancestor's table and its
    ``_deleted_at``, both resolved as PostgreSQL parses it, so a chain crossing apps needs the
    same edges. ``testapp``'s chains are one app, so only the recording can be asserted."""
    refs = _refs_recorded_for('testapp')

    assert ObjectRef('testapp', 'Ensemble', None) in refs
    assert ObjectRef('testapp', 'Ensemble', '_deleted_at') in refs


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
        for app, _ in _declared_dependencies(path.read_text())
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
def _without_dependency(app_label: str, dependency: str, name: str = '0001_initial'):
    """Drop the ``(dependency, name)`` edge from *app_label*'s enforcement migration for the
    duration. The module is evicted from ``sys.modules`` on the way in *and* out --
    ``MigrationLoader`` imports migrations, so a restored file still cached stays live."""
    path = _enforcement_migration(app_label)
    original = path.read_text()
    module = f'tests.{app_label}.migrations.{path.stem}'

    def _reload() -> None:
        sys.modules.pop(module, None)
        importlib.invalidate_caches()

    try:
        path.write_text(original.replace(f"        ('{dependency}', '{name}'),\n", '', 1))
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
def test_the_migration_graph_is_built_once_for_a_whole_check_run(monkeypatch):
    """Building one imports every migration module in the project and the check asks a question
    per in-scope app, so a build per call squares that sweep against the local-app count.
    Counted, because the saving is invisible to every other assertion in this file."""
    from guitars.management.enforcement import operations as operations_module

    real = operations_module.MigrationLoader
    builds: list[object] = []

    def _counted(*args, **kwargs):
        loader = real(*args, **kwargs)
        builds.append(loader)
        return loader

    monkeypatch.setattr(operations_module, 'MigrationLoader', _counted)

    _check('crossapp_dependent', 'crossapp_owner', 'crossapp_third')

    assert len(builds) == 1


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


def test_a_reference_nothing_in_the_history_creates_is_not_checked():
    """Nothing resolves it, so there is no migration to be ordered against and nothing to say.
    The ``--check`` half of the warning above, which the generating half already covers."""
    command = _command_over('testapp', [ObjectRef('crossapp_owner', 'Deleted', 'gone')])

    assert command._missing_edge_notes(apps.get_app_config('testapp')) == []


@pytest.mark.parametrize(
    'ref',
    [
        # A migration older than a deleted model still mentions its table and column, and that
        # is not this check's business: neither can be resolved against the live registry.
        ObjectRef('crossapp_owner', 'Deleted', 'gone'),
        ObjectRef('crossapp_owner', 'Owner', 'gone'),
    ],
)
def test_a_reference_the_registry_can_no_longer_resolve_has_no_column(ref):
    """``None`` rather than a raise, and rather than a guess: the column narrows *which* file a
    note names, so answering it wrongly would name the wrong migration."""
    assert OperationsMixin._ref_column(ref) is None


def test_a_reference_naming_only_a_table_has_no_column():
    """A cascade rule's related table and a joined arm's MTI ancestor name no column of their
    own, so there is nothing for the file match to narrow on."""
    assert OperationsMixin._ref_column(ObjectRef('crossapp_owner', 'Owner')) is None


def test_the_note_names_only_a_migration_carrying_the_column_the_reference_resolves_to():
    """Two refs into one app can share a table and resolve to *different* migrations -- a second
    owning column added later -- so matching the table alone would report the older file forever,
    a ``--check`` failure no edge on the file named could clear."""
    app = apps.get_app_config('crossapp_third')

    with _without_dependency('crossapp_third', 'crossapp_owner'):
        # Same table, same missing edge, both unreachable -- only the column differs, and
        # ``_created_at`` appears nowhere in an enforcement migration's SQL.
        untouched = _command_over(
            'crossapp_third', [ObjectRef('crossapp_owner', 'Owner', '_created_at')]
        )
        named = _command_over('crossapp_third', [ObjectRef('crossapp_owner', 'Owner', 'target')])

        assert untouched._missing_edge_notes(app) == []
        assert len(named._missing_edge_notes(app)) == 1


def test_a_resolved_reference_becomes_an_edge_with_nothing_warned():
    """The whole of the emitting path: a ref resolves, the edge comes back, and no warning is
    raised. Every other outcome here is a warning with no edge, so this is the one that pairs."""
    command = _command_over('testapp', [ObjectRef('crossapp_owner', 'Owner', 'target')])

    assert command._object_dependencies_for(apps.get_app_config('testapp')) == [
        ('crossapp_owner', '0001_initial')
    ]
    assert command._mti_cascade_warnings == []


def test_the_writer_is_handed_the_object_edges_and_not_only_the_function_ones():
    """The wiring, at the seam ``handle()`` hands ``_generate_stage``. Unwiring it would leave
    every test that reads a checked-in migration file green, the edges already being in them."""
    command = _command_over('testapp', [ObjectRef('crossapp_owner', 'Owner', 'target')])

    # An empty operations blob names no trigger header, so nothing comes back but the objects.
    assert command._dependencies_for(apps.get_app_config('testapp'), '') == [
        ('crossapp_owner', '0001_initial')
    ]


# ─── the tenant policy's owner join ───


@pytest.mark.parametrize(
    'ref',
    [
        # ``sql.policy._owner_exists`` names the ancestor's table in ``SELECT 1 FROM ...``
        # and its tenant column in the ``= ANY(...)`` term beside it.
        ObjectRef('crossapp_tenant_ancestor', 'TenantedAncestor', None),
        ObjectRef('crossapp_tenant_ancestor', 'TenantedAncestor', 'label'),
    ],
)
def test_a_tenant_policy_records_the_ancestor_table_and_its_tenant_column(ref):
    """The policy is written into the *child's* app while the tenant column is resolved from
    ``column_owner``, an MTI ancestor that can live anywhere. The table ref it shares with the
    MTI redirect rule; the tenant column is named by nothing else in the chain."""
    assert ref in _refs_recorded_for('crossapp_tenant_child')


def test_the_policy_edge_points_at_the_migration_adding_the_tenant_column():
    """Not the one creating the ancestor's table, which the child's ``parent_ptr`` already
    orders it after: the ancestor was promoted to a tenanted model later, so the column the
    predicate reads arrives in ``0002`` and nothing but this edge orders the policy after it."""
    assert ('crossapp_tenant_ancestor', '0002_tenantedancestor_label') in _dependencies(
        'crossapp_tenant_child'
    )


def test_the_policy_is_planned_after_the_migration_adding_the_column_it_reads():
    """The ordering that matters, read off the graph rather than the file: a fresh
    ``migrate crossapp_tenant_child`` must reach the column before the ``CREATE POLICY``."""
    plan = _plan('crossapp_tenant_child')

    assert plan.index(('crossapp_tenant_ancestor', '0002_tenantedancestor_label')) < plan.index(
        ('crossapp_tenant_child', '0002_auto_enforcement')
    )


@override_settings(
    LOCAL_APPS=[
        'tests.testapp',
        'tests.crossapp_tenant_ancestor',
        'tests.crossapp_tenant_child',
    ]
)
def test_check_fails_when_the_policy_edge_is_absent():
    """The retrofit guard reaching the policy's owner join at all -- the edge is what the tenant
    column needs and nothing else in the chain names it. Which *form* of the table name the
    match has to accept is a separate question, asserted below off the rendered SQL."""
    with _without_dependency(
        'crossapp_tenant_child', 'crossapp_tenant_ancestor', '0002_tenantedancestor_label'
    ):
        with pytest.raises(CommandError) as raised:
            _check('crossapp_tenant_child')

        message = str(raised.value)
        assert 'crossapp_tenant_ancestor_tenantedancestor' in message
        assert "('crossapp_tenant_ancestor', '0002_tenantedancestor_label')," in message

    _check('crossapp_tenant_child')  # and green again once restored


def test_a_policy_naming_an_ancestor_renders_no_quoted_form_to_match():
    """Why the match had to widen, read off the SQL rather than a fixture that also carries a
    rule quoting the same table: ``create_table_rls`` names the ancestor and never quotes it, so
    the quoted membership test alone answers no on the one operation the guard is here for."""
    rendered = '\n'.join(
        sql.create_table_rls(
            table='child_tbl',
            columns={},
            owner_table='ancestor_tbl',
            owner_pk='id',
            child_pk='parent_ptr_id',
            owner_columns={'label': 'label_id'},
        )
    )

    assert '"ancestor_tbl"' not in rendered
    assert OperationsMixin._names_table(rendered, 'ancestor_tbl')


@pytest.mark.parametrize(
    ('content', 'named'),
    [
        ('FROM shop AS _guitars_owner', True),
        ('UPDATE "shop" SET', True),
        # A bare ``shop`` is a substring of ``shopping``; it also sits inside a dependency tuple
        # and inside the escaped literal an MTI ``updated_at`` trigger re-quotes at fire time.
        # None of the three names the table, and a false match blames an innocent file.
        ('FROM shopping AS _guitars_owner', False),
        ("    dependencies = [\n        ('shop', '0001_initial'),\n    ]", False),
        ("EXECUTE FUNCTION set_parent_updated_at('', 'shop', 'id', 'p_id')", False),
    ],
)
def test_a_bare_table_name_is_matched_on_sql_boundaries(content, named):
    """The unquoted form only -- a rule's quoted rendering needs no boundary check."""
    assert OperationsMixin._names_table(content, 'shop') is named


@pytest.mark.parametrize('table', ['MyTable', 'my-table', 'my table'])
def test_a_table_no_policy_could_spell_unquoted_answers_no_rather_than_raising(table):
    """``_quote_table`` takes any ``db_table``; ``policy._qualified_table`` refuses one that is
    not a plain lower-case identifier. A rule may still name such a table, and the guard walks
    every migration file -- so the first one not quoting it must not crash the report."""
    assert not OperationsMixin._names_table('nothing quoting it here', table)
    assert OperationsMixin._names_table(f'UPDATE "{table}" SET', table)
