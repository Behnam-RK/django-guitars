"""The refusals, the audit mode, and the branches a happy path never reaches.

Every test here is about a path that only runs when something is wrong -- a mis-set setting,
an uncoverable model shape, a value that cannot be turned into SQL. Those are exactly the
paths that rot unnoticed, because nothing in normal use touches them, so they are the ones
worth pinning hardest: a guard that has silently stopped guarding looks identical to one that
never had to.
"""

from __future__ import annotations

import ast

import pytest
from django.core.management import CommandError, call_command
from django.db import models
from django.test import override_settings

from guitars import sql
from guitars.management import _generator
from guitars.management.enforcement.identity import _literal
from guitars.models import GuitarModel, LiveManager
from guitars.tenancy import (
    TenantScopeError,
    reporting,
    tenancy_bypassed,
    tenant,
    tenanted_manager,
)
from guitars.tenancy.checks import TENANT_MODEL_ID, check_guitar_models_have_a_tenant
from guitars.tenancy.discovery import _classify
from guitars.tenancy.enforcement import ViolationKind
from tests.conftest import execute as _execute
from tests.testapp.models import Booking, Label, Release, Review, StadiumTour


# ─────────────────────── SQL that cannot be written ────────────────────── #


class TestPolicySqlRefusals:
    def test_a_nul_byte_in_a_role_name_is_refused(self):
        """``quote_ident`` cannot escape it -- PostgreSQL identifiers cannot contain NUL."""
        with pytest.raises(ValueError, match='cannot contain a NUL byte'):
            sql.create_exempt_policy(table='t', role='bad\x00role')

    def test_a_nul_byte_in_a_literal_is_refused(self):
        """Reached through the policy name, which is quoted as a literal inside EXECUTE."""
        with pytest.raises(ValueError, match='cannot contain a NUL byte'):
            sql.drop_exempt_policy(table='t', role='bad\x00role')

    def test_a_policy_with_nothing_to_predicate_on_is_refused(self):
        """Emitting one would be worse than emitting none.

        ``USING (bypass)`` denies every scoped read on the table, which reads as a broken
        deployment rather than as the configuration mistake it is.
        """
        with pytest.raises(ValueError, match='refusing to emit a policy'):
            sql.create_tenant_policy(table='t', columns={})

    def test_an_owner_join_without_its_keys_is_refused(self):
        with pytest.raises(ValueError, match='needs owner_table, owner_pk and child_pk'):
            sql.create_tenant_policy(table='t', columns={}, owner_columns={'label': 'label_id'})


class TestForceIsSeparableFromTheRest:
    """``force=False`` is the whole staged-retrofit workflow, so the shape it emits is pinned.

    ``GUITARS_RLS_FORCE = False`` exists to ship policies *inert* onto a populated database:
    the app role owns its tables and an owner bypasses non-``FORCE`` RLS silently, so the
    policy can be soaked before it binds. Nothing asserted that the flag actually removes the
    statement, and statement coverage could not notice -- ``if force:`` runs either way.
    """

    def test_force_false_omits_the_alter_and_changes_nothing_else(self):
        forced = sql.create_table_rls(table='t', columns={'label': 'label_id'}, force=True)
        inert = sql.create_table_rls(table='t', columns={'label': 'label_id'}, force=False)

        assert forced[-1] == sql.force_rls(table='t')
        assert inert == forced[:-1], 'force=False must drop the ALTER and nothing more'
        # Still ENABLE'd: a policy that is not enabled constrains nobody, forced or not.
        assert sql.enable_rls(table='t') in inert

    def test_the_default_is_forced(self):
        """A library must not ship a security feature that is inert by default -- ADR 0002."""
        assert sql.force_rls(table='t') in sql.create_table_rls(
            table='t', columns={'label': 'label_id'}
        )


# ────────────────────── model shapes that cannot be covered ────────────── #


