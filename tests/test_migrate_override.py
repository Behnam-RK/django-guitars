"""Behavioural test for ``src/guitars/management/commands/migrate.py``.

``guitars.tenancy.W001`` (see ``tests/test_tenancy_checks.py``) only proves the override is
*installed* -- that the ``migrate`` command Django would actually run is an instance of
this package's subclass. It says nothing about what that subclass's ``tenancy_bypassed()``
wrapper actually does to a real migration. ``tests/testapp/migrations/
0016_migrate_override_probe.py`` is the other half: a real ``RunPython`` step that backfills
across two different tenants in one statement, with no tenant scope open anywhere, which
only touches both because this override bypasses tenancy for the whole run. It already ran
once, for real, as part of this database's own migration history (before any test's data
existed) -- the same way it would for any consuming project -- and left its result in a
dedicated one-row marker table for this test to read.
"""

from __future__ import annotations

from tests.conftest import scalar as _scalar


def test_the_cross_tenant_backfill_migration_affected_both_rows(db):
    """Without ``migrate.py``'s ``tenancy_bypassed()``, this fails outright rather than
    silently: ``migrate`` never reaches the cross-tenant ``UPDATE`` at all, because the
    very first ``INSERT`` (creating a row under a tenant with no scope open) is rejected
    by the write guard / RLS policy on ``testapp_release``. Confirmed by temporarily
    removing the wrapper and rebuilding the test database, which failed the whole session
    at migrate time with exactly that error.
    """
    affected = _scalar('SELECT affected_count FROM testapp_migrate_override_probe')
    assert affected == 2, (
        'tests/testapp/migrations/0016_migrate_override_probe.py backfills two rows under '
        'two different tenants in one statement with no tenant scope open anywhere -- '
        'this is only possible because migrate.py bypasses tenancy for its whole run'
    )
