"""Frozen header templates and the scanners that recognise them -- the generator's dedupe
keys (see ``docs/migrations.md``'s "Idempotency has three layers"). Reword one and every
existing migration stops being recognised; the next run emits duplicates."""

from __future__ import annotations

import re


# --- Operation headers: the dedupe keys, frozen (see the module docstring). Kept as bare
# templates, separate from the RunSQL body they precede, so the emitter and the _RE_*
# scanners below are describing one thing rather than two. ---

HEADER_TRIGGER_FUNCTION = '# Define function for updated at triggers!'
HEADER_UPDATED_AT = '# Updated at Trigger on "{table}" table!'
HEADER_SOFT_DELETE = '# Soft Delete Rule on "{table}" table!'
HEADER_SOFT_DELETE_RELATED = (
    '# Soft Delete Related Rule on "{related_table}" that is related to "{table}"!'
)
# A second CASCADE FK between the same two tables needs a header distinct from the
# first's, or it collides on the same dedupe key -- _cascade_candidates below picks one FK
# per pair to keep the plain header above, every other FK gets this one instead.
HEADER_SOFT_DELETE_RELATED_VIA = (
    '# Soft Delete Related Rule on "{related_table}" that is related to "{table}" '
    'via "{foreign_key}"!'
)

# The mirror of the two above, the FK living on the owner. Its own header and rule-name
# prefix, since a rule is namespaced by name alone and a collision replaces rather than
# fails. Always carries the FK, so there is no plain sibling and its scanner is derived.
HEADER_SOFT_DELETE_OWNED = (
    '# Soft Delete Owned Rule on "{dependent_table}" that is owned by "{table}" '
    'via "{foreign_key}"!'
)

# --- Multi-table inheritance (MTI) operations ---

HEADER_PARENT_TRIGGER_FUNCTION = '# Define function for MTI parent updated at triggers!'
HEADER_MTI_UPDATED_AT = (
    '# MTI Updated at Trigger on "{child_table}" table (parent "{parent_table}")!'
)
HEADER_MTI_SOFT_DELETE = (
    '# MTI Soft Delete Rule on "{child_table}" table (parent "{parent_table}")!'
)

# --- Tenancy ---

HEADER_TENANT_POLICY = '# Tenant RLS on "{table}" table! [POLICY:{identity}]'
# A policy whose *shape* changed, not one that's new -- Postgres has no CREATE OR REPLACE
# POLICY, so re-emitting CREATE would fail migrate with "policy already exists".
HEADER_TENANT_POLICY_REPLACED = '# Tenant RLS replaced on "{table}" table! [POLICY:{identity}]'
HEADER_TENANT_FORCE = '# Tenant FORCE RLS on "{table}" table!'
# One function per (column, GUC) pair rather than per table -- GUITARS_TENANT_FIELD is
# project-wide, so this is normally one function, and the trigger header names the function
# it calls so an app's dependency on that function migration can be keyed off the header.
HEADER_TENANT_AUTOFILL_FUNCTION = '# Tenant autofill function "{function}"!'
HEADER_TENANT_AUTOFILL = '# Tenant autofill Trigger on "{table}" table (function "{function}")!'
# The retirement of the above, and the one header meaning an operation *removed* something:
# a rename produces a new (table, function) key and orphans the old trigger, which still
# calls a function dereferencing a dropped column. See ``docs/migrations.md``'s "Retirement".
HEADER_TENANT_AUTOFILL_RETIRED = (
    '# Tenant autofill Trigger retired on "{table}" table (function "{function}")!'
)


# --- Deriving scanners from headers: most _RE_* are *derived* from their HEADER_*
# template, not hand-typed again, so a reworded header can't silently drift from its
# scanner. What can't be derived (fused forms, an uncaptured placeholder) is hand-written. ---
_PLACEHOLDER_IN_ESCAPED_TEMPLATE = re.compile(r'\\\{(\w+)\\\}')

#: A quote-delimited capture group's content, doubled ``""`` standing for one escaped quote
#: -- same disambiguation as ``sql/_identifiers.py``'s ``_QUOTED_QUALIFIED``, needed once a
#: schema-qualified table's pre-quoted form can itself contain a literal ``"``.
_QUOTED_CONTENT = r'(?:[^"]|"")*'


def _derive_scanner(header_template: str) -> re.Pattern[str]:
    """Turn a frozen ``HEADER_*`` template into the regex that recognises it: escape the
    literal text, turn each escaped ``{field}`` into a quote-delimited capture group. A
    fused header, or one with a placeholder it must not capture, stays hand-written."""
    escaped = re.escape(header_template)
    pattern = _PLACEHOLDER_IN_ESCAPED_TEMPLATE.sub(f'({_QUOTED_CONTENT})', escaped)
    return re.compile(pattern)


