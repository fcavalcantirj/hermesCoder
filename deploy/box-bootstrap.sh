#!/usr/bin/env bash
# box-bootstrap.sh — provision a fresh Debian (12/13) box into a full
# hermesCoder: upstream hermes-agent at a pinned release, the official
# Claude-subscription model-provider plugin, a Telegram gateway as a systemd
# user unit, zvec-memory semantic recall (MCP), and the delegate/guard/merge
# toolkit + skills. Mirrors a runbook proven on a Raspberry Pi 5 in production.
#
# Engine lane (since 2026-10): NousResearch/hermes-agent pinned by COMMIT
# (ENGINE_COMMIT, default = release tag v0.21.6) and installed by upstream's
# own installer at that commit (scripts/install.sh → PM: pinned uv, managed
# CPython 3.14, hash-verified venv, ~/.local/bin/hermes). No fork, no vendored
# engine. Model access = your Claude Pro/Max subscription through the official
# Claude Code CLI (npm) + the catalog plugin `claude-subscription-directsdk`;
# the only credential is CLAUDE_CODE_OAUTH_TOKEN in ~/.hermes/.env.
# Everything else (toolkit/, skills/, zvec-memory/, templates) installs from
# THIS repo checkout. The box needs internet for apt/npm/pip + the engine clone.
#
# Usage (as root on the fresh box):
#   1. cp templates/env.template /root/box.env && chmod 600 /root/box.env
#   2. edit /root/box.env (identity + secrets)
#   3. bash box-bootstrap.sh
#
# Idempotent-ish: safe to re-run after a failed step; identity/config/.env are
# overwritten from templates, the engine install and venvs are reused.
set -euo pipefail

BOX_ENV=/root/box.env
[ -f "$BOX_ENV" ] || { echo "FATAL: $BOX_ENV missing (copy templates/env.template)"; exit 1; }
[ "$(stat -c %a "$BOX_ENV")" = "600" ] || { echo "FATAL: $BOX_ENV must be mode 600"; exit 1; }
set -a; . "$BOX_ENV"; set +a

for v in BOX_USER BOX_NAME BOX_DESC AGENT_NAME BOT_HANDLE OWNER_NAME OWNER_ID \
         TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_USERS TELEGRAM_HOME_CHANNEL \
         CLAUDE_CODE_OAUTH_TOKEN; do
  [ -n "${!v:-}" ] || { echo "FATAL: $v is empty in $BOX_ENV"; exit 1; }
done
GO_VERSION="${GO_VERSION:-1.23.4}"
TZ="${TZ:-UTC}"

# Engine + model-access pins (override in box.env only on purpose).
ENGINE_REPO="${ENGINE_REPO:-https://github.com/NousResearch/hermes-agent}"
ENGINE_REF="${ENGINE_REF:-v0.21.6}"                  # label only (release tag)
ENGINE_COMMIT="${ENGINE_COMMIT:-818c13be1dc4fd28987e1e881a9408224afd4535}"  # the commit v0.21.6 points at
PLUGIN_NAME="${PLUGIN_NAME:-claude-subscription-directsdk}"   # Hermes plugin-catalog entry (pinned sha lives in the catalog)
CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION:-latest}"           # npm dist-tag or exact version
CLAUDE_CODE_MIN="${CLAUDE_CODE_MIN:-2.1.293}"                  # plugin README: Haiku 5.5 alias needs >= 2.1.293

HERE="$(cd "$(dirname "$0")" && pwd)"          # <repo>/deploy
ROOT="$(dirname "$HERE")"                       # <repo>
HOMEDIR="/home/$BOX_USER"

say() { echo; echo "== $* =="; }

# ── 1. system ────────────────────────────────────────────────────────
say "system packages"
timedatectl set-timezone "$TZ" 2>/dev/null || true
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# python3/venv/pip are for the zvec sidecar only — the engine brings its own
# interpreter. libatomic1 is required by the Node.js runtime PM stages;
# libxkbcommon0 by PM's optional cua-driver (fails verification without it).
apt-get install -y -qq git python3 python3-venv python3-pip curl ca-certificates \
  build-essential nodejs npm jq rsync ripgrep gh tmux unzip htop libatomic1 libxkbcommon0 >/dev/null

