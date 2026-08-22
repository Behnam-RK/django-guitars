"""The ``makeguitarmigrations`` command: wires ``headers``/``identity``/``scanning``/
``operations`` into the CLI entry point (vocabulary in ``CONTEXT.md``). Header strings are
**frozen** -- reword one and every existing migration stops being recognised."""

from __future__ import annotations

import textwrap
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from django.apps import apps as django_apps
from django.conf import settings
from django.core.management import CommandError
from django.core.management.base import BaseCommand
from django.db import models

from guitars import sql
from guitars.introspection import is_mti_child, owns_column
from guitars.management import _generator
from guitars.management.enforcement.graph import ObjectRef
from guitars.management.enforcement.headers import (
    HEADER_PARENT_TRIGGER_FUNCTION,
    HEADER_TENANT_AUTOFILL_FUNCTION,
    HEADER_TRIGGER_FUNCTION,
)
from guitars.management.enforcement.identity import _operation
from guitars.management.enforcement.operations import OperationsMixin, OwnerArm
from guitars.management.enforcement.scanning import ExistingOperations, scan_existing_operations
from guitars.sql import _identifiers
from guitars.sql import triggers as _triggers
from guitars.tenancy.discovery import owner_autofill_notes, tenant_policies_enabled


if TYPE_CHECKING:
    from django.apps import AppConfig
    from django.db.migrations.loader import MigrationLoader


