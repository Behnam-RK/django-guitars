"""SQL identifier/literal quoting, shared by every ``sql`` submodule. Internal to
:mod:`guitars.sql` -- never re-export a name via ``sql/__init__.py``'s ``__all__``."""

from __future__ import annotations

import hashlib
import re


#: A bare SQL identifier: safe to interpolate unquoted, case-stable because PostgreSQL
#: folds an unquoted identifier to lower case. Anything else must be quoted to work at all.
_BARE_IDENTIFIER = re.compile(r'^[a-z_][a-z0-9_$]*$')


def _bare(kind: str, name: str) -> str:
    """Return *name* unchanged, having proved it needs no quoting -- not an injection
    boundary (values are Django-derived) but a correctness one: unquoted, a hostile name
    binds the wrong table. Raising here catches that at build time."""
    if not _BARE_IDENTIFIER.match(name):
        raise ValueError(
            f'{kind} {name!r} is not a plain lower-case SQL identifier, so it cannot be '
            f'used in a policy definition unquoted. Rename it, or set an explicit '
            f'db_table / db_column that is one.'
        )
    return name


#: Django's schema-qualified ``db_table`` convention: two double-quoted parts joined by a
#: bare ``.``. ``(?:[^"]|"")*`` allows a doubled ``""`` the way Postgres's own escape works.
_QUOTED_QUALIFIED = re.compile(r'^"((?:[^"]|"")*)"\.\"((?:[^"]|"")*)"$')

#: Django's single-part self-quoting convention -- matches the *whole* string, unlike the
#: looser :func:`_is_self_quoted`, which can't tell a malformed 3-part string from a
#: legitimate one-part quoted identifier that happens to contain a literal ``.``.
_QUOTED_UNQUALIFIED = re.compile(r'^"((?:[^"]|"")*)"$')


def _split_qualified(kind: str, name: str) -> tuple[str | None, str]:
    """Return ``(schema, table)`` for a possibly schema-qualified identifier, unvalidated
    (only :func:`_bare_or_qualified` validates). Order matters: Django's pre-quoted form,
    then self-quoted, then bare ``schema.table`` -- a second ``.`` is rejected."""
    quoted = _QUOTED_QUALIFIED.match(name)
    if quoted is not None:
        schema_part, table_part = quoted.groups()
        return schema_part.replace('""', '"'), table_part.replace('""', '"')
    self_quoted = _QUOTED_UNQUALIFIED.match(name)
    if self_quoted is not None:
        return None, self_quoted.group(1).replace('""', '"')
    schema, sep, rest = name.partition('.')
    if not sep:
        return None, name
    if '.' in rest:
        raise ValueError(
            f'{kind} {name!r} has more than one schema-qualifying "." -- only a single '
            f'"schema.table" shape (or Django\'s own quoted \'"schema"."table"\' form) is '
            f'supported.'
        )
    return schema, rest


def _bare_or_qualified(kind: str, name: str) -> tuple[str | None, str]:
    """Like :func:`_split_qualified`, but validates unquoted parts via :func:`_bare` -- for
    a caller that interpolates the result *unquoted*. A caller that quotes/escapes
    regardless of content should call :func:`_split_qualified` directly instead."""
    quoted = _QUOTED_QUALIFIED.match(name) is not None
    schema, table = _split_qualified(kind, name)
    if schema is None:
        return None, _bare(kind, table)
    if quoted:
        return schema, table
    return _bare(f'{kind} schema', schema), _bare(kind, table)


def _escape_ident(name: str) -> str:
    """Escape an identifier's *inner* content, without wrapping it in quotes -- for a
    template that already owns its surrounding ``"..."`` (double-wrapping otherwise).
    """
    if '\x00' in name:
        raise ValueError('SQL identifiers cannot contain a NUL byte.')
    return name.replace('"', '""')


def _unescape_ident(escaped: str) -> str:
    """Inverse of :func:`_escape_ident`; used reading a header value back byte-for-byte --
    see ``enforcement.headers``'s module docstring on why that round-trip must hold."""
    return escaped.replace('""', '"')


def _quote_ident(name: str) -> str:
    """Double-quote an identifier, PostgreSQL's ``quote_ident`` -- for free-form role
    names, not Django-derived ones: ``BI_Reader`` folds to ``bi_reader`` unquoted."""
    return '"' + _escape_ident(name) + '"'


def _quote_qualified(schema: str | None, name: str) -> str:
    """Two-part-safe identifier quoting: each part quoted independently, since quoting
    ``"schema.table"`` whole produces one wrong relation name, not two."""
    if schema is None:
        return _quote_ident(name)
    return f'{_quote_ident(schema)}.{_quote_ident(name)}'


#: Django's single-part self-quoting convention: a ``db_table`` the caller already wrapped
#: in ``"..."``, so ``connection.ops.quote_name()`` leaves it alone on every ORM query.
#: ``len(name) >= 2`` excludes a lone ``"`` satisfying both ``startswith``/``endswith``.
def _is_self_quoted(name: str) -> bool:
    return len(name) >= 2 and name.startswith('"') and name.endswith('"')


def _quote_table(name: str) -> str:
    """Full DDL-ready identifier for a possibly schema-qualified table name. Self-quoted
    passes unchanged (re-quoting would double-wrap it wrong); no ``.`` quotes as one blob;
    otherwise :func:`_bare_or_qualified`. See ADR-0007 for the ORM ``quote_name()`` trap."""
    if _QUOTED_UNQUALIFIED.match(name) is not None:
        return name
    if '.' not in name:
        return _quote_ident(name)
    schema, table = _bare_or_qualified('table', name)
    return _quote_qualified(schema, table)


def _escape_literal(value: str) -> str:
    """Escape a string literal's *inner* content, without wrapping it in quotes -- for a
    template that already owns its surrounding ``'...'`` (double-wrapping otherwise).
    """
    if '\x00' in value:
        raise ValueError('SQL string literals cannot contain a NUL byte.')
    return value.replace("'", "''")


def _quote_literal(value: str) -> str:
    """Single-quote a string literal, PostgreSQL's ``quote_literal``."""
    return "'" + _escape_literal(value) + "'"


def _truncate_utf8(candidate: str, max_bytes: int) -> str:
    """Truncate to at most *max_bytes* UTF-8 bytes without splitting a character -- Postgres
    counts NAMEDATALEN in bytes, not code points, so naive ``candidate[:n]`` can misfire."""
    if max_bytes <= 0:
        return ''
    raw = candidate.encode('utf-8')
    if len(raw) <= max_bytes:
        return candidate
    return raw[:max_bytes].decode('utf-8', errors='ignore')


def _safe_identifier(candidate: str, max_bytes: int = 63) -> str:
    """Truncate to PostgreSQL's 63-byte NAMEDATALEN limit, appending a content-hash suffix
    (hashed over the *full* candidate) so long names sharing a prefix don't collide."""
    raw = candidate.encode('utf-8')
    if len(raw) <= max_bytes:
        return candidate
    digest = hashlib.sha256(candidate.encode('utf-8')).hexdigest()[:10]
    budget = max_bytes - 1 - len(digest)  # 1 byte for the '_' separator
    base = _truncate_utf8(candidate, budget)
    return f'{base}_{digest}'


def _safe_ident(candidate: str) -> str:
    """``_safe_identifier`` then ``_quote_ident`` in one call, truncating *before* quoting
    so a hostile part's doubled inner quotes can't skew the 63-byte budget.
    """
    return _quote_ident(_safe_identifier(candidate))
