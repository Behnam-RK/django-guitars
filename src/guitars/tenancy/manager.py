"""The manager that scopes reads. ``tenanted_manager()`` filters ``get_queryset()`` by the
active frame, returning a queryset (``guitars.tenancy.querysets``) that refuses to run at
all when it's missing. Write guarding lives in ``enforcement``; scope lives in ``spec``."""

from __future__ import annotations

from django.db import models

from .querysets import _guarded_queryset_class, _untenanted_queryset_class
from .scope import MULTI_VALUE_TYPES, get_tenant, is_bypassed


__all__ = ['TenantedManagerBase', 'tenanted_manager']


class TenantedManagerBase:
    """Marker mixed into every manager ``tenanted_manager()`` builds -- contributes no
    behaviour, but gives ``isinstance(Model.objects, TenantedManagerBase)`` a real type to
    recognise instead of relying on the private ``_tenant_dimensions`` attribute."""


def _self_install() -> None:
    """Activate enforcement because a tenanted model was just declared -- the opt-in
    itself, so guitars needs no ``INSTALLED_APPS`` entry. Imported inside the function to
    break a cycle: ``tenancy/__init__`` imports this module, and ``checks`` imports it back."""
    from guitars import tenancy  # noqa: PLC0415 - deferred to break the import cycle

    tenancy.install()


def tenanted_manager(
    _manager_class: type[models.Manager] | models.Manager = models.Manager,
    autofill: bool | None = None,
    **dimensions: str,
):
    """Build a manager enforcing ``dimensions`` (``name='orm__lookup'``) -- usage in
    ``docs/tenancy.md``. Subclasses ``_manager_class`` and :class:`TenantedManagerBase`.
    A manager *instance* (``QuerySet.as_manager()``) is accepted too, not just a class."""
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

    class _TenantedManager(
        _manager_class,  # ty: ignore[unsupported-base]  # dynamic base
        TenantedManagerBase,
    ):
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
