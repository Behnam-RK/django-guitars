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

# ---- Private, non-frozen owned-rule templates: the cascade pair above with the predicate
# sides swapped, the FK living on the owner. The NOT EXISTS is the last-owner guard, always
# emitted -- see ADR 0011 for why it is never derived from a unique constraint. ----

# The declaring column's arm is spelled out rather than composed, so a single-owner dependent
# renders byte-identically to 2.3.0 and its ``[SQL:...]`` identity does not move on upgrade.
# Every other owning column adds an arm via ``{co_owner_guards}``, ``''`` here. See ADR 0012.

_CREATE_SOFT_DELETE_OWNED_OBJECT_RULE = """
    CREATE OR REPLACE RULE {rule_name}
        AS ON UPDATE TO {table}
        WHERE old._deleted_at IS NULL AND new._deleted_at IS NOT NULL AND
              COALESCE(current_setting('rules.hard_deletion', true), '') <> 'on'
        DO ALSO (
            UPDATE {dependent_table}
            SET _deleted_at = NOW()
            WHERE "{dependent_primary_key}" = old."{foreign_key}"
              AND _deleted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM {table} AS guitars_owner
                  WHERE guitars_owner."{foreign_key}" = old."{foreign_key}"
                    AND guitars_owner."{primary_key}" <> old."{primary_key}"
                    AND guitars_owner._deleted_at IS NULL
              ){co_owner_guards}
        );
"""

# One co-owner arm, correlated against the *declaring* rule's column: the target row is named
# by the rule's own key, not the co-owner's. Leads with a newline and ends without one, so
# joining zero of them leaves the template above byte-for-byte untouched.
_SOFT_DELETE_OWNED_CO_OWNER_GUARD = """
              AND NOT EXISTS (
                  SELECT 1 FROM {owner_table} AS {alias}
                  WHERE {alias}."{foreign_key}" = old."{declared_foreign_key}"
{self_exclusion}                    AND {alias}._deleted_at IS NULL
              )"""

# The same arm for an owner that keeps ``_deleted_at`` on an MTI ancestor: the foreign key is on
# its own table and liveness on the ancestor's, so the arm joins the two on the primary-key value
# every table in the chain shares. Read from the ancestor, which is where the column is.
_SOFT_DELETE_OWNED_CO_OWNER_JOINED_GUARD = """
              AND NOT EXISTS (
                  SELECT 1 FROM {owner_table} AS {alias}
                  JOIN {root_table} AS {alias}_root
                      ON {alias}_root."{root_primary_key}" = {alias}."{child_primary_key}"
                  WHERE {alias}."{foreign_key}" = old."{declared_foreign_key}"
{self_exclusion}                    AND {alias}_root._deleted_at IS NULL
              )"""

# Spliced in only where the co-owner sits on the table the rule fires on. Keyed on the *row*:
# a row owning the target through two of its own columns would otherwise read as its own last
# live owner and hold the target alive forever.
_SOFT_DELETE_OWNED_CO_OWNER_SELF_EXCLUSION = (
    '                    AND {alias}."{primary_key}" <> old."{primary_key}"\n'
)

# The other exclusion, on an arm reading the *dependent's* own table -- an ``OwningForeignKey``
# a target declares to itself. A row of it pointing at its own primary key would read as its own
# live owner and pin the target un-archivable. Excluded by the key, which names the target row.
_SOFT_DELETE_OWNED_CO_OWNER_TARGET_EXCLUSION = (
    '                    AND {alias}."{primary_key}" <> old."{foreign_key}"\n'
)

_DROP_SOFT_DELETE_OWNED_OBJECT_RULE = """
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
