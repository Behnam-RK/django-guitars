"""Startup validation for the tenancy settings, moving a typo'd ``GUITARS_TENANT_ENFORCE``
from a ``ValueError`` deep inside the first write that trips a guard to ``manage.py
check``, which ``runserver``/``migrate`` run for you."""

from __future__ import annotations

from django.apps import apps as django_apps
from django.conf import settings
from django.core.checks import Error, Warning, register

from .enforcement import TenantEnforcement
from .spec import tenant_spec


__all__ = [
    'check_guitar_models_have_a_tenant',
    'check_migrate_bypasses_tenancy',
    'check_pooling_leaks_tenant_gucs',
    'check_tenancy_settings',
    'register_checks',
]

#: Django wants a stable id per check; namespaced so it cannot collide.
ENFORCE_ID = 'guitars.tenancy.E001'
AUTOFILL_ID = 'guitars.tenancy.E002'
TENANT_MODEL_ID = 'guitars.tenancy.E003'
MIGRATE_OVERRIDE_ID = 'guitars.tenancy.W001'
POOLING_ID = 'guitars.tenancy.W002'


def _candidate_models(app_configs) -> list:
    """The models a check should consider -- reporting outside the requested apps would
    make a scoped ``manage.py check <app>`` run answer a question it wasn't asked."""
    if app_configs is None:
        return list(django_apps.get_models())
    return [model for config in app_configs for model in config.get_models()]


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
    """Reject a concrete ``GuitarModel`` when ``GUITARS_TENANT_MODEL`` isn't set --
    otherwise it quietly becomes ``SetarModel``, reading as tenanted while enforcing
    nothing. Import deferred: reached while ``guitars.models.base`` still builds it."""
    from guitars.models.base import GuitarModel  # noqa: PLC0415 - deferred: import cycle

    if GuitarModel._guitars_tenancy_installed:
        return []

    subclasses = [
        model for model in _candidate_models(app_configs) if issubclass(model, GuitarModel)
    ]
    if not subclasses:
        # Nobody used the rung: staying silent lets a project on lower rungs check clean.
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


def check_migrate_bypasses_tenancy(app_configs, **kwargs) -> list[Warning]:
    """Warn when guitars' ``migrate`` override (wraps Django's in ``tenancy_bypassed()``)
    is not the one that runs -- otherwise a ``RunPython`` backfill runs unscoped, matches
    nothing, and is marked applied having silently done nothing."""
    # Deferred: this module is imported while guitars.models.base is still executing.
    from django.core.management import get_commands, load_command_class  # noqa: PLC0415

    from guitars.management.commands.migrate import Command as GuitarsMigrate  # noqa: PLC0415

    if not getattr(settings, 'GUITARS_TENANT_POLICIES', True):
        return []
    if not any(tenant_spec(model) for model in _candidate_models(app_configs)):
        return []

    winner = load_command_class(get_commands()['migrate'], 'migrate')
    if isinstance(winner, GuitarsMigrate):
        return []

    return [
        Warning(
            f"`migrate` resolves to {type(winner).__module__}, not guitars' override, so "
            f'migrations will run without tenancy bypassed. A RunPython backfill is then '
            f'filtered by every tenant_scope policy, updates zero rows, and is still marked '
            f'applied.',
            hint=(
                "List 'guitars' earlier in INSTALLED_APPS than the app providing that "
                'command (the earliest app wins), or have that command subclass '
                'guitars.management.commands.migrate.Command.'
            ),
            id=MIGRATE_OVERRIDE_ID,
        )
    ]


def check_pooling_leaks_tenant_gucs(app_configs, **kwargs) -> list[Warning]:
    """Warn when ``DISABLE_SERVER_SIDE_CURSORS`` suggests an external transaction-pooling
    pooler -- see ``docs/tenancy.md``'s "Connection pooling" for the fails-open mechanism
    this check can't detect directly. A nudge, gated like :func:`check_migrate_bypasses_tenancy`."""
    if not getattr(settings, 'GUITARS_TENANT_POLICIES', True):
        return []
    if not any(tenant_spec(model) for model in _candidate_models(app_configs)):
        return []

    flagged = sorted(
        alias
        for alias, config in getattr(settings, 'DATABASES', {}).items()
        if config.get('DISABLE_SERVER_SIDE_CURSORS')
    )
    if not flagged:
        return []

    return [
        Warning(
            f'{", ".join(flagged)} {"has" if len(flagged) == 1 else "have"} '
            f"DISABLE_SERVER_SIDE_CURSORS set -- Django's own recommendation for a "
            f'transaction-pooling connection pooler (e.g. pgbouncer with '
            f"POOL_MODE=transaction). Under transaction pooling, a previous client's "
            f'published tenant.* session setting can still be resident on the physical '
            f'backend handed to the next one -- this fails open, not closed.',
            hint=(
                "Configure the pooler's reset query to clear session state between "
                'clients (pgbouncer: server_reset_query = DISCARD ALL). See the '
                '"Connection pooling" section of docs/tenancy.md.'
            ),
            id=POOLING_ID,
        )
    ]


def register_checks() -> None:
    """Register the checks -- idempotent, Django's registry is a set keyed by function."""
    register(check_tenancy_settings)
    register(check_guitar_models_have_a_tenant)
    register(check_migrate_bypasses_tenancy)
    register(check_pooling_leaks_tenant_gucs)
