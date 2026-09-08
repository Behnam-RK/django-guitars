"""Tests for the self-referential cascade trigger (2.8.0, ADR 0018): the statement-level
``AFTER UPDATE`` a ``ForeignKey('self', CASCADE)`` takes in place of a rule. A rule updating the
table it fires on is rejected at rewrite time, taking *every* ``UPDATE`` to that table with it."""

import pytest
from django.db import connection

from tests.testapp.models import Setlist, SetlistEntry


@pytest.fixture
def tree(db):
    """A three-level chain plus one ordinary cascade child hanging off each level, so both
    families are exercised by one archive: the trigger walks the tree, the cascade rule
    fires for each level's own ``UPDATE``."""
    root = Setlist.objects.create(title='root')
    middle = Setlist.objects.create(title='middle', parent=root)
    leaf = Setlist.objects.create(title='leaf', parent=middle)
    for node in (root, middle, leaf):
        SetlistEntry.objects.create(song=f'{node.title}-song', setlist=node)
    return root, middle, leaf


def _archived(model) -> set[str]:
    field = 'title' if model is Setlist else 'song'
    return {getattr(row, field) for row in model._all_objects.all() if row._deleted_at is not None}


def test_archiving_a_root_archives_every_descendant(tree):
    """The whole point: one statement, and the trigger re-fires itself for each level down."""
    root, _middle, _leaf = tree

    Setlist.objects.filter(pk=root.pk).delete()

    assert _archived(Setlist) == {'root', 'middle', 'leaf'}


def test_archiving_a_root_reaches_the_cascade_children_of_every_level(tree):
    """The trigger's own ``UPDATE`` is an ``UPDATE`` on the tree table like any other, so the
    ordinary cascade rule on that table fires for each level it archives. Without that, the
    trigger would archive the tree and strand every child hanging below the first level."""
    root, _middle, _leaf = tree

    Setlist.objects.filter(pk=root.pk).delete()

    assert _archived(SetlistEntry) == {'root-song', 'middle-song', 'leaf-song'}


def test_every_archived_row_has_its_updated_at_stamped(tree):
    """``_updated_at`` is stamped on every row the archive touches: tree rows by the assignment
    spliced into the trigger's own ``UPDATE``, cascade children by their own trigger. Asserted,
    not reasoned about -- it is the half of the archive no single mechanism owns."""
    # Against ``NOW()``, not against the value before the delete: ``NOW()`` is the *transaction*
    # timestamp, so every enforcement object here stamps one instant, and that instant precedes
    # the Python-side timestamps the rows were created with. "Newer than before" tests nothing.
    root, _middle, _leaf = tree
    with connection.cursor() as cursor:
        cursor.execute('SELECT NOW()')
        now = cursor.fetchone()[0]

    Setlist.objects.filter(pk=root.pk).delete()

    for row in Setlist._all_objects.all():
        assert row._updated_at == now, f'{row.title} kept a stale _updated_at'
    for row in SetlistEntry._all_objects.all():
        assert row._updated_at == now, f'{row.song} kept a stale _updated_at'


def test_archiving_a_leaf_touches_nothing_above_it(tree):
    """The cascade runs one way. A leaf has no children, so the trigger's UPDATE matches no
    row and the recursion stops on the first pass."""
    _root, _middle, leaf = tree

    Setlist.objects.filter(pk=leaf.pk).delete()

    assert _archived(Setlist) == {'leaf'}
    assert _archived(SetlistEntry) == {'leaf-song'}


def test_a_plain_save_on_the_tree_table_still_works(tree):
    """What a cascade *rule* on this shape would have cost: PostgreSQL rejects a rule whose
    action updates the table it fires on at rewrite time, so every UPDATE to the table fails
    -- an ordinary ``save()`` included, long after `migrate` reported success."""
    root, _middle, _leaf = tree

    root.title = 'renamed'
    root.save()

    assert Setlist.objects.get(pk=root.pk).title == 'renamed'
    assert _archived(Setlist) == set()


def test_a_bulk_update_of_an_unrelated_column_archives_nothing(tree):
    """The trigger fires on every ``UPDATE`` (a column list is not available beside transition
    tables), so its in-body guard is what keeps an ordinary write from walking the tree."""
    root, middle, leaf = tree
    for node in (root, middle, leaf):
        node.title = f'{node.title}-v2'
    Setlist.objects.bulk_update([root, middle, leaf], ['title'])

    assert _archived(Setlist) == set()


def test_restoring_a_parent_restores_nothing_below_it(tree):
    """The trigger reads the transition from live to archived only. Clearing ``_deleted_at``
    is the opposite transition and matches nothing, so an un-archive is never a cascade."""
    root, _middle, _leaf = tree
    Setlist.objects.filter(pk=root.pk).delete()

    Setlist._all_objects.filter(pk=root.pk).update(_deleted_at=None)

    assert _archived(Setlist) == {'middle', 'leaf'}


def test_hard_delete_on_the_root_really_removes_the_tree(tree):
    """``hard_delete`` switches the GUC the trigger's body reads, so the trigger stands aside.
    The *instance* form, which walks cascade children: Django emulates ``ON DELETE`` rather than
    declaring it, so the blunt queryset form would fail the deferred constraint at ``COMMIT``."""
    root, _middle, _leaf = tree

    root.hard_delete()

    assert Setlist._all_objects.count() == 0
    assert SetlistEntry._all_objects.count() == 0


def test_the_trigger_exists_on_the_table_and_the_rule_does_not(db):
    """The two families are mutually exclusive for this relation: a trigger named after the
    self key, and no ``soft_delete_related`` rule pointing the tree table at itself."""
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT tgname FROM pg_trigger '
            "WHERE tgrelid = 'testapp_setlist'::regclass AND NOT tgisinternal"
        )
        triggers = {row[0] for row in cursor.fetchall()}
        cursor.execute("SELECT rulename FROM pg_rules WHERE tablename = 'testapp_setlist'")
        rules = {row[0] for row in cursor.fetchall()}

    assert 'soft_delete_self_cascade_15_testapp_setlist_9_parent_id' in triggers
    assert 'soft_delete_related_testapp_setlist' not in rules
    # The ordinary cascade to the entry table is untouched by any of this.
    assert 'soft_delete_related_testapp_setlistentry' in rules
