import contextlib
from collections import defaultdict
from typing import cast

from asgiref.sync import sync_to_async
from django.apps import apps as django_apps
from django.conf import settings
from django.core.exceptions import EmptyResultSet, ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, connections, router, transaction
from django.db.models import (
    CASCADE,
    DateTimeField,
    Field,
    ForeignKey,
    Func,
    Index,
    Manager,
    ManyToOneRel,
    Q,
    QuerySet,
    sql,
)
from django.db.models.base import Model
from django.db.models.deletion import Collector
from django.db.models.signals import post_delete, pre_delete

from guitars import GuitarsError
from guitars.checks import refuses_pk_not_parent_link
from guitars.introspection import (
    column_owner,
    has_column,
    mti_root,
    owned_tenancy_refusals,
    owns_column,
    rule_update_cycle_edges,
)
from guitars.routing import migrates_to_postgresql
from guitars.sql import SWITCH_OFF_HARD_DELETION, SWITCH_ON_HARD_DELETION

from ._cascade_coverage import cascade_plan
from .fields import OwningForeignKey, _targets_primary_key


def _is_mti_model(model: type[Model]) -> bool:
    """Whether *model* participates in multi-table inheritance (as a child or a parent)."""
    return bool(model._meta.parents) or any(
        getattr(rel, 'parent_link', False) for rel in model._meta.related_objects
    )


def _declared_owning_fields(model: type[Model]) -> list[OwningForeignKey]:
    """``OwningForeignKey``s *model* declares whose target has a ``_deleted_at`` to stamp --
    all :func:`_owned_fields` knows before the cycle graph. Split out so a caller can ask the
    cheap half first: that graph sweeps the registry, and most models own nothing."""
    # ``owns_column``, not ``hasattr``: a model with no ``_deleted_at`` at all has no rule to
    # fire, and one inheriting it from an MTI ancestor is refused with a warning, since the
    # rule would fire on a table ``old."<column>"`` cannot reach. See docs/owned-relations.md.
    if not owns_column(model, '_deleted_at'):
        return []
    # Beside that return, not in the comprehension: it does not vary across ``local_fields``,
    # and with a router configured each ask runs the whole chain per alias -- on a path
    # ``hard_delete`` walks per collected model, per fixpoint round.
    if not migrates_to_postgresql(model):
        return []
    # Mirrors ``_owned_candidates``/``_owned_operations``' "nothing to stamp" and non-primary-key
    # refusals: no rule is emitted, so the relation is not followed -- following it destroys what
    # the rule spared, and under a redirected key destroys a row nothing ever owned.
    return [
        field
        for field in model._meta.local_fields
        if isinstance(field, OwningForeignKey)
        and has_column(field.related_model, '_deleted_at')
        and _targets_primary_key(field)
        # And the routing refusal, for the same reason as the two above: the generator
        # writes no rule across a relation either end of which is off PostgreSQL.
        and migrates_to_postgresql(field.related_model)
    ]


def _owned_fields(
    model: type[Model],
    cycles: set[tuple[str, str]] | None = None,
    tenancy_refusals: dict[tuple[str, str, str], list[str]] | None = None,
) -> list[OwningForeignKey]:
    """``OwningForeignKey``s that actually carry a rule: a relation the generator refused has
    none, so following it here destroys what the rule spared. Both graphs are passed in by a
    caller asking about several models, so each registry sweep is paid once."""
    declared = _declared_owning_fields(model)
    # Before either sweep, not after: both read the whole model registry, and most models
    # declare no ownership at all -- ``hard_delete`` asks this of every collected one.
    if not declared:
        return []
    table = model._meta.db_table
    # The same two answers the generator refuses on, shared rather than re-derived so the two
    # sides cannot disagree about which relations carry a rule. *model* is named alongside the
    # registry for the reason ``_declared_owning_fields`` gives: it may not be registered.
    if cycles is None:
        cycles = rule_update_cycle_edges([model, *django_apps.get_models()])
    if tenancy_refusals is None:
        tenancy_refusals = owned_tenancy_refusals([model, *django_apps.get_models()])
    kept = []
    for field in declared:
        dependent_table = column_owner(field.related_model, '_deleted_at')._meta.db_table
        if (table, dependent_table) in cycles:
            continue
        if (dependent_table, table, field.column) in tenancy_refusals:
            continue
        kept.append(field)
    return kept


def _mti_model_chain(model: type[Model]) -> list[type[Model]]:
    """Every model in *model*'s MTI tree, leaf-first -- the whole tree from the root, not
    just *model*'s ancestors, since ``hard_delete`` clears the chain from whatever level it
    was reached at. ``[model]`` for a model with no MTI at all."""
    root = mti_root(model)
    chain: list[type[Model]] = []
    seen: set[type[Model]] = set()

    def _visit(m: type[Model]) -> None:
        if m in seen:
            return
        seen.add(m)
        for rel in m._meta.related_objects:
            if getattr(rel, 'parent_link', False):
                _visit(rel.related_model)  # a more-derived MTI child table
        chain.append(m)

    _visit(root)  # post-order from the root -> children appended before their parent
    return chain


def _rows(model: type[Model], using: str | None) -> QuerySet:
    """Every row of *model* regardless of ``_deleted_at``, on *using*. ``_all_objects`` is set
    dynamically, so the hasattr narrowing does not survive; ``_base_manager`` for anything else
    and never ``_default_manager``, which as in Django's ``Collector`` could hide a referrer."""
    manager = model._all_objects if hasattr(model, '_all_objects') else model._base_manager
    return manager.using(using)  # ty: ignore[unresolved-attribute]


def _key_values(field: Field, pks: set, using: str | None) -> dict:
    """``{value a key aimed at *field*'s target holds: the pk it stands for}`` for *pks*. Identity
    for the usual key, holding the primary key every table in an MTI chain shares; a ``to_field``
    key holds *that* column, read off the one model declaring it, and matches no pk."""
    target = field.target_field if isinstance(field, ForeignKey) else None
    if target is None or target.primary_key:
        return {pk: pk for pk in pks}
    return dict(_rows(target.model, using).filter(pk__in=pks).values_list(target.attname, 'pk'))


