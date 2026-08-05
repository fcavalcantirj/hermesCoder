#!/usr/bin/env bash
# Resource watch (disk / memory / load) for small boxes.
# Each box reports through its OWN bot: the token in $DIR/.env decides the bot
# One instance per box, one Telegram destination each.
# Modeled byte-for-logic on coder-thermal/pi-thermal-watch.sh (edge-triggered).
#
# Runs every 5 min (cron, no sudo). Edge-triggered: ONE alert on OK->ALERT, a
# ✅ recovery note on ALERT->OK, re-alert only if still ALERT after REALERT_MIN.
# State in resource-watch.state next to this script. Never repeats every 5 min.
# State v2: "ALERT <last_send_epoch> <alerting_since_epoch>" — the third field
# feeds the "still degraded 2h35" header; a v1 two-field file still parses.
#
# Alerts are multi-line Telegram HTML (bold header + emoji bars). If the HTML
# send is rejected, the same text is re-sent plain (tags stripped) — an alert
# is never lost to formatting.
#
# Location-independent: DIR is derived from the script's own path, so the same
# file works under any /home/<user> — nothing is user-specific.
#
# Telegram token + chat id: read from $DIR/.env (TELEGRAM_BOT_TOKEN=... and
# TELEGRAM_CHAT_ID=...), or the same-named env vars. Self-contained — never
# reads another agent's ~/.hermes/.env or brain files.
#
# Thresholds (override via env), percentages of used capacity:
#   DISK_WARN=70  DISK_CRIT=85      root filesystem (/) usage
#   MEM_WARN=85   MEM_CRIT=95       (MemTotal-MemAvailable)/MemTotal
#   LOAD_WARN=2.0 LOAD_CRIT=4.0     1-min loadavg / nproc  (per-core ratio)
#   REALERT_MIN=30                  re-alert cadence while still ALERT
# Swap, Pi temp/fan/watts and top-mem lines are CONTEXT ONLY — never triggers.
#
# TEST=1  -> force a one-off 🧪 test alert (real readings) and exit WITHOUT
#           touching state, so normal runs stay quiet afterward.
#
# INSTALL (cron, no sudo):
#   */5 * * * * /home/<user>/resource-watch/resource-watch.sh >> /home/<user>/resource-watch/watch.log 2>&1
set -uo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin:$PATH   # cron has a minimal PATH

DIR="$(cd "$(dirname "$0")" && pwd)"
STATE="$DIR/resource-watch.state"
CHAT="${TELEGRAM_CHAT_ID:-}"   # or TELEGRAM_CHAT_ID= in $DIR/.env (checked below)
DISK_WARN="${DISK_WARN:-70}"; DISK_CRIT="${DISK_CRIT:-85}"
MEM_WARN="${MEM_WARN:-85}";   MEM_CRIT="${MEM_CRIT:-95}"
LOAD_WARN="${LOAD_WARN:-2.0}"; LOAD_CRIT="${LOAD_CRIT:-4.0}"
REALERT_MIN="${REALERT_MIN:-30}"

# token + chat id: env var wins, else $DIR/.env; chat id has NO baked default —
# fail fast with a clear message in the cron log.
TOKEN="${TELEGRAM_BOT_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -f "$DIR/.env" ]; then
  TOKEN="$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "$DIR/.env" | head -1)"
fi
TOKEN="${TOKEN%\"}"; TOKEN="${TOKEN#\"}"          # tolerate optional quotes
if [ -z "$CHAT" ] && [ -f "$DIR/.env" ]; then
  CHAT="$(sed -n 's/^TELEGRAM_CHAT_ID=//p' "$DIR/.env" | head -1)"
fi
[ -n "$CHAT" ] || { echo "FATAL: TELEGRAM_CHAT_ID required (env or $DIR/.env)"; exit 2; }

HOST="$(hostname)"

