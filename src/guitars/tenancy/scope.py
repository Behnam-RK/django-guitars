"""The active tenant frame: a ``ContextVar`` holding ``{dimension: value}``, so a scope
survives ``await``/``sync_to_async``. Nothing here touches the database -- the manager
turns it into a ``WHERE`` clause and the GUC wrapper mirrors it into session settings."""

from __future__ import annotations

import functools
import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from django.db.models import QuerySet

from guitars import GuitarsError
from guitars.gucs import VALUE_SEPARATOR


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


__all__ = [
    'TenantScopeError',
    'TenantScopeMissing',
    'TenantScopeViolation',
    'TenantValueError',
    'get_tenant',
    'is_bypassed',
    'tenancy_bypassed',
    'tenant',
    'tenanted',
]


class TenantScopeError(GuitarsError):
    """Base for every tenant-scope failure -- never raised directly. Catch this for both
    subclasses alike, or :class:`TenantScopeMissing`/:class:`TenantScopeViolation` for one."""


class TenantScopeMissing(TenantScopeError):  # noqa: N818 - name fixed by issue #12, not a typo
    """Raised when a tenant-scoped operation runs with no scope satisfying it -- caller
    forgot to open ``tenant(...)``, or is missing the one dimension needed. Ordinarily a
    403 in a request-handling app, not a 500: nothing is broken."""


class TenantScopeViolation(TenantScopeError):  # noqa: N818 - name fixed by issue #12, not a typo
    """Raised when a write disagrees with the tenant scope that *is* active -- an explicit
    mismatch, an ambiguous multi-value scope, or Postgres's RLS rejecting the statement.
    An alerting signal, not routine 403 material: the application computed the wrong tenant."""


class TenantValueError(GuitarsError):
    """Raised when a tenant dimension's value cannot be safely published -- not a scope
    failure but a data-modeling bug: the pk contains :data:`~guitars.gucs.VALUE_SEPARATOR`.
    See :func:`reject_separator`."""


BYPASS = '_bypass'
"""Reserved key marking the frame as bypassed. Not a dimension."""

MULTI_VALUE_TYPES = (list, tuple, set, frozenset)
"""What counts as "several values" -- ``tenant(shop=[a, b])`` means "either". Named once
because the manager, write guard, and GUC encoder all have to agree; missing one here is a
silent behaviour difference, not an error."""

# No frame entered yet -> empty mapping, which fails closed for scoped models.
_state: ContextVar[dict | None] = ContextVar('guitars_tenant_scope', default=None)


def get_tenant() -> dict:
    """A copy of the active tenant mapping (``{}`` if none), so a caller mutating the
    result can't corrupt the live frame."""
    return dict(_state.get() or {})


def is_bypassed() -> bool:
    """Whether enforcement is currently bypassed (see :func:`tenancy_bypassed`)."""
    return bool((_state.get() or {}).get(BYPASS, False))


def _reject_lazy(dimension: str, value: object) -> None:
    """Refuse a QuerySet as a dimension value: ``str()`` on it (which publish does) runs a
    query, re-entering publish -- surfacing as a ``RecursionError`` far from the mistake."""
    values = value if isinstance(value, MULTI_VALUE_TYPES) else [value]
    if any(isinstance(item, QuerySet) for item in values):
        raise TypeError(
            f'tenant({dimension}=...) got a QuerySet, which is lazy and would be '
            f'evaluated while the scope is published to the database. Pass model '
            f'instances or pks -- list(...) or .values_list("pk", flat=True).'
        )


