"""Tests for guitars.tenancy.discovery -- what the models say the database should look like.

One answer, two consumers: ``makeguitarmigrations`` turns it into migrations at build time
and ``audittenancy`` compares it against a live database. If they could disagree, a green
audit would mean nothing -- so this is tested as the shared answer, not once per caller.

The coverage shapes here are the real test-app models, so a shape that stops being
reachable stops being asserted, rather than passing against a hand-built fixture that no
longer resembles anything.
"""

from __future__ import annotations

import pytest
from django.apps import apps as django_apps

from guitars.tenancy.discovery import TableCoverage, app_coverage, expected_coverage, is_local
from tests.testapp.models import (
    Booking,
    HeadlineFestival,
    Label,
    Release,
    Review,
    StadiumTour,
    Tour,
    WorldTour,
)


@pytest.fixture
def coverage():
    return app_coverage(django_apps.get_app_config('testapp'))


class TestWhichTablesAreCovered:
    def test_every_tenanted_table_and_nothing_else(self, coverage):
        assert set(coverage.tables) == {
            'testapp_booking',
            'testapp_headlinefestival',
            'testapp_release',
            'testapp_stadiumtour',
            'testapp_tour',
            'testapp_track',
            'testapp_worldtour',
        }

    def test_the_tenant_model_itself_is_not_covered(self, coverage):
        """``Label`` *is* the tenant. Scoping it to itself is meaningless, and a policy on it
        would make the tenant table unreadable without already knowing which tenant."""
        assert Label._meta.db_table not in coverage.tables

    def test_an_untenanted_model_is_not_covered(self, coverage):
        """Adoption is per model; ``Band`` never asked."""
        assert 'testapp_band' not in coverage.tables

    def test_a_multi_hop_dimension_is_reported_not_covered(self, coverage):
        assert Review._meta.db_table not in coverage.tables

        note = next(note for note in coverage.notes if 'testapp_review' in note)
        # Dimension *and* lookup: either alone leaves the reader guessing which is meant.
        assert 'label (release__label)' in note
        assert 'Python scoping still applies' in note

    def test_that_is_the_only_note_about_it(self, coverage):
        """One fact, one note.

        A model whose every dimension is multi-hop used to collect two -- a "traverses a
        relation" note naming the dimension and a "skipped" note naming the lookup -- which
        read as two separate problems with one model.
        """
        review_notes = [note for note in coverage.notes if 'testapp_review' in note]
        assert len(review_notes) == 1

    def test_an_own_dimension_survives_a_multi_ancestor_conflict(self, coverage):
        """``HeadlineFestival`` has ``sponsor`` on its own table, while ``market`` and
        ``promoter`` live on two *different* ancestors (``Festival`` and
        ``TouringFestival``) -- one correlated subquery can only reach one of them, so
        both are dropped. The own-table dimension must still get a policy: dropping
        those two must not also drop the one this table can actually enforce itself.
        """
        assert coverage.tables[HeadlineFestival._meta.db_table] == TableCoverage(
            columns={'sponsor': 'sponsor_id'}
        )

        note = next(note for note in coverage.notes if 'testapp_headlinefestival' in note)
        assert "tenant dimensions ['market', 'promoter'] live on more than one ancestor" in note
        assert "its policy still enforces ['sponsor']" in note


class TestHowEachTableIsPredicated:
    def test_an_own_column_table_needs_no_join(self, coverage):
        assert coverage.tables[Release._meta.db_table] == TableCoverage(
            columns={'label': 'label_id'}
        )

    def test_the_mti_root_owns_its_column(self, coverage):
        """The root is an ordinary own-table case; only its children need the join."""
        assert coverage.tables[Tour._meta.db_table].owner_columns is None

    @pytest.mark.parametrize('model', [WorldTour, StadiumTour], ids=lambda m: m.__name__)
    def test_an_mti_child_joins_the_column_owner(self, coverage, model):
        """Both levels join ``testapp_tour``, not their immediate parent.

        The leaf's parent is ``testapp_worldtour``, which has no tenant column either -- so
        predicating against the immediate parent would reference a table that cannot answer.
        """
        table = coverage.tables[model._meta.db_table]

        assert table.columns == {}
        assert table.owner_table == Tour._meta.db_table
        assert table.owner_columns == {'label': 'label_id'}
        assert table.owner_pk == 'id'
        assert table.child_pk == model._meta.pk.column

    def test_the_child_key_is_the_parent_link_column(self, coverage):
        """What makes the correlated join sound: every table in the chain shares one PK
        *value*, so the leaf's parent-link column holds the root's id."""
        assert coverage.tables['testapp_stadiumtour'].child_pk == 'worldtour_ptr_id'
        assert coverage.tables['testapp_worldtour'].child_pk == 'tour_ptr_id'

    def test_a_hand_declared_manager_is_covered_like_any_other(self, coverage):
        """Tenanted-ness is read off the managers, so composing one by hand is enough --
        there is no second registry to register with."""
        assert coverage.tables[Booking._meta.db_table] == TableCoverage(
            columns={'label': 'label_id'}
        )


class TestAsKwargs:
    """What lands in the generated migration."""

    def test_an_own_column_table_omits_the_owner_keys(self, coverage):
        """So a non-MTI migration reads as simply as the case deserves."""
        assert coverage.tables['testapp_release'].as_kwargs() == {'columns': {'label': 'label_id'}}

    def test_an_mti_child_carries_the_whole_join(self, coverage):
        assert coverage.tables['testapp_stadiumtour'].as_kwargs() == {
            'columns': {},
            'owner_table': 'testapp_tour',
            'owner_pk': 'id',
            'child_pk': 'worldtour_ptr_id',
            'owner_columns': {'label': 'label_id'},
        }

    def test_the_kwargs_build_valid_policy_sql(self, coverage):
        """The point of the mapping: it is what ``sql.create_table_rls`` is called with.

        Asserted here rather than only in the generator, so a rename on either side of that
        call is caught by the side that changed.
        """
        from guitars import sql

        statements = sql.create_table_rls(
            table='testapp_stadiumtour', **coverage.tables['testapp_stadiumtour'].as_kwargs()
        )

        assert 'testapp_tour AS _guitars_owner' in statements[0]
        assert "current_setting('tenant.label', true)" in statements[0]


class TestScoping:
    def test_is_local_keys_on_the_dotted_path(self):
        """``LOCAL_APPS`` holds module paths, not Django's short labels."""
        assert is_local(django_apps.get_app_config('testapp'))
        assert not is_local(django_apps.get_app_config('guitars'))

    def test_expected_coverage_unscoped_covers_every_local_app(self, coverage):
        assert set(expected_coverage().tables) == set(coverage.tables)

    def test_expected_coverage_honours_a_requested_label(self, coverage):
        assert set(expected_coverage({'testapp'}).tables) == set(coverage.tables)

    def test_a_label_outside_local_apps_yields_nothing(self):
        """``guitars`` is installed but is not a local app, so it is never scanned."""
        assert expected_coverage({'guitars'}).tables == {}
