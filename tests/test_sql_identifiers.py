"""Direct coverage for ``guitars.sql._identifiers``, independent of any specific emitted
statement.

``tests/test_tenancy_internals.py`` already covers ``_quote_literal``'s NUL-byte guard via
``guitars.sql.policy`` (its only reachable caller); this file is for the helpers -- and the
new M4 additions -- that don't yet have a single obvious call site to hang a test off of.
"""

from __future__ import annotations

import pytest

from guitars.sql import _identifiers


class TestSplitQualified:
    """The permissive parser underneath ``_bare_or_qualified`` -- same shape recognition,
    no content validation. ``operations.py`` uses this directly (not ``_bare_or_qualified``)
    for a value it goes on to quote or escape itself regardless of content: a cascade rule
    name (``_related_rule_name``, always quoted via ``_quote_ident``) and an MTI ancestor's
    schema/table (always an escaped literal argument, re-quoted by PostgreSQL's own ``%I``
    at trigger-fire time). Neither position is rendered unquoted, so neither should reject a
    legal-but-hostile, unqualified ``db_table`` the way an unquoted position must.
    """

    def test_unqualified_name_is_returned_as_is(self):
        assert _identifiers._split_qualified('table', 'events') == (None, 'events')

    def test_unqualified_hostile_name_is_not_rejected(self):
        """The exact gap this function closes: ``_bare_or_qualified`` raises for this same
        input (see ``TestBareOrQualified.test_hostile_unqualified_name_is_rejected`` below),
        but a caller that quotes/escapes the result itself never needed that rejection.
        """
        assert _identifiers._split_qualified('table', 'Order Items') == (None, 'Order Items')

    def test_schema_qualified_name_splits_in_two(self):
        assert _identifiers._split_qualified('table', 'analytics.events') == (
            'analytics',
            'events',
        )

    def test_hostile_qualified_parts_are_not_rejected(self):
        assert _identifiers._split_qualified('table', 'Analytics.My Events') == (
            'Analytics',
            'My Events',
        )

    def test_two_dots_are_still_rejected(self):
        """The second-'.' rejection is a structural question, not a content one, so it stays
        even in the unvalidated parser.
        """
        with pytest.raises(ValueError, match='more than one schema-qualifying'):
            _identifiers._split_qualified('table', 'a.b.c')

    def test_djangos_pre_quoted_form_splits_in_two(self):
        assert _identifiers._split_qualified('table', '"analytics"."events"') == (
            'analytics',
            'events',
        )


class TestBareOrQualified:
    def test_unqualified_name_behaves_like_bare(self):
        assert _identifiers._bare_or_qualified('table', 'events') == (None, 'events')

    def test_hostile_unqualified_name_is_rejected(self):
        """Unlike ``_split_qualified``, this function's result is meant for an unquoted
        interpolation position (``_quote_table``'s dotted branch, ``policy.py``'s
        ``_qualified_table``), so it must still reject what ``_bare()`` always has.
        """
        with pytest.raises(ValueError, match='not a plain lower-case SQL identifier'):
            _identifiers._bare_or_qualified('table', 'Order Items')

    def test_schema_qualified_name_splits_in_two(self):
        assert _identifiers._bare_or_qualified('table', 'analytics.events') == (
            'analytics',
            'events',
        )

    def test_two_dots_are_rejected(self):
        with pytest.raises(ValueError, match='more than one schema-qualifying'):
            _identifiers._bare_or_qualified('table', 'a.b.c')

    def test_uppercase_schema_is_rejected(self):
        with pytest.raises(ValueError, match='not a plain lower-case SQL identifier'):
            _identifiers._bare_or_qualified('table', 'Analytics.events')

    def test_empty_schema_is_rejected(self):
        with pytest.raises(ValueError, match='not a plain lower-case SQL identifier'):
            _identifiers._bare_or_qualified('table', '.events')

    def test_djangos_pre_quoted_form_splits_in_two(self):
        """'"schema"."table"' is Django's own db_table convention for a table meant to be
        read and written through the ORM -- see _quote_table's docstring for why the bare
        'schema.table' form above can never work for that.
        """
        assert _identifiers._bare_or_qualified('table', '"analytics"."events"') == (
            'analytics',
            'events',
        )

    def test_pre_quoted_form_allows_content_bare_would_reject(self):
        """Quoting is what makes an otherwise-hostile part safe -- unlike the bare form,
        mixed case and embedded spaces need no rejection here.
        """
        assert _identifiers._bare_or_qualified('table', '"Analytics"."My Events"') == (
            'Analytics',
            'My Events',
        )

    def test_pre_quoted_form_unescapes_a_doubled_quote(self):
        assert _identifiers._bare_or_qualified('table', '"weird""schema"."events"') == (
            'weird"schema',
            'events',
        )

    def test_three_pre_quoted_parts_are_rejected(self):
        with pytest.raises(ValueError, match='more than one schema-qualifying'):
            _identifiers._bare_or_qualified('table', '"a"."b"."c"')


