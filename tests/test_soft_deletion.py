"""Tests for guitars.models.soft_deletion (PostgreSQL-enforced soft deletion)."""

import pytest

from tests.testapp.models import Album, Band, Genre, Orchestra, Riff


@pytest.mark.django_db
def test_delete_sets_deleted_at_instead_of_removing():
    band = Band.objects.create(name='Rush')
    pk = band.pk

    band.delete()  # the PG rule turns this into a soft delete

    assert not Band.objects.filter(pk=pk).exists()  # hidden from the live manager
    archived = Band._archives.get(pk=pk)
    assert archived._deleted_at is not None
    assert archived.is_deleted
    assert not archived.is_alive


@pytest.mark.django_db
def test_three_managers_partition_rows():
    Band.objects.create(name='Alive')
    doomed = Band.objects.create(name='Doomed')
    doomed.delete()

    assert set(Band.objects.values_list('name', flat=True)) == {'Alive'}
    assert set(Band._archives.values_list('name', flat=True)) == {'Doomed'}
    assert set(Band._all_objects.values_list('name', flat=True)) == {'Alive', 'Doomed'}


@pytest.mark.django_db
def test_queryset_lives_and_archives_helpers():
    a = Band.objects.create(name='A')
    b = Band.objects.create(name='B')
    a_pk = a.pk  # .delete() resets a.pk to None, so capture it first
    a.delete()

    assert list(Band._all_objects.lives) == [b]
    assert list(Band._all_objects.archives) == [Band._archives.get(pk=a_pk)]


@pytest.mark.django_db
def test_cls_property_returns_the_model_class():
    band = Band.objects.create(name='Rush')

    assert band.cls is Band


@pytest.mark.django_db
def test_delete_cascades_soft_delete_to_related():
    band = Band.objects.create(name='Rush')
    album = Album.objects.create(title='Hemispheres', band=band)

    band.delete()

    assert not Album.objects.filter(pk=album.pk).exists()
    assert Album._archives.filter(pk=album.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_hard_delete_removes_instance_and_cascade_children():
    band = Band.objects.create(name='Rush')
    album = Album.objects.create(title='Hemispheres', band=band)
    band.genres.add(Genre.objects.create(name='prog'))  # m2m through row
    band_pk, album_pk = band.pk, album.pk

    band.hard_delete()

    assert not Band._all_objects.filter(pk=band_pk).exists()
    assert not Album._all_objects.filter(pk=album_pk).exists()


@pytest.mark.django_db(transaction=True)
def test_queryset_hard_delete_removes_rows():
    Band.objects.create(name='A')
    Band.objects.create(name='B')

    Band._all_objects.all().hard_delete()

    assert Band._all_objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_mti_queryset_hard_delete_no_op_on_empty_queryset():
    """The MTI branch of QuerySet.hard_delete short-circuits when nothing matches,
    without ever opening a cursor to switch hard-deletion on."""
    assert Orchestra._all_objects.filter(name='does-not-exist').hard_delete() is None


@pytest.mark.django_db(transaction=True)
def test_hard_delete_does_not_cascade_through_non_cascade_relations():
    """Album.producer is a SET_NULL (not CASCADE) FK to Band -- deleting the producer
    band must not hard-delete the album, only null out the FK."""
    producer = Band.objects.create(name='Geddy Co')
    band = Band.objects.create(name='Rush')
    album = Album.objects.create(title='Hemispheres', band=band, producer=producer)

    producer.hard_delete()

    assert Album._all_objects.filter(pk=album.pk).exists()
    album.refresh_from_db()
    assert album.producer_id is None


@pytest.mark.django_db(transaction=True)
def test_hard_delete_hard_deletes_non_soft_deletable_cascade_children():
    """Riff.band is a CASCADE FK from a plain (non-soft-deletable) model -- its rows
    must be genuinely removed, not merely soft-deleted, when the band is hard-deleted."""
    band = Band.objects.create(name='Rush')
    riff = Riff.objects.create(name='Working Man', band=band)

    band.hard_delete()

    assert not Riff.objects.filter(pk=riff.pk).exists()
