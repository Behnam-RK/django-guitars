"""PostgreSQL session-setting names for the tenant frame -- a leaf module, **imports
nothing**, at the top level rather than inside ``tenancy/``. Otherwise every migration's
``from guitars import sql`` would pull in the whole tenancy runtime to read four strings."""

GUC_PREFIX = 'tenant.'
"""Namespace for every tenancy session setting."""

BYPASS_GUC = f'{GUC_PREFIX}bypass'
"""Set to ``'on'`` by ``tenancy_bypassed()``; ``'off'`` otherwise."""

VALUE_SEPARATOR = ','
"""Separator for a dimension's published value. Always encoded as a list, even a single
value, so one policy form serves scalar and collection scopes alike."""

__all__ = ['BYPASS_GUC', 'GUC_PREFIX', 'VALUE_SEPARATOR', 'guc_name']


def guc_name(dimension: str) -> str:
    """Session-setting name for a tenant dimension (``shop`` -> ``tenant.shop``)."""
    return f'{GUC_PREFIX}{dimension}'
