"""A ``CASCADE`` cycle through several tables is enforced like any other key (#85, ADR 0041): its
arms stop at ``_deleted_at IS NULL``, where a rule was rewritten into itself. Raw statements, as
in ``test_cascade_arms``: the collector would name every row itself."""

import pytest
from django.apps import apps as django_apps

from guitars.management.enforcement.command import Command
from guitars.models._cascade_coverage import cascade_plan
from tests.conftest import execute, rows
from tests.testapp.models import Baton, Lineage, Offshoot, Relay


EARLIER = '2000-01-01T00:00:00Z'


def _pair(**held):
    """A relay and a baton holding each other: ``relay.baton`` and ``baton.relay``."""
    relay = Relay.objects.create()
    baton = Baton.objects.create(relay=relay)
    Relay.objects.filter(pk=relay.pk).update(baton=baton)
    return relay, baton


def _stamps(model) -> dict[int, object]:
    return dict(rows(f'SELECT id, _deleted_at FROM {model._meta.db_table}'))


def _archive_relay(pk: int) -> None:
    execute('UPDATE testapp_relay SET _deleted_at = NOW() WHERE id = %s', params=[pk])


@pytest.fixture
def loop(db):
    """``r1 <-> b1``, plus ``r2`` held by ``b1``, ``r3`` archived long ago and held by ``b1``, and
    an unrelated pair the cycle never reaches."""
    r1, b1 = _pair()
    r2 = Relay.objects.create(baton=b1)
    r3 = Relay.objects.create(baton=b1)
    execute('UPDATE testapp_relay SET _deleted_at = %s WHERE id = %s', params=[EARLIER, r3.pk])
    other_relay, other_baton = _pair()
    return {'r1': r1, 'b1': b1, 'r2': r2, 'r3': r3, 'other': (other_relay, other_baton)}


def test_archiving_one_row_archives_everything_the_loop_reaches(loop):
    _archive_relay(loop['r1'].pk)

    relays, batons = _stamps(Relay), _stamps(Baton)
    stamp = relays[loop['r1'].pk]
    assert stamp is not None
    assert batons[loop['b1'].pk] == stamp
    assert relays[loop['r2'].pk] == stamp


def test_a_row_archived_earlier_keeps_its_own_stamp(loop):
    _archive_relay(loop['r1'].pk)

    assert str(_stamps(Relay)[loop['r3'].pk]).startswith('2000-01-01')


def test_a_loop_outside_the_component_is_left_alone(loop):
    other_relay, other_baton = loop['other']

    _archive_relay(loop['r1'].pk)

    assert _stamps(Relay)[other_relay.pk] is None
    assert _stamps(Baton)[other_baton.pk] is None


def test_restoring_a_row_restores_what_was_archived_with_it_up_the_loop_too(loop):
    """The revive matches the stamp, so it travels back up a cycle: restoring ``b1`` restores
    ``r1``, the row that was archived first. Never the row archived on its own."""
    _archive_relay(loop['r1'].pk)

    execute('UPDATE testapp_baton SET _deleted_at = NULL WHERE id = %s', params=[loop['b1'].pk])

    relays = _stamps(Relay)
    assert relays[loop['r1'].pk] is None
    assert relays[loop['r2'].pk] is None
    assert str(relays[loop['r3'].pk]).startswith('2000-01-01')


def test_a_long_alternating_chain_settles_in_one_statement(db):
    """``r0 -> b0 -> r1 -> b1 ...`` with the last baton pointing back at ``r0``: each archive
    nests the next level's trigger, so termination is the rows running out."""
    length = 15
    relays = [Relay.objects.create() for _ in range(length)]
    batons = [Baton.objects.create(relay=relay) for relay in relays]
    for index, relay in enumerate(relays):
        Relay.objects.filter(pk=relay.pk).update(baton=batons[index - 1])

    _archive_relay(relays[0].pk)

    stamps = {*_stamps(Relay).values(), *_stamps(Baton).values()}
    assert len(stamps) == 1 and None not in stamps


def test_the_generator_writes_both_keys_and_notes_no_cycle():
    command = Command()
    command._build_operations(django_apps.get_app_config('testapp'))

    assert not [note for note in command._skipped_rule_notes if 'testapp_relay' in note]
    assert not [note for note in command._skipped_rule_notes if 'testapp_baton' in note]


@pytest.mark.parametrize('model', [Relay, Baton])
def test_the_plan_has_no_gap_for_a_cycle(model):
    assert not [gap for gap in cascade_plan(model)[0] if 'cycle' in gap.reason]


def test_soft_delete_archives_the_loop(loop):
    """``soft_delete()`` raised ``SoftDeleteUnsupportedError`` for a cycle edge."""
    Relay.objects.filter(pk=loop['r1'].pk).soft_delete()

    assert _stamps(Baton)[loop['b1'].pk] is not None
    assert _stamps(Relay)[loop['r2'].pk] is not None


def test_delete_archives_the_loop_through_the_fast_path(loop):
    loop['r1'].delete()

    assert _stamps(Baton)[loop['b1'].pk] is not None


def test_a_key_through_mti_into_its_own_root_cascades(db):
    """The one-node cycle ADR 0025 refused: ``Offshoot`` is a row of ``testapp_lineage`` whose key
    points back at that table, so the joined arm updates the table whose trigger runs it."""
    root = Lineage.objects.create(name='root')
    child = Offshoot.objects.create(name='child', parent=root)
    grandchild = Offshoot.objects.create(name='grandchild', parent=child)
    bystander = Lineage.objects.create(name='bystander')

    execute('UPDATE testapp_lineage SET _deleted_at = NOW() WHERE id = %s', params=[root.pk])

    stamps = _stamps(Lineage)
    assert stamps[root.pk] is not None
    assert stamps[child.pk] == stamps[root.pk] == stamps[grandchild.pk]
    assert stamps[bystander.pk] is None
