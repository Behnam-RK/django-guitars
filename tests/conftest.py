"""Shared fixtures for the tenancy tests. Two tenants, always -- a single-tenant fixture
can't tell "the scope works" from "there was only ever one tenant's data"."""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

import pytest
from django.db import connection

from guitars.tenancy import tenancy_bypassed, tenant
from tests.testapp.models import Booking, Label, Release, StadiumTour, Track


def pytest_configure(config: pytest.Config) -> None:
    """Sets COVERAGE_PROCESS_START before test_ladder.py's subprocess probes spawn, so
    coverage.py's own site-packages shim tracks inside them too -- gated on ``--cov``
    actually being passed, or a plain run leaves stray ``.coverage.<host>.pid<N>`` files."""
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
    """Seed two tenants' worth of rows. Labels are created under ``tenancy_bypassed()`` since
    ``Label`` is the tenant model itself, but rows below it go through a real ``tenant(...)``
    scope, so the fixture also exercises autofill on the way in."""
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
    """One ``Booking`` per tenant, the hand-declared ``tenanted_manager()`` model. Created
    through the bypass since ``Booking`` leaves ``GUITARS_TENANT_AUTOFILL`` at its default
    ``False``, so an unscoped create is the honest way to seed it."""
    with tenancy_bypassed():
        return (
            Booking.objects.create(venue='Aardvark Arena', label=tenants.a),
            Booking.objects.create(venue='Basilisk Bowl', label=tenants.b),
        )


# ─────────────────────────────── raw-cursor helpers ─────────────────────────────── #
# Several modules assert against the database directly (no Django manager between the
# assertion and the row). One definition here instead of copy-pasted cursor blocks.


def execute(*statements: str, params: list | None = None) -> None:
    """Run one or more raw SQL statements, ignoring results. ``params`` binds placeholders
    and is only valid with a single statement -- for the rare parametrized write that would
    otherwise need its own ad hoc cursor block."""
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
    """Fixture form of :func:`execute`. Depends on ``db`` explicitly so a module that only
    injects this fixture still gets a migrated test database first."""
    return execute
