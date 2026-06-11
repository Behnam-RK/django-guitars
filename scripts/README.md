# Release scripts

Two interactive helpers that drive a release end to end. Run them from
anywhere in the repo — both `cd` to the repo root themselves.

```
bump.sh   →  edit pyproject.toml + CHANGELOG.md, commit
release.sh →  git tag + push + GitHub release
```

`pyproject.toml` is the **single source of truth** for the version;
`src/guitars/__init__.py` reads `__version__` from the installed package
metadata (`importlib.metadata`), so nothing else needs bumping.

## `bump.sh` — bump the version

```bash
./scripts/bump.sh patch      # 0.3.0 -> 0.3.1
./scripts/bump.sh minor      # 0.3.0 -> 0.4.0
./scripts/bump.sh major      # 0.3.0 -> 1.0.0
./scripts/bump.sh 1.4.2      # explicit version
```

What it does:

1. Reads the current version from `pyproject.toml` and computes the new one.
2. Refuses to continue if the target tag (`vX.Y.Z`) already exists.
3. Rewrites the project `version` in `pyproject.toml` (leaves the
   `tool.*` `required-version` / `target-version` lines alone).
4. Seeds a `## [X.Y.Z] - <today>` section at the top of `CHANGELOG.md` with
   `Added` / `Changed` / `Fixed` placeholders and a release-tag link reference.
5. Shows the diff, then (on confirm) commits `pyproject.toml` + `CHANGELOG.md`
   as `chore: bump version to X.Y.Z`.

Each mutating step is gated by a `y/N` prompt.

> **Fill the changelog first.** The seeded section ships empty `-` bullets,
> which the trailing-whitespace pre-commit hook rejects — so the in-script
> commit will abort until you write real notes into the new section. Edit the
> section, then `git commit` (or re-run with notes already filled).

## `release.sh` — tag and publish

```bash
./scripts/release.sh         # version from pyproject.toml
./scripts/release.sh 0.4.0   # override the version
```

What it does:

1. Preflight: checks `git`, `gh`, and `gh auth status`; warns on a dirty tree.
2. Resolves the version (arg or `pyproject.toml`) and the tag `vX.Y.Z`.
3. Aborts if the tag already exists locally **or** on `origin`.
4. Pulls the matching `## [X.Y.Z]` section out of `CHANGELOG.md` for the release
   notes; falls back to `gh --generate-notes` when there is no such section.
5. On confirm: creates an annotated tag and pushes it to `origin`.
6. Offers `--prerelease` for `0.x` / `aN` / `bN` / `rcN` versions.
7. On confirm: `gh release create` with the notes, then opens it in the browser.

Each mutating step is gated by a `y/N` prompt.

## Typical flow

```bash
./scripts/bump.sh minor      # bump + seed changelog
$EDITOR CHANGELOG.md         # write the release notes, then commit
./scripts/release.sh         # tag + GitHub release
```

## Requirements

- [`gh`](https://cli.github.com) — authenticated (`gh auth login`). Only
  `release.sh` needs it.
- `perl` — used by `bump.sh` for the in-place edits (preinstalled on macOS
  and most Linux).
