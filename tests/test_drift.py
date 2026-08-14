"""Hostile / drifted-database tests. Two audit layers: ``--check`` is *build-time*
(migration-file digests, never the database); ``audittenancy`` is *runtime* but RLS-only.
A hand-dropped rule/trigger is invisible to *both*; only a policy is caught."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.db import ProgrammingError, transaction

from guitars import sql
from tests.conftest import execute as _execute
from tests.conftest import scalar as _scalar
from tests.testapp.models import Band, Release


def _check(*app_labels) -> str:
    """Run ``makeguitarmigrations --check``, returning its output. Raises ``CommandError``
    itself if the database isn't covered -- callers that expect it to pass just call this."""
    out, err = StringIO(), StringIO()
    call_command('makeguitarmigrations', *app_labels, '--check', stdout=out, stderr=err)
    return out.getvalue() + err.getvalue()


class TestCheckIsBlindToADroppedSoftDeleteRule:
    """``--check`` scans migration files, never the live database, so this is exactly the
    "database drifted from what migrations claim" risk the M2 issue was opened for."""

    def test_check_passes_while_delete_stops_being_soft(self, db):
        band = Band.objects.create(name='Drifted')
        pk = band.pk  # captured before delete() -- Django clears instance.pk after, whether
        # or not the rule actually intercepted the DELETE, so asserting against band.pk
        # afterwards would pass vacuously regardless of the rule's fate.
        _execute(sql.DROP_SOFT_DELETE_RULE.format(table=Band._meta.db_table))

        _check()  # the build-time gate sees nothing wrong: no migration file changed

        band.delete()
        assert not Band._all_objects.filter(pk=pk).exists(), (
            'the soft_delete rule is what turns .delete() into an UPDATE -- without it '
            'the row should be gone from the table entirely, not merely stamped'
        )


class TestCheckIsBlindToADroppedUpdatedAtTrigger:
    """Same blind spot for ``_updated_at``. ``transaction=True``, trigger restored in
    ``finally``: proving "did not move" needs two *separate* transactions, and since this
    is a real commit, the trigger must be put back or later tests inherit a frozen table."""

    @pytest.mark.django_db(transaction=True)
    def test_check_passes_while_updated_at_stops_moving(self):
        table = Band._meta.db_table
        primary_key = Band._meta.pk.column
        band = Band.objects.create(name='Drifted')
        with transaction.atomic():
            band.name = 'Renamed once'
            band.save(update_fields=['name'])
        band.refresh_from_db()
        before = band._updated_at

        _execute(sql.DROP_UPDATED_AT_TRIGGER.format(table=table))
        try:
            _check()  # the build-time gate sees nothing wrong here either

            with transaction.atomic():
                Band.objects.filter(pk=band.pk).update(name='Renamed twice')
            band.refresh_from_db()
            assert band._updated_at == before, (
                'the trigger is what bumps _updated_at on every UPDATE -- without it a '
                'plain queryset.update() should leave the timestamp untouched'
            )
        finally:
            _execute(sql.CREATE_UPDATED_AT_TRIGGER.format(table=table, primary_key=primary_key))


class TestAuditTenancyCatchesWhatCheckCannot:
    """The one drift ``--check`` misses that a *different* command exists to catch --
    the boundary between the two audit layers, made concrete in one test rather than left
    implicit across two files."""

    def test_check_passes_while_audittenancy_fails_on_a_hand_dropped_policy(self, db):
        table = Release._meta.db_table
        _execute(sql.drop_tenant_policy(table=table))

        _check()  # build-time: still "up to date", nothing about the files moved

        out, err = StringIO(), StringIO()
        with pytest.raises(CommandError, match='audit failed'):
            call_command('audittenancy', stdout=out, stderr=err)
        report = out.getvalue() + err.getvalue()
        assert table in report
        assert 'no tenant_scope policy' in report


class TestAdoptAgainstAPartiallyDivergedDatabase:
    """``--adopt``: an enforcement object that's really there but unrecorded. First place
    any test executes the adopt/plain SQL pair against real PostgreSQL, not just their
    text (see test_sql_interface.py) -- both run in the same rollback-wrapped transaction."""

    def test_plain_create_fails_but_adopt_succeeds_against_an_already_present_trigger(self, db):
        table = Band._meta.db_table
        primary_key = Band._meta.pk.column

        # Sanity: the trigger genuinely already exists on this table (it does, via the
        # real migrations) -- so what follows is "nothing recorded" meeting a database
        # that already has the object, not an artificially empty one.
        assert (
            _scalar(
                "SELECT tgname FROM pg_trigger WHERE tgname = 'updated_at_trigger' "
                'AND tgrelid = %s::regclass AND tgisinternal IS FALSE',
                [table],
            )
            == 'updated_at_trigger'
        )

        # The plain form is what the generator emits when nothing is recorded -- correct
        # for a genuinely first-time table, and exactly what fails loudly here instead of
        # silently clobbering a database that has diverged from that assumption.
        with pytest.raises(ProgrammingError, match='already exists'), transaction.atomic():
            _execute(sql.CREATE_UPDATED_AT_TRIGGER.format(table=table, primary_key=primary_key))

        # The adopt form -- emitted only under --adopt -- states the same uncertainty
        # honestly with IF EXISTS, and succeeds against the identical live state.
        _execute(sql.ADOPT_UPDATED_AT_TRIGGER.format(table=table, primary_key=primary_key))
        assert (
            _scalar(
                "SELECT tgname FROM pg_trigger WHERE tgname = 'updated_at_trigger' "
                'AND tgrelid = %s::regclass AND tgisinternal IS FALSE',
                [table],
            )
            == 'updated_at_trigger'
        )
