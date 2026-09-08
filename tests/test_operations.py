"""Tests for :mod:`guitars.operations`. Nothing in this kit retires an enforcement object on
its own, so an author removing a column a rule names has to say so: these pin what
``RetireEnforcement`` drops, what it leaves, and that it refuses to be reversed."""

import pytest
from django.db import connection, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations import Migration
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import SeparateDatabaseAndState
from django.db.migrations.writer import OperationWriter

from guitars.management.enforcement import graph, scanning
from guitars.management.enforcement.scanning import scan_existing_operations
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


# --- The generator reading a retirement back -------------------------------------------------


def _loader_with(app_label: str, migrations_by_name: dict) -> MigrationLoader:
    """A real loader with synthetic migrations grafted on, so the walk is exercised against
    Django's own graph rather than a stub of it."""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    for name, operations in migrations_by_name.items():
        migration = Migration(name, app_label)
        migration.operations = operations
        loader.disk_migrations[(app_label, name)] = migration
    return loader


def test_a_retirement_is_read_off_the_loaded_operations():
    """Not by matching the call in the file's text: a regex over Python call syntax misses
    keyword and quoting variants, and says nothing about ordering against the headers."""
    stem = '0042_auto_enforcement'
    loader = _loader_with('testapp', {stem: [RetireEnforcement('shop_order', column='label_id')]})

    assert graph.retired_enforcement(loader, 'testapp')[stem] == [('shop_order', 'label_id')]


def test_a_retirement_wrapped_in_separate_database_and_state_is_still_seen():
    """The standard idiom for a change the database already has, and what a hand-tuned squash
    carries -- read past exactly as :func:`_establishes` reads past it."""
    stem = '0042_auto_enforcement'
    wrapped = SeparateDatabaseAndState(
        database_operations=[RetireEnforcement('shop_order')], state_operations=[]
    )
    loader = _loader_with('testapp', {stem: [wrapped]})

    assert graph.retired_enforcement(loader, 'testapp')[stem] == [('shop_order', None)]


def test_a_migration_with_no_retirement_is_absent_from_the_walk():
    loader = _loader_with('testapp', {'0042_auto_enforcement': []})

    assert '0042_auto_enforcement' not in graph.retired_enforcement(loader, 'testapp')


def test_a_column_retirement_subtracts_only_the_keys_naming_that_column():
    """A column form matches the families whose dedupe key spells a column, and leaves the
    table-keyed ones: dropping `_deleted_at` retires more than a key can name."""
    keyed = {
        'related': {('shop_line', 'shop_order', 'order_id'): 'aaa', ('shop_line', 'x', 'y'): 'b'},
        'self_cascade': {('shop_line', 'order_id'): 'ccc'},
    }
    whole = {'triggers': {'shop_line': 'ddd'}}

    scanning._subtract_retired('shop_line', 'order_id', keyed, whole)

    assert keyed['related'] == {('shop_line', 'x', 'y'): 'b'}
    assert keyed['self_cascade'] == {}
    assert whole['triggers'] == {'shop_line': 'ddd'}


def test_a_whole_table_retirement_subtracts_both_shapes():
    keyed = {'related': {('shop_line', 'shop_order', 'order_id'): 'aaa'}}
    whole = {'triggers': {'shop_line': 'ddd', 'shop_order': 'eee'}}

    scanning._subtract_retired('shop_line', None, keyed, whole)

    assert keyed['related'] == {}
    assert whole['triggers'] == {'shop_order': 'eee'}


def _retire_at(monkeypatch, stem: str, table: str, column: str | None = None) -> None:
    """Pretend *stem* carried a ``RetireEnforcement``, so the wiring can be exercised against
    the real testapp migrations without a migration that would really drop those objects."""
    real = graph.retired_enforcement
    monkeypatch.setattr(
        scanning,
        'retired_enforcement',
        lambda loader, app: {stem: [(table, column)]} if app == 'testapp' else real(loader, app),
    )


