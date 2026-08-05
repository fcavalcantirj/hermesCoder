---
name: fleet-ssh
description: SSH access to the owner's machine fleet — use when the owner asks to check, diagnose, or fix something on another machine. Emergency ops lane for when the owner only has chat.
---

# Fleet SSH (emergency + ops lane)

You hold a dedicated key (`~/.ssh/id_ed25519_fleet`) with aliases in
`~/.ssh/config`. Usage: `ssh <alias> '<command>'`.

The fleet table is deployment-specific — the operator fills it in on the box
(edit this file after deploy; keep it in sync with `~/.ssh/config`):

| Alias | Machine | What it is |
|---|---|---|
| `self` | this box | your own machine — use for local installs (crontab etc.) when direct access is not allowlisted |
| `<alias>` | `<host>` | `<owner/purpose — mark high-care boxes explicitly>` |

## Standing rules (non-negotiable)

1. **Read-only bias.** Diagnose freely (status, logs, df, journalctl, ps).
   Any WRITE/restart/delete needs the owner's explicit ask for THAT action,
   in this conversation — never inferred, never "while I'm here".
2. **Protected datastores are written ONLY through their owning API** — never
   direct SQL or file edits on a box marked high-care. A WAL-mode SQLite file
   is never `cp`'d (silent data loss); use the SQLite backup API.
3. Never touch another agent's brain/state files (their home dirs, containers,
   `~/.hermes`) unless the owner names the exact file.
4. Report [REAL] evidence (command + output), not summaries of intent.
5. If a fix needs sudo/root you don't have — say so; do not work around.
