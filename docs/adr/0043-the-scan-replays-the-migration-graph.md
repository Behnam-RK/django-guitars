# 0043 — the scan replays the migration graph

- **Status:** accepted
- **Date:** 2026-10-09
- **Affects:** `scanning.scan_existing_operations`, `graph.replay_plan`, `graph.table_changes`, `ExistingOperations.existing_digests`
- **Amends:** [ADR 0019](0019-migration-lifecycle-objects.md), [ADR 0021](0021-retirement-ordered-against-its-create.md), [ADR 0029](0029-retiring-a-deleted-childs-revive-trigger.md); supersedes in part [ADR 0037](0037-a-model-moved-between-apps-is-a-rename.md)

## Context

The scan read every local app's files in **registry order, then filename order**, last write winning, and compensated for that being no clock: a `live_tables` guard standing in for "a model retakes the name later", `_chain_ends` carrying a move to its end, `_rekey` moving the rest after the walk, `keep_existing` so a move never overwrote what another app filed. Nothing reacted to a `DeleteModel`. Three shapes of #66 followed, each reading as covered with `--check` green while the database held nothing, or something stale: a model deleted and later recreated on its table (its rows deleted outright, and under tenancy RLS off); a renamed model's old table retaken by a new model; a model moved to another app and back, whose owned sweep kept naming the intermediate table, failing every `UPDATE` of its owner.

## Decision

- **One order, the one `migrate` runs.** `graph.replay_plan` gives every migration in the plan of every leaf (Django sorts leaves and parents, so it is deterministic), a squash walked as the files it replaced where they are on disk, with the **table events** its operations make in operation order: `create`, `rename`, `drop`, `retire`. `table_changes` reads the database half of a `SeparateDatabaseAndState`, so a move to another app is a rename, a state-only create or delete nothing.
- **Events, then headers.** For each migration the scan applies its events, then reads its headers: a later file wins by being later. A file the graph does not know (an app with no migrations in the plan, a synthetic one) is read after all of them with no events, and one a squash replaced that survives beside an unexpanded squash just before it.
- **A rename moves every family with it** (scalars overwritten, lists of creates merged, older first), its retirement sites included, and records the chain `renamed_tables` keeps. A header written afterwards that still names the emptied name is **forwarded** to the new one, until a `create` takes the name again or a `drop` clears it. That last rule is shape (b): the retaker is a new table.
- **A drop forgets what lived on the table or fires on it**: its own-table rule and triggers, MTI rule and parent trigger, tenant policy with its identity, SQL and FORCE record, its autofill triggers, and the trigger families firing on it (revive and sweep on the owner, self, revive-owner and cascade-owner on the table). It keeps the rule families keyed on it as a related table and every create node, since [ADR 0029](0029-retiring-a-deleted-childs-revive-trigger.md)'s retirement drops them with `IF EXISTS`.
- **A file whose tables were since vacated stops vouching for its `[DIGEST]`.** A recreated table, or one identical to its predecessor, regenerates an operation set equal to an earlier file's, and the file-level guard skipped it for good. `retirement_apps` is untouched ([ADR 0021](0021-retirement-ordered-against-its-create.md), point 9).
- **Retirement and create are still settled by the graph**, per key, after the replay: a line through nodes the graph leaves unordered must not read "unordered" as "later". A squash's replaced files are ordered by their place in `replaces`.
- Gone with their reasons: `live_tables`, `_rekey`, `_current_key`, `_chain_ends`, `_join_chains`, `_latest_names`, `keep_existing`, the `spellings` loop of a retirement, the `carried` exemption of the orphaned-MTI note, and the graph's `renamed_tables`, `renames_by_migration`, `moves_between_apps_by_migration` and `retired_enforcement`.

## Why

Every compensation existed because the replay was not in time order, and a drop had nowhere to act. With one order a rename, a move and a retake are the same event stream, and (c)'s cycle falls out of overwriting at each rename. The new walk is one forward pass over the state, where the rename helpers built a project state twice per migration per app.

## Consequences

**Accepted costs.** A migration that both changes a table and carries enforcement headers is read events first, then headers, whatever their order in the file: the kit never writes one (its files are `RunSQL` alone), a hand-merged one reads the header as written after the change. Two migrations the graph leaves unordered are replayed in `migrate`'s order, so a header may meet an event it did not precede: exactly the missing edges [ADR 0036](0036-order-a-rename-after-the-enforcement-it-vacates.md) names under `--check`. A table change made by `RunSQL` is invisible, as before. A final squash whose replaced files are gone carries no header comments. A parent dropped under a live MTI child is unreachable, Django refusing the state. A dropped table's autofill and singleton functions outlive it. Plpgsql bodies elsewhere that name a dropped table survive it and are retired as [ADR 0029](0029-retiring-a-deleted-childs-revive-trigger.md) says.

**Behaviour change.** A project with a history of the three shapes gets the missing coverage on the next `makemigrations`: a plain `CREATE` for the recreated or retaking table, a re-emitted sweep and owner trigger for a model moved back.

**Reversibility.** The scan reads only; going back is code.

## Related

- [ADR 0013](0013-cross-app-migration-dependency-edges.md) · [ADR 0019](0019-migration-lifecycle-objects.md) · [ADR 0021](0021-retirement-ordered-against-its-create.md) · [ADR 0029](0029-retiring-a-deleted-childs-revive-trigger.md) · [ADR 0036](0036-order-a-rename-after-the-enforcement-it-vacates.md) · [ADR 0037](0037-a-model-moved-between-apps-is-a-rename.md) · #66
