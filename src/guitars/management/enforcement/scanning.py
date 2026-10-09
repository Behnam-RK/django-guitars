"""Scanning migration files for enforcement operations already written -- the read side of
the frozen headers in ``headers.py``. Every local app's migrations are scanned once, so a
partially covered app receives only what it's genuinely missing."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from django.apps import apps as django_apps

from guitars.management import _generator
from guitars.management.enforcement.graph import ReplayUnit, replay_plan
from guitars.management.enforcement.headers import (
    _RE_MTI_SOFT_DELETE,
    _RE_MTI_UPDATED_AT,
    _RE_PARENT_TRIGGER_FUNCTION,
    _RE_SOFT_DELETE,
    _RE_SOFT_DELETE_CASCADE_OWNER,
    _RE_SOFT_DELETE_CASCADE_OWNER_RETIRED,
    _RE_SOFT_DELETE_OWNED,
    _RE_SOFT_DELETE_OWNED_RETIRED,
    _RE_SOFT_DELETE_OWNED_SWEEP,
    _RE_SOFT_DELETE_OWNED_SWEEP_RETIRED,
    _RE_SOFT_DELETE_RELATED,
    _RE_SOFT_DELETE_RELATED_RETIRED,
    _RE_SOFT_DELETE_REVIVE,
    _RE_SOFT_DELETE_REVIVE_OWNER,
    _RE_SOFT_DELETE_REVIVE_OWNER_RETIRED,
    _RE_SOFT_DELETE_REVIVE_RETIRED,
    _RE_SOFT_DELETE_SELF_CASCADE,
    _RE_SOFT_DELETE_SELF_CASCADE_RETIRED,
    _RE_STAMP_FUNCTION,
    _RE_TENANT_AUTOFILL,
    _RE_TENANT_AUTOFILL_FUNCTION,
    _RE_TENANT_AUTOFILL_RETIRED,
    _RE_TENANT_FORCE,
    _RE_TENANT_POLICY,
    _RE_TRIGGER_FUNCTION,
    _RE_UPDATED_AT,
    RE_TENANT_AUTOFILL_FUNCTION,
    RE_TENANT_AUTOFILL_TABLE,
)
from guitars.management.enforcement.identity import (
    _recorded_policy_identity,
    _recorded_sql_identity,
    unforced_policy_tables,
)
from guitars.sql import _identifiers


if TYPE_CHECKING:
    from collections.abc import Callable

    from django.db.migrations.loader import MigrationLoader


class CascadeRetirementSite(NamedTuple):
    """A cascade retirement already on disk, and the create it drops. A file already written is
    never rewritten -- the digest guard skips it -- so this is the only channel by which a
    history whose two halves nothing orders can be told. See ADR 0021."""

    app_label: str
    migration: str
    key: tuple[str, str, str | None]
    #: The create this drop dropped, filled by :func:`_settle_retirement_sites` once the whole
    #: walk is in. ``None`` where no create is recorded, or where several are and the graph
    #: orders none of them before this drop.
    created: tuple[str, str] | None


class ExistingOperations(NamedTuple):
    """Which enforcement operations the migration files already contain, scanned once. The
    first five map key -> ``[SQL:...]`` digest, not a set: conflating "covered" with
    "covered by today's SQL" is how the 1.0.0 guard rewrite once shipped as a no-op."""

    triggers: dict[str, str | None]
    soft_deletes: dict[str, str | None]
    #: Keyed on (related_table, table, foreign_key) -- the third element is ``None`` for the
    #: one FK per pair keeping the plain historical header, or the column for any other.
    soft_delete_related: dict[tuple[str, str, str | None], str | None]
    #: That same key -> every ``(app_label, migration)`` whose header created the rule, oldest
    #: first. A list because a retired key can be created again, and which create a given drop
    #: dropped is what orders it -- a rule being a ``RunSQL``, nothing resolves it. See ADR 0021.
    soft_delete_related_dependencies: dict[tuple[str, str, str | None], list[tuple[str, str]]]
    #: Every cascade retirement already written, with the create it drops. The ``--check``
    #: half of ADR 0021: the emitter cannot reach a file it will never rewrite.
    cascade_retirement_sites: list[CascadeRetirementSite]
    #: The inverse rule for that same cascade key, tracked separately for the sweep's reason:
    #: a cascade already recorded must not read as a revive recorded, or a project upgrading
    #: to 2.11.0 never receives one. Same key shape, so retirement and renames see one key.
    soft_delete_revive: dict[tuple[str, str, str | None], str | None]
    #: Its creates, and its retirements already written -- the two halves ADR 0021 needs, per
    #: family, because a revive's drop is ordered against the migration that created *it*.
    soft_delete_revive_dependencies: dict[tuple[str, str, str | None], list[tuple[str, str]]]
    revive_retirement_sites: list[CascadeRetirementSite]
    #: Keyed on ``(owner_table,)``: since 2.16.0 one revive trigger per owner carries every
    #: key's arm (#70). A 1-tuple so #66's tuple machinery -- re-keying, settling, the
    #: trigger family's table index -- takes it unchanged. Its creates and retirements too.
    soft_delete_revive_owner: dict[tuple[str], str | None]
    soft_delete_revive_owner_dependencies: dict[tuple[str], list[tuple[str, str]]]
    revive_owner_retirement_sites: list[CascadeRetirementSite]
    #: The same trigger once it archives too (2.19.0, #80, ADR 0039): ``soft_delete_revive_owner``
    #: above is the family it superseded, kept to read history and to be retired.
    soft_delete_cascade_owner: dict[tuple[str], str | None]
    soft_delete_cascade_owner_dependencies: dict[tuple[str], list[tuple[str, str]]]
    cascade_owner_retirement_sites: list[CascadeRetirementSite]
    #: Keyed on (dependent_table, table, foreign_key) -- the owner-side mirror of the above.
    #: The FK is never ``None`` here: an owned rule is always named after its column, there
    #: being no pre-2.3.0 plain form to stay compatible with.
    soft_delete_owned: dict[tuple[str, str, str], str | None]
    #: The statement-level sweep for that same triple, tracked separately because it is its
    #: own operation with its own ``[SQL:...]``: a rule already recorded must not read as a
    #: sweep already recorded, or upgrading projects never receive one. See ADR 0014.
    soft_delete_owned_sweep: dict[tuple[str, str, str], str | None]
    #: Keyed on (table, foreign_key) -- one table, not two, a self-referential CASCADE FK
    #: firing on the table it points at. Its own dict for the sweep's reason: a cascade *rule*
    #: already recorded must not read as a trigger recorded. See ADR 0018.
    soft_delete_self_cascade: dict[tuple[str, str], str | None]
    #: The migrations that created each of the three above, for a retirement to depend on: a
    #: self-cascade trigger could be written by an MTI descendant's app (#66, before 2.20.0).
    soft_delete_owned_dependencies: dict[tuple[str, str, str], list[tuple[str, str]]]
    soft_delete_owned_sweep_dependencies: dict[tuple[str, str, str], list[tuple[str, str]]]
    soft_delete_self_cascade_dependencies: dict[tuple[str, str], list[tuple[str, str]]]
    #: Their settled retirements, for a re-adopted create to depend on (ADR 0021).
    owned_retirement_sites: list[CascadeRetirementSite]
    owned_sweep_retirement_sites: list[CascadeRetirementSite]
    mti_triggers: dict[str, str | None]
    mti_soft_deletes: dict[str, str | None]
    #: ``(app_label, migration, kind, table)`` for an MTI header a single migration carries
    #: **twice** -- a proxy's copy of its concrete child's, one table taking one such
    #: operation. Invisible to a set difference, the child requiring that same key.
    duplicate_mti_operations: list[tuple[str, str, str, str]]
    tenant_policies: set[str]
    #: Table -> the ``[POLICY:...]`` identity its **most recent** policy operation carries.
    #: Separate from :attr:`tenant_policy_sql`: identity is what the policy *says* (``force``
    #: excluded, so a settings flip alone can't trigger a replacement); SQL is whether the text is current.
    tenant_policy_identities: dict[str, str]
    #: Table -> the ``[SQL:...]`` digest of its most recent policy operation, or ``None``.
    tenant_policy_sql: dict[str, str | None]
    #: Tables whose policy operation was written with ``force=False`` -- see
    #: :func:`unforced_policy_tables`. These are the only ones a second FORCE stage can act on.
    unforced_policies: set[str]
    tenant_forces: set[str]
    #: ``(table, function)`` -> the ``[SQL:...]`` digest of its most recent tenant-autofill
    #: trigger operation, and the one field this scan *subtracts* from: a retired key must
    #: read as absent, not recorded. The pair because one table can carry several triggers.
    tenant_autofill: dict[tuple[str, str], str | None]
    #: App labels whose history contains a retirement header, of **either** retiring family.
    #: Retirement breaks the file-level ``[DIGEST:...]`` guard's assumption that an operation set
    #: never recurs -- retire, then re-adopt -- so these apps rely on the per-operation guards.
    retirement_apps: set[str]
    #: ``current db_table -> every name it held before, oldest first``. The families whose
    #: object name embeds a table have to drop each of them: a generation between two renames
    #: left an object under the intermediate name.
    renamed_tables: dict[str, list[str]]
    #: Function name -> the migration defining it, and that migration's ``[SQL:...]`` digest.
    #: Dicts rather than the singletons below because autofill is one function per
    #: ``(column, GUC)`` pair -- normally one, but a hand-rolled manager can add more.
    tenant_autofill_function_dependencies: dict[str, tuple[str, str]]
    tenant_autofill_function_sql: dict[str, str | None]
    #: App label -> every ``[DIGEST:...]`` already stamped on its migration files. Harvested
    #: in the same pass as everything above, so "already written" is a dict lookup, not a
    #: fresh directory re-scan.
    existing_digests: dict[str, set[str]]
    trigger_function_dependency: tuple[str, str] | None
    parent_trigger_function_dependency: tuple[str, str] | None
    #: The migration defining ``stamp_updated_at()``, which every own-table ``updated_at_trigger``
    #: calls since 2.19.0 (ADR 0038). Apart from ``trigger_function_dependency``: the frozen
    #: ``set_updated_at()`` migration that history carries and nothing calls.
    stamp_function_dependency: tuple[str, str] | None
    #: The ``[SQL:...]`` digest of the most recent migration defining each singleton
    #: trigger function. Singletons by *existence*, which is why a body change once shipped
    #: nothing: both ensure methods returned early on mere presence.
    trigger_function_sql: str | None
    parent_trigger_function_sql: str | None
    stamp_function_sql: str | None


def _cascade_key(match: re.Match) -> tuple[str, str, str | None]:
    """``(related_table, owner_table, foreign_key)`` off a cascade header. Shared by the create
    scan, its provenance and the retirement pop, which all key the same rule: three spellings of
    one triple is how one of them drifts. ``None`` is the plain historical form's column."""
    return (
        _identifiers._unescape_ident(match.group(1)),
        _identifiers._unescape_ident(match.group(2)),
        _identifiers._unescape_ident(match.group('foreign_key'))
        if match.group('foreign_key') is not None
        else None,
    )


