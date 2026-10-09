"""Merge + ship tool tests — guard is an injected fake; git is real (tmp repos with a
real bare `origin`); `gh` is a recorder. No network, ever.

Contract under test (the owner's global policy, 2026-10-09): no gate — no grant phrase,
no policy map, any branch, any repo. What refuses: guard RED pre (refuse) / post
(roll back), missing refs, nothing to merge, already merged, dirty tree (local lane).
A GREEN merge ships: direct push, else PR + auto-merge; `tests: "ci"` repos only
through the PR lane; --no-ship keeps it local.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import merge_branch as mb  # noqa: E402

BRANCH = "agent/fix-thing-160450"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "hermescoder-home"
    h.mkdir()
    monkeypatch.setattr(mb, "HOME", h)
    monkeypatch.setattr(mb, "LOCK_PATH", h / "delegate.lock")
    monkeypatch.setattr(mb, "GUARD_PATH", h / "golden_guard.py")
    monkeypatch.setattr(mb, "OVERRIDES_PATH", h / "merge-policy.json")
    monkeypatch.setattr(mb, "MERGES_LOG", h / "merges.jsonl")
    monkeypatch.setattr(mb, "WORKTREES_DIR", h / "worktrees")
    monkeypatch.setattr(mb, "SHIP_POLL_SECONDS", 0.0)
    return h


def _run(cwd, *a):
    return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture()
def origin(tmp_path):
    """A real bare remote whose default branch is main."""
    o = tmp_path / "origin.git"
    _run(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(o))
    return o


@pytest.fixture()
def repo(tmp_path, home, origin):
    r = tmp_path / "repo"
    r.mkdir()
    _run(r, "git", "init", "-q", "-b", "main")
    _run(r, "git", "config", "user.name", "t")
    _run(r, "git", "config", "user.email", "t@t")
    (r / "README.md").write_text("fixture\n")
    _run(r, "git", "add", "-A")
    _run(r, "git", "commit", "-q", "-m", "init")
    _run(r, "git", "remote", "add", "origin", str(origin))
    _run(r, "git", "push", "-q", "-u", "origin", "main")
    _run(r, "git", "remote", "set-head", "origin", "main")
    _run(r, "git", "checkout", "-q", "-b", BRANCH)
    (r / "fix.txt").write_text("fix\n")
    _run(r, "git", "add", "-A")
    _run(r, "git", "commit", "-q", "-m", "fix: the thing")
    _run(r, "git", "checkout", "-q", "main")
    return r


def green(repo):
    return {"verdict": "GREEN", "checks": []}


def guard_seq(*verdicts):
    calls = {"n": 0}

    def _g(repo):
        v = verdicts[min(calls["n"], len(verdicts) - 1)]
        calls["n"] += 1
        return {"verdict": v, "checks": []}
    return _g


def sha(repo, ref="HEAD"):
    return _run(repo, "git", "rev-parse", ref).stdout.strip()


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def reject_main_pushes(origin: Path, ref="refs/heads/main"):
    """A pre-receive hook that rejects updates to one ref — the ruleset stand-in."""
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nwhile read old new ref; do\n"
                    f"  [ \"$ref\" = \"{ref}\" ] && {{ echo 'rejected by ruleset' >&2; exit 1; }}\n"
                    "done\nexit 0\n")
    hook.chmod(0o755)


class GhRecorder:
    """Records gh calls; answers pr list/create/merge/view from a tiny state machine."""

    def __init__(self, merged_after_views=1, url="https://github.com/o/r/pull/7",
                 merge_oid="deadbeef", existing=False, create_rc=0, closed=False):
        self.calls = []
        self.views = 0
        self.merged_after_views = merged_after_views
        self.url, self.merge_oid, self.existing = url, merge_oid, existing
        self.create_rc, self.closed = create_rc, closed

    def __call__(self, repo, *args):
        self.calls.append(args)
        if args[:2] == ("pr", "list"):
            return FakeProc(0, self.url + "\n" if self.existing else "")
        if args[:2] == ("pr", "create"):
            return FakeProc(self.create_rc, f"Creating pull request\n{self.url}\n" if self.create_rc == 0 else "",
                            "boom" if self.create_rc else "")
        if args[:2] == ("pr", "merge"):
            return FakeProc(0, "")
        if args[:2] == ("pr", "view"):
            self.views += 1
            if self.closed:
                return FakeProc(0, json.dumps({"state": "CLOSED"}))
            if self.views >= self.merged_after_views:
                return FakeProc(0, json.dumps({"state": "MERGED", "mergedAt": "now",
                                               "mergeCommit": {"oid": self.merge_oid}}))
            return FakeProc(0, json.dumps({"state": "OPEN"}))
        return FakeProc(1, "", f"unexpected gh {args}")


# ---------- no gate: any message, any branch name, optional overrides ----------

def test_any_message_merges_locally_and_audits(home, repo):
    before = sha(repo, "main")
    v = mb.run_merge("iss it fixed? if so, ship", repo, BRANCH, guard_runner=green, ship=False)
    assert v["verdict"] == "MERGED"
    assert sha(repo, "main") != before
    line = json.loads((home / "merges.jsonl").read_text().splitlines()[-1])
    assert line["verdict"] == "MERGED" and line["message"] == "iss it fixed? if so, ship"
    assert "grant" not in line


def test_any_branch_name_is_mergeable(home, repo):
    _run(repo, "git", "branch", "feature/x", BRANCH)
    v = mb.run_merge("", repo, "feature/x", guard_runner=green, ship=False)
    assert v["verdict"] == "MERGED"


def test_missing_overrides_file_is_not_a_gate(home, repo):
    assert not (home / "merge-policy.json").exists()
    v = mb.run_merge("", repo, BRANCH, guard_runner=green, ship=False)
    assert v["verdict"] == "MERGED" and v["overrides"] is None


def test_target_defaults_to_origin_head(home, repo):
    v = mb.run_merge("", repo, BRANCH, guard_runner=green, ship=False)
    assert v["target"] == "main"


def test_target_falls_back_to_main_without_origin_head(home, repo):
    _run(repo, "git", "remote", "remove", "origin")
    v = mb.run_merge("", repo, BRANCH, guard_runner=green, ship=False)
    assert v["verdict"] == "MERGED" and v["target"] == "main"


def test_no_default_branch_refuses(home, repo):
    _run(repo, "git", "remote", "remove", "origin")
    _run(repo, "git", "checkout", "-q", BRANCH)
    _run(repo, "git", "branch", "-D", "main")
    v = mb.run_merge("", repo, BRANCH, guard_runner=green, ship=False)
    assert v["verdict"] == "REFUSED" and "no target" in v["reason"]


def test_overrides_target_and_entry_are_honoured(home, repo):
    _run(repo, "git", "branch", "staging", "main")
    (home / "merge-policy.json").write_text(json.dumps(
        {"repos": {str(repo): {"target": "staging", "cov_path": "pkg"}}}))
    v = mb.run_merge("", repo, BRANCH, guard_runner=green, ship=False)
    assert v["verdict"] == "MERGED" and v["target"] == "staging"
    assert v["overrides"] == {"target": "staging", "cov_path": "pkg"}


def test_explicit_target_wins_over_overrides(home, repo):
    _run(repo, "git", "branch", "staging", "main")
    (home / "merge-policy.json").write_text(json.dumps({"repos": {str(repo): {"target": "staging"}}}))
    v = mb.run_merge("", repo, BRANCH, target="main", guard_runner=green, ship=False)
    assert v["verdict"] == "MERGED" and v["target"] == "main"


def test_guard_runner_gets_the_overrides_key(home, repo, monkeypatch):
    seen = {}

    def fake_run_guard(path, overrides_key=None, base=None):
        seen["key"], seen["base"] = overrides_key, base
        return {"verdict": "GREEN", "checks": []}
    monkeypatch.setattr(mb, "run_guard", fake_run_guard)
    main_before = sha(repo, "main")
    v = mb.run_merge("", repo, BRANCH, guard_runner=mb.run_guard, ship=False)
    assert v["verdict"] == "MERGED" and seen["key"] == repo
    # the guard gates the DELTA: base = merge-base(origin/main, branch) = main's tip before the merge
    assert seen["base"] == v["base"] == main_before


def test_delta_base_prefers_the_remote_target(home, repo, origin, tmp_path):
    # advance origin/main from a second clone; the local main is now stale
    other = tmp_path / "other"
    _run(tmp_path, "git", "clone", "-q", str(origin), str(other))
    _run(other, "git", "config", "user.name", "t"); _run(other, "git", "config", "user.email", "t@t")
    (other / "remote.txt").write_text("remote change\n")
    _run(other, "git", "add", "-A"); _run(other, "git", "commit", "-q", "-m", "remote")
    _run(other, "git", "push", "-q", "origin", "main")
    base = mb.delta_base(repo, BRANCH, "main")
    # branch forked from the old main, which is an ancestor of the new origin/main
    assert base == sha(repo, "main")
    assert sha(repo, "origin/main") != sha(repo, "main")


# ---------- mechanical safety (kept) ----------

def test_missing_branch_refuses(home, repo):
    v = mb.run_merge("", repo, "agent/ghost-000001", guard_runner=green, ship=False)
    assert v["verdict"] == "REFUSED" and "does not exist" in v["reason"]


def test_nothing_to_merge_refuses(home, repo):
    _run(repo, "git", "branch", "agent/empty-000002", "main")
    v = mb.run_merge("", repo, "agent/empty-000002", guard_runner=green, ship=False)
    assert v["verdict"] == "REFUSED" and "nothing to merge" in v["reason"]


def test_already_merged_refuses(home, repo):
    _run(repo, "git", "merge", "--no-ff", "-q", BRANCH, "-m", "manual merge")
    v = mb.run_merge("", repo, BRANCH, guard_runner=green, ship=False)
    assert v["verdict"] == "REFUSED" and "already merged" in v["reason"]


def test_dirty_tree_refuses_local_lane(home, repo):
    (repo / "uncommitted.txt").write_text("dirty\n")
    before = sha(repo, "main")
    v = mb.run_merge("", repo, BRANCH, guard_runner=green, ship=False)
    assert v["verdict"] == "REFUSED" and "clean" in v["reason"]
    assert sha(repo, "main") == before


def test_guard_red_pre_merge_refuses(home, repo):
    before = sha(repo, "main")
    v = mb.run_merge("", repo, BRANCH, guard_runner=guard_seq("RED"), ship=False)
    assert v["verdict"] == "REFUSED" and "guard" in v["reason"].lower()
    assert sha(repo, "main") == before
    assert v["guard_pre"]["verdict"] == "RED"


def test_guard_red_post_merge_rolls_back_exactly(home, repo):
    before = sha(repo, "main")
    v = mb.run_merge("", repo, BRANCH, guard_runner=guard_seq("GREEN", "RED"), ship=False)
    assert v["verdict"] == "FAIL" and "rolled back" in v["reason"]
    assert sha(repo, "main") == before
    assert v["target_sha_before"] == before
    assert v["guard_post"]["verdict"] == "RED"


def test_merge_conflict_aborts_and_restores(home, repo):
    _run(repo, "git", "checkout", "-q", "main")
    (repo / "fix.txt").write_text("conflicting content\n")
    _run(repo, "git", "add", "-A")
    _run(repo, "git", "commit", "-q", "-m", "conflicting change on main")
    before = sha(repo, "main")
    v = mb.run_merge("", repo, BRANCH, guard_runner=green, ship=False)
    assert v["verdict"] == "FAIL" and "merge failed" in v["reason"]
    assert sha(repo, "main") == before
    assert _run(repo, "git", "status", "--porcelain").stdout.strip() == ""


def test_merged_happy_path_local(home, repo):
    before = sha(repo, "main")
    v = mb.run_merge("conserta o bug", repo, BRANCH, guard_runner=green, ship=False)
    assert v["verdict"] == "MERGED"
    assert v["target_sha_before"] == before
    assert v["merge_sha"] == sha(repo, "main") != before
    parents = _run(repo, "git", "rev-list", "--parents", "-1", "HEAD").stdout.split()
    assert len(parents) == 3  # --no-ff
    msg = _run(repo, "git", "log", "-1", "--format=%s").stdout
    assert "conserta o bug" in msg and BRANCH in msg
    assert _run(repo, "git", "branch", "--show-current").stdout.strip() == "main"
    line = json.loads((home / "merges.jsonl").read_text().splitlines()[-1])
    assert line["guard_pre"]["sha256"] and line["guard_post"]["sha256"]


def test_merge_verdict_written_to_run_jsonl(home, repo, tmp_path):
    jsonl = tmp_path / "run.jsonl"
    v = mb.run_merge("", repo, BRANCH, guard_runner=green, jsonl=jsonl, ship=False)
    assert v["verdict"] == "MERGED"
    last = json.loads(jsonl.read_text().splitlines()[-1])
    assert last["_type"] == "MergeVerdict" and last["verdict"] == "MERGED"


def test_pre_merge_guard_runs_on_branch_tip_worktree(home, repo):
    seen = []

    def spy(path):
        seen.append(Path(path))
        return {"verdict": "GREEN", "checks": []}
    v = mb.run_merge("", repo, BRANCH, guard_runner=spy, ship=False)
    assert v["verdict"] == "MERGED"
    assert seen[0] != repo and str(seen[0]).startswith(str(mb.WORKTREES_DIR))
    assert seen[1] == repo
    assert not seen[0].exists()


def test_crash_still_audits_terminal_verdict(home, repo):
    def boom(path):
        raise RuntimeError("guard exploded")
    v = mb.run_merge("", repo, BRANCH, guard_runner=boom, ship=False)
    assert v["verdict"] == "FAIL" and "guard exploded" in v["reason"]
    line = json.loads((home / "merges.jsonl").read_text().splitlines()[-1])
    assert line["verdict"] == "FAIL"


# ---------- ship: direct lane (real bare remote) ----------

def test_ship_direct_pushes_the_target(home, repo, origin):
    gh = GhRecorder()
    v = mb.run_merge("", repo, BRANCH, guard_runner=green, gh=gh)
    assert v["verdict"] == "SHIPPED" and v["via"] == "direct"
    assert sha(origin, "main") == v["merge_sha"] == sha(repo, "main")
    assert gh.calls == []  # no PR needed


def test_no_ship_keeps_origin_untouched(home, repo, origin):
    before = sha(origin, "main")
    v = mb.run_merge("", repo, BRANCH, guard_runner=green, ship=False)
    assert v["verdict"] == "MERGED" and sha(origin, "main") == before


# ---------- ship: PR lane (remote rejects the target; recorded gh) ----------

def test_rejected_push_falls_back_to_pr_lane_and_lands(home, repo, origin):
    reject_main_pushes(origin)
    before = sha(origin, "main")
    gh = GhRecorder(merged_after_views=2)
    v = mb.run_merge("ship it", repo, BRANCH, guard_runner=green, gh=gh, wait_seconds=60)
    assert v["verdict"] == "SHIPPED" and v["via"] == "pr"
    assert v["pr_url"] == gh.url and v["merge_sha"] == "deadbeef"
    assert "rejected" in v["direct_push"]
    # local main was undone (no divergence) and only fast-forwarded to origin
    assert sha(repo, "main") == before == sha(origin, "main")
    assert _run(repo, "git", "status", "--porcelain").stdout.strip() == ""
    # the branch itself reached origin
    assert sha(origin, BRANCH) == sha(repo, BRANCH)
    kinds = [c[:2] for c in gh.calls]
    assert kinds[:3] == [("pr", "list"), ("pr", "create"), ("pr", "merge")]
    create = next(c for c in gh.calls if c[:2] == ("pr", "create"))
    assert "--base" in create and "main" in create and "--head" in create and BRANCH in create
    merge = next(c for c in gh.calls if c[:2] == ("pr", "merge"))
    assert "--auto" in merge and "--merge" in merge
    assert kinds.count(("pr", "view")) == 2


def test_pr_lane_returns_pr_open_at_the_deadline(home, repo, origin):
    reject_main_pushes(origin)
    gh = GhRecorder(merged_after_views=10**6)
    v = mb.run_merge("", repo, BRANCH, guard_runner=green, gh=gh, wait_seconds=0)
    assert v["verdict"] == "PR_OPEN" and v["pr_url"] == gh.url and v["auto_merge"] == "armed"
    assert sha(repo, "main") == sha(origin, "main")


def test_pr_lane_reuses_an_existing_open_pr(home, repo, origin):
    reject_main_pushes(origin)
    gh = GhRecorder(existing=True)
    v = mb.run_merge("", repo, BRANCH, guard_runner=green, gh=gh, wait_seconds=60)
    assert v["verdict"] == "SHIPPED"
    assert all(c[:2] != ("pr", "create") for c in gh.calls)


def test_pr_lane_closed_pr_is_a_fail(home, repo, origin):
    reject_main_pushes(origin)
    gh = GhRecorder(closed=True)
    v = mb.run_merge("", repo, BRANCH, guard_runner=green, gh=gh, wait_seconds=60)
    assert v["verdict"] == "FAIL" and "closed" in v["reason"]


def test_pr_lane_create_failure_is_a_fail(home, repo, origin):
    reject_main_pushes(origin)
    gh = GhRecorder(create_rc=1)
    v = mb.run_merge("", repo, BRANCH, guard_runner=green, gh=gh, wait_seconds=60)
    assert v["verdict"] == "FAIL" and "gh pr create" in v["reason"]


def test_ci_oracle_repo_never_merges_locally_and_forces_pr_lane(home, repo, origin):
    (home / "merge-policy.json").write_text(json.dumps({"repos": {str(repo): {"tests": "ci"}}}))
    (repo / "untracked-scratch.txt").write_text("the PR lane tolerates an untracked file\n")
    before = sha(repo, "main")
    gh = GhRecorder()
    v = mb.run_merge("", repo, BRANCH, guard_runner=green, gh=gh, wait_seconds=60)
    assert v["verdict"] == "SHIPPED" and v["via"] == "pr"
    assert "target_sha_before" not in v and "direct_push" not in v
    assert sha(repo, "main") == before
    assert sha(origin, BRANCH) == sha(repo, BRANCH)


def test_ci_oracle_repo_refuses_no_ship(home, repo):
    (home / "merge-policy.json").write_text(json.dumps({"repos": {str(repo): {"tests": "ci"}}}))
    v = mb.run_merge("", repo, BRANCH, guard_runner=green, ship=False)
    assert v["verdict"] == "REFUSED" and "tests: ci" in v["reason"]


def test_pr_lane_fast_forwards_a_checked_out_target(home, repo, origin):
    reject_main_pushes(origin)
    _run(repo, "git", "checkout", "-q", "main")
    gh = GhRecorder()
    v = mb.run_merge("", repo, BRANCH, guard_runner=green, gh=gh, wait_seconds=60)
    assert v["verdict"] == "SHIPPED"
    assert _run(repo, "git", "branch", "--show-current").stdout.strip() == "main"


# ---------- CLI entry (main): env asserts, lock, exit codes ----------

def _stub_guard(home):
    (home / "golden_guard.py").write_text(
        "import json\nprint(json.dumps({'verdict': 'GREEN', 'checks': []})"
        ".replace(\"'\", '\"'))\n")


def test_main_ships_and_exits_zero(home, repo, origin, monkeypatch, capsys):
    _stub_guard(home)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["merge_branch.py", "--repo", str(repo),
                                      "--branch", BRANCH, "--message", "ship"])
    rc = mb.main()
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["verdict"] == "SHIPPED" and out["via"] == "direct"
    assert sha(origin, "main") == out["merge_sha"]


def test_main_no_ship_merges_locally(home, repo, origin, monkeypatch, capsys):
    _stub_guard(home)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    before = sha(origin, "main")
    monkeypatch.setattr(sys, "argv", ["merge_branch.py", "--repo", str(repo),
                                      "--branch", BRANCH, "--no-ship", "--grant-message", "legacy flag"])
    rc = mb.main()
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["verdict"] == "MERGED" and out["message"] == "legacy flag"
    assert sha(origin, "main") == before


def test_main_refusal_exits_two(home, repo, monkeypatch, capsys):
    _stub_guard(home)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (repo / "dirty.txt").write_text("x\n")
    monkeypatch.setattr(sys, "argv", ["merge_branch.py", "--repo", str(repo),
                                      "--branch", BRANCH, "--no-ship"])
    rc = mb.main()
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["verdict"] == "REFUSED" and "clean" in out["reason"]


def test_main_metered_key_refuses(home, repo, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-nope")
    monkeypatch.setattr(sys, "argv", ["merge_branch.py", "--repo", str(repo),
                                      "--branch", BRANCH])
    rc = mb.main()
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["verdict"] == "REFUSED"


def test_main_busy_when_lock_held(home, repo, monkeypatch, capsys):
    import fcntl
    _stub_guard(home)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    holder = (home / "delegate.lock").open("w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        monkeypatch.setattr(sys, "argv", ["merge_branch.py", "--repo", str(repo),
                                          "--branch", BRANCH])
        rc = mb.main()
        out = json.loads(capsys.readouterr().out)
        assert rc == 3 and out["verdict"] == "BUSY"
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()


def test_main_not_a_repo_exits_three(home, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["merge_branch.py", "--repo", str(tmp_path / "nope"),
                                      "--branch", BRANCH])
    rc = mb.main()
    out = json.loads(capsys.readouterr().out)
    assert rc == 3 and out["verdict"] == "REFUSED"


def test_pythonpath_is_scrubbed_at_import(monkeypatch):
    import importlib
    monkeypatch.setenv("PYTHONPATH", "/some/engine/site-packages")
    importlib.reload(mb)
    assert "PYTHONPATH" not in __import__("os").environ
