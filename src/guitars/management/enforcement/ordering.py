"""Ordering a fresh rename or drop after the enforcement migrations that name its table (#61)."""

# An enforcement migration's SQL names a table, and nothing ordered it before a *later* migration
# of another app renaming that table away or dropping it: a fresh ``migrate`` could run that
# first and fail with ``relation "<table>" does not exist``. See ADR 0036.

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from django.apps import apps as django_apps

from guitars.management import _generator
from guitars.management.enforcement.graph import drop_implied_edges, vacating
from guitars.management.enforcement.operations import OperationsMixin


if TYPE_CHECKING:
    from django.db.migrations import Migration
    from django.db.migrations.loader import MigrationLoader


__all__ = ['order_after_enforcement']


def order_after_enforcement(changes: dict[str, list[Migration]], loader: MigrationLoader) -> None:
    """Add to each migration in *changes* that vacates a table a dependency on every enforcement
    migration of another app naming it, before the file is written: one on disk is never touched."""
    state = loader.project_state()
    local = [app for app in django_apps.get_app_configs() if _generator.is_local(app)]
    for app_label, migrations in changes.items():
        for migration in migrations:
            # One already in the graph is a rewrite (``--update``): the state holds what it did,
            # so replaying it fails, and a file on disk is not this function's to edit.
            if (app_label, migration.name) in loader.graph.node_map:
                continue
            tables: list[str] = []
            for operation in migration.operations:
                tables.extend(vacating(operation, app_label, state))
                operation.state_forwards(app_label, state)
            edges = list(
                dict.fromkeys(
                    (app.label, path.stem)
                    for table in dict.fromkeys(tables)
                    for app in local
                    if app.label != app_label
                    for path, content in _generator.iter_migration_files(app)
                    if _generator.RE_DIGEST.search(content)
                    and OperationsMixin._names_table(content, table)
                    and (app.label, path.stem) in loader.graph.node_map
                )
            )
            # Not an edge the migration's own dependencies already reach: it says nothing new.
            behind = {
                node
                for dependency in migration.dependencies
                if dependency in loader.graph.node_map
                for node in loader.graph.forwards_plan(dependency)
            }
            for edge in drop_implied_edges(loader, edges):
                if edge not in behind:
                    # A list at runtime, which Django's stubs type as a read-only ``Sequence``.
                    cast('list', migration.dependencies).append(edge)
