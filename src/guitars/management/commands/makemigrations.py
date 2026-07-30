"""Override of Django's ``makemigrations`` that also generates the enforcement migrations.

By default (``GUITARS_AUTO_MAKE_MIGRATIONS = True``) a single ``manage.py makemigrations``
produces both Django's own schema migrations and the enforcement migrations that
``makeguitarmigrations`` creates -- timestamp triggers, soft-delete rules and tenant
policies -- so none of them can be silently forgotten. That matters most for the
soft-delete rule: until it exists in the database, ``.delete()`` destroys rows.

Set ``GUITARS_AUTO_MAKE_MIGRATIONS = False`` to opt out and keep the explicit two-command
workflow (``makemigrations`` then ``makeguitarmigrations``).
"""

from __future__ import annotations

from django.conf import settings
from django.core.management import call_command
from django.core.management.commands.makemigrations import Command as MakeMigrationsCommand


class Command(MakeMigrationsCommand):
    def handle(self, *args, **options):
        # 1. Always run the real makemigrations first: the schema migrations must exist
        #    before the enforcement migrations that attach behaviour to those tables.
        super().handle(*args, **options)

        # 2. Recursion + correctness guards. makeguitarmigrations scaffolds its migrations
        #    via `makemigrations --empty`, which re-enters THIS command; skipping on --empty
        #    breaks that cycle and is also the right behaviour (an explicit empty migration
        #    should not trigger generation). --dry-run: the generator has no no-write mode.
        if options.get('empty') or options.get('dry_run'):
            return

        # 3. Opt-out setting, default True for DX.
        if not getattr(settings, 'GUITARS_AUTO_MAKE_MIGRATIONS', True):
            return

        # 4. Delegate to the generator; --check maps to its check_only. Forward any
        #    positional app labels so a scoped `makemigrations blog` scopes the enforcement
        #    step the same way (mirroring Django's own scoping).
        #
        #    --force-rls is deliberately NOT forwarded: it is a staged-retrofit step run by
        #    hand once a soak is clean, not something a routine makemigrations should do.
        call_command(
            'makeguitarmigrations',
            *args,
            check_only=options.get('check_changes', False),
            stdout=self.stdout,
            stderr=self.stderr,
        )
