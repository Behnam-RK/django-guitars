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


# Generates a migration into a checked-in app directory, so a reader on another worker would
# otherwise see the file mid-write. Honoured only because ``--dist loadgroup`` is in addopts.
# ``django_db`` throughout: a ``MigrationLoader`` reads the applied-migrations table first.
pytestmark = [
    pytest.mark.xdist_group(name='retire_edge_migration_files'),
    pytest.mark.django_db(transaction=True),
]

_OWNER = 'crossapp_retire_owner'
_CHILD = 'crossapp_retire_child'
#: The hand-written legacy migration: it carries the create whose app the drop's does not match.
_CREATE = (_CHILD, '0002_auto_enforcement')
_SCOPED = override_settings(LOCAL_APPS=[f'tests.{_OWNER}', f'tests.{_CHILD}'])


def _declared_dependencies(content: str) -> set[tuple[str, str]]:
    """What the file *says*, parsed out of its text rather than read off a loaded ``Migration``
    -- the point is what was written, which is what a consumer's next `migrate` reads."""
    block = re.search(r'dependencies = \[(.*?)\]', content, re.DOTALL)
    assert block is not None
    return set(re.findall(r"\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)", block.group(1)))


@pytest.fixture
def generated():
    """Run the generator over the pair and yield the retirement it wrote, removing it after.
    The two apps are installed but out of ``LOCAL_APPS``, so nothing else in the suite sees
    them -- they are scoped in here and nowhere else."""
    directory = Path(apps.get_app_config(_OWNER).path) / 'migrations'
    before = set(directory.glob('*.py'))
    written = None
    try:
        with _SCOPED:
            call_command('makeguitarmigrations', stdout=StringIO(), stderr=StringIO())
        new = set(directory.glob('*.py')) - before
        assert len(new) == 1, new
        written = new.pop()
        yield written
    finally:
        if written is not None and written.exists():
            written.unlink()


def test_the_retirement_lands_in_the_owner_app_while_the_create_sits_in_the_other(generated):
    """The split itself, on real files rather than a seeded dict. Without it the rest of this
    module would be asserting an edge nothing needs."""
    content = generated.read_text()

    assert 'Soft Delete Related Rule retired' in content
    assert 'crossapp_retire_owner_retiree' in content
    create = Path(apps.get_app_config(_CHILD).path) / 'migrations' / f'{_CREATE[1]}.py'
    assert 'Soft Delete Related Rule on' in create.read_text()


def test_the_written_retirement_declares_the_edge_to_its_create(generated):
    """The fix, as a consumer receives it. Before 2.10.0 this list named only the owner app's
    own previous migration, and a fresh `migrate` was free to reach the drop first."""
    assert _CREATE in _declared_dependencies(generated.read_text())


def test_a_fresh_migrate_plans_the_create_before_the_drop(generated):
    """What the edge buys, asked of the graph rather than of a database: the property is the
    ordering, and a plan is where a fresh ``migrate`` decides it."""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    plan = loader.graph.forwards_plan((_OWNER, generated.stem))

    assert plan.index(_CREATE) < plan.index((_OWNER, generated.stem))


def test_the_pair_applies_to_a_database(generated):
    """End to end. ``migrate`` walks its own plan, so this is the one assertion that would
    have raised `rule ... does not exist` rather than merely ordering wrongly."""
    try:
        with _SCOPED:
            call_command('migrate', _OWNER, stdout=StringIO())

        loader = MigrationLoader(connection, ignore_no_migrations=True)
        assert (_OWNER, generated.stem) in loader.applied_migrations
        assert _CREATE in loader.applied_migrations
    finally:
        with _SCOPED:
            call_command('migrate', _OWNER, 'zero', stdout=StringIO())
            call_command('migrate', _CHILD, 'zero', stdout=StringIO())
