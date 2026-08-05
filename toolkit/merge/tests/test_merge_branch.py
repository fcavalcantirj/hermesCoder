"""Merge-tool tests — guard is an injected fake; git is real (tmp repos).

Every refusal path is here red-on-demand: the tool must refuse loudly, never
merge quietly. The load-bearing test is the "não pode mergear" trap — the exact
grant phrase is a SUFFIX of its own negation, so naive substring matching would
accept it (the classic substring false-accept class).
"""

import fcntl
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import merge_branch as mb  # noqa: E402

BRANCH = "agent/fix-thing-160450"
GRANT = "merge allowed"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "agent-home"
    h.mkdir()
    monkeypatch.setattr(mb, "HOME", h)
    monkeypatch.setattr(mb, "LOCK_PATH", h / "delegate.lock")
    monkeypatch.setattr(mb, "GUARD_PATH", h / "golden_guard.py")
    monkeypatch.setattr(mb, "POLICY_PATH", h / "merge-policy.json")
    monkeypatch.setattr(mb, "MERGES_LOG", h / "merges.jsonl")
    monkeypatch.setattr(mb, "WORKTREES_DIR", h / "worktrees")
    # keep unit tests off the machine's REAL gateway store (the Pi has one)
    monkeypatch.setattr(mb, "HERMES_STATE_DB", h / "no-state.db")
    return h


def _run(cwd, *a):
    return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path, home):
    r = tmp_path / "repo"
    r.mkdir()
    _run(r, "git", "init", "-q", "-b", "main")
    _run(r, "git", "config", "user.name", "t")
    _run(r, "git", "config", "user.email", "t@t")
    (r / "README.md").write_text("fixture\n")
    _run(r, "git", "add", "-A")
    _run(r, "git", "commit", "-q", "-m", "init")
    _run(r, "git", "checkout", "-q", "-b", BRANCH)
    (r / "fix.txt").write_text("fix\n")
    _run(r, "git", "add", "-A")
    _run(r, "git", "commit", "-q", "-m", "fix")
    _run(r, "git", "checkout", "-q", "main")
    (home / "merge-policy.json").write_text(json.dumps(
        {"repos": {str(r): {"targets": ["main"], "lang": "go"}}}))
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


# ---------- grant parsing (the deterministic authorization gate) ----------

@pytest.mark.parametrize("msg,phrase", [
    ("merge allowed", "merge allowed"),
    ("MERGE ALLOWED", "merge allowed"),
    ("Merge Allowed.", "merge allowed"),
    ("merge allowed!", "merge allowed"),
    ("pode mergear", "pode mergear"),
    ("Pode Mergear", "pode mergear"),
    ("fix the login bug\n\nmerge allowed", "merge allowed"),
    ("fix the login bug. merge allowed", "merge allowed"),
    ("merge allowed — fix the login bug", "merge allowed"),
    ("implementa o endpoint, pode mergear", "pode mergear"),
])
def test_parse_grant_accepts(msg, phrase):
    g = mb.parse_grant(msg)
    assert g is not None and g["phrase"] == phrase


@pytest.mark.parametrize("msg", [
    "",
    "please merge",
    "merge allowed?",                   # a question is not a grant
    "pode mergear?",
    "não pode mergear",                 # the negation CONTAINS the grant phrase
    "nao pode mergear",
    "nunca pode mergear",
    "jamais pode mergear",
    "not merge allowed",
    "no merge allowed",
    "don't merge, not merge allowed",
    "sem merge allowed",
    "I think merge allowed is risky",   # mid-sentence: not a line, not an edge
    "merge\nallowed",                   # phrase split across lines never grants
])
def test_parse_grant_refuses(msg):
    assert mb.parse_grant(msg) is None


# ---------- refusal matrix (run_merge) ----------

def test_no_grant_refuses_and_audits(home, repo):
    before = sha(repo, "main")
    v = mb.run_merge("please merge this", repo, BRANCH, guard_runner=green)
    assert v["verdict"] == "REFUSED" and "no grant" in v["reason"]
    assert sha(repo, "main") == before
    line = json.loads((home / "merges.jsonl").read_text().splitlines()[-1])
    assert line["verdict"] == "REFUSED"
    assert line["grant_message"] == "please merge this"


