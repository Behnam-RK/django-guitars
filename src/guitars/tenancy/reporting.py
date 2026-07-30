"""Where tenancy sends its audit signal, without knowing who is listening.

Deliberately dependency-free (standard library only) so the package stays portable: a
project with Sentry, structlog or loguru installs its own reporter via
:func:`set_reporter`; a project with none gets the ``logging`` default and nothing
breaks.

Only ``'audit'`` enforcement mode reports -- ``'strict'`` raises instead, so nothing
here is on the hot path of a healthy deployment.
"""

from __future__ import annotations

import logging
from typing import Protocol


__all__ = ['Reporter', 'report_once', 'reset_reported', 'set_reporter']

logger = logging.getLogger('guitars.tenancy')


class Reporter(Protocol):
    """Receives one audit finding. Implementations must not raise."""

    def __call__(self, message: str, /, **context: object) -> None: ...


def _log_reporter(message: str, /, **context: object) -> None:
    logger.warning('%s | %s', message, context)


_reporter: Reporter = _log_reporter
# Findings already reported by this process, so a hot query path cannot emit thousands of
# identical events. Captured once per distinct cause, not once per occurrence -- the same
# rule a retry storm follows.
_reported: set[object] = set()


def set_reporter(reporter: Reporter) -> None:
    """Replace the audit reporter. The default writes to the ``guitars.tenancy`` logger."""
    global _reporter  # noqa: PLW0603 - module-level singleton is the point
    _reporter = reporter


def reset_reported() -> None:
    """Forget which findings were already reported. For tests."""
    _reported.clear()


def report_once(key: object, message: str, /, **context: object) -> bool:
    """Report ``message`` unless ``key`` was already reported. True if it was sent.

    A reporter that raises must never take the caller's query down with it -- the whole
    point of ``'audit'`` mode is to observe without breaking -- so failures here are
    logged and swallowed.
    """
    if key in _reported:
        return False
    _reported.add(key)
    try:
        _reporter(message, **context)
    except Exception:  # noqa: BLE001 - audit must never break the query it observes
        logger.exception('tenancy audit reporter failed')
    return True
