"""Every byte of raw SQL the kit emits, re-exported flat.

Migrations generated *before* 1.1.0 do ``from guitars import sql`` and reference names
off this module (``sql.CREATE_SOFT_DELETE_RULE.format(...)``); migrations generated from
1.1.0 on carry their SQL literally and reference nothing here. Those pre-1.1.0 migration
files are checked into consuming projects and already applied, so **this module's public
names are a frozen interface** -- split the implementation across submodules as
needed, but a name that ever appeared here must keep resolving here. The obligation is
therefore fixed in size rather than still growing; see ``docs/migrations.md``.

Organised by concern:

* :mod:`.triggers` -- the DB-managed ``_updated_at`` column.
* :mod:`.soft_delete` -- the ``ON DELETE`` rules, cascades, and the hard-deletion
  session switch.
* :mod:`.policy` -- row-level-security policies for tenant scoping. These are
  *functions*, not format-string constants, because the tenant predicate is composed from
  a variable-length ``{dimension: column}`` mapping.

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

from .policy import (
    EXEMPT_POLICY_PREFIX,
    TENANT_POLICY,
    create_exempt_policy,
    create_table_rls,
    create_tenant_policy,
    disable_rls,
    drop_all_exempt_policies,
    drop_exempt_policy,
    drop_table_rls,
    drop_tenant_policy,
    enable_rls,
    force_rls,
    no_force_rls,
    replace_table_rls,
)
from .soft_delete import (
    CHECK_RULE_EXISTS_ON_TABLE,
    CREATE_MTI_SOFT_DELETE_RULE,
    CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE,
    CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE_VIA,
    CREATE_SOFT_DELETE_RULE,
    DROP_MTI_SOFT_DELETE_RULE,
    DROP_SOFT_DELETE_RELATED_OBJECTS_RULE,
    DROP_SOFT_DELETE_RELATED_OBJECTS_RULE_VIA,
    DROP_SOFT_DELETE_RULE,
    SWITCH_OFF_HARD_DELETION,
    SWITCH_ON_HARD_DELETION,
)
from .triggers import (
    ADOPT_PARENT_UPDATED_AT_TRIGGER,
    ADOPT_UPDATED_AT_TRIGGER,
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
    REPLACE_PARENT_UPDATED_AT_TRIGGER,
    REPLACE_PARENT_UPDATED_AT_TRIGGER_FUNCTION,
    REPLACE_UPDATED_AT_TRIGGER,
    REPLACE_UPDATED_AT_TRIGGER_FUNCTION,
)


# One flat sorted list -- frozen names, so a reviewer diffing a release should see an
# insertion, never a reshuffle. tests/test_sql_interface.py compares the set.
__all__ = [
    'ADOPT_PARENT_UPDATED_AT_TRIGGER',
    'ADOPT_UPDATED_AT_TRIGGER',
    'CHECK_PARENT_TRIGGER_FUNCTION_EXISTS',
    'CHECK_RULE_EXISTS_ON_TABLE',
    'CHECK_TRIGGER_EXISTS_ON_TABLE',
    'CHECK_TRIGGER_FUNCTION_EXISTS',
    'CREATE_MTI_SOFT_DELETE_RULE',
    'CREATE_PARENT_UPDATED_AT_TRIGGER',
    'CREATE_PARENT_UPDATED_AT_TRIGGER_FUNCTION',
    'CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE',
    'CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE_VIA',
    'CREATE_SOFT_DELETE_RULE',
    'CREATE_UPDATED_AT_TRIGGER',
    'CREATE_UPDATED_AT_TRIGGER_FUNCTION',
    'DROP_MTI_SOFT_DELETE_RULE',
    'DROP_PARENT_UPDATED_AT_TRIGGER',
    'DROP_PARENT_UPDATED_AT_TRIGGER_FUNCTION',
    'DROP_SOFT_DELETE_RELATED_OBJECTS_RULE',
    'DROP_SOFT_DELETE_RELATED_OBJECTS_RULE_VIA',
    'DROP_SOFT_DELETE_RULE',
    'DROP_UPDATED_AT_TRIGGER',
    'DROP_UPDATED_AT_TRIGGER_FUNCTION',
    'EXEMPT_POLICY_PREFIX',
    'REPLACE_PARENT_UPDATED_AT_TRIGGER',
    'REPLACE_PARENT_UPDATED_AT_TRIGGER_FUNCTION',
    'REPLACE_UPDATED_AT_TRIGGER',
    'REPLACE_UPDATED_AT_TRIGGER_FUNCTION',
    'SWITCH_OFF_HARD_DELETION',
    'SWITCH_ON_HARD_DELETION',
    'TENANT_POLICY',
    'create_exempt_policy',
    'create_table_rls',
    'create_tenant_policy',
    'disable_rls',
    'drop_all_exempt_policies',
    'drop_exempt_policy',
    'drop_table_rls',
    'drop_tenant_policy',
    'enable_rls',
    'force_rls',
    'no_force_rls',
    'replace_table_rls',
]
