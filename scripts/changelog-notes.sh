#!/usr/bin/env bash
#
# changelog-notes.sh — print the CHANGELOG.md section for one version.
#
# Extracts the body under "## [VERSION] ..." up to the next "## [" header and
# writes it to stdout (nothing if there is no such section). Shared by
# release.sh and the release.yml workflow so the extraction lives in one place.
#
# Usage: changelog-notes.sh <version> [changelog-path]
#
set -euo pipefail

VERSION="${1:?usage: changelog-notes.sh <version> [changelog-path]}"
CHANGELOG="${2:-CHANGELOG.md}"

[ -f "$CHANGELOG" ] || exit 0

awk -v ver="$VERSION" '
  $0 ~ "^## \\[" ver "\\]" { capture=1; next }
  capture && /^## \[/      { exit }
  capture                  { print }
' "$CHANGELOG" | sed -e '/./,$!d' | awk 'NR>1 || NF'
