#!/usr/bin/env python3
"""golden_guard.py — deterministic golden-rules gate for delegated coding runs.

Zero LLM. Exit code IS the verdict: 0 = GREEN, 2 = RED, 3 = usage error.
Checks (each independently reported in the JSON verdict on stdout):
  file_ceiling   — no tracked code file exceeds MAX_LINES (markdown, vendored
                   dirs exempt)
  inmemory_grep  — "InMemory" must not appear in non-test, non-vendored code
  tests_coverage — `go test ./...` green AND total coverage >= MIN_COVERAGE
                   (zero test files = RED before any coverage math)
  golangci_lint  — `golangci-lint run` exit code (missing binary fails closed)
  govulncheck    — `govulncheck ./...` exit code (missing binary fails closed)
  metered_key    — no ANTHROPIC_API_KEY / sk-ant-api string in authored code
                   (tests, docs and example templates are expected to name the
                   pattern to demonstrate or scan for it, not leak it)
  token_budget   — (only with --budget-tokens) run-JSONL output tokens within budget
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

MAX_LINES = 900
MIN_COVERAGE = 80.0
HOME = Path(os.environ.get("HERMESCODER_HOME", "~/.hermescoder")).expanduser()
POLICY_PATH = HOME / "merge-policy.json"
# Go-installed gate binaries (golangci-lint, govulncheck) live in ~/go/bin, which
# cron/subprocess PATHs usually lack — extend once at import.
os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + str(Path.home() / "go" / "bin")
CODE_SUFFIXES = {".go", ".py", ".js", ".ts", ".sh", ".c", ".h", ".rs", ".java"}
KEY_PATTERNS = (re.compile(r"ANTHROPIC_API_KEY\s*="), re.compile(r"sk-ant-api\w+"))
# Doc-like content whose whole point is to name a pattern (prose, historical
# diffs) rather than execute or store a value.
DOC_LIKE_SUFFIXES = {".md", ".diff"}
# Fully vendored trees — mirrored wholesale from an upstream fork (see
# scripts/revendor.sh; README.md "topology law: never author in hermes/").
# File size and internal patterns there are the upstream fork's call, not ours.
VENDOR_PREFIXES = ("hermes/",)
# Implementation files that must contain the literal pattern text in order to
# check for it, and are therefore not themselves an instance of the violation.
SELF_EXEMPT = {"toolkit/guard/golden_guard.py", "scripts/secrets-scan.sh"}


# The Hermes terminal tool exports the ENGINE's site-packages as PYTHONPATH into
# every command; under our interpreter that only pollutes the pytest run.
os.environ.pop("PYTHONPATH", None)


def overrides_for(key: Path) -> dict:
    """Optional per-repo overrides (merge-policy.json — not a gate): cov_path (monorepo
    package to gate), python (interpreter for the test run), tests ("ci" = the remote
    CI is the oracle, or a list of pytest paths), cov (list of --cov targets),
    min_coverage. Matched by resolved checkout path; the merge tool passes
    --overrides-key for its temp worktrees so they resolve the real repo's entry."""
    if not POLICY_PATH.is_file():
        return {}
    try:
        repos = json.loads(POLICY_PATH.read_text()).get("repos", {})
    except (json.JSONDecodeError, AttributeError):
        return {}
    rp = str(Path(key).expanduser().resolve())
    for k, cfg in repos.items():
        if str(Path(k).expanduser().resolve()) == rp and isinstance(cfg, dict):
            return cfg
    return {}


def _cov_path_from_policy(repo: Path) -> str | None:
    return overrides_for(repo).get("cov_path")


def select_python(workdir: Path, repo: Path, override: str | None = None) -> str:
    """Interpreter for the test run: an explicit override, else the project's own venv
    (.venv/ or venv/ in the gated dir, then in the repo), else the guard's interpreter."""
    if override:
        return str(Path(override).expanduser())
    for base in (workdir, repo):
        for name in (".venv", "venv"):
            cand = base / name / "bin" / "python"
            if cand.exists():
                return str(cand)
    return sys.executable