class TestQuoteQualified:
    def test_unqualified_quotes_one_part(self):
        assert _identifiers._quote_qualified(None, 'events') == '"events"'

    def test_qualified_quotes_both_parts_joined_by_a_bare_dot(self):
        assert _identifiers._quote_qualified('analytics', 'events') == '"analytics"."events"'


class TestQuoteTable:
    def test_unqualified_name_is_quoted_as_one_opaque_identifier(self):
        assert _identifiers._quote_table('events') == '"events"'

    def test_unqualified_hostile_name_is_still_quoted_not_rejected(self):
        """No shape validation for the no-dot branch -- see the docstring: nothing about
        adding schema support should make an already-working unqualified db_table fail.
        """
        assert _identifiers._quote_table('Order Items') == '"Order Items"'

    def test_bare_qualified_name_renders_as_two_quoted_parts(self):
        assert _identifiers._quote_table('analytics.events') == '"analytics"."events"'

    def test_pre_quoted_qualified_name_round_trips(self):
        assert _identifiers._quote_table('"analytics"."events"') == '"analytics"."events"'

    def test_bare_qualified_name_with_a_hostile_part_raises(self):
        with pytest.raises(ValueError, match='not a plain lower-case SQL identifier'):
            _identifiers._quote_table('Analytics.events')

    def test_unqualified_self_quoted_name_round_trips_unchanged(self):
        """Regression: before this, the no-dot branch re-quoted an already-quoted name
        (Django's own single-part pre-quoting convention -- see _is_self_quoted's
        docstring), double-wrapping it into a different, wrong identifier. A self-quoted
        db_table worked by accident pre-M4 (a raw, unvalidated .format() let it straight
        through); this keeps that working on purpose.
        """
        assert _identifiers._quote_table('"Order Items"') == '"Order Items"'

    def test_unqualified_self_quoted_name_with_an_embedded_escaped_quote_round_trips(self):
        assert _identifiers._quote_table('"weird""table"') == '"weird""table"'


class TestIsSelfQuoted:
    def test_plain_name_is_not_self_quoted(self):
        assert _identifiers._is_self_quoted('events') is False

    def test_wrapped_name_is_self_quoted(self):
        assert _identifiers._is_self_quoted('"Order Items"') is True

    def test_a_single_quote_character_is_not_self_quoted(self):
        """The degenerate one-character case: '"' satisfies both startswith('"') and
        endswith('"') on its own, but is not a wrapped pair.
        """
        assert _identifiers._is_self_quoted('"') is False

    def test_empty_string_is_not_self_quoted(self):
        assert _identifiers._is_self_quoted('') is False


class TestEscapeIdent:
    def test_returns_unwrapped_content(self):
        assert _identifiers._escape_ident('plain') == 'plain'

    def test_doubles_embedded_double_quotes(self):
        assert _identifiers._escape_ident('weird"table') == 'weird""table'

    def test_rejects_a_nul_byte(self):
        with pytest.raises(ValueError, match='identifiers cannot contain a NUL byte'):
            _identifiers._escape_ident('bad\x00value')

    def test_quote_ident_wraps_the_escaped_content(self):
        assert _identifiers._quote_ident('weird"table') == '"weird""table"'


