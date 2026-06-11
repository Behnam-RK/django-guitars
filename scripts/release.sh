#!/usr/bin/env bash
#
# release.sh — interactive git tag + GitHub release for django-guitars.
#
# Reads the version from pyproject.toml, confirms the tag with you, creates an
# annotated tag, pushes it, and opens a GitHub release whose notes are the
# matching section pulled out of CHANGELOG.md (falling back to gh's
# auto-generated notes when the version has no changelog entry).
#
# Usage:
#   ./scripts/release.sh                 # version from pyproject.toml
#   ./scripts/release.sh 0.3.0           # override the version
#
set -euo pipefail

# --- locate repo root regardless of where the script is called from ---------
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

confirm() {  # confirm "prompt" -> returns 0 on yes
  local reply
  read -r -p "$1 [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]]
}

# --- preflight --------------------------------------------------------------
command -v gh >/dev/null   || die "gh (GitHub CLI) not found. Install: https://cli.github.com"
command -v git >/dev/null  || die "git not found."
gh auth status >/dev/null 2>&1 || die "gh not authenticated. Run: gh auth login"
[[ -f "$PYPROJECT" ]] || die "no $PYPROJECT in repo root."

# Clean tree — refuse to tag uncommitted work.
if [[ -n "$(git status --porcelain)" ]]; then
  warn "working tree is dirty:"
  git status --short
  confirm "Tag anyway?" || die "Aborted. Commit or stash first."
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"

# --- resolve version --------------------------------------------------------
if [[ $# -ge 1 ]]; then
  VERSION="$1"
else
  VERSION="$(grep -m1 -E '^version[[:space:]]*=' "$PYPROJECT" | sed -E 's/.*"([^"]+)".*/\1/')"
fi
[[ -n "${VERSION:-}" ]] || die "could not determine version."

TAG="v${VERSION}"
info "Project version: ${c_bold}${VERSION}${c_reset}"
info "Tag to create:   ${c_bold}${TAG}${c_reset}"
info "Branch:          ${c_bold}${BRANCH}${c_reset}"

# Bail early if the tag already exists.
if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
  die "tag ${TAG} already exists locally. Bump the version or delete the tag."
fi
if git ls-remote --exit-code --tags origin "refs/tags/${TAG}" >/dev/null 2>&1; then
  die "tag ${TAG} already exists on origin."
fi

# --- extract release notes from CHANGELOG -----------------------------------
# Pulls the body under "## [VERSION] ..." up to the next "## [" header.
NOTES=""
if [[ -f "$CHANGELOG" ]]; then
  NOTES="$(awk -v ver="$VERSION" '
    $0 ~ "^## \\[" ver "\\]" { capture=1; next }
    capture && /^## \[/      { exit }
    capture                  { print }
  ' "$CHANGELOG" | sed -e '/./,$!d' | awk 'NR>1 || NF')"
fi

NOTES_FILE="$(mktemp)"
trap 'rm -f "$NOTES_FILE"' EXIT
if [[ -n "$NOTES" ]]; then
  printf '%s\n' "$NOTES" > "$NOTES_FILE"
  info "Release notes (from ${CHANGELOG}):"
  printf '%s----------------------------------------%s\n' "$c_bold" "$c_reset"
  cat "$NOTES_FILE"
  printf '%s----------------------------------------%s\n' "$c_bold" "$c_reset"
  USE_AUTO=0
else
  warn "no ${CHANGELOG} section for ${VERSION}; will use GitHub auto-generated notes."
  USE_AUTO=1
fi

# --- confirm and tag --------------------------------------------------------
echo
confirm "Create annotated tag ${TAG} on ${BRANCH} and push to origin?" \
  || die "Aborted before tagging."

git tag -a "$TAG" -m "Release ${TAG}"
ok "created tag ${TAG}"

git push origin "$TAG"
ok "pushed ${TAG} to origin"

# --- GitHub release ---------------------------------------------------------
PRERELEASE_FLAG=()
if [[ "$VERSION" =~ (a|b|rc|alpha|beta|dev)[0-9]* ]] || [[ "$VERSION" == 0.* ]]; then
  if confirm "Mark as pre-release?"; then
    PRERELEASE_FLAG=(--prerelease)
  fi
fi

echo
if confirm "Create GitHub release for ${TAG}?"; then
  if [[ "$USE_AUTO" -eq 1 ]]; then
    gh release create "$TAG" \
      --title "$TAG" \
      --generate-notes \
      --verify-tag \
      "${PRERELEASE_FLAG[@]}"
  else
    gh release create "$TAG" \
      --title "$TAG" \
      --notes-file "$NOTES_FILE" \
      --verify-tag \
      "${PRERELEASE_FLAG[@]}"
  fi
  ok "GitHub release published"
  gh release view "$TAG" --web >/dev/null 2>&1 || true
else
  warn "tag pushed but no GitHub release created."
  warn "create later: gh release create ${TAG} --generate-notes"
fi

ok "Done."
