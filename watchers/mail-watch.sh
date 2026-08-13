#!/usr/bin/env bash
# DasBrowCoder — mail watcher (Composio Gmail lane). Modeled on pr-watch.sh.
# Deterministic, no LLM: every 15 min (cron) polls Gmail READ-ONLY through the
# box's existing Composio connection and Telegram-nudges the owner about mail
# matching MAILWATCH_QUERY that hasn't been seen before. Born 2026-08-13 after
# an inbound fork PR (romain-bury #2) sat 30h unseen — GitHub notification
# EMAIL was the only signal, and nothing watched email.
#
# INVARIANT: this watcher NEVER mutates the inbox. No mark-read, no modify,
# no labels — dedupe is state-file only (seen Gmail message ids, one per
# line, beside the script). Gmail's unread flags belong to the owner.
#
# First run seeds silently (stores ids, sends nothing) — house rule, same as
# pr-watch. The Composio key rides an x-api-key header read from a 0600 file,
# never argv, never logs.
#
# REQUIRED env (no personal defaults, fail-fast):
#   TELEGRAM_CHAT_ID     where nudges go
#   MAILWATCH_ACCOUNT    Composio connected_account_id (gmail, ACTIVE)
#   MAILWATCH_USER_ID    Composio user_id owning that account
# OPTIONAL env:
#   COMPOSIO_KEY_FILE    default ~/.composio.key
#   MAILWATCH_QUERY      default 'from:notifications@github.com is:unread'
#                        (widen deliberately; 'is:unread' alone is a firehose)
#   MAILWATCH_MAX        default 10 (per-poll fetch cap; a busier query
#                        saturates instead of flooding — nudge says "+more")
#   HERMES_ENV_FILE      default ~/.hermes/.env (TELEGRAM_BOT_TOKEN source)
#   MAILWATCH_TEST       1 = prefix nudge with 🧪 (red-on-demand test; doctor
#                        the state file first by deleting one id)
#
# Red-on-demand test:
#   remove one line from mail-watch.state, then MAILWATCH_TEST=1 ./mail-watch.sh
#   — the 🧪-prefixed message must arrive. Never red-test without the flag.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="$HERE/mail-watch.state"

: "${TELEGRAM_CHAT_ID:?TELEGRAM_CHAT_ID required}"
: "${MAILWATCH_ACCOUNT:?MAILWATCH_ACCOUNT required (Composio connected_account_id)}"
: "${MAILWATCH_USER_ID:?MAILWATCH_USER_ID required (Composio user_id)}"
COMPOSIO_KEY_FILE="${COMPOSIO_KEY_FILE:-$HOME/.composio.key}"
MAILWATCH_QUERY="${MAILWATCH_QUERY:-from:notifications@github.com is:unread}"
MAILWATCH_MAX="${MAILWATCH_MAX:-10}"
HERMES_ENV_FILE="${HERMES_ENV_FILE:-$HOME/.hermes/.env}"

[ -r "$COMPOSIO_KEY_FILE" ] || { echo "FATAL: unreadable $COMPOSIO_KEY_FILE" >&2; exit 1; }

# Telegram token: env wins, else the hermes env file (pr-watch pattern).
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] && [ -r "$HERMES_ENV_FILE" ]; then
  TELEGRAM_BOT_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$HERMES_ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' || true)"
fi
[ -n "${TELEGRAM_BOT_TOKEN:-}" ] || { echo "FATAL: no TELEGRAM_BOT_TOKEN (env or $HERMES_ENV_FILE)" >&2; exit 1; }

FIRST_RUN=0
[ -f "$STATE" ] || { FIRST_RUN=1; : > "$STATE"; }

# Poll Composio (READ-ONLY fetch; include_payload=false keeps it light).
FETCH_JSON="$(curl -s -m 45 -X POST \
  'https://backend.composio.dev/api/v3/tools/execute/GMAIL_FETCH_EMAILS' \
  -H "x-api-key: $(cat "$COMPOSIO_KEY_FILE")" \
  -H 'Content-Type: application/json' \
  -d "$(python3 - "$MAILWATCH_ACCOUNT" "$MAILWATCH_USER_ID" "$MAILWATCH_QUERY" "$MAILWATCH_MAX" <<'PYBODY'
import json, sys
print(json.dumps({
    "connected_account_id": sys.argv[1],
    "user_id": sys.argv[2],
    "arguments": {
        "query": sys.argv[3],
        "max_results": int(sys.argv[4]),
        "include_payload": False,
    },
}))
PYBODY
)")" || { echo "$(date -Is) poll failed (curl)" >&2; exit 1; }

# Parse + diff against state. Emits nudge lines for unseen ids; appends them.
# JSON rides a file: stdin already carries the heredoc script, it can't also
# carry the payload (a pipe would be silently clobbered by the heredoc).
POLL_FILE="$HERE/.last-poll.json"
printf '%s' "$FETCH_JSON" > "$POLL_FILE"
NUDGE="$(python3 - "$STATE" "$POLL_FILE" <<'PYDIFF'
import json, sys

state_path = sys.argv[1]
with open(sys.argv[2]) as fh:
    d = json.load(fh)
if not d.get("successful"):
    print("POLL-ERROR: %s" % (d.get("error") or "unknown"), file=sys.stderr)
    sys.exit(1)
msgs = (d.get("data") or {}).get("messages") or []
seen = set()
try:
    with open(state_path) as f:
        seen = {line.strip() for line in f if line.strip()}
except FileNotFoundError:
    pass
fresh = []
for m in msgs:
    mid = m.get("messageId") or m.get("id") or ""
    if not mid or mid in seen:
        continue
    subj = (m.get("subject") or "(no subject)").strip()
    sender = (m.get("sender") or m.get("from") or "?").strip()
    ts = (m.get("messageTimestamp") or "").strip()
    fresh.append((mid, subj[:120], sender[:80], ts))
with open(state_path, "a") as f:
    for mid, *_ in fresh:
        f.write(mid + "\n")
for mid, subj, sender, ts in fresh:
    print("• %s — %s (%s)" % (subj, sender, ts))
PYDIFF
)" || { echo "$(date -Is) poll failed (parse)" >&2; exit 1; }

# First run seeds silently; later runs nudge only when something is new.
if [ "$FIRST_RUN" -eq 1 ]; then
  echo "$(date -Is) seeded silently ($(wc -l < "$STATE") ids)"
  exit 0
fi
[ -n "$NUDGE" ] || { echo "$(date -Is) quiet"; exit 0; }

PREFIX="📧 mail-watch"
[ "${MAILWATCH_TEST:-0}" = "1" ] && PREFIX="🧪 $PREFIX"
COUNT="$(printf '%s\n' "$NUDGE" | grep -c '^•' || true)"
BODY="$PREFIX — $COUNT new (query: $MAILWATCH_QUERY)
$NUDGE"

curl -s -m 30 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=$TELEGRAM_CHAT_ID" \
  --data-urlencode "text=$BODY" \
  --data-urlencode "disable_web_page_preview=true" > /dev/null \
  || { echo "$(date -Is) telegram send failed" >&2; exit 1; }
echo "$(date -Is) nudged: $COUNT new"
