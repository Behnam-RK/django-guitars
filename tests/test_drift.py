"""Hostile / drifted-database tests.

The rest of the suite proves the enforcement layers correct on a database this project's
own tooling built. This file asks a different question: what happens once an operator
touches the database directly -- the exact thing the whole design exists to survive, since
that is precisely the class of write (raw SQL, someone else's migration, a manual "fix")
none of the Python-side guarantees can see coming.

Two audit layers exist, and each has a scope this file makes concrete by finding its edge:

* ``makemigrations``/``makeguitarmigrations --check`` is a *build-time* gate. It compares
  the SQL constants in ``guitars.sql`` against the digests recorded in migration *files* on
  disk -- it never queries the database at all. A live object that diverges from what those
  files describe, without the files themselves changing, is invisible to it by construction.
* ``audittenancy`` is a *runtime* gate, but only for tenant RLS. It has no soft-delete or
  ``_updated_at`` equivalent -- there is no live checker for a hand-dropped rule or trigger
  anywhere in this package.

So a hand-dropped soft-delete rule or ``_updated_at`` trigger is invisible to *both* layers;
only a hand-altered or hand-dropped tenant *policy* is caught, and only by the second one.
That asymmetry, demonstrated rather than asserted, is the point of this module.
"""

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
    """Run ``makeguitarmigrations --check``, returning its output.

    Raises ``CommandError`` itself if the command disagrees that the database is
    covered -- callers that expect it to pass just call this and move on.
    """
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
    """Same blind spot for the other DB-enforced column: a trigger dropped by hand leaves
    ``_updated_at`` frozen on every future write, with nothing here to notice before an
    operator finds a suspiciously stale timestamp.

    ``transaction=True`` and the trigger is restored in ``finally``: proving "the timestamp
    did not move" needs two *separate* transactions (``NOW()`` is fixed for the life of one
    transaction regardless of the trigger, so a single rollback-wrapped test would pass
    vacuously here -- see ``test_base.py::test_updated_at_trigger_advances_on_update`` for
    the positive control that needs the same setting). Because this is a real commit, not a
    fixture rollback, the dropped trigger has to be put back explicitly or every later test
    in the session would run against a table that no longer bumps its own timestamp.
    """

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
    """``--adopt`` exists for exactly one situation: an enforcement object that is really
    there, but whose migration history has no record of it -- a hand-migrated legacy
    project, or a database an earlier, interrupted run left half-covered. Proven directly
    against the SQL forms themselves, since ``guitars.sql``'s names are a frozen interface
    and this is the first place any test actually executes the adopt/plain pair against
    real PostgreSQL rather than only asserting their text (see tests/test_sql_interface.py).

    Both statements run inside the same test's rollback-wrapped transaction, so the plain
    form's failure and the adopt form's success are shown back to back without either one
    leaving the shared test database in a different state than it found it.
    """

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
