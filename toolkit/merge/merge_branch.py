#!/usr/bin/env python3
"""merge_branch.py — deterministic merge + ship tool (W1 v2, 2026-10-09).

the owner's GLOBAL policy (his own words, kept in the bot's USER.md since 2026-08-19,
toolkit aligned 2026-10-09): there is NO merge gate. No grant phrase, no policy map,
any branch, any repo under ~/code. The only things that stop a merge are the oracle
and the mechanics:

  - guard GREEN on the branch tip (temp worktree, never the live checkout) BEFORE,
    and guard GREEN on the merged result AFTER (RED hard-rolls the target back);
  - the branch and the target exist, the branch is not already merged, there is
    something to merge, the working tree is clean (local-merge lane only).

A GREEN merge SHIPS by default (`--no-ship` keeps it local):
  1. direct lane — `git push origin <target>`;
  2. PR lane (when the remote rejects a direct push, e.g. rulesets with required
     checks, or when the repo's overrides say `tests: "ci"` so the remote CI is the
     oracle) — the local merge is undone, the branch is pushed, a PR is opened
     against the target, `gh pr merge --auto --merge` arms GitHub to land it when
     the required checks pass, the tool polls up to SHIP_WAIT_SECONDS and then
     fast-forwards the local target. Verdicts: SHIPPED (via direct|pr), PR_OPEN
     (auto-merge armed, lands by itself), MERGED (local only), REFUSED, FAIL, BUSY.

Optional per-repo overrides live in ~/.hermescoder/merge-policy.json — it is NOT a
gate any more: a repo absent from it gets the defaults (target = the repo's default
branch, tests run locally). Keys: target, cov_path, python, tests ("ci" or a list of
pytest paths), cov (list), min_coverage.

`git` and `gh` are injectable callables (Jr's publish.py pattern) so the whole ship
sequence is unit-tested without a network. Every attempt — refusals too — appends to
~/.hermescoder/merges.jsonl with the owner's optional message verbatim.

Zero LLM. Exit code IS the verdict: 0 = MERGED/SHIPPED/PR_OPEN, 2 = REFUSED/FAIL,
3 = BUSY or usage error. A terminal verdict is printed even on crash.
"""

from __future__ import annotations

import argparse
import fcntl
import functools
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# The Hermes terminal tool exports the ENGINE's site-packages as PYTHONPATH into every
# command (python 3.14 wheels); under our interpreter that is pure pollution for the
# guard's pytest and for every git/gh child. Scrub once, at import.
os.environ.pop("PYTHONPATH", None)

HOME = Path(os.environ.get("HERMESCODER_HOME", "~/.hermescoder")).expanduser()
LOCK_PATH = HOME / "delegate.lock"
GUARD_PATH = HOME / "golden_guard.py"
OVERRIDES_PATH = HOME / "merge-policy.json"
MERGES_LOG = HOME / "merges.jsonl"
WORKTREES_DIR = HOME / "worktrees"
# Bounded wait for GitHub to land an auto-merge PR; the tool returns PR_OPEN after it
# (auto-merge stays armed). Under the Hermes terminal cap (600 s) keep this below it.
SHIP_WAIT_SECONDS = float(os.environ.get("SHIP_WAIT_SECONDS", "420"))
SHIP_POLL_SECONDS = float(os.environ.get("SHIP_POLL_SECONDS", "20"))


# ---------- overrides (optional, never a gate) ----------

def overrides_for(repo: Path) -> dict:
    """Per-repo overrides keyed by resolved checkout path; {} when absent/unreadable."""
    if not OVERRIDES_PATH.is_file():
        return {}
    try:
        repos = json.loads(OVERRIDES_PATH.read_text()).get("repos", {})
    except (json.JSONDecodeError, AttributeError):
        return {}
    rp = str(Path(repo).expanduser().resolve())
    for key, cfg in repos.items():
        if str(Path(key).expanduser().resolve()) == rp and isinstance(cfg, dict):
            return cfg
    return {}


# ---------- git helpers ----------

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def rev(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", ref)


def current_branch(repo: Path) -> str:
    return _git(repo, "branch", "--show-current")


def _ref_exists(repo: Path, ref: str) -> bool:
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=repo, capture_output=True,
    ).returncode == 0


def _is_ancestor(repo: Path, anc: str, desc: str) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", anc, desc],
        cwd=repo, capture_output=True,
    ).returncode == 0


def _dirty(repo: Path) -> bool:
    return bool(_git(repo, "status", "--porcelain"))


