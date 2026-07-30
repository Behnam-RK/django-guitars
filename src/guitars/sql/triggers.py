"""Raw SQL for the DB-managed ``_updated_at`` column.

Two pieces per table: a shared trigger *function* (one per database, a singleton
migration) and a per-table statement *trigger* that calls it. Statement-level
rather than row-level so a single ``UPDATE`` touching a thousand rows bumps them
all in one pass, and ``WHEN (pg_trigger_depth() = 0)`` so the function's own
``UPDATE`` cannot re-enter the trigger.

The MTI pair at the bottom exists because the metadata columns live only on the
ancestor that declares them, while a child-only ``QuerySet.update()`` writes only
the child table -- see the package docstring for the shared-PK invariant that lets
a trigger on the child address the owning ancestor row directly.
"""

# *****************************************************************************************
# ****************************** Updated At Trigger Function ******************************
# *****************************************************************************************

CHECK_TRIGGER_FUNCTION_EXISTS = """
    SELECT proname
    FROM pg_proc
    WHERE proname = 'set_updated_at';
"""

CREATE_UPDATED_AT_TRIGGER_FUNCTION = """
    CREATE FUNCTION set_updated_at()
       RETURNS TRIGGER
       LANGUAGE PLPGSQL
    AS
    $$
    BEGIN
        EXECUTE format(
            'UPDATE %I SET _updated_at = NOW() WHERE %I IN (SELECT %I FROM new_table);',
            TG_TABLE_NAME, TG_ARGV[0], TG_ARGV[0]
        );
        RETURN NULL;
    END;
    $$
"""

DROP_UPDATED_AT_TRIGGER_FUNCTION = """
    DROP FUNCTION set_updated_at();
"""

# ********************************************************************************
# ****************************** Updated At Trigger ******************************
# ********************************************************************************

CHECK_TRIGGER_EXISTS_ON_TABLE = """
    SELECT tgname
    FROM pg_trigger
    WHERE tgname = '{trigger}' AND
          tgrelid = '{table}'::regclass AND
          tgisinternal IS FALSE;
"""

CREATE_UPDATED_AT_TRIGGER = """
    CREATE TRIGGER updated_at_trigger
        AFTER UPDATE ON {table} REFERENCING NEW TABLE AS new_table
        FOR EACH STATEMENT
        WHEN (pg_trigger_depth() = 0)
        EXECUTE FUNCTION set_updated_at('{primary_key}');
"""

DROP_UPDATED_AT_TRIGGER = """
    DROP TRIGGER updated_at_trigger ON {table};
"""

# ---- Parent updated-at trigger function (singleton, sibling of set_updated_at) ----
# Unlike ``set_updated_at`` (which updates ``TG_TABLE_NAME``), this updates a DIFFERENT table --
# the ancestor that actually owns ``_updated_at`` -- so a write to a child-only table still bumps
# the parent's timestamp. Args: parent table, parent pk column, child pk column (in new_table).

CHECK_PARENT_TRIGGER_FUNCTION_EXISTS = """
    SELECT proname
    FROM pg_proc
    WHERE proname = 'set_parent_updated_at';
"""

CREATE_PARENT_UPDATED_AT_TRIGGER_FUNCTION = """
    CREATE FUNCTION set_parent_updated_at()
       RETURNS TRIGGER
       LANGUAGE PLPGSQL
    AS
    $$
    BEGIN
        EXECUTE format(
            'UPDATE %I SET _updated_at = NOW() WHERE %I IN (SELECT %I FROM new_table);',
            TG_ARGV[0], TG_ARGV[1], TG_ARGV[2]
        );
        RETURN NULL;
    END;
    $$
"""

DROP_PARENT_UPDATED_AT_TRIGGER_FUNCTION = """
    DROP FUNCTION set_parent_updated_at();
"""

# ---- Parent updated-at trigger (on the child table, bumps the owner's _updated_at) ----

CREATE_PARENT_UPDATED_AT_TRIGGER = """
    CREATE TRIGGER updated_at_trigger
        AFTER UPDATE ON {child_table} REFERENCING NEW TABLE AS new_table
        FOR EACH STATEMENT
        WHEN (pg_trigger_depth() = 0)
        EXECUTE FUNCTION set_parent_updated_at('{parent_table}', '{parent_pk}', '{child_pk}');
"""

DROP_PARENT_UPDATED_AT_TRIGGER = """
    DROP TRIGGER updated_at_trigger ON {child_table};
"""