class TestDimensionsOnTwoAncestors:
    """One correlated subquery reaches one ancestor, so all such dimensions are dropped.

    Picking one would make the policy's strength depend on field declaration order -- a policy
    whose strength varies with that is worse than a named gap.

    Only the *spec* is stubbed. Declaring throwaway concrete models for this would register
    them in the test app and make every migration command in the suite report them as missing,
    so instead the real three-level MTI chain is reused and told it is scoped on two columns
    that genuinely live on two different ancestors: ``label`` on ``Tour`` and ``continents`` on
    ``WorldTour``. The ownership resolution under test is therefore real; only the declaration
    that reaches it is faked.
    """

    @pytest.fixture
    def spread_spec(self, monkeypatch):
        spec = {'label': 'label', 'region': 'continents'}
        # Patched in both namespaces: discovery imported the function object, and
        # local_tenant_fields calls its own module-level reference.
        for module in ('guitars.tenancy.discovery', 'guitars.tenancy.spec'):
            monkeypatch.setattr(f'{module}.tenant_spec', lambda model, _spec=spec: _spec)
        return spec

    def test_no_owner_join_is_emitted(self, spread_spec):
        coverage, notes = _classify(StadiumTour)

        assert coverage is None or coverage.owner_columns is None
        assert any('more than one ancestor' in note for note in notes)

    def test_the_note_names_the_ancestors_and_what_is_left(self, spread_spec):
        _, notes = _classify(StadiumTour)

        note = next(note for note in notes if 'more than one ancestor' in note)
        assert 'testapp_tour' in note
        assert 'testapp_worldtour' in note
        # It has to say what *is* enforced, not just what was dropped: "skipped" alone reads as
        # "no protection here" on a table that may have some.
        assert 'no policy is emitted' in note or 'still enforces' in note

    def test_the_dropped_dimensions_are_named(self, spread_spec):
        _, notes = _classify(StadiumTour)

        note = next(note for note in notes if 'more than one ancestor' in note)
        assert "'label'" in note
        assert "'region'" in note

    def test_it_is_reported_once_and_not_also_as_uncoverable(self, spread_spec):
        """One fact, one note.

        The uncoverable-model note says the dimensions "have no column on this table or a
        shared-key ancestor", which is the opposite of what happened here -- they have
        columns on *two* ancestors. Emitting both read as two separate problems, and sent
        the reader looking for a missing column that is not missing.
        """
        _, notes = _classify(StadiumTour)

        assert len(notes) == 1
        assert 'skipped:' not in notes[0]


# ─────────────────────────────── audit mode ────────────────────────────── #


