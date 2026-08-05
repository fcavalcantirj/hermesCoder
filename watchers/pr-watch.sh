#!/usr/bin/env bash
# hermesCoder — upstream PR watcher. Modeled on service-watch.sh (2026-07-16).
# Deterministic, no LLM: every 30 min (cron) polls the GitHub API (12
# calls/run) for the PRs this box's stack rides on, and Telegram-nudges
# the owner when one changes (state, merge, comments, review comments, LABELS).
# First run seeds silently.
#
# Calls are AUTHED via the box's own `gh` login when available (5000/h; the
# unauth 60/h house-IP pool was getting exhausted by fleet traffic and runs
# were skipping quietly — blind windows). Falls back to unauth if gh is
# logged out. The token rides an Authorization header only, never argv/logs.
#
# LABELS are part of the change signature (2026-07-20: a maintainer triaged
# a label onto a watched PR and nobody was paged — label events don't touch
# comment counts). They ride the issue object we already fetch (0 extra
# calls), stored base64(sorted csv) so the colon-delimited state line stays
# parseable, and the alert names the exact +added/−removed labels.
#
# Comment counts EXCLUDE the owner's own ($SELF): their reply on a thread used
# to read as "activity" and page them about themselves (2026-07-18). Counting is
# capped at the first 100 comments of each kind per PR (no pagination) — fine
# for a nudger; a >100-comment thread saturates instead of alerting.
# ⚠ Any change to what the signature MEANS (new SELF, new fields — like this
# one) requires deleting the state file after deploy — the next run re-seeds
# silently; a stale-format line would false-fire on every PR.
#
# Watched: whatever PRS names, e.g. PRS="123 456 789"
#
# ISSUES (optional, 2026-07-28): space-separated ISSUE numbers watched the
# same way — state, comments (excl. $SELF), labels; no merge/push fields
# (issues have none), no bump reminder (the watched issues aren't ours to
# bump). Why: all four triage endorsements landed as comments on ISSUES
# (#25267/#26604/#71999/#72000) and only the owner's email inbox saw them —
# this watcher was PR-only by construction. State lines are keyed "i<N>" so
# an issue number can never collide with a PR number; existing PR lines are
# untouched, so adding ISSUES needs NO state wipe on deploy.
#
# BUMP REMINDER (2026-07-26): for PRs AUTHORED by $SELF that are open and
# unmerged, if $SELF made the last comment (ball in the maintainer's court)
# and PRWATCH_BUMP_DAYS days pass with no non-$SELF comment, a ⏰ page says
# a polite bump is in order. Measured from comment timestamps already
# fetched (0 extra calls; PR created_at floors the clock when no $SELF
# comment exists yet, so an ignored fresh PR pages too). Pushes don't reset
# the clock — the wait is about THEIR reaction, not our activity. Warned
# once per silence period (marker = last $SELF comment ts) in
# pr-watch.bump.state, a SEPARATE file: the change-signature state format is
# untouched, so no state wipe on deploy. PRWATCH_BUMP_DAYS=0 disables;
# default 4.
#
# Red-on-demand test:
#   PRWATCH_TEST=1 ./pr-watch.sh          # after doctoring a count in the state
#   file — the 🧪-prefixed message must arrive. Never run a red-test without
#   PRWATCH_TEST=1: the message would be indistinguishable from real activity.
#   (bump red-test: delete the PR's line from pr-watch.bump.state instead of
#   doctoring counts; delete it again after so the next real run re-pages.)
# Source of truth: watchers/pr-watch.sh (hermesCoder repo) — box-agnostic:
# state + log live beside the script, token from env or $HERMES_ENV_FILE.
# REQUIRED env (no personal defaults, fail-fast): PRWATCH_REPO (owner/repo),
#   PRS (space-separated PR numbers), PRWATCH_SELF (your github login,
#   excluded from activity counts), TELEGRAM_CHAT_ID.
#   Optional: PRWATCH_BUMP_DAYS (default 4; 0 = off); ISSUES (space-separated
#   issue numbers, "" = off).
# INSTALL: crontab: */30 * * * * PRWATCH_REPO=… PRS=… ISSUES=… PRWATCH_SELF=… \
#   TELEGRAM_CHAT_ID=… /path/to/pr-watch.sh >> /path/to/watch.log 2>&1
set -uo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin:$PATH

