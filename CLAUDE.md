# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`django-guitars` is a reusable, **abstract-only** Django app: a kit of base models that push object-metadata (timestamps, soft deletion) **into PostgreSQL rules and triggers** instead of Python `save()`/signal overrides. The point is correctness under `bulk_update`, `queryset.delete()`, and raw SQL — code paths that never touch `.save()`. PostgreSQL is the only supported backend.

## Repo layout: shipped package vs. dev harness

The wheel ships **only** `src/guitars/`, mapped to top-level `guitars` (see `[tool.hatch.build.targets.wheel]` in `pyproject.toml`). Everything else is a throwaway harness so the kit can be developed and tested standalone:

- `core/` — minimal Django project (settings/urls/wsgi). No auth, sessions, admin, middleware — just Postgres + the `guitars` app.
- `manage.py` — dev entrypoint (`DJANGO_SETTINGS_MODULE=core.settings`).
- `tests/` — pytest suite + `tests/testapp/` concrete models. The package is abstract-only, so concrete models that exercise the rules/triggers live here.
- `tests/settings.py` extends `core.settings` and sets `LOCAL_APPS = ['tests.testapp']` so generated advanced migrations land under `tests/`, never inside the shipped package.

When editing source, work in `src/guitars/`. When changing test models or harness, work in `tests/` or `core/`.

## Architecture

### The instrument ladder (`src/guitars/models/base.py`)

Abstract bases named by string count, each rung adds capability via mixins:

- `DutarModel` (2) = `UpdatableModel` + `HasCachedPropertyModel`. Adds no columns.
- `SetarModel` (3) = `DatedModel` + `DutarModel`. Adds DB-managed `_created_at` / `_updated_at` + `app_label()` / `model_name()` / `class_name()` helpers.
- `GuitarModel` (6) = `SetarModel` + `SoftDeletableModel`. Full kit; its `Meta` inherits `SoftDeletableModel.Meta` (soft-delete index + default manager).

Each capability is also a standalone mixin exported from `guitars.models`: `UpdatableModel`, `HasCachedPropertyModel`, `DatedModel`, `SoftDeletableModel`.

### Database-enforced behavior is the whole design

The non-obvious core: **behavior is enforced by Postgres, not Python.** Two pieces work together:

1. **`src/guitars/sql.py`** — raw SQL strings for the `set_updated_at()` trigger function, the per-table `updated_at` statement trigger, the `soft_delete` rule, and `soft_delete_related_*` cascade rules.
2. **`makeguitarmigrations` management command** (`src/guitars/management/commands/`) — scans `settings.LOCAL_APPS` models for `_updated_at` / `_deleted_at`, then writes `migrations.RunSQL(...)` migrations wiring those SQL strings to each table. It is **idempotent** via two mechanisms: a `[DIGEST:...]` marker on the first line of generated migrations, and regex scans (`_RE_*`) of existing migration files. The shared trigger function gets a single migration in `TRIGGER_FUNCTION_APP` (default `LOCAL_APPS[0]`); other migrations depend on it. Optional positional app labels scope generation to those apps (empty = all `LOCAL_APPS`), via the `_is_in_scope` predicate; unknown labels raise `CommandError`, mirroring Django's own validation. The trigger-function singleton is still ensured in `TRIGGER_FUNCTION_APP` even when scoped away from its host. Cross-app CASCADE soft-delete rules are attributed to the *parent* model's app (`_build_operations`), so scoping to the child's app alone skips the rule; `_scoped_cascade_gap_notes` surfaces this as a runtime warning rather than leaving it silent — this is the accepted "pragmatic scope" tradeoff, closed by a later run naming the parent's app (or none at all).

