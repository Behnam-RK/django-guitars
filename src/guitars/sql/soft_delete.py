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

# ``{owner_row}`` is the row the key is read off: the rule passes ``old`` and renders
# unchanged, the sweep its own alias, ``old`` being a plpgsql record a table alias may not
# shadow. One placeholder is what lets both splice these arms rather than render them twice.
_SOFT_DELETE_OWNED_CO_OWNER_GUARD = """
              AND NOT EXISTS (
                  SELECT 1 FROM {owner_table} AS {alias}
                  WHERE {alias}."{foreign_key}" = {owner_row}."{declared_foreign_key}"
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
                  WHERE {alias}."{foreign_key}" = {owner_row}."{declared_foreign_key}"
{self_exclusion}                    AND {alias}_root._deleted_at IS NULL
              )"""

# Spliced in only where the arm reads liveness *from* the table the rule fires on -- an MTI
# ancestor's, where it joins. Keyed on the row: one owning the target through two of its own
# columns would otherwise read as its own last live owner and hold the target alive forever.
_SOFT_DELETE_OWNED_CO_OWNER_SELF_EXCLUSION = (
    '                    AND {alias}."{primary_key}" <> {owner_row}."{primary_key}"\n'
)

# The other exclusion, on an arm taking liveness from the *dependent's* own table -- a target
# owning itself, or an MTI child of it owning it back. A row pointing at the target's primary key
# would read as its own live owner and pin it un-archivable. Excluded by the key, which names it.
_SOFT_DELETE_OWNED_CO_OWNER_TARGET_EXCLUSION = (
    '                    AND {alias}."{primary_key}" <> {owner_row}."{foreign_key}"\n'
)

_DROP_SOFT_DELETE_OWNED_OBJECT_RULE = """
    DROP RULE {rule_name} ON {table};
"""

# ---- Private, non-frozen owned *sweep* templates: the statement-level half of the rule
# above, which alone stamps nothing when one statement archives every owner -- for ever. This
# runs once the statement has settled, where liveness is truthful. See ADR 0014. ----

# Additive, never a replacement: a statement archiving owners never creates one, so the rule
# stamps a subset of this and whichever runs first the other's ``_deleted_at IS NULL`` makes
# a no-op. Nothing is retired, which is what ADR 0012 costed the trigger as needing.

# The subquery selects the **before** image, so the key read here is the one ``old`` gives the
# rule. Reading the after image made a statement that archives an owner *and* moves its key
# stamp the new target the rule never touched, and skip the old one it did.

# Terminated ``$$;`` unlike the autofill template it mirrors: that is an operation by itself,
# this is concatenated before its CREATE TRIGGER and an unterminated body swallows it. The
# indentation lands the spliced arms at the depth they are written with.
_CREATE_SOFT_DELETE_OWNED_SWEEP_FUNCTION = """
    CREATE OR REPLACE FUNCTION {function}()
       RETURNS TRIGGER
       LANGUAGE PLPGSQL
    AS
    $$
    BEGIN
        IF COALESCE(current_setting('rules.hard_deletion', true), '') <> 'on' THEN
            UPDATE {dependent_table} AS guitars_dependent
            SET _deleted_at = NOW(){updated_at_assignment}
            FROM (
                SELECT guitars_before.*
                FROM guitars_owned_before AS guitars_before
                JOIN guitars_owned_after AS guitars_after
                    ON guitars_after."{primary_key}" = guitars_before."{primary_key}"
                WHERE guitars_before._deleted_at IS NULL
                  AND guitars_after._deleted_at IS NOT NULL
                  AND guitars_before."{foreign_key}" IS NOT NULL
            ) AS guitars_archived
            WHERE guitars_dependent."{dependent_primary_key}" = guitars_archived."{foreign_key}"
              AND guitars_dependent._deleted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM {table} AS guitars_owner
                  WHERE guitars_owner."{foreign_key}" = guitars_archived."{foreign_key}"
                    AND guitars_owner."{primary_key}" <> guitars_archived."{primary_key}"
                    AND guitars_owner._deleted_at IS NULL
              ){co_owner_guards};
        END IF;
        RETURN NULL;
    END;
    $$;
"""

#: Filled where the dependent owns the column. The rule's UPDATE runs at trigger depth 0 so
#: ``updated_at_trigger`` fires; this runs at depth 1, where its ``WHEN`` suppresses it --
#: without this the column moves on one path and not the other, for one logical event.
_SOFT_DELETE_OWNED_SWEEP_UPDATED_AT = ', _updated_at = NOW()'

_DROP_SOFT_DELETE_OWNED_SWEEP_FUNCTION = """
    DROP FUNCTION {function}();
"""

# No ``WHEN (pg_trigger_depth() = 0)``, unlike the updated_at trigger: the UPDATE above
# archives dependents that own things themselves, whose sweeps fire at depth 1. Recursion ends
# on ``_deleted_at IS NULL``, and a cycle is refused a rule -- so a trigger -- before either.
_CREATE_SOFT_DELETE_OWNED_SWEEP_TRIGGER = """
    CREATE TRIGGER {trigger}
        AFTER UPDATE ON {table}
        REFERENCING OLD TABLE AS guitars_owned_before NEW TABLE AS guitars_owned_after
        FOR EACH STATEMENT
        EXECUTE FUNCTION {function}();
"""

_DROP_SOFT_DELETE_OWNED_SWEEP_TRIGGER = """
    DROP TRIGGER {trigger} ON {table};
"""

# Same two-form split as triggers.py's REPLACE_/ADOPT_ pairs: IF EXISTS is a knowledge claim.
# The function is CREATE OR REPLACE either way -- DROP FUNCTION refuses while a trigger
# depends on it, and CASCADE would take that trigger with it.
_CREATE_SOFT_DELETE_OWNED_SWEEP = (
    _CREATE_SOFT_DELETE_OWNED_SWEEP_FUNCTION + _CREATE_SOFT_DELETE_OWNED_SWEEP_TRIGGER
)

_DROP_SOFT_DELETE_OWNED_SWEEP = (
    _DROP_SOFT_DELETE_OWNED_SWEEP_TRIGGER + _DROP_SOFT_DELETE_OWNED_SWEEP_FUNCTION
)

_REPLACE_SOFT_DELETE_OWNED_SWEEP = (
    _DROP_SOFT_DELETE_OWNED_SWEEP_TRIGGER + _CREATE_SOFT_DELETE_OWNED_SWEEP
)

_ADOPT_SOFT_DELETE_OWNED_SWEEP = (
    """
    DROP TRIGGER IF EXISTS {trigger} ON {table};
"""
    + _CREATE_SOFT_DELETE_OWNED_SWEEP
)

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
