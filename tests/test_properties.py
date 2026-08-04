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
from guitars.sql._identifiers import _BARE_IDENTIFIER, _escape_ident, _escape_literal
from tests.testapp.models import Band


# Mixed case, schema-qualified, and an embedded space -- all legal as a Django `db_table`,
# and all fail _BARE_IDENTIFIER's *shape* check (`^[a-z_][a-z0-9_$]*$`). Deliberately
# excludes reserved words and long names: _BARE_IDENTIFIER checks shape only, so a
# lowercase reserved word like 'select' matches it and a >=63-byte all-lowercase name does
# too -- both pass *both* paths silently. Those are real gaps, but a different one from the
# shape asymmetry below; see test_bare_identifier_only_checks_shape.
_shape_invalid_identifiers = st.one_of(
    st.sampled_from(['Table', 'ORDER', 'Group', 'User']),
    st.just('public.mytable'),
    st.just('Order Items'),
)


class TestIdentifierValidation:
    @given(name=_shape_invalid_identifiers)
    def test_policy_path_raises_a_build_time_error(self, name):
        assert not _BARE_IDENTIFIER.match(name)
        with pytest.raises(ValueError, match='not a plain lower-case SQL identifier'):
            sql.enable_rls(table=name)

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
        """M4: the template now bakes quote characters around its bare-identifier
        positions, so a hostile ``table`` renders safely quoted rather than interpolated
        bare -- even called directly on the raw constant, bypassing the generator's own
        escaping. NOTE: for a schema-qualified name (``'public.mytable'``) this currently
        renders as one quoted blob (``"public.mytable"``), a single wrong-but-safe
        identifier rather than a real two-part schema reference -- that distinction is
        M4's schema-qualified-support commit, not this one.
        """
        assert not _BARE_IDENTIFIER.match(name)
        rendered = sql.CREATE_UPDATED_AT_TRIGGER.format(table=name, primary_key='id')
        assert f'"{name}"' in rendered

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
        """``"{table}"`` is a bare-identifier position -- a value with an embedded ``"``
        must come back escaped via ``_escape_ident`` (doubled), the same way
        ``operations.py`` escapes it before calling ``.format()``.
        """
        rendered = sql.CREATE_SOFT_DELETE_RULE.format(
            table=_escape_ident('weird"table'), primary_key='id'
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
