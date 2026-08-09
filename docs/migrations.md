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
- a `tenanted_manager()` → a row-level-security tenant policy

## Generated migrations carry their SQL literally

A generated operation contains the statements it runs:

```python
# Soft Delete Rule on "blog_post" table! [SQL:9f3c1a20be47]
migrations.RunSQL(
    sql="""
    CREATE OR REPLACE RULE soft_delete AS ON DELETE TO blog_post
    WHERE current_setting('rules.hard_deletion', true) <> 'on'
    DO INSTEAD UPDATE blog_post SET _deleted_at = NOW() WHERE id = old.id;
""",
    reverse_sql="""
    DROP RULE IF EXISTS soft_delete ON blog_post;
""",
)
```

It did not always. Until 1.1.0 the file did `from guitars import sql` and named
the constant, which meant the migration's *meaning* was a function of the
installed version of the kit. Django freezes model state into migration files so
that replaying history reproduces the same database; naming a library constant
un-freezes exactly that — a fresh `migrate` on 1.0.0 built `<> 'on'` rules at
migration `0003`, while a database that ran `0003` on 0.7 had `= 'off'`. Same
history, two different databases.

Migrations already committed on the old form keep working — `guitars.sql`'s names
remain a frozen interface for their sake, forever. Nothing new is added to that
obligation.

## Idempotency has three layers

They answer different questions, and each covers a gap the others cannot see.

1. **A `[DIGEST:…]` marker** on the generated migration's first line identifies an
   unchanged *operation set*. If nothing about an app's operations changed, no new
   migration is written.
2. **Per-operation comment headers** (`# Updated at Trigger on "x" table!`)
   identify which tables are already covered, so a *partially* covered app
   receives exactly the operations it lacks.
3. **A `[SQL:…]` identity** on each header is a digest of that operation's SQL. It
   is what makes a changed SQL constant generate its own migration.

The third exists because the first two together still missed the case that
mattered most. The header scan short-circuits before anything is built, so a table
whose header was recognised was treated as covered *forever* — the file digest was
never even reached, and it could not have helped anyway, since it covered a source
that named the constant rather than containing it. That is how the 1.0.0
soft-delete guard rewrite — the fix for a rolled-back `hard_delete()` turning every
later `.delete()` into a permanent delete — reached every existing database as
nothing at all.

A header with **no** `[SQL:…]` token is a migration written before the SQL was
inlined. It reads as stale, not as covered, so the first run on 1.1.0 regenerates
it once.

> ⚠️ **Those header strings are frozen.** Reword one and every existing migration
> stops being recognised, and the next run emits duplicates.
> `tests/test_enforcement_identity.py` asserts every emitted header is matched by
> the scanner meant to read it. Most scanners in
> `guitars.management.enforcement.headers` are mechanically derived from their
> `HEADER_*` template, so they cannot drift from it by construction; the few that
> fuse two header forms or must not capture their own placeholder stay
> hand-written. `tests/test_header_corpus.py` additionally guards every scanner —
> derived or hand-written — against the project's own committed migration
> history.
>
> The public names in `guitars.sql` are frozen for a different reason: migrations
> generated before 1.1.0 and committed in consuming projects do
> `from guitars import sql` and read them by name, so a rename breaks `migrate` on
> a fresh database there. `tests/test_sql_interface.py` guards the names;
> `FROZEN_SQL_CONSTANTS` / `FROZEN_SQL_CALLABLES` is the list.

## Three forms, and why `IF EXISTS` is not sprinkled everywhere

Each operation is emitted in one of three forms, chosen from what the migration
history records. `IF EXISTS` and `OR REPLACE` are claims about *knowledge*: used
where the answer is known they turn "your database has diverged from its history"
into silence, so each appears on exactly the path where it is true.

| What is recorded | Form | Why |
| --- | --- | --- |
| nothing | plain `CREATE` | A collision must fail `migrate` loudly. `set_updated_at()` is an unqualified public-schema name; silently replacing someone else's would surface as a runtime mystery elsewhere. |
| a different `[SQL:…]`, or none | `DROP` + `CREATE`, no `IF EXISTS` | The object is known to be ours. An unguarded drop reports a diverged database instead of papering over it. |
| `--adopt` | `DROP … IF EXISTS` + `CREATE` | The premise of the flag is that nobody knows what the database holds. The uncertainty is real and was opted into. |

Two exceptions, both for correctness rather than convenience:

- **Soft-delete rules** are always `CREATE OR REPLACE RULE`. An instant without a
  `soft_delete` rule is an instant in which `DELETE` destroys rows, so the
  definition is swapped in place rather than dropped and re-made.
- **Trigger functions** are refreshed with `CREATE OR REPLACE FUNCTION`.
  `DROP FUNCTION` refuses while any trigger depends on it, and `CASCADE` would take
  every table's trigger with it.

## Adopting a database this command did not build

`makeguitarmigrations --adopt [app_label …]` re-emits every enforcement operation
for the apps in scope, in the guarded form, ignoring what the migration history
records.

It exists because there was previously no supported way in. `create_tenant_policy`
is a bare `CREATE POLICY` — PostgreSQL has no `CREATE POLICY IF NOT EXISTS` — so a
table whose policy exists in the database but carries no `[POLICY:…]` header here
took the "not covered" branch, emitted the `CREATE` form, and failed `migrate` with
*policy "tenant_scope" already exists*. The same applied to a project migrating from
another generator whose comment headers this one cannot read.

`--adopt` cannot be combined with `--force-rls`: that flag acts only on tables whose
policies this command already recorded, which is the very thing `--adopt` exists
because you do not have. Run `--adopt` first, then `--force-rls`.

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

**A scaffold can fail after Django has already written it** — if the stdout-parsing
step that recovers the filename doesn't match, the empty file is already on disk with
nothing to reference it by. It is left in place rather than deleted: cleaning up a file
this command didn't stamp is a bigger footgun than leaving it, and an unstamped scaffold
carries no `[DIGEST:...]`, so a later run can never mistake it for already covering
anything — it just sits as visible dead weight, and the raised error names the directory
to check by hand.

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
# Tenant RLS on "billing_invoice" table! [POLICY:57ff74989db7] [SQL:ebff8d5fbdc6]
```

A run that finds a recorded identity different from the one the models now imply
emits a **replacement** rather than nothing:

```python
# Tenant RLS replaced on "billing_invoice" table! [POLICY:0b7c94cc2edc] [SQL:f1a3734d30dd]
migrations.RunSQL(
    sql=[
        """DROP POLICY IF EXISTS tenant_scope ON billing_invoice""",
        ...
    ],
    ...
)
```

The two tokens answer different questions and both are checked. `[POLICY:…]` is
what the policy *says*; `[SQL:…]` is whether the text is the text the kit emits
today. Neither subsumes the other — which is why `force` can stay out of the
identity without becoming invisible.

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
