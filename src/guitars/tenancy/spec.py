"""What a model is tenanted on, read off its managers.

A model is tenanted by virtue of *having* a manager built by ``tenanted_manager()`` --
there is no second registry to keep in step. This module answers "on what?" (``{dimension:
lookup}``, and the local-column subset of it); ``guitars.tenancy.enforcement`` answers "is
this write allowed?", and ``guitars.tenancy.querysets``/``guitars.tenancy.manager`` answer
"is this read allowed?". Split out on its own because both of the others, plus
``guitars.tenancy.discovery`` (RLS policy coverage) and ``guitars.tenancy.checks``, need
these questions answered without pulling in write-guard or queryset machinery.
"""

from __future__ import annotations

import functools

from django.conf import settings
from django.core.exceptions import FieldDoesNotExist
from django.db import models


__all__ = ['local_tenant_fields', 'tenant_spec']


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
    tenant-scoped manager -- there is no second registry to keep in step.

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


def _autofill_default() -> bool:
    # Off by default: a library should not start assigning tenants implicitly on a project
    # that never asked. GuitarModel passes autofill=True explicitly for the FK it owns.
    return bool(getattr(settings, 'GUITARS_TENANT_AUTOFILL', False))


def _autofills(model: type[models.Model]) -> bool:
    for manager in _meta(model).managers:
        if getattr(manager, '_tenant_dimensions', None):
            override = getattr(manager, '_tenant_autofill', None)
            return _autofill_default() if override is None else override
    return False
