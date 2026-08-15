"""Guards on ``guitars.sql``'s public surface, a frozen interface: pre-1.1.0 migrations
do ``from guitars import sql`` and read constants by name, so a rename breaks ``migrate``
in every such project. About *names*, not text -- text may be corrected."""

import re
from pathlib import Path

import guitars
from guitars import sql


#: Every public *constant* ``guitars.sql`` has ever exported. Append-only: a removal
#: silently breaks already-applied migrations. Exception: 2.0.0 removed 4 unused
#: ``CHECK_*`` constants, confirmed to have zero consumers anywhere.
FROZEN_SQL_CONSTANTS = frozenset(
    {
        # _updated_at trigger function + per-table statement trigger
        'CREATE_UPDATED_AT_TRIGGER_FUNCTION',
        'DROP_UPDATED_AT_TRIGGER_FUNCTION',
        'CREATE_UPDATED_AT_TRIGGER',
        'DROP_UPDATED_AT_TRIGGER',
        # Refresh/adopt forms -- inlined by generated migrations, but recorded anyway:
        # the rule here is that no exported name may be renamed unnoticed.
        'REPLACE_UPDATED_AT_TRIGGER_FUNCTION',
        'REPLACE_UPDATED_AT_TRIGGER',
        'ADOPT_UPDATED_AT_TRIGGER',
        # soft deletion: the ON DELETE rule, the cascade rule, the session switch
        'SWITCH_ON_HARD_DELETION',
        'SWITCH_OFF_HARD_DELETION',
        'CREATE_SOFT_DELETE_RULE',
        'DROP_SOFT_DELETE_RULE',
        'CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE',
        'DROP_SOFT_DELETE_RELATED_OBJECTS_RULE',
        'CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE_VIA',
        'DROP_SOFT_DELETE_RELATED_OBJECTS_RULE_VIA',
        # multi-table inheritance: parent trigger function, trigger, redirect rule
        'CREATE_PARENT_UPDATED_AT_TRIGGER_FUNCTION',
        'DROP_PARENT_UPDATED_AT_TRIGGER_FUNCTION',
        'CREATE_PARENT_UPDATED_AT_TRIGGER',
        'DROP_PARENT_UPDATED_AT_TRIGGER',
        'REPLACE_PARENT_UPDATED_AT_TRIGGER_FUNCTION',
        'REPLACE_PARENT_UPDATED_AT_TRIGGER',
        'ADOPT_PARENT_UPDATED_AT_TRIGGER',
        'CREATE_MTI_SOFT_DELETE_RULE',
        'DROP_MTI_SOFT_DELETE_RULE',
        # row-level security: policy names, not statements
        'TENANT_POLICY',
        'EXEMPT_POLICY_PREFIX',
    }
)

#: Every public *callable* ``guitars.sql`` has ever exported. Row-level-security SQL is
#: composed from a variable-length {dimension: column} mapping, so it cannot be a format
#: string. Same append-only rule, same reason: generated migrations call these by name.
FROZEN_SQL_CALLABLES = frozenset(
    {
        'create_tenant_policy',
        'drop_tenant_policy',
        'create_exempt_policy',
        'drop_exempt_policy',
        'drop_all_exempt_policies',
        'enable_rls',
        'disable_rls',
        'force_rls',
        'no_force_rls',
        'create_table_rls',
        'replace_table_rls',
        'drop_table_rls',
    }
)

FROZEN_SQL_NAMES = FROZEN_SQL_CONSTANTS | FROZEN_SQL_CALLABLES

#: Matches ``sql.SOME_NAME``. The ``\b`` matters: without it this also matches the
#: ``sql.`` inside ``reverse_sql=sql.X`` twice, and inside any ``*_sql`` kwarg.
_RE_SQL_REFERENCE = re.compile(r'\bsql\.([A-Z_]+)')

_PACKAGE_ROOT = Path(guitars.__file__).parent
_REPO_ROOT = _PACKAGE_ROOT.parent.parent


def _referenced_names(text: str) -> set[str]:
    return set(_RE_SQL_REFERENCE.findall(text))


def test_no_frozen_sql_name_has_been_dropped():
    """The whole point: a name that shipped must keep resolving."""
    missing = sorted(FROZEN_SQL_NAMES - set(dir(sql)))
    assert not missing, (
        f"These names are part of guitars.sql's frozen interface but no longer "
        f'resolve: {missing}. Already-applied migrations in consuming projects '
        f'reference them, so removing one breaks `migrate` on a fresh database '
        f'there. Re-export it, even if the implementation moved.'
    )


