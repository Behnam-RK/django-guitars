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

# ``new._deleted_at``, not ``NOW()``, and guarded on ``IS NULL`` as every other family is
# (issue #53): unguarded this overwrote a child archived earlier, and the copy makes the
# surviving value the parent's own -- one timestamp per archive, chaining down each level.
_CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE = """
    CREATE OR REPLACE RULE {rule_name}
        AS ON UPDATE TO {table}
        WHERE old._deleted_at IS NULL AND new._deleted_at IS NOT NULL AND
              COALESCE(current_setting('rules.hard_deletion', true), '') <> 'on'
        DO ALSO (
            UPDATE {related_table}
            SET _deleted_at = new._deleted_at
            WHERE "{foreign_key}" = old."{referenced_key}"
              AND _deleted_at IS NULL
        );
"""

_DROP_SOFT_DELETE_RELATED_OBJECTS_RULE = """
    DROP RULE {rule_name} ON {table};
"""

# The inverse (issue #51), statement-level for the reason ADR 0018 converted the self cascade:
# a second ``ON UPDATE`` rule beside the cascade one doubles the rewriter's expansion per
# cascade level -- 2^depth query trees on *every* UPDATE, a plain ``save()`` included.

# The provenance test is the archive timestamp, exact since 2.11.0 stamped a child with its
# parent's own value. Reviving a parent while rewriting its key in one statement leaves the join
# unable to pair the rows, so children stay archived -- hiding, so no refusal, unlike the sweep.
_CREATE_SOFT_DELETE_REVIVE_FUNCTION = """
    CREATE OR REPLACE FUNCTION {function}()
       RETURNS TRIGGER
       LANGUAGE PLPGSQL
    AS
    $$
    BEGIN
        IF COALESCE(current_setting('rules.hard_deletion', true), '') <> 'on' THEN
            UPDATE {related_table} AS guitars_child
            SET _deleted_at = NULL{updated_at_assignment}
            FROM (
                SELECT guitars_before.*
                FROM guitars_revive_before AS guitars_before
                JOIN guitars_revive_after AS guitars_after
                    ON guitars_after."{primary_key}" = guitars_before."{primary_key}"
                WHERE guitars_before._deleted_at IS NOT NULL
                  AND guitars_after._deleted_at IS NULL
            ) AS guitars_revived
            WHERE guitars_child."{foreign_key}" = guitars_revived."{referenced_key}"
              AND guitars_child._deleted_at = guitars_revived._deleted_at;
        END IF;
        RETURN NULL;
    END;
    $$;
"""

#: Spliced where the child owns the column, for the owned sweep's reason below. Redundant under
#: 2.19.0's row trigger (ADR 0038); kept for tables still on the statement trigger, whose ``WHEN``
#: suppresses it at depth 1, and so no ``[SQL:...]`` identity moves.
_SOFT_DELETE_REVIVE_UPDATED_AT = ', _updated_at = NOW()'

_DROP_SOFT_DELETE_REVIVE_FUNCTION = """
    DROP FUNCTION {function}();
"""

# No ``WHEN (pg_trigger_depth() = 0)``, for the owned sweep's reason: the UPDATE above revives
# children that are parents themselves, whose triggers fire at depth 1. Recursion ends on the
# provenance test, no row below carrying the stamp once it has been cleared.
_CREATE_SOFT_DELETE_REVIVE_TRIGGER = """
    CREATE TRIGGER {trigger}
        AFTER UPDATE ON {table}
        REFERENCING OLD TABLE AS guitars_revive_before NEW TABLE AS guitars_revive_after
        FOR EACH STATEMENT
        EXECUTE FUNCTION {function}();
"""

_DROP_SOFT_DELETE_REVIVE_TRIGGER = """
    DROP TRIGGER {trigger} ON {table};
"""