def reject_separator(value: object, *, dimension: str | None = None) -> None:
    """Refuse (never escape) a value containing :data:`~guitars.gucs.VALUE_SEPARATOR` -- the
    policy splits the published GUC on it, so ``'a,b'`` would read as two tenants and match
    both. Called from two places, redundantly -- see CLAUDE.md's load-bearing checklist."""
    values = value if isinstance(value, MULTI_VALUE_TYPES) else [value]
    for item in values:
        pk = getattr(item, 'pk', item)
        if pk is not None and VALUE_SEPARATOR in str(pk):
            # Eager call knows the dimension; publish-time doesn't, so "tenant value" there
            # beats inventing a name.
            subject = f'tenant({dimension}=...) value' if dimension else 'tenant value'
            raise TenantValueError(
                f'{subject} {str(pk)!r} contains {VALUE_SEPARATOR!r}, which separates the '
                f'values of one dimension when the scope is published to PostgreSQL -- a '
                f'row-level-security policy would read it as several tenants and match all '
                f'of them. Use a primary key without {VALUE_SEPARATOR!r}.'
            )


@contextmanager
def tenant(**dimensions: object) -> Iterator[None]:
    """Activate tenant ``dimensions`` for the block. Nested tenants merge with, then
    restore, the enclosing one -- entering always *enforces*, even nested inside
    :func:`tenancy_bypassed`. ``None`` means *absent*, not "match everything"."""
    for dimension, value in dimensions.items():
        # Order is load-bearing: reject_separator's str() fallback would run a QuerySet's
        # query, so _reject_lazy must say no first.
        _reject_lazy(dimension, value)
        reject_separator(value, dimension=dimension)
    merged = dict(_state.get() or {})
    merged[BYPASS] = False
    merged.update(dimensions)
    token = _state.set(merged)
    try:
        yield
    finally:
        _state.reset(token)


@contextmanager
def tenancy_bypassed() -> Iterator[None]:
    """Bypass tenant enforcement for the block -- the deliberate, greppable cross-tenant
    path, and the *only* one. Mirrored into Postgres as ``tenant.bypass = 'on'``, so this
    bypasses both enforcement layers, not just the Python one."""
    with tenant(**{BYPASS: True}):
        yield


_UNBOUND = object()  # sentinel: parameter not bound at all (vs bound to None)


def tenanted(func: Callable | None = None, *, arg: str = 'tenant', dimension: str | None = None):
    """Decorator: run the wrapped callable in a tenant scope from its arguments -- usage in
    ``docs/tenancy.md``. ``arg``/``dimension`` stay separable so collapsing them can't
    silently open the wrong one. Fail-closed on ``None``; generators are rejected outright."""
    scope_dimension = dimension if dimension is not None else arg

    def decorate(fn: Callable) -> Callable:
        if inspect.isgeneratorfunction(fn) or inspect.isasyncgenfunction(fn):
            raise TypeError(
                'tenanted does not support generator functions -- the body runs at '
                'iteration time, outside the scope the decorator would open.'
            )
        signature = inspect.signature(fn)
        fn_name = getattr(fn, '__qualname__', repr(fn))
        if arg not in signature.parameters:
            raise TypeError(f'{fn_name} has no {arg!r} parameter for tenanted to read.')

        def resolve(args: tuple, kwargs: dict) -> object:
            bound = signature.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            value = bound.arguments.get(arg, _UNBOUND)
            if value is None:
                raise TenantScopeMissing(
                    f'{fn_name} needs a non-None {arg!r} to open its tenant scope.'
                )
            return value

        # A callable instance with an async __call__ isn't itself a coroutine function, so
        # it'd take the sync branch -- read __call__ off the type, not the instance.
        if inspect.iscoroutinefunction(fn) or inspect.iscoroutinefunction(type(fn).__call__):

            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                value = resolve(args, kwargs)
                if value is _UNBOUND:  # let Python raise the natural missing-argument TypeError
                    return await fn(*args, **kwargs)
                with tenant(**{scope_dimension: value}):
                    return await fn(*args, **kwargs)

        else:

            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                value = resolve(args, kwargs)
                if value is _UNBOUND:  # let Python raise the natural missing-argument TypeError
                    return fn(*args, **kwargs)
                with tenant(**{scope_dimension: value}):
                    return fn(*args, **kwargs)

        return wrapper

    return decorate if func is None else decorate(func)
