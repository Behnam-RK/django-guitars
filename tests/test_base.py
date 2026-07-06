"""Tests for guitars.models.base (SetarModel / GuitarModel and their mixins)."""

import types

import pytest
from asgiref.sync import async_to_sync
from django.db import transaction

from guitars.models.base import SetarModel
from tests.testapp.models import Band, Genre, Riff


@pytest.mark.django_db
def test_timestamps_set_on_create():
    band = Band.objects.create(name='Rush')
    band.refresh_from_db()  # db_default values are only present after a refresh

    assert band._created_at is not None
    assert band._updated_at is not None


@pytest.mark.django_db(transaction=True)
def test_updated_at_trigger_advances_on_update():
    band = Band.objects.create(name='Rush')
    band.refresh_from_db()
    before = band._updated_at

    with transaction.atomic():
        Band.objects.filter(pk=band.pk).update(name='Yes')

    band.refresh_from_db()
    assert band._updated_at > before


@pytest.mark.django_db
def test_update_sets_and_persists():
    band = Band.objects.create(name='Rush')

    band.update(name='Yes')

    band.refresh_from_db()
    assert band.name == 'Yes'


@pytest.mark.django_db
def test_update_without_save_is_memory_only():
    band = Band.objects.create(name='Rush')

    band.update(name='Yes', _save=False)

    assert band.name == 'Yes'  # changed in memory
    assert Band.objects.get(pk=band.pk).name == 'Rush'  # not in db


@pytest.mark.django_db
def test_update_raises_on_unknown_field():
    band = Band.objects.create(name='Rush')

    with pytest.raises(ValueError, match='Invalid arguments'):
        band.update(bogus='x')


@pytest.mark.django_db
def test_update_ignores_unknown_field_when_not_raising():
    band = Band.objects.create(name='Rush')

    band.update(bogus='x', name='Yes', _raise_for_excessive=False)

    band.refresh_from_db()
    assert band.name == 'Yes'


@pytest.mark.django_db
def test_update_sets_m2m_relations():
    band = Band.objects.create(name='Rush')
    rock = Genre.objects.create(name='rock')
    prog = Genre.objects.create(name='prog')

    band.update(genres=[rock, prog])

    assert set(band.genres.all()) == {rock, prog}


@pytest.mark.django_db
def test_update_m2m_without_save_raises():
    band = Band.objects.create(name='Rush')
    rock = Genre.objects.create(name='rock')

    with pytest.raises(ValueError, match='Cannot update m2m'):
        band.update(genres=[rock], _save=False)


@pytest.mark.django_db
def test_aupdate_persists_changes():
    band = Band.objects.create(name='Rush')

    async_to_sync(band.aupdate)(name='Yes')

    band.refresh_from_db()
    assert band.name == 'Yes'


def test_class_name():
    assert Band.class_name() == 'Band'


def test_app_label_and_model_name():
    assert Band.app_label() == 'testapp'
    assert Band.model_name() == 'band'


def test_app_label_raises_when_meta_app_label_missing():
    fake_cls = types.SimpleNamespace(
        __name__='Fake', _meta=types.SimpleNamespace(app_label='')
    )

    with pytest.raises(AttributeError, match='_meta.app_label is not set'):
        SetarModel.app_label.__func__(fake_cls)


def test_model_name_raises_when_meta_model_name_missing():
    fake_cls = types.SimpleNamespace(
        __name__='Fake', _meta=types.SimpleNamespace(model_name='')
    )

    with pytest.raises(AttributeError, match='_meta.model_name is not set'):
        SetarModel.model_name.__func__(fake_cls)


@pytest.mark.django_db
def test_repr_includes_class_and_editable_fields():
    band = Band.objects.create(name='Rush')

    text = repr(band)

    assert text.startswith('<Band ID:')
    assert 'name: Rush' in text


@pytest.mark.django_db
def test_repr_skips_none_valued_fields():
    band = Band.objects.create(name='Rush')  # nickname left as None

    assert 'nickname' not in repr(band)


@pytest.mark.django_db
def test_cached_property_invalidated_on_refresh():
    band = Band.objects.create(name='rush')
    assert band.shout == 'RUSH'  # caches

    Band.objects.filter(pk=band.pk).update(name='yes')  # bypasses the instance
    band.refresh_from_db()

    assert band.shout == 'YES'  # recomputed after refresh


@pytest.mark.django_db
def test_expire_cached_properties_directly():
    band = Band.objects.create(name='rush')
    assert band.shout == 'RUSH'

    band.name = 'yes'
    band.expire_cached_properties()

    assert band.shout == 'YES'


# --- DutarModel: the lightest rung (update + cached-property invalidation) ---


@pytest.mark.django_db
def test_dutar_update_persists():
    riff = Riff.objects.create(name='intro')

    riff.update(name='outro')

    riff.refresh_from_db()
    assert riff.name == 'outro'


@pytest.mark.django_db
def test_dutar_cached_property_invalidated_on_refresh():
    riff = Riff.objects.create(name='intro')
    assert riff.shout == 'INTRO'  # caches

    Riff.objects.filter(pk=riff.pk).update(name='outro')  # bypasses the instance
    riff.refresh_from_db()

    assert riff.shout == 'OUTRO'  # recomputed after refresh


def test_dutar_has_no_timestamp_fields():
    field_names = {f.name for f in Riff._meta.get_fields()}
    assert '_created_at' not in field_names
    assert '_updated_at' not in field_names
    assert '_deleted_at' not in field_names
