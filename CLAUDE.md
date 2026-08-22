# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`django-guitars` is a reusable, **abstract-only** Django app: a kit of base models that push object-metadata (timestamps, soft deletion) **into PostgreSQL rules and triggers** instead of Python `save()`/signal overrides. The point is correctness under `bulk_update`, `queryset.delete()`, and raw SQL — code paths that never touch `.save()`. PostgreSQL is the only supported backend. The only known consumer, `ganje`, pins `django-guitars>=2.0.0,<3.0`.

## Repo layout: shipped package vs. dev harness

The wheel ships **only** `src/guitars/`, mapped to top-level `guitars` (`[tool.hatch.build.targets.wheel]` in `pyproject.toml`). Everything else is a throwaway harness: `core/` is a minimal Django project (settings/urls/wsgi, no auth/sessions/admin/middleware); `manage.py` is the dev entrypoint (`DJANGO_SETTINGS_MODULE=core.settings`); `tests/` holds the pytest suite plus `tests/testapp/`'s concrete models, since the package itself is abstract-only; `tests/settings.py` extends `core.settings` and sets `LOCAL_APPS = ['tests.testapp']` so generated enforcement migrations land under `tests/`, never inside the shipped package. Edit source in `src/guitars/`; edit test models or harness in `tests/` or `core/`.

## Architecture

### The instrument ladder (`src/guitars/models/base.py`)

