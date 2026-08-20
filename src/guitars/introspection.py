"""Which physical table actually holds a column, given Django's two inheritance styles --
see ``docs/mti.md``. ``hasattr`` is the wrong question: it answers yes for an MTI child
whose column lives on an ancestor's table, where a rule referencing it is invalid SQL."""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.db import models


__all__ = [
    'column_owner',
    'has_column',
    'is_mti_child',
    'mti_root',
    'owns_column',
    'rule_update_cycle_edges',
]


def has_column(model: type[models.Model], colname: str) -> bool:
    """Whether *colname* is reachable on *model* at all -- own table or inherited."""
    return hasattr(model, colname)


def owns_column(model: type[models.Model], colname: str) -> bool:
    """Whether *colname* is a column on *model*'s OWN table -- true for a declared or
    abstract-copied field, false for one inherited through MTI."""
    return any(field.name == colname for field in model._meta.local_fields)


def column_owner(model: type[models.Model], colname: str) -> type[models.Model]:
    """The concrete model whose physical table declares *colname* -- resolves the
    **owner**, not the immediate parent, since a chain three deep may need two hops up."""
    return model._meta.get_field(colname).model


def is_mti_child(model: type[models.Model], colname: str) -> bool:
    """Whether *model* reaches *colname* through an MTI ancestor's table."""
    return (
        bool(model._meta.parents)
        and has_column(model, colname)
        and not owns_column(model, colname)
    )


def mti_root(model: type[models.Model]) -> type[models.Model]:
    """The top of *model*'s MTI chain -- *model* itself if it has none. Every table shares
    one primary-key value, so a walk over the *whole* chain has to start at the root."""
    root = model
    while root._meta.parents:
        root = next(iter(root._meta.parents))
    return root


def _rule_update_edges(candidates: Iterable[type[models.Model]]) -> set[tuple[str, str]]:
    """``(fires_on_table, updates_table)`` for every ON UPDATE soft-delete rule *candidates* call
    for -- cascade and owned alike, each read off the model declaring the foreign key, so a partial
    *candidates* can only miss edges, never invent one. Imports deferred: see below."""
    # Deferred: every other name in this module comes from ``_meta`` alone, while
    # ``guitars.models.fields`` reaches ``guitars.models.__init__`` and the tenancy runtime
    # behind it -- a cost only a caller asking about rules should pay.
    from django.db.models import CASCADE, ForeignKey  # noqa: PLC0415 - see the comment above

    from guitars.models.fields import OwningForeignKey  # noqa: PLC0415 - see the comment above

    edges: set[tuple[str, str]] = set()
    for model in candidates:
        # Both rule kinds live on the table whose ``_deleted_at`` actually flips, so a model
        # that inherits the column declares no rule of its own -- its ancestor does.
        if not owns_column(model, '_deleted_at'):
            continue
        table = model._meta.db_table
        for field in model._meta.local_fields:
            if not isinstance(field, ForeignKey) or not has_column(
                field.related_model, '_deleted_at'
            ):
                continue
            target_table = column_owner(field.related_model, '_deleted_at')._meta.db_table
            if isinstance(field, OwningForeignKey):
                edges.add((table, target_table))  # owned: fires here, updates the target
            # Not ``elif``: one field reaches both generators. ``CASCADE`` on an
            # OwningForeignKey is ``guitars.E001``, but ``--skip-checks`` still reaches the
            # generator, and the two rules are each other's cycle -- both edges detect it.
            if field.remote_field.on_delete is CASCADE and not getattr(
                field.remote_field, 'parent_link', False
            ):
                edges.add((target_table, table))  # cascade: fires on the target, updates here
    return edges


def rule_update_cycle_edges(candidates: Iterable[type[models.Model]]) -> set[tuple[str, str]]:
    """The edges of ``_rule_update_edges`` lying on a cycle, which may never be written: a
    rule's action expands *before* the original statement, so a cycle is rewritten into itself
    and PostgreSQL refuses **every** ``UPDATE`` to every table in it, guard unread."""
    edges = _rule_update_edges(candidates)
    adjacency: dict[str, set[str]] = {}
    for source, target in edges:
        adjacency.setdefault(source, set()).add(target)

    def _reaches(start: str, goal: str) -> bool:
        stack = list(adjacency.get(start, ()))
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node == goal:  # a self-loop falls out of this: goal is its own target
                return True
            if node not in seen:
                seen.add(node)
                stack.extend(adjacency.get(node, ()))
        return False

    # *Every* edge on the cycle, not one chosen edge: which one got refused would otherwise
    # depend on iteration order, and an order-dependent refusal cannot stay stable run to
    # run -- the generator's `--check` would flap and `hard_delete()` would disagree with it.
    return {(source, target) for source, target in edges if _reaches(target, source)}
