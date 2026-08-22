"""Tests for ``enforcement.graph`` (2.5.0): resolving an object an emitted rule names to the
migration that makes it exist. Unit tests over the checked-in history and hand-built graphs;
what the caller does with the edges is ``tests/test_crossapp_migration_edges.py``."""

import pytest
from django.db.migrations import Migration
from django.db.migrations.graph import MigrationGraph
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import (
    AddField,
    AlterField,
    AlterModelTable,
    CreateModel,
    RenameField,
    RenameModel,
)
from django.db.models import CASCADE, AutoField, CharField, ForeignKey

from guitars.management.enforcement.graph import (
    ObjectRef,
    resolve_dependencies,
    resolve_object_migration,
    would_close_a_cycle,
)


@pytest.fixture
def loader():
    """The project's real history. Read-only -- every test here only asks it questions."""
    return MigrationLoader(None, ignore_no_migrations=True)


class _FakeLoader:
    """A loader over migrations built in the test, for histories the checked-in ones have no
    instance of -- a rename, a squash ordering two names against their numeric prefix. Only
    ``graph`` and ``disk_migrations`` are read, which is the whole surface ``graph`` uses."""

    def __init__(self, migrations: dict[tuple[str, str], Migration]):
        self.disk_migrations = migrations
        self.graph = MigrationGraph()
        for key, migration in migrations.items():
            self.graph.add_node(key, migration)
        for key, migration in migrations.items():
            for parent in migration.dependencies:
                self.graph.add_dependency(str(key), key, parent)


def _migration(name: str, app: str, operations: list, dependencies: list) -> Migration:
    migration = Migration(name, app)
    migration.operations = operations
    migration.dependencies = dependencies
    return migration


# ─── resolution against the real history ───


def test_a_field_resolves_to_the_migration_that_creates_it_not_the_app_leaf(loader):
    """The whole point of the edge: depending on the leaf over-constrains the graph and drags
    unrelated migrations forward, which is what the consumer's hand-patch did. ``Kiosk.placard``
    arrives with its ``CreateModel``, long before ``testapp``'s current leaf."""
    resolved = resolve_object_migration(loader, ObjectRef('testapp', 'Kiosk', 'placard'))

    assert resolved == ('testapp', '0029_placard_kiosk_foyer')
    leaves = {name for _, name in loader.graph.leaf_nodes('testapp')}
    assert resolved[1] not in leaves


def test_a_table_only_ref_resolves_to_its_create_model(loader):
    """A cascade rule names the related *table* and no column of it, as does the MTI ancestor a
    joined arm takes liveness from. ``field=None`` asks that question."""
    assert resolve_object_migration(loader, ObjectRef('testapp', 'Placard')) == (
        'testapp',
        '0029_placard_kiosk_foyer',
    )


@pytest.mark.parametrize(
    'ref',
    [
        ObjectRef('testapp', 'NoSuchModel', 'whatever'),
        ObjectRef('testapp', 'Kiosk', 'no_such_field'),
        ObjectRef('no_such_app', 'Thing', 'field'),
    ],
)
def test_an_unresolvable_ref_answers_none_rather_than_guessing(loader, ref):
    """``None`` means "emit no edge", which is exactly 2.4.2's behaviour. An app with no
    migrations at all reaches this too, and inventing an edge there would name nothing."""
    assert resolve_object_migration(loader, ref) is None


# ─── resolution over histories the real one has no instance of ───


def _renamed_field_history() -> _FakeLoader:
    return _FakeLoader(
        {
            ('shop', '0001_initial'): _migration(
                '0001_initial',
                'shop',
                [CreateModel('Shop', [('id', AutoField(primary_key=True))])],
                [],
            ),
            ('shop', '0002_add'): _migration(
                '0002_add',
                'shop',
                [AddField('shop', 'old_name', CharField(max_length=10))],
                [('shop', '0001_initial')],
            ),
            ('shop', '0003_rename'): _migration(
                '0003_rename',
                'shop',
                [RenameField('shop', 'old_name', 'new_name')],
                [('shop', '0002_add')],
            ),
        }
    )


def test_a_renamed_field_resolves_to_the_rename_not_the_original_add():
    """The rule names the *current* spelling, which only exists from the rename onwards.
    Depending on the ``AddField`` would let the rule be created against a column still called
    something else -- the failure the edge exists to prevent, one migration further back."""
    resolved = resolve_object_migration(
        _renamed_field_history(), ObjectRef('shop', 'Shop', 'new_name')
    )

    assert resolved == ('shop', '0003_rename')


