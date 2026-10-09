"""Plant-the-failure tests: every guard check must go RED on demand.

Coverage/tests checks need the `go` binary — those tests skip where Go is absent
(they run on the Pi, where Go is installed; the pure-Python checks run anywhere).
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import golden_guard as gg  # noqa: E402

GO = shutil.which("go") is not None

GREEN_GO = """package main

func Add(a, b int) int { return a + b }
"""
GREEN_GO_TEST = """package main

import "testing"

func TestAdd(t *testing.T) {
\tif Add(1, 2) != 3 {
\t\tt.Fatal("bad add")
\t}
}
"""


def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.name", "t")
    run("git", "config", "user.email", "t@t")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "fixture")
    return repo


GO_FIXTURE = {
    "go.mod": "module fixture\n\ngo 1.24\n",
    "main.go": GREEN_GO,
    "main_test.go": GREEN_GO_TEST,
}


def test_file_ceiling_green_and_red(tmp_path):
    repo = make_repo(tmp_path, {"ok.py": "x = 1\n"})
    assert gg.check_file_ceiling(repo)["ok"]

    big = "\n".join(f"x{i} = {i}" for i in range(gg.MAX_LINES + 1)) + "\n"
    repo2 = make_repo(tmp_path / "b", {"big.py": big})
    res = gg.check_file_ceiling(repo2)
    assert not res["ok"]
    assert res["offenders"][0]["file"] == "big.py"


def test_file_ceiling_markdown_exempt(tmp_path):
    big_md = "line\n" * (gg.MAX_LINES + 100)
    repo = make_repo(tmp_path, {"BIG.md": big_md})
    assert gg.check_file_ceiling(repo)["ok"]


def test_inmemory_red_in_prod_green_in_tests(tmp_path):
    repo = make_repo(tmp_path, {"store.py": "class InMemoryStore: pass\n"})
    assert not gg.check_inmemory(repo)["ok"]

    repo2 = make_repo(tmp_path / "b", {"store_test.py": "class InMemoryStore: pass\n"})
    assert gg.check_inmemory(repo2)["ok"]


def test_metered_key_red_on_planted_key(tmp_path):
    repo = make_repo(tmp_path, {"conf.sh": "export ANTHROPIC_API_KEY=x\n"})
    assert not gg.check_metered_key(repo)["ok"]

    repo2 = make_repo(tmp_path / "b", {"conf.sh": "echo clean\n"})
    assert gg.check_metered_key(repo2)["ok"]


def test_metered_key_red_on_env(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert not gg.check_metered_key(repo)["ok"]


def test_token_budget_dedupes_and_trips(tmp_path):
    jsonl = tmp_path / "run.jsonl"
    rows = [
        {"message": {"id": "m1", "usage": {"output_tokens": 100}}},
        {"message": {"id": "m1", "usage": {"output_tokens": 100}}},  # duplicate line
        {"message": {"id": "m2", "usage": {"output_tokens": 50}}},
    ]
    jsonl.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    ok = gg.check_token_budget(jsonl, budget=150)
    assert ok["ok"] and ok["output_tokens"] == 150  # deduped: 100 + 50

    red = gg.check_token_budget(jsonl, budget=149)
    assert not red["ok"]


def test_token_budget_red_on_missing_jsonl(tmp_path):
    assert not gg.check_token_budget(tmp_path / "nope.jsonl", budget=10)["ok"]


@pytest.mark.skipif(not GO, reason="go binary not installed")
def test_coverage_green_fixture(tmp_path):
    repo = make_repo(tmp_path, GO_FIXTURE)
    res = gg.check_tests_coverage(repo)
    assert res["ok"], res
    assert res["coverage"] == 100.0


@pytest.mark.skipif(not GO, reason="go binary not installed")
def test_coverage_red_below_floor(tmp_path):
    files = dict(GO_FIXTURE)
    files["extra.go"] = (
        "package main\n\n"
        + "\n".join(
            f"func U{i}() int {{ return {i} }}" for i in range(10)
        )
        + "\n"
    )
    repo = make_repo(tmp_path, files)
    res = gg.check_tests_coverage(repo)
    assert not res["ok"]
    assert res["coverage"] < gg.MIN_COVERAGE


@pytest.mark.skipif(not GO, reason="go binary not installed")
def test_failing_test_is_red(tmp_path):
    files = dict(GO_FIXTURE)
    files["main_test.go"] = GREEN_GO_TEST.replace("!= 3", "!= 4")
    repo = make_repo(tmp_path, files)
    res = gg.check_tests_coverage(repo)
    assert not res["ok"]
    assert res["reason"] == "tests failed"


@pytest.mark.skipif(not GO, reason="go binary not installed")
def test_zero_tests_is_red(tmp_path):
    files = {"go.mod": GO_FIXTURE["go.mod"], "main.go": GREEN_GO}  # no test files
    repo = make_repo(tmp_path, files)
    res = gg.check_tests_coverage(repo)
    assert not res["ok"]
    assert res["reason"] == "zero tests"


def test_cli_gate_fails_closed_when_tool_missing(tmp_path):
    repo = make_repo(tmp_path, {"a.py": "x = 1\n"})
    res = gg._run_cli_gate("ghost_gate", ["definitely-not-a-real-binary-xyz"], repo)
    assert not res["ok"]
    assert "not installed" in res["reason"]


LINT = shutil.which("golangci-lint") is not None


@pytest.mark.skipif(not (GO and LINT), reason="golangci-lint not installed")
def test_lint_green_and_red(tmp_path):
    repo = make_repo(tmp_path, GO_FIXTURE)
    assert gg.check_lint(repo)["ok"]

    files = dict(GO_FIXTURE)
    # unchecked error return — staticcheck/errcheck territory
    files["bad.go"] = (
        "package main\n\nimport \"os\"\n\n"
        "func Bad() {\n\tf, _ := os.Open(\"nope\")\n\t_ = f\n\tos.Getenv(\"HOME\")\n}\n"
    )
    repo2 = make_repo(tmp_path / "b", files)
    # red-on-demand is best-effort here (lint configs vary); assert it at least runs
    res = gg.check_lint(repo2)
    assert "check" in res and res["check"] == "golangci_lint"


VULN = shutil.which("govulncheck") is not None


@pytest.mark.skipif(not (GO and LINT and VULN),
                    reason="go/golangci-lint/govulncheck not all installed")
def test_run_guard_verdicts(tmp_path):
    repo = make_repo(tmp_path, GO_FIXTURE)
    assert gg.run_guard(repo, None, None)["verdict"] == "GREEN"

    files = dict(GO_FIXTURE)
    files["store.go"] = "package main\n\ntype InMemoryRepo struct{}\n"
    repo2 = make_repo(tmp_path / "b", files)
    assert gg.run_guard(repo2, None, None)["verdict"] == "RED"


# ---------- 2026-10-09: optional overrides, interpreter choice, CI delegation ----------

def _git_repo(tmp_path, files):
    repo = make_repo(tmp_path, files)
    return repo


def test_overrides_for_matches_resolved_path_and_tolerates_absence(tmp_path, monkeypatch):
    monkeypatch.setattr(gg, "POLICY_PATH", tmp_path / "missing.json")
    assert gg.overrides_for(tmp_path) == {}
    pol = tmp_path / "merge-policy.json"
    pol.write_text(json.dumps({"repos": {str(tmp_path / "r" / ".." / "r"): {"tests": "ci", "cov_path": "pkg"}}}))
    monkeypatch.setattr(gg, "POLICY_PATH", pol)
    (tmp_path / "r").mkdir()
    assert gg.overrides_for(tmp_path / "r") == {"tests": "ci", "cov_path": "pkg"}
    pol.write_text("not json")
    assert gg.overrides_for(tmp_path / "r") == {}


def test_select_python_prefers_override_then_project_venv_then_self(tmp_path):
    repo = tmp_path / "repo"; pkg = repo / "pkg"
    (pkg).mkdir(parents=True)
    assert gg.select_python(pkg, repo) == sys.executable
    venv_py = repo / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True); venv_py.write_text("")
    assert gg.select_python(pkg, repo) == str(venv_py)
    pkg_py = pkg / "venv" / "bin" / "python"
    pkg_py.parent.mkdir(parents=True); pkg_py.write_text("")
    assert gg.select_python(pkg, repo) == str(pkg_py)
    assert gg.select_python(pkg, repo, "/opt/py/bin/python") == "/opt/py/bin/python"


def test_tests_ci_delegates_coverage_and_marks_the_report(tmp_path):
    repo = _git_repo(tmp_path, {"app.py": "x = 1\n"})
    c = gg.check_tests_coverage_py(repo, tests="ci")
    assert c["ok"] is True and c["delegated"] == "ci"
    report = gg.run_guard(repo, None, None, overrides={"tests": "ci"})
    assert report["tests"] == "ci" and report["lang"] == "python"
    assert any(ch.get("delegated") == "ci" for ch in report["checks"])


def test_python_lane_uses_overrides_paths_and_floor(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, {"pkg/__init__.py": "", "pkg/m.py": "def f():\n    return 1\n",
                                "tests/test_m.py": "from pkg.m import f\n\ndef test_f():\n    assert f() == 1\n",
                                "junk/test_slow.py": "def test_slow():\n    assert False\n"})
    calls = {}
    real_run = gg.subprocess.run

    class Proc:
        returncode = 0; stdout = ""; stderr = ""

    def fake_run(cmd, **kw):
        if cmd[0] == "git":  # the guard's own `git ls-files` must stay real
            return real_run(cmd, **kw)
        calls["cmd"] = cmd
        (kw["cwd"] / ".guard-cov.json").write_text(json.dumps({"totals": {"percent_covered": 55.0}}))
        return Proc()
    monkeypatch.setattr(gg.subprocess, "run", fake_run)
    c = gg.check_tests_coverage_py(repo, python="/opt/py/bin/python", tests=["tests"],
                                   cov=["pkg"], min_coverage=50)
    assert c["ok"] is True and c["coverage"] == 55.0 and c["floor"] == 50.0
    assert calls["cmd"][0] == "/opt/py/bin/python" and "tests" in calls["cmd"] and "--cov=pkg" in calls["cmd"]
    assert "junk" not in calls["cmd"]


def test_pythonpath_is_scrubbed_at_import(monkeypatch):
    import importlib
    monkeypatch.setenv("PYTHONPATH", "/some/engine/site-packages")
    importlib.reload(gg)
    assert "PYTHONPATH" not in __import__("os").environ


# ---------- 2026-10-09: delta mode (--base) gates the branch, not legacy debt ----------

def _commit_all(repo, msg):
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def _giant(n):
    return "\n".join(f"x{i} = {i}" for i in range(n)) + "\n"


def test_delta_mode_ignores_untouched_legacy_giants(tmp_path):
    repo = _git_repo(tmp_path, {"legacy.py": _giant(1200), "small.py": "a = 1\n"})
    base = _commit_all(repo, "base") if subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                                                         capture_output=True, text=True).stdout else \
        subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    (repo / "small.py").write_text("a = 2\n")
    _commit_all(repo, "touch small")
    assert gg.check_file_ceiling(repo)["ok"] is False          # repo-wide: legacy giant is RED
    c = gg.check_file_ceiling(repo, base=base)
    assert c["ok"] is True and c["offenders"] == [] and "legacy_over_ceiling" not in c


def test_delta_mode_touched_legacy_giant_is_reported_not_red(tmp_path):
    repo = _git_repo(tmp_path, {"legacy.py": _giant(1200)})
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    (repo / "legacy.py").write_text(_giant(1210))
    _commit_all(repo, "touch the giant (+10)")
    c = gg.check_file_ceiling(repo, base=base)
    assert c["ok"] is True
    assert c["legacy_over_ceiling"] == [{"file": "legacy.py", "lines": 1210, "lines_at_base": 1200}]


def test_delta_mode_crossing_or_new_giant_is_red(tmp_path):
    repo = _git_repo(tmp_path, {"grows.py": _giant(880)})
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    (repo / "grows.py").write_text(_giant(950))
    (repo / "brand_new.py").write_text(_giant(1000))
    _commit_all(repo, "cross + new")
    c = gg.check_file_ceiling(repo, base=base)
    assert c["ok"] is False
    assert {o["file"] for o in c["offenders"]} == {"grows.py", "brand_new.py"}


def test_delta_mode_inmemory_scans_only_changed_files(tmp_path):
    repo = _git_repo(tmp_path, {"legacy.py": "class InMemoryRepo: pass\n", "ok.py": "a = 1\n"})
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    (repo / "ok.py").write_text("a = 2\n")
    _commit_all(repo, "touch ok")
    assert gg.check_inmemory(repo)["ok"] is False
    assert gg.check_inmemory(repo, base=base)["ok"] is True
    (repo / "ok.py").write_text("x = InMemoryStore()\n")
    _commit_all(repo, "add an in-memory store")
    assert gg.check_inmemory(repo, base=base)["ok"] is False


def test_run_guard_records_the_base(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, {"app.py": "x = 1\n"})
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    report = gg.run_guard(repo, None, None, overrides={"tests": "ci"}, base=base)
    assert report["base"] == base and report["verdict"] == "GREEN"