# The owned sweep's two-form split, for its reason: ``IF EXISTS`` is a knowledge claim, and the
# function stays ``CREATE OR REPLACE`` either way -- ``DROP FUNCTION`` refuses while a trigger
# depends on it, and ``CASCADE`` would take that trigger with it.
_CREATE_SOFT_DELETE_REVIVE = (
    _CREATE_SOFT_DELETE_REVIVE_FUNCTION + _CREATE_SOFT_DELETE_REVIVE_TRIGGER
)

_DROP_SOFT_DELETE_REVIVE = _DROP_SOFT_DELETE_REVIVE_TRIGGER + _DROP_SOFT_DELETE_REVIVE_FUNCTION

# ``CREATE TRIGGER`` has no ``OR REPLACE``, so a re-emission needs the drop in front of it or
# it aborts with *trigger ... already exists* -- and the operation being atomic, it takes
# whatever rule sits beside it in that migration down too.
_REPLACE_SOFT_DELETE_REVIVE = _DROP_SOFT_DELETE_REVIVE_TRIGGER + _CREATE_SOFT_DELETE_REVIVE

# ``--adopt`` is the one path that may claim nothing about what the database holds. The
# function stays ``CREATE OR REPLACE`` in both: ``DROP FUNCTION`` refuses while a trigger
# depends on it, and ``CASCADE`` would take that trigger with it.
_ADOPT_SOFT_DELETE_REVIVE = (
    """
    DROP TRIGGER IF EXISTS {trigger} ON {table};
"""
    + _CREATE_SOFT_DELETE_REVIVE
)

# ---- The joined pair: a CASCADE key on an MTI descendant's table, ``_deleted_at`` on an
# ancestor's. A chain stores one value per row in every table, so the descendant's link to the
# ancestor (not its own pk) names that row directly -- one subselect, however deep. ----

_CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE_JOINED = """
    CREATE OR REPLACE RULE {rule_name}
        AS ON UPDATE TO {table}
        WHERE old._deleted_at IS NULL AND new._deleted_at IS NOT NULL AND
              COALESCE(current_setting('rules.hard_deletion', true), '') <> 'on'
        DO ALSO (
            UPDATE {target_table}
            SET _deleted_at = new._deleted_at
            WHERE "{target_pk}" IN (
                SELECT "{child_pk}" FROM {related_table} WHERE "{foreign_key}" = old."{primary_key}"
            )
              AND _deleted_at IS NULL
        );
"""

_CREATE_SOFT_DELETE_REVIVE_FUNCTION_JOINED = """
    CREATE OR REPLACE FUNCTION {function}()
       RETURNS TRIGGER
       LANGUAGE PLPGSQL
    AS
    $$
    BEGIN
        IF COALESCE(current_setting('rules.hard_deletion', true), '') <> 'on' THEN
            UPDATE {target_table} AS guitars_child
            SET _deleted_at = NULL{updated_at_assignment}
            FROM (
                SELECT guitars_before.*
                FROM guitars_revive_before AS guitars_before
                JOIN guitars_revive_after AS guitars_after
                    ON guitars_after."{primary_key}" = guitars_before."{primary_key}"
                WHERE guitars_before._deleted_at IS NOT NULL
                  AND guitars_after._deleted_at IS NULL
            ) AS guitars_revived
            WHERE guitars_child."{target_pk}" IN (
                    SELECT guitars_link."{child_pk}" FROM {related_table} AS guitars_link
                    WHERE guitars_link."{foreign_key}" = guitars_revived."{primary_key}"
                )
              AND guitars_child._deleted_at = guitars_revived._deleted_at;
        END IF;
        RETURN NULL;
    END;
    $$;
"""

_CREATE_SOFT_DELETE_REVIVE_JOINED = (
    _CREATE_SOFT_DELETE_REVIVE_FUNCTION_JOINED + _CREATE_SOFT_DELETE_REVIVE_TRIGGER
)

_REPLACE_SOFT_DELETE_REVIVE_JOINED = (
    _DROP_SOFT_DELETE_REVIVE_TRIGGER + _CREATE_SOFT_DELETE_REVIVE_JOINED
)