class TestAuditMode:
    """Report and proceed, for rolling enforcement onto a populated deployment.

    **Audit mode softens the Python layer only.** Once a ``tenant_scope`` policy binds, the
    database refuses a cross-tenant write regardless of the setting -- there is no session
    variable that says "enforce, but leniently". So audit mode is a tool for the *first* stage
    of a rollout: adopt the managers with ``GUITARS_TENANT_POLICIES = False`` (or before
    migrating the policies), let it name every offending call site, fix them, then turn on
    strict and the policies together.

    These tests run with the policy dropped for the duration, which is that configuration.
    ``TestAuditModeDoesNotSoftenTheDatabase`` covers what happens if you skip the ordering.
    """

    @pytest.fixture(autouse=True)
    def _audit_mode(self):
        """``override_settings`` as a fixture, not a class decorator: Django's decorator only
        supports ``SimpleTestCase`` subclasses, and these are plain pytest classes."""
        reporting.reset_reported()
        with override_settings(GUITARS_TENANT_ENFORCE='audit'):
            yield
        reporting.reset_reported()

    @pytest.fixture
    def unpolicied(self, db):
        """Drop the ``testapp_release`` policy for this test.

        DDL is transactional in PostgreSQL and the test runs inside a transaction, so this is
        undone with everything else. It models the stage of a rollout where the Python layer
        is in place and the database layer is not yet.
        """
        _execute(*sql.drop_table_rls(table=Release._meta.db_table))
        return None

    @pytest.fixture
    def sink(self):
        found = []
        reporting.set_reporter(lambda message, **context: found.append((message, context)))
        yield found
        reporting.set_reporter(reporting._log_reporter)

    def test_a_cross_tenant_write_is_reported_and_proceeds(self, unpolicied, tenants, sink):
        with tenant(label=tenants.a):
            Release(title='crossing', label=tenants.b).save()

        assert any('may not cross tenants' in message for message, _ in sink)
        with tenancy_bypassed():
            assert Release.objects.filter(title='crossing').exists()

    def test_a_cross_tenant_write_reports_structured_context_not_just_a_message(
        self, unpolicied, tenants, sink
    ):
        """A reporter that forwards to Sentry needs to classify programmatically, not
        regex the message -- ``kind``/``model``/``dimension`` are what make that possible.
        """
        with tenant(label=tenants.a):
            Release(title='crossing', label=tenants.b).save()

        _, context = next(pair for pair in sink if 'may not cross tenants' in pair[0])
        assert context['kind'] is ViolationKind.MISMATCH
        assert context['dimension'] == 'label'
        assert 'Release' in context['model']

    def test_an_unscoped_create_is_reported_and_proceeds(self, unpolicied, tenants, sink):
        Release.objects.create(title='unscoped', label=tenants.a)

        assert any('needs an active tenant scope' in message for message, _ in sink)

    def test_an_unscoped_create_reports_its_kind_as_unscoped(self, unpolicied, tenants, sink):
        Release.objects.create(title='unscoped', label=tenants.a)

        _, context = next(pair for pair in sink if 'needs an active tenant scope' in pair[0])
        assert context['kind'] is ViolationKind.UNSCOPED
        assert context['action'] == 'create'
        assert context['model'] == 'Release'

    def test_an_unscoped_bulk_create_is_reported_and_still_guarded(
        self, unpolicied, tenants, sink
    ):
        """The batched path fires no ``pre_save``, so the guard is invoked by hand.

        Without that, ``bulk_create`` would validate strictly less than ``create`` in the one
        mode whose entire purpose is finding out what is wrong.
        """
        Release.objects.bulk_create([Release(title='batched', label=tenants.a)])

        assert any('bulk_create' in message for message, _ in sink)

    def test_a_finding_is_reported_once_per_distinct_cause(self, unpolicied, tenants, sink):
        """A hot query path must not emit thousands of identical events."""
        with tenant(label=tenants.a):
            for index in range(5):
                Release(title=f'crossing-{index}', label=tenants.b).save()

        assert len(sink) == 1

    def test_reads_still_deny_in_audit_mode(self, unpolicied, tenants):
        """Audit mode softens *writes*.

        A read has a value to return, and returning every tenant's rows would be the leak
        itself -- there is no "report and proceed" that is not also a disclosure.
        """
        with pytest.raises(TenantScopeError):
            Release.objects.count()

    def test_a_reporter_that_raises_cannot_break_the_query(self, unpolicied, tenants, caplog):
        """The whole point of audit mode is to observe without breaking."""

        def exploding(message, **context):
            raise RuntimeError('sink is down')

        reporting.set_reporter(exploding)
        try:
            with tenant(label=tenants.a):
                Release(title='crossing', label=tenants.b).save()
        finally:
            reporting.set_reporter(reporting._log_reporter)

        assert 'reporter failed' in caplog.text

    def test_the_default_reporter_logs(self, unpolicied, tenants, caplog):
        """A project with no Sentry or structlog still gets the finding somewhere."""
        with caplog.at_level('WARNING', logger='guitars.tenancy'), tenant(label=tenants.a):
            Release(title='crossing', label=tenants.b).save()

        assert 'may not cross tenants' in caplog.text


