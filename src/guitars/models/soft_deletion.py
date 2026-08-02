import contextlib
from collections import defaultdict

from django.db import connections, transaction
from django.db.models import CASCADE, DateTimeField, Index, Manager, Q, QuerySet, sql
from django.db.models.base import Model

from guitars.sql import SWITCH_OFF_HARD_DELETION, SWITCH_ON_HARD_DELETION


def _is_mti_model(model: type[Model]) -> bool:
    """Whether *model* participates in multi-table inheritance (as a child or a parent)."""
    return bool(model._meta.parents) or any(
        getattr(rel, 'parent_link', False) for rel in model._meta.related_objects
    )


def _mti_table_chain(model: type[Model]) -> list[tuple[str, str]]:
    """Return ``(db_table, pk_column)`` for every table in *model*'s MTI tree, leaf-first.

    Walks up to the inheritance **root** (via ``_meta.parents``) then DFS-descends through every
    MTI child (the ``parent_link`` reverse relations), emitting each table child-before-parent.
    Every table in an MTI chain shares the same primary-key value, so the same ``pk`` list filters
    every level. Leaf-first ordering is FK-safe: a child table's parent-link references its parent's
    row. Covering the whole tree (not just ancestors) means ``hard_delete`` on *any* level -- root,
    middle, or leaf -- clears the entire chain with no orphaned row left in either direction. A
    single-table model yields ``[(own_table, own_pk)]``.
    """

    def _pk_column(m: type[Model]) -> str:
        column = m._meta.pk.column
        if column is None:  # pragma: no cover - always set on a concrete model's own pk
            raise TypeError(f'{m!r} has no primary key column')
        return column

    root = model
    while root._meta.parents:
        root = next(iter(root._meta.parents))

    chain: list[tuple[str, str]] = []
    seen: set[type[Model]] = set()

    def _visit(m: type[Model]) -> None:
        if m in seen:
            return
        seen.add(m)
        for rel in m._meta.related_objects:
            if getattr(rel, 'parent_link', False):
                _visit(rel.related_model)  # a more-derived MTI child table
        chain.append((m._meta.db_table, _pk_column(m)))

    _visit(root)  # post-order from the root -> children appended before their parent
    return chain


class LiveQuerySet(QuerySet):
    """QuerySet scoped to live (non-deleted) records via ``_deleted_at IS NULL``."""

    @property
    def lives(self):
        return self.filter(_deleted_at__isnull=True)


class LiveManager(Manager):
    """Default manager — returns only live records (``_deleted_at IS NULL``).

    These three managers override ``get_queryset()`` for one reason only: to append the
    ``.lives`` / ``.archives`` filter. Everything else is Django's, and that includes
    **instantiating ``self._queryset_class`` rather than a hard-coded class name**.

    That is load-bearing, not style. ``_queryset_class`` is Django's documented seam for
    swapping the queryset a manager hands out, and a subclass that sets it expects to be
    obeyed — ``guitars.tenancy.TenantedManager`` sets it to a subclass whose
    ``bulk_create`` carries the tenant write guard, then calls ``super().get_queryset()``.
    Naming ``LiveQuerySet`` here directly would hand back an unguarded queryset while the
    manager still advertised the guarded one: a security guard that reads as installed and
    silently does nothing. Covered by
    ``tests/test_soft_deletion.py::TestManagerQuerySetClass``.
    """

    _queryset_class = LiveQuerySet

    def get_queryset(self) -> LiveQuerySet:
        # ``self._queryset_class``, never the class named above -- see the note on
        # ``_queryset_class`` below. ``_hints`` is a real runtime attribute (set in
        # Manager.__init__) that django-stubs doesn't declare.
        return self._queryset_class(model=self.model, using=self._db, hints=self._hints).lives  # ty: ignore[unresolved-attribute]