def _unescaped_groups(match: re.Match) -> tuple:
    """A header's key, every group unescaped: the three #66 families' scanners capture exactly
    their key, in order."""
    return tuple(_identifiers._unescape_ident(group) for group in match.groups())


def _respell(key, forward: dict[str, str]):
    """*key* under the name its table now has, where a rename moved it on and nothing has taken
    the old name since (*forward*, kept by the replay). A cascade or owned key names two tables,
    a self-cascade or autofill key one and then a column or function, a per-owner trigger one."""
    if isinstance(key, str):
        return forward.get(key, key)
    if len(key) == 1:
        return (forward.get(key[0], key[0]),)
    if len(key) == 2:
        return (forward.get(key[0], key[0]), key[1])
    related, owner, via = key
    return (forward.get(related, related), forward.get(owner, owner), via)


Aliases = dict[tuple[str, str], tuple[tuple[str, str], int]]


def _settle_retirement_sites(
    sites: list[CascadeRetirementSite],
    recorded: dict[Any, str | None],
    provenance: dict[Any, list[tuple[str, str]]],
    graph,
    aliases: Aliases | None = None,
) -> list[CascadeRetirementSite]:
    """Match each retirement to the create it dropped and pop the key where the drop wins. The
    replay never pops: a line through nodes the graph leaves unordered must not read "unordered"
    as "later", so both questions are settled here, once, by the graph. See ADR 0021, 0043."""
    by_key: dict[Any, list[CascadeRetirementSite]] = {}
    for site in sites:
        by_key.setdefault(site.key, []).append(site)
    settled = []
    for key, drops in by_key.items():
        # In the order they were recorded, which is the replay's.
        drop_nodes = {(site.app_label, site.migration) for site in drops}
        # A migration cannot create what it also retires: a create sharing a file with a drop
        # for the same key is a hand-edited, self-contradicting migration, and is not evidence
        # of anything -- excluded rather than paired with itself.
        creates = [c for c in provenance.get(key, []) if c not in drop_nodes]
        for rank, site in enumerate(drops):
            node = (site.app_label, site.migration)
            settled.append(
                site._replace(
                    key=key,
                    created=_create_this_drop_dropped(node, creates, graph, rank, aliases),
                )
            )
        if _retirement_is_the_last_word(drops, creates, graph, aliases):
            recorded.pop(key, None)
    return settled