Abstract bases named by string count, each rung adds capability via mixins: `TarModel` = `UpdatableModel` + `HasCachedPropertyModel` (no columns, no DB behaviour — the root, unnumbered, "tār" = string); `DutarModel` (2) = `DatedModel` + `TarModel` (DB-managed `_created_at`/`_updated_at` + `app_label()`/`model_name()`/`class_name()` + field-listing `__repr__`); `SetarModel` (3) = `DutarModel` + `SoftDeletableModel` (its `Meta` inherits `SoftDeletableModel.Meta` — **the default rung** for a model that isn't tenanted); `GuitarModel` (6) = `SetarModel` + tenancy, the full kit — see [`docs/tenancy.md`](docs/tenancy.md). Each capability is also a standalone mixin exported from `guitars.models`.

> ⚠️ **Renamed in 1.0.0** — every rung shifted down one to make room for tenancy: 0.7's `DutarModel`→`TarModel`, `SetarModel`→`DutarModel`, `GuitarModel`→`SetarModel` (behaviour-identical); `GuitarModel` kept its name but now means "`SetarModel` + tenancy". See `CHANGELOG.md`.

### Database-enforced behavior is the whole design

The non-obvious core: **behavior is enforced by Postgres, not Python.** Three pieces work together:

1. **`src/guitars/sql/`** — every byte of raw SQL, re-exported flat from `sql/__init__.py`. **`guitars.sql`'s public names are a frozen interface** — migrations generated *before 1.1.0* and checked into consuming projects do `from guitars import sql` and read them by name, so a rename breaks `migrate` on a fresh database there (`tests/test_sql_interface.py` guards this). Migrations generated from 1.1.0 on carry their SQL literally and reference nothing, so the obligation is fixed in size rather than still growing.
2. **`makeguitarmigrations`** — a thin entry point over `src/guitars/management/enforcement/` (`headers.py`, `identity.py`, `scanning.py`, `operations.py`, `command.py`). It scans `settings.LOCAL_APPS` models for `_updated_at`/`_deleted_at`, writing `migrations.RunSQL(...)` with the SQL inlined. **Idempotent** via three mechanisms: a `[DIGEST:...]` marker on the first line, regex scans (`_RE_*`) of existing files, and a `[SQL:<digest>]` identity per operation header — the third is what makes an edited SQL constant generate its own migration, since the header scan short-circuits before the file digest is reached, so through 1.0.x a recognised header meant "covered forever" and the 1.0.0 guard rewrite shipped nothing. A header with no `[SQL:...]` is pre-1.1.0 and reads as stale. Each operation is emitted as plain `CREATE` (nothing recorded), `DROP`+`CREATE` (a recorded digest is stale), or `DROP ... IF EXISTS`+`CREATE` (only under `--adopt`) — `IF EXISTS` on a path where the answer is known would hide a diverged database. Cross-app CASCADE soft-delete rules are attributed to the *parent* model's app, so scoping to the child's app alone skips the rule; this surfaces as a runtime warning rather than silence — the accepted "pragmatic scope" tradeoff.
3. **`makemigrations` override** — by default runs the enforcement generation right after schema migrations (`call_command('makeguitarmigrations', ...)`), gated by `GUITARS_AUTO_MAKE_MIGRATIONS` (default `True`). Skips on `--empty`/`--dry-run` (the `--empty` guard also prevents infinite recursion, since `makeguitarmigrations` scaffolds via `makemigrations --empty`, re-entering this override). `--check` maps to the generator's `check_only`, validating both layers.

**Consequence:** with the default `GUITARS_AUTO_MAKE_MIGRATIONS = True`, `makemigrations` creates the triggers/rules for you. If `False`, plain `makemigrations` does NOT — run `makeguitarmigrations` yourself, and until it runs and you `migrate`, `.delete()` permanently deletes rows. Either way, `--check` fails when migrations are missing — used in CI.

### Feature detail lives in `docs/`

[`soft-deletion.md`](docs/soft-deletion.md) (rules, cascades, `hard_delete` two-phase) · [`migrations.md`](docs/migrations.md) (vocabulary, idempotency, frozen names, rule names, scaffolding, staged RLS) · [`mti.md`](docs/mti.md) (owner resolution, redirect rule, parent trigger, owner-join policy) · [`tenancy.md`](docs/tenancy.md) (both layers, settings, rollout, auditing) · [`api-reference.md`](docs/api-reference.md) (flat public-surface enumeration).

Decisions that were hard to reverse and are surprising without context are ADRs — see the index at [`docs/adr/`](docs/adr/README.md).

**Load-bearing details that are easy to break, kept here as a checklist:**

- `guitars.sql`'s public names and the generated operations' comment headers are a **frozen interface**. Renaming either breaks `migrate` on a fresh database in a consuming project, or makes the generator emit duplicates. `tests/test_sql_interface.py` guards the *names*; `tests/test_enforcement_identity.py` guards the *headers*, asserting every `HEADER_*` template is matched by the `_RE_*` meant to read it. Most `_RE_*` scanners in `src/guitars/management/enforcement/headers.py` are mechanically derived from their `HEADER_*` template (`_derive_scanner`), so they cannot drift from it by construction; the handful that fuse two header forms or must not capture their own placeholder stay hand-written, each with its own reason commented at the definition. `tests/test_header_corpus.py` guards all of them — derived and hand-written alike — against the project's own committed migration history.
- Enforcement SQL is **inlined** into generated migrations, never referenced by name. A migration that reads a library constant at `migrate` time changes meaning when the kit is upgraded — a fresh `migrate` and an incrementally-migrated database diverge while sharing an identical history. Do not "simplify" a generated operation back to `sql.X.format(...)`. A generated migration **depends on what its rules name**. PostgreSQL parses a rule's action at `CREATE` time, so a table or column it references in another app must already exist, and across apps only an explicit dependency orders that — a co-owner arm reads another app's column, a cascade rule names another app's table, an MTI redirect rule names the ancestor's table and its `_deleted_at`. The MTI `_updated_at` trigger is the one that needs nothing: its parent table is an escaped literal re-quoted at fire time, not a parse-time reference. The edge points at the migration that **creates** the object, never the referenced app's leaf: a creating migration is always older than a rule naming it, so it cannot close a cycle, while a leaf edge over-constrains the graph and drags unrelated migrations forward (that is what hand-patching one did in a consumer, trading one ordering failure for another). References are collected **structurally** as the rules are built, never by parsing rendered SQL — a co-owner arm's table appears only in the rule body, not its header. `--check` fails on a missing edge by graph **reachability**, not by the literal tuple, so an ordering guaranteed through another path is accepted, and an edge the generator drops for cycling is not demanded either (it would name a tuple Django rejects — red with no move that clears it); the digest guard skips an already-recorded migration, so the message prints the entry to paste rather than pretending a re-run will fix it. Edges are deliberately **not** in `[DIGEST:...]` — folding them in would re-digest every enforcement migration in every consuming project. See [ADR 0013](docs/adr/0013-cross-app-migration-dependency-edges.md).
- Rule guards are `<> 'on'`, never `= 'off'` — a rolled-back `set_config` reads back as the empty string, and `= 'off'` then failed toward *destroying* data. See `docs/soft-deletion.md`. `guitars.models.OwningForeignKey` is the kit's only custom field: its `deconstruct()` path is **frozen** (migrations record it literally), and its rule's last-owner `NOT EXISTS` guard is unconditional, never derived from a `UniqueConstraint` — dropping a constraint changes no field, so the `[SQL:...]` identity would not move and `--check` would stay green over a rule gone wrong. `hard_delete()` re-implements that predicate in Python, narrower **per row** (it removes a row where the rule stamps a column); the two change together. Not narrower absolutely: the rule's guard is per *statement*, so a target whose owners were all stamped by one `UPDATE` reads as still-owned and stays live, while `hard_delete()` — for which the whole batch is gone by construction — removes it, archive skipped. See `docs/owned-relations.md`. A rule is deduped by PostgreSQL on its **name** per table, so two operations resolving to one name replace rather than collide — the cascade family's plain `<table>_<fk>` suffix can be split two ways and is nonetheless **frozen** (0.x shipped it and nothing retires a rule, so a rename leaves the old rule live in every migrated project), which is why the generator *reports* a clash on stderr and fails `--check` over it instead; the owned family, new in 2.3.0, sizes **every** variable segment (`<n>_<table>_<n>_<fk>`, schema included), so reading each length before its segment leaves no boundary to guess at and no two triples reach one name — sizing only the last would have ruled out the adjacent split while `('a_5_b', 'c')` still met `('a', 'b_1_c')`. The report still covers both: a proof in one family is not a reason to stop watching. `hard_delete()`'s collection reaches no `GenericRelation`: right for the sparing half (no constraint to fail at `COMMIT`), a gap on the collecting half, since only Phase 1's `Collector` walks `_meta.private_fields` and an owned row is stamped by a rule rather than collected. Every `OwningForeignKey` in `tests/testapp` declares `DO_NOTHING`, matching what `docs/owned-relations.md` tells a consumer to use — `SET_NULL` has Django's `Collector` clear the key before the rule rewrites the `DELETE`, so the archived target becomes uncollectable, and `testapp` is what a consumer copies. Which relations carry a rule at all is a shared answer, read by the generator *and* by `hard_delete()`, and there are two halves of it — both over the **whole** registry. `introspection.rule_update_cycle_edges`: an `ON UPDATE` rule cycle bricks every table in it (PostgreSQL rejects every `UPDATE`, guard unread), so every edge on one is refused. `introspection.owned_tenancy_refusals` (2.4.1): a tenant policy on a table an arm reads hides a live owner, so that rule is refused too — `introspection.owner_arms` is the sweep both the arms and this refusal come from. Following in Python what the generator refused destroys what the rule spared, which is what shipping the second half a release late cost. See [ADR 0011](docs/adr/0011-owner-side-soft-delete-ownership.md).
- Column ownership is resolved via `model._meta.get_field(name).model` (`guitars.introspection`), never `hasattr`.
- `guitars.gucs` must import nothing and live outside `tenancy/`, so a generated migration's `from guitars import sql` does not drag in the tenancy runtime.
- The GUC cache key in `tenancy/guc.py` is deliberately more than the values. A stale cache leaves the *previous* tenant live, which fails **open**. Do not simplify it without a test that fails first.
- A tenant value containing `VALUE_SEPARATOR` (`,`) is **refused**, not escaped. The policy splits the GUC on it, so `'a,b'` would otherwise read as two tenants and match both. The guard is `tenancy.scope.reject_separator`, called from **two** places and redundant at neither: `tenant()` at scope entry, and `guc._scalar` at publish time, since a pk that was `None` at entry can still acquire a separator before the frame is published.
- The tenant policy is the one enforcement operation whose SQL depends on more than the table name, so its header carries a `[POLICY:<digest>]` identity and a changed shape emits `sql.replace_table_rls`. Dedupe on the table name alone let a model gain a tenant dimension while the database kept the old predicate, with `--check` green. `force` is excluded from that identity on purpose — it has its own `--force-rls` stage.
- `audittenancy` compares a live policy by its `tenant.*` GUCs and its `pg_depend` column references, never by SQL text: PostgreSQL rewrites a stored policy expression, so text equality can never hold. An autofill function's body *is* compared as text, but whitespace-collapsed (`sql.triggers._squeeze`, the one definition the whole-body compare and the guard probe share) against a body sliced from the one template the generator writes (`sql.triggers._tenant_autofill_body`) — a stored `$$…$$` body keeps the migration's indentation verbatim, so exact equality reports every differently-indented project as drift ([ADR 0010](docs/adr/0010-autofill-body-comparison.md)); the guard fragments the finding names must stay verbatim slices of that template, which `tests/test_enforcement_identity.py` asserts.

**`.update()` and signals:** `UpdatableModel.update(**attrs)`/`aupdate()` set fields + save in one call, writing only changed fields via `update_fields`; M2M via `.set(values, clear=True)` requires `_save=True`, and `_save=False` attrs are NOT carried into a later `_save=True` call unless `_save_all_fields=True`. `guitars.signals.DisableSignals` stashes/restores signal receivers, used by `update(_disable_signals=True)`.

## Commands

Requires [uv](https://docs.astral.sh/uv/) and Docker (for Postgres). Tests run against a **real** Postgres, no SQLite fallback, connecting as a **non-superuser** role (`scripts/postgres-init.sql`) — a superuser or `BYPASSRLS` role bypasses RLS unconditionally, making every RLS assertion pass vacuously. The role has `CREATEDB` so it owns its tables, the exact condition `FORCE ROW LEVEL SECURITY` exists to constrain. An older checkout needs `docker compose down -v` once.

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
python manage.py makeguitarmigrations --adopt  # re-register coverage this command didn't write
```

> ⚠️ **`manage.py` defaults to `core.settings`, which has no test app.** It uses `os.environ.setdefault`, so every `manage.py` command silently runs against a harness with `LOCAL_APPS = []` unless you export `DJANGO_SETTINGS_MODULE=tests.settings`. Without it `makemigrations --check` and `audittenancy` report **vacuously green**, having examined nothing. `pytest` is unaffected (it sets the module via `pyproject.toml`). Always export it before verifying migrations or tenancy by hand.

Set `GUITARS_AUTO_MAKE_MIGRATIONS = False` to make `makemigrations` skip the enforcement step and use the standalone command instead. Releasing (interactive helpers, see `scripts/README.md`):

```bash
./scripts/bump.sh minor       # bump pyproject.toml + seed CHANGELOG, commit
./scripts/release.sh          # git tag + push + GitHub release (gh)
```

`pyproject.toml` is the single source of truth for the version; `guitars.__version__` reads it from installed package metadata (`importlib.metadata`) — no second string to bump.

**Merging to `main` always requires a version bump.** `.github/workflows/tag-release.yml` tags `main` with `v<pyproject version>` on every push and fails if that version isn't strictly newer than the latest tag, then **calls** `_create-release.yml` as a reusable workflow. Run `./scripts/bump.sh` before merging to `main`.

Two things about that wiring look like they could be simplified but are load-bearing: **a call, not a tag-push trigger** (GitHub suppresses workflow triggers for events created with the default `GITHUB_TOKEN`, what pushes the tag, so `release.yml`'s `on: push: tags` would never fire for an auto-tag); and **it calls `_create-release.yml`, not `release.yml`** (a called workflow's `permissions` are validated *statically* across every job — `release.yml` holds the PyPI `publish` job with `id-token: write`, so calling it would fail startup unless the push-to-main run were granted OIDC). Folding the three files back into two reintroduces one of these. PyPI publishing itself lives in `release.yml` and is manual-only, triggered from the Actions tab via `workflow_dispatch`, checking the `publish` input.

Lint / type / security, run via pre-commit:

```bash
uv run ruff check src         # ruff lint (line-length 99, single quotes)
uv run ruff format src
uv run ty check               # type check (excludes tests/)
uv run bandit -c pyproject.toml -r src
```

`pytest` runs with `filterwarnings = ["error"]` and `xfail_strict` — warnings and unexpected passes fail the suite. `ruff` and `ty` are scoped to `src` and exclude `tests/`.

## Conventions

- Metadata fields are underscore-prefixed (`_created_at`, `_updated_at`, `_deleted_at`); non-default managers too (`_archives`, `_all_objects`).
- Editing SQL behavior means editing the relevant module under `src/guitars/sql/` **and** verifying `makeguitarmigrations` still emits/matches it. A new SQL name needs `sql/__init__.py` re-export *and* `FROZEN_SQL_NAMES` (`tests/test_sql_interface.py`), so a later rename is caught rather than shipped.
- Migration-file mechanics live once in `src/guitars/management/_generator.py`; MTI column-ownership resolution lives in `src/guitars/introspection.py` — both shared across every generating command. After model changes in `tests/testapp/`, regenerate migrations with `makemigrations` (also emits trigger/rule migrations by default).
