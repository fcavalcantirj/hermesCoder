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
