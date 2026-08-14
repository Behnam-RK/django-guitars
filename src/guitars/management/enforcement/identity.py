"""Rendering an operation's SQL, and reading back the identity tokens on its header --
:func:`_operation` renders a ``RunSQL`` snippet and stamps a ``[SQL:...]`` digest;
:func:`_recorded_sql_identity`/:func:`_recorded_policy_identity` read one back."""

from __future__ import annotations

import re

from guitars.management import _generator
from guitars.management.enforcement.headers import (
    _RE_FORCED,
    _RE_POLICY_IDENTITY,
    _RE_SQL_IDENTITY,
)
from guitars.sql import _identifiers


def _as_list(statements: str | list[str]) -> list[str]:
    return [statements] if isinstance(statements, str) else list(statements)


def _sql_string_literal(text: str) -> str:
    """One SQL statement as a Python literal, triple-quoted where the text allows it --
    ``repr`` collapses a twenty-line rule to one line of ``\\n`` a reviewer can't read."""
    if '"""' in text or '\\' in text or text.endswith('"'):
        return repr(text)
    return f'"""{text}"""'


def _sql_literal(statements: str | list[str]) -> str:
    """Rendered SQL as a Python literal, for embedding in a generated migration -- carried
    literally, never as ``from guitars import sql`` naming a constant. See ADR-0006."""
    if isinstance(statements, str):
        return _sql_string_literal(statements)
    inner = ''.join(f'        {_sql_string_literal(item)},\n' for item in statements)
    return f'[\n{inner}    ]'


def _sql_digest(forward: str | list[str], reverse: str | list[str]) -> str:
    """Content digest of one operation's SQL, stamped as ``[SQL:...]`` on the header, not
    the file-level ``[DIGEST:...]`` (the per-table scan short-circuits first). Always the
    **canonical** (create) form, never replace/adopt -- see :func:`_operation`."""
    return _generator.digest_of([*_as_list(forward), '--', *_as_list(reverse)])[:12]


def _operation(
    header: str,
    forward: str | list[str],
    reverse: str | list[str],
    *,
    emit: str | list[str] | None = None,
) -> tuple[str, str]:
    """Render one ``RunSQL`` operation, returning ``(source, digest)``. *emit* substitutes
    the replace/adopt form actually written, while the digest stays keyed to the canonical
    *forward* -- digesting the emitted form instead makes successive runs disagree forever."""
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
    """The ``[SQL:...]`` digest on the header line *match* landed on, or ``None`` -- covers
    both "no token" and "pre-inlining migration". Callers treat it as stale, never covered."""
    found = _RE_SQL_IDENTITY.search(_recorded_line(content, match))
    return found.group('sql') if found else None


def _recorded_policy_identity(content: str, match: re.Match) -> str | None:
    """The ``[POLICY:...]`` identity on the header line *match* landed on, or ``None``."""
    found = _RE_POLICY_IDENTITY.search(_recorded_line(content, match))
    return found.group('identity') if found else None


def unforced_policy_tables(content: str, matches: list[re.Match]) -> set[str]:
    """Tables whose policy operation in *content* was written without FORCE. Each operation
    is inspected only within its own text, bounded by the next header -- an unbounded regex
    would claim the next operation's ``force=False`` as its own. Last operation wins."""
    state: dict[str, bool] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        operation = content[match.end() : end]
        # _unescape_ident: match.group(1) is the header's escaped form; the caller compares
        # this set against scanning.py's already-unescaped table names.
        state[_identifiers._unescape_ident(match.group(1))] = (
            'force=False' in operation
            if 'force=' in operation
            else not _RE_FORCED.search(operation)
        )
    return {table for table, unforced in state.items() if unforced}


def _literal(value: object) -> str:
    """Render a value into a generated migration, deterministically -- dicts/sets sorted so
    the digest is stable; scalars go through ``repr`` so ``db_column="o'brien"`` and a
    backslash render as valid Python rather than a syntax error."""
    if isinstance(value, dict):
        items = ', '.join(f'{_literal(k)}: {_literal(v)}' for k, v in sorted(value.items()))
        return '{' + items + '}'
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(_literal(item) for item in value) + ']'
    return repr(value)
