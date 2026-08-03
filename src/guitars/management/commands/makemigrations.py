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
    help = (
        MakeMigrationsCommand.help
        + ' Also generates the enforcement migrations (timestamp triggers, soft-delete '
        'rules, tenant policies) unless GUITARS_AUTO_MAKE_MIGRATIONS is False.'
    )

    def handle(self, *args, **options):
        # 1. Always run the real makemigrations first: the schema migrations must exist
        #    before the enforcement migrations that attach behaviour to those tables.
        #
        #    If Django's own --check finds changes, this raises SystemExit(1) before the
        #    guitar step below ever runs -- so --check currently cannot report both layers
        #    failing in the same invocation, only whichever one Django's half catches first.
        super().handle(*args, **options)

        # 2. Recursion + correctness guards. makeguitarmigrations scaffolds its migrations
        #    via `makemigrations --empty`, which re-enters THIS command; skipping on --empty
        #    breaks that cycle and is also the right behaviour (an explicit empty migration
        #    should not trigger generation). self.empty/self.dry_run are the attributes
        #    Django's own handle() (just called above) sets from the same options.
        if self.empty:
            return
        if self.dry_run:
            # The generator has no no-write mode of its own, so it cannot honestly report
            # under --dry-run -- but silently skipping it left the one command a cautious
            # operator runs to preview changes unable to say a soft-delete rule is missing.
            self.stdout.write('Enforcement-migration status was not checked because of --dry-run.')
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
        #
        #    options['check_changes'] rather than .get(..., False): the dest is a private
        #    argparse detail of Django's own --check flag, and a silently-wrong default
        #    would make --check run in write mode the moment a future Django release
        #    renames it. Indexing fails loudly instead.
        call_command(
            'makeguitarmigrations',
            *args,
            check_only=options['check_changes'],
            stdout=self.stdout,
            stderr=self.stderr,
        )
