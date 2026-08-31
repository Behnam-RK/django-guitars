"""``sweepowned``: the repair half of issue #40. The trigger closes the hole for every future
statement, but a database migrated before it landed holds dependents nothing will ever stamp.
The leak is reproduced by dropping the sweep trigger, which is what such a database is."""

from __future__ import annotations

import re
from unittest import mock

import pytest
from django.core.management import CommandError, call_command
from django.db import connection
from django.utils import timezone
from io import StringIO

from guitars.management.commands import sweepowned as sweepowned_module
from guitars.management.commands.sweepowned import Command
from guitars.management.enforcement.operations import _owned_rule_name
from guitars.sql._identifiers import _unescape_ident
from tests.crossapp_dependent.models import Shared
from tests.crossapp_owner.models import Owner
from tests.testapp.models import Album, Awning, Band, Billboard, Ensemble, Foyer, Kiosk
from tests.testapp.models import NeonMarquee, Placard, PressKit, Rider
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


def _drop_owned_rules(table: str) -> None:
    """Put one table back to never-generated: rules gone, so the database says this relation
    carries none. What an app outside a consumer's LOCAL_APPS actually looks like."""
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT rulename FROM pg_rules WHERE tablename = %s '
            "AND rulename LIKE 'soft/_delete/_owned/_%%' ESCAPE '/'",
            [table],
        )
        for (rule,) in cursor.fetchall():
            cursor.execute(f'DROP RULE "{rule}" ON "{table}"')


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
def test_the_sweep_reaches_a_chained_orphan():
    """Stamping a dependent orphans its own: ``Residency`` owns a ``Rider`` owning a
    ``Stagehand``, and each hop needs a statement archiving every owner or the rule stamps it
    first. One run reaches both only because repair order is by label; the fixpoint is pinned."""
    _drop_sweep_triggers()
    stagehand = Stagehand.objects.create(name='Rigger')
    # Two riders on one stagehand, so archiving the riders is itself a last-owner decision
    # the per-row rule cannot make -- the second hop is left to this command, not to the rule.
    riders = [
        Rider.objects.create(clause='No brown M&Ms', stagehand=stagehand),
        Rider.objects.create(clause='Green room', stagehand=stagehand),
    ]
    for rider in riders:
        Residency.objects.create(venue_name=f'{rider.clause} I', rider=rider)
        Residency.objects.create(venue_name=f'{rider.clause} II', rider=rider)
    Residency.objects.all().delete()  # one statement, every owner of every rider

    assert Rider.objects.count() == 2  # the rule stamped neither hop
    assert Stagehand.objects.filter(pk=stagehand.pk).exists()

    _sweep('--repair')

    assert not Rider.objects.exists()
    assert not Stagehand.objects.filter(pk=stagehand.pk).exists()
    assert 'Owned sweep complete.' in _sweep()  # and the run is a fixpoint


@pytest.mark.django_db
def test_the_repair_runs_to_a_fixpoint_when_the_chain_sorts_against_it():
    """``Residency``'s chain settles in one pass only because label order happens to agree with
    ownership. ``Awning`` sorts *before* the ``NeonMarquee`` rows owning it: pass one spares it
    while they are live, then archives them -- in one statement, so their rule decides nothing."""
    _drop_sweep_triggers()
    awning = Awning.objects.create(fabric='striped')
    # Two owners at each hop, or the per-row rule settles that hop on its own and the
    # statement-level hole this command repairs is never reached.
    for glow in ('amber', 'neon'):
        marquee = NeonMarquee.objects.create(headline=f'{glow} nights', glow=glow, awning=awning)
        Billboard.objects.create(label=f'{glow} north', marquee=marquee)
        Billboard.objects.create(label=f'{glow} south', marquee=marquee)

    Billboard.objects.all().delete()  # one statement, every owner of both marquees

    assert NeonMarquee.objects.count() == 2  # leaked, as a 2.5.x database is
    assert Awning.objects.filter(pk=awning.pk).exists()

    _sweep('--repair')

    assert not NeonMarquee.objects.exists()
    assert not Awning.objects.filter(pk=awning.pk).exists()  # the hop behind the walk
    assert 'Owned sweep complete.' in _sweep()  # and the run really is a fixpoint


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

    # The refusal is what has to be doing the work here, so the *other* gate is taken out of
    # the way: the database is told it holds this rule. Without that, `pg_rules` alone drops
    # the relation and the assertions below hold whether or not the refusals are honoured.
    refused = _owned_rule_name(Placard._meta.db_table, Placard._meta.get_field('parent').column)
    live = Command._owned_rules_in_database('default')
    live.add((Placard._meta.db_table, _unescape_ident(refused[1:-1])))
    with mock.patch.object(Command, '_owned_rules_in_database', staticmethod(lambda using: live)):
        assert 'Owned sweep complete.' in _sweep()

    assert Placard.objects.filter(pk=placard.pk).exists()


