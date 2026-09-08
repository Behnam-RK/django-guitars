"""The one migration operation this kit ships, for an author removing a column or a table an
enforcement object names. Imports Django's migration base and this package's leaf identifier
helpers only, so naming it in a migration drags in no tenancy runtime."""

# Named ``operations`` after ``django.db.migrations.operations``, and **not** ``migrations``:
# ``guitars`` is in ``INSTALLED_APPS``, so ``guitars/migrations/`` is already this app's
# (empty) migrations package and a module of that name would collide with it.

from __future__ import annotations

from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.operations.base import Operation

from guitars.sql import _identifiers


__all__ = ['RetireEnforcement']


# Read by ``pg_depend`` rather than by matching text in ``pg_rules.definition``: that view
# prints identifiers unquoted and lower-cased, so a quoted match finds nothing and an unquoted
# one cannot tell ``shop`` from ``shopping``. ``pg_depend`` is where the error itself comes from.
_RETIRE_SQL = """
DO ${tag}$
DECLARE
    guitars_target regclass := {table}::regclass;
    guitars_column smallint := {column};
    guitars_row record;
BEGIN
    FOR guitars_row IN
        SELECT DISTINCT guitars_rule.rulename AS name,
               guitars_on.oid::regclass AS fires_on
        FROM pg_depend AS guitars_dep
        JOIN pg_rewrite AS guitars_rule
            ON guitars_rule.oid = guitars_dep.objid
           AND guitars_dep.classid = 'pg_rewrite'::regclass
        JOIN pg_class AS guitars_on ON guitars_on.oid = guitars_rule.ev_class
        WHERE guitars_dep.refclassid = 'pg_class'::regclass
          AND guitars_dep.refobjid = guitars_target
          AND (guitars_column IS NULL OR guitars_dep.refobjsubid = guitars_column)
          AND guitars_rule.rulename <> '_RETURN'
    LOOP
        EXECUTE format('DROP RULE IF EXISTS %I ON %s', guitars_row.name, guitars_row.fires_on);
    END LOOP;

    FOR guitars_row IN
        SELECT DISTINCT guitars_policy.polname AS name,
               guitars_policy.polrelid::regclass AS fires_on
        FROM pg_depend AS guitars_dep
        JOIN pg_policy AS guitars_policy
            ON guitars_policy.oid = guitars_dep.objid
           AND guitars_dep.classid = 'pg_policy'::regclass
        WHERE guitars_dep.refclassid = 'pg_class'::regclass
          AND guitars_dep.refobjid = guitars_target
          AND (guitars_column IS NULL OR guitars_dep.refobjsubid = guitars_column)
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I ON %s', guitars_row.name, guitars_row.fires_on);
    END LOOP;

    IF guitars_column IS NULL THEN
        FOR guitars_row IN
            SELECT guitars_trigger.tgname AS name
            FROM pg_trigger AS guitars_trigger
            WHERE guitars_trigger.tgrelid = guitars_target
              AND NOT guitars_trigger.tgisinternal
        LOOP
            EXECUTE format('DROP TRIGGER IF EXISTS %I ON %s', guitars_row.name, guitars_target);
        END LOOP;

        FOR guitars_row IN
            SELECT guitars_policy.polname AS name
            FROM pg_policy AS guitars_policy
            WHERE guitars_policy.polrelid = guitars_target
        LOOP
            EXECUTE format('DROP POLICY IF EXISTS %I ON %s', guitars_row.name, guitars_target);
        END LOOP;
    END IF;
END;
${tag}$;
"""

#: Resolves the column to its ``attnum``, which is what ``pg_depend.refobjsubid`` holds.
#: ``attisdropped`` excluded: a dropped column keeps its ``attnum``, and matching one would
#: retire objects for a column nobody asked about.
_COLUMN_ATTNUM = (
    '(SELECT guitars_attr.attnum FROM pg_attribute AS guitars_attr '
    'WHERE guitars_attr.attrelid = guitars_target AND guitars_attr.attname = {column} '
    'AND NOT guitars_attr.attisdropped)'
)

#: The dollar-quoting tag. An identifier may contain ``$``, so a table or column spelling that
#: closes this tag is refused at construction rather than escaped -- the same call the enforcement
#: generator makes about ``$$`` in a ``db_table``.
_TAG = 'guitars_retire'


class RetireEnforcement(Operation):
    """Drop the enforcement objects depending on *table*, or on one *column* of it. Nothing in
    this kit retires an object on its own, so removing a column a rule names makes
    ``RemoveField`` fail with ``rule ... depends on column``. See ``docs/migrations.md``."""

    # Place it **before** the schema operation that would otherwise fail:
    #     RetireEnforcement('shop_order', column='archived_at'),
    #     migrations.RemoveField('order', 'archived_at'),

    #: No state to change: this operation only ever touches the database.
    reduces_to_sql = True
    #: Refused **before** anything runs, which is why this is the flag and not a raise in
    #: ``database_backwards`` alone: a silent no-op reverse would leave history claiming objects
    #: the database lacks -- on the whole-table path, the table's own ``soft_delete`` rule.
    reversible = False
    #: The ``DO`` block is several statements; a backend without DDL transactions must not be
    #: left half-retired.
    atomic = True

    def __init__(self, table: str, column: str | None = None) -> None:
        self.table = table
        self.column = column
        for kind, value in (('table', table), ('column', column)):
            if value is not None and f'${_TAG}$' in value:
                raise ValueError(
                    f'guitars.operations.RetireEnforcement: the {kind} {value!r} closes the '
                    f'dollar quoting this operation is written with. Rename it.'
                )

    def state_forwards(self, app_label, state):  # noqa: ARG002 - required by the base class
        """Nothing. The models this operation clears the way for are changed by the operations
        beside it; this one is a database-only step."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state) -> None:  # noqa: ARG002
        column = (
            'NULL::smallint'
            if self.column is None
            else _COLUMN_ATTNUM.format(column=_identifiers._quote_literal(self.column))
        )
        # ``params=None``, as ``RunSQL`` passes for the kit's other bodies: this SQL is a
        # plpgsql ``DO`` block whose ``format('%I ...')`` calls would otherwise be read by
        # psycopg as its own placeholders and rejected.
        schema_editor.execute(
            _RETIRE_SQL.format(
                tag=_TAG,
                table=_identifiers._quote_literal(self.table),
                column=column,
            ),
            params=None,
        )

    def database_backwards(self, app_label, schema_editor, from_state, to_state):  # noqa: ARG002
        """Unreachable through ``migrate``, which reads :attr:`reversible` first. Kept for the
        caller that reaches an operation directly, and to name the recovery path once."""
        raise IrreversibleError(
            f'guitars.operations.RetireEnforcement({self.table!r}) cannot be reversed: it '
            f'dropped objects only makeguitarmigrations knows how to write. Reverse the schema '
            f'change by hand, then run `makeguitarmigrations --adopt` to re-assert them.'
        )

    def describe(self) -> str:
        target = self.table if self.column is None else f'{self.table}.{self.column}'
        return f'Retire the guitars enforcement objects depending on {target}'

    @property
    def migration_name_fragment(self) -> str:
        target = self.table if self.column is None else f'{self.table}_{self.column}'
        return f'retire_enforcement_{target}'

    def __repr__(self) -> str:
        column = '' if self.column is None else f', column={self.column!r}'
        return f'<RetireEnforcement {self.table!r}{column}>'