def test_a_name_the_rename_took_away_still_resolves_to_where_it_was_added():
    """Documented, not desirable: nothing tracks that ``old_name`` is gone. Unreachable from the
    generator, whose refs come off live model fields -- a column no model has is one no rule
    names -- and asserted so a caller feeding refs from elsewhere finds the limit here."""
    history = _renamed_field_history()

    assert resolve_object_migration(history, ObjectRef('shop', 'Shop', 'old_name')) == (
        'shop',
        '0002_add',
    )


def _altered_field_history(field: CharField) -> _FakeLoader:
    """``code`` created in ``0001``, then altered in ``0002`` with whatever *field* declares."""
    return _FakeLoader(
        {
            ('shop', '0001_initial'): _migration(
                '0001_initial',
                'shop',
                [
                    CreateModel(
                        'Shop',
                        [('id', AutoField(primary_key=True)), ('code', CharField(max_length=5))],
                    )
                ],
                [],
            ),
            ('shop', '0002_alter'): _migration(
                '0002_alter',
                'shop',
                [AlterField('shop', 'code', field)],
                [('shop', '0001_initial')],
            ),
        }
    )


def test_an_alter_field_moving_the_db_column_establishes_the_column():
    """``db_column`` is the one thing an ``AlterField`` changes about the *physical* column a rule
    names, so the column under its current spelling only exists from the alter on."""
    history = _altered_field_history(CharField(max_length=5, db_column='code_v2'))

    assert resolve_object_migration(history, ObjectRef('shop', 'Shop', 'code')) == (
        'shop',
        '0002_alter',
    )


def test_an_alter_field_leaving_the_column_alone_does_not_move_the_edge():
    """The narrowing that matters: this resolver takes the **last** match, so counting every
    ``AlterField`` would drag the edge off ``0001`` and onto an unrelated ``null=True`` --
    over-constraining the graph exactly the way depending on the app's leaf does."""
    history = _altered_field_history(CharField(max_length=5, null=True))

    assert resolve_object_migration(history, ObjectRef('shop', 'Shop', 'code')) == (
        'shop',
        '0001_initial',
    )


def test_a_renamed_model_establishes_its_columns_under_the_new_name():
    """A renamed model moves the table its columns live on, so a column reached through the new
    model name only exists from the rename on -- even though no field operation mentions it."""
    history = _FakeLoader(
        {
            ('shop', '0001_initial'): _migration(
                '0001_initial',
                'shop',
                [
                    CreateModel(
                        'Outlet',
                        [('id', AutoField(primary_key=True)), ('code', CharField(max_length=5))],
                    )
                ],
                [],
            ),
            ('shop', '0002_rename'): _migration(
                '0002_rename', 'shop', [RenameModel('Outlet', 'Shop')], [('shop', '0001_initial')]
            ),
        }
    )

    assert resolve_object_migration(history, ObjectRef('shop', 'Shop', 'code')) == (
        'shop',
        '0002_rename',
    )


def _retabled_history() -> _FakeLoader:
    return _FakeLoader(
        {
            ('shop', '0001_initial'): _migration(
                '0001_initial',
                'shop',
                [
                    CreateModel(
                        'Shop',
                        [('id', AutoField(primary_key=True)), ('code', CharField(max_length=5))],
                    )
                ],
                [],
            ),
            ('shop', '0002_table'): _migration(
                '0002_table',
                'shop',
                [AlterModelTable('Shop', 'shop_shop_v2')],
                [('shop', '0001_initial')],
            ),
        }
    )


def test_a_retabled_model_resolves_to_the_alter_model_table():
    """``db_table`` moving is the table-level equivalent of a rename: the rule names the new
    table, which does not exist until the ``AlterModelTable`` runs."""
    assert resolve_object_migration(_retabled_history(), ObjectRef('shop', 'Shop')) == (
        'shop',
        '0002_table',
    )


def test_a_retabled_model_moves_its_columns_too():
    """A column is no more present on a table that does not exist yet, so a *column* ref has to
    resolve to the ``AlterModelTable`` as well. Resolving it to the original ``CreateModel``
    would let a fresh ``migrate`` create the rule before the table it names was there."""
    assert resolve_object_migration(_retabled_history(), ObjectRef('shop', 'Shop', 'code')) == (
        'shop',
        '0002_table',
    )


def test_order_is_read_off_the_graph_not_the_numeric_prefix():
    """A hand-written or squashed migration can order two names against their spelling. Sorting
    by name would pick the wrong "last", which for a rename is the wrong column entirely."""
    history = _FakeLoader(
        {
            ('shop', '0002_first_by_name'): _migration(
                '0002_first_by_name',
                'shop',
                [CreateModel('Shop', [('id', AutoField(primary_key=True))])],
                [],
            ),
            # Depends on the higher-numbered one, so it runs *after* it despite sorting before.
            ('shop', '0001_actually_later'): _migration(
                '0001_actually_later',
                'shop',
                [RenameModel('Shop', 'Outlet')],
                [('shop', '0002_first_by_name')],
            ),
        }
    )

    assert resolve_object_migration(history, ObjectRef('shop', 'Outlet')) == (
        'shop',
        '0001_actually_later',
    )


