"""The two queryset shapes a tenant-scoped manager hands out: guarded, and deny-by-default.

``_guarded_queryset_class`` wraps ``bulk_create``/``abulk_create`` with the write guard, for
a normal (satisfied-scope) read. ``_untenanted_queryset_class`` builds the queryset a
manager returns when the required scope is missing: every read raises, and a row-creating
write reports-or-raises depending on the enforcement mode.

**Allow-list, not deny-list.** ``_ALLOWED_UNSCOPED`` names the methods known safe to leave
reachable without an active scope; ``_apply_default_deny_sweep`` then denies every other
public method Django or guitars itself defines that nothing here already handles
explicitly. A method neither an explicit override nor ``_ALLOWED_UNSCOPED`` mentions -- a
future Django release's addition, or a new guitars queryset method nobody classified yet --
is denied by that sweep rather than silently inherited. See
``_untenanted_queryset_class``'s docstring for the one thing the sweep deliberately leaves
reachable: a downstream consumer's own custom queryset method.
"""

from __future__ import annotations

import functools

from django.db import models

from .enforcement import ViolationKind, _guarded, _violation
from .scope import TenantScopeMissing


__all__: list[str] = []

_ALLOWED_UNSCOPED: dict[str, str] = {
    # Django's own QuerySet surface: chain-building or metadata, never a database hit by
    # itself. Checked live against Django 5.0.14, 5.2.15 and 6.0.6 while writing this --
    # tests/test_tenancy_denylist.py's dynamic drift test is what catches the next one,
    # not this comment.
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
    # 'raw' is deliberately ABSENT: calling it doesn't touch the database, but the
    # RawQuerySet it returns is a distinct class that never passes back through this
    # denying queryset -- so allowing it here would be handing out an unscoped escape
    # hatch, the exact inconsistency M5 (#12) resolved by denying it instead. It falls
    # through to the default-deny sweep below like any other unclassified method.
}
"""Methods known safe to leave reachable on an unscoped queryset -- name -> why.

This is the allow-list half of the inversion: everything **not** named here, on a class
Django or guitars itself defines, is denied by ``_apply_default_deny_sweep`` below rather
than silently inherited. Adding a queryset method (guitars' own, or a future Django
release) is now safe by construction -- it is denied until someone moves it here with a
reason.
"""


def _closest_public_definitions(base: type[models.QuerySet]) -> dict[str, object]:
    """``{name: attribute}`` for every public (non-underscore) name reachable on *base*.

    Walks the MRO closest-first, so a subclass's own override wins -- matching what
    ``getattr`` would actually resolve to. Private helpers (``_clone``, ``_chain``,
    ``_filter_or_exclude``, ...) are deliberately excluded: they are plumbing invoked
    *by* the public methods above, not separate entry points, and several of the public
    methods classified here as lazy internally call them -- denying them by name would
    break the very filtering ``_ALLOWED_UNSCOPED`` just declared safe.
    """
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
    """Where *attr* was actually defined, for the default-deny sweep below.

    A ``property``'s module lives on its getter, not on the property object itself.
    """
    if isinstance(attr, property):
        attr = attr.fget
    return getattr(attr, '__module__', None)


def _denies_by_default(module: str | None) -> bool:
    """Whether an unclassified method should default-deny, based on where it lives.

    Django's own QuerySet surface and every queryset method guitars itself adds are both
    fail-closed: an addition to either -- a new Django release, or a future guitars
    queryset method nobody classified yet -- is denied until someone decides it belongs in
    :data:`_ALLOWED_UNSCOPED`. A downstream consumer's own custom queryset method is left
    alone -- see :func:`_untenanted_queryset_class`'s docstring for why denying it eagerly
    would be wrong.
    """
    return bool(module) and (
        module.startswith('django.db.models') or module.startswith('guitars.')
    )


def _unclassified_denier(name: str):
    """Build the method the default-deny sweep installs for one unclassified name.

    Distinct wording from ``_deny``/``_deny_query_write`` below (which describe a
    specific read or write): this one says *why* -- nothing classified it -- since the
    caller does not yet know whether it is a read or a write.
    """

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
    """Deny every public method of *base* the class body above left undecided.

    This is what makes the deny-list an allow-list: anything already overridden in
    *denying*'s own body stays as written, anything in ``_ALLOWED_UNSCOPED`` is left to
    resolve normally through inheritance, and everything else -- on Django's own
    ``QuerySet`` or on any class in the ``guitars`` package -- is denied by name. A
    downstream consumer's own custom queryset method is the one thing this leaves
    reachable: it can only reach the database through a primitive this module already
    denies, so eagerly denying it too would gain nothing and would break audit mode's
    report-and-proceed contract for that method.
    """
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
    """Build the scoped queryset: same as ``base``, but ``bulk_create`` is guarded.

    This has to be the queryset rather than the manager. A manager delegates
    ``bulk_create`` to ``get_queryset()``, so guarding the manager covers
    ``Model.objects.bulk_create(...)`` -- but the moment anything chains,
    ``Model.objects.filter(...).bulk_create(...)`` is Django's own method on a plain
    queryset, with no manager left in the call. That path would then skip both autofill
    and the cross-tenant check, silently.

    Cached per base class, like ``_untenanted_queryset_class`` -- one guarded class per
    queryset class, not one per model.
    """

    class _GuardedQuerySet(base):  # ty: ignore[unsupported-base]  # dynamic base
        """The manager's own queryset with the batched write path guarded."""

        def bulk_create(self, objs, *args, **kwargs):
            return super().bulk_create(_guarded(objs), *args, **kwargs)

        async def abulk_create(self, objs, *args, **kwargs):
            return await super().abulk_create(_guarded(objs), *args, **kwargs)

    return _GuardedQuerySet


