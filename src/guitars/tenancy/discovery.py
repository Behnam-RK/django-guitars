"""Which tables carry a ``tenant_scope`` policy, and how -- one answer, two consumers:
``makeguitarmigrations`` builds migrations from it, ``audittenancy`` compares it against a
live database. See ADR-0003 and ``docs/tenancy.md`` for MTI and reported-not-covered cases."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, TypedDict

from django.apps import apps as django_apps

from guitars.gucs import BYPASS_GUC, guc_name
from guitars.introspection import column_owner, owns_column
from guitars.local_apps import is_local

from .spec import _meta, local_tenant_fields, tenant_spec


if TYPE_CHECKING:
    from django.apps import AppConfig
    from django.db import models


__all__ = [
    'Coverage',
    'PolicyKwargs',
    'TableCoverage',
    'app_coverage',
    'expected_coverage',
    'is_local',
]


class PolicyKwargs(TypedDict, total=False):
    """The shape :meth:`TableCoverage.as_kwargs` produces -- spelled out, not
    ``dict[str, object]``, since the generator *calls* ``sql.create_table_rls(**kwargs)``.
    ``total=False``: owner keys are absent, not ``None``, when there's no owner join."""

    columns: dict[str, str]
    owner_table: str | None
    owner_pk: str | None
    child_pk: str | None
    owner_columns: dict[str, str] | None


class TableCoverage(NamedTuple):
    """How one table's policy should be predicated. ``columns`` maps dimension -> column on
    this table; when the tenant column lives on an MTI ancestor, ``owner_*`` describes the
    join and ``owner_columns`` maps dimension -> column there. A model may have both."""

    columns: dict[str, str]
    owner_table: str | None = None
    owner_pk: str | None = None
    child_pk: str | None = None
    owner_columns: dict[str, str] | None = None

    def as_kwargs(self) -> PolicyKwargs:
        """The keyword arguments ``guitars.sql.create_table_rls`` expects -- owner keys
        omitted entirely when there's no owner join, so a non-MTI migration stays simple."""
        kwargs: PolicyKwargs = {'columns': dict(self.columns)}
        if self.owner_columns:
            kwargs['owner_table'] = self.owner_table
            kwargs['owner_pk'] = self.owner_pk
            kwargs['child_pk'] = self.child_pk
            kwargs['owner_columns'] = dict(self.owner_columns)
        return kwargs

    # ``audittenancy`` compares a *live* policy against the two facts below, not SQL text:
    # Postgres rewrites a stored policy expression, so string equality can never hold.
    # These two survive that rewrite, living beside ``as_kwargs`` for one shared description.

    def policy_gucs(self) -> frozenset[str]:
        """Session-setting names the emitted predicate reads -- recoverable from a stored
        policy because ``current_setting('tenant.x', true)`` survives Postgres's rewrite
        verbatim. A dimension added or removed changes this set, making drift visible."""
        dimensions = set(self.columns) | set(self.owner_columns or {})
        return frozenset({BYPASS_GUC, *(guc_name(dimension) for dimension in dimensions)})

    def policy_columns(self, table: str) -> frozenset[tuple[str, str]]:
        """``(table, column)`` pairs the emitted predicate references -- read from
        ``pg_depend``, not parsed. *table* is a param, not a field, to avoid drifting from
        ``Coverage.tables``'s own key."""
        pairs = {(table, column) for column in self.columns.values()}
        owner_table, owner_pk, child_pk = self.owner_table, self.owner_pk, self.child_pk
        # Same invariant ``sql.policy._predicate`` enforces (raises without them), so a
        # coverage that failed it could never have produced a policy to compare against.
        if self.owner_columns and owner_table and owner_pk and child_pk:
            # The correlated subquery joins on both keys and reads the ancestor's columns.
            pairs.add((table, child_pk))
            pairs.add((owner_table, owner_pk))
            pairs |= {(owner_table, column) for column in self.owner_columns.values()}
        return frozenset(pairs)


class Coverage(NamedTuple):
    """What the models say the database should look like. ``notes`` carries the
    human-readable reason for every model left out, or only partially covered."""

    tables: dict[str, TableCoverage]
    notes: list[str]


