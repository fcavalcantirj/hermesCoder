#!/usr/bin/env bash
# claude-code-eyes launch — awesome-list PR watcher. Sibling of pr-watch.sh
# (2026-07-21). Deterministic, no LLM: every 30 min (cron) polls the GitHub
# API for the awesome-list PRs the claude-code-eyes launch rides on, and
# Telegram-nudges the owner when one changes (state, MERGE, comments, review
# comments, LABELS). First run seeds silently.
#
# DIFFERENCE vs pr-watch.sh: these PRs live in FOUR DIFFERENT repos, so targets
# are "owner/repo#num" (not a single REPO + number list). Everything else — the
# signature, self-exclusion, label tracking, red-on-demand test — is identical.
#
# Calls are AUTHED via the box's own `gh` login when available (5000/h); falls
# back to unauth (60/h shared pool → expect blind windows) if gh is logged out.
# The token rides an Authorization header only, never argv/logs.
#
# Comment counts EXCLUDE the owner's own ($SELF): they authored these PRs, so
# their own replies to a maintainer must not page them about themselves.
# Counting is capped at the first 100 comments of each kind per PR (no
# pagination).
# ⚠ Any change to what the signature MEANS (new SELF / new fields) requires
# deleting the state file after deploy — the next run re-seeds silently; a
# stale-format line would false-fire on every PR.
#
# Watched: whatever CCE_PRS names, e.g. CCE_PRS="owner/repo#12 other/repo#34"
#
# Red-on-demand test:
#   PRWATCH_TEST=1 ./pr-watch-cce.sh      # after doctoring a count in the state
#   file — the 🧪-prefixed message must arrive. Never run a red-test without
#   PRWATCH_TEST=1: the message would be indistinguishable from real activity.
# Source of truth: watchers/pr-watch-cce.sh (hermesCoder repo) — box-agnostic:
# state + log live beside the script, token from env or $HERMES_ENV_FILE.
# INSTALL: crontab: */30 * * * * CCE_PRS=… PRWATCH_SELF=… TELEGRAM_CHAT_ID=… \
#   /path/to/pr-watch-cce.sh >> /path/to/watch.log 2>&1
set -uo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin:$PATH

DIR="$(cd "$(dirname "$0")" && pwd)"
STATE="$DIR/pr-watch-cce.state"
# REQUIRED env (no personal defaults, fail-fast):
PRS="${CCE_PRS:?CCE_PRS required (space-separated owner/repo#number list)}"
SELF="${PRWATCH_SELF:?PRWATCH_SELF required (your github login)}"
CHAT="${TELEGRAM_CHAT_ID:?TELEGRAM_CHAT_ID required}"
TOKEN="${TELEGRAM_BOT_TOKEN:-}"
if [ -z "$TOKEN" ]; then
  TOKEN="$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "${HERMES_ENV_FILE:-$HOME/.hermes/.env}" | head -1)"
fi
TOKEN="${TOKEN%\"}"; TOKEN="${TOKEN#\"}"
TESTPFX=""; [ "${PRWATCH_TEST:-}" = "1" ] && TESTPFX="🧪 red-test: "

# GitHub auth: reuse the box's gh login (5000/h) when present; unauth fallback.
GHTOK="$(gh auth token 2>/dev/null || true)"
GHAUTH=()
if [ -n "$GHTOK" ]; then
  GHAUTH=(-H "Authorization: Bearer $GHTOK")
  echo "github api: authed via gh"
else
  echo "github api: UNAUTH fallback (60/h shared pool — expect blind windows)"
fi

send() {
  [ -n "$TOKEN" ] || { echo "NO TELEGRAM TOKEN — cannot send"; return 1; }
  curl -sS --max-time 15 "https://api.telegram.org/bot$TOKEN/sendMessage" \
    --data-urlencode "chat_id=$CHAT" --data-urlencode "text=$1" >/dev/null
}

