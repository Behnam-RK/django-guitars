"""Which tables carry a ``tenant_scope`` policy, and how -- one answer, two consumers:
``makeguitarmigrations`` builds migrations from it, ``audittenancy`` compares it against a
live database. See ADR-0003 and ``docs/tenancy.md`` for MTI and reported-not-covered cases."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, TypedDict

from django.apps import apps as django_apps

from guitars.gucs import BYPASS_GUC, guc_name
from guitars.introspection import column_owner, owns_column
from guitars.local_apps import is_local
from guitars.sql._identifiers import _safe_identifier

from .spec import _autofills, _meta, local_tenant_fields, tenant_spec


if TYPE_CHECKING:
    from django.apps import AppConfig
    from django.db import models

    #: ``(owner, column) -> _relocatable``'s answer, threaded through one ``app_coverage``
    #: call. That resolution sweeps the *whole* model registry and ``_classify`` runs once
    #: per descendant, so N children of one ancestor otherwise ask it N times over.
    _RelocationMemo = dict[tuple[type[models.Model], str], tuple[str | None, str | None]]

    #: Caller-owned cache for :func:`policy_dimensions` and its assuming twin, keyed by the
    #: model *and* which of the two asked -- they disagree only outside ``LOCAL_APPS``, but
    #: they do disagree, so one key per model would answer whichever asked first.
    _PolicyDimensionMemo = dict[tuple[type[models.Model], bool], frozenset[str]]


__all__ = [
    'AUTOFILL_FUNCTION_PREFIX',
    'Coverage',
    'PolicyKwargs',
    'TableCoverage',
    'app_coverage',
    'assumed_policy_dimensions',
    'autofill_function_name',
    'autofill_trigger_name',
    'expected_coverage',
    'is_local',
    'owner_autofill_notes',
    'policy_dimensions',
]


#: What marks a trigger function as this library's. ``audittenancy`` needs it to tell a
#: stray autofill trigger from an application's own ``BEFORE INSERT`` trigger, which it must
#: never report -- so the two sides read one constant rather than repeating the literal.
AUTOFILL_FUNCTION_PREFIX = 'guitars_fill_'


def autofill_function_name(dimension: str, column: str) -> str:
    """Bare name of the trigger function filling *column* from *dimension*'s GUC. The
    length prefix keeps ``('a', 'b_c')`` and ``('a_b', 'c')`` apart, the same collision
    ``_related_rule_name`` guards against; callers quote it, ``audittenancy`` does not."""
    return _safe_identifier(f'{AUTOFILL_FUNCTION_PREFIX}{len(dimension)}_{dimension}_{column}')


def autofill_trigger_name(function: str) -> str:
    """Bare name of the trigger calling *function*. Derived, not fixed: a trigger name is
    unique **per table**, and a table tenanted on two local dimensions needs one trigger per
    (column, GUC) pair -- under a constant name the second ``CREATE TRIGGER`` collided."""
    return _safe_identifier(f'{function}_trigger')


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
    #: ``{dimension: column}`` this table's own INSERT trigger fills from scope (ADR 0005).
    #: Own-table columns only, and only when the manager autofills -- deliberately outside
    #: ``as_kwargs`` so adding it churns no ``[POLICY:...]`` digest and the release stays additive.
    autofill_columns: dict[str, str] | None = None
    #: Same, for columns living on ``owner_table``: the trigger goes *there*, attributed to
    #: the owner's app, since a trigger here could not write an ancestor's column (ADR 0009).
    #: Outside ``as_kwargs`` for the same reason as above.
    owner_autofill_columns: dict[str, str] | None = None

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


def _classify(
    model: type[models.Model], memo: _RelocationMemo | None = None
) -> tuple[TableCoverage | None, list[str]]:
    """Split a model's tenant dimensions into own-table and ancestor-held, or explain why not.
    *memo* is a caller-owned cache of :func:`_relocatable` answers, shared across the
    descendants of one ancestor -- see its type alias for why that matters."""
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

    # Sourced from ``own``, never ``owner_columns``: a trigger here could not write an
    # ancestor's column. Those relocate onto the owner's table instead (ADR 0009), when
    # every model sharing that one trigger agrees it should exist.
    autofill_columns = dict(own) if own and _autofills(model) else None
    relocated: dict[str, str] = {}
    # ``_autofills`` short-circuits ``_relocatable``'s registry sweep and cannot change its
    # answer: *model* claims every column in ``owner_columns``, so one that does not autofill
    # is always refused. The note is unaffected -- ``owner_autofill_notes`` computes its own.
    if by_owner and _autofills(model):
        relocated = {
            dimension: column
            for dimension, column in owner_columns.items()
            if _relocatable(owner, column, memo)[0] == dimension
        }

    return (
        TableCoverage(
            columns=own,
            owner_table=owner_table,
            owner_pk=owner_pk,
            child_pk=child_pk,
            owner_columns=owner_columns or None,
            autofill_columns=autofill_columns,
            owner_autofill_columns=relocated or None,
        ),
        notes,
    )


def _owner_column_claims(
    owner: type[models.Model], column: str
) -> tuple[dict[type[models.Model], str], set[str]]:
    """Every concrete model resolving *column* to *owner*'s table, mapped to the dimension it
    claims it under, plus that dimension *set* -- returned, not read back off the values,
    since one model naming two dimensions would overwrite itself and slip past rule 2."""
    claims: dict[type[models.Model], str] = {}
    dimensions: set[str] = set()
    for model in django_apps.get_models():
        if not (model is owner or issubclass(model, owner)) or _meta(model).proxy:
            continue
        for dimension, field_name in local_tenant_fields(model).items():
            field = _meta(model).get_field(field_name)
            if field.column == column and column_owner(model, field_name) is owner:
                claims[model] = dimension
                dimensions.add(dimension)
    return claims, dimensions


def _relocatable(
    owner: type[models.Model], column: str, memo: _RelocationMemo | None = None
) -> tuple[str | None, str | None]:
    """:func:`_resolve_relocatable`, answered from *memo* when the caller supplies one. Split
    from the rules themselves so the whole-registry sweep below runs once per
    ``(owner, column)`` rather than once per descendant asking about the same one."""
    if memo is None:
        return _resolve_relocatable(owner, column)
    key = (owner, column)
    if key not in memo:
        memo[key] = _resolve_relocatable(owner, column)
    return memo[key]


def _resolve_relocatable(owner: type[models.Model], column: str) -> tuple[str | None, str | None]:
    """``(dimension to relocate under, refusal note)``. At most one is set; both are ``None``
    when *owner* already autofills *column* itself, which is the MTI case that was always
    correct -- the ancestor's own trigger fires on the row Django writes into its table."""
    claims, dimensions = _owner_column_claims(owner, column)
    owner_table = _meta(owner).db_table

    # Nobody wants it, so there is nothing to refuse. Checked before the conflicts below so
    # a column no claimant autofills stays silent rather than earning a note about a trigger
    # it was never going to get -- the "two notes for one fact" this module already avoids.
    if not any(_autofills(claimant) for claimant in claims):
        return None, None

    owner_fills_it = owner in claims and _autofills(owner)
    if len(dimensions) > 1:
        # The split the opted-out pair below also makes: "left to Python scoping" holds only
        # where no trigger exists. Where the owner autofills this column, one does, stamping
        # every descendant's row from *that* dimension's GUC -- a different, louder fact.
        outcome = (
            f"its owner's own trigger fills it from the '{claims[owner]}' dimension for "
            f'every descendant inserted through this table'
            if owner_fills_it
            else 'left to Python scoping'
        )
        return None, (
            f"'{owner_table}'.{column} is claimed as tenant dimensions {sorted(dimensions)} "
            f'by {sorted(_meta(m).db_table for m in claims)}, so autofilling it would need '
            f'two triggers racing on one table in name order -- {outcome}.'
        )

    opted_out = sorted(_meta(m).db_table for m in claims if not _autofills(m))
    if opted_out and owner_fills_it:
        # Same refusal, different *fact*: the owner's own ``autofill_columns`` already puts a
        # trigger on this table, so "left to Python scoping" below would be false -- the
        # opt-out is being overwritten today, and only dropping that trigger can honour it.
        return None, (
            f"'{owner_table}'.{column} is autofilled by its owner's own trigger, and "
            f'{opted_out} share that column but do not autofill -- an MTI insert of theirs '
            f'writes a row into this same table, so that trigger overwrites their opt-out. '
            f'Pass autofill=False on the owner too if that is wrong.'
        )
    if opted_out:
        return None, (
            f"'{owner_table}'.{column} is not autofilled: {opted_out} share that column and "
            f'do not autofill, and an MTI insert of theirs writes a row into this same table, '
            f'so one trigger would overwrite their opt-out. Left to Python scoping.'
        )

    if owner_fills_it:
        # Already covered by the owner's own ``autofill_columns`` -- relocating here too
        # would emit a second CREATE TRIGGER on that table and fail migrate.
        return None, None

    # Exactly one dimension by here: an empty ``claims`` returns at rule 1 above, and more
    # than one is refused at rule 2 -- so this is the single dimension every claimant agrees on.
    return next(iter(dimensions)), None