_ADOPT_SOFT_DELETE_REVIVE_JOINED = (
    """
    DROP TRIGGER IF EXISTS {trigger} ON {table};
"""
    + _CREATE_SOFT_DELETE_REVIVE_JOINED
)

# ---- One revive trigger per owner table (2.16.0, #70, ADR 0033): every cascade key's revive
# in one function, so a plain ``UPDATE`` of the owner fires one trigger and leaves at its first
# test. The per-key pair above stays for the retirements that replace it and their reverses. ----

# One arm per cascade key, spliced in key order. Each is the per-key body above without its
# guard, which the function below asks once for every arm.
_SOFT_DELETE_REVIVE_ARM = """
            UPDATE {related_table} AS guitars_child
            SET _deleted_at = NULL{updated_at_assignment}
            FROM (
                SELECT guitars_before.*
                FROM guitars_revive_before AS guitars_before
                JOIN guitars_revive_after AS guitars_after
                    ON guitars_after."{primary_key}" = guitars_before."{primary_key}"
                WHERE guitars_before._deleted_at IS NOT NULL
                  AND guitars_after._deleted_at IS NULL
            ) AS guitars_revived
            WHERE guitars_child."{foreign_key}" = guitars_revived."{referenced_key}"
              AND guitars_child._deleted_at = guitars_revived._deleted_at;"""

_SOFT_DELETE_REVIVE_ARM_JOINED = """
            UPDATE {target_table} AS guitars_child
            SET _deleted_at = NULL{updated_at_assignment}
            FROM (
                SELECT guitars_before.*
                FROM guitars_revive_before AS guitars_before
                JOIN guitars_revive_after AS guitars_after
                    ON guitars_after."{primary_key}" = guitars_before."{primary_key}"
                WHERE guitars_before._deleted_at IS NOT NULL
                  AND guitars_after._deleted_at IS NULL
            ) AS guitars_revived
            WHERE guitars_child."{target_pk}" IN (
                    SELECT guitars_link."{child_pk}" FROM {related_table} AS guitars_link
                    WHERE guitars_link."{foreign_key}" = guitars_revived."{primary_key}"
                )
              AND guitars_child._deleted_at = guitars_revived._deleted_at;"""

# The archive arms (#80, ADR 0039): the cascade ``ON UPDATE`` rule as a statement of the owner's
# trigger -- a rule is planned on every ``UPDATE`` (12 statements for one ``Band`` rename). They read
# the *before* image's key and the *after* image's stamp, and keep issue #53's ``IS NULL``.
_SOFT_DELETE_ARCHIVE_ARM = """
            UPDATE {related_table} AS guitars_child
            SET _deleted_at = guitars_archived._deleted_at{updated_at_assignment}
            FROM (
                SELECT guitars_before."{referenced_key}" AS guitars_key, guitars_after._deleted_at
                FROM guitars_revive_before AS guitars_before
                JOIN guitars_revive_after AS guitars_after
                    ON guitars_after."{primary_key}" = guitars_before."{primary_key}"
                WHERE guitars_before._deleted_at IS NULL
                  AND guitars_after._deleted_at IS NOT NULL
            ) AS guitars_archived
            WHERE guitars_child."{foreign_key}" = guitars_archived.guitars_key
              AND guitars_child._deleted_at IS NULL;"""

_SOFT_DELETE_ARCHIVE_ARM_JOINED = """
            UPDATE {target_table} AS guitars_child
            SET _deleted_at = guitars_archived._deleted_at{updated_at_assignment}
            FROM (
                SELECT guitars_before."{primary_key}" AS guitars_key, guitars_after._deleted_at
                FROM guitars_revive_before AS guitars_before
                JOIN guitars_revive_after AS guitars_after
                    ON guitars_after."{primary_key}" = guitars_before."{primary_key}"
                WHERE guitars_before._deleted_at IS NULL
                  AND guitars_after._deleted_at IS NOT NULL
            ) AS guitars_archived
            WHERE guitars_child."{target_pk}" IN (
                    SELECT guitars_link."{child_pk}" FROM {related_table} AS guitars_link
                    WHERE guitars_link."{foreign_key}" = guitars_archived.guitars_key
                )
              AND guitars_child._deleted_at IS NULL;"""

