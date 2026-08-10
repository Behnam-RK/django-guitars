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
    """Base for every tenant-scope failure.

    Catch this to handle any of them alike; catch :class:`TenantScopeMissing` or
    :class:`TenantScopeViolation` to handle just one. Never raised directly -- every
    raise site in guitars raises one of the two subclasses below.
    """


class TenantScopeMissing(TenantScopeError):  # noqa: N818 - name fixed by issue #12, not a typo
    """Raised when a tenant-scoped operation runs with no scope satisfying it.

    The caller-facing case: a read or write needed ``tenant(...)`` active -- wholly
    absent, or missing the one dimension this operation requires -- and found none. In a
    request-handling application this is ordinarily a 403, not a 500: the caller forgot
    to open a scope, nothing is broken.
    """


class TenantScopeViolation(TenantScopeError):  # noqa: N818 - name fixed by issue #12, not a typo
    """Raised when a write disagrees with the tenant scope that *is* active.

    Distinct from :class:`TenantScopeMissing`: a scope is active, but the write
    contradicts it -- an explicit value that does not match, a multi-value scope with
    nothing unambiguous to autofill, or PostgreSQL's own row-level-security policy
    rejecting the statement outright. Ordinarily an alerting signal rather than routine
    403 material: it means something in the application computed the wrong tenant.
    """


class TenantValueError(GuitarsError):
    """Raised when a tenant dimension's value cannot be safely published.

    Not a scope failure -- a data-modeling bug: the value itself (its primary key,
    typically) contains :data:`~guitars.gucs.VALUE_SEPARATOR`, the character the
    row-level-security policy splits a published GUC on to read a multi-value scope. See
    :func:`reject_separator`.
    """


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


def reject_separator(value: object, *, dimension: str | None = None) -> None:
    """Refuse a dimension value the GUC encoding could not carry unambiguously.

    A value containing :data:`~guitars.gucs.VALUE_SEPARATOR` is **refused**, and that is a
    security guard rather than tidiness. The policy predicate splits the published GUC on that
    separator (``= ANY(string_to_array(..., ','))``), so a single pk of ``'acme,globex'``
    encodes byte-for-byte identically to the two-tenant scope ``['acme', 'globex']`` -- and
    PostgreSQL then reads it as "tenant acme OR tenant globex". The Python manager meanwhile
    filters on the exact string and matches neither, so the database half would be strictly
    *wider* than the Python half, on exactly the paths (raw SQL, ``_base_manager``, cascades)
    where the policy is the only guard. That is the one direction this kit must never fail in.

    Refusing rather than escaping is deliberate. ``guitars.sql``'s emitted SQL is a frozen
    interface -- generated migrations already checked into consuming projects call
    ``create_tenant_policy`` by name -- so changing the predicate to carry an escape scheme
    would change SQL those migrations reproduce on a fresh database. Naming the mistake costs a
    tenant model nothing that a sane primary key wanted, in the same spirit as
    :func:`_reject_lazy` and ``sql.policy._bare``.

    **Called from two places, and neither is redundant.** :func:`tenant` calls it at scope
    entry, where the dimension is known and the traceback therefore points at the scope the
    caller opened rather than at whichever query happened to publish it first. ``guc._scalar``
    calls it at publish time, which is the actual boundary: a value whose ``pk`` is ``None`` at
    scope entry -- an unsaved instance -- passes the eager check and can still acquire a
    separator before anything publishes it.

    A ``None`` pk is skipped by both, because it publishes as the empty string, which the
    policy reads as an empty array and therefore denies.
    """
    values = value if isinstance(value, MULTI_VALUE_TYPES) else [value]
    for item in values:
        pk = getattr(item, 'pk', item)
        if pk is not None and VALUE_SEPARATOR in str(pk):
            # The eager call knows which dimension it was handed; the publish-time one does
            # not, and saying "tenant value" there beats inventing a name for it.
            subject = f'tenant({dimension}=...) value' if dimension else 'tenant value'
            raise TenantValueError(
                f'{subject} {str(pk)!r} contains {VALUE_SEPARATOR!r}, which separates the '
                f'values of one dimension when the scope is published to PostgreSQL -- a '
                f'row-level-security policy would read it as several tenants and match all '
                f'of them. Use a primary key without {VALUE_SEPARATOR!r}.'
            )


@contextmanager
def tenant(**dimensions: object) -> Iterator[None]:
    """Activate tenant ``dimensions`` (e.g. ``shop=instance``) for the block.

    Nested tenants merge with, then restore, the enclosing tenant on exit. Entering a
    tenant always *enforces*: an explicit ``tenant(...)`` nested inside
    :func:`tenancy_bypassed` re-enables scoping for its block.

    A ``None`` value is treated as *absent*, not "match everything" -- see
    ``tenanted_manager()``'s ``get_queryset``. A deliberate unfiltered read must say so with
    :func:`tenancy_bypassed`.

    Both guards below run before the frame is entered, so a value that could never be
    published fails at the ``with`` statement rather than inside an unrelated query.
    """
    for dimension, value in dimensions.items():
        # Order is load-bearing: reject_separator reads ``pk`` and falls back to str() on the
        # value itself, and str() on a QuerySet runs a query. _reject_lazy has to have said no
        # first, or the mistake it exists to name would resurface here instead.
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
    ``tenanted_manager()`` was declared with, while the parameter name belongs to the
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

    Fail-closed: a tenant bound to ``None`` raises :class:`TenantScopeMissing` before the
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
                raise TenantScopeMissing(
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