def owner_autofill_notes() -> list[str]:
    """One refusal note per ``(owner_table, column)``, regardless of how many descendants
    claim it: the fact is about the owner's table, not about any one child, and ``_classify``
    runs once per descendant -- emitting there would print the same note N times."""
    # ``''`` records "already resolved, no note" so a second descendant sharing the key
    # doesn't re-run the whole-registry scan ``_relocatable`` performs.
    seen: dict[tuple[type[models.Model], str], str] = {}
    for model in django_apps.get_models():
        # Local claimants only, matching ``expected_coverage``'s gate: a third-party app's
        # models produce no coverage, so a refusal about one names a trigger never to be
        # emitted. ``_relocatable`` below still counts *every* descendant's opt-out.
        if _meta(model).proxy or not tenant_spec(model) or not is_local(_meta(model).app_config):
            continue
        for field_name in local_tenant_fields(model).values():
            if owns_column(model, field_name):
                continue
            owner = column_owner(model, field_name)
            key = (owner, _meta(model).get_field(field_name).column)
            if key in seen:
                continue
            _, note = _relocatable(*key)
            seen[key] = note or ''
    return [
        seen[key] for key in sorted(seen, key=lambda k: (_meta(k[0]).db_table, k[1])) if seen[key]
    ]


def _skip_note(model: type[models.Model], spec: dict[str, str]) -> str:
    """The whole model is uncoverable. Names dimension *and* lookup, since either alone
    leaves the reader guessing which of the two the message is about."""
    dimensions = ', '.join(f'{dimension} ({lookup})' for dimension, lookup in sorted(spec.items()))
    return (
        f"'{_meta(model).db_table}' skipped: tenant dimension(s) {dimensions} have no column "
        f'on this table or a shared-key ancestor, so there is nothing to predicate on. '
        f'Python scoping still applies.'
    )


