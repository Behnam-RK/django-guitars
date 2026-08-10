"""Shared fixtures for the tenancy tests.

Two tenants, always. A single-tenant fixture cannot tell "the scope works" from "there was
only ever one tenant's data", which is the failure mode that matters: every assertion here
is really about the rows the caller must *not* see.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

import pytest
from django.db import connection

from guitars.tenancy import tenancy_bypassed, tenant
from tests.testapp.models import Booking, Label, Release, StadiumTour, Track


def pytest_configure(config: pytest.Config) -> None:
    """test_ladder.py runs some probes in a subprocess (see its module docstring for why).

    Setting COVERAGE_PROCESS_START before those subprocesses spawn makes coverage.py's own
    site-packages shim (installed by the `coverage` package itself, not something added
    here) start tracking inside each one; [tool.coverage.run] parallel=true is what then
    lets pytest-cov combine that data with the main process's at session end. Gated on
    ``--cov`` actually being passed (pytest-cov's own ``cov_source`` option) -- otherwise a
    plain ``pytest`` invocation still spawns coverage.py inside every subprocess and leaves
    a stray ``.coverage.<host>.pid<N>...`` file per probe, despite nothing on the command
    line asking for coverage at all. ``pytest_configure`` runs once per process (main and
    each xdist worker alike) and well before collection reaches any subprocess-spawning
    test, so the timing guarantee the env var needs still holds.
    """
    if config.getoption('cov_source', default=None):
        os.environ.setdefault(
            'COVERAGE_PROCESS_START', str(Path(__file__).resolve().parent.parent / 'pyproject.toml')
        )


class Tenants(NamedTuple):
    """Two labels and one release, track and stadium tour apiece."""

    a: Label
    b: Label
    release_a: Release
    release_b: Release
    track_a: Track
    track_b: Track
    tour_a: StadiumTour
    tour_b: StadiumTour


@pytest.fixture
def tenants(db) -> Tenants:
    """Seed two tenants' worth of rows.

    The labels are created under ``tenancy_bypassed()`` because ``Label`` is the tenant
    model itself and is not tenanted -- but the rows *below* it are created inside a real
    ``tenant(...)`` scope, so the fixture also exercises autofill on the way in. Seeding
    through the bypass instead would leave every test resting on data that never passed the
    write guard.
    """
    with tenancy_bypassed():
        a = Label.objects.create(name='Aardvark Records')
        b = Label.objects.create(name='Basilisk Sound')

    created = {}
    for key, label in (('a', a), ('b', b)):
        with tenant(label=label):
            release = Release.objects.create(title=f'release-{key}')
            created[f'release_{key}'] = release
            created[f'track_{key}'] = Track.objects.create(title=f'track-{key}', release=release)
            created[f'tour_{key}'] = StadiumTour.objects.create(
                name=f'tour-{key}', continents=1, capacity=1000
            )

    return Tenants(a=a, b=b, **created)


@pytest.fixture
def bookings(tenants) -> tuple[Booking, Booking]:
    """One ``Booking`` per tenant -- the hand-declared ``tenanted_manager()`` model.

    Created through the bypass on purpose: ``Booking`` leaves ``GUITARS_TENANT_AUTOFILL``
    at its default of ``False``, so an unscoped create is the honest way to seed it and the
    tests that care about the guard say so themselves.
    """
    with tenancy_bypassed():
        return (
            Booking.objects.create(venue='Aardvark Arena', label=tenants.a),
            Booking.objects.create(venue='Basilisk Bowl', label=tenants.b),
        )


# ─────────────────────────────── raw-cursor helpers ─────────────────────────────── #
#
# Several modules assert against the database directly rather than through the ORM --
# the whole point of a raw DDL probe or a `pg_policies` check is that no Django manager
# sits between the assertion and the row. That need was previously met by copy-pasted
# `with connection.cursor() as cursor: ...` blocks in seven files (independently
# redefined in two of them). One definition here, imported everywhere.


def execute(*statements: str, params: list | None = None) -> None:
    """Run one or more raw SQL statements against the default connection, ignoring results.

    ``params`` binds placeholders and is only valid together with a single statement --
    it exists for the rare write (e.g. a parametrized ``INSERT`` with no ``RETURNING``)
    that would otherwise need its own ad hoc cursor block.
    """
    with connection.cursor() as cursor:
        if params is not None:
            if len(statements) != 1:
                raise ValueError('params requires exactly one statement')
            cursor.execute(statements[0], params)
            return
        for statement in statements:
            cursor.execute(statement)


def scalar(query: str, params: list | None = None):
    """Run *query* and return the first column of its first row, or ``None`` if empty."""
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        row = cursor.fetchone()
    return row[0] if row else None


def rows(query: str, params: list | None = None) -> list:
    """Run *query* and return every row as a list of tuples."""
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchall()


@pytest.fixture
def _execute(db):
    """Fixture form of :func:`execute`, for call sites written as ``_execute(stmt)``.

    Depends on ``db`` explicitly so a module that only ever injects this fixture (and
    never ``db`` itself) still gets a migrated test database before its first statement.
    """
    return execute
