#!/usr/bin/env python3
"""delegate_coder.py — hermesCoder's coding delegate (M4 v0).

One real coding task per invocation, per the kanban ownership contract: the brain
owns lifecycle/acceptance; this delegate is an input lane; its diff is untrusted
until the deterministic guard (and later the checker) says otherwise.

Mechanics:
  - sequential-only: a non-blocking flock on ~/.hermescoder/delegate.lock (exit 3
    if another run is live) — never two SDK sessions at once on this Pi
  - fresh SDK session per task (Ralph-style), cwd = target repo
  - GOLDEN RULES injected verbatim into the system prompt append
  - branch-only: work happens on agent/<slug>-<ts>; main must not move
  - JSONL transcript at ~/.hermescoder/runs/<ts>-<slug>.jsonl with a terminal
    verdict line even on crash
  - after the run: golden_guard.py; one bounded fix round on RED
  - stdout = the verdict JSON (the brain relays it, honest labels)

Subscription lane: no ANTHROPIC_API_KEY may be present (fail-closed, same rule as
the runtime). SDK over CLI: uses claude_agent_sdk.query(), never shells `claude`.
Lane (2026-10-09): the SDK drives the box's own Claude Code CLI (`cli_path` =
`claude` on PATH, the same binary the brain's subscription plugin uses, kept at
`latest` by the bootstrap; unset -> the SDK's bundled CLI) with an explicit model
and effort. Defaults are the owner's pick (Opus 5.5, effort high); override per run:
DELEGATE_MODEL, DELEGATE_EFFORT (low|medium|high|xhigh|max), DELEGATE_CLI.
Under a Hermes gateway turn (HERMES_AGENT / AI_AGENT=hermes-agent markers) a
foreground run is refused — the terminal cap (600 s) would kill it mid-run —
with the exact relaunch: a Hermes background process with notify + --foreground.
Any other interpreter re-execs into ~/.hermescoder/venv/bin/python.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import fcntl
import functools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("HERMESCODER_HOME", "~/.hermescoder")).expanduser()
# The Hermes terminal tool exports the ENGINE's site-packages as PYTHONPATH into every
# command; under our interpreter (and inside the Claude CLI's own shell children) that
# is pure pollution. Scrub once, at import — before the SDK spawns anything.
os.environ.pop("PYTHONPATH", None)
# The delegate's own interpreter (claude-agent-sdk + pytest live there). Launched by any
# other python (a stale habit: the engine venv, system python3), main() re-execs here.
VENV_PYTHON = HOME / "venv" / "bin" / "python"
# gopls (the LSP plugin's server) + the guard's gate binaries live in ~/go/bin;
# the gateway unit's PATH lacks it — extend once at import, same as the guard.
os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + str(Path.home() / "go" / "bin")
LOCK_PATH = HOME / "delegate.lock"
RUNS_DIR = HOME / "runs"
RULES_PATH = HOME / "GOLDEN-RULES.md"
GUARD_PATH = HOME / "golden_guard.py"
MAX_FIX_ROUNDS = 1
MAX_WALL_SECONDS = int(os.environ.get("DELEGATE_MAX_SECONDS", "1800"))  # hard kill

CONTRACT = """\
## Delegated coding task — operating contract (non-negotiable)

- You are hermesCoder's coding delegate working alone in this repository.
- Work ONLY on the current branch. NEVER checkout, merge, push or commit to main.
- TDD: failing test first, minimum code to green, then refactor. Commit in small
  steps on this branch with clear messages.
- Read SPEC.md first if it exists; the spec precedes the code (API-first).
- Long command output floods your context: redirect it to a file and `tail -20`
  the summary (e.g. `go test ./... > /tmp/t.out 2>&1; tail -20 /tmp/t.out`).
- LSP (gopls) is active: heed the diagnostics attached to your edits before
  declaring anything done, and use LSP references before refactoring a shared
  symbol — don't grep when the language server can answer precisely.
- The GOLDEN RULES below govern everything. If a rule blocks you, say so in your
  final message; never silently work around it.
- Finish with a short summary: what changed, what is verified (label [REAL]/[TEST]/
  [UNVERIFIED]), what is blocked.
