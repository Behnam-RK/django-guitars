import contextlib
from collections import defaultdict

from django.db import connections, transaction
from django.db.models import CASCADE, DateTimeField, Index, Manager, Q, QuerySet, sql
from django.db.models.base import Model

from guitars.introspection import mti_root
from guitars.sql import SWITCH_OFF_HARD_DELETION, SWITCH_ON_HARD_DELETION


def _is_mti_model(model: type[Model]) -> bool:
    """Whether *model* participates in multi-table inheritance (as a child or a parent)."""
    return bool(model._meta.parents) or any(
        getattr(rel, 'parent_link', False) for rel in model._meta.related_objects
    )


def _mti_table_chain(model: type[Model]) -> list[tuple[str, str]]:
    """``(db_table, pk_column)`` for every table in *model*'s MTI tree, leaf-first (FK-safe:
    a child's parent-link references its parent's row). Covers the whole tree, not just
    ancestors, so ``hard_delete`` from any level clears the chain with no orphan either way."""

    def _pk_column(m: type[Model]) -> str:
        column = m._meta.pk.column
        if column is None:  # pragma: no cover - always set on a concrete model's own pk
            raise TypeError(f'{m!r} has no primary key column')
        return column

    root = mti_root(model)

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
        """Soft-delete first, then permanently remove this instance and CASCADE-related
        rows -- see ``docs/soft-deletion.md``'s "Hard deletion". Django's CASCADE is
        Python-level, not a DB constraint, so children are deleted before parents."""
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
        root = mti_root(self.__class__)

        with transaction.atomic(using=using):
            # Phase 1 — soft-delete first (idempotent; PG rules cascade to related objects).
            self.delete()

            # Phase 2 — collect related rows and hard-delete child-first. self.pk is None
            # after Phase 1 (Django clears it post-delete), so use the saved pk.
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
                else:  # pragma: no cover - unreachable
                    # A model with no soft-delete rule is always already gone by this point:
                    # Django's Collector (Phase 1's plain delete()) already issued a real
                    # DELETE for it. Kept as a defensive fallback, not a live path.
                    model._default_manager.using(using).filter(pk__in=pks).delete()
