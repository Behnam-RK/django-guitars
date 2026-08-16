# Changelog

<!-- doc-budget: exempt — release history; length tracks release count, not verbosity -->

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Full history and diffs: [GitHub releases](https://github.com/Behnam-RK/django-guitars/releases).

## [Unreleased]

## [2.1.0] - 2026-08-16

- Added: tenant autofill is now a `BEFORE INSERT` trigger ([ADR-0005](docs/adr/0005-trigger-based-tenant-autofill.md)), covering `bulk_create`, multi-row `INSERT`, `INSERT … SELECT` and raw SQL. Run `makemigrations` + `migrate`; **no backfill is needed or possible** — the tenant column is `NOT NULL`, so an existing `NULL` cannot exist.
- Changed: the `pre_save` write guard is now diagnostics. `DisableSignals` (and `update(_disable_signals=True)`) no longer disables tenancy enforcement; it costs the friendly message, not the guarantee.
- Added: `audittenancy` reports a table whose manager autofills but whose trigger is missing — warned by default, fatal under `--require-match`.
- Added: `TableCoverage.autofill_columns`, and `autofill_function_name()`/`autofill_trigger_name()` in `guitars.tenancy.discovery`. The trigger is named after the function it calls, so a table tenanted on two local dimensions gets one trigger per `(column, GUC)` pair instead of two colliding on one name.

## [2.0.3] - 2026-08-14

- Repo-wide documentation shrink pass under an enforced line budget (`scripts/doc_budget.py`, wired into pre-commit).

## [2.0.2] - 2026-08-14

- [ADR-0005](docs/adr/0005-trigger-based-tenant-autofill.md) moves to **accepted**, marked not yet implemented (targeted 2.1.0).

## [2.0.1] - 2026-08-14

- Added: ADR index, three new ADRs, `docs/api-reference.md`.
- Fixed: `scripts/bump.sh` release-section placement and a false "seeded changelog" report; `LOCAL_APPS`/`--adopt`+`--force-rls` doc corrections.

## [2.0.0] - 2026-08-06

- **BREAKING:** all generated SQL now quotes/validates identifiers; `db_table` may be schema-qualified. Run `makeguitarmigrations`/`migrate` once (SQL text only, no data changes).
- **BREAKING:** unscoped-queryset deny-list is now an allow-list; `Manager.raw()` denied unscoped.
- **BREAKING:** `TenantScopeError` splits into `TenantScopeMissing`/`TenantScopeViolation`/`TenantValueError`; `TenantedManager` renamed `tenanted_manager`; `guitars.tenancy.__all__` trimmed (GUC names → `guitars.gucs`, `tenant_spec`/`local_tenant_fields` → `.spec`, lifecycle hooks → `.testing`).
- **BREAKING:** `update(_save=False, _save_all_fields=True)` now raises; `SoftDeletableModel.cls` removed.
- Added: schema-qualified `db_table` end-to-end; `guitars.tenancy.W002` pooling-leak check.
- Changed: audit-mode `Reporter` receives structured context; several internal duplications consolidated.

## [1.3.0] - 2026-08-02

- Added: behavioural test families for concurrency, drift, legacy-migration upgrade, migrate-override, property-based fuzzing, MTI owner-join at depth.

## [1.2.0] - 2026-08-02

- Added: 100%-gated branch coverage; a Python×Django×PostgreSQL CI matrix; `psycopg` extra; a runtime drift check over Django's `QuerySet` surface.

## [1.1.3] - 2026-08-02

- Fixed: a switch-off failure after a successful `hard_delete()` was swallowed, leaking the hard-deletion switch on; `update(_disable_signals=True)` mis-reported a bypass on a no-op write.

## [1.1.2] - 2026-08-02

- Fixed: `DisableSignals` race could permanently disconnect every signal receiver; `_disable_signals=True` over-suppressed all eight `DEFAULT_SIGNALS`; `update()` collapsed an empty field set into a full-row rewrite; `hard_delete()` ignored `self.db`; deny-list missed `_raw_delete`/`explain()`.

## [1.1.1] - 2026-07-31

- Changed: upgraded pinned GitHub Actions versions.

## [1.1.0] - 2026-07-31

- **Action required:** `makemigrations --check` fails on first run after upgrade by design — run it once to deliver the 1.0.0 soft-delete guard fix to existing databases.
- Added: `makeguitarmigrations --adopt`; refresh/adopt SQL forms.
- Fixed: generated migrations no longer `from guitars import sql`, carrying SQL literally instead; changed trigger-function/tenant-policy SQL now correctly re-emits.

## [1.0.2] - 2026-07-31

- Added: `actionlint` in pre-commit.

## [1.0.1] - 2026-07-31

- Fixed: a version bump merged to `main` never actually cut a release, since GitHub suppresses triggers for tags pushed with the default token; `tag-release.yml` now calls `release.yml` directly.

## [1.0.0] - 2026-07-30

First stable release. **BREAKING:** the instrument ladder shifted down one rung (behaviour-identical) to make room for multi-tenancy; `GuitarModel` keeps its name and gains tenancy.
- **BREAKING:** soft-delete rule guards changed from `= 'off'` to `<> 'on'` — see Fixed.
- Added: multi-tenancy end to end (tenant FK, scoped managers, RLS policy, `audittenancy`, tenancy-bypassed `migrate`, system checks); MTI owner-correlated policies; `docs/` and four ADRs.
- Fixed: leaking hard-deletion switch after rollback; missed `cached_property` expiry; `hard_delete()` under server-side binding; unquoted exempt-role names; stale tenant policies after model changes; a comma in a tenant pk widening RLS match; `audittenancy` blind to wrong-scope policies; pk field name used where a column was needed.

## [0.7.0] - 2026-07-06

- Added: full MTI support for dated/soft-deletable models at any depth (requires an MTI child to declare its own empty `Meta`). Not yet supported: cascading into an MTI child via an FK on its own table when `_deleted_at` lives farther up.

## [0.6.0] - 2026-07-03

- Changed: merged `publish.yml` into `release.yml`; CI restricted to `main`.

## [0.5.1] - 2026-07-03

- Changed: `publish.yml` made `workflow_dispatch`-only.

## [0.5.0] - 2026-07-03

- Added: `makemigrations` generates enforcement migrations by default; both commands accept scoping app labels; CI workflows added. Fixed: app-label validation; cross-app cascade-rule skip now warns.

## [0.3.0] - 2026-06-11

- Added: interactive release tooling under `scripts/`; `CLAUDE.md`.

## [0.2.0] - 2026-06-06

- Added: `DutarModel`; `DatedModel`/`UpdatableModel`/`HasCachedPropertyModel` exported.

## [0.1.0] - 2026-06-04

- Added: initial release — `SetarModel`, `GuitarModel`, `SoftDeletableModel`, `DisableSignals`, `makeguitarmigrations`.

[Unreleased]: https://github.com/Behnam-RK/django-guitars/compare/v2.1.0...HEAD
[2.1.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v2.1.0
[2.0.3]: https://github.com/Behnam-RK/django-guitars/releases/tag/v2.0.3
[2.0.2]: https://github.com/Behnam-RK/django-guitars/releases/tag/v2.0.2
[2.0.1]: https://github.com/Behnam-RK/django-guitars/releases/tag/v2.0.1
[2.0.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v2.0.0
[1.3.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v1.3.0
[1.2.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v1.2.0
[1.1.3]: https://github.com/Behnam-RK/django-guitars/releases/tag/v1.1.3
[1.1.2]: https://github.com/Behnam-RK/django-guitars/releases/tag/v1.1.2
[1.1.1]: https://github.com/Behnam-RK/django-guitars/releases/tag/v1.1.1
[1.1.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v1.1.0
[1.0.2]: https://github.com/Behnam-RK/django-guitars/releases/tag/v1.0.2
[1.0.1]: https://github.com/Behnam-RK/django-guitars/releases/tag/v1.0.1
[1.0.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v1.0.0
[0.7.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.7.0
[0.6.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.6.0
[0.5.1]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.5.1
[0.5.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.5.0
[0.3.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.3.0
[0.2.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.2.0
[0.1.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.1.0
