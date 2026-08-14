"""The incremental-upgrade case: an MTI child added to an already-current app. ``Ancestor``
is fully enforced; ``Descendant`` is schema-only, before ``makeguitarmigrations`` re-runs.
Catches a scan mistaking "has an enforcement migration" for "fully covered"."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from django.apps import apps
from django.core.management import CommandError, call_command
from django.test import override_settings


def _check(*app_labels) -> str:
    out, err = StringIO(), StringIO()
    call_command('makeguitarmigrations', *app_labels, '--check', stdout=out, stderr=err)
    return out.getvalue() + err.getvalue()


@pytest.mark.django_db(transaction=True)
@override_settings(LOCAL_APPS=['tests.testapp', 'tests.mti_incremental'])
def test_only_the_new_mti_child_is_reported_missing_and_generated():
    out, err = StringIO(), StringIO()
    with pytest.raises(CommandError):
        call_command('makeguitarmigrations', 'mti_incremental', '--check', stdout=out, stderr=err)
    report = out.getvalue() + err.getvalue()
    assert 'mti_incremental_descendant' in report
    # Ancestor's own, already-current coverage is not re-reported. It IS legitimately
    # named as the *parent* inside the child's own MTI header/trigger, so the own-table
    # header specifically (no "(parent ...)" suffix) is what must be absent.
    assert 'Updated at Trigger on "mti_incremental_ancestor" table!' not in report
    assert 'Soft Delete Rule on "mti_incremental_ancestor" table!' not in report

    migrations_dir = Path(apps.get_app_config('mti_incremental').path) / 'migrations'
    before = set(migrations_dir.glob('*.py'))
    delta_file = None
    try:
        call_command('makeguitarmigrations', 'mti_incremental', stdout=StringIO())

        new_files = set(migrations_dir.glob('*.py')) - before
        assert len(new_files) == 1, new_files
        delta_file = new_files.pop()
        content = delta_file.read_text()

        # Only the child's own MTI operations -- nothing re-emitted for Ancestor's table.
        assert 'mti_incremental_descendant' in content
        assert 'parent "mti_incremental_ancestor"' in content
        assert 'Updated at Trigger on "mti_incremental_ancestor" table!' not in content
        assert 'Soft Delete Rule on "mti_incremental_ancestor" table!' not in content

        call_command('migrate', 'mti_incremental', stdout=StringIO())

        # The upgrade is complete.
        _check('mti_incremental')
    finally:
        if delta_file is not None:
            call_command('migrate', 'mti_incremental', '0003_descendant', stdout=StringIO())
            delta_file.unlink()