def _self_cascade_fields(model: type[Model], using: str | None) -> list[Field]:
    """The ``CASCADE`` keys *model* declares to itself that one recursive query can follow: a plain
    model and a key to the primary key. An MTI chain (each level a table, entered from its root)
    and a ``to_field`` key (matching no pk) keep the level-by-level walk."""
    # A pk with a converter would come back from raw SQL unconverted; keep the ORM's read for it.
    # A pk that is itself a key is read through its target's converters too, so it is refused.
    pk = model._meta.pk
    connection = connections[using or DEFAULT_DB_ALIAS]
    if _is_mti_model(model) or pk.is_relation or pk.get_db_converters(connection):
        return []
    return [
        cast('Field', relation.field)
        for relation in _referring_relations(model)
        if relation.on_delete is CASCADE
        and relation.related_model is model
        and not getattr(relation, 'parent_link', False)
        and _targets_primary_key(cast('ForeignKey', relation.field))
    ]


def _self_descendant_origins(model: type[Model], pks: set, using: str | None) -> dict:
    """``{row: the rows of *pks* it descends from}`` through *model*'s self-referential cascade
    keys, in **one** ``WITH RECURSIVE``; ``UNION`` over the pairs ends a cycle. Raw SQL: database
    policies apply, a tenant dimension kept in Python alone (ADR 0003) does not."""
    origins: dict = defaultdict(set)
    for pk in pks:
        origins[pk].add(pk)
    fields = _self_cascade_fields(model, using)
    if not fields or not pks:
        return origins
    connection = connections[using or DEFAULT_DB_ALIAS]
    quote = connection.ops.quote_name
    table, pk_column = quote(model._meta.db_table), quote(cast(str, model._meta.pk.column))
    # Every key in the one recursion, so a subtree reached by alternating keys is still one query.
    onward = ' OR '.join(f't.{quote(cast(str, f.column))} = guitars_below.pk' for f in fields)
    with connection.cursor() as cursor:
        cursor.execute(
            f'WITH RECURSIVE guitars_below(pk, origin) AS ('  # nosec B608 - names from _meta
            f'SELECT {pk_column}, {pk_column} FROM {table} WHERE {pk_column} = ANY(%s) '
            f'UNION '
            f'SELECT t.{pk_column}, guitars_below.origin FROM {table} AS t '
            f'JOIN guitars_below ON {onward}'
            f') SELECT pk, origin FROM guitars_below',
            [list(pks)],
        )
        for pk, origin in cursor.fetchall():
            origins[pk].add(origin)
    return origins


def _with_self_descendants(model: type[Model], pks: set, using: str | None) -> set:
    """*pks* and every row below them through *model*'s self-referential cascade keys: each row
    once, where :func:`_self_descendant_origins` returns one per seed above it -- the walk needs
    the rows alone, and the pairs grow with the depth when every node is a seed."""
    fields = _self_cascade_fields(model, using)
    if not fields or not pks:
        return set(pks)
    connection = connections[using or DEFAULT_DB_ALIAS]
    quote = connection.ops.quote_name
    table, pk_column = quote(model._meta.db_table), quote(cast(str, model._meta.pk.column))
    columns = [quote(cast(str, field.column)) for field in fields]
    seeds = ' OR '.join(f'{column} = ANY(%s)' for column in columns)
    onward = ' OR '.join(f't.{column} = guitars_below.pk' for column in columns)
    with connection.cursor() as cursor:
        cursor.execute(
            f'WITH RECURSIVE guitars_below(pk) AS ('  # nosec B608 - names come from _meta
            f'SELECT {pk_column} FROM {table} WHERE {seeds} '
            f'UNION '
            f'SELECT t.{pk_column} FROM {table} AS t JOIN guitars_below ON {onward}'
            f') SELECT pk FROM guitars_below',
            [list(pks)] * len(columns),
        )
        return set(pks) | {row[0] for row in cursor.fetchall()}


def _referring_relations(model: type[Model]) -> list:
    """Every reverse relation with a *column* pointing at *model* -- the one walk ``_collect``
    and :func:`_still_referenced` share for the rows that can hold each other back.
    ``include_hidden``: a ``related_name='+'`` key dangles too."""
    # ``ManyToOneRel`` (``OneToOneRel`` and the parent-link with it) is exactly the reverse of a
    # ForeignKey -- the only rel whose ``attname`` is the key column both callers read. An m2m
    # reverse owns none.

    # A ``GenericRelation`` cannot dangle, having no column to dangle by; ``_collect`` walks
    # ``_meta.private_fields`` separately to take those along -- see the doc.

    # Which is why the two walks are no longer identical: what ``_collect`` takes there,
    # :func:`_cascade_closure` does not model, so they can disagree.

    # Since 2.15.0 that cuts the other way too: sparing reads the referrers of every closure
    # row, so a plain key into a generic child goes unseen and the walk aborts at ``COMMIT``
    # rather than sparing its target -- #76.
    return [
        relation
        for relation in model._meta.get_fields(include_hidden=True)
        if isinstance(relation, ManyToOneRel)
    ]


