"""Override of Django's ``makemigrations`` that also generates guitar migrations.

By default (``GUITARS_AUTO_MAKE_MIGRATIONS = True``) a single
``manage.py makemigrations`` produces both the core Django migrations and the
advanced trigger/rule migrations that ``makeguitarmigrations`` creates -- so the
soft-delete rules and ``updated_at`` triggers can never be silently forgotten.

Set ``GUITARS_AUTO_MAKE_MIGRATIONS = False`` to opt out and keep the explicit
two-command workflow (``makemigrations`` then ``makeguitarmigrations``).
"""

from __future__ import annotations

from django.conf import settings
from django.core.management import call_command
from django.core.management.commands.makemigrations import Command as MakeMigrationsCommand


class Command(MakeMigrationsCommand):
    def handle(self, *args, **options):
        # 1. Always run the real makemigrations first: core migrations must
        #    exist before the guitar migrations that depend on them.
        super().handle(*args, **options)

        # 2. Recursion + correctness guards. makeguitarmigrations scaffolds its
        #    migrations via `makemigrations --empty`, which re-enters THIS
        #    command; skipping on --empty breaks that cycle and is also the
        #    right behavior (an explicit empty migration should not trigger
        #    guitar generation). --dry-run: guitar has no no-write mode, so skip.
        if options.get('empty') or options.get('dry_run'):
            return

        # 3. Opt-out setting, default True for DX.
        if not getattr(settings, 'GUITARS_AUTO_MAKE_MIGRATIONS', True):
            return

        # 4. Delegate to the existing command; --check maps to guitar's check_only.
        #    Forward any positional app labels so a scoped `makemigrations blog`
        #    only generates guitar migrations for blog (mirrors Django scoping).
        call_command(
            'makeguitarmigrations',
            *args,
            check_only=options.get('check_changes', False),
            stdout=self.stdout,
            stderr=self.stderr,
        )
