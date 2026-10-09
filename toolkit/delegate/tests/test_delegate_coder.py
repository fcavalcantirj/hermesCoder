"""Delegate tests — SDK and guard are injected fakes; git is real (tmp repos).

Plant-the-failure coverage: crash still writes the terminal verdict line;
main-moved and left-branch both FAIL; guard RED consumes exactly one fix round.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import delegate_coder as dc  # noqa: E402


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "hermescoder-home"
    (h / "runs").mkdir(parents=True)
    (h / "GOLDEN-RULES.md").write_text("# GOLDEN RULES (test fixture)\n")
    monkeypatch.setattr(dc, "HOME", h)
    monkeypatch.setattr(dc, "RUNS_DIR", h / "runs")
    monkeypatch.setattr(dc, "RULES_PATH", h / "GOLDEN-RULES.md")
    monkeypatch.setattr(dc, "GUARD_PATH", h / "golden_guard.py")
    return h


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    run = lambda *a: subprocess.run(a, cwd=r, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.name", "t")
    run("git", "config", "user.email", "t@t")
    (r / "README.md").write_text("fixture\n")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "init")
    return r


def fake_sdk(events=None):
    def _run(prompt, fields, jsonl_path):
        evs = events or [{"message": {"id": "m1", "usage": {"output_tokens": 10}}}]
        with jsonl_path.open("a") as fh:
            for ev in evs:
                fh.write(json.dumps(ev) + "\n")
        return evs
    return _run


def guard_seq(*verdicts):
    calls = {"n": 0}
    def _run(repo, jsonl, budget):
        v = verdicts[min(calls["n"], len(verdicts) - 1)]
        calls["n"] += 1
        return {"verdict": v, "checks": []}
    return _run


def test_slugify_and_branch():
    assert dc.slugify("Add /v1/tasks POST endpoint!") == "add-v1-tasks-post-endpoint"
    assert dc.branch_name("Fix bug", "20260715-1200") == "agent/fix-bug-20260715-1200"
    assert dc.slugify("///") == "task"


def test_options_fields_are_scoped(home, repo):
    f = dc.build_options_fields(repo, "RULES")
    assert f["permission_mode"] == "bypassPermissions"
    assert f["allowed_tools"] == ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "LSP"]
    assert f["setting_sources"] == ["user"]  # SDK is hermetic by default: no
    # user settings → no plugins → no LSP. Pinned so nobody "cleans it up".
    assert f["mcp_servers"] == {}
    assert f["system_prompt"]["append"] == "RULES"
    assert f["system_prompt"]["preset"] == "claude_code"


def test_go_bin_on_path():
    # gopls (LSP) + golangci-lint/govulncheck live in ~/go/bin; the gateway
    # unit's PATH lacks it, so the delegate must extend PATH itself.
    import os as _os
    assert str(Path.home() / "go" / "bin") in _os.environ.get("PATH", "")


def test_usage_dedupes_by_message_id():
    events = [
        {"message": {"id": "m1", "usage": {"output_tokens": 100}}},
        {"message": {"id": "m1", "usage": {"output_tokens": 100}}},
        {"message": {"id": "m2", "usage": {"output_tokens": 5,
                                           "cache_read_input_tokens": 7}}},
    ]
    u = dc.usage_from_events(events)
    assert u["api_calls"] == 2
    assert u["output_tokens"] == 105
    assert u["cache_read_input_tokens"] == 7


def test_usage_prefers_result_message():
    # SDK-dataclass shape: AssistantMessages carry no usage; ResultMessage is
    # authoritative (the all-zeros bug from the first live E2E run, 2026-07-15).
    events = [
        {"_type": "AssistantMessage", "content": [{"text": "hi"}]},
        {"_type": "ResultMessage", "num_turns": 70, "total_cost_usd": 1.48,
         "duration_api_ms": 400000,
         "usage": {"output_tokens": 21250,
                   "cache_creation_input_tokens": 45590,
                   "cache_read_input_tokens": 2966635}},
    ]
    u = dc.usage_from_events(events)
    assert u["api_calls"] == 70
    assert u["output_tokens"] == 21250
    assert u["cache_read_input_tokens"] == 2966635
    assert u["total_cost_usd"] == 1.48


def test_happy_path_pass(home, repo):
    v = dc.run_task("add endpoint", repo,
                    sdk_runner=fake_sdk(), guard_runner=guard_seq("GREEN"))
    assert v["verdict"] == "PASS"
    assert v["branch"].startswith("agent/add-endpoint-")
    assert v["fix_rounds"] == 0
    assert not v["main_moved"]
    # terminal verdict line exists in the transcript
    last = json.loads(Path(v["jsonl"]).read_text().splitlines()[-1])
    assert last["_type"] == "DelegateVerdict" and last["verdict"] == "PASS"


def test_guard_red_then_green_uses_one_fix_round(home, repo):
    v = dc.run_task("t", repo, sdk_runner=fake_sdk(),
                    guard_runner=guard_seq("RED", "GREEN"))
    assert v["verdict"] == "PASS"
    assert v["fix_rounds"] == 1


def test_guard_stays_red_fails_after_bounded_rounds(home, repo):
    v = dc.run_task("t", repo, sdk_runner=fake_sdk(),
                    guard_runner=guard_seq("RED", "RED"))
    assert v["verdict"] == "FAIL"
    assert v["fix_rounds"] == dc.MAX_FIX_ROUNDS
    assert "guard RED" in v["reason"]


def test_main_moved_is_fail(home, repo):
    def evil_sdk(prompt, fields, jsonl_path):
        subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
        (repo / "hack.txt").write_text("oops\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "evil"], cwd=repo, check=True)
        jsonl_path.touch()
        return []
    v = dc.run_task("t", repo, sdk_runner=evil_sdk, guard_runner=guard_seq("GREEN"))
    assert v["verdict"] == "FAIL"
    assert v["main_moved"]


def test_left_branch_is_fail(home, repo):
    def wanderer_sdk(prompt, fields, jsonl_path):
        subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
        jsonl_path.touch()
        return []
    v = dc.run_task("t", repo, sdk_runner=wanderer_sdk, guard_runner=guard_seq("GREEN"))
    assert v["verdict"] == "FAIL"
    assert "left branch" in v["reason"]


def test_evidence_pack_written(home, repo):
    v = dc.run_task("t", repo, sdk_runner=fake_sdk(), guard_runner=guard_seq("GREEN"))
    assert v["verdict"] == "PASS"
    ev = json.loads((repo / ".hermesCoder" / "guard-report.json").read_text())
    assert ev["verdict"] == "GREEN"
    assert (repo / ".hermesCoder" / "diff-vs-main.patch").exists()


def test_wall_clock_kill(monkeypatch):
    import asyncio as aio

    async def sleeper(prompt, fields, jsonl_path):
        await aio.sleep(5)

    monkeypatch.setattr(dc, "_run_sdk_async", sleeper)
    monkeypatch.setattr(dc, "MAX_WALL_SECONDS", 0.2)
    with pytest.raises(Exception):  # TimeoutError surfaces as run FAIL upstream
        dc.run_sdk("p", {}, Path("/tmp/x.jsonl"))


def test_restore_subscription_env(home, monkeypatch):
    # the brain's Bash tool strips the token; delegate must self-restore from claude.env
    (home / "claude.env").write_text('CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-test123"\n')
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    dc.restore_subscription_env()
    import os as _os
    assert _os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-test123"
    assert "CLAUDECODE" not in _os.environ
    assert "CLAUDE_CODE_ENTRYPOINT" not in _os.environ


def test_restore_never_overwrites_existing_token(home, monkeypatch):
    (home / "claude.env").write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-fromfile\n")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fromenv")
    dc.restore_subscription_env()
    import os as _os
    assert _os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-fromenv"


def test_crash_still_writes_terminal_verdict(home, repo):
    def boom(prompt, fields, jsonl_path):
        jsonl_path.touch()
        raise RuntimeError("sdk exploded")
    v = dc.run_task("t", repo, sdk_runner=boom, guard_runner=guard_seq("GREEN"))
    assert v["verdict"] == "FAIL"
    assert "sdk exploded" in v["reason"]
    last = json.loads(Path(v["jsonl"]).read_text().splitlines()[-1])
    assert last["_type"] == "DelegateVerdict" and last["verdict"] == "FAIL"


# ---------- auto-land on PASS (the owner's 2026-08-18 directive); --no-merge keeps a draft ----------

def test_pass_auto_lands_without_any_grant_message(home, repo):
    calls = []
    v = dc.run_task("t", repo, sdk_runner=fake_sdk(), guard_runner=guard_seq("GREEN"),
                    merge_runner=lambda *a, **k: calls.append(a) or {"verdict": "MERGED"})
    assert v["verdict"] == "PASS"
    assert v["merge"]["verdict"] == "MERGED"
    assert len(calls) == 1 and calls[0][0] == ""  # empty message, never None


def test_no_merge_flag_keeps_the_draft_on_its_branch(home, repo):
    calls = []
    v = dc.run_task("t", repo, sdk_runner=fake_sdk(), guard_runner=guard_seq("GREEN"),
                    merge_runner=lambda *a, **k: calls.append(a) or {}, auto_merge=False)
    assert v["verdict"] == "PASS"
    assert "merge" not in v
    assert calls == []


def test_grant_on_pass_invokes_merge_with_verbatim_message(home, repo):
    seen = {}

    def fake_merge(message, repo_, branch, jsonl, ship=True):
        seen.update(message=message, repo=repo_, branch=branch, jsonl=jsonl, ship=ship)
        return {"verdict": "MERGED", "merge_sha": "abc123"}
    msg = "conserta o bug do login.\npode mergear"
    v = dc.run_task("t", repo, sdk_runner=fake_sdk(), guard_runner=guard_seq("GREEN"),
                    grant_message=msg, merge_runner=fake_merge)
    assert v["verdict"] == "PASS"
    assert v["merge"]["verdict"] == "MERGED"
    assert seen["message"] == msg  # verbatim — never rewritten, never parsed here
    assert seen["branch"] == v["branch"]
    assert str(seen["jsonl"]) == v["jsonl"]
    # the merge result rides the terminal verdict line too
    last = json.loads(Path(v["jsonl"]).read_text().splitlines()[-1])
    assert last["merge"]["verdict"] == "MERGED"


def test_grant_on_fail_never_invokes_merge(home, repo):
    calls = []
    v = dc.run_task("t", repo, sdk_runner=fake_sdk(),
                    guard_runner=guard_seq("RED", "RED"),
                    grant_message="merge allowed",
                    merge_runner=lambda *a, **k: calls.append(a) or {})
    assert v["verdict"] == "FAIL"
    assert calls == []
    assert "merge" not in v


def test_merge_crash_does_not_eat_task_verdict(home, repo):
    def boom(*a, **k):
        raise RuntimeError("merge exploded")
    v = dc.run_task("t", repo, sdk_runner=fake_sdk(), guard_runner=guard_seq("GREEN"),
                    grant_message="merge allowed", merge_runner=boom)
    assert v["verdict"] == "PASS"
    assert v["merge"]["verdict"] == "FAIL"
    assert "merge exploded" in v["merge"]["reason"]


# ---------- bounded-turn guard (2026-08-08: the 600s solvr-fix death) ----------
# A foreground delegate inside a hermes gateway turn ALWAYS dies at the 600s
# watchdog. main() must refuse (with the exact --background relaunch) when the
# Claude Code nesting markers are present, honor --foreground as the override,
# and keep --background's fail-fast checks (creds) SYNCHRONOUS — a bad launch
# must report immediately, not from inside a daemon log. The fork itself is
# deliberately untested here (double-fork under pytest is unreliable); the
# guard/ordering contract is what regressions would break.

def _main_with_argv(monkeypatch, capsys, argv, env=None):
    monkeypatch.setattr(sys, "argv", ["delegate_coder.py", *argv])
    for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "HERMES_AGENT", "AI_AGENT",
                "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DELEGATE_NO_REEXEC", "1")
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    rc = dc.main()
    out = json.loads(capsys.readouterr().out)
    return rc, out


def test_bounded_turn_refuses_foreground(home, repo, monkeypatch, capsys):
    rc, out = _main_with_argv(monkeypatch, capsys,
                              ["--task", "t", "--repo", str(repo)],
                              env={"CLAUDECODE": "1"})
    assert rc == 3
    assert out["verdict"] == "REFUSED_FOREGROUND"
    assert "--background" in out["relaunch"]


def test_entrypoint_marker_also_refuses(home, repo, monkeypatch, capsys):
    rc, out = _main_with_argv(monkeypatch, capsys,
                              ["--task", "t", "--repo", str(repo)],
                              env={"CLAUDE_CODE_ENTRYPOINT": "cli"})
    assert rc == 3
    assert out["verdict"] == "REFUSED_FOREGROUND"


def test_foreground_flag_overrides_guard(home, repo, monkeypatch, capsys):
    # Past the guard, the credential check fires first (no claude.env in the
    # fixture HOME) — that FAIL proves the guard let it through.
    rc, out = _main_with_argv(monkeypatch, capsys,
                              ["--task", "t", "--repo", str(repo), "--foreground"],
                              env={"CLAUDECODE": "1"})
    assert rc == 2
    assert "CLAUDE_CODE_OAUTH_TOKEN" in out["reason"]


def test_unbounded_env_never_refuses(home, repo, monkeypatch, capsys):
    rc, out = _main_with_argv(monkeypatch, capsys,
                              ["--task", "t", "--repo", str(repo)])
    assert rc == 2
    assert out["verdict"] == "FAIL"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in out["reason"]


def test_background_failfast_checks_stay_synchronous(home, repo, monkeypatch, capsys):
    # --background with missing creds must FAIL right here, synchronously —
    # never LAUNCHED, never a fork whose only trace is a daemon log.
    rc, out = _main_with_argv(monkeypatch, capsys,
                              ["--task", "t", "--repo", str(repo), "--background"],
                              env={"CLAUDECODE": "1"})
    assert rc == 2
    assert out["verdict"] == "FAIL"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in out["reason"]


def test_refusal_relaunch_quotes_multiword_task(home, repo, monkeypatch, capsys):
    rc, out = _main_with_argv(monkeypatch, capsys,
                              ["--task", "fix the room-read auth bug",
                               "--repo", str(repo)],
                              env={"CLAUDECODE": "1"})
    assert rc == 3
    assert "'fix the room-read auth bug'" in out["relaunch"]


# ---------- 2026-10-09: Hermes lane hygiene ----------

def test_ship_flag_reaches_the_merge_runner(home, repo):
    seen = {}

    def fake_merge(message, repo_, branch, jsonl, ship=True):
        seen["ship"] = ship
        return {"verdict": "MERGED"}
    v = dc.run_task("t", repo, sdk_runner=fake_sdk(), guard_runner=guard_seq("GREEN"),
                    merge_runner=fake_merge, ship=False)
    assert v["verdict"] == "PASS" and seen["ship"] is False


def test_hermes_markers_refuse_foreground_with_hermes_relaunch(home, repo, monkeypatch, capsys):
    rc, out = _main_with_argv(monkeypatch, capsys,
                              ["--task", "fix it", "--repo", str(repo)],
                              env={"HERMES_AGENT": "true", "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"})
    assert rc == 3 and out["verdict"] == "REFUSED_FOREGROUND"
    assert out["relaunch"].startswith("terminal(command=") and "--foreground" in out["relaunch"]
    assert "background=true, notify_on_complete=true" in out["relaunch"]
    assert "by hand" in out["then"]


def test_ai_agent_marker_also_refuses(home, repo, monkeypatch, capsys):
    rc, out = _main_with_argv(monkeypatch, capsys,
                              ["--task", "fix it", "--repo", str(repo)],
                              env={"AI_AGENT": "hermes-agent", "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"})
    assert rc == 3 and out["verdict"] == "REFUSED_FOREGROUND"


def test_claude_code_marker_keeps_the_background_relaunch(home, repo, monkeypatch, capsys):
    rc, out = _main_with_argv(monkeypatch, capsys,
                              ["--task", "fix it", "--repo", str(repo)],
                              env={"CLAUDECODE": "1", "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"})
    assert rc == 3 and out["relaunch"].endswith("--background")


def test_message_alias_and_no_ship_parse(home, repo, monkeypatch, capsys):
    captured = {}

    def fake_run_task(task, repo_, budget, grant_message="", auto_merge=True, ship=True):
        captured.update(message=grant_message, auto_merge=auto_merge, ship=ship)
        return {"verdict": "PASS"}
    monkeypatch.setattr(dc, "run_task", fake_run_task)
    rc, out = _main_with_argv(monkeypatch, capsys,
                              ["--task", "t", "--repo", str(repo), "--message", "ship it",
                               "--no-ship", "--foreground"],
                              env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"})
    assert rc == 0 and captured == {"message": "ship it", "auto_merge": True, "ship": False}


def test_reexec_target_points_at_the_venv_when_elsewhere(home, monkeypatch):
    venv_py = home / "venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("#!/bin/sh\n")
    monkeypatch.setattr(dc, "VENV_PYTHON", venv_py)
    monkeypatch.delenv("DELEGATE_NO_REEXEC", raising=False)
    assert dc.reexec_target() == str(venv_py)
    monkeypatch.setattr(sys, "executable", str(venv_py))
    assert dc.reexec_target() is None


def test_reexec_target_none_without_venv_or_when_disabled(home, monkeypatch):
    monkeypatch.setattr(dc, "VENV_PYTHON", home / "venv" / "bin" / "python")
    monkeypatch.delenv("DELEGATE_NO_REEXEC", raising=False)
    assert dc.reexec_target() is None
    (home / "venv" / "bin").mkdir(parents=True)
    (home / "venv" / "bin" / "python").write_text("")
    monkeypatch.setenv("DELEGATE_NO_REEXEC", "1")
    assert dc.reexec_target() is None


def test_main_reexecs_into_the_venv(home, repo, monkeypatch, capsys):
    venv_py = home / "venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    monkeypatch.setattr(dc, "VENV_PYTHON", venv_py)
    seen = {}

    def fake_execv(path, argv):
        seen["path"], seen["argv"] = path, argv
        raise SystemExit(99)  # execv never returns
    monkeypatch.setattr(dc.os, "execv", fake_execv)
    monkeypatch.setattr(sys, "argv", ["delegate_coder.py", "--task", "t", "--repo", str(repo)])
    monkeypatch.delenv("DELEGATE_NO_REEXEC", raising=False)
    with pytest.raises(SystemExit):
        dc.main()
    assert seen["path"] == str(venv_py) and seen["argv"][0] == str(venv_py) and "--task" in seen["argv"]


def test_pythonpath_is_scrubbed_at_import(monkeypatch):
    import importlib
    monkeypatch.setenv("PYTHONPATH", "/some/engine/site-packages")
    importlib.reload(dc)
    assert "PYTHONPATH" not in __import__("os").environ
