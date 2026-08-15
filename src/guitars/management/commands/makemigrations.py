"""Override of Django's ``makemigrations`` that also generates enforcement migrations by
default (``GUITARS_AUTO_MAKE_MIGRATIONS = True``) -- until a soft-delete rule exists,
``.delete()`` destroys rows. ``= False`` for the explicit two-command workflow."""

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
        # 1. Schema migrations first, so enforcement has tables to attach to. If Django's
        # own --check finds schema changes, this exits before step 4, so a project with
        # both gaps only hears about the schema one first.
        super().handle(*args, **options)

        # 2. self.empty (Django's handle() sets it) breaks the recursion: makeguitarmigrations
        # scaffolds via `makemigrations --empty`, re-entering this command.
        if self.empty:
            return
        # options['dry_run'], not self.dry_run: Django's handle() forces self.dry_run = True
        # under --check too, which would silently skip step 4 on every --check run.
        if options['dry_run']:
            self.stdout.write('Enforcement-migration status was not checked because of --dry-run.')
            return

        # 3. Opt-out setting, default True for DX.
        if not getattr(settings, 'GUITARS_AUTO_MAKE_MIGRATIONS', True):
            return

        # 4. --force-rls deliberately NOT forwarded -- a staged-retrofit step run by hand.
        # options['check_changes'], not .get(...): a silently-wrong default on Django
        # renaming this private dest would run --check in write mode. Fail loud instead.
        call_command(
            'makeguitarmigrations',
            *args,
            check_only=options['check_changes'],
            stdout=self.stdout,
            stderr=self.stderr,
        )
