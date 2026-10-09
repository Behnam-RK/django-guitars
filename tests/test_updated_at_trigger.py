"""The ``_updated_at`` trigger's cost and timing (#80, ADR 0038): a ``BEFORE ROW`` assignment, so
each row an ``UPDATE`` touches is written once. The statement form it replaced issued a second
``UPDATE``, writing every row twice, re-expanded by the table's ``ON UPDATE`` rules."""

from importlib import import_module

import pytest
from django.apps import apps as django_apps
from django.db import connection, transaction

from guitars.management.enforcement.command import Command
from tests.conftest import execute, rows, scalar
from guitars import sql
from guitars.sql import triggers as _triggers
from tests.testapp.models import Album, Band, ChamberOrchestra, Ensemble, Merch, Orchestra


_TABLES = ('testapp_band', 'testapp_album', 'testapp_merch')


def _writes() -> dict[str, int]:
    """Tuples updated so far in this transaction, per table. ``pg_stat_xact_*`` reads the
    backend's pending counters, so a delta across one statement is that statement's writes."""
    counts = dict(
        rows(
            'SELECT relname, n_tup_upd FROM pg_stat_xact_user_tables WHERE relname = ANY(%s)',
            [list(_TABLES)],
        )
    )
    return {table: counts.get(table, 0) for table in _TABLES}


def _writes_of(statement: str, params: list) -> dict[str, int]:
    before = _writes()
    execute(statement, params=params)
    after = _writes()
    return {table: after[table] - before[table] for table in _TABLES}


@pytest.fixture
def chain(db):
    band = Band.objects.create(name='Rush')
    album = Album.objects.create(title='2112', band=band)
    Merch.objects.create(description='shirt', album=album)
    return band


def test_an_update_writes_its_row_once(chain):
    """The owner is the only row a rename touches; the statement form wrote it twice."""
    writes = _writes_of('UPDATE testapp_band SET name = name WHERE id = %s', [chain.pk])

    assert writes == {'testapp_band': 1, 'testapp_album': 0, 'testapp_merch': 0}


def test_an_archive_writes_every_cascaded_row_once(chain):
    """The case #80 measured: each cascade level's rule-generated ``UPDATE`` fired that table's
    own statement trigger at depth 0, and its follow-up wrote the child a second time."""
    writes = _writes_of('UPDATE testapp_band SET _deleted_at = NOW() WHERE id = %s', [chain.pk])

    assert writes == {'testapp_band': 1, 'testapp_album': 1, 'testapp_merch': 1}


@pytest.mark.django_db(transaction=True)
def test_returning_reads_the_stamped_value():
    """An ``AFTER`` trigger's follow-up runs after ``RETURNING`` has been computed, so the
    statement handed back the value the row was about to lose."""
    band = Band.objects.create(name='Rush')
    band.refresh_from_db()

    with transaction.atomic():
        returned = rows(
            'UPDATE testapp_band SET name = %s WHERE id = %s RETURNING _updated_at',
            ['Yes', band.pk],
        )[0][0]

    band.refresh_from_db()
    assert returned == band._updated_at
    assert returned > band._created_at


@pytest.mark.django_db(transaction=True)
def test_a_caller_cannot_set_the_column():
    """Always assigned, never only when the caller left it alone: ``save()`` writes the value
    it loaded, which reads as "set by the caller" once another transaction has moved the row."""
    band = Band.objects.create(name='Rush')
    band.refresh_from_db()

    with transaction.atomic():
        Band.objects.filter(pk=band.pk).update(_updated_at=band._created_at, name='Yes')

    band.refresh_from_db()
    assert band._updated_at > band._created_at


