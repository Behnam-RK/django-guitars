# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`django-guitars` is a reusable, **abstract-only** Django app: a kit of base models that push object-metadata (timestamps, soft deletion) **into PostgreSQL rules and triggers** instead of Python `save()`/signal overrides. The point is correctness under `bulk_update`, `queryset.delete()`, and raw SQL — code paths that never touch `.save()`. PostgreSQL is the only supported backend.

## Repo layout: shipped package vs. dev harness

The wheel ships **only** `src/guitars/`, mapped to top-level `guitars` (see `[tool.hatch.build.targets.wheel]` in `pyproject.toml`). Everything else is a throwaway harness so the kit can be developed and tested standalone:

- `core/` — minimal Django project (settings/urls/wsgi). No auth, sessions, admin, middleware — just Postgres + the `guitars` app.
- `manage.py` — dev entrypoint (`DJANGO_SETTINGS_MODULE=core.settings`).
- `tests/` — pytest suite + `tests/testapp/` concrete models. The package is abstract-only, so concrete models that exercise the rules/triggers live here.
- `tests/settings.py` extends `core.settings` and sets `LOCAL_APPS = ['tests.testapp']` so generated enforcement migrations land under `tests/`, never inside the shipped package.

When editing source, work in `src/guitars/`. When changing test models or harness, work in `tests/` or `core/`.

## Architecture

### The instrument ladder (`src/guitars/models/base.py`)

Abstract bases named by string count, each rung adds capability via mixins:

- `TarModel` = `UpdatableModel` + `HasCachedPropertyModel`. Adds no columns and no DB behaviour — the root, unnumbered ("tār" = string).
- `DutarModel` (2) = `DatedModel` + `TarModel`. Adds DB-managed `_created_at` / `_updated_at` + `app_label()` / `model_name()` / `class_name()` helpers and the field-listing `__repr__`.
- `SetarModel` (3) = `DutarModel` + `SoftDeletableModel`. Its `Meta` inherits `SoftDeletableModel.Meta` (soft-delete index + default manager). **The default rung** for a model that isn't tenanted.
- `GuitarModel` (6) = `SetarModel` + tenancy. Full kit; see [`docs/tenancy.md`](docs/tenancy.md).

Each capability is also a standalone mixin exported from `guitars.models`: `UpdatableModel`, `HasCachedPropertyModel`, `DatedModel`, `SoftDeletableModel`.

**The rungs shifted down one in 1.0.0** to make room for tenancy: 0.7's `DutarModel` → `TarModel`, `SetarModel` → `DutarModel`, `GuitarModel` → `SetarModel`, all behaviour-identical; `GuitarModel` kept its name and took the new meaning. This is why the release is 1.0.0 and not 0.8.0 — `ganje` pins `>=0.7,<1.0`, so shipping it as a minor would silently shift every model there by one capability.

### Database-enforced behavior is the whole design

The non-obvious core: **behavior is enforced by Postgres, not Python.** Two pieces work together:

1. **`src/guitars/sql/`** — every byte of raw SQL, split by concern and re-exported flat from `sql/__init__.py`: `sql/triggers.py` (the `set_updated_at()` function, the per-table `updated_at` statement trigger, the MTI parent trigger) and `sql/soft_delete.py` (the `soft_delete` rule, `soft_delete_related_*` cascade rules, the MTI redirect rule, the hard-deletion session switches). **`guitars.sql`'s public names are a frozen interface** — generated migrations checked into consuming projects do `from guitars import sql` and read them by name, so a rename breaks `migrate` on a fresh database there. `tests/test_sql_interface.py` guards this.
2. **`makeguitarmigrations` management command** (`src/guitars/management/commands/`) — scans `settings.LOCAL_APPS` models for `_updated_at` / `_deleted_at`, then writes `migrations.RunSQL(...)` migrations wiring those SQL strings to each table. It is **idempotent** via two mechanisms: a `[DIGEST:...]` marker on the first line of generated migrations, and regex scans (`_RE_*`) of existing migration files. The shared trigger function gets a single migration in `TRIGGER_FUNCTION_APP` (default `LOCAL_APPS[0]`); other migrations depend on it. Optional positional app labels scope generation to those apps (empty = all `LOCAL_APPS`), via the `_is_in_scope` predicate; unknown labels raise `CommandError`, mirroring Django's own validation. The trigger-function singleton is still ensured in `TRIGGER_FUNCTION_APP` even when scoped away from its host. Cross-app CASCADE soft-delete rules are attributed to the *parent* model's app (`_build_operations`), so scoping to the child's app alone skips the rule; `_scoped_cascade_gap_notes` surfaces this as a runtime warning rather than leaving it silent — this is the accepted "pragmatic scope" tradeoff, closed by a later run naming the parent's app (or none at all).