def delta_base(repo: Path, branch: str, target: str, git=None) -> str | None:
    """merge-base of the branch and the REMOTE target when it exists (the local target
    may be stale), else the local target. None when git cannot tell."""
    runner = git or git_run
    runner(repo, "fetch", "-q", "origin", target)
    for ref in (f"origin/{target}", target):
        if _ref_exists(repo, ref):
            mb = subprocess.run(["git", "merge-base", ref, branch], cwd=repo,
                                capture_output=True, text=True)
            if mb.returncode == 0 and mb.stdout.strip():
                return mb.stdout.strip()
    return None


def default_target(repo: Path) -> str | None:
    """The repo's default branch: origin/HEAD when known, else main, else master."""
    head = subprocess.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=repo, capture_output=True, text=True,
    )
    if head.returncode == 0 and head.stdout.strip():
        name = head.stdout.strip()
        name = name.split("/", 1)[1] if name.startswith("origin/") else name
        if _ref_exists(repo, name):
            return name
    for name in ("main", "master"):
        if _ref_exists(repo, name):
            return name
    return None


# ---------- injectable process runners (tests record them; no network) ----------

def git_run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def gh_run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], cwd=repo, capture_output=True, text=True)


# ---------- guard (subprocess seam; injected in tests) ----------

def run_guard(repo: Path, overrides_key: Path | None = None, base: str | None = None) -> dict:
    """The guard reads the per-repo overrides itself; `overrides_key` names the real
    checkout so a temp worktree still resolves that repo's entry (cov_path, tests…).
    `base` makes the structural checks gate the branch's delta, not legacy debt."""
    cmd = [sys.executable, str(GUARD_PATH), "--repo", str(repo)]
    if overrides_key is not None:
        cmd += ["--overrides-key", str(overrides_key)]
    if base:
        cmd += ["--base", base]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"verdict": "ERROR",
                "reason": f"guard output unparsable: {proc.stdout[-500:]}"}


def _guard_on_branch_tip(repo: Path, branch: str, ts: str, guard_runner) -> dict:
    """Fresh guard on the branch tip in a detached temp worktree — the primary
    checkout is never moved for a verdict that might refuse."""
    wt = WORKTREES_DIR / f"merge-{ts}"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "--detach", str(wt), branch)
    try:
        return guard_runner(wt)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                       cwd=repo, capture_output=True)


def _report_digest(report: dict) -> dict:
    return {
        "verdict": report.get("verdict"),
        "sha256": hashlib.sha256(
            json.dumps(report, sort_keys=True, default=str).encode()
        ).hexdigest(),
        "report": report,
    }


# ---------- environment (same launcher pattern as the delegate) ----------

def restore_subscription_env() -> None:
    """The brain's terminal tool strips CLAUDE_CODE_OAUTH_TOKEN from subprocesses.
    This tool is pure git/gh (no credential used), but every launcher the brain
    spawns keeps the one restore pattern so no future addition rediscovers it."""
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        envfile = HOME / "claude.env"
        if envfile.is_file():
            for line in envfile.read_text().splitlines():
                if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                    val = line.split("=", 1)[1].strip().strip('"')
                    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = val
                    break
    for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "PYTHONPATH"):
        os.environ.pop(var, None)


# ---------- audit ----------

