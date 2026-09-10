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
divergence [ADR 0006](0006-inline-generated-migration-sql.md) exists to prevent.

Found on the 2.9.1 branch through a proxy, but **not** a legacy shape: plain **cross-app MTI**
reaches it. A child in one app over an ancestor declaring `_deleted_at` in another emits the
create while it is walked, while the rule fires on the ancestor's table — so any project with
such a child taking an inbound `CASCADE` key is generating the split today.

**ADR 0013's machinery cannot answer this.** `resolve_object_migration` answers *which migration
establishes this model or field*; a rule is a `RunSQL`, invisible to migration state, and an
`ObjectRef` for the table would resolve to the related model's `CreateModel` — the *table*, not
the *rule*. Confidently wrong is worse than absent.

## Decision

**The scan records which migration created each cascade rule, and the retirement is ordered
against that node.**

1. `ExistingOperations.soft_delete_related_dependencies` maps the cascade key to **every**
   migration whose header created it, oldest first — never popped, and carried through a rename
   with the coverage it mirrors. A list, a retired key being creatable again.
2. `_retired_cascade_operations` records the edge as it emits, structurally (ADR 0013 point 1).
   Nothing re-parses the header the emitter just wrote.
3. **One edge suffices**: the creating migration already carried edges for everything its
   `CREATE RULE` named, so ordering after it orders after the table the `DROP` names.
4. **Emitted under `--adopt` too.** Its `DROP RULE IF EXISTS` turns the abort into *silence* —
   the drop no-ops and the later create leaves the rule live — so adopt needs the edge more.
9. **The digest guard is waived only where a create or drop genuinely recurs this run**, at the
   point each is written, not speculatively at scan time -- a new or permanently-retired key
   never taints its app.
5. **A node the graph does not hold warns and emits no edge** (ADR 0013 point 5), on the write
   path only -- `--check` reaches the note below instead.
6. **Which of a retirement and a create wins is settled by graph order**, after the walk, since
   registry order is not chronological. Per *key*, newest against newest — settling per site
   reads a re-adopted rule as retired. The walk itself never pops a key's coverage, or the same
   scan-order accident this point exists to fix would silently override its own verdict.
7. **Each drop is matched to the create it dropped**: the newest the graph puts *before* it, not
   the newest of the key, which after a re-adoption is the create that drop *precedes*.
8. **`--check` names a retirement already written that nothing orders**, by reachability, joining
   `_missing_edges` so the existing refusal raises it.

## Why

**The emitter alone helps nobody who already has the bug.** A migration on disk is never
rewritten, so the note is the only channel: ADR 0013's "retrofitting is by hand".

**Provenance is cascade-only.** Only a family whose drop is hosted by a different app than its
create can ask this: nothing retires the trigger, soft-delete, owned or self-cascade families, and
the autofill retirement's edge targets the *function* migration.

**Both directions, because the cycle needs both.** A drop ordered after its create is half an
ordering: a re-adopted create ordered against nothing reaches a fresh database first and leaves
the rule dropped where an incremental one has it. So a create carries an edge to the retirement
it revives, for as many cycles as a project runs.

## Consequences

**Accepted costs.**

- **`--check` fails on graphs that passed before**, on files this release did not touch — why
  2.10.0 is a minor, the precedent being 2.5.0. Scoped to the apps a run names, and narrowed by
  two reachability guards and the own-app filter.
- **Two apps creating (or retiring) one rule is unsound and undetected**: one edge cannot order
  three nodes, so the drop can run before the wrong one and leave the rule live where an
  incremental database has it retired.
- **An unordered history is attributed by rank**, the two alternating -- a guess, since silence
  would fall on the histories most in need. It assumes every recorded create was genuine: a
  digest-only replace, or `RetireEnforcement`'s wholesale subtraction, can each misattribute
  rank without either being detected.
- **An edge target can vanish**, removing that app later turning it into `NodeNotFoundError` --
  shared with every ADR 0013 edge, but pointing at an *enforcement* migration, likelier squashed.

**Reversibility.** High: dependencies are graph metadata, not inlined SQL, and the provenance is
re-derived every run. No database carries a trace.

## Alternatives rejected

- **Re-host the retirement into the creating app**, the app's own leaf then ordering it.
  Rejected: it needs the same provenance, so it is strictly more on top of this; it breaks
  `_table_app_labels`' one-table-one-host invariant; and where the creating app has left
  `LOCAL_APPS` the retirement would have no host at all.
- **Refuse, and name the `DROP RULE` by hand**, as an unmapped table is named. Rejected: it needs
  the provenance anyway, then declines to write a correct migration in favour of prose, leaving a
  rule live and archiving rows.
- **Make the `DROP` unconditionally `IF EXISTS`.** One line, no provenance, no edge -- the
  first thing a reviewer suggests. Rejected: it converts an abort into silent divergence, and
  spends the `IF EXISTS` [ADR 0019](0019-migration-lifecycle-objects.md) reserves for `--adopt`.
