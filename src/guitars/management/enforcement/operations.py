"""Building enforcement operations for an app's models: models declare an
``_updated_at``/``_deleted_at`` column or a ``tenanted_manager()``; this turns that into
``RunSQL`` snippets, diffed against what ``scanning.scan_existing_operations`` found."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from django.apps import apps as django_apps
from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.migrations.loader import MigrationLoader

from guitars import sql
from guitars.checks import refuses_soft_delete_rule
from guitars.introspection import (
    CascadeKind,
    OwnerArm,
    classify_cascade,
    column_owner,
    has_column,
    is_mti_child,
    joined_refusal,
    owned_tenancy_refusals,
    owner_arms,
    owns_column,
    rule_update_cycle_edges,
)
from guitars.management import _generator
from guitars.management.enforcement.graph import (
    ObjectRef,
    drop_implied_edges,
    dropped_tables,
    resolve_dependencies,
    resolve_object_migration,
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
    HEADER_SOFT_DELETE_OWNED_RETIRED,
    HEADER_SOFT_DELETE_OWNED_SWEEP,
    HEADER_SOFT_DELETE_OWNED_SWEEP_RETIRED,
    HEADER_SOFT_DELETE_RELATED,
    HEADER_SOFT_DELETE_RELATED_RETIRED,
    HEADER_SOFT_DELETE_RELATED_VIA,
    HEADER_SOFT_DELETE_RELATED_VIA_RETIRED,
    HEADER_SOFT_DELETE_REVIVE_OWNER,
    HEADER_SOFT_DELETE_REVIVE_OWNER_RETIRED,
    HEADER_SOFT_DELETE_REVIVE_RETIRED,
    HEADER_SOFT_DELETE_REVIVE_VIA_RETIRED,
    HEADER_SOFT_DELETE_SELF_CASCADE,
    HEADER_SOFT_DELETE_SELF_CASCADE_RETIRED,
    HEADER_TENANT_AUTOFILL,
    HEADER_TENANT_AUTOFILL_RETIRED,
    HEADER_TENANT_FORCE,
    HEADER_TENANT_POLICY,
    HEADER_TENANT_POLICY_REPLACED,
    HEADER_UPDATED_AT,
    RE_TENANT_AUTOFILL_FUNCTION,
)
from guitars.management.enforcement.identity import _literal, _operation, _sql_digest
from guitars.models.fields import OwningForeignKey, _targets_primary_key
from guitars.routing import migrates_to_postgresql, vendor_skip_note
from guitars.sql import _identifiers
from guitars.sql import policy as _policy
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

    from guitars.management.enforcement.scanning import CascadeRetirementSite, ExistingOperations
    from guitars.tenancy.discovery import TableCoverage


class _RetiredFamily(NamedTuple):
    """One rule family the retirement loop drops: what it recorded, what created it, and the
    four templates that spell its drop, its reverse and its two header forms."""

    recorded: dict[tuple[str, str, str | None], str | None]
    creates: dict[tuple[str, str, str | None], list[tuple[str, str]]]
    #: ``(owner_table, related_table, foreign_key) -> object name``. The owner is in the
    #: signature for the inverse family, whose function is namespaced per schema; the cascade
    #: family's rule is per table and ignores it.
    name: Callable[[str, str, str | None], str]
    #: ``(name, ident_owner_table) -> the slots its templates take``. The two families spell
    #: different objects -- one rule, one function and trigger -- so neither the drop nor the
    #: prior-name drop can share a slot dict.
    slots: Callable[[str, str], dict]
    drop_template: str
    create_template: str
    #: ``(ident_owner_table, names) -> DROP ... IF EXISTS`` over every spelling a rename left.
    drop_prior: Callable[[str, list[str]], str]
    header: str
    via_header: str


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


def _is_joined(model) -> bool:
    """A cascade key declared on *model* whose ``_deleted_at`` lives on an ancestor. A model with
    no such column at all is not joined: it is a flat key that stopped being one."""
    return has_column(model, '_deleted_at') and not owns_column(model, '_deleted_at')


def _parent_link(model, ancestor) -> models.Field:
    """*model*'s link to *ancestor*. Not its primary key: a descendant can declare an explicit one
    beside ``parent_link=True``, and a second concrete parent has a link of its own."""
    link = model._meta.get_ancestor_link(ancestor)
    if link is None:  # pragma: no cover - the caller took *ancestor* from column_owner
        raise ValueError(f'{model.__name__} has no link to {ancestor.__name__}')
    return link


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


def _revive_name(owner_table: str, related_table: str, foreign_key: str | None = None) -> str:
    """The inverse family's identifier, for both its function and its trigger. Sized like
    :func:`_owned_sweep_name` and over the same segments, and for its reason: a trigger is
    namespaced per table but a function per schema, so the owner it fires on must be spelled."""
    # A literal ``via`` splits the two forms: both of the *other* segments are optional -- the
    # schemas -- and sizing alone left ``('myapp.x', None)`` meeting ``('myapp', 'x')``. A sized
    # segment always opens with a digit, so no table or schema can be read as the marker.
    owner_schema, bare_owner = _identifiers._split_qualified('table', owner_table)
    schema, bare_table = _identifiers._split_qualified('table', related_table)
    parts = [
        'soft_delete_revive' if foreign_key is None else 'soft_delete_revive_via',
        *([] if owner_schema is None else [_sized(owner_schema)]),
        _sized(bare_owner),
        *([] if schema is None else [_sized(schema)]),
        _sized(bare_table),
        *([] if foreign_key is None else [_sized(foreign_key)]),
    ]
    return _identifiers._safe_ident('_'.join(parts))


def _revive_owner_name(owner_table: str) -> str:
    """The per-owner revive's identifier (2.16.0, #70), for its function and its trigger. The
    literal ``on`` keeps it apart from :func:`_revive_name`, whose next segment is sized and so
    opens with a digit; the owner spelled whole, schema included, for that function's reason."""
    owner_schema, bare_owner = _identifiers._split_qualified('table', owner_table)
    parts = [
        'soft_delete_revive_on',
        *([] if owner_schema is None else [_sized(owner_schema)]),
        _sized(bare_owner),
    ]
    return _identifiers._safe_ident('_'.join(parts))


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


def _owned_sweep_name(owner_table: str, dependent_table: str, foreign_key: str) -> str:
    """The owned sweep's identifier, for both its function and its trigger. Sized like
    :func:`_owned_rule_name` over one segment more: a rule is namespaced per table, a function
    per schema, and two owner tables share a ``(dependent, fk)`` pair -- see the comment."""
    # ``Kiosk`` and ``Foyer`` both own ``Placard`` through ``placard_id``. Under the rule's
    # spelling the second CREATE FUNCTION replaces the first's body with a predicate reading
    # the wrong owner table. Both schemas folded in for the reason the stem folds one.
    owner_schema, bare_owner = _identifiers._split_qualified('table', owner_table)
    schema, bare_table = _identifiers._split_qualified('table', dependent_table)
    parts = [
        'soft_delete_owned_sweep',
        *([] if owner_schema is None else [_sized(owner_schema)]),
        _sized(bare_owner),
        *([] if schema is None else [_sized(schema)]),
        _sized(bare_table),
        _sized(foreign_key),
    ]
    return _identifiers._safe_ident('_'.join(parts))


