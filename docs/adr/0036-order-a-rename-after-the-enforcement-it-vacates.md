# 0036 — a rename or drop of a table is ordered after the enforcement migrations naming it

- **Status:** proposed
- **Date:** 2026-10-08
- **Affects:** `enforcement.ordering.order_after_enforcement`, `graph.vacated_tables`, `graph.vacating`, `OperationsMixin._missing_rename_edge_notes`, the `makemigrations` override's `write_migration_files`

## Context

[ADR 0013](0013-cross-app-migration-dependency-edges.md) orders an enforcement migration **after** the migration that creates what its SQL names. Nothing ordered it **before** a *later* migration that renames that table away or drops it, in another app. A fresh `migrate` could run that migration first and fail with `relation "anc_root" does not exist`; an incrementally migrated database, applied in the order it was written, never did, so the two diverged (#61). Every consumer's test-database creation hit it once such a rename or delete existed. A rule is a `RunSQL`, invisible to migration state, so nothing in Django's own graph says the later migration must wait.

## Decision

- **The rename or drop depends on the enforcement migration.** `graph.vacated_tables(loader)` replays the whole graph and names, per migration, each table it renames away (`RenameModel`, `AlterModelTable`, a rename that moves nothing excluded) or drops (`DeleteModel`), including the database half of a `SeparateDatabaseAndState`. A table a later model takes again is still named: the older file needs the edge all the same. An enforcement migration of **another** app (the same app's chain already orders it) whose SQL names such a table is added to that migration's dependencies, less any edge the graph already implies.
- **Fresh files only, before they are written.** The `makemigrations` override's `write_migration_files` calls `order_after_enforcement` on the migrations Django has just built, so the file carries the edge from its first save. A migration already on disk is never rewritten, which includes the leaf `--update` rewrites: it is skipped, and `--check` names it.
- **`--check` names the rest.** `_missing_rename_edge_notes` walks the same history against the enforcement files on disk and fails with the migration to edit and the tuple to paste, by graph reachability both ways (an ordering that exists through another path is not reported, and neither is the reverse, which Django rejects).
- Not gated on `GUITARS_AUTO_MAKE_MIGRATIONS`: the edge says nothing about generating enforcement, and a project that generates it by hand needs the order as much.

## Why

- **The edge belongs on the vacating migration.** The enforcement file is older, often applied, and carries a digest the guard trusts; the rename is the new file, written once, and free to carry a dependency.
- **Rejected: `run_before` on the enforcement migration.** A `run_before` naming a migration that does not exist yet raises `NodeNotFoundError`, so it could only ever be a rewrite of a file on disk, which ADR 0013 already declined (and which a later squash of the rename would break).
- **Rejected: make the old SQL survive the rename.** PostgreSQL resolves a name when it parses the statement and the table's OID is not known when the file is written; any lookup at `migrate` time is what [ADR 0006](0006-inline-generated-migration-sql.md) forbids.
- **Rejected: patch files already on disk.** It rewrites consumer history, and on a database where only the rename had been applied (a failed fresh `migrate`) a new dependency raises `InconsistentMigrationHistory`.
- **Strongest objection.** ADR 0013 argued its edges are a proof: they point backwards, so cannot cycle. These point **forward**, from a new migration to an old one, and are safe for the same reason in the other direction: the new migration cannot be reached from a file that was written before it existed.

## Consequences

**Accepted costs.** A rename or delete written while the override was bypassed, or by plain Django's `makemigrations`, is covered by `--check` alone. A rename file in a package the consumer cannot edit has no remedy but the message. Column-level changes (`RenameField`, `RemoveField`, a `db_column` move) are not read: only tables. A file written *after* the rename and naming the same table, because a model took the name again, is skipped when it already depends on the rename; one that does not is named, and the edge is correct for it too.

**Reversibility.** High: a dependency is graph metadata no database records, and removing the call and the check restores 2.17's behaviour.

## Related

- [ADR 0013](0013-cross-app-migration-dependency-edges.md) · [ADR 0019](0019-migration-lifecycle-objects.md) · [ADR 0021](0021-retirement-ordered-against-its-create.md) · [`migrations.md`](../migrations.md) · #61, #66
