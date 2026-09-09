"""Scanning migration files for enforcement operations already written -- the read side of
the frozen headers in ``headers.py``. Every local app's migrations are scanned once, so a
partially covered app receives only what it's genuinely missing."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING, NamedTuple

from django.apps import apps as django_apps

from guitars.management import _generator
from guitars.management.enforcement.graph import (
    renamed_tables,
    renames_by_migration,
    retired_enforcement,
)
from guitars.management.enforcement.headers import (
    _RE_MTI_SOFT_DELETE,
    _RE_MTI_UPDATED_AT,
    _RE_PARENT_TRIGGER_FUNCTION,
    _RE_SOFT_DELETE,
    _RE_SOFT_DELETE_OWNED,
    _RE_SOFT_DELETE_OWNED_SWEEP,
    _RE_SOFT_DELETE_RELATED,
    _RE_SOFT_DELETE_RELATED_RETIRED,
    _RE_SOFT_DELETE_SELF_CASCADE,
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


class ExistingOperations(NamedTuple):
    """Which enforcement operations the migration files already contain, scanned once. The
    first five map key -> ``[SQL:...]`` digest, not a set: conflating "covered" with
    "covered by today's SQL" is how the 1.0.0 guard rewrite once shipped as a no-op."""

    triggers: dict[str, str | None]
    soft_deletes: dict[str, str | None]
    #: Keyed on (related_table, table, foreign_key) -- the third element is ``None`` for the
    #: one FK per pair keeping the plain historical header, or the column for any other.
    soft_delete_related: dict[tuple[str, str, str | None], str | None]
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
    #: The ``[SQL:...]`` digest of the most recent migration defining each singleton
    #: trigger function. Singletons by *existence*, which is why a body change once shipped
    #: nothing: both ensure methods returned early on mere presence.
    trigger_function_sql: str | None
    parent_trigger_function_sql: str | None


def _subtract_retired(
    table: str,
    column: str | None,
    keyed: dict[str, dict],
    whole_table: dict[str, dict],
) -> None:
    """Forget what a ``RetireEnforcement`` dropped, so a later run re-emits what the models
    still call for. *keyed* spell a table **and** a column, so a column form can match them;
    *whole_table* are keyed on a table alone and only the whole-table form reaches them."""
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


def _move_renamed(old: str, new: str, recorded: dict | set) -> None:
    """Move *recorded*'s entries from table *old* onto *new*, in place."""
    # Called as the scan crosses the renaming migration, not over the finished scan: order is
    # what makes a **cycle** right. ``A -> B`` and back leaves two entries under ``A`` and only
    # the walk knows which is newer -- a post-pass guessed, and the pre-cycle one won.
    if isinstance(recorded, set):
        if old in recorded:
            recorded.discard(old)
            recorded.add(new)
        return
    for key in list(recorded):
        # ``isinstance`` first: a string is iterable, so the tuple branch would shred a
        # table-keyed entry into a tuple of characters.
        moved = (
            (new if key == old else key)
            if isinstance(key, str)
            else tuple(new if part == old else part for part in key)
        )
        if moved != key:
            # Overwrite, never ``setdefault``: this runs at the rename, so anything already
            # filed under the destination predates it and the moving entry is the newer.
            recorded[moved] = recorded.pop(key)


