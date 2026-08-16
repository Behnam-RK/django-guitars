"""Relocating a tenant autofill trigger onto the MTI ancestor owning the column (ADR 0009).
A trigger on the child's table could not write the ancestor's column, so it goes there --
but only when every model sharing that one trigger agrees it should exist."""

from __future__ import annotations

import types

import pytest
from django.apps import apps as django_apps

from guitars.management.commands.makeguitarmigrations import Command
from guitars.tenancy import tenancy_bypassed, tenant
from guitars.tenancy.discovery import (
    app_coverage,
    autofill_function_name,
    autofill_trigger_name,
    owner_autofill_notes,
)

from .testapp.models import Arena, Label, StadiumTour, WorldTour


pytestmark = pytest.mark.django_db


@pytest.fixture
def coverage():
    return app_coverage(django_apps.get_app_config('testapp'))


def _testapp():
    return django_apps.get_app_config('testapp')


class TestTheHappyPath:
    def test_the_child_relocates_its_dimension_to_the_owner(self, coverage):
        """``Venue`` holds the column and is not tenanted, so nothing else can carry it."""
        arena = coverage.tables['testapp_arena']

        assert arena.autofill_columns is None
        assert arena.owner_autofill_columns == {'label': 'label_id'}
        assert arena.owner_table == 'testapp_venue'

    def test_the_trigger_is_emitted_on_the_owners_table(self):
        command = Command()
        command.existing.tenant_autofill.clear()

        blob = '\n'.join(command._tenant_autofill_operations(_testapp()))

        function = autofill_function_name('label', 'label_id')
        assert f'CREATE TRIGGER "{autofill_trigger_name(function)}"' in blob
        assert 'ON "testapp_venue"' in blob
        assert 'ON "testapp_arena"' not in blob

    def test_exactly_one_trigger_lands_on_the_owners_table(self):
        """Several children can resolve to one key. `_append_if_stale` dedupes against
        history, not against operations built this run, so a duplicate would reach migrate."""
        command = Command()
        command.existing.tenant_autofill.clear()

        blob = '\n'.join(command._build_operations(_testapp()))

        assert blob.count('ON "testapp_venue"\n') == 1

    def test_the_relocated_key_is_required_so_it_is_never_retired(self):
        """Keyed on the owner's table. Keyed on the child's it would read as unrequired and
        the retirement path would drop it on the very next run."""
        command = Command()
        function = autofill_function_name('label', 'label_id')

        assert ('testapp_venue', function) in command._required_autofill_keys()

    def test_the_owners_app_hosts_the_migration(self):
        """Attributed to the app owning the table, wherever the child lives -- the same
        inversion cascade rules already use."""
        command = Command()

        assert command._table_app_labels()['testapp_venue'] == 'testapp'


class TestTheGuardRefuses:
    def test_a_sibling_opting_out_blocks_the_trigger(self, coverage):
        """An MTI insert of ``LectureHall`` writes a row into ``Hall``, so the trigger would
        stamp it -- overwriting the opt-out ADR 0005 makes auditable as an absent trigger."""
        assert coverage.tables['testapp_concerthall'].owner_autofill_columns is None
        assert coverage.tables['testapp_lecturehall'].owner_autofill_columns is None

    def test_two_dimensions_on_one_owner_column_block_the_trigger(self, coverage):
        """Two GUCs writing one column means two triggers on one table, fired in name
        order -- an ordering nobody declared, where the second silently loses."""
        assert coverage.tables['testapp_tenniscourt'].owner_autofill_columns is None
        assert coverage.tables['testapp_squashcourt'].owner_autofill_columns is None

    def test_no_trigger_is_emitted_for_either_refused_chain(self):
        command = Command()
        command.existing.tenant_autofill.clear()

        blob = '\n'.join(command._tenant_autofill_operations(_testapp()))

        assert 'ON "testapp_hall"' not in blob
        assert 'ON "testapp_court"' not in blob

    @pytest.mark.parametrize(
        ('table', 'fragment'),
        [
            ('testapp_hall', 'do not autofill'),
            ('testapp_court', 'racing on one table'),
        ],
    )
    def test_each_refusal_is_named_exactly_once(self, table, fragment):
        """Skips are design, never silent -- and ``_classify`` runs once per descendant, so
        a note emitted there would print the same fact once per child."""
        notes = [note for note in owner_autofill_notes() if table in note]

        assert len(notes) == 1
        assert fragment in notes[0]

    def test_a_column_nobody_autofills_earns_no_note(self):
        """``HeadlineFestival``'s diamond dimensions are already dropped with their own
        note. A second one about a trigger they were never going to get reads as a second
        problem -- the "two notes for one fact" this module avoids elsewhere."""
        assert not [note for note in owner_autofill_notes() if 'testapp_festival' in note]


