#!/usr/bin/env bash
# Re-vendor the hermes engine from the public fork into hermes/.
#
# Usage: scripts/revendor.sh <fork-ref> [fork-clone-path]
#   fork-ref         commit/branch/tag in the fork to vendor (e.g. 7b7fc4cfa
#                    or feat/claude-agent-sdk-provider)
#   fork-clone-path  local clone of fcavalcantirj/hermes-agent
#                    (default: ~/dev/hermes-agent)
#
# Mirrors the full engine tree (INCLUDING uv.lock/flake.lock — see .gitignore
# re-includes), bumps the engine SHA in README.md's Provenance section, and
# prints the diffstat. Never commits, never pushes — review, then commit with
# a "re-vendor: ..." message per README.md:66 ("Engine upgrades = re-vendor
# from the fork and update Provenance").
#
# Exclusions mirror deploy/box-bootstrap.sh's rsync (venv, node_modules).
set -euo pipefail

REF="${1:?usage: scripts/revendor.sh <fork-ref> [fork-clone-path]}"
FORK="${2:-$HOME/dev/hermes-agent}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

git -C "$FORK" rev-parse --verify --quiet "${REF}^{commit}" >/dev/null \
  || { echo "revendor: ref '$REF' not found in $FORK" >&2; exit 1; }
SHA="$(git -C "$FORK" rev-parse --short=9 "${REF}^{commit}")"

TMP="$(mktemp -d)"
cleanup() {
  git -C "$FORK" worktree remove --force "$TMP/wt" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT

git -C "$FORK" worktree add --detach "$TMP/wt" "$SHA" >/dev/null 2>&1

rsync -a --delete \
  --exclude '.git' --exclude 'venv' --exclude 'node_modules' \
  --exclude '__pycache__' \
  "$TMP/wt/" "$ROOT/hermes/"

# Provenance: rewrite the first engine SHA (the `@ **`<sha>`**` marker) in
# README.md. Prose around it (dates, notes) is the committer's to update.
perl -pi -e 'BEGIN{$done=0}
  if (!$done && /\@ \*\*`[0-9a-f]{7,}`\*\*/) {
    s/\@ \*\*`[0-9a-f]{7,}`\*\*/\@ **`'"$SHA"'`**/; $done=1
  }' "$ROOT/README.md"

# Stage with -f: the vendored hermes/.gitignore (upstream's own) otherwise silently
# drops files upstream force-adds in their repo (web fonts, p5js exporter, examples,
# userStories.json — 26 files as of r3). The index must mirror the fork tree.
git -C "$ROOT" add -f -A -- hermes/
git -C "$ROOT" add README.md
VENDORED="$(git -C "$ROOT" ls-files -- hermes/ | wc -l | tr -d ' ')"
UPSTREAM="$(git -C "$FORK" ls-tree -r --name-only "$SHA" | wc -l | tr -d ' ')"

echo "revendored hermes/ from $FORK @ $SHA"
echo
git -C "$ROOT" --no-pager diff --cached --stat -- hermes/ README.md | tail -15
echo
echo "staged files under hermes/: $VENDORED  (fork tree at $SHA: $UPSTREAM)"
if [ "$VENDORED" != "$UPSTREAM" ]; then
  echo "WARNING: staged/fork file-count mismatch — investigate before committing"
fi
echo
echo "review the staged diff, update Provenance prose if needed, then commit:"
echo "  git commit -m 're-vendor: engine @ $SHA'"