# ─── the edge list a writer gets ───


def test_a_ref_into_the_app_being_written_emits_no_edge(loader):
    """The scaffold already depends on its own app's leaf, so that history is ordered without
    one -- and an edge into the app being written could only name the file being created."""
    edges, unresolved = resolve_dependencies(
        loader, [ObjectRef('testapp', 'Kiosk', 'placard')], own_app='testapp'
    )

    assert (edges, unresolved) == ([], [])


def test_two_refs_resolving_to_one_migration_emit_one_edge(loader):
    """``Kiosk.placard`` and ``Foyer.placard`` both arrive in ``0029``. A rule reading both arms
    must not list it twice; the writer dedupes too, but not against a list built here."""
    edges, unresolved = resolve_dependencies(
        loader,
        [ObjectRef('testapp', 'Kiosk', 'placard'), ObjectRef('testapp', 'Foyer', 'placard')],
        own_app='elsewhere',
    )

    assert edges == [('testapp', '0029_placard_kiosk_foyer')]
    assert unresolved == []


def test_an_unresolvable_ref_is_reported_rather_than_dropped(loader):
    """Separated from the edges so the caller can warn: silently emitting nothing is what 2.4.2
    did, and the whole point of this release is that the silence was the bug."""
    edges, unresolved = resolve_dependencies(
        loader,
        [ObjectRef('testapp', 'Kiosk', 'placard'), ObjectRef('testapp', 'Ghost', 'col')],
        own_app='elsewhere',
    )

    assert edges == [('testapp', '0029_placard_kiosk_foyer')]
    assert [ref.describe() for ref in unresolved] == ['testapp.Ghost.col']


# ─── cycle safety ───


def _two_app_history() -> _FakeLoader:
    return _FakeLoader(
        {
            ('a', '0001_initial'): _migration(
                '0001_initial', 'a', [CreateModel('A', [('id', AutoField(primary_key=True))])], []
            ),
            ('b', '0001_initial'): _migration(
                '0001_initial',
                'b',
                [
                    CreateModel(
                        'B',
                        [
                            ('id', AutoField(primary_key=True)),
                            ('a', ForeignKey('a.A', CASCADE)),
                        ],
                    )
                ],
                [('a', '0001_initial')],
            ),
            ('a', '0002_rule'): _migration('0002_rule', 'a', [], [('a', '0001_initial')]),
        }
    )


def test_an_edge_to_an_older_migration_does_not_close_a_cycle():
    """The ordinary case, and the reason the field-creating migration is the right target: it
    is always older than the rule naming it, so the edge points backwards and cannot loop."""
    history = _two_app_history()

    assert not would_close_a_cycle(history, ('b', '0001_initial'), ('a', '0001_initial'))


def test_an_edge_whose_target_already_depends_on_the_migration_closes_a_cycle():
    """``b.0001`` already depends on ``a.0001``, so making ``a.0001`` depend back on ``b.0001``
    is a loop. Asserted rather than assumed -- Django rejects such a graph with
    ``CircularDependencyError``, which bricks ``migrate`` outright rather than in one order."""
    history = _two_app_history()

    assert would_close_a_cycle(history, ('a', '0001_initial'), ('b', '0001_initial'))


def test_a_node_the_graph_does_not_have_is_not_a_cycle():
    """The migration being written is not in the loaded graph yet -- it is the file about to be
    scaffolded -- so an unknown node answers "no cycle" rather than raising."""
    history = _two_app_history()

    assert not would_close_a_cycle(history, ('a', '9999_not_written_yet'), ('a', '0001_initial'))


def test_a_graph_node_with_no_migration_on_disk_is_skipped():
    """The graph and ``disk_migrations`` can diverge -- Django's loader adds replacement nodes
    for a squash whose replaced files are gone. Skipped rather than raising: the question is
    which migration creates an object, and a node with no operations answers nothing."""
    history = _renamed_field_history()
    del history.disk_migrations[('shop', '0003_rename')]

    # The node is still on the graph, so it is still walked -- and the rename it carried is
    # gone with it, so the answer falls back to where the field was added.
    assert resolve_object_migration(history, ObjectRef('shop', 'Shop', 'new_name')) is None
    assert resolve_object_migration(history, ObjectRef('shop', 'Shop', 'old_name')) == (
        'shop',
        '0002_add',
    )
