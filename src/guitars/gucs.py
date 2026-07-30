"""The names of the PostgreSQL session settings the tenant frame is published as.

A leaf module on purpose: **it imports nothing at all**, and it sits at the top level of
the package rather than inside ``tenancy/``. Both halves of that matter, because two very
different consumers need these strings.

* :mod:`guitars.tenancy.guc` publishes the values at runtime.
* :mod:`guitars.sql.policy` bakes the names into policy SQL at migration-generation time
  -- and ``guitars.sql`` is imported by every generated migration file.

Importing a submodule executes its package's ``__init__`` first, so had these names stayed
at ``guitars.tenancy.names``, every generated migration's ``from guitars import sql`` would
have pulled in the whole tenancy runtime -- connection handling, a signal receiver and a
ContextVar -- to read four strings. Being import-free is not enough on its own; the module
also has to live somewhere whose package is import-free.

The namespace mirrors the kit's existing ``rules.hard_deletion`` convention: the prefix
names the mechanism, the key names the knob.
"""

GUC_PREFIX = 'tenant.'
"""Namespace for every tenancy session setting."""

BYPASS_GUC = f'{GUC_PREFIX}bypass'
"""Set to ``'on'`` by ``tenancy_bypassed()``; ``'off'`` otherwise."""

VALUE_SEPARATOR = ','
"""Separator for a dimension's published value.

A dimension may hold several values -- ``tenant(shop=[a, b])`` filters with ``__in`` --
so values are *always* encoded as a separated list, even a single one. That way one
policy form (membership over ``string_to_array``) serves scalar and collection scopes
alike, and the policy never has to know which it got.
"""

__all__ = ['BYPASS_GUC', 'GUC_PREFIX', 'VALUE_SEPARATOR', 'guc_name']


def guc_name(dimension: str) -> str:
    """Session-setting name for a tenant dimension (``shop`` -> ``tenant.shop``)."""
    return f'{GUC_PREFIX}{dimension}'
