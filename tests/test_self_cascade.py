"""Tests for the self-referential cascade, an arm of the owner's statement trigger since 2.20.0
(ADR 0042; through 2.19.x a trigger of its own, ADR 0018). A rule updating the table it fires on
is rejected at rewrite time, which is why it ever needed one."""

from importlib import import_module

import pytest
from django.db import connection, transaction
from django.db.models import F
from django.db.utils import NotSupportedError
from django.utils import timezone

from guitars.tenancy import tenancy_bypassed, tenant
from tests.conftest import execute, scalar
from tests.testapp.models import Label, Rack, Riser, Setlist, SetlistEntry, Troupe, Twig


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


def test_the_tree_rows_and_every_cascade_child_are_stamped(transactional_db):
    """Every row the archive reaches moves ``_updated_at``, cascade children included. Until
    2.19.0 those below the first level kept a stale value (rule at depth 1, trigger guarded);
    the row trigger has no such guard (ADR 0038)."""
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
    assert moved == {'root-song': True, 'middle-song': True, 'leaf-song': True}


def test_the_orm_delete_path_archives_the_tree_through_the_collector(tree):
    """``.delete()`` reaches the same end state by another road: the collector names every
    level in one statement, so the tree archives whether or not the trigger fires. Kept as the
    path an application takes, asserted as the collector's result -- see :func:`_raw_delete`."""
    root, _middle, _leaf = tree

    Setlist.objects.filter(pk=root.pk).delete()

    assert _archived(Setlist) == {'root', 'middle', 'leaf'}
    assert _archived(SetlistEntry) == {'root-song', 'middle-song', 'leaf-song'}


def test_the_orm_path_stamps_the_same_rows_the_raw_path_does(transactional_db):
    """The same archive reached by the collector, which names every level at depth 0 in one
    statement: every child is stamped, as on the raw path above. The two used to differ per
    caller, which is what kept ``delete()`` off the shortcut for a self-referential key."""
    root = Setlist.objects.create(title='root')
    middle = Setlist.objects.create(title='middle', parent=root)
    leaf = Setlist.objects.create(title='leaf', parent=middle)
    for node in (root, middle, leaf):
        SetlistEntry.objects.create(song=f'{node.title}-song', setlist=node)
    before = {row.song: row._updated_at for row in SetlistEntry._all_objects.all()}

    Setlist.objects.filter(pk=root.pk).delete()

    moved = {
        row.song: row._updated_at > before[row.song] for row in SetlistEntry._all_objects.all()
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


def test_restoring_a_parent_leaves_children_already_restored_untouched(tree):
    """Children are restored first on purpose: the revive finds nothing archived with the root,
    so it writes nothing. The revive itself is pinned below and in ``test_cascade_revive``."""
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


def test_the_owners_trigger_carries_the_self_key_and_no_trigger_of_its_own(db):
    """The self key is an arm of the tree table's owner trigger (ADR 0042); the trigger named
    after it (ADR 0018) is retired, and no ``soft_delete_related`` rule points the table at itself."""
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT tgname FROM pg_trigger '
            "WHERE tgrelid = 'testapp_setlist'::regclass AND NOT tgisinternal"
        )
        triggers = {row[0] for row in cursor.fetchall()}
        cursor.execute("SELECT rulename FROM pg_rules WHERE tablename = 'testapp_setlist'")
        rules = {row[0] for row in cursor.fetchall()}

    assert not [name for name in triggers if name.startswith('soft_delete_self_cascade')]
    assert 'soft_delete_related_testapp_setlist' not in rules
    # The ordinary cascade to the entry table is an arm of the tree table's owner trigger since
    # 2.19.0 (#80, ADR 0039), not a rule beside it: the table holds none but its own.
    assert 'soft_delete_related_testapp_setlistentry' not in rules
    assert 'soft_delete_cascade_on_15_testapp_setlist' in triggers
    # And the self key is in its body, which the entry table's arm alone would not show.
    body = scalar(
        "SELECT prosrc FROM pg_proc WHERE proname = 'soft_delete_cascade_on_15_testapp_setlist'"
    )
    assert 'UPDATE "testapp_setlist" AS guitars_child' in body


def test_an_owned_sweep_fires_from_inside_the_self_key_arm(db):
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

    with pytest.raises(NotSupportedError, match='archived a row whose primary key it also'):
        Setlist._all_objects.filter(pk=root.pk).update(
            id=F('id') + 1000, _deleted_at=timezone.now()
        )


