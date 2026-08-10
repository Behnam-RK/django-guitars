"""The ``makeguitarmigrations`` command: wires headers, identity, scanning and operations
together into the CLI entry point.

Vocabulary, used consistently here and in the docs:

* An **enforcement migration** is a generated migration of ``RunSQL`` operations. Django's
  own migrations describe *schema* -- which tables and columns exist. These describe what
  the database *guarantees about the rows*, for every code path including the ones that
  never call ``save()``.
* An **enforcement operation** is one ``RunSQL`` entry inside such a migration.

There are four kinds, and each already has a precise name of its own:

============================  ===============================================
timestamp trigger             keeps ``_updated_at`` current on any ``UPDATE``
soft-delete rule              rewrites ``DELETE`` into a ``_deleted_at`` stamp
MTI redirect rule / trigger   applies both to a multi-table-inheritance child
tenant policy                 row-level security scoping rows to a tenant
============================  ===============================================

All four ship from one command because they share every mechanic that is actually
difficult: model discovery, MTI column-ownership resolution, dedupe against operations
already written, ``--empty`` scaffolding, digest stamping and app scoping.

Idempotency has three layers (digest, per-operation header, SQL identity); see
``docs/migrations.md``'s "Idempotency has three layers" section for the full account.
The header strings are **frozen**: reword one and every existing migration stops being
recognised, and the next run emits duplicates.

This package is organized by concern rather than as one flat module:

* ``headers.py`` -- the frozen ``HEADER_*`` templates and the ``_RE_*`` scanners derived
  from (most of) them.
* ``identity.py`` -- rendering a ``RunSQL`` operation and reading the ``[SQL:...]``/
  ``[POLICY:...]`` identity tokens back off an already-written header line.
* ``scanning.py`` -- ``ExistingOperations`` and the one-pass scan of every local app's
  migration files that builds it.
* ``operations.py`` -- ``OperationsMixin``, building the operations an app's models need.
* ``command.py`` (this module) -- the thin ``Command`` wiring the above into a CLI.
"""

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
from guitars.management.enforcement.headers import (
    HEADER_PARENT_TRIGGER_FUNCTION,
    HEADER_TRIGGER_FUNCTION,
)
from guitars.management.enforcement.identity import _operation
from guitars.management.enforcement.operations import OperationsMixin
from guitars.management.enforcement.scanning import ExistingOperations, scan_existing_operations


if TYPE_CHECKING:
    from django.apps import AppConfig