@functools.cache
def _untenanted_queryset_class(base: type[models.QuerySet]) -> type[models.QuerySet]:
    """Build the deny-by-default queryset on top of the manager's own queryset.

    Subclassing ``base`` rather than plain ``QuerySet`` is what keeps a custom method
    reachable on the unscoped path. Without it ``Bundle.objects.lives()`` raises
    ``AttributeError`` when a scope is missing instead of the ``TenantScopeError`` that
    says what is actually wrong -- and in audit mode it breaks a path that is only
    supposed to report.

    **Allow-list, not deny-list.** The class body below overrides the methods known to
    need enforcement-mode-aware handling (``create``, ``bulk_create``, ...) or a tailored
    read/write message (``count``, ``update``, ...); ``_apply_default_deny_sweep`` then
    denies everything else Django or guitars defines that is not in
    :data:`_ALLOWED_UNSCOPED`. A method neither this class nor ``_ALLOWED_UNSCOPED``
    mentions -- a future Django release's addition, or a new guitars queryset method
    nobody classified yet -- is denied by that sweep rather than silently inherited. The
    one thing the sweep deliberately leaves alone is a *consumer's* own custom queryset
    method: it was never going to reach the database except through a primitive this
    module already denies, and denying it too would only break audit mode's
    report-and-proceed contract for that method without closing any real gap.

    Cached per base class: the same manager class always yields the same denying class,
    so this builds one apiece rather than one per manager instance.
    """

    class _UntenantedQuerySet(base):  # ty: ignore[unsupported-base]  # dynamic base
        """Returned when a required scope is missing: every read raises TenantScopeError.

        Fail-closed by construction. ``.none()`` stays usable; row-creating writes are
        denied in strict mode and merely reported in audit mode, so enforcement can be
        rolled out without 500-ing live paths.
        """

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
                f'{", ".join(sorted(self._missing))} -- wrap it in tenant(...), or '
                f'tenancy_bypassed() for a deliberate cross-tenant {action}.'
            )

        def _deny(self, *args, **kwargs):
            raise TenantScopeMissing(self._message('read'))

        def _deny_query_write(self, *args, **kwargs):
            # Same refusal, honest wording: these mutate rows rather than read them. Still
            # TenantScopeMissing, not TenantScopeViolation -- the queryset itself has no
            # scope, the same condition _deny above raises for.
            raise TenantScopeMissing(self._message('write'))

        def _plain(self) -> models.QuerySet:
            """A non-denying queryset over the same model, of the manager's own type.

            ``hints`` is carried over, not dropped: it is what a database router reads to
            route a query, so losing it here would send the audit-mode write (and the empty
            queryset ``none()`` hands back) to a different database than the scoped path
            would have used.
            """
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
                # No single `dimension` -- the whole queryset has no active scope on any
                # of `self._missing`, not one field disagreeing with an otherwise-active
                # scope. `_message` already names them; `missing` repeats that as
                # structured context a reporter can read without parsing the message.
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
            # Only reached in audit mode. A partly-satisfied scope still has something to
            # say about the dimensions it *does* cover, and bulk_create fires no pre_save
            # to say it -- so mirror what create() gets for free, or the batched path
            # validates strictly less.
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
        # Set-wide writes: on an unscoped set they would mutate every tenant's rows.
        # bulk_update is denied in its own right, not just because Django currently
        # implements it over .update() -- that is an internal detail, and the guarantee
        # here should not rest on it.
        update = delete = bulk_update = _deny_query_write
        aupdate = adelete = abulk_update = _deny_query_write
        # guitars' own set-wide writes, from HardDeletableQuerySet. Absent from this list
        # they would sail straight through: unscoped, they PERMANENTLY delete every
        # tenant's rows, and before FORCE ROW LEVEL SECURITY is on, the database would not
        # stop them either. Neither has an async twin -- both are sync-only.
        #
        # _hard_delete_own_table is private but denied in its own right, not merely because
        # hard_delete() calls it: it compiles a DeleteQuery straight off ``self.query`` and
        # executes it, so reaching it directly on an unscoped queryset would issue an
        # unfiltered DELETE without passing through the public method at all.
        #
        # tests/test_tenancy_denylist.py fails if a queryset method appears unclassified,
        # so this list cannot quietly fall behind the querysets it guards.
        #
        # _raw_delete is Django's own primitive underneath delete()/Collector: it compiles
        # a DeleteQuery straight off self.query and executes it, with no signals and no
        # per-row guard. Unscoped, that is an unfiltered DELETE across every tenant.
        hard_delete = _hard_delete_own_table = _raw_delete = _deny_query_write
        # iterator()/aiterator() deliberately stream without populating _result_cache, so
        # they skip _fetch_all entirely -- deny them by name.
        iterator = aiterator = _deny

    _apply_default_deny_sweep(_UntenantedQuerySet, base)
    return _UntenantedQuerySet
