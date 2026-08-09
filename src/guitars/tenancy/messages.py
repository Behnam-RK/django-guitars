"""The escape-hatch sentence appended to every tenant-scope violation message.

One shared formatter instead of four independently-typed copies (``guc.py``, and what are
now ``enforcement.py``/``querysets.py``) that would otherwise drift the moment one changed
wording and the others didn't -- which had already happened once: three sites read "wrap
it in ``tenant(...)``, or ``tenancy_bypassed()`` for a deliberate cross-tenant X", a
fourth read "Use ``tenancy_bypassed()`` if that is genuinely intended", and nothing forced
them to agree.
"""

from __future__ import annotations


__all__ = ['remediation']


def remediation(action: str, *, scope_is_active: bool = False) -> str:
    """The standard "how to fix this" sentence for a tenant-scope violation on *action*.

    Meant to follow an em dash (``-- {remediation(...)}``), not a period -- the default
    form starts lowercase.

    ``scope_is_active=True`` is the one case where suggesting ``tenant(...)`` would be
    wrong: a scope is already open and the write simply disagrees with it, so only
    ``tenancy_bypassed()`` is offered as the deliberate escape hatch. That form is
    capitalised, since it is meant to follow a period instead.
    """
    if scope_is_active:
        return f'Use tenancy_bypassed() if that {action} is genuinely intended.'
    return f'wrap it in tenant(...), or tenancy_bypassed() for a deliberate cross-tenant {action}.'
