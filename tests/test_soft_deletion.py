"""Tests for guitars.models.soft_deletion (PostgreSQL-enforced soft deletion)."""

import contextlib

import pytest
from django.db import connection, transaction
from django.db.backends.utils import CursorWrapper

from guitars.models.soft_deletion import (
    AllObjectsManager,
    ArchiveManager,
    HardDeletableQuerySet,
    LiveManager,
    LiveQuerySet,
)
from guitars.sql import SWITCH_OFF_HARD_DELETION, SWITCH_ON_HARD_DELETION
from tests.testapp.models import Album, Band, Genre, Merch, Orchestra, Riff


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
def test_hard_delete_collects_a_diamond_convergence_once():
    """``Merch`` is reachable from ``Album`` by two independent CASCADE relations --
    ``album`` and ``bonus_album`` -- so the DFS in instance-level ``hard_delete()``
    visits it twice: once per relation, each time with a *different* new pk. The
    second visit must not re-append it to the deletion order (it is already queued
    from the first), only fold its new pk into the row already scheduled for that
    table.
    """
    band = Band.objects.create(name='Rush')
    album = Album.objects.create(title='Hemispheres', band=band)
    # Two distinct Merch rows so the two relations bring different, non-overlapping pks.
    via_album = Merch.objects.create(description='Tour shirt', album=album)
    via_bonus = Merch.objects.create(description='Poster', bonus_album=album)

    band.hard_delete()

    assert not Merch._all_objects.filter(pk=via_album.pk).exists()
    assert not Merch._all_objects.filter(pk=via_bonus.pk).exists()


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
    """``hard_delete()`` must delete from the alias the instance is bound to.

    Both the MTI path and ``_hard_delete_own_table`` used to open a cursor on the
    module-global default ``connection`` regardless of ``self.db`` -- on a multi-DB
    project this either errors (no matching row on 'default') or, worse, silently
    deletes the wrong row if one happens to share a pk. A same-named row on 'default'
    surviving is the proof the delete actually went to 'secondary'.
    """
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
    """The switch-off must be this function's own guarantee, not merely an effect of
    the enclosing ``atomic()`` eventually rolling back.

    Proven by making the DELETE raise a plain Python exception that never reaches
    Postgres -- so the transaction is never server-side aborted, and only the
    ``except`` branch in ``_hard_delete_own_table`` decides whether
    ``SWITCH_OFF_HARD_DELETION`` is still attempted afterwards. Before the fix, that line
    was simply never reached.
    """
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
    """A switch-off failure that follows a *successful* DELETE must propagate, not be
    suppressed.

    ``_hard_delete_own_table`` (and the MTI path above it) suppress a failing switch-off
    attempt only when it follows a failed DELETE -- suppressing it there just avoids
    replacing the real error with a second one, since the transaction is typically already
    aborted at that point. Suppressing it unconditionally, including here on the success
    path, would be worse: nothing would tell the caller that ``rules.hard_deletion`` never
    got turned back off, and it would stay 'on' for the rest of any transaction this call
    is nested in -- silently turning later plain ``.delete()`` calls in that same
    transaction into hard deletes instead of archiving them.

    Proven by making only the switch-off statement raise (the DELETE itself succeeds
    normally) inside a caller-owned ``transaction.atomic()`` block, and asserting both that
    the exception surfaces and that it aborts the whole block -- the row must not end up
    hard-deleted behind a swallowed error.
    """
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
    """Every soft-delete manager must instantiate ``self._queryset_class``.

    Not a style preference. ``_queryset_class`` is Django's seam for swapping the queryset
    a manager hands out, and ``guitars.tenancy.TenantedManager`` uses it to install the
    tenant write guard on ``bulk_create``. A manager that names its queryset class
    literally in ``get_queryset()`` ignores the swap and returns an *unguarded* queryset
    while still advertising the guarded one on the class -- so the guard reads as installed
    and does nothing.

    These assert the seam directly, with no database and no tenancy, so a regression is
    attributed to the manager rather than to whatever downstream feature noticed.
    """

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
        """The swap must survive the ``.lives`` clone and win method dispatch.

        ``bulk_create`` on purpose: it is the method tenancy overrides to guard. An empty
        list returns early in Django's implementation, so the un-swapped path is reached
        without touching the database and the mutation shows up as this assertion rather
        than as a connection error.
        """
        calls = []

        class _Recording(LiveQuerySet):
            def bulk_create(self, objs, *args, **kwargs):
                calls.append(objs)
                return objs

        self._bind(LiveManager, _Recording).get_queryset().bulk_create([])

        assert calls == [[]]


class TestTheHardDeletionSwitchCannotLeak:
    """A rolled-back ``hard_delete()`` must not turn later deletes into real ones.

    ``hard_delete()`` opts out of the soft-delete rule by setting ``rules.hard_deletion``
    transaction-locally. PostgreSQL reverts that on rollback -- but not to *unset*: a custom
    GUC that was set and rolled back reads back as the **empty string**, while one that was
    never set reads as NULL. A guard written ``= 'off'`` matches neither, so the rule stops
    firing and ``DELETE`` means what it says.

    The blast radius is the connection, not the transaction: with ``CONN_MAX_AGE`` or any
    pool, one rolled-back transaction containing a ``hard_delete()`` would silently turn
    every subsequent ``.delete()`` into permanent data loss for as long as that connection
    lived. Hence ``<> 'on'`` everywhere -- anything but an explicit opt-in preserves the row.
    """

    @staticmethod
    def _switch() -> str | None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('rules.hard_deletion', true)")
            return cursor.fetchone()[0]

    @pytest.mark.django_db(transaction=True)
    def test_a_rolled_back_hard_delete_leaves_soft_deletion_working(self):
        """``transaction=True`` on purpose: the rollback has to be a real one.

        Under the default fixture every test already runs inside a transaction that is
        rolled back at the end, which is precisely how this bug reaches the *next* test --
        but reproducing it inside one test needs a transaction this test controls.
        """
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