3. **`makemigrations` override** (`src/guitars/management/commands/makemigrations.py`) — subclasses Django's command so that, by default, `makemigrations` runs the enforcement generation right after the schema migrations (via `call_command('makeguitarmigrations', ...)`). Gated by `GUITARS_AUTO_MAKE_MIGRATIONS` (default `True`; set `False` for the explicit two-command workflow). It skips the enforcement step on `--empty`/`--dry-run` — the `--empty` guard also prevents infinite recursion, since `makeguitarmigrations` scaffolds its files via `makemigrations --empty`, which re-enters this override. `--check` maps to the generator's `check_only`, so `makemigrations --check` validates both layers. Positional app labels are forwarded to the guitar step, so a scoped `makemigrations blog` only generates enforcement migrations for `blog`.

**Consequence:** with the default `GUITARS_AUTO_MAKE_MIGRATIONS = True`, `makemigrations` creates the triggers/rules for you. If you set it to `False`, plain `makemigrations` does NOT create them — you must run `makeguitarmigrations` yourself, and until it runs and you `migrate`, `.delete()` permanently deletes rows (the soft-delete protection is not wired up). Either way, `--check` fails (non-zero) when migrations are missing — used in CI.

### Feature detail lives in `docs/`

The deep explanations moved out of this file so they stay one source of truth for
contributors and consumers alike. Read the relevant one before changing behaviour:

| Topic | Doc |
| --- | --- |
| Soft-delete rules, cascades, `hard_delete` two-phase, the hard-deletion switch | [`docs/soft-deletion.md`](docs/soft-deletion.md) |
| Enforcement-migration vocabulary, idempotency, frozen names, scaffolding, staged RLS | [`docs/migrations.md`](docs/migrations.md) |
| Owner resolution, redirect rule, parent trigger, owner-join policy, hard-delete chain | [`docs/mti.md`](docs/mti.md) |
| Both enforcement layers, settings, rollout order, auditing | [`docs/tenancy.md`](docs/tenancy.md) |

Decisions that were hard to reverse and are surprising without context are ADRs:

- [`0001`](docs/adr/0001-swappable-tenant-model.md) — why `GuitarModel` owns a swappable tenant FK, and what that costs.
- [`0002`](docs/adr/0002-force-rls-by-default.md) — why `FORCE ROW LEVEL SECURITY` is the default.
- [`0003`](docs/adr/0003-mti-owner-join-policy.md) — why MTI children get their own policy; includes the "RLS with no policy is default-DENY" finding.
- [`0004`](docs/adr/0004-unscoped-base-manager.md) — why `base_manager_name` is left unset, with the evidence.
- [`0005`](docs/adr/0005-trigger-based-tenant-autofill.md) — **proposed, not implemented.** Moving tenant autofill into a `BEFORE INSERT` trigger and demoting the `pre_save` guard to diagnostics. Describes planned work, *not* current behaviour — autofill today is the `pre_save` receiver in `tenancy/manager.py`.

**Load-bearing details that are easy to break, kept here as a checklist:**

- `guitars.sql`'s public names and the generated operations' comment headers are a **frozen interface**. Renaming either breaks `migrate` on a fresh database in a consuming project, or makes the generator emit duplicates. `tests/test_sql_interface.py` guards the names.
- Rule guards are `<> 'on'`, never `= 'off'` — a rolled-back `set_config` reads back as the empty string, and `= 'off'` then failed toward *destroying* data. See `docs/soft-deletion.md`.
- Column ownership is resolved via `model._meta.get_field(name).model` (`guitars.introspection`), never `hasattr`.
- `guitars.gucs` must import nothing and live outside `tenancy/`, so a generated migration's `from guitars import sql` does not drag in the tenancy runtime.
- The GUC cache key in `tenancy/guc.py` is deliberately more than the values. A stale cache leaves the *previous* tenant live, which fails **open**. Do not simplify it without a test that fails first.
- A tenant value containing `VALUE_SEPARATOR` (`,`) is **refused**, not escaped. The policy splits the GUC on it, so `'a,b'` would otherwise read as two tenants and match both — the database half becoming wider than the Python half. Escaping instead would change the SQL frozen migrations reproduce. The guard is `tenancy.scope.reject_separator`, called from **two** places and redundant at neither: `tenant()` at scope entry, so the traceback names the dimension and the `with` that opened it, and `guc._scalar` at publish time, because a pk that was `None` at entry can still acquire a separator before the frame is published.
- The tenant policy is the one enforcement operation whose SQL depends on more than the table name, so its header carries a `[POLICY:<digest>]` identity and a changed shape emits `sql.replace_table_rls`. Dedupe on the table name alone let a model gain a tenant dimension while the database kept the old predicate, with `--check` green. `force` is excluded from that identity on purpose — it has its own `--force-rls` stage.
- `audittenancy` compares a live policy by its `tenant.*` GUCs and its `pg_depend` column references, never by SQL text: PostgreSQL rewrites a stored policy expression, so text equality can never hold.

### `.update()` and signals

- `UpdatableModel.update(**attrs)` / `aupdate()` set fields + save in one call, writing only changed fields via `update_fields`. M2M handled via `.set(values, clear=True)`, requires `_save=True`. `_save=False` attrs are NOT carried into a later `_save=True` call unless `_save_all_fields=True`.
- `guitars.signals.DisableSignals` — context manager that stashes/restores signal receivers; used by `update(_disable_signals=True)`.

## Commands

