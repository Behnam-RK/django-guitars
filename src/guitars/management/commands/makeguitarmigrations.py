"""Generate the kit's **enforcement migrations**.

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

**Idempotency has two layers**, and both matter. A ``[DIGEST:...]`` marker on the first
line identifies an unchanged operation set; per-operation comment headers
(``# Updated at Trigger on "x" table!``) identify which tables are already covered, so a
*partially* covered app gets only the genuinely new operations. Those header strings are
therefore **frozen**: reword one and every existing migration stops being recognised, and
the next run emits duplicates.
"""

from __future__ import annotations

import re
import textwrap
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from django.apps import apps as django_apps
from django.conf import settings
from django.core.management import CommandError
from django.core.management.base import BaseCommand
from django.db import models

from guitars.introspection import column_owner, has_column, is_mti_child, owns_column
from guitars.management import _generator
from guitars.tenancy.discovery import app_coverage


if TYPE_CHECKING:
    from django.apps import AppConfig

    from guitars.tenancy.discovery import TableCoverage


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

TENANT_POLICY_OPERATION = """\
# Tenant RLS on "{table}" table!
migrations.RunSQL(
    sql=sql.create_table_rls({arguments}),
    reverse_sql=sql.drop_table_rls(table='{table}'{exempt_argument}),
),
"""

TENANT_FORCE_OPERATION = """\
# Tenant FORCE RLS on "{table}" table!
migrations.RunSQL(
    sql=sql.force_rls(table='{table}'),
    reverse_sql=sql.no_force_rls(table='{table}'),
),
"""

# Regex patterns for recognising enforcement operations already written to migration files.
# These headers are the dedupe keys -- see the module docstring on why they are frozen.
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
# The FORCE header carries an extra token, so the plain RLS pattern can never match it.
_RE_TENANT_POLICY = re.compile(r'# Tenant RLS on "([^"]+)" table!')
_RE_TENANT_FORCE = re.compile(r'# Tenant FORCE RLS on "([^"]+)" table!')
# A policy operation that shipped *without* inline FORCE -- the only kind `--force-rls` has
# anything to do for. `force` is written into the operation as a literal by
# `_tenant_policy_operation`, so the migration text is the record of what was decided when it
# was generated. Without this the flag emitted a redundant FORCE migration for every tenanted
# table on any project using the default GUITARS_RLS_FORCE = True.
_RE_TENANT_POLICY_UNFORCED = re.compile(
    r'# Tenant RLS on "([^"]+)" table!\n(?:.*\n){0,6}?.*force=False', re.MULTILINE
)
# The [DIGEST:...] marker is matched by _generator.RE_DIGEST.


def _literal(value: object) -> str:
    """Render a value into a generated migration, deterministically.

    Dicts and sets are emitted in sorted order so the content digest is stable. An unstable
    rendering produces a new digest -- and therefore a new migration -- on every run.
    """
    if isinstance(value, dict):
        items = ', '.join(f'{_literal(k)}: {_literal(v)}' for k, v in sorted(value.items()))
        return '{' + items + '}'
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(_literal(item) for item in value) + ']'
    if isinstance(value, str):
        return f"'{value}'"
    return repr(value)


class ExistingOperations(NamedTuple):
    """Which enforcement operations the migration files already contain.

    Scanned once at construction, by comment header, so a partially covered app receives
    only the operations it is genuinely missing. Named rather than a positional tuple: this
    grew to seven fields while it was one, and a caller unpacking seven anonymous sets in
    the right order is a bug waiting to happen.
    """

    triggers: set[str]
    soft_deletes: set[str]
    soft_delete_related: set[tuple[str, str]]
    mti_triggers: set[str]
    mti_soft_deletes: set[str]
    tenant_policies: set[str]
    #: Tables whose policy operation was written with ``force=False`` -- see
    #: ``_RE_TENANT_POLICY_UNFORCED``. These are the only ones a second FORCE stage can act on.
    unforced_policies: set[str]
    tenant_forces: set[str]
    trigger_function_dependency: tuple[str, str] | None
    parent_trigger_function_dependency: tuple[str, str] | None