# --- presentation helpers -------------------------------------------------------
esc() { local s=${1//&/&amp;}; s=${s//</&lt;}; s=${s//>/&gt;}; printf '%s' "$s"; }

# bar <pct> <warn_pct> <crit_pct> — 10-segment emoji bar, fill color by severity
bar() {
  local pct=${1%%.*} warn=${2%%.*} crit=${3%%.*} fill="🟩" out="" i n
  pct=${pct:-0}
  n=$(( (pct + 5) / 10 )); [ "$n" -gt 10 ] && n=10; [ "$n" -lt 0 ] && n=0
  if [ "$pct" -ge "$crit" ]; then fill="🟥"; elif [ "$pct" -ge "$warn" ]; then fill="🟨"; fi
  for ((i=0; i<n; i++));  do out+="$fill"; done
  for ((i=n; i<10; i++)); do out+="⬜"; done
  printf '%s' "$out"
}

fmt_up() {  # compact uptime: 5d 9h / 3h 12m / 45m
  local s d h m; s=$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 0)
  d=$((s/86400)); h=$((s%86400/3600)); m=$((s%3600/60))
  if [ "$d" -gt 0 ]; then printf '%dd %dh' "$d" "$h"
  elif [ "$h" -gt 0 ]; then printf '%dh %dm' "$h" "$m"
  else printf '%dm' "$m"; fi
}

fmt_dur() {  # seconds -> "3d 2h" / "2h35" / "45m"
  local s=$1 d h m; d=$((s/86400)); h=$((s%86400/3600)); m=$((s%3600/60))
  if [ "$d" -gt 0 ]; then printf '%dd %dh' "$d" "$h"
  elif [ "$h" -gt 0 ]; then printf '%dh%02d' "$h" "$m"
  else printf '%dm' "$m"; fi
}

# Total board power (W) from the Pi 5 PMIC — only meaningful where vcgencmd exists.
pmic_watts() {
  vcgencmd pmic_read_adc 2>/dev/null | awk '
    { name=$1; eq=index($2,"="); val=substr($2,eq+1); sub(/[AV]$/,"",val)
      if (name ~ /_A$/) { k=name; sub(/_A$/,"",k); amp[k]=val+0 }
      else if (name ~ /_V$/) { k=name; sub(/_V$/,"",k); volt[k]=val+0 } }
    END { p=0; for (k in amp) if (k in volt) p+=amp[k]*volt[k]; printf "%.1f", p }'
}

# --- read sensors --------------------------------------------------------------
# disk: root filesystem usage %
DISK_PCT=$(df -P / | awk 'NR==2{gsub("%","",$5); print $5+0}')
DISK_H=$(df -h / | awk 'NR==2{print $3"/"$2}')

# memory: used% = (MemTotal - MemAvailable) / MemTotal
read -r MEM_TOTAL_KB MEM_AVAIL_KB <<<"$(awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{print t" "a}' /proc/meminfo)"
MEM_TOTAL_KB="${MEM_TOTAL_KB:-0}"; MEM_AVAIL_KB="${MEM_AVAIL_KB:-0}"
MEM_PCT=$(awk "BEGIN{ if ($MEM_TOTAL_KB>0) printf \"%d\", (($MEM_TOTAL_KB-$MEM_AVAIL_KB)*100/$MEM_TOTAL_KB); else print 0 }")
MEM_USED_MB=$(( (MEM_TOTAL_KB - MEM_AVAIL_KB) / 1024 ))
MEM_TOTAL_MB=$(( MEM_TOTAL_KB / 1024 ))

# swap (context only, never a trigger); line omitted when the box has no swap
read -r SWAP_TOTAL_KB SWAP_FREE_KB <<<"$(awk '/^SwapTotal:/{t=$2} /^SwapFree:/{f=$2} END{print t" "f}' /proc/meminfo)"
SWAP_TOTAL_KB="${SWAP_TOTAL_KB:-0}"; SWAP_FREE_KB="${SWAP_FREE_KB:-0}"
SWAP_TXT=""
if [ "$SWAP_TOTAL_KB" -gt 0 ]; then
  SWAP_USED_MB=$(( (SWAP_TOTAL_KB - SWAP_FREE_KB) / 1024 ))
  SWAP_TOTAL_MB=$(( SWAP_TOTAL_KB / 1024 ))
  SWAP_PCT=$(( (SWAP_TOTAL_KB - SWAP_FREE_KB) * 100 / SWAP_TOTAL_KB ))
  if [ "$SWAP_PCT" -ge 95 ]; then SWAP_TXT="swap FULL ${SWAP_USED_MB}/${SWAP_TOTAL_MB} MB"
  else SWAP_TXT="swap ${SWAP_USED_MB}/${SWAP_TOTAL_MB} MB"; fi
fi

# load: 1-min avg normalized by cores
LOAD1=$(cut -d' ' -f1 /proc/loadavg); LOAD1="${LOAD1:-0}"
NPROC=$(nproc 2>/dev/null || echo 1); [ "$NPROC" -ge 1 ] 2>/dev/null || NPROC=1
LOAD_RATIO=$(awk "BEGIN{printf \"%.2f\", $LOAD1/$NPROC}")

# Pi-only context line (temp / fan / watts) — silently absent on x86 boxes
PI_LINE=""
if command -v vcgencmd >/dev/null 2>&1; then
  TEMP=$(vcgencmd measure_temp 2>/dev/null | grep -oE '[0-9.]+' | head -1)
  FAN_CUR=""; FAN_MAX=""; FAN_RPM=""
  for d in /sys/class/thermal/cooling_device*; do
    [ "$(cat "$d/type" 2>/dev/null)" = "pwm-fan" ] || continue
    FAN_CUR=$(cat "$d/cur_state" 2>/dev/null || echo "")
    FAN_MAX=$(cat "$d/max_state" 2>/dev/null || echo "")
    break
  done
  for f in /sys/class/hwmon/hwmon*/fan1_input; do [ -e "$f" ] && { FAN_RPM=$(cat "$f" 2>/dev/null || echo ""); break; }; done
  WATTS="$(pmic_watts)"
  [ -n "$TEMP" ] && PI_LINE="🌡 ${TEMP}°C"
  if [ -n "$FAN_MAX" ] && [ "$FAN_MAX" -gt 0 ] 2>/dev/null; then
    PI_LINE="${PI_LINE:+$PI_LINE · }🌀 fan ${FAN_CUR}/${FAN_MAX}${FAN_RPM:+ (${FAN_RPM} rpm)}"
  fi
  if [ -n "$WATTS" ] && [ "$WATTS" != "0.0" ]; then
    PI_LINE="${PI_LINE:+$PI_LINE · }⚡ ${WATTS} W"
  fi
fi

# top memory consumers, aggregated by command name (context only); MB below 1G
TOP_MEM=$(ps -eo comm=,rss= 2>/dev/null | awk '
  NF>=2 { rss=$NF+0; c=$0
          sub(/[ \t]+[0-9]+[ \t]*$/,"",c); sub(/^[ \t]+/,"",c)
          a[c]+=rss; n[c]++ }
  END   { for (c in a) printf "%d\t%d\t%s\n", a[c], n[c], c }' \
  | sort -rn | head -2 | awk -F'\t' '
  { x=($2>1) ? " ×"$2 : ""; sep=(NR>1 ? " · " : "")
    if ($1 >= 1048576) printf "%s%s%s ≈ %.1fG", sep, $3, x, $1/1048576
    else               printf "%s%s%s ≈ %dM",   sep, $3, x, $1/1024 }')
TOP_MEM="$(esc "$TOP_MEM")"

# one-line snapshot for the cron LOG (Telegram gets the multi-line body below)
SNAP="disk ${DISK_PCT}% (${DISK_H}) · mem ${MEM_PCT}% (${MEM_USED_MB}/${MEM_TOTAL_MB}MB) · load ${LOAD1} (${LOAD_RATIO}x ${NPROC}c) · $(uptime -p 2>/dev/null || echo up)"

# --- build the Telegram body (HTML, multi-line, emoji bars) ---------------------
BAR_MEM=$(bar "$MEM_PCT" "$MEM_WARN" "$MEM_CRIT")
BAR_DISK=$(bar "$DISK_PCT" "$DISK_WARN" "$DISK_CRIT")
LOAD_PCT=$(awk "BEGIN{printf \"%d\", ($LOAD_RATIO/$LOAD_CRIT)*100}")
LOAD_WARN_PCT=$(awk "BEGIN{printf \"%d\", ($LOAD_WARN/$LOAD_CRIT)*100}")
BAR_LOAD=$(bar "$LOAD_PCT" "$LOAD_WARN_PCT" 100)

# one block per metric (bar line + └ detail line), blank lines between blocks —
# Telegram strips leading spaces, so hierarchy comes from └, never indentation
BODY="🧠 Mem ${BAR_MEM} ${MEM_PCT}%
└ ${MEM_USED_MB}/${MEM_TOTAL_MB} MB${SWAP_TXT:+ · ${SWAP_TXT}}

💾 Disk ${BAR_DISK} ${DISK_PCT}%
└ ${DISK_H} used

📊 Load ${BAR_LOAD} ${LOAD_RATIO}× per core
└ 1-min ${LOAD1} · ${NPROC} cores"
CTX=""
[ -n "$PI_LINE" ] && CTX+="${PI_LINE}"$'\n'
CTX+="⏱ up $(fmt_up)"
[ -n "$TOP_MEM" ] && CTX+=$'\n'"🔎 top: ${TOP_MEM}"
BODY+=$'\n\n'"${CTX}"

HOSTE="$(esc "$HOST")"

send() {  # send <html-text> — parse_mode=HTML; plain fallback ONLY on a definitive 400 reject
  [ -n "$TOKEN" ] || { echo "NO TELEGRAM TOKEN — cannot send"; return 1; }
  local resp plain
  resp=$(curl -sS --max-time 15 "https://api.telegram.org/bot$TOKEN/sendMessage" \
    --data-urlencode "chat_id=$CHAT" --data-urlencode "parse_mode=HTML" \
    --data-urlencode "text=$1" 2>&1) || true
  case "$resp" in *'"ok":true'*|*'"ok": true'*) return 0 ;; esac
  case "$resp" in
    *'"error_code":400'*|*'"error_code": 400'*)
      # our HTML was definitively rejected — a plain resend cannot duplicate
      echo "HTML rejected (${resp:0:160}) — retrying plain"
      plain=$(printf '%s' "$1" | sed -e 's/<[^>]*>//g' -e 's/&lt;/</g' -e 's/&gt;/>/g' -e 's/&amp;/\&/g')
      resp=$(curl -sS --max-time 15 "https://api.telegram.org/bot$TOKEN/sendMessage" \
        --data-urlencode "chat_id=$CHAT" --data-urlencode "text=$plain" 2>&1) || true
      case "$resp" in *'"ok":true'*|*'"ok": true'*) return 0 ;; esac
      echo "plain send failed too (${resp:0:160})"; return 1 ;;
    *)
      # transport error / 429 / 5xx — resending now risks a duplicate; the
      # edge-triggered state machine retries on the next cron run instead
      echo "send failed (${resp:0:160})"; return 1 ;;
  esac
}

