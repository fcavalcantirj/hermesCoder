#!/usr/bin/env bash
set -uo pipefail
cd "$HOME/resource-watch" || exit 3
chmod +x resource-watch.sh
chmod 600 .env 2>/dev/null || true
echo "== $(hostname) / $(whoami) =="
echo -n "SYNTAX: "; bash -n resource-watch.sh && echo OK || { echo FAIL; exit 4; }
# idempotent cron: preserve every other line, manage only our own
LINE="*/5 * * * * $HOME/resource-watch/resource-watch.sh >> $HOME/resource-watch/watch.log 2>&1"
( crontab -l 2>/dev/null | grep -vF 'resource-watch/resource-watch.sh'; echo "$LINE" ) | crontab -
echo "CRON:"; crontab -l 2>/dev/null | grep -F 'resource-watch/resource-watch.sh'
echo "NORMAL PASS:"; ./resource-watch.sh
echo -n "STATE: "; cat resource-watch.state; echo
echo "TEST FIRE @ $(date '+%F %T %Z'):"; TEST=1 ./resource-watch.sh
echo -n "STATE AFTER TEST (must be OK ...): "; cat resource-watch.state; echo
