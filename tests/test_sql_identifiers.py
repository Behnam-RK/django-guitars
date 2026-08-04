"""Direct coverage for ``guitars.sql._identifiers``, independent of any specific emitted
statement.

``tests/test_tenancy_internals.py`` already covers ``_quote_literal``'s NUL-byte guard via
``guitars.sql.policy`` (its only reachable caller); this file is for the helpers -- and the
new M4 additions -- that don't yet have a single obvious call site to hang a test off of.
"""

from __future__ import annotations

import pytest

from guitars.sql import _identifiers


class TestBareOrQualified:
    def test_unqualified_name_behaves_like_bare(self):
        assert _identifiers._bare_or_qualified('table', 'events') == (None, 'events')

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


class TestQuoteQualified:
    def test_unqualified_quotes_one_part(self):
        assert _identifiers._quote_qualified(None, 'events') == '"events"'

    def test_qualified_quotes_both_parts_joined_by_a_bare_dot(self):
        assert _identifiers._quote_qualified('analytics', 'events') == '"analytics"."events"'


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
