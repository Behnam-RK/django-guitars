#!/usr/bin/env bash
#
# bump.sh — interactive version bumper for django-guitars.
#
# Bumps the version in pyproject.toml and seeds a new CHANGELOG.md section
# (with a release-tag link reference) so ./scripts/release.sh can tag and
# publish straight afterwards.
#
# Usage:
#   ./scripts/bump.sh patch      # 0.2.0 -> 0.2.1
#   ./scripts/bump.sh minor      # 0.2.0 -> 0.3.0
#   ./scripts/bump.sh major      # 0.2.0 -> 1.0.0
#   ./scripts/bump.sh 1.4.2      # explicit version
#
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

PYPROJECT="pyproject.toml"
CHANGELOG="CHANGELOG.md"

# --- pretty output ----------------------------------------------------------
c_blue=$'\033[34m'; c_green=$'\033[32m'; c_yellow=$'\033[33m'
c_red=$'\033[31m'; c_bold=$'\033[1m'; c_reset=$'\033[0m'
info()  { printf '%s==>%s %s\n' "$c_blue" "$c_reset" "$*"; }
ok()    { printf '%s✓%s %s\n'   "$c_green" "$c_reset" "$*"; }
warn()  { printf '%s!%s %s\n'   "$c_yellow" "$c_reset" "$*"; }
die()   { printf '%s✗%s %s\n'   "$c_red" "$c_reset" "$*" >&2; exit 1; }
confirm() { local r; read -r -p "$1 [y/N] " r; [[ "$r" =~ ^[Yy]$ ]]; }

# --- args -------------------------------------------------------------------
[[ $# -eq 1 ]] || die "usage: $0 {major|minor|patch|X.Y.Z}"
ARG="$1"

[[ -f "$PYPROJECT" ]] || die "no $PYPROJECT in repo root."
CURRENT="$(grep -m1 -E '^version[[:space:]]*=' "$PYPROJECT" | sed -E 's/.*"([^"]+)".*/\1/')"
[[ "$CURRENT" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "current version '$CURRENT' is not X.Y.Z."

IFS='.' read -r MA MI PA <<< "$CURRENT"

case "$ARG" in
  major) NEW="$((MA+1)).0.0" ;;
  minor) NEW="${MA}.$((MI+1)).0" ;;
  patch) NEW="${MA}.${MI}.$((PA+1))" ;;
  [0-9]*.[0-9]*.[0-9]*)
    [[ "$ARG" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "explicit version must be X.Y.Z."
    NEW="$ARG" ;;
  *) die "unknown bump '$ARG' (use major|minor|patch|X.Y.Z)." ;;
esac

[[ "$NEW" != "$CURRENT" ]] || die "new version equals current ($CURRENT)."

TAG="v${NEW}"
info "Current: ${c_bold}${CURRENT}${c_reset}"
info "New:     ${c_bold}${NEW}${c_reset}  (tag ${TAG})"

# --- guard: tag must not already exist --------------------------------------
if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null 2>&1; then
  die "tag ${TAG} already exists. Pick a higher version."
fi

# --- date (no GNU/BSD dependency beyond %F) ---------------------------------
TODAY="$(date +%F)"

# --- repo URL for the changelog link reference ------------------------------
REMOTE="$(git remote get-url origin 2>/dev/null || true)"
REPO_PATH="$(printf '%s' "$REMOTE" \
  | sed -E 's#^git@github\.com:#https://github.com/#; s#\.git$##')"
[[ "$REPO_PATH" == https://github.com/* ]] || REPO_PATH="https://github.com/Behnam-RK/django-guitars"

echo
confirm "Apply bump ${CURRENT} -> ${NEW}?" || die "Aborted."

# --- 1. pyproject.toml version ----------------------------------------------
# Only the first top-level `version = "..."` (project version), not the
# tool.* required-version / target-version lines further down.
perl -0pi -e 'BEGIN{$n=0} s/^(version\s*=\s*")[^"]+(")/${1}'"$NEW"'${2}/m && $n++ unless $n' "$PYPROJECT"
GREP_NEW="$(grep -m1 -E '^version[[:space:]]*=' "$PYPROJECT" | sed -E 's/.*"([^"]+)".*/\1/')"
[[ "$GREP_NEW" == "$NEW" ]] || die "pyproject.toml version did not update (got '$GREP_NEW')."
ok "pyproject.toml -> ${NEW}"
# Note: src/guitars/__init__.py reads its version from installed package
# metadata (importlib.metadata), so pyproject.toml is the single source of
# truth — nothing else to bump.

# --- 2. CHANGELOG.md section + link reference -------------------------------
if [[ -f "$CHANGELOG" ]]; then
  if grep -qE "^## \[${NEW//./\\.}\]" "$CHANGELOG"; then
    warn "CHANGELOG already has a [${NEW}] section; leaving it untouched."
  else
    NEW_SECTION="## [${NEW}] - ${TODAY}\n\n### Added\n\n- \n\n### Changed\n\n- \n\n### Fixed\n\n- \n"
    # Insert the new section directly above the first existing release section.
    perl -0pi -e 'BEGIN{$s="'"$NEW_SECTION"'\n"; $done=0}
                  s/(^## \[)/$done++ ? "$1" : "$s$1"/me' "$CHANGELOG"
    # Add the tag link reference above the first existing reference line.
    LINK="[${NEW}]: ${REPO_PATH}/releases/tag/${TAG}"
    perl -0pi -e 'BEGIN{$l="'"$LINK"'\n"; $done=0}
                  s/(^\[[0-9])/$done++ ? "$1" : "$l$1"/me' "$CHANGELOG"
    ok "CHANGELOG.md seeded [${NEW}] - ${TODAY}"
    warn "Fill in the new CHANGELOG section before releasing."
  fi
else
  warn "no $CHANGELOG; skipped changelog update."
fi

# --- 3. show diff -----------------------------------------------------------
echo
info "Diff:"
git --no-pager diff -- "$PYPROJECT" "$CHANGELOG" || true

# --- 4. optional commit -----------------------------------------------------
echo
if confirm "Commit the bump?"; then
  git add "$PYPROJECT" "$CHANGELOG"
  git commit -m "chore: bump version to ${NEW}"
  ok "committed"
  info "Next: edit CHANGELOG, then ./scripts/release.sh"
else
  warn "left staged changes unmade; commit manually when ready."
fi

ok "Done."
