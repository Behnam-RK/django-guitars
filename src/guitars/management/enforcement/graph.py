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
    'drop_implied_edges',
    'resolve_dependencies',
    'resolve_object_migration',
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


def drop_implied_edges(
    loader: MigrationLoader, edges: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """*edges* with every one already reachable from another removed. Two refs into one app
    routinely resolve to different migrations -- a table and a column added later -- and the
    older is then implied, so writing it says nothing the graph did not already say."""
    plans = {
        edge: set(loader.graph.forwards_plan(edge)) - {edge}
        for edge in edges
        if edge in loader.graph.node_map
    }
    # Maximal elements of a strict partial order, so the answer is well defined and non-empty:
    # reachability on an acyclic graph cannot have two edges imply each other. An edge the graph
    # does not have is kept -- nothing can be shown to imply it, and dropping it would lose it.
    return [edge for edge in edges if not any(edge in plan for plan in plans.values())]


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


def retired_enforcement(
    loader: MigrationLoader, app_label: str
) -> dict[str, list[tuple[str, str | None]]]:
    """``migration name -> [(table, column), ...]`` for every ``RetireEnforcement`` *app_label*
    has written. Read off the **loaded operations**: a regex over Python call syntax misses
    keyword and quoting variants, and gives no ordering against the headers the scan reads."""
    # Deferred: ``guitars.operations`` is a public module a consumer's migration imports, and
    # nothing in the generator should pay for it on a run that meets no retirement.
    from guitars.operations import RetireEnforcement  # noqa: PLC0415 - see the comment above

    def _retirements(operation) -> list[tuple[str, str | None]]:
        # Unwrapped for ``_establishes``' reason: this is the standard idiom for a change the
        # database already has, and a hand-tuned squash carries it.
        if isinstance(operation, SeparateDatabaseAndState):
            return [
                retirement
                for inner in (*operation.database_operations, *operation.state_operations)
                for retirement in _retirements(inner)
            ]
        if isinstance(operation, RetireEnforcement):
            return [(operation.table, operation.column)]
        return []

    found: dict[str, list[tuple[str, str | None]]] = {}
    for name in _app_migrations_in_order(loader, app_label):
        migration = loader.disk_migrations.get((app_label, name))
        if migration is None:
            continue
        retirements = [r for operation in migration.operations for r in _retirements(operation)]
        if retirements:
            found[name] = retirements
    return found


def renamed_tables(loader: MigrationLoader, app_label: str) -> dict[str, list[str]]:
    """``current db_table -> every name it held before, oldest first``, for the renames in
    *app_label*'s history. Empty, and cheap, for the apps that never renamed one."""
    ordered = _app_migrations_in_order(loader, app_label)
    interesting = [
        name
        for name in ordered
        if (app_label, name) in loader.disk_migrations
        and any(
            _renaming(operation)
            for operation in loader.disk_migrations[app_label, name].operations
        )
    ]
    if not interesting:
        return {}

    # Resolved through Django's own migration state rather than by re-deriving its naming
    # rules: an explicit ``db_table`` survives a ``RenameModel`` untouched, and an
    # ``AlterModelTable`` moves a table with no model rename at all.
    renames: dict[str, list[str]] = {}
    for name in interesting:
        before = _tables_by_model(loader, app_label, ordered, upto=name, inclusive=False)
        after = _tables_by_model(loader, app_label, ordered, upto=name, inclusive=True)
        for operation in loader.disk_migrations[app_label, name].operations:
            for old_model, new_model in _renaming(operation):
                # Read per *operation*, not by diffing the two states: a ``RenameModel``
                # changes the model name too, so the same table appears under two keys and a
                # diff sees one model gone and another arrived.
                old_table, new_table = before.get(old_model), after.get(new_model)
                if old_table and new_table and old_table != new_table:
                    # **Every** prior name, not just the first. A generation that ran between
                    # two renames left an object named after the intermediate table, and only
                    # dropping each leaves one object behind. See ``docs/migrations.md``.
                    renames[new_table] = [*renames.pop(old_table, []), old_table]
    return renames


def _renaming(operation) -> list[tuple[str, str]]:
    """``(old model name, new model name)`` for an operation that can move a table, lowercased
    as the migration state keys them. Unwrapped for :func:`_establishes`' reason."""
    if isinstance(operation, SeparateDatabaseAndState):
        return [
            pair
            for inner in (*operation.database_operations, *operation.state_operations)
            for pair in _renaming(inner)
        ]
    if isinstance(operation, RenameModel):
        return [(operation.old_name_lower, operation.new_name_lower)]
    if isinstance(operation, AlterModelTable):
        return [(operation.name_lower, operation.name_lower)]
    return []


def _tables_by_model(
    loader: MigrationLoader, app_label: str, ordered: list[str], *, upto: str, inclusive: bool
) -> dict[str, str]:
    """``model name -> db_table`` for *app_label* as of *upto*, read off the migration state."""
    index = ordered.index(upto) + (1 if inclusive else 0)
    state = loader.project_state([(app_label, ordered[index - 1])] if index else [])
    return {
        model_name: model_state.options.get('db_table') or f'{label}_{model_name}'
        for (label, model_name), model_state in state.models.items()
        if label == app_label
    }