def _cascade_closure(
    root: type[Model], pks: set, using: str | None
) -> tuple[dict[type[Model], set], dict[type[Model], dict]]:
    """Every row, by model, that collecting *pks* of *root* takes along through reverse ``CASCADE``
    -- the walk ``_collect`` performs, to the same depth -- and, per row, which of *pks* takes it.
    One hop is not enough: a *grand*child goes too, and a plain key into it dangles just as hard."""
    taken: dict[type[Model], set] = defaultdict(set)
    origins: dict[type[Model], dict] = defaultdict(lambda: defaultdict(set))
    pending: list[tuple[type[Model], dict]] = [(root, {pk: {pk} for pk in pks})]
    while pending:
        model, reached = pending.pop()
        known = origins[model]
        # A row is walked again when it gains a target it was not yet known to go with: a row
        # two targets reach must spare both, so each has to arrive at it.
        fresh = {pk: new for pk, came in reached.items() if (new := came - known[pk])}
        if not fresh:
            continue
        # The subtree in one query, so the self-referential key below is skipped, not re-walked.
        below: dict = defaultdict(set)
        for pk, seeds in _self_descendant_origins(model, set(fresh), using).items():
            for seed in seeds:
                below[pk] |= fresh[seed]
        fresh = {pk: new for pk, came in below.items() if (new := came - known[pk])}
        for pk, new in fresh.items():
            known[pk] |= new
        taken[model].update(fresh)
        followed = _self_cascade_fields(model, using)
        for relation in _referring_relations(model):
            if relation.on_delete is not CASCADE:
                continue
            field = cast('Field', relation.field)
            if field in followed:
                continue
            related_model = cast('type[Model]', relation.related_model)
            keys = _key_values(field, set(fresh), using)
            children: dict = defaultdict(set)
            for child_pk, value in (
                _rows(related_model, using)
                .filter(**{f'{field.attname}__in': keys})
                .values_list('pk', field.attname)
            ):
                children[child_pk] |= fresh[keys[value]]
            # From the child's MTI *root*, and a parent-link from the level it names -- the
            # same two cases ``_collect`` distinguishes, since this has to reach exactly the
            # rows it will. The declaring level alone would leave its ancestors' rows out.
            pending.append(
                (
                    related_model
                    if getattr(relation, 'parent_link', False)
                    else mti_root(related_model),
                    children,
                )
            )
    return taken, origins


def _still_referenced(
    target: type[Model], pks: set, claimed: dict[type[Model], set], using: str | None
) -> set:
    """Which of *pks* a row that outlives the collection still points at, through **any**
    foreign key, not only the one that declared ownership, at the target **or any row its
    cascade takes along** (#71): a surviving key of any kind dangles at ``COMMIT``."""
    # Rows collecting the chain takes along, by **row**, not relation: one model can hold a
    # ``CASCADE`` key *and* a plain one to the same target, and discounting the relation alone
    # held the target back forever. Whole closure, not one hop -- see ``_cascade_closure``.
    taken, origins = _cascade_closure(mti_root(target), pks, using)
    # By MTI tree, not level: a parent-link walk records one row at every level it reaches.
    trees: dict[type[Model], dict] = defaultdict(lambda: defaultdict(set))
    for level, level_origins in origins.items():
        for pk, came in level_origins.items():
            trees[mti_root(level)][pk] |= came
    referenced: set = set()
    for model, reached in trees.items():
        # Every model in the row's MTI tree, not its level alone: removing it removes the whole
        # chain, so a key into any level holds the same pk value and dangles just as hard.
        # Deduped -- an inherited relation is reported per level, and one read answers for all.
        relations = dict.fromkeys(
            relation
            for level in _mti_model_chain(model)
            for relation in _referring_relations(level)
        )
        for relation in relations:
            # A parent-link is the same object one table down, collected with the chain; a
            # ``CASCADE`` referrer goes with the row it points at, and is in ``taken`` above.
            if getattr(relation, 'parent_link', False) or relation.on_delete is CASCADE:
                continue
            related_model = cast('type[Model]', relation.related_model)
            field = cast('Field', relation.field)
            # No emptiness guard: an ``__in`` over no keys is an empty result either way.
            keys = _key_values(field, set(reached), using)
            rows = _rows(related_model, using).filter(**{f'{field.attname}__in': keys})
            going = claimed.get(related_model, set()) | taken.get(related_model, set())
            if going:
                rows = rows.exclude(pk__in=going)
            for value in rows.values_list(field.attname, flat=True):
                referenced |= reached[keys[value]]
            if referenced >= pks:  # nothing left to spare; skip the remaining relations
                return referenced
    return referenced


class _OwnedScan:
    """What the ``hard_delete`` fixpoint has read: owner rows enumerated, targets *spared* (asked
    again each round, since a referrer holding one back can be claimed later), and the rule graph.
    A fresh one reproduces a full rescan."""

    def __init__(self) -> None:
        self.scanned: dict[type[Model], set] = defaultdict(set)
        self.spared: dict[tuple[type[Model], str], set] = defaultdict(set)
        self.graph_for: frozenset[type[Model]] | None = None
        self.cycles: set = set()
        self.refusals: dict = {}

    def graph(self, claimed: dict[type[Model], set]) -> tuple[set, dict]:
        # Redone only for a claimed model outside the registry: a registered one is already in
        # the sweep, so the answer cannot have moved.
        registry = set(django_apps.get_models())
        extra = frozenset(model for model in claimed if model not in registry)
        if self.graph_for is None or not extra <= self.graph_for:
            swept = [*claimed, *registry]
            self.cycles = rule_update_cycle_edges(swept)
            self.refusals = owned_tenancy_refusals(swept)
            self.graph_for = extra | (self.graph_for or frozenset())
        return self.cycles, self.refusals


