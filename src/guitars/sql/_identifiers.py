"""SQL identifier and literal quoting, shared by every ``sql`` submodule.

Internal to :mod:`guitars.sql` on purpose: nothing outside the package needs to escape
its own generated DDL, and re-exporting these publicly would extend the frozen-interface
obligation (see the package docstring) to implementation details that have no reason to
be stable API. Import from here within ``sql/``; never add a name from this module to
``sql/__init__.py``'s ``__all__``.
"""

from __future__ import annotations

import hashlib
import re


#: A bare SQL identifier: safe to interpolate unquoted, and case-stable because PostgreSQL
#: folds an unquoted identifier to lower case. Anything outside this is either a mistake or
#: something that has to be quoted to work at all.
_BARE_IDENTIFIER = re.compile(r'^[a-z_][a-z0-9_$]*$')


def _bare(kind: str, name: str) -> str:
    """Return *name* unchanged, having proved it needs no quoting.

    Nothing untrusted reaches these functions -- tables, columns and primary keys are
    resolved from Django's ``model._meta``, and the result is written into a migration file
    for review -- so this is not an injection boundary. It is a *correctness* one:
    ``db_table = 'Order Items'`` is legal Django, and interpolating it bare produces SQL
    that fails at ``migrate`` time or, worse, binds a different table than the one named.

    Raising here moves that from a puzzling migrate-time error to a build-time one that
    names the setting or field responsible.
    """
    if not _BARE_IDENTIFIER.match(name):
        raise ValueError(
            f'{kind} {name!r} is not a plain lower-case SQL identifier, so it cannot be '
            f'used in a policy definition unquoted. Rename it, or set an explicit '
            f'db_table / db_column that is one.'
        )
    return name


#: Django's own convention for a schema-qualified ``db_table``: each side already
#: double-quoted, joined by a bare ``.`` -- see ``_bare_or_qualified``'s docstring for why
#: this, and not an unquoted ``schema.table``, is the form a *Django-ORM-usable* model
#: needs. ``(?:[^"]|"")*`` inside each side allows an embedded, doubled ``""`` the same way
#: Postgres's own identifier-quoting escape works.
_QUOTED_QUALIFIED = re.compile(r'^"((?:[^"]|"")*)"\.\"((?:[^"]|"")*)"$')


def _split_qualified(name: str) -> tuple[str | None, str]:
    """Return ``(schema, table)`` for a possibly schema-qualified identifier, performing no
    content validation on either part -- only :func:`_bare_or_qualified` validates; this is
    the shared parser underneath it, and the entry point for a caller that has no unquoted
    interpolation of its own to protect (see that function's docstring for why one exists).

    Two ways *name* can be schema-qualified, checked in this order:

    1. **Django's own pre-quoted convention**, ``'"schema"."table"'`` -- recognised first
       because it is the *only* ``db_table`` shape for which Django's own query compiler
       (which calls ``connection.ops.quote_name()`` on ``db_table`` for every ORM query,
       and passes an already ``"``-wrapped string through unchanged) and this kit's
       generated SQL agree on the same physical relation. A model meant to be read and
       written through the Django ORM, not just have enforcement SQL generated for it,
       needs this form -- see ``_quote_table``'s docstring for what breaks otherwise.
       Each side's quoted content is returned as-is (unescaping a doubled ``""``).
    2. **A bare, unquoted ``schema.table``**, split on the first ``.``. Lighter-weight than
       the quoted form, for a table this project's ORM never queries directly (only
       enforcement SQL is generated against it). A second ``.`` describes a shape
       (``a.b.c``) neither form supports and is rejected outright -- that much is a
       structural question, not a content one, so it is checked here rather than left to
       each caller.
    """
    quoted = _QUOTED_QUALIFIED.match(name)
    if quoted is not None:
        schema_part, table_part = quoted.groups()
        return schema_part.replace('""', '"'), table_part.replace('""', '"')
    schema, sep, rest = name.partition('.')
    if not sep:
        return None, name
    if '.' in rest:
        raise ValueError(
            f'{name!r} has more than one schema-qualifying "." -- only a single '
            f'"schema.table" shape (or Django\'s own quoted \'"schema"."table"\' form) is '
            f'supported.'
        )
    return schema, rest


