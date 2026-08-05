#!/usr/bin/env bash
set -uo pipefail
ENV_FILE="${1:-$HOME/.hermes/.env}"
TOKEN="$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "$ENV_FILE" | head -1)"
TOKEN="${TOKEN%\"}"; TOKEN="${TOKEN#\"}"
echo "token length: ${#TOKEN}"
curl -sS --max-time 12 "https://api.telegram.org/bot$TOKEN/getMe" \
  | grep -oE '"username":"[^"]*"|"ok":(true|false)'