def test_the_scan_forgets_what_a_retirement_dropped(monkeypatch):
    """The wiring, end to end against the real testapp migrations. Retired in the last
    migration, so nothing re-asserts it afterwards and the next run emits it again rather than
    reading the database as covered."""
    assert 'testapp_setlistentry' in scan_existing_operations().soft_deletes

    _retire_at(monkeypatch, '0046_auto_enforcement', 'testapp_setlistentry')

    existing = scan_existing_operations()
    assert 'testapp_setlistentry' not in existing.soft_deletes
    assert 'testapp_setlistentry' not in existing.triggers
    assert ('testapp_setlistentry', 'testapp_setlist', None) not in existing.soft_delete_related
    # Only that table: the tree beside it keeps everything of its own.
    assert 'testapp_setlist' in existing.soft_deletes


def test_a_migration_after_the_retirement_records_the_key_again(monkeypatch):
    """The ordering rule, and why the subtraction happens inside the file walk. Retired at
    0043: 0045 and 0046 re-assert the self-cascade trigger afterwards and undo that half, while
    the plain rule -- written once, at 0042 -- stays forgotten."""
    _retire_at(monkeypatch, '0043_riser_rack_troupe', 'testapp_setlist')

    existing = scan_existing_operations()

    assert ('testapp_setlist', 'parent_id') in existing.soft_delete_self_cascade
    assert 'testapp_setlist' not in existing.soft_deletes


def test_a_column_retirement_leaves_the_tables_own_coverage(monkeypatch):
    """The column form reaches the keyed families only. The cascade rule reading
    ``setlist_id`` goes; the entry table's own soft-delete rule and trigger stay, because
    dropping one column is not dropping the model."""
    _retire_at(monkeypatch, '0046_auto_enforcement', 'testapp_setlistentry', 'setlist_id')

    existing = scan_existing_operations()

    assert ('testapp_setlistentry', 'testapp_setlist', None) not in existing.soft_delete_related
    assert 'testapp_setlistentry' in existing.soft_deletes
    assert 'testapp_setlistentry' in existing.triggers


def test_a_graph_node_with_no_disk_migration_is_skipped():
    """A squash replaces its nodes, so the graph can name a migration no file backs. Reading
    past it is the same call ``resolve_object_migration`` makes."""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    missing = next(name for (app, name) in loader.disk_migrations if app == 'testapp')
    del loader.disk_migrations[('testapp', missing)]

    assert missing not in graph.retired_enforcement(loader, 'testapp')


def test_retiring_a_tenanted_table_forgets_its_policy_and_autofill(monkeypatch):
    """A whole-table retirement drops the table's policies with its triggers, so the scan has
    to forget those too -- otherwise ``--check`` reads a policied table the database no longer
    protects as covered."""
    existing = scan_existing_operations()
    assert 'testapp_troupe' in existing.tenant_policies
    assert any(table == 'testapp_troupe' for table, _function in existing.tenant_autofill)

    _retire_at(monkeypatch, '0046_auto_enforcement', 'testapp_troupe')

    existing = scan_existing_operations()
    assert 'testapp_troupe' not in existing.tenant_policies
    assert not any(table == 'testapp_troupe' for table, _function in existing.tenant_autofill)
    # A sibling tenanted table is untouched.
    assert 'testapp_release' in existing.tenant_policies


# --- The guards, each of which was deletable with the suite green ---------------------------


def test_an_unresolvable_column_refuses_rather_than_dropping_the_whole_table(db):
    """The escalation this guard exists for: with no `RAISE`, the attnum lookup returns NULL,
    every ``scoped_to_column`` test reads as the whole-table path, and a typo -- or the
    operation placed *after* its `RemoveField` -- silently drops the table's every object."""
    # A savepoint, so the aborted transaction the RAISE leaves behind does not stop the
    # assertion below from reading the catalogue.
    with pytest.raises(Exception, match='names no column'), transaction.atomic():
        _apply(RetireEnforcement('testapp_setlistentry', column='no_such_column'))

    # Nothing went with it.
    assert _objects('testapp_setlistentry') == (['soft_delete'], ['updated_at_trigger'])


