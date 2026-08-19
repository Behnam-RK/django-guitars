"""``OwningForeignKey`` -- the one custom field the kit ships, declaring that a row **owns**
what its foreign key points at. Imports nothing from ``guitars``, so the generator reading
it does not drag the tenancy runtime in -- the same discipline as ``guitars.gucs``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core import checks
from django.db.models import CASCADE, ForeignKey


if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.core.checks import CheckMessage


__all__ = ['OwningForeignKey']


class OwningForeignKey(ForeignKey):
    """A ``ForeignKey`` whose row owns what it points at: soft-deleting the declaring row
    soft-deletes the target too, unless another live row still owns it. ``on_delete`` says
    the opposite -- see ``docs/owned-relations.md``."""

    def check(self, **kwargs: Any) -> list[CheckMessage]:
        return [*super().check(**kwargs), *self._check_on_delete_not_cascade()]

    def _check_on_delete_not_cascade(self) -> list[CheckMessage]:
        """``CASCADE`` and ownership are contradictory, not merely redundant -- reported as a
        check rather than raised at import so it surfaces the way every other model
        misconfiguration does."""
        if self.remote_field.on_delete is not CASCADE:
            return []
        return [
            checks.Error(
                'OwningForeignKey cannot use on_delete=CASCADE.',
                hint=(
                    'CASCADE means deleting the target deletes this row -- the opposite of '
                    'ownership -- and would also emit the soft-delete cascade rule in the '
                    'wrong direction. Use SET_NULL, PROTECT, RESTRICT or DO_NOTHING.'
                ),
                obj=self,
                id='guitars.E001',
            )
        ]

    def deconstruct(self) -> tuple[str, str, Sequence[Any], dict[str, Any]]:
        name, _path, args, kwargs = super().deconstruct()
        # Pinned to the public path rather than this module's: a generated migration records
        # the path literally, and is already applied in consuming projects, so moving this
        # file would break `migrate` on a fresh database there. The string is frozen.
        return name, 'guitars.models.OwningForeignKey', args, kwargs
