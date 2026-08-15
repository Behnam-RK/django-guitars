"""Raw SQL for the DB-managed ``_updated_at`` column: a shared trigger *function* and a
per-table statement *trigger* calling it. Public ``CREATE_PARENT_UPDATED_AT_*`` keep their
frozen pre-2.0.0 3-arg call; the private ``_CREATE_PARENT_...`` forms below are current."""

# *****************************************************************************************
# ****************************** Updated At Trigger Function ******************************
# *****************************************************************************************

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

# OR REPLACE, forced not defensive: DROP FUNCTION refuses while a trigger depends on it,
# and CASCADE would take those triggers with it. Emitted only where the generator knows
# the function is already ours; the plain CREATE above still fails loudly on collision.
REPLACE_UPDATED_AT_TRIGGER_FUNCTION = """
    CREATE OR REPLACE FUNCTION set_updated_at()
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

# ********************************************************************************
# ****************************** Updated At Trigger ******************************
# ********************************************************************************

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

# No CREATE OR REPLACE TRIGGER before PG 14, so refreshed via drop-then-create (ACCESS
# EXCLUSIVE, no window). IF EXISTS is a knowledge claim: REPLACE_ fails loudly on
# divergence; ADOPT_ (--adopt only) is honest about not knowing what's there.
REPLACE_UPDATED_AT_TRIGGER = (
    """
    DROP TRIGGER updated_at_trigger ON {table};
"""
    + CREATE_UPDATED_AT_TRIGGER
)

ADOPT_UPDATED_AT_TRIGGER = (
    """
    DROP TRIGGER IF EXISTS updated_at_trigger ON {table};
"""
    + CREATE_UPDATED_AT_TRIGGER
)

# ---- Parent updated-at trigger function: updates a DIFFERENT table than TG_TABLE_NAME.
# Branches on TG_NARGS (3 vs 4) since a trigger's arg list is frozen at CREATE time, so a
# pre-2.0.0 trigger keeps calling the 3-arg form until recreated. ----

CREATE_PARENT_UPDATED_AT_TRIGGER_FUNCTION = """
    CREATE FUNCTION set_parent_updated_at()
       RETURNS TRIGGER
       LANGUAGE PLPGSQL
    AS
    $$
    BEGIN
        IF TG_NARGS = 3 THEN
            EXECUTE format(
                'UPDATE %I SET _updated_at = NOW() WHERE %I IN (SELECT %I FROM new_table);',
                TG_ARGV[0], TG_ARGV[1], TG_ARGV[2]
            );
        ELSIF TG_ARGV[0] = '' THEN
            EXECUTE format(
                'UPDATE %I SET _updated_at = NOW() WHERE %I IN (SELECT %I FROM new_table);',
                TG_ARGV[1], TG_ARGV[2], TG_ARGV[3]
            );
        ELSE
            EXECUTE format(
                'UPDATE %I.%I SET _updated_at = NOW() WHERE %I IN (SELECT %I FROM new_table);',
                TG_ARGV[0], TG_ARGV[1], TG_ARGV[2], TG_ARGV[3]
            );
        END IF;
        RETURN NULL;
    END;
    $$
"""

DROP_PARENT_UPDATED_AT_TRIGGER_FUNCTION = """
    DROP FUNCTION set_parent_updated_at();
"""

# Same reasoning as REPLACE_UPDATED_AT_TRIGGER_FUNCTION above.
REPLACE_PARENT_UPDATED_AT_TRIGGER_FUNCTION = """
    CREATE OR REPLACE FUNCTION set_parent_updated_at()
       RETURNS TRIGGER
       LANGUAGE PLPGSQL
    AS
    $$
    BEGIN
        IF TG_NARGS = 3 THEN
            EXECUTE format(
                'UPDATE %I SET _updated_at = NOW() WHERE %I IN (SELECT %I FROM new_table);',
                TG_ARGV[0], TG_ARGV[1], TG_ARGV[2]
            );
        ELSIF TG_ARGV[0] = '' THEN
            EXECUTE format(
                'UPDATE %I SET _updated_at = NOW() WHERE %I IN (SELECT %I FROM new_table);',
                TG_ARGV[1], TG_ARGV[2], TG_ARGV[3]
            );
        ELSE
            EXECUTE format(
                'UPDATE %I.%I SET _updated_at = NOW() WHERE %I IN (SELECT %I FROM new_table);',
                TG_ARGV[0], TG_ARGV[1], TG_ARGV[2], TG_ARGV[3]
            );
        END IF;
        RETURN NULL;
    END;
    $$
"""

# ---- Parent updated-at trigger (on the child table, bumps the owner's _updated_at) ----

CREATE_PARENT_UPDATED_AT_TRIGGER = """
    CREATE TRIGGER updated_at_trigger
        AFTER UPDATE ON "{child_table}" REFERENCING NEW TABLE AS new_table
        FOR EACH STATEMENT
        WHEN (pg_trigger_depth() = 0)
        EXECUTE FUNCTION set_parent_updated_at('{parent_table}', '{parent_pk}', '{child_pk}');
"""

DROP_PARENT_UPDATED_AT_TRIGGER = """
    DROP TRIGGER updated_at_trigger ON "{child_table}";
"""

# Same two-form split as REPLACE_/ADOPT_UPDATED_AT_TRIGGER above.
REPLACE_PARENT_UPDATED_AT_TRIGGER = (
    """
    DROP TRIGGER updated_at_trigger ON "{child_table}";
"""
    + CREATE_PARENT_UPDATED_AT_TRIGGER
)

ADOPT_PARENT_UPDATED_AT_TRIGGER = (
    """
    DROP TRIGGER IF EXISTS updated_at_trigger ON "{child_table}";
"""
    + CREATE_PARENT_UPDATED_AT_TRIGGER
)

# ---- Private, schema-aware MTI parent trigger templates: the public forms above keep the
# frozen pre-2.0.0 3-arg convention; a 4th required kwarg would KeyError old migrations
# (see soft_delete.py's rule_name). Not exported -- not part of the frozen interface. ----

_CREATE_PARENT_UPDATED_AT_TRIGGER = """
    CREATE TRIGGER updated_at_trigger
        AFTER UPDATE ON {child_table} REFERENCING NEW TABLE AS new_table
        FOR EACH STATEMENT
        WHEN (pg_trigger_depth() = 0)
        EXECUTE FUNCTION set_parent_updated_at(
            '{parent_schema}', '{parent_table}', '{parent_pk}', '{child_pk}'
        );
"""

_DROP_PARENT_UPDATED_AT_TRIGGER = """
    DROP TRIGGER updated_at_trigger ON {child_table};
"""

_REPLACE_PARENT_UPDATED_AT_TRIGGER = (
    """
    DROP TRIGGER updated_at_trigger ON {child_table};
"""
    + _CREATE_PARENT_UPDATED_AT_TRIGGER
)

_ADOPT_PARENT_UPDATED_AT_TRIGGER = (
    """
    DROP TRIGGER IF EXISTS updated_at_trigger ON {child_table};
"""
    + _CREATE_PARENT_UPDATED_AT_TRIGGER
)
