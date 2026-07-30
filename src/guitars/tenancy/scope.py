"""The active tenant frame: a ``ContextVar`` holding ``{dimension: value}``.

State lives in a ``ContextVar`` so a scope opened in an ``async`` frame survives
``await`` / ``sync_to_async``. Nothing here touches the database -- the manager turns
the frame into a ``WHERE`` clause (``manager.py``) and the GUC wrapper mirrors it into
PostgreSQL session settings (``guc.py``).
"""

from __future__ import annotations

import functools
import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from django.db.models import QuerySet


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


__all__ = [
    'TenantScopeError',
    'get_tenant',
    'is_bypassed',
    'tenancy_bypassed',
    'tenant',
    'tenanted',
]


class TenantScopeError(Exception):
    """Raised when a tenant-scoped query runs without its required scope active."""


BYPASS = '_bypass'
"""Reserved key marking the frame as bypassed. Not a dimension."""

MULTI_VALUE_TYPES = (list, tuple, set, frozenset)
"""What counts as "several values" for a dimension -- ``tenant(shop=[a, b])`` means "either".

Named once because three modules have to agree: the manager (``__in`` instead of ``=``),
the write guard (membership instead of equality), and the GUC encoder (a separated list
instead of a scalar). Missing one of these types in one place is a silent behaviour
difference, not an error.
"""

# No frame entered yet -> empty mapping, which fails closed for scoped models.
_state: ContextVar[dict | None] = ContextVar('guitars_tenant_scope', default=None)


def get_tenant() -> dict:
    """A copy of the active tenant mapping (``{}`` if none is active).

    A copy, so a caller mutating the result cannot corrupt the live frame.
    """
    return dict(_state.get() or {})


def is_bypassed() -> bool:
    """Whether enforcement is currently bypassed (see :func:`tenancy_bypassed`)."""
    return bool((_state.get() or {}).get(BYPASS, False))


def _reject_lazy(dimension: str, value: object) -> None:
    """Refuse a QuerySet as a dimension value, before anything tries to use it.

    A scope value is eventually ``str()``-ed to publish it as a session setting, and
    ``str()`` on a QuerySet runs a query -- inside the publish, which re-enters the
    publish, which queries again. The real symptom is a ``RecursionError`` from
    somewhere unrelated to the mistake, so name the mistake here instead.
    """
    values = value if isinstance(value, MULTI_VALUE_TYPES) else [value]
    if any(isinstance(item, QuerySet) for item in values):
        raise TypeError(
            f'tenant({dimension}=...) got a QuerySet, which is lazy and would be '
            f'evaluated while the scope is published to the database. Pass model '
            f'instances or pks -- list(...) or .values_list("pk", flat=True).'
        )


@contextmanager
def tenant(**dimensions: object) -> Iterator[None]:
    """Activate tenant ``dimensions`` (e.g. ``shop=instance``) for the block.

    Nested tenants merge with, then restore, the enclosing tenant on exit. Entering a
    tenant always *enforces*: an explicit ``tenant(...)`` nested inside
    :func:`tenancy_bypassed` re-enables scoping for its block.

    A ``None`` value is treated as *absent*, not "match everything" -- see
    ``TenantedManager.get_queryset``. A deliberate unfiltered read must say so with
    :func:`tenancy_bypassed`.
    """
    for dimension, value in dimensions.items():
        _reject_lazy(dimension, value)
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
    """Bypass tenant enforcement for the block (admin, migrations, jobs, shell).

    The deliberate, greppable cross-tenant path, and the *only* one -- there is no
    unscoped manager and no ``across_tenants()`` shortcut, so every cross-tenant access
    in a codebase is found by grepping for this name.

    Mirrored into PostgreSQL as ``tenant.bypass = 'on'``, which the row-level-security
    policies honour -- so this bypasses *both* enforcement layers, not just the Python
    one.
    """
    with tenant(**{BYPASS: True}):
        yield


_UNBOUND = object()  # sentinel: parameter not bound at all (vs bound to None)


def tenanted(func: Callable | None = None, *, arg: str = 'tenant', dimension: str | None = None):
    """Decorator: run the wrapped callable inside a tenant scope taken from its arguments.

    Two separate things, deliberately separable:

    * ``arg`` -- which *parameter* to read the value from (default ``'tenant'``).
    * ``dimension`` -- which *scope dimension* to open. Defaults to ``arg``, which is
      right whenever the parameter is named after the dimension.

    They are distinct because the dimension must match what the model's
    ``TenantedManager`` was declared with, while the parameter name belongs to the
    function's own signature. Collapsing them would silently open a dimension no manager
    scopes on -- which fails *closed* on a scoped read, but only after the call has
    already done its work unscoped in every other respect.

    Entrypoints that resolve a tenant from their payload (queue handlers, webhook
    receivers, gRPC servicers) decorate instead of repeating the ``with`` in every body::

        @tenanted
        def handle_uninstalled(tenant: Organization, payload: dict) -> None: ...


        @tenanted(arg='shop')  # parameter and dimension agree
        async def refresh_token(shop: Shop) -> None: ...


        @tenanted(arg='target_shop', dimension='shop')  # they do not
        def migrate_to(target_shop: Shop) -> None: ...

    Fail-closed: a tenant bound to ``None`` raises :class:`TenantScopeError` before the
    wrapped callable runs. A *missing* required argument is not re-labeled -- the call
    proceeds unscoped so Python raises its natural ``TypeError``. Sync and async
    callables are supported -- including an object whose ``__call__`` is async -- and the
    scope rides the ContextVar across ``await``. Generator functions are rejected at
    decoration time: their body runs at iteration, after the scope would already have
    closed.
    """
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
                raise TenantScopeError(
                    f'{fn_name} needs a non-None {arg!r} to open its tenant scope.'
                )
            return value

        # A callable *instance* with an ``async def __call__`` is not itself a coroutine
        # function, so it would take the sync branch and close the scope before the caller
        # ever awaits. Read ``__call__`` off the type, not the instance.
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
