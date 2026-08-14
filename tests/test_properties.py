"""Property-based tests: identifier validation, and update() flag combinatorics -- gaps
neither example-based tests nor 100% coverage can see. Identifiers: M4 closed the
asymmetry between the tenant-policy and trigger/rule paths. update(): the 16-way flag cross product."""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from guitars import sql
from guitars.sql._identifiers import _BARE_IDENTIFIER, _escape_literal, _quote_table
from tests.testapp.models import Band


# Mixed case and an embedded space -- both legal as a Django db_table, both fail the
# shape check. Excludes reserved words/long names and dots (different validation paths).
_shape_invalid_identifiers = st.one_of(
    st.sampled_from(['Table', 'ORDER', 'Group', 'User']),
    st.just('Order Items'),
)

# A hostile schema/table part, or more than one qualifying '.', rejected once a name is
# treated as schema-qualified at all, on both the policy and trigger/rule paths.
_shape_invalid_qualified_identifiers = st.one_of(
    st.just('Public.mytable'),
    st.just('public.MyTable'),
    st.just('a.b.c'),
)


class TestIdentifierValidation:
    @given(name=_shape_invalid_identifiers)
    def test_policy_path_raises_a_build_time_error(self, name):
        assert not _BARE_IDENTIFIER.match(name)
        with pytest.raises(ValueError, match='not a plain lower-case SQL identifier'):
            sql.enable_rls(table=name)

    @given(name=_shape_invalid_qualified_identifiers)
    def test_policy_path_raises_a_build_time_error_for_a_hostile_schema_qualified_name(self, name):
        """M4 validates each side of the '.' -- a hostile part or second '.' raises,
        rather than silently binding the wrong relation."""
        with pytest.raises(ValueError):
            sql.enable_rls(table=name)

    def test_policy_path_accepts_a_schema_qualified_table(self):
        """M4: 'schema.table' is now an ordinary, valid ``table`` -- resolving the
        contradiction with ``audittenancy``'s own schema-per-tenant support."""
        assert sql.enable_rls(table='public.mytable') == (
            'ALTER TABLE public.mytable ENABLE ROW LEVEL SECURITY'
        )

    def test_policy_path_quotes_a_hostile_pre_quoted_schema_qualified_table(self):
        """Regression: Django's pre-quoted form isn't re-validated as bare, so the
        tenant-policy path must re-quote the parts, not join them bare -- joining bare
        relocated the case-folding bug this milestone fixes elsewhere."""
        assert sql.enable_rls(table='"Analytics"."My Events"') == (
            'ALTER TABLE "Analytics"."My Events" ENABLE ROW LEVEL SECURITY'
        )

    @given(
        name=st.one_of(
            st.sampled_from(['select', 'table', 'order', 'group', 'user']),
            st.text(alphabet='abcdefghijklmnopqrstuvwxyz_', min_size=63, max_size=100),
        )
    )
    def test_bare_identifier_only_checks_shape(self, name):
        """A lowercase reserved word or long identifier passes ``_bare()`` silently --
        shape-only, never reservedness or length. Real gaps, but on both paths alike."""
        assert _BARE_IDENTIFIER.match(name)
        assert sql.enable_rls(table=name) == f'ALTER TABLE {name} ENABLE ROW LEVEL SECURITY'

    @given(name=_shape_invalid_identifiers)
    def test_trigger_rule_path_is_defensible_against_hostile_identifiers(self, name):
        """An unqualified hostile ``table`` still renders safely quoted via
        ``_quote_table``'s permissive no-dot branch -- unaffected by M4's schema
        validation, which only applies once a name is treated as schema-qualified."""
        assert not _BARE_IDENTIFIER.match(name)
        rendered = sql.CREATE_UPDATED_AT_TRIGGER.format(table=_quote_table(name), primary_key='id')
        assert f'"{name}"' in rendered

    @given(name=_shape_invalid_qualified_identifiers)
    def test_trigger_rule_path_raises_for_a_hostile_schema_qualified_name(self, name):
        with pytest.raises(ValueError):
            _quote_table(name)

    def test_trigger_rule_path_renders_a_schema_qualified_table_as_two_quoted_parts(self):
        """A genuine two-part reference can't be permissively quoted as one blob --
        quoting ``"schema.table"`` whole would target one wrong relation, not two."""
        assert _quote_table('public.mytable') == '"public"."mytable"'

    def test_embedded_quote_in_a_literal_position_is_doubled_not_broken(self):
        """``'{primary_key}'`` is a string-literal argument, not a bare identifier -- an
        embedded ``'`` must come back doubled via ``_escape_literal``, or the quote breaks."""
        rendered = sql.CREATE_UPDATED_AT_TRIGGER.format(
            table='band', primary_key=_escape_literal("weird'pk")
        )
        assert "set_updated_at('weird''pk')" in rendered

    def test_embedded_quote_in_a_bare_identifier_position_is_doubled_not_broken(self):
        """``{table}`` is a table-DDL position filled by ``_quote_table`` -- an embedded
        ``"`` must come back doubled by that same permissive no-dot branch."""
        rendered = sql.CREATE_SOFT_DELETE_RULE.format(
            table=_quote_table('weird"table'), primary_key='id'
        )
        assert 'ON DELETE TO "weird""table"' in rendered


class TestUpdateFlagCombinatorics:
    """The 16-way cross product of ``update()``'s four flags, pinned against a real save."""

    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        save=st.booleans(),
        save_all_fields=st.booleans(),
        raise_for_excessive=st.booleans(),
        disable_signals=st.booleans(),
    )
    def test_flag_combinations_match_documented_semantics(
        self, db, save, save_all_fields, raise_for_excessive, disable_signals
    ):
        band = Band.objects.create(name='Original', nickname='orig-nick')
        # Changed in memory only, never passed to update() -- the one way to observe
        # whether `update_fields` ends up `None` (writes it too) or `{'name'}` (leaves it).
        band.nickname = 'manually-changed'

        call_kwargs = {
            '_save': save,
            '_save_all_fields': save_all_fields,
            '_raise_for_excessive': raise_for_excessive,
            '_disable_signals': disable_signals,
            'name': 'Updated',
            'bogus_field': 'ignored',
        }

        if raise_for_excessive:
            with pytest.raises(ValueError, match='bogus_field'):
                band.update(**call_kwargs)
            # _prepare_update raises before setting anything at all.
            assert band.name == 'Original'
            return

        if not save and save_all_fields:
            # M5 (#12): meaningless, not merely unusual -- _save_all_fields has nothing to
            # act on when nothing is saved. Checked after excessive-fields, which must win.
            with pytest.raises(ValueError, match='_save_all_fields'):
                band.update(**call_kwargs)
            assert band.name == 'Original'
            return

        band.update(**call_kwargs)
        # The valid attr is set in memory regardless of every other flag.
        assert band.name == 'Updated'

        refetched = Band.objects.get(pk=band.pk)
        if not save:
            assert refetched.name == 'Original'
            assert refetched.nickname == 'orig-nick'
            return

        assert refetched.name == 'Updated'
        if save_all_fields:
            assert refetched.nickname == 'manually-changed'
        else:
            assert refetched.nickname == 'orig-nick'
