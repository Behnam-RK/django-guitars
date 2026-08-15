"""Which physical table actually holds a column, given Django's two inheritance styles --
see ``docs/mti.md``. ``hasattr`` is the wrong question: it answers yes for an MTI child
whose column lives on an ancestor's table, where a rule referencing it is invalid SQL."""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from django.db import models


__all__ = ['column_owner', 'has_column', 'is_mti_child', 'mti_root', 'owns_column']


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