def test_the_trigger_is_a_row_trigger_before_the_update(db):
    """Pinned off the catalogue: the two behaviours above hold for a ``BEFORE ROW`` trigger, and
    the statement form fails both, but nothing else would notice a regression to it."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgtype FROM pg_trigger WHERE tgname = 'updated_at_trigger' "
            "AND tgrelid = 'testapp_band'::regclass"
        )
        (tgtype,) = cursor.fetchone()
    row, before = 1, 2
    assert tgtype & row and tgtype & before


def _updated_at_op(recorded: str) -> str:
    """The generated ``updated_at_trigger`` operation for ``Band`` against a history that
    recorded *recorded* for its table."""
    command = Command()
    command.existing.triggers['testapp_band'] = recorded
    operations = command._build_operations(django_apps.get_app_config('testapp'))
    (operation,) = [op for op in operations if 'Updated at Trigger on "testapp_band"' in op]
    return operation


def test_replacing_the_statement_trigger_reverses_to_it():
    """Unapplying the migration that swaps the trigger must not leave the table with none."""
    legacy_digest, _ = Command._legacy_updated_at_restore('"testapp_band"', 'id')

    operation = _updated_at_op(legacy_digest)

    forward, reverse = operation.split('reverse_sql=')
    assert 'CREATE OR REPLACE TRIGGER updated_at_trigger' in forward
    assert 'AFTER UPDATE ON "testapp_band" REFERENCING NEW TABLE' in reverse
    assert "set_updated_at('id')" in reverse


def test_replacing_any_other_predecessor_reverses_to_a_drop():
    """The restore is keyed on the digest the statement form recorded: a later replace of a
    row trigger must not reverse to a trigger two shapes old."""
    operation = _updated_at_op('stale0000000')

    assert (
        'DROP TRIGGER updated_at_trigger ON "testapp_band"' in operation.split('reverse_sql=')[1]
    )
    assert 'AFTER UPDATE' not in operation


def test_the_generated_reverse_puts_the_statement_trigger_back(db):
    """Against real PostgreSQL, off the committed replacement itself: run its ``reverse_sql`` for
    ``Band`` and read the catalogue. Not ``migrate`` back across it, which would unapply every
    later ``testapp`` migration too, irreversible ``RetireEnforcement`` ones among them."""
    module = import_module('tests.testapp.migrations.0083_auto_enforcement')
    (reverse,) = [
        op.reverse_sql
        for op in module.Migration.operations
        if 'ON "testapp_band"' in op.reverse_sql and 'updated_at_trigger' in op.reverse_sql
    ]
    catalogue = (
        "SELECT tgtype FROM pg_trigger WHERE tgname = 'updated_at_trigger' "
        "AND tgrelid = 'testapp_band'::regclass"
    )
    row, before = 1, 2

    with transaction.atomic():
        execute(reverse)
        statement_level = scalar(catalogue)
        transaction.set_rollback(True)

    assert not statement_level & (row | before)
    assert scalar(catalogue) & row and scalar(catalogue) & before


# ---- The MTI parent trigger (#84, ADR 0040): its follow-up skips an ancestor already stamped.

_MTI_TABLES = ('testapp_ensemble', 'testapp_orchestra', 'testapp_chamberorchestra')


def _mti_writes() -> dict[str, int]:
    counts = dict(
        rows(
            'SELECT relname, n_tup_upd FROM pg_stat_xact_user_tables WHERE relname = ANY(%s)',
            [list(_MTI_TABLES)],
        )
    )
    return {table: counts.get(table, 0) for table in _MTI_TABLES}


def _ancestor_writes(action) -> int:
    """Ancestor tuples *action* updates, inside one transaction -- the unit Django's ``save()``
    wraps an MTI child in. Rows are created by the caller, committed beforehand: a row born in
    the same transaction already carries ``NOW()``, so no follow-up could tell from it."""
    with transaction.atomic():
        before = _mti_writes()['testapp_ensemble']
        action()
        return _mti_writes()['testapp_ensemble'] - before


# (model, a column only its own table holds, its primary key column)
_CHILDREN = [
    pytest.param(Orchestra, 'conductor', 'ensemble_ptr_id', id='Orchestra'),
    pytest.param(ChamberOrchestra, 'seats', 'orchestra_ptr_id', id='ChamberOrchestra'),
]


def _create(model) -> int:
    return model.objects.create(name='NYP', conductor='Bernstein').pk


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(('model', 'column', 'key'), _CHILDREN)
def test_a_full_save_of_a_child_writes_its_ancestor_once(model, column, key):
    """Django updates the ancestor first, so its row trigger has stamped it by the time the child
    row's statement trigger runs; the follow-up then rewrote an identical value."""
    pk = _create(model)

    def save():
        instance = model.objects.get(pk=pk)
        instance.name = 'NYPO'
        instance.save()

    assert _ancestor_writes(save) == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(('model', 'column', 'key'), _CHILDREN)
def test_a_child_only_update_still_stamps_its_ancestor(model, column, key):
    """Django never touches the ancestor for a child-only column, so here the follow-up is the
    only write -- and must still land, one transaction later than the row's last stamp."""
    pk = _create(model)
    before = Ensemble._all_objects.get(pk=pk)._updated_at

    writes = _ancestor_writes(
        lambda: execute(
            f'UPDATE {model._meta.db_table} SET {column} = {column} WHERE {key} = %s', params=[pk]
        )
    )

    assert writes == 1
    assert Ensemble._all_objects.get(pk=pk)._updated_at > before


def _stamp_then_touch_child(pk: int) -> None:
    execute('UPDATE testapp_ensemble SET name = name WHERE id = %s', params=[pk])
    execute(
        'UPDATE testapp_orchestra SET conductor = conductor WHERE ensemble_ptr_id = %s',
        params=[pk],
    )


@pytest.mark.django_db(transaction=True)
def test_an_ancestor_already_stamped_this_transaction_is_not_rewritten():
    """Skipped by value: the ancestor's ``_updated_at`` already reads this transaction's ``NOW()``."""
    pk = _create(Orchestra)

    assert _ancestor_writes(lambda: _stamp_then_touch_child(pk)) == 1


@pytest.mark.django_db(transaction=True)
def test_the_three_argument_form_skips_too():
    """A trigger written before 2.0.0 passes three arguments and calls the same function: its
    branch carries the guard as well. Rolled back, so no later test meets the legacy trigger."""
    pk = _create(Orchestra)
    legacy = sql.CREATE_PARENT_UPDATED_AT_TRIGGER.format(
        child_table='testapp_orchestra',
        parent_table='testapp_ensemble',
        parent_pk='id',
        child_pk='ensemble_ptr_id',
    )

    with transaction.atomic():
        execute('DROP TRIGGER updated_at_trigger ON testapp_orchestra')
        execute(legacy)
        writes = _ancestor_writes(lambda: _stamp_then_touch_child(pk))
        transaction.set_rollback(True)

    assert writes == 1


def test_the_generated_reverse_puts_the_unguarded_body_back(db):
    """Off the committed replacement itself, as above: unapplying must not ``DROP FUNCTION``
    under the live MTI triggers that call it, but put the body it replaced back."""
    module = import_module(
        'tests.testapp.migrations.0087_auto_enforcement_parent_trigger_function'
    )
    (operation,) = module.Migration.operations
    body = 'SELECT prosrc FROM pg_proc WHERE proname = %s'

    with transaction.atomic():
        assert 'IS DISTINCT FROM NOW()' in scalar(body, ['set_parent_updated_at'])
        execute(operation.reverse_sql)
        restored = scalar(body, ['set_parent_updated_at'])
        transaction.set_rollback(True)

    assert 'IS DISTINCT FROM NOW()' not in restored
    assert 'IS DISTINCT FROM NOW()' in scalar(body, ['set_parent_updated_at'])
