# GOLDEN RULES — hard rules for every coding task

These are the non-negotiables the agent works under. They are not style
preferences — the deterministic guard (`~/.hermescoder/golden_guard.py`) enforces
the machine-checkable ones with exit codes, and the merge tool re-runs the full
guard before any branch lands. Edit this file to make the rules yours; the agent
reads it before every coding task.

1. **Tests first, always.** New behavior arrives with tests; the suite stays
   green and total coverage stays at or above 80%. Zero test files on a change
   is an automatic RED — before any coverage math.
2. **No file grows past ~900 lines.** When a file approaches the ceiling, split
   it. Markdown is exempt; code is not.
3. **No fake persistence in production code.** In-memory stand-ins ("InMemory"
   anything) live in tests only. Production paths talk to the real store —
   "TODO: replace with DB later" never ships.
4. **Lint and vulnerability checks pass, fail-closed.** A missing linter or
   scanner binary is a RED, not a skip.
5. **Never a metered API key in the repo.** The agent runs on subscription
   OAuth; `ANTHROPIC_API_KEY`-style metered keys must not appear anywhere in
   tracked files. The guard greps for them on every run.
6. **Drafts land on `agent/*` branches; the owner merges.** The agent never
   merges its own work — merges happen only on the owner's explicit grant, and
   the guard runs again on the branch tip at merge time.
7. **Stay on budget.** Delegated runs respect their token budget; a run that
   blows past it is a RED even if the code is good.
8. **Rules evolve.** When a lesson is learned the hard way, it gets crystallized
   here — memory, skills, and golden rules grow together.

The reference guard ships Go-centric checks (`go test`, `golangci-lint`,
`govulncheck`); adapt the tooling to your stack, keep the rules.
