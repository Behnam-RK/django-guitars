# django-guitars

🎸 *Django object-metadata the **database** enforces — not your `.save()` method.*

Most Django soft-delete and timestamp libraries live in Python: a signal here, a
`save()` override there. It holds up right until a `bulk_update`, a raw `UPDATE`,
or a `queryset.delete()` strolls straight past your code — and leaves the
metadata lying.

**django-guitars pushes that work down into PostgreSQL itself** — rules and
triggers, not signals. So `_created_at` / `_updated_at` / `_deleted_at` stay
honest no matter how a row gets touched: ORM, bulk, raw SQL, all of it. The
database keeps score; you just write models. Use only the pieces you need.

[![PyPI version](https://img.shields.io/pypi/v/django-guitars.svg)](https://pypi.org/project/django-guitars/)
[![Python versions](https://img.shields.io/pypi/pyversions/django-guitars.svg)](https://pypi.org/project/django-guitars/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Requirements

- **Python** ≥ 3.10
- **Django** ≥ 5.0 — uses `db_default`
- **PostgreSQL** — currently the only supported backend; the soft-delete rule and
  `_updated_at` trigger live in the database itself. Other backends are on the
  roadmap.

> **Status:** 1.0.0. The public API — the base models, the managers, the
> `guitars.sql` names generated migrations depend on, and the `GUITARS_*`
> settings — is stable, and breaking changes now require a major version.

## Installation

```bash
pip install django-guitars
```

Add the app to your settings:

```python
INSTALLED_APPS = [
    # ...
    "guitars",
]
```

## Where to find what

| If you want to… | Read |
| --- | --- |
| pick a base model | [Pick your instrument](#pick-your-instrument), below |
| understand soft deletion, cascades and `hard_delete` | [`docs/soft-deletion.md`](docs/soft-deletion.md) |
| scope rows to a tenant | [`docs/tenancy.md`](docs/tenancy.md) |
| use multi-table inheritance | [`docs/mti.md`](docs/mti.md) |
| know how the triggers, rules and policies get into your database | [`docs/migrations.md`](docs/migrations.md) |
| know *why* something was built this way | [`docs/adr/`](docs/adr/) |
| upgrade from 0.7 | [`CHANGELOG.md`](CHANGELOG.md) |

## Pick your instrument

The base models are named after string instruments, fewest strings to most — and
the strings *are* the feature ladder. (`du` = two, `se` = three in Persian; `tar`
= "string". A guitar has six. Django Reinhardt, the jazz guitarist this whole
package winks at, would approve.)

| Base | Strings | What you get |
| --- | :---: | --- |
| `TarModel`    | — | `.update()` / `.aupdate()` and cached-property invalidation on `refresh_from_db()`. The featherweight — adds no columns and no database behaviour. (`tār` = "string": the root every rung is counted from.) |
| `DutarModel`  | 2 | Everything in `TarModel` **plus** DB-managed `_created_at` / `_updated_at` (default `NOW()`; `_updated_at` is ridden by a statement trigger, so it's right even under bulk/raw updates) and `app_label()` / `model_name()` / `class_name()` helpers. |
| `SetarModel`  | 3 | Everything in `DutarModel` **plus** PostgreSQL soft deletion. **The one to reach for by default.** |
| `GuitarModel` | 6 | Everything in `SetarModel` **plus** [multi-tenancy](#multi-tenancy): a tenant FK, tenant-scoped managers, and a row-level-security policy. The full kit. |

Prefer to tune your own chord? Each capability is a standalone mixin in
`guitars.models`: `UpdatableModel`, `HasCachedPropertyModel`, `DatedModel`, and
`SoftDeletableModel`.

```python
from django.db import models

from guitars.models import SetarModel


class Article(SetarModel):
    title = models.CharField(max_length=200)
```

> ⚠️ **Renamed in 1.0.0.** Every rung shifted down one to make room for
> tenancy: 0.7's `DutarModel` is now `TarModel`, `SetarModel` is now
> `DutarModel`, and `GuitarModel` is now `SetarModel` — all three
> behaviour-identical. `GuitarModel` keeps its name but now means
> "`SetarModel` + tenancy". See [`CHANGELOG.md`](CHANGELOG.md).

### `.update()` — set and save in one strum

Available on every rung (it comes from `TarModel`):

```python
article.update(title="New title")         # set fields + save (only changed fields)
article.update(title="x", _save=False)    # change in memory only, no DB write
await article.aupdate(title="async")      # async variant
```

> Note: attributes set with `_save=False` are **not** carried into a later
> `_save=True` call unless you also pass `_save_all_fields=True`.

### Soft deletion

For models inheriting `SoftDeletableModel` (or `SetarModel` / `GuitarModel`), `.delete()` becomes
a **soft delete**: the row stays and `_deleted_at` is set. Because a PostgreSQL
rule does the work, it holds even for queryset bulk deletes and raw SQL — there's
no `.save()` to skip. Three managers expose the data:

```python
Article.objects.all()         # live rows only (the default manager)
Article._archives.all()       # soft-deleted rows only
Article._all_objects.all()    # everything

article.delete()              # soft delete — sets _deleted_at
article.is_deleted            # True
article.is_alive              # False

# Actually want it gone? hard_delete bypasses the rule (and takes CASCADE kids with it):
article.hard_delete()                           # this row + CASCADE children
Article._all_objects.filter(...).hard_delete()  # in bulk
```

Soft-deleting a row also soft-deletes rows related by `on_delete=CASCADE`.

#### Multi-table inheritance

Subclassing a concrete `SetarModel` (Django [multi-table inheritance](https://docs.djangoproject.com/en/stable/topics/db/models/#multi-table-inheritance)) works: the child gets its own table, but timestamps and soft deletion still resolve to the parent — deleting a child soft-deletes it (child row preserved), a child-only `update()` still bumps the parent's `_updated_at`, and `hard_delete()` clears the whole table chain with no orphaned parent row.

```python
class Ensemble(SetarModel):
    name = models.CharField(max_length=100)

class Orchestra(Ensemble):          # own table; metadata lives on Ensemble's
    conductor = models.CharField(max_length=100)

    class Meta:                      # required — see below
        pass
```

> ⚠️ **MTI children must declare their own `Meta`** (an empty `class Meta: pass`
> is enough). Otherwise Django re-declares the parent's `_deleted_at` partial
> index against the child's table, which has no such column, and raises
> `models.E016`. Managers are still inherited.

> ⚠️ **Required setup.** The soft-delete rule (and the `_updated_at` trigger) live
> in a migration generated by [`makeguitarmigrations`](#makeguitarmigrations).
> By default `makemigrations` generates it for you (see below); until it's
> created and you `migrate`, **`.delete()` permanently deletes the row** — the
> protection isn't wired up yet.

### `makeguitarmigrations`

The triggers and rules don't come from plain Django schema migrations — they
live in separate migrations generated by this command, and they're **required**
for soft deletion and the `_updated_at` trigger to work.

By default you don't run it directly: `makemigrations` is extended to generate
these enforcement migrations right after the schema ones, so a single command keeps
both in sync:

```bash
python manage.py makemigrations   # generates core + trigger/rule migrations
```

Prefer the explicit two-command workflow? Turn the extension off and run the
command yourself:

```python
# settings.py
GUITARS_AUTO_MAKE_MIGRATIONS = False   # defaults to True
```

```bash
python manage.py makemigrations
python manage.py makeguitarmigrations
```

Either way, the command scans your **first-party** apps for models with
`_updated_at` / `_deleted_at` and writes the matching trigger/rule migrations.
Tell it which apps are yours:

```python
# settings.py
LOCAL_APPS = ["blog", "shop"]      # apps the command scans

# Optional: which app hosts the shared trigger-function migration.
# Defaults to LOCAL_APPS[0].
# TRIGGER_FUNCTION_APP = "blog"
```

Both commands accept optional app labels to scope generation, mirroring
Django's own `makemigrations`: `makemigrations blog` (or `makeguitarmigrations
blog`) only touches `blog`, and an unknown label is rejected the same way
Django's is. With no labels, every app in `LOCAL_APPS` is scanned.

> **Cross-app cascade rules:** a soft-delete cascade rule (e.g. "deleting a
> `Band` cascades to its `Album`s") is written into the *parent* model's
> migration — `Band`'s app here. If that parent's app isn't named in a scoped
> run, the cascade rule is skipped even when the child's app is; the command
> prints a warning naming the skipped rule and the app to include. Run without
> labels, or name the parent's app, to close the gap.

Use `--check` in CI to fail when enforcement migrations are missing. With the
extension on, `makemigrations --check` validates **both** the core and the
trigger/rule migrations; the standalone form still works too:

```bash
python manage.py makemigrations --check       # checks both layers
python manage.py makeguitarmigrations --check # checks the trigger/rule layer only
```

### Multi-tenancy

`GuitarModel` is `SetarModel` plus tenancy, enforced in **two** layers. Point it
at your tenant model:

```python
# settings.py
GUITARS_TENANT_MODEL = "accounts.Organization"
GUITARS_TENANT_FIELD = "org"            # optional; the FK's name and the scope
                                        # dimension. Defaults to "tenant".
```

```python
from guitars.models import GuitarModel
from guitars.tenancy import tenancy_bypassed, tenant


class Invoice(GuitarModel):             # gains a non-null org FK (CASCADE, editable=False)
    amount = models.IntegerField()


with tenant(org=acme):
    Invoice.objects.all()               # acme's invoices
    Invoice.objects.create(amount=10)   # org filled in from the scope

Invoice.objects.all()                   # TenantScopeError — no scope, no rows

with tenancy_bypassed():                # the one explicit cross-tenant path
    Invoice.objects.count()             # every tenant
```

The **Python layer** is the loud one: all three managers refuse to run without
an active scope, and a write that contradicts it raises rather than landing in
the wrong tenant. The **PostgreSQL layer** is the complete one — a
`tenant_scope` row-level-security policy, generated by `makeguitarmigrations`
and driven by session settings, enforces the same scope on paths no manager sees:
joins, cascades, `_base_manager`, `instance.save()` and raw SQL. Neither is
redundant; without the database there are holes, without Python a missing scope
is silent.

Two things worth knowing before you adopt it:

- **The policy binds only if your app role does not own its tables — or the
  table is `FORCE`d.** guitars emits `FORCE ROW LEVEL SECURITY` by default for
  exactly this reason. A superuser or a `BYPASSRLS` role bypasses policies
  unconditionally, so don't run your app as one.
- **`migrate` runs bypassed**, because a `RunPython` backfill has no tenant
  scope and would otherwise match zero rows and be marked applied. guitars
  overrides `migrate` to do this, which means `guitars` must come *before* any
  other app providing that command in `INSTALLED_APPS`.

| Setting | Default | Effect |
| --- | --- | --- |
| `GUITARS_TENANT_MODEL` | *(required for `GuitarModel`)* | Tenant model, `"app.Model"`. Missing it is a `guitars.tenancy.E003` system-check error. |
| `GUITARS_TENANT_FIELD` | `"tenant"` | Name of the FK, and of the scope dimension. |
| `GUITARS_TENANT_ENFORCE` | `"strict"` | `"audit"` reports a write violation once per distinct finding and proceeds — for rolling enforcement onto a live deployment. |
| `GUITARS_TENANT_AUTOFILL` | `False` | Fill a missing tenant from the active scope. `GuitarModel` passes `True` for its own FK regardless. |
| `GUITARS_TENANT_POLICIES` | `True` | `False` keeps the Python layer and leaves the database alone. |
| `GUITARS_RLS_FORCE` | `True` | `False` ships policies inert, for a staged retrofit; `makeguitarmigrations --force-rls` lands `FORCE` later. |
| `GUITARS_RLS_EXEMPT_ROLES` | `[]` | Roles granted a `SELECT`-only exemption policy (reporting, BI), guarded on the role existing. |

Auditing a live database, as a deploy step:

```bash
python manage.py audittenancy --require-force --require-match
```

It compares every policy-eligible table against `pg_class` / `pg_policy` and
exits non-zero on a missing policy, a missing `ENABLE`, or — with
`--require-force` — a table where the owner would silently bypass the policy.

### `DisableSignals`

A context manager that temporarily disconnects Django signals — handy for bulk
imports or silent saves:

```python
from django.db.models.signals import post_save

from guitars.signals import DisableSignals

with DisableSignals():                    # all default signals
    instance.save()                       # nothing fires

with DisableSignals(signals=[post_save]): # only the listed signals
    instance.save()
```

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker (for PostgreSQL).

```bash
uv sync                  # install dependencies + the package (editable)
docker compose up -d     # start PostgreSQL (skip if you already run one on :4455)
uv run pytest            # run the test suite
uv run pytest --cov=guitars --cov-report=term-missing
```

The test suite defines concrete models in `tests/testapp` (the shipped package is
abstract-only) and runs against a real PostgreSQL database, so the rules and
triggers are actually exercised — not mocked.

The suite connects as a deliberately **non-superuser** role, provisioned by
[`scripts/postgres-init.sql`](scripts/postgres-init.sql). A superuser bypasses
row-level security unconditionally, so tenancy policies could never be proven
against one. If you have a checkout from before that script existed, recreate the
volume once so the role gets created:

```bash
docker compose down -v && docker compose up -d --wait
```

Running your own PostgreSQL on `:4455` instead? Create the equivalent role there:
`CREATE ROLE guitars LOGIN CREATEDB PASSWORD 'guitars';` — `CREATEDB` is what lets
the test runner build its own database, which also makes the role the owner of
every table in it.

### Releasing

Two interactive helpers in [`scripts/`](scripts/README.md) drive a release:

```bash
./scripts/bump.sh minor   # bump pyproject.toml + seed CHANGELOG, then commit
$EDITOR CHANGELOG.md       # write the release notes
./scripts/release.sh       # git tag + push + GitHub release (via gh)
```

`pyproject.toml` is the single source of truth for the version —
`guitars.__version__` reads it from the installed package metadata. See
[`scripts/README.md`](scripts/README.md) for details.

## License

[MIT](LICENSE) © 2026 Behnam RK
