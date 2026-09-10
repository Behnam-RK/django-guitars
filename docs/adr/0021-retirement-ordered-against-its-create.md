# 0021 — a retirement is ordered against the migration that created what it drops

- **Status:** accepted — implemented in 2.10.0
- **Date:** 2026-09-10
- **Amends:** nothing — extends [ADR 0013](0013-cross-app-migration-dependency-edges.md)'s edge
  doctrine to an object migration state cannot see.
- **Affects:** `makeguitarmigrations`, `enforcement.scanning`, `enforcement.operations`

## Context

A cascade rule's **create** lands in the owner model's app. Its **retirement** is hosted by the
app owning the table the rule fires on — `_table_app_labels`, built from the live registry, which
never reads a migration file. The two agree only by accident, and where they differ nothing orders
the `DROP RULE` after the `CREATE RULE`: a fresh `migrate` reaches the drop first and aborts with
`rule "…" for relation "…" does not exist`, while an incrementally-migrated database is untouched.
That is the divergence [ADR 0006](0006-inline-generated-migration-sql.md) exists to prevent,
reached by the ordering failure ADR 0013 was written for.

Found by a review round on the 2.9.1 branch and reproduced on 2.9.0. That release keyed reverse
relations on `_meta.concrete_model`, stopping a proxy emitting the create into its own app, so the
split now needs two apps sharing one `db_table` (`models.W035`) — the population that matters is
histories already written.

**ADR 0013's machinery cannot answer this.** `resolve_object_migration` answers *which migration
establishes this model or field*. A rule is a `RunSQL`, invisible to migration state, and an
`ObjectRef` for the table would resolve to the related model's `CreateModel` — a node guaranteeing
the *table*, not the *rule*. Confidently wrong is worse than absent.

## Decision

**The scan records which migration created each cascade rule, and the retirement is ordered
against that node.**

1. `ExistingOperations.soft_delete_related_dependencies` maps the cascade key to the
   `(app_label, migration)` whose header created it — beside the digest, never popped or
   subtracted, and carried through a rename with the coverage it mirrors.
2. `_retired_cascade_operations` records the edge as it emits, structurally (ADR 0013 point 1).
   Nothing re-parses the header the emitter just wrote.
3. **One edge suffices.** The creating migration already carried edges for everything its
   `CREATE RULE` named, so ordering after it orders after the table the `DROP` names too.
4. **Emitted under `--adopt` too.** Its `DROP RULE IF EXISTS` turns the abort into *silence* —
   the drop no-ops and the later create leaves the rule live — so adopt needs the edge more.
5. **A node the graph does not hold warns and emits no edge** (ADR 0013 point 5): the node was
   read off a header, and a squash can have replaced the file since.
6. **`--check` names a retirement already written that nothing orders**, by reachability, joining
   `_missing_edges` so the existing refusal raises it. `cascade_retirement_sites` records each with
   the create it dropped, snapshotted at the pop and falling back to the finished map — apps walk
   in registry order, so the create may be scanned second.

## Why

**The emitter alone helps nobody who has the bug.** A migration on disk is never rewritten — the
digest guard skips it — so the note is the only channel: ADR 0013's "retrofitting is by hand",
with a `DROP` as the thing to retrofit.

**Provenance is cascade-only.** Only a family whose drop is hosted by a different app than its
create can ask this. Nothing retires the trigger, soft-delete, owned or self-cascade families, and
the autofill retirement's edge targets the *function* migration, already recorded.

**Re-creation needs no special case.** A key retired twice is popped by the scan both times, so
the second retirement is never re-emitted — and the edge it was written with was decided when the
create was its own app's, and dropped as a self-reference.

## Consequences

**Accepted costs.**

- **`--check` fails on graphs that passed before**, on files this release did not touch. Why
  2.10.0 is a minor, the precedent being 2.5.0. Two reachability guards and the own-app filter
  narrow it; the residual is an ordering guaranteed outside the graph, which the kit cannot see.
- **Cross-app recording is not chronological.** Apps walk in registry order, so two creating one
  rule record last-write-wins. The failure is a *weaker* edge, never a wrong one: every create of
  a key renders one rule name on one table. Inherited from the digest beside it.
- **Two apps creating one rule stays unsound.** One edge cannot order three nodes. The `--check`
  half reports it, which is the best available outcome; nothing here claims more.
- **An edge target can vanish.** Removing the creating app later turns it into
  `NodeNotFoundError` — shared with every ADR 0013 edge, but this one points at an *enforcement*
  migration rather than an `0001_initial`, so it is likelier to be squashed. The `node_map` guard
  covers generation, not a later removal.
- A scoped run may emit an edge into an app outside the run. That is correct: an edge names a
  migration, it does not require the app to be in `LOCAL_APPS`.

**Reversibility.** High, as ADR 0013's is: dependencies are graph metadata, not inlined SQL, and
the provenance is re-derived every run. No database carries a trace.

## Alternatives rejected

- **Re-host the retirement into the creating app.** Fixes the disagreement at source, the app's
  own leaf then ordering it. Rejected: it needs the same provenance, so it is strictly more on top
  of this; it breaks `_table_app_labels`' one-table-one-host invariant, whose stated reason is that
  two apps emitting one `DROP` fail the second at `migrate`; it desynchronises
  `_scoped_cascade_retirement_notes`, which reasons in the owner table's app; and where the
  creating app has left `LOCAL_APPS` the retirement would have no host at all.
- **Refuse, and name the `DROP RULE` by hand**, as an unmapped table is named. Rejected: it needs
  the provenance first anyway, then declines to write a correct migration in favour of prose, and
  its steady state leaves a rule live and archiving rows.
- **Make the `DROP` unconditionally `IF EXISTS`.** One line, no provenance, no edge, and the
  first thing a reviewer suggests. Rejected: it converts an abort into silent divergence, and
  spends the `IF EXISTS` that [ADR 0019](0019-migration-lifecycle-objects.md) reserves for
  `--adopt`, the one path honest about not knowing what the database holds.
