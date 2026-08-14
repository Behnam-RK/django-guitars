"""Tenant-scoping safeguard for multi-tenant Django models -- fail-closed, two cooperating
layers (Python loud/greppable, PostgreSQL RLS complete). See ``docs/tenancy.md``.
``__all__`` is application-facing only; ``gucs``/``spec``/``testing`` are separate on purpose."""

from __future__ import annotations

from .checks import register_checks
from .enforcement import (
    TenantEnforcement,
    ViolationKind,
    install_write_guards,
    uninstall_write_guards,
)
from .guc import install as install_tenant_guc
from .guc import uninstall as uninstall_tenant_guc
from .manager import TenantedManagerBase, tenanted_manager
from .reporting import Reporter, set_reporter
from .scope import (
    TenantScopeError,
    TenantScopeMissing,
    TenantScopeViolation,
    TenantValueError,
    get_tenant,
    is_bypassed,
    tenancy_bypassed,
    tenant,
    tenanted,
)


__all__ = [
    'Reporter',
    'TenantEnforcement',
    'TenantScopeError',
    'TenantScopeMissing',
    'TenantScopeViolation',
    'TenantValueError',
    'TenantedManagerBase',
    'ViolationKind',
    'get_tenant',
    'install',
    'is_bypassed',
    'set_reporter',
    'tenancy_bypassed',
    'tenant',
    'tenanted',
    'tenanted_manager',
]


def install() -> None:
    """Activate enforcement. Idempotent, called two ways -- ``GuitarsConfig.ready()`` and
    ``tenanted_manager()`` at model-definition time -- since guitars is usable as a pure
    library with no ``INSTALLED_APPS`` entry, where the AppConfig hook never runs."""
    install_tenant_guc()
    install_write_guards()
    register_checks()


def uninstall() -> None:
    """Deactivate enforcement. For tests -- see ``guitars.tenancy.testing``."""
    uninstall_tenant_guc()
    uninstall_write_guards()
