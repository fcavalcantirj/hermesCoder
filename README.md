# hermesCoder

**A 24/7 coding agent that lives on your own box and talks to you on Telegram —
powered by your Claude subscription, no API bill.**

hermesCoder turns a fresh Debian machine into a full autonomous-agent stack built
on [hermes-agent](https://github.com/NousResearch/hermes-agent), pinned to an
upstream release. Model access is your Claude Pro/Max subscription through the
official Claude Code CLI, wired in by Nous' official catalog plugin
[claude-subscription-directsdk](https://github.com/NousResearch/hermes-plugin-claude-subscription-directsdk):
Hermes keeps its own agent loop, tools, approvals and compaction, and every turn
runs on the plan you already pay for. No API key, no per-token bill.
(The earlier in-tree Agent SDK provider we submitted as
[PR #65982](https://github.com/NousResearch/hermes-agent/pull/65982) is retired
in favour of that plugin — same goal, now upstream-maintained.)

Around the engine: a Telegram-native gateway (your coder is one chat away,
wherever you are), semantic long-term memory, a delegate/guard/merge toolkit for
real work on your own repos, and watchdogs that page you when something needs
eyes.

**Runs anywhere.** A Hetzner VPS, the machine under your desk, a Raspberry Pi —
if it boots Debian, it can host your coder. The full stack runs smooth on small
hardware: a Raspberry Pi 5 carries it 24/7 in production, and mini PCs, Radxa
boards, old laptops, and 2-vCPU budget VPSes are all comfortable homes.

> Community project on top of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
> (MIT) — not an official Nous Research or Anthropic product.

## Why we built it

We wanted a **subscription-powered Claude coding agent, hermes flavor**: the
Claude plan we already pay for driving a bot that thinks all day — no metered API
bill — wrapped in everything hermes-agent does well: the Telegram-native gateway,
plugins, cron, skills, multi-provider engine. We first built that bridge
ourselves (PR #65982, in production on our boxes for months); upstream then
shipped the official plugin, which measured faster and 2–3× cheaper in tokens on
the same tasks, so this stack now runs on it. What remains ours is everything
around the engine, packaged as this runbook.

## What you get

| Piece | What it does |
|---|---|
| `deploy/` | `box-bootstrap.sh` — one script from fresh Debian to running agent: system deps, Claude Code CLI, the pinned hermes-agent release via upstream's own installer, the subscription plugin, identity, config, the systemd user unit. Fill `box.env` from the template, run, done. |
| `toolkit/` | delegate (agent writes code on a branch, you get a verdict), guard (golden-rules enforcement), merge (owner-granted merges only, `agent/*` branch namespace) |
| `zvec-memory/` | semantic recall over the agent's memories — zvec + [jina.ai](https://jina.ai) embeddings (`jina-embeddings-v4`, 2048-dim) when `JINA_API_KEY` is set; falls back to local bge-small with no key (works, weaker recall) |
| `watchers/` | pr-watch (pages you on PR changes), resource-watch (disk/mem/load for small boxes) — env-driven, fail-closed, state kept beside the script |
| `skills/` | agent-face (talking-head UI for your agent), coder-delegate, fleet-ssh, merge-grant |
| `deploy/templates/` | identity (SOUL/USER), config, golden rules, merge policy — everything placeholder-templated; your agent's name, owner, and channels are yours |

## Quickstart

> ### ⚡ Don't read docs — paste one prompt
>
> **The fastest way to get running: [the hermesCoder setup prompt](https://gist.github.com/fcavalcantirj/2475371cf893fe16c6ed767fd127e23d).**
> Paste it into Claude Code (or any capable coding agent) and it drives the
> whole thing for you: asks where to run (this machine, a VPS, a Pi), names
> your agent, walks you through the Telegram bot and Claude subscription
> tokens, sells you the free [jina.ai](https://jina.ai) memory upgrade, runs
> the install, and **verifies everything before claiming it's done**. Ten
> minutes, mostly spent thinking of a good name.

Prefer to drive it yourself? On a fresh Debian 12/13 box (Pi, mini PC, VPS — root):

```bash
git clone https://github.com/fcavalcantirj/hermesCoder.git
cd hermesCoder/deploy
cp templates/env.template /root/box.env && chmod 600 /root/box.env
# edit /root/box.env — bot token (@BotFather), your Telegram id,
# Claude OAuth token (`claude setup-token`), agent name
bash box-bootstrap.sh
```

The bootstrap installs everything, runs the engine's smoke suites on the box,
and starts the gateway as a systemd user unit. Message your bot on Telegram —
it's your coder now.

The engine is installed by upstream's own installer at a pinned release commit
(`ENGINE_COMMIT`, overridable in `box.env`); the subscription plugin comes from
the Hermes plugin catalog at the sha the catalog pins.

## Engine provenance

- **Engine:** [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
  release **v0.21.6** (commit `818c13be1dc4fd28987e1e881a9408224afd4535`), installed by
  `scripts/install.sh` at that commit (PM-managed: pinned `uv`, managed CPython 3.14,
  venv hash-verified against `uv.lock`). Nothing is forked or vendored.
- **Model lane:** the official catalog plugin
  [`claude-subscription-directsdk`](https://github.com/NousResearch/hermes-plugin-claude-subscription-directsdk)
  (tier official, v0.3.3 at `4bc79c78031d`) + the Claude Code CLI (`npm`, ≥ 2.1.293).
  The plugin spawns `claude` once per Hermes call and inherits whatever account it
  is logged into (`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`).
- **History:** the stack ran for months on our in-tree Agent SDK provider,
  [PR #65982](https://github.com/NousResearch/hermes-agent/pull/65982) (now draft),
  with satellites [#65978](https://github.com/NousResearch/hermes-agent/pull/65978),
  [#72001](https://github.com/NousResearch/hermes-agent/pull/72001),
  [#74238](https://github.com/NousResearch/hermes-agent/pull/74238);
  [#72002](https://github.com/NousResearch/hermes-agent/pull/72002) landed upstream.
  Upstream chose the plugin architecture (core seam #117451) and ships the
  official plugin; we measured it against ours and moved. Everything deployed
  here is public code at a pinned sha.
- **Update:** bump `ENGINE_COMMIT`/`ENGINE_REF` in `box.env` and re-run the
  bootstrap, or on the box `hermes update` (stable channel = final releases only).

## Golden rules of coding

The agent doesn't just write code — it works under **golden rules**: a short
file of hard, non-negotiable rules (tests-first with 80%+ coverage, ~900-line
file ceiling, no fake in-memory persistence in production paths, lint and
vulnerability checks fail-closed, never a metered API key in the repo). The
bootstrap installs a starter set from
[`deploy/templates/GOLDEN-RULES.template.md`](deploy/templates/GOLDEN-RULES.template.md)
— edit it, make the rules yours.

What makes them golden is enforcement, not prose: `toolkit/guard/golden_guard.py`
is a **deterministic, zero-LLM gate** — exit code is the verdict — that checks
every delegated run, and the merge tool re-runs the full guard on the branch tip
before anything lands. Drafts live on `agent/*` branches; merges happen only on
the deterministic guard (GREEN before and after the merge) — there is no grant phrase; a shipped change is pushed or lands through a PR with auto-merge, and you can keep any run as a draft with `--no-merge`.

## Security posture

Secrets never live in this repo — templates only, and `scripts/secrets-scan.sh`
(red-tested) guards every push. On the box, secrets sit in `~/.hermes/.env`
(mode 600), the gateway answers only your allowed Telegram ids, and merges to
your repos happen only when the golden guard is GREEN on the branch tip and on the merged result. See [SECURITY.md](SECURITY.md).

## License

MIT ([LICENSE](LICENSE)). The hermes-agent engine is MIT © 2025 Nous Research.