class Command(OperationsMixin, BaseCommand):
    """Generates the enforcement migrations: triggers, rules and tenant policies.

    Each kind is driven by the shape of the model, so declaring the thing *is* the opt-in
    and there is no registry to keep in step:

    * ``_updated_at`` -> a statement-level timestamp trigger.
    * ``_deleted_at`` -> a soft-delete rule, plus cascade rules for related
      soft-deletable models whose FK is ``on_delete=CASCADE``.
    * a ``tenanted_manager()`` -> a row-level-security tenant policy.

    Multi-table inheritance is handled throughout: because the relevant columns physically
    live on an ancestor's table, each column's owner is resolved via
    ``guitars.introspection`` rather than ``hasattr``, and an MTI child gets the
    parent-propagating trigger, the redirect rule, and the owner-join policy instead of the
    own-table forms. See ``docs/mti.md``.

    Run after ``makemigrations`` when models change -- or let ``makemigrations`` run it for
    you, which is the default.
    """

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
        # property -- and mutable alongside it for the same reason: a singleton function
        # migration is only "already done" when it both exists and defines the SQL the kit
        # emits today.
        self.trigger_function_dependency: tuple[str, str] | None = None
        self.parent_trigger_function_dependency: tuple[str, str] | None = None
        self.trigger_function_sql: str | None = None
        self.parent_trigger_function_sql: str | None = None

        # Cross-app / MTI cascade rules skipped this run, surfaced as warnings (not silent).
        self._mti_cascade_warnings: list[str] = []
        # Tables tenancy discovery could not cover, with the reason. Also surfaced.
        self._tenancy_notes: list[str] = []

        self._existing: ExistingOperations | None = None

    @property
    def existing(self) -> ExistingOperations:
        """Every enforcement operation already on disk, scanned once and cached.

        Not scanned eagerly in ``__init__``: Django constructs a ``Command()`` for
        ``--help`` and the command registry, and neither needs a filesystem scan of every
        local app's migrations. The first real access -- by ``handle()``, or directly by a
        test exercising internals below the CLI entry point -- triggers the scan and copies
        the singleton-function state onto ``self`` exactly once.
        """
        if self._existing is None:
            self._existing = scan_existing_operations()
            self.trigger_function_dependency = self._existing.trigger_function_dependency
            self.parent_trigger_function_dependency = (
                self._existing.parent_trigger_function_dependency
            )
            self.trigger_function_sql = self._existing.trigger_function_sql
            self.parent_trigger_function_sql = self._existing.parent_trigger_function_sql
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
        """Whether to emit tenant policies at all.

        ``False`` keeps the Python enforcement layer while leaving the database alone --
        for adopting the loud layer first, or for a database where the application role
        cannot own its tables and so could never be constrained by RLS anyway.
        """
        return bool(getattr(settings, 'GUITARS_TENANT_POLICIES', True))

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
        """Rewrite a migration file to include the given custom *operations*.

        Thin wrapper naming this command's marker text; the mechanics live in
        ``_generator``, shared with every command that generates migrations.
        """
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

        Returns ``((app_label, stem), digest)`` for a migration it wrote, or ``None`` if the
        recorded one is already current.

        These two functions are singletons by *existence*, and that is precisely why a change
        to either body used to ship nothing: the first thing this did was return early on
        "a migration mentioning the function exists somewhere". It now also compares the
        recorded ``[SQL:...]`` digest, so an edited body produces a second migration carrying
        the ``OR REPLACE`` form. ``OR REPLACE`` rather than DROP + CREATE is forced, not
        defensive -- ``DROP FUNCTION`` refuses while any trigger depends on it, and CASCADE
        would take every table's trigger with it.

        Under ``--adopt`` the ``OR REPLACE`` form is used even when nothing is recorded at
        all: the whole premise of the flag is a database the generator has no record of, and
        a plain ``CREATE FUNCTION`` there fails migrate with "function already exists" on
        exactly the database ``--adopt`` exists to bring in. There is no separate adopt form
        for a function the way there is for a trigger, because ``OR REPLACE`` is already the
        one form that is correct whether or not the function exists.
        """
        current_source, current_digest = _operation(header, create, drop)
        if recorded is not None and (recorded_digest == current_digest and not adopt):
            return None

        stale = recorded is not None
        if check_only:
            # Bare message, not self.style.ERROR(...): Django's own top-level handler
            # applies style.ERROR() again when it prints an uncaught CommandError, and
            # double-wrapping garbled the ANSI codes. Compare _report_missing's raise below,
            # which never wrapped its message and was always correct.
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
        """
        Ensure a current standalone migration for the trigger function exists in the host app.
        Sets ``self.trigger_function_dependency`` when done.
        Returns True if a new migration was created, False if the recorded one is current.
        """
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
        """
        Ensure a current standalone migration for the MTI parent updated-at function exists.
        Sets ``self.parent_trigger_function_dependency`` when done. Kept separate from the
        base trigger-function migration so that adding MTI support never re-digests (and thus
        regenerates) the existing single-table function migration.
        Returns True if a new migration was created, False if the recorded one is current.
        """
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

        # Step 1: Ensure the singleton function migration(s) exist, so all subsequent app
        # migrations can safely depend on them. Scoped to the requested apps, but still hosted
        # in TRIGGER_FUNCTION_APP (a hard prerequisite) even if that host app wasn't named.
        # The base ``set_updated_at`` function is needed by own-table triggers; the MTI
        # ``set_parent_updated_at`` function only by MTI children that inherit ``_updated_at``.
        in_scope_models = [
            model
            for app in django_apps.get_app_configs()
            if _generator.is_in_scope(app, requested)
            for model in app.get_models()
        ]
        needs_trigger_function = any(owns_column(m, '_updated_at') for m in in_scope_models)
        needs_parent_function = any(is_mti_child(m, '_updated_at') for m in in_scope_models)

        # Under --check, a stale/missing function migration raises immediately from inside
        # _ensure_function_migration -- caught here rather than left to propagate, so it can
        # join the per-app report below instead of hiding it: function-migration staleness
        # used to fail fast while app-operation staleness aggregated into one report, so a
        # project with both problems only ever heard about whichever this method reached
        # first.
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
        # Step 2: per-app trigger / soft-delete migrations, scoped to `requested`.
        # Intentionally skips cross-app CASCADE rules whose parent app isn't in
        # scope (see `_scoped_cascade_gap_notes`) -- surfaced below, not silent.
        stage_changed, check_missing = self._generate_stage(
            requested,
            migration_name='auto_enforcement',
            build_ops=lambda app: self._build_operations(app, adopt=adopt),
            check_only=check_only,
            dependencies_for=self._function_dependencies_for,
        )
        changes_made = changes_made or stage_changed

        # Step 3: surface cross-app cascade rules this scoped run intentionally
        # did not create, so the "pragmatic scope" tradeoff is never silent.
        for note in self._scoped_cascade_gap_notes(requested):
            self.stdout.write(self.style.WARNING(note))

        # Surface MTI cascade rules skipped because cascading INTO an MTI child is unsupported.
        for note in self._mti_cascade_warnings:
            self.stderr.write(self.style.WARNING(note))

        # Tables tenancy could not cover, and why. Skips are design, never silent.
        for note in self._tenancy_notes:
            self.stdout.write(self.style.WARNING(note))

        if check_missing or function_check_messages:
            self._report_missing(check_missing, function_check_messages)

        if not changes_made and not check_only:
            self.stdout.write('No changes detected')

    def _report_missing(
        self,
        check_missing: list[tuple[str, list[str]]],
        function_check_messages: list[str] | None = None,
    ) -> None:
        """Print what ``--check`` found and exit non-zero.

        "or outdated" is not padding: an operation here may be a *replacement* for a policy
        whose shape no longer matches the models, which is a migration the app needs despite
        already having one for that table.

        *function_check_messages* prints first: a missing/stale singleton function migration
        is a hard prerequisite every per-app migration depends on, so it reads as the more
        fundamental problem even though both are reported together.
        """
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
        """Emit only ``FORCE ROW LEVEL SECURITY`` migrations, for tables already policied.

        A separate stage rather than part of the main run, because it exists for exactly one
        situation: a retrofit onto a populated database that set ``GUITARS_RLS_FORCE = False``
        so policies could be shipped inert, soaked, and only then made binding. Mixing it
        into the normal run would defeat the staging it exists to provide.
        """
        if self._rls_force_enabled():
            # Not an error, and not redundant either: this is the shape of a *finished*
            # retrofit. Policies shipped inert under GUITARS_RLS_FORCE = False, the setting
            # was then flipped to True, and those already-policied tables still need their
            # FORCE. New policies get it inline, so the run below finds only the backlog --
            # commonly nothing, which is why it says so rather than failing.
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
