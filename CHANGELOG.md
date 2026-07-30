# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-07-30

First stable release. Adds multi-tenancy as a database-enforced capability, and
renames the base models to make room for it.

### ⚠️ BREAKING

**Every rung of the instrument ladder shifted down one.** All three renames are
behaviour-identical — the same class under a new name:

| 0.7.0 | 1.0.0 | Behaviour |
| --- | --- | --- |
| `DutarModel` | `TarModel` | identical |
| `SetarModel` | `DutarModel` | identical |
| `GuitarModel` | `SetarModel` | identical |
| — | `GuitarModel` | **new meaning:** `SetarModel` + tenancy |

To upgrade without adopting tenancy, rename in that order — bottom-up, so no
intermediate state collides:

```
GuitarModel -> SetarModel
SetarModel  -> DutarModel
DutarModel  -> TarModel
```

Nothing else about those models changed: no new columns, no new migrations, no
behavioural difference. `makemigrations` should report no changes afterwards.

`GuitarModel` keeps its name and gains tenancy, so a model left on `GuitarModel`
by accident will fail loudly rather than quietly: without `GUITARS_TENANT_MODEL`
the `guitars.tenancy.E003` system check errors and names it.

**The soft-delete rule guards changed.** Databases migrated before 1.0.0 still
carry the old SQL — see *Fixed* below for why that matters and how to replace it.

### Added

- **Multi-tenancy**, enforced in two layers. `GuitarModel` contributes a non-null
  tenant `ForeignKey` (target `GUITARS_TENANT_MODEL`, name `GUITARS_TENANT_FIELD`,
  `CASCADE`, `editable=False`), wraps all three managers in `TenantedManager`, and
  gets a `tenant_scope` row-level-security policy from `makeguitarmigrations`.
  The Python layer raises `TenantScopeError` on an unscoped read or a cross-tenant
  write; the PostgreSQL layer enforces the same scope on joins, cascades,
  `_base_manager`, `instance.save()` and raw SQL. See
  [`docs/tenancy.md`](docs/tenancy.md).
- `guitars.tenancy` public API: `tenant()`, `tenancy_bypassed()`, `@tenanted`,
  `TenantedManager`, `TenantScopeError`, `get_tenant()`, `is_bypassed()`,
  `set_reporter()`, `tenant_spec()`, `local_tenant_fields()`.
- **MTI children get their own policy**, correlated to the ancestor holding the
  tenant column by the shared primary key. Relying on the ancestor's policy would
  leave every child-only statement unfiltered. See
  [ADR 0003](docs/adr/0003-mti-owner-join-policy.md).
- New settings: `GUITARS_TENANT_MODEL`, `GUITARS_TENANT_FIELD`,
  `GUITARS_TENANT_ENFORCE`, `GUITARS_TENANT_AUTOFILL`, `GUITARS_TENANT_POLICIES`,
  `GUITARS_RLS_FORCE`, `GUITARS_RLS_EXEMPT_ROLES`.
- New commands: `audittenancy` (asks a live database whether the policies bind,
  with `--require-force` for a deploy gate) and a `migrate` override that runs
  inside `tenancy_bypassed()` — without it a `RunPython` backfill under RLS
  matches no rows and is silently marked applied.
- `makeguitarmigrations --force-rls`, the second stage of a staged retrofit.
- System checks `guitars.tenancy.E001`–`E003`, registered at import of
  `guitars.models` so they fire even when guitars is used as a pure library.
- `docs/` — tenancy, soft deletion, migrations, MTI — plus four ADRs and
  `CONTEXT.md`, the domain glossary.

### Fixed

- **A rolled-back `hard_delete()` turned every later `.delete()` on that
  connection into a permanent delete.** `hard_delete()` sets
  `rules.hard_deletion` transaction-locally; PostgreSQL reverts that on rollback,
  but not to *unset* — a custom setting that was set and rolled back reads back as
  the **empty string**. The rule guards tested `= 'off'`, which the empty string
  matches neither way, so the `DO INSTEAD` rule stopped firing. With
  `CONN_MAX_AGE` or any pool the blast radius is the connection, not the
  transaction. All guards are now `<> 'on'`: anything but an explicit opt-in
  preserves the row.

  **Existing databases need one action.** The rules were created by migrations
  that call these SQL constants by name, so a database migrated before 1.0.0 still
  carries the old guard. Replace them by re-applying the enforcement migration:

  ```bash
  python manage.py migrate <app> <previous_migration>   # reverse_sql drops the rules
  python manage.py migrate <app>                        # forward re-creates them
  ```

  New databases are correct on first `migrate`. No new migration is generated —
  the idempotency digest covers the operation source, not the SQL it expands to.
- Role names in `GUITARS_RLS_EXEMPT_ROLES` were interpolated into policy SQL
  **unquoted**, so `metabase-ro` was a syntax error and `BI_Reader` silently bound
  `bi_reader`. Role-derived names are now quoted at both nesting levels, and every
  table/column identifier must prove it is a bare lower-case identifier or the
  generator refuses at build time rather than emitting SQL that fails inside
  `migrate`. Output for ordinary lower-case names is byte-identical.