def _owned_targets(
    claimed: dict[type[Model], set], using: str | None, scan: _OwnedScan | None = None
) -> list[tuple[type[Model], set]]:
    """``(model, pks)`` for every owned row *claimed* is the last owner of -- the rule's
    ``NOT EXISTS``, narrowed three ways below because this *removes* the row where the rule
    only stamps a column. *claimed* is every row going away, not one group's; see below."""
    scan = scan or _OwnedScan()
    found: dict[type[Model], set] = defaultdict(set)
    # The cheap half first: the graph below sweeps the whole registry, and ``hard_delete`` runs
    # this to a fixpoint over models that nearly all own nothing. ``pks`` too -- ``claimed`` is
    # a defaultdict, so a model looked at and found empty must not buy that sweep either.
    owning = {
        model: pks for model, pks in claimed.items() if pks and _declared_owning_fields(model)
    }
    if not owning:
        return []
    # Once for the walk, not per round or per claimed model: the rule graph and its tenancy half
    # are registry-wide. Claimed models are named beside the registry as ``_owned_fields`` names
    # its own, since they may not be registered.
    cycles, tenancy_refusals = scan.graph(claimed)
    for model, pks in owning.items():
        # Only owners not read before: a target of an owner read earlier was asked then, and a
        # target spared then is carried in ``scan.spared`` and asked again below.
        new = pks - scan.scanned[model]
        for field in _owned_fields(model, cycles, tenancy_refusals):
            carried = scan.spared[(model, field.name)]
            owned_pks = (
                set(
                    _rows(model, using)
                    .filter(pk__in=new)
                    .exclude(**{field.attname: None})
                    .values_list(field.attname, flat=True)
                )
                if new
                else set()
            )
            everything = owned_pks | carried
            if not everything:
                continue
            # Narrowed: (1) the whole claimed batch is spared, not one row -- all of it is
            # going; (2) no `_deleted_at` filter, an archived referrer's key is still on disk;
            # (3) *any* surviving reference holds the row back, not just the owning column.
            candidates = everything
            # A *shrinking* fixpoint, not one subtraction: a pk spared here keeps its CASCADE
            # closure alive, so a referrer inside it survives after all and holds another pk
            # back. Each round is a strict subset of the last, which is what terminates it.
            while candidates:
                referenced = _still_referenced(field.related_model, candidates, claimed, using)
                if not referenced:
                    break
                candidates = candidates - referenced
            scan.spared[(model, field.name)] = everything - candidates
            # Guarded: ``found`` is a defaultdict, so an unguarded ``update`` would mint a
            # ``(model, set())`` row for a relation that spared everything, and the caller
            # would enter a fixpoint round over rows that do not exist.
            if candidates:
                found[field.related_model].update(candidates)
        scan.scanned[model] |= new
    return list(found.items())


@contextlib.contextmanager
def _hard_deletion_on(using: str | None, *, savepoint: bool = True):
    """The session switch on for the block, off after it. It is transaction-local, so a rollback
    restores it; a failing block tries to switch off before re-raising (suppressed, the
    transaction being likely aborted), but a failing switch-off on success must abort."""
    alias = using or DEFAULT_DB_ALIAS
    with (
        connections[alias].cursor() as cursor,
        transaction.atomic(using=alias, savepoint=savepoint),
    ):
        cursor.execute(SWITCH_ON_HARD_DELETION)
        try:
            yield
        except Exception:
            with contextlib.suppress(Exception):
                cursor.execute(SWITCH_OFF_HARD_DELETION)
            raise
        else:
            cursor.execute(SWITCH_OFF_HARD_DELETION)


def _mti_table_chain(model: type[Model]) -> list[tuple[str, str]]:
    """``(db_table, pk_column)`` for every table in *model*'s MTI tree, leaf-first (FK-safe:
    a child's parent-link references its parent's row). Covers the whole tree, not just
    ancestors, so ``hard_delete`` from any level clears the chain with no orphan either way."""

    def _pk_column(m: type[Model]) -> str:
        column = m._meta.pk.column
        if column is None:  # pragma: no cover - always set on a concrete model's own pk
            raise TypeError(f'{m!r} has no primary key column')
        return column

    # One traversal, shared with ``_still_referenced``: the set of tables a chain's rows live
    # in and the set of models whose referrers hold those rows back have to be the same one.
    return [(m._meta.db_table, _pk_column(m)) for m in _mti_model_chain(model)]


class SoftDeleteUnsupportedError(Exception):
    """``soft_delete()`` on a model whose cascade reaches an edge no rule carries, so archiving
    in SQL would leave rows **live** under an archived parent. Use ``.delete()``."""


class HardDeleteIncompleteError(GuitarsError):
    """``hard_delete()`` removed fewer rows from a table than it collected, so it rolled back
    rather than commit part of the tree: a row hidden by a scope or policy, removed first by
    another transaction, or already gone. See ``docs/soft-deletion.md``'s "Hard deletion"."""


def _require_removed(table: str, collected: int, removed: int) -> None:
    if removed != collected:
        raise HardDeleteIncompleteError(
            f'hard_delete() removed {removed} of the {collected} rows it collected from {table}: '
            f'a tenant scope or row-level policy hides a row, another transaction removed one '
            f'first, or the row was already gone (one cause: a table with no soft-delete rule yet '
            f'loses it to delete(), so run makeguitarmigrations and migrate). Rolled back.'
        )


def _require_covered(model: type[Model], using: str) -> None:
    if connections[using].vendor != 'postgresql':
        raise SoftDeleteUnsupportedError(
            f'{model._meta.label}.soft_delete() on {using!r} ({connections[using].vendor}): the '
            f'cascade is PostgreSQL rules, so only the matched rows would be archived.'
        )
    blocking = [gap for gap in cascade_plan(model)[0] if gap.blocking]
    if blocking:
        listed = '; '.join(f'{gap.edge} ({gap.reason})' for gap in blocking)
        # ``.delete()`` is no way out of #64: its redirect rule archives another row too.
        advice = (
            'Fix the model first (guitars.E005).'
            if any('#64' in gap.reason for gap in blocking)
            else 'Use .delete(), which applies them in Python.'
        )
        raise SoftDeleteUnsupportedError(
            f'{model._meta.label}.soft_delete() would leave rows live under an archived parent: '
            f'{listed}. {advice}'
        )


def _write_alias(queryset: QuerySet) -> str:
    """The alias a write goes to: ``queryset.db`` is the read alias until ``_for_write`` is set."""
    chained = queryset._chain()  # ty: ignore[unresolved-attribute]
    chained._for_write = True
    return chained.db


# One statement per this many keys: PostgreSQL refuses a statement over 65535 parameters.
_PK_BATCH = 10_000


