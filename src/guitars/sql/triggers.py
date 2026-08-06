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

Table-DDL positions (the ``ON`` target of ``CREATE``/``DROP TRIGGER``) come in two shapes,
because unlike a column, a table name may be schema-qualified:

* ``{table}`` (own-table trigger) owns no quote characters of its own -- the caller supplies
  an already-quoted, already-qualified string via ``guitars.sql._identifiers._quote_table``,
  which may render as ``"table"`` or ``"schema"."table"``. Changing an existing public
  constant's *rendered text* this way is safe (no ``.format()`` kwarg changed), unlike adding
  a placeholder would be -- see the package docstring on what "frozen" actually covers.
* ``"{child_table}"`` (the public, pre-2.0.0 ``CREATE_PARENT_UPDATED_AT_TRIGGER`` and its
  ``REPLACE_``/``ADOPT_`` forms) keeps its template-owned quotes and its 3-arg
  ``set_parent_updated_at(parent_table, parent_pk, child_pk)`` call untouched, because a
  schema argument is a **new required kwarg** with no equivalent in migrations generated
  before this feature existed -- adding one would ``KeyError`` the same way an unplanned
  ``rule_name`` kwarg did for the cascade-rule constants (see ``soft_delete.py``). The current
  generator instead uses the private ``_CREATE_PARENT_UPDATED_AT_TRIGGER``/``_REPLACE_``/
  ``_ADOPT_``/``_DROP_`` forms below, which take a caller-quoted ``{child_table}`` and a
  4-arg, schema-aware ``set_parent_updated_at`` call.

``'{primary_key}'`` / ``'{parent_pk}'`` / ``'{child_pk}'`` inside the ``set_updated_at(...)``/
``set_parent_updated_at(...)`` calls are **string-literal** arguments to those functions
(``_escape_literal``) -- columns are never schema-qualified, and the function itself
re-quotes them as identifiers at trigger-fire time via ``%I``, so they must not be
double-quoted here too. ``'{parent_schema}'``/``'{parent_table}'`` (private form only) are
likewise string-literal arguments, split for the same reason ``_quote_table`` splits a table
DDL position: a single ``%I`` cannot render ``"schema"."table"`` as two quoted parts, only as
one (wrong) identifier. ``parent_schema`` is the empty string for an unqualified parent
table -- see ``set_parent_updated_at``'s body for how it branches on this, and on ``TG_NARGS``
for the pre-2.0.0 3-arg form it must go on understanding forever.
"""

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

# ---- Parent updated-at trigger function (singleton, sibling of set_updated_at) ----
# Unlike ``set_updated_at`` (which updates ``TG_TABLE_NAME``), this updates a DIFFERENT table --
# the ancestor that actually owns ``_updated_at`` -- so a write to a child-only table still bumps
# the parent's timestamp.
#
# Two calling conventions, both handled, because a *trigger's* argument list is frozen into
# ``pg_trigger`` at ``CREATE TRIGGER`` time and does not change when this function's body is
# later replaced: an MTI child trigger created before 2.0.0 still calls
# ``set_parent_updated_at('<parent_table>', '<parent_pk>', '<child_pk>')`` -- three args, no
# schema -- until *that trigger* is itself dropped and recreated (which
# ``makeguitarmigrations`` does the next time it runs, once this function's changed body makes
# its digest stale). Branching on ``TG_NARGS`` rather than requiring every trigger to be
# upgraded in lockstep with this function means there is no window, however brief, in which an
# not-yet-upgraded trigger calls a function that no longer understands the args it was built
# with.
#
# The 4-arg form's schema arrives as its own arg rather than folded into the table name
# (``'schema.table'``) because a single ``%I`` quotes a string as *one* identifier --
# ``format('%I', 'schema.table')`` produces the single, wrong relation ``"schema.table"``, not
# a qualified reference to two. Two ``%I``\\ s joined by a literal ``.`` is what PostgreSQL
# itself does internally for a qualified name, so the branch below mirrors that rather than
# trying to make one placeholder do both jobs. An empty first arg is the unqualified case --
# the 4-arg form always passes exactly four args, never conditionally omits the schema, so
# ``TG_ARGV[0] = ''`` is unambiguous there.

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

# ---- Private, schema-aware MTI parent trigger templates for the current generator ----
# The public CREATE_PARENT_UPDATED_AT_TRIGGER/REPLACE_/ADOPT_ above keep the pre-2.0.0
# calling convention (three set_parent_updated_at args, no schema) untouched -- migrations
# generated before 1.1.0 call .format() on them directly with that exact kwarg set, and a
# fourth required kwarg would KeyError the same way an unplanned rule_name kwarg did for the
# cascade-rule constants (see soft_delete.py). set_parent_updated_at() itself stays a single
# shared function either way (see its CREATE/REPLACE bodies' TG_NARGS branch) so a trigger
# created by either form keeps working after the function is upgraded -- there is no window
# in which an old, still-3-arg trigger calls a function that no longer understands it.
#
# Not exported from sql/__init__.py -- not part of the frozen interface.

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
