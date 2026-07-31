"""Which tables are supposed to carry a ``tenant_scope`` policy, and how.

One answer, two consumers: ``makeguitarmigrations`` turns it into migrations at build
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
  one ancestor, and which one it reached would come down to field declaration order. All
  of them are dropped rather than one picked arbitrarily.

A model can therefore end up *partially* covered: its own-table dimensions get a policy
while the ones above are left to Python scoping. That is reported as what it is. Every note
names the dimensions it dropped and the ones the policy still enforces, because "skipped"
alone would read as "no protection here" on a table that has some.

Skips are design, not gaps -- but never silent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, TypedDict

from django.apps import apps as django_apps
from django.conf import settings

from guitars.gucs import BYPASS_GUC, guc_name
from guitars.introspection import column_owner, owns_column

from .manager import _meta, local_tenant_fields, tenant_spec


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
    """The shape :meth:`TableCoverage.as_kwargs` produces.

    Spelled out rather than left as ``dict[str, object]`` because the generator now *calls*
    ``sql.create_table_rls(**kwargs)`` instead of rendering the call as text into a
    migration. Text was never type-checked; a real call is, and an untyped mapping makes
    every parameter read as ``object``.

    ``total=False``: the owner keys are absent, not ``None``, when there is no owner join.
    """

    columns: dict[str, str]
    owner_table: str | None
    owner_pk: str | None
    child_pk: str | None
    owner_columns: dict[str, str] | None


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

    def as_kwargs(self) -> PolicyKwargs:
        """The keyword arguments ``guitars.sql.create_table_rls`` expects.

        Owner keys are omitted entirely when there is no owner join, so a non-MTI table's
        generated migration stays as simple as it reads.
        """
        kwargs: PolicyKwargs = {'columns': dict(self.columns)}
        if self.owner_columns:
            kwargs['owner_table'] = self.owner_table
            kwargs['owner_pk'] = self.owner_pk
            kwargs['child_pk'] = self.child_pk
            kwargs['owner_columns'] = dict(self.owner_columns)
        return kwargs

    # ``audittenancy`` compares a *live* policy against the two facts below rather than
    # against the SQL text: PostgreSQL rewrites a policy expression when it stores it
    # (``current_setting('tenant.x'::text, true) AS current_setting``, columns parenthesised
    # and casts made explicit), so string equality against what ``sql.policy`` emitted can
    # never hold. These two sets survive that rewrite intact -- and they live here, beside
    # ``as_kwargs`` which the generator uses, so the audit and the generator read one
    # description of the same policy. That is the whole reason this module exists.

    def policy_gucs(self) -> frozenset[str]:
        """Session-setting names the emitted predicate reads.

        Recoverable from a stored policy because ``current_setting('tenant.x', true)`` keeps
        its literal argument verbatim through PostgreSQL's rewrite. A dimension added to or
        removed from a model changes this set, which is what makes the drift visible.
        """
        dimensions = set(self.columns) | set(self.owner_columns or {})
        return frozenset({BYPASS_GUC, *(guc_name(dimension) for dimension in dimensions)})

    def policy_columns(self, table: str) -> frozenset[tuple[str, str]]:
        """``(table, column)`` pairs the emitted predicate references.

        Read back from ``pg_depend`` rather than parsed: creating a policy records a real
        dependency on every column its expression touches, so this is the catalog's own
        answer and needs no regex. A renamed tenant column changes this set even when the
        dimension names are untouched.

        *table* is a parameter rather than a field because it is this coverage's key in
        ``Coverage.tables``, and duplicating it here would let the two drift apart.
        """
        pairs = {(table, column) for column in self.columns.values()}
        owner_table, owner_pk, child_pk = self.owner_table, self.owner_pk, self.child_pk
        # The three-way check is the same invariant ``sql.policy._predicate`` enforces (it
        # raises when ``owner_columns`` arrives without them), so a coverage that failed it
        # could never have produced a policy for this table to be compared against. Spelling
        # it out here rather than asserting keeps the optional fields narrowed for the checker.
        if self.owner_columns and owner_table and owner_pk and child_pk:
            # The correlated subquery joins on both keys and reads the ancestor's columns.
            pairs.add((table, child_pk))
            pairs.add((owner_table, owner_pk))
            pairs |= {(owner_table, column) for column in self.owner_columns.values()}
        return frozenset(pairs)


class Coverage(NamedTuple):
    """What the models say the database should look like.

    ``notes`` carries the human-readable reason for every model that was considered and
    left out, or only partially covered.
    """

    tables: dict[str, TableCoverage]
    notes: list[str]


def is_local(app: AppConfig) -> bool:
    """Whether *app* is one of the project's own, per ``settings.LOCAL_APPS``.

    Keyed on ``app.name``, because ``LOCAL_APPS`` holds dotted module paths
    (``tests.testapp``) rather than Django's short labels.

    ``guitars.management._generator.is_local`` is the same one-liner, and the duplication is
    **deliberate** -- do not collapse the two. This package is documented as importing only
    the standard library and Django so that it can move as a unit, while ``_generator`` lives
    in the management layer and imports ``django.core.management``. Importing either from the
    other inverts a layer: the management commands already depend on this package (they call
    :func:`app_coverage`), so the dependency may not also run the other way. One shared line
    is the cheaper price.
    """
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
        # Dropping ALL of them rather than picking one ancestor arbitrarily: one correlated
        # subquery reaches one ancestor, and which one it happened to be would depend on
        # field declaration order -- a policy whose strength varied with that is worse than
        # a named gap. Any own-table dimensions still get their policy, so the note has to
        # say what is and is not enforced, not just that something was skipped.
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
            # Return here rather than falling through to the skip note, which would add a
            # second note for the same fact -- and a wrong one: it says these dimensions
            # "have no column on this table or a shared-key ancestor", when what actually
            # happened is that they have columns on *two* of them. The note above already
            # says no policy is emitted, and says why.
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