class Command(OperationsMixin, BaseCommand):
    """Generates enforcement migrations, driven by the shape of the model
    (``_updated_at`` -> trigger, ``_deleted_at`` -> soft-delete rule, ``tenanted_manager()``
    -> RLS policy). MTI columns resolve via ``guitars.introspection``, never ``hasattr``."""

    help = (
        'Creates enforcement migrations (timestamp triggers, soft-delete rules, tenant policies).'
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.all_models: list[type[models.Model]] = []
        self.reverse_relations_mapping: defaultdict[type[models.Model], set] = defaultdict(set)
        self._setup_models_and_reverse_relations()

        # (app_label, migration_stem) tuples or None, pointing at the singleton function
        # migrations. Populated from self.existing on first access, not here -- see that
        # property.
        self.trigger_function_dependency: tuple[str, str] | None = None
        self.parent_trigger_function_dependency: tuple[str, str] | None = None
        self.trigger_function_sql: str | None = None
        self.parent_trigger_function_sql: str | None = None
        # Keyed by function name, not singletons: autofill is one function per (column, GUC)
        # pair -- normally one, since GUITARS_TENANT_FIELD is project-wide.
        self.tenant_autofill_dependencies: dict[str, tuple[str, str]] = {}
        self.tenant_autofill_sql: dict[str, str | None] = {}

        # Rules skipped this run, surfaced as warnings (not silent): cross-app / MTI cascade
        # rules, the owned rules whose three shapes the generator refuses, and either kind
        # refused for closing an ON UPDATE rule cycle. One list, one report loop.
        self._mti_cascade_warnings: list[str] = []
        # Two relations whose rules would share one name on one table. Emitted anyway -- the
        # cascade spelling is frozen -- so the report is the only thing standing between a
        # silent replacement and the operator. See ``_claim_rule_name``.
        self._rule_name_clashes: list[str] = []
        # Owned rules refused this run that the project *already* recorded, so the stale one is
        # live in every migrated database. Errors, and they fail ``--check``: refusing emits
        # nothing, so nothing else would notice. See ``_refuse_owned``.
        self._refusals_over_live_rules: list[str] = []
        # Enforcement migrations naming another app's table with nothing ordering them
        # against its creation. Errors, and they fail ``--check``: a fresh `migrate` fails
        # on them while an incremental one passes, so nothing else notices. See ADR 0013.
        self._missing_edges: list[str] = []
        self._claimed_rule_names: dict[tuple[str, str], tuple] = {}
        # Tables tenancy discovery could not cover, with the reason. Also surfaced.
        self._tenancy_notes: list[str] = []

        self._existing: ExistingOperations | None = None

        # Project-wide autofill maps, memoised for one command: `_build_operations` runs per
        # in-scope app and each sweeps *every* local app's coverage. Safe to cache -- they read
        # only the model registry and GUITARS_TENANT_POLICIES, neither moving mid-`handle()`.
        self._table_app_labels_cache: dict[str, str] | None = None
        # Lazy, not built here: the graph reads ``self.all_models``, which a caller can *replace*
        # after construction -- the generation tests do, ``isolate_apps`` swapping ``Options.apps``
        # rather than the registry this constructor read. Building it here freezes the old answer.
        self._rule_cycle_cache: set[tuple[str, str]] | None = None
        # Lazy for the same reason, and registry-wide for the reason ``_owner_arms`` gives.
        self._owner_arms_cache: dict[str, list[OwnerArm]] | None = None
        # The other half of that shared answer, cached and lazy for the same two reasons -- and
        # ``None`` rather than ``{}`` as the "not swept yet" mark, an empty verdict being the
        # ordinary answer for a project with no tenancy at all.
        self._owned_tenancy_cache: dict[tuple[str, str, str], list[str]] | None = None
        # Objects the operations built for an app name, keyed by that app: a rule's action is
        # parsed at CREATE time, so anything it references in *another* app needs a dependency
        # edge. Filled as the rules are built and read back per app -- see ``_object_refs``.
        self._object_refs: dict[str, list[ObjectRef]] = {}
        # The project's migration graph, shared by the two readers of it below and dropped
        # whenever this command writes a file. ``None`` is "not built"; building one imports
        # every migration module in the project, so it is worth not doing per app.
        self._loader_cache: MigrationLoader | None = None
        self._required_autofill_cache: dict[tuple[str, str], tuple[str, str]] | None = None
        self._relocated_autofill_cache: dict[tuple[str, str], tuple[str, str]] | None = None

    @property
    def existing(self) -> ExistingOperations:
        """Every enforcement operation already on disk, scanned once and cached. Not eager
        in ``__init__``: Django constructs a ``Command()`` for ``--help`` and the registry,
        neither needing a filesystem scan of every local app's migrations."""
        if self._existing is None:
            self._existing = scan_existing_operations()
            self.trigger_function_dependency = self._existing.trigger_function_dependency
            self.parent_trigger_function_dependency = (
                self._existing.parent_trigger_function_dependency
            )
            self.trigger_function_sql = self._existing.trigger_function_sql
            self.parent_trigger_function_sql = self._existing.parent_trigger_function_sql
            self.tenant_autofill_dependencies = dict(
                self._existing.tenant_autofill_function_dependencies
            )
            self.tenant_autofill_sql = dict(self._existing.tenant_autofill_function_sql)
        return self._existing

    def add_arguments(self, parser):
        parser.add_argument(
            'args',
            metavar='app_label',
            nargs='*',
            help='Optional app labels to scope generation to (default: all LOCAL_APPS).',
        )
        parser.add_argument(
            '--check',
            action='store_true',
            dest='check_only',
            help=(
                'Exit with a non-zero status if model changes are missing migrations '
                "and don't actually write them."
            ),
        )
        parser.add_argument(
            '--adopt',
            action='store_true',
            dest='adopt',
            help=(
                'Re-emit every enforcement operation for the apps in scope, in a form that '
                'is correct whether or not the database object already exists. For adopting '
                'a database whose triggers, rules or policies were created outside this '
                'command -- by hand, or by another generator whose comment headers this one '
                'cannot read.'
            ),
        )
        parser.add_argument(
            '--force-rls',
            action='store_true',
            dest='force_rls',
            help=(
                'Generate FORCE ROW LEVEL SECURITY migrations for tables whose tenant '
                'policies already exist, and nothing else. Only needed when '
                'GUITARS_RLS_FORCE is False, which defers FORCE to a second stage for a '
                'retrofit onto a populated database.'
            ),
        )

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    @staticmethod
    def _tenant_policies_enabled() -> bool:
        """Whether to emit tenant policies at all -- delegated, so the refusal this gates and
        the one ``hard_delete()`` mirrors cannot come to read different settings."""
        return tenant_policies_enabled()

    @staticmethod
    def _rls_force_enabled() -> bool:
        return bool(getattr(settings, 'GUITARS_RLS_FORCE', True))

    @staticmethod
    def _rls_exempt_roles() -> list[str]:
        return list(getattr(settings, 'GUITARS_RLS_EXEMPT_ROLES', []))

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def _setup_models_and_reverse_relations(self) -> None:
        """Populate ``all_models`` and ``reverse_relations_mapping`` from installed apps."""
        for app in django_apps.get_app_configs():
            self.all_models.extend(app.get_models())

        for model in self.all_models:
            for field in model._meta.get_fields():
                if isinstance(field, models.ForeignKey):
                    self.reverse_relations_mapping[field.related_model].add(
                        (model, field, field.remote_field.on_delete)
                    )

    # ------------------------------------------------------------------
    # Migration-file helpers
    # ------------------------------------------------------------------

    def _get_trigger_function_host_app(self) -> AppConfig:
        """Return the ``AppConfig`` that will host the singleton trigger-function migration."""
        host_app_name = getattr(settings, 'TRIGGER_FUNCTION_APP', None) or settings.LOCAL_APPS[0]
        host_app_label = host_app_name.rsplit('.', 1)[-1]
        return django_apps.get_app_config(host_app_label)

    @staticmethod
    def _write_migration_file(
        app: AppConfig,
        migration_file: str,
        operations: list[str],
        operations_digest: str,
        dependencies: list[tuple[str, str]] | None = None,
    ) -> None:
        """Rewrite a migration file to include the given custom *operations* -- thin
        wrapper naming this command's marker text; mechanics live in ``_generator``."""
        _generator.write_migration_file(
            app=app,
            migration_file=migration_file,
            operations=operations,
            operations_digest=operations_digest,
            generated_by='makeguitarmigrations',
            dependencies=dependencies,
        )

    # ------------------------------------------------------------------
    # Trigger function migration
    # ------------------------------------------------------------------

    def _ensure_function_migration(
        self,
        *,
        recorded: tuple[str, str] | None,
        recorded_digest: str | None,
        header: str,
        create: str,
        replace: str,
        drop: str,
        name: str,
        missing_message: str,
        stale_message: str,
        check_only: bool,
        adopt: bool = False,
        dependencies: list[tuple[str, str]] | None = None,
    ) -> tuple[tuple[str, str], str] | None:
        """Ensure the host app has a current migration for one singleton trigger function.
        Compares the recorded ``[SQL:...]`` digest, not mere existence, so an edited body
        reships via ``OR REPLACE`` (``DROP FUNCTION`` refuses while a trigger depends on it)."""
        current_source, current_digest = _operation(header, create, drop)
        if recorded is not None and (recorded_digest == current_digest and not adopt):
            return None

        stale = recorded is not None
        if check_only:
            # Bare message, not self.style.ERROR(...): Django's own handler applies
            # style.ERROR() again on an uncaught CommandError, and double-wrapping garbled
            # the ANSI codes.
            raise CommandError(stale_message if stale else missing_message)

        if stale or adopt:
            current_source, _ = _operation(header, create, drop, emit=replace)

        host_app = self._get_trigger_function_host_app()
        migration_file = _generator.create_empty_migration_file(host_app, name=name)
        self._write_migration_file(
            app=host_app,
            migration_file=migration_file,
            operations=[current_source],
            operations_digest=_generator.digest_of([current_source]),
            dependencies=dependencies,
        )

        self.stdout.write(
            self.style.MIGRATE_HEADING(f"Enforcement migrations for '{host_app.label}':")
        )
        self.stdout.write(f'  migrations/{migration_file}')
        return (host_app.label, Path(migration_file).stem), current_digest

    def _ensure_trigger_function_migration(
        self, *, check_only: bool = False, adopt: bool = False
    ) -> bool:
        """Ensure a current standalone migration for the trigger function exists in the
        host app. Sets ``self.trigger_function_dependency``; returns whether it wrote one."""
        written = self._ensure_function_migration(
            recorded=self.trigger_function_dependency,
            recorded_digest=self.trigger_function_sql,
            header=HEADER_TRIGGER_FUNCTION,
            create=sql.CREATE_UPDATED_AT_TRIGGER_FUNCTION,
            replace=sql.REPLACE_UPDATED_AT_TRIGGER_FUNCTION,
            drop=sql.DROP_UPDATED_AT_TRIGGER_FUNCTION,
            name='auto_enforcement_trigger_function',
            missing_message=(
                '\n\tRun `manage.py makeguitarmigrations` to create '
                'the trigger function migration!\n'
            ),
            stale_message=(
                '\n\tThe updated-at trigger function has changed since the migration that '
                'defines it was written.\n\tRun `manage.py makeguitarmigrations` to '
                'regenerate it.\n'
            ),
            check_only=check_only,
            adopt=adopt,
        )
        if written is None:
            return False
        self.trigger_function_dependency, self.trigger_function_sql = written
        return True

    def _ensure_parent_trigger_function_migration(
        self, *, check_only: bool = False, adopt: bool = False
    ) -> bool:
        """Ensure a current migration for the MTI parent updated-at function. Kept separate
        from the base trigger-function migration so adding MTI support never re-digests
        (and regenerates) the existing single-table one."""
        written = self._ensure_function_migration(
            recorded=self.parent_trigger_function_dependency,
            recorded_digest=self.parent_trigger_function_sql,
            header=HEADER_PARENT_TRIGGER_FUNCTION,
            create=sql.CREATE_PARENT_UPDATED_AT_TRIGGER_FUNCTION,
            replace=sql.REPLACE_PARENT_UPDATED_AT_TRIGGER_FUNCTION,
            drop=sql.DROP_PARENT_UPDATED_AT_TRIGGER_FUNCTION,
            name='auto_enforcement_parent_trigger_function',
            missing_message=(
                '\n\tRun `manage.py makeguitarmigrations` to create '
                'the MTI parent trigger function migration!\n'
            ),
            stale_message=(
                '\n\tThe MTI parent updated-at trigger function has changed since the '
                'migration that defines it was written.\n\tRun '
                '`manage.py makeguitarmigrations` to regenerate it.\n'
            ),
            check_only=check_only,
            adopt=adopt,
            dependencies=[self.trigger_function_dependency]
            if self.trigger_function_dependency
            else None,
        )
        if written is None:
            return False
        self.parent_trigger_function_dependency, self.parent_trigger_function_sql = written
        return True

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def _required_autofill_functions(self, requested: set[str]) -> dict[str, tuple[str, str]]:
        """``function name -> (dimension, column)`` every in-scope table's autofill trigger
        will call. Read from the same coverage the triggers are built from, so the function
        migrations and the triggers depending on them can never disagree about the name."""
        # Scoped by the trigger's *host* app, not the declarer: they differ once a trigger is
        # attributed to an ancestor's app, and the declarer's scope would emit a trigger calling
        # a function never written. ``.get``: an ancestor outside LOCAL_APPS hosts nothing.
        hosting = self._table_app_labels()
        return {
            function: value
            for (table, function), value in self._required_autofill_keys().items()
            if (host := hosting.get(table)) is not None
            and _generator.is_in_scope(django_apps.get_app_config(host), requested)
        }

    def _ensure_tenant_autofill_function_migration(
        self,
        function: str,
        dimension: str,
        column: str,
        *,
        check_only: bool = False,
        adopt: bool = False,
    ) -> bool:
        """Ensure a current migration for one autofill trigger function. Kept off the two
        singletons above for the reason they are kept off each other: re-digesting an
        existing function migration regenerates it, for a function that did not change."""
        # Built in sql.triggers, not here: ``audittenancy`` renders the same body to compare
        # a live function against, and a second copy of these slots would let the two disagree.
        slots = _triggers._tenant_autofill_slots(dimension, column, function)
        written = self._ensure_function_migration(
            recorded=self.tenant_autofill_dependencies.get(function),
            recorded_digest=self.tenant_autofill_sql.get(function),
            header=HEADER_TENANT_AUTOFILL_FUNCTION.format(
                function=_identifiers._escape_ident(function)
            ),
            create=_triggers._CREATE_TENANT_AUTOFILL_FUNCTION.format(**slots),
            replace=_triggers._REPLACE_TENANT_AUTOFILL_FUNCTION.format(**slots),
            drop=_triggers._DROP_TENANT_AUTOFILL_FUNCTION.format(**slots),
            name=f'auto_enforcement_{function}',
            missing_message=(
                f'\n\tRun `manage.py makeguitarmigrations` to create the tenant '
                f'autofill function migration for {column!r}!\n'
            ),
            stale_message=(
                f'\n\tThe tenant autofill function for {column!r} has changed since the '
                f'migration that defines it was written.\n\tRun '
                f'`manage.py makeguitarmigrations` to regenerate it.\n'
            ),
            check_only=check_only,
            adopt=adopt,
        )
        if written is None:
            return False
        self.tenant_autofill_dependencies[function], self.tenant_autofill_sql[function] = written
        return True

    def _refuse_a_rule_name_clash(self, *, check_only: bool) -> None:
        """Fail a ``--check`` run over a shared rule name. One of the two rules does not exist
        in the database the migration ships to, which is precisely what ``--check`` is for; a
        generating run has already written the files, so there it stays a report."""
        if check_only and self._rule_name_clashes:
            # Bare message, as _ensure_singleton_migration explains: Django re-styles an
            # uncaught CommandError, and double-wrapping garbles the ANSI codes.
            raise CommandError(self._rule_name_clashes[0])

    def _refuse_a_missing_edge(self, *, check_only: bool) -> None:
        """Fail a ``--check`` run over a rule nothing orders against the table it names. New
        in 2.5.0, and the reason this is a minor release: a graph that was green before now
        fails, which is the point -- it was green all the way to a virgin-database failure."""
        if check_only and self._missing_edges:
            raise CommandError(self._missing_edges[0])

    def _refuse_a_stale_owned_rule(self, *, check_only: bool) -> None:
        """Fail a ``--check`` run over an owned rule that is refused but already recorded.
        Unlike a name clash this one is not emitted at all, so a generating run leaves the
        database exactly as wrong as it found it -- the report is all either run can do."""
        if check_only and self._refusals_over_live_rules:
            raise CommandError(self._refusals_over_live_rules[0])

    def handle(self, *app_labels, **options):
        check_only: bool = options['check_only']
        force_rls: bool = options.get('force_rls', False)
        adopt: bool = options.get('adopt', False)
        # Positional app labels scope generation; empty => all local apps.
        requested: set[str] = set(app_labels)

        _generator.validate_app_labels(requested)

        if force_rls and adopt:
            raise CommandError(
                '--adopt and --force-rls cannot be combined. --force-rls is the second stage '
                'of a retrofit and acts only on tables whose policies this command already '
                'recorded; --adopt exists precisely because that record is missing. Run '
                '--adopt first, then --force-rls.'
            )

        if force_rls:
            return self._handle_force_rls_stage(requested, check_only=check_only)

        # Force self.existing's lazy scan now: Step 1 below reads trigger_function_dependency/
        # _sql directly off self, not through self.existing, since it mutates them after
        # writing a migration -- so they must already carry the scanned values by then.
        _ = self.existing

        # Step 1: ensure the singleton function migration(s) exist so later app migrations
        # can depend on them -- scoped to requested apps, but always hosted in
        # TRIGGER_FUNCTION_APP even if that host app wasn't named.
        in_scope_models = [
            model
            for app in django_apps.get_app_configs()
            if _generator.is_in_scope(app, requested)
            for model in app.get_models()
        ]
        needs_trigger_function = any(owns_column(m, '_updated_at') for m in in_scope_models)
        needs_parent_function = any(is_mti_child(m, '_updated_at') for m in in_scope_models)

        # Under --check, a stale/missing function migration raises from inside
        # _ensure_function_migration -- caught here so it joins the per-app report below
        # instead of a project with both problems only hearing about whichever came first.
        function_check_messages: list[str] = []
        changes_made = False
        if needs_trigger_function:
            try:
                changes_made = self._ensure_trigger_function_migration(
                    check_only=check_only, adopt=adopt
                )
            except CommandError as err:
                function_check_messages.append(str(err))
        if needs_parent_function:
            try:
                changes_made = (
                    self._ensure_parent_trigger_function_migration(
                        check_only=check_only, adopt=adopt
                    )
                    or changes_made
                )
            except CommandError as err:
                function_check_messages.append(str(err))
        # Sorted so two functions in one run write their migrations in a stable order, which
        # a digest-stamped file must have or successive runs disagree about what changed.
        for function, (dimension, column) in sorted(
            self._required_autofill_functions(requested).items()
        ):
            try:
                changes_made = (
                    self._ensure_tenant_autofill_function_migration(
                        function, dimension, column, check_only=check_only, adopt=adopt
                    )
                    or changes_made
                )
            except CommandError as err:
                function_check_messages.append(str(err))
        # Step 2: per-app trigger / soft-delete migrations, scoped to `requested`.
        # Intentionally skips cross-app CASCADE rules whose parent app isn't in
        # scope (see `_scoped_cascade_gap_notes`) -- surfaced below, not silent.
        stage_changed, check_missing = self._generate_stage(
            requested,
            migration_name='auto_enforcement',
            build_ops=lambda app: self._build_operations(app, adopt=adopt),
            check_only=check_only,
            dependencies_for=self._dependencies_for,
            adopt=adopt,
        )
        changes_made = changes_made or stage_changed

        # Step 3: surface cross-app cascade rules this scoped run intentionally
        # did not create, so the "pragmatic scope" tradeoff is never silent.
        for note in (
            self._scoped_cascade_gap_notes(requested)
            + self._scoped_owned_gap_notes(requested)
            + self._scoped_autofill_gap_notes(requested)
        ):
            self.stdout.write(self.style.WARNING(note))

        # MTI cascade rules skipped as warnings; two relations sharing a rule name as an error,
        # that one being *emitted* rather than skipped -- so one of the pair does not exist in
        # the database it ships to. One loop, so neither kind can go unwritten.
        for paint, note in [
            *((self.style.WARNING, note) for note in self._mti_cascade_warnings),
            *((self.style.ERROR, note) for note in self._rule_name_clashes),
            *((self.style.ERROR, note) for note in self._refusals_over_live_rules),
            *((self.style.ERROR, note) for note in self._missing_edges),
        ]:
            self.stderr.write(paint(note))

        # Tables tenancy could not cover, and why. Skips are design, never silent -- so the
        # relocation refusals print here too, not only via `expected_coverage`. Once per run,
        # not per app: a refusal is a fact about the owner's table, shared by every child.
        relocation_notes = owner_autofill_notes() if self._tenant_policies_enabled() else []
        for note in self._tenancy_notes + relocation_notes:
            self.stdout.write(self.style.WARNING(note))

        # Autofill coverage this command recorded but can no longer retire or attribute --
        # an orphaned function is inert, an unmapped table has no app to migrate into.
        for note in self._unmapped_autofill_notes() + self._orphaned_autofill_function_notes():
            self.stdout.write(self.style.WARNING(note))

        # After every note above, for the reason `function_check_messages` is collected rather
        # than raised in place: a run with two problems must report both, and this one raises.
        # Before `_report_missing`, which also raises -- a clash is the more fundamental.
        self._refuse_a_rule_name_clash(check_only=check_only)
        self._refuse_a_stale_owned_rule(check_only=check_only)
        self._refuse_a_missing_edge(check_only=check_only)

        if check_missing or function_check_messages:
            self._report_missing(check_missing, function_check_messages)

        if not changes_made and not check_only:
            self.stdout.write('No changes detected')

    def _report_missing(
        self,
        check_missing: list[tuple[str, list[str]]],
        function_check_messages: list[str] | None = None,
    ) -> None:
        """Print what ``--check`` found and exit non-zero. "or outdated" is not padding --
        an operation may be a *replacement* for a policy whose shape no longer matches.
        *function_check_messages* prints first: it's the more fundamental prerequisite."""
        for message in function_check_messages or []:
            self.stderr.write(self.style.ERROR(message))
        for app_label, operations in check_missing:
            self.stderr.write(
                self.style.ERROR(f"Missing or outdated enforcement migrations for '{app_label}':")
            )
            for operation in operations:
                self.stderr.write(textwrap.indent(operation, '    '))
        raise CommandError('Run `manage.py makeguitarmigrations` to create missing migrations.')

    def _handle_force_rls_stage(self, requested: set[str], *, check_only: bool) -> None:
        """Emit only ``FORCE ROW LEVEL SECURITY`` migrations, for tables already policied --
        a separate stage for exactly one case: a retrofit that shipped policies inert
        (``GUITARS_RLS_FORCE = False``) to soak before making them binding."""
        if self._rls_force_enabled():
            # Not an error: this is a *finished* retrofit. Policies shipped inert, the
            # setting was flipped to True, and those tables still need their FORCE --
            # new policies get it inline, so the run below finds only the backlog.
            self.stdout.write(
                self.style.WARNING(
                    'GUITARS_RLS_FORCE is True, so new tenant policies already emit FORCE. '
                    '--force-rls only covers tables whose policies shipped before it was '
                    'turned on; expect no changes if there are none.'
                )
            )
        if not self._tenant_policies_enabled():
            self.stdout.write(
                self.style.WARNING(
                    'GUITARS_TENANT_POLICIES is False, so no tenant policies exist to force.'
                )
            )
            return

        changes_made, check_missing = self._generate_stage(
            requested,
            migration_name='auto_tenant_force',
            build_ops=self._tenant_force_operations,
            check_only=check_only,
        )

        for note in self._tenancy_notes:
            self.stdout.write(self.style.WARNING(note))

        if check_missing:
            self._report_missing(check_missing)

        if not changes_made and not check_only:
            self.stdout.write('No changes detected')
