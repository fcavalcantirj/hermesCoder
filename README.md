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

## What you get

| Piece | What it does |
|---|---|
| `deploy/` | `box-bootstrap.sh` — one script from fresh Debian to running agent: system deps, engine, venv, identity, config, systemd user units. Fill `box.env` from the template, run, done. |
| `toolkit/` | delegate (agent writes code on a branch, you get a verdict), guard (golden-rules enforcement), merge (owner-granted merges only, `agent/*` branch namespace) |
| `zvec-memory/` | semantic recall over the agent's memories (zvec + embeddings; Jina quality lane or local-only fallback) |
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

## Security posture

Secrets never live in this repo — templates only, and `scripts/secrets-scan.sh`
(red-tested) guards every push. On the box, secrets sit in `~/.hermes/.env`
(mode 600), the gateway answers only your allowed Telegram ids, and merges to
your repos happen only on your explicit grant. See [SECURITY.md](SECURITY.md).

## License

MIT ([LICENSE](LICENSE)). The hermes-agent engine is MIT © 2025 Nous Research.