def test_a_table_that_does_not_exist_refuses(db):
    """A typo in the table name is a mistake, not a no-op: silence there would read as a
    retirement that happened."""
    with pytest.raises(Exception, match='names no table'), transaction.atomic():
        _apply(RetireEnforcement('testapp_no_such_table'))


def test_a_consumers_own_trigger_and_policy_survive_the_whole_table_form(db):
    """This operation is irreversible, so it takes only what this kit mints. A consumer object
    blocking the same change is the consumer's to drop -- and putting one back is not something
    the generator knows how to do."""
    with connection.cursor() as cursor:
        cursor.execute(
            'CREATE TRIGGER zz_consumer_audit AFTER INSERT ON testapp_setlistentry '
            "FOR EACH STATEMENT EXECUTE FUNCTION set_updated_at('id')"
        )
        cursor.execute('ALTER TABLE testapp_setlistentry ENABLE ROW LEVEL SECURITY')
        cursor.execute('CREATE POLICY zz_consumer_policy ON testapp_setlistentry USING (true)')

    _apply(RetireEnforcement('testapp_setlistentry'))

    assert _objects('testapp_setlistentry') == ([], ['zz_consumer_audit'])
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT polname FROM pg_policy WHERE polrelid = 'testapp_setlistentry'::regclass"
        )
        assert [row[0] for row in cursor.fetchall()] == ['zz_consumer_policy']


def test_a_consumers_own_rule_survives_the_column_form(db):
    """The rule loop is filtered by name too, for the same reason. It means a consumer rule
    naming the column still blocks their `RemoveField`, which is theirs to resolve."""
    with connection.cursor() as cursor:
        cursor.execute(
            'CREATE RULE zz_consumer_rule AS ON UPDATE TO testapp_setlistentry '
            'DO ALSO SELECT 1 WHERE new.setlist_id IS NOT NULL'
        )

    _apply(RetireEnforcement('testapp_setlistentry', column='setlist_id'))

    assert 'zz_consumer_rule' in _objects('testapp_setlistentry')[0]


def test_a_mixed_case_db_table_resolves(db):
    """``'MyTable'::regclass`` resolves ``mytable``; the operation goes through
    ``to_regclass`` over the quoted spelling the rest of the kit writes."""
    with connection.cursor() as cursor:
        cursor.execute('CREATE TABLE "MixedCase" (id serial primary key)')
        cursor.execute('CREATE RULE soft_delete_related_probe AS ON UPDATE TO "MixedCase" '
                       'DO ALSO SELECT 1')

    _apply(RetireEnforcement('MixedCase'))

    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_rules WHERE tablename = 'MixedCase'")
        assert cursor.fetchone()[0] == 0


def test_retiring_the_last_tenant_policy_takes_row_level_security_down_with_it(db):
    """Row-level security is a table *flag*, not an object `pg_depend` reaches. Dropping the
    last `tenant_scope` off a FORCEd table would leave it returning no rows to anyone, the
    owner included, silently and irreversibly."""
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT relrowsecurity, relforcerowsecurity FROM pg_class '
            "WHERE oid = 'testapp_troupe'::regclass"
        )
        assert cursor.fetchone() == (True, True)

    _apply(RetireEnforcement('testapp_troupe', column='label_id'))

    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT relrowsecurity, relforcerowsecurity FROM pg_class '
            "WHERE oid = 'testapp_troupe'::regclass"
        )
        assert cursor.fetchone() == (False, False)
        cursor.execute(
            "SELECT count(*) FROM pg_policy WHERE polrelid = 'testapp_troupe'::regclass"
        )
        assert cursor.fetchone()[0] == 0
