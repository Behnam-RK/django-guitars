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

Two placeholder kinds, escaped differently by the caller before ``.format()``:
``"{table}"``/``"{child_table}"`` are **identifier** positions (``guitars.sql._identifiers
._escape_ident``); ``'{primary_key}'``/``'{parent_table}'``/``'{parent_pk}'``/
``'{child_pk}'`` inside the ``set_updated_at(...)``/``set_parent_updated_at(...)`` calls are
**string-literal** arguments to those functions (``_escape_literal``) -- the function itself
re-quotes them as identifiers at trigger-fire time via ``%I``, so they must not be
double-quoted here too.
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

# ``OR REPLACE`` rather than DROP + CREATE, and that is forced rather than defensive:
# ``DROP FUNCTION`` refuses while any trigger depends on the function, and ``CASCADE``
# would take those triggers with it -- leaving every table without its ``_updated_at``
# trigger until the next statement. Replacing the body in place keeps them attached.
#
# The plain ``CREATE`` above stays the *first* thing written for a database, so a genuine
# collision on this unqualified public-schema name still fails ``migrate`` loudly instead
# of guitars silently clobbering someone else's ``set_updated_at()``. This form is emitted
# only where the generator knows the function is already ours -- a refresh or ``--adopt``.
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

CHECK_TRIGGER_EXISTS_ON_TABLE = """
    SELECT tgname
    FROM pg_trigger
    WHERE tgname = '{trigger}' AND
          tgrelid = '{table}'::regclass AND
          tgisinternal IS FALSE;
"""

CREATE_UPDATED_AT_TRIGGER = """
    CREATE TRIGGER updated_at_trigger
        AFTER UPDATE ON "{table}" REFERENCING NEW TABLE AS new_table
        FOR EACH STATEMENT
        WHEN (pg_trigger_depth() = 0)
        EXECUTE FUNCTION set_updated_at('{primary_key}');
"""

DROP_UPDATED_AT_TRIGGER = """
    DROP TRIGGER updated_at_trigger ON "{table}";
"""

# PostgreSQL has no ``CREATE OR REPLACE TRIGGER`` before 14, and this package declares no
# minimum, so a trigger is refreshed by dropping and re-creating. There is no window: the
# ``DROP`` takes ACCESS EXCLUSIVE and holds it until commit, so no ``UPDATE`` can slip
# between the two statements and escape the trigger.
#
# Two forms, because the ``IF EXISTS`` is a claim about knowledge, not a safety net:
#
# * REPLACE_ is emitted where the recorded migration history says this trigger is ours.
#   A bare ``DROP`` there fails loudly if the database has diverged from its history --
#   which is exactly the signal you want, not one to swallow.
# * ADOPT_ is emitted only under ``makeguitarmigrations --adopt``, where the whole premise
#   is that the operator does not know what the database already has. There the
#   uncertainty is real and was opted into explicitly, so ``IF EXISTS`` states it honestly.
REPLACE_UPDATED_AT_TRIGGER = (
    """
    DROP TRIGGER updated_at_trigger ON "{table}";
"""
    + CREATE_UPDATED_AT_TRIGGER
)

ADOPT_UPDATED_AT_TRIGGER = (
    """
    DROP TRIGGER IF EXISTS updated_at_trigger ON "{table}";
"""
    + CREATE_UPDATED_AT_TRIGGER
)

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

# Same reasoning as REPLACE_UPDATED_AT_TRIGGER_FUNCTION above.
REPLACE_PARENT_UPDATED_AT_TRIGGER_FUNCTION = """
    CREATE OR REPLACE FUNCTION set_parent_updated_at()
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
