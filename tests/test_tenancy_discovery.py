"""Tests for guitars.tenancy.discovery -- what the models say the database should look like.
One answer, two consumers (``makeguitarmigrations``, ``audittenancy``): if they could
disagree, a green audit would mean nothing. Coverage shapes are real test-app models."""

from __future__ import annotations

import pytest
from django.apps import apps as django_apps

from guitars.tenancy import tenanted_manager
from guitars.tenancy.discovery import TableCoverage, app_coverage, expected_coverage, is_local
from tests.testapp.models import (
    Booking,
    HeadlineFestival,
    Label,
    Release,
    Review,
    StadiumTour,
    Tour,
    Venue,
    WorldTour,
)


@pytest.fixture
def coverage():
    return app_coverage(django_apps.get_app_config('testapp'))


@pytest.fixture
def release_proxy():
    """A proxy over a tenanted model, registered for one test and withdrawn afterwards --
    left in the registry it would join every other test's model sweep (and, through
    ``reverse_relations_mapping``, the generator's cascade candidates)."""

    class ReleaseProxy(Release):
        class Meta:
            proxy = True
            app_label = 'testapp'

    yield ReleaseProxy
    # ``AppConfig.models`` *is* ``apps.all_models[label]``, so one pop withdraws both.
    django_apps.all_models['testapp'].pop('releaseproxy', None)
    django_apps.clear_cache()


@pytest.fixture
def venue_proxy():
    """A proxy declaring the tenanted manager its *concrete* model does not have -- the one
    shape where skipping proxies loses coverage rather than restoring it."""

    class VenueProxy(Venue):
        objects = tenanted_manager(label='label')

        class Meta:
            proxy = True
            app_label = 'testapp'

    yield VenueProxy
    django_apps.all_models['testapp'].pop('venueproxy', None)
    django_apps.clear_cache()


class TestWhichTablesAreCovered:
    def test_every_tenanted_table_and_nothing_else(self, coverage):
        assert set(coverage.tables) == {
            'testapp_arena',
            'testapp_booking',
            'testapp_concerthall',
            'testapp_headlinefestival',
            'testapp_lecturehall',
            'testapp_release',
            'testapp_squashcourt',
            'testapp_stadiumtour',
            'testapp_tenniscourt',
            'testapp_tour',
            'testapp_track',
            'testapp_worldtour',
        }

    def test_a_proxy_does_not_overwrite_its_concrete_models_coverage(self, release_proxy):
        """A proxy has no ``local_fields``, so ``_classify`` can only read it as an MTI child
        of its own table. Declared after ``Release``, that answer replaced the real one: the
        policy became a self-join and merely adding a proxy generated a ``DROP TRIGGER``."""
        coverage = app_coverage(django_apps.get_app_config('testapp'))
        release = coverage.tables[Release._meta.db_table]

        assert release.columns == {'label': 'label_id'}
        assert release.autofill_columns == {'label': 'label_id'}
        assert release.owner_table is None

    def test_a_proxy_tenanted_on_its_own_is_named_rather_than_dropped_in_silence(
        self, venue_proxy
    ):
        """Skipping proxies is right, but a manager declared *on* one is the case where the
        concrete model contributes nothing in its place. Left silent, a model that reads as
        tenanted gets no policy and no trigger and nothing anywhere says so."""
        coverage = app_coverage(django_apps.get_app_config('testapp'))

        assert Venue._meta.db_table not in coverage.tables
        assert [note for note in coverage.notes if 'VenueProxy' in note]

    def test_a_proxy_inheriting_the_manager_earns_no_note(self, release_proxy):
        """Its concrete model already covers the table, so a note would name a gap that
        isn't one -- the "two notes for one fact" this module avoids elsewhere."""
        coverage = app_coverage(django_apps.get_app_config('testapp'))

        assert not [note for note in coverage.notes if 'ReleaseProxy' in note]

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
        """One fact, one note -- used to collect two ("traverses a relation" plus
        "skipped"), reading as two separate problems with one model."""
        review_notes = [note for note in coverage.notes if 'testapp_review' in note]
        assert len(review_notes) == 1

    def test_an_own_dimension_survives_a_multi_ancestor_conflict(self, coverage):
        """``sponsor`` is on its own table, while ``market``/``promoter`` live on two
        *different* ancestors -- one correlated subquery can reach only one, so both are
        dropped, but the own-table dimension must still get a policy."""
        assert coverage.tables[HeadlineFestival._meta.db_table] == TableCoverage(
            columns={'sponsor': 'sponsor_id'}
        )

        note = next(note for note in coverage.notes if 'testapp_headlinefestival' in note)
        assert "tenant dimensions ['market', 'promoter'] live on more than one ancestor" in note
        assert "its policy still enforces ['sponsor']" in note


