# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
