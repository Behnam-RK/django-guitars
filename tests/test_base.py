"""Tests for guitars.models.base (DutarModel / SetarModel and their mixins)."""

import types

import pytest
from asgiref.sync import async_to_sync
from django.db import transaction
from django.db.models.signals import post_save, pre_save

from guitars.models.base import DutarModel
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


@pytest.mark.django_db(transaction=True)
def test_update_with_no_scalar_fields_writes_nothing():
    """A bare, argument-less update() must not become a full-row rewrite.

    ``_prepare_update`` used to collapse an empty ``updating_fields`` set to
    ``update_fields=None`` (truthiness), which Django reads as "save every field" --
    the opposite of the docstring's promise that only the passed attrs are written.
    ``_updated_at``'s trigger is the observable proof: a real UPDATE bumps it, an
    empty one (Django short-circuits before issuing any SQL) must not.
    """
    band = Band.objects.create(name='Rush')
    band.refresh_from_db()
    before = band._updated_at

    band.update()

    band.refresh_from_db()
    assert band._updated_at == before


@pytest.mark.django_db(transaction=True)
def test_update_m2m_only_writes_no_scalar_column():
    """An M2M-only update() must not also rewrite every scalar column."""
    band = Band.objects.create(name='Rush')
    band.refresh_from_db()
    before = band._updated_at
    rock = Genre.objects.create(name='rock')

    band.update(genres=[rock])

    assert set(band.genres.all()) == {rock}
    band.refresh_from_db()
    assert band._updated_at == before  # no scalar column touched


@pytest.mark.django_db
def test_updatable_fields_skips_columnless_relations(monkeypatch):
    """``_updatable_fields`` must record a columnless relation's *name* without also
    adding a phantom column for it.

    The guard's own comment says it exists for relations with no column, but nothing in
    the test app actually produces one: even the M2M ``genres`` reports a truthy
    ``column`` (Django falls back to the field's ``attname``). Rather than reshape the
    schema chasing a case real relations don't produce here, this synthesizes one.
    """
    band = Band.objects.create(name='Rush')
    real_get_fields = type(band)._meta._get_fields
    phantom = types.SimpleNamespace(name='phantom_relation')  # deliberately no `.column`

    monkeypatch.setattr(
        type(band)._meta, '_get_fields', lambda *a, **kw: [*real_get_fields(*a, **kw), phantom]
    )

    assert 'phantom_relation' in band._updatable_fields
    assert None not in band._updatable_fields  # the falsy column must never be added


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
def test_update_m2m_without_save_raises_before_mutating_scalar_fields():
    """A raising update() must leave the instance untouched -- validation has to run
    before any attribute is set, not interleaved with setting them."""
    band = Band.objects.create(name='Rush')
    rock = Genre.objects.create(name='rock')

    with pytest.raises(ValueError, match='Cannot update m2m'):
        band.update(name='Yes', genres=[rock], _save=False)

    assert band.name == 'Rush'  # unchanged in memory


@pytest.mark.django_db
def test_update_disable_signals_only_narrows_to_save_signals(monkeypatch):
    """``_disable_signals=True`` must disable exactly pre_save/post_save, not the other
    six DEFAULT_SIGNALS -- a bare ``DisableSignals()`` would also suppress
    pre_migrate/post_migrate/pre_init/post_init/pre_delete/post_delete for the
    duration of the call, which is scope creep for a save path.

    ``DisableSignals.__exit__`` always restores fully before ``update()`` returns in
    the single-threaded case, so a black-box check of ``pre_migrate.receivers`` before
    and after the call cannot tell the eight-signal default apart from the narrowed
    pair -- both leave it intact by the time control returns. What actually differs is
    the ``signals=`` argument passed for the call's duration, so that is what this pins.
    """
    import guitars.signals as signals_module

    captured = {}
    real_init = signals_module.DisableSignals.__init__

    def spy_init(self, signals=None):
        captured['signals'] = signals
        real_init(self, signals=signals)

    monkeypatch.setattr(signals_module.DisableSignals, '__init__', spy_init)

    band = Band.objects.create(name='Rush')
    band.update(name='Yes', _disable_signals=True)

    assert captured['signals'] == [pre_save, post_save]


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
    fake_cls = types.SimpleNamespace(__name__='Fake', _meta=types.SimpleNamespace(app_label=''))

    with pytest.raises(AttributeError, match='_meta.app_label is not set'):
        DutarModel.app_label.__func__(fake_cls)


def test_model_name_raises_when_meta_model_name_missing():
    fake_cls = types.SimpleNamespace(__name__='Fake', _meta=types.SimpleNamespace(model_name=''))

    with pytest.raises(AttributeError, match='_meta.model_name is not set'):
        DutarModel.model_name.__func__(fake_cls)


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


@pytest.mark.django_db
def test_inherited_cached_property_invalidated_on_refresh():
    """``whisper`` lives on WhisperMixin's __dict__, not Band's -- a leaf-only scan skips it."""
    band = Band.objects.create(name='RUSH')
    assert band.whisper == 'rush'  # caches on the instance

    Band.objects.filter(pk=band.pk).update(name='YES')  # bypasses the instance
    band.refresh_from_db()

    assert band.whisper == 'yes'  # stale 'rush' when only Band.__dict__ was searched


def test_expire_cached_properties_accepts_an_inherited_name():
    band = Band(name='RUSH')
    assert band.whisper == 'rush'

    band.name = 'YES'
    band.expire_cached_properties('whisper')  # KeyError before the MRO walk

    assert band.whisper == 'yes'


# --- TarModel: the lightest rung (update + cached-property invalidation) ---


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