class TestHowEachTableIsPredicated:
    def test_an_own_column_table_needs_no_join(self, coverage):
        assert coverage.tables[Release._meta.db_table] == TableCoverage(
            columns={'label': 'label_id'}, autofill_columns={'label': 'label_id'}
        )

    def test_the_mti_root_owns_its_column(self, coverage):
        """The root is an ordinary own-table case; only its children need the join."""
        assert coverage.tables[Tour._meta.db_table].owner_columns is None

    @pytest.mark.parametrize('model', [WorldTour, StadiumTour], ids=lambda m: m.__name__)
    def test_an_mti_child_joins_the_column_owner(self, coverage, model):
        """Both levels join ``testapp_tour``, not their immediate parent -- the leaf's parent
        ``testapp_worldtour`` has no tenant column either, so it couldn't answer."""
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
        """The point of the mapping: what ``sql.create_table_rls`` is called with. Asserted
        here too, not only in the generator, so a rename is caught by whichever side changed."""
        from guitars import sql

        statements = sql.create_table_rls(
            table='testapp_stadiumtour', **coverage.tables['testapp_stadiumtour'].as_kwargs()
        )

        assert 'testapp_tour AS _guitars_owner' in statements[0]
        assert "current_setting('tenant.label', true)" in statements[0]


class TestWhichTablesAutofill:
    """``autofill_columns`` is what ADR 0005's INSERT trigger is generated from, so a table
    listed here gains one and a table absent stays fillable only from Python."""

    @pytest.mark.parametrize('model', [Release, Tour], ids=lambda m: m.__name__)
    def test_a_guitar_model_autofills_its_own_column(self, coverage, model):
        assert coverage.tables[model._meta.db_table].autofill_columns == {'label': 'label_id'}

    @pytest.mark.parametrize('model', [Booking, HeadlineFestival], ids=lambda m: m.__name__)
    def test_a_hand_declared_manager_does_not_autofill_by_default(self, coverage, model):
        """None of these pass ``autofill=``, so they fall back to ``GUITARS_TENANT_AUTOFILL``
        (``False`` in this harness) -- and the opt-out becomes visible as an absent trigger."""
        assert coverage.tables[model._meta.db_table].autofill_columns is None

    @pytest.mark.parametrize('model', [WorldTour, StadiumTour], ids=lambda m: m.__name__)
    def test_an_mti_child_leaves_autofill_to_the_column_owner(self, coverage, model):
        """The column lives on ``testapp_tour``; a trigger here would have nothing to write.
        The owner appears in the same scan with the column local to it and gets the trigger."""
        assert coverage.tables[model._meta.db_table].autofill_columns is None

    def test_a_multi_hop_table_is_not_covered_at_all(self, coverage):
        """``Review`` scopes on ``release__label`` -- no column here to fill or predicate on."""
        assert Review._meta.db_table not in coverage.tables

    def test_autofill_columns_stays_out_of_the_policy_kwargs(self, coverage):
        """Load-bearing: ``_policy_identity`` digests ``as_kwargs()``, so if the new field
        leaked in, every existing ``[POLICY:...]`` header would go stale and 2.1.0 would
        replace every policy in every consuming project instead of only adding triggers."""
        release = coverage.tables[Release._meta.db_table]
        assert release.autofill_columns
        assert 'autofill_columns' not in release.as_kwargs()
        assert release.as_kwargs() == TableCoverage(columns={'label': 'label_id'}).as_kwargs()


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
