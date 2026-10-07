"""Whether archiving a model's rows in SQL reaches what Django's collector would have, read off
the registry through ``introspection.classify_cascade`` -- the predicate the generator writes
its rules by, so the two cannot disagree about which keys carry one. See ``docs/soft-delete-api.md``."""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, NamedTuple, cast

from django.apps import apps as django_apps
from django.core.signals import setting_changed
from django.db.models import CASCADE, DO_NOTHING, ForeignKey
from django.db.models.deletion import get_candidate_relations_to_delete
from django.db.models.signals import class_prepared

from guitars.introspection import (
    CascadeKind,
    classify_cascade,
    column_owner,
    has_column,
    rule_update_cycle_edges,
)
from guitars.local_apps import is_local
from guitars.routing import migrates_to_postgresql

from .fields import OwningForeignKey, _targets_primary_key


if TYPE_CHECKING:
    from django.db.models import Model
    from django.db.models.fields.related import ForeignObject


__all__ = ['Gap', 'cascade_plan', 'clear_cascade_plan_cache']


class Gap(NamedTuple):
    """One edge where an archive in SQL and the collector part ways."""

    edge: str
    reason: str
    #: ``True`` where a rules-only archive would leave rows **live** under an archived parent,
    #: failing toward exposing data. ``False`` where Django applies something in Python (an
    #: ``on_delete``, a plain child's removal) that a rule never did, which is defined behaviour.
    blocking: bool


@cache
def _registry_cycle_edges() -> frozenset[tuple[str, str]]:
    return frozenset(rule_update_cycle_edges(django_apps.get_models()))


def clear_cascade_plan_cache() -> None:
    """Forget every cached plan -- for a test that changes what the registry or router says."""
    _registry_cycle_edges.cache_clear()
    cascade_plan.cache_clear()


def _chain(model: type[Model], holder: type[Model]) -> list[type[Model]]:
    """*model* and every ancestor down to *holder*: each one's own rule is written from the pass
    over its own app."""
    return [model, *(m for m in model._meta.get_parent_list() if issubclass(m, holder))]


def _own_key_levels(model: type[Model]) -> list[type[Model]]:
    """The models from *model* up to, not including, its column holder whose primary key is not
    their link to their parent (#64): a key into any model below one of them stores that key,
    where the rules on the root's table compare the root's id."""
    holder = column_owner(model, '_deleted_at')
    return [
        level
        for level in _chain(model, holder)
        if level is not holder and level._meta.pk not in level._meta.parents.values()
    ]


def _keys_into_it_store_the_roots_id(model: type[Model]) -> bool:
    """Whether a key into *model* holds the id the rules on the root's table compare it with."""
    return not _own_key_levels(model)


def _enforcement_gaps(model: type[Model]) -> list[Gap]:
    """A reached model whose own ``DELETE`` is not rewritten: nothing archives it. Every gap is
    reported, once: a non-blocking one (the fast path declines) must not hide a blocking one
    (``soft_delete()`` would leave rows live), and the same reason from two levels is one."""
    # Deferred: ``guitars.checks`` reaches the models package this module is part of.
    from guitars.checks import refuses_soft_delete_rule  # noqa: PLC0415

    if not has_column(model, '_deleted_at'):
        return [Gap(model._meta.label, 'is not soft-deletable', True)]
    holder = column_owner(model, '_deleted_at')
    gaps: dict[str, bool] = {}

    def add(reason: str, blocking: bool) -> None:
        gaps[reason] = gaps.get(reason, False) or blocking

    # Every model on the chain: a rule is written from the pass over its *own* app, so one outside
    # LOCAL_APPS or routed away has none even under a covered ancestor. Blocking only where a rule
    # is written from that model's app -- otherwise nothing is left live, the fast path declines.
    for owner in dict.fromkeys(_chain(model, holder)):
        needed = _needs_rules_from_its_app(owner)
        if not is_local(django_apps.get_app_config(owner._meta.app_label)):
            add(f"'{owner._meta.app_label}' is not in LOCAL_APPS", needed)
        if not migrates_to_postgresql(owner):
            add('is routed off PostgreSQL', needed)
    if refuses_soft_delete_rule(model):
        add('its chain is refused a soft-delete rule (guitars.E003)', True)
    # Own key not its link to the ancestor (#64): the redirect rule archives another row, and a
    # key into a model below it stores that key. The stamp is right, the cascade out of it is
    # not: blocking where a rule needs such a key, else a decline of the fast path.
    levels = _own_key_levels(model)
    if levels:
        reaching = any(
            _needs_rules_from_its_app(owner) and not _keys_into_it_store_the_roots_id(owner)
            for owner in _chain(model, holder)
            if owner is not holder
        )
        add('its primary key is not its parent link (#64, guitars.E005)', reaching)
    return [Gap(model._meta.label, reason, blocking) for reason, blocking in gaps.items()]