def _self_cascade_name(table: str, foreign_key: str) -> str:
    """The self-cascade trigger's identifier, for both its trigger and its function. Sized
    like :func:`_owned_rule_name`, and over the same segments: the table the key points at is
    the table the trigger fires on, so unlike the sweep there is no second table to fold in."""
    # Sized for :func:`_owned_rule_name`'s reason, not the frozen cascade family's: nothing
    # predates 2.8.0 here either, so the name was free to be built with no boundary to guess
    # at. The distinct prefix is what keeps it from meeting any of the other three families.
    schema, bare_table = _identifiers._split_qualified('table', table)
    parts = [
        'soft_delete_self_cascade',
        *([] if schema is None else [_sized(schema)]),
        _sized(bare_table),
        _sized(foreign_key),
    ]
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
        _vendor_skip_notes: list[str]
        _skipped_rule_notes: list[str]
        _rule_name_clashes: list[str]
        _claimed_rule_names: dict[tuple[str, str], tuple]
        #: Keyed on the name alone, unlike the above: a sweep's function is namespaced per
        #: schema, so two owner tables can collide on one where their rules cannot.
        _claimed_sweep_names: dict[str, tuple]
        trigger_function_dependency: tuple[str, str] | None
        parent_trigger_function_dependency: tuple[str, str] | None
        tenant_autofill_dependencies: dict[str, tuple[str, str]]
        reverse_relations_mapping: dict[type[models.Model], set]
        all_models: list[type[models.Model]]
        _rule_cycle_cache: set[tuple[str, str]] | None
        _owner_arms_cache: dict[str, list[OwnerArm]] | None
        _owned_tenancy_cache: dict[tuple[str, str, str], list[str]] | None
        _object_refs: dict[str, list[ObjectRef]]
        _retirement_edges: dict[str, list[tuple[str, str]]]
        _loader_cache: MigrationLoader | None
        _dropped_tables_cache: tuple[MigrationLoader, dict[str, tuple[str, str]]] | None
        _refusals_over_live_rules: list[str]
        _missing_edges: list[str]
        _unresolved_reference_notes: list[str]
        _table_app_labels_cache: dict[str, str] | None
        _routed_away_cache: frozenset[str] | None
        _cascade_key_maps_cache: (
            tuple[dict[tuple[str, str, str | None], str], dict[str, type[models.Model]]] | None
        )
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

    def _record_policy_object_refs(self, app: AppConfig, coverage: TableCoverage) -> None:
        """Note the objects a tenant policy's owner join names. ``sql.policy._owner_exists``
        reads the MTI ancestor's table and each tenant column on it, both resolved as
        ``CREATE POLICY`` is parsed, and that ancestor routinely lives in another app."""
        if coverage.owner_model is None:
            return
        self._record_app_object_ref(app.label, coverage.owner_model)
        for field in coverage.owner_fields or ():
            self._record_app_object_ref(app.label, coverage.owner_model, field)

    def _tenant_policy_operations(self, app: AppConfig, *, adopt: bool = False) -> list[str]:
        """Tenant-policy create/replace operations *app* is missing or has outdated."""
        if not self._tenant_policies_enabled():
            return []

        coverage = app_coverage(app)
        self._tenancy_notes.extend(coverage.notes)

        operations: list[str] = []
        for table, table_coverage in sorted(coverage.tables.items()):
            self._record_policy_object_refs(app, table_coverage)
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
            # A proxy owns no table, so every operation it earns is one its concrete model
            # already has -- keyed on the same ``db_table``, so it collides rather than adds.
            # Filtered as ``_table_app_labels`` filters it, and as the tenancy walk does.
            if model._meta.proxy:
                continue
            # Every family below is PostgreSQL DDL, and ``migrate`` asks the router about
            # each ``RunSQL`` it applies -- so a model the router sends elsewhere earns
            # operations its own backend's parser refuses. Asked once, for all seven.
            if not migrates_to_postgresql(model):
                self._vendor_skip_notes.append(vendor_skip_note(model))
                continue
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

            # Asked of the whole chain above, so a *descendant* of a refused model is refused
            # with it: it meets no plain parent itself, and the redirect rule below is ``DO
            # INSTEAD`` -- the same row-keeping, one table further down, dangling at COMMIT.
            orphan_ancestors = refuses_soft_delete_rule(model)
            if orphan_ancestors:
                # Re-asked here rather than trusted from ``guitars.E003``: ``--skip-checks``
                # reaches the generator, and emitting the rule anyway is what makes the shape
                # abort at COMMIT -- the child's row is kept while the ancestor's is removed.
                for owner, parent in orphan_ancestors:
                    self._skipped_rule_notes.append(
                        f"Soft delete rule on '{table}' skipped: '{owner.__name__}' carries "
                        f'_deleted_at while its multi-table-inheritance ancestor '
                        f"'{parent._meta.db_table}' declares none: a rule would keep this row "
                        f"while the ancestor's unguarded DELETE removes the row it points at, "
                        f'aborting at COMMIT, and without one a delete destroys the chain. See '
                        f'guitars.E003 for the fix.'
                    )
            elif owns_column(model, '_deleted_at'):
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
                # The redirect rule's action names the *ancestor's* table and ``_deleted_at``,
                # both resolved as PostgreSQL parses it, so a chain crossing apps needs the same
                # edges. Unlike the trigger above: its parent table is a literal, quoted to fire.
                ancestor = column_owner(model, '_deleted_at')
                self._record_object_ref(model, ancestor)
                self._record_object_ref(model, ancestor, '_deleted_at')
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
            + self._retired_cascade_operations(app, adopt=adopt)
            # After the per-key revives that retirement drops: the names never collide, so two
            # triggers reviving one row for a statement would be harmless, but it reads right.
            + self._revive_operations(app, adopt=adopt)
            + self._retired_autofill_operations(app, adopt=adopt)
            + self._retired_trigger_operations(app)
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
                    # A routed-away table maps to nothing, which is what withholds the
                    # retirement: ``_retired_cascade_operations`` drops only on positive
                    # evidence, and "maps to no local model" is the scoped-run reading too.
                    if not migrates_to_postgresql(model):
                        continue
                    hosting.setdefault(model._meta.db_table, app.label)
        self._table_app_labels_cache = hosting
        return hosting

    def _routed_away_tables(self) -> frozenset[str]:
        """Every local table the router migrates off PostgreSQL. Read by the note families
        that compare recorded coverage against required: without it a routed-away model's
        own migration reads as abandoned and each would advise dropping it by hand."""
        if self._routed_away_cache is not None:
            return self._routed_away_cache
        tables = {
            model._meta.db_table
            for app in django_apps.get_app_configs()
            if _generator.is_local(app)
            for model in app.get_models()
            if not model._meta.proxy and not migrates_to_postgresql(model)
        }
        self._routed_away_cache = frozenset(tables)
        return self._routed_away_cache

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

    def _cascade_key_maps(
        self,
    ) -> tuple[dict[tuple[str, str, str | None], str], dict[str, type[models.Model]]]:
        """``(required cascade keys -> the FK column each names, table -> its model)``, from one
        silent sweep of every local model. Cached: retirement asks it once per app, and the
        sweep walks the whole registry."""
        if self._cascade_key_maps_cache is not None:
            return self._cascade_key_maps_cache
        required: dict[tuple[str, str, str | None], str] = {}
        models_by_table: dict[str, type[models.Model]] = {}
        self._required_self_cascade_keys: set[tuple[str, str]] = set()
        # Each owner's revive arms, off this sweep: MTI descendants in other apps contribute them.
        # Keyed on the real column, not the cascade key, whose ``None`` form two relations from
        # one model to an MTI parent and its child share -- and each is owed its own arm.
        self._revive_arm_sources: dict[str, dict[tuple, tuple[type[models.Model], str]]] = {}
        # Each owner's model and the apps whose models contribute arms to it, for a host.
        self._revive_owners: dict[str, tuple[type[models.Model], set[str]]] = {}
        for app in django_apps.get_app_configs():
            if not _generator.is_local(app):
                continue
            for model in app.get_models():
                # Filed before the gate below: ``_retired_cascade_column`` looks a table up
                # here to spell a reverse, and a routed-away table is still a table.
                models_by_table.setdefault(model._meta.db_table, model)
                if not has_column(model, '_deleted_at') or not migrates_to_postgresql(model):
                    continue
                owner = column_owner(model, '_deleted_at')
                owner_table = owner._meta.db_table
                # ``report=False``: this sweep covers apps the run was never asked about, and
                # their misconfigurations are not its to report -- ``_owned_candidates``' rule.
                candidates, selfs = self._cascade_candidates(model, owner_table, report=False)
                self._required_self_cascade_keys.update(
                    (owner_table, fk_field.column) for fk_field in selfs
                )
                for related_model, fk_field, is_primary in candidates:
                    related_table = related_model._meta.db_table
                    column = fk_field.column
                    key = (related_table, owner_table, None if is_primary else column)
                    required[key] = column
                    self._revive_arm_sources.setdefault(owner_table, {})[
                        (related_table, owner_table, column)
                    ] = (related_model, column)
                    contributors = self._revive_owners.setdefault(owner_table, (owner, set()))[1]
                    contributors.add(app.label)
        self._cascade_key_maps_cache = (required, models_by_table)
        return self._cascade_key_maps_cache

    def _revive_arms_by_owner(self) -> dict[str, dict[tuple, tuple[type[models.Model], str]]]:
        """``owner_table -> {(related_table, owner_table, column): (related model, column)}``
        for every relation a revive arm is owed, off the same sweep as :meth:`_cascade_key_maps`."""
        self._cascade_key_maps()
        return self._revive_arm_sources

    def _revive_host(self, owner_table: str) -> str | None:
        """The app writing *owner_table*'s revive trigger, for life: the app whose migrations
        created it; else the table's own host, as retirement's is; else -- an owner outside
        ``LOCAL_APPS`` -- the smallest-label app contributing an arm."""
        # Kept with its creator because any other host could move -- contributors change, the
        # owner's app joins ``LOCAL_APPS`` -- and a second app would create it again. A creator
        # gone from ``LOCAL_APPS`` is unscanned, as for every family. Routed away is no host.
        if owner_table in self._routed_away_tables():
            return None
        # And an owner outside ``LOCAL_APPS``, which that set cannot see: its model can.
        self._cascade_key_maps()
        owner, contributors = self._revive_owners.get(owner_table, (None, set()))
        if owner is not None and not migrates_to_postgresql(owner):
            return None
        local = {app.label for app in django_apps.get_app_configs() if _generator.is_local(app)}
        creates = self.existing.soft_delete_revive_owner_dependencies.get((owner_table,), [])
        created_in = [label for label, _migration in creates if label in local]
        if created_in:
            return created_in[-1]
        hosted = self._table_app_labels().get(owner_table)
        if hosted is not None:
            return hosted
        return min(contributors, default=None)

    def _required_self_cascades(self) -> set[tuple[str, str]]:
        """``(table, foreign_key)`` of every self-cascade trigger the models call for, off the
        same sweep as :meth:`_cascade_key_maps`."""
        self._cascade_key_maps()
        return self._required_self_cascade_keys

    @staticmethod
    def _declared_owned_keys() -> set[tuple[str, str, str]]:
        """Every owned key a declaration still names, *refused or not*: a refusal escalates a
        live rule to a failing ``--check`` already, so only an undeclared key is retired."""
        return {
            (
                column_owner(field.related_model, '_deleted_at')._meta.db_table,
                column_owner(model, '_deleted_at')._meta.db_table,
                field.column,
            )
            for app in django_apps.get_app_configs()
            if _generator.is_local(app)
            for model in app.get_models()
            if has_column(model, '_deleted_at')
            for field in OperationsMixin._declared_owning_fields(model)
            if has_column(field.related_model, '_deleted_at')
        }

    def _retired_trigger_operations(self, app: AppConfig) -> list[str]:
        """Retire the owned rule, its sweep and the self-cascade trigger whose key the models no
        longer call for (#66): their plpgsql bodies name the column, so after ``DROP COLUMN ...
        CASCADE`` each failed every UPDATE on its table. ``IF EXISTS`` and every spelling."""
        hosting = self._table_app_labels()
        declared = self._declared_owned_keys()
        required_selfs = self._required_self_cascades()
        quote = _identifiers._quote_table
        operations: list[str] = []
        owned = set(self.existing.soft_delete_owned) | set(self.existing.soft_delete_owned_sweep)
        for dependent_table, owner_table, foreign_key in sorted(owned - declared):
            if hosting.get(owner_table) != app.label:
                continue
            key = (dependent_table, owner_table, foreign_key)
            pairs = [
                (owner, dependent)
                for owner in (*self._prior_names(owner_table), owner_table)
                for dependent in (*self._prior_names(dependent_table), dependent_table)
            ]
            slots = {
                'dependent_table': _identifiers._escape_ident(dependent_table),
                'table': _identifiers._escape_ident(owner_table),
                'foreign_key': _identifiers._escape_ident(foreign_key),
            }
            for recorded, creates, header, drop, name in (
                (
                    self.existing.soft_delete_owned,
                    self.existing.soft_delete_owned_dependencies,
                    HEADER_SOFT_DELETE_OWNED_RETIRED,
                    self._drop_prior_rules(
                        quote(owner_table),
                        sorted({_owned_rule_name(d, foreign_key) for _o, d in pairs}),
                    ),
                    _owned_rule_name(dependent_table, foreign_key),
                ),
                (
                    self.existing.soft_delete_owned_sweep,
                    self.existing.soft_delete_owned_sweep_dependencies,
                    HEADER_SOFT_DELETE_OWNED_SWEEP_RETIRED,
                    self._drop_prior_triggers(
                        {'table': quote(owner_table)},
                        [_owned_sweep_name(o, d, foreign_key) for o, d in pairs],
                    ),
                    _owned_sweep_name(owner_table, dependent_table, foreign_key),
                ),
            ):
                if key in recorded:
                    self._record_retirement_edge(app.label, key, creates)
                    operations.append(
                        self._retirement(
                            app.label, header.format(**slots), drop, name, owner_table
                        )
                    )
        for table, foreign_key in sorted(
            set(self.existing.soft_delete_self_cascade) - required_selfs
        ):
            if hosting.get(table) != app.label:
                continue
            header = HEADER_SOFT_DELETE_SELF_CASCADE_RETIRED.format(
                table=_identifiers._escape_ident(table),
                foreign_key=_identifiers._escape_ident(foreign_key),
            )
            drop = self._drop_prior_triggers(
                {'table': quote(table)},
                [
                    _self_cascade_name(name, foreign_key)
                    for name in (*self._prior_names(table), table)
                ],
            )
            name = _self_cascade_name(table, foreign_key)
            # Ordered after its create: an MTI descendant's pass writes this trigger into the
            # descendant's app, while the retirement is hosted by the table's.
            self._record_retirement_edge(
                app.label,
                (table, foreign_key),
                self.existing.soft_delete_self_cascade_dependencies,
            )
            operations.append(self._retirement(app.label, header, drop, name, table))
        # An owner whose last cascade key went: its revive has no arm left (2.16.0, #70). An
        # owner keeping any key is re-emitted with the arm gone instead, by its digest moving.
        owed = set(self._revive_arms_by_owner())
        for (table,) in sorted(set(self.existing.soft_delete_revive_owner)):
            if table in owed or self._revive_host(table) != app.label:
                continue
            header = HEADER_SOFT_DELETE_REVIVE_OWNER_RETIRED.format(
                table=_identifiers._escape_ident(table)
            )
            drop = self._drop_prior_triggers(
                {'table': quote(table)},
                [_revive_owner_name(name) for name in (*self._prior_names(table), table)],
            )
            self._record_retirement_edge(
                app.label, (table,), self.existing.soft_delete_revive_owner_dependencies
            )
            operations.append(
                self._retirement(app.label, header, drop, _revive_owner_name(table), table)
            )
        return operations

    def _retirement(self, app_label: str, header: str, drop: str, name: str, table: str) -> str:
        """One #66 retirement: the drop, and a reverse that refuses and points at ``--adopt``."""
        # Its operation set may recur once a key is re-adopted and retired again.
        self.existing.retirement_apps.add(app_label)
        reverse = _soft_delete._REFUSE_REVERSING_RETIREMENT.format(
            literal_name=_identifiers._quote_literal(name),
            literal_table=_identifiers._quote_literal(table),
        )
        source, _ = _operation(header, drop, reverse)
        return source

    def _retired_cascade_column(
        self, key: tuple[str, str, str | None], models_by_table: dict[str, type[models.Model]]
    ) -> str | None:
        """The column a retired rule read, so its ``reverse_sql`` can recreate it. The ``_via``
        form spells it in the key; the primary form does not, so it is recovered as the first
        remaining foreign key from the child to that owner -- the same order that picked it."""
        related_model = models_by_table.get(key[0])
        if related_model is None:  # pragma: no cover - the caller checks hosting first
            return None
        # A proxy binds the same table and may reach the map first, from an app registered
        # earlier. Its ``local_fields`` are empty, so the scan below would recover nothing and
        # the reverse would refuse where it could have rebuilt the rule.
        related_model = related_model._meta.concrete_model or related_model
        # A joined rule updates the ancestor, which the key does not name: the flat template
        # would rebuild it against a table with no ``_deleted_at``. So the reverse refuses.
        if _is_joined(related_model):
            return None
        if key[2] is not None:
            return key[2]
        # Not filtered to cascade candidates: the relaxed field is the one that stopped being
        # one, and is the common case. So the net is wide, and where it catches more than one
        # the reverse refuses -- guessing rebuilds the rule on a column it never read.
        columns = sorted(
            field.column
            for field in related_model._meta.local_fields
            if isinstance(field, models.ForeignKey)
            and has_column(field.related_model, '_deleted_at')
            and column_owner(field.related_model, '_deleted_at')._meta.db_table == key[1]
        )
        return columns[0] if len(columns) == 1 else None

    def _retired_cascade_families(self, key: tuple[str, str, str | None]) -> list[_RetiredFamily]:
        """The cascade rule and its inverse, as the retirement loop needs to see them. Both or
        neither is the wrong answer -- a key recorded before 2.11.0 has no revive to drop -- so
        each carries the recorded map its arm tests against."""
        return [
            _RetiredFamily(
                recorded=self.existing.soft_delete_related,
                creates=self.existing.soft_delete_related_dependencies,
                name=lambda _owner, related, via: _related_rule_name(related, via),
                slots=lambda name, table: {'rule_name': name, 'table': table},
                drop_template=_soft_delete._DROP_SOFT_DELETE_RELATED_OBJECTS_RULE,
                create_template=_soft_delete._CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE,
                drop_prior=self._drop_prior_rules,
                header=HEADER_SOFT_DELETE_RELATED_RETIRED,
                via_header=HEADER_SOFT_DELETE_RELATED_VIA_RETIRED,
            ),
            _RetiredFamily(
                recorded=self.existing.soft_delete_revive,
                creates=self.existing.soft_delete_revive_dependencies,
                name=_revive_name,
                slots=lambda name, table: {
                    'function': name,
                    'trigger': name,
                    'table': table,
                },
                drop_template=_soft_delete._DROP_SOFT_DELETE_REVIVE,
                create_template=_soft_delete._CREATE_SOFT_DELETE_REVIVE,
                drop_prior=lambda table, names: self._drop_prior_triggers({'table': table}, names),
                header=HEADER_SOFT_DELETE_REVIVE_RETIRED,
                via_header=HEADER_SOFT_DELETE_REVIVE_VIA_RETIRED,
            ),
        ]

    def _note_a_cycle_retirement(self, key: tuple, models_by_table: dict) -> None:
        """Say so when a retired rule goes because its key lies on a rule cycle: the "skipped"
        notes read as "left alone", and the next migration drops what was working."""
        related_table, owner_table, _ = key
        related = models_by_table[related_table]
        if not has_column(related, '_deleted_at'):  # no longer soft-deletable: nothing to cycle
            return
        updated = column_owner(related._meta.concrete_model or related, '_deleted_at')
        if (owner_table, updated._meta.db_table) not in self._rule_cycle_edges():
            return
        # The edge is shared by every descendant of one ancestor: only a key still cascading is
        # on the cycle, a relaxed one is retired for its own reason.
        if not any(
            on_delete is models.CASCADE
            and (rel_model._meta.concrete_model or rel_model)
            is (related._meta.concrete_model or related)
            for rel_model, _, on_delete in self.reverse_relations_mapping.get(
                models_by_table[owner_table], ()
            )
        ):
            return
        note = (
            f"Cascade rules on '{owner_table}' for '{related_table}' are dropped: the key lies "
            'on a rule cycle, where every edge is refused, and a live rule goes with it.'
        )
        if note not in self._skipped_rule_notes:
            self._skipped_rule_notes.append(note)

    @staticmethod
    def _retired_key_is_joined(related_table: str, models_by_table: dict) -> bool:
        """Whether the retired rule updated an ancestor -- the one refusal whose cause is not
        an unrecorded column. Through ``concrete_model`` for ``_retired_cascade_column``'s reason."""
        model = models_by_table[related_table]
        return _is_joined(model._meta.concrete_model or model)

    def _revive_updated_at(self, related_table: str) -> str:
        """The ``_updated_at`` splice for a revive body rebuilt by a retirement's reverse. Its
        ``UPDATE`` runs at trigger depth 1, where ``updated_at_trigger``'s ``WHEN`` suppresses
        that trigger -- so the column has to move here or it moves on neither path."""
        _required, models_by_table = self._cascade_key_maps()
        model = models_by_table.get(related_table)
        if model is None:
            return ''
        # Through ``concrete_model``, as ``_retired_cascade_column`` resolves the same map: a
        # proxy binds the same table and may reach it first, from an app registered earlier,
        # and declares no ``local_fields`` -- so ``owns_column`` would answer for the wrong one.
        model = model._meta.concrete_model or model
        if not owns_column(model, '_updated_at'):
            return ''
        return _soft_delete._SOFT_DELETE_REVIVE_UPDATED_AT

    def _superseded_revive_reverse(self, key: tuple, slots: dict, column: str) -> str:
        """The per-key revive a 2.16.0 retirement drops, rebuilt as the per-key emitter wrote it:
        the flat or joined template, ``_updated_at`` spliced where the row it revives owns it.
        Its models off the arm sweep, which reaches a child or owner outside ``LOCAL_APPS``."""
        related_table, owner_table, _via = key
        related_model, _column = self._revive_arms_by_owner()[owner_table][
            (related_table, owner_table, column)
        ]
        related_model = related_model._meta.concrete_model or related_model
        owner = self._revive_owners[owner_table][0]
        owner = owner._meta.concrete_model or owner
        ident_owner_pk = _identifiers._escape_ident(cast(str, owner._meta.pk.column))
        joined = not owns_column(related_model, '_deleted_at')
        target = column_owner(related_model, '_deleted_at')
        rebuilt = {
            **slots,
            'related_table': _identifiers._quote_table(related_table),
            'primary_key': ident_owner_pk,
            'foreign_key': _identifiers._escape_ident(column),
            'updated_at_assignment': (
                _soft_delete._SOFT_DELETE_REVIVE_UPDATED_AT
                if owns_column(target, '_updated_at')
                else ''
            ),
        }
        if not joined:
            return _soft_delete._CREATE_SOFT_DELETE_REVIVE.format(**rebuilt)
        return _soft_delete._CREATE_SOFT_DELETE_REVIVE_JOINED.format(
            **rebuilt,
            target_table=_identifiers._quote_table(target._meta.db_table),
            target_pk=_identifiers._escape_ident(cast(str, target._meta.pk.column)),
            child_pk=_identifiers._escape_ident(
                cast(str, _parent_link(related_model, target).column)
            ),
        )

    def _retired_cascade_operations(self, app: AppConfig, *, adopt: bool = False) -> list[str]:
        """Drop cascade rules *app*'s tables record but the models no longer call for -- a
        ``CASCADE`` key relaxed to ``SET_NULL``, made owning, or removed. Positive evidence
        only: both tables must still map, or a scoped run would retire a live rule."""
        hosting = self._table_app_labels()
        required, models_by_table = self._cascade_key_maps()
        operations: list[str] = []
        # The union, because the two families can be recorded apart: a key retired before
        # 2.11.0 has a cascade to drop and no revive. Each arm emits only where *its* family
        # recorded the key, which a bundled drop could never say.
        recorded = set(self.existing.soft_delete_related) | set(self.existing.soft_delete_revive)
        for key in sorted(recorded, key=lambda k: (k[0], k[1], k[2] or '')):
            # A per-key revive is retired whether or not its key still cascades: since 2.16.0
            # the owner's one trigger carries its arm (#70). The rule only once it is unowed.
            rule_retired = key not in required
            if not rule_retired and key not in self.existing.soft_delete_revive:
                continue
            related_table, owner_table, via = key
            # A superseded revive needs no evidence: the models still call for its key, and the
            # owner's trigger carries its arm. It goes where that trigger is written, then.
            if not rule_retired:
                if self._revive_host(owner_table) != app.label:
                    continue
                deleted = False
            else:
                # Both, not just the host: a table mapping to nothing is a *deleted* model on one
                # reading and an app dropped from LOCAL_APPS on another. Only the migration
                # history tells them apart; without its evidence the key is named, not retired.
                if hosting.get(owner_table) != app.label:
                    continue
                # The one unmapped table with evidence behind it: a ``DeleteModel`` dropped it,
                # which took the rule and left the owner's revive trigger failing every UPDATE.
                deleted = related_table not in hosting and related_table in self._dropped_tables()
                if related_table not in hosting and not deleted:
                    continue
            column = (
                required[key]
                if not rule_retired
                else None
                if deleted
                else self._retired_cascade_column(key, models_by_table)
            )
            if rule_retired and not deleted:
                self._note_a_cycle_retirement(key, models_by_table)
            ident_owner_table = _identifiers._quote_table(owner_table)
            for family in self._retired_cascade_families(key):
                if key not in family.recorded:
                    continue
                if not rule_retired and family.recorded is not self.existing.soft_delete_revive:
                    continue
                self._record_retirement_edge(app.label, key, family.creates)
                if deleted:
                    # After the drop of the child's table, not only after the create: run first,
                    # it removed both objects while the child was live and archivable.
                    deleting_node = self._dropped_tables()[related_table]
                    if deleting_node[0] != app.label:
                        self._record_edge(self._retirement_edges, app.label, deleting_node)
                rule_name = family.name(owner_table, related_table, via)
                slots = family.slots(rule_name, ident_owner_table)
                # ``IF EXISTS`` on every retirement, over every name the table has held (ADR
                # 0029): ``DROP ... CASCADE`` takes a rule with the column or table it reads, and
                # a hand-drop the docs advised takes a trigger, so absent is the expected case.
                drop = family.drop_prior(
                    ident_owner_table,
                    # Both tables' spellings: a revive's name embeds the owner too, and a renamed
                    # owner carries the trigger under its old one. Deduped for the rule, whose
                    # name spells the related table alone.
                    list(
                        dict.fromkeys(
                            family.name(owner, related, via)
                            for owner in (*self._prior_names(owner_table), owner_table)
                            for related in (*self._prior_names(related_table), related_table)
                        )
                    ),
                )
                reverse = (
                    # A key the models still cascade is a revive superseded, not one unowed: its
                    # reverse rebuilds it as it was, joined form included, so 2.16.0 unapplies.
                    self._superseded_revive_reverse(key, slots, required[key])
                    if not rule_retired
                    else family.create_template.format(
                        **slots,
                        related_table=_identifiers._quote_table(related_table),
                        primary_key=_identifiers._escape_ident(
                            cast(str, models_by_table[owner_table]._meta.pk.column)
                        ),
                        foreign_key=_identifiers._escape_ident(column),
                        updated_at_assignment=self._revive_updated_at(related_table),
                    )
                    if column is not None
                    # Passed as ``RAISE`` arguments, not interpolated into the literal: the
                    # quoted forms escape ``"`` but not ``'``, so a db_table carrying one
                    # would break it.
                    else (
                        _soft_delete._REFUSE_RECREATING_DROPPED_RULE
                        if deleted
                        else _soft_delete._REFUSE_RECREATING_JOINED_RULE
                        if self._retired_key_is_joined(related_table, models_by_table)
                        else _soft_delete._REFUSE_RECREATING_RETIRED_RULE
                    ).format(
                        literal_rule_name=_identifiers._quote_literal(rule_name),
                        literal_table=_identifiers._quote_literal(owner_table),
                    )
                )
                header = (
                    family.header.format(
                        related_table=_identifiers._escape_ident(related_table),
                        table=_identifiers._escape_ident(owner_table),
                    )
                    if via is None
                    else family.via_header.format(
                        related_table=_identifiers._escape_ident(related_table),
                        table=_identifiers._escape_ident(owner_table),
                        foreign_key=_identifiers._escape_ident(via),
                    )
                )
                # Not ``_append_if_stale``, for ``_retired_autofill_operations``' reason: the
                # set difference above is the whole idempotency mechanism, and "recorded digest
                # differs -> replace" means nothing for a drop.
                source, _ = _operation(header, drop, reverse)
                operations.append(source)
        return operations

    def _orphaned_mti_notes(self) -> list[str]:
        """Recorded MTI operations no local model calls for -- through 2.9.0 a **proxy** over a
        model owning the column earned them, naming its own table as parent, and a model
        flattened out of inheritance leaves the same record. Named, not retired: repairs differ."""
        # Blind by construction to a proxy over a *real* MTI child: it recorded the very key
        # the child still requires, so the difference is empty. That one is read off the file
        # instead, by ``_duplicated_mti_notes``, which needs no difference to see it.
        hosting = self._table_app_labels()
        # A name a rename freed and a later model retook. The scan leaves the record under the
        # freed name while that name is live, and the object went with the table -- so it is
        # the rename's, not an orphan, and naming it sends a consumer to a live operation.
        carried = {old for chain in self.existing.renamed_tables.values() for old in chain}
        required_triggers = set()
        required_soft_deletes = set()
        for app in django_apps.get_app_configs():
            if not _generator.is_local(app):
                continue
            for model in app.get_models():
                if is_mti_child(model, '_updated_at'):
                    required_triggers.add(model._meta.db_table)
                if is_mti_child(model, '_deleted_at'):
                    required_soft_deletes.add(model._meta.db_table)

        notes: list[str] = []
        for kind, column, inert, recorded, required in (
            (
                'MTI Updated at Trigger',
                '_updated_at',
                "the plain form collides with the concrete model's own trigger, PostgreSQL "
                'refusing a second of one name on a table, so that migration aborted -- but '
                'the --adopt form drops before it creates, so that one applied',
                self.existing.mti_triggers,
                required_triggers,
            ),
            (
                'MTI Soft Delete Rule',
                '_deleted_at',
                'PostgreSQL dedupes a rule on its name per table, so nothing collided -- '
                'though the trigger beside it in the same atomic migration may still have '
                'aborted the pair, on every ladder rung carrying both columns',
                self.existing.mti_soft_deletes,
                required_soft_deletes,
            ),
        ):
            for table in sorted(set(recorded) - required):
                # Positive evidence, 2.9.0's rule: a table mapping to nothing is a deleted
                # model on one reading and a scoped run on another, and stays silent. A hosted
                # table whose model does not call for the operation is the shape below.
                if table not in hosting or table in carried:
                    continue
                notes.append(
                    f"{kind} on '{table}' is recorded, but no local model reaches {column} "
                    f'through an ancestor. Either a proxy model earned the operation before '
                    f'2.9.1, naming its own table as its parent -- {inert} -- or a model was '
                    f'flattened out of inheritance and left the object live. Delete the '
                    f'operation from the migration that writes it either way; this command '
                    f'cannot repair a file. Then look in the database rather than assuming, '
                    f'and drop by hand whatever survived.'
                )
        return notes

    #: What a repeated header of each kind did to its migration. Only the plain trigger form
    #: collides, and the scan reads a comment, which cannot say which form wrote it -- so the
    #: note carries the whole answer rather than picking the half that sounds worst.
    _DUPLICATE_MTI_EFFECT = {
        'MTI Updated at Trigger': (
            'The plain form of that operation cannot apply, PostgreSQL refusing a second '
            'trigger of one name on a table, and a rule beside it in the same atomic migration '
            'goes down with it -- but the --adopt form drops before it creates, so a history '
            'generated that way applied and is carrying the second'
        ),
        'MTI Soft Delete Rule': (
            'That operation applies either way, the rule form being CREATE OR REPLACE and '
            'PostgreSQL deduping a rule on its name per table, so the copy only ever replaced '
            'the first -- unless a repeated trigger in the same atomic migration took it down'
        ),
    }

    def _duplicated_mti_notes(self) -> list[str]:
        """One migration carrying an MTI header twice, which is the only evidence of the proxy
        shape :meth:`_orphaned_mti_notes` cannot see. Same-app only: a proxy declared in another
        app writes its copy into that app's own file, and one table takes one operation *there*."""
        return [
            f"{kind} on '{table}' is written twice by migration '{migration}' of app "
            f"'{app_label}' -- the table as that migration spells it, which a later rename may "
            f'have moved on from. One table takes one such operation, so the second is a copy: '
            f'a proxy model earned it before 2.9.1, or a migration was edited by hand, or two '
            f'models share that ``db_table``. '
            f'{self._DUPLICATE_MTI_EFFECT[kind]}. Delete the repeated operation from that file, '
            f'keeping one, and look in the database rather than assuming; this command cannot '
            f'repair a file.'
            for app_label, migration, kind, table in self.existing.duplicate_mti_operations
        ]

    def _unmapped_cascade_notes(self) -> list[str]:
        """Recorded cascade rules this run will not retire because a table they name maps to no
        local model. Named rather than dropped: that is a deleted model on one reading and a
        scoped run on another, and following the wrong one destroys a live cascade."""
        hosting = self._table_app_labels()
        required, _models = self._cascade_key_maps()
        notes: list[str] = []
        # Deduped by key across both families: one unretirable cascade key is one finding.
        recorded = set(self.existing.soft_delete_related) | set(self.existing.soft_delete_revive)
        for key in sorted(recorded - set(required), key=lambda k: (k[0], k[1], k[2] or '')):
            related_table, owner_table, via = key
            if owner_table in hosting and related_table in hosting:
                continue
            # Retired above on the evidence of a ``DeleteModel``; or the owner's table itself was
            # dropped, taking every rule and trigger on it.
            dropped = self._dropped_tables()
            if owner_table in dropped or (owner_table in hosting and related_table in dropped):
                continue
            # A routed-away table maps to nothing by design, and the vendor note already
            # says why. Advising a by-hand drop here would name the same model twice.
            if {owner_table, related_table} & self._routed_away_tables():
                continue
            # **Both** halves, as the owned pair set the precedent: the cascade alone leaves the
            # revive reviving children whose stamp still matches. Only halves recorded here are
            # named, ``IF EXISTS`` as the generated retirements say it (ADR 0029).
            quoted_owner = _identifiers._quote_table(owner_table)
            drop = '\n'.join(
                statement.strip()
                for recorded_in, statement in (
                    (
                        self.existing.soft_delete_related,
                        self._drop_prior_rules(
                            quoted_owner, [_related_rule_name(related_table, via)]
                        ),
                    ),
                    (
                        self.existing.soft_delete_revive,
                        self._drop_prior_triggers(
                            {'table': quoted_owner},
                            [_revive_name(owner_table, related_table, via)],
                        ),
                    ),
                )
                if key in recorded_in
            )
            # Named for the halves actually recorded, not "Cascade" flat: the DROP below is
            # whichever this project has, and a note opening on the wrong one reads as stale.
            recorded_families = [
                label
                for label, recorded_in in (
                    ('Cascade', self.existing.soft_delete_related),
                    ('Revive', self.existing.soft_delete_revive),
                )
                if key in recorded_in
            ]
            families = ' and '.join(recorded_families)
            # Agreement on the combined branch: "Cascade and Revive rule ... is recorded"
            # reads as one object where the note is about two.
            noun, verb = ('rules', 'are') if len(recorded_families) > 1 else ('rule', 'is')
            notes.append(
                f"{families} {noun} on '{owner_table}' related to '{related_table}' {verb} "
                f'recorded but the models no longer call for it, and one of those tables maps '
                f'to no local model -- so this run cannot tell a deleted model from an app '
                f'outside LOCAL_APPS, and will not retire it. If the rule is really gone, '
                f'drop it by hand: {drop}'
            )
        return notes

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
            # ``IF EXISTS`` as every retirement says it (ADR 0029): a column dropped with
            # ``CASCADE`` or a hand-drop leaves nothing for a strict drop to find.
            drop = _triggers._ADOPT_DROP_TENANT_AUTOFILL_TRIGGER.format(**slots)
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
            if table in hosting or table in self._routed_away_tables():
                continue
            slots = self._autofill_slots(table, function)
            notes.append(
                f"Tenant autofill trigger on '{table}' (function '{function}') is recorded "
                f'but no local model maps to that table, so it cannot be retired here. If '
                f'the table still exists, drop it by hand: '
                f'{_triggers._ADOPT_DROP_TENANT_AUTOFILL_TRIGGER.format(**slots).strip()}'
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
        self._record_app_object_ref(model._meta.app_label, referenced, field)

    def _record_app_object_ref(
        self, app_label: str, referenced: type[models.Model], field: str | None = None
    ) -> None:
        """The same, for an operation built from a *table* rather than a model in hand -- a
        tenant policy, whose coverage is keyed by table name. The app is the one being scanned,
        which is where the operation lands, so it is passed rather than read off a model."""
        ref = ObjectRef(referenced._meta.app_label, referenced.__name__, field)
        refs = self._object_refs.setdefault(app_label, [])
        if ref not in refs:
            refs.append(ref)

    def _record_retirement_edge(
        self,
        app_label: str,
        key: tuple,
        creates_by_key: dict[Any, list[tuple[str, str]]],
    ) -> None:
        """Order a cascade retirement against the migration that created the object it drops.
        Read off the scan rather than resolved: neither is visible to migration state, and the
        drop is hosted by the owner table's app either way."""
        # *creates_by_key* is required, not defaulted: the cascade's creates cannot order the
        # revive's drop, and a default would hand a caller that forgot it the wrong family.

        # This drop is genuinely being written, so its operation set may recur if the key is
        # re-adopted and retired again -- tainted here, not for every app that has ever retired
        # anything, or a one-time retirement disables the guard forever needlessly.
        self.existing.retirement_apps.add(app_label)
        creates = creates_by_key.get(key, [])
        # The newest: the drop being written now comes after every one of them, so the last is
        # the one whose rule is live. Own-app creates are ordered by that app's own history.
        if not creates or creates[-1][0] == app_label:
            return
        self._record_edge(self._retirement_edges, app_label, creates[-1])

    def _record_readoption_edge(
        self,
        app_label: str,
        key: tuple,
        recorded: dict[Any, str | None] | None = None,
        retirement_sites: list[CascadeRetirementSite] | None = None,
    ) -> None:
        """Order a cascade create against the retirement it revives. Without it a fresh
        ``migrate`` can run the ``CREATE`` before that ``DROP`` and end with no rule, where an
        incremental database has one -- the mirror of the drop's own edge. See ADR 0021."""
        # Per family, for :meth:`_record_retirement_edge`'s reason.
        if recorded is None:
            recorded = self.existing.soft_delete_related
        if retirement_sites is None:
            retirement_sites = self.existing.cascade_retirement_sites
        # Only where the key is *not* recorded: it was retired and this run is reviving it. A
        # key still recorded was never dropped, and an edge for it is one the app's own history
        # already implies -- which ``drop_implied_edges`` cannot see, comparing only candidates.
        if key in recorded:
            return
        sites = [(site.app_label, site.migration) for site in retirement_sites if site.key == key]
        # Genuinely absent with no retirement history is a brand new key, not a re-adoption --
        # its create has never recurred, so the digest guard has nothing to yield for.
        if not sites:
            return
        # Reaching here, this app is re-emitting a create it already wrote once -- breaking
        # the digest guard's "an operation set never recurs" assumption on purpose.
        self.existing.retirement_apps.add(app_label)
        if sites[-1][0] == app_label:
            return
        self._record_edge(self._retirement_edges, app_label, sites[-1])

    @staticmethod
    def _record_edge(
        edges_by_app: dict[str, list[tuple[str, str]]], app_label: str, edge: tuple[str, str]
    ) -> None:
        """File *edge* under the app whose migration will carry it, once."""
        edges = edges_by_app.setdefault(app_label, [])
        if edge not in edges:
            edges.append(edge)

    def _refuse_owned(self, key: tuple[str, str, str] | None, message: str) -> None:
        """Record an owned-rule refusal, escalating where anything for *key* is already
        recorded: refusing emits nothing, so what is live stays live and wrong under a green
        ``--check``, and no command retires it. See ADR 0012."""
        self._skipped_rule_notes.append(message)
        if key is None:
            return
        # Both halves, or an operator told to drop the rule leaves the 2.6.0 sweep stamping
        # the very rows the refusal exists to spare -- and a sweep can outlive its rule, the
        # two being separate operations with separate identities.
        recorded = [
            existing
            for existing in (
                self.existing.soft_delete_owned,
                self.existing.soft_delete_owned_sweep,
            )
            if key in existing
        ]
        if not recorded:
            return
        dependent_table, owner_table, foreign_key = key
        table = _identifiers._quote_table(owner_table)
        drops = []
        if key in self.existing.soft_delete_owned:
            drops.append(f'DROP RULE {_owned_rule_name(dependent_table, foreign_key)} ON {table};')
        if key in self.existing.soft_delete_owned_sweep:
            sweep = _owned_sweep_name(owner_table, dependent_table, foreign_key)
            drops.append(f'DROP TRIGGER {sweep} ON {table}; DROP FUNCTION {sweep}();')
        self._refusals_over_live_rules.append(
            f"Owned enforcement on '{dependent_table}' owned by '{owner_table}' via "
            f"'{foreign_key}' is refused but already exists in this project's migrations. "
            'It is still live in any migrated database and no longer correct. Drop it by '
            f'hand: {" ".join(drops)}'
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

    def _cascade_candidates(
        self, model: type[models.Model], owner_table: str, *, report: bool = True
    ) -> tuple[list[tuple[type[models.Model], models.ForeignKey, bool]], list[models.ForeignKey]]:
        """CASCADE FKs pointing at *model*: the ones taking a **rule**, flagged whether each is
        the *primary* one for its related_table (the first in sorted order, keeping the historical
        plain form), and the self-referential ones taking a **trigger** instead (ADR 0018)."""
        seen_related_tables: set[str] = set()
        candidates: list[tuple[type[models.Model], models.ForeignKey, bool]] = []
        self_cascades: list[models.ForeignKey] = []
        for related_model, fk_field, on_delete in sorted(
            self.reverse_relations_mapping[model],
            key=lambda t: (t[0]._meta.db_table, t[1].column),
        ):
            # ``classify_cascade`` leaves out structural parent-links and MTI-inherited FKs: the
            # redirect rule already ties a child's deletion to the owner, and every table in a
            # chain shares one ``_deleted_at``, so that rule already archives them.
            kind = classify_cascade(
                related_model, fk_field, on_delete, owner_table, self._rule_cycle_edges()
            )
            if kind is CascadeKind.NONE:
                continue
            related_table = related_model._meta.db_table
            if kind is CascadeKind.SELF:
                self_cascades.append(fk_field)
                continue
            if kind is CascadeKind.CYCLE:
                if report:
                    self._skipped_rule_notes.append(
                        self._cycle_warning(
                            'Cascade',
                            f"'{related_table}'",
                            owner_table,
                            column_owner(related_model, '_deleted_at')._meta.db_table,
                        )
                    )
                continue
            if kind is CascadeKind.REFUSED:
                if report:
                    self._skipped_rule_notes.append(
                        f"Cascade '{related_table}' -> '{owner_table}' skipped: "
                        f'{joined_refusal(related_model, fk_field)}; Django archives it in Python.'
                    )
                continue
            is_primary = related_table not in seen_related_tables
            seen_related_tables.add(related_table)
            candidates.append((related_model, fk_field, is_primary))
        return candidates, self_cascades

    @staticmethod
    def _drop_prior_rules(table: str, names: list[str]) -> str:
        """``DROP RULE IF EXISTS`` for each name a rename left behind. Without it the
        carried-over rule stays live beside the new one, both cascading, neither retired."""
        return ''.join(
            _soft_delete._DROP_RENAMED_RULE.format(old_rule_name=name, table=table)
            for name in names
        )

    @staticmethod
    def _drop_prior_triggers(slots: dict, names: list[str]) -> str:
        """``DROP TRIGGER``/``DROP FUNCTION IF EXISTS`` for each name a rename left behind."""
        return ''.join(
            _soft_delete._DROP_RENAMED_TRIGGER.format(
                old_trigger=name, old_function=name, table=slots['table']
            )
            for name in names
        )

    def _owned_sweep_form(
        self,
        slots: dict,
        owner_table: str,
        dependent_table: str,
        foreign_key: str,
        *,
        unrenamed: str,
    ) -> str:
        """:meth:`_self_cascade_form` for the sweep, whose name folds in **two** tables --
        either of which a rename can have moved, and each through its own chain."""
        if not self._renamed(owner_table, dependent_table):
            return unrenamed.format(**slots)
        owners = [owner_table, *self._prior_names(owner_table)]
        dependents = [dependent_table, *self._prior_names(dependent_table)]
        names = [
            _owned_sweep_name(owner, dependent, foreign_key)
            for owner in owners
            for dependent in dependents
            if (owner, dependent) != (owner_table, dependent_table)
        ]
        return self._drop_prior_triggers(
            slots, names
        ) + _soft_delete._ADOPT_SOFT_DELETE_OWNED_SWEEP.format(**slots)

    def _self_cascade_form(
        self, slots: dict, owner_table: str, foreign_key: str, *, unrenamed: str
    ) -> str:
        """The replace or adopt form, *unrenamed* being which applies when no rename moved the
        table. Where one did, every prior name goes and the adopt body drops the current one
        ``IF EXISTS`` on top -- which spelling is live depends on when a generation last ran."""
        if not self._renamed(owner_table):
            return unrenamed.format(**slots)
        return self._drop_prior_triggers(
            slots,
            [_self_cascade_name(name, foreign_key) for name in self._prior_names(owner_table)],
        ) + _soft_delete._ADOPT_SOFT_DELETE_SELF_CASCADE.format(**slots)

    def _renamed(self, *tables: str) -> bool:
        """Whether any of *tables* has a prior name still worth dropping. Asked of
        :meth:`_prior_names`, not of the chain: with nothing left to drop the strict form is
        honest, and an ``IF EXISTS`` on a known answer would hide a diverged database."""
        return any(self._prior_names(table) for table in tables)

    def _prior_names(self, table: str) -> list[str]:
        """Every *dead* name *table* held before, oldest first: all of them, a generation
        between two renames having left an object under the intermediate one."""
        # Filtered here, not in the chain the scan needs whole: a freed name retaken by a
        # later ``CreateModel`` must not be dropped, but must still translate -- emptying the
        # chain leaves the renamed table uncovered, so the plain CREATE collides after all.

        chain = self.existing.renamed_tables.get(table, [])
        if not chain:
            # The overwhelming common case, and the reason the registry sweep below is not
            # cached: a project that renamed nothing never reaches it at all.
            return []

        # Asked of the **whole** registry rather than ``LOCAL_APPS``: a name retaken by a model
        # this generator never writes for is no less live, and dropping its objects no less wrong.
        live = {
            model._meta.db_table
            for app in django_apps.get_app_configs()
            for model in app.get_models()
        }
        return [name for name in chain if name not in live]

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

    def _claim_sweep_function_name(
        self, name: str, relation: tuple, *, kind: str = 'Owned sweep'
    ) -> None:
        """:meth:`_claim_rule_name`'s equivalent, keyed on the name **alone**: a function is
        namespaced per schema, so the second of two relations reaching one name overwrites the
        first's body. One registry across families, which makes disjointness a checked claim."""
        claimed = self._claimed_sweep_names.setdefault(name, relation)
        if claimed != relation:
            self._rule_name_clashes.append(
                f'{kind} function {name} is named by both '
                f'{_rule_relation_label(claimed)} and {_rule_relation_label(relation)}. '
                'A function is namespaced per schema, so the second replaces the first and '
                "one of the two tables' triggers would run the other's predicate. Rename a "
                'column or a table so the two names differ.'
            )

    def _revive_operations(self, app: AppConfig, *, adopt: bool = False) -> list[str]:
        """One revive trigger per owner table *app* hosts, carrying every cascade key's arm
        (2.16.0, #70, ADR 0033): a plain ``UPDATE`` of the owner fires it once, and it leaves at
        its first test unless the statement revived a row. Hosted as retirement is."""
        operations: list[str] = []
        for owner_table in sorted(self._revive_arms_by_owner()):
            if self._revive_host(owner_table) != app.label:
                continue
            slots = self._revive_owner_slots(owner_table)
            if slots is None:
                # Every arm, or the owner, refused: nothing is emitted and, its keys still
                # calling for arms, nothing retires it -- so a recorded one is named for a hand
                # drop, as the per-key trigger's refusal was, failing ``--check``.
                if (owner_table,) in self.existing.soft_delete_revive_owner:
                    name = _revive_owner_name(owner_table)
                    self._refusals_over_live_rules.append(
                        f"Revive trigger on '{owner_table}' is refused but already exists in "
                        "this project's migrations. It is still live in any migrated database. "
                        f'Drop it by hand: DROP TRIGGER {name} ON '
                        f'{_identifiers._quote_table(owner_table)}; DROP FUNCTION {name}();'
                    )
                continue
            name = slots['function']
            # Claimed on the name alone, as every trigger family's function is: a function is
            # namespaced per schema, so two owner tables could otherwise meet on one name.
            self._claim_sweep_function_name(name, (owner_table, owner_table, None), kind='Revive')
            key = (owner_table,)
            # Against the retirement it revives, as the self cascade's create is (ADR 0021).
            self._record_readoption_edge(
                app.label,
                key,
                self.existing.soft_delete_revive_owner,
                self.existing.revive_owner_retirement_sites,
            )
            self._append_if_stale(
                operations,
                self.existing.soft_delete_revive_owner,
                key,
                HEADER_SOFT_DELETE_REVIVE_OWNER.format(
                    table=_identifiers._escape_ident(owner_table)
                ),
                _soft_delete._CREATE_SOFT_DELETE_REVIVE_OWNER.format(**slots),
                _soft_delete._DROP_SOFT_DELETE_REVIVE.format(**slots),
                # ``CREATE TRIGGER`` has no ``OR REPLACE``, so a re-emission needs the drop.
                replace=self._revive_owner_form(
                    slots, owner_table, unrenamed=_soft_delete._REPLACE_SOFT_DELETE_REVIVE_OWNER
                ),
                adopt=self._revive_owner_form(
                    slots, owner_table, unrenamed=_soft_delete._ADOPT_SOFT_DELETE_REVIVE_OWNER
                ),
                is_adopt=adopt,
            )
        return operations

    def _revive_owner_slots(self, owner_table: str, *, quiet: bool = False) -> dict | None:
        """Every slot of *owner_table*'s revive trigger, arms rendered, or ``None`` where every
        arm or the owner itself is refused. *quiet* for a caller only comparing digests, so a
        refusal is reported once, by the run that emits."""
        self._cascade_key_maps()
        owner, _contributors = self._revive_owners[owner_table]
        owner = owner._meta.concrete_model or owner
        arms = self._revive_arms_by_owner()[owner_table]
        name = _revive_owner_name(owner_table)
        slots = {
            'function': name,
            'trigger': name,
            'table': _identifiers._quote_table(owner_table),
            'primary_key': _identifiers._escape_ident(cast(str, owner._meta.pk.column)),
        }
        rendered = [
            arm
            for key in sorted(arms, key=lambda k: (k[0], k[2] or ''))
            if (arm := self._revive_arm(key, *arms[key], slots['primary_key'], quiet=quiet))
            is not None
        ]
        if not rendered:
            return None
        # Refused rather than escaped, as each arm's own slots are: the function name and
        # the owner's are spliced into the same dollar-quoted body.
        if any('$$' in slots[slot] for slot in ('function', 'table', 'primary_key')):
            if not quiet:
                self._skipped_rule_notes.append(
                    f"Revive trigger on '{owner_table}' skipped: its table or primary key "
                    'contains "$$", which closes the dollar quoting this trigger function '
                    'depends on. Set a db_table / db_column without it.'
                )
            return None
        return slots | {'arms': ''.join(rendered)}

    def _revive_arm(
        self,
        key: tuple,
        related_model: type[models.Model],
        column: str,
        ident_owner_pk: str,
        *,
        quiet: bool = False,
    ) -> str | None:
        """One cascade key's arm of its owner's revive: the per-key body without its guard. A
        joined key, whose ``_deleted_at`` lives on an ancestor, revives that ancestor's row."""
        related_table = related_model._meta.db_table
        target = column_owner(related_model, '_deleted_at')
        joined = not owns_column(related_model, '_deleted_at')
        slots = {
            'related_table': _identifiers._quote_table(related_table),
            'primary_key': ident_owner_pk,
            'foreign_key': _identifiers._escape_ident(column),
            # This runs at trigger depth 1, where ``updated_at_trigger``'s ``WHEN`` suppresses
            # it, so the column has to move here or it moves on neither path.
            'updated_at_assignment': (
                _soft_delete._SOFT_DELETE_REVIVE_UPDATED_AT
                if owns_column(target, '_updated_at')
                else ''
            ),
        }
        if joined:
            slots |= {
                'target_table': _identifiers._quote_table(target._meta.db_table),
                'target_pk': _identifiers._escape_ident(cast(str, target._meta.pk.column)),
                'child_pk': _identifiers._escape_ident(
                    cast(str, _parent_link(related_model, target).column)
                ),
            }
        if any('$$' in rendered for rendered in slots.values()):
            if quiet:
                return None
            self._skipped_rule_notes.append(
                f"Revive arm for '{key[1]}' -> '{related_table}' skipped: a table or column it "
                'names contains "$$", which closes the dollar quoting its trigger function '
                'depends on. Set a db_table / db_column without it.'
            )
            return None
        template = (
            _soft_delete._SOFT_DELETE_REVIVE_ARM_JOINED
            if joined
            else _soft_delete._SOFT_DELETE_REVIVE_ARM
        )
        return template.format(**slots)

    def _revive_owner_form(self, slots: dict, owner_table: str, *, unrenamed: str) -> str:
        """:meth:`_self_cascade_form` for the per-owner revive: the name spells the owner
        alone, so only the owner's rename leaves a prior spelling to drop."""
        if not self._renamed(owner_table):
            return unrenamed.format(**slots)
        return self._drop_prior_triggers(
            slots, [_revive_owner_name(name) for name in self._prior_names(owner_table)]
        ) + _soft_delete._ADOPT_SOFT_DELETE_REVIVE_OWNER.format(**slots)

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
        candidates, self_cascades = self._cascade_candidates(model, owner_table)
        for related_model, fk_field, is_primary in candidates:
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
            # A joined rule stamps the ancestor and reads the descendant's parent link, so it
            # names the ancestor's table and the link column too -- on neither of which the key
            # alone says anything. ``target`` is ``related_model`` itself for the flat form.
            joined = not owns_column(related_model, '_deleted_at')
            target = column_owner(related_model, '_deleted_at')
            if joined:
                self._record_object_ref(
                    model, related_model, _parent_link(related_model, target).name
                )
            # ``SET _deleted_at`` is a second column, and not necessarily as old as the table:
            # a model promoted to ``SetarModel`` gains it in a later migration, and an edge to
            # the creation alone would let the rule be created before the column exists.
            self._record_object_ref(model, target, '_deleted_at')
            # And, where this key was retired before, the drop that retired it: a re-adopted
            # create reaching a fresh database first leaves the rule dropped and never rebuilt.
            self._record_readoption_edge(model._meta.app_label, key)
            # The relation, not `key`: `key` drops the column on the plain form, and two
            # relations can then share one key -- see `_claim_rule_name`'s docstring.
            self._claim_rule_name(
                owner_table, rule_name, (related_table, owner_table, fk_field.column)
            )
            # One template pair for both cases -- see soft_delete.py's private
            # _CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE for why the public, frozen constants
            # of the same name (the old rule_name-less signature) aren't used here.
            if joined:
                forward = _soft_delete._CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE_JOINED.format(
                    rule_name=rule_name,
                    table=ident_owner_table,
                    related_table=ident_related_table,
                    primary_key=ident_owner_pk,
                    foreign_key=ident_foreign_key,
                    target_table=_identifiers._quote_table(target._meta.db_table),
                    target_pk=_identifiers._escape_ident(cast(str, target._meta.pk.column)),
                    child_pk=_identifiers._escape_ident(
                        cast(str, _parent_link(related_model, target).column)
                    ),
                )
            else:
                forward = _soft_delete._CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE.format(
                    rule_name=rule_name,
                    table=ident_owner_table,
                    related_table=ident_related_table,
                    primary_key=ident_owner_pk,
                    foreign_key=ident_foreign_key,
                )
            # A rule's name embeds the child's table, so a rename leaves the carried-over rule
            # live beside the new one -- both cascading, and nothing later retires either.
            replace = forward
            if self._renamed(related_table):
                replace = (
                    self._drop_prior_rules(
                        ident_owner_table,
                        [
                            _related_rule_name(name, None if is_primary else fk_field.column)
                            for name in self._prior_names(related_table)
                        ],
                    )
                    + forward
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
                replace=replace,
            )
        for fk_field in self_cascades:
            self._self_cascade_operation(
                ops,
                owner=owner,
                owner_table=owner_table,
                header_owner_table=header_owner_table,
                ident_owner_table=ident_owner_table,
                ident_owner_pk=ident_owner_pk,
                foreign_key=fk_field.column,
                adopt=adopt,
                app_label=model._meta.app_label,
            )
        return ops

    def _self_cascade_operation(
        self,
        ops: list[str],
        *,
        owner: type[models.Model],
        owner_table: str,
        header_owner_table: str,
        ident_owner_table: str,
        ident_owner_pk: str,
        foreign_key: str,
        adopt: bool,
        app_label: str,
    ) -> None:
        """The statement-level trigger a self-referential CASCADE FK takes in place of the rule
        the loop above emits (ADR 0018). Appended from inside :meth:`_cascade_operations`, after
        the same refusals -- so which self keys carry a trigger *is* which would carry a rule."""
        # No object refs: CREATE TRIGGER names only the table it fires on and plpgsql resolves
        # no body at CREATE FUNCTION time. An MTI descendant's key at its root does reach here,
        # from the descendant's pass and app, so its retirement depends on this migration (#66).
        name = _self_cascade_name(owner_table, foreign_key)
        ident_foreign_key = _identifiers._escape_ident(foreign_key)
        slots = {
            'function': name,
            'trigger': name,
            'table': ident_owner_table,
            'primary_key': ident_owner_pk,
            'foreign_key': ident_foreign_key,
            # The sweep's reason: this UPDATE runs at trigger depth >= 1, where
            # ``updated_at_trigger``'s ``WHEN`` suppresses it. Conditional because a model can
            # carry ``_deleted_at`` with no ``_updated_at`` -- not the MTI shape, E003 refuses it.
            'updated_at_assignment': (
                _soft_delete._SOFT_DELETE_SELF_CASCADE_UPDATED_AT
                if owns_column(owner, '_updated_at')
                else ''
            ),
        }
        key = (owner_table, foreign_key)
        # Refused rather than escaped, exactly as the owned sweep refuses it: an identifier
        # admits '$', so a db_table like 'a$$b' would close this template's dollar quoting
        # early and the generated migration would fail `migrate` with a bare syntax error.
        for slot, rendered in slots.items():
            if '$$' in rendered:
                self._skipped_rule_notes.append(
                    f"Self cascade trigger for '{owner_table}.{foreign_key}' skipped: the "
                    f'{slot} {rendered!r} contains "$$", which closes the dollar quoting this '
                    f'trigger function depends on -- the generated migration would not apply. '
                    f'Set a db_table / db_column without it.'
                )
                if key in self.existing.soft_delete_self_cascade:
                    self._refusals_over_live_rules.append(
                        f"Self cascade trigger on '{owner_table}' via '{foreign_key}' is "
                        "refused but already exists in this project's migrations. It is still "
                        'live in any migrated database. Drop it by hand: DROP TRIGGER '
                        f'{name} ON {ident_owner_table}; DROP FUNCTION {name}();'
                    )
                return
        # Claimed only once it is really emitted, as the sweep's name is: a refused trigger
        # holding its name would report a clash against the one relation that does reach it.
        self._claim_sweep_function_name(
            name, (owner_table, owner_table, foreign_key), kind='Self cascade trigger'
        )
        # Against the retirement it revives, which the table's app hosts while this create
        # can land in an MTI descendant's: unordered, a fresh migrate runs it first (ADR 0021).
        self._record_readoption_edge(
            app_label,
            key,
            self.existing.soft_delete_self_cascade,
            self.existing.self_cascade_retirement_sites,
        )
        self._append_if_stale(
            ops,
            self.existing.soft_delete_self_cascade,
            key,
            HEADER_SOFT_DELETE_SELF_CASCADE.format(
                table=header_owner_table, foreign_key=ident_foreign_key
            ),
            _soft_delete._CREATE_SOFT_DELETE_SELF_CASCADE.format(**slots),
            _soft_delete._DROP_SOFT_DELETE_SELF_CASCADE.format(**slots),
            replace=self._self_cascade_form(
                slots,
                owner_table,
                foreign_key,
                unrenamed=_soft_delete._REPLACE_SOFT_DELETE_SELF_CASCADE,
            ),
            adopt=self._self_cascade_form(
                slots,
                owner_table,
                foreign_key,
                unrenamed=_soft_delete._ADOPT_SOFT_DELETE_SELF_CASCADE,
            ),
            is_adopt=adopt,
        )

    @staticmethod
    def _is_owned_candidate(model: type[models.Model], fk_field: models.Field) -> bool:
        """Whether this outbound FK gets an owned soft-delete rule -- the mirror of
        :func:`~guitars.introspection.is_cascade_candidate`, read off the declaration rather than ``on_delete``,
        which describes the opposite direction and cannot express ownership."""
        # Spelled out again in ``introspection.owner_arms`` and ``owned_tenancy_refusals``,
        # which cannot call this without losing the ``isinstance`` narrowing ``ty`` reads
        # ``field.column``/``related_model`` through. A clause added here belongs in both.
        return (
            isinstance(fk_field, OwningForeignKey)
            # An FK reached through MTI is the same physical column on the ancestor's table,
            # covered by that ancestor's own pass -- as in is_cascade_candidate.
            and fk_field.model is model
            # Both ends again: the rule fires on the owner's table and its action stamps the
            # dependent's, so either one routed off PostgreSQL leaves it unwritable.
            and migrates_to_postgresql(model)
            and migrates_to_postgresql(fk_field.related_model)
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
            # The owned rule's name embeds the *dependent's* table, so a rename there leaves
            # the carried-over rule live beside the new one -- both cascading, neither retired,
            # and the stale one frozen at the old predicate.
            owned_replace = forward
            if self._renamed(dependent_table):
                owned_replace = (
                    self._drop_prior_rules(
                        ident_owner_table,
                        [
                            _owned_rule_name(name, fk_field.column)
                            for name in self._prior_names(dependent_table)
                        ],
                    )
                    + forward
                )
            # Each half against the retirement it revives, as the cascade pair does (ADR 0021).
            for recorded, sites in (
                (self.existing.soft_delete_owned, self.existing.owned_retirement_sites),
                (
                    self.existing.soft_delete_owned_sweep,
                    self.existing.owned_sweep_retirement_sites,
                ),
            ):
                self._record_readoption_edge(model._meta.app_label, key, recorded, sites)
            self._append_if_stale(
                ops,
                self.existing.soft_delete_owned,
                key,
                header,
                forward,
                reverse,
                is_adopt=adopt,
                replace=owned_replace,
            )
            self._append_owned_sweep(
                ops,
                key=key,
                dependent=dependent,
                co_owners=co_owners,
                owner_table=owner_table,
                header_owner_table=header_owner_table,
                ident_owner_table=ident_owner_table,
                ident_owner_pk=ident_owner_pk,
                ident_foreign_key=ident_foreign_key,
                foreign_key=fk_field.column,
                adopt=adopt,
            )
        return ops

    def _append_owned_sweep(
        self,
        ops: list[str],
        *,
        key: tuple[str, str, str],
        dependent: type[models.Model],
        co_owners: list[OwnerArm],
        owner_table: str,
        header_owner_table: str,
        ident_owner_table: str,
        ident_owner_pk: str,
        ident_foreign_key: str,
        foreign_key: str,
        adopt: bool,
    ) -> None:
        """The statement-level companion to the rule just emitted (ADR 0014), appended from
        inside :meth:`_owned_operations` after every refusal that loop applies and against the
        same *key* -- so which relations carry a sweep *is* which carry a rule."""
        # No object refs of its own: CREATE TRIGGER names only the table it fires on, and
        # plpgsql does not resolve a body at CREATE FUNCTION time, so nothing here is a
        # parse-time reference. The rule's refs, recorded above, order the runtime case.
        dependent_table = key[0]
        name = _owned_sweep_name(owner_table, dependent_table, foreign_key)
        slots = {
            'function': name,
            'trigger': name,
            'table': ident_owner_table,
            'dependent_table': _identifiers._quote_table(dependent_table),
            'dependent_primary_key': _identifiers._escape_ident(
                cast(str, dependent._meta.pk.column)
            ),
            'primary_key': ident_owner_pk,
            'foreign_key': ident_foreign_key,
            # Stamped here rather than left to the dependent's own trigger: that one carries
            # ``WHEN (pg_trigger_depth() = 0)`` and this UPDATE runs at depth 1, so without
            # this the column moves on the rule's path and not on this one.
            'updated_at_assignment': (
                _soft_delete._SOFT_DELETE_OWNED_SWEEP_UPDATED_AT
                if owns_column(dependent, '_updated_at')
                else ''
            ),
            # The same rendered arms the rule carries, correlated against this form's alias
            # instead of ``old`` -- a plpgsql record variable a table alias may not shadow.
            'co_owner_guards': self._owned_co_owner_guards(
                co_owners,
                owner_table,
                ident_owner_pk,
                ident_foreign_key,
                dependent_table,
                _identifiers._escape_ident(cast(str, dependent._meta.pk.column)),
                owner_row='guitars_archived',
            ),
            # The same arms again for the pk-rewrite guard, which asks the UPDATE's question
            # over the *vanished* rows: asking it without them refused a statement whose
            # dependent a co-owner on another column, or another table, still held.
            'guard_co_owner_guards': self._owned_co_owner_guards(
                co_owners,
                owner_table,
                ident_owner_pk,
                ident_foreign_key,
                dependent_table,
                _identifiers._escape_ident(cast(str, dependent._meta.pk.column)),
                owner_row='guitars_before',
            ),
        }
        # Refused rather than escaped, as the autofill slots refuse it: an identifier admits
        # '$', so a db_table like 'a$$b' would close this template's dollar quoting early and
        # the generated migration would fail `migrate` with a bare syntax error.
        for slot, rendered in slots.items():
            if '$$' in rendered:
                # No key: ``_refuse_owned``'s escalation would name the *rule* the loop above
                # just emitted. Only the sweep is refused, so only a recorded sweep is stale,
                # and that is escalated on its own below.
                self._refuse_owned(
                    None,
                    f"Owned sweep for '{owner_table}.{foreign_key}' -> '{dependent_table}' "
                    f'skipped: the {slot} {rendered!r} contains "$$", which closes the dollar '
                    f'quoting this trigger function depends on -- the generated migration '
                    f'would not apply. Set a db_table / db_column without it.',
                )
                if key in self.existing.soft_delete_owned_sweep:
                    self._refusals_over_live_rules.append(
                        f"Owned sweep on '{dependent_table}' owned by '{owner_table}' via "
                        f"'{foreign_key}' is refused but already exists in this project's "
                        'migrations. It is still live in any migrated database and running a '
                        f'stale predicate. Drop it by hand: DROP TRIGGER {name} ON '
                        f'{ident_owner_table}; DROP FUNCTION {name}();'
                    )
                return
        # Claimed only once it is really emitted: a refused sweep holding its name would
        # report a clash against the one relation that does reach it.
        self._claim_sweep_function_name(name, key)
        self._append_if_stale(
            ops,
            self.existing.soft_delete_owned_sweep,
            key,
            HEADER_SOFT_DELETE_OWNED_SWEEP.format(
                dependent_table=_identifiers._escape_ident(dependent_table),
                table=header_owner_table,
                foreign_key=ident_foreign_key,
            ),
            _soft_delete._CREATE_SOFT_DELETE_OWNED_SWEEP.format(**slots),
            _soft_delete._DROP_SOFT_DELETE_OWNED_SWEEP.format(**slots),
            replace=self._owned_sweep_form(
                slots,
                owner_table,
                dependent_table,
                foreign_key,
                unrenamed=_soft_delete._REPLACE_SOFT_DELETE_OWNED_SWEEP,
            ),
            adopt=self._owned_sweep_form(
                slots,
                owner_table,
                dependent_table,
                foreign_key,
                unrenamed=_soft_delete._ADOPT_SOFT_DELETE_OWNED_SWEEP,
            ),
            is_adopt=adopt,
        )

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
        owner_row: str = 'old',
    ) -> str:
        """The rendered co-owner arms, or ``''`` where the dependent is owned from exactly one
        place -- which is what makes that case byte-identical to 2.3.0. Aliases are numbered
        from 1, arm 0 keeping the literal ``guitars_owner`` the template spells out."""
        # *owner_row* names the row each arm reads the target's key off; defaulting to ``old``
        # keeps every rule's SQL, and its ``[SQL:...]``, where 2.4.0 left it.
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
                    alias=excluded_alias, primary_key=ident_owner_pk, owner_row=owner_row
                )
            elif excluded == dependent_table:
                # An arm taking liveness from the dependent's own table -- a target owning
                # itself, or an MTI child of it owning it back. The row the rule stamps must not
                # count as its own live owner, or nothing archives it. Named by the key.
                self_exclusion = _soft_delete._SOFT_DELETE_OWNED_CO_OWNER_TARGET_EXCLUSION.format(
                    alias=excluded_alias,
                    primary_key=ident_dependent_pk,
                    foreign_key=ident_declared_foreign_key,
                    owner_row=owner_row,
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
                'owner_row': owner_row,
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
                # Routed away: no run emits this rule, so reporting a scoped run's *gap* in it
                # sends the reader to add an app that would still write nothing.
                if not has_column(model, '_deleted_at') or not migrates_to_postgresql(model):
                    continue
                # The rule lives on the table that owns _deleted_at (the model itself, or its
                # MTI ancestor), matching where `_cascade_operations` places it.
                table = column_owner(model, '_deleted_at')._meta.db_table
                # Shared with _cascade_operations, which is what makes "closed by a later run
                # naming the parent's app" a promise this check can actually verify: the two
                # must agree on both which FKs count and which dedupe key each one uses.

                # Self keys dropped: that trigger lands in the app being scanned, so a scoped
                # run has nothing to report for it that this note's cascade-rule gap covers.
                candidates, _self_cascades = self._cascade_candidates(model, table)
                for related_model, fk_field, is_primary in candidates:
                    if model_app_label.get(related_model) not in requested:
                        continue
                    related_table = related_model._meta.db_table
                    key = (related_table, table, None if is_primary else fk_field.column)
                    # The cascade alone: since 2.16.0 the inverse is the owner's one trigger, whose
                    # host need not be this app -- ``_scoped_revive_notes`` names it, by digest.
                    if key in self.existing.soft_delete_related:
                        continue
                    notes.append(
                        f"Cascade rule on '{related_table}' related to '{table}' skipped: parent "
                        f"app '{app.label}' is not in this scoped run."
                    )
        return notes + self._scoped_cascade_retirement_notes(requested)

    def _scoped_cascade_retirement_notes(self, requested: set[str]) -> list[str]:
        """The other direction, and the dangerous half to leave silent: a rule the models no
        longer call for, whose *owner's* app is out of the run. The creation gap merely delays a
        rule; this one leaves a live rule still archiving rows, with ``--check`` green."""
        hosting = self._table_app_labels()
        required, _models = self._cascade_key_maps()
        notes = []
        # Both families, deduped by key: one out-of-scope owner leaves one live cascade, and
        # naming it twice would read as two rules still archiving rows.
        recorded = set(self.existing.soft_delete_related) | set(self.existing.soft_delete_revive)
        for key in sorted(recorded - set(required), key=lambda k: (k[0], k[1], k[2] or '')):
            related_table, owner_table, via = key
            owner_app = hosting.get(owner_table)
            if related_table not in hosting:
                # Only where a revive was recorded: before 2.11.0 there is none, and nothing
                # is broken. Named per trigger, so two keys to one owner read as two.
                if related_table not in self._dropped_tables():
                    continue
                if key in self.existing.soft_delete_revive:
                    if owner_app is not None and owner_app not in requested:
                        notes.append(
                            f'Revive trigger {_revive_name(owner_table, related_table, via)} on '
                            f"'{owner_table}' names '{related_table}', whose model was deleted, "
                            'and fails every UPDATE on that table until it is retired -- which '
                            f"only a run including '{owner_app}' writes."
                        )
                # The owner's one trigger (#70) carries the arm instead, failing only an UPDATE
                # that revives a row, the early exit sparing the rest. Its own host writes it,
                # the app that created it (ADR 0033), which need not be the table's.
                elif (owner_table,) in self.existing.soft_delete_revive_owner:
                    revive_app = self._revive_host(owner_table)
                    if revive_app is not None and revive_app not in requested:
                        notes.append(
                            f'Revive trigger {_revive_owner_name(owner_table)} on '
                            f"'{owner_table}' names '{related_table}', whose model was deleted, "
                            'and fails every UPDATE reviving a row there until it is re-emitted '
                            f"or retired -- which only a run including '{revive_app}' writes."
                        )
                continue
            if owner_app is None or owner_app in requested:
                continue
            notes.append(
                f"Cascade rule on '{owner_table}' related to '{related_table}' is recorded but "
                f'the models no longer call for it, and it cannot be retired here: its app '
                f"'{owner_app}' is not in this scoped run. Until a run includes that app the "
                f'rule stays live and goes on archiving rows.'
            )
        return notes

    def _scoped_trigger_retirement_notes(self, requested: set[str]) -> list[str]:
        """#66's retirements a scoped run leaves unwritten because the table they fire on is
        hosted by an app outside it: each trigger named, since it fails every UPDATE there."""
        # An unscoped run writes these itself; only a scoped one can leave them behind.
        if not requested:
            return []
        hosting = self._table_app_labels()
        undeclared = (
            set(self.existing.soft_delete_owned) | set(self.existing.soft_delete_owned_sweep)
        ) - self._declared_owned_keys()
        unrequired = set(self.existing.soft_delete_self_cascade) - self._required_self_cascades()
        fires_on = [(key, key[1]) for key in sorted(undeclared)] + [
            (key, key[0]) for key in sorted(unrequired)
        ]
        return [
            f"Enforcement on '{table}' for {key} is recorded but the models no longer call for "
            f"it, and it cannot be retired here: its app '{hosting[table]}' is not in this "
            f'scoped run. Until a run includes that app it may fail every UPDATE on the table.'
            for key, table in fires_on
            if hosting.get(table) not in (None, *requested)
        ] + self._scoped_revive_notes(requested)

    def _scoped_revive_notes(self, requested: set[str]) -> list[str]:
        """An owner's revive trigger a scoped run leaves missing, stale or unretired because its
        app is out of scope (#70): its arms can come from the apps in scope, an MTI descendant's
        key above all, and nothing else this run writes would say so."""
        notes = []
        owed = self._revive_arms_by_owner()
        for owner_table in sorted(
            set(owed) | {key[0] for key in self.existing.soft_delete_revive_owner}
        ):
            recorded = self.existing.soft_delete_revive_owner
            if owner_table in owed:
                host = self._revive_host(owner_table)
                slots = self._revive_owner_slots(owner_table, quiet=True)
                current = (
                    None
                    if slots is None
                    else _sql_digest(
                        _soft_delete._CREATE_SOFT_DELETE_REVIVE_OWNER.format(**slots),
                        _soft_delete._DROP_SOFT_DELETE_REVIVE.format(**slots),
                    )
                )
                if current is None or recorded.get((owner_table,), '') == current:
                    continue
                state = 'missing' if (owner_table,) not in recorded else 'out of date'
            else:
                host = self._revive_host(owner_table)
                state = 'no longer called for'
            if host is None or host in requested:
                continue
            notes.append(
                f"Revive trigger {_revive_owner_name(owner_table)} on '{owner_table}' is {state}, "
                f"and only a run including '{host}' writes it: until then a revive there may "
                'leave children archived, or fail on an arm naming a column or table now gone.'
            )
        return notes

    def _migration_loader(self) -> MigrationLoader:
        """The project's migration graph, built at most once between writes. Building one imports
        every migration module in the project, and both readers below ask one question per app --
        so a per-call build squares that sweep against the local-app count."""
        if self._loader_cache is None:
            self._loader_cache = MigrationLoader(None, ignore_no_migrations=True)
        return self._loader_cache

    def _dropped_tables(self) -> dict[str, tuple[str, str]]:
        """:func:`dropped_tables` over :meth:`_migration_loader`, recomputed with it, less any
        table a model of an app with no migrations holds -- migration state cannot see those."""
        loader = self._migration_loader()
        if self._dropped_tables_cache is None or self._dropped_tables_cache[0] is not loader:
            live = {model._meta.db_table for model in django_apps.get_models()}
            self._dropped_tables_cache = (
                loader,
                {
                    table: node
                    for table, node in dropped_tables(loader).items()
                    if table not in live
                },
            )
        return self._dropped_tables_cache[1]

    def _drop_cached_migration_loader(self) -> None:
        """Forget the graph after writing a migration file. The file carries edges into other
        apps, so it moves what the *next* app's reachability question answers -- the scaffold
        write itself needs no drop, a ref always resolving in an app the new node is not in."""
        self._loader_cache = None

    def _dependencies_for(self, app: AppConfig, operations_blob: str) -> list[tuple[str, str]]:
        """Every edge *app*'s new migration needs: the shared-function ones read off the
        operation headers, one per object the rules name in another app, and one per rule a
        retirement drops that another app created. In that order, so an old file reads the same."""
        edges = self._function_dependencies_for(operations_blob) + self._object_dependencies_for(
            app
        )
        retirement = [
            edge for edge in self._retired_cascade_dependencies_for(app) if edge not in edges
        ]
        if not retirement:
            return edges
        # ``drop_implied_edges`` over the union but keeping only its verdict on the new ones: the
        # two halves above keep the answer they had, and a retirement edge the file already
        # reaches says nothing the graph did not. ADR 0013's "an implied edge is dropped".
        kept = set(drop_implied_edges(self._migration_loader(), edges + retirement))
        return edges + [edge for edge in retirement if edge in kept]

    def _retired_cascade_dependencies_for(self, app: AppConfig) -> list[tuple[str, str]]:
        """Edges to the migrations that created the rules *app*'s retirements drop. A ``DROP
        RULE`` reaching a fresh database before its ``CREATE`` aborts ``migrate``; under
        ``--adopt``'s ``IF EXISTS`` it leaves the rule live instead, which is worse."""
        edges = self._retirement_edges.get(app.label, [])
        if not edges:
            return []
        loader = self._migration_loader()
        resolved = []
        for edge in edges:
            if edge in loader.graph.node_map:
                resolved.append(edge)
                continue
            # Warned, not refused, as an unresolvable object reference is: the scan read this
            # node off a header, and a squash since then can have replaced the file.
            self._unresolved_reference_notes.append(
                f"Enforcement migration for '{app.label}' retires a cascade rule created by "
                f"'{edge[0]}.{edge[1]}', which is not in the migration graph, so no dependency "
                'edge was emitted. A fresh `migrate` may reach the drop first. Add the edge by '
                'hand against whatever replaced that migration.'
            )
        return resolved

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
        loader = self._migration_loader()
        edges, unresolved = resolve_dependencies(loader, refs, own_app=app.label)
        for ref in unresolved:
            # Warned, not refused: an app with no migrations at all is a legitimate
            # configuration, and withdrawing a rule that works today would be the worse trade.
            # Its own list, not the skipped-rule one: the rule *was* emitted.
            self._unresolved_reference_notes.append(
                f"Enforcement migration for '{app.label}' references '{ref.describe()}', but no "
                'migration in that app creates it, so no dependency edge was emitted. A fresh '
                '`migrate` may reach the rule first. Add the edge by hand if that app is '
                'migrated elsewhere.'
            )
        return drop_implied_edges(loader, edges)

    def _missing_retirement_edge_notes(self, requested: set[str]) -> list[str]:
        """Cascade retirements already written that nothing orders against the migration
        creating the rule they drop. Once per run, not per app: the emitter never rewrites a
        file the digest guard skips, so for those histories this note is the only channel."""
        # Scoped like every other refusal: the scan reads all of LOCAL_APPS, and a per-app CI
        # job going red over a file it was not asked about has no fix available from that job.

        # Both families, each paired with the minter that spells *its* rule: a site carries
        # no family of its own, and the two resolve to different creates, so one shared minter
        # sends the reader grepping for a name the `migrate` failure never prints.
        sites = [
            (site, name)
            for site, name in (
                *(
                    (site, lambda _owner, related, via: _related_rule_name(related, via))
                    for site in self.existing.cascade_retirement_sites
                ),
                *((site, _revive_name) for site in self.existing.revive_retirement_sites),
            )
            if not requested or site.app_label in requested
        ]
        if not sites:
            return []
        loader = self._migration_loader()
        notes: list[str] = []
        for site, rule_name_for in sites:
            # Resolved by the scan, per site: the newest create the graph puts *before* this
            # drop. Naming the newest create outright would, after a re-adoption, print a tuple
            # ordering this drop after the rule it revives, destroying it on every database.
            created = site.created
            node = (site.app_label, site.migration)
            if (
                created is None
                or created[0] == site.app_label
                or created not in loader.graph.node_map
                or node not in loader.graph.node_map
                # Reachability, as ``_missing_edge_notes`` asks it: an ordering guaranteed
                # through another path is guaranteed. And the reverse, which Django rejects
                # outright, so reporting it would be red with no move that clears it.
                or created in set(loader.graph.forwards_plan(node))
                or node in set(loader.graph.forwards_plan(created))
            ):
                continue
            # Already quoted by ``_safe_ident``: quoting it again prints a name no `migrate`
            # log carries, and the whole point of the sentence is that it can be grepped for.
            rule_name = rule_name_for(site.key[1], site.key[0], site.key[2])
            # A renamed *related* table makes that drop ``IF EXISTS`` over every prior
            # spelling -- ``_retired_cascade_operations`` checks that table alone, an owner
            # rename playing no part -- so it no-ops instead of aborting, and the rule stays live.
            symptom = (
                'a fresh `migrate` reaches the drop before that create, and the drop being '
                '`IF EXISTS` over the names this table has held, it silently does nothing and '
                'leaves the rule live'
                if self._renamed(site.key[0])
                else f'a fresh `migrate` reaches the drop first and fails with `rule '
                f'{rule_name} for relation "{site.key[1]}" does not exist`'
            )
            notes.append(
                f"Enforcement migration '{site.app_label}.{site.migration}' drops the cascade "
                f"rule on '{site.key[1]}' related to '{site.key[0]}', but nothing orders it "
                f"after '{created[0]}.{created[1]}', which creates that rule -- {symptom}. "
                f'Add to its dependencies:\n'
                f"        ('{created[0]}', '{created[1]}'),"
            )
        return notes

    def _missing_edge_notes(self, app: AppConfig) -> list[str]:
        """Enforcement migrations of *app* that name another app's table without being ordered
        against whatever creates it. Reachability, not a literal edge: an ordering already
        guaranteed through another path is guaranteed, and flagging it would be a false alarm."""
        # Cross-app refs only, and filtered before the loader is built for the reason
        # ``_object_dependencies_for`` gives -- a single-app project builds none.
        refs = [ref for ref in self._object_refs.get(app.label, []) if ref.app_label != app.label]
        if not refs:
            return []
        loader = self._migration_loader()
        notes: list[str] = []
        for ref in refs:
            edge = resolve_object_migration(loader, ref)
            table = self._ref_table(ref)
            if edge is None or table is None:
                continue
            column = self._ref_column(ref)
            # Everything *edge* already depends on: pasting the edge onto one of those is the
            # one shape that genuinely cycles, and Django rejects such a graph outright -- so
            # reporting it would be red with no move that clears it.
            behind_edge = (
                set(loader.graph.forwards_plan(edge)) if edge in loader.graph.node_map else set()
            )
            for path, content in _generator.iter_migration_files(app):
                # Only a migration whose SQL actually names the table: refs are collected per
                # app, and an app's earlier enforcement migration may predate the rule needing
                # this one. ``_quote_table`` renders it as the rule does, so membership is exact.
                node = (app.label, path.stem)
                if (
                    not _generator.RE_DIGEST.search(content)
                    or not self._names_table(content, table)
                    # ...and the column, where the ref names one: two refs can share a table
                    # and resolve to *different* migrations, and matching the table alone then
                    # reports the older file forever. Unquoted -- the bare name is in both.
                    or (column is not None and column not in content)
                    or node not in loader.graph.node_map
                    or node in behind_edge
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
    def _names_table(content: str, table: str) -> bool:
        """Whether *content*'s SQL names *table*, in either form the kit emits. A rule renders
        ``_quote_table`` -- quoted per part, so membership is exact; a policy's owner join
        renders ``policy._qualified_table``, which leaves a bare name unquoted."""
        if _identifiers._quote_table(table) in content:
            return True
        try:
            bare = _policy._qualified_table(table)
        except ValueError:
            # A ``db_table`` no policy could spell unquoted: the quoted form above is then the
            # only one emitted, so the answer is no rather than a crashed report. ``_quote_table``
            # raises for its own shapes uncaught -- rendering such a rule raises before this.
            return False
        # Delimited by SQL, never by a quote: a bare ``shop`` is a substring of ``shopping``,
        # sits inside a ``('shop', '0001_initial')`` dependency tuple, and is the escaped
        # literal an MTI ``updated_at`` trigger re-quotes at fire time -- none of those name it.
        edge = r'[\w."\']'
        return re.search(rf'(?<!{edge}){re.escape(bare)}(?!{edge})', content) is not None

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
            waive_digest_guard = not adopt and app.label in self.existing.retirement_apps
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
            self._drop_cached_migration_loader()

            self.stdout.write(
                self.style.MIGRATE_HEADING(f"Enforcement migrations for '{app.label}':")
            )
            self.stdout.write(f'  migrations/{migration_file}')
            changes_made = True

        return changes_made, check_missing