# A statement that archives a row *and* rewrites its primary key leaves the arms nothing to pair
# it by, where the rule read ``old.`` per row. Refused once a live child holds a key of any live
# before-row, not only a vanished one: a pk reused inside the statement vanishes from no one.
_SOFT_DELETE_LEAK_CHECK = """
                EXISTS (
                    SELECT 1 FROM {related_table} AS guitars_child
                    WHERE guitars_child._deleted_at IS NULL
                      AND guitars_child."{foreign_key}" IN (
                          SELECT guitars_held."{referenced_key}"
                          FROM guitars_revive_before AS guitars_held
                          WHERE guitars_held._deleted_at IS NULL
                      )
                )"""

_SOFT_DELETE_LEAK_CHECK_JOINED = """
                EXISTS (
                    SELECT 1 FROM {target_table} AS guitars_child
                    WHERE guitars_child._deleted_at IS NULL
                      AND guitars_child."{target_pk}" IN (
                          SELECT guitars_link."{child_pk}" FROM {related_table} AS guitars_link
                          WHERE guitars_link."{foreign_key}" IN (
                              SELECT guitars_held."{primary_key}"
                              FROM guitars_revive_before AS guitars_held
                              WHERE guitars_held._deleted_at IS NULL
                          )
                      )
                )"""

# ``{dollar}`` is ``$$`` unless a name in the body holds one (``operations._dollar_quote``, #80). The
# first ``EXISTS`` is one left join of the transition tables, which a plain ``save()`` stops at: a
# row whose ``_deleted_at`` flipped, or one archived whose key moved; the arms sit behind their own.
_CREATE_SOFT_DELETE_REVIVE_OWNER_FUNCTION = """
    CREATE OR REPLACE FUNCTION {function}()
       RETURNS TRIGGER
       LANGUAGE PLPGSQL
    AS
    {dollar}
    BEGIN
        IF COALESCE(current_setting('rules.hard_deletion', true), '') <> 'on' AND EXISTS (
            SELECT 1
            FROM guitars_revive_after AS guitars_after
            LEFT JOIN guitars_revive_before AS guitars_before
                ON guitars_before."{primary_key}" = guitars_after."{primary_key}"
            WHERE (guitars_before."{primary_key}" IS NULL AND guitars_after._deleted_at IS NOT NULL)
               OR (guitars_before."{primary_key}" IS NOT NULL
                   AND (guitars_before._deleted_at IS NULL) <> (guitars_after._deleted_at IS NULL))
        ) THEN
            IF EXISTS (
                SELECT 1
                FROM guitars_revive_after AS guitars_after
                WHERE guitars_after._deleted_at IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM guitars_revive_before AS guitars_before
                      WHERE guitars_before."{primary_key}" = guitars_after."{primary_key}"
                  )
            ) AND ({leak_checks}
            ) THEN
                RAISE EXCEPTION
                    'guitars: a statement on % archived a row whose primary key it also '
                    'rewrote, and a live child is left holding a key it read. The cascade pairs '
                    'a row across the statement on its primary key, so it cannot tell which '
                    'before-row that archived row was, and would leave the child live under an '
                    'archived parent. Rewrite the key and archive the row in separate '
                    'statements.', TG_TABLE_NAME
                    USING ERRCODE = 'feature_not_supported';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM guitars_revive_before AS guitars_before
                JOIN guitars_revive_after AS guitars_after
                    ON guitars_after."{primary_key}" = guitars_before."{primary_key}"
                WHERE guitars_before._deleted_at IS NULL
                  AND guitars_after._deleted_at IS NOT NULL
            ) THEN{archive_arms}
            END IF;
            IF EXISTS (
                SELECT 1
                FROM guitars_revive_before AS guitars_before
                JOIN guitars_revive_after AS guitars_after
                    ON guitars_after."{primary_key}" = guitars_before."{primary_key}"
                WHERE guitars_before._deleted_at IS NOT NULL
                  AND guitars_after._deleted_at IS NULL
            ) THEN{arms}
            END IF;
        END IF;
        RETURN NULL;
    END;
    {dollar};
"""

