# 0029 — triggers are retired with their key, every retirement says `IF EXISTS`

- **Status:** accepted
- **Date:** 2026-10-03
- **Affects:** `graph.dropped_tables`, `graph.recreated_tables`, `_retired_cascade_operations`, `_retired_trigger_operations`, `_subtract_retired`, every `HEADER_*_RETIRED`

## Context

A plpgsql trigger body records no `pg_depend` dependency on the tables and columns it names. So `DROP TABLE … CASCADE` and `DROP COLUMN … CASCADE` take the rules beside a trigger and leave the trigger, which then fails **every** `UPDATE` on the table it fires on. Four of the kit's triggers name another table or a key column: the revive (on the owner, naming the child, #63), the owned sweep and the self-cascade trigger (naming the key column), and the tenant autofill trigger. Through 2.12.0:

- a deleted child's key was only *named*, since an unmapped table is a deleted model on one reading and an app outside `LOCAL_APPS` on the other;
- the owned rule, its sweep and the self-cascade trigger were never retired at all;
- the scan read a column `RetireEnforcement` as dropping the owner's revive and sweep, which it never does;
- every retirement drop was strict, so a drop of a rule `DROP … CASCADE` had already taken failed `migrate`;
- a table deleted and recreated kept headers for objects it no longer had, so nothing re-created them.

## Decision

- **Evidence from migration history.** `graph.dropped_tables` walks the migration **graph** forward once, reading state before each operation, and returns each table a top-level `DeleteModel` dropped and no model holds by the end, with the migration that dropped it; the generator then leaves out any table a model in the registry holds now. A recorded cascade key whose child is in that set is retired, ordered after that migration.
- **Retire the trigger families.** A recorded owned key no `OwningForeignKey` declares any more, and a self-cascade key the models no longer call for, are retired in the app hosting the table they fire on. Like the cascade family they record their creates, settle their retirements against the graph, and depend on the newest create in another app: an MTI descendant's pass writes a self-cascade trigger into the descendant's app. A declared but refused owned key is left to the existing `--check` failure.
- **Every retirement says `IF EXISTS`**, over every name the tables have held. The new retirements' reverse, and a deleted child's, refuses and points at `--adopt`; a cascade or autofill retirement still recreates what it can.
- **The scan forgets only what `RetireEnforcement` drops**: a trigger only on a whole-table retirement of the table it fires on.
- **A recreated table fails `--check`.** `graph.recreated_tables` names the migration that took a dropped table back. It is decided **per object**: every object recorded on that table that the current models still call for, and none of whose creates descends from the recreate, is named, and `--check` fails pointing at `makeguitarmigrations --adopt`. One object re-created on the table vouches for no other.

## Why

- **Why migration history?** It is the only record that tells "deleted" from "out of scope": an app dropped from `LOCAL_APPS` is still installed and its models are in the final state. The graph, not the files, because a pending squash leaves replaced migrations on disk that the graph has dropped. Per operation, because a squash creates and deletes in one file and a rename can precede the delete. Top level only, so a `SeparateDatabaseAndState` move between apps keeps its table.
- **Why `IF EXISTS`, against [ADR 0019](0019-migration-lifecycle-objects.md)?** That rule exists because `IF EXISTS` where the answer is known hides a diverged database. For a retirement the answer is not known: `DROP … CASCADE` takes a rule with the column or table it reads, on Django 5.x before the generator's drop runs, and the docs advised hand-dropping sweep and self-cascade triggers. Absent is the goal, and the drop names every spelling the tables held, so tolerating absence cannot hide a live object under another name. Creates keep the strict forms.
- **Why detect a recreate rather than repair it?** Repair needs a creating migration recorded per key for every family, tenant policies included, plus adopt-form re-emission ordered after the recreate. `--adopt` already does the re-emission, and its object references resolve to the *last* migration establishing a table, the recreate, so its output descends from it and the check goes green.
- **Why a refusing reverse?** What a retirement dropped reads a column or table the models no longer have. Unapply with `--fake`, restore the models, run `makeguitarmigrations --adopt`: a plain regeneration still reads the key as covered.
- **Strongest objection.** `IF EXISTS` would also hide a drop that ran before its create on a fresh `migrate`, the create then bringing the object back. Every family therefore orders its retirement after its create (ADR 0021), and a deleted child's after its deletion too.

## Consequences

**Accepted costs.** An unscoped `--check` turns red for every project carrying such a leak until the retirements are generated; that is the point. A deletion the loader cannot see, because the deleting app was later removed with its migrations, is still only named. One pass over the migration state per run, a few milliseconds on the test project. A fresh `migrate` can still order a deleted child's `DeleteModel` before the owner's older enforcement migration (#61).

**Reversibility.** Restoring strict drops would break `migrate` for every project that followed the hand-drop advice or ran `RemoveField` on Django 5.x. The retirement migrations already written stay valid either way.

## Related

- [ADR 0019](0019-migration-lifecycle-objects.md) · [ADR 0021](0021-retirement-ordered-against-its-create.md) · [ADR 0024](0024-inverse-cascade-revive-rules.md) · [`migrations.md`](../migrations.md) · #63 · #66
