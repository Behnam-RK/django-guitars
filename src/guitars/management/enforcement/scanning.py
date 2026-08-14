"""Scanning migration files for enforcement operations already written -- the read side of
the frozen headers in ``headers.py``. Every local app's migrations are scanned once, so a
partially covered app receives only what it's genuinely missing."""

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
    """Which enforcement operations the migration files already contain, scanned once. The
    first five map key -> ``[SQL:...]`` digest, not a set: conflating "covered" with
    "covered by today's SQL" is how the 1.0.0 guard rewrite once shipped as a no-op."""

    triggers: dict[str, str | None]
    soft_deletes: dict[str, str | None]
    #: Keyed on (related_table, table, foreign_key) -- the third element is ``None`` for the
    #: one FK per pair keeping the plain historical header, or the column for any other.
    soft_delete_related: dict[tuple[str, str, str | None], str | None]
    mti_triggers: dict[str, str | None]
    mti_soft_deletes: dict[str, str | None]
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


def scan_existing_operations() -> ExistingOperations:
    """Scan every local app's migration files for enforcement operations already written --
    by comment header, so a partially covered app receives exactly what it lacks."""
    # Table (or table pair) -> the [SQL:...] digest of its most recent operation.
    # Last write wins throughout, which is only the currently-applied answer because
    # _generator.iter_migration_files yields in filename order -- see its docstring.
    existing_triggers: dict[str, str | None] = {}
    existing_soft_deletes: dict[str, str | None] = {}
    existing_soft_delete_related: dict[tuple[str, str, str | None], str | None] = {}
    existing_mti_triggers: dict[str, str | None] = {}
    existing_mti_soft_deletes: dict[str, str | None] = {}
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
