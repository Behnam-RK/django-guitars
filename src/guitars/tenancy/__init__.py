"""Tenant-scoping safeguard for multi-tenant Django models.

Fail-closed defence against cross-tenant leaks, in two cooperating layers:

* **Python** (``scope``, ``spec``, ``enforcement``, ``querysets`` and ``manager``) -- a
  model built with ``tenanted_manager()`` refuses reads unless the required dimension is
  active (``tenant(...)`` / ``@tenanted``), raising ``TenantScopeError`` on a forgotten
  filter, and guards writes so a row cannot be created into another tenant. Loud and
  greppable: this is the layer that tells a developer they got it wrong.
* **PostgreSQL** (``guc`` + row-level-security policies from ``guitars.sql.policy``) --
  the same frame is published as ``tenant.*`` session settings, and policies enforce it
  on every statement. This is the layer that is actually *complete*: it covers joins,
  cascades, ``_base_manager``, ``instance.save()`` and raw SQL, none of which ever
  consult a Django manager.

Neither is redundant. Without the database the coverage has holes; without Python a
missing scope is silent. ``tenancy_bypassed()`` is the one explicit, greppable
cross-tenant path, and bypasses both layers at once.

Every failure raises a :class:`TenantScopeError` subclass: :class:`TenantScopeMissing`
when no scope satisfies the operation (ordinarily a 403 in application code, not a 500),
:class:`TenantScopeViolation` when an active scope's write disagrees with it or
PostgreSQL's own policy rejects the statement (ordinarily an alerting signal -- something
computed the wrong tenant), and :class:`TenantValueError` when a value cannot be safely
published at all. Catch ``TenantScopeError`` to handle any scope failure alike, or a
specific subclass to handle just one.

State lives in a ``ContextVar`` so a scope survives ``await`` / ``sync_to_async``.

Portability: this package imports only the standard library and Django. Anything
host-specific arrives through ``reporting.set_reporter``, so it can move as a unit.

**This module's ``__all__`` is the application-facing surface only.** Three things
consumers sometimes reach for live elsewhere on purpose:

* ``guitars.gucs`` (``BYPASS_GUC``, ``GUC_PREFIX``, ``VALUE_SEPARATOR``, ``guc_name``) --
  a leaf module a generated migration's ``from guitars import sql`` can read without
  pulling in this whole runtime. Re-exporting the names here would defeat that.
* ``guitars.tenancy.spec`` (``tenant_spec``, ``local_tenant_fields``) -- what a model is
  tenanted on, read by the RLS policy generator and by ``discovery``/``checks``. A
  generator-facing internal question, not an application-facing one.
* ``guitars.tenancy.testing`` -- ``uninstall`` and the lower-level install/uninstall hooks
  for one layer at a time, for a test suite that needs to install and tear down
  enforcement per test. Not part of the application-facing API surface -- a real project
  calls :func:`install` (below) once, or lets ``GuitarsConfig.ready()`` do it.

See ``docs/tenancy.md``.
"""

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
    """Activate enforcement. Idempotent, and called for you two ways.

    ``GuitarsConfig.ready()`` calls this when ``guitars`` is in ``INSTALLED_APPS``, and
    ``tenanted_manager()`` calls it at model-definition time. Belt and braces on purpose:
    guitars is usable as a pure library with no ``INSTALLED_APPS`` entry, and in that
    configuration the AppConfig hook never runs. Relying on it alone would leave the
    models scoping their own reads while the write guards and the database layer silently
    did nothing -- enforcement that looks present and is not.
    """
    install_tenant_guc()
    install_write_guards()
    register_checks()


def uninstall() -> None:
    """Deactivate enforcement. For tests -- see ``guitars.tenancy.testing``."""
    uninstall_tenant_guc()
    uninstall_write_guards()
