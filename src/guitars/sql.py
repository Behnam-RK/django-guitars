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

# *********************************************************************************
# ****************************** Soft Deletion Rules ******************************
# *********************************************************************************

SWITCH_ON_HARD_DELETION = "SELECT set_config('rules.hard_deletion', 'on', TRUE);"

SWITCH_OFF_HARD_DELETION = "SELECT set_config('rules.hard_deletion', 'off', TRUE);"

CHECK_RULE_EXISTS_ON_TABLE = """
    SELECT rulename
    FROM pg_rules
    WHERE rulename = '{rule}' AND
          tablename = '{table}';
"""

CREATE_SOFT_DELETE_RULE = """
    CREATE RULE soft_delete
        AS ON DELETE TO {table}
        WHERE COALESCE(current_setting('rules.hard_deletion', true), 'off') = 'off'
        DO INSTEAD (
            UPDATE {table}
            SET _deleted_at = NOW()
            WHERE {primary_key} = old.{primary_key} AND _deleted_at IS NULL
        );
"""

DROP_SOFT_DELETE_RULE = """
    DROP RULE soft_delete ON {table};
"""

CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE = """
    CREATE RULE soft_delete_related_{related_table}
        AS ON UPDATE TO {table}
        WHERE old._deleted_at IS NULL AND new._deleted_at IS NOT NULL AND
              COALESCE(current_setting('rules.hard_deletion', true), 'off') = 'off'
        DO ALSO (
            UPDATE {related_table}
            SET _deleted_at = NOW()
            WHERE {foreign_key} = old.{primary_key}
        );
"""

DROP_SOFT_DELETE_RELATED_OBJECTS_RULE = """
    DROP RULE soft_delete_related_{related_table} ON {table};
"""

# ***********************************************************************************************
# ****************************** Multi-Table Inheritance (MTI) ***********************************
# ***********************************************************************************************
#
# In Django MTI a concrete child model gets its OWN table whose primary key is a
# ``OneToOneField(parent_link=True)`` referencing the parent's table; the metadata columns
# (``_updated_at`` / ``_deleted_at``) live ONLY on the ancestor that declares them. Because every
# table in an MTI chain shares the SAME primary-key value, a rule/trigger on any descendant table
# can address the owning ancestor row directly via ``owner_pk = old.<child_pk>``.

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

# ---- MTI soft-delete rule (on the child table, soft-deletes the owner, preserves child row) ----
# ``DO INSTEAD`` suppresses the physical delete of the child row and marks the owning ancestor
# instead. The ``_deleted_at IS NULL`` guard makes it idempotent across the per-table DELETEs
# Django issues for an MTI chain, so the owner's cascade rules fire exactly once.

CREATE_MTI_SOFT_DELETE_RULE = """
    CREATE RULE soft_delete
        AS ON DELETE TO {child_table}
        WHERE COALESCE(current_setting('rules.hard_deletion', true), 'off') = 'off'
        DO INSTEAD (
            UPDATE {parent_table}
            SET _deleted_at = NOW()
            WHERE {parent_pk} = old.{child_pk} AND _deleted_at IS NULL
        );
"""

DROP_MTI_SOFT_DELETE_RULE = """
    DROP RULE soft_delete ON {child_table};
"""