ARCH=$(dpkg --print-architecture)   # arm64 | amd64
if ! command -v /usr/local/go/bin/go >/dev/null 2>&1; then
  say "go $GO_VERSION ($ARCH)"
  curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${ARCH}.tar.gz" | tar -C /usr/local -xz
fi
ln -sf /usr/local/go/bin/go /usr/local/bin/go

# ── 2. Claude Code CLI (the model lane) ──────────────────────────────
# The plugin drives the official `claude` executable; it has no credentials of
# its own. System-wide npm install so the gateway unit and the owner's shell
# see the same binary. Version floor from the plugin README.
say "claude CLI @ $CLAUDE_CODE_VERSION (min $CLAUDE_CODE_MIN)"
npm install -g --silent "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"
CLAUDE_VER="$(claude --version 2>/dev/null | awk '{print $1}')"
[ "$(printf '%s\n%s\n' "$CLAUDE_CODE_MIN" "$CLAUDE_VER" | sort -V | head -1)" = "$CLAUDE_CODE_MIN" ] \
  || { echo "FATAL: claude $CLAUDE_VER < $CLAUDE_CODE_MIN"; exit 1; }
echo "claude $CLAUDE_VER at $(command -v claude)"

# ── 3. user ──────────────────────────────────────────────────────────
say "user $BOX_USER + linger"
id "$BOX_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$BOX_USER"
loginctl enable-linger "$BOX_USER"
# cd ~ first — runuser inherits this script's cwd (the root-owned repo
# checkout), git stats the cwd and dies, and the `|| true` swallowed it:
# the fresh user ended up with NO git identity (caught by the first
# fresh-container E2E, 2026-07-21).
runuser -u "$BOX_USER" -- bash -c \
  "cd ~ && git config --global user.name '$OWNER_NAME' && git config --global init.defaultBranch main" || true
BOX_UID=$(id -u "$BOX_USER")
RUNDIR="/run/user/$BOX_UID"
# user manager needs a moment after enable-linger on first boot
for _ in $(seq 1 10); do [ -d "$RUNDIR" ] && break; sleep 1; done

as_user() { runuser -u "$BOX_USER" -- bash -lc "cd ~ && $*"; }
as_user_sysd() { runuser -u "$BOX_USER" -- env "XDG_RUNTIME_DIR=$RUNDIR" \
  "DBUS_SESSION_BUS_ADDRESS=unix:path=$RUNDIR/bus" bash -lc "cd ~ && $*"; }
HERMES="$HOMEDIR/.local/bin/hermes"

# ── 4. hermes engine: upstream release, upstream installer ───────────
# scripts/install.sh at the pinned commit: clones ~/.hermes/hermes-agent,
# stages uv + CPython 3.14 + tools via PM, hash-verifies the venv against
# uv.lock, writes ~/.local/bin/hermes. --non-interactive skips the setup and
# gateway wizards (we render config + unit ourselves). Never touches an
# existing config.yaml/.env. Re-runs update the checkout in place.
say "hermes engine $ENGINE_REF ($ENGINE_COMMIT) via upstream installer"
as_user "curl -fsSL '${ENGINE_REPO%/}/raw/${ENGINE_COMMIT}/scripts/install.sh' -o ~/hermes-install.sh \
  || curl -fsSL 'https://raw.githubusercontent.com/NousResearch/hermes-agent/${ENGINE_COMMIT}/scripts/install.sh' -o ~/hermes-install.sh"
as_user "bash ~/hermes-install.sh --non-interactive --commit '$ENGINE_COMMIT' && rm -f ~/hermes-install.sh"
as_user "[ -x '$HERMES' ] && '$HERMES' --version"
as_user "cd ~/.hermes/hermes-agent && [ \"\$(git rev-parse HEAD)\" = '$ENGINE_COMMIT' ]" \
  || { echo "FATAL: engine checkout is not at $ENGINE_COMMIT"; exit 1; }

# Messaging SDKs are opt-in extras under PM ([all] excludes them). The gateway
# unit runs headless and cannot answer the lazy-install prompt, so install now.
say "engine extras: telegram"
as_user "'$HERMES' pm install --extra telegram 2>&1 | tail -2"