def test_grant_mentioning_other_branch_refuses(home, repo):
    v = mb.run_merge("merge allowed agent/other-branch-123456", repo, BRANCH,
                     guard_runner=green)
    assert v["verdict"] == "REFUSED" and "mismatch" in v["reason"]


def test_grant_mentioning_wrong_timestamp_refuses(home, repo):
    v = mb.run_merge("merge allowed 999999", repo, BRANCH, guard_runner=green)
    assert v["verdict"] == "REFUSED" and "mismatch" in v["reason"]


def test_grant_mentioning_matching_timestamp_merges(home, repo):
    v = mb.run_merge("merge allowed 160450", repo, BRANCH, guard_runner=green)
    assert v["verdict"] == "MERGED"


def test_repo_not_in_policy_refuses(home, repo):
    (home / "merge-policy.json").write_text(json.dumps({"repos": {}}))
    v = mb.run_merge(GRANT, repo, BRANCH, guard_runner=green)
    assert v["verdict"] == "REFUSED" and "policy" in v["reason"]


def test_missing_policy_file_refuses(home, repo):
    (home / "merge-policy.json").unlink()
    v = mb.run_merge(GRANT, repo, BRANCH, guard_runner=green)
    assert v["verdict"] == "REFUSED" and "policy" in v["reason"]


def test_target_not_allowed_by_policy_refuses(home, repo):
    v = mb.run_merge(GRANT, repo, BRANCH, target="staging", guard_runner=green)
    assert v["verdict"] == "REFUSED" and "target" in v["reason"]


def test_default_target_comes_from_policy(home, repo):
    v = mb.run_merge(GRANT, repo, BRANCH, guard_runner=green)
    assert v["verdict"] == "MERGED" and v["target"] == "main"


def test_non_agent_branch_refuses(home, repo):
    _run(repo, "git", "branch", "feature/x", BRANCH)
    v = mb.run_merge(GRANT, repo, "feature/x", guard_runner=green)
    assert v["verdict"] == "REFUSED" and "agent/" in v["reason"]


def test_missing_branch_refuses(home, repo):
    v = mb.run_merge(GRANT, repo, "agent/ghost-000001", guard_runner=green)
    assert v["verdict"] == "REFUSED" and "does not exist" in v["reason"]


def test_nothing_to_merge_refuses(home, repo):
    _run(repo, "git", "branch", "agent/empty-000002", "main")
    v = mb.run_merge(GRANT, repo, "agent/empty-000002", guard_runner=green)
    assert v["verdict"] == "REFUSED" and "nothing to merge" in v["reason"]


def test_already_merged_refuses(home, repo):
    _run(repo, "git", "merge", "--no-ff", "-q", BRANCH, "-m", "manual merge")
    v = mb.run_merge(GRANT, repo, BRANCH, guard_runner=green)
    assert v["verdict"] == "REFUSED" and "already merged" in v["reason"]


def test_dirty_tree_refuses(home, repo):
    (repo / "uncommitted.txt").write_text("dirty\n")
    before = sha(repo, "main")
    v = mb.run_merge(GRANT, repo, BRANCH, guard_runner=green)
    assert v["verdict"] == "REFUSED" and "clean" in v["reason"]
    assert sha(repo, "main") == before


def test_guard_red_pre_merge_refuses(home, repo):
    before = sha(repo, "main")
    v = mb.run_merge(GRANT, repo, BRANCH, guard_runner=guard_seq("RED"))
    assert v["verdict"] == "REFUSED" and "guard" in v["reason"].lower()
    assert sha(repo, "main") == before
    assert v["guard_pre"]["verdict"] == "RED"


def test_guard_red_post_merge_rolls_back_exactly(home, repo):
    before = sha(repo, "main")
    v = mb.run_merge(GRANT, repo, BRANCH, guard_runner=guard_seq("GREEN", "RED"))
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
    v = mb.run_merge(GRANT, repo, BRANCH, guard_runner=green)
    assert v["verdict"] == "FAIL" and "merge failed" in v["reason"]
    assert sha(repo, "main") == before
    assert _run(repo, "git", "status", "--porcelain").stdout.strip() == ""