DIR="$(cd "$(dirname "$0")" && pwd)"
STATE="$DIR/pr-watch.state"
REPO="${PRWATCH_REPO:?PRWATCH_REPO required (owner/repo to watch)}"
PRS="${PRS:?PRS required (space-separated PR numbers)}"
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

# sig <pr> → "state:merged:mergeable:comments:review_comments:b64labels:headsha"
# HEAD SHA is part of the change signature (2026-07-26: two pushes landed on a
# watched PR and nobody was paged — pushes touched nothing else the signature
# reads. Unlike comments, pushes are NOT $SELF-filtered: the owner gets a page on
# any push, his own included — it doubles as delivery confirmation).
# (empty on API failure). The two counts are computed from the comment LISTS,
# not the issue/pull counters, so $SELF's own comments can be excluded. Labels
# come from the issue object (no extra call), base64(sorted csv) so the
# colon-delimited line survives any label name.
# mergeable: "clean" | "CONFLICT" | "?" — GitHub computes it lazily, so "?"
# (null, still computing) is CARRIED OVER from the previous state in the loop
# below rather than compared: a transient "?" must never fire an alert, and
# the next poll resolves it. A clean→CONFLICT flip is the page that ends
# "having to ask whether the stack drifted" (2026-07-23).
sig() {
  local n="$1" issue pull icomments rcomments
  issue=$(curl -sS "${GHAUTH[@]}" --max-time 20 "https://api.github.com/repos/$REPO/issues/$n") || return 1
  pull=$(curl -sS "${GHAUTH[@]}" --max-time 20 "https://api.github.com/repos/$REPO/pulls/$n") || return 1
  # mergeable is computed lazily: the first GET often returns null and merely
  # TRIGGERS the compute. One short retry resolves it most of the time; a
  # still-null lands as "?" and the carry logic below keeps it silent.
  if printf '%s' "$pull" | grep -q '"mergeable": *null'; then
    sleep 4
    pull=$(curl -sS "${GHAUTH[@]}" --max-time 20 "https://api.github.com/repos/$REPO/pulls/$n") || return 1
  fi
  icomments=$(curl -sS "${GHAUTH[@]}" --max-time 20 "https://api.github.com/repos/$REPO/issues/$n/comments?per_page=100") || return 1
  rcomments=$(curl -sS "${GHAUTH[@]}" --max-time 20 "https://api.github.com/repos/$REPO/pulls/$n/comments?per_page=100") || return 1
  ISSUE_JSON="$issue" PULL_JSON="$pull" IC_JSON="$icomments" RC_JSON="$rcomments" \
    PRWATCH_SELF="$SELF" python3 - <<'PY'
import base64, json, os, sys
try:
    issue = json.loads(os.environ["ISSUE_JSON"]); pull = json.loads(os.environ["PULL_JSON"])
    ic = json.loads(os.environ["IC_JSON"]); rc = json.loads(os.environ["RC_JSON"])
    self_login = os.environ["PRWATCH_SELF"]
    # rate-limited / error bodies are dicts with "message", not the real shapes
    if "state" not in issue or "merged" not in pull \
            or not isinstance(ic, list) or not isinstance(rc, list):
        sys.exit(1)
    nc = sum(1 for c in ic if (c.get("user") or {}).get("login") != self_login)
    nrc = sum(1 for c in rc if (c.get("user") or {}).get("login") != self_login)
    mg = pull.get("mergeable")
    mergeable = "clean" if mg is True else ("CONFLICT" if mg is False else "?")
    labels = ",".join(sorted((l.get("name") or "") for l in (issue.get("labels") or [])))
    lb = base64.b64encode(labels.encode()).decode() or "-"
    head = (pull.get("head") or {}).get("sha") or "-"
    sig = f'{issue["state"]}:{str(pull["merged"]).lower()}:{mergeable}:{nc}:{nrc}:{lb}:{head[:9]}'
    # Bump-reminder inputs (space-separated AFTER the sig; the state file and
    # signature comparison keep taking the first token only): epoch of the
    # last $SELF comment (PR created_at floors it for a $SELF-authored PR so
    # a never-answered fresh PR still ages), epoch of the last non-$SELF
    # comment, and whether $SELF authored the PR.
    from datetime import datetime, timezone
    def ep(s):
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    all_c = ic + rc
    self_ts = max((ep(c["created_at"]) for c in all_c
                   if (c.get("user") or {}).get("login") == self_login), default=0)
    other_ts = max((ep(c["created_at"]) for c in all_c
                    if (c.get("user") or {}).get("login") != self_login), default=0)
    auth_self = 1 if (pull.get("user") or {}).get("login") == self_login else 0
    if auth_self and pull.get("created_at"):
        self_ts = max(self_ts, ep(pull["created_at"]))
    print(f'{sig} {self_ts} {other_ts} {auth_self}')
except Exception:
    sys.exit(1)
PY
}