def _now() -> Func:
    """``NOW()``, the transaction's start, which every rule and trigger writes. Django's ``Now()``
    renders ``STATEMENT_TIMESTAMP()`` on PostgreSQL, so a descendant stamped by a rule would not
    carry the parent's value, and a revive keys on exactly that equality."""
    return Func(function='NOW', output_field=DateTimeField())


def _refuse_an_own_key(model: type[Model]) -> None:
    """``hard_delete()`` runs no system check, and its walk seeds the ancestor with the child's
    own key as if it were the link: for a model ``guitars.E005`` refuses it removed an unrelated
    row of the ancestor for good (#64)."""
    if refuses_pk_not_parent_link(model):
        raise ImproperlyConfigured(
            f"hard_delete() on '{model._meta.label}': it, or a model above it, declares a primary "
            f'key of its own beside its multi-table-inheritance parent link (guitars.E005), so '
            f'the walk would remove another row of the ancestor. Fix the model first.'
        )


def _guard_bulk(queryset: QuerySet, name: str) -> None:
    """Django's ``delete()`` guards, under the caller's own name."""
    queryset._not_support_combined_queries(name)  # ty: ignore[unresolved-attribute]
    if queryset.query.is_sliced:
        raise TypeError(f"Cannot use 'limit' or 'offset' with {name}().")
    if queryset.query.distinct_fields:
        raise TypeError(f'Cannot call {name}() after .distinct(*fields).')
    if queryset._fields is not None:  # ty: ignore[unresolved-attribute]
        raise TypeError(f'Cannot call {name}() after .values() or .values_list()')


def _matching_pks(queryset: QuerySet, field: str = 'pk') -> list:
    """The keys *queryset* matches, read before any rule runs: a rule's cascade runs ahead of the
    statement that fired it, so a ``WHERE`` reading what the cascade changes would skip the
    parent, and an aggregate or window cannot be a ``WHERE`` at all. The collector reads first too."""
    doomed = queryset._chain()  # ty: ignore[unresolved-attribute]
    doomed._for_write = True
    doomed.query.select_for_update = False
    doomed.query.select_related = False
    doomed.query.clear_ordering(force=True)
    return list(doomed.values_list(field, flat=True))


def _by_pk(model: type[Model], using: str, pks: list):
    """Querysets over *pks* in batches, on the plain base manager: the keys already carry the
    original filter, and re-applying it is the hazard above."""
    for start in range(0, len(pks), _PK_BATCH):
        yield model._base_manager.using(using).filter(pk__in=pks[start : start + _PK_BATCH])


def _fast_delete_applies(model: type[Model], using: str) -> bool:
    """Whether ``.delete()`` can be one ``DELETE`` the rules rewrite, ending where Django's
    collector would have. Asked per call: a receiver can be connected at runtime."""
    if not getattr(settings, 'GUITARS_DELETE_FAST_PATH', True):
        return False
    if connections[using].vendor != 'postgresql':
        return False
    gaps, reached = cascade_plan(model)
    if gaps:
        return False
    # *model* too, not only what the walk reached: it resolves a proxy to its concrete model, but
    # the collector signals with the class of the instances it loaded, which is the proxy.
    return not any(
        pre_delete.has_listeners(sender) or post_delete.has_listeners(sender)
        for sender in (model, *reached)
    )


class LiveQuerySet(QuerySet):
    """QuerySet scoped to live (non-deleted) records via ``_deleted_at IS NULL``."""

    @property
    def lives(self):
        return self.filter(_deleted_at__isnull=True)

    def soft_delete(self) -> int:
        """Archive the matching live rows with ``UPDATE``\\ s; the rules cascade them. Returns the
        number stamped. Skips ``on_delete`` and the delete signals, and raises
        ``SoftDeleteUnsupportedError`` where the rules alone leave rows live. See ``docs/soft-delete-api.md``."""
        using = _write_alias(self)
        _require_covered(self.model, using)
        _guard_bulk(self, 'soft_delete')
        # Stamped on the table holding the column, by its own key: through an MTI child, Django's
        # ``update()`` re-reads the keys and updates the ancestor by id alone, guard dropped.
        holder = column_owner(self.model, '_deleted_at')
        pks = _matching_pks(self.filter(_deleted_at__isnull=True), holder._meta.pk.name)
        # One transaction, as the collector's is: a failing batch leaves nothing half-archived,
        # and every batch's ``NOW()`` is the one instant.
        with transaction.atomic(using=using, savepoint=False):
            return sum(
                batch.filter(_deleted_at__isnull=True).update(_deleted_at=_now())
                for batch in _by_pk(holder, using, pks)
            )

    async def asoft_delete(self) -> int:
        return await sync_to_async(self.soft_delete)()

    # Never reachable from a manager: `Model.objects.soft_delete()` would archive the table.
    soft_delete.queryset_only = True  # ty: ignore[unresolved-attribute]
    asoft_delete.queryset_only = True  # ty: ignore[unresolved-attribute]

    def delete(self):
        """Django's ``delete()``, as ``DELETE``\\ s the rules rewrite where nothing is lost by it
        (``_fast_delete_applies``). Same return value, same guards, same end state."""
        using = _write_alias(self)
        if not _fast_delete_applies(self.model, using):
            return super().delete()
        _guard_bulk(self, 'delete')  # Django's own, ahead of the shortcut
        with transaction.atomic(using=using, savepoint=False):  # all batches or none
            for batch in _by_pk(self.model, using, _matching_pks(self)):
                batch._raw_delete(using=using)
        self._result_cache = None
        return 0, {}

    delete.alters_data = True  # ty: ignore[unresolved-attribute]
    delete.queryset_only = True  # ty: ignore[unresolved-attribute]


