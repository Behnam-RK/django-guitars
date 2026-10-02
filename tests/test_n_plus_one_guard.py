"""Canaries for the suite-wide N+1 guard (django-zeal, tests/conftest.py). A detector that
detects nothing reads as "no N+1" on every green run, so pin that it still fires with the
settings allow-list in force, and that both ways around a lazy load stay quiet."""

from __future__ import annotations

import pytest
from zeal import NPlusOneError

from tests.testapp.models import Album, Band


@pytest.fixture
def albums(db) -> None:
    band = Band.objects.create(name='Rush')
    for number in range(3):
        Album.objects.create(title=f'album-{number}', band=band)


def test_a_forward_foreign_key_loop_is_flagged(albums):
    with pytest.raises(NPlusOneError):
        for album in Album.objects.all():
            album.band  # noqa: B018 - the lazy load is the point


def test_select_related_is_clean(albums):
    for album in Album.objects.select_related('band'):
        album.band  # noqa: B018


def test_reading_the_attname_is_clean(albums):
    for album in Album.objects.all():
        album.band_id  # noqa: B018
