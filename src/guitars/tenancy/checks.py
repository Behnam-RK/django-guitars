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
from django.core.checks import Error, Warning, register

from .manager import TenantEnforcement, tenant_spec


__all__ = [
    'check_guitar_models_have_a_tenant',
    'check_migrate_bypasses_tenancy',
    'check_tenancy_settings',
    'register_checks',
]

#: Django wants a stable id per check; namespaced so it cannot collide.
ENFORCE_ID = 'guitars.tenancy.E001'
AUTOFILL_ID = 'guitars.tenancy.E002'
TENANT_MODEL_ID = 'guitars.tenancy.E003'
MIGRATE_OVERRIDE_ID = 'guitars.tenancy.W001'


def _candidate_models(app_configs) -> list:
    """The models a check should consider.

    ``manage.py check <app>`` passes ``app_configs``; reporting models outside the
    requested apps would make a scoped run answer a question it was not asked.
    """
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
        model for model in _candidate_models(app_configs) if issubclass(model, GuitarModel)
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


def check_migrate_bypasses_tenancy(app_configs, **kwargs) -> list[Warning]:
    """Warn when guitars' ``migrate`` override is not the one that will run.

    ``guitars.management.commands.migrate`` wraps Django's in ``tenancy_bypassed()``,
    because a ``RunPython`` backfill runs with no tenant scope active and every
    ``tenant_scope`` policy therefore matches nothing: the ``UPDATE`` reports zero rows,
    no error is raised, and the migration is marked applied. A backfill that silently did
    nothing is the worst outcome available -- it surfaces much later as missing data, with
    a green migration history pointing away from the cause.

    Which override runs is decided by ``INSTALLED_APPS`` order: Django's ``get_commands()``
    walks ``reversed(apps.get_app_configs())`` and lets each app overwrite the previous
    entry, so the app listed **earliest** wins. That was an ordering convention documented
    in a module docstring and enforced by nothing, which is a poor guard for a failure this
    quiet -- hence this check.

    Resolved by ``isinstance`` against the class rather than by comparing app names, so a
    project that subclasses the override to add behaviour of its own is correctly silent.

    Gated twice, because a warning that fires where the hazard cannot is a warning people
    learn to skip: only when some model is actually tenanted, and only when
    ``GUITARS_TENANT_POLICIES`` leaves the database layer switched on. A ``Warning`` rather
    than an ``Error`` because a project with no data migrations is unaffected, and only its
    authors know that.
    """
    # Deferred: this module is imported while ``guitars.models.base`` is still executing,
    # and both of these reach back into the management layer -- which imports models.
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


def register_checks() -> None:
    """Register the checks. Idempotent -- Django's registry is a set, keyed by function."""
    register(check_tenancy_settings)
    register(check_guitar_models_have_a_tenant)
    register(check_migrate_bypasses_tenancy)
