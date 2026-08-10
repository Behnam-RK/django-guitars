"""Which physical table actually holds a column, given Django's two inheritance styles.

The kit's SQL is written against *tables*, so "does this model have ``_deleted_at``?"
is the wrong question -- ``hasattr`` answers yes for a multi-table-inheritance child
whose column lives on an ancestor's table, and a rule referencing a column the child
table does not have is invalid SQL.

The distinction Django draws:

* **Abstract base** -- fields are *copied onto* each concrete subclass, so they are
  local, and the concrete model owns its own column.
* **Multi-table inheritance** -- the child gets its own table whose primary key is a
  ``OneToOneField(parent_link=True)``; inherited fields are *not* local, and the
  column stays on the ancestor that declared it.

Two consumers depend on getting this right, which is why it lives here rather than
inside either of them: ``makeguitarmigrations`` (which table gets the trigger or
rule) and ``guitars.tenancy.discovery`` (which table can carry a row-level-security
policy predicating on a tenant column).
"""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from django.db import models


__all__ = ['column_owner', 'has_column', 'is_mti_child', 'mti_root', 'owns_column']


def has_column(model: type[models.Model], colname: str) -> bool:
    """Whether *colname* is reachable on *model* at all -- own table or inherited."""
    return hasattr(model, colname)


def owns_column(model: type[models.Model], colname: str) -> bool:
    """Whether *colname* is a column on *model*'s OWN table.

    True for a field declared on the model or copied from an abstract base; False for
    a field inherited through MTI, which physically lives on an ancestor's table.
    """
    return any(field.name == colname for field in model._meta.local_fields)


def column_owner(model: type[models.Model], colname: str) -> type[models.Model]:
    """The concrete model whose physical table declares *colname*.

    *model* itself for an own-table column; the owning ancestor for an MTI-inherited
    one. Note this resolves the **owner**, not the immediate parent -- in a chain
    three deep the column may live two tables up, and predicating against the
    immediate parent would reference a table that has no such column either.
    """
    return model._meta.get_field(colname).model


def is_mti_child(model: type[models.Model], colname: str) -> bool:
    """Whether *model* reaches *colname* through an MTI ancestor's table."""
    return (
        bool(model._meta.parents)
        and has_column(model, colname)
        and not owns_column(model, colname)
    )


def mti_root(model: type[models.Model]) -> type[models.Model]:
    """The top of *model*'s multi-table-inheritance chain -- *model* itself if it has none.

    Walks ``_meta.parents`` up to the model with no MTI parent of its own. Every table in
    an MTI chain shares the same primary-key value, so the root is where a walk over the
    *whole* chain (ancestors and descendants alike, not just ancestors) has to start --
    ``guitars.models.soft_deletion`` uses this to reach ancestor tables only otherwise
    visible through the parent-link reverse relation.
    """
    root = model
    while root._meta.parents:
        root = next(iter(root._meta.parents))
    return root
