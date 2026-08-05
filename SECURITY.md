# Security

hermesCoder provisions an autonomous coding agent that holds real credentials
(Telegram bot token, Claude OAuth token) on your box. Treat the box like
production.

## Reporting a vulnerability

Open a GitHub issue for non-sensitive reports. For anything exploitable
(credential leakage, sandbox escape, injection into the gateway), use GitHub's
private vulnerability reporting on this repository instead of a public issue.

## Baseline expectations the kit ships with

- Secrets live only in `~/.hermes/.env` (mode 600) on the box — never in this
  repo. `scripts/secrets-scan.sh` is red-tested; run it before any push.
- The Telegram gateway only answers `TELEGRAM_ALLOWED_USERS`.
- Engine installs are pinned (`ENGINE_REF`) — no floating `main`.
