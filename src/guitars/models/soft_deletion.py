import contextlib
from collections import defaultdict
from typing import cast

from django.apps import apps as django_apps
from django.db import connections, transaction
from django.db.models import (
    CASCADE,
    DateTimeField,
    Field,
    ForeignKey,
    Index,
    Manager,
    ManyToOneRel,
    Q,
    QuerySet,
    sql,
)
from django.db.models.base import Model

from guitars.introspection import (
    column_owner,
    has_column,
    mti_root,
    owned_tenancy_refusals,
    owns_column,
    rule_update_cycle_edges,
)
from guitars.sql import SWITCH_OFF_HARD_DELETION, SWITCH_ON_HARD_DELETION

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
    # Mirrors ``_owned_candidates``/``_owned_operations``' "nothing to stamp" and non-primary-key
    # refusals: no rule is emitted, so the relation is not followed -- following it destroys what
    # the rule spared, and under a redirected key destroys a row nothing ever owned.
    return [
        field
        for field in model._meta.local_fields
        if isinstance(field, OwningForeignKey)
        and has_column(field.related_model, '_deleted_at')
        and _targets_primary_key(field)
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


def _referring_relations(model: type[Model]) -> list:
    """Every reverse relation with a *column* pointing at *model* -- the one walk ``_collect``
    and :func:`_still_referenced` share, so what is collected and what holds a row back cannot
    disagree. ``include_hidden``: a ``related_name='+'`` key dangles too."""
    # ``ManyToOneRel`` (``OneToOneRel`` and the parent-link with it) is exactly the reverse of a
    # ForeignKey -- the only rel whose ``attname`` is the key column both callers read. An m2m
    # reverse owns none.

    # A ``GenericRelation`` cannot dangle, having no column to dangle by; ``_collect`` walks
    # ``_meta.private_fields`` separately to take those along -- see the doc.
    return [
        relation
        for relation in model._meta.get_fields(include_hidden=True)
        if isinstance(relation, ManyToOneRel)
    ]


def _cascade_closure(root: type[Model], pks: set, using: str | None) -> dict[type[Model], set]:
    """Every row, by model, that collecting *pks* of *root* takes along through reverse ``CASCADE``
    -- the walk ``_collect`` performs, to the same depth. One hop is not enough: a *grand*child goes
    too, and counting its plain key holds the row back forever, not for one fixpoint pass."""
    taken: dict[type[Model], set] = defaultdict(set)
    pending: list[tuple[type[Model], set]] = [(root, pks)]
    while pending:
        model, model_pks = pending.pop()
        fresh = model_pks - taken[model]
        if not fresh:
            continue
        taken[model].update(fresh)
        for relation in _referring_relations(model):
            if relation.on_delete is not CASCADE:
                continue
            field = cast('Field', relation.field)
            related_model = cast('type[Model]', relation.related_model)
            child_pks = set(
                _rows(related_model, using)
                .filter(**{f'{field.attname}__in': _key_values(field, fresh, using)})
                .values_list('pk', flat=True)
            )
            # From the child's MTI *root*, and a parent-link from the level it names -- the
            # same two cases ``_collect`` distinguishes, since this has to reach exactly the
            # rows it will. The declaring level alone would leave its ancestors' rows out.
            pending.append(
                (
                    related_model
                    if getattr(relation, 'parent_link', False)
                    else mti_root(related_model),
                    child_pks,
                )
            )
    return taken


def _still_referenced(
    target: type[Model], pks: set, claimed: dict[type[Model], set], using: str | None
) -> set:
    """Which of *pks* a row that outlives the collection still points at, through **any**
    foreign key, not only the one that declared ownership: removing a row is not stamping one,
    and a surviving key of any kind dangles at ``COMMIT`` and fails the deferred constraint."""
    # Rows collecting the chain takes along, by **row**, not relation: one model can hold a
    # ``CASCADE`` key *and* a plain one to the same target, and discounting the relation alone
    # held the target back forever. Whole closure, not one hop -- see ``_cascade_closure``.
    taken = _cascade_closure(mti_root(target), pks, using)
    referenced: set = set()
    # Every model in *target*'s MTI tree, not *target* alone: collecting it removes the whole
    # chain, so a key into any level holds the same pk value and dangles just as hard. Deduped --
    # an inherited relation is reported per level, and one read of it answers for them all.
    relations = dict.fromkeys(
        relation for level in _mti_model_chain(target) for relation in _referring_relations(level)
    )
    for relation in relations:
        # A parent-link is the same object one table down, collected with the chain; a
        # ``CASCADE`` referrer goes with the row it points at, and is in ``taken`` above.
        if getattr(relation, 'parent_link', False) or relation.on_delete is CASCADE:
            continue
        related_model = cast('type[Model]', relation.related_model)
        field = cast('Field', relation.field)
        # No emptiness guard: an ``__in`` over no keys is an empty result either way.
        keys = _key_values(field, pks, using)
        rows = _rows(related_model, using).filter(**{f'{field.attname}__in': keys})
        going = claimed.get(related_model, set()) | taken.get(related_model, set())
        if going:
            rows = rows.exclude(pk__in=going)
        referenced.update(keys[value] for value in rows.values_list(field.attname, flat=True))
        if referenced >= pks:  # nothing left to spare; skip the remaining relations
            return referenced
    return referenced


def _owned_targets(
    claimed: dict[type[Model], set], using: str | None
) -> list[tuple[type[Model], set]]:
    """``(model, pks)`` for every owned row *claimed* is the last owner of -- the rule's
    ``NOT EXISTS``, narrowed three ways below because this *removes* the row where the rule
    only stamps a column. *claimed* is every row going away, not one group's; see below."""
    found: dict[type[Model], set] = defaultdict(set)
    # The cheap half first: the graph below sweeps the whole registry, and ``hard_delete`` runs
    # this to a fixpoint over models that nearly all own nothing. ``pks`` too -- ``claimed`` is
    # a defaultdict, so a model looked at and found empty must not buy that sweep either.
    owning = {
        model: pks for model, pks in claimed.items() if pks and _declared_owning_fields(model)
    }
    if not owning:
        return []
    # Once per call, not once per claimed model: the graph is registry-wide and identical for
    # every one of them. The claimed models are named alongside the registry for the same
    # reason ``_owned_fields`` names its own -- they may not be registered.
    swept = [*claimed, *django_apps.get_models()]
    cycles = rule_update_cycle_edges(swept)
    # The tenancy half of the same shared answer, and a second sweep of the registry -- paid per
    # round like the graph above, ``claimed`` growing as rounds run. Skipped outright where
    # ``GUITARS_TENANT_POLICIES`` is off; otherwise cheapest where no model is tenanted.
    tenancy_refusals = owned_tenancy_refusals(swept)
    for model, pks in owning.items():
        for field in _owned_fields(model, cycles, tenancy_refusals):
            owned_pks = set(
                _rows(model, using)
                .filter(pk__in=pks)
                .exclude(**{field.attname: None})
                .values_list(field.attname, flat=True)
            )
            if not owned_pks:
                continue
            # Narrowed: (1) the whole claimed batch is spared, not one row -- all of it is
            # going; (2) no `_deleted_at` filter, an archived referrer's key is still on disk;
            # (3) *any* surviving reference holds the row back, not just the owning column.
            candidates = owned_pks
            # A *shrinking* fixpoint, not one subtraction: a pk spared here keeps its CASCADE
            # closure alive, so a referrer inside it survives after all and holds another pk
            # back. Each round is a strict subset of the last, which is what terminates it.
            while candidates:
                referenced = _still_referenced(field.related_model, candidates, claimed, using)
                if not referenced:
                    break
                candidates = candidates - referenced
            # Guarded: ``found`` is a defaultdict, so an unguarded ``update`` would mint a
            # ``(model, set())`` row for a relation that spared everything, and the caller
            # would enter a fixpoint round over rows that do not exist.
            if candidates:
                found[field.related_model].update(candidates)
    return list(found.items())


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


class LiveQuerySet(QuerySet):
    """QuerySet scoped to live (non-deleted) records via ``_deleted_at IS NULL``."""

    @property
    def lives(self):
        return self.filter(_deleted_at__isnull=True)


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
        model = self.model
        if not _is_mti_model(model):
            return self._hard_delete_own_table()

        pks = list(self.values_list('pk', flat=True))
        if not pks:
            return None
        placeholders = ', '.join(['%s'] * len(pks))
        db_connection = connections[self.db]
        quote = db_connection.ops.quote_name
        with db_connection.cursor() as cursor, transaction.atomic(using=self.db):
            cursor.execute(SWITCH_ON_HARD_DELETION)
            try:
                for table, pk_column in _mti_table_chain(model):
                    # Identifiers come from model._meta (trusted); PK values are parameterized.
                    sql_stmt = (
                        f'DELETE FROM {quote(table)} WHERE {quote(pk_column)} IN ({placeholders})'  # noqa: E501  # nosec B608
                    )
                    cursor.execute(sql_stmt, pks)
            except Exception:
                # Suppressed here (unlike the success path below): the transaction is
                # likely already aborted, so a failing switch-off would only replace the
                # real error. See docs/soft-deletion.md on the leaked-switch danger.
                with contextlib.suppress(Exception):
                    cursor.execute(SWITCH_OFF_HARD_DELETION)
                raise
            else:
                # Not suppressed: a failed switch-off here must abort the transaction, or
                # 'rules.hard_deletion' leaks 'on' for the rest of any enclosing transaction.
                cursor.execute(SWITCH_OFF_HARD_DELETION)
            return None

    # Marks `hard_delete` as queryset-only for Manager.from_queryset(); a valid runtime
    # attribute assignment on a function object that stub-based checkers can't model.
    hard_delete.queryset_only = True  # ty: ignore[unresolved-attribute]

    def _hard_delete_own_table(self):
        """Delete only this queryset's own-table rows -- used per table by instance
        ``hard_delete``, so this must never reach into ancestor tables. Its own
        ``atomic()``, or autocommit lets the switch expire before the DELETE it unlocks."""
        with connections[self.db].cursor() as cursor:
            query = self.query.clone()
            query.__class__ = sql.DeleteQuery
            compiled, params = query.sql_with_params()
            with transaction.atomic(using=self.db):
                cursor.execute(SWITCH_ON_HARD_DELETION)
                try:
                    result = cursor.execute(compiled, params)
                except Exception:
                    with contextlib.suppress(Exception):
                        cursor.execute(SWITCH_OFF_HARD_DELETION)
                    raise
                else:
                    cursor.execute(SWITCH_OFF_HARD_DELETION)
                return result


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

    def hard_delete(self):
        """Soft-delete first, then permanently remove this instance, its CASCADE-related rows,
        and whatever it owns -- see ``docs/soft-deletion.md``'s "Hard deletion". Children go
        before parents (CASCADE is Python-level); an owned row goes after its owner."""
        using = self._state.db
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
                claimed[model].update(new_pks)
                to_delete[model].update(new_pks)
                # ``_referring_relations``, not ``_meta.related_objects``: that drops a
                # ``related_name='+'`` key, leaving a hidden CASCADE child behind to dangle.
                # It is also the list ``_still_referenced`` discounts against.
                for relation in _referring_relations(model):
                    if relation.on_delete is not CASCADE:
                        continue
                    related_model = relation.related_model
                    field = cast('Field', relation.field)
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

                # Collected all the same, as Phase 1's ``Collector`` collects it, or the child is
                # archived and then left pointing at a primary key nothing holds.

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
            # and stamp whatever this row was the last owner of).
            self.delete()

            # Phase 2 — collect related rows and hard-delete child-first. self.pk is None
            # after Phase 1 (Django clears it post-delete), so use the saved pk.
            _collect_group(root, {pk})
            # A fixpoint, not one pass: `_owned_targets` spares a row something outside the
            # batch references, and `claimed` grows as rounds run, so a row held back by a
            # not-yet-collected reference becomes collectable later.
            dispatched: dict[type[Model], set] = defaultdict(set)
            while True:
                fresh: list[tuple[type[Model], set]] = []
                for owned_model, owned_pks in _owned_targets(claimed, using):
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

            for to_delete, model_order in groups:
                for model in model_order:
                    pks = list(to_delete[model])
                    # `_all_objects` is added dynamically by SoftDeletableModel subclasses, so a
                    # static checker can't see it -- or `_hard_delete_own_table` on its queryset --
                    # through the hasattr guard.
                    if hasattr(model, '_all_objects'):
                        # Own-table primitive: each MTI table is a separate ``model_order`` entry,
                        # so this must not reach into ancestor tables (which ``hard_delete`` would).
                        model._all_objects.using(using).filter(  # ty: ignore[unresolved-attribute]
                            pk__in=pks
                        )._hard_delete_own_table()
                    # `no cover` because no *test* model reaches it, not because nothing can: an
                    # owned group runs no Collector, so an m2m through row of an owned row lands
                    # here for real -- add one to `tests/testapp` before trusting this path.
                    else:  # pragma: no cover - no testapp owned model carries an m2m
                        # Read through ``_rows``, which no default manager can filter.
                        _rows(model, using).filter(pk__in=pks).delete()
