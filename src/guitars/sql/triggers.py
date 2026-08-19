"""Raw SQL for the DB-managed ``_updated_at`` column and for tenant autofill (ADR 0005):
shared trigger *functions* plus the per-table *triggers* calling them. Public
``CREATE_PARENT_UPDATED_AT_*`` keep their frozen 3-arg call; ``_``-prefixed forms are not."""

from __future__ import annotations

from guitars.gucs import BYPASS_GUC, VALUE_SEPARATOR, guc_name
from guitars.sql._identifiers import _escape_ident, _escape_literal, _safe_ident


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

# ---- Private, non-frozen tenant-autofill templates (ADR 0005): fill a NULL tenant column
# from the active scope's GUC, covering multi-row INSERT, INSERT ... SELECT and raw SQL,
# which reach no pre_save. One function per (column, GUC) pair -- see the ADR's benchmark. ----

# The column is baked in statically rather than reached via TG_ARGV: PL/pgSQL cannot write a
# dynamically-named column on NEW, and the to_jsonb/jsonb_populate_record round trip that
# would is +61% per row against +3% for this form (ADR 0005, measured on 20,000-row inserts).

# The bypass guard reads `= 'on'`, NOT the `<> 'on'` of soft_delete.py's rules: there the safe
# answer is "keep guarding", here it is "decline to fill", so the safe direction is the
# opposite one. Every guard below fails toward leaving NULL, which WITH CHECK then refuses.

# The separator guard is the multi-tenant scope, and cannot be dropped because `guc._scalar`
# now refuses a separator inside a *scalar* -- a collection scope legitimately publishes
# `a,b`, and filling from either half would write the row into a tenant nobody named.
_CREATE_TENANT_AUTOFILL_FUNCTION = """
    CREATE FUNCTION {function}()
       RETURNS TRIGGER
       LANGUAGE PLPGSQL
    AS
    $$
    BEGIN
        IF NEW."{column}" IS NOT NULL
           OR COALESCE(current_setting('{bypass_guc}', true), '') = 'on'
           OR COALESCE(current_setting('{guc}', true), '') = ''
           OR position('{separator}' in current_setting('{guc}', true)) > 0
        THEN
            RETURN NEW;
        END IF;
        NEW."{column}" := current_setting('{guc}', true);
        RETURN NEW;
    END;
    $$
"""

_DROP_TENANT_AUTOFILL_FUNCTION = """
    DROP FUNCTION {function}();
"""

# OR REPLACE for the same reason as REPLACE_UPDATED_AT_TRIGGER_FUNCTION above: DROP FUNCTION
# refuses while a trigger depends on it, and CASCADE would take those triggers with it.
_REPLACE_TENANT_AUTOFILL_FUNCTION = _CREATE_TENANT_AUTOFILL_FUNCTION.replace(
    'CREATE FUNCTION', 'CREATE OR REPLACE FUNCTION', 1
)

# Named after the function it calls, not a fixed `tenant_autofill_trigger`: a trigger name is
# unique per table, and `tenanted_manager(org='org', region='region', autofill=True)` needs
# one per (column, GUC) pair -- under a constant name the second CREATE failed migrate.
_CREATE_TENANT_AUTOFILL_TRIGGER = """
    CREATE TRIGGER {trigger}
        BEFORE INSERT ON {table}
        FOR EACH ROW
        EXECUTE FUNCTION {function}();
"""

_DROP_TENANT_AUTOFILL_TRIGGER = """
    DROP TRIGGER {trigger} ON {table};
"""

# Same two-form split as REPLACE_/ADOPT_UPDATED_AT_TRIGGER above: IF EXISTS is a knowledge
# claim, so only the --adopt path -- which is honest about not knowing -- may assert it.
_ADOPT_DROP_TENANT_AUTOFILL_TRIGGER = """
    DROP TRIGGER IF EXISTS {trigger} ON {table};
"""

# Both halves are the drop templates above verbatim, so retirement (which emits a drop alone)
# and replacement can never disagree about how a trigger is dropped.
_REPLACE_TENANT_AUTOFILL_TRIGGER = _DROP_TENANT_AUTOFILL_TRIGGER + _CREATE_TENANT_AUTOFILL_TRIGGER

_ADOPT_TENANT_AUTOFILL_TRIGGER = (
    _ADOPT_DROP_TENANT_AUTOFILL_TRIGGER + _CREATE_TENANT_AUTOFILL_TRIGGER
)


def _squeeze(statement: str) -> str:
    """Collapse runs of whitespace to one space -- a dollar-quoted body keeps the indentation
    the generator gave it, the one difference a comparison against a rendered template must
    never report. Here, so the whole-body compare and the guard probe share one tolerance."""
    return ' '.join(statement.split())


#: Stands in for the slot only the CREATE/DROP headers use, where a caller wants the body
#: alone. Never reaches SQL: :func:`_tenant_autofill_body` slices the header away, and no
#: guard fragment interpolates ``{function}``.
_BODY_ONLY_FUNCTION = 'body_only'


