"""Every byte of raw SQL the kit emits, re-exported flat.

Generated migrations do ``from guitars import sql`` and reference names off this
module (``sql.CREATE_SOFT_DELETE_RULE.format(...)``). Those migration files are
checked into consuming projects and already applied, so **this module's public
names are a frozen interface** -- split the implementation across submodules as
needed, but a name that ever appeared here must keep resolving here.

Organised by concern:

* :mod:`.triggers` -- the DB-managed ``_updated_at`` column.
* :mod:`.soft_delete` -- the ``ON DELETE`` rules, cascades, and the hard-deletion
  session switch.

Multi-table inheritance (the ``*_MTI_*`` and ``*_PARENT_*`` names) spans both,
because it needs the same treatment for timestamps and for deletion. It rests on
one invariant, stated once here rather than in each submodule:

    In Django MTI a concrete child model gets its OWN table whose primary key is a
    ``OneToOneField(parent_link=True)`` referencing the parent's table; the metadata
    columns (``_updated_at`` / ``_deleted_at``) live ONLY on the ancestor that
    declares them. Because every table in an MTI chain shares the SAME primary-key
    value, a rule or trigger on any descendant table can address the owning ancestor
    row directly via ``owner_pk = old.<child_pk>``.
"""

from .soft_delete import (
    CHECK_RULE_EXISTS_ON_TABLE,
    CREATE_MTI_SOFT_DELETE_RULE,
    CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE,
    CREATE_SOFT_DELETE_RULE,
    DROP_MTI_SOFT_DELETE_RULE,
    DROP_SOFT_DELETE_RELATED_OBJECTS_RULE,
    DROP_SOFT_DELETE_RULE,
    SWITCH_OFF_HARD_DELETION,
    SWITCH_ON_HARD_DELETION,
)
from .triggers import (
    CHECK_PARENT_TRIGGER_FUNCTION_EXISTS,
    CHECK_TRIGGER_EXISTS_ON_TABLE,
    CHECK_TRIGGER_FUNCTION_EXISTS,
    CREATE_PARENT_UPDATED_AT_TRIGGER,
    CREATE_PARENT_UPDATED_AT_TRIGGER_FUNCTION,
    CREATE_UPDATED_AT_TRIGGER,
    CREATE_UPDATED_AT_TRIGGER_FUNCTION,
    DROP_PARENT_UPDATED_AT_TRIGGER,
    DROP_PARENT_UPDATED_AT_TRIGGER_FUNCTION,
    DROP_UPDATED_AT_TRIGGER,
    DROP_UPDATED_AT_TRIGGER_FUNCTION,
)


__all__ = [
    'CHECK_PARENT_TRIGGER_FUNCTION_EXISTS',
    'CHECK_RULE_EXISTS_ON_TABLE',
    'CHECK_TRIGGER_EXISTS_ON_TABLE',
    'CHECK_TRIGGER_FUNCTION_EXISTS',
    'CREATE_MTI_SOFT_DELETE_RULE',
    'CREATE_PARENT_UPDATED_AT_TRIGGER',
    'CREATE_PARENT_UPDATED_AT_TRIGGER_FUNCTION',
    'CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE',
    'CREATE_SOFT_DELETE_RULE',
    'CREATE_UPDATED_AT_TRIGGER',
    'CREATE_UPDATED_AT_TRIGGER_FUNCTION',
    'DROP_MTI_SOFT_DELETE_RULE',
    'DROP_PARENT_UPDATED_AT_TRIGGER',
    'DROP_PARENT_UPDATED_AT_TRIGGER_FUNCTION',
    'DROP_SOFT_DELETE_RELATED_OBJECTS_RULE',
    'DROP_SOFT_DELETE_RULE',
    'DROP_UPDATED_AT_TRIGGER',
    'DROP_UPDATED_AT_TRIGGER_FUNCTION',
    'SWITCH_OFF_HARD_DELETION',
    'SWITCH_ON_HARD_DELETION',
]
