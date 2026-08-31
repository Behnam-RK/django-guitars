# 0014 — the last-owner guard gains a statement-level trigger beside the rule

- **Status:** accepted — implemented in 2.6.0, extended in 2.7.0
- **Date:** 2026-08-30
- **Affects:** `guitars.sql.soft_delete`, `makeguitarmigrations`, `sweepowned`
- **Amends:** [ADR 0012](0012-cross-owner-last-owner-guard.md) — reverses its rejection of a
  statement-level trigger, closing [ADR 0011](0011-owner-side-soft-delete-ownership.md):64's
  per-**statement** half; the rest of both stands.

## Context

A rule's action expands *before* the original update, so when one statement soft-deletes several
co-owners of one dependent, every arm still reads its siblings as live and nothing stamps the
dependent. ADR 0011 recorded this as an accepted limit and 0012 re-affirmed it — "a hole that
**fails safe**". Issue #40, from the consumer whose `bundle.pdp_display`/`shop.default_pdp_display`
pair motivated 0012, asked for it to be closed, and changes the weighing twice. **The hole is
permanent:** once the last owner is archived nothing ever stamps the dependent again, so "fails
safe" holds of the *direction* alone while the state stays wrong for ever. **And ordinary code
walks into it:** `QuerySet.delete()` is how Django deletes, nothing at the declaration site tells
it from a loop, and `Collector` batches by model, so a third model's cascade reaches the same
statement with nobody writing a queryset delete.

## Decision

**Each owned rule is paired with an `AFTER UPDATE ... FOR EACH STATEMENT` trigger** re-asking the
same arms once the statement has settled, over an `OLD TABLE`/`NEW TABLE` transition-table join.

- **Additive, never a replacement.** A statement archiving owners creates none, so the rule stamps a
  strict subset of the sweep and whichever runs first the other's `_deleted_at IS NULL` makes it a
  no-op — nothing retired, so 0012's missing rule-retirement machinery is moot.
- **Emitted from inside `_owned_operations`' own loop**, on the same key and after every refusal it
  applies, so which relations carry a sweep *is* the rule's answer — `_rule_update_edges`,
  `rule_update_cycle_edges`, `owned_tenancy_refusals` and `hard_delete()` untouched, and owned edges
  leaving that graph was 0012's strongest objection.
- **The arms are spliced in verbatim**, the archived owners aliased into a subquery the templates
  read as `{owner_row}` — the rule passes the literal `old`, so every existing rule still renders
  byte-identically and no `[SQL:…]` identity moves. The alias may not *be* `old`, a plpgsql record
  variable PostgreSQL will not let it shadow. It selects the **before** image, so the key it reads
  is `old`'s: off the after image, archiving an owner *and* moving its key stamped the new target
  and skipped the held one, breaking the subset.
- **No `WHEN (pg_trigger_depth() = 0)`**, unlike the `updated_at` trigger it mirrors: the sweep's
  `UPDATE` must fire the dependent's sweep in turn, or a chain stops a hop short. Recursion ends on
  `_deleted_at IS NULL`, and a cycle is refused a rule — so a trigger — before either. Depth 1 is
  also why it stamps `_updated_at` itself, that trigger's `WHEN` suppressing it there. Where the
  column is an MTI **ancestor's** (2.7.0) that takes a second `UPDATE`, not a data-modifying CTE
  around the first: PostgreSQL refuses a `DO ALSO` rule inside `WITH`, which is exactly a dependent
  owning something itself. It re-reads no arm — `_deleted_at = NOW()` names what the first `UPDATE`
  stamped, and an `EXISTS` over the transition tables holds it to this statement's own owners.
- **The function's name sizes one segment more than the rule's:** a rule is namespaced per table, a
  function per schema, so `Kiosk`/`Foyer` sharing `(Placard, placard_id)` would collide in a body.
- **No cross-app dependency edges of its own:** `CREATE TRIGGER` names only its own table and
  PL/pgSQL resolves no body at `CREATE FUNCTION` time, so nothing here is a parse-time reference ([ADR 0013](0013-cross-app-migration-dependency-edges.md)); the rule's refs order the runtime case.
- **`sweepowned` repairs what is already lost**, the trigger reaching no database that leaked
  before it. It follows no refused relation, and none the database holds no rule for — what the
  generator would emit *today* is the wrong question — and re-asks its predicate inside the
  `UPDATE`. It repairs to a **fixpoint** (2.7.0): a pass walks dependents in model-label order, so a
  chain sorting against it is left a hop short, the repairing `UPDATE` firing the rule but not the
  trigger such a database lacks. Bounded by the dependent count (a chain is acyclic, a cycle being
  refused a rule); exceeding it raises, an old enough database predating that refusal.

## Why not the alternatives

**A guard the rule could express on its own** — issue #40's option 2, excluding rows whose
`_deleted_at` this statement is setting. Not implementable: a rule's action sees only `old`/`new`
for the current row, and is rewritten *into* the query rather than run after it. **Widening it to
every inbound foreign key** stays rejected on 0012's ground: a referrer without `_deleted_at` has
no column to read liveness from, and would pin the dependent for ever.

**A `QuerySet.delete()` override, or a warning** — issue #40's option 4. Per-row fallback
contradicts the thesis the kit exists for, that correctness holds on paths never touching Python;
it is O(n) statements, bypassed by raw SQL and by the `Collector` batching the report names, and a
system check sees a call pattern, not a declaration. **The sweep without the command** leaves every
pre-2.6.0 database wrong with no way back.

## Consequences

**Accepted costs.**
- A new operation family: header, scanner, container, name family, corpus baseline — the bulk of the
  change, smaller than 0012 estimated: the owned SQL templates were private already.
- One `CREATE FUNCTION`/`CREATE TRIGGER` per owned relation, plus a statement-level trigger per
  `UPDATE` on an owner table, matching nothing where the rule already stamped the row.
- A `db_table` or column containing `$$` is **refused**, not escaped: it closes the body's dollar
  quoting, so `migrate` would fail. Only the sweep is skipped, and only a recorded one named to drop.
- `hard_delete()`'s behaviour is unchanged, its *intermediate* state is not: a target whose owners
  one `UPDATE` archived is archived in Phase 1 and removed by Phase 2, not live straight to gone.
  `sweepowned` needs a role seeing every tenant, a policy hiding a live owner manufacturing an
  orphan that Python cannot refuse.
- A statement rewriting a **live owning row's own pk**, leaving its target unowned, is **refused**
  (`feature_not_supported`): the transition tables have no other row identity. Django never does.
- A statement that **permutes** primary keys among owning rows is out of scope and undetectable:
  every before-key is still in the after image, so the guard sees no vanished row and the join
  pairs each before-row with another's. `QuerySet.update()` rejects `pk=`, so this too is raw SQL.

**Reversibility.** Low, as 0011 and 0012 describe: the SQL is inlined, so a database keeps what it
was migrated with until a regeneration and a `migrate`, and dropping the family leaves every
migrated project's triggers live.

## Related

- [ADR 0012](0012-cross-owner-last-owner-guard.md) — amended · [ADR 0011](0011-owner-side-soft-delete-ownership.md) — where the limit was recorded · [ADR 0013](0013-cross-app-migration-dependency-edges.md) — why this needs no edges · [`docs/owned-relations.md`](../owned-relations.md) — the guide
