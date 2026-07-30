"""Startup validation for the tenancy settings.

``GUITARS_TENANT_ENFORCE`` is read at write time, so a typo in it would otherwise surface
as a ``ValueError`` raised from inside the first write that happens to trip a guard -- on
a request, long after deploy, and nowhere near the setting. A system check moves that to
``manage.py check``, which ``runserver`` and ``migrate`` run for you.

Django only, like the rest of this package.
"""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, register

from .manager import TenantEnforcement


__all__ = ['check_tenancy_settings', 'register_checks']

#: Django wants a stable id per check; namespaced so it cannot collide.
ENFORCE_ID = 'guitars.tenancy.E001'
AUTOFILL_ID = 'guitars.tenancy.E002'


def check_tenancy_settings(app_configs, **kwargs) -> list[Error]:
    """Reject a GUITARS_TENANT_ENFORCE / GUITARS_TENANT_AUTOFILL value the guards can't honour."""
    errors = []

    configured = getattr(settings, 'GUITARS_TENANT_ENFORCE', TenantEnforcement.STRICT)
    allowed = [mode.value for mode in TenantEnforcement]
    if str(configured) not in allowed:
        errors.append(
            Error(
                f'GUITARS_TENANT_ENFORCE is {configured!r}, which is not a tenant '
                f'enforcement mode.',
                hint=f'Use one of {allowed}.',
                id=ENFORCE_ID,
            )
        )

    # Deliberately strict about the type: GUITARS_TENANT_AUTOFILL is read through bool(),
    # so a string like 'False' would silently enable autofill.
    autofill = getattr(settings, 'GUITARS_TENANT_AUTOFILL', False)
    if not isinstance(autofill, bool):
        errors.append(
            Error(
                f'GUITARS_TENANT_AUTOFILL must be a bool, got '
                f'{type(autofill).__name__} ({autofill!r}).',
                hint="A non-empty string such as 'False' would read as True.",
                id=AUTOFILL_ID,
            )
        )

    return errors


def register_checks() -> None:
    """Register the settings checks. Idempotent -- Django dedupes by function."""
    register(check_tenancy_settings)