# Both pre- and post-1.1.0 migrations carry the same header text (only what follows it
# changed, from a named sql constant to inlined SQL), so one scanner recognises both.
_RE_TRIGGER_FUNCTION = _derive_scanner(HEADER_TRIGGER_FUNCTION)
_RE_PARENT_TRIGGER_FUNCTION = _derive_scanner(HEADER_PARENT_TRIGGER_FUNCTION)
_RE_UPDATED_AT = _derive_scanner(HEADER_UPDATED_AT)
_RE_SOFT_DELETE = _derive_scanner(HEADER_SOFT_DELETE)
# Derivable where its cascade siblings are not: the owned header has one form, always
# carrying the FK, so there is nothing to fuse an optional group around.
_RE_SOFT_DELETE_OWNED = _derive_scanner(HEADER_SOFT_DELETE_OWNED)
_RE_TENANT_FORCE = _derive_scanner(HEADER_TENANT_FORCE)
_RE_TENANT_AUTOFILL_FUNCTION = _derive_scanner(HEADER_TENANT_AUTOFILL_FUNCTION)
# Derived, and deliberately NOT fused with _RE_TENANT_AUTOFILL the way _RE_TENANT_POLICY
# fuses its two forms: there both forms mean "recorded, and this is its shape", here they
# mean opposite things about existence, so one scanner reading both would retire forever.
_RE_TENANT_AUTOFILL_RETIRED = _derive_scanner(HEADER_TENANT_AUTOFILL_RETIRED)

# Captures BOTH halves, unlike the MTI pair below: two local dimensions mean one trigger per
# (column, GUC) pair on one table, so the table alone let the second's digest overwrite the
# first's. A rename reads as uncovered, which is right -- the trigger is named after it.
_RE_TENANT_AUTOFILL = re.compile(
    rf'# Tenant autofill Trigger on "({_QUOTED_CONTENT})" table \(function "({_QUOTED_CONTENT})"\)'
)
#: Group numbers on :data:`_RE_TENANT_AUTOFILL`, named so the two readers of that header --
#: the ``(table, function)`` dedupe key and ``_function_dependencies_for``'s edge to the
#: function migration -- index it by meaning rather than by a bare 1/2.
RE_TENANT_AUTOFILL_TABLE = 1
RE_TENANT_AUTOFILL_FUNCTION = 2

# Hand-written: fuses HEADER_SOFT_DELETE_RELATED and its _VIA sibling into one optional
# trailing group. Stops short of the header's trailing "!", harmless since [SQL:...] is
# read from the tail independently of where this match ends.
_RE_SOFT_DELETE_RELATED = re.compile(
    rf'# Soft Delete Related Rule on "({_QUOTED_CONTENT})" that is related to "({_QUOTED_CONTENT})"'
    rf'(?: via "(?P<foreign_key>{_QUOTED_CONTENT})")?'
)
# Hand-written: naive derivation would also capture parent_table, so a parent's own rename
# would read an MTI child's still-correct trigger as uncovered and duplicate it. Both stop
# right after the child table's closing quote, before "(parent ..." starts.
_RE_MTI_UPDATED_AT = re.compile(rf'# MTI Updated at Trigger on "({_QUOTED_CONTENT})" table')
_RE_MTI_SOFT_DELETE = re.compile(rf'# MTI Soft Delete Rule on "({_QUOTED_CONTENT})" table')
# Hand-written: fuses the CREATE and replaced forms via one optional group -- for every
# purpose here both mean "this table's policy is recorded, and this is its shape". The
# FORCE header carries an extra token before "RLS" and can never match this pattern.
_RE_TENANT_POLICY = re.compile(
    rf'# Tenant RLS (?:replaced )?on "({_QUOTED_CONTENT})" table! \[POLICY:\w+\]'
)
# The [DIGEST:...] marker is matched by _generator.RE_DIGEST.

# The per-operation content digest, read off the header line's tail rather than folded
# into each regex above, so every header regex stays byte-identical to its frozen string
# and still matches a migration written before this token existed (read as stale, re-emitted).
_RE_SQL_IDENTITY = re.compile(r'\[SQL:(?P<sql>\w+)\]')

# The tenant-policy header's own identity token (table, predicate, exempt roles --
# deliberately not `force`, see Command._policy_identity). Read the same tail-search way
# as [SQL:...] above rather than folded into _RE_TENANT_POLICY's own capture groups.
_RE_POLICY_IDENTITY = re.compile(r'\[POLICY:(?P<identity>\w+)\]')

# Whether a policy operation's forward SQL forces row-level security. The lookbehind is not
# optional: every policy's reverse_sql carries "NO FORCE ROW LEVEL SECURITY" in the same
# slice of text, and without it every table would read as forced.
_RE_FORCED = re.compile(r'(?<!NO )FORCE ROW LEVEL SECURITY')
