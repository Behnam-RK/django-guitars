"""Raw SQL for PostgreSQL-enforced soft deletion. ``.delete()`` never reaches Python: a
rule rewrites it to an ``UPDATE`` stamping ``_deleted_at``, holding for cascades and raw
SQL too. **Guards are ``<> 'on'``, never ``= 'off'``** -- see CLAUDE.md's checklist."""

# *********************************************************************************
# ****************************** Soft Deletion Rules ******************************
# *********************************************************************************

SWITCH_ON_HARD_DELETION = "SELECT set_config('rules.hard_deletion', 'on', TRUE);"

SWITCH_OFF_HARD_DELETION = "SELECT set_config('rules.hard_deletion', 'off', TRUE);"

CREATE_SOFT_DELETE_RULE = """
    CREATE OR REPLACE RULE soft_delete
        AS ON DELETE TO {table}
        WHERE COALESCE(current_setting('rules.hard_deletion', true), '') <> 'on'
        DO INSTEAD (
            UPDATE {table}
            SET _deleted_at = NOW()
            WHERE "{primary_key}" = old."{primary_key}" AND _deleted_at IS NULL
        );
"""

DROP_SOFT_DELETE_RULE = """
    DROP RULE soft_delete ON {table};
"""

CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE = """
    CREATE OR REPLACE RULE soft_delete_related_{related_table}
        AS ON UPDATE TO "{table}"
        WHERE old._deleted_at IS NULL AND new._deleted_at IS NOT NULL AND
              COALESCE(current_setting('rules.hard_deletion', true), '') <> 'on'
        DO ALSO (
            UPDATE "{related_table}"
            SET _deleted_at = NOW()
            WHERE "{foreign_key}" = old."{primary_key}"
        );
"""

DROP_SOFT_DELETE_RELATED_OBJECTS_RULE = """
    DROP RULE soft_delete_related_{related_table} ON "{table}";
"""

# A second CASCADE FK needs a rule name distinct from the first's -- Postgres namespaces a
# rule by name alone, so reusing it would silently replace, not add, a cascade.

CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE_VIA = """
    CREATE OR REPLACE RULE soft_delete_related_{related_table}_{foreign_key}
        AS ON UPDATE TO "{table}"
        WHERE old._deleted_at IS NULL AND new._deleted_at IS NOT NULL AND
              COALESCE(current_setting('rules.hard_deletion', true), '') <> 'on'
        DO ALSO (
            UPDATE "{related_table}"
            SET _deleted_at = NOW()
            WHERE "{foreign_key}" = old."{primary_key}"
        );
"""

DROP_SOFT_DELETE_RELATED_OBJECTS_RULE_VIA = """
    DROP RULE soft_delete_related_{related_table}_{foreign_key} ON "{table}";
"""

# ---- Private, non-frozen cascade-rule templates: take an externally-computed
# NAMEDATALEN-safe ``rule_name`` (operations.py's ``_related_rule_name``), serving both
# plain and VIA cases since only the name differed. Not exported. ----

_CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE = """
    CREATE OR REPLACE RULE {rule_name}
        AS ON UPDATE TO {table}
        WHERE old._deleted_at IS NULL AND new._deleted_at IS NOT NULL AND
              COALESCE(current_setting('rules.hard_deletion', true), '') <> 'on'
        DO ALSO (
            UPDATE {related_table}
            SET _deleted_at = NOW()
            WHERE "{foreign_key}" = old."{primary_key}"
        );
"""

_DROP_SOFT_DELETE_RELATED_OBJECTS_RULE = """
    DROP RULE {rule_name} ON {table};
"""

# ---- MTI soft-delete rule: preserves the child row, marks the owning ancestor instead.
# ``_deleted_at IS NULL`` makes it idempotent across the per-table DELETEs Django issues
# for an MTI chain, so the owner's cascade rules fire exactly once. ----

CREATE_MTI_SOFT_DELETE_RULE = """
    CREATE OR REPLACE RULE soft_delete
        AS ON DELETE TO {child_table}
        WHERE COALESCE(current_setting('rules.hard_deletion', true), '') <> 'on'
        DO INSTEAD (
            UPDATE {parent_table}
            SET _deleted_at = NOW()
            WHERE "{parent_pk}" = old."{child_pk}" AND _deleted_at IS NULL
        );
"""

DROP_MTI_SOFT_DELETE_RULE = """
    DROP RULE soft_delete ON {child_table};
"""