# ── 5. official subscription provider plugin ─────────────────────────
say "plugin $PLUGIN_NAME"
as_user "'$HERMES' plugins install '$PLUGIN_NAME' --yes-deps --enable --force 2>&1 | tail -4"
PLUGIN_DIR="$HOMEDIR/.hermes/plugins/claude-subscription-directsdk-experimental"
[ -f "$PLUGIN_DIR/plugin.yaml" ] || { echo "FATAL: plugin not installed at $PLUGIN_DIR"; exit 1; }
echo "plugin $(as_user "git -C '$PLUGIN_DIR' rev-parse --short HEAD" 2>/dev/null || echo '?') $(grep -E '^version:' "$PLUGIN_DIR/plugin.yaml")"

# ── 6. identity + config from templates ──────────────────────────────
say "identity + config"
render() {  # render <template> <dest>  (owner-substituted)
  sed -e "s|__HOME__|$HOMEDIR|g" -e "s|__AGENT_NAME__|$AGENT_NAME|g" \
      -e "s|__BOT_HANDLE__|$BOT_HANDLE|g" -e "s|__OWNER_NAME__|$OWNER_NAME|g" \
      -e "s|__OWNER_ID__|$OWNER_ID|g" -e "s|__BOX_NAME__|$BOX_NAME|g" \
      -e "s|__BOX_DESC__|$BOX_DESC|g" -e "s|__TZ__|$TZ|g" "$1" > "$2"
}
as_user "mkdir -p ~/.hermescoder ~/.hermes/memories ~/.hermes/skills ~/code"

# SOUL.md = identity slot #1 of Hermes' native system-prompt composer.
render "$HERE/templates/SOUL.template.md"  /tmp/SOUL.md
render "$HERE/templates/USER.template.md"  /tmp/USER.md
render "$HERE/templates/config.yaml"       /tmp/config.yaml
install -o "$BOX_USER" -g "$BOX_USER" -m 644 /tmp/SOUL.md "$HOMEDIR/.hermes/SOUL.md"
install -o "$BOX_USER" -g "$BOX_USER" -m 644 /tmp/SOUL.md "$HOMEDIR/.hermescoder/SOUL.md"
install -o "$BOX_USER" -g "$BOX_USER" -m 644 /tmp/USER.md "$HOMEDIR/.hermes/memories/USER.md"
install -o "$BOX_USER" -g "$BOX_USER" -m 644 /tmp/config.yaml "$HOMEDIR/.hermes/config.yaml"
rm -f /tmp/SOUL.md /tmp/USER.md /tmp/config.yaml
as_user "[ -f ~/.hermes/memories/MEMORY.md ] || echo '# Working memory (self-curated via the memory tool)' > ~/.hermes/memories/MEMORY.md"

say "secrets (~/.hermes/.env, mode 600)"
cat > /tmp/hermes.env <<EOF
CLAUDE_CODE_OAUTH_TOKEN=$CLAUDE_CODE_OAUTH_TOKEN
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_ALLOWED_USERS=$TELEGRAM_ALLOWED_USERS
TELEGRAM_HOME_CHANNEL=$TELEGRAM_HOME_CHANNEL
TELEGRAM_HOME_CHANNEL_NAME="${TELEGRAM_HOME_CHANNEL_NAME:-Owner DM}"
EOF
install -o "$BOX_USER" -g "$BOX_USER" -m 600 /tmp/hermes.env "$HOMEDIR/.hermes/.env"
rm -f /tmp/hermes.env

# The plugin inherits whatever `claude` can log in as. Prove the token works
# for the box user BEFORE the gateway starts (fail-closed: no login = no agent).
say "claude login check (as $BOX_USER, token from ~/.hermes/.env)"
as_user "set -a; . ~/.hermes/.env; set +a; claude auth status 2>/dev/null | grep -q '\"loggedIn\": true'" \
  || { echo "FATAL: claude auth status is not loggedIn for $BOX_USER — check CLAUDE_CODE_OAUTH_TOKEN"; exit 1; }
echo "claude: logged in"