class TestAuditModeDoesNotSoftenTheDatabase:
    """The ordering constraint, pinned rather than left as advice.

    Someone who sets ``audit`` expecting no 500s, on a database whose policies already bind,
    gets 500s -- from the database, after the Python layer has already waved the write
    through. Better to have that as a test than as a surprise.
    """

    @pytest.fixture(autouse=True)
    def _audit_mode(self):
        reporting.reset_reported()
        with override_settings(GUITARS_TENANT_ENFORCE='audit'):
            yield
        reporting.reset_reported()

    def test_the_policy_still_refuses_a_cross_tenant_write(self, tenants):
        with (
            tenant(label=tenants.a),
            pytest.raises(TenantScopeError, match='rejected by a tenant policy'),
        ):
            Release(title='crossing', label=tenants.b).save()

    def test_the_finding_was_still_reported_first(self, tenants, caplog):
        """So the audit log is useful even though the write failed -- the Python layernames the
        call site, which the database error cannot."""
        with caplog.at_level('WARNING', logger='guitars.tenancy'), tenant(label=tenants.a):
            with pytest.raises(TenantScopeError):
                Release(title='crossing', label=tenants.b).save()

        assert 'may not cross tenants' in caplog.text

    def test_audittenancy_warns_about_the_combination(self, db):
        """The audit command is the one place that can see both halves at once: it knows the
        setting and it just asked the database whether the policies bind."""
        from io import StringIO

        out = StringIO()
        call_command('audittenancy', stdout=out, stderr=out)

        assert 'GUITARS_TENANT_ENFORCE' in out.getvalue()
        assert 'audit' in out.getvalue()


class TestEnforcementSettingIsValidated:
    def test_a_bad_mode_is_a_system_check_error(self):
        with override_settings(GUITARS_TENANT_ENFORCE='lenient'):
            from guitars.tenancy.checks import check_tenancy_settings

            errors = check_tenancy_settings(None)

        assert [error.id for error in errors] == ['guitars.tenancy.E001']

    def test_a_string_autofill_is_a_system_check_error(self):
        """``'False'`` would read as True through ``bool()`` -- enabling what it spells out
        as disabled."""
        with override_settings(GUITARS_TENANT_AUTOFILL='False'):
            from guitars.tenancy.checks import check_tenancy_settings

            errors = check_tenancy_settings(None)

        assert [error.id for error in errors] == ['guitars.tenancy.E002']

    def test_a_bad_mode_raises_at_write_time_too(self, tenants):
        """The check is a convenience, not the enforcement: the guard itself still refuses
        rather than guessing which mode was meant."""
        with (
            override_settings(GUITARS_TENANT_ENFORCE='lenient'),
            tenant(label=tenants.a),
            pytest.raises(ValueError, match='lenient'),
        ):
            Release(title='x', label=tenants.b).save()


class TestTheTenantModelCheckInProcess:
    def test_it_names_every_offending_model(self, monkeypatch):
        """The harness *does* configure tenancy, so the unwired state is simulated here.

        ``tests/test_ladder.py`` proves the real import-time behaviour in subprocesses; this
        covers the check's own logic, which is what decides the message a developer reads.
        """
        monkeypatch.setattr(GuitarModel, '_guitars_tenancy_installed', False)

        errors = check_guitar_models_have_a_tenant(None)

        assert [error.id for error in errors] == [TENANT_MODEL_ID]
        for model in ('testapp.Release', 'testapp.Track', 'testapp.Tour'):
            assert model in errors[0].msg


# ───────────────────────── manager-level refusals ──────────────────────── #


class TestAutofillRefusals:
    def test_autofill_on_a_multi_hop_dimension_is_rejected_at_declaration(self):
        """Not silently ignored. There is no column to fill, so asking for it is a mistake
        worth naming where it was made rather than where it fails to happen."""
        with pytest.raises(TypeError, match='needs a dimension stored on this table'):
            tenanted_manager(_manager_class=LiveManager, autofill=True, label='release__label')

    def test_a_queryset_as_a_scope_value_is_rejected(self):
        """``str()`` on a QuerySet runs a query -- inside the publish, which re-enters the
        publish. The real symptom is a RecursionError from somewhere unrelated."""
        with pytest.raises(TypeError, match='got a QuerySet'):
            with tenant(label=Label.objects.all()):
                pass

    def test_a_manager_instance_is_accepted_as_well_as_a_class(self):
        """``QuerySet.as_manager()`` hands back an instance, and subclassing one fails with a
        baffling ``BaseManager.__init__() takes 1 positional argument``."""
        manager = tenanted_manager(_manager_class=LiveManager(), label='label')

        assert manager._tenant_dimensions == {'label': 'label'}


