"""Scanning migration files for enforcement operations already written.

The read side of the frozen headers in ``headers.py``: for every local app, every
migration file is scanned once, by comment header, so a partially covered app receives
only the operations it is genuinely missing.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING, NamedTuple

from django.apps import apps as django_apps

from guitars.management import _generator
from guitars.management.enforcement.headers import (
    _RE_MTI_SOFT_DELETE,
    _RE_MTI_UPDATED_AT,
    _RE_PARENT_TRIGGER_FUNCTION,
    _RE_SOFT_DELETE,
    _RE_SOFT_DELETE_RELATED,
    _RE_TENANT_FORCE,
    _RE_TENANT_POLICY,
    _RE_TRIGGER_FUNCTION,
    _RE_UPDATED_AT,
)
from guitars.management.enforcement.identity import (
    _recorded_policy_identity,
    _recorded_sql_identity,
    unforced_policy_tables,
)
from guitars.sql import _identifiers


if TYPE_CHECKING:
    from collections.abc import Callable


class ExistingOperations(NamedTuple):
    """Which enforcement operations the migration files already contain.

    Scanned once at construction, by comment header, so a partially covered app receives
    only the operations it is genuinely missing. Named rather than a positional tuple: this
    is ten fields and was once an anonymous one, and a caller unpacking ten sets in the right
    order is a bug waiting to happen.

    Each of the first five maps its key to the ``[SQL:...]`` digest the **most recent**
    operation for that key was written with, or ``None`` for a header from before that token
    existed. Mappings rather than sets because "is this table covered" and "is it covered by
    the SQL the kit emits today" are different questions, and answering only the first is how
    the 1.0.0 soft-delete guard rewrite reached every existing database as a no-op. ``in``
    behaves identically on a dict, so membership tests elsewhere read unchanged.
    """

    triggers: dict[str, str | None]
    soft_deletes: dict[str, str | None]
    #: Keyed on (related_table, table, foreign_key) -- the third element is ``None`` for
    #: the one FK per pair that keeps the plain, historical header (see
    #: HEADER_SOFT_DELETE_RELATED_VIA's comment), or the FK's column for any other FK on
    #: the same pair.
    soft_delete_related: dict[tuple[str, str, str | None], str | None]
    mti_triggers: dict[str, str | None]
    mti_soft_deletes: dict[str, str | None]
    tenant_policies: set[str]
    #: Table -> the ``[POLICY:...]`` identity its **most recent** policy operation was written
    #: with. Compared against the identity the models imply now, so a coverage shape that
    #: changed produces a replacement instead of being mistaken for already covered. Most
    #: recent, not any: a shape taken A -> B -> A must match the migration applied last, so
    #: ``_generator.iter_migration_files`` yields in filename order.
    #:
    #: Kept alongside :attr:`tenant_policy_sql` rather than folded into it, because the two
    #: answer different questions. The identity is what the policy *says*, with ``force``
    #: deliberately excluded so flipping ``GUITARS_RLS_FORCE`` cannot trigger a full
    #: replacement and defeat the staged ``--force-rls`` retrofit. The SQL digest is whether
    #: the text is current. Either one changing means the table needs a replacement.
    tenant_policy_identities: dict[str, str]
    #: Table -> the ``[SQL:...]`` digest of its most recent policy operation, or ``None``.
    tenant_policy_sql: dict[str, str | None]
    #: Tables whose policy operation was written with ``force=False`` -- see
    #: :func:`unforced_policy_tables`. These are the only ones a second FORCE stage can act on.
    unforced_policies: set[str]
    tenant_forces: set[str]
    #: App label -> every ``[DIGEST:...]`` already stamped on one of its migration files.
    #: Harvested during the same file-by-file pass as everything else above, so a later
    #: "has this operation set already been written" check is a dict lookup instead of a
    #: fresh directory re-scan (see ``handle()`` and ``_handle_force_rls_stage``, which
    #: previously called ``_generator.migration_with_digest_exists`` for this).
    existing_digests: dict[str, set[str]]
    trigger_function_dependency: tuple[str, str] | None
    parent_trigger_function_dependency: tuple[str, str] | None
    #: The ``[SQL:...]`` digest of the most recent migration defining each singleton trigger
    #: function, or ``None`` for one written before the token existed. The functions are
    #: singletons by *existence*, which is why a change to either body previously shipped
    #: nothing at all: the first thing both ensure methods did was return early.
    trigger_function_sql: str | None
    parent_trigger_function_sql: str | None


def scan_existing_operations() -> ExistingOperations:
    """Scan every local app's migration files for enforcement operations already written.

    Recognition is by comment header, per operation, so an app that is partially covered
    receives exactly the operations it lacks rather than a duplicate of the whole set.
    """
    # Table (or table pair) -> the [SQL:...] digest of its most recent operation.
    # Last write wins throughout, which is only the currently-applied answer because
    # _generator.iter_migration_files yields in filename order -- see its docstring.
    existing_triggers: dict[str, str | None] = {}
    existing_soft_deletes: dict[str, str | None] = {}
    existing_soft_delete_related: dict[tuple[str, str, str | None], str | None] = {}
    existing_mti_triggers: dict[str, str | None] = {}
    existing_mti_soft_deletes: dict[str, str | None] = {}
    # (regex, dict, key_fn) for every scan that is a plain "finditer, record by key"
    # pass -- the singleton-function searches and the tenant-policy/force blocks below
    # do not fit this shape (a `.search` rather than `.finditer`, or extra bookkeeping
    # per match) and stay as their own code.
    # Every captured group here is _unescape_ident'd before use: operations.py writes a
    # table containing a literal '"' (Django's pre-quoted schema-qualified convention) into
    # its header doubled (_escape_ident), so headers.py's broadened `_QUOTED_CONTENT`
    # pattern can match it without the embedded quote closing the header's own delimiter
    # early -- undoing that here is what makes the captured text equal, byte-for-byte, the
    # same `model._meta.db_table` a later run recomputes fresh as its dict key. A table with
    # no embedded quote round-trips through _unescape_ident unchanged.
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

    for app in django_apps.get_app_configs():
        if not _generator.is_local(app):
            continue
        for path, content in _generator.iter_migration_files(app):
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

            for pattern, target, key_fn in scan_table:
                for match in pattern.finditer(content):
                    target[key_fn(match)] = _recorded_sql_identity(content, match)

            policy_matches = list(_RE_TENANT_POLICY.finditer(content))
            unforced_in_file = unforced_policy_tables(content, policy_matches)
            for match in policy_matches:
                table = _identifiers._unescape_ident(match.group(1))
                existing_tenant_policies.add(table)
                # Last write wins, within a file and across them -- files arrive in
                # filename order, which is application order.
                # Unlike [SQL:...], [POLICY:...] is not optional -- HEADER_TENANT_POLICY
                # always carries it, so a match of _RE_TENANT_POLICY always has one.
                policy_identity = _recorded_policy_identity(content, match)
                if policy_identity is None:  # pragma: no cover - unreachable: see note below
                    # HEADER_TENANT_POLICY always writes [POLICY:...] inline, so a match of
                    # _RE_TENANT_POLICY always has one -- this guards the invariant rather
                    # than a real code path, in case a future header edit ever breaks it.
                    raise RuntimeError(
                        f'Tenant RLS header for "{table}" matched but carried no '
                        f'[POLICY:...] identity -- HEADER_TENANT_POLICY always writes one.'
                    )
                existing_policy_identities[table] = policy_identity
                existing_policy_sql[table] = _recorded_sql_identity(content, match)
                # Last write wins here too, and for the same reason. Accumulating these
                # by union instead left a table on the backlog forever once any migration
                # had written it ``force=False``: a later replacement carrying
                # ``force=True`` inlines FORCE and so emits no FORCE header for
                # ``tenant_forces`` to find, and ``--force-rls`` then wrote a redundant
                # migration for a table that was already forced.
                existing_policy_force[table] = table in unforced_in_file
            existing_tenant_forces.update(
                _identifiers._unescape_ident(m.group(1))
                for m in _RE_TENANT_FORCE.finditer(content)
            )

    return ExistingOperations(
        triggers=existing_triggers,
        soft_deletes=existing_soft_deletes,
        soft_delete_related=existing_soft_delete_related,
        mti_triggers=existing_mti_triggers,
        mti_soft_deletes=existing_mti_soft_deletes,
        tenant_policies=existing_tenant_policies,
        tenant_policy_identities=existing_policy_identities,
        tenant_policy_sql=existing_policy_sql,
        unforced_policies={table for table, unforced in existing_policy_force.items() if unforced},
        tenant_forces=existing_tenant_forces,
        existing_digests=dict(existing_digests),
        trigger_function_dependency=trigger_function_dep,
        parent_trigger_function_dependency=parent_trigger_function_dep,
        trigger_function_sql=trigger_function_sql,
        parent_trigger_function_sql=parent_trigger_function_sql,
    )
