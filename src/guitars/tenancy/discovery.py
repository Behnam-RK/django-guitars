"""Which tables are supposed to carry a ``tenant_scope`` policy, and how.

One answer, two consumers: ``maketenantmigrations`` turns it into migrations at build
time, ``audittenancy`` compares it against a live database. Keeping the rule here means the
audit can never quietly disagree with the generator about what coverage *should* look like
-- a drift that would make a green audit meaningless.

A model is covered when its tenant dimensions can be predicated on from its own table. Two
shapes satisfy that:

* **Own-table columns** -- the ordinary case, including fields copied down from an abstract
  base.
* **Columns on a multi-table-inheritance ancestor** -- covered through a correlated
  subquery on the shared primary key. guitars does *not* treat MTI children as protected
  transitively, which is the tempting shortcut: a child-only statement
  (``queryset.update()`` on child-local fields, a ``DELETE`` on the child table,
  ``.values()`` of child-only columns) never touches the ancestor, so an ancestor-only
  policy never applies to it. The kit already knows this -- it is why
  ``set_parent_updated_at`` exists.

What is still reported rather than covered:

* **Multi-hop dimensions** (``shop='post__shop'``) -- no column here at all to predicate
  on. The Python manager still scopes reads; RLS coverage arrives via the table the hop
  lands on.
* **Dimensions spread across two different ancestors** -- one correlated subquery reaches
  one ancestor, and emitting a policy covering only some dimensions would look like
  protection while enforcing less than the model declares.

Skips are design, not gaps -- but never silent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from django.apps import apps as django_apps
from django.conf import settings

from guitars.introspection import column_owner, owns_column

from .manager import _meta, local_tenant_fields, tenant_spec


if TYPE_CHECKING:
    from django.apps import AppConfig
    from django.db import models


__all__ = ['Coverage', 'TableCoverage', 'app_coverage', 'expected_coverage', 'is_local']


class TableCoverage(NamedTuple):
    """How one table's policy should be predicated.

    ``columns`` maps dimension -> column on this table. When the tenant column lives on an
    MTI ancestor instead, ``owner_*`` describes the join and ``owner_columns`` maps
    dimension -> column over there. A model may legitimately have both.
    """

    columns: dict[str, str]
    owner_table: str | None = None
    owner_pk: str | None = None
    child_pk: str | None = None
    owner_columns: dict[str, str] | None = None

    def as_kwargs(self) -> dict[str, object]:
        """The keyword arguments ``guitars.sql.create_table_rls`` expects.

        Owner keys are omitted entirely when there is no owner join, so a non-MTI table's
        generated migration stays as simple as it reads.
        """
        kwargs: dict[str, object] = {'columns': dict(self.columns)}
        if self.owner_columns:
            kwargs['owner_table'] = self.owner_table
            kwargs['owner_pk'] = self.owner_pk
            kwargs['child_pk'] = self.child_pk
            kwargs['owner_columns'] = dict(self.owner_columns)
        return kwargs


class Coverage(NamedTuple):
    """What the models say the database should look like.

    ``notes`` carries the human-readable reason for every model that was considered and
    left out, or only partially covered.
    """

    tables: dict[str, TableCoverage]
    notes: list[str]


def is_local(app: AppConfig) -> bool:
    """Whether *app* is one of the project's own, per ``settings.LOCAL_APPS``."""
    return app.name in settings.LOCAL_APPS


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
            # MTI: the field resolves, but the column physically lives on an ancestor.
            # Resolve the OWNER, not the immediate parent -- in a chain three deep the
            # column may live two tables up, and predicating against the parent would
            # reference a table that has no such column either.
            by_owner.setdefault(column_owner(model, field_name), {})[dimension] = column

    if len(spec) > len(local):
        uncovered = sorted(set(spec) - set(local))
        notes.append(
            f"'{_meta(model).db_table}': dimensions {uncovered} traverse a relation and are "
            f'not covered by its policy (Python scoping still applies).'
        )

    if len(by_owner) > 1:
        owners = sorted(_meta(owner).db_table for owner in by_owner)
        notes.append(
            f"'{_meta(model).db_table}': tenant dimensions live on more than one ancestor "
            f'({owners}); one correlated subquery reaches one ancestor, so no policy is '
            f'emitted for them rather than one that enforces less than the model declares.'
        )
        by_owner = {}

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
    lookups = ', '.join(sorted(spec.values()))
    return (
        f"'{_meta(model).db_table}' skipped: tenant dimension(s) [{lookups}] have no column "
        f'on this table or a shared-key ancestor, so there is nothing to predicate on. '
        f'Python scoping still applies.'
    )


def app_coverage(app: AppConfig) -> Coverage:
    """Policy-eligible tables for one app.

    Proxies share their concrete model's table, so tables dedupe naturally via the dict key.
    """
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
    """Policy-eligible tables across every local app, optionally scoped.

    *requested* holds app labels; empty or ``None`` means all local apps.
    """
    tables: dict[str, TableCoverage] = {}
    notes: list[str] = []
    for app in django_apps.get_app_configs():
        if not is_local(app) or (requested and app.label not in requested):
            continue
        coverage = app_coverage(app)
        tables.update(coverage.tables)
        notes.extend(coverage.notes)
    return Coverage(tables=tables, notes=notes)