class TestUnscopedQuerysetSurface:
    def test_none_stays_usable(self, db):
        """Framework-level empties must still resolve, so ``.none()`` is deliberately not
        denied -- it cannot disclose a row."""
        assert list(Release.objects.none()) == []

    def test_iterator_is_denied_by_name(self, db):
        """``iterator()`` streams without populating ``_result_cache``, so it skips the
        ``_fetch_all`` chokepoint every other read funnels through."""
        with pytest.raises(TenantScopeError):
            list(Release.objects.iterator())

    def test_a_custom_queryset_method_raises_the_right_error(self, db):
        """The denying queryset subclasses the manager's own, so ``lives`` exists and refuses
        rather than raising AttributeError and naming the wrong problem."""
        with pytest.raises(TenantScopeError):
            Release.objects.all().lives.count()

    def test_aggregate_is_denied(self, db):
        with pytest.raises(TenantScopeError):
            Release.objects.aggregate(models.Count('id'))


# ────────────────────────── generator edge paths ───────────────────────── #


class TestLiteralRendering:
    """Migrations are rendered deterministically or every run emits a new one."""

    def test_dicts_sort(self):
        assert _literal({'b': '2', 'a': '1'}) == "{'a': '1', 'b': '2'}"

    def test_lists_and_tuples_render_as_lists(self):
        assert _literal(['a', 'b']) == "['a', 'b']"
        assert _literal(('a', 'b')) == "['a', 'b']"

    def test_other_values_fall_back_to_repr(self):
        assert _literal(True) == 'True'
        assert _literal(None) == 'None'

    def test_nesting_is_sorted_all_the_way_down(self):
        assert _literal({'k': ['z', 'a']}) == "{'k': ['z', 'a']}"

    def test_ordinary_identifiers_render_single_quoted(self):
        """The shape every real table and column takes -- and the digests already on disk."""
        assert _literal('testapp_release') == "'testapp_release'"
        assert _literal({'label': 'label_id'}) == "{'label': 'label_id'}"

    def test_awkward_strings_stay_valid_python(self):
        """A hand-built f"'{value}'" would emit a syntax error here, or eat the backslash.

        Unreachable through ``sql.policy._bare``, which refuses anything but a plain
        lower-case identifier -- but the refusal happens at *migrate* time, in the consuming
        project, and a migration file that cannot even be imported is a worse way to find
        out than the ValueError that was supposed to say so.
        """
        for value in ("o'brien", 'back\\slash', 'new\nline'):
            assert ast.literal_eval(_literal(value)) == value