def test_all_is_exhaustive_and_matches_the_frozen_set():
    """``__all__`` must describe reality both ways: a name absent from it is invisible to
    ``import *``; a name in it that doesn't exist raises on ``import *``."""
    exported = set(sql.__all__)
    public = {
        name
        for name in dir(sql)
        if not name.startswith('_')
        # Submodules are reachable as attributes once imported but are not part of the
        # flat interface migrations use.
        and name not in {'policy', 'soft_delete', 'triggers'}
    }

    assert exported == public, (
        f'__all__ and the module contents disagree. '
        f'In __all__ only: {sorted(exported - public)}. '
        f'In the module only: {sorted(public - exported)}.'
    )
    # New SQL is allowed; forgetting to record it in the frozen set is not, because
    # the next refactor would then be free to rename it.
    assert exported == set(FROZEN_SQL_NAMES), (
        f'guitars.sql exports names that are not recorded as frozen: '
        f'{sorted(exported - set(FROZEN_SQL_NAMES))}. Add them to FROZEN_SQL_NAMES '
        f'so a later rename is caught.'
    )


def test_every_frozen_constant_is_a_usable_sql_string():
    """Migrations call ``.format(...)`` on these, so a non-string fails at migrate time."""
    for name in sorted(FROZEN_SQL_CONSTANTS):
        value = getattr(sql, name)
        assert isinstance(value, str), f'sql.{name} is {type(value).__name__}, not str'
        assert value.strip(), f'sql.{name} is empty'


def test_every_rule_is_created_or_replace():
    """A rule must be redefinable without an instant where the table has none -- with no
    ``soft_delete`` rule a ``DELETE`` destroys the row, so replacement is only safe because
    PostgreSQL swaps ``OR REPLACE`` in place instead of dropping and re-creating."""
    rule_constants = sorted(name for name in FROZEN_SQL_CONSTANTS if name.startswith('CREATE_'))
    creating_rules = [name for name in rule_constants if ' RULE ' in getattr(sql, name)]

    assert creating_rules, 'found no rule-creating constants -- has a name changed?'
    plain = [name for name in creating_rules if 'CREATE OR REPLACE RULE' not in getattr(sql, name)]
    assert not plain, (
        f'these create a rule without OR REPLACE, so replacing one is unsafe: {plain}'
    )


def test_every_frozen_callable_is_callable():
    """Migrations *call* these, so a constant shadowing one fails at migrate time."""
    for name in sorted(FROZEN_SQL_CALLABLES):
        assert callable(getattr(sql, name)), f'sql.{name} is not callable'


def test_the_constant_and_callable_sets_do_not_overlap():
    """A name cannot be both, and the two tests above would disagree about which it is."""
    assert not (FROZEN_SQL_CONSTANTS & FROZEN_SQL_CALLABLES)


def test_generator_templates_reference_only_names_that_resolve():
    """Cross-check the generator's templates: a renamed constant isn't a Python error
    here, only a broken migration at ``migrate`` time. Scans every module under
    ``guitars.management.enforcement``, since a ``sql.*`` reference can live in any."""
    enforcement_dir = _PACKAGE_ROOT / 'management' / 'enforcement'
    modules = sorted(enforcement_dir.glob('*.py'))
    assert modules, 'found no modules under guitars/management/enforcement/'

    referenced: set[str] = set()
    for path in modules:
        referenced |= _referenced_names(path.read_text(encoding='utf-8'))

    assert referenced, 'found no sql.* references in the generator -- has the regex rotted?'
    unresolved = sorted(name for name in referenced if not hasattr(sql, name))
    assert not unresolved, (
        f'guitars.management.enforcement emits migrations referencing sql names that do '
        f'not exist: {unresolved}'
    )


def test_checked_in_migrations_reference_only_names_that_resolve():
    """The same check against migrations the harness already generated -- these stand in
    for migration files in consuming projects, whose ``migrate`` breaks the same way."""
    migrations = sorted((_REPO_ROOT / 'tests' / 'testapp' / 'migrations').glob('0*.py'))
    assert migrations, 'no generated migrations found to check'

    referenced: set[str] = set()
    for path in migrations:
        referenced |= _referenced_names(path.read_text(encoding='utf-8'))

    assert referenced, 'no sql.* references across any migration -- has the regex rotted?'
    unresolved = sorted(name for name in referenced if not hasattr(sql, name))
    assert not unresolved, (
        f'Checked-in migrations reference sql names that no longer exist: {unresolved}'
    )
