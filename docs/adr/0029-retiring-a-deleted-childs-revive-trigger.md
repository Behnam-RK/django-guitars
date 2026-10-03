# 0029 — a deleted child's revive trigger is retired on migration-history evidence

- **Status:** accepted
- **Date:** 2026-10-03
- **Affects:** `graph.dropped_tables`, `_retired_cascade_operations`, `_unmapped_cascade_notes`

## Context

Since 2.11.0 a cascade key emits a rule and a statement-level revive trigger, both on the owner's table. Deleting the child model runs `DROP TABLE … CASCADE`, which takes the rule (its action references the child through `pg_depend`) but not the trigger (a plpgsql body records no dependency). The trigger then fails **every** `UPDATE` on the owner with `relation … does not exist`, while `--check` stayed green (#63). The retirement path could not help: it retires only when both tables still map to local models, because an unmapped table is a deleted model on one reading and an app outside `LOCAL_APPS` on the other. `RetireEnforcement` cannot help either: it reaches what depends on the named table, and the leaked trigger depends on nothing and sits on another table.

## Decision

`graph.dropped_tables(loader)` reads positive evidence of a deletion in one forward walk over the migration **graph**, with the state read before each operation: a **top-level** `DeleteModel` in any app the loader knows, whose model owned a table (not a proxy, not unmanaged), and whose table no model holds by the end of the history, nor any model of an app without migrations. A recorded cascade key whose child table is in that set, and whose owner still maps, is retired, both halves, with `DROP … IF EXISTS` over every name the child's table has held and a reverse that refuses. It is no longer named as unretirable, and neither is a key whose owner table was itself dropped. A run scoped away from the owner's app names the trigger it leaves broken.

## Why

- **Why migration history?** It is the only record that distinguishes "deleted" from "out of scope": an app dropped from `LOCAL_APPS` is still installed and its models are still in the final state. The graph, not the files, because a pending squash leaves replaced migrations on disk that the graph has dropped. Per operation, not per migration, because a squash creates and deletes in one file and a rename can precede the delete. Top level only, so a model moved between apps through `SeparateDatabaseAndState` keeps its table. The final-state check stops a later model reusing the `db_table` from being retired; it does not restore a rule the deletion took from it (#66).
- **Why `IF EXISTS`, against [ADR 0019](0019-migration-lifecycle-objects.md)'s rule of reserving it for `--adopt`?** That rule exists because `IF EXISTS` on a path where the answer is known hides a diverged database. Here the answer is known to be "possibly already gone" for both halves: the rule went with the table wherever the deletion ran first, nothing orders the two on a fresh `migrate`, and the stopgap documented on #63 drops the trigger by hand. Absent is the desired end state, and the drop names every spelling the table held, so tolerating absence cannot hide a live object under another name.
- **Why a refusing reverse?** The child model is gone, so nothing says which column the rule read. To migrate back past it, unapply it with `--fake`, then reverse the deletion and regenerate.
- **Strongest objection.** A `DeleteModel` that is later undone by hand, outside migrations, would have its trigger retired. That database has already diverged from its own history.

## Consequences

**Accepted costs.** An unscoped `--check` turns red for every project that already deleted such a child, until the retirement is generated; that is the point. One extra pass over the migration state per run while a recorded key is unmapped. The fresh-`migrate` ordering between the old enforcement migration and the child's `DeleteModel` is not fixed here (#61), nor are the other plpgsql triggers that outlive what they name: the owned sweep, the self-cascade trigger, and a revive after `RemoveField` (#66).

**Reversibility.** Removing the evidence read restores "named, not retired". The retirement migrations it wrote stay valid either way.

## Related

- [ADR 0019](0019-migration-lifecycle-objects.md) · [ADR 0021](0021-retirement-ordered-against-its-create.md) · [ADR 0024](0024-inverse-cascade-revive-rules.md) · [`migrations.md`](../migrations.md) · #63