def _tracked_files(repo: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    return [repo / line for line in out.splitlines() if line.strip()]


def _is_vendored(rel: Path) -> bool:
    p = rel.as_posix()
    return any(p.startswith(prefix) for prefix in VENDOR_PREFIXES)


def _is_test_path(rel: Path) -> bool:
    """Filename or any containing directory names a test (tests may use
    in-memory doubles and must plant real-looking secrets to test redaction)."""
    return "test" in rel.name.lower() or any("test" in part.lower() for part in rel.parts[:-1])


def _out_of_cov(rel: Path, cov_path: str | None) -> bool:
    """In a monorepo, cov_path names the one package the guard gates (same
    signal coverage already uses). When set, the structural scans skip files
    outside it — otherwise an unrelated sibling subproject (e.g. tooling with a
    giant test file or its own .venv) fails the whole repo's merge."""
    if not cov_path:
        return False
    prefix = cov_path.strip("/") + "/"
    return not rel.as_posix().startswith(prefix)


def changed_since(repo: Path, base: str) -> set[str]:
    """Paths changed on this checkout since `base` (merge-base semantics: base...HEAD)."""
    out = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=repo,
                         capture_output=True, text=True, check=True).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def lines_at(repo: Path, base: str, rel: str) -> int | None:
    """Line count of `rel` at `base`, or None when the file did not exist there."""
    proc = subprocess.run(["git", "show", f"{base}:{rel}"], cwd=repo, capture_output=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.count(b"\n")


def check_file_ceiling(repo: Path, cov_path: str | None = None, base: str | None = None) -> dict:
    """RED when a tracked code file exceeds MAX_LINES. With `base` (the merge/delegate
    lane: gate the DELTA, not legacy debt) only files changed since base count, and a
    changed file that was ALREADY over the ceiling at base is reported as legacy, not RED
    — a branch may touch a giant to fix a bug; it may not create one or push one over."""
    offenders, legacy = [], []
    changed = changed_since(repo, base) if base else None
    for f in _tracked_files(repo):
        if f.suffix not in CODE_SUFFIXES or not f.is_file():
            continue
        rel = f.relative_to(repo)
        if _is_vendored(rel) or _out_of_cov(rel, cov_path):
            continue
        if changed is not None and rel.as_posix() not in changed:
            continue
        n = sum(1 for _ in f.open(errors="replace"))
        if n > MAX_LINES:
            entry = {"file": str(rel), "lines": n}
            was = lines_at(repo, base, rel.as_posix()) if base else None
            if was is not None and was > MAX_LINES:
                legacy.append({**entry, "lines_at_base": was})
            else:
                offenders.append(entry)
    return {"check": "file_ceiling", "ok": not offenders, "offenders": offenders,
            **({"legacy_over_ceiling": legacy} if legacy else {}),
            **({"base": base} if base else {})}


def check_inmemory(repo: Path, cov_path: str | None = None, base: str | None = None) -> dict:
    changed = changed_since(repo, base) if base else None
    offenders = []
    for f in _tracked_files(repo):
        if f.suffix not in CODE_SUFFIXES or not f.is_file():
            continue
        rel = f.relative_to(repo)
        if (_is_vendored(rel) or _is_test_path(rel)
                or rel.as_posix() in SELF_EXEMPT or _out_of_cov(rel, cov_path)
                or (changed is not None and rel.as_posix() not in changed)):
            continue
        for i, line in enumerate(f.open(errors="replace"), 1):
            if "InMemory" in line:
                offenders.append({"file": str(rel), "line": i})
    return {"check": "inmemory_grep", "ok": not offenders, "offenders": offenders}


def check_tests_coverage(repo: Path) -> dict:
    # Anti-cheat FIRST: an agent that deletes tests to "pass" the floor must go
    # RED explicitly ("zero tests = fail the build"), before any coverage math.
    if not any(f.name.endswith("_test.go") for f in _tracked_files(repo)):
        return {"check": "tests_coverage", "ok": False, "reason": "zero tests"}
    prof = repo / ".guard-cover.out"
    try:
        test = subprocess.run(
            ["go", "test", "./...", f"-coverprofile={prof}"],
            cwd=repo, capture_output=True, text=True, timeout=600,
        )
        if test.returncode != 0:
            return {"check": "tests_coverage", "ok": False,
                    "reason": "tests failed", "output": test.stdout[-2000:] + test.stderr[-500:]}
        func = subprocess.run(
            ["go", "tool", "cover", f"-func={prof}"],
            cwd=repo, capture_output=True, text=True, timeout=120,
        )
        m = re.search(r"total:\s+\(statements\)\s+([\d.]+)%", func.stdout)
        if not m:
            return {"check": "tests_coverage", "ok": False, "reason": "no total in cover -func"}
        pct = float(m.group(1))
        return {"check": "tests_coverage", "ok": pct >= MIN_COVERAGE,
                "coverage": pct, "floor": MIN_COVERAGE}
    finally:
        prof.unlink(missing_ok=True)


def check_metered_key(repo: Path) -> dict:
    offenders = []
    for f in _tracked_files(repo):
        if not f.is_file():
            continue
        rel = f.relative_to(repo)
        if (
            _is_test_path(rel)
            or rel.suffix in DOC_LIKE_SUFFIXES
            or rel.name.endswith(".example")
            or rel.as_posix() in SELF_EXEMPT
        ):
            continue
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for pat in KEY_PATTERNS:
            if pat.search(text):
                offenders.append({"file": str(rel), "pattern": pat.pattern})
    if os.environ.get("ANTHROPIC_API_KEY"):
        offenders.append({"env": "ANTHROPIC_API_KEY present in guard environment"})
    return {"check": "metered_key", "ok": not offenders, "offenders": offenders}


def check_token_budget(jsonl: Path, budget: int) -> dict:
    seen: dict[str, int] = {}
    total = 0
    if not jsonl.is_file():
        return {"check": "token_budget", "ok": False, "reason": f"run jsonl missing: {jsonl}"}
    with jsonl.open() as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = ev.get("message") or {}
            usage = msg.get("usage") or ev.get("usage") or {}
            out = usage.get("output_tokens")
            if not out:
                continue
            mid = msg.get("id") or f"line-{len(seen)}"
            if mid not in seen:  # dedupe: one API message spans multiple JSONL lines
                seen[mid] = out
    total = sum(seen.values())
    return {"check": "token_budget", "ok": total <= budget,
            "output_tokens": total, "budget": budget}


def _run_cli_gate(name: str, cmd: list[str], repo: Path, timeout: int = 600) -> dict:
    """A deterministic external gate: exit code is the verdict; a MISSING tool
    fails closed (reported, never silently skipped)."""
    import shutil as _shutil
    if _shutil.which(cmd[0]) is None:
        return {"check": name, "ok": False, "reason": f"{cmd[0]} not installed"}
    try:
        proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"check": name, "ok": False, "reason": f"timeout after {timeout}s"}
    return {"check": name, "ok": proc.returncode == 0,
            **({} if proc.returncode == 0 else
               {"output": (proc.stdout + proc.stderr)[-2000:]})}


