# 0040 — the MTI parent trigger skips an ancestor this transaction already stamped

- **Status:** accepted
- **Date:** 2026-10-09
- **Affects:** `set_parent_updated_at()`, `_CREATE_PARENT_UPDATED_AT_TRIGGER_FUNCTION`, `_ensure_function_migration`
- **Amends:** [ADR 0038](0038-updated-at-is-a-row-trigger.md)

## Context

[ADR 0038](0038-updated-at-is-a-row-trigger.md) left the MTI parent trigger alone: a row trigger on a child table cannot assign `NEW._updated_at` on the ancestor, so `set_parent_updated_at()` stays `AFTER UPDATE … FOR EACH STATEMENT` with one dynamic `UPDATE` of the ancestor (#84). Since 2.19.0 that follow-up was the one place the kit wrote a row twice. A full `save()` of a child makes it so: Django, inside `transaction.atomic`, updates the ancestor first, whose row trigger stamps the transaction's `NOW()`, and the follow-up then rewrote the row with the same value. Measured as ancestor tuple updates per `save()`: 2 before, 1 after. A child-only `update_fields` save never touches the ancestor in Django, so there the follow-up is the only write.

Alternatives: one static function per child, which PL/pgSQL could plan once per session; keeping the follow-up and accepting the second write.

## Decision

- The follow-up's `WHERE` gains `AND _updated_at IS DISTINCT FROM NOW()`, in all three argument-count branches (the pre-2.0.0 three-argument form, the unqualified four-argument form and the schema-qualified one). A row already stamped by this transaction is left alone; it would have been rewritten with the same value.
- **The function body is replaced in place** (`CREATE OR REPLACE FUNCTION set_parent_updated_at()`). Its signature, its arguments and every trigger calling it are unchanged, so no child table is migrated or locked and no `[SQL:…]` identity but the function's moves. The new body is a private constant; the public `*_PARENT_UPDATED_AT_TRIGGER_FUNCTION` names stay as migrations written before 1.1.0 read them.
- The replace's reverse puts the old body back (`restore`), not `DROP FUNCTION`, which fails while a trigger depends on it.
- **The `WHEN (pg_trigger_depth() = 0)` stays.** No path of the kit updates a child table at depth 1 or more and leaves its ancestor stale: archive arms write `_deleted_at`, which lives with `_updated_at` (the shape where it does not is `guitars.E003`).

## Why

The guard is semantically a no-op, so it needs no opt-in and no new object to retire on a rename or drop. A per-child function would also drop the per-call planning, but a plpgsql body records no dependency, so each would need its own name, host, rename and retirement handling, for a statement of one probe and, now, usually no write. If planning proves to matter, it is a separate issue.

## Consequences

**Accepted costs.** The dynamic `EXECUTE` is still planned on every statement. A user trigger updating a child table at depth 1 or more leaves its ancestor's `_updated_at` as it was. The reverse restores the body the frozen constant holds, which is the one before this change and no later one.

**Reversibility.** Unapplying the one migration puts the old body back.

## Related

- [ADR 0038](0038-updated-at-is-a-row-trigger.md) · [ADR 0039](0039-cascade-arms-in-the-owner-trigger.md) · #80 · #84
