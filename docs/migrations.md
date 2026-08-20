# Enforcement migrations

## Vocabulary

An **enforcement migration** is a generated migration of `RunSQL` operations, describing what the database **guarantees about the rows** (not schema, already covered by Django's own migrations) for every code path, including ones that never call `save()`. An **enforcement operation** is one `RunSQL` entry: a timestamp trigger (`_updated_at`), a soft-delete rule (`_deleted_at`), an owned soft-delete rule (`OwningForeignKey`), an MTI redirect rule/trigger, a tenant policy (RLS), or a tenant-autofill `BEFORE INSERT` trigger. All six ship from one command sharing discovery, dedupe, scaffolding, digest stamping, and app scoping. The shape of the model *is* the opt-in — no registry.

## Generating them

`makemigrations` is extended to run the generator right after the schema migrations by default, so the two cannot drift (`--check` fails CI if either layer is missing). Set `GUITARS_AUTO_MAKE_MIGRATIONS = False` for the explicit two-command workflow instead. `LOCAL_APPS` names which apps the generator scans — a label matching no `AppConfig.name` scans nothing and `--check` exits 0, not an error.

> ⚠️ With `GUITARS_AUTO_MAKE_MIGRATIONS = False`, plain `makemigrations` does **not** create the rules. Until `makeguitarmigrations` runs and you `migrate`, `.delete()` permanently deletes rows.

## Generated migrations carry their SQL literally

A generated operation contains the statements it runs, not a reference to a library constant — until 1.1.0 the file did `from guitars import sql`, so a migration's *meaning* depended on the installed kit version: the same migration number built `<> 'on'` rules on 1.0.0 but `= 'off'` on 0.7. `guitars.sql`'s names remain a frozen interface for that committed form's sake.

## Idempotency has three layers

1. **A `[DIGEST:…]` marker** on the migration's first line identifies an unchanged *operation set* — if nothing changed, no new migration is written.
2. **Per-operation comment headers** identify which tables are already covered, so a *partially* covered app receives only what it lacks.
3. **A `[SQL:…]` identity** on each header digests that operation's SQL — what makes a changed SQL constant generate its own migration.

The third exists because the first two missed the case that mattered most: the header scan short-circuits before anything is built, so a recognised header read as covered *forever* — how the 1.0.0 soft-delete guard rewrite reached every existing database as nothing at all. A header with **no** `[SQL:…]` predates SQL inlining and reads as stale, regenerated once on 1.1.0.

> ⚠️ **Header strings are frozen.** Reword one and every existing migration stops being recognised, emitting duplicates. `tests/test_header_corpus.py` guards every scanner against the project's own committed migration history.

## Three forms

Each operation is emitted in one of three forms, chosen from what history records — `IF EXISTS`/`OR REPLACE` are claims about *knowledge*:

| Recorded | Form | Why |
| --- | --- | --- |
| nothing | plain `CREATE` | A collision must fail loudly. |
| a different/no `[SQL:…]` | `DROP`+`CREATE`, no `IF EXISTS` | Known to be ours; an unguarded drop reports drift instead of hiding it. |
| `--adopt` | `DROP … IF EXISTS`+`CREATE` | The flag's premise is that nobody knows what the database holds. |

Soft-delete rules and trigger functions are the two exceptions, always `CREATE OR REPLACE` — no instant without a rule, and `DROP FUNCTION` refuses while any trigger depends on it. `--adopt [app_label …]` exists because `create_tenant_policy` is a bare `CREATE POLICY` (no `IF NOT EXISTS`), so an unrecorded-but-real policy used to fail `migrate` with *already exists*. Cannot combine with `--force-rls` — run `--adopt` first.

## Retirement

The three forms above all *assert* an object. **Nothing else is ever retired**: no rule or trigger of the other five kinds is dropped when the model stops calling for it, so removing the field behind one makes Django's `RemoveField` fail at `migrate` (`rule … depends on column`) and relaxing one — a `CASCADE` foreign key made `SET_NULL`, an `OwningForeignKey` made plain — leaves the rule live with `--check` green. Both want a hand-written `DROP RULE` in the same migration. Tenant autofill triggers are the one kind that can also be **retired**, because their dedupe key is `(table, function)` and the function is named after the `(dimension, column)` pair — so renaming `GUITARS_TENANT_FIELD` or a `db_column` mints a new key and orphans the old trigger, which then dereferences a dropped column and fails *every* `INSERT` on the table. A key recorded but no longer required emits `HEADER_TENANT_AUTOFILL_RETIRED` and a bare `DROP TRIGGER` (`reverse_sql` recreates it), and the scan **subtracts** that key — the only place it does, which is what makes a later re-adoption emit a plain `CREATE` rather than a drop against nothing. Idempotency is the set difference alone, not a digest comparison. Two deliberate refusals: a table no local model maps to is *named, not retired* (no app to migrate into, and no migration-state graph to ask which app owned it), and an orphaned **function** is named rather than dropped (it is inert, `DROP FUNCTION` must follow every dependent trigger, and a scoped run cannot prove an out-of-scope app has none). `--adopt` uses `DROP … IF EXISTS` but never re-retires an already-retired key, since re-asserting an *absence* would grow history on every run.

## Singletons, cross-app cascades, scaffolding

`set_updated_at()`/`set_parent_updated_at()` are shared functions with one migration each in `TRIGGER_FUNCTION_APP`, kept separate so adding MTI support doesn't re-digest the single-table function migration. Tenant autofill hosts its functions there too, but keyed by name rather than as singletons — one per distinct `(column, GUC)` pair, which is **one** for a typical project since `GUITARS_TENANT_FIELD` is project-wide. Its trigger header names the function it calls, so an app depends only on the function migrations its own triggers use. The singleton is still ensured in its host app even when a scoped run named a different app, because every other enforcement migration depends on it. A cascade rule is written into the **parent** model's migration; if that app isn't in a scoped run, it's skipped with a warning naming the app to include — the accepted "pragmatic scope" tradeoff. An *owned* rule has no such gap: it fires on the declaring model's own table, so it always lands in the app being scanned. The graph of ON UPDATE rules the generator refuses cycles from is read over the **whole** registry rather than the apps in scope — scoping narrows what gets written, never which rules exist, and a cycle detected from half the models would be a different set of refusals run to run. The generator rewrites a `makemigrations --empty` scaffold; one that fails after Django wrote it is left in place, carrying no `[DIGEST:…]`, so a later run can't mistake it for coverage.

## Staging row-level security

`GUITARS_RLS_FORCE` defaults to `True`. For a retrofit onto a populated database, ship inert (`GUITARS_RLS_FORCE = False`) and force later via `--force-rls`; `force`/`exempt_roles` are **literal arguments**, never read from settings at migrate time. A tenant policy's SQL depends on a variable `{dimension: column}` mapping, so its header carries a `[POLICY:<digest>]` identity separate from `[SQL:…]` — a changed identity emits a **replacement**, catching a model that gained a dimension while `--check` would otherwise report clean. `force` is excluded from that identity, with its own staged mechanism, and `replace_table_rls` leaves `ENABLE`/`FORCE` alone so the table is never briefly unpolicied. `audittenancy` covers a generated-but-never-applied replacement — see [`tenancy.md`](tenancy.md#auditing).

## Odds and ends

`migrate` runs inside `tenancy_bypassed()` — a `RunPython` backfill under RLS otherwise matches no rows and gets marked applied anyway — so `guitars` must appear in `INSTALLED_APPS` **before** anything else defining `migrate`. Edit SQL under `src/guitars/sql/` and verify the generator still matches it; a new name needs `FROZEN_SQL_NAMES` too, and an existing value needs a down-then-up re-apply since already-migrated databases carry the old text.

## Related

- [Soft deletion](soft-deletion.md) · [MTI](mti.md) · [Tenancy](tenancy.md)
