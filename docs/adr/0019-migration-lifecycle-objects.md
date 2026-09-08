# 0019 — renames carry coverage, retirement needs evidence, and one operation may drop

- **Status:** accepted — implemented in 2.9.0
- **Date:** 2026-09-08
- **Affects:** `guitars.operations`, `makeguitarmigrations`, `guitars.sql.soft_delete`
- **Amends:** nothing. Fills the gap [ADR 0006](0006-inline-generated-migration-sql.md) leaves —
  it settles what a generated migration *says*, never what happens when the thing it names moves
  or goes away.

## Context

Every enforcement object this kit writes is asserted and never retired. That was a deliberate
simplification, and three ordinary schema changes break against it. A `RenameModel` leaves the
object in place under the new table while the coverage asserting it is filed under the old name.
A relaxed `CASCADE` key leaves its rule live with `--check` green. A removed column makes
`RemoveField` fail with `rule … depends on column`, and the documented answer was to hand-write
a `DROP RULE`.

## Decision

**A rename carries coverage forward, and the chain keeps every prior name.** The mapping is
derived by replaying Django's own migration state either side of each `RenameModel` /
`AlterModelTable`, read per *operation* rather than by diffing the two states — a rename changes
the model name too, so a diff sees one model gone and another arrived. Every prior name, not
just the first: a generation that ran between two renames left an object under the intermediate
one, and only dropping each leaves a single object behind.

**Liveness is filtered where the drop is emitted, not in the chain.** A name a rename frees can
be retaken by a later `CreateModel`. Dropping that table's objects would be wrong, but its
coverage must still translate — filtering the chain instead leaves the renamed table reading as
uncovered, and the plain `CREATE` that follows collides with what the rename carried over. The
question is asked of the whole model registry, not `LOCAL_APPS`: a name retaken by a model this
generator never writes for is no less live.

**Retirement requires positive evidence.** A cascade rule is dropped only where its key is
recorded, the models no longer require it, and **both** its tables still map. A table mapping to
nothing is a deleted model on one reading and an app dropped out of `LOCAL_APPS` on another, and
this generator cannot tell them apart — following the wrong one destroys a live cascade, so
those are *named* with the exact statement instead. Never absence-based: that is the trap
`sweepowned` was taught to avoid by following what the database holds rather than what the
generator would emit today.

**`RetireEnforcement` drops only what this kit mints.** It is irreversible by the `reversible`
flag, so `migrate` refuses before running anything, and what it drops is written only by the
generator. That is exactly why it is filtered by name as well as by dependency: a consumer's own
rule, trigger or policy is never taken, and one blocking the same change is the consumer's to
drop. It refuses on a table that does not exist and on a column that does not resolve — without
that, a typo or the operation placed *after* its `RemoveField` silently escalated a one-column
retirement into dropping everything on the table.

**Row-level security is torn down per table stripped.** It is a table *flag*, not an object
`pg_depend` reaches, so dropping the last `tenant_scope` off a FORCEd table leaves it returning
no rows to anyone, the owner included. A tenant policy is filed against the column it reads, so
an MTI child's lives on the ancestor: retiring the ancestor drops the *children's* policies while
the target stays the ancestor. Keyed on the target, two tables of three were left invisible.
Gated on having dropped one of ours from that table, so a consumer who secured their own table
keeps it.

## Consequences

- `guitars.operations.RetireEnforcement`'s import path and signature are **frozen**, and more
  brittle than `guitars.sql`'s names: a consumer's migration imports it by that path, and unlike
  enforcement SQL an operation cannot be inlined. Guarded in `tests/test_operations.py`.
- The module is `operations.py`, not `migrations.py`: `guitars` is in `INSTALLED_APPS` and
  `guitars/migrations/` is already this app's empty migrations package.
- `pg_depend`, never `pg_rules.definition` — that view prints identifiers unquoted and
  lower-cased, so a quoted match finds nothing and a bare one cannot tell `shop` from `shopping`.
- The retirement's `reverse_sql` recreates the rule where the column can be recovered. The
  primary form's key never spelled one, because the historical rule name does not, so it is read
  back off the first remaining foreign key to that owner; where the field is gone the reverse
  raises rather than succeeding into a database missing a rule its history claims.
- `--adopt` remains the one path that may say `IF EXISTS`, being the one honest about not
  knowing — except where a rename already made the drop all-`IF EXISTS` over every spelling.

## Alternatives rejected

- **Absence-based retirement.** Cannot distinguish a deleted model from a scoped run, and the
  failure destroys a live cascade.
- **Dropping everything that depends on the table.** Simpler, and it takes consumer objects an
  irreversible operation cannot put back.
- **Filtering the rename chain itself.** One layer too high: the chain is also what the scan
  translates coverage through, so emptying it reintroduces the collision it was meant to prevent.