# ---------- the happy path ----------

def test_merged_happy_path(home, repo):
    before = sha(repo, "main")
    v = mb.run_merge("conserta o bug. pode mergear", repo, BRANCH,
                     guard_runner=green)
    assert v["verdict"] == "MERGED"
    assert v["target_sha_before"] == before
    assert v["merge_sha"] == sha(repo, "main") != before
    # --no-ff: the merge commit has two parents
    parents = _run(repo, "git", "rev-list", "--parents", "-1", "HEAD").stdout.split()
    assert len(parents) == 3
    # the authorization is permanent git history
    msg = _run(repo, "git", "log", "-1", "--format=%s").stdout
    assert "pode mergear" in msg
    # repo ends on the target, clean
    cur = _run(repo, "git", "branch", "--show-current").stdout.strip()
    assert cur == "main"
    # audit line carries the quote and both guard hashes
    line = json.loads((home / "merges.jsonl").read_text().splitlines()[-1])
    assert line["verdict"] == "MERGED"
    assert line["grant_message"] == "conserta o bug. pode mergear"
    assert line["grant"]["phrase"] == "pode mergear"
    assert line["guard_pre"]["sha256"] and line["guard_post"]["sha256"]


def test_merge_verdict_written_to_run_jsonl(home, repo, tmp_path):
    jsonl = tmp_path / "run.jsonl"
    v = mb.run_merge(GRANT, repo, BRANCH, guard_runner=green, jsonl=jsonl)
    assert v["verdict"] == "MERGED"
    last = json.loads(jsonl.read_text().splitlines()[-1])
    assert last["_type"] == "MergeVerdict" and last["verdict"] == "MERGED"


def test_pre_merge_guard_runs_on_branch_tip_worktree(home, repo):
    seen = []

    def spy(path):
        seen.append(Path(path))
        return {"verdict": "GREEN", "checks": []}
    v = mb.run_merge(GRANT, repo, BRANCH, guard_runner=spy)
    assert v["verdict"] == "MERGED"
    # first call = temp worktree (not the repo), second = the repo post-merge
    assert seen[0] != repo and str(seen[0]).startswith(str(mb.WORKTREES_DIR))
    assert seen[1] == repo
    # temp worktree cleaned up
    assert not seen[0].exists()


def test_crash_still_audits_terminal_verdict(home, repo):
    def boom(path):
        raise RuntimeError("guard exploded")
    v = mb.run_merge(GRANT, repo, BRANCH, guard_runner=boom)
    assert v["verdict"] == "FAIL" and "guard exploded" in v["reason"]
    line = json.loads((home / "merges.jsonl").read_text().splitlines()[-1])
    assert line["verdict"] == "FAIL"


# ---------- provenance (anti-fabrication: the grant must exist in the
# gateway's own store, which the brain cannot write) ----------

def _make_store(path, rows):
    """rows: list of (source, role, content, ts)."""
    import sqlite3
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, source TEXT)")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id INT,"
                " role TEXT, content TEXT, timestamp REAL)")
    for i, (source, role, content, ts) in enumerate(rows, 1):
        con.execute("INSERT INTO sessions VALUES (?, ?)", (i, source))
        con.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
                    (i, i, role, content, ts))
    con.commit()
    con.close()


@pytest.fixture()
def store(home, monkeypatch):
    db = home / "state.db"

    def _fill(rows):
        _make_store(db, rows)
        monkeypatch.setattr(mb, "HERMES_STATE_DB", db)
        return db
    return _fill


def test_provenance_skipped_when_no_store(home, repo, monkeypatch):
    monkeypatch.setattr(mb, "HERMES_STATE_DB", home / "missing.db")
    v = mb.run_merge(GRANT, repo, BRANCH, guard_runner=green)
    assert v["verdict"] == "MERGED"


def test_provenance_passes_on_real_recent_grant(home, repo, store):
    import time as _t
    store([("telegram", "user", "fix the bug.\nmerge allowed", _t.time() - 60)])
    v = mb.run_merge(GRANT, repo, BRANCH, guard_runner=green)
    assert v["verdict"] == "MERGED"