class TestUnescapeIdent:
    """The inverse of _escape_ident, used to recover a header's original table name --
    see headers.py's module docstring on why the header must round-trip byte-for-byte.
    """

    def test_returns_content_with_no_doubled_quote_unchanged(self):
        assert _identifiers._unescape_ident('plain') == 'plain'

    def test_undoubles_an_escaped_quote(self):
        assert _identifiers._unescape_ident('weird""table') == 'weird"table'

    def test_round_trips_with_escape_ident(self):
        original = '"analytics"."events"'
        assert _identifiers._unescape_ident(_identifiers._escape_ident(original)) == original


class TestEscapeLiteral:
    def test_returns_unwrapped_content(self):
        assert _identifiers._escape_literal('plain') == 'plain'

    def test_doubles_embedded_single_quotes(self):
        assert _identifiers._escape_literal("O'Brien") == "O''Brien"

    def test_rejects_a_nul_byte(self):
        with pytest.raises(ValueError, match='string literals cannot contain a NUL byte'):
            _identifiers._escape_literal('bad\x00value')


class TestTruncateUtf8:
    def test_short_string_is_unchanged(self):
        assert _identifiers._truncate_utf8('short', 63) == 'short'

    def test_cuts_on_a_byte_boundary_not_mid_character(self):
        # Each 'é' is 2 bytes in UTF-8; a budget of 3 bytes must not split the second one.
        result = _identifiers._truncate_utf8('éé', 3)
        assert result.encode('utf-8') == 'é'.encode()

    def test_zero_budget_returns_empty(self):
        assert _identifiers._truncate_utf8('anything', 0) == ''


class TestSafeIdentifier:
    def test_short_candidate_is_returned_unchanged(self):
        assert _identifiers._safe_identifier('soft_delete_related_events') == (
            'soft_delete_related_events'
        )

    def test_long_candidate_is_truncated_to_the_byte_budget(self):
        candidate = 'soft_delete_related_' + 'x' * 60
        result = _identifiers._safe_identifier(candidate)
        assert len(result.encode('utf-8')) <= 63
        assert result.endswith(_identifiers.hashlib.sha256(candidate.encode()).hexdigest()[:10])

    def test_two_long_names_sharing_a_prefix_produce_distinct_results(self):
        prefix = 'soft_delete_related_' + 'x' * 50
        first = _identifiers._safe_identifier(prefix + '_orders')
        second = _identifiers._safe_identifier(prefix + '_invoices')
        assert first != second
        assert len(first.encode('utf-8')) <= 63
        assert len(second.encode('utf-8')) <= 63

    def test_divergence_only_in_the_truncated_tail_still_produces_distinct_results(self):
        """The hash is over the *full* candidate, so two names differing only past the
        truncation point must not collide -- hashing just the kept prefix would defeat
        the whole point of this function.
        """
        long_prefix = 'a' * 80
        first = _identifiers._safe_identifier(long_prefix + '_one')
        second = _identifiers._safe_identifier(long_prefix + '_two')
        assert first != second

    def test_multi_byte_boundary_candidate_round_trips_cleanly(self):
        candidate = 'é' * 40  # 80 bytes, over the 63-byte limit
        result = _identifiers._safe_identifier(candidate)
        assert len(result.encode('utf-8')) <= 63
        result.encode('utf-8').decode('utf-8')  # must not raise


class TestSafeIdent:
    """``_safe_identifier`` then ``_quote_ident`` in one call -- the shared helper
    ``policy._exempt_policy_name`` and ``operations._related_rule_name`` each use for a
    derived, unbounded-length name.
    """

    def test_short_candidate_is_truncated_then_quoted(self):
        assert _identifiers._safe_ident('rls_exempt_reporting') == '"rls_exempt_reporting"'

    def test_long_candidate_is_truncated_before_being_quoted(self):
        candidate = 'rls_exempt_' + 'x' * 60
        result = _identifiers._safe_ident(candidate)
        # Quoted, and the unquoted content is exactly what _safe_identifier would produce.
        assert result == f'"{_identifiers._safe_identifier(candidate)}"'
        assert len(result) <= 65  # 63-byte identifier + 2 quote characters

    def test_a_hostile_candidate_is_quoted_rather_than_rejected(self):
        assert _identifiers._safe_ident('rls_exempt_Weird Role') == '"rls_exempt_Weird Role"'