class LiveManager(Manager):
    """Default manager — only live records, via ``self._queryset_class`` (never a
    hard-coded name) -- load-bearing: ``tenanted_manager()`` swaps it for a guarded
    subclass, and naming ``LiveQuerySet`` directly would silently hand back an unguarded one."""

    _queryset_class = LiveQuerySet

    def get_queryset(self) -> LiveQuerySet:
        # ``self._queryset_class``, never the class named above -- see the class docstring.
        # ``_hints`` is a real runtime attribute django-stubs doesn't declare.
        return self._queryset_class(model=self.model, using=self._db, hints=self._hints).lives  # ty: ignore[unresolved-attribute]


class HardDeletableQuerySet(LiveQuerySet):
    """QuerySet that can access archived records and perform hard deletes -- see
    ``docs/soft-deletion.md``'s "Hard deletion" for the session-switch mechanism."""

    @property
    def archives(self):
        return self.filter(_deleted_at__isnull=False)

    def hard_delete(self):
        """Permanently remove matching rows. For an MTI model, also removes every other
        table in the chain by shared PK, regardless of level. Blunt: unlike instance
        ``hard_delete()``, this does not walk reverse-FK cascade children."""
        # First: compiled as a ``DELETE``, a slice, a combinator or ``DISTINCT ON`` is dropped, so
        # the statement removes rows other than the ones the queryset matches.
        _guard_bulk(self, 'hard_delete')
        model = self.model
        _refuse_an_own_key(model)
        if not _is_mti_model(model):
            return self._hard_delete_own_table()

        # The write alias, once: ``self.db`` is the read alias and is asked of the router afresh
        # each time, so the switch and the ``DELETE`` could otherwise land on different ones.
        using = _write_alias(self)
        # Distinct: a filter across a many-valued relation returns a key once per joined row, and
        # the count below is of rows.
        pks = list(dict.fromkeys(self.using(using).values_list('pk', flat=True)))
        if not pks:
            return None
        placeholders = ', '.join(['%s'] * len(pks))
        db_connection = connections[using]
        quote = db_connection.ops.quote_name
        # Every key has a row in the model's own table and each ancestor's; a descendant's
        # table holds rows only for the keys that are one, so it has no count to meet.
        own_chain = {m._meta.db_table for m in (model, *model._meta.get_parent_list())}
        with _hard_deletion_on(using), db_connection.cursor() as cursor:
            for table, pk_column in _mti_table_chain(model):
                # Identifiers come from model._meta (trusted); PK values are parameterized.
                sql_stmt = (
                    f'DELETE FROM {quote(table)} WHERE {quote(pk_column)} IN ({placeholders})'  # noqa: E501  # nosec B608
                )
                cursor.execute(sql_stmt, pks)
                # A table's row hidden or gone would leave the rest of the chain half removed.
                if table in own_chain:
                    _require_removed(table, len(pks), cursor.rowcount)
        return None

    # Marks `hard_delete` as queryset-only for Manager.from_queryset(); a valid runtime
    # attribute assignment on a function object that stub-based checkers can't model.
    hard_delete.queryset_only = True  # ty: ignore[unresolved-attribute]

    def _hard_delete_own_table(self):
        """Delete only this queryset's own-table rows, never an ancestor table's: the non-MTI
        queryset ``hard_delete``. Its own switch and ``atomic()``, or autocommit lets the
        switch expire before the DELETE it unlocks. Both on the write alias, resolved once."""
        using = _write_alias(self)
        where = self.query.where
        if where.contains_aggregate or where.contains_over_clause:
            # Not a ``WHERE`` at all (#73): read the keys, as ``delete()`` does, and remove those.
            return self._hard_delete_by_key(using)
        try:
            with _hard_deletion_on(using):
                self.using(using)._delete_own_table_rows()
        # ``none()``, ``pk__in=[]``: no SQL, nothing to remove. Here, not in the primitive: the
        # instance walk collected its rows, so a table compiling to nothing must abort it.
        except EmptyResultSet:
            pass
        return None

    def _hard_delete_by_key(self, using: str) -> None:
        """The matched keys, read first, removed in batches: a bare queryset, since the keys
        already carry this one's filter and scope, and every batch must remove all it names."""
        pks = list(dict.fromkeys(_matching_pks(self.using(using))))  # distinct, as the MTI form
        if not pks:
            return None
        with _hard_deletion_on(using):
            for start in range(0, len(pks), _PK_BATCH):
                batch = pks[start : start + _PK_BATCH]
                bare = HardDeletableQuerySet(model=self.model, using=using).filter(pk__in=batch)
                _require_removed(
                    self.model._meta.db_table, len(batch), bare._delete_own_table_rows()
                )
        return None

    def _delete_own_table_rows(self) -> int:
        """The ``DELETE`` alone, for a caller that already holds the switch (see
        :func:`_hard_deletion_on`): instance ``hard_delete`` runs every table under one. Returns
        the rows it removed."""
        with connections[self.db].cursor() as cursor:
            query = self.query.clone()
            query.__class__ = sql.DeleteQuery
            compiled, params = query.sql_with_params()
            cursor.execute(compiled, params)
            return cursor.rowcount


class ArchiveManager(Manager):
    """Manager that returns only soft-deleted records (``_deleted_at IS NOT NULL``)."""

    _queryset_class = HardDeletableQuerySet

    def get_queryset(self) -> HardDeletableQuerySet:
        return self._queryset_class(
            model=self.model,
            using=self._db,
            hints=self._hints,  # ty: ignore[unresolved-attribute]
        ).archives


class AllObjectsManager(Manager):
    """Manager returning every record, exposed as ``_all_objects``. ``.lives``/``.archives``
    are mirrored onto it so either half is reachable without ``get_queryset()`` first."""

    _queryset_class = HardDeletableQuerySet

    def get_queryset(self) -> HardDeletableQuerySet:
        return self._queryset_class(
            model=self.model,
            using=self._db,
            hints=self._hints,  # ty: ignore[unresolved-attribute]
        )

    @property
    def lives(self):
        return self.get_queryset().lives

    @property
    def archives(self):
        return self.get_queryset().archives


