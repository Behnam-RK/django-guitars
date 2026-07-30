"""Guards on ``guitars.sql``'s public surface, which is a frozen interface.

Generated migrations are written into consuming projects, checked into their VCS,
and applied to their databases. Every one of them does ``from guitars import sql``
and reads constants off it by name. So renaming or removing a public name here does
not break *this* repo's tests -- it breaks ``migrate`` on a fresh database in every
project that ever ran the generator, at which point the only fix is hand-editing
historical migration files.

These tests exist so that failure is caught here instead. They are deliberately
about *names*, not SQL text: the text may be corrected (a bug fix lands in every
project on upgrade, which is the point of keeping it in the library), but a name
that ever shipped must keep resolving.
"""

import re
from pathlib import Path

import guitars
from guitars import sql


#: Every public name ``guitars.sql`` has ever exported. Append-only: add to this
#: set when a feature adds SQL, never remove from it. A removal here is the one
#: change that silently breaks already-applied migrations elsewhere.
FROZEN_SQL_NAMES = frozenset(
    {
        # _updated_at trigger function + per-table statement trigger
        'CHECK_TRIGGER_FUNCTION_EXISTS',
        'CREATE_UPDATED_AT_TRIGGER_FUNCTION',
        'DROP_UPDATED_AT_TRIGGER_FUNCTION',
        'CHECK_TRIGGER_EXISTS_ON_TABLE',
        'CREATE_UPDATED_AT_TRIGGER',
        'DROP_UPDATED_AT_TRIGGER',
        # soft deletion: the ON DELETE rule, the cascade rule, the session switch
        'SWITCH_ON_HARD_DELETION',
        'SWITCH_OFF_HARD_DELETION',
        'CHECK_RULE_EXISTS_ON_TABLE',
        'CREATE_SOFT_DELETE_RULE',
        'DROP_SOFT_DELETE_RULE',
        'CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE',
        'DROP_SOFT_DELETE_RELATED_OBJECTS_RULE',
        # multi-table inheritance: parent trigger function, trigger, redirect rule
        'CHECK_PARENT_TRIGGER_FUNCTION_EXISTS',
        'CREATE_PARENT_UPDATED_AT_TRIGGER_FUNCTION',
        'DROP_PARENT_UPDATED_AT_TRIGGER_FUNCTION',
        'CREATE_PARENT_UPDATED_AT_TRIGGER',
        'DROP_PARENT_UPDATED_AT_TRIGGER',
        'CREATE_MTI_SOFT_DELETE_RULE',
        'DROP_MTI_SOFT_DELETE_RULE',
    }
)

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
        f'These names are part of guitars.sql\'s frozen interface but no longer '
        f'resolve: {missing}. Already-applied migrations in consuming projects '
        f'reference them, so removing one breaks `migrate` on a fresh database '
        f'there. Re-export it, even if the implementation moved.'
    )


def test_all_is_exhaustive_and_matches_the_frozen_set():
    """``__all__`` must describe reality, in both directions.

    A name present but absent from ``__all__`` is invisible to ``import *`` and to
    tooling; a name in ``__all__`` that does not exist raises on ``import *``.
    """
    exported = set(sql.__all__)
    public = {name for name in dir(sql) if name.isupper() and not name.startswith('_')}

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


def test_every_public_name_is_a_usable_sql_string():
    """Migrations call ``.format(...)`` on these, so a non-string fails at migrate time."""
    for name in sorted(FROZEN_SQL_NAMES):
        value = getattr(sql, name)
        assert isinstance(value, str), f'sql.{name} is {type(value).__name__}, not str'
        assert value.strip(), f'sql.{name} is empty'


def test_generator_templates_reference_only_names_that_resolve():
    """Cross-check the command's operation templates against the module.

    The templates are strings, so a renamed constant is not a Python error here --
    it becomes a broken generated migration that only fails when someone runs
    ``migrate`` on a fresh database.
    """
    command = _PACKAGE_ROOT / 'management' / 'commands' / 'makeguitarmigrations.py'
    referenced = _referenced_names(command.read_text(encoding='utf-8'))

    assert referenced, 'found no sql.* references in the generator -- has the regex rotted?'
    unresolved = sorted(name for name in referenced if not hasattr(sql, name))
    assert not unresolved, (
        f'{command.name} emits migrations referencing sql names that do not exist: '
        f'{unresolved}'
    )


def test_checked_in_migrations_reference_only_names_that_resolve():
    """The same check against the migrations the harness has already generated.

    These stand in for the migration files living in consuming projects: if a name
    they reference stops resolving, their ``migrate`` breaks.
    """
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
