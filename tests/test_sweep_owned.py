"""``sweepowned``: the repair half of issue #40. The trigger closes the hole for every future
statement, but a database migrated before it landed holds dependents nothing will ever stamp.
The leak is reproduced by dropping the sweep trigger, which is what such a database is."""

from __future__ import annotations

import pytest
from django.core.management import CommandError, call_command
from django.db import connection
from io import StringIO

from tests.testapp.models import Album, Band, Ensemble, Foyer, Kiosk, Placard, PressKit, Rider
from tests.testapp.models import Residency, Stagehand


@pytest.fixture
def band(db):
    return Band.objects.create(name='Rush')


def _drop_sweep_triggers():
    """Put the database back in its pre-2.6.0 shape -- rules, no statement-level sweep. The
    leak this command repairs is unreachable while the trigger is installed, so a test that
    did not do this could only ever assert the command finds nothing."""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT tgname, relname FROM pg_trigger
            JOIN pg_class ON pg_class.oid = pg_trigger.tgrelid
            WHERE tgname LIKE 'soft_delete_owned_sweep%'
        """)
        for trigger, table in cursor.fetchall():
            cursor.execute(f'DROP TRIGGER "{trigger}" ON "{table}"')


def _sweep(*args) -> str:
    out = StringIO()
    call_command('sweepowned', *args, stdout=out, stderr=out)
    return out.getvalue()


@pytest.mark.django_db
def test_the_sweep_reports_and_repairs_the_row_a_pre_2_6_0_database_leaked(band):
    """Issue #40's own reproduction, on a database without the trigger: one statement over
    two co-owners leaves the kit live with no live owner and nothing that will ever stamp it."""
    _drop_sweep_triggers()
    kit = PressKit.objects.create(headline='Shared')
    Album.objects.create(title='Hemispheres', band=band, press_kit=kit)
    Album.objects.create(title='Permanent Waves', band=band, press_kit=kit)
    Album.objects.filter(press_kit=kit).delete()

    assert PressKit.objects.filter(pk=kit.pk).exists()  # leaked, as 2.5.1 would leave it

    with pytest.raises(CommandError, match='Re-run with --repair'):
        _sweep()
    assert PressKit.objects.filter(pk=kit.pk).exists()  # reporting alone changes nothing

    assert 'stamped' in _sweep('--repair')
    assert not PressKit.objects.filter(pk=kit.pk).exists()
    assert PressKit._archives.get(pk=kit.pk)._deleted_at is not None


@pytest.mark.django_db
def test_the_sweep_is_a_no_op_on_a_healthy_database(band):
    """The trigger is installed, so the same statement archives the kit as it goes and the
    command has nothing left to find. No exception: nothing is wrong."""
    kit = PressKit.objects.create(headline='Shared')
    Album.objects.create(title='Hemispheres', band=band, press_kit=kit)
    Album.objects.create(title='Permanent Waves', band=band, press_kit=kit)
    Album.objects.filter(press_kit=kit).delete()

    assert 'Owned sweep complete.' in _sweep()


@pytest.mark.django_db
def test_the_sweep_spares_a_dependent_a_live_owner_still_holds(band):
    """The last-owner guard, re-asked in Python. A live owner on any owning column -- here a
    different table -- means the row is not an orphan, however many archived owners it has."""
    _drop_sweep_triggers()
    kit = PressKit.objects.create(headline='Shared')
    Album.objects.create(title='Hemispheres', band=band, press_kit=kit)
    Album.objects.create(title='Permanent Waves', band=band, press_kit=kit)
    Ensemble.objects.create(name='Quartet', press_kit=kit)  # live, and still owns it
    Album.objects.filter(press_kit=kit).delete()

    assert 'Owned sweep complete.' in _sweep()
    assert PressKit.objects.filter(pk=kit.pk).exists()


@pytest.mark.django_db
def test_the_sweep_leaves_a_dependent_no_owner_ever_held():
    """The other half of the predicate, and the one that would be silent data loss without
    it: a kit nobody ever pointed at has no live owner either, but no rule would ever have
    stamped it. An archived owner is what makes a row an *orphan* rather than merely unowned."""
    _drop_sweep_triggers()
    kit = PressKit.objects.create(headline='Never owned')

    assert 'Owned sweep complete.' in _sweep()
    assert PressKit.objects.filter(pk=kit.pk).exists()


@pytest.mark.django_db
def test_the_sweep_reaches_a_chained_orphan_across_two_runs():
    """Stamping a dependent can orphan its own dependents: ``Residency`` owns a ``Rider``
    which owns a ``Stagehand``. Without the trigger the first hop leaks, and repairing it
    exposes the second -- which is why an operator runs this to a fixpoint."""
    _drop_sweep_triggers()
    stagehand = Stagehand.objects.create(name='Rigger')
    rider = Rider.objects.create(clause='No brown M&Ms', stagehand=stagehand)
    Residency.objects.create(venue_name='Massey Hall', rider=rider)
    Residency.objects.create(venue_name='Hammersmith', rider=rider)
    Residency.objects.filter(rider=rider).delete()

    _sweep('--repair')
    assert not Rider.objects.filter(pk=rider.pk).exists()
    _sweep('--repair')
    assert not Stagehand.objects.filter(pk=stagehand.pk).exists()


@pytest.mark.django_db
def test_the_sweep_scopes_to_the_app_labels_it_is_given(band):
    """Scoped by the *dependent's* app, and validated: a typo'd label would otherwise match
    nothing and report a clean sweep that examined nothing at all."""
    _drop_sweep_triggers()
    kit = PressKit.objects.create(headline='Shared')
    Album.objects.create(title='Hemispheres', band=band, press_kit=kit)
    Album.objects.create(title='Permanent Waves', band=band, press_kit=kit)
    Album.objects.filter(press_kit=kit).delete()

    with pytest.raises(CommandError, match='No installed app with label'):
        _sweep('nosuchapp')
    with pytest.raises(CommandError, match='Re-run with --repair'):
        _sweep('testapp')


@pytest.mark.django_db
def test_the_sweep_does_not_follow_a_relation_the_generator_refused():
    """``Placard`` owns itself, so its relation closes an ON UPDATE cycle and is refused a
    rule. Following it here would destroy exactly what that refusal spared -- so a placard
    orphaned only through the refused column is not a finding, whatever its owners look like."""
    _drop_sweep_triggers()
    placard = Placard.objects.create(caption='Self-owned')
    child = Placard.objects.create(caption='Child', parent=placard)
    child.delete()  # the refused relation: nothing stamps `placard`

    assert 'Owned sweep complete.' in _sweep()
    assert Placard.objects.filter(pk=placard.pk).exists()


@pytest.mark.django_db
def test_the_sweep_counts_an_archived_co_owner_on_another_table():
    """The stamping half reads every rule-carrying relation, not only the one that went
    last: a placard whose kiosk and foyer were archived by two separate statements is an
    orphan through either, and one run finds it."""
    _drop_sweep_triggers()
    placard = Placard.objects.create(caption='Closed Run')
    kiosk = Kiosk.objects.create(label='Lobby', placard=placard)
    foyer = Foyer.objects.create(label='Mezzanine', placard=placard)
    Kiosk.objects.filter(pk=kiosk.pk).delete()
    Foyer.objects.filter(pk=foyer.pk).delete()

    _sweep('--repair')
    assert not Placard.objects.filter(pk=placard.pk).exists()
