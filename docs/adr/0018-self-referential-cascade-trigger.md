# 0018 — a self-referential cascade is a statement-level trigger, not a rule

- **Status:** accepted — implemented in 2.8.0
- **Date:** 2026-09-08
- **Affects:** `guitars.sql.soft_delete`, `makeguitarmigrations`
- **Amends:** nothing. Narrows one refusal that has stood since 0.x; the cycle refusal it sits
  beside is unchanged. Borrows its mechanism from [ADR 0014](0014-statement-level-owned-sweep.md).

## Context

A `CASCADE` foreign key pointing a table at itself — a tree — got no cascade rule and a warning
saying to cascade it in Python. The refusal is right about rules: PostgreSQL expands a rule's
action *before* the statement that triggered it, so `ON UPDATE TO t DO ALSO UPDATE t` is rewritten
into itself and the server rejects **every** `UPDATE` to that table, an ordinary `save()` included,
with the rule's own `WHERE` guard never read. `migrate` reports success and the table is bricked.

The consumer that hit this (an offer tree) cascaded in Python instead. That works and is the wrong
layer: every other cascade in the kit is a database guarantee that holds under `bulk_update`,
`queryset.delete()` and raw SQL, which is the whole reason the enforcement lives in the database.

A statement-level trigger takes no part in rule rewriting, so it can do what the rule cannot. The
kit already ships that exact shape as the owned sweep (ADR 0014), which is what makes this a small
decision rather than a new mechanism.

## Decision

Emit a statement-level `AFTER UPDATE` trigger, with `OLD`/`NEW` transition tables, for a
self-referential `CASCADE` foreign key **whose table is the one owning `_deleted_at`**. A self
key declared on an MTI *child* whose `_deleted_at` lives on an ancestor gets neither this
trigger nor a rule, as before this ADR — the same gap the flat cascade rule has there. Its function archives the children of every row whose
`_deleted_at` flipped from null in this statement.

**Self-referential only.** A cycle through two or more tables stays refused. Converting *any one*
of its edges to a trigger would unbrick it just as well, and that is precisely the problem: which
edge gets converted has no stable answer, and would shift as models are added or removed. A
refusal that moves run to run is worse than one that holds. A self loop is one edge, so there is
nothing to choose.

**No `pg_trigger_depth()` guard**, unlike the `_updated_at` trigger. Here the trigger re-fires
*itself*: its own `UPDATE` archives one level of the tree and is another statement on the same
table, so the next level follows. Recursion ends when a level archives nothing — the transition
tables then hold no flipped row, the `UPDATE` matches nothing, and no further statement runs. Tree
depth is bounded by `max_stack_depth`, one nested plpgsql frame per level, which no realistic tree
approaches.

**`AFTER UPDATE`, not `AFTER UPDATE OF _deleted_at`.** The column list was the intended shape: a
tree table is written far more often than it is archived, and both archiving paths name
`_deleted_at` in their `SET` list, so `OF` would have skipped the transition-table join on every
ordinary write. PostgreSQL refuses it — *transition tables cannot be specified for triggers with
column lists* — and the transition tables are the mechanism. The cheap test moved into the body
instead: the function returns before touching the tree unless some row's `_deleted_at` is actually
non-null in the new image.

**Under tenancy it behaves like the cascade rules, not like the owned family.** The child `UPDATE`
runs under the invoker's row-level security, so a child the current tenant scope cannot see stays
live. That is the cascade rules' existing behaviour and it fails **safe** — a leaked live row, never
a wrongly archived one — where the owned family's tenancy failure destroys a live owner, which is
why that one is refused instead. A self key on a tenanted tree points inside one tenant by
construction, so a hidden child is a data error rather than a shape the kit has to serve. No
`SECURITY DEFINER`: enforcement never escalates privilege, and the non-superuser role the tests run
as could not create such a function against a table it does not own.

## Consequences

- The next `makeguitarmigrations` in an existing project emits this trigger for every
  self-referential `CASCADE` key it finds. A Python fallback prescribed by the old docs becomes
  redundant rather than wrong: it re-archives rows the trigger already stamped, and
  `_deleted_at IS NULL` makes that a no-op.
- `_updated_at` splits in two, and `tests/test_self_cascade.py` pins both halves. The **tree
  rows** get it from an assignment spliced into the trigger's own `UPDATE`, for the sweep's
  reason: that `UPDATE` runs at trigger depth ≥ 1, where `updated_at_trigger`'s
  `WHEN (pg_trigger_depth() = 0)` suppresses it. The ordinary **cascade children of every level
  below the first** do **not** get one — that rule fires from inside the trigger at depth 1 and
  sets only `_deleted_at`, so only the root's own children, archived by the original depth-0
  statement, are stamped. An accepted gap, as this ADR's plan expected. Closing it means
  splicing `_updated_at` into the cascade rule's action, which moves the `[SQL:...]` identity of
  every cascade rule in every consuming project — too much for a timestamp on an archived row.
- The trigger's `UPDATE` fires the table's other `ON UPDATE` cascade rules for each level, so a
  tree's ordinary children cascade with it rather than being stranded below the first level.
- The name sizes every variable segment (`soft_delete_self_cascade_<n>_<schema>_<n>_<table>_<n>_<fk>`)
  for the owned family's reason: nothing predates it, so there was no boundary left to guess at.
  A distinct prefix keeps it from meeting the other three families at all.
- No cross-app dependency edges. `CREATE TRIGGER` names only the table it fires on, plpgsql does
  not resolve a body at `CREATE FUNCTION` time, and the one table involved is the declaring app's
  own — so unlike a cascade rule there is nothing here that can cross an app.
- `introspection._rule_update_edges` keeps the `(T, T)` self edge. It describes rules, and an
  *owned* rule on the same shape must still be refused through it; the cascade emitter routes the
  self key to the trigger before consulting that set.

## Alternatives rejected

- **Convert every refused cycle edge.** No stable choice of edge; see above. The trigger route is
  recorded here as available should a consumer ever present a multi-table cycle worth the cost.
- **Keep the Python fallback as the answer.** It is correct only through the ORM, which is the one
  guarantee this kit exists to stop relying on.
- **Stamp `_updated_at` from inside the cascade rule.** It would move the `[SQL:...]` identity of
  every cascade rule in every consuming project, to fix something that measured as not broken.