def check_lint(repo: Path) -> dict:
    return _run_cli_gate("golangci_lint", ["golangci-lint", "run", "--timeout", "300s"], repo)


def check_vuln(repo: Path) -> dict:
    return _run_cli_gate("govulncheck", ["govulncheck", "./..."], repo)


def _detect_lang(repo: Path) -> str:
    """Go when a go.mod is present; otherwise Python when Python sources/markers
    exist. Unknown repos default to 'go' — the strictest, fail-closed path."""
    if (repo / "go.mod").exists():
        return "go"
    markers = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")
    if any((repo / m).exists() for m in markers) or any(
        f.suffix == ".py" for f in _tracked_files(repo)
    ):
        return "python"
    return "go"


def check_tests_coverage_py(repo: Path, cov_path: str | None = None, *,
                            python: str | None = None, tests=None,
                            cov: list | None = None,
                            min_coverage: float | None = None) -> dict:
    """Python analogue of check_tests_coverage: pytest must pass AND total
    statement coverage >= MIN_COVERAGE. Zero test files = RED before any
    coverage math (same anti-cheat as the Go path). Runs under the same
    interpreter that launched the guard (has pytest + pytest-cov).

    `cov_path` scopes the run to a sub-package of a monorepo: tests run with
    that dir as cwd and coverage is measured on its package(s) only. A `src/`
    layout is honoured — the package under src/ is the cov target and src/ is
    prepended to PYTHONPATH so the checkout under test (not an editable install
    pointing elsewhere, e.g. a merge worktree) is the code being measured."""
    floor = MIN_COVERAGE if min_coverage is None else float(min_coverage)
    if tests == "ci":
        # The remote CI gate is this repo's test oracle (suite exceeds the local
        # budget / needs its own environment); the merge tool forces the PR lane.
        return {"check": "tests_coverage", "ok": True, "delegated": "ci",
                "reason": "tests delegated to the remote CI required checks (overrides tests: ci)"}
    workdir = (repo / cov_path) if cov_path else repo
    if not workdir.is_dir():
        return {"check": "tests_coverage", "ok": False,
                "reason": f"cov_path does not exist: {cov_path}"}
    wd_resolved = workdir.resolve()
    if not any(
        f.suffix == ".py" and "test" in f.name.lower()
        and str(f.resolve()).startswith(str(wd_resolved))
        for f in _tracked_files(repo)
    ):
        return {"check": "tests_coverage", "ok": False, "reason": "zero tests"}
    src = workdir / "src"
    pkg_root = src if src.is_dir() else workdir
    pkgs = [
        d.name for d in pkg_root.iterdir()
        if d.is_dir() and (d / "__init__.py").exists() and "test" not in d.name.lower()
    ]
    cov_args = [f"--cov={p}" for p in (cov or pkgs)] or ["--cov=."]
    test_paths = [str(t) for t in tests] if isinstance(tests, list) else []
    py = select_python(workdir, repo, python)
    env = dict(os.environ)
    if src.is_dir():
        env["PYTHONPATH"] = str(src) + os.pathsep + env.get("PYTHONPATH", "")
    cov_json = workdir / ".guard-cov.json"
    try:
        # `-m "not hw"`: real-hardware tests NEVER run in the automated/merge
        # oracle — a merge must not physically deauth an AP or reset a board.
        # Every hardware test carries the `hw` marker; the read-only + driven
        # real-hardware tiers run separately on the Pi (per-rung [REAL] gate).
        try:
            test = subprocess.run(
                [py, "-m", "pytest", *cov_args, *test_paths, "-m", "not hw",
                 f"--cov-report=json:{cov_json}", "-q"],
                cwd=workdir, capture_output=True, text=True, timeout=600, env=env,
            )
        except subprocess.TimeoutExpired:
            # A hang must be a clean RED, never an uncaught crash: an uncaught
            # TimeoutExpired here aborts run_guard before it prints its JSON, so
            # the delegate sees empty stdout and reports "guard output
            # unparsable" instead of the real cause.
            return {"check": "tests_coverage", "ok": False,
                    "reason": "tests timed out (600s) — scope with cov_path?"}
        if test.returncode != 0:
            return {"check": "tests_coverage", "ok": False, "reason": "tests failed",
                    "output": test.stdout[-2000:] + test.stderr[-500:]}
        try:
            pct = float(json.loads(cov_json.read_text())["totals"]["percent_covered"])
        except (OSError, KeyError, ValueError) as exc:
            return {"check": "tests_coverage", "ok": False,
                    "reason": f"no coverage total: {exc}"}
        return {"check": "tests_coverage", "ok": pct >= floor,
                "coverage": round(pct, 1), "floor": floor, "python": py,
                **({"cov_path": cov_path} if cov_path else {})}
    finally:
        cov_json.unlink(missing_ok=True)