# --- TEST mode: force one alert, do NOT touch state ----------------------------
if [ "${TEST:-0}" = "1" ]; then
  send "🧪 <b>${HOSTE} — resource-watch TEST</b>

${BODY}

<i>thresholds: disk ${DISK_WARN}/${DISK_CRIT}% · mem ${MEM_WARN}/${MEM_CRIT}% · load ${LOAD_WARN}/${LOAD_CRIT}×</i>" \
    && echo "test alert sent ($HOST)" || echo "test alert FAILED ($HOST)"
  exit 0
fi

# --- decide OK vs ALERT --------------------------------------------------------
# Severity per metric: 0 ok / 1 warn / 2 CRITICAL. Thresholds unchanged.
REASONS=(); HEADS=(); WORST=0
if   [ "$DISK_PCT" -ge "$DISK_CRIT" ]; then REASONS+=("disk ${DISK_PCT}% CRITICAL (≥${DISK_CRIT}%)"); HEADS+=("2 DISK ${DISK_PCT}% CRITICAL")
elif [ "$DISK_PCT" -ge "$DISK_WARN" ]; then REASONS+=("disk ${DISK_PCT}% warn (≥${DISK_WARN}%)");     HEADS+=("1 DISK ${DISK_PCT}% warn"); fi

if   [ "$MEM_PCT" -ge "$MEM_CRIT" ]; then REASONS+=("mem ${MEM_PCT}% CRITICAL (≥${MEM_CRIT}%)"); HEADS+=("2 MEM ${MEM_PCT}% CRITICAL")
elif [ "$MEM_PCT" -ge "$MEM_WARN" ]; then REASONS+=("mem ${MEM_PCT}% warn (≥${MEM_WARN}%)");     HEADS+=("1 MEM ${MEM_PCT}% warn"); fi