def _proxy_note(model: type[models.Model]) -> str:
    """A proxy's tenanted manager buys no enforcement. Named rather than dropped in silence:
    the manager reads like the concrete model's, so nothing else says the table went bare."""
    return (
        f"'{_meta(model).db_table}' skipped: '{model.__name__}' is a proxy, which owns no "
        f'columns of its own, so its tenanted manager cannot be turned into a policy or an '
        f'autofill trigger. Declare tenanted_manager() on '
        f"'{_meta(model).concrete_model.__name__}' instead. Python scoping still applies."
    )


def policy_dimensions(
    model: type[models.Model], memo: _PolicyDimensionMemo | None = None
) -> frozenset[str]:
    """Tenant dimensions a policy *this kit generates* predicates *model*'s table on -- not
    ``tenant_spec``, whose dimension traversing a relation or living on two ancestors is left to
    Python scoping and filters nothing, so the spec reports a filter no ``SELECT`` ever meets."""
    return _dimensions(model, memo, outside_local_apps=frozenset())


def assumed_policy_dimensions(
    model: type[models.Model], memo: _PolicyDimensionMemo | None = None
) -> frozenset[str]:
    """:func:`policy_dimensions`, but a model outside ``LOCAL_APPS`` has its manager read as
    enforced rather than as absent: this kit writes no policy there, and that model's own
    package may. For a caller whose safe answer is "assume this read can be filtered"."""
    return _dimensions(model, memo, outside_local_apps=None)


def _dimensions(
    model: type[models.Model],
    memo: _PolicyDimensionMemo | None,
    *,
    outside_local_apps: frozenset[str] | None,
) -> frozenset[str]:
    """The two above, which differ only in what a model outside ``LOCAL_APPS`` answers --
    ``outside_local_apps`` is that answer, ``None`` meaning "its declared spec". Splitting the
    default is the point: subtracted one way and added the other, one default cannot be safe."""
    concrete = _meta(model).concrete_model
    spec = tenant_spec(concrete)
    if not spec:
        return frozenset()
    if not is_local(django_apps.get_app_config(_meta(concrete).app_label)):
        return frozenset(spec) if outside_local_apps is None else outside_local_apps
    key = (concrete, outside_local_apps is None)
    if memo is not None and key in memo:
        return memo[key]
    # Memoised by the caller, not here: ``_classify`` reaches ``_relocatable``'s registry sweep
    # for an MTI model that autofills, and a rule with N arms asks about N models -- while a
    # module-level cache would answer from a registry, and a ``LOCAL_APPS``, since replaced.
    coverage, _ = _classify(concrete)
    dimensions = (
        frozenset()
        if coverage is None
        else frozenset(coverage.columns) | frozenset(coverage.owner_columns or {})
    )
    if memo is not None:
        memo[key] = dimensions
    return dimensions


def app_coverage(app: AppConfig) -> Coverage:
    """Policy-eligible tables for one app -- proxies are skipped, their concrete model
    contributing the same table."""
    tables: dict[str, TableCoverage] = {}
    notes: list[str] = []
    # Owned by this call, not module-level: the model registry moves between calls (a test
    # registering a model, an app loaded late), and a stale answer here retires a trigger.
    memo: _RelocationMemo = {}
    for model in app.get_models():
        if not tenant_spec(model):
            continue
        if _meta(model).proxy:
            # A proxy has no ``local_fields``, so ``_classify`` returns a self-join that, keyed
            # by db_table, overwrote the real answer and retired its trigger. Silent where the
            # concrete model covers the table; a manager *on* the proxy does not, so it is named.
            if not tenant_spec(_meta(model).concrete_model):
                notes.append(_proxy_note(model))
            continue
        coverage, model_notes = _classify(model, memo)
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
    # Appended once for the whole run, not per app: a refused relocation is a fact about the
    # owner's table, which several apps' children can share.
    notes.extend(owner_autofill_notes())
    return Coverage(tables=tables, notes=notes)