- `makeguitarmigrations` reported a cascade foreign key reached through MTI as an
  unsupported limitation, when the ancestor's own rule already covers the whole
  chain through the shared `_deleted_at`.
- **A tenant policy was never regenerated once it existed**, so a model that gained
  a tenant dimension — or had its tenant column renamed, or its
  `GUITARS_RLS_EXEMPT_ROLES` edited — kept the *old, weaker* predicate in the
  database. Dedupe was keyed on the table name alone, and the comment header carried
  none of the predicate, so the generator emitted nothing and
  `makemigrations --check` reported nothing to do: the Python layer enforced the new
  dimension while row-level security enforced only the old one, silently, on exactly
  the paths (raw SQL, `_base_manager`, cascades) where the policy is the only guard.

  Policy headers now carry a `[POLICY:<digest>]` identity covering everything that
  determines what the policy says, and a changed shape emits `sql.replace_table_rls`
  — PostgreSQL has no `CREATE OR REPLACE POLICY`, so re-emitting the `CREATE` form
  would fail `migrate`. `force` is excluded from the identity, keeping the
  `--force-rls` retrofit workflow unchanged. See
  [`docs/migrations.md`](docs/migrations.md).
- **A tenant primary key containing a comma silently widened the policy to several
  tenants.** The predicate splits the published GUC on `,` and tests membership, so a
  single pk of `acme,globex` encoded identically to the two-tenant scope
  `['acme', 'globex']` and PostgreSQL read it as "tenant acme OR tenant globex" —
  while the Python manager filtered on the exact string and matched neither. The
  database half was therefore strictly *wider* than the Python half. Such a value is
  now refused when the scope is published, so it fails closed. Only reachable with a
  non-integer tenant primary key.
- `audittenancy` could not see a policy that existed but enforced the **wrong
  scope**, and counted a table without `FORCE` as "enforced" in its summary. It now
  compares each live policy's `tenant.*` settings and its `pg_depend` column
  references against what the models imply — warned by default, fatal under the new
  `--require-match` — and counts only clean tables as enforced. It also resolves a
  table name through the search path in the order PostgreSQL does, so two same-named
  tables in two schemas no longer collide.
- The settings are compared for **both halves of a policy separately**. `USING`
  governs reads and `WITH CHECK` governs writes, and they are independently
  editable: a policy left as `USING (<tenant match>) WITH CHECK (true)` reads as
  fully scoped while accepting every cross-tenant write, and neither the `USING`
  settings nor the `pg_depend` columns give it away (`true` references nothing).
- `makeguitarmigrations` interpolated the primary-key *field name* into the
  timestamp trigger and soft-delete rule where PostgreSQL needs the *column*. The
  two agree for an ordinary `id`, so this only bit a model whose primary key sets
  `db_column` or is a `OneToOneField(primary_key=True)` — where the generated rule
  named a column that does not exist and failed at `migrate`. The MTI forms already
  used the column.

### Changed

- `sql.py` became the `sql/` package (`triggers`, `soft_delete`, `policy`), still
  re-exported flat — `from guitars import sql` is unchanged, and every name in it
  remains a frozen interface that generated migrations read by name.
- Migration-file mechanics (digest stamping, scanning, `--empty` scaffolding, app
  scoping) moved to `guitars.management._generator`. MTI column-ownership
  resolution moved to `guitars.introspection`, shared by the generator and by
  tenancy coverage discovery so the two cannot disagree.
- The generated migrations and the command's output use one vocabulary,
  **enforcement migrations**, with four precisely-named kinds inside. The previous
  wording ("advanced migrations") described nothing, and the same concept had
  gathered four names. Generated filenames are now `*_auto_enforcement*`.
- `makeguitarmigrations` now also generates tenant policies, so there is still one
  command. `audittenancy` validates app labels the way the generator does, so a
  typo cannot produce a green gate that audited nothing.
- The test suite runs as a deliberately non-superuser PostgreSQL role that owns its
  tables (`scripts/postgres-init.sql`) — the exact condition `FORCE ROW LEVEL
  SECURITY` exists to constrain. Existing checkouts need
  `docker compose down -v` once.
- CI now gates on `makemigrations --check`, `audittenancy --require-force`, and
  100% coverage.

## [0.7.0] - 2026-07-06

### Added

- Full **multi-table inheritance (MTI)** support for dated and soft-deletable
  models. A concrete model subclassing another concrete `GuitarModel` now works
  end to end: `makeguitarmigrations` detects that a child's `_updated_at` /
  `_deleted_at` columns live on an ancestor table (via column ownership, not
  `hasattr`) and generates the right database objects — a redirect soft-delete
  rule that preserves the child row and marks the parent, a `set_parent_updated_at`
  trigger so a child-only `update()` still bumps the parent's `_updated_at`, and
  cascade rules attached to the owning table. `hard_delete()` (instance and
  queryset) now clears the whole MTI table chain with no orphaned parent row.
  Works at any inheritance depth via the shared-PK invariant.

