# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `makemigrations` now also generates the advanced trigger/rule migrations that
  `makeguitarmigrations` produces, so the soft-delete rules and `updated_at`
  triggers can no longer be silently forgotten. `makemigrations --check`
  validates both layers. Opt out with `GUITARS_AUTO_MAKE_MIGRATIONS = False` to
  keep the explicit two-command workflow; the standalone `makeguitarmigrations`
  command is unchanged.

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

[0.3.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.3.0
[0.2.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.2.0
[0.1.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.1.0
