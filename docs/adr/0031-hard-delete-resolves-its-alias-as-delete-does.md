# 0031 — `hard_delete()` resolves its database as `delete()` does

- **Status:** accepted
- **Date:** 2026-10-07
- **Affects:** `SoftDeletableModel.hard_delete`, `HardDeletableQuerySet.hard_delete`, `_hard_delete_own_table`, `_write_alias`

## Context

`hard_delete()` is two phases in one transaction: Phase 1 is the instance's own `delete()`, which archives through the rules, and Phase 2 reads the tree and removes it under a transaction-local switch. Through 2.14.2 the two phases could pick different databases:

- the instance form used `_state.db`, while Phase 1's `delete()` asks `router.db_for_write(instance=self)`, which consults the router **before** `_state.db`. An instance read from one alias, under a router writing to another, was archived there and then removed with its tree where it was read. An instance built as `Model(pk=...)` has no `_state.db`, so its walk read and removed its tree on the read alias;
- the queryset forms used `self.db`, the **read** alias, and asked the router for it again for the `atomic()`. A split router had rows deleted on the replica; a router answering differently on each call left the switch's cursor in autocommit, so the rows were archived rather than removed.

Found across rounds 2–4 of the review loop on #69, the first while #69 itself briefly made the split worse.

## Decision

- The instance form resolves `using = router.db_for_write(self.__class__, instance=self)`, the same call `delete()` makes, and every read, the switch and every `DELETE` of Phase 2 use it. Phase 1 still calls `self.delete()` with no arguments, which asks the router again, so an override without a `using` parameter keeps working.
- The queryset forms resolve `_write_alias(self)` once, as `delete()`'s fast path and `soft_delete()` do; an explicit `.using()` still wins. The MTI form reads its keys there too.

## Why

- **One database per walk.** A walk whose phases land on different aliases archives in one place and removes in another, and nothing raises. Resolving the same way removes the question for any router that answers the same twice.
- **`delete()`'s answer, not a new one.** Phase 1 *is* `delete()`, so any other resolution can disagree with it. The router-first order is Django's own documented precedence.
- **Rejected: `_state.db` first.** It keeps a loaded instance where it was read, but only for Phase 2; Phase 1 would still go where the router says.
- **Strongest objection.** Phase 1 asks the router a second time, so a write router answering differently between the two calls still splits the walk: an alternating router had an unrelated row sharing the pk archived on the other alias. Passing `using` to `self.delete()` would close that and break every `delete()` override taking no `using`. Write routers are expected to answer the same question the same way, as Django's own `QuerySet.update()` assumes when it asks twice, so the override wins.

## Consequences

**Accepted costs.** Under a router, an instance loaded from one alias is now hard-deleted where the router writes, not where it was read. That matches `delete()`, but it is a change for anyone who relied on the read alias.

**Reversibility.** Going back would restore the split, a silent partial result. Without a router nothing changed: `db_for_write` falls back to `_state.db`, then `default`.

## Related

- [ADR 0022](0022-router-gated-enforcement.md) · [`soft-deletion.md`](../soft-deletion.md) · #69
