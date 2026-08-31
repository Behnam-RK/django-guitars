"""Tests for guitars.models.soft_deletion (PostgreSQL-enforced soft deletion)."""

import contextlib

import pytest
from django.db import transaction
from django.db.backends.utils import CursorWrapper

from guitars.models.soft_deletion import (
    AllObjectsManager,
    ArchiveManager,
    HardDeletableQuerySet,
    LiveManager,
    LiveQuerySet,
)
from guitars.sql import SWITCH_OFF_HARD_DELETION, SWITCH_ON_HARD_DELETION
from tests.conftest import scalar as _scalar
from tests.testapp.models import Album, Band, Genre, Merch, Orchestra, Riff
from tests.testapp.models import Scribble, Signboard


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
def test_hard_delete_collects_a_diamond_convergence_once():
    """``Merch`` has two independent CASCADE relations to ``Album``, so the DFS in
    ``hard_delete()`` visits it twice -- the second visit must fold its new pk in,
    not re-append it to the deletion order."""
    band = Band.objects.create(name='Rush')
    album = Album.objects.create(title='Hemispheres', band=band)
    # Two distinct Merch rows so the two relations bring different, non-overlapping pks.
    via_album = Merch.objects.create(description='Tour shirt', album=album)
    via_bonus = Merch.objects.create(description='Poster', bonus_album=album)

    band.hard_delete()

    assert not Merch._all_objects.filter(pk=via_album.pk).exists()
    assert not Merch._all_objects.filter(pk=via_bonus.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_hard_delete_takes_a_generic_relation_child_with_it():
    """A ``GenericRelation`` child has no key column, so Phase 2's walk could not see it while
    Phase 1's ``Collector`` archived it, and the row survived pointing at a gone primary key.
    Collected from the same ``_meta.private_fields`` since 2.7.0."""
    signboard = Signboard.objects.create(caption='Farewell')
    scribble = Scribble.objects.create(text='sold out', content_object=signboard)

    signboard.hard_delete()

    assert not Signboard._all_objects.filter(pk=signboard.pk).exists()
    assert not Scribble._all_objects.filter(pk=scribble.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_hard_delete_leaves_another_targets_generic_children_alone():
    """Collected through ``bulk_related_objects``, which asks about *these* rows: a child of
    another target shares the content type and must not come along with it."""
    doomed = Signboard.objects.create(caption='Farewell')
    kept = Signboard.objects.create(caption='Encore')
    Scribble.objects.create(text='sold out', content_object=doomed)
    survivor = Scribble.objects.create(text='on sale', content_object=kept)

    doomed.hard_delete()

    assert Scribble.objects.filter(pk=survivor.pk).exists()
    assert Signboard.objects.filter(pk=kept.pk).exists()


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


@pytest.mark.django_db(transaction=True, databases=['default', 'secondary'])
def test_instance_hard_delete_operates_on_the_bound_alias():
    """``hard_delete()`` must delete from the alias the instance is bound to, not a
    hardcoded default ``connection`` -- a same-named row surviving on 'default' is
    the proof the delete actually went to 'secondary'."""
    default_band = Band.objects.create(name='Default')
    secondary_band = Band.objects.using('secondary').create(name='Secondary')

    secondary_band.hard_delete()

    assert not Band._all_objects.using('secondary').filter(pk=secondary_band.pk).exists()
    assert Band._all_objects.using('default').filter(pk=default_band.pk).exists()


@pytest.mark.django_db(transaction=True, databases=['default', 'secondary'])
def test_mti_queryset_hard_delete_operates_on_the_bound_alias():
    """The MTI branch of ``HardDeletableQuerySet.hard_delete`` on a non-default alias."""
    orchestra = Orchestra.objects.using('secondary').create(name='Philharmonic', conductor='Kar')

    Orchestra._all_objects.using('secondary').filter(pk=orchestra.pk).hard_delete()

    assert not Orchestra._all_objects.using('secondary').filter(pk=orchestra.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_switch_off_is_attempted_even_when_the_delete_itself_raises(monkeypatch):
    """The switch-off must be ``_hard_delete_own_table``'s own guarantee, not merely an
    effect of the enclosing ``atomic()`` rolling back -- proven via a Python-raised
    exception that never reaches Postgres, so only the ``except`` branch decides."""
    band = Band.objects.create(name='Doomed')
    calls = []
    real_execute = CursorWrapper.execute

    def spy_execute(self, sql, params=None):
        calls.append(sql)
        if isinstance(sql, str) and sql.strip().upper().startswith('DELETE FROM'):
            raise RuntimeError('simulated failure -- never reaches postgres')
        return real_execute(self, sql, params)

    monkeypatch.setattr(CursorWrapper, 'execute', spy_execute)

    with pytest.raises(RuntimeError, match='simulated failure'):
        Band._all_objects.filter(pk=band.pk).hard_delete()

    assert calls.count(SWITCH_ON_HARD_DELETION) == 1
    assert calls.count(SWITCH_OFF_HARD_DELETION) == 1


@pytest.mark.django_db(transaction=True)
def test_mti_switch_off_is_attempted_even_when_a_delete_raises(monkeypatch):
    """MTI-path analogue of ``test_switch_off_is_attempted_even_when_the_delete_itself_raises``
    -- the ``except`` branch in the MTI loop of ``HardDeletableQuerySet.hard_delete`` must
    also attempt the switch-off and then re-raise the real error, not swallow it."""
    orchestra = Orchestra.objects.create(name='Philharmonic', conductor='Kar')
    calls = []
    real_execute = CursorWrapper.execute

    def spy_execute(self, sql, params=None):
        calls.append(sql)
        if isinstance(sql, str) and sql.strip().upper().startswith('DELETE FROM'):
            raise RuntimeError('simulated failure -- never reaches postgres')
        return real_execute(self, sql, params)

    monkeypatch.setattr(CursorWrapper, 'execute', spy_execute)

    with pytest.raises(RuntimeError, match='simulated failure'):
        Orchestra._all_objects.filter(pk=orchestra.pk).hard_delete()

    assert calls.count(SWITCH_ON_HARD_DELETION) == 1
    assert calls.count(SWITCH_OFF_HARD_DELETION) == 1


@pytest.mark.django_db(transaction=True)
def test_switch_off_failure_after_a_successful_delete_is_not_swallowed(monkeypatch):
    """A switch-off failure after a *successful* DELETE must propagate, not be
    suppressed, or ``rules.hard_deletion`` stays stuck 'on' for the rest of the
    transaction, silently turning later plain ``.delete()`` calls into hard deletes."""
    band = Band.objects.create(name='Doomed')
    real_execute = CursorWrapper.execute

    def spy_execute(self, sql, params=None):
        if isinstance(sql, str) and sql.strip() == SWITCH_OFF_HARD_DELETION.strip():
            raise RuntimeError('simulated failure of the switch-off statement itself')
        return real_execute(self, sql, params)

    monkeypatch.setattr(CursorWrapper, 'execute', spy_execute)

    with (
        pytest.raises(RuntimeError, match='simulated failure of the switch-off'),
        transaction.atomic(),
    ):
        Band._all_objects.filter(pk=band.pk).hard_delete()

    # The enclosing atomic() must have rolled back the DELETE along with the propagated
    # error -- not left it committed with the switch silently stuck 'on'.
    assert Band._all_objects.filter(pk=band.pk).exists()


class TestManagerQuerySetClass:
    """Every soft-delete manager must instantiate ``self._queryset_class``, the seam
    ``tenanted_manager()`` swaps to install the write guard -- naming the queryset
    class literally would return an unguarded queryset while the guard reads installed."""

    @staticmethod
    def _bind(manager_class, queryset_class):
        """A manager instance with ``_queryset_class`` swapped, outside model machinery."""
        manager = type('_Probe', (manager_class,), {'_queryset_class': queryset_class})()
        manager.model = Band
        manager._db = None
        manager._hints = {}
        return manager

    @pytest.mark.parametrize(
        ('manager_class', 'base_queryset'),
        [
            (LiveManager, LiveQuerySet),
            (ArchiveManager, HardDeletableQuerySet),
            (AllObjectsManager, HardDeletableQuerySet),
        ],
    )
    def test_get_queryset_honours_the_swapped_class(self, manager_class, base_queryset):
        swapped = type('_Swapped', (base_queryset,), {})
        manager = self._bind(manager_class, swapped)

        # `is`, not isinstance: the whole failure mode is getting the BASE class back, and
        # the base passes isinstance for a subclass swap.
        assert type(manager.get_queryset()) is swapped

    def test_a_swapped_override_actually_runs(self):
        """The swap must survive the ``.lives`` clone and win method dispatch, tested via
        ``bulk_create`` -- the method tenancy overrides to guard. An empty list returns
        early in Django's implementation, so no database is touched."""
        calls = []

        class _Recording(LiveQuerySet):
            def bulk_create(self, objs, *args, **kwargs):
                calls.append(objs)
                return objs

        self._bind(LiveManager, _Recording).get_queryset().bulk_create([])

        assert calls == [[]]


class TestTheHardDeletionSwitchCannotLeak:
    """A rolled-back ``hard_delete()`` must not leak: a rolled-back GUC reads back as
    the empty string, not NULL, so the guard must be ``<> 'on'`` everywhere (see
    CLAUDE.md), or later pooled-connection deletes silently become permanent."""

    @staticmethod
    def _switch() -> str | None:
        return _scalar("SELECT current_setting('rules.hard_deletion', true)")

    @pytest.mark.django_db(transaction=True)
    def test_a_rolled_back_hard_delete_leaves_soft_deletion_working(self):
        """``transaction=True`` needed because the rollback must be a real one, not the
        default fixture's own -- that outer rollback is precisely how this bug used to
        reach the *next* test."""
        keeper = Band.objects.create(name='Keeper')

        with contextlib.suppress(RuntimeError), transaction.atomic():
            Band.objects.create(name='Doomed').hard_delete()
            raise RuntimeError('roll it back')

        # The empty-string placeholder is the state that used to break the rule. Asserted so
        # the test is known to be reproducing the real condition and not passing vacuously.
        assert self._switch() == ''

        keeper.delete()

        assert Band._archives.filter(name='Keeper').exists()
        assert Band._all_objects.filter(name='Keeper').exists()

    @pytest.mark.django_db(transaction=True)
    def test_the_cascade_rule_survives_it_too(self):
        """The related-objects rule carries the same guard, so it needs the same proof."""
        band = Band.objects.create(name='Parent')
        Album.objects.create(title='Child', band=band)

        with contextlib.suppress(RuntimeError), transaction.atomic():
            Band.objects.create(name='Doomed').hard_delete()
            raise RuntimeError('roll it back')

        band.delete()

        assert Album._archives.filter(title='Child').exists()

    @pytest.mark.django_db(transaction=True)
    def test_hard_delete_still_hard_deletes(self):
        """The other direction: the opt-in must keep working, or the fix traded one bug for
        another."""
        band = Band.objects.create(name='Gone')
        pk = band.pk

        band.hard_delete()

        assert not Band._all_objects.filter(pk=pk).exists()


@pytest.mark.django_db(transaction=True)
def test_a_second_hard_delete_still_reaches_an_archived_generic_child():
    """``bulk_related_objects`` reads through Django's own unfiltered ``_base_manager``, never
    the ``LiveManager`` that is ``_default_manager`` -- so a generic child an earlier
    ``delete()`` already archived is still collectable, as ``_rows`` guarantees for the rest."""
    signboard = Signboard.objects.create(caption='Farewell')
    scribble = Scribble.objects.create(text='sold out', content_object=signboard)
    pk = signboard.pk  # Django clears it on delete, as ``hard_delete`` saves it for

    signboard.delete()  # Phase 1's work on its own: both archived, neither row removed
    assert Scribble._archives.filter(pk=scribble.pk).exists()

    Signboard._all_objects.get(pk=pk).hard_delete()

    assert not Signboard._all_objects.filter(pk=pk).exists()
    assert not Scribble._all_objects.filter(pk=scribble.pk).exists()
