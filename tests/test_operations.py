"""Tests for :mod:`guitars.operations`. Nothing in this kit retires an enforcement object on
its own, so an author removing a column a rule names has to say so: these pin what
``RetireEnforcement`` drops, what it leaves, and that it refuses to be reversed."""

import pytest
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.writer import OperationWriter

from guitars.operations import RetireEnforcement


def _objects(table: str) -> tuple[list[str], list[str]]:
    """The rules on *table* and the user triggers on it, both sorted."""
    with connection.cursor() as cursor:
        cursor.execute('SELECT rulename FROM pg_rules WHERE tablename = %s ORDER BY 1', [table])
        rules = [row[0] for row in cursor.fetchall()]
        cursor.execute(
            'SELECT tgname FROM pg_trigger WHERE tgrelid = %s::regclass '
            'AND NOT tgisinternal ORDER BY 1',
            [table],
        )
        return rules, [row[0] for row in cursor.fetchall()]


def _apply(operation: RetireEnforcement) -> None:
    with connection.schema_editor(atomic=False) as editor:
        operation.database_forwards('testapp', editor, None, None)


def test_column_mode_drops_only_what_depends_on_that_column(db):
    """The narrow form, and the one an author reaches for: it takes the cascade rule that
    names the column -- which lives on the *other* table -- and nothing else."""
    _apply(RetireEnforcement('testapp_setlistentry', column='setlist_id'))

    assert _objects('testapp_setlist')[0] == ['soft_delete']
    # The child's own rule and trigger are untouched: the column is going, not the table.
    assert _objects('testapp_setlistentry') == (['soft_delete'], ['updated_at_trigger'])


def test_column_mode_leaves_the_self_cascade_trigger_alone(db):
    """A trigger is not a column dependency. Retiring a column must not take the tree's
    trigger with it, which a table-wide sweep would."""
    _apply(RetireEnforcement('testapp_setlistentry', column='setlist_id'))

    assert (
        'soft_delete_self_cascade_15_testapp_setlist_9_parent_id' in _objects('testapp_setlist')[1]
    )


def test_table_mode_also_takes_the_tables_own_rules_and_triggers(db):
    """The whole-table form, for a model being deleted: the dependent rule on the other table
    goes as before, and so do the objects sitting on the table itself."""
    _apply(RetireEnforcement('testapp_setlistentry'))

    assert _objects('testapp_setlistentry') == ([], [])
    # And only that table's: the parent keeps everything of its own.
    assert _objects('testapp_setlist')[0] == ['soft_delete']


def test_it_unblocks_the_drop_column_that_would_otherwise_fail(db):
    """The failure the operation exists for. Django 6.0's ``sql_delete_column`` carries no
    ``CASCADE``, so a rule naming the column makes ``RemoveField`` fail at ``migrate`` --
    spelled here as the bare ``ALTER TABLE`` so the test says the same thing on 5.2."""
    with pytest.raises(Exception, match='depends on column'), connection.cursor() as cursor:
        cursor.execute('ALTER TABLE testapp_setlistentry DROP COLUMN setlist_id')


def test_the_drop_column_succeeds_once_the_enforcement_is_retired(db):
    """The other half: same statement, after the operation."""
    _apply(RetireEnforcement('testapp_setlistentry', column='setlist_id'))

    with connection.cursor() as cursor:
        cursor.execute('ALTER TABLE testapp_setlistentry DROP COLUMN setlist_id')

    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT count(*) FROM information_schema.columns '
            "WHERE table_name = 'testapp_setlistentry' AND column_name = 'setlist_id'"
        )
        assert cursor.fetchone()[0] == 0


def test_retiring_a_table_nothing_depends_on_is_a_no_op(db):
    """Idempotent by construction -- every drop is ``IF EXISTS`` and the scan simply finds
    nothing -- so running it twice, or against an already-clean table, is safe."""
    _apply(RetireEnforcement('testapp_setlistentry'))
    _apply(RetireEnforcement('testapp_setlistentry'))

    assert _objects('testapp_setlistentry') == ([], [])


def test_it_refuses_to_be_reversed():
    """``migrate`` reads :attr:`reversible` and stops before running anything, which is the
    safe direction: what this dropped is written only by the generator, and a silent no-op
    reverse would leave history claiming objects the database no longer has."""
    assert RetireEnforcement('shop_order').reversible is False

    with pytest.raises(IrreversibleError, match='makeguitarmigrations --adopt'):
        RetireEnforcement('shop_order').database_backwards('shop', None, None, None)


def test_it_round_trips_through_the_migration_writer():
    """An author writes it into a migration by hand and the scanner reads it back, so the
    written form has to reconstruct -- including the keyword argument."""
    source, imports = OperationWriter(
        RetireEnforcement('shop_order', column='archived_at'), indentation=0
    ).serialize()

    assert 'import guitars.operations' in imports
    # Rebuilt from the written text rather than matched against it: the writer is free to
    # choose positional or keyword form and to wrap lines, and none of that is the contract.
    import guitars.operations  # noqa: PLC0415 - the written source needs the name bound

    rebuilt = eval(source.rstrip(','), {'guitars': guitars})  # noqa: S307 - our own output
    assert (rebuilt.table, rebuilt.column) == ('shop_order', 'archived_at')


def test_a_name_that_closes_the_dollar_quoting_is_refused():
    """An identifier admits ``$``, and this operation is written as a dollar-quoted ``DO``
    block, so a name closing that tag is refused rather than escaped -- the same call the
    enforcement generator makes about ``$$`` in a ``db_table``."""
    with pytest.raises(ValueError, match='closes the dollar quoting'):
        RetireEnforcement('a$guitars_retire$b')

    with pytest.raises(ValueError, match='closes the dollar quoting'):
        RetireEnforcement('shop_order', column='a$guitars_retire$b')


def test_it_describes_both_forms():
    """``describe()`` is what ``sqlmigrate`` and ``migrate --plan`` print, and
    ``migration_name_fragment`` is what names an autogenerated migration file."""
    whole, one_column = (
        RetireEnforcement('shop_order'),
        RetireEnforcement('shop_order', column='archived_at'),
    )

    assert whole.describe().endswith('shop_order')
    assert one_column.describe().endswith('shop_order.archived_at')
    assert whole.migration_name_fragment == 'retire_enforcement_shop_order'
    assert one_column.migration_name_fragment == 'retire_enforcement_shop_order_archived_at'
    assert repr(whole) == "<RetireEnforcement 'shop_order'>"
    assert repr(one_column) == "<RetireEnforcement 'shop_order', column='archived_at'>"