# sig <owner/repo#num> → "state:merged:comments:review_comments:b64labels"
# (empty on API failure). Counts come from the comment LISTS (not the issue/pull
# counters) so $SELF's own comments can be excluded. Labels come from the issue
# object (no extra call), base64(sorted csv) so the colon-delimited line
# survives any label name.
sig() {
  local t="$1" repo n issue pull icomments rcomments
  repo="${t%#*}"; n="${t##*#}"
  issue=$(curl -sS "${GHAUTH[@]}" --max-time 20 "https://api.github.com/repos/$repo/issues/$n") || return 1
  pull=$(curl -sS "${GHAUTH[@]}" --max-time 20 "https://api.github.com/repos/$repo/pulls/$n") || return 1
  icomments=$(curl -sS "${GHAUTH[@]}" --max-time 20 "https://api.github.com/repos/$repo/issues/$n/comments?per_page=100") || return 1
  rcomments=$(curl -sS "${GHAUTH[@]}" --max-time 20 "https://api.github.com/repos/$repo/pulls/$n/comments?per_page=100") || return 1
  ISSUE_JSON="$issue" PULL_JSON="$pull" IC_JSON="$icomments" RC_JSON="$rcomments" \
    PRWATCH_SELF="$SELF" python3 - <<'PY'
import base64, json, os, sys
try:
    issue = json.loads(os.environ["ISSUE_JSON"]); pull = json.loads(os.environ["PULL_JSON"])
    ic = json.loads(os.environ["IC_JSON"]); rc = json.loads(os.environ["RC_JSON"])
    self_login = os.environ["PRWATCH_SELF"]
    if "state" not in issue or "merged" not in pull \
            or not isinstance(ic, list) or not isinstance(rc, list):
        sys.exit(1)
    nc = sum(1 for c in ic if (c.get("user") or {}).get("login") != self_login)
    nrc = sum(1 for c in rc if (c.get("user") or {}).get("login") != self_login)
    labels = ",".join(sorted((l.get("name") or "") for l in (issue.get("labels") or [])))
    lb = base64.b64encode(labels.encode()).decode() or "-"
    print(f'{issue["state"]}:{str(pull["merged"]).lower()}:{nc}:{nrc}:{lb}')
except Exception:
    sys.exit(1)
PY
}

# label_delta <old_b64> <new_b64> → "+added; −removed" (for the alert)
label_delta() {
  OLDB="$1" NEWB="$2" python3 - <<'PY'
import base64, os
def dec(s):
    if not s or s == "-":
        return set()
    try:
        return set(filter(None, base64.b64decode(s).decode().split(",")))
    except Exception:
        return set()
old, new = dec(os.environ["OLDB"]), dec(os.environ["NEWB"])
parts = []
added, removed = sorted(new - old), sorted(old - new)
if added:
    parts.append(" ".join("+" + a for a in added))
if removed:
    parts.append(" ".join("−" + r for r in removed))
print("; ".join(parts))
PY
}

mkdir -p "$DIR"
touch "$STATE"
CHANGES=()
SEEDED=0
for t in $PRS; do
  repo="${t%#*}"; n="${t##*#}"
  NEW=$(sig "$t") || { echo "pr $t: API failure — skipped (state kept)"; continue; }
  OLD=$(awk -v k="$t" '$1==k{print $2; exit}' "$STATE")
  if [ -z "$OLD" ]; then
    SEEDED=1
  elif [ "$OLD" != "$NEW" ]; then
    IFS=: read -r os om oc orc olb <<< "$OLD"
    IFS=: read -r ns nm nc nrc nlb <<< "$NEW"
    DETAIL=""
    [ "$os" != "$ns" ] && DETAIL+="state $os→$ns; "
    [ "$om" != "$nm" ] && DETAIL+="MERGED; "
    [ "$oc" != "$nc" ] && DETAIL+="comments $oc→$nc; "
    [ "$orc" != "$nrc" ] && DETAIL+="review comments $orc→$nrc; "
    if [ "${olb:-}" != "${nlb:-}" ]; then
      LDIFF="$(label_delta "${olb:-}" "${nlb:-}")"
      [ -n "$LDIFF" ] && DETAIL+="labels: $LDIFF; "
    fi
    CHANGES+=("$repo #$n — ${DETAIL%; }
https://github.com/$repo/pull/$n")
  fi
  awk -v k="$t" '$1!=k' "$STATE" > "$STATE.tmp" 2>/dev/null || true
  echo "$t $NEW" >> "$STATE.tmp"
  mv "$STATE.tmp" "$STATE"
done

if [ "${#CHANGES[@]}" -gt 0 ]; then
  BODY=$(printf '%s\n\n' "${CHANGES[@]}")
  send "${TESTPFX}🔔 claude-code-eyes launch — PR activity:

${BODY%$'\n\n'}" && echo "notified: ${#CHANGES[@]} PR(s) changed"
elif [ "$SEEDED" = "1" ]; then
  echo "seeded state (quiet)"
else
  echo "ok — no PR changes (quiet)"
fi
