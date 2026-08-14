"""``migrate``, run with tenant enforcement bypassed -- otherwise a ``RunPython`` backfill
runs unscoped and is marked applied having silently done nothing. Requires guitars
**earliest** in ``INSTALLED_APPS``; ``guitars.tenancy.W001`` checks this isn't lost."""

from __future__ import annotations

from django.core.management.commands.migrate import Command as MigrateCommand

from guitars.tenancy import tenancy_bypassed


class Command(MigrateCommand):
    """Django's ``migrate``, wrapped in ``tenancy_bypassed()``."""

    def handle(self, *args, **options):
        with tenancy_bypassed():
            return super().handle(*args, **options)