"""


# ---------- small pure helpers (unit-tested) ----------

def slugify(task: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "task"


def branch_name(task: str, ts: str) -> str:
    return f"agent/{slugify(task)}-{ts}"


def build_prompt(task: str) -> str:
    return f"{CONTRACT}\n## The task\n\n{task}\n"


def build_options_fields(repo: Path, rules_text: str) -> dict:
    """ClaudeAgentOptions fields — plain dict so tests assert without the SDK.
    Mirrors the runtime's build_option_fields() shape (claude_agent_sdk_session.py)."""
    return {
        "cwd": str(repo),
        # Autonomous coding needs to run `go test`/`go get`/git non-interactively.
        # acceptEdits denies un-allowlisted Bash headless (same wall the briefing
        # hit). bypassPermissions is the intended mode for an unattended coder —
        # scoped safe here: cwd is one repo, work is branch-only, and the guard
        # asserts main never moved. It is NOT a chat session.
        "permission_mode": "bypassPermissions",
        "system_prompt": {
            "type": "preset",
            "preset": "claude_code",
            "append": rules_text,
        },
        # LSP = the plugin-backed language-server tool (gopls): navigation ops
        # need it allowlisted; diagnostics-on-edit ride Edit results regardless.
        "allowed_tools": ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "LSP"],
        # The SDK is hermetic by default (setting_sources=None → no user
        # settings, therefore NO plugins and no LSP). "user" loads
        # ~/.claude/settings.json + installed plugins (gopls-lsp); permissions
        # stay governed by permission_mode above.
        "setting_sources": ["user"],
        "mcp_servers": {},
        # Best lane, explicit (never the CLI's silent default): model + effort, and the
        # box's own up-to-date `claude` (None -> the SDK's bundled CLI). Env-overridable.
        "model": os.environ.get("DELEGATE_MODEL") or "claude-opus-5-5",
        "effort": os.environ.get("DELEGATE_EFFORT") or "high",
        "cli_path": os.environ.get("DELEGATE_CLI") or shutil.which("claude"),
    }


def usage_from_events(events: list[dict]) -> dict:
    """Usage totals for the run.

    Authoritative source: the SDK's ResultMessage (dataclass-shaped event with
    cumulative usage + num_turns). Fallback for raw-CLI-shaped events (per-message
    usage): message-id-deduped sums — one API message spans multiple JSONL lines.
    """
    for ev in reversed(events):
        if ev.get("_type") == "ResultMessage" and isinstance(ev.get("usage"), dict):
            u = ev["usage"]
            return {
                "api_calls": ev.get("num_turns", 0),
                "output_tokens": u.get("output_tokens", 0),
                "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
                "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
                "total_cost_usd": ev.get("total_cost_usd"),
                "duration_api_ms": ev.get("duration_api_ms"),
            }
    seen: dict[str, dict] = {}
    for ev in events:
        msg = ev.get("message") or {}
        u = msg.get("usage") or {}
        if u and msg.get("id"):
            seen[msg["id"]] = u
    return {
        "api_calls": len(seen),
        "output_tokens": sum(v.get("output_tokens", 0) for v in seen.values()),
        "cache_creation_input_tokens": sum(
            v.get("cache_creation_input_tokens", 0) for v in seen.values()
        ),
        "cache_read_input_tokens": sum(
            v.get("cache_read_input_tokens", 0) for v in seen.values()
        ),
    }


def restore_subscription_env() -> None:
    """Nested-session reality (the Claudius culture-TL;DR lesson, relearned live
    2026-07-15): Claude Code STRIPS CLAUDE_CODE_OAUTH_TOKEN from tool
    subprocesses, so a delegate spawned by the brain's Bash tool starts
    credential-less and dies in ~90ms with zero API calls. Restore the token
    deterministically from the mode-600 claude.env (single source on the box)
    and scrub the nesting markers so the child CLI starts clean."""
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        envfile = HOME / "claude.env"
        if envfile.is_file():
            for line in envfile.read_text().splitlines():
                if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                    val = line.split("=", 1)[1].strip().strip('"')
                    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = val
                    break
    for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        os.environ.pop(var, None)


