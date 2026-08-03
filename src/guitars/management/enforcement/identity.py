"""Rendering an operation's SQL, and reading back the identity tokens on its header.

Both directions of the same mechanism: :func:`_operation` renders a ``RunSQL`` snippet and
stamps a ``[SQL:...]`` content digest onto its header line; :func:`_recorded_sql_identity`
and :func:`_recorded_policy_identity` read a digest/identity back off an already-written
header line, via the same tail-search pattern (see ``headers.py`` for why both tokens are
read that way despite answering different questions).
"""

from __future__ import annotations

import re

from guitars.management import _generator
from guitars.management.enforcement.headers import (
    _RE_FORCED,
    _RE_POLICY_IDENTITY,
    _RE_SQL_IDENTITY,
)


def _as_list(statements: str | list[str]) -> list[str]:
    return [statements] if isinstance(statements, str) else list(statements)


def _sql_string_literal(text: str) -> str:
    """One SQL statement as a Python literal.

    Triple-quoted where the text allows it, because a reviewer reads these in a diff and
    ``repr`` collapses a twenty-line rule to one line of ``\\n``. The guard is narrow on
    purpose: a trailing double quote would run into the closing delimiter, an embedded
    triple quote would close it early, and a backslash would be re-interpreted as an escape.
    """
    if '"""' in text or '\\' in text or text.endswith('"'):
        return repr(text)
    return f'"""{text}"""'


def _sql_literal(statements: str | list[str]) -> str:
    """Rendered SQL as a Python literal, for embedding in a generated migration.

    Generated migrations carry their SQL **literally** rather than doing
    ``from guitars import sql`` and naming the constants. Django freezes model state into
    migration files so replaying history reproduces the same database; a migration that
    reads a library constant at ``migrate`` time un-freezes precisely that -- a fresh
    ``migrate`` on one version of the kit and an incrementally-migrated database on another
    end up differing while sharing an identical migration history.

    That indirection is also why a change to a SQL constant used to ship no migration at
    all: the ``[DIGEST:...]`` marker covers the operation source, and the source named the
    constant rather than containing it, so no edit to the SQL could ever move the digest.
    """
    if isinstance(statements, str):
        return _sql_string_literal(statements)
    inner = ''.join(f'        {_sql_string_literal(item)},\n' for item in statements)
    return f'[\n{inner}    ]'


def _sql_digest(forward: str | list[str], reverse: str | list[str]) -> str:
    """Content digest of one operation's SQL, stamped into its header as ``[SQL:...]``.

    This is what makes a changed SQL constant generate its own migration, and it is the
    reason the header carries it rather than the file-level ``[DIGEST:...]``: the per-table
    ``_RE_*`` scan short-circuits first, reporting the table covered, so no operation is
    ever built and the file digest is never reached. The short-circuit is the header, so
    the header is where the content identity has to live.

    Always taken over the **canonical** (create) form of the forward SQL, never over the
    replace or adopt form actually emitted -- see :func:`_operation`.
    """
    return _generator.digest_of([*_as_list(forward), '--', *_as_list(reverse)])[:12]


def _operation(
    header: str,
    forward: str | list[str],
    reverse: str | list[str],
    *,
    emit: str | list[str] | None = None,
) -> tuple[str, str]:
    """Render one ``RunSQL`` operation, returning ``(source, digest)``.

    *emit* substitutes the SQL actually written -- the replace or adopt form -- while the
    digest stays keyed to the canonical *forward*. The two must not be conflated: the
    digest answers "which definition is installed", the form answers "how do I install it
    given what the migration history records". Digesting the emitted form instead makes
    successive runs disagree forever -- a fresh database records the create form's digest,
    the next run builds the replace form to compare against it, the two differ, and a
    replacement migration is written on every run.
    """
    digest = _sql_digest(forward, reverse)
    source = (
        f'{header} [SQL:{digest}]\n'
        f'migrations.RunSQL(\n'
        f'    sql={_sql_literal(emit if emit is not None else forward)},\n'
        f'    reverse_sql={_sql_literal(reverse)},\n'
        f'),\n'
    )
    return source, digest


