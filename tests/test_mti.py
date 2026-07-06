"""Tests for multi-table-inheritance (MTI) support in dated / soft-deletable models.

These exercise the PG triggers and rules generated for MTI children, whose ``_updated_at`` /
``_deleted_at`` columns physically live on an ancestor table. See ``tests/testapp/models.py``:
``Ensemble`` (MTI parent) -> ``Orchestra`` (child) -> ``ChamberOrchestra`` (grandchild), plus
``Section`` (a soft-deletable model with a CASCADE FK into the MTI child ``Orchestra``).
"""

import types

import pytest
from django.db import connection

from guitars.models.soft_deletion import _mti_table_chain
from tests.testapp.models import ChamberOrchestra, Ensemble, Orchestra, Section


def _row_exists(model: type, pk: int) -> bool:
    """Whether a physical row exists in *model*'s own table (bypasses the ORM/joins).

    Uses the model's own PK column, which for an MTI child is its parent-link column
    (``ensemble_ptr_id`` / ``orchestra_ptr_id``), all sharing the same value down the chain.
    """
    table = model._meta.db_table
    pk_column = model._meta.pk.column
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT 1 FROM {table} WHERE {pk_column} = %s', [pk])
        return cursor.fetchone() is not None


@pytest.mark.django_db
def test_soft_delete_child_preserves_all_tables_and_marks_parent():
    orchestra = Orchestra.objects.create(name='LSO', conductor='Rattle')
    pk = orchestra.pk

    orchestra.delete()  # child-table DELETE -> MTI redirect rule -> soft-delete on ensemble

    # Hidden from the live manager, visible in archives (join across both tables works).
    assert not Orchestra.objects.filter(pk=pk).exists()
    archived = Orchestra._archives.get(pk=pk)
    assert archived._deleted_at is not None
    assert archived.conductor == 'Rattle'  # child-only column preserved

    # Both physical rows still exist: the child row was preserved, the parent only marked.
    assert _row_exists(Orchestra, pk)
    assert _row_exists(Ensemble, pk)


@pytest.mark.django_db
def test_soft_delete_via_parent_instance_is_consistent():
    orchestra = Orchestra.objects.create(name='BPO', conductor='Petrenko')
    pk = orchestra.pk

    # Delete through the parent model instance; Django cascades to the child table first.
    Ensemble.objects.get(pk=pk).delete()

    assert not Orchestra.objects.filter(pk=pk).exists()
    assert not Ensemble.objects.filter(pk=pk).exists()
    assert Orchestra._archives.filter(pk=pk).exists()
    assert _row_exists(Orchestra, pk)


@pytest.mark.django_db(transaction=True)
def test_child_only_update_bumps_parent_updated_at():
    orchestra = Orchestra.objects.create(name='NYP', conductor='Bernstein')
    pk = orchestra.pk
    before = Ensemble._all_objects.get(pk=pk)._updated_at

    # QuerySet.update on a child-only field touches only testapp_orchestra; the parent trigger
    # must still bump testapp_ensemble._updated_at.
    Orchestra.objects.filter(pk=pk).update(conductor='Mahler')

    after = Ensemble._all_objects.get(pk=pk)._updated_at
    assert after > before


@pytest.mark.django_db
def test_multilevel_child_soft_delete_resolves_to_root():
    chamber = ChamberOrchestra.objects.create(name='ASMF', conductor='Marriner', seats=40)
    pk = chamber.pk

    chamber.delete()

    assert not ChamberOrchestra.objects.filter(pk=pk).exists()
    assert ChamberOrchestra._archives.filter(pk=pk).exists()
    # All three tables in the chain are preserved; the root Ensemble row carries _deleted_at.
    assert _row_exists(ChamberOrchestra, pk)
    assert _row_exists(Orchestra, pk)
    ensemble = Ensemble._all_objects.get(pk=pk)
    assert ensemble._deleted_at is not None