# ---------- git helpers ----------

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def rev(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", ref)


def create_branch(repo: Path, name: str) -> None:
    _git(repo, "checkout", "-q", "-b", name)


def current_branch(repo: Path) -> str:
    return _git(repo, "branch", "--show-current")


# ---------- SDK run (isolated seam; mocked in tests) ----------

def _event_to_dict(message) -> dict:
    if dataclasses.is_dataclass(message):
        d = dataclasses.asdict(message)
    else:
        d = {"repr": repr(message)}
    d["_type"] = type(message).__name__
    return d


async def _run_sdk_async(prompt: str, fields: dict, jsonl_path: Path) -> list[dict]:
    from claude_agent_sdk import ClaudeAgentOptions, query

    events: list[dict] = []
    with jsonl_path.open("a") as fh:
        async for message in query(prompt=prompt, options=ClaudeAgentOptions(**fields)):
            ev = _event_to_dict(message)
            events.append(ev)
            fh.write(json.dumps(ev, default=str) + "\n")
            fh.flush()
    return events


def run_sdk(prompt: str, fields: dict, jsonl_path: Path) -> list[dict]:
    async def _bounded():
        # Hard wall-clock kill (E2B-style lifetime cap): a hung/looping session
        # must not run unbounded on the subscription.
        return await asyncio.wait_for(
            _run_sdk_async(prompt, fields, jsonl_path), timeout=MAX_WALL_SECONDS
        )
    return asyncio.run(_bounded())


# ---------- guard ----------

def run_guard(repo: Path, jsonl: Path, budget: int | None, base: str | None = None) -> dict:
    cmd = [sys.executable, str(GUARD_PATH), "--repo", str(repo), "--jsonl", str(jsonl)]
    if budget is not None:
        cmd += ["--budget-tokens", str(budget)]
    if base:
        cmd += ["--base", base]  # gate the delta of this run, not the repo's legacy debt
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"verdict": "ERROR", "reason": f"guard output unparsable: {proc.stdout[-500:]}"}


def write_evidence(repo: Path, guard: dict) -> None:
    """Evidence pack for the read-only checker (SWE-Review: reviewers with
    structured evidence beat diff-only): guard report + diff vs main, as files
    the checker can Read/Grep. Best-effort — evidence must never fail a run."""
    try:
        ev = repo / ".hermesCoder"
        ev.mkdir(exist_ok=True)
        (ev / "guard-report.json").write_text(json.dumps(guard, indent=2) + "\n")
        diff = subprocess.run(["git", "diff", "main...HEAD"], cwd=repo,
                              capture_output=True, text=True, timeout=60).stdout
        (ev / "diff-vs-main.patch").write_text(diff)
    except Exception:  # noqa: BLE001
        pass


# ---------- fire-time merge grant (W1) ----------

def _default_merge_runner(message: str, repo: Path, branch: str, jsonl: Path,
                          ship: bool = True) -> dict:
    """Auto-land channel. merge_branch.py is deployed beside us in HOME; library call
    (not subprocess) because we already hold the flock its CLI takes. No gate: the
    merge tool re-runs the guard pre+post and ships (push, or PR + auto-merge) unless
    ship=False. The optional message is audit/annotation only."""
    sys.path.insert(0, str(HOME))
    import merge_branch  # noqa: PLC0415 — lazy: plain runs never need it
    return merge_branch.run_merge(message, repo, branch, jsonl=jsonl, ship=ship)


# ---------- orchestration ----------