Requires [uv](https://docs.astral.sh/uv/) and Docker (for Postgres). Tests run against a **real** Postgres — there is no SQLite fallback.

The suite connects as a **non-superuser** role created by `scripts/postgres-init.sql` (mounted into the container's `docker-entrypoint-initdb.d`). This is load-bearing, not hygiene: a superuser — and separately, any role with `BYPASSRLS` — bypasses row-level security unconditionally, so every RLS assertion would pass vacuously. The role has `CREATEDB` so the test runner builds its own database and therefore *owns* its tables, which is the exact condition `FORCE ROW LEVEL SECURITY` exists to constrain. Since the init script only runs against an empty data directory, an older checkout needs `docker compose down -v` once.

```bash
uv sync                       # install deps + package (editable)
docker compose up -d          # start Postgres on :4455
docker compose down -v && docker compose up -d --wait   # once, if upgrading an old volume
uv run pytest                 # full suite (settings: tests.settings, auto via pyproject)
uv run pytest --cov=guitars --cov-report=term-missing
uv run pytest tests/test_base.py::TestUpdate::test_x   # single test
export DJANGO_SETTINGS_MODULE=tests.settings    # REQUIRED for anything touching tests/testapp
python manage.py makemigrations                # core + trigger/rule migrations (default)
python manage.py makemigrations --check        # CI: fail if either layer is missing
python manage.py makeguitarmigrations          # trigger/rule migrations only (standalone)
python manage.py makeguitarmigrations --check  # CI: fail if missing
```

> ⚠️ **`manage.py` defaults to `core.settings`, which has no test app.** It uses
> `os.environ.setdefault`, so every `manage.py` command silently runs against a harness with
> `LOCAL_APPS = []` unless you export `DJANGO_SETTINGS_MODULE=tests.settings`. Without it
> `makemigrations --check` reports "No changes detected" and `audittenancy` reports
> "0 table(s) expected … passed" — both **vacuously green**, having examined nothing. `pytest`
> is unaffected (it sets the module via `pyproject.toml`), so a suite that passes proves
> nothing about the commands. Always export it before verifying migrations or tenancy by hand.

Set `GUITARS_AUTO_MAKE_MIGRATIONS = False` to make `makemigrations` skip the enforcement step and use the standalone command instead.

Releasing (interactive helpers, see `scripts/README.md`):

```bash
./scripts/bump.sh minor       # bump pyproject.toml + seed CHANGELOG, commit
./scripts/release.sh          # git tag + push + GitHub release (gh)
```

`pyproject.toml` is the single source of truth for the version;
`guitars.__version__` reads it from installed package metadata
(`importlib.metadata`) — no second string to bump.

**Merging to `main` always requires a version bump.** `.github/workflows/tag-release.yml`
tags `main` with `v<pyproject version>` on every push and fails the job if that version
isn't strictly newer than the latest existing tag — then **calls** the
`Release and Publish` workflow (`release.yml`) as a reusable workflow to create the
GitHub release. A call, not `release.yml`'s own `on: push: tags` trigger, because
GitHub suppresses workflow triggers for events created with the default `GITHUB_TOKEN`
— which is what pushes the tag, so the push trigger never fires. That was silently the
case through v1.0.0 (every release up to it was cut by hand); don't "simplify" the two
jobs back into one trigger. Run
`./scripts/bump.sh` (or edit `pyproject.toml` directly) before merging to `main`.
PyPI publishing lives in that same `release.yml` but is manual-only: it never runs on a
tag push. Trigger it from the Actions tab via `workflow_dispatch`, picking the tag from
the "Use workflow from" ref selector and checking the `publish` input.

Lint / type / security (configured in `pyproject.toml`, run via pre-commit):

```bash
uv run ruff check src         # ruff lint (line-length 99, single quotes)
uv run ruff format src
uv run ty check               # type check (excludes tests/)
uv run bandit -c pyproject.toml -r src
```

`pytest` runs with `filterwarnings = ["error"]` and `xfail_strict` — warnings and unexpected passes fail the suite. `ruff` and `ty` are scoped to `src` and exclude `tests/`.

## Conventions

- Metadata fields are underscore-prefixed (`_created_at`, `_updated_at`, `_deleted_at`); non-default managers too (`_archives`, `_all_objects`).
- Editing SQL behavior means editing the relevant module under `src/guitars/sql/` **and** verifying `makeguitarmigrations` still emits/matches it (the command's `_RE_*` regexes key off the comment headers in the generated operation templates). Adding a new SQL name means re-exporting it from `sql/__init__.py` *and* recording it in `FROZEN_SQL_NAMES` (`tests/test_sql_interface.py`), so a later rename is caught rather than shipped.
- Migration-file mechanics — digest stamping, scanning, `--empty` scaffolding, app scoping — live once in `src/guitars/management/_generator.py`, shared by every generating command. MTI column-ownership resolution lives in `src/guitars/introspection.py`, shared by the generator and the tenancy policy discovery.
- After model changes in `tests/testapp/`, regenerate migrations with `makemigrations` (which now also emits the trigger/rule migrations, since `GUITARS_AUTO_MAKE_MIGRATIONS` defaults to `True`).
