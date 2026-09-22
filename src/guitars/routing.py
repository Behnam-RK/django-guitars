"""Whether this kit's PostgreSQL enforcement applies to a model, per the project's own
database router -- a leaf module for ``local_apps``' reason: ``tenancy`` and ``management``
both need it. ``migrate`` asks the same question of every ``RunSQL`` it applies."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.db import connections, router


if TYPE_CHECKING:
    from django.db.models import Model


__all__ = ['ENFORCEMENT_VENDOR', 'migrates_to_postgresql', 'migration_aliases', 'vendor_skip_note']

ENFORCEMENT_VENDOR = 'postgresql'


def migration_aliases(model: type[Model]) -> list[str]:
    """The aliases the router migrates *model* onto, in ``DATABASES`` order -- asked with
    ``allow_migrate_model``, which is what ``RunSQL.database_forwards`` consults, rather than
    ``db_for_write``, which answers where a *query* goes and defaults to ``default``."""
    return [alias for alias in connections if router.allow_migrate_model(alias, model)]


def migrates_to_postgresql(model: type[Model]) -> bool:
    """Whether any alias *model* migrates onto is PostgreSQL, so enforcement is worth
    generating. ``any``, not ``all``: a model on a PostgreSQL alias and another still needs
    its rules there, and withholding them leaves ``.delete()`` destroying rows."""
    # No router means no routing to consult, and returning early keeps the common case from
    # constructing a wrapper or importing a backend -- so the generator still opens nothing.
    if not getattr(settings, 'DATABASE_ROUTERS', None):
        return True
    return any(
        connections[alias].vendor == ENFORCEMENT_VENDOR for alias in migration_aliases(model)
    )


def vendor_skip_note(model: type[Model]) -> str:
    """The note for a model the router sends off PostgreSQL, in ``_skip_note``'s voice. One
    string per model, not one per caller: both the generator and tenancy discovery reach a
    tenanted model, and two spellings of one skip print as two findings."""
    aliases = migration_aliases(model)
    where = ', '.join(repr(alias) for alias in aliases) or 'no configured alias'
    vendors = ', '.join(sorted({connections[alias].vendor for alias in aliases}))
    carrying = f' ({vendors})' if vendors else ''
    return (
        f"'{model._meta.db_table}' skipped: the database router migrates "
        f"'{model._meta.label}' only to {where}{carrying}, and this kit's triggers, rules and "
        f'policies are PostgreSQL, so none is created for it. Python scoping still applies '
        f'where a tenanted manager declares it.'
    )
