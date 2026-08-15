"""What a model is tenanted on, read off its managers -- a model is tenanted by *having*
a manager built by ``tenanted_manager()``, no second registry. This module answers "on
what?"; ``enforcement``/``querysets``/``manager`` answer "is this write/read allowed?"."""

from __future__ import annotations

import functools

from django.conf import settings
from django.core.exceptions import FieldDoesNotExist
from django.db import models


__all__ = ['local_tenant_fields', 'tenant_spec']


def _meta(model: type[models.Model]):
    """``Model._meta``, in one place -- private Django API, so a future release moving it
    is one line to change instead of a dozen call sites."""
    return model._meta


@functools.cache
def tenant_spec(model: type[models.Model]) -> dict[str, str]:
    """``{dimension: lookup}`` for ``model``, or ``{}`` if not tenanted -- read off its
    managers. Cached per class: the ``pre_save`` receiver runs on every save with no
    sender, and a model's managers are fixed once its class is built."""
    for manager in _meta(model).managers:
        dimensions = getattr(manager, '_tenant_dimensions', None)
        if dimensions:
            return dimensions
    return {}


def local_tenant_fields(model: type[models.Model]) -> dict[str, str]:
    """``{dimension: local field name}`` -- only dimensions on this table. A multi-hop
    dimension has no column here, so it's filtered out rather than half-supported."""
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
