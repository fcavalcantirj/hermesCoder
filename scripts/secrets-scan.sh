#!/usr/bin/env bash
# secrets-scan.sh — refuse to ship a credential. Run from the repo root before
# every push. Exit 0 = clean, 1 = HIT (prints file:line), 2 = usage error.
#
# Red-on-demand: SECSCAN_TEST=1 plants a fake telegram token in a temp file
# inside the tree, asserts the scan FAILS on it, cleans up, then runs for real.
# Shapes covered (real-credential shapes, not placeholder prefixes):
#   - Telegram bot token        1234567890:AA... (35-char tail)
#   - Private key PEM headers
#   - Anthropic key             sk-ant-<20+>
#   - GitHub tokens             gho_/ghp_/github_pat_
#   - Filled env assignments    TELEGRAM_BOT_TOKEN=<value>, *_OAUTH_TOKEN=, JINA_API_KEY=, ANTHROPIC_API_KEY=
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

# Scope: OUR layer. hermes/ is excluded — it is a verbatim snapshot of the
# PUBLIC upstream source (its docs/tests are full of placeholder tokens and
# redaction fixtures; anything in it is already public on GitHub). Test files
# are excluded for the same reason (deliberately-fake tokens in leak-tests).
# The env-assignment pattern ignores values starting with $ (template
# interpolation in box-bootstrap.sh), and empty values (env.template).
scan() {
  grep -rInE \
    -e '[0-9]{8,10}:AA[A-Za-z0-9_-]{33}' \
    -e '-----BEGIN [A-Z ]*PRIVATE KEY' \
    -e 'sk-ant-[A-Za-z0-9_-]{20,}' \
    -e 'gho_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}' \
    -e '^(TELEGRAM_BOT_TOKEN|CLAUDE_CODE_OAUTH_TOKEN|JINA_API_KEY|ANTHROPIC_API_KEY|SOLVR_ROOM_TOKEN)=[^[:space:]#"'"'"'$]+' \
    --exclude-dir=.git --exclude-dir=__pycache__ --exclude-dir=.pytest_cache \
    --exclude-dir=hermes --exclude-dir=test --exclude-dir=tests \
    --exclude=secrets-scan.sh --exclude='*.test.ts' \
    .
}

if [ "${SECSCAN_TEST:-}" = "1" ]; then
  probe=".secscan-probe.tmp"
  printf 'TELEGRAM_BOT_TOKEN=1234567890:AA%s\n' "$(printf 'x%.0s' $(seq 1 33))" > "$probe"
  if scan >/dev/null 2>&1; then
    rm -f "$probe"
    echo "RED-TEST OK: planted token was detected"
  else
    rm -f "$probe"
    echo "RED-TEST FAILED: planted token NOT detected — scanner is broken" >&2
    exit 2
  fi
fi

hits="$(scan)"
if [ -n "$hits" ]; then
  echo "SECRETS-SCAN: HIT(S) FOUND — do not push:" >&2
  echo "$hits" >&2
  exit 1
fi
echo "secrets-scan: clean"
