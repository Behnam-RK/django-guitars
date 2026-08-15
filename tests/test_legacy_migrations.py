"""Reproduces an already-migrated downstream project, with migrations matching the real
pre-1.1.0 shape (no ``[SQL:...]`` header identity). Regression guard for ``2ba86a3``: any
recognised header used to read as "covered forever" regardless of a matching digest."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from django.apps import apps
from django.core.management import CommandError, call_command
from django.test import override_settings

from tests.legacy_migrations.models import LegacyAlbum, LegacyBand


def _check(*app_labels) -> str:
    out, err = StringIO(), StringIO()
    call_command('makeguitarmigrations', *app_labels, '--check', stdout=out, stderr=err)
    return out.getvalue() + err.getvalue()


@pytest.mark.django_db(transaction=True)
@override_settings(LOCAL_APPS=['tests.testapp', 'tests.legacy_migrations'])
def test_upgrading_an_already_migrated_legacy_project():
    # The legacy migrations really did run -- a real, already-migrated database, not a
    # fixture that merely claims to be. If this fails, nothing below is testing anything.
    band = LegacyBand.objects.create(name='Legacy Band')
    band.refresh_from_db()
    assert band._updated_at is not None

    # The regression guard: a header-only match with no [SQL:...] must read as stale, not
    # as "covered forever". --check should refuse, not pass vacuously.
    with pytest.raises(CommandError, match='create missing migrations'):
        _check('legacy_migrations')

    migrations_dir = Path(apps.get_app_config('legacy_migrations').path) / 'migrations'
    before = set(migrations_dir.glob('*.py'))
    delta_file = None
    try:
        call_command('makeguitarmigrations', 'legacy_migrations', stdout=StringIO())

        new_files = set(migrations_dir.glob('*.py')) - before
        assert len(new_files) == 1, new_files
        delta_file = new_files.pop()
        content = delta_file.read_text()

        # Modern shape: SQL inlined, nothing left referencing back into the package --
        # a migration generated today is immune to a future change in the sql.X constants.
        assert 'from guitars import sql' not in content
        # One stamped identity per operation: band trigger, band rule, album trigger,
        # album rule, and the band<->album cascade rule.
        assert content.count('[SQL:') == 5, content
        # The *replace* form specifically, not a plain create: the generator knows this
        # object already exists (a header was found, just with no matching digest), and a
        # plain CREATE here would fail `migrate` with "already exists".
        assert 'DROP TRIGGER updated_at_trigger ON "legacy_migrations_legacyband"' in content

        call_command('migrate', 'legacy_migrations', stdout=StringIO())

        # The upgrade is complete: the same command that refused above now passes clean.
        _check('legacy_migrations')

        # And enforcement genuinely still works post-upgrade, not merely "no error" --
        # the trigger/rule this delta rewrote are the identical objects a live table needs.
        album = LegacyAlbum.objects.create(title='Legacy Album', band=band)
        pk = album.pk
        album.delete()
        assert LegacyAlbum._all_objects.filter(pk=pk, _deleted_at__isnull=False).exists()
    finally:
        if delta_file is not None:
            call_command('migrate', 'legacy_migrations', '0002_auto_advanced', stdout=StringIO())
            delta_file.unlink()
