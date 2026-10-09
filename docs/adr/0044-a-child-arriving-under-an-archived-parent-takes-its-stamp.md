# 0044 — a child arriving under an archived parent takes its stamp

- **Status:** accepted
- **Date:** 2026-10-09
- **Affects:** `_guard_operations`, `_retired_guard_operations`, `sql.soft_delete._CREATE_SOFT_DELETE_GUARD`, `RetireEnforcement`, `GUITARS_CASCADE_GUARD`
- **Amends:** [ADR 0039](0039-cascade-arms-in-the-owner-trigger.md) (its arms record no dependency; the guard's trigger carries no column list for the same reason)

## Context

The cascade is an arm of the owner's trigger, and it only reaches rows that exist when the archive's statement starts. Two sessions showed what that leaves (#91). A child inserted under a parent while another session archives it commits live under an archived parent, in either order, and so does a child re-pointed at one. The archive is an `UPDATE` of a non-key column, which takes `FOR NO KEY UPDATE`. The foreign-key check of the insert takes `FOR KEY SHARE`, and those two do not conflict. The rule the arms replaced had the same snapshot, so this is old, but it was never measured, and READ COMMITTED and REPEATABLE READ both leak.

## Decision

- **Every cascade child table carries one guard**, `soft_delete_guard_on_<n>_<table>`: a `BEFORE INSERT OR UPDATE … FOR EACH ROW` trigger with one block per cascade key. A block runs only when the key is set, the row is live and the key is new or moved. It reads the parent with `SELECT _deleted_at … FOR SHARE` and, if the parent is archived, copies the parent's stamp onto the child.
- **`FOR SHARE` is the point.** It conflicts with the archive's lock, so a child waits for an uncommitted archive and then sees it, and an archive waits for an uncommitted insert and then archives its child through the arm. Both orders end consistent, at READ COMMITTED.
- **A key declared on an MTI descendant** archives the ancestor's row: an `AFTER` trigger running `UPDATE <ancestor> SET _deleted_at = stamp WHERE pk = NEW.<link> AND _deleted_at IS NULL`, as `NEW` of the descendant cannot carry a column its ancestor owns.
- **The stamp is the parent's own**, so the revive arm (ADR 0024) matches it and a restore of the parent brings the late child back. A child archived on its own keeps its stamp.
- **No column list.** `UPDATE OF <key>` would record a dependency on the key, so `RemoveField` of a cascade key would fail `DROP COLUMN` before the migration re-emitting the guard could run. The function returns early unless the key changed.
- **On by default; `GUITARS_CASCADE_GUARD = False` retires every recorded guard**, `IF EXISTS`, with a reverse that refuses and points at `--adopt`, as the other retirements do. A table that stops being a cascade child retires its guard the same way, and `RetireEnforcement` takes it with the table's other triggers, in its column form too: the body names the key and `_deleted_at` and records no dependency, so one left behind fails every write to the child once the column is gone, not only an archive.

## Why

The alternatives were to document the gap and to lock the parent inside the arm. A lock in the arm cannot help the insert that commits first, which is the more common order. A guard on the child closes both, and the cost is one lookup per write that sets or moves the key.

## Consequences

**Behaviour change.** A child created under an archived parent is archived with it, where it was live. Code that relied on creating a live child under an archived parent now needs the parent restored first. The generator writes one migration per app hosting a cascade child on upgrade.

**Cost.** Each insert, and each update that moves the key, reads the parent and holds `FOR SHARE` on it until commit, so it waits for a concurrent update of that parent row, and a parent updated at once by a stream of inserts is contended. The share locks of two inserts do not conflict with each other but each conflicts with the other's later non-key `UPDATE` of the parent, so two transactions that each insert a child and then update the same parent (a denormalised total, a counter) **deadlock**, where without the guard they serialise: PostgreSQL aborts one with `40P01`. The archive's lock is the same mode as that update's, so no weaker lock closes the race and spares this; `GUITARS_CASCADE_GUARD = False` is the way out, and `tests/test_concurrency_arms.py` pins it. A bulk load into a child table pays a parent lookup per row. The trigger runs as the invoker, and `FOR SHARE` needs `UPDATE` on the parent table, so where the invoker lacks it the guard reads the parent plainly (`has_any_column_privilege`): no lock, so the race stays open for that role, but no insert fails for want of one. Under row-level security `FOR SHARE` applies the **update** policies, so a parent the invoker may read but not update reads as absent: nothing is stamped and the child stays live, as for a hidden parent (ADR 0039). `session_replication_role = replica` skips it.

**Limits, pinned in `tests/test_concurrency_arms.py`.** An archive in a REPEATABLE READ transaction whose snapshot predates a child's commit still misses that child, since the child was inserted under a live parent; two SERIALIZABLE sessions are refused with `40001`. A stale instance that created the row does not know the stamp the guard gave it, and its later `save()` writes back the `_deleted_at` it loaded, reviving the child, as it does for any child an arm archived. A child restored by hand under an archived parent stays live: the guard reads the key when it arrives, not on every write. In one statement that rewrites a parent's key and moves its children, the guard sees the parent the statement has already rewritten or not by row order, and either outcome is consistent, so `test_a_key_rewrite_that_reparents_its_children_is_still_refused` sets it aside.

**Accepted.** Like an arm, the guard records no `pg_depend`: dropping or renaming a key column fails an insert into the child until `makeguitarmigrations` re-emits the function.

## Related

- [ADR 0014](0014-statement-level-owned-sweep.md) · [ADR 0024](0024-inverse-cascade-revive-rules.md) · [ADR 0039](0039-cascade-arms-in-the-owner-trigger.md) · #91
