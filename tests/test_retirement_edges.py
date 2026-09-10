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
#: Written by the generator itself while walking the MTI child, though the rule it creates fires
#: on the ancestor's table -- which is the whole split, and needs no proxy and no ``db_table``
#: shared between apps.
_CREATE = (_CHILD, '0002_auto_enforcement')
_RETIREMENT = (_OWNER, '0003_auto_enforcement')
#: The key relaxed, then restored, then relaxed again. One edge orders a drop after its create;
#: the cycle needs the mirror too, or a re-adopted create reaches a fresh database first and
#: leaves the rule dropped where an incremental one has it.
_READOPTION = (_CHILD, '0005_auto_enforcement')
_SECOND_RETIREMENT = (_OWNER, '0004_auto_enforcement')
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
    # The create names the ancestor's table too, from the other app's file.
    create = _migration(_CREATE).read_text()
    assert 'Soft Delete Related Rule on' in create
    assert 'crossapp_retire_owner_retiree' in create


def test_the_written_retirement_declares_the_edge_to_its_create():
    """The fix, as a consumer receives it. Before 2.10.0 this list named only the owner app's
    own previous migration, and a fresh `migrate` was free to reach the drop first."""
    assert _CREATE in _declared_dependencies(_migration(_RETIREMENT).read_text())


def test_the_readopted_create_declares_the_retirement_it_revives():
    """The mirror edge. A create ordered against nothing can reach a fresh database before the
    drop it revives, which then drops the rule the models call for -- so the fresh database
    ends without it while an incremental one has it."""
    assert _RETIREMENT in _declared_dependencies(_migration(_READOPTION).read_text())


def test_a_fresh_migrate_plans_the_whole_cycle_in_order():
    """What the two edges buy, asked of the graph rather than a database. Create, drop,
    re-create, drop: every one of the four ordered against the one before it."""
    plan = MigrationLoader(None, ignore_no_migrations=True).graph.forwards_plan(_SECOND_RETIREMENT)

    assert [plan.index(node) for node in (_CREATE, _RETIREMENT, _READOPTION)] == sorted(
        plan.index(node) for node in (_CREATE, _RETIREMENT, _READOPTION)
    )
    assert plan.index(_READOPTION) < plan.index(_SECOND_RETIREMENT)


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
def test_the_rule_is_gone_from_a_database_that_applied_the_pair():
    """End to end, off the suite's own ``migrate``. Asserting the *rule* rather than the two
    nodes: `migrate` sorts leaves by app name here, so `crossapp_retire_child` would precede
    the owner even unordered, and asserting both applied passes with the edge removed."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rulename FROM pg_rules WHERE tablename = 'crossapp_retire_owner_retiree'"
        )
        rules = {row[0] for row in cursor.fetchall()}

    # The cycle ends retired, so the cascade rule is gone -- which is also what an
    # incrementally-migrated database holds, the property ADR 0006 is about.
    assert 'soft_delete_related_crossapp_retire_child_dependant' not in rules
    assert 'soft_delete' in rules