def _bare_or_qualified(kind: str, name: str) -> tuple[str | None, str]:
    """Return ``(schema, table)`` for a possibly schema-qualified identifier, validating any
    part that is not already quoted.

    Built on :func:`_split_qualified` for the shape (see its docstring for the two forms
    recognised and the second-``.`` rejection). What this adds is validation of the *content*
    of a part that came back unquoted:

    * Django's pre-quoted ``'"schema"."table"'`` form needs none -- quoting is what makes an
      otherwise-hostile schema or table name safe here, the same permissive reasoning
      ``_quote_table``'s no-dot branch already uses for an unqualified name.
    * A bare, unquoted part is validated via :func:`_bare`, so a hostile part fails at build
      time rather than producing DDL that parses but binds the wrong relation.

    ``_bare()`` itself stays single-part-only (see its docstring) so every call site that
    predates schema-qualified support keeps behaving exactly as before; this is the
    schema-aware entry point new call sites opt into -- specifically, ones that go on to
    interpolate the returned parts *unquoted* (:func:`_quote_table`'s dotted branch,
    ``sql/policy.py``'s ``_qualified_table``). A caller that quotes or escapes the result
    itself regardless of content (a rule name, a trigger-function literal argument later
    re-quoted by PostgreSQL's own ``%I``) wants :func:`_split_qualified` directly instead --
    this function's validation would otherwise reject a legal, unqualified ``db_table`` like
    ``'Order Items'`` that caller was never going to render unquoted in the first place.
    """
    quoted = _QUOTED_QUALIFIED.match(name) is not None
    schema, table = _split_qualified(name)
    if schema is None:
        return None, _bare(kind, table)
    if quoted:
        return schema, table
    return _bare(f'{kind} schema', schema), _bare(kind, table)


def _escape_ident(name: str) -> str:
    """Escape an identifier's *inner* content, without wrapping it in quotes.

    For templates that already own their surrounding ``"..."`` (e.g. the trigger and
    soft-delete templates' quoted ``ON "{table}"`` positions) -- wrapping again here would
    double the quotes. Same escaping rule as :func:`_quote_ident`, split out for the same
    reason :func:`_escape_literal` is: a value substituted into an already-quoted template
    position needs escaping but not another layer of wrapping.
    """
    if '\x00' in name:
        raise ValueError('SQL identifiers cannot contain a NUL byte.')
    return name.replace('"', '""')


def _quote_ident(name: str) -> str:
    """Double-quote an identifier, PostgreSQL's ``quote_ident``.

    Used for role-derived names, which -- unlike tables and columns -- are free-form
    ``settings`` text rather than something Django derived. ``BI_Reader`` and
    ``metabase-ro`` are both perfectly ordinary PostgreSQL roles that only bind when
    quoted; bare, the first silently becomes ``bi_reader`` and the second is a syntax
    error.
    """
    return '"' + _escape_ident(name) + '"'


def _quote_qualified(schema: str | None, name: str) -> str:
    """Two-part-safe identifier quoting, joined by the literal ``.`` PostgreSQL requires
    unquoted between a schema and its table.

    Each part is quoted independently. Quoting the whole ``"schema.table"`` string as one
    identifier -- the easy mistake -- produces a single, wrong relation name rather than a
    schema-qualified reference to two.
    """
    if schema is None:
        return _quote_ident(name)
    return f'{_quote_ident(schema)}.{_quote_ident(name)}'