def run_task(
    task: str,
    repo: Path,
    budget: int | None = None,
    sdk_runner=run_sdk,
    guard_runner=run_guard,
    grant_message: str | None = None,
    merge_runner=None,
    auto_merge: bool = True,
    ship: bool = True,
) -> dict:
    ts = time.strftime("%Y%m%d-%H%M%S")
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    jsonl = RUNS_DIR / f"{ts}-{slugify(task)}.jsonl"
    verdict: dict = {
        "verdict": "FAIL",
        "reason": "crashed before completion",
        "task": task,
        "repo": str(repo),
        "jsonl": str(jsonl),
    }
    t0 = time.time()
    try:
        rules_text = RULES_PATH.read_text()
        main_before = rev(repo, "main")
        base_sha = rev(repo, "HEAD")  # the branch starts here: the guard gates the delta from it
        branch = branch_name(task, ts)
        create_branch(repo, branch)
        verdict.update({"branch": branch, "main_sha_before": main_before, "base_sha": base_sha})
        if guard_runner is run_guard:
            guard_runner = functools.partial(run_guard, base=base_sha)

        fields = build_options_fields(repo, rules_text + "\n\n" + CONTRACT)
        events = sdk_runner(build_prompt(task), fields, jsonl)
        usage = usage_from_events(events)

        guard = guard_runner(repo, jsonl, budget)
        write_evidence(repo, guard)
        fix_rounds = 0
        while guard.get("verdict") == "RED" and fix_rounds < MAX_FIX_ROUNDS:
            fix_rounds += 1
            fix_prompt = (
                f"{CONTRACT}\n## Fix round {fix_rounds}\n\n"
                "The deterministic golden-rules guard is RED on this branch. "
                "Fix ONLY what the report below names, keeping all rules:\n\n"
                f"```json\n{json.dumps(guard, indent=2)}\n```\n"
            )
            events = sdk_runner(fix_prompt, fields, jsonl)
            u2 = usage_from_events(events)
            for k in usage:
                usage[k] += u2.get(k, 0)
            guard = guard_runner(repo, jsonl, budget)

        main_after = rev(repo, "main")
        main_moved = main_after != main_before
        on_branch = current_branch(repo) == branch
        ok = guard.get("verdict") == "GREEN" and not main_moved and on_branch
        verdict.update({
            "verdict": "PASS" if ok else "FAIL",
            "reason": ("guard GREEN, branch-only respected" if ok else
                       "main moved" if main_moved else
                       f"left branch (on '{current_branch(repo)}')" if not on_branch else
                       "guard RED after fix round"),
            "guard": guard,
            "fix_rounds": fix_rounds,
            "main_moved": main_moved,
            "usage": usage,
        })
        # Auto-land on PASS (grant-phrase ceremony removed 2026-08-18, all repos).
        # The merge tool still re-runs the full guard at merge time — a merge
        # failure never eats the task verdict. `--no-merge` keeps a draft on the
        # branch for the rare case you want to inspect before it lands.
        if auto_merge and verdict["verdict"] == "PASS":
            runner = merge_runner or _default_merge_runner
            try:
                verdict["merge"] = runner(grant_message or "", repo, branch, jsonl, ship=ship)
            except Exception as exc:  # noqa: BLE001
                verdict["merge"] = {"verdict": "FAIL",
                                    "reason": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:  # noqa: BLE001 — verdict line must always exist
        verdict.update({"verdict": "FAIL", "reason": f"{type(exc).__name__}: {exc}"})
    finally:
        verdict["duration_s"] = round(time.time() - t0, 1)
        with jsonl.open("a") as fh:
            fh.write(json.dumps({"_type": "DelegateVerdict", **verdict}, default=str) + "\n")
    return verdict


def reexec_target() -> str | None:
    """The venv interpreter to re-exec into, or None when already there (or when the
    venv does not exist, e.g. a fresh box before step 7, or DELEGATE_NO_REEXEC=1)."""
    if os.environ.get("DELEGATE_NO_REEXEC"):
        return None
    if not VENV_PYTHON.exists():
        return None
    try:
        if Path(sys.executable).resolve() == VENV_PYTHON.resolve():
            return None
    except OSError:
        return None
    return str(VENV_PYTHON)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--budget-tokens", type=int)
    ap.add_argument("--message", "--grant-message", dest="grant_message", default="",
                    help="optional owner message (audit/merge-commit annotation only; "
                         "there is no grant gate)")
    ap.add_argument("--no-merge", action="store_true",
                    help="keep the draft on its agent/* branch instead of "
                         "auto-landing on PASS (default is auto-land + ship)")
    ap.add_argument("--no-ship", action="store_true",
                    help="auto-land locally but do not push / open a PR")
    ap.add_argument("--background", action="store_true",
                    help="daemonize: detach, log to RUNS_DIR, print the log path "
                         "and exit immediately (the verdict JSON is the log's "
                         "last DelegateVerdict line)")
    ap.add_argument("--foreground", action="store_true",
                    help="override the bounded-turn guard and run synchronously "
                         "anyway (you are accepting the caller's turn timeout)")
    args = ap.parse_args()

    # Own interpreter first: the SDK and pytest live in ~/.hermescoder/venv. Launched
    # by anything else (the retired engine venv, system python3), re-exec there.
    target = reexec_target()
    if target is not None:
        os.execv(target, [target, *sys.argv])

    # Bounded-turn guard (2026-08-08, Hermes markers added 2026-10-09): a delegate run
    # takes tens of minutes; a foreground run inside a gateway turn dies at the 600 s
    # terminal cap with no verdict (observed twice: solvr fix 2026-08-08, Jr tick fix
    # 2026-10-09). Refuse with the exact relaunch instead of dying silently.
    hermes = bool(os.environ.get("HERMES_AGENT")) or os.environ.get("AI_AGENT") == "hermes-agent"
    claude_code = os.environ.get("CLAUDECODE") == "1" or bool(os.environ.get("CLAUDE_CODE_ENTRYPOINT"))
    if (hermes or claude_code) and not args.background and not args.foreground:
        if hermes:
            relaunch = ("terminal(command=" + shlex.join([sys.executable, *sys.argv, "--foreground"])
                        + ", background=true, notify_on_complete=true)")
            then = ("Hermes notifies you when the process exits; its output is the verdict JSON. "
                    "Do not poll, do not finish the task by hand.")
        else:
            relaunch = shlex.join([sys.executable, *sys.argv, "--background"])
            then = ("end your turn; the verdict lands in the printed log — read it on a LATER "
                    "turn (tail the log, look for the final DelegateVerdict line)")
        print(json.dumps({
            "verdict": "REFUSED_FOREGROUND",
            "reason": "delegate runs outlive the 600s turn budget — a foreground run inside a "
                      "turn always dies at the cap with no verdict",
            "relaunch": relaunch,
            "then": then,
        }, indent=2))
        return 3

    # Fail-closed subscription rule — same contract as the runtime.
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(json.dumps({"verdict": "FAIL",
                          "reason": "ANTHROPIC_API_KEY present — refusing (subscription lane only)"}))
        return 2
    restore_subscription_env()
    # Positive lane assert: the credential must BE a setup-token (sk-ant-oat01-),
    # not merely "not a metered key" (cabinlab/litellm-claude-code pattern).
    oat = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not oat:
        print(json.dumps({"verdict": "FAIL",
                          "reason": "no CLAUDE_CODE_OAUTH_TOKEN (env stripped and claude.env missing) — cannot start"}))
        return 2
    if oat and not oat.startswith("sk-ant-oat01-"):
        print(json.dumps({"verdict": "FAIL",
                          "reason": "CLAUDE_CODE_OAUTH_TOKEN is not a sk-ant-oat01- setup-token — refusing"}))
        return 2

    repo = Path(args.repo).expanduser()
    if not (repo / ".git").exists():
        print(json.dumps({"verdict": "FAIL", "reason": f"not a git repo: {repo}"}))
        return 3

    HOME.mkdir(parents=True, exist_ok=True)
    lock_fh = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"verdict": "BUSY",
                          "reason": "another delegate run is in progress (sequential-only)"}))
        return 3

    if args.background:
        # Daemonize AFTER the cheap fail-fast checks (creds, repo, lock) so
        # those still report synchronously. The child inherits the flock'd
        # descriptor — the parent must exit via os._exit so its finally-less
        # return can't LOCK_UN the shared description out from under the child.
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        _ts = time.strftime("%Y%m%dT%H%M%S")
        daemon_log = RUNS_DIR / f"{_ts}-{slugify(args.task)}-daemon.log"
        pid = os.fork()
        if pid > 0:
            print(json.dumps({
                "verdict": "LAUNCHED",
                "pid": pid,
                "log": str(daemon_log),
                "note": "running detached; the final DelegateVerdict line of "
                        "the log is the verdict JSON — check on a later turn",
            }, indent=2))
            sys.stdout.flush()
            os._exit(0)
        os.setsid()
        _fd = os.open(daemon_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(_fd, 1)
        os.dup2(_fd, 2)
        os.close(_fd)
        _devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(_devnull, 0)
        os.close(_devnull)

    try:
        verdict = run_task(args.task, repo, args.budget_tokens,
                           grant_message=args.grant_message,
                           auto_merge=not args.no_merge, ship=not args.no_ship)
        print(json.dumps(verdict, indent=2, default=str))
        return 0 if verdict["verdict"] == "PASS" else 2
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


if __name__ == "__main__":
    sys.exit(main())
