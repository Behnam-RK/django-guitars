"""Behavioural test for ``src/guitars/management/commands/migrate.py``. W001 only proves
the override is *installed* -- ``0016_migrate_override_probe.py`` already ran a real
cross-tenant backfill, leaving its result in a marker table read here."""

from __future__ import annotations

from tests.conftest import scalar as _scalar


def test_the_cross_tenant_backfill_migration_affected_both_rows(db):
    """Without ``migrate.py``'s ``tenancy_bypassed()``, this fails outright: ``migrate``
    never reaches the cross-tenant ``UPDATE`` since the first ``INSERT`` is rejected by
    the write guard / RLS -- confirmed by removing the wrapper and rebuilding the database."""
    affected = _scalar('SELECT affected_count FROM testapp_migrate_override_probe')
    assert affected == 2, (
        'tests/testapp/migrations/0016_migrate_override_probe.py backfills two rows under '
        'two different tenants in one statement with no tenant scope open anywhere -- '
        'this is only possible because migrate.py bypasses tenancy for its whole run'
    )
