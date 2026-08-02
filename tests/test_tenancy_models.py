"""Tenancy against real models and a real PostgreSQL database.

``tests/test_tenancy_rls.py`` proves the *SQL* with hand-written DDL; this file proves the
whole stack the way a consumer meets it: a ``GuitarModel`` subclass, its three managers, the
write guard, the soft-delete rules and the policy, all at once.

The harness sets ``GUITARS_TENANT_FIELD = 'label'``, so every assertion here also exercises
a non-default field name -- the column, the session setting ``tenant.label``, the policy
predicate and the scope dimension all have to be the renamed one, and only a test that
never mentions ``'tenant'`` can show they moved together.

Two tenants throughout. The interesting assertion is never "I can see my rows" but "I
cannot see theirs", and one tenant's data cannot demonstrate that.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.utils import IntegrityError

from guitars.tenancy import TenantScopeError, tenancy_bypassed, tenant
from guitars.tenancy import reporting
from tests.testapp.models import Band, Booking, Label, Release, Review, StadiumTour, Tour, Track


def _count(table: str) -> int:
    """Row count straight from the database, with no ORM and therefore no manager.

    What the policy alone decides. A manager could produce the right number for the wrong
    reason; this cannot.
    """
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT count(*) FROM {table}')  # noqa: S608 - fixed literal names
        return cursor.fetchone()[0]


# ───────────────────────────────── reads ───────────────────────────────── #


class TestScopedReads:
    def test_a_scope_sees_only_its_own_rows(self, tenants):
        with tenant(label=tenants.a):
            assert list(Release.objects.values_list('title', flat=True)) == ['release-a']

    def test_bypass_sees_every_tenant(self, tenants):
        with tenancy_bypassed():
            assert Release.objects.count() == 2

    def test_a_collection_scope_sees_all_of_its_tenants(self, tenants):
        with tenant(label=[tenants.a, tenants.b]):
            assert Release.objects.count() == 2

    @pytest.mark.parametrize('manager', ['objects', '_archives', '_all_objects'])
    def test_every_manager_refuses_without_a_scope(self, tenants, manager):
        """All three, not just the default.

        ``_archives`` and ``_all_objects`` exist to see rows ``objects`` hides, so an
        unscoped one would be the widest leak in the kit.
        """
        with pytest.raises(TenantScopeError, match='active tenant scope on label'):
            getattr(Release, manager).count()

    def test_the_error_names_the_missing_dimension_and_the_way_out(self, tenants):
        with pytest.raises(TenantScopeError) as caught:
            Release.objects.count()

        message = str(caught.value)
        assert 'label' in message
        assert 'tenant(' in message
        assert 'tenancy_bypassed()' in message

    def test_a_none_valued_scope_is_absent_not_wildcard(self, tenants):
        """The difference between "no tenant" and "every tenant" has to be explicit."""
        with tenant(label=None), pytest.raises(TenantScopeError):
            Release.objects.count()

    def test_a_nested_scope_restores_the_outer_one(self, tenants):
        with tenant(label=tenants.a):
            with tenant(label=tenants.b):
                assert list(Release.objects.values_list('title', flat=True)) == ['release-b']
            assert list(Release.objects.values_list('title', flat=True)) == ['release-a']

    def test_an_inner_scope_re_enforces_inside_a_bypass(self, tenants):
        with tenancy_bypassed():
            assert Release.objects.count() == 2
            with tenant(label=tenants.a):
                assert Release.objects.count() == 1

    def test_an_untenanted_model_is_untouched(self, tenants):
        """Adoption is per model. A ``SetarModel`` must not start demanding a scope."""
        assert Label.objects.count() == 2


class TestTheDatabaseCoversWhatPythonCannot:
    """The paths that never consult a manager. These are the reason the policy exists."""

    def test_raw_sql_is_scoped(self, tenants):
        with tenant(label=tenants.a):
            assert _count('testapp_release') == 1
        with tenancy_bypassed():
            assert _count('testapp_release') == 2

    def test_the_base_manager_is_scoped_by_the_policy(self, tenants):
        """``_base_manager`` applies no tenant filter in Python -- deliberately, see
        ``GuitarModel.Meta``. It is scoped anyway, because the policy does not care which
        manager asked.

        This is the assertion that makes the ``base_manager_name`` decision defensible
        rather than merely argued: leaving it unscoped costs nothing, because the layer
        below is not optional.
        """
        with tenant(label=tenants.a):
            assert Release._base_manager.count() == 1
        with tenancy_bypassed():
            assert Release._base_manager.count() == 2

    def test_a_join_is_scoped(self, tenants):
        """The child's own policy filters the join, with no manager involved in the SQL."""
        with tenant(label=tenants.a):
            assert list(Track.objects.values_list('release__title', flat=True)) == ['release-a']

    def test_a_reverse_relation_fetch_is_scoped(self, tenants):
        with tenant(label=tenants.a):
            assert tenants.release_a.tracks.count() == 1
            # The other tenant's release is not even reachable to ask.
            assert Release.objects.filter(pk=tenants.release_b.pk).count() == 0


