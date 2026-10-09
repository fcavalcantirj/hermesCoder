---
name: coder-delegate
description: Delegate any real coding task (write/change/fix code in a repository) to the coding delegate, which lands and ships the result itself. Use whenever the owner asks for code work — never write project code directly in the chat session.
---

# Coder delegate — when and how

**When:** any real coding task — new endpoint, bug fix, refactor, tests. If it changes
a repository, it goes through the delegate. Never write project code in this chat
session; your chat context is for talking to the owner, not for coding.

**How — ALWAYS as a Hermes background process with a completion notice** (a delegate
run takes 5–30 minutes; a foreground terminal call dies at the 600 s cap with no
verdict, and the delegate refuses foreground runs under Hermes for exactly that reason):

```
terminal(
  command="~/.hermescoder/venv/bin/python ~/.hermescoder/delegate_coder.py --foreground --repo ~/code/<name> --task \"<one clear, self-contained task>\" --message \"<the owner's message VERBATIM>\"",
  background=true,
  notify_on_complete=true
)
```

Then END YOUR TURN. Hermes pings you when the process exits; its output is the verdict
JSON. Do not poll, do not sleep, do not run `delegate_coder.py` any other way (any
other python re-execs into the venv anyway).

- `--message` is optional: the owner's triggering message byte-for-byte (escape embedded
  `"` as `\"`). It is annotation only (audit log, merge commit) — there is NO grant
  gate, nothing is parsed from it.
- Default on PASS = **auto-land and ship**: the delegate merges the branch into the
  repo's default branch (guard GREEN before AND after, else rolled back) and pushes;
  when the remote refuses a direct push (rulesets / required checks) or the repo's
  overrides say `tests: ci`, it opens a PR with auto-merge and GitHub lands it when
  the checks pass. `--no-ship` lands locally only; `--no-merge` keeps a draft on its
  `agent/*` branch.
- Repos live under `~/code/`; the practice repo is `~/code/<your-practice-repo>`.
- One task per run, sequential-only. A `BUSY` verdict means another run is live: say
  so and wait; never kill it.

**Relaying the verdict (non-negotiable):**

- Relay the verdict JSON honestly: verdict, branch, guard result, fix rounds, token
  usage, and the `merge` block (MERGED / SHIPPED via direct|pr with `pr_url` /
  PR_OPEN / REFUSED / FAIL) unedited. Label claims [REAL]/[TEST]/[UNVERIFIED] — the
  guard's GREEN is [TEST] evidence, not proof the feature is right.
- On FAIL: report exactly what failed (guard check, branch violation, crash) and
  discuss with the owner. NEVER silently retry, never "fix" it yourself in chat.
- **If a run dies without a verdict (killed, crashed, lost), RE-RUN the delegate.**
  Never finish the delegate's work by hand: your chat session is not the coder.
- You never touch main yourself; the tool does the merge and the push.
