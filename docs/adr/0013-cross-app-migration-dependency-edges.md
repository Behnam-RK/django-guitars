# 0013 — a generated migration depends on what its rules name

- **Status:** accepted — implemented in 2.5.0
- **Date:** 2026-08-22
- **Affects:** `makeguitarmigrations`, `guitars.management.enforcement.graph`, `guitars.management._generator`, `guitars.tenancy.discovery`
- **Reverses:** the "**assumed, not enforced**" paragraph in [`docs/migrations.md`](../migrations.md), which recorded emitting no edge as a deliberate choice. Both its rationale and its stated remedy are wrong in practice; see below.

## Context

PostgreSQL parses a rule's action when the rule is **created**, not when it fires. Every table and
column an emitted rule names must therefore already exist at `migrate` time. Within one app, the
scaffold's dependency on that app's leaf orders this for free. Across apps, only an explicit
`dependencies` entry does — and the generator emitted none.

Four emitted objects name another app's tables and columns:

- **Owned rules** (2.4.0). One `NOT EXISTS` arm per owning column targeting the dependent, and a co-owner
  routinely lives in another app; a *joined* arm also reads the MTI ancestor it takes liveness
  from, and the rule `UPDATE`s the dependent's table.
- **Cascade rules** (0.x). The action names `{related_table}`, in another app whenever the foreign key
  crosses one. **Pre-existing**; 2.4.0's arms only made it reachable in the other direction.
- **The tenant RLS policy** (2.1.x). `sql.policy._owner_exists` names the MTI ancestor's table and its tenant column inside `EXISTS (…)`, resolved as `CREATE POLICY` is parsed. The policy is written into the *child's* app while `discovery.by_owner` keys on `column_owner(model, field)` — an ancestor that can live anywhere. **Pre-existing**, and the one family whose table is rendered *unquoted* (`policy._qualified_table`), which the `--check` table match has to accept.
- **The MTI redirect rule** (0.x). Its action `UPDATE`s the ancestor's table and its `_deleted_at`.
  `parent_ptr` orders the child's `CreateModel` after the ancestor's *table*, but an ancestor
  promoted to `SetarModel` gains `_deleted_at` later, ordered against nothing. Its `_updated_at`
  twin needs no edge: that parent table is a *literal* argument, `%I`-quoted when the trigger fires.

A consumer hit it on a virgin database — `psycopg.errors.UndefinedColumn: column
guitars_owner_1.default_pdp_display_id does not exist`, applying `bundles.0056_auto_enforcement`.
Generation order is not application order: Django orders cross-app migrations by explicit
dependencies and then by leaf, so that migration was planned 36 nodes ahead of the `shops` one
creating the column it read.

### Why the previous decision does not survive contact

`docs/migrations.md` recorded the gap as deliberate, on the grounds that "an edge into an app the
models have no relation to can close a cycle in the migration graph, which bricks `migrate` entirely
rather than in one order", and named the remedy as "a hand-written `dependencies` entry".

- The cycle risk is real **only for a leaf edge**. An edge at the migration that *creates* the object is by construction older than any rule naming it, so it cannot close a cycle.
- The stated remedy was tested in the consumer and **trades one failure for another**. Adding
  `('shops', '0035_…')` to `bundles/0056` dragged `shops/0031_auto_enforcement` — which references
  `bundles_pdpdisplay` and carries no `bundles` edge — ahead of the migration creating that table,
  giving `relation "bundles_pdpdisplay" does not exist`. Hand-patching leaf edges is precisely the
  over-constraint the doc warned about, recommended by the doc that warned about it.

So the choice was never "edge or no edge". It was "which node the edge points at", and the old text
answered a question nobody needed to ask.

## Decision

**Emit an edge to the migration that creates each object an emitted operation names, for every family.**

1. **References are collected structurally, as the rules are built** — never by parsing rendered
   SQL. A co-owner arm's table appears only in the rule body, not in its header, and reading
   structure over text is what `audittenancy` already does with `pg_depend` ([ADR 0010](0010-autofill-body-comparison.md)).