# ───────────────────────────────── writes ──────────────────────────────── #


class TestWrites:
    def test_create_autofills_the_tenant(self, tenants):
        with tenant(label=tenants.a):
            release = Release.objects.create(title='new')

        assert release.label_id == tenants.a.pk

    def test_create_with_a_matching_explicit_tenant_is_allowed(self, tenants):
        with tenant(label=tenants.a):
            release = Release.objects.create(title='new', label=tenants.a)

        assert release.label_id == tenants.a.pk

    def test_create_into_another_tenant_is_refused(self, tenants):
        with tenant(label=tenants.a), pytest.raises(TenantScopeError, match='may not cross'):
            Release.objects.create(title='sneaky', label=tenants.b)

    def test_instance_save_is_guarded_too(self, tenants):
        """The guard is a ``pre_save`` receiver, so it covers the path no manager sees."""
        with tenant(label=tenants.a), pytest.raises(TenantScopeError, match='may not cross'):
            Release(title='sneaky', label=tenants.b).save()

    def test_an_unscoped_create_is_refused(self, tenants):
        with pytest.raises(TenantScopeError):
            Release.objects.create(title='nowhere')

    def test_bulk_create_autofills(self, tenants):
        with tenant(label=tenants.a):
            Release.objects.bulk_create([Release(title='x'), Release(title='y')])
            assert Release.objects.count() == 3

    def test_bulk_create_after_chaining_is_still_guarded(self, tenants):
        """``filter()`` hands back a queryset and leaves the manager behind.

        The guard therefore lives on the queryset class, not the manager. It used to be
        installed via ``_queryset_class`` on a manager whose ``get_queryset()`` ignored it,
        which made this exact call unguarded -- so autofill silently did nothing.
        """
        with tenant(label=tenants.a):
            Release.objects.filter(title='irrelevant').bulk_create([Release(title='z')])

            assert Release.objects.filter(title='z').get().label_id == tenants.a.pk

    def test_bulk_create_into_another_tenant_is_refused(self, tenants):
        with tenant(label=tenants.a), pytest.raises(TenantScopeError, match='may not cross'):
            Release.objects.bulk_create([Release(title='x', label=tenants.b)])

    def test_a_collection_scope_refuses_to_autofill(self, tenants):
        """ "Either of these" is not a value a column can hold.

        Refused whatever the length -- unwrapping a one-element collection would make the
        write depend on how many tenants the caller's list happened to contain.
        """
        with (
            tenant(label=[tenants.a, tenants.b]),
            pytest.raises(TenantScopeError, match='no one value to autofill'),
        ):
            Release.objects.create(title='ambiguous')

    def test_a_collection_scope_still_accepts_an_explicit_tenant(self, tenants):
        with tenant(label=[tenants.a, tenants.b]):
            assert Release.objects.create(title='explicit', label=tenants.a).label_id

    def test_update_is_confined_to_the_scope(self, tenants):
        with tenant(label=tenants.a):
            assert Release.objects.update(title='renamed') == 1
        with tenancy_bypassed():
            assert sorted(Release.objects.values_list('title', flat=True)) == [
                'release-b',
                'renamed',
            ]

    def test_an_update_cannot_move_a_row_to_another_tenant(self, tenants):
        """The policy's ``WITH CHECK`` is what stops this; no Python guard sees ``update()``."""
        with (
            tenant(label=tenants.a),
            pytest.raises(TenantScopeError, match='rejected by a tenant'),
        ):
            Release.objects.update(label=tenants.b)

    @pytest.mark.parametrize(
        'call',
        [
            pytest.param(lambda: Release.objects.update(title='x'), id='update'),
            pytest.param(lambda: Release.objects.all().delete(), id='delete'),
            pytest.param(lambda: Release.objects.bulk_update([], ['title']), id='bulk_update'),
            pytest.param(lambda: Release._all_objects.all().hard_delete(), id='hard_delete'),
            pytest.param(
                lambda: Release._all_objects.all()._hard_delete_own_table(),
                id='_hard_delete_own_table',
            ),
        ],
    )
    def test_set_wide_writes_are_denied_without_a_scope(self, tenants, call):
        """Unscoped, each of these would reach every tenant's rows.

        ``hard_delete`` matters most: it deletes permanently, and before
        ``FORCE ROW LEVEL SECURITY`` was on, the database would not have stopped it either.
        """
        with pytest.raises(TenantScopeError):
            call()

        # And nothing happened -- the refusal is before the statement, not after it.
        with tenancy_bypassed():
            assert Release._all_objects.count() == 2

    def test_instance_hard_delete_is_denied_without_a_scope(self, tenants):
        """Fail-closed: the row survives, live.

        The in-memory instance is left with ``pk = None``, because phase one calls
        ``self.delete()`` -- which Django always does, on success or failure. The instance
        must not be reused after this; the *database* is what the guarantee is about.
        """
        pk = tenants.release_a.pk

        with pytest.raises(TenantScopeError):
            tenants.release_a.hard_delete()

        with tenancy_bypassed():
            assert Release.objects.filter(pk=pk).exists()

    def test_a_scoped_hard_delete_removes_only_its_own_rows(self, tenants):
        """Instance-level, which walks CASCADE children -- the queryset form deliberately
        does not, so calling it here would leave a track pointing at nothing."""
        with tenant(label=tenants.a):
            tenants.release_a.hard_delete()

        with tenancy_bypassed():
            assert list(Release._all_objects.values_list('title', flat=True)) == ['release-b']
            assert list(Track._all_objects.values_list('title', flat=True)) == ['track-b']


