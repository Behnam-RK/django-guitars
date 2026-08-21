# 0011 — soft-delete ownership is declared by a field subclass and always guarded

- **Status:** accepted — implemented in 2.3.0; the per-**column** limit below is superseded by [ADR 0012](0012-cross-owner-last-owner-guard.md) (2.4.0)
- **Date:** 2026-08-19
- **Affects:** `guitars.models.OwningForeignKey`, `guitars.sql.soft_delete`, `makeguitarmigrations`, `SoftDeletableModel.hard_delete`

## Context

Soft-delete cascades were inbound only. `_cascade_candidates` walks `model._meta.related_objects` — reverse relations — keeps `on_delete == CASCADE`, and emits a rule on the table whose `_deleted_at` flips:

```
ON UPDATE TO {owner_table}  DO ALSO  UPDATE {dependent} … WHERE "{fk_col}" = old."{pk}"
```

The missing shape is a row that *owns* what it points at, with the foreign key on the owner. It is the same rule with the predicate sides swapped, and there was no way to ask for it. The consumer that forced the question had written the rule by hand, in the only hand-written enforcement migration in its repository, with a header explaining why the generator could not own it: declaring the relation `CASCADE` to reach `_cascade_candidates` would have emitted the rule *backwards*, soft-deleting the owner when the target went.

`on_delete` cannot carry this. It describes what happens to *this* row when the target is deleted — the opposite direction — so it is not merely an inconvenient spelling but the wrong question. Ownership needed its own declaration.

Two things had to be decided.

**How it is declared.** Either a `ForeignKey` subclass, or a class-level list of field names the generator reads off the model (`soft_delete_owns = ('pdp_display',)`), leaving the relation a plain `ForeignKey`.

**Whether the generated rule carries a last-owner guard.** The relation the consumer needed it for is single-owner today, enforced by a `UniqueConstraint`, and its own source says sharing one target across several owners is planned. Three options: always emit a `NOT EXISTS` guard over other live owners; emit it only where no `UniqueConstraint` covers the column; or never emit it and document ownership as valid under single ownership alone.

## Decision

**A `ForeignKey` subclass, `guitars.models.OwningForeignKey`,** with no additional flag — the class name is the declaration. `deconstruct()` pins the recorded path to `guitars.models.OwningForeignKey` rather than the module the class lives in, and a `check()` override refuses `on_delete=CASCADE` as `guitars.E001`.

**The last-owner guard is always emitted**, whatever the constraints on the column say:

```sql
AND NOT EXISTS (
    SELECT 1 FROM {table} AS guitars_owner
    WHERE guitars_owner."{foreign_key}" = old."{foreign_key}"
      AND guitars_owner."{primary_key}" <> old."{primary_key}"
      AND guitars_owner._deleted_at IS NULL
)
```

`hard_delete()` applies the same predicate in Python before removing an owned row, narrowed three ways because it *removes* the row where the rule only stamps a column: it spares the whole collected batch rather than one row; it counts an **archived** referrer as still referring, that row's foreign key being on disk; and it consults **every** foreign key into the target — not just the owning column, and across every table of an MTI chain, since collecting one row of a chain removes them all. All three exist because dropping a still-referenced row fails the deferred constraint at `COMMIT` and takes the transaction with it. Sparing there leaves the target archived — where the rule had already put it.

## Why

**A field subclass over a model attribute.** The declaration belongs where `on_delete` is, because it answers the same kind of question about the same relation; a reader deciding what happens when this row dies should not have to look in two places. A name list is also unchecked: a typo, or a field later renamed, silently stops generating a rule, and the failure mode of a *missing* soft-delete rule is a permanently deleted row. The subclass is checkable by construction.

The strongest objection is that this makes `deconstruct()` part of the frozen interface, and `guitars.models.OwningForeignKey` a string that appears literally in migrations already applied in consuming projects — exactly the class of obligation [ADR 0006](0006-inline-generated-migration-sql.md) exists to keep from growing. Accepted, and bounded: it is one path, pinned deliberately rather than inherited from the module layout, so moving the file later costs nothing.