def _recorded_line(content: str, match: re.Match) -> str:
    """The full text of the line *match* landed on, from its start to the next newline."""
    line_end = content.find('\n', match.start())
    return content[match.start() : line_end if line_end != -1 else len(content)]


def _recorded_sql_identity(content: str, match: re.Match) -> str | None:
    """The ``[SQL:...]`` digest on the header line *match* landed on, or ``None``.

    ``None`` covers both "no token" (a pre-inlining migration) and a header this token was
    never written to. Callers treat it as stale rather than as covered, which is the whole
    point: the previous behaviour was to treat any recognised header as current forever.
    """
    found = _RE_SQL_IDENTITY.search(_recorded_line(content, match))
    return found.group('sql') if found else None


def _recorded_policy_identity(content: str, match: re.Match) -> str | None:
    """The ``[POLICY:...]`` identity on the header line *match* landed on, or ``None``."""
    found = _RE_POLICY_IDENTITY.search(_recorded_line(content, match))
    return found.group('identity') if found else None


def unforced_policy_tables(content: str, matches: list[re.Match]) -> set[str]:
    """Tables whose policy operation in *content* was written without FORCE.

    *matches* is every ``_RE_TENANT_POLICY`` match in *content*, computed by the caller and
    passed in rather than re-found here: the caller (:func:`guitars.management.enforcement.
    scanning.scan_existing_operations`) already scans for the same pattern to build
    ``existing_tenant_policies``, and finding it a second time here doubled the cost of
    every scan for no reason.

    The only kind ``--force-rls`` has anything to do for: the migration text is the record
    of what was decided when it was generated. Without this the flag emitted a redundant FORCE
    migration for every tenanted table on any project using the default
    ``GUITARS_RLS_FORCE = True``.

    Two formats, because both exist in the wild. A migration written before the SQL was
    inlined records the decision as a ``force=False`` keyword argument; one written since
    records it by containing -- or not containing -- an
    ``ALTER TABLE ... FORCE ROW LEVEL SECURITY`` statement. Reading only the second would
    make every legacy operation look unforced and put already-forced tables back on the
    FORCE backlog, so the keyword is honoured wherever it is still present.

    Each operation is inspected **only within its own text**, bounded by the next policy header.
    A single regex spanning a few lines after the header cannot do this: operations are about
    that long, so a lazy match from a ``force=True`` operation reaches into the next one, claims
    *its* ``force=False``, and consumes the header on the way -- flagging the table that is
    already forced and missing the one that is not. Exactly backwards, and only on a file with
    two adjacent operations, which is every real file.

    Within a file the **last** operation for a table wins, the same rule the caller applies
    across files. Accumulating into a set instead would let a table that shipped
    ``force=False`` and was later replaced with ``force=True`` in the same file stay on the
    FORCE backlog forever -- a redundant migration for a table that is already forced, which
    is the bug this function was extracted to fix, one scope smaller.
    """
    state: dict[str, bool] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        operation = content[match.end() : end]
        state[match.group(1)] = (
            'force=False' in operation
            if 'force=' in operation
            else not _RE_FORCED.search(operation)
        )
    return {table for table, unforced in state.items() if unforced}


def _literal(value: object) -> str:
    """Render a value into a generated migration, deterministically.

    Dicts and sets are emitted in sorted order so the content digest is stable. An unstable
    rendering produces a new digest -- and therefore a new migration -- on every run.

    Scalars (strings included) go through ``repr``, which is what makes the output valid
    Python for every value rather than only for the tidy ones: hand-building a string as
    ``f"'{value}'"`` renders ``db_column="o'brien"`` as a syntax error and a backslash as an
    escape sequence. For every identifier the kit accepts -- ``sql.policy._bare`` requires a
    plain lower-case one -- ``repr`` produces the identical single-quoted text, so digests of
    existing migrations are unchanged.
    """
    if isinstance(value, dict):
        items = ', '.join(f'{_literal(k)}: {_literal(v)}' for k, v in sorted(value.items()))
        return '{' + items + '}'
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(_literal(item) for item in value) + ']'
    return repr(value)
