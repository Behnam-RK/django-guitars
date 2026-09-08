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
of its edges would unbrick it too, and that is the problem: which edge has no stable answer, and
would shift as models are added. A refusal that moves run to run is worse than one that holds.

**No `pg_trigger_depth()` guard**, unlike the `_updated_at` trigger. Here the trigger re-fires
*itself*: its own `UPDATE` archives one level of the tree and is another statement on the same
table, so the next level follows. Recursion ends when a level archives nothing, and the body's
`EXISTS` guard is what makes it *terminate* rather than merely run cheaply: a statement trigger
fires on an `UPDATE` matching zero rows, so without it the function's own no-op `UPDATE` re-fires
it until the stack blows. Tree depth is bounded by `max_stack_depth`, one nested plpgsql frame per
level — measured at roughly **500 levels** on the default 2 MB. Past it the statement aborts and
the whole archive rolls back, so a too-deep tree is a visible error, never a half-archived one.

**`AFTER UPDATE`, not `AFTER UPDATE OF _deleted_at`.** The column list was the intended shape, a
tree being written far oftener than archived. PostgreSQL refuses it — *transition tables cannot be
specified for triggers with column lists* — and those tables are the mechanism, so the cheap test
moved into the body: it returns unless a row's `_deleted_at` flipped from null in this statement,
the `UPDATE`'s own predicate.

**Under tenancy it behaves like the cascade rules, not like the owned family.** The child `UPDATE`
runs under the invoker's row-level security, so a child the scope cannot see stays live. That is
the cascade rules' behaviour and it fails **safe** — a leaked live row, never a wrongly archived
one — where the owned family's tenancy failure destroys a live owner, which is why that one is
refused. No `SECURITY DEFINER`: enforcement never escalates privilege, and the non-superuser role
the tests run as could not create such a function against a table it does not own.

## Consequences

- The next `makeguitarmigrations` in an existing project emits this trigger for every
  self-referential `CASCADE` key it finds. A Python fallback prescribed by the old docs becomes
  redundant rather than wrong: it re-archives rows the trigger already stamped, and
  `_deleted_at IS NULL` makes that a no-op.
- `_updated_at` splits in two, and `tests/test_self_cascade.py` pins both halves. **Tree rows**
  always get it, from an assignment spliced into the trigger's own `UPDATE`, for the sweep's
  reason: that `UPDATE` runs at depth ≥ 1, where `updated_at_trigger`'s `WHEN` suppresses it. An
  ordinary **cascade child** gets one only where its level was reached by one collector statement
  rather than by the trigger — `.delete()` stamps every child, a raw or bulk archive only the
  root's own. **The same archive, a different outcome per caller**, which is the divergence this
  kit exists to remove, so it is stated rather than buried. Accepted for the reason below.
- The trigger's `UPDATE` fires the table's other `ON UPDATE` cascade rules for each level, so a
  tree's ordinary children cascade with it rather than stranding below the first level.
- **A primary-key rewrite on a live parent with live children is refused**, with the owned
  sweep's error class. Correlation is on the key, so a moved key leaves no after-image to match
  and an archive becomes indistinguishable from a re-key, leaking the subtree in silence.
  Django's foreign keys are `DEFERRABLE INITIALLY DEFERRED`, so the shape is reachable. Narrow:
  a parent with nothing live below it re-keys and archives in one statement uncomplainingly.
- **No repair command, deliberately.** A pre-2.8.0 database has no rule for the shape, so a raw
  or bulk archive of a tree root there left live children under an archived parent. The refusal
  was documented from 0.x and said to cascade in Python, and repair needs no command: archive
  the children of every archived parent, to a fixpoint.
- The name sizes every variable segment for the owned family's reason — nothing predates it, so
  no boundary was left to guess at — and a distinct prefix keeps it clear of the other three.
- No cross-app dependency edges: `CREATE TRIGGER` names only its own table, and plpgsql resolves
  no body at `CREATE FUNCTION` time.
- `introspection._rule_update_edges` keeps the `(T, T)` self edge, describing *rules*, so an owned
  rule on the shape stays refused; the emitter routes the self key out before consulting it.

## Alternatives rejected

- **Convert every refused cycle edge.** No stable choice of edge; see above. The trigger route is
  recorded here as available should a consumer ever present a multi-table cycle worth the cost.
- **Keep the Python fallback as the answer.** It is correct only through the ORM, which is the one
  guarantee this kit exists to stop relying on.
- **Stamp `_updated_at` from inside the cascade rule.** It is the only way to close the
  caller-dependent gap above, and it moves the `[SQL:...]` identity of every cascade rule in
  every consuming project — too much for a timestamp on a row that is already archived.