def _needs_rules_from_its_app(model: type[Model]) -> bool:
    """Whether a rule is written from this model's own app pass: a ``CASCADE`` key *to* it (from a
    model with ``_deleted_at``, not into an ancestor, not a parent link) or an ``OwningForeignKey``
    it declares. Read without the generator's refusals, so it only over-blocks."""
    concrete = model._meta.concrete_model or model
    if any(isinstance(field, OwningForeignKey) for field in concrete._meta.local_fields):
        return True
    return any(
        relation.field.remote_field.on_delete is CASCADE  # ty: ignore[unresolved-attribute]
        and not relation.field.remote_field.parent_link  # ty: ignore[unresolved-attribute]
        and (relation.model._meta.concrete_model or relation.model) is concrete  # ty: ignore[unresolved-attribute]
        and has_column(cast('type[Model]', relation.related_model), '_deleted_at')
        for relation in get_candidate_relations_to_delete(model._meta)
    )


@cache
def cascade_plan(model: type[Model]) -> tuple[tuple[Gap, ...], frozenset[type[Model]]]:
    """``(gaps, reached)`` for archiving *model*'s rows: where it parts from the collector, and
    every model the collector would have touched (ancestors included, for their signals)."""
    start = model._meta.concrete_model or model
    cycle_edges = _registry_cycle_edges()
    gaps: list[Gap] = []
    reached: set[type[Model]] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current in reached:
            continue
        reached.add(current)
        reached.update(current._meta.get_parent_list())
        gaps.extend(_enforcement_gaps(current))
        # An owned key stores its target's own key, and the owned rule stamps the root's row by
        # it (#64): no ``CASCADE`` edge carries it, so it is read off the declaring model here.
        gaps.extend(
            Gap(
                f'{current._meta.label}.{field.name}',
                'owns a model whose primary key is not its parent link (#64, guitars.E005)',
                True,
            )
            for field in current._meta.local_fields
            if isinstance(field, OwningForeignKey)
            and has_column(field.related_model, '_deleted_at')
            and not _keys_into_it_store_the_roots_id(field.related_model)
        )
        if not has_column(current, '_deleted_at'):
            continue
        for owner in (current, *current._meta.get_parent_list()):
            gaps.extend(
                Gap(f'{owner._meta.label}.{field.name}', 'is a generic relation', True)
                for field in owner._meta.private_fields
                if hasattr(field, 'bulk_related_objects')
            )
        for relation in get_candidate_relations_to_delete(current._meta):
            # A reverse relation always names the model declaring the key; the stubs type both
            # ends as optional / as a plain ``Field``.
            related = cast('type[Model]', relation.related_model)
            field = cast('ForeignObject', relation.field)  # ty: ignore[unresolved-attribute]
            # Structural: the child shares the row, so archiving it archives the child. Its own
            # relations still have to be walked.
            if field.remote_field.parent_link:
                stack.append(related)
                continue
            on_delete = field.remote_field.on_delete
            if on_delete is DO_NOTHING:
                continue
            edge = f'{related._meta.label}.{field.name} -> {current._meta.label}'
            if on_delete is not CASCADE:
                gaps.append(
                    Gap(
                        edge,
                        f'{getattr(on_delete, "__name__", on_delete)} is applied in Python',
                        False,
                    )
                )
            elif not has_column(related, '_deleted_at'):
                gaps.append(Gap(edge, f'{related._meta.label} is not soft-deletable', False))
            else:
                target = field.related_model._meta.concrete_model or field.related_model
                kind = classify_cascade(
                    related,
                    field,
                    on_delete,
                    column_owner(target, '_deleted_at')._meta.db_table,
                    set(cycle_edges),
                )
                if kind in (CascadeKind.RULE, CascadeKind.SELF) and not _targets_primary_key(
                    cast(ForeignKey, field)
                ):
                    # The rule correlates ``fk = old.<pk>``, so a ``to_field`` key archives nothing.
                    gaps.append(
                        Gap(
                            edge,
                            f'targets to_field {field.target_field.name!r}, not the primary key',
                            True,
                        )
                    )
                if kind is CascadeKind.SELF:
                    # Below the first level the trigger's own UPDATE runs at depth 1, where
                    # ``updated_at_trigger`` is suppressed; the collector's single depth-0
                    # statement stamps every child. Defined for ``soft_delete()``, not transparent.
                    gaps.append(
                        Gap(edge, 'self-referential: `_updated_at` below level one', False)
                    )
                if kind in (CascadeKind.RULE, CascadeKind.SELF):
                    stack.append(related)
                else:
                    gaps.append(Gap(edge, f'no rule is written for it ({kind.value})', True))
    return tuple(gaps), frozenset(reached)


def _invalidate(**kwargs) -> None:
    clear_cascade_plan_cache()


# A model registered later adds a relation to the models already planned, and a setting (the
# apps in LOCAL_APPS, a router) changes what each edge is -- so a plan is only as old as either.
class_prepared.connect(_invalidate, weak=False, dispatch_uid='guitars_cascade_plan_models')
setting_changed.connect(_invalidate, weak=False, dispatch_uid='guitars_cascade_plan_settings')