# ``CREATE OR REPLACE TRIGGER`` (PG 14, the floor) for the re-emission of a trigger that exists:
# ``DROP`` would hold ACCESS EXCLUSIVE on the owner until the migration commits, and one
# migration holds it on every owner table of the app. Adopt is the same statement.
_CREATE_SOFT_DELETE_REVIVE_OWNER = (
    _CREATE_SOFT_DELETE_REVIVE_OWNER_FUNCTION + _CREATE_SOFT_DELETE_REVIVE_TRIGGER
)
_REPLACE_SOFT_DELETE_REVIVE_OWNER = (
    _CREATE_SOFT_DELETE_REVIVE_OWNER_FUNCTION
    + _CREATE_SOFT_DELETE_REVIVE_TRIGGER.replace('CREATE TRIGGER', 'CREATE OR REPLACE TRIGGER', 1)
)
_ADOPT_SOFT_DELETE_REVIVE_OWNER = _REPLACE_SOFT_DELETE_REVIVE_OWNER

# What 2.16.0 -- 2.18.x wrote: the revive half alone. Kept to rebuild it as the reverse of the
# migration that retires it (``_legacy_revive_owner_reverse``).
_LEGACY_CREATE_SOFT_DELETE_REVIVE_OWNER_FUNCTION = """
    CREATE OR REPLACE FUNCTION {function}()
       RETURNS TRIGGER
       LANGUAGE PLPGSQL
    AS
    {dollar}
    BEGIN
        IF COALESCE(current_setting('rules.hard_deletion', true), '') <> 'on' AND EXISTS (
            SELECT 1
            FROM guitars_revive_before AS guitars_before
            JOIN guitars_revive_after AS guitars_after
                ON guitars_after."{primary_key}" = guitars_before."{primary_key}"
            WHERE guitars_before._deleted_at IS NOT NULL
              AND guitars_after._deleted_at IS NULL
        ) THEN{arms}
        END IF;
        RETURN NULL;
    END;
    {dollar};
"""
_LEGACY_CREATE_SOFT_DELETE_REVIVE_OWNER = (
    _LEGACY_CREATE_SOFT_DELETE_REVIVE_OWNER_FUNCTION + _CREATE_SOFT_DELETE_REVIVE_TRIGGER
)

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

# The two transition tables correlate on the primary key, their only row identity, so a statement
# rewriting a live owning row's key leaves the join unable to say which after-row that before-row
# became -- and so unable to say whether it was archived. Refused rather than guessed.

# Refused *narrowly*: the guard asks the ``UPDATE`` below's own question -- dependent still live,
# no live owner, every co-owner arm -- over the vanished rows instead of the matched ones. Asking
# less refused a statement whose dependent a co-owner still held, or was already archived.

# Keeping the row and letting the post-statement ``NOT EXISTS`` decide was tried and rejected: it
# stamps a target an owner merely *reassigned away from*, which neither the rule nor the matched
# path stamps, so one corner of the sweep would archive on a reassignment and the rest not.

# Out of scope, and undetectable here: a statement that *permutes* primary keys among rows leaves
# every before-key present in the after image, so the guard sees no vanished row and the join
# below pairs each before-row with another's after-image. There is no second row identity to ask.

