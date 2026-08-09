"""Test-only lifecycle hooks for the tenancy runtime.

**Not part of the application-facing API surface.** A real project calls
``guitars.tenancy.install()`` once (or lets ``GuitarsConfig.ready()`` do it) and never
calls ``uninstall()`` at all -- enforcement, once on, stays on for the life of the
process. What lives here exists for a test suite that needs to install, uninstall, or
re-install *one specific layer* in isolation (the GUC wrapper, the write guards, the
system checks) rather than the whole stack at once, which is exactly what guitars' own
test suite -- and a consuming project's tenancy test fixtures -- needs and an ordinary
application never does.

Deliberately its own module rather than folded back into ``guitars.tenancy.__all__``:
that was the twenty-three-name surface this split exists to shrink, and re-exporting
these at the same level as ``tenant()``/``tenanted_manager()`` would just move the
"too many audiences in one export list" problem down by one, not solve it.
"""

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
