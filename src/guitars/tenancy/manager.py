"""The manager that scopes reads, and the guards that scope writes.

Reads: :func:`TenantedManager` filters ``get_queryset()`` by the active frame, and
returns a queryset that refuses to run at all when the frame is missing.

Writes: a ``pre_save`` receiver plus a ``bulk_create`` override fill in the tenant when
it is absent and reject it when it contradicts the active frame. Because the receiver is
on the *signal*, it covers every ``save()`` -- including ``instance.save()`` and writes
routed through ``_base_manager``, neither of which consults a manager. The
``bulk_create`` guard lives on the *queryset* rather than the manager, because chaining
leaves the manager behind: ``Model.objects.filter(...)`` hands back a queryset, and a
guard the manager owned alone simply would not be on it.

What this deliberately does not cover: ``queryset.update()``, ``bulk_create`` on a
non-tenanted manager (no signal, no override), cascade deletes, and raw SQL. Those are
the database's job -- its ``WITH CHECK`` sees every statement, which is the whole reason
enforcement lives there too. What Python adds is a diagnosable error and an autofill the
database cannot perform.
"""

from __future__ import annotations

import functools
from enum import Enum

from django.conf import settings
from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models.signals import pre_save

from .reporting import report_once
from .scope import MULTI_VALUE_TYPES, TenantScopeError, get_tenant, is_bypassed


__all__ = [
    'TenantEnforcement',
    'TenantedManager',
    'apply_write_guard',
    'install_write_guards',
    'local_tenant_fields',
    'tenant_spec',
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


def _enforcement() -> TenantEnforcement:
    return TenantEnforcement(getattr(settings, 'GUITARS_TENANT_ENFORCE', TenantEnforcement.STRICT))


def _autofill_default() -> bool:
    # Off by default: a library should not start assigning tenants implicitly on a project
    # that never asked. GuitarModel passes autofill=True explicitly for the FK it owns.
    return bool(getattr(settings, 'GUITARS_TENANT_AUTOFILL', False))


def _violation(message: str, *, key: object) -> None:
    """Raise, or merely report, depending on the enforcement mode."""
    if _enforcement() is TenantEnforcement.AUDIT:
        report_once(key, message, mode=TenantEnforcement.AUDIT.value)
        return
    raise TenantScopeError(message)


def _pk(value: object) -> object:
    return getattr(value, 'pk', value)


def _matches(expected: object, actual: object) -> bool:
    """Whether ``actual`` satisfies ``expected``, which may be a collection."""
    if isinstance(expected, MULTI_VALUE_TYPES):
        return any(str(_pk(item)) == str(actual) for item in expected)
    return str(_pk(expected)) == str(actual)


# ─────────────────────────────── model spec ─────────────────────────────── #


def _meta(model: type[models.Model]):
    """``Model._meta``, in one place.

    A named seam rather than inline ``model._meta`` at a dozen call sites: it is private
    Django API, so if a future release moves it there is one line to change. The return is
    left unannotated deliberately -- ``Options`` is generic over the model and pinning it
    here would buy nothing these call sites use.
    """
    return model._meta


@functools.cache
def tenant_spec(model: type[models.Model]) -> dict[str, str]:
    """``{dimension: lookup}`` for ``model``, or ``{}`` if it is not tenanted.

    Read off the model's managers, so a model is tenanted by virtue of *having* a
    ``TenantedManager`` -- there is no second registry to keep in step.

    Cached per model class, because the ``pre_save`` receiver is connected without a
    sender: it runs on **every** save in the project, tenanted model or not, and each one
    would otherwise walk ``_meta.managers`` to be told "no" again. A model's managers are
    fixed once its class is built -- Django copies an abstract base's down at subclass
    creation -- so the answer cannot change under the cache. A later ``add_to_class`` of a
    manager would be the one thing that invalidates it; nothing in the kit does that after
    class creation, and ``GuitarModel``'s own contribution happens at import, before any
    concrete subclass exists.

    Note the cache lives *inside* the function, so patching the module attribute in a test
    replaces it wholesale and is unaffected -- which is how ``discovery`` and this module's
    own ``local_tenant_fields`` are stubbed.
    """
    for manager in _meta(model).managers:
        dimensions = getattr(manager, '_tenant_dimensions', None)
        if dimensions:
            return dimensions
    return {}


def local_tenant_fields(model: type[models.Model]) -> dict[str, str]:
    """``{dimension: local field name}`` -- only dimensions on this table.

    A multi-hop dimension (``shop='post__shop'``) has no column here, so it can be
    neither autofilled nor covered by a row-level-security policy; it is filtered out
    rather than half-supported.
    """
    fields = {}
    for dimension, lookup in tenant_spec(model).items():
        if '__' in lookup:
            continue
        try:
            field = _meta(model).get_field(lookup)
        except FieldDoesNotExist:
            # A lookup naming no field simply isn't local. Caught by type rather than as a
            # bare Exception so a genuine bug in the surrounding code still surfaces.
            continue
        if getattr(field, 'concrete', False):
            fields[dimension] = lookup
    return fields


def _autofills(model: type[models.Model]) -> bool:
    for manager in _meta(model).managers:
        if getattr(manager, '_tenant_dimensions', None):
            override = getattr(manager, '_tenant_autofill', None)
            return _autofill_default() if override is None else override
    return False


# ────────────────────────────── write guards ────────────────────────────── #


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
                )
            else:
                setattr(instance, attname, _pk(expected))
        elif not _matches(expected, actual):
            _violation(
                f'{label} write sets {dimension}={actual!r} while the active scope is '
                f'{_pk(expected)!r} -- a write may not cross tenants. Use '
                f'tenancy_bypassed() if that is genuinely intended.',
                key=(label, dimension, 'mismatch'),
            )