class HardDeletableQuerySet(LiveQuerySet):
    """QuerySet that can access archived records and perform hard deletes.

    ``.hard_delete()`` temporarily sets the PostgreSQL session variable
    ``rules.hard_deletion = 'on'`` so the soft-delete rule is bypassed,
    then executes a real ``DELETE`` statement inside a transaction.
    """

    @property
    def archives(self):
        return self.filter(_deleted_at__isnull=False)

    def hard_delete(self):
        """Permanently remove matching rows from the database.

        For a multi-table-inheritance model this also removes the corresponding rows from every
        other table in the inheritance chain (descendants and ancestors, leaf-to-root by shared PK)
        so no orphaned row is left behind, regardless of which level the queryset is on. Like the
        single-table path, this is a blunt instrument: it does not walk reverse-FK cascade children
        (other than the MTI chain itself) -- callers needing that should use instance
        ``hard_delete()``.
        """
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
            finally:
                # See _hard_delete_own_table for why this is suppressed rather than
                # left to raise: if a DELETE above failed, the transaction is already
                # aborted and this statement would only replace that error with its own.
                with contextlib.suppress(Exception):
                    cursor.execute(SWITCH_OFF_HARD_DELETION)
            return None

    # Marks `hard_delete` as queryset-only for Manager.from_queryset(); a valid runtime
    # attribute assignment on a function object that stub-based checkers can't model.
    hard_delete.queryset_only = True  # ty: ignore[unresolved-attribute]

    def _hard_delete_own_table(self):
        """Delete only this queryset's own-table rows (the single-table primitive).

        Used both for non-MTI models and, per model, by instance-level ``hard_delete`` -- which
        collects the whole MTI chain into its own child-first ``model_order`` and deletes each
        table separately, so this must never reach into ancestor tables.

        Three statements, three ``execute`` calls -- like the MTI path above, and not
        splice-able back into one string: a parameterised multi-statement ``execute`` only
        works under client-side binding, so one call would break the moment a consumer sets
        psycopg's ``server_side_binding`` option. ``atomic()`` is what keeps the split safe
        even without the ``finally`` below: the switch is transaction-local, and in
        autocommit each statement would otherwise be its own transaction -- the switch
        expiring before the DELETE it exists to unlock, which then archives instead of
        deleting. The ``finally`` makes turning the switch back off a guarantee of this
        function rather than a side effect of whatever ``atomic()`` block happens to be
        enclosing it -- on the failure path the DELETE has already aborted the
        transaction, so the switch-off attempt there would only raise its own error in
        place of the real one, hence the suppression.
        """
        with connections[self.db].cursor() as cursor:
            query = self.query.clone()
            query.__class__ = sql.DeleteQuery
            compiled, params = query.sql_with_params()
            with transaction.atomic(using=self.db):
                cursor.execute(SWITCH_ON_HARD_DELETION)
                try:
                    result = cursor.execute(compiled, params)
                finally:
                    with contextlib.suppress(Exception):
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
    """Manager that returns every record, live and soft-deleted alike.

    The unfiltered view, exposed as ``_all_objects``. ``.lives`` and ``.archives`` are
    mirrored onto the manager so either half is reachable without going through
    ``get_queryset()`` first.
    """

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
    """Abstract model that enables PostgreSQL-level soft deletion.

    Deletion logic lives entirely in the database via PostgreSQL rules
    generated by ``makeguitarmigrations``. Calling Django's ``.delete()``
    is intercepted by a rule that sets ``_deleted_at = NOW()`` instead of
    removing the row.

    Three managers control record visibility:

    - ``objects`` (``LiveManager``) — only live records (default).
    - ``_archives`` (``ArchiveManager``) — only soft-deleted records.
    - ``_all_objects`` (``AllObjectsManager``) — everything.
    """

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
    def cls(self):
        return self.__class__

    @property
    def is_deleted(self):
        return bool(self._deleted_at)

    @property
    def is_alive(self):
        return not self.is_deleted

    def hard_delete(self):
        """Soft-delete first, then permanently remove this instance and all CASCADE-related rows.

        Two-phase approach:
        1. ``self.delete()`` — triggers the PG soft-delete rule, which also fires the PG
           cascade-soft-delete rules for every related ``SoftDeletableModel``.  The call is
           idempotent: the rule's ``WHERE _deleted_at IS NULL`` guard makes it a no-op when
           the row is already soft-deleted.
        2. DFS collection + hard-delete — walks ``on_delete=CASCADE`` FK relations via
           ``_all_objects`` (so already-soft-deleted rows are included), builds a child-first
           deletion order, and bulk-hard-deletes each model's rows inside one transaction.

        For a multi-table-inheritance instance the DFS starts from the MTI **root** (with the
        shared PK): the parent-link reverse relation is itself an ``on_delete=CASCADE`` relation,
        so every table in the chain (and any CASCADE child of any ancestor) is collected into
        the same child-first order and each table is hard-deleted separately -- no orphaned
        parent row, no FK violation.

        Note: Django's ``on_delete=CASCADE`` is Python-level (``Collector``-based).  Django
        does **not** create ``ON DELETE CASCADE`` constraints in PostgreSQL, so a raw DELETE
        on the parent would be rejected by the DB's FK check.  That is why we must collect
        and delete children before parents ourselves.
        """
        using = self._state.db
        pk = self.pk  # save before Phase 1 resets self.pk to None
        to_delete: dict[type[Model], set] = defaultdict(set)
        model_order: list[type[Model]] = []

        def _collect(model: type[Model], pks: set) -> None:
            new_pks = pks - to_delete[model]
            if not new_pks:
                return
            to_delete[model].update(new_pks)
            for relation in model._meta.related_objects:
                if relation.on_delete is not CASCADE:
                    continue
                related_model = relation.related_model
                # `_all_objects` is added dynamically by SoftDeletableModel subclasses, so the
                # hasattr guard's type narrowing doesn't survive into `mgr`'s inferred type.
                mgr = (
                    related_model._all_objects
                    if hasattr(related_model, '_all_objects')
                    else related_model._default_manager
                )
                child_pks = set(
                    mgr.using(using)  # ty: ignore[unresolved-attribute]
                    .filter(**{f'{relation.field.name}__in': new_pks})
                    .values_list('pk', flat=True)
                )
                _collect(related_model, child_pks)
            if model not in model_order:
                model_order.append(model)

        # Start the DFS from the MTI root so ancestor tables (reachable only via the parent-link
        # reverse CASCADE relation) are collected too; ``root is self.__class__`` for non-MTI.
        root = self.__class__
        while root._meta.parents:
            root = next(iter(root._meta.parents))

        with transaction.atomic():
            # Phase 1 — soft-delete first (idempotent; PG rules cascade to related objects).
            self.delete()

            # Phase 2 — collect all related rows (now all soft-deleted) and hard-delete
            # in child-first order so no FK constraint is violated.
            # NOTE: self.pk is None after Phase 1 (Django clears it post-delete), use saved pk;
            # the PK is shared across the whole MTI chain, so it filters every level.
            _collect(root, {pk})

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
                else:  # pragma: no cover - unreachable: see note below
                    # A CASCADE-related model with no soft-delete rule of its own is always
                    # already gone by this point -- Django's Collector (triggered by Phase 1's
                    # plain ``self.delete()``) walks the *entire* CASCADE graph reachable from
                    # any level of the MTI chain (ancestors' own dependents included, since it
                    # also recurses into ``_meta.parents``) and issues a real, unintercepted
                    # DELETE for any table without a rule. Kept for symmetry with the
                    # ``_all_objects`` branch above and as a defensive fallback.
                    model._default_manager.using(using).filter(pk__in=pks).delete()
