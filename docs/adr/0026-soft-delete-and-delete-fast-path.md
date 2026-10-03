# 0026 — `soft_delete()` and a transparent `delete()` fast path

- **Status:** accepted
- **Date:** 2026-10-02
- **Affects:** `LiveQuerySet.delete`, `SoftDeletableModel.delete`, `soft_delete()`, `GUITARS_DELETE_FAST_PATH`

## Context

Django's deletion `Collector` runs in Python before the rules do. For a soft-deletable model that work is redundant, and where an MTI child has an incoming non-`DO_NOTHING` key it costs one parent read per row (#55). On a clean tree the collector is already constant-time, so the cost is real only for that shape. The options on the table: patch the collector, add an explicit method only, or change `.delete()` only.

## Decision

Both of the last two. **`soft_delete()`** is the opt-in: it reads the keys then `UPDATE`s by key, returns rows stamped, skips `on_delete` and signals, and raises where the rules alone leave rows live. **`.delete()`** reads the keys, then `DELETE`s by key (`_raw_delete`) so the existing rule rewrites it, when nothing is lost by it, returning `(0, {})` and clearing `pk` exactly as before. Both read eligibility from `guitars.models._cascade_coverage`, which walks the collector's own relations through `introspection.classify_cascade`, the predicate the generator writes rules by.

## Why

- **Why not only change `.delete()`?** It must stay faithful, so it declines for a receiver, `SET_NULL`, a plain child or a self-referential tree. Those are the cases an app with signals is in, so `soft_delete()` is the only fix there.
- **Why not only `soft_delete()`?** Every existing `.delete()` caller would keep the cost.
- **Why `_raw_delete`, not `UPDATE`.** It runs the SQL `.delete()` ends in, so an MTI child needs no root resolution (the redirect rule does it) and the stamp is `NOW()`. `soft_delete()` writes `NOW()` itself: Django's `Now()` renders `STATEMENT_TIMESTAMP()`, which a revive would not match.
- **Why the keys first.** A rule's cascade runs before the statement that fired it, so a `WHERE` reading what the cascade changes would skip the parent, and an aggregate in it would fail. The collector reads first too.
- **Strongest objection.** A clean `.delete()` is no longer the collector, so a divergence is possible. A parity test found one (a self-referential tree) and an independent review found four more: the `WHERE` hazard, an aggregate in it, a leaf instance's return value, and a `to_field` key.

## Consequences

**Accepted costs.** Eligibility is read from the registry and assumes the enforcement migrations are applied; a database behind them is already red under `makemigrations --check`. Receivers are checked per call. It is two statements, not one. `Model.soft_delete()` costs one `SELECT` to refresh the stamps. **It assumes a consistent tree:** the rules cascade only through rows that flip, so a live row under an archived ancestor is left live where the collector would reach it. Documented, not enforced. The setting is the escape hatch.

**Reversibility.** `GUITARS_DELETE_FAST_PATH = False` restores the collector. Removing `soft_delete()` would be an API break.

## Related

- [`soft_delete()` and the fast path](../soft-delete-api.md) · [ADR 0024](0024-inverse-cascade-revive-rules.md) · [ADR 0025](0025-joined-cascade-rule.md)
