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
from guitars.sql import policy as _policy


__all__ = ['RetireEnforcement']


# Read by ``pg_depend`` rather than by matching text in ``pg_rules.definition``: that view
# prints identifiers unquoted and lower-cased, so a quoted match finds nothing and an unquoted
# one cannot tell ``shop`` from ``shopping``. ``pg_depend`` is where the error itself comes from.
_RETIRE_SQL = """
DO ${tag}$
DECLARE
    guitars_target regclass := to_regclass({table});
    guitars_column smallint;
    guitars_row record;
    guitars_dropped_ours boolean := false;
BEGIN
    IF guitars_target IS NULL THEN
        RAISE EXCEPTION
            'guitars: RetireEnforcement names no table %. Nothing was dropped.', {table}
            USING ERRCODE = 'undefined_table';
    END IF;

    IF {scoped_to_column} THEN
        SELECT guitars_attr.attnum INTO guitars_column
        FROM pg_attribute AS guitars_attr
        WHERE guitars_attr.attrelid = guitars_target
          AND guitars_attr.attname = {column}
          AND NOT guitars_attr.attisdropped;
        IF guitars_column IS NULL THEN
            RAISE EXCEPTION
                'guitars: RetireEnforcement names no column % on %. Place it *before* the '
                'operation that removes the column, not after. Nothing was dropped.',
                {column}, guitars_target
                USING ERRCODE = 'undefined_column';
        END IF;
    END IF;

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
          AND (NOT {scoped_to_column} OR guitars_dep.refobjsubid = guitars_column)
          AND guitars_rule.rulename LIKE 'soft\\_delete%'
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
          AND (NOT {scoped_to_column} OR guitars_dep.refobjsubid = guitars_column)
          AND guitars_policy.polname = '{policy}'
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I ON %s', guitars_row.name, guitars_row.fires_on);
        guitars_dropped_ours := true;
    END LOOP;

    -- Row-level security is a table *flag*, not an object ``pg_depend`` reaches: dropping the
    -- last ``tenant_scope`` off a FORCEd table would leave it returning no rows to anyone, the
    -- owner included, silently and irreversibly. Torn down in ``drop_table_rls``'s order --
    -- NO FORCE before DISABLE, so the table is never forced-but-disabled.

    -- Gated on having dropped *our* policy just now, not merely on none being left: a consumer
    -- who enabled row-level security themselves, with their own policies and none of ours, must
    -- keep it. Disabling that would be a silent security downgrade on an irreversible step.
    IF guitars_dropped_ours AND NOT EXISTS (
        SELECT 1 FROM pg_policy AS guitars_policy
        WHERE guitars_policy.polrelid = guitars_target
          AND guitars_policy.polname = '{policy}'
    ) THEN
        EXECUTE format('ALTER TABLE %s NO FORCE ROW LEVEL SECURITY', guitars_target);
        EXECUTE format('ALTER TABLE %s DISABLE ROW LEVEL SECURITY', guitars_target);
        FOR guitars_row IN
            SELECT guitars_policy.polname AS name
            FROM pg_policy AS guitars_policy
            WHERE guitars_policy.polrelid = guitars_target
              AND guitars_policy.polname LIKE 'rls\\_exempt\\_%'
        LOOP
            EXECUTE format('DROP POLICY IF EXISTS %I ON %s', guitars_row.name, guitars_target);
        END LOOP;
    END IF;

    IF NOT {scoped_to_column} THEN
        FOR guitars_row IN
            SELECT guitars_trigger.tgname AS name
            FROM pg_trigger AS guitars_trigger
            WHERE guitars_trigger.tgrelid = guitars_target
              AND NOT guitars_trigger.tgisinternal
              AND guitars_trigger.tgparentid = 0
              AND (
                  guitars_trigger.tgname = 'updated_at_trigger'
                  OR guitars_trigger.tgname LIKE 'soft\\_delete\\_self\\_cascade%'
                  OR guitars_trigger.tgname LIKE 'soft\\_delete\\_owned\\_sweep%'
                  OR guitars_trigger.tgname LIKE 'guitars\\_fill%'
              )
        LOOP
            EXECUTE format('DROP TRIGGER IF EXISTS %I ON %s', guitars_row.name, guitars_target);
        END LOOP;
    END IF;
END;
${tag}$;
"""

_TAG = 'guitars_retire'


class RetireEnforcement(Operation):
    """Drop **this kit's** enforcement objects depending on *table*, or on one *column* of it.
    Nothing here retires one on its own, so removing a column a rule names makes ``RemoveField``
    fail with ``rule ... depends on column``. See ``docs/migrations.md``."""

    # Matched by name as well as by dependency, so a consumer's own rule, trigger or policy is
    # never taken: this operation is irreversible, and what it drops it cannot put back.

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
        # ``to_regclass`` over the *quoted* spelling, not a bare literal: ``'MyTable'::regclass``
        # resolves ``mytable`` and a name with a space raises, while ``_quote_table`` produces
        # the form the rest of the kit writes -- and refuses a spelling it cannot.

        # ``params=None``, as ``RunSQL`` passes for the kit's other bodies: the ``format('%I')``
        # calls below would otherwise be read by psycopg as its own placeholders.
        schema_editor.execute(
            _RETIRE_SQL.format(
                tag=_TAG,
                table=_identifiers._quote_literal(_identifiers._quote_table(self.table)),
                column=_identifiers._quote_literal(self.column or ''),
                scoped_to_column='false' if self.column is None else 'true',
                policy=_policy.TENANT_POLICY,
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