# ── 7. toolkit + skills (from this repo) ─────────────────────────────
say "hermesCoder toolkit + skills"
install -o "$BOX_USER" -g "$BOX_USER" -m 644 "$ROOT/toolkit/delegate/delegate_coder.py" "$HOMEDIR/.hermescoder/delegate_coder.py"
install -o "$BOX_USER" -g "$BOX_USER" -m 644 "$ROOT/toolkit/guard/golden_guard.py"     "$HOMEDIR/.hermescoder/golden_guard.py"
install -o "$BOX_USER" -g "$BOX_USER" -m 644 "$ROOT/toolkit/merge/merge_branch.py"     "$HOMEDIR/.hermescoder/merge_branch.py"
install -o "$BOX_USER" -g "$BOX_USER" -m 755 "$ROOT/toolkit/install_skill.py"          "$HOMEDIR/.hermescoder/install_skill.py"
install -o "$BOX_USER" -g "$BOX_USER" -m 755 "$ROOT/toolkit/optimize_skill.py"         "$HOMEDIR/.hermescoder/optimize_skill.py"
install -o "$BOX_USER" -g "$BOX_USER" -m 644 "$HERE/templates/GOLDEN-RULES.template.md" "$HOMEDIR/.hermescoder/GOLDEN-RULES.md"
install -o "$BOX_USER" -g "$BOX_USER" -m 644 "$HERE/templates/merge-policy.json" "$HOMEDIR/.hermescoder/merge-policy.json"
if ls "$ROOT"/identity/agents/*.md >/dev/null 2>&1; then
  as_user "mkdir -p ~/.hermescoder/agents"
  install -o "$BOX_USER" -g "$BOX_USER" -m 644 "$ROOT"/identity/agents/*.md "$HOMEDIR/.hermescoder/agents/"
fi
# Skills: ~/.hermes/skills/ is Hermes' single source of truth (bundled skills
# are copied there by the installer; ours land beside them). ~/.hermescoder/
# keeps a pristine copy. rsync as ROOT + chown (repo checkout under /root is
# unreadable to the user).
for dest in "$HOMEDIR/.hermes/skills" "$HOMEDIR/.hermescoder/skills"; do
  rsync -a "$ROOT/skills/" "$dest/"
  chown -R "$BOX_USER:$BOX_USER" "$dest"
done

# ── 8. zvec-memory sidecar (from this repo) ──────────────────────────
# Registered as a Hermes MCP server in config.yaml (mcp_servers.zvec-memory).
say "zvec-memory sidecar"
rsync -a --exclude __pycache__ --exclude .pytest_cache --exclude venv \
  "$ROOT/zvec-memory/" "$HOMEDIR/zvec-memory/"
chown -R "$BOX_USER:$BOX_USER" "$HOMEDIR/zvec-memory"
as_user "cd ~/zvec-memory && { [ -d venv ] || python3 -m venv venv; } && \
  venv/bin/pip install -q zvec fastembed 'mcp<2' pytest"   # server targets the mcp 1.x API (2.x breaks it — container E2E 2026-10-08)
say "zvec test suite on this box"
as_user "set -o pipefail; cd ~/zvec-memory && venv/bin/python -m pytest tests/ -q 2>&1 | tail -1"
say "warm local embedder (downloads bge-small once)"
as_user "set -o pipefail; cd ~/zvec-memory && venv/bin/python -c 'import zvec_memory_core as c; print(\"warm:\", len(c.embed_local([\"warmup\"])[0]))' 2>&1 | tail -1"
as_user "mkdir -p ~/.hermes/zvec-memory"
if [ -n "${JINA_API_KEY:-}" ]; then
  printf '%s' "$JINA_API_KEY" > /tmp/jina.key
  install -o "$BOX_USER" -g "$BOX_USER" -m 600 /tmp/jina.key "$HOMEDIR/.hermes/zvec-memory/jina.key"
  rm -f /tmp/jina.key
  echo "jina key installed (quality lane ON)"
else
  echo "⚠⚠⚠ RECALL QUALITY WARNING ⚠⚠⚠"
  echo "  No JINA_API_KEY in box.env — semantic memory runs LOCAL-ONLY (bge-small)."
  echo "  Recall works but is noticeably weaker than the jina-embeddings-v4 lane."
  echo "  For best results add a key later: zvec-memory/README.md §4 (jina.key file)."
fi

# ── 9. engine smoke (deterministic, no model call) ───────────────────
say "engine smoke"
# rich truncates the Name column on narrow terminals ("claude-subscr…"): force a wide one.
as_user "COLUMNS=250 '$HERMES' plugins list 2>&1 | grep -q 'claude-subscription-directsdk-experimental.*enabled'" \
  || { echo "FATAL: plugin not listed as enabled by hermes plugins list"; exit 1; }
as_user "'$HERMES' doctor 2>&1 | grep -E 'python-telegram-bot|Python 3' | head -3"
as_user "'$HERMES' doctor 2>&1 | grep -q '✓ python-telegram-bot'" \
  || { echo "FATAL: python-telegram-bot missing (hermes pm install --extra telegram failed?)"; exit 1; }

# ── 10. optional extras (best-effort, never fatal) ───────────────────
say "go tools for the guard (best-effort, slow; SKIP_GO_TOOLS=1 skips)"
# cd ~ first — runuser inherits the script's cwd (the repo checkout, usually
# under /root and unreadable to the box user), and go's toolchain refuses an
# unreadable cwd (proven on run 2 of the stack-branch era)
if [ "${SKIP_GO_TOOLS:-0}" = "1" ]; then
  echo "skipped (SKIP_GO_TOOLS=1)"
else
  as_user "export PATH=\$PATH:/usr/local/go/bin:\$HOME/go/bin && \
    go install golang.org/x/tools/gopls@latest && \
    go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest && \
    go install golang.org/x/vuln/cmd/govulncheck@latest" \
    || echo "WARN: go tools incomplete — guard lint/vuln steps will fail until installed"
fi

# ── 11. gateway unit (upstream-generated) ────────────────────────────
# `hermes gateway install` writes the canonical unit for THIS install (install
# launcher, cgroup cleanup, exit codes 75/78) so `hermes update` keeps owning it.
# Real box: user scope (linger). Container with systemd as PID 1 (the fresh-
# container E2E): upstream refuses a user unit there (the home dir may be a
# host bind-mount → a second poller on the host), so install the isolated
# system service it recommends, run as the box user.
say "systemd unit via hermes gateway install"
in_container() { [ -f /.dockerenv ] || grep -qaE 'docker|containerd|libpod|lxc' /proc/1/cgroup 2>/dev/null; }
if in_container; then
  HERMES_HOME="$HOMEDIR/.hermes" "$HERMES" gateway install --system --run-as-user "$BOX_USER" \
    --force --start-now --start-on-login 2>&1 | tail -4
  sleep 8
  UNIT="$(systemctl list-units --all --plain --no-legend 'hermes-gateway*' | awk '{print $1}' | head -1)"
  STATE="$(systemctl is-active "$UNIT" || true)"
  echo "unit=$UNIT state=$STATE (system scope, container)"
  [ "$STATE" = active ] || { journalctl -u "$UNIT" --no-pager -n 40 2>/dev/null | grep -iE 'error|fatal|rejected|exit' | tail -5; echo "FATAL: gateway unit $UNIT is $STATE"; exit 1; }
else
  as_user_sysd "'$HERMES' gateway install --force --start-now --start-on-login 2>&1 | tail -4"
  sleep 8
  STATE="$(as_user_sysd 'systemctl --user is-active hermes-gateway' || true)"
  echo "unit=hermes-gateway state=$STATE (user scope)"
  [ "$STATE" = active ] || { as_user_sysd "journalctl --user -u hermes-gateway --no-pager -n 40 2>/dev/null | grep -iE 'error|fatal|rejected|exit' | tail -5"; echo "FATAL: gateway unit is $STATE"; exit 1; }
fi

# ── 12. report ───────────────────────────────────────────────────────
say "DONE — $AGENT_NAME on $BOX_NAME"
cat <<EOF
Engine:    hermes-agent $ENGINE_REF ($ENGINE_COMMIT) · plugin $PLUGIN_NAME · claude $CLAUDE_VER
Gateway:   $STATE
Owner:     $OWNER_NAME ($OWNER_ID)  allowlist: $TELEGRAM_ALLOWED_USERS
Bot:       @$BOT_HANDLE
Next (from the operator's machine):
  1. headless PONG as $BOX_USER:  ~/.local/bin/hermes chat -Q -q 'Reply with exactly: pong'
  2. owner sends /start to @$BOT_HANDLE, then a PONG message
  3. red-on-demand gates: zvec JSON-RPC probe + missing-DB + StoreBusy
     (zvec-memory/README.md §8 in this repo)
  4. create the nightly consolidation cron once the gateway answers:
     hermes cron create '0 3 * * *' '<consolidation prompt>' \\
       --name nightly-memory-consolidation --deliver telegram
Update later: bump ENGINE_COMMIT/ENGINE_REF in box.env and re-run, or on the box
  as $BOX_USER: hermes update   (stable channel = final releases only)
EOF