def test_a_key_rewrite_on_a_parent_with_no_live_children_is_not_refused(db):
    """The refusal is narrow: with nothing live below it, there is no subtree to leak and so
    nothing the trigger needed to decide. A leaf re-keys and archives in one statement."""
    leaf = Setlist.objects.create(title='leaf')

    Setlist._all_objects.filter(pk=leaf.pk).update(id=F('id') + 1000, _deleted_at=timezone.now())

    assert _archived(Setlist) == {'leaf'}


def test_a_key_rewrite_that_archives_nothing_is_not_refused(db):
    """The guard is gated on an archive having happened, so renumbering keys never raises --
    that being the first half of the two-statement pattern the refusal itself prescribes."""
    # The children move with the key. Leaving one behind on the old key is the other shape a
    # gate-less guard would refuse, but it cannot be tested: the tree is then inconsistent at
    # ``COMMIT``, and the deferred foreign key rejects it whatever this trigger decides.
    root = Setlist.objects.create(title='root')
    child = Setlist.objects.create(title='child', parent=root)
    new_key = root.pk + 1000

    with connection.cursor() as cursor:
        cursor.execute('SET CONSTRAINTS ALL DEFERRED')
        cursor.execute(
            'UPDATE testapp_setlist SET id = CASE WHEN id = %s THEN %s ELSE id END, '
            'parent_id = CASE WHEN id = %s THEN %s ELSE parent_id END WHERE id IN (%s, %s)',
            [root.pk, new_key, child.pk, new_key, root.pk, child.pk],
        )

    assert _archived(Setlist) == set()


def test_a_key_rewrite_that_reparents_its_children_is_still_refused_without_the_guard(db):
    """The arm that matters most, and the one a guard reading only the *old* key misses: moving
    the children onto the new key in the same statement empties the old one, so nothing looks
    orphaned there while the subtree is just as unreachable."""
    root = Setlist.objects.create(title='root')
    child = Setlist.objects.create(title='child', parent=root)
    new_key = root.pk + 1000

    with pytest.raises(NotSupportedError, match='archived a row whose primary key it also'):
        with connection.cursor() as cursor:
            # With the cascade guard on (2.22.0) the child reads the root the statement has already
            # rewritten and is archived by it, so the guard is set aside to reach the refusal.
            cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
            cursor.execute(
                'ALTER TABLE testapp_setlist DISABLE TRIGGER soft_delete_guard_on_15_testapp_setlist'
            )
            cursor.execute('SET CONSTRAINTS ALL DEFERRED')
            cursor.execute(
                'UPDATE testapp_setlist SET id = CASE WHEN id = %s THEN %s ELSE id END, '
                'parent_id = CASE WHEN id = %s THEN %s ELSE parent_id END, '
                '_deleted_at = CASE WHEN id = %s THEN NOW() ELSE _deleted_at END '
                'WHERE id IN (%s, %s)',
                [root.pk, new_key, child.pk, new_key, root.pk, root.pk, child.pk],
            )


def test_a_key_rewrite_that_reparents_its_children_leaves_none_live_under_the_guard(db):
    """With the guard on, the same statement is refused or the child is archived with the root,
    by the table's physical order: either way no live row hangs under an archived one."""
    root = Setlist.objects.create(title='root')
    child = Setlist.objects.create(title='child', parent=root)
    new_key = root.pk + 1000

    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute('SET CONSTRAINTS ALL DEFERRED')
            cursor.execute(
                'UPDATE testapp_setlist SET id = CASE WHEN id = %s THEN %s ELSE id END, '
                'parent_id = CASE WHEN id = %s THEN %s ELSE parent_id END, '
                '_deleted_at = CASE WHEN id = %s THEN NOW() ELSE _deleted_at END '
                'WHERE id IN (%s, %s)',
                [root.pk, new_key, child.pk, new_key, root.pk, root.pk, child.pk],
            )
    except NotSupportedError:
        return

    assert Setlist._all_objects.get(pk=child.pk)._deleted_at is not None
    assert Setlist._all_objects.get(pk=child.pk).parent_id == new_key


T_PARENT = '2021-06-15T12:30:00Z'


def _stamp(pk: int):
    return Setlist._all_objects.get(pk=pk)._deleted_at


def test_every_level_carries_the_parents_own_stamp(tree):
    """An arm of the owner's trigger (ADR 0042) copies the parent's ``_deleted_at``. The trigger
    it replaced wrote ``NOW()``, which no provenance test could match. An explicit stamp, since
    this test's one transaction shares a ``NOW()`` with everything in it."""
    root, middle, leaf = tree

    execute(
        'UPDATE testapp_setlist SET _deleted_at = %s WHERE id = %s', params=[T_PARENT, root.pk]
    )

    assert str(_stamp(middle.pk)) == str(_stamp(leaf.pk)) == str(_stamp(root.pk))
    assert _stamp(root.pk).isoformat().startswith('2021-06-15T12:30:00')