class TestUpdateDisableSignalsReporting:
    """``update(_disable_signals=True)`` suppresses ``pre_save`` -- which is also where
    the tenant write guard lives. Nothing here can refuse the write without breaking the
    flag's whole purpose, so it must at least say so."""

    @pytest.fixture(autouse=True)
    def _isolated_reporter(self):
        original = reporting._reporter
        reporting.reset_reported()
        yield
        reporting.set_reporter(original)
        reporting.reset_reported()

    def test_disabling_signals_on_a_tenanted_model_is_reported(self, tenants):
        seen = []
        reporting.set_reporter(lambda message, /, **context: seen.append((message, context)))

        with tenant(label=tenants.a):
            tenants.release_a.update(title='renamed', _disable_signals=True)

        assert len(seen) == 1
        message, context = seen[0]
        assert 'Release' in message
        assert 'disable_signals' in message
        assert context == {'model': 'Release'}

    def test_repeated_calls_on_the_same_model_report_only_once(self, tenants):
        seen = []
        reporting.set_reporter(lambda message, /, **context: seen.append(message))

        with tenant(label=tenants.a):
            tenants.release_a.update(title='one', _disable_signals=True)
        with tenant(label=tenants.b):
            tenants.release_b.update(title='two', _disable_signals=True)

        assert len(seen) == 1  # deduped by model class, not by instance

    def test_disabling_signals_on_an_untenanted_model_is_not_reported(self, db):
        seen = []
        reporting.set_reporter(lambda message, /, **context: seen.append(message))

        band = Band.objects.create(name='Rush')
        band.update(name='Yes', _disable_signals=True)

        assert seen == []

    def test_disabling_signals_without_it_actually_being_requested_is_not_reported(self, tenants):
        seen = []
        reporting.set_reporter(lambda message, /, **context: seen.append(message))

        with tenant(label=tenants.a):
            tenants.release_a.update(title='renamed')  # no _disable_signals

        assert seen == []


# ─────────────────────── soft deletion under FORCE ─────────────────────── #