def _tenant_autofill_slots(dimension: str, column: str, function: str) -> dict[str, str]:
    """The slots every ``_*_TENANT_AUTOFILL_FUNCTION`` template takes -- one builder for the
    generator and for ``audittenancy``, since two copies could disagree about a healthy body.
    *function* stays required: a default would render ``CREATE FUNCTION ""()`` if forgotten."""
    slots = {
        'function': _safe_ident(function),
        'column': _escape_ident(column),
        'guc': _escape_literal(guc_name(dimension)),
        'bypass_guc': _escape_literal(BYPASS_GUC),
        'separator': _escape_literal(VALUE_SEPARATOR),
    }
    # Refused in the *shared* builder: `_BARE_IDENTIFIER` admits '$', so a db_column like 'a$$b'
    # otherwise renders a template whose dollar quoting closes early, and the generator emits a
    # migration `migrate` rejects with a bare syntax error while only the audit complained.
    for slot in ('column', 'guc'):
        if '$$' in slots[slot]:
            raise ValueError(
                f'the tenant autofill {slot} {slots[slot]!r} contains "$$", which closes the '
                f'dollar quoting this function template depends on -- the generated migration '
                f'would not apply. Set a db_column / tenant dimension without it.'
            )
    return slots


def _tenant_autofill_body(dimension: str, column: str) -> str:
    """What PostgreSQL stores in ``pg_proc.prosrc`` for this pair -- the text between the
    template's two ``$$`` delimiters, which a dollar-quoted body keeps verbatim. Sliced from
    the template, so the audit compares against the generator's definition, not a second."""
    rendered = _CREATE_TENANT_AUTOFILL_FUNCTION.format(
        **_tenant_autofill_slots(dimension, column, _BODY_ONLY_FUNCTION)
    )
    # Checked, not assumed: the slice is the body only while the template holds the two
    # delimiters it was written with -- otherwise a truncated prefix, reported as drift on a
    # healthy database forever. The slots refuse a '$$' arriving from a column; this, from an edit.
    if (delimiters := rendered.count('$$')) != 2:
        raise ValueError(
            f'the tenant autofill function for {column!r} renders {delimiters} "$$" delimiters '
            f'rather than 2 -- something in this template broke the dollar quoting the body '
            f'slice depends on.'
        )
    return rendered.split('$$')[1]


# Guards the audit probes when a body does not match, so a finding names the hazard rather than
# only "differs". Each fragment is a verbatim slice of the template above, asserted by
# tests/test_enforcement_identity.py: else a probe could describe a guard the kit stopped emitting.
_TENANT_AUTOFILL_GUARDS: tuple[tuple[str, str], ...] = (
    (
        'NEW."{column}" IS NOT NULL',
        'has no explicit-value guard, so it overwrites a tenant the caller supplied with the '
        'active scope -- a write WITH CHECK would have refused is accepted instead',
    ),
    (
        "COALESCE(current_setting('{bypass_guc}', true), '') = 'on'",
        'has no tenant bypass guard, so an INSERT inside tenancy_bypassed() is stamped with '
        'whatever scope was published last instead of being left alone',
    ),
    (
        "COALESCE(current_setting('{guc}', true), '') = ''",
        'has no unscoped guard, so an INSERT outside any tenant frame is stamped from a '
        'session setting rather than failing on NOT NULL',
    ),
    (
        "position('{separator}' in current_setting('{guc}', true)) > 0",
        'has no separator guard, so an INSERT under a scope naming several tenants writes the '
        'joined list into the column, matching no tenant',
    ),
    (
        'NEW."{column}" := current_setting(\'{guc}\', true)',
        'never assigns the tenant column from the active scope, so it fills nothing',
    ),
)


def _tenant_autofill_guard_status(dimension: str, column: str, body: str) -> tuple[list[str], int]:
    """Which of the guards above *body* is missing, described, and how many it still has.
    Whitespace-insensitive for the reason the body comparison is: the generator re-indents
    what it writes. The count is what tells "lost a guard" from "not this shape at all"."""
    slots = _tenant_autofill_slots(dimension, column, _BODY_ONLY_FUNCTION)
    # Comment-only lines dropped before the squeeze, and only here: collapsing newlines puts a
    # commented-out guard back beside live code, where it still reads as a substring and probes
    # intact. Whole lines only, so a `--` inside a string literal is never eaten (ADR 0010).
    squeezed = _squeeze(
        '\n'.join(line for line in body.splitlines() if not line.lstrip().startswith('--'))
    )
    # Probed once per guard and carried as a flag, not re-derived by matching descriptions back:
    # two guards that ever shared wording would otherwise vouch for each other and go unreported.
    probed = [
        (description, _squeeze(fragment.format(**slots)) in squeezed)
        for fragment, description in _TENANT_AUTOFILL_GUARDS
    ]
    return (
        [description for description, intact in probed if not intact],
        sum(intact for _, intact in probed),
    )