def _retirement_is_the_last_word(
    drops: list[CascadeRetirementSite],
    creates: list[tuple[str, str]],
    graph,
    aliases: Aliases | None = None,
) -> bool:
    """Whether the newest drop of a key comes after its newest create. Per key, not per site:
    an older drop is ordered before the create that revived the rule, and settling on that one
    reads a live rule as retired, so the next retirement is never emitted."""
    if not creates:
        return False
    node = (drops[-1].app_label, drops[-1].migration)
    if _orders(creates[-1], node, graph, aliases):
        return True
    # A create the graph puts *after* this drop is a re-adoption, and the rule is live again.
    if any(_orders(node, create, graph, aliases) for create in creates):
        return False
    # Nothing orders them either way, which is the pre-2.10.0 history this release exists for.
    # Counting is the only signal left, and the two alternate: relax, restore, relax.
    return len(drops) >= len(creates)


def _orders(
    earlier: tuple[str, str], later: tuple[str, str], graph, aliases: Aliases | None = None
) -> bool:
    """Whether the graph puts *earlier* before *later*. Unordered is not "earlier": reading a
    re-adopted create as ordered would pop a live rule's coverage. A file a squash replaced is
    its squash, and two of one squash's files are ordered by the squash's ``replaces``."""
    aliases = aliases or {}
    first, first_rank = aliases.get(earlier, (earlier, 0))
    second, second_rank = aliases.get(later, (later, 0))
    if first == second and (earlier in aliases or later in aliases):
        return first_rank < second_rank
    if first not in graph.node_map or second not in graph.node_map:
        return False
    return first in set(graph.forwards_plan(second))


def _create_this_drop_dropped(
    node: tuple[str, str],
    creates: list[tuple[str, str]],
    graph,
    rank: int,
    aliases: Aliases | None = None,
) -> tuple[str, str] | None:
    """The newest create *node*'s drop can have dropped -- the last one the graph puts before
    it. Not simply the newest: after a re-adoption that one is the create the drop *precedes*,
    and naming it would order the drop after the rule it revives, for good."""
    ordered = [create for create in creates if _orders(create, node, graph, aliases)]
    if ordered:
        return ordered[-1]
    # Nothing orders them at all -- the pre-2.10.0 history this release exists for. Paired by
    # rank, the two alternating: the *n*th drop dropped the *n*th create. A guess, and named as
    # one in the note, but the alternative is silence on the histories most in need of it.
    return creates[rank] if rank < len(creates) else None


def _subtract_retired(
    table: str,
    column: str | None,
    keyed: dict[str, dict],
    whole_table: dict[str, dict],
    triggers: dict[str, tuple[dict, int]] | None = None,
) -> None:
    """Forget what a ``RetireEnforcement`` dropped, so a later run re-emits what the models
    still call for. *keyed* spell a table and column, *whole_table* a table, *triggers* a family
    with the index of the table it fires on; only a whole-table form reaches the last two."""
    # Exactly what the operation drops, never more: a trigger only on the table it names, whole.
    # Forgetting one it left live read it as gone, and nothing retired it again (#66).
    if column is None:
        for recorded, fires_on in (triggers or {}).values():
            for key in [k for k in recorded if k[fires_on] == table]:
                del recorded[key]
    for recorded in keyed.values():
        # ``k[-1] is None`` is the cascade family's *primary* form, whose key drops the column
        # as the historical rule name does, so a column retirement takes it unseen: over-
        # subtracting costs a re-emitted CREATE OR REPLACE, under-subtracting hides a drop.
        matches = [
            k
            for k in recorded
            if k[0] == table and (column is None or k[-1] == column or k[-1] is None)
        ]
        for key in matches:
            del recorded[key]
    if column is not None:
        return
    for recorded in whole_table.values():
        recorded.pop(table, None)