3. **`makemigrations` override** (`src/guitars/management/commands/makemigrations.py`) — subclasses Django's command so that, by default, `makemigrations` runs the guitar generation right after the core migrations (via `call_command('makeguitarmigrations', ...)`). Gated by `GUITARS_AUTO_MAKE_MIGRATIONS` (default `True`; set `False` for the explicit two-command workflow). It skips the guitar step on `--empty`/`--dry-run` — the `--empty` guard also prevents infinite recursion, since `makeguitarmigrations` scaffolds its files via `makemigrations --empty`, which re-enters this override. `--check` maps to guitar's `check_only`, so `makemigrations --check` validates both layers. Positional app labels are forwarded to the guitar step, so a scoped `makemigrations blog` only generates guitar migrations for `blog`.

**Consequence:** with the default `GUITARS_AUTO_MAKE_MIGRATIONS = True`, `makemigrations` creates the triggers/rules for you. If you set it to `False`, plain `makemigrations` does NOT create them — you must run `makeguitarmigrations` yourself, and until it runs and you `migrate`, `.delete()` permanently deletes rows (the soft-delete protection is not wired up). Either way, `--check` fails (non-zero) when migrations are missing — used in CI.

### Soft deletion mechanics (`src/guitars/models/soft_deletion.py`)

- `.delete()` is intercepted by a PG `ON DELETE` rule that sets `_deleted_at = NOW()`. Cascades to `on_delete=CASCADE` related soft-deletable models via `soft_delete_related_*` rules — works for bulk/raw deletes since there's no `.save()` to skip.
- Three managers: `objects` (live only, default), `_archives` (soft-deleted only), `_all_objects` (everything).
- `hard_delete()` bypasses the rule by setting the PG session var `rules.hard_deletion = 'on'` (see `SWITCH_ON/OFF_HARD_DELETION`). Instance-level `hard_delete()` is two-phase: soft-delete first (cascades), then DFS-collect CASCADE children and bulk-hard-delete child-first — because Django's CASCADE is Python-level (`Collector`), Postgres has no `ON DELETE CASCADE` constraint, so a raw parent DELETE would hit an FK check.

### `.update()` and signals

- `UpdatableModel.update(**attrs)` / `aupdate()` set fields + save in one call, writing only changed fields via `update_fields`. M2M handled via `.set(values, clear=True)`, requires `_save=True`. `_save=False` attrs are NOT carried into a later `_save=True` call unless `_save_all_fields=True`.
- `guitars.signals.DisableSignals` — context manager that stashes/restores signal receivers; used by `update(_disable_signals=True)`.

## Commands

Requires [uv](https://docs.astral.sh/uv/) and Docker (for Postgres). Tests run against a **real** Postgres — there is no SQLite fallback.

```bash
uv sync                       # install deps + package (editable)
docker compose up -d          # start Postgres on :4455
uv run pytest                 # full suite (settings: tests.settings, auto via pyproject)
uv run pytest --cov=guitars --cov-report=term-missing
uv run pytest tests/test_base.py::TestUpdate::test_x   # single test
python manage.py makemigrations                # core + trigger/rule migrations (default)
python manage.py makemigrations --check        # CI: fail if either layer is missing
python manage.py makeguitarmigrations          # trigger/rule migrations only (standalone)
python manage.py makeguitarmigrations --check  # CI: fail if missing
```

Set `GUITARS_AUTO_MAKE_MIGRATIONS = False` to make `makemigrations` skip the guitar step and use the standalone command instead.

Releasing (interactive helpers, see `scripts/README.md`):

```bash
./scripts/bump.sh minor       # bump pyproject.toml + seed CHANGELOG, commit
./scripts/release.sh          # git tag + push + GitHub release (gh)
```

`pyproject.toml` is the single source of truth for the version;
`guitars.__version__` reads it from installed package metadata
(`importlib.metadata`) — no second string to bump.

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
- Editing SQL behavior means editing `src/guitars/sql.py` **and** verifying `makeguitarmigrations` still emits/matches it (the command's `_RE_*` regexes key off the comment headers in the generated operation templates).
- After model changes in `tests/testapp/`, regenerate migrations with `makemigrations` (which now also emits the trigger/rule migrations, since `GUITARS_AUTO_MAKE_MIGRATIONS` defaults to `True`).
