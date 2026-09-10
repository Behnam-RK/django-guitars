# 0021 — a retirement is ordered against the migration that created what it drops

- **Status:** accepted — implemented in 2.10.0
- **Date:** 2026-09-10
- **Amends:** nothing — it extends [ADR 0013](0013-cross-app-migration-dependency-edges.md).
- **Affects:** `makeguitarmigrations`, `enforcement.scanning`, `enforcement.operations`

## Context

A cascade rule's **create** lands in the owner model's app; its **retirement** in the app owning
the table the rule fires on, per `_table_app_labels`, built from the live registry and never
reading a migration file. Those agree only by accident, and where they differ nothing orders the
`DROP RULE` after the `CREATE RULE`: a fresh `migrate` reaches the drop first and aborts with
`rule "…" for relation "…" does not exist`, while an incremental database is untouched — the
divergence [ADR 0006](0006-inline-generated-migration-sql.md) exists to prevent, reached by the
ordering failure ADR 0013 was written for.

Found on the 2.9.1 branch through a proxy, but **not** a legacy shape: plain **cross-app MTI**
reaches it. A child in one app over an ancestor declaring `_deleted_at` in another emits the
create while it is walked, while the rule fires on the ancestor's table — so any project with
such a child taking an inbound `CASCADE` key is generating the split today.

**ADR 0013's machinery cannot answer this.** `resolve_object_migration` answers *which migration
establishes this model or field*. A rule is a `RunSQL`, invisible to migration state, and an
`ObjectRef` for the table would resolve to the related model's `CreateModel` — a node guaranteeing
the *table*, not the *rule*. Confidently wrong is worse than absent.

## Decision

**The scan records which migration created each cascade rule, and the retirement is ordered
against that node.**

1. `ExistingOperations.soft_delete_related_dependencies` maps the cascade key to the
   `(app_label, migration)` whose header created it — never popped, and carried through a
   rename with the coverage it mirrors.
2. `_retired_cascade_operations` records the edge as it emits, structurally (ADR 0013 point 1).
   Nothing re-parses the header the emitter just wrote.
3. **One edge suffices**: the creating migration already carried edges for everything its
   `CREATE RULE` named, so ordering after it orders after the table the `DROP` names.
4. **Emitted under `--adopt` too.** Its `DROP RULE IF EXISTS` turns the abort into *silence* —
   the drop no-ops and the later create leaves the rule live — so adopt needs the edge more.
5. **A node the graph does not hold warns and emits no edge** (ADR 0013 point 5) -- it was read
   off a header, and a squash can have replaced the file. On the write path only: `--check`
   reaches the note below instead.
6. **Which of a retirement and a create wins is settled by graph order**, after the walk, since
   registry order is not chronological. The retirement wins only where the create is an ancestor
   of it: unordered is not "the create is older", and reading it so pops a live rule.
7. **`--check` names a retirement already written that nothing orders**, by reachability, joining
   `_missing_edges` so the existing refusal raises it. `cascade_retirement_sites` records each with
   the create it dropped, snapshotted at the pop and falling back to the finished map — apps walk
   in registry order, so the create may be scanned second.

## Why

**The emitter alone helps nobody who already has the bug.** A migration on disk is never
rewritten, so the note is the only channel: ADR 0013's "retrofitting is by hand".

**Provenance is cascade-only.** Only a family whose drop is hosted by a different app than its
create can ask this: nothing retires the trigger, soft-delete, owned or self-cascade families, and
the autofill retirement's edge targets the *function* migration.

**Re-creation needs no special case.** A key retired twice is popped both times, so the second
retirement is never re-emitted — its edge having been decided when the create was its own app's.

## Consequences

**Accepted costs.**

- **`--check` fails on graphs that passed before**, on files this release did not touch — why
  2.10.0 is a minor, the precedent being 2.5.0. Scoped to the apps a run names, and narrowed by
  two reachability guards and the own-app filter.
- **Cross-app recording is not chronological.** Two apps creating one rule record
  last-write-wins, a *weaker* edge but never a wrong one: every create of a key renders one rule
  name on one table. Which of a create and a retirement wins no longer rides on it (decision 6).
- **Two apps creating one rule stays unsound**: one edge cannot order three nodes. The
  `--check` half reports it, which is the best available outcome.
- **A key retired more than once is not reported at all**: its older sites name a create the
  scan cannot attribute and the newest may be a re-adoption, so the tuple would order the old
  drop after the create reviving the rule.
- **An edge target can vanish**, removing the creating app later turning it into
  `NodeNotFoundError`. Shared with every ADR 0013 edge, but this one points at an *enforcement*
  migration, likelier to be squashed.

**Reversibility.** High: dependencies are graph metadata, not inlined SQL, and the provenance is
re-derived every run. No database carries a trace.

## Alternatives rejected

- **Re-host the retirement into the creating app**, the app's own leaf then ordering it.
  Rejected: it needs the same provenance, so it is strictly more on top of this; it breaks
  `_table_app_labels`' one-table-one-host invariant, whose reason is that two apps emitting one
  `DROP` fail the second at `migrate`; and where the creating app has left `LOCAL_APPS` the
  retirement would have no host at all.
- **Refuse, and name the `DROP RULE` by hand**, as an unmapped table is named. Rejected: it needs
  the provenance first anyway, then declines to write a correct migration in favour of prose, and
  its steady state leaves a rule live and archiving rows.
- **Make the `DROP` unconditionally `IF EXISTS`.** One line, no provenance, no edge, and the
  first thing a reviewer suggests. Rejected: it converts an abort into silent divergence, and
  spends the `IF EXISTS` that [ADR 0019](0019-migration-lifecycle-objects.md) reserves for
  `--adopt`, the one path honest about not knowing what the database holds.