def _classify(model: type[models.Model]) -> tuple[TableCoverage | None, list[str]]:
    """Split a model's tenant dimensions into own-table and ancestor-held, or explain why not."""
    spec = tenant_spec(model)
    notes: list[str] = []

    own: dict[str, str] = {}
    by_owner: dict[type[models.Model], dict[str, str]] = {}

    local = local_tenant_fields(model)
    for dimension, field_name in local.items():
        column = _meta(model).get_field(field_name).column
        if owns_column(model, field_name):
            own[dimension] = column
        else:
            # MTI: resolve the OWNER, not the immediate parent -- in a chain three deep
            # the column may live two tables up.
            by_owner.setdefault(column_owner(model, field_name), {})[dimension] = column

    # Only when something *is* covered. A model whose every dimension is multi-hop gets the
    # skip note below instead -- two notes for one fact, one naming the dimension and one the
    # lookup, read as two separate problems.
    if local and len(spec) > len(local):
        uncovered = sorted(
            f'{dimension} ({spec[dimension]})' for dimension in set(spec) - set(local)
        )
        notes.append(
            f"'{_meta(model).db_table}': dimensions {uncovered} traverse a relation and are "
            f'not covered by its policy, which enforces {sorted(local)} '
            f'(Python scoping still applies to all of them).'
        )

    if len(by_owner) > 1:
        owners = sorted(_meta(owner).db_table for owner in by_owner)
        dropped = sorted(dim for columns in by_owner.values() for dim in columns)
        # Dropping ALL rather than picking one ancestor arbitrarily -- which one it reached
        # would depend on field declaration order, worse than a named gap. See ADR-0003.
        remaining = (
            f'its policy still enforces {sorted(own)}'
            if own
            else 'no policy is emitted for this table'
        )
        notes.append(
            f"'{_meta(model).db_table}': tenant dimensions {dropped} live on more than one "
            f'ancestor ({owners}) and one correlated subquery reaches only one, so they are '
            f'left to Python scoping -- {remaining}.'
        )
        by_owner = {}
        if not own:
            # Return here, not fall through to the skip note: that note would wrongly say
            # "no column on any ancestor" when these actually have columns on *two*.
            return None, notes

    if not own and not by_owner:
        notes.append(_skip_note(model, spec))
        return None, notes

    owner_columns: dict[str, str] = {}
    owner_table = owner_pk = child_pk = None
    if by_owner:
        owner, owner_columns = next(iter(by_owner.items()))
        owner_table = _meta(owner).db_table
        owner_pk = _meta(owner).pk.column
        # The child's own primary key is its parent-link column, and every table in an MTI
        # chain shares one primary-key VALUE -- which is what makes this join sound.
        child_pk = _meta(model).pk.column

    return (
        TableCoverage(
            columns=own,
            owner_table=owner_table,
            owner_pk=owner_pk,
            child_pk=child_pk,
            owner_columns=owner_columns or None,
        ),
        notes,
    )


def _skip_note(model: type[models.Model], spec: dict[str, str]) -> str:
    """The whole model is uncoverable. Names dimension *and* lookup, since either alone
    leaves the reader guessing which of the two the message is about."""
    dimensions = ', '.join(f'{dimension} ({lookup})' for dimension, lookup in sorted(spec.items()))
    return (
        f"'{_meta(model).db_table}' skipped: tenant dimension(s) {dimensions} have no column "
        f'on this table or a shared-key ancestor, so there is nothing to predicate on. '
        f'Python scoping still applies.'
    )


def app_coverage(app: AppConfig) -> Coverage:
    """Policy-eligible tables for one app -- proxies share their concrete model's table,
    so tables dedupe naturally via the dict key."""
    tables: dict[str, TableCoverage] = {}
    notes: list[str] = []
    for model in app.get_models():
        if not tenant_spec(model):
            continue
        coverage, model_notes = _classify(model)
        notes.extend(model_notes)
        if coverage is not None:
            tables[_meta(model).db_table] = coverage
    return Coverage(tables=tables, notes=notes)


def expected_coverage(requested: set[str] | None = None) -> Coverage:
    """Policy-eligible tables across every local app, optionally scoped. *requested* holds
    app labels; empty or ``None`` means all."""
    tables: dict[str, TableCoverage] = {}
    notes: list[str] = []
    for app in django_apps.get_app_configs():
        if not is_local(app) or (requested and app.label not in requested):
            continue
        coverage = app_coverage(app)
        tables.update(coverage.tables)
        notes.extend(coverage.notes)
    return Coverage(tables=tables, notes=notes)
