# 0015 — a soft-deletable MTI child under a plain ancestor is refused, not supported

- **Status:** accepted — implemented in 2.7.0
- **Affects:** `guitars.checks`, `makeguitarmigrations`, [ADR 0014](0014-statement-level-owned-sweep.md)
- **Date:** 2026-08-31

## Context

Django's `Collector` issues one `DELETE` per table in a multi-table-inheritance chain. The kit
covers that with two rules: a model owning `_deleted_at` gets a soft-delete rule on its own
table, and a child inheriting the column gets the **redirect** rule, which stamps the ancestor
instead. Both assume the column lives at or above the level being deleted.

The inverse shape — a child declaring `_deleted_at` while its concrete ancestor has none — was
neither covered nor refused. The generator emitted a *plain* rule on the child, so the child's
`DELETE` became an `UPDATE` and its row survived, while the ancestor's `DELETE` met no rule and
really removed the row the surviving child points at. The statement then aborts at `COMMIT`:

```
update or delete on table "testapp_marquee" violates foreign key constraint
"testapp_neonmarquee_marquee_ptr_id_87f804c2_fk_testapp_m" on table "testapp_neonmarquee"
```

Nothing reached this before 2.7.0 because the ladder hands out `_deleted_at` and `_updated_at`
together: every rung from `SetarModel` up carries both, so the split needs a `DutarModel` parent
and a child mixing in `SoftDeletableModel` by hand. A branch closing an unrelated gap built
exactly that shape as a fixture and found it undeletable.

## Decision

**Refuse the shape.** `guitars.E003` reports it at `manage.py check`, and the generator re-asks
the same question of the column's *owner* (`checks.refuses_soft_delete_rule`, see Consequences)
and emits no soft-delete rule, naming the model on stderr.

- **An error, not a warning.** The row does not merely go unstamped: the statement aborts, and no
  runtime path in the kit can spare it. A warning would leave a project shipping a model nobody
  can delete.
- **Re-asked by the generator**, as `OwningForeignKey`'s own checks are, for the reason
  [ADR 0011](0011-owner-side-soft-delete-ownership.md) gives: `--skip-checks` reaches the
  generator and `hard_delete()` runs no checks at all.
- **Refusing beats emitting.** The rule is what turns a clean failure into a corrupt one — without
  it the ancestor's `DELETE` takes the whole chain, which is at least consistent.

## Why not the alternatives

**Guard the ancestor's `DELETE` too**, with a rule suppressing it while a soft-deletable
descendant exists. It works, and it is a new operation family — header, derived scanner,
container, name family, corpus baseline — plus a second rule per chain in every consuming
project. A release of its own, and one nobody has asked for: no consumer has ever declared
this shape.

**Support it in the owned sweep only.** The shape is reachable through an owner's rule and sweep
even while `.delete()` is broken, so `_updated_at` could be stamped there and the delete left
failing. That is a shape the kit half-supports, which is worse than one it refuses — and it was
the reason [ADR 0014](0014-statement-level-owned-sweep.md)'s ancestor `_updated_at` work was
dropped rather than shipped.

**Document it and move on.** The trap stays silent for anyone who does not read the doc, and
`tests/testapp` is what a consumer copies.

## Consequences

- **The failure direction moves from aborting to destroying, and `guitars.E003` is the only thing
  holding it.** Refusing means no rule on the child's table, so `Collector`'s two `DELETE`s both
  execute and the whole chain goes — where before 2.7.0 the statement aborted and lost nothing.
  The check is an `Error`, so `manage.py check`, `migrate` and `runserver` all refuse to start
  while such a model exists; `--skip-checks` walks past that, and so does any code path that never
  runs checks. This is the one place in the kit where a refusal fails toward destroying data, and
  it is accepted only because the shape cannot be *made* to work without the new operation family
  below, and because nothing that previously succeeded starts failing.
- It is also **asymmetric between a fresh and an incrementally-migrated database**, which is what
  [ADR 0006](0006-inline-generated-migration-sql.md) otherwise exists to prevent: no command
  retires a rule, so a project that already migrated keeps the old rule and goes on aborting,
  while a fresh `migrate` of the identical history gets no rule and destroys. Dropping the live
  rule by hand is what makes the two agree, and the shape has to be removed either way.
- A project already running this shape gets a hard `check` failure on upgrade. It was already
  unable to delete those rows, so nothing that worked stops working.
- **The generator's refusal is asked of the column's *owner*, not of the model in front of it**,
  so a concrete descendant of a refused model is refused with it. Such a descendant declares
  nothing itself, so it would otherwise fall through to the MTI redirect rule — `DO INSTEAD`, the
  same row-keeping the refusal exists to withhold, dangling at `COMMIT` one table further down.
  `guitars.E003` still reports the *declaring* model alone: one finding per root cause, and
  making that ancestor soft-deletable fixes every descendant with it.
- `_updated_at` on an MTI ancestor is now unreachable from the owned sweep by construction,
  which is what lets ADR 0014 state the sweep stamps only the dependent's own table.
- The kit still has no way to make `_deleted_at` and `_updated_at` live on different tables.
  That is a limitation of the ladder, recorded here rather than worked around.

## Related

- [ADR 0011](0011-owner-side-soft-delete-ownership.md) — why a check is re-asked by the generator · [ADR 0014](0014-statement-level-owned-sweep.md) — the branch that found it · [`docs/mti.md`](../mti.md) — the supported shapes
