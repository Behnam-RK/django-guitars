# 0037 — a model moved between apps is read as a rename, and its coverage followed to the end of its chain

- **Status:** accepted; superseded in part by [ADR 0043](0043-the-scan-replays-the-migration-graph.md), which replays the graph and removes `_chain_ends` and `keep_existing`
- **Date:** 2026-10-08
- **Affects:** `graph.moves_between_apps_by_migration`, `graph._moved_out`, `scanning._move_renamed(keep_existing=)`, `scanning._chain_ends`, `scanning._join_chains`, `scanning._latest_names`

## Context

[ADR 0019](0019-migration-lifecycle-objects.md) re-keys recorded coverage onto the table a model now uses, from the renames in each app's own migration state. A model **moved between apps** (`SeparateDatabaseAndState`: an `AlterModelTable` on the database side, a `DeleteModel` on the state side, the destination app creating it state-only) is a rename the state cannot show: the state after has no such model, so the new table was never read, and the destination read as uncovered. The next run emitted a plain `CREATE TRIGGER` that collides (#66).

The scan walks one app's files at a time, in registry order, not in time. Entries another app filed under the new name may be older or newer than the move; entries it filed under the old name may be walked after the move was read.

## Decision

- **The move is read off the operation**, apart from same-app renames: `moves_between_apps_by_migration` takes the new table from the `AlterModelTable`, where `renames_by_migration` still reads the state either side.
- **A move never overwrites.** In the walk it uses `_move_renamed(..., keep_existing=True)`: what the destination already holds stays, the moving entry fills a gap, and dependency lists merge. A same-app rename keeps overwriting: inside one app's walk whatever is under the destination predates it.
- **What the walk could not move is carried afterwards.** After the walk, and before any retirement is settled, each moved name (and each name it moved onto) is followed to the end of its chain, moves then renames (`_chain_ends`), nearest the end first so the newer entry is the one kept, over every family, skipping a name a live model holds.
- **Chains are joined across apps** (`_join_chains`): a rename in the destination's app extends the chain the move began in the source's, so every spelling the table held is dropped, and where two chains share a name the longest says where it ended (`_latest_names`), never the dict order.

## Why

- **Chronology cannot be assumed across apps.** Overwriting, the same-app rule, drops a newer destination entry; skipping the move leaves the old rule live. Keeping what is there and following the chain afterwards is right whichever app is scanned first, which the real-generator runs of the review confirmed in every order tried.
- **Rejected: record the move in the destination's history.** The destination's migration is state-only, with no table to read the old name from.

## Consequences

**Accepted costs.** A model moved *back* (`anc -> shop -> anc`) keeps the older record under the original name rather than the newer one made on the way: a stale rule name, no functional loss, still reached by the drops over every spelling. Two models leaving the same old name at different times share one entry in the move map; the last wins. Neither case was found in a real history.

An enforcement migration naming a moved table is ordered after the destination's state-only `CreateModel`, not after the migration doing the database rename, so whether a fresh `migrate` works can depend on app names; the move's database half has to be ordered first by hand. This predates 2.18.0 and [ADR 0036](0036-order-a-rename-after-the-enforcement-it-vacates.md) does not cover it.

A name freed by a move and retaken by a live model is [#66](https://github.com/Behnam-RK/django-guitars/issues/66)'s deferred shape.

**Reversibility.** High: the scan only reads. Removing the move route restores 2.17's behaviour for these histories.

## Related

- [ADR 0019](0019-migration-lifecycle-objects.md) · [ADR 0021](0021-retirement-ordered-against-its-create.md) · [ADR 0036](0036-order-a-rename-after-the-enforcement-it-vacates.md) · [`migrations.md`](../migrations.md) · #66
