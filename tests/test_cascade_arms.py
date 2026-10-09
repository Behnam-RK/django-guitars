"""The cascade as an arm of the owner's trigger (#80, ADR 0039), by what it does to rows. Raw
statements, not the ORM: Django's collector names every level itself, so an ORM-driven test
passes with the arms dropped."""

from importlib import import_module

import pytest
from django.db import connection, transaction

from tests.conftest import execute, rows, scalar
from tests.testapp.models import Album, Band, Catalog, Listing, Merch


@pytest.fixture
def two_bands(db):
    made = []
    for name in ('Rush', 'Yes'):
        band = Band.objects.create(name=name)
        album = Album.objects.create(title=f'{name}-album', band=band)
        Merch.objects.create(description=f'{name}-shirt', album=album)
        made.append(band)
    return made


def _live(model) -> int:
    return model.objects.count()


def test_one_statement_archives_several_owners_and_every_level_below(two_bands):
    execute(
        'UPDATE testapp_band SET _deleted_at = NOW() WHERE id = ANY(%s)',
        params=[[band.pk for band in two_bands]],
    )

    assert (_live(Band), _live(Album), _live(Merch)) == (0, 0, 0)


def test_a_child_carries_its_parents_own_stamp_down_every_level(two_bands):
    stamp = '2020-01-01 00:00:00+00'
    execute(
        'UPDATE testapp_band SET _deleted_at = %s WHERE id = %s', params=[stamp, two_bands[0].pk]
    )

    stamps = {
        scalar(f'SELECT _deleted_at FROM {table} WHERE _deleted_at IS NOT NULL')
        for table in ('testapp_band', 'testapp_album', 'testapp_merch')
    }
    assert len(stamps) == 1 and None not in stamps


def test_a_child_archived_earlier_keeps_its_own_stamp(two_bands):
    album = Album._all_objects.get(band=two_bands[0])
    execute("UPDATE testapp_album SET _deleted_at = '2019-05-05 00:00:00+00' WHERE id = %s", params=[album.pk])

    execute('UPDATE testapp_band SET _deleted_at = NOW() WHERE id = %s', params=[two_bands[0].pk])

    assert str(scalar('SELECT _deleted_at FROM testapp_album WHERE id = %s', [album.pk])).startswith(
        '2019-05-05'
    )


def test_an_update_that_moves_no_deleted_at_cascades_nothing(two_bands):
    execute('UPDATE testapp_band SET name = name || %s', params=['!'])

    assert (_live(Band), _live(Album), _live(Merch)) == (2, 2, 2)


def test_the_hard_deletion_switch_stops_the_arms(two_bands):
    """The same switch every rule reads: while it is on, nothing cascades."""
    with transaction.atomic():
        execute("SELECT set_config('rules.hard_deletion', 'on', TRUE)")
        execute('UPDATE testapp_band SET _deleted_at = NOW() WHERE id = %s', params=[two_bands[0].pk])

    assert (_live(Band), _live(Album), _live(Merch)) == (1, 2, 2)


def test_a_key_moving_in_the_same_statement_still_finds_its_children(db):
    """The arm reads the owner's *before* image for the key, as the rule read ``old.``: a child
    pointing at the old code is found, where the after image would find none."""
    catalog = Catalog.objects.create(code='A')
    Listing.objects.create(catalog=catalog, name='one')

    execute("UPDATE testapp_catalog SET code = 'B', _deleted_at = NOW() WHERE id = %s", params=[catalog.pk])

    assert Listing.objects.count() == 0
    # The deferred foreign key is left dangling by the move; settled so teardown can check it.
    execute("UPDATE testapp_listing SET catalog_id = 'B'")


def test_the_revive_still_clears_what_the_archive_took(two_bands):
    execute('UPDATE testapp_band SET _deleted_at = NOW() WHERE id = %s', params=[two_bands[0].pk])
    execute('UPDATE testapp_band SET _deleted_at = NULL WHERE id = %s', params=[two_bands[0].pk])

    assert (_live(Band), _live(Album), _live(Merch)) == (2, 2, 2)


@pytest.mark.django_db(transaction=True)
def test_a_cascaded_child_is_stamped_whatever_the_trigger_depth():
    """The arm's ``UPDATE`` runs at depth 1; the row trigger has no guard to suppress it."""
    band = Band.objects.create(name='Rush')
    album = Album.objects.create(title='2112', band=band)
    before = scalar('SELECT _updated_at FROM testapp_album WHERE id = %s', [album.pk])

    execute('UPDATE testapp_band SET _deleted_at = NOW() WHERE id = %s', params=[band.pk])

    assert scalar('SELECT _updated_at FROM testapp_album WHERE id = %s', [album.pk]) > before


def test_no_cascade_or_owned_rule_is_left_in_the_database(db):
    """Retired by 0085, which is what ``migrate`` ran: only each table's own ``soft_delete``
    (and the MTI redirect) remain among ``soft_delete*`` rules that fire on ``UPDATE``."""
    left = rows(
        "SELECT rulename FROM pg_rules WHERE tablename LIKE 'testapp\\_%' "
        "AND (rulename LIKE 'soft\\_delete\\_related%' OR rulename LIKE 'soft\\_delete\\_owned%')"
    )

    assert left == []


def test_the_migrations_reverse_rebuilds_the_rules_and_the_revive_only_trigger(db):
    """Unapplying 0085 leaves no gap: every rule it dropped is back, as 2.18 wrote it, and the
    owner's trigger is the revive-only one again. Run off the migration's own ``reverse_sql``."""
    module = import_module('tests.testapp.migrations.0085_auto_enforcement')
    function = 'soft_delete_revive_on_12_testapp_band'

    with transaction.atomic():
        for operation in reversed(module.Migration.operations):
            with connection.cursor() as cursor:
                cursor.execute(operation.reverse_sql)
        rules = scalar(
            "SELECT count(*) FROM pg_rules WHERE tablename LIKE 'testapp\\_%' "
            "AND (rulename LIKE 'soft\\_delete\\_related%' OR rulename LIKE 'soft\\_delete\\_owned%')"
        )
        body = scalar("SELECT prosrc FROM pg_proc WHERE proname = %s", [function])
        transaction.set_rollback(True)

    assert rules > 0
    assert 'guitars_archived' not in body and 'guitars_revived' in body
