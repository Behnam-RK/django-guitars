from __future__ import annotations

import hashlib
import re
import textwrap
from collections import defaultdict
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

from django.apps import apps as django_apps
from django.conf import settings
from django.core.management import CommandError, call_command
from django.core.management.base import BaseCommand
from django.db import models


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

# Regex patterns for scanning existing custom operations in migration files.
_RE_TRIGGER_FUNCTION = re.compile(r'CREATE_UPDATED_AT_TRIGGER_FUNCTION')
_RE_UPDATED_AT = re.compile(r'# Updated at Trigger on "([^"]+)" table!')
_RE_SOFT_DELETE = re.compile(r'# Soft Delete Rule on "([^"]+)" table!')
_RE_SOFT_DELETE_RELATED = re.compile(
    r'# Soft Delete Related Rule on "([^"]+)" that is related to "([^"]+)"'
)
_RE_DIGEST = re.compile(r'\[DIGEST:(?P<digest>\w+)\]')


class Command(BaseCommand):
    """Generates migrations for PostgreSQL triggers and rules.

    Scans all local app models for ``_updated_at`` (creates statement-level
    update trigger) and ``_deleted_at`` (creates soft-delete rule + cascade
    rules for related soft-deletable models with ``on_delete=CASCADE``).

    Run after ``makemigrations`` whenever models inheriting ``DatedModel``
    or ``SoftDeletableModel`` are added or changed.
    """

    help = 'Creates Custom Migration Files (Triggers, Rules, etc.)'

    def __init__(self, *args, **kwargs):  # pragma: no cover
        super().__init__(*args, **kwargs)

        self.all_models: list[type[models.Model]] = []
        self.reverse_relations_mapping: defaultdict[type[models.Model], set] = defaultdict(set)
        self._setup_models_and_reverse_relations()

        # trigger_function_dependency is a (app_label, migration_stem) tuple or None.
        self.trigger_function_dependency: tuple[str, str] | None = None

        # Scan existing migration files to discover already-defined custom operations.
        (
            self.existing_triggers,
            self.existing_soft_deletes,
            self.existing_soft_delete_related,
            self.trigger_function_dependency,
        ) = self._scan_existing_custom_operations()

    def add_arguments(self, parser):  # pragma: no cover
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
            for field in model._meta.get_fields():  # ty: ignore[unresolved-attribute]
                if isinstance(field, models.ForeignKey):
                    self.reverse_relations_mapping[field.related_model].add(
                        (model, field, field.remote_field.on_delete)
                    )

    # ------------------------------------------------------------------
    # Migration-file scanning
    # ------------------------------------------------------------------

    @staticmethod
    def _iter_migration_files(app: AppConfig):
        """Yield ``(path, content)`` for every migration file in *app*."""
        migrations_dir = Path(app.path) / 'migrations'
        if not migrations_dir.is_dir():
            return
        for path in migrations_dir.glob('*.py'):
            yield path, path.read_text()

    def _scan_existing_custom_operations(self):
        """
        Scan all local apps' migration files to discover already-defined custom operations.

        Returns a 4-tuple of:
            - existing_triggers:          set of table names with updated_at triggers
            - existing_soft_deletes:      set of table names with soft-delete rules
            - existing_soft_delete_related: set of ``(related_table, table)`` tuples
            - trigger_function_dep:       ``(app_label, migration_stem)`` or ``None``
        """
        existing_triggers: set[str] = set()
        existing_soft_deletes: set[str] = set()
        existing_soft_delete_related: set[tuple[str, str]] = set()
        trigger_function_dep: tuple[str, str] | None = None

        for app in django_apps.get_app_configs():
            if app.name not in settings.LOCAL_APPS:
                continue
            for path, content in self._iter_migration_files(app):
                if _RE_TRIGGER_FUNCTION.search(content):
                    trigger_function_dep = (app.label, path.stem)

                existing_triggers.update(m.group(1) for m in _RE_UPDATED_AT.finditer(content))
                existing_soft_deletes.update(m.group(1) for m in _RE_SOFT_DELETE.finditer(content))
                existing_soft_delete_related.update(
                    (m.group(1), m.group(2)) for m in _RE_SOFT_DELETE_RELATED.finditer(content)
                )

        return (
            existing_triggers,
            existing_soft_deletes,
            existing_soft_delete_related,
            trigger_function_dep,
        )

    # ------------------------------------------------------------------
    # Migration-file helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _migration_with_digest_exists(app: AppConfig, operations_digest: str) -> bool:
        """Return ``True`` if a migration with the given digest already exists in *app*."""
        migrations_dir = Path(app.path) / 'migrations'
        if not migrations_dir.is_dir():
            return False
        for path in migrations_dir.glob('*.py'):
            first_line = path.read_text().split('\n', 1)[0]
            match = _RE_DIGEST.search(first_line)
            if match and match.group('digest') == operations_digest:
                return True
        return False

    def _get_trigger_function_host_app(self) -> AppConfig:
        """Return the ``AppConfig`` that will host the singleton trigger-function migration."""
        host_app_name = getattr(settings, 'TRIGGER_FUNCTION_APP', None) or settings.LOCAL_APPS[0]
        host_app_label = host_app_name.rsplit('.', 1)[-1]
        return django_apps.get_app_config(host_app_label)

    def _create_empty_migration_file(
        self, app: AppConfig, name: str = 'auto_advanced'
    ) -> str:  # pragma: no cover
        """Run ``makemigrations --empty`` and return the created filename."""
        buf = StringIO()
        call_command('makemigrations', app.label, '--name', name, '--empty', stdout=buf)
        output = buf.getvalue()

        pattern = rf'/(?P<filename>\d{{4}}_{re.escape(name)}\.py)'
        match = re.search(pattern, output)
        if not match:
            raise CommandError(
                f'Could not find the created migration file! Command output: {output}'
            )

        return match.group('filename')

    @staticmethod
    def _write_migration_file(
        app: AppConfig,
        migration_file: str,
        operations: list[str],
        operations_digest: str,
        dependency: tuple[str, str] | None = None,
    ) -> None:
        """Rewrite a migration file to include the given custom *operations*."""
        file_path = Path(app.path) / 'migrations' / migration_file
        lines = file_path.read_text().splitlines(keepends=True)

        # Replace the first line with our digest marker.
        lines[0] = f'# Generated by makeguitarmigrations command! [DIGEST:{operations_digest}]\n'

        # Insert the sql import right after the existing imports.
        lines.insert(3, 'from guitars import sql\n')
        lines.insert(3, '\n')

        # Append indented operations before the closing bracket.
        for operation in operations:
            indented = textwrap.indent(operation, ' ' * 8)
            for line in indented.split('\n'):
                lines.insert(-1, f'{line}\n')

        # Add a dependency on the trigger-function migration if needed.
        migration_stem = Path(migration_file).stem
        if dependency and migration_stem != dependency[1]:
            dep_line = f'        ("{dependency[0]}", "{dependency[1]}"),\n'
            dep_idx = next(i for i, line in enumerate(lines) if 'dependencies = [' in line)
            lines.insert(dep_idx + 1, dep_line)

        file_path.write_text(''.join(lines))

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
        operations_digest = hashlib.md5(
            TRIGGER_FUNCTION_OPERATION.encode(), usedforsecurity=False
        ).hexdigest()
        migration_file = self._create_empty_migration_file(
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

            if hasattr(model, '_updated_at') and table not in self.existing_triggers:
                operations.append(
                    UPDATED_AT_OPERATION.format(table=table, primary_key=primary_key)
                )

            if hasattr(model, '_deleted_at'):
                if table not in self.existing_soft_deletes:
                    operations.append(
                        SOFT_DELETE_OPERATION.format(table=table, primary_key=primary_key)
                    )

                for related_model, fk_field, on_delete in sorted(
                    self.reverse_relations_mapping[model],
                    key=lambda t: (t[0]._meta.db_table, t[1].column),
                ):
                    if on_delete != models.CASCADE or not hasattr(related_model, '_deleted_at'):
                        continue
                    related_table = related_model._meta.db_table
                    if (related_table, table) in self.existing_soft_delete_related:
                        continue
                    deferred.append(
                        SOFT_DELETE_RELATED_OPERATION.format(
                            table=table,
                            related_table=related_table,
                            primary_key=primary_key,
                            foreign_key=fk_field.column,
                        )
                    )

        return operations + deferred

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def handle(self, *args, **options):  # pragma: no cover
        check_only: bool = options['check_only']

        # Step 1: Ensure the singleton trigger-function migration exists,
        # so all subsequent app migrations can safely depend on it.
        needs_trigger_function = any(
            hasattr(model, '_updated_at')
            for app in django_apps.get_app_configs()
            if app.name in settings.LOCAL_APPS
            for model in app.get_models()
        )
        # Step 2: Per-app table-specific trigger / rule migrations.
        changes_made = needs_trigger_function and self._ensure_trigger_function_migration(
            check_only=check_only
        )
        check_missing: list[tuple[str, list[str]]] = []

        for app in django_apps.get_app_configs():
            if app.name not in settings.LOCAL_APPS:
                continue

            operations = self._build_operations(app)
            if not operations:
                continue

            operations_digest = hashlib.md5(
                '\n'.join(operations).encode(), usedforsecurity=False
            ).hexdigest()
            if self._migration_with_digest_exists(app, operations_digest):
                continue

            if check_only:
                check_missing.append((app.label, operations))
                continue

            migration_file = self._create_empty_migration_file(app)
            self._write_migration_file(
                app=app,
                migration_file=migration_file,
                operations=operations,
                operations_digest=operations_digest,
                dependency=self.trigger_function_dependency,
            )

            self.stdout.write(
                self.style.MIGRATE_HEADING(f"Advanced migrations for '{app.label}':")
            )
            self.stdout.write(f'  migrations/{migration_file}')
            changes_made = True

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
