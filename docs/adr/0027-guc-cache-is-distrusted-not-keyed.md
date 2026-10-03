# 0027 — the GUC cache is distrusted after a revert, not keyed on savepoints

- **Status:** accepted
- **Date:** 2026-10-03
- **Affects:** `guitars.tenancy.guc` (`_fingerprint`, `_distrust`, `_reverts_a_set`)

## Context

The tenant policy reads `tenant.*` session settings that the execute wrapper publishes with `set_config(..., true)`, and the publisher caches what it last sent. Through 2.11.0 the cache key included `connection.savepoint_ids`, so the first statement inside every savepoint and the `RELEASE` that closed it each republished. `update()` wraps its own `atomic()`, so N calls inside one transaction cost about 2N extra round trips. Dropping the savepoint ids from the key was the obvious fix, and it exposed a worse problem: the cache stored the *desired* state, while a `SET LOCAL` is undone by anything that ends a transaction or a savepoint. A cache that believes a value the database no longer holds leaves the previous tenant live, which fails **open**.

## Decision

The key is only whether a transaction is open, plus the transaction marker. The cache is **distrusted** after any statement that begins `ROLLBACK`, `ABORT`, `COMMIT` or `END` (`... PREPARED` excluded), sent as text or bytes, and after a multi-statement string containing one. Every republish clears every dimension ever published on the connection, not only the last frame's. The matcher is a linear scanner (`_reverts_a_set`), not a regex.

## Why

- **Why not keep the savepoint ids?** A push or release reverts no `SET LOCAL`, so the key paid for events that change nothing.
- **Why distrust rather than track?** The ways a setting disappears are not all visible to Django: `transaction.savepoint_rollback()`, raw SQL, `ROLLBACK AND CHAIN`, a pooled connection checked out again. Reading the statement stream sees all of them; mirroring Django's state sees only the ones it routes.
- **Why a scanner?** An earlier regex hung on a statement with about 50 block comments. The scan is linear and has a regression test.
- **Strongest objection.** Distrust is conservative: a false positive costs one republish. A false negative fails open, so a new revert shape must add a test before it adds code.

## Consequences

**Accepted costs.** The scanner must be kept in step with PostgreSQL's transaction-ending syntax.

**Reversibility.** Restoring the savepoint ids in the key is a one-line change that costs the round trips back; nothing stored depends on it. Do not remove the distrust: it is what keeps the key from failing open.

## Related

- [Tenancy](../tenancy.md) · CHANGELOG 2.11.1
