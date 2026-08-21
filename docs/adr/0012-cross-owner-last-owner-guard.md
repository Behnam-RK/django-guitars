# 0012 — the last-owner guard reads every owning column, not just its own

- **Status:** accepted — implemented in 2.4.0
- **Date:** 2026-08-21
- **Affects:** `guitars.sql.soft_delete`, `makeguitarmigrations`
- **Amends:** [ADR 0011](0011-owner-side-soft-delete-ownership.md) — supersedes its per-**column** limit and the claim that lifting it needs a statement-level trigger. The rest of 0011 stands.

## Context

2.3.0 shipped the last-owner guard as one `NOT EXISTS` over the declaring owner's own column on its
own table. ADR 0011 recorded the consequence as an accepted cost — "a second `OwningForeignKey` to
the same row does not spare it" — and `docs/owned-relations.md` listed it as a limit.

It is not a limit; it is data loss, and a consumer hit it within a day of the release. Where a
dependent is owned from two places, soft-deleting the last owner *of one kind* archives it while a
live owner *of another kind* still points at it: with `bundle.pdp_display` and
`shop.default_pdp_display` both owning display `D`, deleting the only bundle using `D` archived it
out from under a live shop. `on_delete` cannot substitute — `RESTRICT`/`PROTECT` govern deletion of
the **target**, while the owned rule fires when an **owner** is soft-deleted. `hard_delete()` was not
broken — `_still_referenced` already walked every relation into the target's MTI chain — but Phase 1
is a plain `self.delete()`, which fires the rule, so the display was archived all the same.

## Decision

Each rule carries one `NOT EXISTS` arm **per owning (table, column) pair** targeting the dependent.
N owning columns produce N rules of N arms.

- **Self-exclusion is keyed on the row, not the column.** Arms on the table the rule fires on carry
  `<alias>."{pk}" <> old."{pk}"` — excluding only the declaring column's arm would let a row owning
  the target through two of its own columns read as its own last live owner and hold it alive
  forever. An arm on the *dependent's* table excludes `old."{fk}"` instead, a target owning itself
  otherwise reading as its own live owner; any other table has no row going away here to exclude.
- **An owner that inherits `_deleted_at` contributes a *joined* arm.** Its key is on its own table
  and its liveness on an MTI ancestor's, so the arm joins the two on the primary-key value the chain
  shares — the correlation the rule itself rests on. Reading such an owner as contributing nothing
  was a second instance of the bug above, reproducible in `tests/testapp` over one `PressKit`.
- **Arms come from the whole model registry**, not `LOCAL_APPS` — matching
  `introspection.rule_update_cycle_edges`, which sweeps it so the generator and `hard_delete()`
  cannot disagree. A live owner is live whether or not the kit generates for its app; excluding
  non-local owners would re-create this bug for third-party models.
- **A single-owner dependent renders byte-identically to 2.3.0.** Arm 0 stays spelled out in the
  template with the literal `guitars_owner` alias and `{co_owner_guards}` collapses to `''`, so its
  `[SQL:…]` identity does not move and no `DROP`+`CREATE` is emitted. Later arms take
  `guitars_owner_1`, … sorted by `(owner_table, fk_column)`, so no digest moves with registry order.
  Rule **names** are unchanged either way, so a regenerated rule replaces the same object.
- **A co-owner refused where a policy on its table filters on a dimension the dependent's does
  not.** An arm's `NOT EXISTS` is an ordinary `SELECT`, so such a policy hides an out-of-tenant live
  owner: the guard reads "last owner" and stamps a still-owned row — the one place the kit's guards
  do not fail safe. An arm reaches tables the declaring model never names, which is what makes
  documenting it insufficient. Per **dimension**, the session already being inside the ones the
  dependent's own policy filters on, and per what a policy **predicates** rather than what a manager
  declares, a dimension traversing a relation filtering nothing — `policy_dimensions`, whose two
  callers take opposite defaults outside `LOCAL_APPS`, one adding and one subtracting. The declaring
  owner's own tenancy is **not** re-examined: that shape shipped in 2.3.0.
