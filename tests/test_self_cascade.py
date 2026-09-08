"""Tests for the self-referential cascade trigger (2.8.0, ADR 0018): the statement-level
``AFTER UPDATE`` a ``ForeignKey('self', CASCADE)`` takes in place of a rule. A rule updating the
table it fires on is rejected at rewrite time, taking *every* ``UPDATE`` to that table with it."""

import pytest
from django.db import connection
from django.db.models import F
from django.db.utils import NotSupportedError
from django.utils import timezone

from guitars.tenancy import tenancy_bypassed, tenant
from tests.testapp.models import Label, Rack, Riser, Setlist, SetlistEntry, Troupe


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


@pytest.fixture
def tenanted_trees(db):
    """Two tenants, each with a two-level ``Troupe`` tree, for the RLS assertions below."""
    import types

    out = []
    for name in ('a', 'b'):
        label = Label.objects.create(name=f'label-{name}')
        with tenant(label=label):
            root = Troupe.objects.create(name=f'{name}-root')
            child = Troupe.objects.create(name=f'{name}-child', parent=root)
        out.append(types.SimpleNamespace(label=label, root=root, child=child))
    return tuple(out)


#: The human-readable field each model in this module is identified by in an assertion.
_LABEL_FIELD = {Setlist: 'title', SetlistEntry: 'song', Rack: 'label', Riser: 'height'}


def _archived(model) -> set[str]:
    field = _LABEL_FIELD[model]
    return {getattr(row, field) for row in model._all_objects.all() if row._deleted_at is not None}


def _raw_delete(pk: int) -> None:
    """Archive one row without Django's collector, which is the only way to see this trigger
    work: ``.delete()`` walks the subtree in Python and names every level in one statement, so
    the rule archives the lot at depth 0 and the trigger matches nothing."""
    # Verified rather than argued: with every archive test on the ORM path, dropping the
    # trigger left all nine of them green. On this path four of them fail.
    _raw_delete_from('testapp_setlist', pk)


def _raw_delete_from(table: str, pk: int) -> None:
    """:func:`_raw_delete` for any of this module's tables. The name is a literal from the
    call site, never user input, so the interpolation is the only way to parameterise it."""
    with connection.cursor() as cursor:
        cursor.execute(f'DELETE FROM {table} WHERE id = %s', [pk])  # noqa: S608


def test_archiving_a_root_archives_every_descendant(tree):
    """The whole point: one statement naming only the root, and the trigger re-fires itself
    for each level down. Raw, so the collector cannot archive the subtree first -- see
    :func:`_raw_delete`."""
    root, _middle, _leaf = tree

    _raw_delete(root.pk)

    assert _archived(Setlist) == {'root', 'middle', 'leaf'}


def test_archiving_a_root_reaches_the_cascade_children_of_every_level(tree):
    """The trigger's own ``UPDATE`` is an ``UPDATE`` on the tree table like any other, so the
    ordinary cascade rule on that table fires for each level it archives. Without that, the
    trigger would archive the tree and strand every child hanging below the first level."""
    root, _middle, _leaf = tree

    _raw_delete(root.pk)

    assert _archived(SetlistEntry) == {'root-song', 'middle-song', 'leaf-song'}


def test_the_tree_rows_are_stamped_and_the_deeper_cascade_children_are_not(transactional_db):
    """The archive's ``_updated_at`` splits in two and this pins both halves: tree rows stamped
    by the trigger's own spliced ``UPDATE``, cascade children below the first level **not** --
    their rule fires at depth 1, where ``updated_at_trigger``'s ``WHEN`` suppresses it."""
    # ``transactional_db`` because ``NOW()`` is the *transaction* timestamp: under one
    # surrounding transaction the rows are created at the very instant the enforcement later
    # stamps, and every comparison below holds whether or not anything stamped anything.
    root = Setlist.objects.create(title='root')
    middle = Setlist.objects.create(title='middle', parent=root)
    leaf = Setlist.objects.create(title='leaf', parent=middle)
    for node in (root, middle, leaf):
        SetlistEntry.objects.create(song=f'{node.title}-song', setlist=node)
    before = {row.pk: row._updated_at for row in Setlist._all_objects.all()}
    entries_before = {row.song: row._updated_at for row in SetlistEntry._all_objects.all()}

    _raw_delete(root.pk)

    for row in Setlist._all_objects.all():
        assert row._updated_at > before[row.pk], f'{row.title} kept a stale _updated_at'
    moved = {
        row.song: row._updated_at > entries_before[row.song]
        for row in SetlistEntry._all_objects.all()
    }
    assert moved == {'root-song': True, 'middle-song': False, 'leaf-song': False}


def test_the_orm_delete_path_archives_the_tree_through_the_collector(tree):
    """``.delete()`` reaches the same end state by another road: the collector names every
    level in one statement, so the tree archives whether or not the trigger fires. Kept as the
    path an application takes, asserted as the collector's result -- see :func:`_raw_delete`."""
    root, _middle, _leaf = tree

    Setlist.objects.filter(pk=root.pk).delete()

    assert _archived(Setlist) == {'root', 'middle', 'leaf'}
    assert _archived(SetlistEntry) == {'root-song', 'middle-song', 'leaf-song'}