class TestGeneratorSettings:
    def test_policies_can_be_switched_off_entirely(self, db):
        """``GUITARS_TENANT_POLICIES = False`` keeps the Python layer and leaves the database
        alone -- for adopting the loud layer first, or for a database whose application role
        cannot own its tables and so could never be constrained by RLS anyway."""
        with override_settings(GUITARS_TENANT_POLICIES=False):
            from io import StringIO

            out = StringIO()
            call_command('makeguitarmigrations', '--check', stdout=out, stderr=out)

        # Nothing missing: with policies off, the already-written tenant operations are not
        # expected and no new ones are wanted.
        assert 'Missing' not in out.getvalue()

    def test_force_rls_writes_nothing_when_force_shipped_inline(self, db):
        """The default is ``GUITARS_RLS_FORCE = True``, so every policy already carries FORCE.

        It used to emit a redundant FORCE migration per tenanted table here, because it keyed
        only on "a policy exists and has no separate FORCE operation" -- which is true of every
        policy generated with FORCE inline. It now reads the ``force=`` literal the operation
        was written with, so a second stage only acts on policies that really shipped inert.
        """
        from io import StringIO

        out = StringIO()
        call_command('makeguitarmigrations', '--force-rls', stdout=out, stderr=out)
        output = out.getvalue()

        assert 'already emit FORCE' in output
        assert 'No changes detected' in output

    def test_force_rls_is_quiet_in_the_configuration_it_exists_for(self, db):
        """``GUITARS_RLS_FORCE = False`` is the retrofit the flag was built for.

        Its sibling above covers the *finished* retrofit, where the setting is back on and the
        warning is the point. Here the setting is still off, so there is nothing to warn about
        -- and a stage that lectured the operator every time they ran it in the intended
        configuration is a stage they learn to ignore. This repo's own policies all shipped
        forced, so there is still no backlog to act on; what is asserted is the absence of the
        notice, not the absence of work.
        """
        from io import StringIO

        out = StringIO()
        with override_settings(GUITARS_RLS_FORCE=False):
            call_command('makeguitarmigrations', '--force-rls', stdout=out, stderr=out)
        output = out.getvalue()

        assert 'already emit FORCE' not in output
        assert 'No changes detected' in output

    def test_force_rls_says_so_when_policies_are_off(self, db):
        from io import StringIO

        out = StringIO()
        with override_settings(GUITARS_TENANT_POLICIES=False):
            call_command('makeguitarmigrations', '--force-rls', stdout=out, stderr=out)

        assert 'no tenant policies exist to force' in out.getvalue()

    def test_exempt_roles_reach_the_generated_operation(self, db):
        """Written into the migration as a literal, so the same history always produces the
        same database -- and so the reverse drops exactly what the forward created."""
        from django.apps import apps as django_apps

        from guitars.management.enforcement.command import Command
        from guitars.tenancy.discovery import app_coverage

        coverage = app_coverage(django_apps.get_app_config('testapp'))
        with override_settings(GUITARS_RLS_EXEMPT_ROLES=['metabase_ro']):
            operation, _ = Command()._tenant_policy_operation(
                'testapp_release', coverage.tables['testapp_release'], replacing=False
            )

        # The role reaches the operation as SQL now, not as an `exempt_roles=[...]` keyword:
        # settings are resolved at generation time and the statements written literally, so
        # editing the setting later cannot change what an applied migration means.
        assert 'CREATE POLICY "rls_exempt_metabase_ro" ON testapp_release' in operation
        assert 'TO "metabase_ro"' in operation
        # And in the reverse, or the drop would miss the exemption policy the forward created.
        assert 'DROP POLICY IF EXISTS "rls_exempt_metabase_ro" ON testapp_release' in operation


class TestScaffoldingFailsLoudly:
    def test_an_unparseable_makemigrations_output_is_an_error(self, monkeypatch):
        """Django prints the path it wrote rather than returning it.

        Guessing -- rewriting whichever file a glob found first -- would corrupt an unrelated
        migration, so a failure to match the filename is raised instead.
        """
        monkeypatch.setattr(_generator, 'call_command', lambda *a, **k: None)
        app = __import__('django.apps', fromlist=['apps']).apps.get_app_config('testapp')

        with pytest.raises(CommandError, match='Could not find the created migration file'):
            _generator.create_empty_migration_file(app, 'whatever')


class TestUncoverableModelsStillScope:
    def test_the_multi_hop_model_has_no_local_tenant_field(self):
        """What makes it uncoverable, asserted at the source rather than via the note."""
        from guitars.tenancy.spec import local_tenant_fields, tenant_spec

        assert tenant_spec(Review) == {'label': 'release__label'}
        assert local_tenant_fields(Review) == {}

    def test_a_hand_declared_manager_reports_its_local_field(self):
        from guitars.tenancy.spec import local_tenant_fields

        assert local_tenant_fields(Booking) == {'label': 'label'}

    def test_an_untenanted_model_has_no_spec(self):
        from guitars.tenancy.spec import tenant_spec

        assert tenant_spec(Label) == {}
