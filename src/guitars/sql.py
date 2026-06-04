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
