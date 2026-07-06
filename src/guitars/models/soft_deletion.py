from collections import defaultdict

from django.db import connection, transaction
from django.db.models import CASCADE, DateTimeField, Index, Manager, Q, QuerySet, sql
from django.db.models.base import Model

from guitars.sql import SWITCH_OFF_HARD_DELETION, SWITCH_ON_HARD_DELETION


def _mti_table_chain(model: type[Model]) -> list[tuple[str, str]]:
    """Return ``(db_table, pk_column)`` for *model* and each MTI ancestor, leaf-first.

    Empty ancestor list for a single-table model (``[(own_table, own_pk)]``). Every table in an
    MTI chain shares the same primary-key value, so the same ``pk`` list filters every level.
    Leaf-first ordering is FK-safe: a child table's parent-link references its parent's row.
    """
    chain = [(model._meta.db_table, model._meta.pk.column)]  # ty:ignore[unresolved-attribute]
    current = model
    while current._meta.parents:  # ty:ignore[unresolved-attribute]
        parent = next(iter(current._meta.parents))  # ty:ignore[unresolved-attribute]
        chain.append((parent._meta.db_table, parent._meta.pk.column))
        current = parent
    return chain


class LiveQuerySet(QuerySet):
    """QuerySet scoped to live (non-deleted) records via ``_deleted_at IS NULL``."""

    @property
    def lives(self):
        return self.filter(_deleted_at__isnull=True)


class LiveManager(Manager):
    """Default manager — returns only live records (``_deleted_at IS NULL``)."""

    _queryset_class = LiveQuerySet

    def get_queryset(self) -> LiveQuerySet:
        return LiveQuerySet(model=self.model, using=self._db, hints=self._hints).lives


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
        ancestor table (leaf-to-root, by shared PK) so no orphaned parent row is left behind.
        Like the single-table path, this is a blunt instrument: it does not walk reverse-FK
        cascade children -- callers needing that should use instance ``hard_delete()``.
        """
        model = self.model
        if not model._meta.parents:  # ty:ignore[unresolved-attribute]
            return self._hard_delete_own_table()

        pks = list(self.values_list('pk', flat=True))
        if not pks:
            return None
        placeholders = ', '.join(['%s'] * len(pks))
        quote = connection.ops.quote_name
        with connection.cursor() as cursor, transaction.atomic():
            cursor.execute(SWITCH_ON_HARD_DELETION)
            for table, pk_column in _mti_table_chain(model):  # ty:ignore[invalid-argument-type]
                # Identifiers come from model._meta (trusted); the PK values are parameterized.
                sql_stmt = (
                    f'DELETE FROM {quote(table)} WHERE {quote(pk_column)} IN ({placeholders})'  # noqa: E501  # nosec B608
                )
                cursor.execute(sql_stmt, pks)
            cursor.execute(SWITCH_OFF_HARD_DELETION)
            return None

    hard_delete.queryset_only = True  # ty:ignore[unresolved-attribute]

    def _hard_delete_own_table(self):
        """Delete only this queryset's own-table rows (the single-table primitive).

        Used both for non-MTI models and, per model, by instance-level ``hard_delete`` -- which
        collects the whole MTI chain into its own child-first ``model_order`` and deletes each
        table separately, so this must never reach into ancestor tables.
        """
        with connection.cursor() as cursor:
            query = self.query.clone()
            query.__class__ = sql.DeleteQuery
            compiled, params = query.sql_with_params()
            with transaction.atomic():
                return cursor.execute(
                    f'{SWITCH_ON_HARD_DELETION}\n{compiled};\n{SWITCH_OFF_HARD_DELETION}', params
                )


class ArchiveManager(Manager):
    """Manager that returns only soft-deleted records (``_deleted_at IS NOT NULL``)."""

    _queryset_class = HardDeletableQuerySet

    def get_queryset(self) -> HardDeletableQuerySet:
        return HardDeletableQuerySet(model=self.model, using=self._db, hints=self._hints).archives


class AllObjectsManager(Manager):
    """ """

    _queryset_class = HardDeletableQuerySet

    def get_queryset(self) -> HardDeletableQuerySet:
        return HardDeletableQuerySet(model=self.model, using=self._db, hints=self._hints)

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
    - ``_all_objects`` (``AllObjectManager``) — everything.
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
        to_delete: dict[type, set] = defaultdict(set)
        model_order: list[type] = []

        def _collect(model: type, pks: set) -> None:
            new_pks = pks - to_delete[model]
            if not new_pks:
                return
            to_delete[model].update(new_pks)
            for relation in model._meta.related_objects:  # ty:ignore[unresolved-attribute]
                if relation.on_delete is not CASCADE:
                    continue
                related_model = relation.related_model
                mgr = (
                    related_model._all_objects
                    if hasattr(related_model, '_all_objects')
                    else related_model._default_manager
                )
                child_pks = set(
                    mgr.using(using)
                    .filter(**{f'{relation.field.name}__in': new_pks})
                    .values_list('pk', flat=True)
                )
                _collect(related_model, child_pks)
            if model not in model_order:
                model_order.append(model)

        # Start the DFS from the MTI root so ancestor tables (reachable only via the parent-link
        # reverse CASCADE relation) are collected too; ``root is self.__class__`` for non-MTI.
        root = self.__class__
        while root._meta.parents:  # ty:ignore[unresolved-attribute]
            root = next(iter(root._meta.parents))  # ty:ignore[unresolved-attribute]

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
                if hasattr(model, '_all_objects'):
                    # Own-table primitive: each MTI table is a separate ``model_order`` entry,
                    # so this must not reach into ancestor tables (which ``hard_delete`` would).
                    model._all_objects.using(using).filter(pk__in=pks)._hard_delete_own_table()  # ty:ignore[unresolved-attribute]
                else:
                    model._default_manager.using(using).filter(pk__in=pks).delete()  # ty:ignore[unresolved-attribute]
