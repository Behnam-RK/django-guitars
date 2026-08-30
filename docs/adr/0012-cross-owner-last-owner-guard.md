# 0012 — the last-owner guard reads every owning column, not just its own

- **Status:** accepted — implemented in 2.4.0; its rejection of a statement-level trigger is superseded by [ADR 0014](0014-statement-level-owned-sweep.md) (2.6.0). The rest stands.
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

- **Self-exclusion is keyed on the row, and on the table liveness is read from.** An arm reading the
  table the rule fires on carries `<alias>."{pk}" <> old."{pk}"`; one reading the dependent's carries
  `<alias>."{pk}" <> old."{fk}"` — a row owning the target through two of its own columns, or a
  target owning itself, would otherwise be its own last live owner and hold it alive for ever. For a
  joined arm that table and alias are the *ancestor's*, which is what it matches one row per.
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
  declares, a dimension traversing a relation filtering nothing — `policy_dimensions`, asked of
  *both* tables a joined arm reads and with opposite defaults outside `LOCAL_APPS` on its adding and
  subtracting sides. The declaring owner's own tenancy is **not** re-examined: it shipped in 2.3.0.
- **A refusal over a rule that already exists fails `--check`.** The other refusals only ever fire
  on relations that never had a rule; this one can fire on one that does, and refusing emits
  nothing, so the stale rule would stay live and wrong under a green `--check`. Escalated to a
  stderr `ERROR` naming the `DROP RULE` to run — no command retires a rule.

## Why not the alternatives

**A statement-level trigger** — the obvious reading of ADR 0011:64, chosen and then reversed during
planning; **reversed again in 2.6.0**, see [ADR 0014](0014-statement-level-owned-sweep.md). It closes
the per-**statement** limit, but a transition table carries rows from one statement on one table, so it
does **not** subsume the arms: cross-owner is always two statements on two tables. Additive, not a
replacement — and against it: a whole new operation family; rule-retirement machinery that does not
exist; and owned edges leaving `introspection._rule_update_edges`, silently changing which cascade
relations get refused *and* what `hard_delete()` follows. All for a hole that **fails safe** — in
*direction* only, nothing stamping the dependent later either, which is what #40 established.

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
- An owner with **no** `_deleted_at` at all contributes no arm either, there being no column to read liveness from — so a target it holds can be archived while it stays permanently live. That relation is already reported as a misconfiguration and `hard_delete()` still spares the row, its guard counting any referrer; it predates the arms.
- Every arm is a lookup on that owner's foreign-key column; an unindexed one makes each soft delete a sequential scan of that table.
- One shape goes green-to-red on upgrade: two owners where a policy on a co-owner's table filters on a dimension the dependent's does not. That is the tenancy refusal working.
- `hard_delete()`'s candidate test (`models.soft_deletion._owned_fields`) did not mirror this refusal when 2.4.0 shipped, so it followed a relation the generator refuses and removed a row a live out-of-tenant co-owner still referenced. **Closed in 2.4.1**: the decision moved to `introspection.owned_tenancy_refusals`, beside `rule_update_cycle_edges`, and both sides read it. The generator still owns the *message*; only the verdict is shared.

**Reversibility.** Low, exactly as ADR 0011 describes: the SQL is inlined, so an existing database keeps whatever it was migrated with until a regeneration and a `migrate`.

## Related

- [ADR 0011](0011-owner-side-soft-delete-ownership.md) — the decision this amends
- [`docs/owned-relations.md`](../owned-relations.md) — the feature guide
- [ADR 0006](0006-inline-generated-migration-sql.md) — why the SQL is inlined, which is what makes byte-identity worth keeping
