"""Raw SQL for PostgreSQL-enforced soft deletion.

``.delete()`` never reaches Python: an ``ON DELETE ... DO INSTEAD`` rule rewrites it
into an ``UPDATE`` that stamps ``_deleted_at``. Because the rewrite happens in the
database, it holds for ``queryset.delete()``, cascades and raw SQL alike -- none of
which call ``save()``.

``hard_delete()`` opts out by setting the ``rules.hard_deletion`` session variable,
which every rule below tests. It is set transaction-locally (the ``TRUE`` third
argument to ``set_config``), so it cannot leak past the block that set it.

**Every guard is written ``<> 'on'``, never ``= 'off'``**, and that is not a style
choice. A custom GUC that has never been set reads as NULL, but one that was set
transaction-locally and then *rolled back* reads as the **empty string** -- PostgreSQL
leaves a placeholder behind rather than removing it. Under ``= 'off'`` that empty
string matches neither branch, so the rule stops firing and ``DELETE`` becomes a real
delete: one rolled-back transaction containing a ``hard_delete()`` would silently
turn every later ``.delete()`` on that connection into permanent data loss, for as
long as the connection lived. ``<> 'on'`` inverts the default, so anything other than
an explicit opt-in preserves the row. The failure direction has to be "keep the
data".

**Every rule is created ``OR REPLACE``**, which is what makes replacing one safe.
PostgreSQL swaps the definition in place inside the transaction, so there is no
instant at which the table has no ``soft_delete`` rule -- and an instant without it
is an instant in which ``DELETE`` means what it says. The alternative, reversing the
enforcement migration and re-applying it, opens exactly that window between two
commands (and reverses every migration after it besides). A database carrying the
pre-1.0.0 ``= 'off'`` guard is upgraded by re-running these statements, nothing
more; see ``docs/soft-deletion.md``.

The MTI redirect rule at the bottom preserves the child row and stamps the *owner*
instead -- see the package docstring for the shared-PK invariant it relies on.

Two placeholder kinds. Column positions (``"{primary_key}"``, ``"{parent_pk}"``,
``"{child_pk}"``, ``"{foreign_key}"``) keep their template-owned quotes, escaped by the
caller via ``guitars.sql._identifiers._escape_ident`` before ``.format()`` -- a column is
never schema-qualified, so a single pair of quotes is always correct. Table-DDL positions
(``{table}``, ``{related_table}``, ``{child_table}``, ``{parent_table}`` where each names a
table rather than a column) own no quote characters of their own: the caller supplies an
already-quoted, already-qualified string via ``guitars.sql._identifiers._quote_table``,
which may render as ``"table"`` or ``"schema"."table"`` -- a baked-in single pair of quotes
would quote a ``"schema.table"`` string as one wrong identifier instead of two correct ones.
Changing a public constant's rendered text this way (no ``.format()`` kwarg added or
removed) is safe; see the package docstring on what "frozen" actually covers.

The public ``CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE``/``_VIA`` constants and their
``DROP`` counterparts keep their pre-2.0.0 calling convention -- rule name embedded
literally as ``soft_delete_related_{related_table}``/``..._{foreign_key}`` -- on purpose:
migrations generated before 1.1.0 call ``.format(table=..., related_table=...,
primary_key=..., foreign_key=...)`` directly on these names, with no ``rule_name`` kwarg,
and that calling convention is frozen (see the package docstring). A NAMEDATALEN-safe,
quoted rule name is a *new* value with no counterpart in that old convention, so it cannot
be threaded through the public constants without breaking them. The current generator
instead uses the private ``_CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE``/``_DROP_...`` pair
below, which take an externally-computed ``{rule_name}`` (see ``operations.py``'s
``_related_rule_name``) and serve both the plain and ``_VIA`` cases, since the body SQL
never differed between them -- only the rule name did, and that is now the caller's
concern entirely.
"""

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

# A second CASCADE FK from the same related table to the same parent needs a rule name
# distinct from the first's: a PostgreSQL rule is namespaced by name alone, not by what it
# references, so reusing the plain form's name under a second FK silently replaces the
# first FK's cascade rather than adding to it -- one of the two relations stops being
# enforced with no error anywhere. The generator picks one FK per (related_table, table)
# pair to keep the plain name above unchanged (so every already-migrated project's lone
# cascade rule for a pair is untouched); every other FK on the same pair gets this form
# instead, with its own column folded into the name by the caller (``_related_rule_name``).

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

# ---- Private, non-frozen cascade-rule templates for the current generator ----
# The public constants above keep the pre-2.0.0 convention of embedding the rule name
# directly from ``related_table``/``foreign_key`` -- unquoted and untruncated, matching
# what migrations generated before this module's NAMEDATALEN-safety existed already call.
# These two serve the current generator instead: the body SQL never differed between the
# plain and ``_VIA`` cases, only the rule name did, so a caller that has already computed a
# quoted, NAMEDATALEN-safe ``rule_name`` externally (``operations.py``'s
# ``_related_rule_name``) needs only one CREATE template and one DROP template, not four.
# Not exported from ``sql/__init__.py`` -- not part of the frozen interface.

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

# ---- MTI soft-delete rule (on the child table, soft-deletes the owner, preserves child row) ----
# ``DO INSTEAD`` suppresses the physical delete of the child row and marks the owning ancestor
# instead. The ``_deleted_at IS NULL`` guard makes it idempotent across the per-table DELETEs
# Django issues for an MTI chain, so the owner's cascade rules fire exactly once.

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