# isig <issue> → "state:comments:b64labels" — the issue-only subset of sig():
# no pull/merge/mergeable/head fields (issues have none), same $SELF-filtered
# comment count, same base64 label encoding. 2 API calls per issue.
isig() {
  local n="$1" issue icomments
  issue=$(curl -sS "${GHAUTH[@]}" --max-time 20 "https://api.github.com/repos/$REPO/issues/$n") || return 1
  icomments=$(curl -sS "${GHAUTH[@]}" --max-time 20 "https://api.github.com/repos/$REPO/issues/$n/comments?per_page=100") || return 1
  ISSUE_JSON="$issue" IC_JSON="$icomments" PRWATCH_SELF="$SELF" python3 - <<'PY'
import base64, json, os, sys
try:
    issue = json.loads(os.environ["ISSUE_JSON"])
    ic = json.loads(os.environ["IC_JSON"])
    self_login = os.environ["PRWATCH_SELF"]
    # rate-limited / error bodies are dicts with "message", not the real shapes
    if "state" not in issue or not isinstance(ic, list):
        sys.exit(1)
    nc = sum(1 for c in ic if (c.get("user") or {}).get("login") != self_login)
    labels = ",".join(sorted((l.get("name") or "") for l in (issue.get("labels") or [])))
    lb = base64.b64encode(labels.encode()).decode() or "-"
    print(f'{issue["state"]}:{nc}:{lb}')
except Exception:
    sys.exit(1)
PY
}

