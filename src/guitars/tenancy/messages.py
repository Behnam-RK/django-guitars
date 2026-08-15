"""The escape-hatch sentence appended to every tenant-scope violation message -- one
shared formatter instead of independently-typed copies across ``guc.py``,
``enforcement.py``, and ``querysets.py``, which had already drifted apart once."""

from __future__ import annotations


__all__ = ['remediation']


def remediation(action: str, *, scope_is_active: bool = False) -> str:
    """The standard "how to fix this" sentence for a violation on *action*. Meant to
    follow an em dash (default form starts lowercase). ``scope_is_active=True`` is the one
    case where suggesting ``tenant(...)`` would be wrong -- a scope is already open."""
    if scope_is_active:
        return f'Use tenancy_bypassed() if that {action} is genuinely intended.'
    return f'wrap it in tenant(...), or tenancy_bypassed() for a deliberate cross-tenant {action}.'