# Terminated ``{dollar};`` unlike the autofill template it mirrors: that is an operation by itself,
# this is concatenated before its CREATE TRIGGER and an unterminated body swallows it. The
# indentation lands the spliced arms at the depth they are written with.
_CREATE_SOFT_DELETE_OWNED_SWEEP_FUNCTION = """
    CREATE OR REPLACE FUNCTION {function}()
       RETURNS TRIGGER
       LANGUAGE PLPGSQL
    AS
    {dollar}
    BEGIN
        IF COALESCE(current_setting('rules.hard_deletion', true), '') <> 'on' THEN
            IF EXISTS (
                SELECT 1
                FROM guitars_owned_before AS guitars_before
                JOIN {dependent_table} AS guitars_dependent
                    ON guitars_dependent."{dependent_primary_key}" = guitars_before."{foreign_key}"
                WHERE guitars_before._deleted_at IS NULL
                  AND guitars_before."{foreign_key}" IS NOT NULL
                  AND guitars_dependent._deleted_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM guitars_owned_after AS guitars_after
                      WHERE guitars_after."{primary_key}" = guitars_before."{primary_key}"
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM {table} AS guitars_owner
                      WHERE guitars_owner."{foreign_key}" = guitars_before."{foreign_key}"
                        AND guitars_owner._deleted_at IS NULL
                  ){guard_co_owner_guards}
            ) THEN
                RAISE EXCEPTION
                    'guitars: a statement on % rewrote the primary key of a live owning row '
                    'whose dependent is now held by no live owner. The owned sweep correlates '
                    'its transition tables on the primary key, so it cannot tell whether that '
                    'row was archived, and would leak the dependent permanently. Rewrite the '
                    'key and archive the row in separate statements.', TG_TABLE_NAME
                    USING ERRCODE = 'feature_not_supported';
            END IF;
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
    {dollar};
"""

#: Filled where the dependent owns the column: this runs at depth 1, where a pre-2.19.0 statement
#: trigger's ``WHEN`` suppresses ``updated_at_trigger``. Redundant under the row trigger (ADR 0038),
#: kept for tables not yet regenerated and so no ``[SQL:...]`` identity moves.
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

# A retired rule whose column is unrecoverable -- the primary form's key never spelled it, and
# the field is gone -- gets this as its ``reverse_sql``. Refusing loudly beats a silent no-op
# that would leave history claiming a rule the database does not have.
_REFUSE_RECREATING_RETIRED_RULE = """
    DO $guitars_retired$
    BEGIN
        RAISE EXCEPTION
            'guitars: rule % on % cannot be recreated -- the migration that retired it could '
            'not record which column it read. Restore the foreign key in the models and run '
            'makeguitarmigrations.', {literal_rule_name}, {literal_table}
            USING ERRCODE = 'feature_not_supported';
    END;
    $guitars_retired$;
"""

# The same refusal for a joined key, whose column is recorded but whose rule updated an ancestor
# table that the key does not name -- a different cause, so a different message.
_REFUSE_RECREATING_JOINED_RULE = """
    DO $guitars_retired$
    BEGIN
        RAISE EXCEPTION
            'guitars: rule % on % cannot be recreated -- it updated an ancestor table that '
            'the migration that retired it did not record. Restore the foreign key in the '
            'models and run makeguitarmigrations.', {literal_rule_name}, {literal_table}
            USING ERRCODE = 'feature_not_supported';
    END;
    $guitars_retired$;
"""

# The same refusal for a key whose child model was deleted (#63): nothing left in the models says
# which column it read, and the table it fired into is gone.
_REFUSE_RECREATING_DROPPED_RULE = """
    DO $guitars_retired$
    BEGIN
        RAISE EXCEPTION
            'guitars: % on % cannot be recreated -- the model it read was deleted. To migrate '
            'back past this, unapply this migration with --fake, reverse the deletion, then run '
            'makeguitarmigrations --adopt to rebuild it.',
            {literal_rule_name}, {literal_table}
            USING ERRCODE = 'feature_not_supported';
    END;
    $guitars_retired$;
"""

# The same refusal where a name the rebuilt body would splice in holds ``$$``, which closes the
# dollar quoting its trigger function depends on: the forward path skips such a key, so the
# reverse cannot rebuild it either.
_REFUSE_RECREATING_DOLLAR_QUOTED = """
    DO $guitars_retired$
    BEGIN
        RAISE EXCEPTION
            'guitars: % on % cannot be recreated -- a table or column it names contains two '
            'dollar signs in a row, which closes the dollar quoting its trigger function depends '
            'on. Rename it, then run makeguitarmigrations.',
            {literal_rule_name}, {literal_table}
            USING ERRCODE = 'feature_not_supported';
    END;
    $guitars_retired$;
"""

