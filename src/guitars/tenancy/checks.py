"""Startup validation for the tenancy settings.

``GUITARS_TENANT_ENFORCE`` is read at write time, so a typo in it would otherwise surface
as a ``ValueError`` raised from inside the first write that happens to trip a guard -- on
a request, long after deploy, and nowhere near the setting. A system check moves that to
``manage.py check``, which ``runserver`` and ``migrate`` run for you.

Django only, like the rest of this package.
"""

from __future__ import annotations

from django.apps import apps as django_apps
from django.conf import settings
from django.core.checks import Error, register

from .manager import TenantEnforcement


__all__ = ['check_guitar_models_have_a_tenant', 'check_tenancy_settings', 'register_checks']

#: Django wants a stable id per check; namespaced so it cannot collide.
ENFORCE_ID = 'guitars.tenancy.E001'
AUTOFILL_ID = 'guitars.tenancy.E002'
TENANT_MODEL_ID = 'guitars.tenancy.E003'


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


def check_guitar_models_have_a_tenant(app_configs, **kwargs) -> list[Error]:
    """Reject a concrete ``GuitarModel`` when ``GUITARS_TENANT_MODEL`` is not set.

    ``GuitarModel`` is the tenanted rung. Without the setting there is no model for its
    foreign key to point at, so it contributes neither the field nor the scoped managers
    and quietly becomes ``SetarModel`` -- a model that reads as tenanted at every call
    site and enforces nothing. That is the one outcome the whole feature exists to
    prevent, so it is an error rather than a warning, and it names the rung to use if
    tenancy was not what was wanted.

    The import is deferred because ``guitars.models.base`` imports this package to build
    ``GuitarModel`` in the first place; by check time the cycle has long resolved.
    ``_guitars_tenancy_installed`` is read off the class rather than re-derived from the
    setting, so this check and the wiring can never disagree about whether it happened.
    """
    from guitars.models.base import GuitarModel  # noqa: PLC0415 - deferred: import cycle

    if GuitarModel._guitars_tenancy_installed:
        return []

    subclasses = [
        model
        for model in django_apps.get_models()
        if issubclass(model, GuitarModel) and not model._meta.abstract
    ]
    if not subclasses:
        # Nobody used the rung, so nothing is unprotected. Staying silent here is what
        # lets a project on the lower rungs run `manage.py check` clean.
        return []

    names = ', '.join(sorted(model._meta.label for model in subclasses))
    return [
        Error(
            f'GUITARS_TENANT_MODEL is not set, so GuitarModel contributed no tenant '
            f'field and no tenant-scoped managers -- {names} are not tenanted despite '
            f'subclassing the tenanted rung.',
            hint=(
                "Set GUITARS_TENANT_MODEL = '<app_label>.<ModelName>', or subclass "
                'SetarModel instead, which is GuitarModel without tenancy.'
            ),
            id=TENANT_MODEL_ID,
        )
    ]


def register_checks() -> None:
    """Register the checks. Idempotent -- Django's registry is a set, keyed by function."""
    register(check_tenancy_settings)
    register(check_guitar_models_have_a_tenant)
