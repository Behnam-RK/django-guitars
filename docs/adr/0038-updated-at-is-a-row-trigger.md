# 0038 — `_updated_at` is a row trigger that assigns, not a statement trigger that re-updates

- **Status:** accepted
- **Date:** 2026-10-08
- **Affects:** `stamp_updated_at()`, `triggers._CREATE_STAMP_UPDATED_AT_TRIGGER`, `HEADER_STAMP_FUNCTION`, `Command._ensure_stamp_function_migration`, `ExistingOperations.stamp_function_dependency`, `set_updated_at()` (frozen)

## Context

Issue #80, found on a consumer's staging database: one single-row `UPDATE` of a table with six cascade rules became 35 `Update` nodes over 13 tables, 65 trigger firings, and `updated_at_trigger` was 466 of 576 ms. The rows themselves cost 6 ms.

The trigger was `AFTER UPDATE … REFERENCING NEW TABLE … FOR EACH STATEMENT`, calling `set_updated_at()`, which ran a second `UPDATE <table> SET _updated_at = NOW()` through `EXECUTE format(…)`. Three costs followed. The dynamic SQL is planned on every call. The follow-up is itself a statement on the table, so its `ON UPDATE` rules are expanded and planned again, and each rule-generated child `UPDATE` fires that child's own `updated_at_trigger`. And every row touched is written twice, doubling dead tuples and WAL. `WHEN (pg_trigger_depth() = 0)` stopped the trigger firing on itself but not the rewriter. The statement-level shape is in the initial commit; no ADR or changelog entry gives a reason for it.

Alternatives on the table: a `BEFORE ROW` assignment that is conditional on the caller not having set the column; the same form unconditional; keeping the statement trigger and caching its plan per table.

## Decision

- **`BEFORE UPDATE … FOR EACH ROW` calling `stamp_updated_at()`**, whose body is `NEW._updated_at := NOW(); RETURN NEW;`. Same trigger name (`updated_at_trigger`) and the same header (`HEADER_UPDATED_AT`), so the scan reads it as the same object; its `[SQL:…]` identity moves, so every project's next `makeguitarmigrations` emits a replacement per table: `CREATE OR REPLACE TRIGGER` (PG 14, the floor), not `DROP` + `CREATE`, since one migration per app would otherwise hold `ACCESS EXCLUSIVE` on every table of the app until it commits. Its reverse puts the statement trigger back where that is what was recorded (keyed on the digest it recorded), and drops otherwise.
- **Always assigns.** `save()` writes the value it loaded, which reads as "set by the caller" once another transaction has moved the row, so the conditional form (`IS NOT DISTINCT FROM OLD`) would keep a stale value. The column is DB-managed and `editable=False`; a caller cannot set it, as before.
- **No `WHEN (pg_trigger_depth() = 0)`.** The guard existed to stop the old form re-firing on its own follow-up. A row trigger issues none, so every row an `UPDATE` touches is stamped at any depth. That closes the accepted gap in [ADR 0018](0018-self-referential-cascade-trigger.md): cascade children below the first level of a self-referential tree no longer keep a stale `_updated_at`, and a raw `DELETE` and the ORM collector agree. The `_deleted_at` fast path therefore no longer stands aside for a self-referential key (`cascade_plan` drops its non-blocking gap), and `tests/test_delete_characterization.py` already compares both paths.
- **A new private function, not a replacement.** `stamp_updated_at()` sits beside the frozen `set_updated_at()`, in its own singleton migration (`HEADER_STAMP_FUNCTION`, recorded as `stamp_function_*`). `set_updated_at()` stays in every migrated database, called by nothing once its tables are regenerated, and is neither dropped nor ensured again; a fresh project never creates it. Its public constants, `HEADER_TRIGGER_FUNCTION` and `_RE_TRIGGER_FUNCTION` stay, to read history.
- Own-table triggers depend on the stamp function's migration, and so does the MTI parent function's, for ordering only: it calls nothing of it.
- The splices `, _updated_at = NOW()` in the revive, sweep and self-cascade bodies stay: they keep those families' `[SQL:…]` identities still and cover a table whose trigger has not been regenerated yet.
- **Out of scope:** `set_parent_updated_at` (the MTI parent trigger) must update another table, so it keeps its statement trigger, its guard and its second `UPDATE`; the ancestor's new row trigger stamps the same `NOW()` again on it, harmlessly. Tracked as a follow-up.

## Why

Replacing `set_updated_at()` in place was the smaller change and was refused: the function is called by a *statement* trigger on every table not yet regenerated (another app's migration unapplied, or a scoped run), where `NEW` is unassigned, so every `UPDATE` there would fail until each table was migrated. A new name makes the two forms coexist for as long as a project takes to migrate.

The strongest objection to a row trigger is per-row overhead on a bulk `UPDATE`. A plpgsql `BEFORE ROW` assignment adds a small per-row cost, which is not benchmarked here: #80 measured the rows themselves at 6 ms of 576, and the assignment replaces the second statement, the dynamic plan and the transition table. The strongest objection to *removing* the depth guard is that it widens what the trigger touches; it widens it to exactly the rows the statement already writes, and the collector path already stamped them.

## Consequences

**Accepted costs.** Every project regenerates one function migration and one replacement migration per app, and `--check` is red until it does. The restoring reverse reads the frozen `set_updated_at()`, so it holds only while that function exists, which a migrated database keeps. `set_updated_at()` lingers as an unused function. The MTI parent trigger still pays its second statement.

**Reversibility.** Easy for the database (a trigger swap), hard in practice for consumers: it is a minor release that every project must migrate, so reverting means another one.

## Related

- [ADR 0018](0018-self-referential-cascade-trigger.md) · [ADR 0019](0019-migration-lifecycle-objects.md) · [ADR 0026](0026-soft-delete-and-delete-fast-path.md) · [`migrations.md`](../migrations.md) · [`mti.md`](../mti.md) · #80
