# `soft_delete()` and the `delete()` fast path

Both exist because Django's deletion `Collector` runs in Python before the rules do. For a soft-deletable model that work is redundant, and for an MTI child it can cost a query per row (#55).

## When `.delete()` actually has an N+1

On Django 5.0–6.0 a clean tree is a constant number of `DELETE … IN (…)`: the collector fast-deletes what has no listeners and no incoming key. The per-row parent read (`SELECT … WHERE id = N LIMIT 21`) appears when an MTI child has an **incoming non-`DO_NOTHING` key** and no signal receiver, so its rows are loaded with `.only()` and each parent is fetched singly. A receiver on the *child* makes Django load its rows whole, which avoids that read; one on the *parent* does not.

## `QuerySet.soft_delete()` and `Model.soft_delete()`

It reads the matching keys, then `UPDATE`s by key in batches of 10,000: `SET _deleted_at = NOW()`, and the rules cascade it. An MTI child queryset stamps its ancestor's row. That is `1 + ceil(n / 10,000)` statements for `n` rows however large the tree, in one transaction: a failing batch archives nothing, and every batch stamps the same instant. The keys come first because a rule's cascade runs *before* the statement that fired it: a `WHERE` that reads what the cascade changes would skip the parent, and an aggregate in a `WHERE` would fail outright.

- **Returns the number of rows stamped**, not `.delete()`'s tuple. `0` for rows already archived, which keep their stamp.
- **Skips Python.** No `on_delete` (`SET_NULL`, `PROTECT`), no `pre_delete`/`post_delete`, and plain children (no `_deleted_at`, such as an M2M through row) are left in place. Keep `.delete()` where you need those.
- **Raises `SoftDeleteUnsupportedError`** where a rule-only archive would leave rows **live** under an archived parent: a `GenericRelation`, a cycle-refused or unenforced edge, a `to_field` key (the cascade rule compares the target's primary key, so it would match the wrong rows), a model routed off PostgreSQL, or a non-PostgreSQL connection. It names the edge or the connection. A sliced, `values()` or `distinct(*fields)` queryset raises `TypeError`, and a combined one `NotSupportedError`, as for `.delete()`.
- `Model.soft_delete()` keeps the pk and sets `_deleted_at`/`_updated_at` from the database (one `SELECT` after the `UPDATE`); a row hidden by row-level security stamps 0 and leaves the instance unchanged. `asoft_delete()` twins both.
- Unreachable from a manager (`Model.objects.soft_delete()` would archive the table), and denied on an unscoped tenant queryset as `update()` is.

## The transparent `.delete()` fast path

When the tree is fully covered, `.delete()` reads the keys, then issues `DELETE`s by key that the existing rule rewrites. It returns `(0, {})` and clears `pk`, exactly as before (a single instance of a model nothing depends on returns `(0, {'app.Model': 0})`, as Django's own shortcut does; a queryset always returns `(0, {})`), and runs Django's guards first. It **declines**, running the collector unchanged, when any of these holds:

| Reached model has… | Why the collector must run |
| --- | --- |
| a `pre_delete`/`post_delete` receiver (checked per call) | it sends the signals |
| `SET_NULL`, `PROTECT`, `RESTRICT`, `SET(…)` | it applies them |
| a plain child or M2M through row | it removes them |
| a `GenericRelation`, a `to_field` key, or an edge with no rule | rows would stay live |
| a self-referential key | `_updated_at` below level one moves only under the collector |
| a non-PostgreSQL alias | the rules are PostgreSQL DDL |

## What both assume

The cascade passes only through rows that *flip* to archived, so a **live row under an already-archived ancestor** (say a child created through `_all_objects` beneath an archived parent) is reached by the collector and left live by the rules. Both assume the tree is consistent, and that the enforcement migrations are applied.

Both read the keys with a plain `SELECT`, so on a very large table the planner needs current statistics (`ANALYZE`) to choose an index for the `IN (…)` batches. `GUITARS_DELETE_FAST_PATH = False` turns the fast path off. Eligibility is read off the registry through the same predicate the generator writes rules by. See [ADR 0026](adr/0026-soft-delete-and-delete-fast-path.md).

## Related

- [Soft deletion](soft-deletion.md) · [MTI](mti.md) · [Tenancy](tenancy.md) · [API reference](api-reference.md)