class TestSoftDeletionUnderPolicy:
    """The soft-delete rule rewrites ``DELETE`` into ``UPDATE``, which the policy also checks."""

    def test_a_scoped_delete_soft_deletes(self, tenants):
        """The rewritten ``UPDATE`` has to pass ``WITH CHECK`` -- it leaves the tenant column
        untouched, so it does, but only a real ``DELETE`` under ``FORCE`` proves it."""
        with tenant(label=tenants.a):
            tenants.release_a.delete()

            assert not Release.objects.filter(title='release-a').exists()
            assert Release._archives.filter(title='release-a').exists()

    def test_the_cascade_rule_reaches_the_scoped_rows(self, tenants):
        """``Release`` -> ``Track`` is a ``DO ALSO`` rule firing inside a policied table."""
        with tenant(label=tenants.a):
            tenants.release_a.delete()

            assert Track._archives.filter(title='track-a').exists()

    def test_the_cascade_rule_cannot_reach_another_tenant(self, tenants):
        with tenant(label=tenants.a):
            tenants.release_a.delete()
        with tenancy_bypassed():
            assert Track.objects.filter(title='track-b').exists()

    def test_deleting_a_tenant_unscoped_does_not_archive_its_rows(self, tenants):
        """A sharp edge, asserted so it is a known shape rather than a surprise.

        ``Label`` is the tenant model and is *not* tenanted, so deleting one needs no scope.
        Its cascade rule then tries to archive the tenanted rows -- and the policy filters
        that ``UPDATE`` to nothing, because no scope is active. The tenant is archived and
        its rows are not.

        Fail-closed rather than wrong: nothing is destroyed and nothing leaks. But a caller
        who wants the cascade has to say which tenant they mean, which is the next test.
        """
        label_pk, release_pk = tenants.a.pk, tenants.release_a.pk

        tenants.a.delete()

        with tenancy_bypassed():
            assert Label._archives.filter(pk=label_pk).exists()
            assert Release.objects.filter(pk=release_pk).exists()

    def test_deleting_a_tenant_inside_its_own_scope_archives_its_rows(self, tenants):
        release_pk, other_pk = tenants.release_a.pk, tenants.release_b.pk

        with tenant(label=tenants.a):
            tenants.a.delete()

        with tenancy_bypassed():
            assert not Release.objects.filter(pk=release_pk).exists()
            assert Release._archives.filter(pk=release_pk).exists()
            # The other tenant is untouched, which is the whole point of doing it scoped.
            assert Release.objects.filter(pk=other_pk).exists()


# ────────────────────── multi-table inheritance ────────────────────────── #


class TestTenantedMti:
    """The tenant column lives two tables up; the child carries an owner-join policy.

    Without it, a child-only statement -- which never touches the ancestor -- would be
    unfiltered. This is the gap the kit already knew about for timestamps
    (``set_parent_updated_at``), reached from the other side.
    """

    def test_a_scoped_read_of_the_leaf_sees_only_its_own_tenant(self, tenants):
        with tenant(label=tenants.a):
            assert list(StadiumTour.objects.values_list('name', flat=True)) == ['tour-a']

    def test_the_root_is_scoped_too(self, tenants):
        with tenant(label=tenants.a):
            assert list(Tour.objects.values_list('name', flat=True)) == ['tour-a']

    def test_a_child_only_update_cannot_reach_another_tenant(self, tenants):
        """``capacity`` lives on the leaf table alone, so this ``UPDATE`` never joins the
        ancestor that holds the tenant column. Only the leaf's own policy can confine it."""
        with tenant(label=tenants.a):
            assert StadiumTour.objects.update(capacity=99) == 1

        with tenancy_bypassed():
            assert sorted(StadiumTour.objects.values_list('capacity', flat=True)) == [99, 1000]

    def test_a_child_only_raw_delete_cannot_reach_another_tenant(self, tenants):
        """Raw SQL against the leaf table, with no ORM and no manager anywhere in the call.

        The effect is asserted rather than ``cursor.rowcount``: the leaf carries the MTI
        redirect rule, so a ``DELETE`` here is rewritten ``DO INSTEAD`` into an ``UPDATE`` on
        the ancestor, and the reported row count describes the substituted statement.
        """
        a_pk, b_pk = tenants.tour_a.pk, tenants.tour_b.pk

        with tenant(label=tenants.a), connection.cursor() as cursor:
            cursor.execute('DELETE FROM testapp_stadiumtour')

        with tenancy_bypassed():
            assert StadiumTour._archives.filter(pk=a_pk).exists()
            assert StadiumTour.objects.filter(pk=b_pk).exists()

    def test_raw_sql_against_the_leaf_table_is_scoped(self, tenants):
        with tenant(label=tenants.a):
            assert _count('testapp_stadiumtour') == 1
        with tenancy_bypassed():
            assert _count('testapp_stadiumtour') == 2

    def test_an_unscoped_read_of_the_leaf_is_denied(self, tenants):
        with pytest.raises(TenantScopeError):
            StadiumTour.objects.count()

    def test_a_scoped_hard_delete_clears_the_whole_chain(self, tenants):
        with tenant(label=tenants.a):
            tenants.tour_a.hard_delete()

        with tenancy_bypassed():
            # No orphaned ancestor row anywhere in the chain.
            assert _count('testapp_stadiumtour') == 1
            assert _count('testapp_worldtour') == 1
            assert _count('testapp_tour') == 1

    def test_deleting_the_leaf_soft_deletes_via_the_redirect_rule(self, tenants):
        pk = tenants.tour_a.pk

        with tenant(label=tenants.a):
            tenants.tour_a.delete()

            assert not StadiumTour.objects.filter(pk=pk).exists()
            assert StadiumTour._archives.filter(pk=pk).exists()
            # The child row is preserved, which is what the redirect rule is for.
            assert _count('testapp_stadiumtour') == 1


