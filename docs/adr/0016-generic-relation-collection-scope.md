# 0016 — hard deletion collects generic children wherever their row is going

- **Status:** accepted — implemented in 2.7.0
- **Affects:** `guitars.models.SoftDeletableModel.hard_delete`, [ADR 0011](0011-owner-side-soft-delete-ownership.md)
- **Date:** 2026-09-01

## Context

`hard_delete()` runs in two phases: Phase 1 soft-deletes through Django's `Collector`, letting
the rules cascade; Phase 2 walks the same shape itself and removes the rows. 2.7.0 taught Phase 2
to follow a `GenericRelation`, which it had never seen — the child has no key column, so
`_referring_relations` cannot find it, and it was archived by Phase 1 and then left pointing at a
primary key nothing held.

The obvious way to write that is "collect what Phase 1 archived". It is not what the code does,
and the two cannot be made equal:

- `Collector.collect()` recurses into a multi-table-inheritance ancestor with
  `collect_related=False`, and returns on that flag (`django/db/models/deletion.py:304`) *before*
  its own `private_fields` walk (`:364`). So deleting through a child never asks the ancestor's
  `GenericRelation`. Phase 2 enters at `mti_root` and walks down, so it asks every level.
- An owned target is stamped by the owned rule, in the database. No `Collector` runs for it at
  all, so nothing of its generic children is ever archived.

A first reading of this called it a defect: Phase 2 destroying a row Phase 1 spared is the
direction the kit treats as severe.

## Decision

**Keep the wider walk**, and say so where the code says anything at all.

Phase 2 takes a generic child wherever the row it points at is going. That is every level this
walk reaches, because sparing happens earlier: `_owned_targets` decides which targets survive,
and `_collect_group` only then walks `private_fields`. A spared target never has its generic
children collected.

## Why

The severe-direction reading does not survive the check. In every path Phase 2 reaches, the row
the generic child points at *is being removed* — so taking the child with it prevents a dangling
`object_id`, rather than destroying a row that survives. There is no path where a generic child
is removed while its referent lives.

Matching Phase 1 exactly is the alternative that looks safer and is not. It would leave an MTI
ancestor's generic children live and pointing at a removed primary key — the pre-2.7.0 bug, moved
one level up rather than fixed. Widening Phase 1 instead (making the kit's own soft-delete reach
an ancestor's generic children) changes ordinary `.delete()` behaviour to fix a `hard_delete()`
asymmetry, which is a much larger blast radius for the same end.

Nothing constrains a `GenericRelation` at the database level, so none of this can abort a
statement. It is a choice about what a removal leaves behind, not about integrity.

## Consequences

**Accepted costs.** `hard_delete()` and `.delete()` disagree about an MTI ancestor's generic
children: the first removes them, the second leaves them live. That is deliberate — only the
first removes the row they point at — but it is a difference a reader will trip over, which is
why `_collect`'s comment states the rule rather than claiming the two walks agree.

`_referring_relations` is no longer the single walk `_collect` and `_still_referenced` share.
`_cascade_closure` does not model the generic hop, so the two can disagree — only ever toward
sparing, since a generic child holds nothing back and `taken` is only ever subtracted from the
referrer set. An owned target can therefore be left archived rather than removed, never removed
while something still points at it.

**Reversibility.** High. The walk is a dozen lines in `_collect` with no generated SQL behind it,
so narrowing it later changes no database and no migration history.

## Related

- [ADR 0011](0011-owner-side-soft-delete-ownership.md) — why `hard_delete()` re-implements the
  rule's predicate in Python · [`docs/soft-deletion.md`](../soft-deletion.md) — the two phases in
  full · [`docs/owned-relations.md`](../owned-relations.md) — the sparing half
