# hermesCoder

**A 24/7 coding agent that lives on your own box and talks to you on Telegram —
powered by your Claude subscription, no API bill.**

hermesCoder turns a fresh Debian machine into a full autonomous-agent stack built
on [hermes-agent](https://github.com/NousResearch/hermes-agent). At its core is
the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk) — Anthropic's
official agent runtime, the same harness that powers Claude Code — wired into
hermes-agent as a first-class model provider under subscription OAuth with
fail-closed billing. The same Claude plan you already pay for drives your agent
around the clock. That provider is our work, submitted upstream as
[PR #65982](https://github.com/NousResearch/hermes-agent/pull/65982) — the PR
that makes this stack possible.

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

We wanted a **subscription-enabled Claude Agent SDK agent, hermes flavor**: the
Agent SDK's full harness (tools, sessions, permissions, skills) running under
the Claude plan we already pay for — no metered API bill for a bot that thinks
all day — wrapped in everything hermes-agent does well: the Telegram-native
gateway, plugins, cron, skills, multi-provider engine. Neither existed as one
thing, so we wired the SDK in as a hermes-agent provider ([PR #65982](https://github.com/NousResearch/hermes-agent/pull/65982)),
ran it 24/7 on our own boxes, and packaged the whole runbook as this repo.

## What you get

| Piece | What it does |
|---|---|
| `deploy/` | `box-bootstrap.sh` — one script from fresh Debian to running agent: system deps, engine, venv, identity, config, systemd user units. Fill `box.env` from the template, run, done. |
| `toolkit/` | delegate (agent writes code on a branch, you get a verdict), guard (golden-rules enforcement), merge (owner-granted merges only, `agent/*` branch namespace) |
| `zvec-memory/` | semantic recall over the agent's memories — zvec + [jina.ai](https://jina.ai) embeddings (`jina-embeddings-v4`, 2048-dim) when `JINA_API_KEY` is set; falls back to local bge-small with no key (works, weaker recall) |
| `watchers/` | pr-watch (pages you on PR changes), resource-watch (disk/mem/load for small boxes) — env-driven, fail-closed, state kept beside the script |
| `skills/` | agent-face (talking-head UI for your agent), coder-delegate, fleet-ssh, merge-grant |
| `deploy/templates/` | identity (SOUL/USER), config, systemd unit, settings — everything placeholder-templated; your agent's name, owner, and channels are yours |

## Quickstart

On a fresh Debian 12/13 box (Pi, mini PC, VPS — root):

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

The engine is pulled as a pinned shallow clone (`ENGINE_REF`, overridable in
`box.env`); a checkout that vendors `hermes/` locally is used as-is instead —
same script, two lanes.

## Engine provenance

The engine branch carries the `claude-agent-sdk` provider stack — the official
Agent SDK as a first-class hermes-agent runtime under subscription OAuth, with
fail-closed billing guards, session continuity, and background-task delivery.
It lives publicly on [the fork](https://github.com/fcavalcantirj/hermes-agent)
and is submitted upstream: PRs
[#65982](https://github.com/NousResearch/hermes-agent/pull/65982) (the provider),
[#65978](https://github.com/NousResearch/hermes-agent/pull/65978),
[#72001](https://github.com/NousResearch/hermes-agent/pull/72001),
[#74238](https://github.com/NousResearch/hermes-agent/pull/74238);
[#72002](https://github.com/NousResearch/hermes-agent/pull/72002) is already
merged upstream. Everything deployed here is public code at a pinned SHA.

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
your explicit grant.

## Security posture

Secrets never live in this repo — templates only, and `scripts/secrets-scan.sh`
(red-tested) guards every push. On the box, secrets sit in `~/.hermes/.env`
(mode 600), the gateway answers only your allowed Telegram ids, and merges to
your repos happen only on your explicit grant. See [SECURITY.md](SECURITY.md).

## License

MIT ([LICENSE](LICENSE)). The hermes-agent engine is MIT © 2025 Nous Research.