@pytest.mark.django_db
def test_the_sweep_counts_an_archived_co_owner_on_another_table():
    """The stamping half reads every rule-carrying relation, not only the one that went last.
    Two of each, deleted per table in one statement: with one owner apiece the rule stamps the
    placard as the second goes, and the command is handed a row already archived."""
    _drop_sweep_triggers()
    placard = Placard.objects.create(caption='Closed Run')
    Kiosk.objects.create(label='Lobby', placard=placard)
    Kiosk.objects.create(label='Annex', placard=placard)
    Foyer.objects.create(label='Mezzanine', placard=placard)
    Foyer.objects.create(label='Balcony', placard=placard)
    Kiosk.objects.filter(placard=placard).delete()  # one statement, both kiosks
    Foyer.objects.filter(placard=placard).delete()  # one statement, both foyers

    assert Placard.objects.filter(pk=placard.pk).exists()  # the rule stamped nothing

    _sweep('--repair')
    assert not Placard.objects.filter(pk=placard.pk).exists()


@pytest.mark.django_db
def test_the_sweep_reports_the_number_of_tables_it_actually_checked(band):
    """The count is what the run examined, not what the project holds. Reporting the whole
    registry under a scoped run is the vacuously-green report the label validation above
    exists to prevent, arriving by the other door."""
    _drop_sweep_triggers()
    kit = PressKit.objects.create(headline='Shared')
    Album.objects.create(title='Hemispheres', band=band, press_kit=kit)
    Album.objects.create(title='Permanent Waves', band=band, press_kit=kit)
    Album.objects.filter(press_kit=kit).delete()

    unscoped = re.search(r'(\d+) dependent table\(s\) checked', _sweep('--repair'))
    scoped = re.search(r'(\d+) dependent table\(s\) checked', _sweep('testapp'))

    assert int(unscoped.group(1)) > int(scoped.group(1))
    # `crossapp_dependent_shared` is a dependent too, and the scoped run did not reach it.
    assert int(scoped.group(1)) >= 1


@pytest.mark.django_db
def test_the_sweep_follows_an_owner_whose_app_is_outside_local_apps():
    """What the generator would emit *today* is the wrong question. `crossapp_owner` is out of
    ``LOCAL_APPS``, yet its migrations created a live owned rule, so it leaks like any other
    and the repair is exactly as owed. Gating on ``LOCAL_APPS`` declined it."""
    _drop_sweep_triggers()
    shared = Shared.objects.create()
    Owner.objects.create(target=shared)
    Owner.objects.create(target=shared)
    Owner.objects.filter(target=shared).delete()  # one statement, both owners: leaked

    assert Shared.objects.filter(pk=shared.pk).exists()
    _sweep('--repair')
    assert not Shared.objects.filter(pk=shared.pk).exists()


@pytest.mark.django_db
def test_the_sweep_does_not_follow_a_relation_the_database_has_no_rule_for():
    """The other half of the same question: a declared ``OwningForeignKey`` whose rule was
    never created -- an app never generated for -- stamped nothing going away, so repairing
    through it invents a soft-deletion the database was never asked for."""
    _drop_sweep_triggers()
    _drop_owned_rules('crossapp_owner_owner')
    shared = Shared.objects.create()
    Owner.objects.create(target=shared)
    Owner.objects.create(target=shared)
    Owner._all_objects.filter(target=shared).update(_deleted_at=timezone.now())

    assert 'Owned sweep complete.' in _sweep()
    assert Shared.objects.filter(pk=shared.pk).exists()


