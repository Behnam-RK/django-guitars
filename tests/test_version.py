"""Tests for guitars.__version__ resolution."""

import importlib
import importlib.metadata
from importlib.metadata import PackageNotFoundError

import guitars


def test_version_falls_back_when_package_metadata_is_missing():
    original_version = importlib.metadata.version

    def raise_not_found(name):
        raise PackageNotFoundError(name)

    importlib.metadata.version = raise_not_found
    try:
        importlib.reload(guitars)
        assert guitars.__version__ == '0.0.0+unknown'
    finally:
        # Restore the real lookup *before* reloading again, so guitars ends up
        # holding the genuine installed version for any test that runs after this one.
        importlib.metadata.version = original_version
        importlib.reload(guitars)


def test_version_resolves_from_installed_package_metadata():
    assert guitars.__version__ != '0.0.0+unknown'
