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
- `GuitarModel` (6) = `SetarModel` + tenancy. Full kit; see "Tenancy" below.

Each capability is also a standalone mixin exported from `guitars.models`: `UpdatableModel`, `HasCachedPropertyModel`, `DatedModel`, `SoftDeletableModel`.

**The rungs shifted down one in 1.0.0** to make room for tenancy: 0.7's `DutarModel` → `TarModel`, `SetarModel` → `DutarModel`, `GuitarModel` → `SetarModel`, all behaviour-identical; `GuitarModel` kept its name and took the new meaning. This is why the release is 1.0.0 and not 0.8.0 — `ganje` pins `>=0.7,<1.0`, so shipping it as a minor would silently shift every model there by one capability.

### Database-enforced behavior is the whole design

The non-obvious core: **behavior is enforced by Postgres, not Python.** Two pieces work together:

1. **`src/guitars/sql/`** — every byte of raw SQL, split by concern and re-exported flat from `sql/__init__.py`: `sql/triggers.py` (the `set_updated_at()` function, the per-table `updated_at` statement trigger, the MTI parent trigger) and `sql/soft_delete.py` (the `soft_delete` rule, `soft_delete_related_*` cascade rules, the MTI redirect rule, the hard-deletion session switches). **`guitars.sql`'s public names are a frozen interface** — generated migrations checked into consuming projects do `from guitars import sql` and read them by name, so a rename breaks `migrate` on a fresh database there. `tests/test_sql_interface.py` guards this.
2. **`makeguitarmigrations` management command** (`src/guitars/management/commands/`) — scans `settings.LOCAL_APPS` models for `_updated_at` / `_deleted_at`, then writes `migrations.RunSQL(...)` migrations wiring those SQL strings to each table. It is **idempotent** via two mechanisms: a `[DIGEST:...]` marker on the first line of generated migrations, and regex scans (`_RE_*`) of existing migration files. The shared trigger function gets a single migration in `TRIGGER_FUNCTION_APP` (default `LOCAL_APPS[0]`); other migrations depend on it. Optional positional app labels scope generation to those apps (empty = all `LOCAL_APPS`), via the `_is_in_scope` predicate; unknown labels raise `CommandError`, mirroring Django's own validation. The trigger-function singleton is still ensured in `TRIGGER_FUNCTION_APP` even when scoped away from its host. Cross-app CASCADE soft-delete rules are attributed to the *parent* model's app (`_build_operations`), so scoping to the child's app alone skips the rule; `_scoped_cascade_gap_notes` surfaces this as a runtime warning rather than leaving it silent — this is the accepted "pragmatic scope" tradeoff, closed by a later run naming the parent's app (or none at all).

3. **`makemigrations` override** (`src/guitars/management/commands/makemigrations.py`) — subclasses Django's command so that, by default, `makemigrations` runs the enforcement generation right after the schema migrations (via `call_command('makeguitarmigrations', ...)`). Gated by `GUITARS_AUTO_MAKE_MIGRATIONS` (default `True`; set `False` for the explicit two-command workflow). It skips the enforcement step on `--empty`/`--dry-run` — the `--empty` guard also prevents infinite recursion, since `makeguitarmigrations` scaffolds its files via `makemigrations --empty`, which re-enters this override. `--check` maps to the generator's `check_only`, so `makemigrations --check` validates both layers. Positional app labels are forwarded to the guitar step, so a scoped `makemigrations blog` only generates enforcement migrations for `blog`.

**Consequence:** with the default `GUITARS_AUTO_MAKE_MIGRATIONS = True`, `makemigrations` creates the triggers/rules for you. If you set it to `False`, plain `makemigrations` does NOT create them — you must run `makeguitarmigrations` yourself, and until it runs and you `migrate`, `.delete()` permanently deletes rows (the soft-delete protection is not wired up). Either way, `--check` fails (non-zero) when migrations are missing — used in CI.

### Soft deletion mechanics (`src/guitars/models/soft_deletion.py`)

- `.delete()` is intercepted by a PG `ON DELETE` rule that sets `_deleted_at = NOW()`. Cascades to `on_delete=CASCADE` related soft-deletable models via `soft_delete_related_*` rules — works for bulk/raw deletes since there's no `.save()` to skip.
- Three managers: `objects` (live only, default), `_archives` (soft-deleted only), `_all_objects` (everything).
- `hard_delete()` bypasses the rule by setting the PG session var `rules.hard_deletion = 'on'` (see `SWITCH_ON/OFF_HARD_DELETION`). Instance-level `hard_delete()` is two-phase: soft-delete first (cascades), then DFS-collect CASCADE children and bulk-hard-delete child-first — because Django's CASCADE is Python-level (`Collector`), Postgres has no `ON DELETE CASCADE` constraint, so a raw parent DELETE would hit an FK check.

### Multi-table inheritance (MTI)

A concrete model subclassing another concrete `GuitarModel`/`DatedModel`/`SoftDeletableModel` (Django MTI) is fully supported. The metadata columns (`_updated_at`/`_deleted_at`) physically live on the ancestor that declares them, but the shipped models "just work" because of the **shared-PK invariant** (every table in an MTI chain shares one PK value) and column-ownership detection.

