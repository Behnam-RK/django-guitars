"""Test-only lifecycle hooks for the tenancy runtime -- **not** application-facing. A real
project calls ``install()`` once and never ``uninstall()``; this exists for a test suite
installing/uninstalling *one layer* in isolation. Kept out of ``guitars.tenancy.__all__``."""

from __future__ import annotations

from . import uninstall
from .checks import register_checks
from .enforcement import install_write_guards, uninstall_write_guards
from .guc import install as install_tenant_guc
from .guc import uninstall as uninstall_tenant_guc


__all__ = [
    'install_tenant_guc',
    'install_write_guards',
    'register_checks',
    'uninstall',
    'uninstall_tenant_guc',
    'uninstall_write_guards',
]