def _quote_table(name: str) -> str:
    """Full DDL-ready identifier for a *possibly* schema-qualified table name.

    Two behaviours, deliberately different, split on whether *name* contains a ``.`` at
    all:

    * **No ``.``**: quoted as one opaque identifier via :func:`_quote_ident`, with no shape
      validation -- the permissive, whole-string-blob quoting the trigger/rule templates
      have used since quoting was first added to them. An unqualified ``db_table`` with
      reserved-word or mixed-case content (``'Order Items'``) keeps working exactly as
      before; nothing about adding schema support should make that *narrower*.
    * **Contains a ``.``**: routed through :func:`_bare_or_qualified`, which recognises
      either Django's own pre-quoted ``'"schema"."table"'`` convention or a bare
      ``'schema.table'``, and raises a build-time ``ValueError`` for a hostile bare part or
      a second ``.``. A two-part reference cannot be permissively quoted the way a single
      opaque string can -- ``"schema.table"`` quoted whole is one wrong identifier, and
      correctly splitting it into two requires knowing where the boundary actually is.

    A model meant to be read and written through the Django ORM -- not just have
    enforcement SQL generated for it -- must use the pre-quoted form: Django's own query
    compiler calls ``connection.ops.quote_name()`` on ``db_table`` for *every* ORM query
    (not only the ``CREATE TABLE`` migrations write), and that function only passes a
    string through unchanged when it already starts and ends with ``"``. An unquoted
    ``'schema.table'`` therefore gets wrapped by Django itself as one wrong identifier on
    every ORM read or write, regardless of what this kit emits -- it only ever works for a
    table this project's ORM never queries directly.
    """
    if '.' not in name:
        return _quote_ident(name)
    schema, table = _bare_or_qualified('table', name)
    return _quote_qualified(schema, table)


def _escape_literal(value: str) -> str:
    """Escape a string literal's *inner* content, without wrapping it in quotes.

    For templates that already own their surrounding ``'...'`` (e.g. the trigger
    templates' ``'{primary_key}'`` argument) -- wrapping again here would double the
    quotes. Same escaping rules as :func:`_quote_literal`, split out because the two
    nesting levels -- a whole statement, versus a value substituted into a position that
    is already quoted -- need a different amount of wrapping.
    """
    if '\x00' in value:
        raise ValueError('SQL string literals cannot contain a NUL byte.')
    return value.replace("'", "''")


def _quote_literal(value: str) -> str:
    """Single-quote a string literal, PostgreSQL's ``quote_literal``.

    Applied to the whole ``EXECUTE`` payload as well as to individual values, so the two
    nesting levels inside a ``DO`` block each get escaped exactly once.
    """
    return "'" + _escape_literal(value) + "'"


def _truncate_utf8(candidate: str, max_bytes: int) -> str:
    """Truncate *candidate* to at most *max_bytes* UTF-8 bytes without splitting a character.

    PostgreSQL counts NAMEDATALEN in bytes, not code points, so a naive ``candidate[:n]``
    can cut a multi-byte character in half. Encode, cut on the byte boundary, and decode
    dropping a partial trailing character rather than raising on it.
    """
    if max_bytes <= 0:
        return ''
    raw = candidate.encode('utf-8')
    if len(raw) <= max_bytes:
        return candidate
    return raw[:max_bytes].decode('utf-8', errors='ignore')


def _safe_identifier(candidate: str, max_bytes: int = 63) -> str:
    """Truncate *candidate* to PostgreSQL's NAMEDATALEN (63-byte) identifier limit.

    A name at or under the limit is returned unchanged, so this only ever affects the rare
    long-name case -- most rule/policy names never grow past 63 bytes, and this must not
    change what they render as.

    Over the limit, the name is cut short and a content-hash suffix is appended, so two
    long candidates that only *start* identically -- including ones that diverge somewhere
    in the part that would otherwise be cut away -- still produce distinct results. The
    hash is computed over the *full, untruncated* candidate for exactly that reason: hashing
    only the truncated remainder would let two names sharing a long common prefix collide
    again, defeating the point.

    Ten hex characters (40 bits) of SHA-256: this runs at build time over a handful of
    human-reviewed migration statements, not a hot path, so there is no reason to shave
    the suffix shorter at the cost of collision odds.
    """
    raw = candidate.encode('utf-8')
    if len(raw) <= max_bytes:
        return candidate
    digest = hashlib.sha256(candidate.encode('utf-8')).hexdigest()[:10]
    budget = max_bytes - 1 - len(digest)  # 1 byte for the '_' separator
    base = _truncate_utf8(candidate, budget)
    return f'{base}_{digest}'