class TestTheMtiRegression:
    """``Tour`` is itself tenanted and autofilling, so its own trigger already fires on the
    row Django writes into its table. That case was always correct and must stay untouched."""

    @pytest.mark.parametrize('model', [WorldTour, StadiumTour], ids=lambda m: m.__name__)
    def test_a_child_of_an_autofilling_owner_relocates_nothing(self, coverage, model):
        table = coverage.tables[model._meta.db_table]

        assert table.autofill_columns is None
        assert table.owner_autofill_columns is None

    def test_the_owners_table_gets_exactly_one_trigger(self):
        """Three models resolve to ``testapp_tour``. Without the "owner already autofills"
        rule each would relocate onto it and migrate would fail on the second CREATE."""
        command = Command()
        command.existing.tenant_autofill.clear()

        blob = '\n'.join(command._build_operations(_testapp()))

        assert blob.count('ON "testapp_tour"\n') == 1


class TestItStaysOutOfThePolicyIdentity:
    def test_as_kwargs_ignores_the_relocated_columns(self, coverage):
        """Leaking it re-digests every ``[POLICY:...]`` header, turning an additive release
        into "replace every policy in every consuming project"."""
        assert 'owner_autofill_columns' not in coverage.tables['testapp_arena'].as_kwargs()

    def test_the_policy_identity_is_unchanged_by_relocation(self):
        command = Command()
        coverage = app_coverage(_testapp()).tables['testapp_arena']

        with_relocation = command._policy_identity('testapp_arena', coverage)
        without = command._policy_identity(
            'testapp_arena', coverage._replace(owner_autofill_columns=None)
        )

        assert with_relocation == without


class TestScopedRuns:
    def test_a_run_scoping_out_the_owner_says_so(self):
        command = Command()
        command.existing.tenant_autofill.clear()

        notes = command._scoped_autofill_gap_notes({'other_app'})

        assert any('testapp_venue' in note and 'not in this scoped run' in note for note in notes)

    def test_an_unscoped_run_reports_no_gap(self):
        command = Command()

        assert command._scoped_autofill_gap_notes(set()) == []

    def test_an_already_written_trigger_is_not_reported_as_a_gap(self):
        """It exists; naming it would send the operator chasing a migration already on disk."""
        command = Command()

        assert command._scoped_autofill_gap_notes({'other_app'}) == []

    def test_the_function_migration_follows_the_hosting_app(self):
        """Scoped by the app hosting the trigger, not the one declaring the dimension --
        otherwise the trigger calls a function whose migration was never written."""
        command = Command()
        function = autofill_function_name('label', 'label_id')

        assert function in command._required_autofill_functions({'testapp'})
        assert function not in command._required_autofill_functions({'other_app'})


class TestAgainstTheRealDatabase:
    def test_an_insert_omitting_the_tenant_takes_the_active_scope(self):
        """The behaviour issue #28 is about: before relocation this failed on a bare NOT
        NULL, because the child's table had no trigger and the ancestor's had none either."""
        label = Label.objects.create(name='acme')

        with tenant(label=label.pk):
            arena = Arena.objects.create(name='dome', seats=100)

        assert arena.label_id == label.pk

    def test_a_bypassed_insert_still_refuses(self):
        """ADR 0005's measured matrix: the trigger declines to fill under bypass, and the
        NOT NULL is what stops a row landing with no tenant at all."""
        from django.db import IntegrityError, transaction

        with tenancy_bypassed(), pytest.raises(IntegrityError), transaction.atomic():
            Arena.objects.create(name='void', seats=1)

    def test_the_trigger_really_is_on_the_ancestors_table(self):
        from .conftest import rows

        function = autofill_function_name('label', 'label_id')
        found = rows(
            """
            SELECT c.relname FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_proc p ON p.oid = t.tgfoid
            WHERE p.proname = %s AND NOT t.tgisinternal
            """,
            [function],
        )

        assert ('testapp_venue',) in found
        assert ('testapp_arena',) not in found


class TestTheAuditFollowsTheTrigger:
    def test_a_present_relocated_trigger_is_not_drift(self):
        from guitars.management.commands.audittenancy import Command as Audit

        coverage = app_coverage(_testapp()).tables['testapp_arena']
        state = types.SimpleNamespace(
            autofill_functions=frozenset({autofill_function_name('label', 'label_id')})
        )

        assert Audit._autofill_drift('testapp_arena', coverage, {'testapp_venue': state}) == []

    def test_a_missing_relocated_trigger_names_the_ancestor(self):
        """The finding is about the child, but the fix is on a different table -- so the
        message has to say which."""
        from guitars.management.commands.audittenancy import Command as Audit

        coverage = app_coverage(_testapp()).tables['testapp_arena']
        state = types.SimpleNamespace(autofill_functions=frozenset())

        findings = Audit._autofill_drift('testapp_arena', coverage, {'testapp_venue': state})

        assert len(findings) == 1
        assert "ancestor 'testapp_venue'" in findings[0]

    def test_an_ancestor_off_the_search_path_is_its_own_finding(self):
        """Reporting it as a missing trigger would send the operator to makeguitarmigrations
        for a table this connection cannot even see."""
        from guitars.management.commands.audittenancy import Command as Audit

        coverage = app_coverage(_testapp()).tables['testapp_arena']

        findings = Audit._autofill_drift('testapp_arena', coverage, {})

        assert len(findings) == 1
        assert 'cannot be verified' in findings[0]
