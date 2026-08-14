"""Mirror the active tenant frame into PostgreSQL session settings, published lazily
before each statement. **The cache key is deliberately more than the values** -- see
CLAUDE.md's checklist and :func:`_transaction_marker`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connections, transaction
from django.db.backends.signals import connection_created

from guitars.gucs import BYPASS_GUC, VALUE_SEPARATOR, guc_name

from .messages import remediation
from .scope import (
    BYPASS,
    MULTI_VALUE_TYPES,
    TenantScopeViolation,
    get_tenant,
    reject_separator,
)


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from django.db.backends.base.base import BaseDatabaseWrapper


__all__ = [
    'desired_state',
    'encode_value',
    'install',
    'install_on',
    'uninstall',
]

_CACHE = '_guitars_tenant_guc'
_SYNCING = '_guitars_tenant_guc_syncing'
# SQLSTATE 42501 insufficient_privilege -- what a WITH CHECK violation raises.
_RLS_SQLSTATE = '42501'
# SQLSTATE 25P02 in_failed_sql_transaction -- every statement is refused until rollback.
_ABORTED_SQLSTATE = '25P02'


def _sqlstate(exc: BaseException) -> str | None:
    """The driver's SQLSTATE for *exc*, whichever driver it came from -- psycopg names it
    ``sqlstate``, psycopg2 ``pgcode``, and guitars pins neither."""
    return getattr(exc, 'sqlstate', None) or getattr(exc, 'pgcode', None)


def _scalar(value: object) -> str:
    """One dimension value as its GUC text, refusing via
    :func:`~guitars.tenancy.scope.reject_separator` -- called here too, not just at scope
    entry, since a pk that was ``None`` at scope open can acquire one before publish."""
    reject_separator(value)
    pk = getattr(value, 'pk', value)
    return '' if pk is None else str(pk)


def encode_value(value: object) -> str:
    """Encode a dimension value as its GUC string (sorted, so it is stable)."""
    if isinstance(value, MULTI_VALUE_TYPES):
        return VALUE_SEPARATOR.join(sorted(_scalar(item) for item in value))
    return _scalar(value)


def desired_state() -> dict[str, str]:
    """The GUC mapping the active frame should produce."""
    active = get_tenant()
    state = {BYPASS_GUC: 'on' if active.get(BYPASS, False) else 'off'}
    for dimension, value in active.items():
        if dimension != BYPASS:
            state[guc_name(dimension)] = encode_value(value)
    return state


def _fingerprint(connection: BaseDatabaseWrapper) -> tuple:
    # savepoint_ids shrinks on ROLLBACK TO SAVEPOINT, which also reverts any SET made
    # after that savepoint -- so its shape is part of the signal we need.
    return (connection.in_atomic_block, tuple(connection.savepoint_ids))


def _transaction_marker(
    connection: BaseDatabaseWrapper, superseded: Callable[[], None] | None = None
) -> Callable[[], None]:
    """Register a no-op commit hook whose *presence* identifies this transaction --
    :func:`_fingerprint` alone can't distinguish two sibling ``atomic()`` blocks. A
    ``superseded`` marker is replaced *in place*, never filtered out."""

    def marker() -> None:
        """Never does anything -- only its presence carries information."""

    if superseded is not None:
        # Fresh savepoint ids: this marker vouches for the SET LOCAL just issued. A dead
        # superseded isn't found, and we fall through to a plain registration below.
        entry = (set(connection.savepoint_ids), marker, False)
        for index, existing in enumerate(connection.run_on_commit):
            if existing[1] is superseded:
                connection.run_on_commit[index] = entry  # ty: ignore[invalid-assignment]
                return marker
    transaction.on_commit(marker, using=connection.alias)
    return marker


def _marker_live(connection: BaseDatabaseWrapper, marker: Callable[[], None] | None) -> bool:
    if marker is None:  # published at session level; no transaction to outlive
        return True
    return any(hook is marker for _, hook, *_ in connection.run_on_commit)


def _publish(connection: BaseDatabaseWrapper, state: dict[str, str]) -> None:
    cached = getattr(connection, _CACHE, None)
    previous = cached[0] if cached else {}
    updates = dict(state)
    # A dimension present in the last frame but absent now must be cleared, or a policy
    # would keep matching against a tenant nobody is scoped to.
    for stale in previous:
        updates.setdefault(stale, '')

    # Transaction-local inside a block: it then cannot outlive the block, so a commit or
    # rollback can't leave a value we'd wrongly believe is still set.
    is_local = connection.in_atomic_block
    fragments = ', '.join(['set_config(%s, %s, %s)'] * len(updates))
    params: list[str | bool] = []
    for name, value in updates.items():
        params += [name, value, is_local]

    setattr(connection, _SYNCING, True)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f'SELECT {fragments}', params)  # noqa: S608 - fixed fragments, bound params
    finally:
        setattr(connection, _SYNCING, False)
    # Registered only after the write landed, so a failed publish leaves no marker
    # claiming this transaction is already up to date -- and the one it replaces is
    # swapped out in place, only once there is a successor.
    marker = (
        _transaction_marker(connection, superseded=cached[2] if cached else None)
        if is_local
        else None
    )
    setattr(connection, _CACHE, (state, _fingerprint(connection), marker))


def _walk_chain(exc: BaseException) -> Iterator[BaseException]:
    """Walk *exc*'s cause/context chain, cycle-safe. Both links: ``__cause__`` (Django's
    ``raise ... from``) and ``__context__`` (a bare re-raise). Tracked by identity, since
    an exception free to define ``__eq__`` could make two distinct links look the same."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _aborted_transaction(exc: BaseException) -> bool:
    """Whether *exc* is "the transaction is already aborted", anywhere in its chain."""
    return any(_sqlstate(link) == _ABORTED_SQLSTATE for link in _walk_chain(exc))