# The reverse of #66's retirements: what they dropped reads a column or table the models no
# longer have, so nothing here can rebuild it. ``--adopt`` re-emits what the models call for.
_REFUSE_REVERSING_RETIREMENT = """
    DO $guitars_retired$
    BEGIN
        RAISE EXCEPTION
            'guitars: % on % was retired and cannot be rebuilt by reversing this migration. '
            'Unapply it with --fake, restore the models, then run makeguitarmigrations --adopt.',
            {literal_name}, {literal_table}
            USING ERRCODE = 'feature_not_supported';
    END;
    $guitars_retired$;
"""

# ---- Self-referential cascade: a trigger where the family above is a rule. A rule updating the
# table it fires on is rewritten into itself and PostgreSQL rejects **every** ``UPDATE`` there.
# Self keys only -- a multi-table cycle has no stable choice of edge. See ADR 0018. ----


# Guard one refuses what guard two cannot see: a row archived under a key this statement also
# rewrote has no before-image to match, so a live child at *either* key -- the old one, or the
# new one it was re-parented onto -- would leak in silence.

# Gated on an archive having happened, so a plain re-key never raises. Narrower than the owned
# sweep's, which fires on the ambiguity alone: the parity is in the error class, not the reach.

# Guard two **terminates** the recursion rather than merely cheapening it: a statement trigger
# fires on an UPDATE matching zero rows, so without it this function's own no-op UPDATE re-fires
# it forever. It asks the UPDATE's own predicate, the archived transition.
_CREATE_SOFT_DELETE_SELF_CASCADE_FUNCTION = """
    CREATE OR REPLACE FUNCTION {function}()
       RETURNS TRIGGER
       LANGUAGE PLPGSQL
    AS
    $$
    BEGIN
        IF COALESCE(current_setting('rules.hard_deletion', true), '') <> 'on' THEN
            IF EXISTS (
                SELECT 1
                FROM guitars_self_after AS guitars_after
                WHERE guitars_after._deleted_at IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM guitars_self_before AS guitars_before
                      WHERE guitars_before."{primary_key}" = guitars_after."{primary_key}"
                  )
                  AND EXISTS (
                      SELECT 1 FROM {table} AS guitars_child
                      WHERE guitars_child._deleted_at IS NULL
                        AND (
                            guitars_child."{foreign_key}" = guitars_after."{referenced_key}"
                            OR guitars_child."{foreign_key}" IN (
                                SELECT guitars_vanished."{referenced_key}"
                                FROM guitars_self_before AS guitars_vanished
                                WHERE guitars_vanished._deleted_at IS NULL
                                  AND NOT EXISTS (
                                      SELECT 1 FROM guitars_self_after AS guitars_kept
                                      WHERE guitars_kept."{primary_key}"
                                          = guitars_vanished."{primary_key}"
                                  )
                            )
                        )
                  )
            ) THEN
                RAISE EXCEPTION
                    'guitars: a statement on % archived a row whose primary key it also '
                    'rewrote, and a live child is left holding one of the two keys. The self '
                    'cascade trigger correlates its transition tables on the primary key, so '
                    'it cannot tell which before-row that archived row was, and would leak the '
                    'whole subtree permanently. Rewrite the key and archive the row in '
                    'separate statements.', TG_TABLE_NAME
                    USING ERRCODE = 'feature_not_supported';
            END IF;
        END IF;
        IF COALESCE(current_setting('rules.hard_deletion', true), '') <> 'on'
           AND EXISTS (
               SELECT 1
               FROM guitars_self_before AS guitars_before
               JOIN guitars_self_after AS guitars_after
                   ON guitars_after."{primary_key}" = guitars_before."{primary_key}"
               WHERE guitars_before._deleted_at IS NULL
                 AND guitars_after._deleted_at IS NOT NULL
           ) THEN
            UPDATE {table} AS guitars_child
            SET _deleted_at = NOW(){updated_at_assignment}
            FROM (
                SELECT guitars_before."{referenced_key}" AS guitars_key
                FROM guitars_self_before AS guitars_before
                JOIN guitars_self_after AS guitars_after
                    ON guitars_after."{primary_key}" = guitars_before."{primary_key}"
                WHERE guitars_before._deleted_at IS NULL
                  AND guitars_after._deleted_at IS NOT NULL
            ) AS guitars_archived
            WHERE guitars_child."{foreign_key}" = guitars_archived.guitars_key
              AND guitars_child._deleted_at IS NULL;
        END IF;
        RETURN NULL;
    END;
    $$;
"""

