"""The write guards: fill in a missing tenant, or refuse a write that contradicts one.

A ``pre_save`` receiver plus a ``bulk_create`` override fill in the tenant when it is
absent and reject it when it contradicts the active frame. Because the receiver is on the
*signal*, it covers every ``save()`` -- including ``instance.save()`` and writes routed
through ``_base_manager``, neither of which consults a manager. The ``bulk_create`` guard
itself lives on the queryset (``guitars.tenancy.querysets``), which is what actually calls
:func:`apply_write_guard` per object -- chaining leaves the manager behind
(``Model.objects.filter(...)`` hands back a queryset), so a guard the manager owned alone
would not be on it.

What this deliberately does not cover: ``queryset.update()``, ``bulk_create`` on a
non-tenanted manager (no signal, no override), cascade deletes, and raw SQL. Those are
the database's job -- its ``WITH CHECK`` sees every statement, which is the whole reason
enforcement lives there too. What Python adds is a diagnosable error and an autofill the
database cannot perform.
"""

from __future__ import annotations

from enum import Enum

from django.conf import settings
from django.db import models
from django.db.models.signals import pre_save

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
    """Report and proceed.

    For rolling enforcement out over a populated deployment: it names the offending
    paths without 500-ing them.
    """

    # StrEnum parity on Python 3.10, which guitars still supports (ganje required 3.12).
    # Without this, ``str(TenantEnforcement.STRICT)`` yields 'TenantEnforcement.STRICT'
    # rather than 'strict' -- and checks.py compares ``str(configured)`` against the
    # allowed values, so a project passing the enum itself would fail its own check.
    __str__ = str.__str__


class ViolationKind(str, Enum):
    """What kind of write-guard violation this is.

    Previously computed by :func:`apply_write_guard` for internal dedup only, and thrown
    away before reaching the ``Reporter`` -- a custom reporter (one forwarding to Sentry,
    say) had nothing to classify on but regexing the message string. Now passed straight
    through as structured ``kind=`` context; see :func:`_violation`.
    """

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
    """Raise ``exception``, or merely report ``kind``/``context``, depending on the mode.

    ``exception`` is the caller's choice between :class:`TenantScopeMissing` (no scope
    satisfies this) and :class:`TenantScopeViolation` (a scope is active but this write
    contradicts it) -- audit mode reports the same finding regardless of which, since
    nothing here raises in that mode at all. ``kind`` and any extra ``context`` (typically
    ``model=`` and ``dimension=``) reach the ``Reporter`` as structured keyword arguments
    rather than being folded into the message string alone, so a reporter that forwards
    to Sentry or similar can classify programmatically instead of regexing it.
    """
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
                    f'take one from -- wrap it in tenant(...), or tenancy_bypassed() '
                    f'for a deliberate cross-tenant write.',
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
                # A collection scope reads as "either of these", which a column holding one
                # value cannot express. Refused whatever the length: unwrapping a
                # one-element collection would make the write depend on how many tenants
                # the caller's list happened to contain.
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
                setattr(instance, attname, _pk(expected))
        elif not _matches(expected, actual):
            _violation(
                f'{label} write sets {dimension}={actual!r} while the active scope is '
                f'{_pk(expected)!r} -- a write may not cross tenants. Use '
                f'tenancy_bypassed() if that is genuinely intended.',
                key=(label, dimension, 'mismatch'),
                exception=TenantScopeViolation,
                kind=ViolationKind.MISMATCH,
                model=label,
                dimension=dimension,
            )


def _guarded(objs) -> list:
    """Materialise ``objs`` and guard each one.

    Shared by every ``bulk_create`` override (``guitars.tenancy.querysets``): the batched
    path sends no ``pre_save``, so the guard has to be invoked by hand or that path would
    skip autofill and validation both.
    """
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