def test_the_orm_path_stamps_the_cascade_children_the_raw_path_leaves_stale(transactional_db):
    """The other half of the split: the gap is not "below the first level" but "reached by the
    trigger rather than by one collector statement". The collector names every level at depth 0,
    so every child is stamped here -- the same archive, a different outcome per caller."""
    root = Setlist.objects.create(title='root')
    middle = Setlist.objects.create(title='middle', parent=root)
    leaf = Setlist.objects.create(title='leaf', parent=middle)
    for node in (root, middle, leaf):
        SetlistEntry.objects.create(song=f'{node.title}-song', setlist=node)
    before = {row.song: row._updated_at for row in SetlistEntry._all_objects.all()}

    Setlist.objects.filter(pk=root.pk).delete()

    moved = {
        row.song: row._updated_at > before[row.song]
        for row in SetlistEntry._all_objects.all()
    }
    assert moved == {'root-song': True, 'middle-song': True, 'leaf-song': True}


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


def test_restoring_a_parent_cascades_nothing_either_way(tree):
    """The trigger reads the live-to-archived transition only, so an un-archive is never a
    cascade. Children left **live** under an archived parent on purpose: with them already
    archived, a trigger firing on either direction would find nothing to touch and still pass."""
    root, middle, leaf = tree
    _raw_delete(root.pk)
    Setlist._all_objects.filter(pk__in=[middle.pk, leaf.pk]).update(_deleted_at=None)
    assert _archived(Setlist) == {'root'}

    Setlist._all_objects.filter(pk=root.pk).update(_deleted_at=None)

    assert _archived(Setlist) == set()


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


def test_an_owned_sweep_fires_from_inside_the_self_cascade_trigger(db):
    """Where the two statement-level families meet, in the shape needing the **sweep** and not
    the rule beside it: two child racks share one riser, so the trigger archives both owners in
    one depth-1 ``UPDATE`` and each reads the other as live to the rule's last-owner guard."""
    # One owner per riser would not do: the rule's NOT EXISTS excludes the archiving row
    # itself, so it stamps a solely-owned riser unaided and dropping the sweep changes nothing.
    shared = Riser.objects.create(height='shared')
    root = Rack.objects.create(label='root', riser=Riser.objects.create(height='root-riser'))
    Rack.objects.create(label='c1', parent=root, riser=shared)
    Rack.objects.create(label='c2', parent=root, riser=shared)

    _raw_delete_from('testapp_rack', root.pk)

    assert _archived(Rack) == {'root', 'c1', 'c2'}
    assert _archived(Riser) == {'root-riser', 'shared'}


def test_a_tenanted_tree_cascades_within_the_scope(tenanted_trees):
    """ADR 0018 claims parity with the cascade rules under tenancy. Transition tables are not
    RLS-filtered, so the in-scope half is worth measuring rather than reasoning about."""
    a, _b = tenanted_trees

    with tenant(label=a.label):
        _raw_delete_from('testapp_troupe', a.root.pk)

    with tenancy_bypassed():
        assert {row.name for row in Troupe._all_objects.all() if row._deleted_at} == {
            'a-root',
            'a-child',
        }


def test_a_tenanted_tree_cannot_archive_another_tenants_child(tenanted_trees):
    """The direction the ADR commits to: a child the active scope cannot see is **left live**,
    never archived. Not vacuous -- the same archive under ``tenancy_bypassed()`` *does* take
    the cross-tenant child, so the policy is what holds it, not an absence of reach."""
    a, b = tenanted_trees
    # b's child is reparented under a's root with tenancy bypassed -- the cross-tenant row a
    # scoped archive must not touch. Only the policy stands between them.
    with tenancy_bypassed():
        Troupe._all_objects.filter(pk=b.child.pk).update(parent_id=a.root.pk)

    with tenant(label=a.label):
        _raw_delete_from('testapp_troupe', a.root.pk)

    with tenancy_bypassed():
        assert Troupe._all_objects.get(pk=b.child.pk)._deleted_at is None


def test_a_key_rewrite_on_a_parent_with_live_children_is_refused(db):
    """A statement that rewrites a live parent's primary key leaves the trigger unable to say
    which after-row it became, and so whether it was archived -- it would silently leak the
    whole subtree. The owned sweep refuses the same ambiguity, so this one does too."""
    root = Setlist.objects.create(title='root')
    Setlist.objects.create(title='child', parent=root)

    with pytest.raises(NotSupportedError, match='rewrote the primary key of a live row'):
        Setlist._all_objects.filter(pk=root.pk).update(
            id=F('id') + 1000, _deleted_at=timezone.now()
        )


def test_a_key_rewrite_on_a_parent_with_no_live_children_is_not_refused(db):
    """The refusal is narrow: with nothing live below it, there is no subtree to leak and so
    nothing the trigger needed to decide. A leaf re-keys and archives in one statement."""
    leaf = Setlist.objects.create(title='leaf')

    Setlist._all_objects.filter(pk=leaf.pk).update(id=F('id') + 1000, _deleted_at=timezone.now())

    assert _archived(Setlist) == {'leaf'}
