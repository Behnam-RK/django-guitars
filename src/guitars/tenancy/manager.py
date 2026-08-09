"""The manager that scopes reads.

``TenantedManager`` filters ``get_queryset()`` by the active frame, and returns a queryset
(``guitars.tenancy.querysets``) that refuses to run at all when the frame is missing. Write
guarding lives in ``guitars.tenancy.enforcement``; what a model is tenanted *on* lives in
``guitars.tenancy.spec``. This module is the thin factory that ties the three together into
one manager class.
"""

from __future__ import annotations

from django.db import models

from .querysets import _guarded_queryset_class, _untenanted_queryset_class
from .scope import MULTI_VALUE_TYPES, get_tenant, is_bypassed


__all__ = ['TenantedManager']


def _self_install() -> None:
    """Activate enforcement because a tenanted model was just declared.

    Declaring a ``TenantedManager`` *is* the opt-in, so nothing needs remembering and
    guitars needs no ``INSTALLED_APPS`` entry. ``GuitarsConfig.ready()`` calls the same
    idempotent ``install()`` when the app *is* installed; whichever fires first wins and
    the second is a no-op.

    Imported inside the function, not at module scope, to break a genuine cycle:
    ``tenancy/__init__`` imports this module, and ``checks`` (which ``install()`` needs)
    imports ``TenantEnforcement`` from ``enforcement``. By the time any model is defined,
    the package is fully imported and this resolves straight out of ``sys.modules``.
    """
    from guitars import tenancy  # noqa: PLC0415 - deferred to break the import cycle

    tenancy.install()


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
