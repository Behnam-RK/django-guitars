"""Building enforcement operations for an app's models: models declare an
``_updated_at``/``_deleted_at`` column or a ``tenanted_manager()``; this turns that into
``RunSQL`` snippets, diffed against what ``scanning.scan_existing_operations`` found."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, cast

from django.apps import apps as django_apps
from django.db import models

from guitars import sql
from guitars.introspection import column_owner, has_column, is_mti_child, owns_column
from guitars.management import _generator
from guitars.management.enforcement.headers import (
    _RE_MTI_UPDATED_AT,
    _RE_TENANT_AUTOFILL,
    _RE_TENANT_AUTOFILL_RETIRED,
    _RE_UPDATED_AT,
    HEADER_MTI_SOFT_DELETE,
    HEADER_MTI_UPDATED_AT,
    HEADER_SOFT_DELETE,
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
from guitars.sql import _identifiers
from guitars.sql import soft_delete as _soft_delete
from guitars.sql import triggers as _triggers
from guitars.tenancy.discovery import app_coverage, autofill_function_name, autofill_trigger_name


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


def _related_rule_name(related_table: str, foreign_key: str | None = None) -> str:
    """The cascade rule's identifier, NAMEDATALEN-truncated before quoting. Schema folded in
    **length-prefixed**, not underscore-joined -- plain ``f'{schema}_{table}'`` would let
    ``('tenant_a', 'events')`` and ``('tenant', 'a_events')`` collide on one name."""
    schema, bare_related_table = _identifiers._split_qualified('table', related_table)
    name = (
        f'soft_delete_related_{bare_related_table}'
        if schema is None
        else f'soft_delete_related_{len(schema)}_{schema}_{bare_related_table}'
    )
    if foreign_key is not None:
        name = f'{name}_{foreign_key}'
    return _identifiers._safe_ident(name)


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
        trigger_function_dependency: tuple[str, str] | None
        parent_trigger_function_dependency: tuple[str, str] | None
        tenant_autofill_dependencies: dict[str, tuple[str, str]]
        reverse_relations_mapping: dict[type[models.Model], set]
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
        for app in django_apps.get_app_configs():
            if not _generator.is_local(app):
                continue
            for model in app.get_models():
                # Proxies come through ``get_models`` but own no table. Letting one declared
                # in an earlier app claim the host would write the DROP (or a relocated
                # CREATE) into an app with no ordering against the migration that created it.
                if model._meta.proxy:
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
            f'hand-written trigger uses it: '
            f'{_triggers._DROP_TENANT_AUTOFILL_FUNCTION.format(function=_identifiers._safe_ident(function)).strip()}'
            for function in sorted(
                set(self.existing.tenant_autofill_function_dependencies) - called
            )
        ]

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
        dependencies_for: Callable[[str], list[tuple[str, str]]] | None = None,
    ) -> tuple[bool, list[tuple[str, list[str]]]]:
        """Scaffold-and-write one migration per in-scope app with new operations. Shared by
        ``handle()`` and :meth:`_handle_force_rls_stage`, once near-identical copies.
        Returns ``(changes_made, check_missing)`` since callers flush different warnings."""
        changes_made = False
        check_missing: list[tuple[str, list[str]]] = []
        for app in django_apps.get_app_configs():
            if not _generator.is_in_scope(app, requested):
                continue

            operations = build_ops(app)
            if not operations:
                continue

            operations_digest = _generator.digest_of(operations)
            # Retirement breaks the file-digest guard's assumption that an operation set never
            # recurs: retire an autofill trigger, re-adopt it, and the CREATE is byte-identical
            # to an earlier migration's. Past one, the exact per-operation guards stand alone.
            if (
                app.label not in self.existing.autofill_retirement_apps
                and operations_digest in self.existing.existing_digests.get(app.label, set())
            ):
                continue

            if check_only:
                check_missing.append((app.label, operations))
                continue

            migration_file = _generator.create_empty_migration_file(app, migration_name)
            dependencies = dependencies_for('\n'.join(operations)) if dependencies_for else None
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