2. **The edge targets the creating migration, never the app leaf.** `resolve_object_migration` walks
   the app's migrations in **graph** order (a squash can order two names against their numeric
   prefix) and takes the **last** operation establishing the object under its *current* name — a
   `RenameField` supersedes the `AddField`, the rule naming the current spelling.
3. **No cycle guard, because a cycle cannot happen.** One was written during development and was theatre: it stood in the file being written by the app's newest leaf, which — the loader having been invalidated on the scaffold write — *was* that scaffold, and nothing on disk can depend on a node written a moment ago, so the check answered "no cycle" for every possible input; its one test faked the verdict. Both went, the invalidation with it: a ref always resolves in another app, whose history the new node cannot change. Point 2 is the real argument and it is a proof — an edge at the creating migration points backwards. The one shape that genuinely cycles, an edge onto a migration already depending on the file that needs it, is what point 5 declines to report.
4. **An unresolvable reference warns and emits no edge** — 2.4.2's behaviour, said out loud. An app
   with no migrations is legitimate (a third-party model its own package migrates), and refusing
   there would withdraw a rule that works today.
5. **`--check` fails on a missing edge**, by **reachability** rather than by the literal tuple: an
   ordering already guaranteed through another path is guaranteed, and flagging it would fail a
   build over a graph that works. The message prints the tuple to paste — and is withheld where the
   edge would land on a migration already depending on that file: Django rejects such a graph, so
   printing it would be red with no move that clears it.

## Consequences

- **`--check` now fails on graphs that passed before.** Why 2.5.0 is a minor release, and the point:
  those graphs were green all the way to a virgin-database failure.
- **Retrofitting is by hand.** A migration already recorded is skipped by the digest guard, so
  re-running the generator adds nothing to it — hence a `--check` message actionable on its own.
  Folding edges into `[DIGEST:…]` was rejected: it would re-digest every enforcement migration in
  every consuming project, the exact cost [ADR 0006](0006-inline-generated-migration-sql.md) avoids.
- **An edge the graph already implies is dropped** (`drop_implied_edges`). Two refs into one app routinely resolve differently — a table, and a column added later — and the older is then reachable from the newer, so writing it says nothing and reads as if the rule needed two orderings. Only *implied* ones go: edges neither of which reaches the other both stay, and so does one on a node the loader never saw.
- **Not minimal in one remaining way.** References are recorded when a rule is *built*, but an operation already recorded is not re-emitted, so a partial regeneration can attach an edge for a rule in an earlier migration. It cannot cycle and is the edge that one should have carried.
- **A latent bug in the writer had to be fixed first.** `write_migration_file` deduped a dependency
  by migration **name** alone — harmless for function migrations, whose names never collided, but
  cross-app edges routinely point at an `0001_initial`, so the scaffold's own masked another app's
  and the edge was dropped. Found by reading a generated file: it was computed, then discarded.

**Reversibility.** High, unlike most decisions here: dependencies are graph metadata, not inlined
SQL, so removing an edge changes ordering and nothing else. No database carries a trace of it.

## Alternatives considered

- **Depend on the referenced app's leaf.** What a hand-patching consumer reaches for, and what the
  old doc implied. Over-constrains the graph, drags unrelated migrations forward, and is the one
  shape that can genuinely cycle. Rejected — the failure mode, not the fix.
- **Refuse the rule when a reference is unresolvable.** Symmetrical with the tenancy refusal
  ([ADR 0012](0012-cross-owner-last-owner-guard.md)). Rejected — it withdraws working rules over an
  app with no migrations of its own, and drags the shared verdict into the migration layer.
- **Emit a no-op migration carrying only the missing edges.** Automatable retrofit. Rejected: an
  operations-free migration has an empty digest, so it needs its own identity scheme, and it grows
  history in every app to fix a handful of files.
