---
name: merge-ship
description: Land and ship an existing branch (any branch, any repo under ~/code) through the deterministic merge tool — guard GREEN before and after, then push or PR + auto-merge. Use when the owner asks to merge, land or ship a branch that already exists (usually a delegate branch that was kept as a draft or whose ship step did not finish).
---

# Merge + ship — post-hoc channel

**Policy (the owner, global, standing):** there is no merge gate. No grant phrase, no
policy map, any branch, any repo. What can still stop a merge is only the oracle and
the mechanics: the golden guard must be GREEN on the branch tip and on the merged
result (RED rolls back), the branch must exist and not be merged yet, the tree must be
clean for a local merge.

**How — as a Hermes background process with a completion notice** (the PR lane waits
for GitHub's checks; a foreground call can hit the 600 s cap):

```
terminal(
  command="~/.hermescoder/venv/bin/python ~/.hermescoder/merge_branch.py --repo ~/code/<name> --branch <branch> --message \"<the owner's message VERBATIM>\"",
  background=true,
  notify_on_complete=true
)
```

- `--message` is optional annotation (audit + merge commit). Nothing is parsed from it.
- `--target <branch>` overrides the default target (the repo's default branch).
- `--no-ship` merges locally only. Default ships: `git push`, or — when the remote
  refuses (rulesets / required checks) or the repo's overrides say `tests: ci` — the
  branch is pushed, a PR is opened and `gh pr merge --auto` arms it; GitHub lands it when
  the required checks pass. Local main is fast-forwarded afterwards.
- If you are not CERTAIN which branch the owner means, ask — never guess.
- A `BUSY` verdict means a delegate or merge run is live: say so and wait; never kill it.

**Relaying (non-negotiable):** relay the tool's verdict JSON honestly — SHIPPED (with
`via` and `merge_sha`, plus `pr_url` on the PR lane), PR_OPEN (auto-merge armed; it
lands by itself — say so, do not poll), MERGED (local only), REFUSED (exact reason),
FAIL, BUSY. A REFUSED is a valid answer: report it; never work around it by merging or
pushing by hand. Every attempt is audited in `~/.hermescoder/merges.jsonl`.
