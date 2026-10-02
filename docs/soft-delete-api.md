# `soft_delete()` and the `delete()` fast path

Both exist because Django's deletion `Collector` runs in Python before the rules do. For a soft-deletable model that work is redundant, and for an MTI child it can cost a query per row (#55).

## When `.delete()` actually has an N+1

On Django 5.0–6.0 a clean tree is a constant number of `DELETE … IN (…)`: the collector fast-deletes what has no listeners and no incoming key. The per-row parent read (`SELECT … WHERE id = N LIMIT 21`) appears when an MTI child has an **incoming non-`DO_NOTHING` key** and no signal receiver, so its rows are loaded with `.only()` and each parent is fetched singly. A model with a delete receiver also loads, but without that read.

## `QuerySet.soft_delete()` and `Model.soft_delete()`

One `UPDATE … SET _deleted_at = NOW() WHERE _deleted_at IS NULL`; the rules cascade it. An MTI child queryset stamps its ancestor's row. A constant number of statements however large the tree.

- **Returns the number of rows stamped**, not `.delete()`'s tuple. `0` for rows already archived, which keep their stamp.
- **Skips Python.** No `on_delete` (`SET_NULL`, `PROTECT`), no `pre_delete`/`post_delete`, and plain children (no `_deleted_at`, such as an M2M through row) are left in place. Keep `.delete()` where you need those.
- **Raises `SoftDeleteUnsupportedError`** where a rule-only archive would leave rows **live** under an archived parent: a `GenericRelation`, a cycle-refused or unenforced edge, a model routed off PostgreSQL. It names the edge.
- `Model.soft_delete()` keeps the pk and sets `_deleted_at`/`_updated_at` from the database (one `SELECT` after the `UPDATE`). `asoft_delete()` twins both.
- Unreachable from a manager (`Model.objects.soft_delete()` would archive the table), and denied on an unscoped tenant queryset as `update()` is.

## The transparent `.delete()` fast path

When the tree is fully covered, `.delete()` issues one `DELETE` that the existing rule rewrites. It returns `(0, {})` and clears `pk`, exactly as before, and runs Django's own guards (sliced, `values()`, `distinct(*fields)`, combined) first. It **declines**, running the collector unchanged, when any of these holds:

| Reached model has… | Why the collector must run |
| --- | --- |
| a `pre_delete`/`post_delete` receiver (checked per call) | it sends the signals |
| `SET_NULL`, `PROTECT`, `RESTRICT`, `SET(…)` | it applies them |
| a plain child or M2M through row | it removes them |
| a `GenericRelation`, or an edge with no rule | rows would stay live |
| a self-referential key | `_updated_at` below level one moves only under the collector |
| a non-PostgreSQL alias | the rules are PostgreSQL DDL |

`GUITARS_DELETE_FAST_PATH = False` turns it off. Eligibility is read off the registry through the same predicate the generator writes rules by, and assumes the enforcement migrations are applied. See [ADR 0026](adr/0026-soft-delete-and-delete-fast-path.md).

## Related

- [Soft deletion](soft-deletion.md) · [MTI](mti.md) · [Tenancy](tenancy.md) · [API reference](api-reference.md)