class SoftDeletableModel(Model):
    """Abstract model enabling PostgreSQL-level soft deletion. ``.delete()`` is intercepted
    by a generated rule that sets ``_deleted_at = NOW()`` instead of removing the row.
    Three managers: ``objects`` (live), ``_archives`` (deleted), ``_all_objects`` (both)."""

    _deleted_at = DateTimeField(
        verbose_name='Deleted at',
        null=True,
        editable=False,
    )

    objects = LiveManager()  # ```.objects``` attribute excludes "archived" records!

    _archives = ArchiveManager()  # ```.archived``` attribute excludes "active" records!
    _all_objects = AllObjectsManager()  # ```._all_objects``` attribute returns all records!

    class Meta:
        abstract = True
        default_manager_name = 'objects'
        indexes = [
            Index(
                fields=['_deleted_at'],
                condition=Q(_deleted_at__isnull=True),
                name='%(class)s_deleted_at',
            ),
        ]

    @property
    def is_deleted(self):
        return bool(self._deleted_at)

    @property
    def is_alive(self):
        return not self.is_deleted

    def delete(self, using=None, keep_parents=False):
        """Django's ``delete()``, as one ``DELETE`` the rules rewrite where nothing is lost by it
        -- see :meth:`LiveQuerySet.delete`. Clears the pk as Django does."""
        using = using or router.db_for_write(self.__class__, instance=self)
        if self.pk is None or not _fast_delete_applies(type(self), using):
            return super().delete(using=using, keep_parents=keep_parents)
        # Django's single-instance shortcut names the model; a model with dependents returns {}.
        leaf = Collector(using=using, origin=self).can_fast_delete(self)
        count = type(self)._base_manager.using(using).filter(pk=self.pk)._raw_delete(using)
        setattr(self, self._meta.pk.attname, None)
        return (count, {self._meta.label: count}) if leaf else (0, {})

    delete.alters_data = True  # ty: ignore[unresolved-attribute]

    def soft_delete(self, using=None) -> int:
        """Archive this row in one ``UPDATE`` and set ``_deleted_at`` (and ``_updated_at``) on
        the instance, keeping the pk -- the row still exists. Returns 1, or 0 if already archived.
        Same contract as :meth:`LiveQuerySet.soft_delete`."""
        if self.pk is None:
            raise ValueError(
                f"{self.__class__.__name__} object can't be soft-deleted because its "
                f'{self._meta.pk.attname} attribute is set to None.'
            )
        using = using or router.db_for_write(self.__class__, instance=self)
        _require_covered(type(self), using)
        # The holder's row, found *through this model's own table*, in one guarded statement: its
        # row-level policy is asked there (an MTI child's can hide a row the holder's would not),
        # and the guard stays beside the write.
        holder = column_owner(type(self), '_deleted_at')
        # ``_deleted_at IS NULL`` here too: it joins every table in the chain, so a middle table's
        # policy is asked as the queryset form asks it, not only the leaf's and the holder's.
        own = type(self)._base_manager.using(using).filter(pk=self.pk, _deleted_at__isnull=True)
        stamped = (
            holder._base_manager.using(using)
            .filter(pk__in=own.values(holder._meta.pk.name), _deleted_at__isnull=True)
            .update(_deleted_at=_now())
        )
        fields = [
            '_deleted_at',
            *(['_updated_at'] if has_column(type(self), '_updated_at') else []),
        ]
        with contextlib.suppress(type(self).DoesNotExist):  # row-level security hides the row
            self.refresh_from_db(using=using, fields=fields)
        return stamped

    soft_delete.alters_data = True  # ty: ignore[unresolved-attribute]

    async def asoft_delete(self, using=None) -> int:
        return await sync_to_async(self.soft_delete)(using=using)

    asoft_delete.alters_data = True  # ty: ignore[unresolved-attribute]

    def hard_delete(self):
        """Soft-delete first, then permanently remove this instance, its CASCADE-related rows,
        and whatever it owns -- see ``docs/soft-deletion.md``'s "Hard deletion". Children go
        before parents (CASCADE is Python-level); an owned row goes after its owner."""
        # Resolved as Phase 1's ``delete()`` resolves it -- the router before ``_state.db``, which
        # ``Model(pk=...)`` lacks -- so both phases land on one alias for a consistent router.
        _refuse_an_own_key(type(self))
        using = router.db_for_write(self.__class__, instance=self)
        pk = self.pk  # save before Phase 1 resets self.pk to None
        # One (rows, order) group per ownership hop: the first this row and its
        # reverse-CASCADE children, each later one an owned row. Run in order, since an
        # owner still references what it owns.
        groups: list[tuple[dict[type[Model], set], list[type[Model]]]] = []
        # Claimed across *all* groups, not per group: the per-group set-difference guard
        # would otherwise not stop two models that own each other from recurring forever.
        claimed: dict[type[Model], set] = defaultdict(set)

        def _collect_group(root: type[Model], seed: set) -> None:
            to_delete: dict[type[Model], set] = defaultdict(set)
            model_order: list[type[Model]] = []

            def _collect(model: type[Model], pks: set) -> None:
                new_pks = pks - claimed[model]
                if not new_pks:
                    return
                # The whole subtree below a self-referential key in one recursive query, so
                # that key is not walked a level at a time below.
                new_pks = _with_self_descendants(model, new_pks, using) - claimed[model]
                claimed[model].update(new_pks)
                to_delete[model].update(new_pks)
                followed = _self_cascade_fields(model, using)
                # ``_referring_relations``, not ``_meta.related_objects``: that drops a
                # ``related_name='+'`` key, leaving a hidden CASCADE child behind to dangle.
                # It is also the list ``_still_referenced`` discounts against.
                for relation in _referring_relations(model):
                    if relation.on_delete is not CASCADE:
                        continue
                    related_model = relation.related_model
                    field = cast('Field', relation.field)
                    if field in followed:
                        continue
                    # Through ``_key_values``, as ``_still_referenced`` reads the same relations:
                    # missing a ``to_field`` child is not a smaller collection but a broken one,
                    # discounted there *because* this collects it. An empty ``__in`` needs no guard.
                    keys = _key_values(field, new_pks, using)
                    child_pks = set(
                        _rows(related_model, using)
                        .filter(**{f'{field.attname}__in': keys})
                        .values_list('pk', flat=True)
                    )
                    # From the child's MTI *root*, as the seed and the owned hop both are:
                    # the declaring level alone strands its ancestors' rows. A parent-link
                    # walks *down* instead, and re-entering at its root collects nothing.
                    _collect(
                        related_model
                        if getattr(relation, 'parent_link', False)
                        else mti_root(related_model),
                        child_pks,
                    )
                # A ``GenericRelation`` lives in ``_meta.private_fields`` and owns no key column,
                # so ``_referring_relations`` cannot see it -- and must not: with no constraint to
                # fail at ``COMMIT`` it never holds a row back.

                # Collected all the same, or the child is left pointing at a primary key nothing
                # holds. Taken wherever the row it points at is going, which is every level this
                # walk reaches -- sparing happens in ``_owned_targets``, before a group is built.

                # Wider than Phase 1 by that rule, not equal to it: ``Collector`` returns before
                # its own ``private_fields`` walk when it collects an MTI *ancestor*, and an
                # owned target is stamped by the rule, which runs no ``Collector`` at all.

                # Duck-typed on ``bulk_related_objects``, as that ``Collector`` is, so nothing
                # here imports ``contenttypes`` -- an app a consumer need not have installed.
                generic = [
                    private
                    for private in model._meta.private_fields
                    if hasattr(private, 'bulk_related_objects')
                ]
                if generic:
                    # Read once for the whole set rather than per relation: the rows are the
                    # same either way, and this is the only place the walk needs instances.
                    instances = list(_rows(model, using).filter(pk__in=new_pks))
                    for private in generic:
                        generic_pks = set(
                            private.bulk_related_objects(instances, using).values_list(
                                'pk', flat=True
                            )
                        )
                        _collect(mti_root(private.related_model), generic_pks)
                if model not in model_order:
                    model_order.append(model)

            _collect(root, seed)
            groups.append((to_delete, model_order))

        # Start the DFS from the MTI root so ancestor tables (reachable only via the parent-link
        # reverse CASCADE relation) are collected too; ``root is self.__class__`` for non-MTI.
        root = mti_root(self.__class__)

        with transaction.atomic(using=using):
            # Phase 1 — soft-delete first (idempotent; PG rules cascade to related objects,
            # and stamp whatever this row was the last owner of). Still ``self.delete()``: an
            # override of it runs, and a survivor's ``_updated_at`` is the collector's to move.
            self.delete()

            # Phase 2 — collect related rows and hard-delete child-first. self.pk is None
            # after Phase 1 (Django clears it post-delete), so use the saved pk.
            _collect_group(root, {pk})
            # A fixpoint, not one pass: `_owned_targets` spares a row something outside the
            # batch references, and `claimed` grows as rounds run, so a row held back by a
            # not-yet-collected reference becomes collectable later.
            dispatched: dict[type[Model], set] = defaultdict(set)
            scan = _OwnedScan()  # one for the fixpoint: each round reads only what is new
            while True:
                fresh: list[tuple[type[Model], set]] = []
                for owned_model, owned_pks in _owned_targets(claimed, using, scan):
                    # `dispatched`, not `claimed`: every pk is collected at most once, which
                    # is what bounds this loop. `claimed` is keyed by the model actually
                    # collected, which for an MTI target is the root, not `owned_model`.
                    pending = owned_pks - dispatched[owned_model]
                    if pending:
                        dispatched[owned_model].update(pending)
                        fresh.append((owned_model, pending))
                if not fresh:
                    break
                for owned_model, owned_pks in fresh:
                    # Appended after every group already collected, which is the order the
                    # foreign keys need: whatever references an owned row is in an earlier one.
                    _collect_group(mti_root(owned_model), owned_pks)

            # One switch for every table, not one per table: it is transaction-local and the
            # walk is one transaction, so the per-table on/off was five statements a table
            # (savepoint, on, delete, off, release) for nothing a rollback does not already do.
            with _hard_deletion_on(using, savepoint=False):
                for to_delete, model_order in groups:
                    for model in model_order:
                        pks = list(to_delete[model])
                        # `_all_objects` is added dynamically by SoftDeletableModel subclasses, so a
                        # static checker can't see it -- or `_delete_own_table_rows` on its queryset --
                        # through the hasattr guard.
                        if hasattr(model, '_all_objects'):
                            # Own-table primitive: each MTI table is a separate ``model_order`` entry,
                            # so this must not reach into ancestor tables (which ``hard_delete`` would).
                            rows = model._all_objects.using(using).filter(  # ty: ignore[unresolved-attribute]
                                pk__in=pks
                            )
                            # A table compiling to nothing (``tenant(label=[])``) removed none.
                            try:
                                removed = rows._delete_own_table_rows()
                            except EmptyResultSet:
                                removed = 0
                            # Every row collected, or none: a hidden or vanished row (#72)
                            # rolls the walk back rather than commit the rest of the tree.
                            _require_removed(model._meta.db_table, len(pks), removed)
                        # A plain model under an owned row (an owned group runs no Collector): an
                        # m2m through row, or a plain MTI chain (`Amp`/`Gear` in `tests/testapp`).
                        else:
                            # Switched off, so a receiver archives soft-deletable rows; no ``finally``.
                            # Not counted (ADR 0032): the collector cascades by its own rules, so a
                            # row can go before its own entry; a row policy hides a row from both.
                            with connections[using].cursor() as cursor:
                                cursor.execute(SWITCH_OFF_HARD_DELETION)
                                _rows(model, using).filter(pk__in=pks).delete()
                                cursor.execute(SWITCH_ON_HARD_DELETION)