@pytest.mark.django_db
def test_delete_child_cascades_soft_delete_to_related():
    orchestra = Orchestra.objects.create(name='VPO', conductor='Kleiber')
    section = Section.objects.create(name='Strings', orchestra=orchestra)

    orchestra.delete()  # fires the cascade rule that lives on the ensemble table

    assert not Section.objects.filter(pk=section.pk).exists()
    assert Section._archives.filter(pk=section.pk).exists()


@pytest.mark.django_db
def test_managers_partition_mti_rows():
    live = Orchestra.objects.create(name='Live', conductor='A')
    doomed = Orchestra.objects.create(name='Doomed', conductor='B')
    doomed.delete()

    assert set(Orchestra.objects.values_list('name', flat=True)) == {'Live'}
    assert set(Orchestra._archives.values_list('name', flat=True)) == {'Doomed'}
    assert set(Orchestra._all_objects.values_list('name', flat=True)) == {'Live', 'Doomed'}
    assert Orchestra._all_objects.get(pk=live.pk) == live


@pytest.mark.django_db(transaction=True)
def test_instance_hard_delete_removes_whole_chain_and_children():
    chamber = ChamberOrchestra.objects.create(name='ASMF', conductor='Marriner', seats=40)
    section = Section.objects.create(name='Winds', orchestra=chamber)
    pk = chamber.pk

    chamber.hard_delete()

    # No orphaned ancestor rows anywhere in the chain, and the CASCADE child is gone too.
    assert not _row_exists(ChamberOrchestra, pk)
    assert not _row_exists(Orchestra, pk)
    assert not _row_exists(Ensemble, pk)
    assert not Section._all_objects.filter(pk=section.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_queryset_hard_delete_removes_ancestor_rows():
    a = Orchestra.objects.create(name='A', conductor='x')
    b = Orchestra.objects.create(name='B', conductor='y')
    pks = [a.pk, b.pk]

    Orchestra._all_objects.all().hard_delete()

    assert Orchestra._all_objects.count() == 0
    for pk in pks:
        assert not _row_exists(Orchestra, pk)
        assert not _row_exists(Ensemble, pk)  # no orphaned parent rows


class _FakeMTIModel:
    """Minimal stand-in for a Django model class, exposing only what
    ``_mti_table_chain`` reads off ``_meta``. Plain identity hash/eq (unlike
    ``types.SimpleNamespace``, which is unhashable) so it can go in the
    traversal's ``seen`` set.
    """

    def __init__(self, db_table, related_objects=()):
        self._meta = types.SimpleNamespace(
            parents={},
            related_objects=list(related_objects),
            pk=types.SimpleNamespace(column='id'),
            db_table=db_table,
        )


def test_mti_table_chain_dedupes_a_child_reachable_via_two_parent_links():
    """Defensive guard: a child reachable more than once from the same parent (which
    real Django MTI never produces, since it's a tree, but the traversal still guards
    against it) must appear exactly once, after being fully visited (post-order)."""
    child = _FakeMTIModel('child')
    child_link = types.SimpleNamespace(parent_link=True, related_model=child)
    root = _FakeMTIModel('root', related_objects=[child_link, child_link])

    assert _mti_table_chain(root) == [('child', 'id'), ('root', 'id')]


@pytest.mark.django_db(transaction=True)
def test_queryset_hard_delete_from_parent_removes_descendant_rows():
    # hard_delete on a *parent/root* queryset must clear descendant tables too, otherwise the
    # child row's parent-link FK would be orphaned (deferred FK check -> IntegrityError at commit).
    chamber = ChamberOrchestra.objects.create(name='ASMF', conductor='Marriner', seats=40)
    plain = Ensemble.objects.create(name='Plain')
    pk, plain_pk = chamber.pk, plain.pk

    Ensemble._all_objects.all().hard_delete()

    assert Ensemble._all_objects.count() == 0
    # The whole chain under the deleted parent row is gone -- no orphaned descendant rows.
    assert not _row_exists(ChamberOrchestra, pk)
    assert not _row_exists(Orchestra, pk)
    assert not _row_exists(Ensemble, pk)
    assert not _row_exists(Ensemble, plain_pk)
