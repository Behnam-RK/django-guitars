"""Property-based tests: identifier validation, and update()/aupdate() flag combinatorics.

Two independent gaps neither example-based tests nor 100% coverage can see:

* **Identifiers.** ``sql/policy.py``'s ``_bare()`` raises a build-time ``ValueError`` for
  anything failing ``^[a-z_][a-z0-9_$]*$`` -- the "clear build-time error" for a hostile
  ``db_table``/``db_column``. But ``_bare()`` is applied *only* on the tenant-policy path
  (every call site is in ``sql/policy.py``); the trigger and rule SQL
  ``makeguitarmigrations.py`` builds via ``sql.CREATE_UPDATED_AT_TRIGGER.format(table=...)``
  and friends has no validation at all. So ``db_table = 'Order Items'`` raises cleanly on a
  ``GuitarModel`` and silently generates broken SQL on a ``SetarModel``. See CLAUDE.md's
  "Findings that change the issue's plan", finding 3 -- fixing this is M4's job; this file
  only pins the asymmetry so M4 lands against a red test rather than a blind spot.

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
from guitars.sql._identifiers import _BARE_IDENTIFIER
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

    @pytest.mark.xfail(
        strict=True,
        reason=(
            'M4: makeguitarmigrations._build_operations interpolates db_table bare into '
            'sql.CREATE_UPDATED_AT_TRIGGER/CREATE_SOFT_DELETE_RULE with no _bare()-equivalent '
            'guard, unlike the tenant-policy path. Flip this once M4 adds one there.'
        ),
    )
    @given(name=_shape_invalid_identifiers)
    def test_trigger_rule_path_is_defensible_against_hostile_identifiers(self, name):
        assert not _BARE_IDENTIFIER.match(name)
        rendered = sql.CREATE_UPDATED_AT_TRIGGER.format(table=name, primary_key='id')
        # "Defensible" = either this call would have raised above (it never does today), or
        # the identifier comes back safely quoted rather than interpolated bare.
        assert f'"{name}"' in rendered


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
