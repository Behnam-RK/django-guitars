"""The write guards, **diagnostics since 2.1.0**: correctness is the ADR 0005 autofill
trigger plus the policy's ``WITH CHECK``, which no signal can switch off. What survives here
is the message naming model/dimension/fix, and audit mode -- see ``docs/tenancy.md``."""

from __future__ import annotations

from enum import Enum

from django.conf import settings
from django.db import models
from django.db.models.signals import pre_save

from .messages import remediation
from .reporting import report_once
from .scope import (
    MULTI_VALUE_TYPES,
    TenantScopeError,
    TenantScopeMissing,
    TenantScopeViolation,
    get_tenant,
    is_bypassed,
)
from .spec import _autofills, _meta, local_tenant_fields


__all__ = [
    'TenantEnforcement',
    'ViolationKind',
    'apply_write_guard',
    'install_write_guards',
    'uninstall_write_guards',
]

_WRITE_GUARD_UID = 'guitars_tenant_write_guard'


class TenantEnforcement(str, Enum):
    """What a write-guard violation does."""

    STRICT = 'strict'
    """Raise. The resting state."""

    AUDIT = 'audit'
    """Report and proceed -- for rolling enforcement out over a populated deployment
    without 500-ing the offending paths."""

    # StrEnum parity on Python 3.10: without this, str(TenantEnforcement.STRICT) yields
    # 'TenantEnforcement.STRICT', and checks.py's str(configured) comparison would fail.
    __str__ = str.__str__


class ViolationKind(str, Enum):
    """What kind of write-guard violation this is -- passed to the ``Reporter`` as
    structured ``kind=`` context (see :func:`_violation`) rather than left for a custom
    reporter to regex out of the message string."""

    UNSCOPED = 'unscoped'
    """No active tenant scope at all -- nothing to take a value from."""

    MISSING = 'missing'
    """A scope is active, but this field is unset and autofill is off."""

    AMBIGUOUS = 'ambiguous'
    """A scope is active but names several tenants, so there is no one value to autofill."""

    MISMATCH = 'mismatch'
    """An explicit value contradicts the active scope."""

    # StrEnum parity on Python 3.10 -- see TenantEnforcement's own copy of this trick,
    # immediately above, for why.
    __str__ = str.__str__


def _enforcement() -> TenantEnforcement:
    return TenantEnforcement(getattr(settings, 'GUITARS_TENANT_ENFORCE', TenantEnforcement.STRICT))


def _violation(
    message: str,
    *,
    key: object,
    exception: type[TenantScopeError],
    kind: ViolationKind,
    **context: object,
) -> None:
    """Raise ``exception``, or merely report ``kind``/``context``, depending on the mode --
    audit mode reports the same finding regardless of which exception was chosen.
    ``context`` reaches the ``Reporter`` as structured kwargs, not just message text."""
    if _enforcement() is TenantEnforcement.AUDIT:
        report_once(key, message, mode=TenantEnforcement.AUDIT.value, kind=kind, **context)
        return
    raise exception(message)


def _pk(value: object) -> object:
    return getattr(value, 'pk', value)


def _matches(expected: object, actual: object) -> bool:
    """Whether ``actual`` satisfies ``expected``, which may be a collection."""
    if isinstance(expected, MULTI_VALUE_TYPES):
        return any(str(_pk(item)) == str(actual) for item in expected)
    return str(_pk(expected)) == str(actual)


def apply_write_guard(instance: models.Model) -> None:
    """Autofill or validate every local tenant dimension on ``instance``."""
    if is_bypassed():
        return
    fields = local_tenant_fields(type(instance))
    if not fields:
        return

    active = get_tenant()
    label = f'{type(instance).__module__}.{type(instance).__qualname__}'
    for dimension, field_name in fields.items():
        expected = active.get(dimension)
        attname = _meta(type(instance)).get_field(field_name).attname
        actual = getattr(instance, attname, None)

        if expected is None:
            if actual is None:
                _violation(
                    f'{label} write has no {dimension!r} and no active tenant scope to '
                    f'take one from -- {remediation("write")}',
                    key=(label, dimension, 'unscoped'),
                    exception=TenantScopeMissing,
                    kind=ViolationKind.UNSCOPED,
                    model=label,
                    dimension=dimension,
                )
            # An explicit value with no active scope is the pre-existing "create takes an
            # explicit tenant" path; the database still checks it.
            continue

        if actual is None:
            if not _autofills(type(instance)):
                _violation(
                    f'{label} write is missing {dimension!r}. Pass it explicitly, or '
                    f'enable GUITARS_TENANT_AUTOFILL to take it from the active scope.',
                    key=(label, dimension, 'missing'),
                    exception=TenantScopeViolation,
                    kind=ViolationKind.MISSING,
                    model=label,
                    dimension=dimension,
                )
            elif isinstance(expected, MULTI_VALUE_TYPES):
                # A collection scope reads as "either of these"; refused whatever the
                # length, or the write would depend on how many tenants the list contained.
                _violation(
                    f'{label} write has no {dimension!r} and the active scope names '
                    f'several ({[_pk(item) for item in expected]!r}), so there is no '
                    f'one value to autofill. Pass {dimension!r} explicitly.',
                    key=(label, dimension, 'ambiguous'),
                    exception=TenantScopeViolation,
                    kind=ViolationKind.AMBIGUOUS,
                    model=label,
                    dimension=dimension,
                )
            else:
                # NOT redundant with the database trigger, which cannot reach back into the
                # instance: this is what makes `obj.tenant_id` readable the moment `save()`
                # returns, instead of None until the row is refetched. Do not delete.
                setattr(instance, attname, _pk(expected))
        elif not _matches(expected, actual):
            _violation(
                f'{label} write sets {dimension}={actual!r} while the active scope is '
                f'{_pk(expected)!r} -- a write may not cross tenants. '
                f'{remediation("write", scope_is_active=True)}',
                key=(label, dimension, 'mismatch'),
                exception=TenantScopeViolation,
                kind=ViolationKind.MISMATCH,
                model=label,
                dimension=dimension,
            )


def _guarded(objs) -> list:
    """Materialise ``objs`` and guard each one -- shared by every ``bulk_create`` override,
    since the batched path sends no ``pre_save``. Belt-and-braces since 2.1.0: the autofill
    trigger covers this path too, so what this adds is the better message, not the fill."""
    objs = list(objs)
    for obj in objs:
        apply_write_guard(obj)
    return objs


def _on_pre_save(sender, instance, **kwargs) -> None:
    apply_write_guard(instance)


def install_write_guards() -> None:
    """Guard every ``save()`` (and therefore ``create()``). Idempotent."""
    pre_save.connect(_on_pre_save, dispatch_uid=_WRITE_GUARD_UID)


def uninstall_write_guards() -> None:
    pre_save.disconnect(dispatch_uid=_WRITE_GUARD_UID)
