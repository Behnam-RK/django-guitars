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
    own_key_between,
    rule_update_cycle_edges,
)
from guitars.local_apps import is_local
from guitars.routing import migrates_to_postgresql

from .fields import _targets_primary_key


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


def _enforcement_gaps(model: type[Model], *, has_inbound_keys: bool) -> list[Gap]:
    """A reached model whose own ``DELETE`` is not rewritten: nothing archives it. A model with
    no key cascading into it is only a gap for the fast path (*has_inbound_keys* false): the
    collector would delete its row, but ``soft_delete()`` leaves nothing live."""
    # Deferred: ``guitars.checks`` reaches the models package this module is part of.
    from guitars.checks import refuses_soft_delete_rule  # noqa: PLC0415

    if not has_column(model, '_deleted_at'):
        return [Gap(model._meta.label, 'is not soft-deletable', True)]
    holder = column_owner(model, '_deleted_at')
    # Every model on the chain: a redirect rule is written from the pass over its *own* app, so
    # a model outside LOCAL_APPS or routed away has none even under a covered ancestor.
    for owner in dict.fromkeys(_chain(model, holder)):
        if not is_local(django_apps.get_app_config(owner._meta.app_label)):
            reason = f"'{owner._meta.app_label}' is not in LOCAL_APPS"
            return [Gap(model._meta.label, reason, has_inbound_keys)]
        if not migrates_to_postgresql(owner):
            return [Gap(model._meta.label, 'is routed off PostgreSQL', has_inbound_keys)]
    if refuses_soft_delete_rule(model):
        return [
            Gap(model._meta.label, 'its chain is refused a soft-delete rule (guitars.E003)', True)
        ]
    # The redirect rule joins on the child's own key, which is not its link to the ancestor
    # when it declares one of its own (#64): it archives another row and leaves this one live.
    if model is not holder and (
        model._meta.pk is not model._meta.get_ancestor_link(holder)
        or own_key_between(model, holder) is not None
    ):
        return [Gap(model._meta.label, 'its primary key is not its parent link (#64)', True)]
    return []


def _has_inbound_cascade_keys(model: type[Model]) -> bool:
    """Whether a foreign key still cascades into *model* (a parent link is structural)."""
    return any(
        not relation.field.remote_field.parent_link  # ty: ignore[unresolved-attribute]
        and relation.field.remote_field.on_delete is not DO_NOTHING  # ty: ignore[unresolved-attribute]
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
        gaps.extend(
            _enforcement_gaps(current, has_inbound_keys=_has_inbound_cascade_keys(current))
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
