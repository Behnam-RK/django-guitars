"""``hard_delete()`` removes every row it collected or nothing (#72): a ``DELETE`` that removes
fewer rows than were collected for its table -- a tenant scope or row policy hiding one, another
transaction removing one first -- aborts the walk, rolling back what went before it."""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.backends.utils import CursorWrapper

from guitars.models import HardDeleteIncompleteError
from guitars.sql import SWITCH_ON_HARD_DELETION
from guitars.tenancy import tenancy_bypassed, tenant
from tests.testapp.models import Offer, Orchestra, Release, Review, Tier


def _removing_first(monkeypatch, table: str, pk) -> None:
    """Before the first ``DELETE`` on *table* once the switch is on, remove row *pk* there:
    another writer, in effect. Phase 1's own ``DELETE``s run switched off and are archives."""
    real = CursorWrapper.execute
    state = {'on': False, 'done': False}

    def execute(self, sql, params=None):
        if isinstance(sql, str):
            if SWITCH_ON_HARD_DELETION in sql:
                state['on'] = True
            elif state['on'] and not state['done'] and sql.startswith(f'DELETE FROM "{table}"'):
                state['done'] = True
                real(self, f'DELETE FROM "{table}" WHERE id = %s', [pk])
        return real(self, sql, params)

    monkeypatch.setattr(CursorWrapper, 'execute', execute)


def test_another_tenants_scope_aborts_the_walk_instead_of_removing_the_children(tenants):
    """The policy hides the root, so its ``DELETE`` removes nothing; the children's policy-free
    table did not stop theirs. Committed, the children were gone and the root live."""
    with tenant(label=tenants.a):
        Review.objects.create(body='kept', release=tenants.release_a)
    pk = tenants.release_a.pk

    with tenant(label=tenants.b), pytest.raises(HardDeleteIncompleteError, match='testapp_release'):
        tenants.release_a.hard_delete()

    with tenancy_bypassed():
        assert Review._all_objects.filter(body='kept').exists()
        assert Release._all_objects.filter(pk=pk).exists()


@pytest.mark.django_db
def test_a_row_removed_by_another_writer_aborts_the_walk(monkeypatch):
    offer = Offer.objects.create(name='o')
    tier = Tier.objects.create(offer=offer)
    Tier.objects.create(offer=offer)
    pk = offer.pk
    _removing_first(monkeypatch, 'testapp_tier', tier.pk)

    with pytest.raises(HardDeleteIncompleteError, match=r'removed 1 of the 2 rows .* testapp_tier'):
        offer.hard_delete()

    monkeypatch.undo()
    assert Offer._all_objects.filter(pk=pk).exists()
    assert Tier._all_objects.filter(offer_id=pk).count() == 2


@pytest.mark.django_db
def test_the_mti_queryset_form_does_not_remove_half_a_chain(monkeypatch):
    orchestra = Orchestra.objects.create(name='o', conductor='c')
    pk = orchestra.pk
    _removing_first(monkeypatch, 'testapp_ensemble', pk)

    with pytest.raises(HardDeleteIncompleteError, match='testapp_ensemble'):
        Orchestra._all_objects.filter(pk=pk).hard_delete()

    monkeypatch.undo()
    assert Orchestra._all_objects.filter(pk=pk).exists()


@pytest.mark.django_db
def test_a_walk_that_removes_everything_it_collected_still_commits():
    offer = Offer.objects.create(name='o')
    Tier.objects.create(offer=offer)

    offer.hard_delete()

    assert not Offer._all_objects.exists()
    assert not Tier._all_objects.exists()
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('rules.hard_deletion', true)")
        assert cursor.fetchone()[0] in (None, '', 'off')


@pytest.mark.django_db
def test_the_key_read_path_removes_every_key_it_read_or_nothing(monkeypatch):
    """A window filter's keys are read first (#73); one gone by its ``DELETE`` aborts it too."""
    from django.db.models import Window  # noqa: PLC0415
    from django.db.models.functions import RowNumber  # noqa: PLC0415

    from tests.testapp.models import Band  # noqa: PLC0415

    bands = [Band.objects.create(name=name) for name in ('a', 'b')]
    _removing_first(monkeypatch, 'testapp_band', bands[0].pk)
    ranked = Band._all_objects.annotate(rn=Window(RowNumber(), order_by='pk')).filter(rn__lte=2)

    with pytest.raises(HardDeleteIncompleteError, match='removed 1 of the 2 rows'):
        ranked.hard_delete()

    monkeypatch.undo()
    assert Band._all_objects.count() == 2
