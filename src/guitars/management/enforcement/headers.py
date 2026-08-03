"""Frozen header templates and the scanners that recognise them.

These headers are the dedupe keys the whole generator relies on -- see
``guitars.management.commands.makeguitarmigrations``'s module docstring for the full
three-layer idempotency account. Reword one and every existing migration stops being
recognised; the next run emits duplicates.
"""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Operation headers
# ---------------------------------------------------------------------------
# The dedupe keys. Frozen strings -- see the module docstring. Kept as bare header
# templates, separate from the ``RunSQL`` body they precede, so the emitter below and the
# ``_RE_*`` scanners further down are describing one thing rather than two.

HEADER_TRIGGER_FUNCTION = '# Define function for updated at triggers!'
HEADER_UPDATED_AT = '# Updated at Trigger on "{table}" table!'
HEADER_SOFT_DELETE = '# Soft Delete Rule on "{table}" table!'
HEADER_SOFT_DELETE_RELATED = (
    '# Soft Delete Related Rule on "{related_table}" that is related to "{table}"!'
)
# A second CASCADE FK from the same related_table to the same owner table needs a header
# distinct from the first's, or this one and the regex below collide on the same dedupe key
# -- see sql.CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE_VIA's comment for why the underlying
# PostgreSQL rule name has to disambiguate too. _cascade_candidates below picks one FK per
# pair (sorted first by column) to keep the plain header above unchanged; every other FK on
# the same pair gets this one, naming which column it is.
HEADER_SOFT_DELETE_RELATED_VIA = (
    '# Soft Delete Related Rule on "{related_table}" that is related to "{table}" '
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
# A policy whose *shape* changed, rather than one that did not exist. PostgreSQL has no
# CREATE OR REPLACE POLICY, so re-emitting the CREATE form would fail migrate with "policy
# tenant_scope already exists" -- see ``sql.replace_table_rls``.
HEADER_TENANT_POLICY_REPLACED = '# Tenant RLS replaced on "{table}" table! [POLICY:{identity}]'
HEADER_TENANT_FORCE = '# Tenant FORCE RLS on "{table}" table!'


# ---------------------------------------------------------------------------
# Deriving scanners from headers
# ---------------------------------------------------------------------------

# Regex patterns for recognising enforcement operations already written to migration files.
# These headers are the dedupe keys -- see the module docstring on why they are frozen.
#
# Most are *derived* from the HEADER_* template they recognise, rather than hand-typed a
# second time: the two used to be independent copies of the same information with nothing
# keeping them in sync, which is how a header could be reworded (or a regex "cleaned up")
# without its counterpart changing to match -- a silent duplicate-migration or silent
# no-op, the worst failure this tool has. Deriving one from the other makes that class of
# drift impossible for every pair that can be derived at all.
_PLACEHOLDER_IN_ESCAPED_TEMPLATE = re.compile(r'\\\{(\w+)\\\}')


def _derive_scanner(header_template: str) -> re.Pattern[str]:
    """Turn a frozen ``HEADER_*`` template into the regex that recognises it.

    Escapes the template's literal text, then turns each escaped ``{field}``
    placeholder into a positional, quote-delimited capture group -- every placeholder
    in the templates this is applied to sits inside a pair of double quotes, so that is
    the only shape handled. A header that fuses two forms, needs a *named* group, or
    must NOT capture one of its own placeholders (see the MTI, soft-delete-related and
    tenant-policy patterns below) resists derivation for a specific, commented reason,
    and stays hand-written.
    """
    escaped = re.escape(header_template)
    pattern = _PLACEHOLDER_IN_ESCAPED_TEMPLATE.sub(r'([^"]+)', escaped)
    return re.compile(pattern)


# The two function patterns used to match the *constant reference*
# (``sql.CREATE_UPDATED_AT_TRIGGER_FUNCTION``) rather than the comment header. Now that
# generated migrations inline their SQL there is no such reference to find, so they match
# the header like every other kind. Both forms of migration carry the header, so this
# recognises the ones already written as well as the ones written from here on.
_RE_TRIGGER_FUNCTION = _derive_scanner(HEADER_TRIGGER_FUNCTION)
_RE_PARENT_TRIGGER_FUNCTION = _derive_scanner(HEADER_PARENT_TRIGGER_FUNCTION)
_RE_UPDATED_AT = _derive_scanner(HEADER_UPDATED_AT)
_RE_SOFT_DELETE = _derive_scanner(HEADER_SOFT_DELETE)
_RE_TENANT_FORCE = _derive_scanner(HEADER_TENANT_FORCE)

# Fuses HEADER_SOFT_DELETE_RELATED and HEADER_SOFT_DELETE_RELATED_VIA -- one optional
# trailing group standing in for two header forms -- so hand-written rather than
# mechanically derived from either alone. Deliberately does not consume the header's
# trailing ``!``: it stops right after the (optional) foreign_key's closing quote, one
# character short of the literal text. That is harmless, not an oversight to "fix" -- the
# ``[SQL:...]`` identity below is read from the *tail* of the header line independently of
# where this match ends, so an unconsumed ``!`` in between changes nothing it reads.
_RE_SOFT_DELETE_RELATED = re.compile(
    r'# Soft Delete Related Rule on "([^"]+)" that is related to "([^"]+)"'
    r'(?: via "(?P<foreign_key>[^"]+)")?'
)
# MTI headers carry a leading "MTI " token, so they never collide with the single-table
# patterns above (which anchor on ``# Updated`` / ``# Soft`` immediately after the comment
# mark). Hand-written rather than derived: naively deriving HEADER_MTI_UPDATED_AT /
# HEADER_MTI_SOFT_DELETE would also capture ``parent_table`` out of ``(parent "...")!``,
# and matching on it would mean a parent model's own restructuring (e.g. a table rename)
# reads an MTI child's still-correct trigger as "not covered" and duplicates it. Both stop
# right after the child table's closing quote, before ``(parent "..."`` even starts.
_RE_MTI_UPDATED_AT = re.compile(r'# MTI Updated at Trigger on "([^"]+)" table')
_RE_MTI_SOFT_DELETE = re.compile(r'# MTI Soft Delete Rule on "([^"]+)" table')
# Matches both policy forms -- the initial CREATE and a later replacement -- because for
# every purpose here they mean the same thing: "this table's policy is recorded in a
# migration, and this is the shape it was written with". The ``[POLICY:...]`` identity is
# what makes a *changed* shape detectable at all; without it the table name alone said only
# that some policy existed, so a model gaining a tenant dimension silently kept the old,
# weaker predicate while --check reported nothing to do. Hand-written, not derived: it fuses
# two header forms via one optional group, which a single-template deriver cannot express.
#
# The FORCE header carries an extra token before "RLS", so it can never match this pattern.
# Note this leaves [POLICY:...] itself out of the pattern -- read separately, the same way
# as [SQL:...] below; see _recorded_policy_identity for why.
_RE_TENANT_POLICY = re.compile(r'# Tenant RLS (?:replaced )?on "([^"]+)" table! \[POLICY:\w+\]')
# The [DIGEST:...] marker is matched by _generator.RE_DIGEST.

# The per-operation content digest, read off whatever remains of the header line after one
# of the patterns above matched. Deliberately a *separate* pattern applied to the tail
# rather than an optional group bolted onto each one: every header regex above stays
# byte-identical to the frozen strings it has always been, and each still recognises a
# migration written before this token existed. An absent token means "written before the
# SQL was inlined", which reads as stale and is re-emitted once -- the mechanism by which
# the 1.0.0 soft-delete guard fix finally generates itself on an existing project.
_RE_SQL_IDENTITY = re.compile(r'\[SQL:(?P<sql>\w+)\]')

# The tenant-policy header's own identity token -- the coverage *shape* a policy was
# written with (table, predicate, exempt roles; deliberately not `force`, see
# Command._policy_identity). Read the same way as [SQL:...] above: a pattern applied to
# the header line's own tail, rather than a capture group folded into _RE_TENANT_POLICY
# itself. The two tokens answer different questions and neither replaces the other, but
# there is no reason for one to be read out via a bespoke named group on its header regex
# while the other is read via a generic tail search -- unifying *how* both are read is what
# lets the header/identity split (see the package layout in #10) draw one clean line
# instead of two different ones.
_RE_POLICY_IDENTITY = re.compile(r'\[POLICY:(?P<identity>\w+)\]')

# Whether a policy operation's forward SQL forces row-level security. The lookbehind is not
# optional: every policy operation's ``reverse_sql`` carries ``NO FORCE ROW LEVEL SECURITY``
# in the same slice of text, and without it every table reads as forced.
_RE_FORCED = re.compile(r'(?<!NO )FORCE ROW LEVEL SECURITY')