### Notes

- MTI children of a soft-deletable base must declare their own `Meta` (an empty
  `class Meta: pass` suffices) so Django doesn't re-declare the parent's
  `_deleted_at` partial index against the child's non-local column
  (`models.E016`).
- Not yet supported: cascading *into* an MTI child through a FK on the child's
  own table when its `_deleted_at` lives on a farther ancestor; the command skips
  it with a warning rather than emitting a broken rule.

## [0.6.0] - 2026-07-03

### Changed

- Merged the separate `publish.yml` workflow into `release.yml`, now named
  "Release and Publish". A `vX.Y.Z` tag push still only creates the GitHub
  Release; PyPI publishing remains manual-only, opted into via the `publish`
  input on a `workflow_dispatch` run.
- Release/publish `workflow_dispatch` runs now select the tag from the native
  "Use workflow from" ref selector instead of a free-text input.
- Restricted CI to the `main` branch and removed the `develop` branch from the
  development flow.

## [Unreleased]

## [0.5.1] - 2026-07-03

### Added

- `makemigrations` now also generates the advanced trigger/rule migrations that
  `makeguitarmigrations` produces, so the soft-delete rules and `updated_at`
  triggers can no longer be silently forgotten. `makemigrations --check`
  validates both layers. Opt out with `GUITARS_AUTO_MAKE_MIGRATIONS = False` to
  keep the explicit two-command workflow; the standalone `makeguitarmigrations`
  command is unchanged.

### Changed

- `makeguitarmigrations` now accepts optional app labels to scope generation
  (e.g. `makeguitarmigrations blog`), and `makemigrations` forwards any app
  labels it receives, so a scoped `makemigrations blog` only generates guitar
  migrations for `blog`. With no labels, all `LOCAL_APPS` are scanned as before.
  An unknown app label is now rejected the same way Django's own
  `makemigrations` rejects one, so a typo can no longer turn `--check` into a
  silent no-op. Cross-app CASCADE soft-delete rules are attributed to the
  *parent* model's app, so scoping to a child app alone skips the rule; the
  command now prints a warning naming the skipped rule and the app to include
  to close the gap.
- (dev only) `publish.yml` is now `workflow_dispatch`-only instead of firing on
  every `vX.Y.Z` tag push, so shipping to PyPI is a deliberate manual step.
  `release.yml` now only creates a GitHub Release for tags reachable from
  `main`, and its "update an existing release" path no longer breaks (it
  previously passed `gh release edit` a `--generate-notes` flag that command
  doesn't support).

## [0.3.0] - 2026-06-11

### Added

- Interactive release tooling under `scripts/` (development only, not shipped
  in the wheel): `bump.sh` bumps `pyproject.toml` and seeds a changelog
  section; `release.sh` creates the git tag and GitHub release from the
  matching changelog notes. Documented in `scripts/README.md`.

### Changed

- `guitars.__version__` is now read from the installed package metadata
  (`importlib.metadata`) instead of a hardcoded string, making
  `pyproject.toml` the single source of truth for the version.

### Documentation

- `CLAUDE.md` repo guidance for contributors and AI assistants, plus a
  "Releasing" section in the README.
- Clarified the setar etymology (three strings by name) versus the model's
  actual string-count ladder.

## [0.2.0] - 2026-06-06

### Added

- `DutarModel` — the lightest base: `.update()` / `.aupdate()` and
  cached-property invalidation, with no timestamp or soft-delete columns.
- `DatedModel`, `UpdatableModel`, and `HasCachedPropertyModel` are now exported
  from `guitars.models` for composing custom bases.

### Changed

- `SetarModel` now builds on `DutarModel` (`DatedModel` + `DutarModel`); the
  public API is unchanged.

## [0.1.0] - 2026-06-04

### Added

- `SetarModel` — base abstract model: DB-default `_created_at` / `_updated_at`
  timestamps, `.update()` / `.aupdate()` helpers, and cached-property
  invalidation on `refresh_from_db()`.
- `GuitarModel` — `SetarModel` combined with `SoftDeletableModel`.
- `SoftDeletableModel` with `LiveManager` / `ArchiveManager` /
  `AllObjectsManager` — PostgreSQL-enforced soft deletion, cascade soft delete,
  and `hard_delete()`.
- `DisableSignals` context manager for temporarily muting Django signals.
- `makeguitarmigrations` management command — generates the PostgreSQL
  trigger/rule migrations behind the timestamps and soft deletion.

[Unreleased]: https://github.com/Behnam-RK/django-guitars/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.7.0
[0.6.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.6.0
[0.5.1]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.5.1
[0.3.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.3.0
[0.2.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.2.0
[0.1.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.1.0
