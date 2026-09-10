"""A cascade retirement is ordered against the migration that created the rule it drops
(2.10.0, issue #49). The create is hosted by the app that walked the owner model; the drop by
the app owning the table it fires on. Those differ, and only an explicit edge orders them."""

from __future__ import annotations

import re
from io import StringIO
from pathlib import Path

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import override_settings


# Nothing here writes a migration. Generating one into a checked-in app made this module race
# every other worker's ``MigrationLoader``, which imports each file it finds: one saw the
# transient file listed and then gone, and failed with ``ModuleNotFoundError``.
_OWNER = 'crossapp_retire_owner'
_CHILD = 'crossapp_retire_child'
#: The hand-written legacy migration: it carries the create whose app the drop's does not match.
_CREATE = (_CHILD, '0002_auto_enforcement')
_RETIREMENT = (_OWNER, '0002_auto_enforcement')
_SCOPED = override_settings(LOCAL_APPS=[f'tests.{_OWNER}', f'tests.{_CHILD}'])


def _migration(node: tuple[str, str]) -> Path:
    return Path(apps.get_app_config(node[0]).path) / 'migrations' / f'{node[1]}.py'


def _declared_dependencies(content: str) -> set[tuple[str, str]]:
    """What the file *says*, parsed out of its text rather than read off a loaded ``Migration``
    -- the point is what was written, which is what a consumer's next `migrate` reads."""
    block = re.search(r'dependencies = \[(.*?)\]', content, re.DOTALL)
    assert block is not None
    return set(re.findall(r"\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)", block.group(1)))


def test_the_retirement_lands_in_the_owner_app_while_the_create_sits_in_the_other():
    """The split itself, on real files rather than a seeded dict. Without it the rest of this
    module would be asserting an edge nothing needs."""
    retirement = _migration(_RETIREMENT).read_text()

    assert 'Soft Delete Related Rule retired' in retirement
    assert 'crossapp_retire_owner_retiree' in retirement
    assert 'Soft Delete Related Rule on' in _migration(_CREATE).read_text()


def test_the_written_retirement_declares_the_edge_to_its_create():
    """The fix, as a consumer receives it. Before 2.10.0 this list named only the owner app's
    own previous migration, and a fresh `migrate` was free to reach the drop first."""
    assert _CREATE in _declared_dependencies(_migration(_RETIREMENT).read_text())


def test_a_fresh_migrate_plans_the_create_before_the_drop():
    """What the edge buys, asked of the graph rather than of a database: the property is the
    ordering, and a plan is where a fresh ``migrate`` decides it."""
    plan = MigrationLoader(None, ignore_no_migrations=True).graph.forwards_plan(_RETIREMENT)

    assert plan.index(_CREATE) < plan.index(_RETIREMENT)


@pytest.mark.django_db(transaction=True)
def test_the_generator_would_write_that_file_again_unchanged():
    """The committed retirement is the emitter's own output, not a hand-made stand-in, so a
    change in how the edge is emitted fails here rather than passing against a stale artifact.
    Through ``--check``, the file being one the generator never rewrites once recorded."""
    out, err = StringIO(), StringIO()

    with _SCOPED:
        call_command('makeguitarmigrations', '--check', stdout=out, stderr=err)

    assert 'crossapp_retire' not in err.getvalue()


@pytest.mark.django_db(transaction=True)
def test_the_pair_applies_to_a_database():
    """End to end, off the suite's own ``migrate`` rather than a second one: it walks the same
    plan, so this is the assertion that would have raised `rule ... does not exist`."""
    applied = MigrationLoader(connection, ignore_no_migrations=True).applied_migrations

    assert _CREATE in applied
    assert _RETIREMENT in applied