**The guard, unconditionally.** Deriving it from constraint shape reads as the cheaper, more precise option, and is the one to avoid. It makes the rule's SQL depend on something no part of the enforcement layer watches: dropping a `UniqueConstraint` is an ordinary schema migration that changes no model field, so the rule's `[SQL:…]` identity would not move, `--check` would stay green, and the database would keep an unguarded rule while the models now permit sharing. The first shared row soft-deleted would take its target with it. That is silent data loss on a code path nobody edited — the same shape as the 1.0.0 guard rewrite that shipped as nothing.

Never emitting it and documenting the restriction fails for the same reason one step earlier: the restriction is invisible at the call site, and the consumer that motivated the feature has already said it intends to lift it.

The cost of always emitting it is one `NOT EXISTS` on the foreign-key column per soft delete, which Django indexes by default.

## Consequences

**Accepted costs.**

- `guitars.models.OwningForeignKey` is a frozen path, and `deconstruct()` joins the frozen interface `guitars.sql`'s names already occupy.
- Adopting the field on an existing relation emits a state-only `AlterField`.
- Every owned soft delete pays for the guard subquery, including on relations that are unique by constraint and can never have a second owner.
- A relation that would close a **cycle** of `ON UPDATE` rules is refused with a warning. The degenerate case is self-ownership, or ownership of an MTI descendant, where the rule's action updates the table it fires on; the general case is a loop through other models' owned or `CASCADE` rules. A rule's action expands *before* the original statement, so PostgreSQL rewrites the cycle into itself and rejects every `UPDATE` to every table in it — a plain `save()` included — as infinite rule recursion, at rewrite time, before any `WHERE` guard runs. **Every** edge on a cycle is refused rather than one chosen edge: which one got dropped would otherwise depend on iteration order, and an order-dependent refusal cannot stay stable from run to run. The graph is derived from the whole model registry (`introspection.rule_update_cycle_edges`) and shared with `hard_delete()`, so the two layers cannot disagree about which relations carry a rule.
- The owner-side MTI case is refused, not solved: a model declaring the foreign key on its own table while inheriting `_deleted_at` from an ancestor gets a warning and no rule, since `old."<column>"` cannot name a column that is not on the table the rule fires on. This mirrors the inbound limitation `_cascade_candidates` already reports.
- `hard_delete()` re-implements the guard in Python, narrowed a third way beyond the two above: **any** surviving foreign key holds an owned row back, not only the owning column, because dropping a still-referenced row fails the deferred constraint at `COMMIT`. Collection therefore runs to a fixpoint — a row spared by a reference that is *itself* collected later is picked up on a later pass. The two predicates must be changed together; a test asserts the sparing behaviour on both paths. It also has to re-implement the *candidate* test — a relation the generator refused (a cycle, the owner-side MTI case above, or a target with no `_deleted_at`) has no rule, so following it in Python would destroy exactly what the rule spared.
- The guard is per foreign-key **column** and per **statement**, not per target row. A second `OwningForeignKey` to the same row does not spare it, and one statement soft-deleting every owner leaves the target alive, since PostgreSQL runs an `ON UPDATE` rule's action before the original update — each guard still sees its siblings as live. Both are documented in [`docs/owned-relations.md`](../owned-relations.md); lifting either means leaving the rule form behind for a statement-level trigger.

  **Superseded in 2.4.0 for the per-column half** — and the trigger claim with it. [ADR 0012](0012-cross-owner-last-owner-guard.md) lifts the per-column limit by giving each rule one `NOT EXISTS` arm per owning column, staying in the rule form: cross-owner is always two statements on two tables, which a transition table cannot span, so the trigger would have been additive rather than a replacement. The per-**statement** half stands as written.

**Reversibility.** Low cost to extend, high cost to reverse. Adding a keyword to relax the guard later is additive. Removing the guard from already-generated migrations is not: the SQL is inlined, so an existing database keeps whatever it was migrated with until a regeneration and a `migrate`.

## Related

- [`docs/owned-relations.md`](../owned-relations.md) — the feature guide
- [ADR 0012](0012-cross-owner-last-owner-guard.md) — amends the per-column limit above
- [`docs/mti.md`](../mti.md) — the owner-side limitation
- [ADR 0006](0006-inline-generated-migration-sql.md) — why the SQL is inlined, and what that freezes