def scan_existing_operations(loader: MigrationLoader | None = None) -> ExistingOperations:
    """Scan every local app's migration files for enforcement operations already written, by
    comment header, so a partially covered app receives exactly what it lacks."""
    # *loader* is the caller's cached one. A retirement is read off loaded operations, so one
    # is built here when none is given, and at most once for the whole scan.

    # Table (or table pair) -> the [SQL:...] digest of its most recent operation.
    # Last write wins throughout, which is only the currently-applied answer because
    # _generator.iter_migration_files yields in filename order -- see its docstring.
    existing_triggers: dict[str, str | None] = {}
    existing_soft_deletes: dict[str, str | None] = {}
    existing_soft_delete_related: dict[tuple[str, str, str | None], str | None] = {}
    existing_soft_delete_owned: dict[tuple[str, str, str], str | None] = {}
    existing_soft_delete_owned_sweep: dict[tuple[str, str, str], str | None] = {}
    existing_soft_delete_self_cascade: dict[tuple[str, str], str | None] = {}
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
        (
            _RE_SOFT_DELETE_RELATED,
            existing_soft_delete_related,
            lambda m: (
                _identifiers._unescape_ident(m.group(1)),
                _identifiers._unescape_ident(m.group(2)),
                _identifiers._unescape_ident(m.group('foreign_key'))
                if m.group('foreign_key') is not None
                else None,
            ),
        ),
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
    existing_digests: defaultdict[str, set[str]] = defaultdict(set)
    trigger_function_dep: tuple[str, str] | None = None
    parent_trigger_function_dep: tuple[str, str] | None = None
    trigger_function_sql: str | None = None
    parent_trigger_function_sql: str | None = None
    autofill_function_deps: dict[str, tuple[str, str]] = {}
    autofill_function_sql: dict[str, str | None] = {}
    built_loader = loader
    _pending_renames: dict[str, list[str]] = {}
    live_tables = {
        model._meta.db_table for app in django_apps.get_app_configs() for model in app.get_models()
    }

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
        'soft_delete_owned_sweep': existing_soft_delete_owned_sweep,
        'soft_delete_self_cascade': existing_soft_delete_self_cascade,
    }
    whole_table_families = {
        'triggers': existing_triggers,
        'soft_deletes': existing_soft_deletes,
        'mti_triggers': existing_mti_triggers,
        'mti_soft_deletes': existing_mti_soft_deletes,
    }

    for app in django_apps.get_app_configs():
        if _generator.is_local(app):
            _pending_renames.update(renamed_tables(_ensure_loader(), app.label))

    for app in django_apps.get_app_configs():
        if not _generator.is_local(app):
            continue
        retired = retired_enforcement(_ensure_loader(), app.label)
        moves = renames_by_migration(_ensure_loader(), app.label)
        every_family = (
            existing_triggers,
            existing_soft_deletes,
            existing_soft_delete_related,
            existing_soft_delete_owned,
            existing_soft_delete_owned_sweep,
            existing_soft_delete_self_cascade,
            existing_mti_triggers,
            existing_mti_soft_deletes,
            existing_tenant_autofill,
            existing_tenant_policies,
            existing_policy_identities,
            existing_policy_sql,
            existing_policy_force,
            existing_tenant_forces,
        )
        # A retirement names the table as spelled *now*, while the keys it must subtract may
        # still be filed under a name a rename left behind -- the post-pass would then move the
        # old key back over the hole and a dropped object would read as covered.
        spellings = {
            table: [table, *_pending_renames.get(table, [])]
            for table, _ in [pair for pairs in retired.values() for pair in pairs]
        }
        for path, content in _generator.iter_migration_files(app):
            # Before this file's headers, not after: an operation retiring a key and a header
            # re-asserting it in the same migration means the migration re-asserts it.
            # Before this file's own headers and its retirements: the rename happened first.
            for old_table, new_table in moves.get(path.stem, ()):
                # A freed name already retaken by another model keeps its own coverage.
                if old_table in live_tables:
                    continue
                for recorded in every_family:
                    _move_renamed(old_table, new_table, recorded)

            for table, column in retired.get(path.stem, ()):
                for spelling in spellings[table]:
                    _subtract_retired(spelling, column, keyed_families, whole_table_families)
                    # A tenant policy is dropped on **either** path -- it is filed against the
                    # column it reads, so a column form takes it too. Forgetting it only on the
                    # whole-table path leaves tenancy off with ``--check`` green.
                    existing_tenant_policies.discard(spelling)
                    existing_policy_identities.pop(spelling, None)
                    existing_policy_sql.pop(spelling, None)
                    existing_policy_force.pop(spelling, None)
                    existing_tenant_forces.discard(spelling)
                    if column is None:
                        # The trigger loop really is whole-table-only, so autofill is too.
                        for key in [k for k in existing_tenant_autofill if k[0] == spelling]:
                            del existing_tenant_autofill[key]

            digest_match = _generator.RE_DIGEST.search(content.split('\n', 1)[0])
            if digest_match:
                existing_digests[app.label].add(digest_match.group('digest'))

            function_match = _RE_TRIGGER_FUNCTION.search(content)
            if function_match:
                trigger_function_dep = (app.label, path.stem)
                trigger_function_sql = _recorded_sql_identity(content, function_match)
            parent_match = _RE_PARENT_TRIGGER_FUNCTION.search(content)
            if parent_match:
                parent_trigger_function_dep = (app.label, path.stem)
                parent_trigger_function_sql = _recorded_sql_identity(content, parent_match)

            # finditer, not search: unlike the two singletons above, one migration may define
            # several autofill functions, and each is recorded under its own name.
            for autofill_match in _RE_TENANT_AUTOFILL_FUNCTION.finditer(content):
                function = _identifiers._unescape_ident(autofill_match.group(1))
                autofill_function_deps[function] = (app.label, path.stem)
                autofill_function_sql[function] = _recorded_sql_identity(content, autofill_match)

            for pattern, target, key_fn in scan_table:
                for match in pattern.finditer(content):
                    target[key_fn(match)] = _recorded_sql_identity(content, match)

            # Recorded per file, not per family: a repeat is only visible while the file is
            # open, and the key it writes is the one a real MTI child writes too.
            for pattern, kind in (
                (_RE_MTI_UPDATED_AT, 'MTI Updated at Trigger'),
                (_RE_MTI_SOFT_DELETE, 'MTI Soft Delete Rule'),
            ):
                seen_mti: set[str] = set()
                for match in pattern.finditer(content):
                    table = _identifiers._unescape_ident(match.group(1))
                    if table in seen_mti:
                        duplicate_mti.append((app.label, path.stem, kind, table))
                    seen_mti.add(table)

            # Bespoke rather than a scan_table row, because these two headers partition one
            # key space and retirement *subtracts* -- the only place this scan does. A pop,
            # not a sentinel: a re-adopted column must read as uncovered and plainly CREATE.
            for match in _RE_TENANT_AUTOFILL.finditer(content):
                existing_tenant_autofill[_autofill_key(match)] = _recorded_sql_identity(
                    content, match
                )
            # After ``scan_table`` recorded this file's create headers, so retire-then-create
            # inside one migration reads as the create -- the order the emitter writes them in.
            cascade_retirements = list(_RE_SOFT_DELETE_RELATED_RETIRED.finditer(content))
            for match in cascade_retirements:
                existing_soft_delete_related.pop(
                    (
                        _identifiers._unescape_ident(match.group(1)),
                        _identifiers._unescape_ident(match.group(2)),
                        _identifiers._unescape_ident(match.group('foreign_key'))
                        if match.group('foreign_key') is not None
                        else None,
                    ),
                    None,
                )
            if cascade_retirements:
                retirement_apps.add(app.label)

            retirements = list(_RE_TENANT_AUTOFILL_RETIRED.finditer(content))
            for match in retirements:
                existing_tenant_autofill.pop(_autofill_key(match), None)
            if retirements:
                # Recorded per app, not per key: this is what tells `_generate_stage` its
                # file-level digest guard can no longer assume operation sets never recur.
                retirement_apps.add(app.label)

            policy_matches = list(_RE_TENANT_POLICY.finditer(content))
            unforced_in_file = unforced_policy_tables(content, policy_matches)
            for match in policy_matches:
                table = _identifiers._unescape_ident(match.group(1))
                existing_tenant_policies.add(table)
                # Last write wins, within a file and across them (filename order is
                # application order). Unlike [SQL:...], [POLICY:...] is never optional.
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
            existing_tenant_forces.update(
                _identifiers._unescape_ident(m.group(1))
                for m in _RE_TENANT_FORCE.finditer(content)
            )

    # One map across every local app: a cascade rule's key names two tables, and they can
    # belong to different apps, so translating per app would leave half a key behind.
    renames = _pending_renames

    return ExistingOperations(
        triggers=existing_triggers,
        soft_deletes=existing_soft_deletes,
        soft_delete_related=existing_soft_delete_related,
        soft_delete_owned=existing_soft_delete_owned,
        soft_delete_owned_sweep=existing_soft_delete_owned_sweep,
        soft_delete_self_cascade=existing_soft_delete_self_cascade,
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
        renamed_tables=renames,
        tenant_autofill_function_dependencies=autofill_function_deps,
        tenant_autofill_function_sql=autofill_function_sql,
        existing_digests=dict(existing_digests),
        trigger_function_dependency=trigger_function_dep,
        parent_trigger_function_dependency=parent_trigger_function_dep,
        trigger_function_sql=trigger_function_sql,
        parent_trigger_function_sql=parent_trigger_function_sql,
    )
