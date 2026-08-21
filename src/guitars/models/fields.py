"""``OwningForeignKey`` -- the one custom field the kit ships, declaring that a row **owns**
what its foreign key points at. Imports nothing from ``guitars`` itself, but reaching it runs
``guitars.models.__init__`` -- tidiness, not the hard isolation ``guitars.gucs`` holds to."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core import checks
from django.core.exceptions import FieldDoesNotExist
from django.db.models import CASCADE, SET_DEFAULT, SET_NULL, ForeignKey


if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.core.checks import CheckMessage


__all__ = ['OwningForeignKey']


def _targets_primary_key(field: ForeignKey) -> bool:
    """Whether *field* correlates against its target's primary key, which every owned rule
    assumes. ``guitars.E002`` reports otherwise, but ``--skip-checks`` still reaches the
    generator and ``hard_delete()`` runs no checks at all, so both re-ask this."""
    try:
        target_field = field.target_field
    except FieldDoesNotExist:
        # `to_field` names a column the target does not have. Reported as fields.E312, and
        # the model is unusable either way; resolving it here would raise out of the check
        # framework, replacing every reported error with a traceback.
        return False
    related_pk = field.related_model._meta.pk
    return related_pk is None or target_field is related_pk


class OwningForeignKey(ForeignKey):
    """A ``ForeignKey`` whose row owns what it points at: soft-deleting the declaring row
    soft-deletes the target too, unless another live row still owns it. ``on_delete`` says
    the opposite -- see ``docs/owned-relations.md``."""

    def check(self, **kwargs: Any) -> list[CheckMessage]:
        return [
            *super().check(**kwargs),
            *self._check_on_delete_not_cascade(),
            *self._check_on_delete_keeps_the_key(),
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
                    'wrong direction. Use DO_NOTHING, PROTECT or RESTRICT.'
                ),
                obj=self,
                id='guitars.E001',
            )
        ]

    def _check_on_delete_keeps_the_key(self) -> list[CheckMessage]:
        """A warning, not an error: an ``on_delete`` that *clears* this column loses the only
        route back to the archived target. Legal, occasionally wanted, and silent -- the row is
        archived on time and simply becomes uncollectable, which no later check can spot."""
        on_delete = self.remote_field.on_delete
        # ``SET(value)`` is a closure, so identity alone would miss it -- and it clears the key
        # exactly as its two named siblings do. Matched by name, the only handle it offers.
        clears_key = on_delete in {SET_NULL, SET_DEFAULT} or (
            getattr(on_delete, '__name__', '') == 'set_on_delete'
        )
        if not clears_key:
            return []
        return [
            checks.Warning(
                'OwningForeignKey with an on_delete that clears the key loses the target.',
                hint=(
                    "Deleting the *target* runs Django's Collector, which clears this column "
                    'on every owner before the rule turns the DELETE into an UPDATE. The '
                    'archived row is then unreachable from its former owners and hard_delete() '
                    'can no longer collect it. Use DO_NOTHING, PROTECT or RESTRICT.'
                ),
                obj=self,
                id='guitars.W001',
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
            # `to_field` names a column the target does not have -- reported as fields.E312.
            # Resolved here rather than left to the helper so naming it in the message below
            # cannot raise out of the check framework and replace every error with a traceback.
            return []
        if _targets_primary_key(self):
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
        name, path, args, kwargs = super().deconstruct()
        # Pinned to the public path rather than this module's: a migration records it
        # literally, so moving this file would break `migrate` on a fresh database in a
        # consuming project. Frozen -- and only for *this* class; see the class docstring.
        if type(self) is OwningForeignKey:
            return name, 'guitars.models.OwningForeignKey', args, kwargs
        return name, path, args, kwargs
