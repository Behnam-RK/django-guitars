"""``migrate``, run with tenant enforcement bypassed.

Migrations are inherently cross-tenant: a ``RunPython`` backfill rewrites every tenant's
rows, and no tenant scope is -- or could be -- active while it runs. Under row-level
security that is not a loud failure. The policy simply matches nothing, so the ``UPDATE``
reports zero rows and the migration is marked applied. **A backfill that silently did
nothing is the worst outcome available**, because it surfaces much later as missing data
with a green migration history pointing away from the cause.

Bypassing here is safe in a way it would not be at runtime: ``migrate`` is
operator-invoked, single-purpose, and already trusted with DDL.

This override is why guitars must be in ``INSTALLED_APPS`` *ahead of* anything else
providing a ``migrate`` command for a tenanted project. Django's ``get_commands()`` walks
``reversed(apps.get_app_configs())`` and lets each app overwrite the previous entry, so
the app appearing **earliest** in ``INSTALLED_APPS`` is the one that wins. The same
ordering rule applies to the ``makemigrations`` override alongside this one.

Losing that race is silent, so it is checked rather than left to the reader of this
docstring: ``guitars.tenancy.W001`` resolves the ``migrate`` that would actually run and
warns when it is not an instance of the class below.
"""

from __future__ import annotations

from django.core.management.commands.migrate import Command as MigrateCommand

from guitars.tenancy import tenancy_bypassed


class Command(MigrateCommand):
    """Django's ``migrate``, wrapped in ``tenancy_bypassed()``."""

    def handle(self, *args, **options):
        with tenancy_bypassed():
            return super().handle(*args, **options)