def _guarded(objs) -> list:
    """Materialise ``objs`` and guard each one.

    Shared by every ``bulk_create`` override: the batched path sends no ``pre_save``, so
    the guard has to be invoked by hand or that path would skip autofill and validation
    both.
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


def _self_install() -> None:
    """Activate enforcement because a tenanted model was just declared.

    Declaring a ``TenantedManager`` *is* the opt-in, so nothing needs remembering and
    guitars needs no ``INSTALLED_APPS`` entry. ``GuitarsConfig.ready()`` calls the same
    idempotent ``install()`` when the app *is* installed; whichever fires first wins and
    the second is a no-op.

    Imported inside the function, not at module scope, to break a genuine cycle:
    ``tenancy/__init__`` imports this module, and ``checks`` (which ``install()`` needs)
    imports ``TenantEnforcement`` from it. By the time any model is defined, the package
    is fully imported and this resolves straight out of ``sys.modules``.
    """
    from guitars import tenancy  # noqa: PLC0415 - deferred to break the import cycle

    tenancy.install()


# ─────────────────────────────── read guards ────────────────────────────── #

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
Django or guitars itself defines, is denied by ``_default_deny_sweep`` below rather than
silently inherited. Adding a queryset method (guitars' own, or a future Django release)
is now safe by construction -- it is denied until someone moves it here with a reason.
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

    Distinct wording from ``_deny``/``_deny_query_write`` above (which describe a
    specific read or write): this one says *why* -- nothing classified it -- since the
    caller does not yet know whether it is a read or a write.
    """

    def _denier(self, *args, **kwargs):
        model_name = self.model.__name__ if self.model else 'Query'
        raise TenantScopeError(
            f'{model_name}.{name}() is not classified as safe to call without an active '
            f'tenant scope, and guitars denies what it has not classified. If {name!r} '
            f'is lazy or otherwise harmless, add it to _ALLOWED_UNSCOPED in '
            f'guitars/tenancy/manager.py with a reason; otherwise deny it explicitly.'
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
            raise TenantScopeError(self._message('read'))

        def _deny_query_write(self, *args, **kwargs):
            # Same refusal, honest wording: these mutate rows rather than read them.
            raise TenantScopeError(self._message('write'))

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
            _violation(self._message(action), key=(model_name, action, 'unscoped-write'))

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


def TenantedManager(  # noqa: N802 - returns a manager instance; PascalCase reads as a type
    _manager_class: type[models.Manager] | models.Manager = models.Manager,
    autofill: bool | None = None,
    **dimensions: str,
):
    """Build a manager enforcing ``dimensions`` (``name='orm__lookup'``).

    Returns an instance subclassing ``_manager_class`` so the underlying queryset
    (soft-delete filtering, custom methods) is preserved; the tenant filter layers on top
    of ``super().get_queryset()``. Multi-hop lookups and several dimensions are allowed::

        TenantedManager(shop='shop')
        TenantedManager(_manager_class=LiveManager, shop='shop')
        TenantedManager(shop='post__shop')
        TenantedManager(shop='shop', user='author')

    ``autofill`` overrides ``GUITARS_TENANT_AUTOFILL`` for this model -- pass ``False``
    where taking the tenant implicitly would be wrong (an append-only archive, say).
    Only a dimension that is a local column can be autofilled, so requesting it for a
    multi-hop dimension is rejected here rather than silently doing nothing.

    ``QuerySet.as_manager()`` is accepted as well as a manager class: it hands back an
    *instance*, and subclassing one fails with a baffling
    ``BaseManager.__init__() takes 1 positional argument``. A manager holds no state
    until ``contribute_to_class``, so its class is all we need.
    """
    _self_install()
    if isinstance(_manager_class, models.Manager):
        _manager_class = type(_manager_class)
    required = set(dimensions)
    if autofill and any('__' in lookup for lookup in dimensions.values()):
        multi_hop = sorted(lookup for lookup in dimensions.values() if '__' in lookup)
        raise TypeError(
            f'autofill needs a dimension stored on this table, but {multi_hop} '
            f'traverse a relation. Drop autofill, or scope on a local field.'
        )

    # Both derived classes are built from the manager's own queryset, so custom methods
    # survive on the scoped path and on the unscoped one alike.
    base_queryset = getattr(_manager_class, '_queryset_class', models.QuerySet)
    denying = _untenanted_queryset_class(base_queryset)

    class _TenantedManager(_manager_class):  # ty: ignore[unsupported-base]  # dynamic base
        #: Read by tenant_spec() and by the RLS policy generator.
        _tenant_dimensions = dict(dimensions)
        _tenant_autofill = autofill
        #: Everything this manager hands out is guarded, chained or not -- and since the
        #: manager's own bulk_create delegates here, that covers
        #: Model.objects.bulk_create() too, with no second copy of the guard.
        _queryset_class = _guarded_queryset_class(base_queryset)

        def get_queryset(self) -> models.QuerySet:
            if is_bypassed():
                return super().get_queryset()
            active = get_tenant()
            # Satisfied only when present AND non-None; a None value would skip its filter
            # and fail OPEN, so treat it as missing.
            missing = {dim for dim in required if active.get(dim) is None}
            if missing:
                return denying(
                    self.model,
                    using=self._db,
                    # Carried over for the same reason the soft-delete managers pass it: a
                    # database router reads hints to route, and this queryset stands in for
                    # the one ``super().get_queryset()`` would have built.
                    hints=self._hints,
                    # The denying subclass's own kwarg; the checker only sees the declared
                    # type[QuerySet], which a dynamic class cannot refine.
                    missing=missing,  # ty: ignore[unknown-argument]
                )
            filters: dict[str, object] = {}
            for dim in required:
                value = active[dim]
                lookup = dimensions[dim]
                if isinstance(value, MULTI_VALUE_TYPES):
                    filters[f'{lookup}__in'] = value
                else:
                    filters[lookup] = value
            return super().get_queryset().filter(**filters)

    return _TenantedManager()
