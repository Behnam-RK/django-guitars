"""Property-based tests: identifier validation, and update()/aupdate() flag combinatorics.

Two independent gaps neither example-based tests nor 100% coverage can see:

* **Identifiers.** ``sql/policy.py``'s ``_bare()`` raises a build-time ``ValueError`` for
  anything failing ``^[a-z_][a-z0-9_$]*$`` -- the "clear build-time error" for a hostile
  ``db_table``/``db_column``. Before M4, ``_bare()`` was applied *only* on the
  tenant-policy path: the trigger and rule SQL ``makeguitarmigrations.py`` builds via
  ``sql.CREATE_UPDATED_AT_TRIGGER.format(table=...)`` and friends had no validation at
  all, so ``db_table = 'Order Items'`` raised cleanly on a ``GuitarModel`` and silently
  generated broken SQL on a ``SetarModel``. M4 closed that asymmetry by baking quoting
  into the trigger/rule templates themselves (see ``sql/triggers.py``/``sql/soft_delete.py``),
  so ``test_trigger_rule_path_is_defensible_against_hostile_identifiers`` below now passes
  for real rather than as a pinned ``xfail``.

  Separately, ``_bare()`` checks *shape* only -- never reservedness or length -- so a
  lowercase reserved word or a >=63-byte name passes it silently on *both* paths alike;
  that is a real gap too, just not the shape asymmetry the other two tests exist to pin.

* **update() flags.** ``_save`` x ``_save_all_fields`` x ``_raise_for_excessive`` x
  ``_disable_signals`` is 16 combinations against ``models/base.py``. The docstring there
  names roughly three by example; this pins the rest. The ``_disable_signals`` + tenant-guard
  reporting path itself already has dedicated coverage in
  ``test_tenancy_models.py::TestUpdateDisableSignalsReporting`` -- this file is about the
  ``update_fields``/``_save``/``_raise_for_excessive`` interactions, which are identical
  whether or not the model is tenanted, so a plain ``SetarModel`` (``Band``) is enough.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from guitars import sql
from guitars.sql._identifiers import _BARE_IDENTIFIER, _escape_literal, _quote_table
from tests.testapp.models import Band


# Mixed case and an embedded space -- both legal as a Django `db_table`, and both fail
# _BARE_IDENTIFIER's *shape* check (`^[a-z_][a-z0-9_$]*$`). Deliberately excludes reserved
# words and long names: _BARE_IDENTIFIER checks shape only, so a lowercase reserved word
# like 'select' matches it and a >=63-byte all-lowercase name does too -- both pass *both*
# paths silently. Those are real gaps, but a different one from the shape asymmetry below;
# see test_bare_identifier_only_checks_shape. Deliberately excludes anything containing a
# '.' too -- a schema-qualified name is a different shape question, covered separately by
# _shape_invalid_qualified_identifiers below, since M4 treats "no dot" and "one dot" as two
# different validation paths (see _identifiers._quote_table's docstring).
_shape_invalid_identifiers = st.one_of(
    st.sampled_from(['Table', 'ORDER', 'Group', 'User']),
    st.just('Order Items'),
)

# A hostile schema or table part, or more than one qualifying '.' -- all rejected once a
# name is treated as schema-qualified at all, on both the policy and trigger/rule paths
# alike (see test_policy_path_raises_a_build_time_error_for_a_hostile_schema_qualified_name
# and test_trigger_rule_path_raises_for_a_hostile_schema_qualified_name).
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
        """M4: schema-qualified support validates each side of the '.' -- a hostile schema
        or table part, or more than one '.', raises exactly like an unqualified hostile name
        always has, rather than silently binding the wrong relation.
        """
        with pytest.raises(ValueError):
            sql.enable_rls(table=name)

    def test_policy_path_accepts_a_schema_qualified_table(self):
        """M4: 'schema.table' is now an ordinary, valid `table` -- the contradiction with
        `audittenancy`'s own schema-per-tenant support this milestone resolves.
        """
        assert sql.enable_rls(table='public.mytable') == (
            'ALTER TABLE public.mytable ENABLE ROW LEVEL SECURITY'
        )

    def test_policy_path_quotes_a_hostile_pre_quoted_schema_qualified_table(self):
        """Regression: a tenanted model's ``db_table`` in Django's pre-quoted
        ``'"schema"."table"'`` convention is *not* re-validated as bare by
        ``_bare_or_qualified`` (quoting is what already made hostile content safe -- see its
        docstring), so the tenant-policy path must re-quote the parts it gets back rather than
        joining them bare. Joining bare rendered exactly the unquoted, case-folding bug this
        milestone exists to fix, just relocated from the trigger/rule path to this one -- and
        every table exercised by ``tests/test_schema_qualified.py`` happens to be all-lowercase,
        so it never caught this.
        """
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
        """A lowercase reserved word or a long, otherwise-legal identifier passes
        ``_bare()`` silently -- it checks character shape only, never reservedness or
        length (no 63-byte check anywhere in ``src/``). Both are real, currently-unguarded
        risks (a reserved word fails at ``migrate`` with a syntax error; two distinct
        70-byte names collide on the same Postgres-truncated 63-byte one), but they hold
        on *both* paths alike -- not the shape asymmetry the other two tests pin.
        """
        assert _BARE_IDENTIFIER.match(name)
        assert sql.enable_rls(table=name) == f'ALTER TABLE {name} ENABLE ROW LEVEL SECURITY'

    @given(name=_shape_invalid_identifiers)
    def test_trigger_rule_path_is_defensible_against_hostile_identifiers(self, name):
        """An unqualified hostile ``table`` (no ``.``) still renders safely quoted as one
        opaque identifier via ``_quote_table``'s permissive, no-dot branch -- unaffected by
        M4's schema-qualified support, which only adds validation once a name is treated as
        schema-qualified at all (see ``_shape_invalid_qualified_identifiers``'s tests). The
        template itself owns no quote characters from M4 on (see triggers.py's module
        docstring), so the caller -- here, ``_quote_table`` directly -- is what defends it.
        """
        assert not _BARE_IDENTIFIER.match(name)
        rendered = sql.CREATE_UPDATED_AT_TRIGGER.format(table=_quote_table(name), primary_key='id')
        assert f'"{name}"' in rendered

    @given(name=_shape_invalid_qualified_identifiers)
    def test_trigger_rule_path_raises_for_a_hostile_schema_qualified_name(self, name):
        with pytest.raises(ValueError):
            _quote_table(name)

    def test_trigger_rule_path_renders_a_schema_qualified_table_as_two_quoted_parts(self):
        """A genuine two-part reference can't be permissively quoted as one blob -- quoting
        ``"schema.table"`` whole would target one wrong relation instead of two correct
        ones, so a valid qualified name renders as two independently-quoted parts.
        """
        assert _quote_table('public.mytable') == '"public"."mytable"'

    def test_embedded_quote_in_a_literal_position_is_doubled_not_broken(self):
        """``'{primary_key}'`` in the trigger templates is a string-literal argument to
        ``set_updated_at()``/``set_parent_updated_at()``, not a bare identifier -- a value
        with an embedded ``'`` must come back escaped via ``_escape_literal`` (doubled),
        the same way ``operations.py`` escapes it before calling ``.format()``, or the
        statement's own quote boundary breaks.
        """
        rendered = sql.CREATE_UPDATED_AT_TRIGGER.format(
            table='band', primary_key=_escape_literal("weird'pk")
        )
        assert "set_updated_at('weird''pk')" in rendered

    def test_embedded_quote_in_a_bare_identifier_position_is_doubled_not_broken(self):
        """``{table}`` in the soft-delete templates is a table-DDL position, filled by
        ``_quote_table`` -- a value with an embedded ``"`` must come back escaped (doubled)
        by that same permissive, no-dot branch, the same way ``operations.py`` quotes it
        before calling ``.format()``.
        """
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
            # M5 (#12): the one combination that is meaningless rather than merely
            # unusual -- _save_all_fields has nothing to act on when nothing is saved
            # this call. Checked ahead of the excessive-fields case above: that one
            # raises regardless of this combination, so it must win when both apply --
            # which the `if raise_for_excessive: return` above already guarantees.
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