def _audit(verdict: dict, jsonl: Path | None) -> None:
    """Best-effort audit — must never mask the real verdict."""
    try:
        MERGES_LOG.parent.mkdir(parents=True, exist_ok=True)
        with MERGES_LOG.open("a") as fh:
            fh.write(json.dumps(verdict, default=str) + "\n")
        if jsonl is not None:
            with jsonl.open("a") as fh:
                fh.write(json.dumps({"_type": "MergeVerdict", **verdict},
                                    default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _refused(verdict: dict, reason: str) -> dict:
    verdict.update({"verdict": "REFUSED", "reason": reason})
    return verdict


# ---------- ship lanes ----------

def _pr_url_for_branch(repo: Path, branch: str, gh) -> str | None:
    out = gh(repo, "pr", "list", "--head", branch, "--state", "open", "--json", "url",
             "--jq", ".[0].url")
    url = (out.stdout or "").strip() if out.returncode == 0 else ""
    return url or None


def _ship_pr_lane(verdict: dict, repo: Path, branch: str, target: str, subject: str,
                  git, gh, wait_seconds: float, sleep) -> dict:
    """Branch push → PR → auto-merge → bounded wait → fast-forward the local target.
    The local target is never written by this lane except by a fast-forward to what
    GitHub merged, so the checkout never diverges from origin."""
    push = git(repo, "push", "-u", "origin", branch)
    if push.returncode != 0:
        verdict.update({"verdict": "FAIL",
                        "reason": "branch push failed: " + (push.stderr or push.stdout)[-500:]})
        return verdict
    url = _pr_url_for_branch(repo, branch, gh)
    if url is None:
        body = (f"Shipped by the merge tool: guard GREEN on `{verdict.get('branch_sha', '')[:12]}`.\n\n"
                f"Message: {verdict.get('message') or '(none)'}")
        created = gh(repo, "pr", "create", "--base", target, "--head", branch,
                     "--title", subject, "--body", body)
        if created.returncode != 0:
            verdict.update({"verdict": "FAIL",
                            "reason": "gh pr create failed: " + (created.stderr or created.stdout)[-500:]})
            return verdict
        url = next((tok for tok in (created.stdout or "").split() if tok.startswith("https://")), None) \
            or _pr_url_for_branch(repo, branch, gh)
        if url is None:
            verdict.update({"verdict": "FAIL", "reason": "gh pr create returned no URL"})
            return verdict
    verdict["pr_url"] = url
    armed = gh(repo, "pr", "merge", url, "--auto", "--merge")
    verdict["auto_merge"] = "armed" if armed.returncode == 0 else \
        "not-armed: " + (armed.stderr or armed.stdout)[-300:]
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        view = gh(repo, "pr", "view", url, "--json", "state,mergedAt,mergeCommit")
        state, merge_sha = "", None
        if view.returncode == 0:
            try:
                data = json.loads(view.stdout or "{}")
                state = str(data.get("state", "")).upper()
                merge_sha = (data.get("mergeCommit") or {}).get("oid")
            except json.JSONDecodeError:
                pass
        if state == "MERGED":
            # Fast-forward the local target to what GitHub merged (ref update only when the
            # target is not the current checkout; ff-only pull when it is).
            git(repo, "fetch", "origin", target)
            if current_branch(repo) == target:
                git(repo, "merge", "--ff-only", f"origin/{target}")
            else:
                git(repo, "fetch", "origin", f"{target}:{target}")
            verdict.update({"verdict": "SHIPPED", "via": "pr", "merge_sha": merge_sha,
                            "reason": f"PR merged on GitHub after the required checks; local {target} fast-forwarded"})
            return verdict
        if state == "CLOSED":
            verdict.update({"verdict": "FAIL", "reason": f"PR closed without merge: {url}"})
            return verdict
        if time.monotonic() >= deadline:
            verdict.update({"verdict": "PR_OPEN",
                            "reason": f"PR open, auto-merge {verdict['auto_merge']}; GitHub lands it when the "
                                      f"required checks pass — nothing else to do: {url}"})
            return verdict
        sleep(SHIP_POLL_SECONDS)


# ---------- orchestration ----------

def run_merge(
    message: str,
    repo: Path,
    branch: str,
    target: str | None = None,
    guard_runner=run_guard,
    jsonl: Path | None = None,
    ship: bool = True,
    git=git_run,
    gh=gh_run,
    wait_seconds: float | None = None,
    sleep=time.sleep,
) -> dict:
    ts = time.strftime("%Y%m%d-%H%M%S")
    t0 = time.time()
    verdict: dict = {
        "verdict": "FAIL",
        "reason": "crashed before completion",
        "ts": ts,
        "repo": str(repo),
        "branch": branch,
        "target": target,
        "message": message or "",
        "ship": bool(ship),
    }
    try:
        ov = overrides_for(repo)
        ci_oracle = ov.get("tests") == "ci"
        verdict["overrides"] = ov or None
        if target is None:
            target = ov.get("target") or default_target(repo)
        if not target:
            return _refused(verdict, "no target: no origin/HEAD, main or master in this repo")
        verdict["target"] = target
        base = delta_base(repo, branch, target, git) if _ref_exists(repo, branch) else None
        verdict["base"] = base
        if guard_runner is run_guard:
            guard_runner = functools.partial(run_guard, overrides_key=repo, base=base)

        if not _ref_exists(repo, branch):
            return _refused(verdict, f"branch does not exist: {branch}")
        if not _ref_exists(repo, target):
            return _refused(verdict, f"target does not exist: {target}")
        if rev(repo, branch) == rev(repo, target):
            return _refused(verdict, "nothing to merge: branch is at the target sha")
        if _is_ancestor(repo, branch, target):
            return _refused(verdict, f"already merged: {branch} is an ancestor of {target}")
        verdict["branch_sha"] = rev(repo, branch)
        subject = _git(repo, "log", "-1", "--format=%s", branch) or f"merge: {branch}"

        pre = _guard_on_branch_tip(repo, branch, ts, guard_runner)
        verdict["guard_pre"] = _report_digest(pre)
        if pre.get("verdict") != "GREEN":
            return _refused(verdict,
                            "guard not GREEN on the branch tip at merge time — "
                            "whatever any earlier run said")

        wait = SHIP_WAIT_SECONDS if wait_seconds is None else wait_seconds
        if ci_oracle:
            # The remote CI is this repo's test oracle (its suite exceeds the local budget):
            # never merge locally, never push the target — GitHub lands the PR.
            if not ship:
                return _refused(verdict,
                                "overrides say tests: ci — this repo lands only through the PR lane "
                                "(drop --no-ship)")
            return _ship_pr_lane(verdict, repo, branch, target, subject, git, gh, wait, sleep)

        if _dirty(repo):
            return _refused(verdict,
                            "working tree not clean — commit/stash before merging")

        prior = current_branch(repo)
        before = rev(repo, target)
        verdict["target_sha_before"] = before
        _git(repo, "checkout", "-q", target)
        try:
            note = f" — {message}" if message else ""
            _git(repo, "merge", "--no-ff", branch,
                 "-m", f"merge: {branch} (merge tool, {ts}){note}")
        except subprocess.CalledProcessError as exc:
            subprocess.run(["git", "merge", "--abort"], cwd=repo, capture_output=True)
            if prior and prior != target:
                subprocess.run(["git", "checkout", "-q", prior], cwd=repo,
                               capture_output=True)
            verdict.update({"verdict": "FAIL",
                            "reason": "merge failed (conflict?): "
                                      f"{(exc.stderr or exc.stdout or '')[-500:]}"})
            return verdict

        post = guard_runner(repo)
        verdict["guard_post"] = _report_digest(post)
        if post.get("verdict") != "GREEN":
            _git(repo, "reset", "--hard", before)
            verdict.update({"verdict": "FAIL",
                            "reason": "post-merge guard RED — merge rolled back "
                                      f"to {before}"})
            return verdict

        merge_sha = rev(repo, "HEAD")
        verdict.update({"verdict": "MERGED", "merge_sha": merge_sha,
                        "reason": "guard GREEN pre and post; merged --no-ff (local)"})
        if not ship:
            return verdict

        pushed = git(repo, "push", "origin", target)
        if pushed.returncode == 0:
            verdict.update({"verdict": "SHIPPED", "via": "direct",
                            "reason": f"guard GREEN pre and post; merged --no-ff and pushed origin/{target}"})
            return verdict
        # The remote refused the target (rulesets / required checks / protection): undo
        # the local merge so the checkout cannot diverge, then let GitHub land a PR.
        verdict["direct_push"] = "rejected: " + (pushed.stderr or pushed.stdout)[-300:]
        _git(repo, "reset", "--hard", before)
        return _ship_pr_lane(verdict, repo, branch, target, subject, git, gh, wait, sleep)
    except Exception as exc:  # noqa: BLE001 — verdict line must always exist
        verdict.update({"verdict": "FAIL",
                        "reason": f"{type(exc).__name__}: {exc}"})
        return verdict
    finally:
        verdict["duration_s"] = round(time.time() - t0, 1)
        _audit(verdict, jsonl)


LANDED = {"MERGED", "SHIPPED", "PR_OPEN"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", required=True)
    ap.add_argument("--message", "--grant-message", dest="message", default="",
                    help="optional owner message (audit + merge-commit annotation only)")
    ap.add_argument("--target", help="merge target; default = the repo's default branch")
    ap.add_argument("--no-ship", action="store_true",
                    help="merge locally only (default ships: push, or PR + auto-merge)")
    ap.add_argument("--jsonl", help="delegate run JSONL to append MergeVerdict to")
    args = ap.parse_args()

    # Fail-closed subscription rule — same contract as delegate and guard.
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(json.dumps({"verdict": "REFUSED",
                          "reason": "ANTHROPIC_API_KEY present — refusing "
                                    "(subscription lane only)"}))
        return 2
    restore_subscription_env()

    repo = Path(args.repo).expanduser()
    if not (repo / ".git").exists():
        print(json.dumps({"verdict": "REFUSED", "reason": f"not a git repo: {repo}"}))
        return 3

    HOME.mkdir(parents=True, exist_ok=True)
    lock_fh = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"verdict": "BUSY",
                          "reason": "a delegate or merge run is in progress "
                                    "(sequential-only)"}))
        return 3

    try:
        verdict = run_merge(
            args.message, repo, args.branch, target=args.target,
            jsonl=Path(args.jsonl).expanduser() if args.jsonl else None,
            ship=not args.no_ship,
        )
        print(json.dumps(verdict, indent=2, default=str))
        return 0 if verdict["verdict"] in LANDED else 2
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


if __name__ == "__main__":
    sys.exit(main())