def _move_renamed(
    old: str, new: str, recorded: dict | set, position: dict[tuple[str, str], int] | None = None
) -> None:
    """Move *recorded*'s entries from table *old* onto *new*, in place, at the migration that
    renames it. Anything already filed under *new* predates the rename, so a scalar is
    overwritten and a list of creates merged, ordered by *position* in the replay (ADR 0043)."""
    if isinstance(recorded, set):
        if old in recorded:
            recorded.discard(old)
            recorded.add(new)
        return
    for key in list(recorded):
        # ``isinstance`` first: a string is iterable, so the tuple branch would shred a
        # table-keyed entry into a tuple of characters.
        # Table positions only: a column or function segment named like the table is not it.
        moved = _respell(key, {old: new})
        if moved == key:
            continue
        value = recorded.pop(key)
        if isinstance(value, list) and isinstance(recorded.get(moved), list):
            kept = recorded[moved]
            kept.extend(node for node in value if node not in kept)
            if position is not None:
                kept.sort(key=lambda node: position.get(node, len(position)))
        else:
            recorded[moved] = value


class _FileRecord:
    """One migration file's claim on a ``[DIGEST:...]``: it vouches for its operation set only
    while the tables it named still carry what it wrote (ADR 0043)."""

    __slots__ = ('app_label', 'digest', 'tables', 'void')

    def __init__(self, app_label: str, digest: str) -> None:
        self.app_label = app_label
        self.digest = digest
        self.tables: set[str] = set()
        self.void = False


def _tables_named(key) -> tuple[str, ...]:
    """The tables a recorded key names: the whole of a string, the first of a one- or two-part
    tuple, the first two of a cascade or owned key."""
    if isinstance(key, str):
        return (key,)
    return tuple(key[:2]) if len(key) == 3 else (key[0],)


def _replay_units(
    loader: MigrationLoader, files: dict[tuple[str, str], str]
) -> tuple[list[ReplayUnit], Aliases]:
    """The migrations in the order ``migrate`` runs them, then any file on disk the graph does
    not know, which has no events. A file a squash replaced that survives beside a squash that
    was not expanded is read just before that squash: its headers are older than anything after."""
    units = replay_plan(loader)
    placed = {(unit.app_label, unit.name) for unit in units}
    replaced_by: dict[tuple[str, str], tuple[str, str]] = {}
    for node, migration in loader.graph.nodes.items():
        for each in getattr(migration, 'replaces', None) or ():
            replaced_by[each] = node
    before_squash: dict[tuple[str, str], list[ReplayUnit]] = {}
    stray: list[ReplayUnit] = []
    for key in files:
        if key in placed:
            continue
        if key in replaced_by:
            unit = ReplayUnit(key[0], key[1], replaced_by[key], ())
            before_squash.setdefault(replaced_by[key], []).append(unit)
        else:
            stray.append(ReplayUnit(key[0], key[1], key, ()))
    ordered: list[ReplayUnit] = []
    for unit in units:
        ordered.extend(before_squash.pop((unit.app_label, unit.name), ()))
        ordered.append(unit)
    # Every graph node is in some leaf's plan, so a squash unit always came by above and nothing
    # is left in ``before_squash``: the stray files are the whole remainder.
    ordered.extend(stray)
    # A squash is its replaced files' graph node: ordered against another file by the node, and
    # against its own replaced files by their place in the replay.
    aliases: Aliases = {}
    by_node: dict[tuple[str, str], list[ReplayUnit]] = defaultdict(list)
    for unit in ordered:
        by_node[unit.graph_node].append(unit)
    for node, members in by_node.items():
        if len(members) > 1:
            for rank, unit in enumerate(members):
                aliases[unit.app_label, unit.name] = (node, rank)
    return ordered, aliases