def _run_cli_gate_optional(name: str, cmd: list[str], repo: Path, timeout: int = 600) -> dict:
    """Like _run_cli_gate, but for language gates with no cross-language parity:
    a MISSING binary is reported skipped/ok (Go's golangci-lint/govulncheck have
    no drop-in Python equivalent that is guaranteed installed), while a PRESENT
    tool still fails closed on a non-zero exit."""
    import shutil as _shutil
    if _shutil.which(cmd[0]) is None:
        return {"check": name, "ok": True, "skipped": f"{cmd[0]} not installed"}
    try:
        proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"check": name, "ok": False, "reason": f"timeout after {timeout}s"}
    return {"check": name, "ok": proc.returncode == 0,
            **({} if proc.returncode == 0 else
               {"output": (proc.stdout + proc.stderr)[-2000:]})}


def check_lint_py(repo: Path) -> dict:
    return _run_cli_gate_optional("golangci_lint", ["ruff", "check", "."], repo)


def check_vuln_py(repo: Path) -> dict:
    return _run_cli_gate_optional("govulncheck", ["pip-audit", "--progress-spinner", "off"], repo)


def run_guard(repo: Path, jsonl: Path | None, budget: int | None,
              cov_path: str | None = None, overrides: dict | None = None,
              base: str | None = None) -> dict:
    ov = overrides or {}
    lang = _detect_lang(repo)
    if lang == "python":
        lang_checks = [check_tests_coverage_py(repo, cov_path, python=ov.get("python"),
                                               tests=ov.get("tests"), cov=ov.get("cov"),
                                               min_coverage=ov.get("min_coverage")),
                       check_lint_py(repo), check_vuln_py(repo)]
    else:
        lang_checks = [check_tests_coverage(repo), check_lint(repo), check_vuln(repo)]
    checks = [
        check_file_ceiling(repo, cov_path, base),
        check_inmemory(repo, cov_path, base),
        *lang_checks,
        check_metered_key(repo),
    ]
    if budget is not None:
        checks.append(check_token_budget(jsonl or Path("/nonexistent"), budget))
    verdict = "GREEN" if all(c["ok"] for c in checks) else "RED"
    return {"verdict": verdict, "checks": checks, "lang": lang, **({"base": base} if base else {}),
            **({"tests": "ci"} if ov.get("tests") == "ci" else {})}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--jsonl")
    ap.add_argument("--budget-tokens", type=int)
    ap.add_argument("--cov-path",
                    help="scope coverage to this sub-package dir (monorepo); "
                         "falls back to merge-policy.json cov_path for --repo")
    ap.add_argument("--base",
                    help="gate only the delta since this commit (merge-base semantics); "
                         "legacy files already over the ceiling at the base are reported, not RED")
    ap.add_argument("--overrides-key",
                    help="checkout path whose merge-policy.json entry applies "
                         "(the merge tool runs the guard in a temp worktree)")
    args = ap.parse_args()
    repo = Path(args.repo).expanduser()
    if not (repo / ".git").exists():
        print(json.dumps({"verdict": "ERROR", "reason": f"not a git repo: {repo}"}))
        return 3
    ov = overrides_for(Path(args.overrides_key).expanduser() if args.overrides_key else repo)
    cov_path = args.cov_path or ov.get("cov_path")
    report = run_guard(repo, Path(args.jsonl).expanduser() if args.jsonl else None,
                       args.budget_tokens, cov_path=cov_path, overrides=ov, base=args.base)
    print(json.dumps(report, indent=2))
    return 0 if report["verdict"] == "GREEN" else 2


if __name__ == "__main__":
    sys.exit(main())
