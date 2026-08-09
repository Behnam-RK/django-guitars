"""Mirror the active tenant frame into PostgreSQL session settings (GUCs).

Row-level-security policies cannot read a Python ``ContextVar``, so the frame is
published as ``tenant.<dimension>`` plus ``tenant.bypass``. Policies read them with
``current_setting(..., true)``; an unset GUC yields ``NULL`` and therefore denies, which
is what makes the database half fail closed.

**Lazy, not eager.** Publishing happens in a ``connection.execute_wrappers`` entry,
immediately before a statement runs, and only when the desired state differs from what
this connection was last told. A ``tenant()`` block that never queries costs nothing,
nested blocks cost nothing (only the effective value matters), and a tenant switch costs
one extra round trip.

**Why the cache key is more than the values.** PostgreSQL reverts a session-level ``SET``
when a transaction aborts, and reverts a transaction-local one when it ends at all. Either
way the connection silently stops matching what we cached -- and a stale cache means we
skip a needed update and the *previous* tenant's value stays live, which fails OPEN.

Two things guard the cache:

* ``in_atomic_block`` and the savepoint stack, which catch entering or leaving a
  transaction and a rollback to a savepoint.
* A per-transaction marker, because the two above are *shapes*, not identities: two
  sibling ``atomic()`` blocks at the same depth look identical, so a value published
  transaction-locally in the first would otherwise be assumed still live in the second --
  after the commit that discarded it. See ``_transaction_marker``.

None of this is incidental complexity: every guard here corresponds to a way the cache
can fail open. Do not simplify it without a test that fails first.
"""

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
    from collections.abc import Iterator


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
    """The driver's SQLSTATE for *exc*, whichever driver it came from.

    psycopg names it ``sqlstate``, psycopg2 ``pgcode``. guitars pins neither, so a consumer
    picks one and Django supports both.
    """
    return getattr(exc, 'sqlstate', None) or getattr(exc, 'pgcode', None)


def _scalar(value: object) -> str:
    """One dimension value as its GUC text, refusing anything the encoding cannot carry.

    Accepts a model instance, a pk, or anything ``str()``-able -- mirroring what ``tenant()``
    already accepts and what the manager filters on.

    The refusal itself lives in :func:`~guitars.tenancy.scope.reject_separator`, which
    ``tenant()`` also calls at scope entry. See it for why a separator is refused rather than
    escaped, and why this later call is not redundant with the earlier one -- in short, a pk
    that was ``None`` when the scope opened can still acquire one before it is published, and
    this is the boundary the policy actually reads.
    """
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


def _fingerprint(connection) -> tuple:
    # savepoint_ids shrinks on ROLLBACK TO SAVEPOINT, which also reverts any SET made
    # after that savepoint -- so its shape is part of the signal we need.
    return (connection.in_atomic_block, tuple(connection.savepoint_ids))


def _transaction_marker(connection, superseded=None):
    """Register a no-op commit hook whose *presence* identifies this transaction.

    Django exposes no transaction counter, and the fingerprint above only describes the
    current transaction's shape -- sibling ``atomic()`` blocks at the same depth are
    indistinguishable by it. ``run_on_commit`` is the one piece of per-transaction state
    Django reliably discards for us: it is emptied on commit and on rollback, and entries
    registered inside a savepoint are dropped when that savepoint rolls back. So "our
    marker is still registered" means exactly "the transaction that published is the one
    we are still in".

    A ``superseded`` marker is replaced *in place*, never filtered out: removing an entry
    renumbers everything after it, and Django's ``TestCase.captureOnCommitCallbacks``
    slices ``run_on_commit`` from a start index saved when the capture opened -- so
    dropping a pre-capture entry mid-window silently disowns a consumer callback
    registered inside the window. In-place replacement also keeps the list at one marker
    per transaction, so a capture window is never padded with stale no-ops (a loop
    switching tenants inside one ``atomic()`` would otherwise leave one per switch).

    ``using`` is mandatory here, not cosmetic: ``on_commit`` defaults to the *default*
    alias, so on any other connection the marker would land on the wrong list -- or run
    immediately, if the default alias happens to be in autocommit -- and this connection's
    cache would then never look live.
    """

    def marker() -> None:
        """Never does anything -- only its presence carries information."""

    if superseded is not None:
        # Mirror ``on_commit``'s entry shape: (active savepoint ids, func, robust).
        # Fresh savepoint ids, not the superseded entry's: this marker vouches for the
        # ``SET LOCAL`` just issued, which a rollback of a savepoint active *now* would
        # revert. A dead ``superseded`` (transaction already ended, or its savepoint
        # rolled back) simply isn't found, and we fall through to a plain registration.
        entry = (set(connection.savepoint_ids), marker, False)
        for index, existing in enumerate(connection.run_on_commit):
            if existing[1] is superseded:
                connection.run_on_commit[index] = entry
                return marker
    transaction.on_commit(marker, using=connection.alias)
    return marker