def _ensure(connection: BaseDatabaseWrapper) -> None:
    if connection.vendor != 'postgresql':
        return
    state = desired_state()
    cached = getattr(connection, _CACHE, None)
    if cached is not None:
        cached_state, cached_fingerprint, marker = cached
        if (
            cached_state == state
            and cached_fingerprint == _fingerprint(connection)
            and _marker_live(connection, marker)
        ):
            return
    try:
        _publish(connection, state)
    except Exception as exc:  # noqa: BLE001 - narrowed immediately below
        if not _aborted_transaction(exc):
            raise
        # Postgres refuses every statement until rollback, including the recovery ROLLBACK
        # TO SAVEPOINT itself; raising here would wedge the connection permanently. Cannot
        # fail open: the cache is untouched and no statement can run while aborted.
        return


def _rls_violation(exc: BaseException) -> BaseException | None:
    """The RLS-violating error in ``exc``'s chain, if any -- the SQLSTATE-carrying error is
    usually a link down, not what we're handed. English-only message test separates it
    from an ordinary ``permission denied`` sharing the same SQLSTATE 42501."""
    for link in _walk_chain(exc):
        if _sqlstate(link) == _RLS_SQLSTATE and 'row-level security' in str(link).lower():
            return link
    return None


def _wrapper(
    execute: Callable, sql: str, params: object, many: bool, context: dict[str, object]
) -> object:
    connection: BaseDatabaseWrapper = context['connection']  # ty: ignore[invalid-assignment]
    # Re-entrancy guard: _publish issues SQL of its own through this same path.
    if not getattr(connection, _SYNCING, False):
        _ensure(connection)
    try:
        return execute(sql, params, many, context)
    except Exception as exc:
        violation = _rls_violation(exc)
        if violation is None:
            raise
        # TenantScopeViolation, not Missing: last-resort layer (joins, cascades, raw SQL),
        # so a rejection may mean no scope was ever opened -- filed as a violation anyway
        # since both share the TenantScopeError base a caller can catch either way.
        raise TenantScopeViolation(
            f'write rejected by a tenant policy -- the row does not belong to the '
            f'active tenant, or no tenant scope is active -- {remediation("write")} '
            f'Database said: {violation}'
        ) from exc


def install_on(connection: BaseDatabaseWrapper) -> None:
    """Attach the wrapper to one connection and forget its cached GUC state."""
    if hasattr(connection, _CACHE):
        delattr(connection, _CACHE)
    if _wrapper not in connection.execute_wrappers:
        connection.execute_wrappers.append(_wrapper)


def _on_connection_created(
    sender: object, connection: BaseDatabaseWrapper, **kwargs: object
) -> None:
    # A brand-new session has no GUCs set, so the cache must start empty -- install_on()
    # clears it. This is also what makes a reconnect safe.
    install_on(connection)


def install() -> None:
    """Publish the tenant frame on every connection, now and in future. Idempotent."""
    connection_created.connect(_on_connection_created, dispatch_uid=_CACHE)
    for alias in connections:
        install_on(connections[alias])


def uninstall() -> None:
    """Detach the wrapper everywhere. For tests."""
    connection_created.disconnect(dispatch_uid=_CACHE)
    for alias in connections:
        connection = connections[alias]
        if _wrapper in connection.execute_wrappers:
            connection.execute_wrappers.remove(_wrapper)
        if hasattr(connection, _CACHE):
            delattr(connection, _CACHE)
