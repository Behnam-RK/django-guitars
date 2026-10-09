# 0041 — a cycle of cascade keys is enforced, as arms of the owners' triggers

- **Status:** accepted
- **Date:** 2026-10-09
- **Affects:** `introspection.classify_cascade`, `CascadeKind`, `_cascade_candidates`, `cascade_plan`
- **Amends:** [ADR 0018](0018-self-referential-cascade-trigger.md), [ADR 0025](0025-joined-cascade-rule.md), [ADR 0039](0039-cascade-arms-in-the-owner-trigger.md)

## Context

A cascade was an `ON UPDATE` rule, and a cycle of those is rewritten into itself: PostgreSQL rejects every `UPDATE` of every table on it, guard unread. So every cascade edge on a cycle was refused (a warning, no rule, `SoftDeleteUnsupportedError` from `soft_delete()`), and a self-referential key was given a trigger of its own to get round it ([ADR 0018](0018-self-referential-cascade-trigger.md)). Since 2.19.0 ([ADR 0039](0039-cascade-arms-in-the-owner-trigger.md)) there is no cascade rule: each key is an arm of the owner's statement trigger, and a trigger takes no part in rewriting. The reason for the refusal was gone (#85).

## Decision

- `classify_cascade` no longer refuses a cascade key for lying on a cycle, nor a joined key into its own root (the one-node case of [ADR 0025](0025-joined-cascade-rule.md)). It takes an archive arm and a revive arm like any other. `CascadeKind.CYCLE` and the parameter that fed it are gone; `cascade_plan` reports no gap for one.
- **Owned edges keep the refusal**, read from the same graph over the whole registry, so an owned edge on a mixed cycle stays refused while the cascade edges on it are written. `hard_delete()` re-implements the owned last-owner predicate in Python and `sweepowned` follows it; neither is proven on a cycle. `rule_update_cycle_edges` keeps its name and its answer for that family.
- The self-referential trigger is unchanged by this decision.

## Why

Termination needs no depth guard. Every archive arm filters `_deleted_at IS NULL` and sets a non-null value, so each nested level must flip a still-live row. A revive arm only clears a stamp, and matches the stamp of the row restored, so revives never start an archive. A zero-row arm `UPDATE` still fires the target's trigger, which stops at its first `EXISTS` on empty transition tables.

## Consequences

**Behaviour change.** Every project that declared a cascade cycle gets arms it never had. The stamp spreads: the whole reachable component carries the first archived parent's `_deleted_at` ([ADR 0023](0023-cascade-stamps-its-parents-timestamp.md)). A revive matches that stamp, so it travels **up** the loop too: restoring `b1` restores the `a1` that was archived first, and everything else archived with them, while a row archived on its own keeps its stamp. In an acyclic chain a restore only ever travels down. A child archived in the same transaction as its parent is still revived with it ([ADR 0024](0024-inverse-cascade-revive-rules.md)), now across the loop.

**Depth.** Nesting is bounded by the path through rows, `max_stack_depth` being the only hard limit: the exposure the self trigger documents.

**Reversibility.** Making a key `SET_NULL` retires its arms as any relaxed key is. Reverting the code would need the refusal restored and the arms retired.

## Related

- [ADR 0011](0011-owner-side-soft-delete-ownership.md) · [ADR 0018](0018-self-referential-cascade-trigger.md) · [ADR 0024](0024-inverse-cascade-revive-rules.md) · [ADR 0039](0039-cascade-arms-in-the-owner-trigger.md) · #85