@pytest.mark.django_db
def test_repair_re_asks_the_predicate_against_an_owner_that_appeared_after_the_scan():
    """The scan and the stamp are two statements. An owner committed between them makes the
    scan's verdict stale, and stamping on it destroys a row a live owner holds -- the very
    loss the last-owner guard exists for."""
    _drop_sweep_triggers()
    placard = Placard.objects.create(caption='Contended')
    Kiosk.objects.create(label='Lobby', placard=placard)
    Kiosk.objects.create(label='Balcony', placard=placard)
    Kiosk.objects.filter(placard=placard).delete()  # one statement, both owners: leaked
    assert Placard.objects.filter(pk=placard.pk).exists()

    # ``timezone.now`` is read while building the UPDATE, after the scan has been read back
    # and before the statement runs -- exactly the window a concurrent commit lands in.
    real_now = sweepowned_module.timezone.now
    arrived = []

    def racing_now():
        if not arrived:
            arrived.append('once')  # before the create, whose own now() would recurse
            Foyer.objects.create(label='Mezzanine', placard=placard)
        return real_now()

    sweepowned_module.timezone.now = racing_now
    try:
        _sweep('--repair')
    finally:
        sweepowned_module.timezone.now = real_now

    assert Placard.objects.filter(pk=placard.pk).exists()


@pytest.mark.django_db
def test_the_sweep_matches_an_owner_whose_db_table_is_self_quoted(monkeypatch):
    """``pg_rules`` reports bare identifiers; a ``db_table`` may carry Django's own quoted form,
    which this gate has to normalise. Compared raw it matches no rule, so every relation on that
    owner table is dropped and the run reports a clean sweep: green, having done nothing."""
    bare, _ = Command._rule_carrying_owners('default')
    assert Kiosk in [owner for owner, _ in bare[Placard]]  # the control, on the plain name

    monkeypatch.setattr(Kiosk._meta, 'db_table', f'"{Kiosk._meta.db_table}"')
    quoted, unresolved = Command._rule_carrying_owners('default')

    assert Kiosk in [owner for owner, _ in quoted[Placard]]
    assert unresolved == []  # resolvable, so nothing is skipped and nothing is reported


@pytest.mark.django_db
def test_the_sweep_does_not_let_a_row_be_its_own_live_owner():
    """The rule's target exclusion, re-asked in Python: an arm taking liveness from the
    *dependent's own* table reads a self-pointing row as its own live owner and spares it for
    ever. ``Placard.parent`` is such an arm -- refused a rule, but the sparing half is wider."""
    _drop_sweep_triggers()
    placard = Placard.objects.create(caption='Self-parented')
    Placard.objects.filter(pk=placard.pk).update(parent=placard)  # its own parent
    Kiosk.objects.create(label='Lobby', placard=placard)
    Kiosk.objects.create(label='Annex', placard=placard)
    Kiosk.objects.filter(placard=placard).delete()  # one statement, every rule-carrying owner

    assert Placard.objects.filter(pk=placard.pk).exists()  # the rule stamped nothing

    _sweep('--repair')

    assert not Placard.objects.filter(pk=placard.pk).exists()


@pytest.mark.django_db
def test_the_sweep_names_a_relation_whose_table_it_cannot_spell(monkeypatch):
    """A ``db_table`` with two schema-qualifying dots is one ``_split_qualified`` refuses.
    Raising would take the whole report down over one relation, so the run skips it -- and
    names it, a silent skip being a clean sweep of a table nothing swept."""
    monkeypatch.setattr(Kiosk._meta, 'db_table', 'one.two.three')

    owners, unresolved = Command._rule_carrying_owners('default')

    assert Kiosk not in [owner for owner, _ in owners.get(Placard, ())]
    assert any('testapp.Kiosk' in line for line in unresolved)
    assert any('more than one schema-qualifying' in line for line in unresolved)


@pytest.mark.django_db
def test_the_sweep_reports_an_unspellable_relation_to_the_operator(monkeypatch):
    """The other half: a skip the caller can see is worth nothing if the run does not say so.
    Printed before the gate, so a report that raises still carries it."""
    monkeypatch.setattr(
        Command,
        '_rule_carrying_owners',
        classmethod(lambda cls, using: ({}, ['testapp.Kiosk -> testapp.Placard skipped: nope'])),
    )

    assert 'testapp.Kiosk -> testapp.Placard skipped: nope' in _sweep()