- **Detection** — `makeguitarmigrations` resolves the owning table per column via `model._meta.get_field(name).model` (`_column_owner`/`_owns`/`_is_mti_child`), not `hasattr`. Abstract-base concrete models still own their own columns; MTI children resolve to the ancestor.
- **Soft delete** — each MTI child table gets a redirect rule (`CREATE_MTI_SOFT_DELETE_RULE`): `ON DELETE ... DO INSTEAD` preserves the child row and sets `_deleted_at` on the **owner** table (`WHERE owner_pk = old.<child_pk>`). Django deletes child-before-parent, so the parent's own rule then no-ops via the `_deleted_at IS NULL` guard — cascades fire exactly once, in both delete directions and at any depth.
- **`_updated_at` propagation** — a child-only `QuerySet.update()` touches only the child table, so each MTI child also gets a `set_parent_updated_at()` trigger that bumps the owner's `_updated_at`. This function is a second singleton (migration `*_auto_enforcement_parent_trigger_function`, hosted in `TRIGGER_FUNCTION_APP`), parallel to `set_updated_at()`.
- **Cascade INTO an MTI child** (a CASCADE FK whose target is an MTI child) attaches its `soft_delete_related_*` rule to the target's **owner** table (the FK column holds the shared PK). The MTI parent-link (a CASCADE `OneToOne`) is skipped — it's structural, handled by the redirect rule, not a user cascade FK.
- **`hard_delete`** — instance-level starts the DFS from the MTI **root** (the parent-link reverse is itself a CASCADE relation), so every table in the chain is collected child-first and each is deleted via the own-table primitive `_hard_delete_own_table`. Queryset-level `hard_delete()` deletes the whole table chain leaf-to-root by shared PK (`_mti_table_chain`) so no orphaned ancestor row is left.
- **Required boilerplate** — an MTI child of a soft-deletable base **must declare its own `Meta`** (an empty `class Meta: pass` suffices) so Django doesn't re-declare the parent's `%(class)s_deleted_at` partial index against the child's non-local `_deleted_at` column (`models.E016`). Managers are still inherited.
- **Known limitation** — cascading *into* an MTI child through a FK declared on the child's **own** table while its `_deleted_at` lives on a farther ancestor is not supported (needs a join form); `makeguitarmigrations` skips it with a warning rather than emitting a broken rule.

See `tests/testapp/models.py` (`Ensemble → Orchestra → ChamberOrchestra`, plus `Section`) and `tests/test_mti.py`.

### Tenancy (`src/guitars/tenancy/`)

Same thesis as soft deletion, applied to *which rows a caller may see*: a Python layer that fails **loudly** and a PostgreSQL layer that is actually **complete**.

- **Frame** — `scope.py` holds a `ContextVar` of `{dimension: value}`. `tenant(**dims)` enters one, `tenancy_bypassed()` is the single greppable cross-tenant path, `@tenanted` reads a value off a function argument (`arg` = which parameter, `dimension` = which scope key; separable on purpose). A `None` value counts as *absent*, not "match everything".
- **Python enforcement** — `manager.py`. `TenantedManager(_manager_class=..., autofill=..., **dimensions)` returns a manager instance; missing scope yields a *denying* queryset built from the manager's own queryset class, so custom methods still resolve and raise `TenantScopeError` rather than `AttributeError`. Writes are guarded by a `pre_save` receiver (covers `instance.save()` and `_base_manager`) plus a `bulk_create` override on the **queryset** (chaining leaves the manager behind). The deny-list must stay exhaustive — `tests/test_tenancy_denylist.py` fails if any queryset method is unclassified.
- **PostgreSQL enforcement** — `guc.py` mirrors the frame into `tenant.*` session settings inside a `connection.execute_wrappers` entry, lazily and cached. **The cache key is deliberately more than the values** (`in_atomic_block`, the savepoint stack, and a per-transaction `run_on_commit` marker): a stale cache leaves the *previous* tenant live, which fails **open**. Do not simplify it without a test that fails first. Publishing is skipped on SQLSTATE 25P02 — an aborted transaction refuses every statement including the `ROLLBACK TO SAVEPOINT` that recovers it, so raising there wedges the connection permanently.
- **Policies** — `sql/policy.py` (settings-free; `force` and `exempt_roles` are baked into the migration as literals so one migration history always yields one database). The predicate is list-tolerant (`col::text = ANY(string_to_array(current_setting(...), ','))`) so one form serves scalar and collection scopes, every NULL path denies, and `current_setting` sits in a scalar subquery so the planner hoists it to an InitPlan. **RLS enabled with no policy is default-DENY**, not default-allow.
- **MTI** — an MTI child gets an **owner-join** policy (correlated `EXISTS` on the shared PK) rather than relying on the ancestor's, because a child-only statement never touches the ancestor. Same gap `set_parent_updated_at` closes for timestamps, from the other side.
- **Coverage** — `discovery.py` is the single answer shared by the generator and `audittenancy`, so the build gate and the live audit cannot disagree. It reports rather than covers: multi-hop dimensions, and dimensions spread across two ancestors. A model can be *partially* covered; the note names what is and isn't enforced.
- **Three silent bypasses** to remember: SUPERUSER, the `BYPASSRLS` attribute, and the table OWNER without `FORCE`. The last is why `GUITARS_RLS_FORCE` defaults to `True` and why the test suite runs as a non-superuser role that owns its tables.
- `base_manager_name` is deliberately **left unset** on `GuitarModel` — see the reasoning in its `Meta`. Django's rule is that a base manager must not filter, `_base_manager` is on the `save()` path where `_insert`/`_update` are not deny-listed, and it is precisely the path RLS covers completely.

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
python manage.py makemigrations                # core + trigger/rule migrations (default)
python manage.py makemigrations --check        # CI: fail if either layer is missing
python manage.py makeguitarmigrations          # trigger/rule migrations only (standalone)
python manage.py makeguitarmigrations --check  # CI: fail if missing
```

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
isn't strictly newer than the latest existing tag — which then fans out to the
`Release and Publish` workflow (`release.yml`) to create the GitHub release. Run
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
