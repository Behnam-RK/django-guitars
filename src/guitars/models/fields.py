"""``OwningForeignKey`` -- the one custom field the kit ships, declaring that a row **owns**
what its foreign key points at. Imports nothing from ``guitars`` itself, but reaching it runs
``guitars.models.__init__`` -- tidiness, not the hard isolation ``guitars.gucs`` holds to."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core import checks
from django.core.exceptions import FieldDoesNotExist
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
        return [
            *super().check(**kwargs),
            *self._check_on_delete_not_cascade(),
            *self._check_targets_the_primary_key(),
        ]

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

    def _check_targets_the_primary_key(self) -> list[CheckMessage]:
        """The rule correlates ``old."<fk column>"`` against the dependent's *primary key* --
        also what makes ownership into an MTI child work, a chain sharing one such value.
        Against any other column it stamps the wrong row, or none."""
        if isinstance(self.remote_field.model, str):  # pragma: no cover - lazy ref unresolved
            # super().check() reports the unresolvable reference; nothing here can run yet.
            return []
        try:
            target_field = self.target_field
        except FieldDoesNotExist:
            # `to_field` names a column the target does not have. super().check() has already
            # reported that as fields.E312; resolving it here would raise out of the check
            # framework, replacing every reported error with a traceback.
            return []
        related_pk = self.related_model._meta.pk
        if related_pk is None or target_field is related_pk:
            return []
        return [
            checks.Error(
                "OwningForeignKey must point at the target model's primary key.",
                hint=(
                    f"to_field='{target_field.name}' names a non-primary-key column, "
                    'but the generated rule correlates the foreign key against the target '
                    'primary key, so it would stamp the wrong row. Drop to_field.'
                ),
                obj=self,
                id='guitars.E002',
            )
        ]

    def deconstruct(self) -> tuple[str, str, Sequence[Any], dict[str, Any]]:
        name, _path, args, kwargs = super().deconstruct()
        # Pinned to the public path rather than this module's: a generated migration records
        # the path literally, and is already applied in consuming projects, so moving this
        # file would break `migrate` on a fresh database there. The string is frozen.
        return name, 'guitars.models.OwningForeignKey', args, kwargs
