"""Whether an app is one of the project's own, per ``settings.LOCAL_APPS`` -- a leaf module
(like ``guitars.gucs``) so ``tenancy`` and ``management`` can both depend on it without
depending on each other. Duplicated verbatim between them before this existed."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings


if TYPE_CHECKING:
    from django.apps import AppConfig


__all__ = ['is_local']


def is_local(app: AppConfig) -> bool:
    """Whether *app* is one of the project's own, per ``settings.LOCAL_APPS`` -- keyed on
    ``app.name`` (the same string ``INSTALLED_APPS`` holds), not the short ``app.label``,
    which diverges for a nested app (``tests.testapp``, label ``testapp``)."""
    return app.name in settings.LOCAL_APPS