#: Spliced for the sweep's reason (above). A slot: a model may carry no ``_updated_at``. Redundant
#: under 2.19.0's row trigger (ADR 0038), kept for unregenerated tables so no identity moves.
_SOFT_DELETE_SELF_CASCADE_UPDATED_AT = ', _updated_at = NOW()'

_DROP_SOFT_DELETE_SELF_CASCADE_FUNCTION = """
    DROP FUNCTION {function}();
"""

# No ``WHEN (pg_trigger_depth() = 0)``: here the trigger re-fires *itself*, its own UPDATE being
# another statement on this table, which is how the next level down is reached. Recursion ends
# where a level archives nothing. Depth is bounded by ``max_stack_depth``, one frame per level.
_CREATE_SOFT_DELETE_SELF_CASCADE_TRIGGER = """
    CREATE TRIGGER {trigger}
        AFTER UPDATE ON {table}
        REFERENCING OLD TABLE AS guitars_self_before NEW TABLE AS guitars_self_after
        FOR EACH STATEMENT
        EXECUTE FUNCTION {function}();
"""

_DROP_SOFT_DELETE_SELF_CASCADE_TRIGGER = """
    DROP TRIGGER {trigger} ON {table};
"""

# The owned sweep's four forms, for its reasons: IF EXISTS is a knowledge claim, so only --adopt
# says it, and the function stays CREATE OR REPLACE everywhere -- DROP FUNCTION refuses while a
# trigger depends on it, and CASCADE would take that trigger with it.
_CREATE_SOFT_DELETE_SELF_CASCADE = (
    _CREATE_SOFT_DELETE_SELF_CASCADE_FUNCTION + _CREATE_SOFT_DELETE_SELF_CASCADE_TRIGGER
)

_DROP_SOFT_DELETE_SELF_CASCADE = (
    _DROP_SOFT_DELETE_SELF_CASCADE_TRIGGER + _DROP_SOFT_DELETE_SELF_CASCADE_FUNCTION
)

_REPLACE_SOFT_DELETE_SELF_CASCADE = (
    _DROP_SOFT_DELETE_SELF_CASCADE_TRIGGER + _CREATE_SOFT_DELETE_SELF_CASCADE
)

_ADOPT_SOFT_DELETE_SELF_CASCADE = (
    """
    DROP TRIGGER IF EXISTS {trigger} ON {table};
"""
    + _CREATE_SOFT_DELETE_SELF_CASCADE
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

# ---- Rename forms. PostgreSQL carries an object with its table, so a name-bearing one survives
# a rename under the name the *old* table gave it while the CREATE below mints a new one. ``IF
# EXISTS`` because a project that never applied the old coverage has nothing to drop. ----

_DROP_RENAMED_TRIGGER = """
    DROP TRIGGER IF EXISTS {old_trigger} ON {table};
    DROP FUNCTION IF EXISTS {old_function}();
"""

#: Prepended to a rule's ``CREATE OR REPLACE``: that alone would leave the carried-over rule
#: live beside the new one, both cascading, which is a duplicate rather than a failure -- but a
#: duplicate nothing later retires.
_DROP_RENAMED_RULE = """
    DROP RULE IF EXISTS {old_rule_name} ON {table};
"""
