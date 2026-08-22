"""Which migration first makes an object an emitted rule names exist. A rule action is parsed
by PostgreSQL when the rule is created, so every table and column it references must already be
there -- and across apps only an explicit dependency says so. See ADR 0013."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from django.db.migrations.operations import (
    AddField,
    AlterField,
    AlterModelTable,
    CreateModel,
    RenameField,
    RenameModel,
    SeparateDatabaseAndState,
)


if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.db.migrations.loader import MigrationLoader


__all__ = [
    'ObjectRef',
    'resolve_dependencies',
    'resolve_object_migration',
    'would_close_a_cycle',
]


class ObjectRef(NamedTuple):
    """One object an emitted rule names, as the model layer knows it. *field* is ``None``
    where only the table has to exist -- a cascade rule's related table, or the MTI ancestor
    a joined arm takes liveness from, neither of which names a column of its own."""

    app_label: str
    model: str
    field: str | None = None

    def describe(self) -> str:
        """The ref as a warning names it -- ``app.Model.field``, or ``app.Model`` for a table."""
        stem = f'{self.app_label}.{self.model}'
        return stem if self.field is None else f'{stem}.{self.field}'


def _app_migrations_in_order(loader: MigrationLoader, app_label: str) -> list[str]:
    """*app_label*'s migrations in dependency order, earliest first. Read off the graph rather
    than sorted by name: the numeric prefix is a convention, and a squash or a hand-written
    migration can order two names against their spelling."""
    ordered: list[str] = []
    for leaf in sorted(loader.graph.leaf_nodes(app_label)):
        for node in loader.graph.forwards_plan(leaf):
            if node[0] == app_label and node[1] not in ordered:
                ordered.append(node[1])
    return ordered


def _establishes(operation, model: str, field: str | None) -> bool:
    """Whether *operation* makes ``model[.field]`` exist **under the name asked for**: a rename
    counts and the earlier creation then does not, the rule naming the current spelling. An
    ``AlterField`` counts only where the field declares a ``db_column``."""
    # Unwrapped, not skipped: this is the standard idiom for a column the database already has
    # (and what a hand-tuned squash carries), and reading past it resolves the ref to nothing --
    # a warning and no edge, which is the pre-2.5.0 failure with a log line in front of it.
    if isinstance(operation, SeparateDatabaseAndState):
        return any(
            _establishes(inner, model, field)
            for inner in (*operation.database_operations, *operation.state_operations)
        )
    model_lower = model.lower()
    if field is None:
        return (
            isinstance(operation, CreateModel | AlterModelTable)
            and operation.name.lower() == model_lower
        ) or (isinstance(operation, RenameModel) and operation.new_name.lower() == model_lower)

    if isinstance(operation, CreateModel) and operation.name.lower() == model_lower:
        return any(name.lower() == field.lower() for name, _ in operation.fields)
    if isinstance(operation, AddField):
        return (
            operation.model_name.lower() == model_lower and operation.name.lower() == field.lower()
        )
    if isinstance(operation, AlterField):
        # A ``db_column`` is the only *physical* change an ``AlterField`` makes, and this
        # resolver takes the **last** match, so counting every one drags the edge onto an
        # unrelated ``null=True``. Nothing holds the previous state, hence "declares", not "moved".
        return (
            operation.field.db_column is not None
            and operation.model_name.lower() == model_lower
            and operation.name.lower() == field.lower()
        )
    if isinstance(operation, RenameField):
        return (
            operation.model_name.lower() == model_lower
            and operation.new_name.lower() == field.lower()
        )
    # A renamed *model*, or one whose ``db_table`` moved, moves the table its column lives on,
    # so the column exists under this model's name only from there. Both, not just the rename:
    # a column is no more present on a table that does not exist yet -- see the branch above.
    return (isinstance(operation, RenameModel) and operation.new_name.lower() == model_lower) or (
        isinstance(operation, AlterModelTable) and operation.name.lower() == model_lower
    )


def resolve_object_migration(loader: MigrationLoader, ref: ObjectRef) -> tuple[str, str] | None:
    """The ``(app_label, migration_name)`` that last establishes *ref* under its current name,
    or ``None`` where nothing does -- an app with no migrations, or one whose history does not
    mention the object. A caller that cannot resolve a ref emits no edge, as before 2.5.0."""
    found: str | None = None
    for name in _app_migrations_in_order(loader, ref.app_label):
        migration = loader.disk_migrations.get((ref.app_label, name))
        if migration is None:
            continue
        # The *last* establishing operation, not the first: a rename supersedes the creation,
        # and depending on the creation alone would let the rule run against the old spelling.
        if any(
            _establishes(operation, ref.model, ref.field) for operation in migration.operations
        ):
            found = name
    return None if found is None else (ref.app_label, found)


def would_close_a_cycle(
    loader: MigrationLoader, migration: tuple[str, str], dependency: tuple[str, str]
) -> bool:
    """Whether depending on *dependency* would make the graph cyclic. Asserted rather than
    assumed: an edge to the migration that *creates* an object is older than the rule naming it
    and so should never close one, but a graph Django rejects bricks ``migrate`` outright."""
    # *dependency* reaching *migration* is what a cycle is here -- the new edge points the other
    # way. Read off the existing graph, so no node has to be added to ask.
    if dependency not in loader.graph.node_map or migration not in loader.graph.node_map:
        return False
    return migration in set(loader.graph.forwards_plan(dependency)) - {dependency}


def resolve_dependencies(
    loader: MigrationLoader,
    refs: Iterable[ObjectRef],
    *,
    own_app: str,
) -> tuple[list[tuple[str, str]], list[ObjectRef]]:
    """``(edges, unresolved)`` for *refs*. Refs into *own_app* are dropped: the scaffold already
    depends on that app's leaf, so its own history is ordered without an edge -- and an edge
    into the app being written would name the migration currently being created."""
    edges: list[tuple[str, str]] = []
    unresolved: list[ObjectRef] = []
    for ref in refs:
        if ref.app_label == own_app:
            continue
        resolved = resolve_object_migration(loader, ref)
        if resolved is None:
            unresolved.append(ref)
        elif resolved not in edges:
            edges.append(resolved)
    return edges, unresolved