if   awk "BEGIN{exit !($LOAD_RATIO >= $LOAD_CRIT)}"; then REASONS+=("load ${LOAD1} (${LOAD_RATIO}x cores) CRITICAL (≥${LOAD_CRIT}x)"); HEADS+=("2 LOAD ${LOAD_RATIO}× per core CRITICAL")
elif awk "BEGIN{exit !($LOAD_RATIO >= $LOAD_WARN)}"; then REASONS+=("load ${LOAD1} (${LOAD_RATIO}x cores) warn (≥${LOAD_WARN}x)");     HEADS+=("1 LOAD ${LOAD_RATIO}× per core warn"); fi

HEAD=""
if [ "${#HEADS[@]}" -gt 0 ]; then
  for h in "${HEADS[@]}"; do
    sev=${h%% *}
    [ "$sev" -gt "$WORST" ] && { WORST=$sev; HEAD=${h#* }; }
  done
  [ "${#HEADS[@]}" -gt 1 ] && HEAD="${HEAD} · +$(( ${#HEADS[@]} - 1 )) more"
fi

NOW=$(date +%s)
# SINCE (3rd field) = when this alert episode began. A v1 two-field state file
# cannot tell us that (v1 rewrote its epoch on every re-send), so SINCE stays
# EMPTY and the header omits the duration rather than fabricating one.
read -r LASTSTATE LASTEPOCH SINCE <<< "$(cat "$STATE" 2>/dev/null || echo 'OK 0')"
LASTEPOCH="${LASTEPOCH:-0}"; SINCE="${SINCE:-}"

if [ "${#REASONS[@]}" -gt 0 ]; then
  JOINED=$(printf '%s; ' "${REASONS[@]}"); JOINED="${JOINED%; }"
  if [ "$LASTSTATE" != "ALERT" ]; then
    send "🚨 <b>${HOSTE} — ${HEAD}</b>

${BODY}" && { echo "ALERT $NOW $NOW" > "$STATE"; echo "alert sent: ${JOINED}"; }
  elif [ $((NOW - LASTEPOCH)) -ge $((REALERT_MIN * 60)) ]; then
    DURTXT=""; [ -n "$SINCE" ] && DURTXT=" $(fmt_dur $((NOW - SINCE)))"
    send "⏳ <b>${HOSTE} — still degraded${DURTXT} · ${HEAD}</b>

${BODY}" && { echo "ALERT $NOW${SINCE:+ $SINCE}" > "$STATE"; echo "re-alert sent: ${JOINED}"; }
  else
    echo "ALERT $LASTEPOCH${SINCE:+ $SINCE}" > "$STATE"; echo "still alert — quiet (dedup)"
  fi
else
  if [ "$LASTSTATE" = "ALERT" ]; then
    if send "✅ <b>${HOSTE} — back to normal</b>

${BODY}"; then
      echo "OK $NOW" > "$STATE"; echo "recovery sent"
    else
      # keep ALERT so the recovery notice is retried next run, not lost
      echo "ALERT $LASTEPOCH${SINCE:+ $SINCE}" > "$STATE"; echo "recovery send FAILED — retrying next run"
    fi
  else
    echo "ok — ${SNAP} (quiet)"
    echo "OK $NOW" > "$STATE"
  fi
fi