def scan_existing_operations(loader: MigrationLoader | None = None) -> ExistingOperations:
    """Scan every local app's migration files for enforcement operations already written, by
    comment header. Replayed in ``migrate``'s order, each file's table events before its headers,
    so a later file wins by being later (ADR 0043)."""
    # *loader* is the caller's cached one. Events are read off loaded operations, so one is
    # built here when none is given, and at most once for the whole scan.

    # Table (or table pair) -> the [SQL:...] digest of its most recent operation.
    # Last write wins throughout: the replay is in graph order, which is time.
    existing_triggers: dict[str, str | None] = {}
    existing_soft_deletes: dict[str, str | None] = {}
    existing_soft_delete_related: dict[tuple[str, str, str | None], str | None] = {}
    cascade_deps: dict[tuple[str, str, str | None], list[tuple[str, str]]] = {}
    retirement_sites: list[CascadeRetirementSite] = []
    existing_soft_delete_revive: dict[tuple[str, str, str | None], str | None] = {}
    revive_deps: dict[tuple[str, str, str | None], list[tuple[str, str]]] = {}
    revive_retirement_sites: list[CascadeRetirementSite] = []
    existing_soft_delete_owned: dict[tuple[str, str, str], str | None] = {}
    existing_soft_delete_owned_sweep: dict[tuple[str, str, str], str | None] = {}
    existing_soft_delete_self_cascade: dict[tuple[str, str], str | None] = {}
    existing_soft_delete_revive_owner: dict[tuple[str], str | None] = {}
    existing_soft_delete_cascade_owner: dict[tuple[str], str | None] = {}
    owned_deps: dict = {}
    revive_owner_deps: dict = {}
    cascade_owner_deps: dict = {}
    sweep_deps: dict = {}
    self_deps: dict = {}
    trigger_retirement_sites: dict[int, list[CascadeRetirementSite]] = {}
    existing_mti_triggers: dict[str, str | None] = {}
    existing_mti_soft_deletes: dict[str, str | None] = {}
    duplicate_mti: list[tuple[str, str, str, str]] = []
    existing_tenant_autofill: dict[tuple[str, str], str | None] = {}
    retirement_apps: set[str] = set()
    # (regex, dict, key_fn) for every plain "finditer, record by key" scan -- the
    # singleton-function and tenant-policy/force blocks below don't fit this shape.
    # Every group is _unescape_ident'd, undoing operations.py's doubled '"'.
    scan_table: list[tuple[re.Pattern, dict, Callable[[re.Match], object]]] = [
        (_RE_UPDATED_AT, existing_triggers, lambda m: _identifiers._unescape_ident(m.group(1))),
        (
            _RE_SOFT_DELETE,
            existing_soft_deletes,
            lambda m: _identifiers._unescape_ident(m.group(1)),
        ),
        (_RE_SOFT_DELETE_RELATED, existing_soft_delete_related, _cascade_key),
        (_RE_SOFT_DELETE_REVIVE, existing_soft_delete_revive, _cascade_key),
        (
            _RE_SOFT_DELETE_OWNED,
            existing_soft_delete_owned,
            lambda m: (
                _identifiers._unescape_ident(m.group(1)),
                _identifiers._unescape_ident(m.group(2)),
                _identifiers._unescape_ident(m.group(3)),
            ),
        ),
        (
            _RE_SOFT_DELETE_OWNED_SWEEP,
            existing_soft_delete_owned_sweep,
            lambda m: (
                _identifiers._unescape_ident(m.group(1)),
                _identifiers._unescape_ident(m.group(2)),
                _identifiers._unescape_ident(m.group(3)),
            ),
        ),
        (
            _RE_SOFT_DELETE_REVIVE_OWNER,
            existing_soft_delete_revive_owner,
            lambda m: (_identifiers._unescape_ident(m.group(1)),),
        ),
        (
            _RE_SOFT_DELETE_CASCADE_OWNER,
            existing_soft_delete_cascade_owner,
            lambda m: (_identifiers._unescape_ident(m.group(1)),),
        ),
        (
            _RE_SOFT_DELETE_SELF_CASCADE,
            existing_soft_delete_self_cascade,
            lambda m: (
                _identifiers._unescape_ident(m.group(1)),
                _identifiers._unescape_ident(m.group(2)),
            ),
        ),
        (
            _RE_MTI_UPDATED_AT,
            existing_mti_triggers,
            lambda m: _identifiers._unescape_ident(m.group(1)),
        ),
        (
            _RE_MTI_SOFT_DELETE,
            existing_mti_soft_deletes,
            lambda m: _identifiers._unescape_ident(m.group(1)),
        ),
    ]

    def _autofill_key(match: re.Match) -> tuple[str, str]:
        return (
            _identifiers._unescape_ident(match.group(RE_TENANT_AUTOFILL_TABLE)),
            _identifiers._unescape_ident(match.group(RE_TENANT_AUTOFILL_FUNCTION)),
        )

    existing_tenant_policies: set[str] = set()
    existing_policy_identities: dict[str, str] = {}
    existing_policy_sql: dict[str, str | None] = {}
    #: Table -> whether its *most recent* policy operation was written ``force=False``.
    #: A mapping rather than a set so a later operation can take a table back off the
    #: FORCE backlog; see where it is filled.
    existing_policy_force: dict[str, bool] = {}
    existing_tenant_forces: set[str] = set()
    file_records: list[_FileRecord] = []
    trigger_function_dep: tuple[str, str] | None = None
    parent_trigger_function_dep: tuple[str, str] | None = None
    stamp_function_dep: tuple[str, str] | None = None
    trigger_function_sql: str | None = None
    parent_trigger_function_sql: str | None = None
    stamp_function_sql: str | None = None
    autofill_function_deps: dict[str, tuple[str, str]] = {}
    autofill_function_sql: dict[str, str | None] = {}
    built_loader = loader
    # Emptied name -> where its objects went, for a header still naming the old one; cleared
    # where a model takes the name or the table is dropped. ``held`` is ``db_table -> every name
    # it held``, kept whole: ``_prior_names`` filters it against the live registry.
    forward: dict[str, str] = {}
    held: dict[str, list[str]] = {}

    def _ensure_loader() -> MigrationLoader:
        """The caller's loader, or one built once here. Building imports every migration module
        in the project, so it is built at most once for the whole scan."""
        nonlocal built_loader
        if built_loader is None:
            from django.db.migrations.loader import (  # noqa: PLC0415 - see the docstring
                MigrationLoader as _Loader,
            )

            built_loader = _Loader(None, ignore_no_migrations=True)
        return built_loader

    # The families a column-scoped retirement can name, and the ones only a whole-table one
    # reaches. Both hold live references to the dicts above, so a subtraction is seen by the
    # rest of the scan -- which is the point: a later migration re-recording a key wins again.
    keyed_families = {
        'soft_delete_related': existing_soft_delete_related,
        'soft_delete_owned': existing_soft_delete_owned,
    }
    # A revive and a sweep fire on the owner (index 1), a self cascade on its own table.
    trigger_families = {
        'soft_delete_revive': (existing_soft_delete_revive, 1),
        'soft_delete_owned_sweep': (existing_soft_delete_owned_sweep, 1),
        'soft_delete_self_cascade': (existing_soft_delete_self_cascade, 0),
        'soft_delete_revive_owner': (existing_soft_delete_revive_owner, 0),
        'soft_delete_cascade_owner': (existing_soft_delete_cascade_owner, 0),
    }
    whole_table_families = {
        'triggers': existing_triggers,
        'soft_deletes': existing_soft_deletes,
        'mti_triggers': existing_mti_triggers,
        'mti_soft_deletes': existing_mti_soft_deletes,
    }
    # What a rename carries to its new name: every family and every provenance list. A site
    # list is re-keyed beside them, below.
    every_family = (
        existing_triggers,
        existing_soft_deletes,
        existing_soft_delete_related,
        cascade_deps,
        existing_soft_delete_revive,
        revive_deps,
        existing_soft_delete_owned,
        owned_deps,
        existing_soft_delete_owned_sweep,
        sweep_deps,
        existing_soft_delete_self_cascade,
        self_deps,
        existing_soft_delete_revive_owner,
        revive_owner_deps,
        existing_soft_delete_cascade_owner,
        cascade_owner_deps,
        existing_mti_triggers,
        existing_mti_soft_deletes,
        existing_tenant_autofill,
        existing_tenant_policies,
        existing_policy_identities,
        existing_policy_sql,
        existing_policy_force,
        existing_tenant_forces,
    )

    def _forget_policy_and_autofill(table: str, *, whole_table: bool) -> None:
        # A tenant policy is dropped on **either** path -- it is filed against the column it
        # reads, so a column form takes it too. Forgetting it only on the whole-table path
        # leaves tenancy off with ``--check`` green.
        existing_tenant_policies.discard(table)
        existing_policy_identities.pop(table, None)
        existing_policy_sql.pop(table, None)
        existing_policy_force.pop(table, None)
        existing_tenant_forces.discard(table)
        if whole_table:
            # The trigger loop really is whole-table-only, so autofill is too.
            for key in [k for k in existing_tenant_autofill if k[0] == table]:
                del existing_tenant_autofill[key]

    def _void_digests_naming(table: str) -> None:
        for record in file_records:
            if table in record.tables:
                record.void = True

    def _rekey_sites(old: str, new: str) -> None:
        def _moved(site: CascadeRetirementSite) -> CascadeRetirementSite:
            key = _respell(site.key, {old: new})
            return site._replace(key=cast('tuple[str, str, str | None]', key))

        retirement_sites[:] = [_moved(site) for site in retirement_sites]
        revive_retirement_sites[:] = [_moved(site) for site in revive_retirement_sites]
        for sites in trigger_retirement_sites.values():
            sites[:] = [_moved(site) for site in sites]

    def _apply(event) -> None:
        if event.kind == 'create':
            # A model takes the name: whatever forwarded from it is somebody else's now.
            forward.pop(event.table, None)
        elif event.kind == 'rename':
            old, new_name = event.table, str(event.new_table)
            for recorded in every_family:
                _move_renamed(old, new_name, recorded, position)
            _rekey_sites(old, new_name)
            held[new_name] = [
                name
                for name in dict.fromkeys([*held.get(new_name, []), *held.pop(old, []), old])
                if name != new_name
            ]
            _void_digests_naming(old)
            for emptied, target in list(forward.items()):
                if target == old:
                    forward[emptied] = new_name
            forward[old] = new_name
            forward.pop(new_name, None)
        elif event.kind == 'drop':
            # What lived on the table went with it; the rule families keyed on it as a related
            # table stay, for the dropped-child retirement (ADR 0029), as do ``held`` and the
            # provenance. A trigger elsewhere whose body names the table survives it.
            _subtract_retired(event.table, None, {}, whole_table_families, trigger_families)
            _forget_policy_and_autofill(event.table, whole_table=True)
            _void_digests_naming(event.table)
            for emptied in [k for k, v in forward.items() if v == event.table or k == event.table]:
                del forward[emptied]
        else:  # 'retire'
            _subtract_retired(
                event.table, event.column, keyed_families, whole_table_families, trigger_families
            )
            _forget_policy_and_autofill(event.table, whole_table=event.column is None)

    def _record(key):
        return _respell(key, forward)

    files: dict[tuple[str, str], str] = {}
    for app in django_apps.get_app_configs():
        if _generator.is_local(app):
            for path, content in _generator.iter_migration_files(app):
                files[app.label, path.stem] = content
    units, aliases = _replay_units(_ensure_loader(), files)
    position = {(unit.app_label, unit.name): index for index, unit in enumerate(units)}

    for unit in units:
        for event in unit.events:
            _apply(event)
        content = files.get((unit.app_label, unit.name))
        if content is None:
            continue
        app_label, stem = unit.app_label, unit.name
        record: _FileRecord | None = None
        digest_match = _generator.RE_DIGEST.search(content.split('\n', 1)[0])
        if digest_match:
            record = _FileRecord(app_label, digest_match.group('digest'))
            file_records.append(record)

        function_match = _RE_TRIGGER_FUNCTION.search(content)
        if function_match:
            trigger_function_dep = (app_label, stem)
            trigger_function_sql = _recorded_sql_identity(content, function_match)
        stamp_match = _RE_STAMP_FUNCTION.search(content)
        if stamp_match:
            stamp_function_dep = (app_label, stem)
            stamp_function_sql = _recorded_sql_identity(content, stamp_match)
        parent_match = _RE_PARENT_TRIGGER_FUNCTION.search(content)
        if parent_match:
            parent_trigger_function_dep = (app_label, stem)
            parent_trigger_function_sql = _recorded_sql_identity(content, parent_match)

        # finditer, not search: unlike the two singletons above, one migration may define
        # several autofill functions, and each is recorded under its own name.
        for autofill_match in _RE_TENANT_AUTOFILL_FUNCTION.finditer(content):
            function = _identifiers._unescape_ident(autofill_match.group(1))
            autofill_function_deps[function] = (app_label, stem)
            autofill_function_sql[function] = _recorded_sql_identity(content, autofill_match)

        for pattern, target, key_fn in scan_table:
            for match in pattern.finditer(content):
                key = _record(key_fn(match))
                target[key] = _recorded_sql_identity(content, match)
                if record is not None:
                    record.tables.update(_tables_named(key))

        # Bespoke, one family asking this: the only one whose drop is hosted by a different
        # app than its create. A list, not last-write-wins -- a key created, retired and
        # created again has two, and which of them a drop dropped is what orders it.
        for match in _RE_SOFT_DELETE_RELATED.finditer(content):
            creates = cascade_deps.setdefault(_record(_cascade_key(match)), [])
            # One entry per migration: the plain and ``_via`` forms of one pair share a
            # key when the column is dropped, so a file can name it more than once.
            if (app_label, stem) not in creates:
                creates.append((app_label, stem))

        # The same, for the inverse family: its drop is ordered against the migration that
        # created *it*, which the cascade's own creates cannot answer -- the two land
        # together today, but nothing enforces that, and a mis-ordered DROP is silent.
        for match in _RE_SOFT_DELETE_REVIVE.finditer(content):
            creates = revive_deps.setdefault(_record(_cascade_key(match)), [])
            if (app_label, stem) not in creates:
                creates.append((app_label, stem))

        # #66's families record their creates too, and their retirements are settled by the
        # graph rather than popped here: a trigger can be written by an MTI descendant's app.
        for pattern, deps in (
            (_RE_SOFT_DELETE_OWNED, owned_deps),
            (_RE_SOFT_DELETE_OWNED_SWEEP, sweep_deps),
            (_RE_SOFT_DELETE_SELF_CASCADE, self_deps),
            (_RE_SOFT_DELETE_REVIVE_OWNER, revive_owner_deps),
            (_RE_SOFT_DELETE_CASCADE_OWNER, cascade_owner_deps),
        ):
            for match in pattern.finditer(content):
                creates = deps.setdefault(_record(_unescaped_groups(match)), [])
                if (app_label, stem) not in creates:
                    creates.append((app_label, stem))

        # Recorded per file, not per family: a repeat is only visible while the file is
        # open, and the key it writes is the one a real MTI child writes too.
        for pattern, kind in (
            (_RE_MTI_UPDATED_AT, 'MTI Updated at Trigger'),
            (_RE_MTI_SOFT_DELETE, 'MTI Soft Delete Rule'),
        ):
            seen_mti: set[str] = set()
            for match in pattern.finditer(content):
                table = _identifiers._unescape_ident(match.group(1))
                # Recorded on the *second* copy only: a third would otherwise print the
                # same sentence again, and the reader has one file to open either way.
                if table in seen_mti:
                    entry = (app_label, stem, kind, table)
                    if entry not in duplicate_mti:
                        duplicate_mti.append(entry)
                seen_mti.add(table)

        # Bespoke rather than a scan_table row, because these two headers partition one
        # key space and retirement *subtracts*. A pop, not a sentinel: a re-adopted column
        # must read as uncovered and plainly CREATE.
        for match in _RE_TENANT_AUTOFILL.finditer(content):
            key = _record(_autofill_key(match))
            existing_tenant_autofill[key] = _recorded_sql_identity(content, match)
            if record is not None:
                record.tables.add(key[0])
        # Not popped here: a line of nodes the graph leaves unordered must not read "unordered"
        # as "later". Left for the settle pass, which asks the graph. See ADR 0021.
        for match in _RE_SOFT_DELETE_RELATED_RETIRED.finditer(content):
            retirement_sites.append(
                CascadeRetirementSite(app_label, stem, _record(_cascade_key(match)), None)
            )
        for match in _RE_SOFT_DELETE_REVIVE_RETIRED.finditer(content):
            revive_retirement_sites.append(
                CascadeRetirementSite(app_label, stem, _record(_cascade_key(match)), None)
            )
        for pattern, recorded in (
            (_RE_SOFT_DELETE_OWNED_RETIRED, existing_soft_delete_owned),
            (_RE_SOFT_DELETE_OWNED_SWEEP_RETIRED, existing_soft_delete_owned_sweep),
            (_RE_SOFT_DELETE_SELF_CASCADE_RETIRED, existing_soft_delete_self_cascade),
            (_RE_SOFT_DELETE_REVIVE_OWNER_RETIRED, existing_soft_delete_revive_owner),
            (_RE_SOFT_DELETE_CASCADE_OWNER_RETIRED, existing_soft_delete_cascade_owner),
        ):
            for match in pattern.finditer(content):
                trigger_retirement_sites.setdefault(id(recorded), []).append(
                    CascadeRetirementSite(app_label, stem, _record(_unescaped_groups(match)), None)
                )
                # Not ``retirement_apps``: ADR 0021 waives the digest guard where a create or
                # drop recurs *this run*, at the point it is written. A scan-time flag made the
                # one-time retirement of every owned rule (#80, ADR 0039) permanent for the app.
        retirements = list(_RE_TENANT_AUTOFILL_RETIRED.finditer(content))
        for match in retirements:
            existing_tenant_autofill.pop(_record(_autofill_key(match)), None)
        if retirements:
            # Recorded per app, not per key: this is what tells `_generate_stage` its
            # file-level digest guard can no longer assume operation sets never recur.
            retirement_apps.add(app_label)

        policy_matches = list(_RE_TENANT_POLICY.finditer(content))
        unforced_in_file = unforced_policy_tables(content, policy_matches)
        for match in policy_matches:
            table = _record(_identifiers._unescape_ident(match.group(1)))
            existing_tenant_policies.add(table)
            if record is not None:
                record.tables.add(table)
            # Last write wins, within a file and across them (replay order is time). Unlike
            # [SQL:...], [POLICY:...] is never optional.
            policy_identity = _recorded_policy_identity(content, match)
            if policy_identity is None:  # pragma: no cover - unreachable
                # HEADER_TENANT_POLICY always writes [POLICY:...] inline, so this guards
                # the invariant rather than a real code path.
                raise RuntimeError(
                    f'Tenant RLS header for "{table}" matched but carried no '
                    f'[POLICY:...] identity -- HEADER_TENANT_POLICY always writes one.'
                )
            existing_policy_identities[table] = policy_identity
            existing_policy_sql[table] = _recorded_sql_identity(content, match)
            # Last write wins here too: a union instead would leave a table on the
            # backlog forever after one force=False write, even once superseded.
            existing_policy_force[table] = table in unforced_in_file
        for match in _RE_TENANT_FORCE.finditer(content):
            table = _record(_identifiers._unescape_ident(match.group(1)))
            existing_tenant_forces.add(table)
            if record is not None:
                record.tables.add(table)

    # Settled after the replay, because that is one line through nodes the graph may leave
    # unordered, and this question is the graph's: see ADR 0021.
    graph = _ensure_loader().graph
    retirement_sites = _settle_retirement_sites(
        retirement_sites, existing_soft_delete_related, cascade_deps, graph, aliases
    )
    # Twice, once per family: the helper is already generic over its arguments, and the
    # two answers are independent -- a key retired before 2.11.0 has a drop for the cascade
    # and no revive to pair with, so sharing one settle would read that as a missing create.
    revive_retirement_sites = _settle_retirement_sites(
        revive_retirement_sites, existing_soft_delete_revive, revive_deps, graph, aliases
    )
    owned_sites, sweep_sites, _self_sites, revive_owner_sites, cascade_owner_sites = (
        _settle_retirement_sites(
            trigger_retirement_sites.get(id(recorded), []), recorded, deps, graph, aliases
        )
        for recorded, deps in (
            (existing_soft_delete_owned, owned_deps),
            (existing_soft_delete_owned_sweep, sweep_deps),
            (existing_soft_delete_self_cascade, self_deps),
            (existing_soft_delete_revive_owner, revive_owner_deps),
            (existing_soft_delete_cascade_owner, cascade_owner_deps),
        )
    )

    existing_digests: defaultdict[str, set[str]] = defaultdict(set)
    for file_record in file_records:
        if not file_record.void:
            existing_digests[file_record.app_label].add(file_record.digest)

    return ExistingOperations(
        triggers=existing_triggers,
        soft_deletes=existing_soft_deletes,
        soft_delete_related=existing_soft_delete_related,
        soft_delete_related_dependencies=cascade_deps,
        cascade_retirement_sites=retirement_sites,
        soft_delete_revive=existing_soft_delete_revive,
        soft_delete_revive_dependencies=revive_deps,
        revive_retirement_sites=revive_retirement_sites,
        soft_delete_revive_owner=existing_soft_delete_revive_owner,
        soft_delete_revive_owner_dependencies=revive_owner_deps,
        revive_owner_retirement_sites=revive_owner_sites,
        soft_delete_cascade_owner=existing_soft_delete_cascade_owner,
        soft_delete_cascade_owner_dependencies=cascade_owner_deps,
        cascade_owner_retirement_sites=cascade_owner_sites,
        soft_delete_owned=existing_soft_delete_owned,
        soft_delete_owned_sweep=existing_soft_delete_owned_sweep,
        soft_delete_self_cascade=existing_soft_delete_self_cascade,
        soft_delete_owned_dependencies=owned_deps,
        soft_delete_owned_sweep_dependencies=sweep_deps,
        soft_delete_self_cascade_dependencies=self_deps,
        owned_retirement_sites=owned_sites,
        owned_sweep_retirement_sites=sweep_sites,
        mti_triggers=existing_mti_triggers,
        mti_soft_deletes=existing_mti_soft_deletes,
        duplicate_mti_operations=duplicate_mti,
        tenant_policies=existing_tenant_policies,
        tenant_policy_identities=existing_policy_identities,
        tenant_policy_sql=existing_policy_sql,
        unforced_policies={table for table, unforced in existing_policy_force.items() if unforced},
        tenant_forces=existing_tenant_forces,
        tenant_autofill=existing_tenant_autofill,
        retirement_apps=retirement_apps,
        renamed_tables=held,
        tenant_autofill_function_dependencies=autofill_function_deps,
        tenant_autofill_function_sql=autofill_function_sql,
        existing_digests=dict(existing_digests),
        trigger_function_dependency=trigger_function_dep,
        parent_trigger_function_dependency=parent_trigger_function_dep,
        stamp_function_dependency=stamp_function_dep,
        trigger_function_sql=trigger_function_sql,
        parent_trigger_function_sql=parent_trigger_function_sql,
        stamp_function_sql=stamp_function_sql,
    )
