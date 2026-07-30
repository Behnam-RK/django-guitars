# Enforcement migrations

## Vocabulary

An **enforcement migration** is a generated migration of `RunSQL` operations.
Django's own migrations describe *schema* — which tables and columns exist. These
describe what the database **guarantees about the rows**, for every code path
including the ones that never call `save()`.

An **enforcement operation** is one `RunSQL` entry inside such a migration. There
are four kinds, each with a precise name of its own:

| Kind | What it does |
| --- | --- |
| timestamp trigger | keeps `_updated_at` current on any `UPDATE` |
| soft-delete rule | rewrites `DELETE` into a `_deleted_at` stamp |
| MTI redirect rule / trigger | the same two, for a multi-table-inheritance child |
| tenant policy | row-level security scoping rows to a tenant |

All four ship from one command, because they share every mechanic that is
actually difficult: model discovery, MTI column-ownership resolution, dedupe
against operations already written, `--empty` scaffolding, digest stamping and app
scoping.

## Generating them

By default you never run the generator directly — `makemigrations` is extended to
run it right after the schema migrations, so the two cannot drift:

```bash
python manage.py makemigrations        # schema + enforcement
python manage.py makemigrations --check   # CI: fails if either layer is missing
```

Tell it which apps are yours:

```python
LOCAL_APPS = ["blog", "shop"]      # apps the generator scans
# TRIGGER_FUNCTION_APP = "blog"    # optional; hosts the shared function migration
```

Prefer the explicit two-command workflow?

```python
GUITARS_AUTO_MAKE_MIGRATIONS = False
```

```bash
python manage.py makemigrations
python manage.py makeguitarmigrations
```

> ⚠️ With `GUITARS_AUTO_MAKE_MIGRATIONS = False`, plain `makemigrations` does
> **not** create the rules. Until `makeguitarmigrations` runs and you `migrate`,
> `.delete()` permanently deletes rows.

Both commands accept app labels to scope generation, mirroring Django's own
`makemigrations`. An unknown label is rejected the same way Django rejects it —
because a typo that silently matched no app would turn `--check` into a no-op
that exits 0 having validated nothing.

## What drives generation

The shape of the model *is* the opt-in. There is no registry:

- `_updated_at` → a statement-level timestamp trigger
- `_deleted_at` → a soft-delete rule, plus cascade rules for related
  soft-deletable models whose FK is `on_delete=CASCADE`
- a `TenantedManager` → a row-level-security tenant policy

## Idempotency has two layers

Both matter, and they answer different questions.

1. **A `[DIGEST:…]` marker** on the generated migration's first line identifies an
   unchanged *operation set*. If nothing about an app's operations changed, no new
   migration is written.
2. **Per-operation comment headers** (`# Updated at Trigger on "x" table!`)
   identify which tables are already covered, so a *partially* covered app
   receives exactly the operations it lacks.

The digest alone cannot handle a partially covered app; the headers alone cannot
tell "already done" from "done differently". Hence both.

> ⚠️ **Those header strings are frozen.** Reword one and every existing migration
> stops being recognised, and the next run emits duplicates. The same goes for
> the public names in `guitars.sql`: generated migrations in consuming projects do
> `from guitars import sql` and read them by name, so a rename breaks `migrate` on
> a fresh database there. `tests/test_sql_interface.py` guards the names;
> `FROZEN_SQL_CONSTANTS` / `FROZEN_SQL_CALLABLES` is the list.

## Singleton function migrations

Two PL/pgSQL functions are shared by every table, so each gets exactly one
migration in `TRIGGER_FUNCTION_APP` (default `LOCAL_APPS[0]`), which the per-app
migrations depend on:

- `set_updated_at()` — for own-table timestamp triggers
- `set_parent_updated_at()` — for MTI children, so a child-only `UPDATE` bumps the
  ancestor's `_updated_at`

They are separate migrations on purpose: adding MTI support must not re-digest —
and therefore regenerate — the existing single-table function migration.

The singleton is still ensured in its host app even when a scoped run named a
different app, because every other enforcement migration depends on it.

## Cross-app cascades

A cascade soft-delete rule ("deleting a `Band` cascades to its `Album`s") is
written into the **parent** model's migration — `Band`'s app. If that parent's app
is not named in a scoped run, the rule is skipped even when the child's app is.

This is the accepted "pragmatic scope" tradeoff, mirroring Django, which also only
touches the apps you name. It is not silent: the command prints a warning naming
the skipped rule and the app to include. Run without labels, or name the parent's
app, to close it.

