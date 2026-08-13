"""Whether an app is one of the project's own, per ``settings.LOCAL_APPS``.

A leaf module on purpose, following the precedent ``guitars.gucs`` already set: it
imports only ``django.conf.settings``, and sits at the top level of the package rather
than inside ``tenancy/`` or ``management/``, so either can depend on it without either
depending on the other.

Before this module existed, ``is_local`` was duplicated verbatim between
``guitars.tenancy.discovery`` and ``guitars.management._generator`` -- deliberately,
because neither package was allowed to import the other: ``tenancy`` is documented as
importing only the standard library and Django so it can move as a unit, while
``management`` already depends on ``tenancy`` (it calls
:func:`~guitars.tenancy.discovery.app_coverage`), so the dependency could not also run
the other way. A shared leaf module -- the same shape ``guitars.gucs`` already uses for
exactly this problem -- is what was actually missing; the duplication was never load-
bearing, just a symptom of nowhere neutral to put it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings


if TYPE_CHECKING:
    from django.apps import AppConfig


__all__ = ['is_local']


def is_local(app: AppConfig) -> bool:
    """Whether *app* is one of the project's own, per ``settings.LOCAL_APPS``.

    Keyed on ``app.name`` -- the same string ``INSTALLED_APPS`` holds -- rather than
    Django's short ``app.label``. The two coincide for a top-level app (``blog``) but
    diverge for a nested one (``tests.testapp``, label ``testapp``), so matching on the
    label would silently miss every app whose module path is dotted.
    """
    return app.name in settings.LOCAL_APPS
