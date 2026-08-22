"""Building enforcement operations for an app's models: models declare an
``_updated_at``/``_deleted_at`` column or a ``tenanted_manager()``; this turns that into
``RunSQL`` snippets, diffed against what ``scanning.scan_existing_operations`` found."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, cast

from django.apps import apps as django_apps
from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.migrations.loader import MigrationLoader

from guitars import sql
from guitars.introspection import (
    OwnerArm,
    column_owner,
    has_column,
    is_mti_child,
    owned_tenancy_refusals,
    owner_arms,
    owns_column,
    rule_update_cycle_edges,
)
from guitars.management import _generator
from guitars.management.enforcement.graph import (
    ObjectRef,
    resolve_dependencies,
    resolve_object_migration,
    would_close_a_cycle,
)
from guitars.management.enforcement.headers import (
    _RE_MTI_UPDATED_AT,
    _RE_TENANT_AUTOFILL,
    _RE_TENANT_AUTOFILL_RETIRED,
    _RE_UPDATED_AT,
    HEADER_MTI_SOFT_DELETE,
    HEADER_MTI_UPDATED_AT,
    HEADER_SOFT_DELETE,
    HEADER_SOFT_DELETE_OWNED,
    HEADER_SOFT_DELETE_RELATED,
    HEADER_SOFT_DELETE_RELATED_VIA,
    HEADER_TENANT_AUTOFILL,
    HEADER_TENANT_AUTOFILL_RETIRED,
    HEADER_TENANT_FORCE,
    HEADER_TENANT_POLICY,
    HEADER_TENANT_POLICY_REPLACED,
    HEADER_UPDATED_AT,
    RE_TENANT_AUTOFILL_FUNCTION,
)
from guitars.management.enforcement.identity import _literal, _operation
from guitars.models.fields import OwningForeignKey, _targets_primary_key
from guitars.sql import _identifiers
from guitars.sql import soft_delete as _soft_delete
from guitars.sql import triggers as _triggers
from guitars.tenancy.discovery import (
    app_coverage,
    autofill_function_name,
    autofill_trigger_name,
)


if TYPE_CHECKING:
    from collections.abc import Callable

    from django.apps import AppConfig
    from django.core.management.base import OutputWrapper
    from django.core.management.color import Style

    from guitars.management.enforcement.scanning import ExistingOperations
    from guitars.tenancy.discovery import TableCoverage


class _OperationRow(NamedTuple):
    """One row for :meth:`Command._append_if_stale`. Named rather than four positional args
    per call site (trigger/soft-delete x own-table/MTI): those sites differ only in which
    constants they name, so one row shape read through a single loop shows the difference."""

    recorded: dict
    key: object
    header: str
    forward: str | list[str]
    reverse: str | list[str]
    replace: str | list[str] | None = None
    adopt: str | list[str] | None = None


def _rule_stem(prefix: str, table: str) -> str:
    """``<prefix>_<table>``, schema folded in **length-prefixed** rather than underscore-joined:
    plain ``f'{schema}_{table}'`` lets ``('tenant_a', 'events')`` and ``('tenant', 'a_events')``
    collide on one name."""
    schema, bare_table = _identifiers._split_qualified('table', table)
    return (
        f'{prefix}_{bare_table}'
        if schema is None
        else f'{prefix}_{len(schema)}_{schema}_{bare_table}'
    )


def _related_rule_name(related_table: str, foreign_key: str | None = None) -> str:
    """The inbound cascade rule's identifier, NAMEDATALEN-truncated before quoting. One FK per
    pair keeps the bare, unsuffixed form for backward compatibility, so *foreign_key* is
    optional here and required for owned."""
    # Plain-joined, ambiguous the way the stem is not -- and frozen: this spelling shipped in
    # 0.x, and since no command retires a rule, renaming it would leave every migrated project
    # with the old rule live beside the new. ``_claim_rule_name`` reports a clash instead.
    stem = _rule_stem('soft_delete_related', related_table)
    return _identifiers._safe_ident(stem if foreign_key is None else f'{stem}_{foreign_key}')


def _sized(segment: str) -> str:
    """One name segment with its length in front of it, so a left-to-right read finds where it
    ends -- the only way concatenating variable-length identifiers stays reversible."""
    return f'{len(segment)}_{segment}'


def _owned_rule_name(dependent_table: str, foreign_key: str) -> str:
    """The owned rule's identifier: **every** variable segment sized, so no two
    ``(schema, table, foreign_key)`` triples can name one rule. Nothing predates 2.3.0, so this
    family was free to be built that way where the frozen cascade one cannot be."""
    # Sized *each*, not just the last: a length at one end alone rules out only the adjacent
    # split, so ``('a_5_b', 'c')`` would still have met ``('a', 'b_1_c')``. Reading each length
    # before its segment leaves no boundary to guess at -- a proof, not a narrowing.
    schema, bare_table = _identifiers._split_qualified('table', dependent_table)
    sized_schema = [] if schema is None else [_sized(schema)]
    # The prefix differs from the cascade family for the same reason both are guarded: a rule
    # is namespaced per table by name alone, so a shared name replaces rather than collides.
    parts = ['soft_delete_owned', *sized_schema, _sized(bare_table), _sized(foreign_key)]
    return _identifiers._safe_ident('_'.join(parts))


def _rule_relation_label(relation: tuple) -> str:
    """A relation as prose for a clash report, phrased like the headers: the other table and
    the column, never which of the two holds it -- the cascade family reads the column off the
    child and the owned family off the owner, and the report names the table separately."""
    other, _table, foreign_key = relation
    return f"'{other}' via '{foreign_key}'"


class OperationsMixin:
    """Per-app enforcement-operation building, shared by Command via multiple inheritance."""

    if TYPE_CHECKING:
        # Provided by Command (command.py) once the two are combined -- declared here only
        # so the type checker knows what `self` carries. No runtime presence.
        existing: ExistingOperations
        stdout: OutputWrapper
        stderr: OutputWrapper
        style: Style
        _tenancy_notes: list[str]
        _mti_cascade_warnings: list[str]
        _rule_name_clashes: list[str]
        _claimed_rule_names: dict[tuple[str, str], tuple]
        trigger_function_dependency: tuple[str, str] | None
        parent_trigger_function_dependency: tuple[str, str] | None
        tenant_autofill_dependencies: dict[str, tuple[str, str]]
        reverse_relations_mapping: dict[type[models.Model], set]
        all_models: list[type[models.Model]]
        _rule_cycle_cache: set[tuple[str, str]] | None
        _owner_arms_cache: dict[str, list[OwnerArm]] | None
        _owned_tenancy_cache: dict[tuple[str, str, str], list[str]] | None
        _object_refs: dict[str, list[ObjectRef]]
        _refusals_over_live_rules: list[str]
        _missing_edges: list[str]
        _table_app_labels_cache: dict[str, str] | None
        _required_autofill_cache: dict[tuple[str, str], tuple[str, str]] | None
        _relocated_autofill_cache: dict[tuple[str, str], tuple[str, str]] | None

        @staticmethod
        def _write_migration_file(
            app: AppConfig,
            migration_file: str,
            operations: list[str],
            operations_digest: str,
            dependencies: list[tuple[str, str]] | None = None,
        ) -> None: ...
        @staticmethod
        def _tenant_policies_enabled() -> bool: ...
        @staticmethod
        def _rls_force_enabled() -> bool: ...
        @staticmethod
        def _rls_exempt_roles() -> list[str]: ...

    def _policy_identity(self, table: str, coverage: TableCoverage) -> str:
        """Digest of what the ``tenant_scope`` policy *says*, stamped into the header so a
        later run can tell "has a policy" from "has the policy the models imply". ``force``
        is excluded -- folding it in would replace every table on one settings flip."""
        identity = {
            'table': table,
            **coverage.as_kwargs(),
            'exempt_roles': self._rls_exempt_roles(),
        }
        return _generator.digest_of([_literal(identity)])[:12]

    def _tenant_policy_operation(
        self, table: str, coverage: TableCoverage, *, replacing: bool
    ) -> tuple[str, str]:
        """One ``tenant_scope`` policy operation, ``force``/``exempt_roles`` resolved from
        settings and written in literally. *replacing* picks ``replace_table_rls``, whose
        honest ``reverse_sql`` drops RLS rather than claiming to restore an unknown shape."""
        # Kept out of the coverage mapping rather than merged into it: the coverage kwargs are
        # a typed shape describing what the policy predicates on, while these two are
        # environment decisions resolved here so they can be written into the SQL literally.
        exempt_roles = self._rls_exempt_roles() or None
        force = self._rls_force_enabled()
        coverage_kwargs = coverage.as_kwargs()

        forward = sql.create_table_rls(
            table=table, force=force, exempt_roles=exempt_roles, **coverage_kwargs
        )
        reverse = sql.drop_table_rls(table=table, exempt_roles=exempt_roles)
        # The header's `{table}` slot is quote-delimited, so a table already containing a
        # literal `"` needs _escape_ident or it closes the delimiter early and the scanner
        # never matches. scanning.py's _unescape_ident undoes it for the dict-key round trip.
        header = (HEADER_TENANT_POLICY_REPLACED if replacing else HEADER_TENANT_POLICY).format(
            table=_identifiers._escape_ident(table),
            identity=self._policy_identity(table, coverage),
        )
        return _operation(
            header,
            forward,
            reverse,
            emit=sql.replace_table_rls(
                table=table, force=force, exempt_roles=exempt_roles, **coverage_kwargs
            )
            if replacing
            else None,
        )

    def _tenant_force_operations(self, app: AppConfig) -> list[str]:
        """FORCE-only operations for *app* -- the ``--force-rls`` retrofit stage. Only
        touches a table already policied whose policy shipped without FORCE inline; new
        policies emit FORCE themselves, so this is purely the legacy backlog."""
        if not self._tenant_policies_enabled():
            return []

        coverage = app_coverage(app)
        self._tenancy_notes.extend(coverage.notes)

        operations: list[str] = []
        for table in sorted(coverage.tables):
            # Nothing to do if: FORCE already has its own operation; no policy operation
            # exists yet (a coverage gap FORCE must not paper over); or the policy shipped
            # with FORCE inline already, the default.
            if (
                table in self.existing.tenant_forces
                or table not in self.existing.tenant_policies
                or table not in self.existing.unforced_policies
            ):
                continue
            force_source, _ = _operation(
                # See _tenant_policy_operation's comment on why the header's `{table}` slot
                # needs _escape_ident (the SQL's own table arg, below, is separate).
                HEADER_TENANT_FORCE.format(table=_identifiers._escape_ident(table)),
                sql.force_rls(table=table),
                sql.no_force_rls(table=table),
            )
            operations.append(force_source)
        return operations

    def _tenant_policy_operations(self, app: AppConfig, *, adopt: bool = False) -> list[str]:
        """Tenant-policy create/replace operations *app* is missing or has outdated."""
        if not self._tenant_policies_enabled():
            return []

        coverage = app_coverage(app)
        self._tenancy_notes.extend(coverage.notes)

        operations: list[str] = []
        for table, table_coverage in sorted(coverage.tables.items()):
            # Two independent reasons to replace: the identity answers "does the policy
            # still say what the models imply" (a dimension or role changed); the SQL digest
            # answers "is the emitted text still what's on disk". Checking only one misses the other.
            recorded_identity = self.existing.tenant_policy_identities.get(table)
            current_identity = self._policy_identity(table, table_coverage)
            create_source, create_digest = self._tenant_policy_operation(
                table, table_coverage, replacing=False
            )

            if adopt:
                # --adopt's premise: a policy exists but was never recorded, and Postgres has
                # no CREATE POLICY IF NOT EXISTS. The replace form drops first and is correct
                # either way -- the only thing the generator can honestly assume here.
                replace_source, _ = self._tenant_policy_operation(
                    table, table_coverage, replacing=True
                )
                operations.append(replace_source)
            elif recorded_identity is None:
                operations.append(create_source)
            elif (
                recorded_identity != current_identity
                or self.existing.tenant_policy_sql.get(table) != create_digest
            ):
                replace_source, _ = self._tenant_policy_operation(
                    table, table_coverage, replacing=True
                )
                operations.append(replace_source)
        return operations

    def _append_if_stale(
        self,
        operations: list[str],
        recorded: dict,
        key,
        header: str,
        forward: str | list[str],
        reverse: str | list[str],
        *,
        is_adopt: bool = False,
        replace: str | list[str] | None = None,
        adopt: str | list[str] | None = None,
    ) -> None:
        """Append one operation unless already current. Which of the three forms
        (plain/replace/adopt) is decided by what the migration history knows -- see
        ``docs/migrations.md``'s three-forms section. *adopt* is the SQL for it."""
        source, digest = _operation(header, forward, reverse)
        if is_adopt:
            source, _ = _operation(header, forward, reverse, emit=adopt or replace or forward)
        elif key not in recorded:
            pass  # `source` already holds the create form.
        elif recorded[key] == digest:
            return
        else:
            source, _ = _operation(header, forward, reverse, emit=replace or forward)
        operations.append(source)

    @staticmethod
    def _mti_context(model: type[models.Model], table: str, column: str) -> dict[str, str]:
        """The ``{child_table, child_pk, parent_table, parent_pk}`` an MTI operation needs.
        Parametrized on *column*: ``_updated_at``/``_deleted_at`` resolve independently via
        :func:`column_owner`, and nothing guarantees the same ancestor owns both."""
        owner = column_owner(model, column)
        return {
            'child_table': table,
            'child_pk': cast(str, model._meta.pk.column),
            'parent_table': owner._meta.db_table,
            'parent_pk': cast(str, owner._meta.pk.column),
        }

    def _build_operations(self, app: AppConfig, *, adopt: bool = False) -> list[str]:
        """Return a list of SQL operation snippets needed for *app*'s models."""
        operations: list[str] = []
        deferred: list[str] = []

        for model in app.get_models():
            table = model._meta.db_table
            # The *column*, not the field name -- they agree for a plain `id` pk, but a
            # `OneToOneField(primary_key=True)` pk (name `owner`, column `owner_id`) would
            # otherwise produce a rule referencing a column that doesn't exist.
            primary_key = cast(str, model._meta.pk.column)

            rows: list[_OperationRow] = []

            # --- updated_at trigger: own table vs. MTI parent-propagation --- `table`/
            # `child_table` are DDL positions (_quote_table); `primary_key`/`parent_pk`/
            # `child_pk` are literal trigger-function arguments (_escape_literal).
            if owns_column(model, '_updated_at'):
                qualified_table = _identifiers._quote_table(table)
                literal_primary_key = _identifiers._escape_literal(primary_key)
                rows.append(
                    _OperationRow(
                        recorded=self.existing.triggers,
                        key=table,
                        # The header's `{table}` slot needs _escape_ident, unlike
                        # `qualified_table` above (the SQL body's own DDL-ready form) --
                        # see _tenant_policy_operation's comment.
                        header=HEADER_UPDATED_AT.format(table=_identifiers._escape_ident(table)),
                        forward=sql.CREATE_UPDATED_AT_TRIGGER.format(
                            table=qualified_table, primary_key=literal_primary_key
                        ),
                        reverse=sql.DROP_UPDATED_AT_TRIGGER.format(table=qualified_table),
                        replace=sql.REPLACE_UPDATED_AT_TRIGGER.format(
                            table=qualified_table, primary_key=literal_primary_key
                        ),
                        adopt=sql.ADOPT_UPDATED_AT_TRIGGER.format(
                            table=qualified_table, primary_key=literal_primary_key
                        ),
                    )
                )
            elif is_mti_child(model, '_updated_at'):
                mti = self._mti_context(model, table, '_updated_at')
                # _split_qualified, not the validating _bare_or_qualified: parent_schema/
                # parent_table become escaped *literal* args, re-quoted by %I at trigger-fire
                # time -- a hostile-but-legal ancestor db_table must not be rejected here.
                parent_schema, parent_bare_table = _identifiers._split_qualified(
                    'table', mti['parent_table']
                )
                mti_literal = {
                    'child_table': _identifiers._quote_table(mti['child_table']),
                    'parent_schema': _identifiers._escape_literal(parent_schema or ''),
                    'parent_table': _identifiers._escape_literal(parent_bare_table),
                    'parent_pk': _identifiers._escape_literal(mti['parent_pk']),
                    'child_pk': _identifiers._escape_literal(mti['child_pk']),
                }
                # Header placeholders only -- see _tenant_policy_operation's comment for why.
                mti_header = {
                    'child_table': _identifiers._escape_ident(mti['child_table']),
                    'parent_table': _identifiers._escape_ident(mti['parent_table']),
                }
                rows.append(
                    _OperationRow(
                        recorded=self.existing.mti_triggers,
                        key=table,
                        header=HEADER_MTI_UPDATED_AT.format(**mti_header),
                        forward=_triggers._CREATE_PARENT_UPDATED_AT_TRIGGER.format(**mti_literal),
                        reverse=_triggers._DROP_PARENT_UPDATED_AT_TRIGGER.format(
                            child_table=_identifiers._quote_table(table)
                        ),
                        replace=_triggers._REPLACE_PARENT_UPDATED_AT_TRIGGER.format(**mti_literal),
                        adopt=_triggers._ADOPT_PARENT_UPDATED_AT_TRIGGER.format(**mti_literal),
                    )
                )

            # --- soft-delete rule: own table vs. MTI redirect-to-owner --- No replace/adopt
            # form: created OR REPLACE, since an instant without one is an instant where
            # DELETE destroys rows.
            if owns_column(model, '_deleted_at'):
                qualified_table = _identifiers._quote_table(table)
                rows.append(
                    _OperationRow(
                        recorded=self.existing.soft_deletes,
                        key=table,
                        header=HEADER_SOFT_DELETE.format(table=_identifiers._escape_ident(table)),
                        forward=sql.CREATE_SOFT_DELETE_RULE.format(
                            table=qualified_table,
                            primary_key=_identifiers._escape_ident(primary_key),
                        ),
                        reverse=sql.DROP_SOFT_DELETE_RULE.format(table=qualified_table),
                    )
                )
            elif is_mti_child(model, '_deleted_at'):
                mti = self._mti_context(model, table, '_deleted_at')
                mti_ident = {
                    'child_table': _identifiers._quote_table(mti['child_table']),
                    'parent_table': _identifiers._quote_table(mti['parent_table']),
                    'child_pk': _identifiers._escape_ident(mti['child_pk']),
                    'parent_pk': _identifiers._escape_ident(mti['parent_pk']),
                }
                # Header placeholders only -- see the updated_at branch above for why this
                # is separate from mti_ident (which quotes for the SQL body, not a comment).
                mti_header = {
                    'child_table': _identifiers._escape_ident(mti['child_table']),
                    'parent_table': _identifiers._escape_ident(mti['parent_table']),
                }
                rows.append(
                    _OperationRow(
                        recorded=self.existing.mti_soft_deletes,
                        key=table,
                        header=HEADER_MTI_SOFT_DELETE.format(**mti_header),
                        forward=sql.CREATE_MTI_SOFT_DELETE_RULE.format(**mti_ident),
                        reverse=sql.DROP_MTI_SOFT_DELETE_RULE.format(
                            child_table=_identifiers._quote_table(table)
                        ),
                    )
                )

            for row in rows:
                self._append_if_stale(
                    operations,
                    row.recorded,
                    row.key,
                    row.header,
                    row.forward,
                    row.reverse,
                    is_adopt=adopt,
                    replace=row.replace,
                    adopt=row.adopt,
                )

            # --- cascade rules for CASCADE FKs pointing at this model (deferred so they
            #     always follow the owner's own soft-delete rule) ---
            if has_column(model, '_deleted_at'):
                deferred.extend(self._cascade_operations(model, adopt=adopt))
            # Owner-side ownership: same table, opposite predicate, and always in the app
            # this loop is already scanning. Outside the guard above so an OwningForeignKey
            # on a model with no `_deleted_at` warns rather than generating nothing.
            deferred.extend(self._owned_operations(model, adopt=adopt))

        # Tenant policies last: they are independent of the triggers and rules above (a
        # policy references neither), so they sort to the end where they read as a group.
        return (
            operations
            + deferred
            # Retire before create: a rename emits both in one migration, and "retire, then
            # create" is the order that reads correctly. The names never collide, so this
            # is legibility rather than correctness.
            + self._retired_autofill_operations(app, adopt=adopt)
            + self._tenant_autofill_operations(app, adopt=adopt)
            + self._tenant_policy_operations(app, adopt=adopt)
        )

    @staticmethod
    def _autofill_slots(table: str, function: str) -> dict[str, str]:
        """The DDL slots every autofill trigger template takes. Derivable from the recorded
        ``(table, function)`` key alone -- no model or column lookup -- which is what lets
        retirement still build a DROP after the column that named it is gone."""
        return {
            'table': _identifiers._quote_table(table),
            'function': _identifiers._safe_ident(function),
            'trigger': _identifiers._safe_ident(autofill_trigger_name(function)),
        }

    def _table_app_labels(self) -> dict[str, str]:
        """``db_table`` -> the local app whose migrations host operations on it, first app
        winning. One table, one host: two apps each emitting the same DROP would fail the
        second at ``migrate``. Shared by retirement and owner-attributed autofill."""
        if self._table_app_labels_cache is not None:
            return self._table_app_labels_cache
        hosting: dict[str, str] = {}
        # Only a model with a ``CreateModel`` behind it can host: a proxy owns no table, and an
        # unmanaged one shadowing another app's would write the DROP into an app with no
        # ordering against the table's creation. Unmanaged still hosts as a fallback.
        for managed in (True, False):
            for app in django_apps.get_app_configs():
                if not _generator.is_local(app):
                    continue
                for model in app.get_models():
                    if model._meta.proxy or bool(model._meta.managed) is not managed:
                        continue
                    hosting.setdefault(model._meta.db_table, app.label)
        self._table_app_labels_cache = hosting
        return hosting

    def _autofill_key_maps(
        self,
    ) -> tuple[dict[tuple[str, str], tuple[str, str]], dict[tuple[str, str], tuple[str, str]]]:
        """``(required, relocated)``, both from **one** sweep of every local app's coverage.
        Relocated is a subset of required, and the sweep is the expensive part -- each
        ``_classify`` of an ancestor-owned column scans the whole model registry."""
        required, relocated = self._required_autofill_cache, self._relocated_autofill_cache
        if required is not None and relocated is not None:
            return required, relocated
        # Bound to the cache slots up front rather than at each return: the loop below fills
        # these same objects, so one assignment covers both exits.
        required, relocated = {}, {}
        self._required_autofill_cache, self._relocated_autofill_cache = required, relocated
        if not self._tenant_policies_enabled():
            return required, relocated
        for app in django_apps.get_app_configs():
            if not _generator.is_local(app):
                continue
            for table, coverage in app_coverage(app).tables.items():
                for dimension, column in (coverage.autofill_columns or {}).items():
                    required[(table, autofill_function_name(dimension, column))] = (
                        dimension,
                        column,
                    )
                # A relocated dimension's trigger lives on the ancestor's table, so it is
                # keyed there -- and several children may resolve to the same one key.
                if not (coverage.owner_autofill_columns and coverage.owner_table):
                    continue
                for dimension, column in coverage.owner_autofill_columns.items():
                    key = (coverage.owner_table, autofill_function_name(dimension, column))
                    required[key] = relocated[key] = (dimension, column)
        return required, relocated

    def _required_autofill_keys(self) -> dict[tuple[str, str], tuple[str, str]]:
        """Every ``(table, function)`` the models currently require -> its ``(dimension,
        column)``. Deliberately project-wide: retirement subtracts from this, and a scoped
        view would read another app's live trigger as no longer required and drop it."""
        return self._autofill_key_maps()[0]

    def _relocated_autofill_keys(self) -> dict[tuple[str, str], tuple[str, str]]:
        """The subset of :meth:`_required_autofill_keys` whose trigger sits on an MTI
        ancestor's table. Project-wide on purpose: the child declaring the dimension may be
        out of a scoped run while the owner hosting the trigger is in it."""
        return self._autofill_key_maps()[1]

    def _scoped_autofill_gap_notes(self, requested: set[str]) -> list[str]:
        """Autofill triggers this scoped run won't touch: a relocated one it won't create
        (keyed off the app hosting the *ancestor's* table) and a stale one it won't retire.
        Closed by a later unscoped run -- the tradeoff cross-app cascade rules already make."""
        if not requested or not self._tenant_policies_enabled():
            return []
        hosting = self._table_app_labels()
        notes = [
            f"Tenant autofill trigger on '{table}' (function '{function}') skipped: the "
            f"tenant column lives on that ancestor, whose app '{hosting[table]}' is not in "
            f'this scoped run.'
            for table, function in sorted(self._relocated_autofill_keys())
            if table in hosting
            and hosting[table] not in requested
            and (table, function) not in self.existing.tenant_autofill
        ]
        # The other direction, and the dangerous half to leave silent: an orphaned trigger
        # dereferences a dropped column and fails *every* INSERT on its table, so a scoped
        # run that cannot retire it has to say which app to name instead.
        required = self._required_autofill_keys()
        notes.extend(
            f"Tenant autofill trigger on '{table}' (function '{function}') is recorded but "
            f"no longer required, and the app hosting that table, '{hosting[table]}', is not "
            f'in this scoped run, so it was not retired. Re-run without a scope, or name '
            f'that app.'
            for table, function in sorted(set(self.existing.tenant_autofill) - set(required))
            if table in hosting and hosting[table] not in requested
        )
        return notes

    def _tenant_autofill_operations(self, app: AppConfig, *, adopt: bool = False) -> list[str]:
        """``BEFORE INSERT`` autofill triggers *app* is missing or has outdated (ADR 0005), on
        *app*'s own tables plus any ancestor table it hosts for a relocated dimension (ADR
        0009). Only where a manager autofills, so an opt-out is auditable as an absent one."""
        if not self._tenant_policies_enabled():
            return []

        # This app's own coverage, not the project-wide required map: _build_operations is
        # called directly with apps outside LOCAL_APPS, which that map excludes. Notes are
        # collected by _tenant_policy_operations off the same call, else each prints twice.
        keys: dict[tuple[str, str], None] = {}
        for table, coverage in app_coverage(app).tables.items():
            for dimension, column in (coverage.autofill_columns or {}).items():
                keys[(table, autofill_function_name(dimension, column))] = None

        # Triggers relocated onto an ancestor's table are attributed to the app hosting that
        # table, wherever the child lives -- the same inversion cascade rules already use.
        hosting = self._table_app_labels()
        for table, function in self._relocated_autofill_keys():
            if hosting.get(table) == app.label:
                keys[(table, function)] = None

        operations: list[str] = []
        for table, function in sorted(keys):
            slots = self._autofill_slots(table, function)
            self._append_if_stale(
                operations,
                self.existing.tenant_autofill,
                # Keyed on the pair, not the table: a table tenanted on two local
                # dimensions carries one trigger per (column, GUC) pair, and the table
                # alone would let the second overwrite the first's recorded digest.
                (table, function),
                HEADER_TENANT_AUTOFILL.format(
                    table=_identifiers._escape_ident(table),
                    function=_identifiers._escape_ident(function),
                ),
                _triggers._CREATE_TENANT_AUTOFILL_TRIGGER.format(**slots),
                _triggers._DROP_TENANT_AUTOFILL_TRIGGER.format(**slots),
                is_adopt=adopt,
                replace=_triggers._REPLACE_TENANT_AUTOFILL_TRIGGER.format(**slots),
                adopt=_triggers._ADOPT_TENANT_AUTOFILL_TRIGGER.format(**slots),
            )
        return operations

    def _retired_autofill_operations(self, app: AppConfig, *, adopt: bool = False) -> list[str]:
        """Drop autofill triggers *app*'s tables record but the models no longer require. A
        renamed dimension or column names a new function, orphaning the old trigger -- which
        still dereferences the dropped column and fails every INSERT on the table."""
        if not self._tenant_policies_enabled():
            return []

        hosting = self._table_app_labels()
        required = self._required_autofill_keys()
        operations: list[str] = []
        for table, function in sorted(set(self.existing.tenant_autofill) - set(required)):
            if hosting.get(table) != app.label:
                continue
            slots = self._autofill_slots(table, function)
            drop = _triggers._DROP_TENANT_AUTOFILL_TRIGGER.format(**slots)
            # Not _append_if_stale: its "recorded digest differs -> replace" branch is
            # meaningless for a drop. The set difference above is the whole idempotency
            # mechanism, so the [SQL:...] stamped here is written and never read.
            source, _ = _operation(
                HEADER_TENANT_AUTOFILL_RETIRED.format(
                    table=_identifiers._escape_ident(table),
                    function=_identifiers._escape_ident(function),
                ),
                drop,
                _triggers._CREATE_TENANT_AUTOFILL_TRIGGER.format(**slots),
                emit=_triggers._ADOPT_DROP_TENANT_AUTOFILL_TRIGGER.format(**slots)
                if adopt
                else drop,
            )
            operations.append(source)
        return operations

    def _unmapped_autofill_notes(self) -> list[str]:
        """Triggers on tables no local model claims: recorded ones that cannot be retired and
        required ones that cannot be created, both for want of an app to write the migration
        into -- this generator has no migration-state graph. Named, because skips are design."""
        if not self._tenant_policies_enabled():
            return []

        hosting = self._table_app_labels()
        required = self._required_autofill_keys()
        notes: list[str] = []
        for table, function in sorted(set(self.existing.tenant_autofill) - set(required)):
            if table in hosting:
                continue
            slots = self._autofill_slots(table, function)
            notes.append(
                f"Tenant autofill trigger on '{table}' (function '{function}') is recorded "
                f'but no local model maps to that table, so it cannot be retired here. If '
                f'the table still exists, drop it by hand: '
                f'{_triggers._DROP_TENANT_AUTOFILL_TRIGGER.format(**slots).strip()}'
            )
        # The other direction: a relocated trigger whose ancestor lives outside LOCAL_APPS.
        # Nothing hosts it, so `audittenancy` would report it missing on every run forever.
        for table, function in sorted(required):
            if table in hosting:
                continue
            notes.append(
                f"Tenant autofill trigger on '{table}' (function '{function}') is required "
                f'but no local model maps to that table -- the tenant column lives on an '
                f'ancestor outside LOCAL_APPS, so there is no app to write the migration '
                f'into. Add that app to LOCAL_APPS, or pass autofill=False on the '
                f'descendants claiming the column.'
            )
        return notes

    def _orphaned_autofill_function_notes(self) -> list[str]:
        """Autofill functions no recorded or required trigger still calls. Inert, so noted
        rather than dropped: DROP FUNCTION must follow every trigger that depends on it, and
        a scoped run cannot prove an out-of-scope app has no trigger left calling it."""
        if not self._tenant_policies_enabled():
            return []

        called = {function for _, function in self.existing.tenant_autofill}
        called.update(function for _, function in self._required_autofill_keys())
        return [
            f"Tenant autofill function '{function}' is no longer called by any trigger this "
            f'command records. It is inert; retire it deliberately once you are sure no '
            f"hand-written trigger uses it -- and note that a retirement migration's "
            f'reverse_sql recreates the trigger calling it, so dropping it makes that '
            f'migration irreversible: '
            f'{_triggers._DROP_TENANT_AUTOFILL_FUNCTION.format(function=_identifiers._safe_ident(function)).strip()}'
            for function in sorted(
                set(self.existing.tenant_autofill_function_dependencies) - called
            )
        ]

    def _rule_cycle_edges(self) -> set[tuple[str, str]]:
        """ON UPDATE rule edges this command may not write, because they lie on a cycle --
        see ``introspection.rule_update_cycle_edges``. Read over the whole registry, not the
        app in scope: scoping narrows what gets written, never which rules exist."""
        if self._rule_cycle_cache is None:
            self._rule_cycle_cache = rule_update_cycle_edges(self.all_models)
        return self._rule_cycle_cache

    def _owner_arms(self) -> dict[str, list[OwnerArm]]:
        """``dependent_table -> every owning column pointing at it``, from the shared sweep.
        Cached rather than re-derived: ``all_models`` is replaced after construction by
        ``isolate_apps``, so the sweep has to be lazy. See ADR 0012."""
        if self._owner_arms_cache is None:
            self._owner_arms_cache = owner_arms(self.all_models)
        return self._owner_arms_cache

    def _owned_tenancy_refusals(self) -> dict[tuple[str, str, str], list[str]]:
        """The other half of that shared answer: which owned rules a tenant policy on a table
        their guard reads makes unsafe to write. Read here to refuse them and by
        ``hard_delete()`` to not follow them -- one sweep, lazy for the same reason."""
        if self._owned_tenancy_cache is None:
            self._owned_tenancy_cache = owned_tenancy_refusals(self.all_models)
        return self._owned_tenancy_cache

    def _record_object_ref(
        self, model: type[models.Model], referenced: type[models.Model], field: str | None = None
    ) -> None:
        """Note that a rule built for *model*'s app names *referenced*. Keyed by the app whose
        migration will carry the operation, which is ``model``'s: ``_build_operations`` is
        called per app and reads only that app's models, so the two cannot come apart."""
        ref = ObjectRef(referenced._meta.app_label, referenced.__name__, field)
        refs = self._object_refs.setdefault(model._meta.app_label, [])
        if ref not in refs:
            refs.append(ref)

    def _refuse_owned(self, key: tuple[str, str, str] | None, message: str) -> None:
        """Record an owned-rule refusal, escalating where a rule for *key* is already recorded:
        refusing emits nothing, so the stale rule stays live and wrong under a green
        ``--check``, and no command retires it. See ADR 0012."""
        self._mti_cascade_warnings.append(message)
        if key is not None and key in self.existing.soft_delete_owned:
            dependent_table, owner_table, foreign_key = key
            self._refusals_over_live_rules.append(
                f"Owned rule on '{dependent_table}' owned by '{owner_table}' via "
                f"'{foreign_key}' is refused but already exists in this project's migrations. "
                'It is still live in any migrated database and no longer correct. Drop it by '
                f'hand: DROP RULE {_owned_rule_name(dependent_table, foreign_key)} ON '
                f'{_identifiers._quote_table(owner_table)};'
            )

    @staticmethod
    def _cycle_warning(kind: str, subject: str, fires_on: str, updates: str) -> str:
        """The shared refusal text for a rule that would close an ON UPDATE cycle -- one
        wording for both kinds, only the subject differing. The two tables are spelled out,
        not joined by an arrow: a cascade rule's subject *is* the table it updates."""
        return (
            f'{kind} rule for {subject} skipped: it fires on '
            f"'{fires_on}' and updates '{updates}', closing a cycle of ON UPDATE rules that "
            'PostgreSQL rejects as infinite rule recursion on every UPDATE to any table in '
            'that cycle -- including a plain save(). Break the cycle by cascading one of its '
            'steps in Python.'
        )

    @staticmethod
    def _is_cascade_candidate(related_model, fk_field, on_delete) -> bool:
        """Whether this reverse relation gets a cascade soft-delete rule. Shared by
        :meth:`_cascade_operations` (writes the rules) and :meth:`_scoped_cascade_gap_notes`
        (reports what a scoped run left out) so the two can't drift apart on which FKs count."""
        return (
            on_delete == models.CASCADE
            and has_column(related_model, '_deleted_at')
            # The MTI parent-link (a CASCADE OneToOne) is structural, not a user cascade FK.
            and not getattr(fk_field.remote_field, 'parent_link', False)
            # An FK reached through MTI is not a second FK: it is the *same physical column*
            # on the ancestor's table, which appears in the caller's loop in its own right.
            and fk_field.model is related_model
        )

    def _cascade_candidates(
        self, model: type[models.Model], owner_table: str
    ) -> list[tuple[type[models.Model], models.ForeignKey, bool]]:
        """CASCADE FKs pointing at *model*, flagged whether each is the *primary* one for
        its related_table -- the first in sorted order, keeping the historical plain form so
        an already-migrated project's lone cascade rule is never re-emitted."""
        seen_related_tables: set[str] = set()
        candidates: list[tuple[type[models.Model], models.ForeignKey, bool]] = []
        for related_model, fk_field, on_delete in sorted(
            self.reverse_relations_mapping[model],
            key=lambda t: (t[0]._meta.db_table, t[1].column),
        ):
            # Structural parent-links and MTI-inherited FKs are excluded there: the MTI
            # redirect rule already ties a child's deletion to the owner, and every table in
            # an MTI chain shares one ``_deleted_at``, so that rule already archives them.
            if not self._is_cascade_candidate(related_model, fk_field, on_delete):
                continue
            related_table = related_model._meta.db_table
            # A rule whose action updates the table it fires on is rewritten into itself, and
            # PostgreSQL then refuses *every* UPDATE there -- a plain save() included -- at
            # rewrite time, so the WHERE guard never runs. A self-referential CASCADE FK.
            if related_table == owner_table:
                self._mti_cascade_warnings.append(
                    f"Cascade rule for '{related_table}' -> '{owner_table}' skipped: the rule "
                    'would update the same table it fires on, which PostgreSQL rejects as '
                    'infinite rule recursion on every UPDATE to that table. A self-referential '
                    'CASCADE foreign key has to be cascaded in Python.'
                )
                continue
            # The same rejection one hop further out: two tables whose rules update each
            # other are rewritten into each other. Checked against the whole-registry graph,
            # so a cycle closed through another app's model is still caught.
            if (owner_table, related_table) in self._rule_cycle_edges():
                self._mti_cascade_warnings.append(
                    self._cycle_warning(
                        'Cascade', f"'{related_table}'", owner_table, related_table
                    )
                )
                continue
            # The flat rule does UPDATE related_table SET _deleted_at -- only valid when the
            # related child owns that column on the table its FK lives on. An FK whose
            # _deleted_at lives on a farther MTI ancestor needs a join form not emitted yet.
            if not owns_column(related_model, '_deleted_at'):
                self._mti_cascade_warnings.append(
                    f"Cascade rule for '{related_table}' -> '{owner_table}' skipped: "
                    f"'{related_model.__name__}' declares this foreign key on its own table "
                    'but inherits _deleted_at from a multi-table-inheritance ancestor, which '
                    'needs a join form the generator does not emit yet.'
                )
                continue
            is_primary = related_table not in seen_related_tables
            seen_related_tables.add(related_table)
            candidates.append((related_model, fk_field, is_primary))
        return candidates

    def _claim_rule_name(self, table: str, rule_name: str, relation: tuple) -> None:
        """Record that *relation* -- ``(other_table, table, foreign_key)``, the column **always**
        filled in and never the operation's dedupe key -- names *rule_name* on *table*, reporting
        a second one resolving to the same pair. See ``docs/migrations.md``'s "Rule names"."""
        claimed = self._claimed_rule_names.setdefault((table, rule_name), relation)
        if claimed != relation:
            self._rule_name_clashes.append(
                f"Rule {rule_name} on '{table}' is named by both "
                f'{_rule_relation_label(claimed)} and {_rule_relation_label(relation)}. '
                'PostgreSQL keeps one rule per name per table, so '
                'the second replaces the first and that relation stops cascading. Rename a '
                'column or a table so the two names differ. Where the shared name carries no '
                'column at all -- one model holding a key to both an MTI parent and its child, '
                'which resolve to one owner table -- renaming cannot help: point both keys at '
                'one level of the chain, or cascade one of the two in Python.'
            )

    def _cascade_operations(self, model: type[models.Model], *, adopt: bool = False) -> list[str]:
        """Cascade soft-delete rules for CASCADE FKs pointing at *model*. Lives on the table
        whose ``_deleted_at`` actually flips: *model*'s own, or the owning MTI ancestor --
        ``ON UPDATE TO child_table`` would never fire, a child's own column is never written."""
        owner = column_owner(model, '_deleted_at')
        owner_table = owner._meta.db_table
        owner_pk = cast(str, owner._meta.pk.column)
        # Loop-invariant: owner_table/owner_pk are fixed for every candidate below, so their
        # quoted/escaped SQL forms and header-escaped form are each computed once rather than
        # once per FK.
        ident_owner_table = _identifiers._quote_table(owner_table)
        ident_owner_pk = _identifiers._escape_ident(owner_pk)
        header_owner_table = _identifiers._escape_ident(owner_table)

        ops: list[str] = []
        for related_model, fk_field, is_primary in self._cascade_candidates(model, owner_table):
            related_table = related_model._meta.db_table
            # SQL body uses _quote_table/_escape_ident throughout; the header's
            # `{table}`/`{related_table}` slots need the same _escape_ident treatment for
            # the same reason -- see _tenant_policy_operation's comment.
            ident_related_table = _identifiers._quote_table(related_table)
            ident_foreign_key = _identifiers._escape_ident(fk_field.column)
            header_related_table = _identifiers._escape_ident(related_table)
            if is_primary:
                key = (related_table, owner_table, None)
                header = HEADER_SOFT_DELETE_RELATED.format(
                    related_table=header_related_table, table=header_owner_table
                )
                rule_name = _related_rule_name(related_table)
            else:
                key = (related_table, owner_table, fk_field.column)
                header = HEADER_SOFT_DELETE_RELATED_VIA.format(
                    related_table=header_related_table,
                    table=header_owner_table,
                    foreign_key=_identifiers._escape_ident(fk_field.column),
                )
                rule_name = _related_rule_name(related_table, fk_field.column)
            # The rule's action names the related table *and* the foreign key on it, so one
            # field ref covers both. Pre-existing gap, not new in 2.4.0: a CASCADE crossing
            # apps has always emitted a rule naming a table nothing ordered it against.
            self._record_object_ref(model, related_model, fk_field.name)
            # ``SET _deleted_at`` is a second column, and not necessarily as old as the table:
            # a model promoted to ``SetarModel`` gains it in a later migration, and an edge to
            # the creation alone would let the rule be created before the column exists.
            self._record_object_ref(model, related_model, '_deleted_at')
            # The relation, not `key`: `key` drops the column on the plain form, and two
            # relations can then share one key -- see `_claim_rule_name`'s docstring.
            self._claim_rule_name(
                owner_table, rule_name, (related_table, owner_table, fk_field.column)
            )
            # One template pair for both cases -- see soft_delete.py's private
            # _CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE for why the public, frozen constants
            # of the same name (the old rule_name-less signature) aren't used here.
            forward = _soft_delete._CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE.format(
                rule_name=rule_name,
                table=ident_owner_table,
                related_table=ident_related_table,
                primary_key=ident_owner_pk,
                foreign_key=ident_foreign_key,
            )
            reverse = _soft_delete._DROP_SOFT_DELETE_RELATED_OBJECTS_RULE.format(
                rule_name=rule_name, table=ident_owner_table
            )
            self._append_if_stale(
                ops,
                self.existing.soft_delete_related,
                key,
                header,
                forward,
                reverse,
                is_adopt=adopt,
            )
        return ops

    @staticmethod
    def _is_owned_candidate(model: type[models.Model], fk_field: models.Field) -> bool:
        """Whether this outbound FK gets an owned soft-delete rule -- the mirror of
        :meth:`_is_cascade_candidate`, read off the declaration rather than ``on_delete``,
        which describes the opposite direction and cannot express ownership."""
        # Spelled out again in ``introspection.owner_arms`` and ``owned_tenancy_refusals``,
        # which cannot call this without losing the ``isinstance`` narrowing ``ty`` reads
        # ``field.column``/``related_model`` through. A clause added here belongs in both.
        return (
            isinstance(fk_field, OwningForeignKey)
            # An FK reached through MTI is the same physical column on the ancestor's table,
            # covered by that ancestor's own pass -- as in _is_cascade_candidate.
            and fk_field.model is model
        )

    @staticmethod
    def _declared_owning_fields(model: type[models.Model]) -> list[models.ForeignKey]:
        """Every ``OwningForeignKey`` *model* declares on its own table, in column order --
        read before any of the refusals below, so a declaration that ends up generating
        nothing can still be named in the warning that says why."""
        return sorted(
            (
                cast('models.ForeignKey', field)
                for field in model._meta.local_fields
                if OperationsMixin._is_owned_candidate(model, field)
            ),
            key=lambda field: cast(str, field.column),
        )

    def _owned_candidates(
        self,
        model: type[models.Model],
        owner_table: str,
        declared: list[models.ForeignKey],
        *,
        report: bool = True,
    ) -> list[models.ForeignKey]:
        """``OwningForeignKey``s declared on *model*, in column order; *declared* is passed in,
        the caller needing the same list first. Skipped with a warning where *model* inherits
        ``_deleted_at``: the rule fires on an ancestor's table ``old."<column>"`` cannot reach."""

        def refuse(key: tuple[str, str, str] | None, message: str) -> None:
            """``report=False`` where the caller asks only *which* relations carry a rule:
            ``_scoped_owned_gap_notes`` re-runs this over apps the run was never asked about,
            whose misconfigurations are not its to report, still less to fail ``--check`` over."""
            if report:
                self._refuse_owned(key, message)

        candidates: list[models.ForeignKey] = []
        for fk_field in declared:
            # A target with no ``_deleted_at`` has nothing to stamp. Warned rather than
            # skipped in silence: unlike a plain CASCADE FK, an OwningForeignKey has no
            # other purpose, so a declaration that generates nothing is a misconfiguration.
            related_model = fk_field.related_model
            if not has_column(related_model, '_deleted_at'):
                # No key to escalate on: the rule's dedupe key names the table holding the
                # target's ``_deleted_at``, and there is none, so no recorded rule can match.
                refuse(
                    None,
                    f"Owned rule for '{model._meta.db_table}.{fk_field.column}' skipped: "
                    f"'{related_model.__name__}' has no _deleted_at column, so "
                    'there is nothing for the rule to stamp. Make the target soft-deletable, '
                    'or declare a plain ForeignKey.',
                )
                continue
            if not owns_column(model, '_deleted_at'):
                # The arrow names the table the rule would *update*, as the cascade warnings'
                # does -- never ``owner_table``, which is where it would fire and has nothing
                # to do with this relation's target. The body names that one instead.
                dependent_table = column_owner(related_model, '_deleted_at')._meta.db_table
                refuse(
                    (dependent_table, owner_table, fk_field.column),
                    f"Owned rule for '{model._meta.db_table}.{fk_field.column}' -> "
                    f"'{dependent_table}' skipped: '{model.__name__}' declares this foreign key "
                    'on its own table but inherits _deleted_at from a multi-table-inheritance '
                    f"ancestor, so the rule would fire on '{owner_table}', a table the column "
                    'is not on.',
                )
                continue
            # ``guitars.E002``'s twin, as ``_rule_update_edges`` is ``E001``'s: the check
            # reports a redirected key, but ``--skip-checks`` still reaches here and the rule
            # would correlate the key against a primary key it never held.
            if not _targets_primary_key(fk_field):
                refuse(
                    (
                        column_owner(related_model, '_deleted_at')._meta.db_table,
                        owner_table,
                        fk_field.column,
                    ),
                    f"Owned rule for '{model._meta.db_table}.{fk_field.column}' skipped: "
                    # ``remote_field.field_name``, not ``target_field.name``: the latter raises
                    # where ``to_field`` names nothing, which is one of the cases refused here.
                    f"to_field='{fk_field.remote_field.field_name}' is not the target's "
                    'primary key, which is what the rule correlates the key against, so it '
                    'would stamp the wrong row. Drop to_field (guitars.E002).',
                )
                continue
            candidates.append(fk_field)
        return candidates

    def _owned_operations(self, model: type[models.Model], *, adopt: bool = False) -> list[str]:
        """Owned soft-delete rules for ``OwningForeignKey``s declared on *model*: the row
        soft-deletes what it owns, unless a sibling owner is still alive. Lives on the same
        table its cascade rules do -- the one whose ``_deleted_at`` actually flips."""
        declared = self._declared_owning_fields(model)
        if not declared:
            return []
        # An owner with no ``_deleted_at`` at all is never soft-deleted, so nothing ever
        # flips to fire the rule. Warned, not passed over: it is the same misconfiguration
        # as a target with none, and the reason ADR 0011 chose a checkable field subclass.
        if not has_column(model, '_deleted_at'):
            for fk_field in declared:
                # No key: the dedupe key's middle term is the table owning the *owner's*
                # ``_deleted_at``, and there is none, so no recorded rule can match.
                self._refuse_owned(
                    None,
                    f"Owned rule for '{model._meta.db_table}.{fk_field.column}' skipped: "
                    f"'{model.__name__}' has no _deleted_at column, so it is never "
                    'soft-deleted and nothing would ever fire the rule. Make the owner '
                    'soft-deletable, or declare a plain ForeignKey.',
                )
            return []
        owner = column_owner(model, '_deleted_at')
        owner_table = owner._meta.db_table
        # Loop-invariant, for the same reason as in _cascade_operations: fixed for every
        # candidate, so the quoted/escaped forms are computed once rather than once per FK.
        ident_owner_table = _identifiers._quote_table(owner_table)
        ident_owner_pk = _identifiers._escape_ident(cast(str, owner._meta.pk.column))
        header_owner_table = _identifiers._escape_ident(owner_table)

        ops: list[str] = []
        for fk_field in self._owned_candidates(model, owner_table, declared):
            # The dependent's own ``_deleted_at`` may live on an MTI ancestor, and
            # correlating against that ancestor's table is still right: every table in a
            # chain shares one primary-key *value*, which is exactly what the FK holds.
            dependent = column_owner(fk_field.related_model, '_deleted_at')
            dependent_table = dependent._meta.db_table
            key = (dependent_table, owner_table, fk_field.column)
            # A rule whose action updates the table it fires on is rewritten into itself, and
            # PostgreSQL then refuses *every* UPDATE there -- a plain save() included -- at
            # rewrite time, so the WHERE guard never gets to run. See docs/owned-relations.md.
            if dependent_table == owner_table:
                self._refuse_owned(
                    key,
                    f"Owned rule for '{owner_table}.{fk_field.column}' -> "
                    f"'{dependent_table}' skipped: the rule would update the same table it "
                    'fires on, which PostgreSQL rejects as infinite rule recursion on every '
                    'UPDATE to that table. Handle this ownership in Python.',
                )
                continue
            # The multi-table form of the same rejection -- see _rule_cycle_edges.
            if (owner_table, dependent_table) in self._rule_cycle_edges():
                self._refuse_owned(
                    key,
                    self._cycle_warning(
                        'Owned',
                        f"'{owner_table}.{fk_field.column}'",
                        owner_table,
                        dependent_table,
                    ),
                )
                continue
            # Before the arms, not after: a refused relation renders no guard, and the lookup
            # would be one more walk of every arm pointing at this dependent for nothing.
            if self._refuse_owned_tenancy_mismatch(key):
                continue
            ident_foreign_key = _identifiers._escape_ident(fk_field.column)
            co_owners = self._co_owner_arms(dependent_table, owner_table, fk_field.column)
            # Everything the action names outside its own table, each ``_deleted_at`` alongside
            # the table holding it: a model promoted to ``SetarModel`` gains that column later
            # than its table, so an edge to the table alone would not order the column.
            self._record_object_ref(model, dependent)
            self._record_object_ref(model, dependent, '_deleted_at')
            for arm in co_owners:
                self._record_object_ref(model, arm.owner_model, arm.fk_name)
                if arm.root_model is not None:
                    self._record_object_ref(model, arm.root_model)
                    self._record_object_ref(model, arm.root_model, '_deleted_at')
                else:
                    self._record_object_ref(model, arm.owner_model, '_deleted_at')
            header = HEADER_SOFT_DELETE_OWNED.format(
                dependent_table=_identifiers._escape_ident(dependent_table),
                table=header_owner_table,
                foreign_key=ident_foreign_key,
            )
            rule_name = _owned_rule_name(dependent_table, fk_field.column)
            # ``key`` *is* the relation here: an owned key always carries its column, so
            # unlike the cascade family above the two cannot come apart.
            self._claim_rule_name(owner_table, rule_name, key)
            forward = _soft_delete._CREATE_SOFT_DELETE_OWNED_OBJECT_RULE.format(
                rule_name=rule_name,
                table=ident_owner_table,
                dependent_table=_identifiers._quote_table(dependent_table),
                dependent_primary_key=_identifiers._escape_ident(
                    cast(str, dependent._meta.pk.column)
                ),
                primary_key=ident_owner_pk,
                foreign_key=ident_foreign_key,
                co_owner_guards=self._owned_co_owner_guards(
                    co_owners,
                    owner_table,
                    ident_owner_pk,
                    ident_foreign_key,
                    dependent_table,
                    _identifiers._escape_ident(cast(str, dependent._meta.pk.column)),
                ),
            )
            reverse = _soft_delete._DROP_SOFT_DELETE_OWNED_OBJECT_RULE.format(
                rule_name=rule_name, table=ident_owner_table
            )
            self._append_if_stale(
                ops,
                self.existing.soft_delete_owned,
                key,
                header,
                forward,
                reverse,
                is_adopt=adopt,
            )
        return ops

    def _co_owner_arms(
        self, dependent_table: str, owner_table: str, fk_column: str
    ) -> list[OwnerArm]:
        """Every *other* owning column pointing at this dependent. Arm 0 -- the rule's own
        column -- is spelled out in the template, which is what keeps a single-owner dependent
        byte-identical to 2.3.0."""
        return [
            arm
            for arm in self._owner_arms().get(dependent_table, ())
            if (arm.owner_table, arm.fk_column) != (owner_table, fk_column)
        ]

    def _refuse_owned_tenancy_mismatch(self, key: tuple[str, str, str]) -> bool:
        """The shared refusal, reported. Only the message is the generator's: the decision has
        to be one answer, or ``hard_delete()`` removes what a refused rule spared."""
        tenanted = self._owned_tenancy_refusals().get(key)
        if not tenanted:
            return False
        dependent_table, owner_table, foreign_key = key
        self._refuse_owned(
            key,
            f"Owned rule for '{owner_table}.{foreign_key}' -> '{dependent_table}' skipped: "
            f'co-owner {"table" if len(tenanted) == 1 else "tables"} '
            f'{", ".join(repr(table) for table in tenanted)} '
            f'{"is" if len(tenanted) == 1 else "are"} tenanted on a dimension the policy on '
            f"'{dependent_table}' does not filter on, so the last-owner guard reads those tables "
            'through a tenant policy and cannot see a live owner in another tenant -- it would '
            "stamp a still-owned row. Keep an owned target inside its owners' tenant "
            'dimension, or leave them all untenanted. See docs/owned-relations.md.',
        )
        return True

    @staticmethod
    def _owned_co_owner_guards(
        co_owners: list[OwnerArm],
        owner_table: str,
        ident_owner_pk: str,
        ident_declared_foreign_key: str,
        dependent_table: str,
        ident_dependent_pk: str,
    ) -> str:
        """The rendered co-owner arms, or ``''`` where the dependent is owned from exactly one
        place -- which is what makes that case byte-identical to 2.3.0. Aliases are numbered
        from 1, arm 0 keeping the literal ``guitars_owner`` the template spells out."""
        arms: list[str] = []
        for position, arm in enumerate(co_owners, start=1):
            alias = f'guitars_owner_{position}'
            # Against the table liveness is read from, and its alias: a joined arm matches one
            # row per *ancestor* row, so excluding on the table holding the key would leave the
            # row the statement is about counting as an owner of what it owns.
            excluded = arm.liveness_table()
            excluded_alias = alias if arm.root_table is None else f'{alias}_root'
            if excluded == owner_table:
                # Per *row*, not per column: the row being soft-deleted must not count as its
                # own live co-owner, or one owning the target through two of its columns holds
                # the target alive forever.
                self_exclusion = _soft_delete._SOFT_DELETE_OWNED_CO_OWNER_SELF_EXCLUSION.format(
                    alias=excluded_alias, primary_key=ident_owner_pk
                )
            elif excluded == dependent_table:
                # An arm taking liveness from the dependent's own table -- a target owning
                # itself, or an MTI child of it owning it back. The row the rule stamps must not
                # count as its own live owner, or nothing archives it. Named by the key.
                self_exclusion = _soft_delete._SOFT_DELETE_OWNED_CO_OWNER_TARGET_EXCLUSION.format(
                    alias=excluded_alias,
                    primary_key=ident_dependent_pk,
                    foreign_key=ident_declared_foreign_key,
                )
            else:
                # No row on any other table is going away in this statement.
                self_exclusion = ''
            shared = {
                'owner_table': _identifiers._quote_table(arm.owner_table),
                'alias': alias,
                'foreign_key': _identifiers._escape_ident(arm.fk_column),
                'declared_foreign_key': ident_declared_foreign_key,
                'self_exclusion': self_exclusion,
            }
            if arm.root_table is None:
                arms.append(_soft_delete._SOFT_DELETE_OWNED_CO_OWNER_GUARD.format(**shared))
                continue
            arms.append(
                _soft_delete._SOFT_DELETE_OWNED_CO_OWNER_JOINED_GUARD.format(
                    root_table=_identifiers._quote_table(arm.root_table),
                    root_primary_key=_identifiers._escape_ident(cast(str, arm.root_pk)),
                    child_primary_key=_identifiers._escape_ident(cast(str, arm.child_pk)),
                    **shared,
                )
            )
        return ''.join(arms)

    def _scoped_owned_gap_notes(self, requested: set[str]) -> list[str]:
        """Owned rules this scoped run leaves stale: their guards read the whole registry, so a
        model in an in-scope app moves the rule text of one that is not. Warned like its
        cascade twin, not escalated -- an unscoped run, which is what CI runs, re-derives it."""
        if not requested:
            return []

        in_scope_tables = {
            model._meta.db_table
            for app in django_apps.get_app_configs()
            if _generator.is_local(app) and app.label in requested
            for model in app.get_models()
        }
        if not in_scope_tables:
            return []

        notes: list[str] = []
        for app in django_apps.get_app_configs():
            if not _generator.is_local(app) or app.label in requested:
                continue
            for model in app.get_models():
                if not owns_column(model, '_deleted_at'):
                    continue
                owner_table = model._meta.db_table
                for fk_field in self._owned_candidates(
                    model, owner_table, self._declared_owning_fields(model), report=False
                ):
                    dependent = column_owner(fk_field.related_model, '_deleted_at')
                    dependent_table = dependent._meta.db_table
                    co_owners = self._co_owner_arms(dependent_table, owner_table, fk_field.column)
                    # The refusals ``_owned_operations`` applies after the candidate test. A
                    # relation refused there has no rule for an arm to make stale, so a note
                    # about it would ask for an unscoped run that emits the same note again.
                    if (
                        dependent_table == owner_table
                        or (owner_table, dependent_table) in self._rule_cycle_edges()
                        or (dependent_table, owner_table, fk_field.column)
                        in self._owned_tenancy_refusals()
                    ):
                        continue
                    # Every table the arm names, a joined one naming two: either moving puts
                    # this rule's text out of date, and neither is re-derived by this run.
                    touching = sorted(
                        {
                            table
                            for arm in co_owners
                            for table, _model in arm.reads()
                            if table in in_scope_tables
                        }
                    )
                    if not touching:
                        continue
                    notes.append(
                        f"Owned rule on '{dependent_table}' owned by '{owner_table}' via "
                        f"'{fk_field.column}' may be stale: its last-owner guard reads "
                        f"{', '.join(repr(table) for table in touching)}, in this run's "
                        f"scope, but app '{app.label}' is not. Re-run "
                        '`makeguitarmigrations` without app labels.'
                    )
        return notes

    def _scoped_cascade_gap_notes(self, requested: set[str]) -> list[str]:
        """Describe cross-app CASCADE soft-delete rules this scoped run won't create -- keyed
        off the *parent* app (holding ``_deleted_at``), so scoping the parent out skips it.
        The accepted "pragmatic scope" tradeoff; closed by a later, unscoped run."""
        if not requested:
            return []

        model_app_label = {
            model: app.label
            for app in django_apps.get_app_configs()
            if _generator.is_local(app)
            for model in app.get_models()
        }

        notes: list[str] = []
        for app in django_apps.get_app_configs():
            if not _generator.is_local(app) or app.label in requested:
                continue
            for model in app.get_models():
                if not has_column(model, '_deleted_at'):
                    continue
                # The rule lives on the table that owns _deleted_at (the model itself, or its
                # MTI ancestor), matching where `_cascade_operations` places it.
                table = column_owner(model, '_deleted_at')._meta.db_table
                # Shared with _cascade_operations, which is what makes "closed by a later run
                # naming the parent's app" a promise this check can actually verify: the two
                # must agree on both which FKs count and which dedupe key each one uses.
                for related_model, fk_field, is_primary in self._cascade_candidates(model, table):
                    if model_app_label.get(related_model) not in requested:
                        continue
                    related_table = related_model._meta.db_table
                    key = (related_table, table, None if is_primary else fk_field.column)
                    if key in self.existing.soft_delete_related:
                        continue
                    notes.append(
                        f"Cascade rule on '{related_table}' related to '{table}' skipped: "
                        f"parent app '{app.label}' is not in this scoped run."
                    )
        return notes

    def _dependencies_for(self, app: AppConfig, operations_blob: str) -> list[tuple[str, str]]:
        """Every edge *app*'s new migration needs: the shared-function ones read off the
        operation headers, plus one per object the rules name in another app. Both, in that
        order, so a file's dependency list reads the same as it did before 2.5.0 plus."""
        return self._function_dependencies_for(operations_blob) + self._object_dependencies_for(
            app
        )

    def _object_dependencies_for(self, app: AppConfig) -> list[tuple[str, str]]:
        """Edges to the migrations that create what *app*'s rules reference. A rule's action is
        parsed by PostgreSQL at ``CREATE`` time, so a cross-app table or column it names must
        already exist -- and only an explicit dependency orders that. See ADR 0013."""
        # Own-app refs are filtered before the loader is built, not inside ``resolve_dependencies``
        # alone: building one imports every migration module in the project, and a single-app
        # project -- every consumer before 2.5.0 -- has nothing else for it to answer.
        refs = [ref for ref in self._object_refs.get(app.label, []) if ref.app_label != app.label]
        if not refs:
            return []
        # Loaded here rather than in the constructor: the scaffold for *this* app was written
        # moments ago by ``create_empty_migration_file``, and the graph has to include it for
        # the cycle question below to be asked about the file actually being written.
        loader = MigrationLoader(None, ignore_no_migrations=True)
        edges, unresolved = resolve_dependencies(loader, refs, own_app=app.label)
        for ref in unresolved:
            # Warned, not refused: an app with no migrations at all is a legitimate
            # configuration, and withdrawing a rule that works today would be the worse trade.
            self._mti_cascade_warnings.append(
                f"Enforcement migration for '{app.label}' references '{ref.describe()}', but no "
                'migration in that app creates it, so no dependency edge was emitted. A fresh '
                '`migrate` may reach the rule first. Add the edge by hand if that app is '
                'migrated elsewhere.'
            )
        return [edge for edge in edges if not self._refuse_cyclic_edge(app, edge, loader)]

    def _missing_edge_notes(self, app: AppConfig) -> list[str]:
        """Enforcement migrations of *app* that name another app's table without being ordered
        against whatever creates it. Reachability, not a literal edge: an ordering already
        guaranteed through another path is guaranteed, and flagging it would be a false alarm."""
        # Cross-app refs only, and filtered before the loader is built for the reason
        # ``_object_dependencies_for`` gives -- a single-app project builds none.
        refs = [ref for ref in self._object_refs.get(app.label, []) if ref.app_label != app.label]
        if not refs:
            return []
        loader = MigrationLoader(None, ignore_no_migrations=True)
        notes: list[str] = []
        for ref in refs:
            edge = resolve_object_migration(loader, ref)
            table = self._ref_table(ref)
            if edge is None or table is None:
                continue
            column = self._ref_column(ref)
            for path, content in _generator.iter_migration_files(app):
                # Only a migration whose SQL actually names the table: refs are collected per
                # app, and an app's earlier enforcement migration may predate the rule needing
                # this one. ``_quote_table`` always quotes, so membership is exact.
                node = (app.label, path.stem)
                if (
                    not _generator.RE_DIGEST.search(content)
                    or f'"{table}"' not in content
                    # ...and the column, where the ref names one: two refs can share a table
                    # and resolve to *different* migrations, and matching the table alone then
                    # reports the older file forever. Unquoted -- the bare name is in both.
                    or (column is not None and column not in content)
                    or node not in loader.graph.node_map
                    or edge in set(loader.graph.forwards_plan(node))
                ):
                    continue
                note = (
                    f"Enforcement migration '{app.label}.{path.stem}' creates a rule naming "
                    f"'{table}', but nothing orders it after '{edge[0]}.{edge[1]}', which "
                    'creates that table -- a fresh `migrate` can reach the rule first and fail '
                    f'with `relation "{table}" does not exist`. Add to its dependencies:\n'
                    f"        ('{edge[0]}', '{edge[1]}'),"
                )
                # Deduped: several refs into one app resolve to one migration -- a table and
                # the ``_deleted_at`` on it -- and the note names the file and the edge only.
                if note not in notes:
                    notes.append(note)
        return notes

    @staticmethod
    def _ref_table(ref: ObjectRef) -> str | None:
        """The physical table behind *ref*, or ``None`` where the model is gone -- a migration
        older than a deleted model still mentions its table, and that is not this check's
        business. Read off the registry, the same place the refs themselves came from."""
        try:
            return django_apps.get_model(ref.app_label, ref.model)._meta.db_table
        except LookupError:
            return None

    @staticmethod
    def _ref_column(ref: ObjectRef) -> str | None:
        """The physical column behind *ref*, or ``None`` for a ref naming only a table -- and
        for one whose field the registry no longer has, the same "not this check's business"
        answer :meth:`_ref_table` gives. Read off the registry, like the refs themselves."""
        if ref.field is None:
            return None
        try:
            model = django_apps.get_model(ref.app_label, ref.model)
        except LookupError:
            return None
        try:
            return cast('str', model._meta.get_field(ref.field).column)
        except FieldDoesNotExist:
            return None

    def _refuse_cyclic_edge(self, app: AppConfig, edge: tuple[str, str], loader) -> bool:
        """Whether *edge* has to be dropped for closing a cycle. It should never be: an edge to
        the migration that *creates* an object is older than the rule naming it. Asked anyway --
        a graph Django rejects bricks ``migrate`` outright, worse than what the edge prevents."""
        leaves = sorted(loader.graph.leaf_nodes(app.label))
        # The app's newest leaf stands in for the file being written: normally the scaffold
        # itself, the loader having been built after it was written, and otherwise the leaf the
        # scaffold depends on -- anything reaching which reaches the new file too.
        if not leaves or not would_close_a_cycle(loader, leaves[-1], edge):
            return False
        self._mti_cascade_warnings.append(
            f"Enforcement migration for '{app.label}' needs '{edge[0]}.{edge[1]}' to run first, "
            f"but '{edge[0]}' already depends on '{app.label}', so the edge would make the "
            'migration graph cyclic and Django would refuse every `migrate`. Emitted without '
            f"it -- migrate '{edge[0]}' before '{app.label}', or split the two apps' models."
        )
        return True

    def _function_dependencies_for(self, operations_blob: str) -> list[tuple[str, str]]:
        """Function-migration dependencies an app's operations actually require -- keyed off
        the operation headers, since only ``updated_at`` and autofill triggers call a shared
        function, so an app never depends on a migration (or its ordering) it doesn't use."""
        deps: list[tuple[str, str]] = []
        if self.trigger_function_dependency and _RE_UPDATED_AT.search(operations_blob):
            deps.append(self.trigger_function_dependency)
        if self.parent_trigger_function_dependency and _RE_MTI_UPDATED_AT.search(operations_blob):
            deps.append(self.parent_trigger_function_dependency)
        # Per function, not per kind: an app depends only on the autofill functions its own
        # triggers name. The retired header counts too -- its reverse_sql recreates the
        # trigger, so this edge is what makes reversing a retirement possible.
        for pattern in (_RE_TENANT_AUTOFILL, _RE_TENANT_AUTOFILL_RETIRED):
            for match in pattern.finditer(operations_blob):
                dependency = self.tenant_autofill_dependencies.get(
                    _identifiers._unescape_ident(match.group(RE_TENANT_AUTOFILL_FUNCTION))
                )
                if dependency and dependency not in deps:
                    deps.append(dependency)
        return deps

    def _generate_stage(
        self,
        requested: set[str],
        *,
        migration_name: str,
        build_ops: Callable[[AppConfig], list[str]],
        check_only: bool,
        dependencies_for: Callable[[AppConfig, str], list[tuple[str, str]]] | None = None,
        adopt: bool = False,
    ) -> tuple[bool, list[tuple[str, list[str]]]]:
        """Scaffold-and-write one migration per in-scope app with new operations. Shared by
        ``handle()`` and :meth:`_handle_force_rls_stage`, once near-identical copies. Returns
        ``(changes_made, check_missing)``: callers flush different warnings. See *adopt* below."""
        changes_made = False
        check_missing: list[tuple[str, list[str]]] = []
        for app in django_apps.get_app_configs():
            if not _generator.is_in_scope(app, requested):
                continue

            operations = build_ops(app)
            # Before the early exits below: an app whose operations are all already recorded
            # emits nothing, and a missing edge on one of *those* is exactly what needs saying.
            self._missing_edges.extend(self._missing_edge_notes(app))
            if not operations:
                continue

            operations_digest = _generator.digest_of(operations)
            # Retirement makes an operation set recur (retire, re-adopt, same CREATE), so the
            # guard must yield -- but never under *adopt*, where it is the only idempotency
            # there is. Safe: the adopt form's DROP ... IF EXISTS digests differently.
            waive_digest_guard = not adopt and app.label in self.existing.autofill_retirement_apps
            if not waive_digest_guard and operations_digest in self.existing.existing_digests.get(
                app.label, set()
            ):
                continue

            if check_only:
                check_missing.append((app.label, operations))
                continue

            migration_file = _generator.create_empty_migration_file(app, migration_name)
            dependencies = (
                dependencies_for(app, '\n'.join(operations)) if dependencies_for else None
            )
            self._write_migration_file(
                app=app,
                migration_file=migration_file,
                operations=operations,
                operations_digest=operations_digest,
                dependencies=dependencies,
            )

            self.stdout.write(
                self.style.MIGRATE_HEADING(f"Enforcement migrations for '{app.label}':")
            )
            self.stdout.write(f'  migrations/{migration_file}')
            changes_made = True

        return changes_made, check_missing