# label_delta <old_b64> <new_b64> → "+added +… −removed −…" (for the alert)
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
BUMPSTATE="$DIR/pr-watch.bump.state"
BUMP_DAYS="${PRWATCH_BUMP_DAYS:-4}"
touch "$BUMPSTATE"
CHANGES=()
BUMPS=()
SEEDED=0
for n in $PRS; do
  SIGDATA=$(sig "$n") || { echo "pr $n: API failure — skipped (state kept)"; continue; }
  read -r NEW SELFTS OTHERTS AUTHSELF <<< "$SIGDATA"
  OLD=$(sed -n "s/^$n //p" "$STATE" | head -1)
  # "?" (GitHub still computing mergeability) carries the previous value so it
  # can neither fire an alert nor overwrite a known clean/CONFLICT in state.
  if [ -n "$OLD" ]; then
    IFS=: read -r _os _om omg _rest <<< "$OLD"
    IFS=: read -r f1 f2 nmg f4 f5 f6 f7 <<< "$NEW"
    if [ "$nmg" = "?" ] && [ -n "${omg:-}" ] && [ "$omg" != "?" ]; then
      NEW="$f1:$f2:$omg:$f4:$f5:$f6:$f7"
    fi
  fi
  if [ -z "$OLD" ]; then
    SEEDED=1
  elif [ "$OLD" != "$NEW" ]; then
    IFS=: read -r os om omg oc orc olb ohd <<< "$OLD"
    IFS=: read -r ns nm nmg nc nrc nlb nhd <<< "$NEW"
    DETAIL=""
    [ "$os" != "$ns" ] && DETAIL+="state $os→$ns; "
    [ "$om" != "$nm" ] && DETAIL+="MERGED; "
    # ?→X is a lazy-compute RESOLUTION, not a real flip — update state
    # silently; only concrete↔concrete transitions page.
    if [ "${omg:-}" != "${nmg:-}" ] && [ "${omg:-?}" != "?" ]; then
      if [ "$nmg" = "CONFLICT" ]; then
        DETAIL+="⚠ went CONFLICTING (rebase needed); "
      else
        DETAIL+="mergeable ${omg}→${nmg:-?}; "
      fi
    fi
    [ "$oc" != "$nc" ] && DETAIL+="comments $oc→$nc; "
    [ "$orc" != "$nrc" ] && DETAIL+="review comments $orc→$nrc; "
    if [ "${olb:-}" != "${nlb:-}" ]; then
      LDIFF="$(label_delta "${olb:-}" "${nlb:-}")"
      [ -n "$LDIFF" ] && DETAIL+="labels: $LDIFF; "
    fi
    # "-" or empty (pre-headsha state line / API oddity) never pages — only a
    # concrete sha→sha flip is a push.
    if [ -n "${ohd:-}" ] && [ "${ohd:-}" != "-" ] && [ -n "${nhd:-}" ] \
        && [ "${nhd:-}" != "-" ] && [ "$ohd" != "$nhd" ]; then
      DETAIL+="pushed $ohd→$nhd; "
    fi
    CHANGES+=("PR #$n — ${DETAIL%; }
https://github.com/$REPO/pull/$n")
  fi
  grep -v "^$n " "$STATE" > "$STATE.tmp" 2>/dev/null || true
  echo "$n $NEW" >> "$STATE.tmp"
  mv "$STATE.tmp" "$STATE"

  # Bump reminder: our PR, still open+unmerged, we spoke last, and the
  # silence crossed the threshold. Warned once per silence period (marker =
  # last $SELF comment epoch); a new $SELF comment restarts the clock, a
  # maintainer reply clears the condition.
  if [ "${AUTHSELF:-0}" = "1" ] && [ "$BUMP_DAYS" != "0" ] \
      && [[ "$NEW" == open:false:* ]] \
      && [ "${SELFTS:-0}" -gt "${OTHERTS:-0}" ]; then
    NOWEP=$(date +%s)
    if [ $((NOWEP - SELFTS)) -ge $((BUMP_DAYS * 86400)) ]; then
      WARNED=$(sed -n "s/^$n //p" "$BUMPSTATE" | head -1)
      if [ "$WARNED" != "$SELFTS" ]; then
        DAYS=$(( (NOWEP - SELFTS) / 86400 ))
        BUMPS+=("PR #$n — ${DAYS}d of maintainer silence since your last move (threshold ${BUMP_DAYS}d). Polite bump is in order.
https://github.com/$REPO/pull/$n")
        grep -v "^$n " "$BUMPSTATE" > "$BUMPSTATE.tmp" 2>/dev/null || true
        echo "$n $SELFTS" >> "$BUMPSTATE.tmp"
        mv "$BUMPSTATE.tmp" "$BUMPSTATE"
      fi
    fi
  fi
done

for n in ${ISSUES:-}; do
  NEW=$(isig "$n") || { echo "issue $n: API failure — skipped (state kept)"; continue; }
  OLD=$(sed -n "s/^i$n //p" "$STATE" | head -1)
  if [ -z "$OLD" ]; then
    SEEDED=1
  elif [ "$OLD" != "$NEW" ]; then
    IFS=: read -r os oc olb <<< "$OLD"
    IFS=: read -r ns nc nlb <<< "$NEW"
    DETAIL=""
    [ "$os" != "$ns" ] && DETAIL+="state $os→$ns; "
    [ "$oc" != "$nc" ] && DETAIL+="comments $oc→$nc; "
    if [ "${olb:-}" != "${nlb:-}" ]; then
      LDIFF="$(label_delta "${olb:-}" "${nlb:-}")"
      [ -n "$LDIFF" ] && DETAIL+="labels: $LDIFF; "
    fi
    CHANGES+=("issue #$n — ${DETAIL%; }
https://github.com/$REPO/issues/$n")
  fi
  grep -v "^i$n " "$STATE" > "$STATE.tmp" 2>/dev/null || true
  echo "i$n $NEW" >> "$STATE.tmp"
  mv "$STATE.tmp" "$STATE"
done

if [ "${#CHANGES[@]}" -gt 0 ]; then
  BODY=$(printf '%s\n\n' "${CHANGES[@]}")
  send "${TESTPFX}🔔 hermes upstream activity:

${BODY%$'\n\n'}" && echo "notified: ${#CHANGES[@]} PR(s) changed"
elif [ "$SEEDED" = "1" ]; then
  echo "seeded state (quiet)"
else
  echo "ok — no PR changes (quiet)"
fi

if [ "${#BUMPS[@]}" -gt 0 ]; then
  BODY=$(printf '%s\n\n' "${BUMPS[@]}")
  send "${TESTPFX}⏰ PR bump reminder:

${BODY%$'\n\n'}" && echo "bump-warned: ${#BUMPS[@]} PR(s) stale"
fi
