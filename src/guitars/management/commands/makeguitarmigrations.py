from __future__ import annotations

import re
import textwrap
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from django.apps import apps as django_apps
from django.conf import settings
from django.core.management import CommandError
from django.core.management.base import BaseCommand
from django.db import models

from guitars.introspection import column_owner, has_column, is_mti_child, owns_column
from guitars.management import _generator


if TYPE_CHECKING:
    from django.apps import AppConfig


TRIGGER_FUNCTION_OPERATION = """\
# Define function for updated at triggers!
migrations.RunSQL(
    sql=sql.CREATE_UPDATED_AT_TRIGGER_FUNCTION,
    reverse_sql=sql.DROP_UPDATED_AT_TRIGGER_FUNCTION,
),
"""

UPDATED_AT_OPERATION = """\
# Updated at Trigger on "{table}" table!
migrations.RunSQL(
    sql=sql.CREATE_UPDATED_AT_TRIGGER.format(table='{table}', primary_key='{primary_key}'),
    reverse_sql=sql.DROP_UPDATED_AT_TRIGGER.format(table='{table}'),
),
"""

SOFT_DELETE_OPERATION = """\
# Soft Delete Rule on "{table}" table!
migrations.RunSQL(
    sql=sql.CREATE_SOFT_DELETE_RULE.format(table='{table}', primary_key='{primary_key}'),
    reverse_sql=sql.DROP_SOFT_DELETE_RULE.format(table='{table}'),
),
"""

SOFT_DELETE_RELATED_OPERATION = """\
# Soft Delete Related Rule on "{related_table}" that is related to "{table}"!
migrations.RunSQL(
    sql=sql.CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE.format(
        table='{table}',
        related_table='{related_table}',
        primary_key='{primary_key}',
        foreign_key='{foreign_key}',
    ),
    reverse_sql=sql.DROP_SOFT_DELETE_RELATED_OBJECTS_RULE.format(
        table='{table}', related_table='{related_table}'
    ),
),
"""

# --- Multi-table inheritance (MTI) operations ---

PARENT_TRIGGER_FUNCTION_OPERATION = """\
# Define function for MTI parent updated at triggers!
migrations.RunSQL(
    sql=sql.CREATE_PARENT_UPDATED_AT_TRIGGER_FUNCTION,
    reverse_sql=sql.DROP_PARENT_UPDATED_AT_TRIGGER_FUNCTION,
),
"""

MTI_UPDATED_AT_OPERATION = """\
# MTI Updated at Trigger on "{child_table}" table (parent "{parent_table}")!
migrations.RunSQL(
    sql=sql.CREATE_PARENT_UPDATED_AT_TRIGGER.format(
        child_table='{child_table}',
        parent_table='{parent_table}',
        parent_pk='{parent_pk}',
        child_pk='{child_pk}',
    ),
    reverse_sql=sql.DROP_PARENT_UPDATED_AT_TRIGGER.format(child_table='{child_table}'),
),
"""

MTI_SOFT_DELETE_OPERATION = """\
# MTI Soft Delete Rule on "{child_table}" table (parent "{parent_table}")!
migrations.RunSQL(
    sql=sql.CREATE_MTI_SOFT_DELETE_RULE.format(
        child_table='{child_table}',
        parent_table='{parent_table}',
        parent_pk='{parent_pk}',
        child_pk='{child_pk}',
    ),
    reverse_sql=sql.DROP_MTI_SOFT_DELETE_RULE.format(child_table='{child_table}'),
),
"""

# Regex patterns for scanning existing custom operations in migration files.
_RE_TRIGGER_FUNCTION = re.compile(r'CREATE_UPDATED_AT_TRIGGER_FUNCTION')
_RE_PARENT_TRIGGER_FUNCTION = re.compile(r'CREATE_PARENT_UPDATED_AT_TRIGGER_FUNCTION')
_RE_UPDATED_AT = re.compile(r'# Updated at Trigger on "([^"]+)" table!')
_RE_SOFT_DELETE = re.compile(r'# Soft Delete Rule on "([^"]+)" table!')
_RE_SOFT_DELETE_RELATED = re.compile(
    r'# Soft Delete Related Rule on "([^"]+)" that is related to "([^"]+)"'
)
# MTI headers carry a leading "MTI " token, so they never collide with the single-table
# patterns above (which anchor on ``# Updated`` / ``# Soft`` immediately after the comment mark).
_RE_MTI_UPDATED_AT = re.compile(r'# MTI Updated at Trigger on "([^"]+)" table')
_RE_MTI_SOFT_DELETE = re.compile(r'# MTI Soft Delete Rule on "([^"]+)" table')
# The [DIGEST:...] marker is matched by _generator.RE_DIGEST, shared with
# maketenantmigrations -- one pattern, so the two can't drift on what a stamp looks like.