def test_a_child_archived_earlier_keeps_its_own_stamp(tree):
    root, middle, _leaf = tree
    execute(
        "UPDATE testapp_setlist SET _deleted_at = '2000-01-01T00:00:00Z' WHERE id = %s",
        params=[middle.pk],
    )

    execute(
        'UPDATE testapp_setlist SET _deleted_at = %s WHERE id = %s', params=[T_PARENT, root.pk]
    )

    assert _stamp(middle.pk).isoformat().startswith('2000-01-01')


def test_restoring_a_parent_restores_the_subtree_archived_with_it_and_no_other(tree):
    root, middle, leaf = tree
    execute(
        "UPDATE testapp_setlist SET _deleted_at = '2000-01-01T00:00:00Z' WHERE id = %s",
        params=[leaf.pk],
    )
    execute(
        'UPDATE testapp_setlist SET _deleted_at = %s WHERE id = %s', params=[T_PARENT, root.pk]
    )

    execute('UPDATE testapp_setlist SET _deleted_at = NULL WHERE id = %s', params=[root.pk])

    assert _stamp(middle.pk) is None
    assert _stamp(leaf.pk).isoformat().startswith('2000-01-01')


def test_unapplying_the_retirement_puts_the_self_trigger_back(db):
    """Off the committed migration itself: the key still cascades, so the retirement is a
    supersession and its reverse rebuilds the trigger, where one for a key gone refuses."""
    module = import_module('tests.testapp.migrations.0093_auto_enforcement')
    (reverse,) = [
        op.reverse_sql
        for op in module.Migration.operations
        if 'DROP TRIGGER IF EXISTS "soft_delete_self_cascade_15_testapp_setlist_9_parent_id"'
        in op.sql
    ]
    catalogue = (
        'SELECT count(*) FROM pg_trigger WHERE tgname = '
        "'soft_delete_self_cascade_15_testapp_setlist_9_parent_id'"
    )

    with transaction.atomic():
        assert scalar(catalogue) == 0
        execute(reverse)
        rebuilt = scalar(catalogue)
        transaction.set_rollback(True)

    assert rebuilt == 1


def test_the_self_key_is_filed_as_an_arm_of_its_owner(db):
    """Off the registry-wide sweep the owner trigger is rendered from: without the self loop in
    ``_cascade_key_maps`` the key is silently left without its cascade, ``--check`` green."""
    from guitars.management.enforcement.command import Command  # noqa: PLC0415

    arms = Command()._revive_arms_by_owner()['testapp_setlist']

    assert ('testapp_setlist', 'testapp_setlist', 'parent_id') in arms


class TestAModelWithNoUpdatedAt:
    """``Twig`` is soft-deletable and nothing more: its arm has no ``_updated_at`` to stamp, so
    ``_arm_slots`` renders the assignment empty and the ``UPDATE`` writes ``_deleted_at`` alone."""

    @staticmethod
    def _archive(pk: int) -> None:
        with connection.cursor() as cursor:
            cursor.execute('UPDATE testapp_twig SET _deleted_at = NOW() WHERE id = %s', [pk])

    def test_the_arms_assign_no_updated_at(self):
        from guitars.management.enforcement.command import Command  # noqa: PLC0415

        slots = Command()._revive_owner_slots('testapp_twig')

        assert 'SET _deleted_at = guitars_archived._deleted_at' in slots['archive_arms']
        assert '_updated_at' not in slots['archive_arms']

    def test_an_archive_takes_the_tree_with_the_parents_stamp(self, db):
        root = Twig.objects.create()
        child = Twig.objects.create(parent=root)
        grandchild = Twig.objects.create(parent=child)

        self._archive(root.pk)

        stamps = {
            Twig._all_objects.get(pk=twig.pk)._deleted_at for twig in (root, child, grandchild)
        }
        assert len(stamps) == 1
        assert None not in stamps

    def test_a_restore_revives_it(self, db):
        root = Twig.objects.create()
        child = Twig.objects.create(parent=root)
        self._archive(root.pk)

        with connection.cursor() as cursor:
            cursor.execute('UPDATE testapp_twig SET _deleted_at = NULL WHERE id = %s', [root.pk])

        assert Twig.objects.filter(pk__in=[root.pk, child.pk]).count() == 2