def _marker_live(connection, marker) -> bool:
    if marker is None:  # published at session level; no transaction to outlive
        return True
    return any(hook is marker for _, hook, *_ in connection.run_on_commit)


def _publish(connection, state: dict[str, str]) -> None:
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
    params: list[object] = []
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
    """Walk *exc*'s cause/context chain, cycle-safe.

    Both links are followed: ``__cause__`` for an explicit ``raise ... from``, which is
    what Django uses, and ``__context__`` for an error re-raised inside another ``except``
    block without one. Tracked by identity -- a chain can cycle, and an exception free to
    define ``__eq__`` would otherwise let two distinct links look like the same one.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _aborted_transaction(exc: BaseException) -> bool:
    """Whether *exc* is "the transaction is already aborted", anywhere in its chain."""
    return any(_sqlstate(link) == _ABORTED_SQLSTATE for link in _walk_chain(exc))


def _ensure(connection) -> None:
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
        # PostgreSQL has already failed this transaction and refuses every statement until
        # it is rolled back -- including, crucially, the ROLLBACK TO SAVEPOINT that Django
        # issues to recover. That rollback goes through connection.cursor(), so it reaches
        # this wrapper first; raising here would block the only statement that can clear the
        # error, wedging the connection permanently and masking the original failure.
        #
        # Skipping cannot fail open. The cache is left untouched, so the next statement
        # republishes; and while the transaction is aborted no statement can execute at all,
        # so there is no window in which a query runs against stale settings. Once the
        # rollback lands, transaction-local settings are reverted anyway and the fingerprint
        # and marker checks above force a fresh publish.
        return


def _rls_violation(exc: BaseException) -> BaseException | None:
    """The RLS-violating error in ``exc``'s chain, if any.

    Django wraps backend errors, so the driver error carrying ``sqlstate`` is usually a
    link down the chain rather than the exception we are handed -- see :func:`_walk_chain`.

    The message test is what separates an RLS rejection from an ordinary
    ``permission denied``, which shares SQLSTATE 42501. It is English-only: on a server
    with a non-English ``lc_messages`` the rejection stays a raw ``ProgrammingError``
    instead of a ``TenantScopeViolation``. That degrades the message, not the enforcement
    -- the write is refused either way -- and PostgreSQL offers no distinct SQLSTATE or
    diagnostic field to key on instead.
    """
    for link in _walk_chain(exc):
        if _sqlstate(link) == _RLS_SQLSTATE and 'row-level security' in str(link).lower():
            return link
    return None


def _wrapper(execute, sql, params, many, context):
    connection = context['connection']
    # Re-entrancy guard: _publish issues SQL of its own through this same path.
    if not getattr(connection, _SYNCING, False):
        _ensure(connection)
    try:
        return execute(sql, params, many, context)
    except Exception as exc:
        violation = _rls_violation(exc)
        if violation is None:
            raise
        # Postgres names the table but not the tenant, and the traceback points at the
        # cursor rather than the caller. Re-raise as the error the rest of the codebase
        # already catches and greps for. TenantScopeViolation, not TenantScopeMissing:
        # this is the layer of last resort -- joins, cascades, _base_manager, raw SQL --
        # so a rejection here may mean no scope was ever opened in Python at all, not
        # only a wrong one. It is filed as a violation anyway because both raise through
        # the same TenantScopeError base a caller can catch either way, and "the database
        # refused this write" reads as an alerting signal here regardless of which.
        raise TenantScopeViolation(
            f'write rejected by a tenant policy -- the row does not belong to the '
            f'active tenant, or no tenant scope is active -- {remediation("write")} '
            f'Database said: {violation}'
        ) from exc


def install_on(connection) -> None:
    """Attach the wrapper to one connection and forget its cached GUC state."""
    if hasattr(connection, _CACHE):
        delattr(connection, _CACHE)
    if _wrapper not in connection.execute_wrappers:
        connection.execute_wrappers.append(_wrapper)


def _on_connection_created(sender, connection, **kwargs) -> None:
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