# ──────────────── dimensions a policy cannot cover ─────────────────────── #


class TestMultiHopDimension:
    """``Review`` scopes through a relation, so it has no tenant column of its own.

    Python still scopes it; the database cannot. Half-supporting that would be worse than
    naming it, which is what the generator's skip note does.
    """

    @pytest.fixture
    def reviews(self, tenants):
        with tenancy_bypassed():
            return (
                Review.objects.create(body='ra', release=tenants.release_a),
                Review.objects.create(body='rb', release=tenants.release_b),
            )

    def test_python_scoping_still_applies(self, reviews, tenants):
        with tenant(label=tenants.a):
            assert list(Review.objects.values_list('body', flat=True)) == ['ra']

    def test_an_unscoped_read_is_still_denied(self, reviews):
        with pytest.raises(TenantScopeError):
            Review.objects.count()

    def test_the_table_carries_no_policy(self, reviews):
        """Asserted against the database, because the alternative to a *named* gap is a
        policy that looks like protection and predicates on nothing."""
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT count(*) FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid '
                'WHERE c.relname = %s',
                ['testapp_review'],
            )
            assert cursor.fetchone()[0] == 0

    def test_raw_sql_is_therefore_not_scoped(self, reviews):
        """The honest consequence, pinned. This is what the skip note is warning about."""
        with tenancy_bypassed():
            assert _count('testapp_review') == 2


class TestHandDeclaredManager:
    """``Booking`` composes ``TenantedManager`` over ``LiveManager`` itself.

    The path a project takes to scope a model without moving it to the ``GuitarModel`` rung.
    """

    def test_the_scoped_manager_filters(self, bookings, tenants):
        with tenant(label=tenants.a):
            assert list(Booking.objects.values_list('venue', flat=True)) == ['Aardvark Arena']

    def test_an_unscoped_read_is_denied(self, bookings):
        with pytest.raises(TenantScopeError):
            Booking.objects.count()

    def test_only_the_declared_manager_is_scoped(self, bookings):
        """The asymmetry is real, and it is the argument for the ``GuitarModel`` rung.

        Only ``objects`` was wrapped, so ``_archives`` and ``_all_objects`` still answer
        unscoped -- in *Python*. The row-level-security policy does not care which manager
        asked, so what comes back is still only what the caller may see; here, with no scope
        active, that is nothing.
        """
        assert Booking._all_objects.count() == 0

        with tenancy_bypassed():
            assert Booking._all_objects.count() == 2

    def test_autofill_is_not_assumed_for_a_hand_declared_manager(self, tenants):
        """``GUITARS_TENANT_AUTOFILL`` defaults to False, and this model leaves it there.

        ``GuitarModel`` passes ``autofill=True`` for the field it owns, because that field is
        framework-owned and ``editable=False``. A field the project declared itself is its
        own business, so the default stands and the write is refused rather than guessed at.
        """
        with tenant(label=tenants.a), pytest.raises(TenantScopeError, match='is missing'):
            Booking.objects.create(venue='Nowhere')

    def test_an_explicit_tenant_satisfies_it(self, tenants):
        with tenant(label=tenants.a):
            booking = Booking.objects.create(venue='Somewhere', label=tenants.a)

        assert booking.label_id == tenants.a.pk

    def test_the_policy_still_covers_it(self, bookings, tenants):
        """It has a local tenant column, so it is policy-eligible like any other."""
        with tenant(label=tenants.a):
            assert _count('testapp_booking') == 1


class TestNonNullTenantIsEnforcedByTheDatabase(object):
    def test_a_null_tenant_cannot_be_written(self, tenants):
        """The FK is non-null, so even the bypass cannot create an orphan row.

        Worth pinning: a nullable tenant column would produce rows no scope matches and no
        policy hides -- invisible to every tenant and to the bypass alike.
        """
        with tenancy_bypassed(), pytest.raises(IntegrityError):
            Release.objects.create(title='orphan', label=None)