- **A refusal over a rule that already exists fails `--check`.** The other refusals only ever fire
  on relations that never had a rule; this one can fire on one that does, and refusing emits
  nothing, so the stale rule would stay live and wrong under a green `--check`. Escalated to a
  stderr `ERROR` naming the `DROP RULE` to run — no command retires a rule.

## Why not the alternatives

**A statement-level trigger** — the obvious reading of ADR 0011:64, chosen and then reversed during
planning. It closes the per-**statement** limit, but a transition table carries rows from one
statement on one table, so it does **not** subsume the arms: cross-owner is always two statements on
two tables. Additive, not a replacement — and against it: a whole new operation family (header,
scanner, scanning container, name family, corpus entry); rule-retirement machinery that does not
exist, a 2.3.0 rule left live beside a trigger stamping exactly where the trigger spares; and owned
edges leaving `introspection._rule_update_edges`, silently changing both which cascade relations get
refused *and* what `hard_delete()` follows. All for a hole that **fails safe**, in a release blocking
a consumer — the limit stays open, 0011:64's surviving half.

**Widening the guard to every inbound foreign key**, converging on `_still_referenced`'s breadth.
Rejected on a defect that does not survive contact: a referrer table without `_deleted_at` has no
column to read liveness from, so one such table would pin the dependent un-archivable forever. The
Python guard omits that filter because it *removes* rows and must survive the deferred foreign-key
check. The rule asks "is this still **owned**"; `hard_delete()`, "can this be **removed**".

**Cross-app migration dependency edges.** An arm makes a rule reference another app's table, and
PostgreSQL resolves table references when it parses a rule action, so that table has to exist by
then. Not added: an edge into an app the models have no relation to can close a cycle in the
migration graph and brick `migrate` outright — worse than what it prevents. See `migrations.md`.

## Consequences

**Accepted costs.**

- A rule's text now depends on models in packages the kit does not generate for, so upgrading such a package can move its `[SQL:…]` identity. Intended, not drift.
- A scoped `makeguitarmigrations B --check` can exit green over a stale rule in app A, arms being registry-wide while A is never re-derived. Warned by `_scoped_owned_gap_notes`, the twin of `_scoped_cascade_gap_notes`, and not escalated: an unscoped run — what CI runs — re-derives every rule.
- An owner that inherits `_deleted_at` is *seen* by every other rule's guard but still **stamps** nothing when it is soft-deleted, its own rule being refused. So a dependent whose last owner is one of those is left live rather than archived — the fail-safe half of that refusal, and the reason the joined arm is worth having without the rule.
- Every arm is a lookup on that owner's foreign-key column; an unindexed one makes each soft delete a sequential scan of that table.
- One shape goes green-to-red on upgrade: two owners where a policy on a co-owner's table filters on a dimension the dependent's does not. That is the tenancy refusal working.
- `hard_delete()`'s candidate test (`models.soft_deletion._owned_fields`) mirrors the *other* refusals but **not** this one, so it still follows a relation the generator now refuses — and, reading the co-owner through the same policy, removes a row that co-owner still references. The foreign-key check is exempt from RLS, so that aborts the transaction at `COMMIT` rather than destroying anything: loud, and the same outcome `docs/owned-relations.md` already records for the per-visible-row limit. That guarantee is the *constraint*, not the guard, so an `OwningForeignKey(db_constraint=False)` gives it up and the removal goes through — not refused, being a legal Django option, but the one shape where this gap is silent. Known gap, not a decision: the shared answer belongs in `guitars.introspection` alongside `rule_update_cycle_edges`.

**Reversibility.** Low, exactly as ADR 0011 describes: the SQL is inlined, so an existing database keeps whatever it was migrated with until a regeneration and a `migrate`.

## Related

- [ADR 0011](0011-owner-side-soft-delete-ownership.md) — the decision this amends
- [`docs/owned-relations.md`](../owned-relations.md) — the feature guide
- [ADR 0006](0006-inline-generated-migration-sql.md) — why the SQL is inlined, which is what makes byte-identity worth keeping