class Command(BaseCommand):
    """Generates migrations for PostgreSQL triggers and rules.

    Scans all local app models for ``_updated_at`` (creates statement-level
    update trigger) and ``_deleted_at`` (creates soft-delete rule + cascade
    rules for related soft-deletable models with ``on_delete=CASCADE``).

    Multi-table-inheritance children are handled too: because their metadata
    columns physically live on an ancestor table, each column's owning table is
    resolved via ``_column_owner`` (not ``hasattr``), and MTI children get a
    parent-propagation updated-at trigger and a redirect soft-delete rule instead
    of own-table objects. See the "Multi-table inheritance" section in CLAUDE.md.

    Run after ``makemigrations`` whenever models inheriting ``DatedModel``
    or ``SoftDeletableModel`` are added or changed.
    """

    help = 'Creates Custom Migration Files (Triggers, Rules, etc.)'

    def __init__(self, *args, **kwargs):  # pragma: no cover
        super().__init__(*args, **kwargs)

        self.all_models: list[type[models.Model]] = []
        self.reverse_relations_mapping: defaultdict[type[models.Model], set] = defaultdict(set)
        self._setup_models_and_reverse_relations()

        # (app_label, migration_stem) tuples or None, pointing at the singleton function migrations.
        self.trigger_function_dependency: tuple[str, str] | None = None
        self.parent_trigger_function_dependency: tuple[str, str] | None = None

        # Cross-app / MTI cascade rules skipped this run, surfaced as warnings (not silent).
        self._mti_cascade_warnings: list[str] = []

        # Scan existing migration files to discover already-defined custom operations.
        (
            self.existing_triggers,
            self.existing_soft_deletes,
            self.existing_soft_delete_related,
            self.existing_mti_triggers,
            self.existing_mti_soft_deletes,
            self.trigger_function_dependency,
            self.parent_trigger_function_dependency,
        ) = self._scan_existing_custom_operations()

    def add_arguments(self, parser):  # pragma: no cover
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
    # Migration-file scanning
    # ------------------------------------------------------------------

    def _scan_existing_custom_operations(self):
        """
        Scan all local apps' migration files to discover already-defined custom operations.

        Returns a 7-tuple of:
            - existing_triggers:          set of table names with updated_at triggers
            - existing_soft_deletes:      set of table names with soft-delete rules
            - existing_soft_delete_related: set of ``(related_table, table)`` tuples
            - existing_mti_triggers:      set of child table names with MTI parent updated-at triggers
            - existing_mti_soft_deletes:  set of child table names with MTI soft-delete rules
            - trigger_function_dep:       ``(app_label, migration_stem)`` or ``None``
            - parent_trigger_function_dep: ``(app_label, migration_stem)`` or ``None``
        """
        existing_triggers: set[str] = set()
        existing_soft_deletes: set[str] = set()
        existing_soft_delete_related: set[tuple[str, str]] = set()
        existing_mti_triggers: set[str] = set()
        existing_mti_soft_deletes: set[str] = set()
        trigger_function_dep: tuple[str, str] | None = None
        parent_trigger_function_dep: tuple[str, str] | None = None

        for app in django_apps.get_app_configs():
            if app.name not in settings.LOCAL_APPS:
                continue
            for path, content in _generator.iter_migration_files(app):
                if _RE_TRIGGER_FUNCTION.search(content):
                    trigger_function_dep = (app.label, path.stem)
                if _RE_PARENT_TRIGGER_FUNCTION.search(content):
                    parent_trigger_function_dep = (app.label, path.stem)

                existing_triggers.update(m.group(1) for m in _RE_UPDATED_AT.finditer(content))
                existing_soft_deletes.update(m.group(1) for m in _RE_SOFT_DELETE.finditer(content))
                existing_soft_delete_related.update(
                    (m.group(1), m.group(2)) for m in _RE_SOFT_DELETE_RELATED.finditer(content)
                )
                existing_mti_triggers.update(
                    m.group(1) for m in _RE_MTI_UPDATED_AT.finditer(content)
                )
                existing_mti_soft_deletes.update(
                    m.group(1) for m in _RE_MTI_SOFT_DELETE.finditer(content)
                )

        return (
            existing_triggers,
            existing_soft_deletes,
            existing_soft_delete_related,
            existing_mti_triggers,
            existing_mti_soft_deletes,
            trigger_function_dep,
            parent_trigger_function_dep,
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

        Thin wrapper naming this command's marker text and import line; the mechanics
        are shared with ``maketenantmigrations`` (see ``_generator``).
        """
        _generator.write_migration_file(
            app=app,
            migration_file=migration_file,
            operations=operations,
            operations_digest=operations_digest,
            generated_by='makeguitarmigrations',
            import_line='from guitars import sql',
            dependencies=dependencies,
        )

    # ------------------------------------------------------------------
    # Trigger function migration
    # ------------------------------------------------------------------

    def _ensure_trigger_function_migration(
        self, *, check_only: bool = False
    ) -> bool:  # pragma: no cover
        """
        Ensure a standalone migration for the trigger function exists in the host app.
        Sets ``self.trigger_function_dependency`` when done.
        Returns True if a new migration was created, False if it already existed.
        """
        if self.trigger_function_dependency:
            return False

        if check_only:
            raise CommandError(
                self.style.ERROR(
                    '\n\tRun `manage.py makeguitarmigrations` to create '
                    'the trigger function migration!\n'
                )
            )

        host_app = self._get_trigger_function_host_app()
        operations_digest = _generator.digest_of([TRIGGER_FUNCTION_OPERATION])
        migration_file = _generator.create_empty_migration_file(
            host_app, name='auto_advanced_trigger_function'
        )
        self._write_migration_file(
            app=host_app,
            migration_file=migration_file,
            operations=[TRIGGER_FUNCTION_OPERATION],
            operations_digest=operations_digest,
        )

        migration_stem = Path(migration_file).stem
        self.trigger_function_dependency = (host_app.label, migration_stem)

        self.stdout.write(
            self.style.MIGRATE_HEADING(f"Advanced migrations for '{host_app.label}':")
        )
        self.stdout.write(f'  migrations/{migration_file}')
        return True

    def _ensure_parent_trigger_function_migration(
        self, *, check_only: bool = False
    ) -> bool:  # pragma: no cover
        """
        Ensure a standalone migration for the MTI parent updated-at function exists.
        Sets ``self.parent_trigger_function_dependency`` when done. Kept separate from the
        base trigger-function migration so that adding MTI support never re-digests (and thus
        regenerates) the existing single-table function migration.
        Returns True if a new migration was created, False if it already existed.
        """
        if self.parent_trigger_function_dependency:
            return False

        if check_only:
            raise CommandError(
                self.style.ERROR(
                    '\n\tRun `manage.py makeguitarmigrations` to create '
                    'the MTI parent trigger function migration!\n'
                )
            )

        host_app = self._get_trigger_function_host_app()
        operations_digest = _generator.digest_of([PARENT_TRIGGER_FUNCTION_OPERATION])
        migration_file = _generator.create_empty_migration_file(
            host_app, name='auto_advanced_parent_trigger_function'
        )
        self._write_migration_file(
            app=host_app,
            migration_file=migration_file,
            operations=[PARENT_TRIGGER_FUNCTION_OPERATION],
            operations_digest=operations_digest,
            dependencies=[self.trigger_function_dependency]
            if self.trigger_function_dependency
            else None,
        )

        migration_stem = Path(migration_file).stem
        self.parent_trigger_function_dependency = (host_app.label, migration_stem)

        self.stdout.write(
            self.style.MIGRATE_HEADING(f"Advanced migrations for '{host_app.label}':")
        )
        self.stdout.write(f'  migrations/{migration_file}')
        return True

    # ------------------------------------------------------------------
    # Per-app operations
    # ------------------------------------------------------------------

    def _build_operations(self, app: AppConfig) -> list[str]:
        """Return a list of SQL operation snippets needed for *app*'s models."""
        operations: list[str] = []
        deferred: list[str] = []

        for model in app.get_models():
            table = model._meta.db_table
            primary_key = model._meta.pk.name

            # --- updated_at trigger: own table vs. MTI parent-propagation ---
            if owns_column(model, '_updated_at'):
                if table not in self.existing_triggers:
                    operations.append(
                        UPDATED_AT_OPERATION.format(table=table, primary_key=primary_key)
                    )
            elif is_mti_child(model, '_updated_at') and table not in self.existing_mti_triggers:
                owner = column_owner(model, '_updated_at')
                operations.append(
                    MTI_UPDATED_AT_OPERATION.format(
                        child_table=table,
                        child_pk=model._meta.pk.column,
                        parent_table=owner._meta.db_table,
                        parent_pk=owner._meta.pk.column,
                    )
                )

            # --- soft-delete rule: own table vs. MTI redirect-to-owner ---
            if owns_column(model, '_deleted_at'):
                if table not in self.existing_soft_deletes:
                    operations.append(
                        SOFT_DELETE_OPERATION.format(table=table, primary_key=primary_key)
                    )
            elif (
                is_mti_child(model, '_deleted_at') and table not in self.existing_mti_soft_deletes
            ):
                owner = column_owner(model, '_deleted_at')
                operations.append(
                    MTI_SOFT_DELETE_OPERATION.format(
                        child_table=table,
                        child_pk=model._meta.pk.column,
                        parent_table=owner._meta.db_table,
                        parent_pk=owner._meta.pk.column,
                    )
                )

            # --- cascade rules for CASCADE FKs pointing at this model (deferred so they
            #     always follow the owner's own soft-delete rule) ---
            if has_column(model, '_deleted_at'):
                deferred.extend(self._cascade_operations(model))

        return operations + deferred

    def _cascade_operations(self, model: type[models.Model]) -> list[str]:
        """Cascade soft-delete rules for ``on_delete=CASCADE`` FKs pointing at *model*.

        The rule is an ``ON UPDATE`` rule that must live on the table whose ``_deleted_at``
        column actually flips: *model*'s own table for the single-table case, or the owning
        ancestor when *model* is an MTI child (an ``ON UPDATE TO child_table`` rule would never
        fire, since the child table's ``_deleted_at`` is never written). The related child's FK
        column holds the shared MTI pk value, so matching it against the owner pk still works.
        """
        owner = column_owner(model, '_deleted_at')
        owner_table = owner._meta.db_table
        owner_pk = owner._meta.pk.column

        ops: list[str] = []
        for related_model, fk_field, on_delete in sorted(
            self.reverse_relations_mapping[model],
            key=lambda t: (t[0]._meta.db_table, t[1].column),
        ):
            if on_delete != models.CASCADE or not has_column(related_model, '_deleted_at'):
                continue
            # The MTI parent-link (a CASCADE OneToOne) is structural, not a user cascade FK --
            # the MTI redirect rule already ties the child's deletion to the owner, so no
            # soft-delete-related rule is needed (or valid) for it.
            if getattr(fk_field.remote_field, 'parent_link', False):
                continue
            related_table = related_model._meta.db_table
            # The flat cascade rule does ``UPDATE related_table SET _deleted_at`` -- only valid
            # when the related child owns ``_deleted_at`` on the very table its FK lives on.
            # Cascading INTO an MTI child (its column on a farther ancestor) needs a join form
            # we don't emit yet; surface it instead of writing a rule that references a missing
            # column.
            if (
                not owns_column(related_model, '_deleted_at')
                or fk_field.model is not related_model
            ):
                self._mti_cascade_warnings.append(
                    f"Cascade rule for '{related_table}' -> '{owner_table}' skipped: "
                    f"'{related_model.__name__}' inherits _deleted_at via multi-table "
                    'inheritance; cascading into an MTI child is not supported yet.'
                )
                continue
            if (related_table, owner_table) in self.existing_soft_delete_related:
                continue
            ops.append(
                SOFT_DELETE_RELATED_OPERATION.format(
                    table=owner_table,
                    related_table=related_table,
                    primary_key=owner_pk,
                    foreign_key=fk_field.column,
                )
            )
        return ops

    def _scoped_cascade_gap_notes(self, requested: set[str]) -> list[str]:
        """Describe cross-app CASCADE soft-delete rules this scoped run will not create.

        A rule like "deleting Band cascades to Album" is generated while
        ``_build_operations`` processes Band's app (the *parent* holding
        ``_deleted_at``), not Album's — so if the parent's app is scoped out,
        the rule is skipped even when the child's app is in scope. This is the
        intended "pragmatic scope" tradeoff (mirrors Django, which also only
        touches the apps you name), not a bug; it's closed by a later run that
        includes the parent's app label (or no labels at all).

        Only reported when the *child* (related) model's app is itself part of
        this scoped run — otherwise the gap is about two apps neither of which
        the caller asked to generate migrations for, which is just noise.
        """
        if not requested:
            return []

        model_app_label = {
            model: app.label
            for app in django_apps.get_app_configs()
            if app.name in settings.LOCAL_APPS
            for model in app.get_models()
        }

        notes: list[str] = []
        for app in django_apps.get_app_configs():
            if app.name not in settings.LOCAL_APPS or app.label in requested:
                continue
            for model in app.get_models():
                if not has_column(model, '_deleted_at'):
                    continue
                # The rule lives on the table that owns _deleted_at (the model itself, or its
                # MTI ancestor), matching where `_cascade_operations` places it.
                table = column_owner(model, '_deleted_at')._meta.db_table
                for related_model, _fk_field, on_delete in self.reverse_relations_mapping[model]:
                    if on_delete != models.CASCADE or not has_column(related_model, '_deleted_at'):
                        continue
                    if getattr(_fk_field.remote_field, 'parent_link', False):
                        continue
                    if model_app_label.get(related_model) not in requested:
                        continue
                    related_table = related_model._meta.db_table
                    if (related_table, table) in self.existing_soft_delete_related:
                        continue
                    notes.append(
                        f"Cascade rule on '{related_table}' related to '{table}' skipped: "
                        f"parent app '{app.label}' is not in this scoped run."
                    )
        return notes

    def _function_dependencies_for(self, operations_blob: str) -> list[tuple[str, str]]:
        """Function-migration dependencies an app's operations actually require.

        Only ``updated_at`` triggers call a shared trigger function: own-table triggers use
        ``set_updated_at`` (the base function migration), MTI parent-propagation triggers use
        ``set_parent_updated_at`` (the parent function migration). Soft-delete and cascade rules
        call no function, so an app emitting only those needs neither dependency. Keying off the
        operation headers (rather than appending both deps unconditionally) keeps an app's
        migration from being coupled to a function migration -- and its host app's ordering --
        it never uses.
        """
        deps: list[tuple[str, str]] = []
        if self.trigger_function_dependency and _RE_UPDATED_AT.search(operations_blob):
            deps.append(self.trigger_function_dependency)
        if self.parent_trigger_function_dependency and _RE_MTI_UPDATED_AT.search(operations_blob):
            deps.append(self.parent_trigger_function_dependency)
        return deps

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def handle(self, *app_labels, **options):
        check_only: bool = options['check_only']
        # Positional app labels scope generation; empty => all local apps.
        requested: set[str] = set(app_labels)

        _generator.validate_app_labels(requested)

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

        changes_made = needs_trigger_function and self._ensure_trigger_function_migration(
            check_only=check_only
        )
        if needs_parent_function:
            changes_made = (
                self._ensure_parent_trigger_function_migration(check_only=check_only)
                or changes_made
            )
        check_missing: list[tuple[str, list[str]]] = []

        # Step 2: per-app trigger / soft-delete migrations, scoped to `requested`.
        # Intentionally skips cross-app CASCADE rules whose parent app isn't in
        # scope (see `_scoped_cascade_gap_notes`) -- surfaced below, not silent.
        for app in django_apps.get_app_configs():
            if not _generator.is_in_scope(app, requested):
                continue

            operations = self._build_operations(app)
            if not operations:
                continue

            operations_blob = '\n'.join(operations)
            operations_digest = _generator.digest_of(operations)
            if _generator.migration_with_digest_exists(app, operations_digest):
                continue

            if check_only:
                check_missing.append((app.label, operations))
                continue

            migration_file = _generator.create_empty_migration_file(app, 'auto_advanced')
            self._write_migration_file(
                app=app,
                migration_file=migration_file,
                operations=operations,
                operations_digest=operations_digest,
                dependencies=self._function_dependencies_for(operations_blob),
            )

            self.stdout.write(
                self.style.MIGRATE_HEADING(f"Advanced migrations for '{app.label}':")
            )
            self.stdout.write(f'  migrations/{migration_file}')
            changes_made = True

        # Step 3: surface cross-app cascade rules this scoped run intentionally
        # did not create, so the "pragmatic scope" tradeoff is never silent.
        for note in self._scoped_cascade_gap_notes(requested):
            self.stdout.write(self.style.WARNING(note))

        # Surface MTI cascade rules skipped because cascading INTO an MTI child is unsupported.
        for note in self._mti_cascade_warnings:
            self.stderr.write(self.style.WARNING(note))

        if check_missing:
            for app_label, operations in check_missing:
                self.stderr.write(
                    self.style.ERROR(f"Missing advanced migrations for '{app_label}':")
                )
                for op in operations:
                    self.stderr.write(textwrap.indent(op, '    '))
            raise CommandError(
                'Run `manage.py makeguitarmigrations` to create missing migrations.'
            )

        if not changes_made and not check_only:
            self.stdout.write('No changes detected')
