"""Which migration first makes an object an emitted rule names exist. A rule action is parsed
by PostgreSQL when the rule is created, so every table and column it references must already be
there -- and across apps only an explicit dependency says so. See ADR 0013."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, cast

from django.db import connection
from django.db.backends.utils import truncate_name
from django.db.migrations.operations import (
    AddField,
    AlterField,
    AlterModelTable,
    CreateModel,
    DeleteModel,
    RenameField,
    RenameModel,
    SeparateDatabaseAndState,
)
from django.db.migrations.state import ProjectState


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from django.db.migrations.loader import MigrationLoader


__all__ = [
    'ObjectRef',
    'ReplayUnit',
    'TableEvent',
    'drop_implied_edges',
    'replay_plan',
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


class TableEvent(NamedTuple):
    """What a migration does to a table, in operation order (ADR 0043): *kind* ``'create'``,
    ``'rename'`` (to *new_table*, objects and all), ``'drop'`` or ``'retire'`` (a
    ``RetireEnforcement``, of one *column* where given)."""

    kind: str
    table: str
    new_table: str | None = None
    column: str | None = None


class ReplayUnit(NamedTuple):
    """One migration file in the order ``migrate`` runs it. *name* is the file whose headers are
    read; *graph_node* the node ``migrate`` knows, which differs for a file a squash replaced."""

    app_label: str
    name: str
    graph_node: tuple[str, str]
    events: tuple[TableEvent, ...]


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


def _units(
    loader: MigrationLoader, *, expand_squashes: bool
) -> Iterator[tuple[str, str, tuple[str, str], tuple]]:
    """``(app, file, graph node, operations)`` for every migration, in the order a fresh ``migrate``
    runs (Django sorts the leaves and parents, so it is deterministic). *expand_squashes* walks a
    squash as the replaced files on disk, then as itself with no operations: they ran already."""
    plan: dict[tuple[str, str], None] = {}
    for leaf in loader.graph.leaf_nodes():
        plan.update(dict.fromkeys(loader.graph.forwards_plan(leaf)))
    for node in plan:
        yield from _expand(loader, node, node, expand=expand_squashes)


def _expand(
    loader: MigrationLoader, key: tuple[str, str], node: tuple[str, str], *, expand: bool
) -> Iterator[tuple[str, str, tuple[str, str], tuple]]:
    migration = (
        loader.graph.nodes[key] if key in loader.graph.nodes else loader.disk_migrations[key]
    )
    replaced = list(getattr(migration, 'replaces', None) or ())
    if expand and replaced and all(each in loader.disk_migrations for each in replaced):
        for each in replaced:
            yield from _expand(loader, each, node, expand=True)
        yield (key[0], key[1], node, ())
        return
    yield (key[0], key[1], node, tuple(migration.operations))


def _walk(
    loader: MigrationLoader, *, expand_squashes: bool = False
) -> Iterator[tuple[str, str, tuple[str, str], object, ProjectState]]:
    """Every operation of :func:`_units` with the state **before** it, advanced once the caller has
    looked. A walk over the *graph*, never the files, since a pending squash leaves replaced files
    on disk the graph has dropped; per operation, so a rename before a delete is seen as it ran."""
    state = ProjectState(real_apps=loader.unmigrated_apps)
    for app_label, name, node, operations in _units(loader, expand_squashes=expand_squashes):
        for operation in operations:
            yield app_label, name, node, operation, state
            operation.state_forwards(app_label, state)


def dropped_tables(loader: MigrationLoader) -> dict[str, tuple[str, str]]:
    """Tables a ``DeleteModel`` dropped and nothing holds by the end of the history, in any app
    the loader knows, each with the migration that dropped it, for a retirement to follow.
    Positive evidence of a deletion, which an unmapped table alone is not."""
    dropped: dict[str, tuple[str, str]] = {}
    state = None
    for app_label, name, _node, operation, state in _walk(loader):
        # What the database does: a ``DeleteModel`` inside ``SeparateDatabaseAndState`` that is
        # state-only moves a model between apps and leaves its table where it is, while one in
        # its database half drops the table.
        for before, after in table_changes(operation, app_label, state):
            if before is not None and after is None:
                dropped[before] = (app_label, name)
    if state is None:
        return {}
    # A later model taking the same ``db_table`` holds it again. ``state`` is the walk's, last
    # advanced past its final operation when the generator finished.
    held = {
        _table_of(label, model_name, model_state)
        for (label, model_name), model_state in state.models.items()
    }
    return {table: node for table, node in dropped.items() if table not in held}


def vacated_tables(loader: MigrationLoader) -> dict[tuple[str, str], list[str]]:
    """``migration -> the tables it renames away or drops``, in any app the loader knows (#61).
    An enforcement migration of another app may still name one, and nothing orders it before the
    migration that moves it."""
    # Each operation's own table change: a rename and a retable alike, and the database half of
    # a ``SeparateDatabaseAndState`` the state half hides. A table a later model takes again is
    # still vacated: the older file needs it.
    vacated: dict[tuple[str, str], list[str]] = {}
    for app_label, name, _node, operation, state in _walk(loader):
        if old := vacating(operation, app_label, state):
            vacated.setdefault((app_label, name), []).extend(old)
    return vacated


def vacating(operation, app_label: str, state: ProjectState) -> list[str]:
    """The tables *operation* leaves behind when run on *state*, before it is applied to it."""
    return [
        before
        for before, after in table_changes(operation, app_label, state)
        if before is not None and before != after
    ]


def table_changes(
    operation, app_label: str, state: ProjectState
) -> list[tuple[str | None, str | None]]:
    """``(table before, table after)`` for what *operation* does to a table, read on *state*
    before it is applied: ``(None, t)`` a create, ``(t, None)`` a drop, ``(a, b)`` a rename or a
    retable. Nothing for a change that moves no table, a proxy or an unmanaged model."""
    if isinstance(operation, SeparateDatabaseAndState):
        # The database half only: the state half decides what Django believes, and for a model
        # moved between apps it deletes the model while the database half renames the table. A
        # state-only create or delete therefore reads as nothing, as it is.
        inner_operations = operation.database_operations
        if len(inner_operations) > 1:
            # Run one after another on a copy, as ``database_forwards`` runs them.
            state = state.clone()
        pairs: list[tuple[str | None, str | None]] = []
        for inner in inner_operations:
            pairs.extend(table_changes(inner, app_label, state))
            if len(inner_operations) > 1:
                inner.state_forwards(app_label, state)
        return pairs
    if isinstance(operation, CreateModel):
        if not _owns_options(operation.options):
            return []
        explicit = operation.options.get('db_table')
        return [(None, explicit or _default_table(app_label, operation.name_lower))]
    if isinstance(operation, RenameModel):
        name = operation.old_name_lower
    elif isinstance(operation, (DeleteModel, AlterModelTable)):
        name = operation.name_lower
    else:
        return []
    model_state = state.models.get((app_label, name))
    if model_state is None or not _owns_a_table(model_state):
        return []
    before = _table_of(app_label, name, model_state)
    # What the table is called afterwards; ``None`` for a drop. An explicit ``db_table`` survives
    # a ``RenameModel``, and a retable to the name it has moves nothing.
    after = None
    if isinstance(operation, RenameModel):
        after = _table_of(app_label, operation.new_name_lower, model_state)
    elif isinstance(operation, AlterModelTable):
        after = operation.table or _default_table(app_label, name)
    return [(before, after)] if before != after else []


def _retirements(operation) -> list[tuple[str, str | None]]:
    """The ``(table, column)`` of every ``RetireEnforcement`` in *operation*. Unwrapped for
    ``_establishes``' reason: this is the standard idiom for a change the database already has,
    and a hand-tuned squash carries it."""
    # Deferred: ``guitars.operations`` is a public module a consumer's migration imports, and
    # nothing in the generator should pay for it on a run that meets no retirement.
    from guitars.operations import RetireEnforcement  # noqa: PLC0415 - see the comment above

    if isinstance(operation, SeparateDatabaseAndState):
        return [
            retirement
            for inner in (*operation.database_operations, *operation.state_operations)
            for retirement in _retirements(inner)
        ]
    if isinstance(operation, RetireEnforcement):
        return [(operation.table, operation.column)]
    return []


def replay_plan(loader: MigrationLoader) -> list[ReplayUnit]:
    """Every migration in the order ``migrate`` runs it, each with the table events its operations
    make, in operation order (ADR 0043). The scan applies a file's events, then its headers: the
    order in which a database came to hold what the headers say it holds."""
    state = ProjectState(real_apps=loader.unmigrated_apps)
    units: list[ReplayUnit] = []
    for app_label, name, node, operations in _units(loader, expand_squashes=True):
        events: list[TableEvent] = []
        for operation in operations:
            for before, after in table_changes(operation, app_label, state):
                if before is None:
                    events.append(TableEvent('create', cast('str', after)))
                elif after is None:
                    events.append(TableEvent('drop', before))
                else:
                    events.append(TableEvent('rename', before, after))
            events.extend(
                TableEvent('retire', table, column=column)
                for table, column in _retirements(operation)
            )
            operation.state_forwards(app_label, state)
        units.append(ReplayUnit(app_label, name, node, tuple(events)))
    return units


def _owns_a_table(model_state) -> bool:
    """A proxy shares its concrete model's table, and Django drops no unmanaged table."""
    return _owns_options(model_state.options)


def _owns_options(options: dict) -> bool:
    return bool(options.get('managed', True)) and not options.get('proxy')


def _table_of(app_label: str, model_name: str, model_state) -> str:
    """The table ``Options`` gives a model: a default name is shortened past the backend's
    limit, or a long app label never matches the key the scan recorded."""
    explicit = model_state.options.get('db_table')
    if explicit:
        return explicit
    return _default_table(app_label, model_name)


def _default_table(app_label: str, model_name: str) -> str:
    """The name Django gives a model with no ``db_table``, shortened past the backend's limit."""
    return truncate_name(f'{app_label}_{model_name}', connection.ops.max_name_length())
