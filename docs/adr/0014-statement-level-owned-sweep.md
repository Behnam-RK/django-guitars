# 0014 — the last-owner guard gains a statement-level trigger beside the rule

- **Status:** accepted — implemented in 2.6.0
- **Date:** 2026-08-30
- **Affects:** `guitars.sql.soft_delete`, `makeguitarmigrations`, `sweepowned`
- **Amends:** [ADR 0012](0012-cross-owner-last-owner-guard.md) — reverses its rejection of a
  statement-level trigger, closing [ADR 0011](0011-owner-side-soft-delete-ownership.md):64's
  per-**statement** half. The rest of both stands.

## Context

A rule's action expands *before* the original update, so when one statement soft-deletes several
co-owners of one dependent, every arm still reads its siblings as live and nothing stamps the
dependent. ADR 0011 recorded this as an accepted limit and ADR 0012 re-affirmed it — "a hole that
**fails safe**". Issue #40, from the consumer whose `bundle.pdp_display`/`shop.default_pdp_display`
pair motivated 0012, asked for it to be closed. Two things in the report change the weighing:

- **The hole is permanent, not deferred.** Once the last owner is archived nothing will ever stamp
  the dependent again. "Fails safe" holds of the *direction* — a leaked live row rather than an
  archived owned one — but the state is wrong for ever, and a dependent mirroring an external
  resource leaves that side unreclaimed.
- **Ordinary code walks into it.** `QuerySet.delete()` is how Django deletes, and nothing at the
  declaration site distinguishes it from a loop. `Collector` also batches by model on its own, so a
  cascade from a third model reaches the same statement without anyone writing a queryset delete.

## Decision

**Each owned rule is paired with an `AFTER UPDATE ... FOR EACH STATEMENT` trigger** re-asking the
same arms once the statement has settled, over an `OLD TABLE`/`NEW TABLE` transition-table join.

- **Additive, never a replacement.** A statement that archives owners never creates one, so the
  rule stamps a strict subset of what the sweep does and whichever runs first the other's
  `_deleted_at IS NULL` makes it a no-op — nothing retired, so 0012's missing machinery is moot.
- **Emitted from inside `_owned_operations`' own loop**, on the same key and after every refusal
  that loop applies, so which relations carry a sweep is not a second answer — it *is* the rule's,
  and `_rule_update_edges`, `rule_update_cycle_edges`, `owned_tenancy_refusals` and `hard_delete()`
  are untouched. That owned edges would leave that graph was 0012's strongest objection.
- **The arms are spliced in verbatim**, the archived owners aliased into a subquery the templates
  read as `{owner_row}` — the rule passes the literal `old`, so every existing rule still renders
  byte-identically and no `[SQL:…]` identity moves. The alias may not itself *be* `old`, a plpgsql
  record variable PostgreSQL will not let it shadow. It selects the **before** image, so the key it
  reads is `old`'s: off the after image, archiving an owner *and* moving its key in one statement
  stamped the newly-pointed-at row, skipped the held one, and broke the subset.
- **No `WHEN (pg_trigger_depth() = 0)`**, unlike the `updated_at` trigger it mirrors: the sweep's
  `UPDATE` must fire the dependent's sweep in turn, or a chain stops a hop short. Recursion ends on
  `_deleted_at IS NULL`, and a cycle is refused a rule — so a trigger — before either. Running at
  depth 1 is also why it stamps `_updated_at` itself: that trigger's `WHEN` suppresses it there.
- **The function's name sizes one segment more than the rule's.** A rule is namespaced per table, a
  function per schema, and two owner tables share a `(dependent, fk)` pair — `Kiosk` and `Foyer`
  both own `Placard` through `placard_id`, where the rule's spelling would have the second
  `CREATE OR REPLACE FUNCTION` overwrite the first's body.
- **No cross-app dependency edges of its own.** `CREATE TRIGGER` names only the table it fires on
  and PL/pgSQL resolves no function body at `CREATE FUNCTION` time, so nothing here is the
  parse-time reference [ADR 0013](0013-cross-app-migration-dependency-edges.md) exists for; the
  rule's refs, recorded for the same relation in one pass, order the runtime case anyway.
- **`sweepowned` repairs what is already lost**, the trigger reaching no database that leaked
  before it. It follows no refused relation, and none the database holds no rule for — what the
  generator would emit *today* is the wrong question. Its predicate is re-asked inside the `UPDATE`.

## Why not the alternatives

**A guard the rule could express on its own** — issue #40's option 2, excluding rows whose
`_deleted_at` the current statement is setting. Not implementable: a rule's action sees only
`old`/`new` for the current row, and is rewritten *into* the query rather than run after it.

**Widening the guard to every inbound foreign key.** Still rejected on 0012's ground: a referrer
table without `_deleted_at` has no column to read liveness from, and would pin the dependent
un-archivable for ever.

**A `QuerySet.delete()` override, or a warning** — issue #40's option 4. Per-row fallback
contradicts the thesis the kit exists for, that correctness holds on paths never touching Python;
it is O(n) statements, bypassed by raw SQL and by the `Collector` batching the report itself names.
A system check cannot see it either — the shape is a call pattern, not a declaration.

**The sweep without the command**, leaving every pre-2.6.0 database wrong with no way back.

## Consequences

**Accepted costs.**
- A new operation family: header, derived scanner, container, name family, corpus baseline — the
  bulk of the change, smaller than 0012 estimated: the owned SQL templates were private already.
- One `CREATE FUNCTION` and `CREATE TRIGGER` per owned relation, plus a statement-level trigger
  per `UPDATE` on an owner table, matching nothing where the rule already stamped the row.
- A `db_table` or column containing `$$` is **refused**, not escaped: it closes the body's dollar
  quoting and `migrate` would fail on a syntax error. The sweep alone is skipped, the rule beside
  it untouched, so only a sweep already recorded is named to drop by hand.
- `hard_delete()`'s behaviour is unchanged, its *intermediate* state is not: a target whose owners
  one `UPDATE` archived is archived in Phase 1 and removed by Phase 2, not live straight to gone.
- `sweepowned` reads with tenancy bypassed and needs a role that sees every tenant: through a
  policy a hidden live owner would manufacture an orphan, unrefusable in Python.
- The transition tables correlate on the primary key, their only row identity, so a statement
  writing an owner's **pk** while archiving it drops it from the join and the sweep misses it.

**Reversibility.** Low, as 0011 and 0012 describe: the SQL is inlined, so a database keeps what it
was migrated with until a regeneration and a `migrate`, and dropping the family later leaves every
migrated project's triggers live, no command retiring one.

## Related

- [ADR 0012](0012-cross-owner-last-owner-guard.md) — amended · [ADR 0011](0011-owner-side-soft-delete-ownership.md) — where the limit was recorded · [ADR 0013](0013-cross-app-migration-dependency-edges.md) — why this needs no edges · [`docs/owned-relations.md`](../owned-relations.md) — the guide
