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
the **target**, while the owned rule fires when an **owner** is soft-deleted. Different events.

What was *not* broken: `hard_delete()`. `_still_referenced` already walked every `ManyToOneRel` into
the target's MTI chain, so it spared the display from removal — but its Phase 1 is a plain
`self.delete()`, which fires the rule, so the display was archived all the same. The defect was
entirely in the generated SQL.

## Decision

Each rule carries one `NOT EXISTS` arm **per owning (table, column) pair** targeting the dependent.
N owning columns produce N rules of N arms.

- **Self-exclusion is keyed on the row, not the column.** Arms on the table the rule fires on carry
  `<alias>."{pk}" <> old."{pk}"`; arms on other tables carry none. Excluding only the declaring
  column's arm would let a row owning the target through two of its own columns read as its own last
  live owner and hold the target alive forever. Other tables have no row going away in this
  statement, so there is nothing there to exclude.
- **Arms come from the whole model registry**, not `LOCAL_APPS` — matching
  `introspection.rule_update_cycle_edges`, which sweeps the registry so the generator and
  `hard_delete()` cannot disagree. A live owner is live whether or not the kit generates for its
  app; excluding non-local owners would re-create this bug for third-party models.
- **A single-owner dependent renders byte-identically to 2.3.0.** Arm 0 stays spelled out in the
  template with the literal `guitars_owner` alias and `{co_owner_guards}` collapses to `''`, so its
  `[SQL:…]` identity does not move and no `DROP`+`CREATE` is emitted — the upgrade diff stays
  proportional to the bug. Later arms take `guitars_owner_1`, `guitars_owner_2`, … and are sorted by
  `(owner_table, fk_column)`, so no digest moves with registry order. Rule **names** are unchanged
  either way, so a regenerated rule replaces the same object and nothing needs retiring.
- **A tenanted co-owner over an untenanted dependent is refused.** An arm's `NOT EXISTS` is an
  ordinary `SELECT`, so a policy on a table it reads hides an out-of-tenant live owner: the guard
  reads "last owner" and stamps a still-owned row — the one place the kit's guards do not fail safe.
  2.3.0 could reach that only through the table you declared the key on; an arm reaches tables the
  declaring model never names, which is what makes documenting it insufficient. The declaring
  owner's own tenancy is deliberately **not** re-examined: that shape shipped in 2.3.0, and refusing
  it now would turn a green `--check` red on upgrade.
- **A refusal over a rule that already exists fails `--check`.** The other refusals only ever fire
  on relations that never had a rule; this one can fire on one that does, and refusing emits
  nothing, so the stale rule would stay live and wrong under a green `--check`. Escalated to a
  stderr `ERROR` naming the `DROP RULE` to run — no command retires a rule.

## Why not the alternatives

**A statement-level trigger** — the obvious reading of ADR 0011:64, chosen and then reversed during
planning. It closes one more hole, the per-**statement** limit where one multi-row `UPDATE`
soft-deleting two owners leaves the dependent alive. But a transition table carries rows from one
statement on one table, so it does **not** subsume the arms: cross-owner is always two statements on
two tables. The trigger is additive, not a replacement. Against it: a whole new operation family
(header, derived scanner, scanning container, name family, corpus entry); rule-retirement machinery
that does not exist, since a 2.3.0 rule left live beside a trigger stamps exactly where the trigger
spares; and owned edges leaving `introspection._rule_update_edges`, silently changing both which
cascade relations get refused *and* what `hard_delete()` follows. All for a hole that **fails safe**,
in a release blocking a consumer. The per-statement limit stays open — 0011:64's surviving half.

**Widening the guard to every inbound foreign key**, converging on the breadth `_still_referenced`
already has. Rejected on a defect that does not survive contact: a referrer table without
`_deleted_at` has no column to read liveness from, so one such table would pin the dependent
un-archivable forever, silently. The SQL/Python asymmetry is principled, not drift — the Python
guard omits the `_deleted_at` filter on purpose because it *removes* rows and must survive the
foreign-key check at `COMMIT`, while the rule only stamps a column and has no integrity stake. The
rule asks "is this still **owned**"; `hard_delete()` asks "can this be **removed**".

**Cross-app migration dependency edges.** An arm makes a rule reference another app's table, and
PostgreSQL resolves table references when it parses a rule action. Not added: the cascade family has
referenced foreign-app tables without an edge since 0.x, so this is no new class of hazard. Written
down in [`docs/migrations.md`](../migrations.md) rather than left implicit.

## Consequences

**Accepted costs.**

- A rule's text now depends on models in packages the kit does not generate for, so upgrading such a package can move its `[SQL:…]` identity. Intended, not drift.
- A scoped `makeguitarmigrations B --check` can exit green over a stale rule in app A, arms being registry-wide while A is never re-derived. Warned by `_scoped_owned_gap_notes`, the twin of `_scoped_cascade_gap_notes`, and not escalated: an unscoped run — what CI runs — re-derives every rule.
- A co-owner keeping `_deleted_at` on an MTI ancestor contributes **no** arm, the arm needing a join the template has no shape for. Such an owner is already refused a rule of its own and reported, so the gap is visible rather than silent.
- Every arm is a lookup on that owner's foreign-key column; an unindexed one makes each soft delete a sequential scan of that table.
- One shape goes green-to-red on upgrade: two owners where a co-owner is tenanted and the dependent is not. That is the tenancy refusal working.

**Reversibility.** Low, exactly as ADR 0011 describes: the SQL is inlined, so an existing database keeps whatever it was migrated with until a regeneration and a `migrate`.

## Related

- [ADR 0011](0011-owner-side-soft-delete-ownership.md) — the decision this amends
- [`docs/owned-relations.md`](../owned-relations.md) — the feature guide
- [ADR 0006](0006-inline-generated-migration-sql.md) — why the SQL is inlined, which is what makes byte-identity worth keeping