class Command(BaseCommand):
    """Generates the enforcement migrations: triggers, rules and tenant policies.

    Each kind is driven by the shape of the model, so declaring the thing *is* the opt-in
    and there is no registry to keep in step:

    * ``_updated_at`` -> a statement-level timestamp trigger.
    * ``_deleted_at`` -> a soft-delete rule, plus cascade rules for related
      soft-deletable models whose FK is ``on_delete=CASCADE``.
    * a ``TenantedManager`` -> a row-level-security tenant policy.

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

        # (app_label, migration_stem) tuples or None, pointing at the singleton function migrations.
        self.trigger_function_dependency: tuple[str, str] | None = None
        self.parent_trigger_function_dependency: tuple[str, str] | None = None

        # Cross-app / MTI cascade rules skipped this run, surfaced as warnings (not silent).
        self._mti_cascade_warnings: list[str] = []
        # Tables tenancy discovery could not cover, with the reason. Also surfaced.
        self._tenancy_notes: list[str] = []

        self.existing = self._scan_existing_operations()
        self.trigger_function_dependency = self.existing.trigger_function_dependency
        self.parent_trigger_function_dependency = self.existing.parent_trigger_function_dependency

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
    # Migration-file scanning
    # ------------------------------------------------------------------

    def _scan_existing_operations(self) -> ExistingOperations:
        """Scan every local app's migration files for enforcement operations already written.

        Recognition is by comment header, per operation, so an app that is partially covered
        receives exactly the operations it lacks rather than a duplicate of the whole set.
        """
        existing_triggers: set[str] = set()
        existing_soft_deletes: set[str] = set()
        existing_soft_delete_related: set[tuple[str, str]] = set()
        existing_mti_triggers: set[str] = set()
        existing_mti_soft_deletes: set[str] = set()
        existing_tenant_policies: set[str] = set()
        existing_unforced_policies: set[str] = set()
        existing_tenant_forces: set[str] = set()
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
                existing_tenant_policies.update(
                    m.group(1) for m in _RE_TENANT_POLICY.finditer(content)
                )
                existing_unforced_policies.update(
                    m.group(1) for m in _RE_TENANT_POLICY_UNFORCED.finditer(content)
                )
                existing_tenant_forces.update(
                    m.group(1) for m in _RE_TENANT_FORCE.finditer(content)
                )

        return ExistingOperations(
            triggers=existing_triggers,
            soft_deletes=existing_soft_deletes,
            soft_delete_related=existing_soft_delete_related,
            mti_triggers=existing_mti_triggers,
            mti_soft_deletes=existing_mti_soft_deletes,
            tenant_policies=existing_tenant_policies,
            unforced_policies=existing_unforced_policies,
            tenant_forces=existing_tenant_forces,
            trigger_function_dependency=trigger_function_dep,
            parent_trigger_function_dependency=parent_trigger_function_dep,
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
        live in ``_generator``, shared with every command that generates migrations.
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

    def _ensure_trigger_function_migration(self, *, check_only: bool = False) -> bool:
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
            host_app, name='auto_enforcement_trigger_function'
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
            self.style.MIGRATE_HEADING(f"Enforcement migrations for '{host_app.label}':")
        )
        self.stdout.write(f'  migrations/{migration_file}')
        return True

    def _ensure_parent_trigger_function_migration(self, *, check_only: bool = False) -> bool:
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
            host_app, name='auto_enforcement_parent_trigger_function'
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
            self.style.MIGRATE_HEADING(f"Enforcement migrations for '{host_app.label}':")
        )
        self.stdout.write(f'  migrations/{migration_file}')
        return True

    # ------------------------------------------------------------------
    # Per-app operations
    # ------------------------------------------------------------------

    def _tenant_policy_operation(self, table: str, coverage: TableCoverage) -> str:
        """One ``tenant_scope`` policy operation, with the environment decisions baked in.

        ``force`` and ``exempt_roles`` are written into the migration as literals rather
        than read from settings at migrate time, so the same migration history always
        produces the same database -- see ``guitars.sql.policy``.
        """
        exempt_roles = self._rls_exempt_roles()
        arguments = {
            'table': table,
            **coverage.as_kwargs(),
            'force': self._rls_force_enabled(),
        }
        if exempt_roles:
            arguments['exempt_roles'] = exempt_roles
        return TENANT_POLICY_OPERATION.format(
            table=table,
            arguments=', '.join(f'{key}={_literal(value)}' for key, value in arguments.items()),
            # The reverse has to drop the same exemption policies the forward created, so the
            # role list is repeated rather than left to a default.
            exempt_argument=f', exempt_roles={_literal(exempt_roles)}' if exempt_roles else '',
        )

    def _tenant_operations(self, app: AppConfig, *, force_rls: bool) -> list[str]:
        """Tenant-policy operations *app* is missing, for the requested stage."""
        if not self._tenant_policies_enabled():
            return []

        coverage = app_coverage(app)
        self._tenancy_notes.extend(coverage.notes)

        operations: list[str] = []
        for table, table_coverage in sorted(coverage.tables.items()):
            if force_rls:
                # Three ways there is nothing to do, and each would otherwise write a
                # migration that changes nothing:
                #   * FORCE already has its own operation;
                #   * no policy operation exists yet -- a coverage gap FORCE must not paper
                #     over by forcing a table that nothing scopes;
                #   * the policy shipped with FORCE inline, which is the default.
                if (
                    table in self.existing.tenant_forces
                    or table not in self.existing.tenant_policies
                    or table not in self.existing.unforced_policies
                ):
                    continue
                operations.append(TENANT_FORCE_OPERATION.format(table=table))
            elif table not in self.existing.tenant_policies:
                operations.append(self._tenant_policy_operation(table, table_coverage))
        return operations

    def _build_operations(self, app: AppConfig) -> list[str]:
        """Return a list of SQL operation snippets needed for *app*'s models."""
        operations: list[str] = []
        deferred: list[str] = []

        for model in app.get_models():
            table = model._meta.db_table
            primary_key = model._meta.pk.name

            # --- updated_at trigger: own table vs. MTI parent-propagation ---
            if owns_column(model, '_updated_at'):
                if table not in self.existing.triggers:
                    operations.append(
                        UPDATED_AT_OPERATION.format(table=table, primary_key=primary_key)
                    )
            elif is_mti_child(model, '_updated_at') and table not in self.existing.mti_triggers:
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
                if table not in self.existing.soft_deletes:
                    operations.append(
                        SOFT_DELETE_OPERATION.format(table=table, primary_key=primary_key)
                    )
            elif (
                is_mti_child(model, '_deleted_at') and table not in self.existing.mti_soft_deletes
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

        # Tenant policies last: they are independent of the triggers and rules above (a
        # policy references neither), so they sort to the end where they read as a group.
        return operations + deferred + self._tenant_operations(app, force_rls=False)

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
            # An FK reached through MTI is not a second FK: it is the *same physical column*
            # on the ancestor's table. The ancestor appears in this same loop with that
            # column local to it, so its rule is emitted -- and because every table in an
            # MTI chain shares one ``_deleted_at``, that one rule already archives the
            # children too. Emitting anything here would be a duplicate, and warning about
            # it would report a covered case as a limitation.
            if fk_field.model is not related_model:
                continue
            # The flat cascade rule does ``UPDATE related_table SET _deleted_at`` -- only valid
            # when the related child owns ``_deleted_at`` on the very table its FK lives on.
            # An FK declared on an MTI child's *own* table while its ``_deleted_at`` lives on a
            # farther ancestor needs a join form we don't emit yet; surface it instead of
            # writing a rule that references a missing column.
            if not owns_column(related_model, '_deleted_at'):
                self._mti_cascade_warnings.append(
                    f"Cascade rule for '{related_table}' -> '{owner_table}' skipped: "
                    f"'{related_model.__name__}' declares this foreign key on its own table "
                    'but inherits _deleted_at from a multi-table-inheritance ancestor, which '
                    'needs a join form the generator does not emit yet.'
                )
                continue
            if (related_table, owner_table) in self.existing.soft_delete_related:
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
                    if (related_table, table) in self.existing.soft_delete_related:
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
        force_rls: bool = options.get('force_rls', False)
        # Positional app labels scope generation; empty => all local apps.
        requested: set[str] = set(app_labels)

        _generator.validate_app_labels(requested)

        if force_rls:
            return self._handle_force_rls_stage(requested, check_only=check_only)

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

            migration_file = _generator.create_empty_migration_file(app, 'auto_enforcement')
            self._write_migration_file(
                app=app,
                migration_file=migration_file,
                operations=operations,
                operations_digest=operations_digest,
                dependencies=self._function_dependencies_for(operations_blob),
            )

            self.stdout.write(
                self.style.MIGRATE_HEADING(f"Enforcement migrations for '{app.label}':")
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

        # Tables tenancy could not cover, and why. Skips are design, never silent.
        for note in self._tenancy_notes:
            self.stdout.write(self.style.WARNING(note))

        if check_missing:
            self._report_missing(check_missing)

        if not changes_made and not check_only:
            self.stdout.write('No changes detected')

    def _report_missing(self, check_missing: list[tuple[str, list[str]]]) -> None:
        """Print what ``--check`` found and exit non-zero."""
        for app_label, operations in check_missing:
            self.stderr.write(
                self.style.ERROR(f"Missing enforcement migrations for '{app_label}':")
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

        changes_made = False
        check_missing: list[tuple[str, list[str]]] = []
        for app in django_apps.get_app_configs():
            if not _generator.is_in_scope(app, requested):
                continue

            operations = self._tenant_operations(app, force_rls=True)
            if not operations:
                continue

            operations_digest = _generator.digest_of(operations)
            if _generator.migration_with_digest_exists(app, operations_digest):
                continue

            if check_only:
                check_missing.append((app.label, operations))
                continue

            migration_file = _generator.create_empty_migration_file(app, 'auto_tenant_force')
            self._write_migration_file(
                app=app,
                migration_file=migration_file,
                operations=operations,
                operations_digest=operations_digest,
            )
            self.stdout.write(
                self.style.MIGRATE_HEADING(f"Enforcement migrations for '{app.label}':")
            )
            self.stdout.write(f'  migrations/{migration_file}')
            changes_made = True

        for note in self._tenancy_notes:
            self.stdout.write(self.style.WARNING(note))

        if check_missing:
            self._report_missing(check_missing)

        if not changes_made and not check_only:
            self.stdout.write('No changes detected')
