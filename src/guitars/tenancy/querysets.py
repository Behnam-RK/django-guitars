"""The two queryset shapes a tenant-scoped manager hands out: ``_guarded_queryset_class``
(bulk-write guard for a satisfied scope) and ``_untenanted_queryset_class`` (deny-by-default
when the scope is missing). See ADR-0008 for why this is an allow-list, not a deny-list."""

from __future__ import annotations

import functools

from django.db import models

from .enforcement import ViolationKind, _guarded, _violation
from .messages import remediation
from .scope import TenantScopeMissing


__all__: list[str] = []

_ALLOWED_UNSCOPED: dict[str, str] = {
    # Django's own QuerySet surface: chain-building or metadata, never a database hit by
    # itself. tests/test_tenancy_denylist.py's dynamic drift test catches the next one.
    'alias': 'lazy: builds a new queryset, does not touch the database',
    'all': 'lazy: builds a new queryset, does not touch the database',
    'annotate': 'lazy: builds a new queryset, does not touch the database',
    'as_manager': 'metadata: returns a Manager, never queries',
    'complex_filter': 'lazy: builds a new queryset, does not touch the database',
    'dates': 'lazy: builds a new queryset, does not touch the database',
    'datetimes': 'lazy: builds a new queryset, does not touch the database',
    'db': 'metadata: resolves the alias to use, does not query',
    'defer': 'lazy: builds a new queryset, does not touch the database',
    'difference': 'lazy: builds a new queryset, does not touch the database',
    'distinct': 'lazy: builds a new queryset, does not touch the database',
    'exclude': 'lazy: builds a new queryset, does not touch the database',
    'extra': 'lazy: builds a new queryset, does not touch the database',
    'filter': 'lazy: builds a new queryset, does not touch the database',
    'intersection': 'lazy: builds a new queryset, does not touch the database',
    'only': 'lazy: builds a new queryset, does not touch the database',
    'order_by': 'lazy: builds a new queryset, does not touch the database',
    'ordered': 'metadata: inspects the query, does not execute it',
    'prefetch_related': 'lazy: builds a new queryset, does not touch the database',
    'query': 'metadata: builds the Query object, does not execute it',
    'resolve_expression': 'metadata: used when this queryset is nested in another query',
    'reverse': 'lazy: builds a new queryset, does not touch the database',
    'select_for_update': 'lazy: builds a new queryset, does not touch the database',
    'select_related': 'lazy: builds a new queryset, does not touch the database',
    'union': 'lazy: builds a new queryset, does not touch the database',
    'using': 'lazy: builds a new queryset, does not touch the database',
    'values': 'lazy: builds a new queryset, does not touch the database',
    'values_list': 'lazy: builds a new queryset, does not touch the database',
    # guitars' own additions.
    'lives': 'property returning self.filter(...) -- lazy, chains into a denying clone',
    'archives': 'property returning self.filter(...) -- lazy, chains into a denying clone',
    # 'raw' is deliberately ABSENT: the RawQuerySet it returns never passes back through
    # this denying queryset, so allowing it here would hand out an unscoped escape hatch --
    # the exact inconsistency M5 (#12) resolved by denying it instead.
}
"""Methods known safe on an unscoped queryset -- name -> why. Everything **not** named
here, on a class Django or guitars defines, is denied by ``_apply_default_deny_sweep``
rather than silently inherited. See ADR-0008."""


def _closest_public_definitions(base: type[models.QuerySet]) -> dict[str, object]:
    """``{name: attribute}`` for every public (non-underscore) name reachable on *base*,
    MRO-walked closest-first. Private helpers (``_clone``, ``_chain``, ...) are excluded --
    plumbing the public methods call internally, not separate entry points."""
    found: dict[str, object] = {}
    for klass in base.__mro__:
        if klass is object:
            continue
        for name, value in vars(klass).items():
            if name.startswith('_'):
                continue
            found.setdefault(name, value)
    return found


def _defining_module(attr: object) -> str | None:
    """Where *attr* was defined -- a ``property``'s module lives on its getter, not the
    property object itself."""
    if isinstance(attr, property):
        attr = attr.fget
    return getattr(attr, '__module__', None)


def _denies_by_default(module: str | None) -> bool:
    """Whether an unclassified method default-denies: Django's own QuerySet surface and
    every guitars queryset method are fail-closed, until moved to
    :data:`_ALLOWED_UNSCOPED`. A downstream consumer's own custom method is left alone."""
    return bool(module) and (
        module.startswith('django.db.models') or module.startswith('guitars.')
    )


def _unclassified_denier(name: str):
    """The method the default-deny sweep installs for one unclassified name -- distinct
    wording from ``_deny``/``_deny_query_write`` below, since the caller doesn't yet know
    whether it's a read or a write."""

    def _denier(self, *args, **kwargs):
        model_name = self.model.__name__ if self.model else 'Query'
        raise TenantScopeMissing(
            f'{model_name}.{name}() is not classified as safe to call without an active '
            f'tenant scope, and guitars denies what it has not classified. If {name!r} '
            f'is lazy or otherwise harmless, add it to _ALLOWED_UNSCOPED in '
            f'guitars/tenancy/querysets.py with a reason; otherwise deny it explicitly.'
        )

    _denier.__name__ = f'_deny_unclassified_{name}'
    _denier.__qualname__ = _denier.__name__
    return _denier


def _apply_default_deny_sweep(denying: type[models.QuerySet], base: type[models.QuerySet]) -> None:
    """Deny every public method of *base* left undecided by *denying*'s own class body or
    :data:`_ALLOWED_UNSCOPED`. A downstream consumer's own custom method is the one thing
    left reachable -- it can only reach the database through a primitive already denied."""
    handled = set(vars(denying))
    for name, original in _closest_public_definitions(base).items():
        if name in handled or name in _ALLOWED_UNSCOPED:
            continue
        if not _denies_by_default(_defining_module(original)):
            continue
        denier = _unclassified_denier(name)
        setattr(denying, name, property(denier) if isinstance(original, property) else denier)


