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
    Q,
    QuerySet,
    sql,
)
from django.db.models.base import Model

from guitars.introspection import (
    column_owner,
    has_column,
    mti_root,
    owns_column,
    rule_update_cycle_edges,
)
from guitars.sql import SWITCH_OFF_HARD_DELETION, SWITCH_ON_HARD_DELETION

from .fields import OwningForeignKey


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
    # Mirrors ``_owned_candidates``/``_owned_operations``' "nothing to stamp" refusal: no rule
    # is emitted, so the relation is not followed -- following it destroys what the rule spared.
    return [
        field
        for field in model._meta.local_fields
        if isinstance(field, OwningForeignKey) and has_column(field.related_model, '_deleted_at')
    ]


def _owned_fields(
    model: type[Model], cycles: set[tuple[str, str]] | None = None
) -> list[OwningForeignKey]:
    """``OwningForeignKey``s that actually carry a rule: a relation the generator refused has
    none, so following it here destroys what the rule spared. *cycles* is the graph below,
    passed in by a caller asking about several models so it is built once."""
    declared = _declared_owning_fields(model)
    # Before the graph, not after: building it sweeps the whole model registry, and most
    # models declare no ownership at all -- ``hard_delete`` asks this of every collected one.
    if not declared:
        return []
    table = model._meta.db_table
    # The same graph the generator refuses cycle edges from -- a self-owning relation is the
    # 1-cycle in it. Shared rather than re-derived, so the two cannot disagree about which
    # relations carry a rule. *model* is named too: it may not be a registered one.
    if cycles is None:
        cycles = rule_update_cycle_edges([model, *django_apps.get_models()])
    return [
        field
        for field in declared
        if (table, column_owner(field.related_model, '_deleted_at')._meta.db_table) not in cycles
    ]


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


def _key_values(level: type[Model], field: Field, pks: set, using: str | None) -> dict:
    """``{value a key into *level* holds: the pk it stands for}`` for *pks*. Identity for the
    usual key, which holds a primary key -- what every table in an MTI chain shares. A
    ``to_field`` key holds *that* column, matches no pk, and would read as absent."""
    target = field.target_field if isinstance(field, ForeignKey) else None
    if target is None or target.primary_key:
        return {pk: pk for pk in pks}
    return dict(_rows(level, using).filter(pk__in=pks).values_list(target.attname, 'pk'))


def _still_referenced(
    target: type[Model], pks: set, claimed: dict[type[Model], set], using: str | None
) -> set:
    """Which of *pks* a row that outlives the collection still points at, through **any**
    foreign key, not only the one that declared ownership: removing a row is not stamping one,
    and a surviving key of any kind dangles at ``COMMIT`` and fails the deferred constraint."""
    referenced: set = set()
    # Every model in *target*'s MTI tree, not *target* alone: collecting it removes the whole
    # table chain, so a key into any other level holds the same pk value and dangles just as
    # hard -- and ``get_fields`` reports only the level it is asked about.
    for level in _mti_model_chain(target):
        # ``include_hidden``: a ``related_name='+'`` foreign key is left out of
        # ``related_objects``, and its column dangles exactly like any other.
        for relation in level._meta.get_fields(include_hidden=True):
            if not (relation.is_relation and relation.auto_created and not relation.concrete):
                continue
            # A ManyToManyRel owns no column; the through table's key arrives as a separate
            # hidden relation, counted below -- nothing collects a through row for an owned
            # target. A parent-link is the same object one table down, collected with the chain.
            if relation.many_to_many or getattr(relation, 'parent_link', False):
                continue
            # A CASCADE referrer `_collect` follows is collected *with* the target, so nothing
            # of it survives to dangle. Discounted here, not left to the fixpoint: it is only
            # ever collected as a consequence of collecting the row it would hold back.
            if getattr(relation, 'on_delete', None) is CASCADE and not relation.hidden:
                continue
            related_model = cast('type[Model]', relation.related_model)
            field = cast('Field', relation.field)  # ty: ignore[unresolved-attribute]
            attname = field.attname
            keys = _key_values(level, field, pks, using)
            if not keys:
                continue
            rows = _rows(related_model, using).filter(**{f'{attname}__in': keys})
            going = claimed.get(related_model, set())
            if going:
                rows = rows.exclude(pk__in=going)
            referenced.update(keys[value] for value in rows.values_list(attname, flat=True))
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
    # The cheap half of the question first: building the graph below sweeps the whole model
    # registry, and ``hard_delete`` runs this to a fixpoint on every soft-deletable model,
    # nearly all of which own nothing. Nothing declared means nothing to ask the graph about.
    owning = {model: pks for model, pks in claimed.items() if _declared_owning_fields(model)}
    if not owning:
        return []
    # Once per call, not once per claimed model: the graph is registry-wide and identical for
    # every one of them. The claimed models are named alongside the registry for the same
    # reason ``_owned_fields`` names its own -- they may not be registered.
    cycles = rule_update_cycle_edges([*claimed, *django_apps.get_models()])
    for model, pks in owning.items():
        for field in _owned_fields(model, cycles):
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
            found[field.related_model].update(
                owned_pks - _still_referenced(field.related_model, owned_pks, claimed, using)
            )
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
                for relation in model._meta.related_objects:
                    if relation.on_delete is not CASCADE:
                        continue
                    related_model = relation.related_model
                    field = cast('Field', relation.field)
                    # Through ``_key_values``, exactly as ``_still_referenced`` reads the same
                    # relations: missing a ``to_field`` child here is not a smaller collection
                    # but a broken one -- it is discounted there *because* this collects it.
                    keys = _key_values(model, field, new_pks, using)
                    if not keys:
                        continue
                    child_pks = set(
                        _rows(related_model, using)
                        .filter(**{f'{field.attname}__in': keys})
                        .values_list('pk', flat=True)
                    )
                    _collect(related_model, child_pks)
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
                    else:  # pragma: no cover - unreachable
                        # A model with no soft-delete rule is always already gone by this point:
                        # Django's Collector (Phase 1's plain delete()) already issued a real
                        # DELETE for it. Kept as a defensive fallback, not a live path.
                        model._default_manager.using(using).filter(pk__in=pks).delete()