## Scaffolding

The generator does not template a migration from scratch. It runs
`makemigrations --empty` and rewrites the result, which is why the
`makemigrations` override skips the enforcement step on `--empty` — otherwise the
two would recurse. Django prints the path it wrote rather than returning it, so
the filename is parsed back out of the captured output, and a failure to match is
raised rather than guessed at: the alternative is rewriting whichever file a glob
happened to find first.

The generated import is inserted after the scaffold's **last** import rather than
at a fixed offset, so a change to Django's `--empty` template cannot land it
inside the class body.

## Staging row-level security

`GUITARS_RLS_FORCE` defaults to `True`, so policies bind on first migrate. For a
retrofit onto a populated database you can ship them inert and force them later:

```python
GUITARS_RLS_FORCE = False    # policies exist; the owning role bypasses them
```

```bash
python manage.py makeguitarmigrations --force-rls    # later, once the soak is clean
```

That stage reads the `force=` literal each policy operation was generated with, so
it only touches policies that really shipped inert. Running it on a fully-forced
database correctly does nothing.

Note that `force` and `exempt_roles` are written into the migration as **literal
arguments**, never read from settings at migrate time. A migration whose SQL
depended on the settings in force when it ran would produce different databases
from the same migration history — and would silently change an already-reviewed
migration's meaning when someone edited a setting.

### A policy whose shape changes gets replaced

A tenant policy is the one enforcement operation whose SQL is not a function of
the table name alone: its predicate comes from a *variable* `{dimension: column}`
mapping. So its comment header carries a `[POLICY:<digest>]` identity covering
everything that determines what the policy says — the dimensions, the columns,
the owner join, and `exempt_roles`:

```python
# Tenant RLS on "billing_invoice" table! [POLICY:57ff74989db7]
```

A run that finds a recorded identity different from the one the models now imply
emits a **replacement** rather than nothing:

```python
# Tenant RLS replaced on "billing_invoice" table! [POLICY:0b7c94cc2edc]
migrations.RunSQL(
    sql=sql.replace_table_rls(table='billing_invoice', columns={...}, force=True),
    reverse_sql=sql.drop_table_rls(table='billing_invoice'),
)
```

Three ordinary changes take that path: a model gaining or losing a tenant
dimension, a renamed tenant column, and an edited `GUITARS_RLS_EXEMPT_ROLES`.
Without the identity, the header recorded only that *some* policy existed, so a
model that gained a dimension kept the old, weaker predicate in the database
while `makemigrations --check` reported nothing to do.

`force` is deliberately **not** part of the identity. It is an `ALTER TABLE`
rather than part of the policy, and it has its own staged mechanism above —
folding it in would make flipping `GUITARS_RLS_FORCE` replace every policy and
defeat the retrofit that setting exists for.

`replace_table_rls` drops and recreates the policies but leaves `ENABLE` and
`FORCE` alone, so at no point in the transaction is the table enabled-but-
unpolicied (which is default-DENY) or policied-but-disabled (which is no
protection). Its `reverse_sql` drops RLS rather than restoring the previous
predicate, which the generator does not know: reversing past the migration leaves
the table unpolicied, and rolling forward rebuilds the current shape.

`audittenancy` covers the other half — a replacement that was generated but never
applied. See [`tenancy.md`](tenancy.md#auditing).

## Migrate runs bypassed

`migrate` is overridden to run inside `tenancy_bypassed()`. Without it a
`RunPython` backfill under row-level security matches no rows and silently does
nothing, then gets marked applied.

That override is why `guitars` must appear in `INSTALLED_APPS` **before** anything
else defining a `migrate` command: Django's `get_commands()` walks
`reversed(get_app_configs())` and lets each app overwrite the previous entry, so
the app appearing earliest wins.

## Editing the SQL

Changing enforcement behaviour means editing the relevant module under
`src/guitars/sql/` **and** verifying `makeguitarmigrations` still emits and
matches it — the `_RE_*` regexes key off the comment headers in the generated
operation templates.

Adding a new SQL name means re-exporting it from `sql/__init__.py` *and* recording
it in `FROZEN_SQL_NAMES`, so a later rename is caught rather than shipped.

Changing an existing SQL *value* is a different matter: databases already migrated
carry the old text, because the migration called the constant by name at the time
it ran. Re-applying the enforcement migration (down one, then up) replaces them.

## Related

- [Soft deletion](soft-deletion.md) · [MTI](mti.md) · [Tenancy](tenancy.md)