@functools.cache
def _guarded_queryset_class(base: type[models.QuerySet]) -> type[models.QuerySet]:
    """Build the scoped queryset: same as ``base``, but ``bulk_create`` is guarded. Has to
    be the queryset, not the manager -- ``Model.objects.filter(...).bulk_create(...)`` is
    Django's own method on a plain queryset with no manager left in the call."""

    class _GuardedQuerySet(base):  # ty: ignore[unsupported-base]  # dynamic base
        """The manager's own queryset with the batched write path guarded."""

        def bulk_create(self, objs, *args, **kwargs):
            return super().bulk_create(_guarded(objs), *args, **kwargs)

        async def abulk_create(self, objs, *args, **kwargs):
            return await super().abulk_create(_guarded(objs), *args, **kwargs)

    return _GuardedQuerySet


@functools.cache
def _untenanted_queryset_class(base: type[models.QuerySet]) -> type[models.QuerySet]:
    """Build the deny-by-default queryset on top of the manager's own queryset. Subclasses
    ``base`` rather than plain ``QuerySet`` so a consumer's custom method stays reachable
    (raising ``TenantScopeError`` instead of ``AttributeError``). See ADR-0008."""

    class _UntenantedQuerySet(base):  # ty: ignore[unsupported-base]  # dynamic base
        """Returned when a required scope is missing: every read raises TenantScopeError.
        ``.none()`` stays usable; row-creating writes are denied in strict mode and
        reported in audit mode, so enforcement can roll out without 500-ing live paths."""

        def __init__(self, *args, missing: set[str] | None = None, **kwargs) -> None:
            # Defaulted because Django's _clone() reconstructs via ``__class__(...)``
            # without our kwarg; _clone() below restores the real value afterwards.
            self._missing = missing or set()
            super().__init__(*args, **kwargs)

        def _clone(self):
            clone = super()._clone()
            clone._missing = self._missing
            return clone

        def _message(self, action: str) -> str:
            model_name = self.model.__name__ if self.model else 'Query'
            return (
                f'{model_name} {action} needs an active tenant scope on '
                f'{", ".join(sorted(self._missing))} -- {remediation(action)}'
            )

        def _deny(self, *args, **kwargs):
            raise TenantScopeMissing(self._message('read'))

        def _deny_query_write(self, *args, **kwargs):
            # Same refusal, honest wording: these mutate rows rather than read them. Still
            # TenantScopeMissing, not TenantScopeViolation -- the queryset itself has no
            # scope, the same condition _deny above raises for.
            raise TenantScopeMissing(self._message('write'))

        def _plain(self) -> models.QuerySet:
            """A non-denying queryset over the same model. ``hints`` is carried over, not
            dropped: a database router reads it to route a query, so losing it here would
            send audit-mode writes to a different database than the scoped path used."""
            return base(model=self.model, using=self._db, hints=self._hints)

        def none(self) -> models.QuerySet:
            # Plain (non-denying) empty queryset so framework-level empties still resolve.
            return self._plain().none()

        def _deny_write(self, action: str):
            """Deny a row-creating write, or report it in audit mode."""
            model_name = self.model.__name__ if self.model else 'Query'
            _violation(
                self._message(action),
                key=(model_name, action, 'unscoped-write'),
                exception=TenantScopeMissing,
                kind=ViolationKind.UNSCOPED,
                model=model_name,
                action=action,
                # No single `dimension`: the whole queryset has no scope on any of
                # `self._missing`, so `missing` repeats that as structured context.
                missing=sorted(self._missing),
            )

        def create(self, **kwargs):
            self._deny_write('create')
            return self._plain().create(**kwargs)

        async def acreate(self, **kwargs):
            self._deny_write('create')
            return await self._plain().acreate(**kwargs)

        def bulk_create(self, objs, *args, **kwargs):
            self._deny_write('bulk_create')
            # Only reached in audit mode: bulk_create fires no pre_save, so mirror what
            # create() gets for free, or the batched path validates strictly less.
            objs = _guarded(objs)
            return self._plain().bulk_create(objs, *args, **kwargs)

        async def abulk_create(self, objs, *args, **kwargs):
            self._deny_write('bulk_create')
            objs = _guarded(objs)
            return await self._plain().abulk_create(objs, *args, **kwargs)

        # Row materialisation funnels through _fetch_all (iteration, len, bool, repr,
        # values, first/last, get, in_bulk): one chokepoint catches them all.
        _fetch_all = _deny
        # DB hits that bypass _fetch_all need explicit denial.
        count = exists = aggregate = explain = _deny
        acount = aexists = aaggregate = _deny
        # Set-wide writes: on an unscoped set they would mutate every tenant's rows. Not
        # denied merely because Django implements bulk_update over .update() -- an internal
        # detail the guarantee here should not rest on.
        update = delete = bulk_update = _deny_query_write
        aupdate = adelete = abulk_update = _deny_query_write
        # guitars' own set-wide writes (HardDeletableQuerySet): unscoped, both PERMANENTLY
        # delete every tenant's rows. _hard_delete_own_table/_raw_delete are private but
        # denied in their own right -- both compile a DeleteQuery off self.query directly.
        hard_delete = _hard_delete_own_table = _raw_delete = _deny_query_write
        # iterator()/aiterator() stream without populating _result_cache, skipping
        # _fetch_all entirely -- deny them by name.
        iterator = aiterator = _deny

    _apply_default_deny_sweep(_UntenantedQuerySet, base)
    return _UntenantedQuerySet