def test_provenance_refuses_fabricated_grant(home, repo, store):
    import time as _t
    store([("telegram", "user", "how is the branch looking?", _t.time() - 60)])
    v = mb.run_merge(GRANT, repo, BRANCH, guard_runner=green)
    assert v["verdict"] == "REFUSED" and "provenance" in v["reason"]


def test_provenance_negated_store_never_grants(home, repo, store):
    # The owner really wrote the NEGATION; a fabricating brain relays the clean
    # phrase. Substring provenance would pass ("não pode mergear" contains
    # "pode mergear") — parse_grant on the STORED text must refuse instead.
    import time as _t
    store([("telegram", "user", "não pode mergear", _t.time() - 60)])
    v = mb.run_merge("pode mergear", repo, BRANCH, guard_runner=green)
    assert v["verdict"] == "REFUSED" and "provenance" in v["reason"]


def test_provenance_stale_grant_refuses(home, repo, store):
    import time as _t
    store([("telegram", "user", "merge allowed", _t.time() - 3 * 24 * 3600)])
    v = mb.run_merge(GRANT, repo, BRANCH, guard_runner=green)
    assert v["verdict"] == "REFUSED" and "provenance" in v["reason"]


def test_provenance_grant_for_other_branch_refuses(home, repo, store):
    import time as _t
    store([("telegram", "user", "merge allowed agent/other-999999",
            _t.time() - 60)])
    v = mb.run_merge(GRANT, repo, BRANCH, guard_runner=green)
    assert v["verdict"] == "REFUSED" and "provenance" in v["reason"]


def test_provenance_ignores_non_telegram_and_non_user_rows(home, repo, store):
    import time as _t
    store([
        ("cron", "user", "merge allowed", _t.time() - 60),
        ("telegram", "assistant", "merge allowed", _t.time() - 60),
    ])
    v = mb.run_merge(GRANT, repo, BRANCH, guard_runner=green)
    assert v["verdict"] == "REFUSED" and "provenance" in v["reason"]


# ---------- CLI entry (main): env asserts, lock, exit codes ----------

def _stub_guard(home):
    (home / "golden_guard.py").write_text(
        "import json\nprint(json.dumps({'verdict': 'GREEN', 'checks': []})"
        ".replace(\"'\", '\"'))\n")


def test_main_merges_and_exits_zero(home, repo, monkeypatch, capsys):
    _stub_guard(home)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["merge_branch.py", "--repo", str(repo),
                                      "--branch", BRANCH, "--grant-message", GRANT])
    rc = mb.main()
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["verdict"] == "MERGED"


def test_main_refusal_exits_two(home, repo, monkeypatch, capsys):
    _stub_guard(home)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["merge_branch.py", "--repo", str(repo),
                                      "--branch", BRANCH,
                                      "--grant-message", "não pode mergear"])
    rc = mb.main()
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["verdict"] == "REFUSED"


def test_main_metered_key_refuses(home, repo, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-nope")
    monkeypatch.setattr(sys, "argv", ["merge_branch.py", "--repo", str(repo),
                                      "--branch", BRANCH, "--grant-message", GRANT])
    rc = mb.main()
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and "ANTHROPIC_API_KEY" in out["reason"]


def test_main_busy_when_lock_held(home, repo, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fh = (home / "delegate.lock").open("w")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(sys, "argv", ["merge_branch.py", "--repo", str(repo),
                                      "--branch", BRANCH, "--grant-message", GRANT])
    rc = mb.main()
    out = json.loads(capsys.readouterr().out)
    assert rc == 3 and out["verdict"] == "BUSY"
    fcntl.flock(fh, fcntl.LOCK_UN)
    fh.close()


def test_main_not_a_repo_exits_three(home, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["merge_branch.py", "--repo",
                                      str(tmp_path / "nope"),
                                      "--branch", BRANCH, "--grant-message", GRANT])
    rc = mb.main()
    out = json.loads(capsys.readouterr().out)
    assert rc == 3 and "not a git repo" in out["reason"]
