#!/usr/bin/env python3
"""Sanctioned trigger-optimizer launcher for hermesCoder — the ONLY
agent-side way to run skill-creator's run_loop.

Why this exists: the agreed optimizer parameters ("high caps with runaway
brakes" — max-iterations 20, runs-per-query 3..5) once lived only in the
agent's conversational memory, and the first real launch silently fell back
to run_loop's defaults (5 iterations). Deterministic beats remembered: the
canonical flags live HERE, the allowlist admits THIS script (the
delegate_coder.py pattern — narrow auditable executable, not a generic
tool), and drift becomes impossible.

Usage:
    optimize_skill.py <skill-name-or-path> [--eval-set PATH]
                      [--runs-per-query N] [--park-installed]
                      [--dry-run] [--foreground]

- <skill-name-or-path>: a name under ~/.claude/skills/ (e.g. launch-repo)
  or a path to a skill directory (must contain SKILL.md).
- Eval set defaults to ~/notes/<name>-skill-workspace/trigger-evals.json.
- Pinned, not overridable: --max-iterations 20 (brake, not target — the
  loop early-exits on all_passed), --model claude-opus-4-8, --report none,
  --verbose, --results-dir <workspace>/results.
- --runs-per-query defaults to 3, clamped to 1..5 (the agreed band).
- Refuses to start while another run_loop is alive (no double-burn on a
  thermally-loaded Pi).
- Detaches by default (own session, output -> <workspace>/optimizer.log);
  --foreground streams to the terminal instead.

MEASUREMENT PRECONDITIONS (established 2026-07-27, empirically — full story
in the repo's session history; both faults produced identical flat-0% recall
and each masked the other):

1. CURRENT claude CLI. The harness detects triggers by parsing
   `--include-partial-messages` stream-json events; CLI 2.1.210 emitted
   shapes the detector cannot see (skills/subagent stream changes landed
   ~2.1.217-2.1.220). Box bumped to 2.1.220 on 2026-07-27. If recall reads
   a flat 0% on every query, check `claude --version` FIRST.
2. THE SKILL MUST NOT BE INSTALLED under its real name while the eval runs.
   The harness plants a uuid-suffixed probe command and counts a trigger
   only when the model invokes THAT probe; an installed twin wins every
   organic trigger instead (verified: shadowed organic rate 0.0, same query
   unshadowed 1.0 on the same box+CLI). Optimize BEFORE installing, or use
   --park-installed: parks ~/.claude/skills/<name> aside for the run and
   restores it afterwards (md5-verified) — NOTE the live agent cannot
   trigger the skill while a parked run is in flight.

This script warns when the target is installed and --park-installed is off.

Exit codes: 0 launched/dry-run; 1 usage/refusal (incl. concurrent run);
2 missing skill or eval set; run exit code in --foreground/supervised mode.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
SKILLS_ROOT = HOME / ".claude/skills"
RUN_LOOP_DIR = HOME / ".claude/skills/skill-creator"
PINNED_MAX_ITERATIONS = "20"
PINNED_MODEL = "claude-opus-4-8"
RUNS_MIN, RUNS_MAX = 1, 5


def fail(msg: str, code: int = 1) -> "NoReturn":  # noqa: F821
    print(f"optimize_skill: {msg}", file=sys.stderr)
    sys.exit(code)


def _run_loop_alive() -> bool:
    result = subprocess.run(
        ["pgrep", "-f", "scripts.run_loop"], capture_output=True, text=True
    )
    return result.returncode == 0 and result.stdout.strip() != ""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="optimize_skill.py", add_help=True,
        description="Deterministic launcher for skill-creator's trigger optimizer.",
    )
    parser.add_argument("skill", help="skill name under ~/.claude/skills or a skill dir path")
    parser.add_argument("--eval-set", default=None,
                        help="eval JSON (default: ~/notes/<name>-skill-workspace/trigger-evals.json)")
    parser.add_argument("--runs-per-query", type=int, default=3,
                        help=f"runs per query, clamped to {RUNS_MIN}..{RUNS_MAX} (default 3)")
    parser.add_argument("--park-installed", action="store_true",
                        help="park ~/.claude/skills/<name> for the run and "
                             "restore it afterwards (md5-verified) — removes "
                             "the probe-shadowing installed twin")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the exact command and exit")
    parser.add_argument("--foreground", action="store_true",
                        help="run attached instead of detaching")
    parser.add_argument("--_supervised", action="store_true",
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    candidate = Path(args.skill).expanduser()
    skill_dir = candidate.resolve() if (candidate / "SKILL.md").is_file() \
        else (SKILLS_ROOT / args.skill)
    if not (skill_dir / "SKILL.md").is_file():
        fail(f"no skill at {skill_dir} (SKILL.md missing)", 2)
    name = skill_dir.name

    installed = SKILLS_ROOT / name
    installed_twin = (installed / "SKILL.md").is_file()
    if installed_twin and not args.park_installed:
        print(f"optimize_skill: WARNING — '{name}' is installed under "
              f"~/.claude/skills; the installed skill SHADOWS the eval probe "
              f"and recall will read ~0% regardless of description quality "
              f"(see docstring). Rerun with --park-installed, or expect "
              f"noise.", file=sys.stderr)

    workspace = HOME / "notes" / f"{name}-skill-workspace"
    eval_set = Path(args.eval_set).expanduser().resolve() if args.eval_set \
        else workspace / "trigger-evals.json"
    if not eval_set.is_file():
        fail(f"eval set missing: {eval_set} (create it or pass --eval-set)", 2)

    runs = min(max(args.runs_per_query, RUNS_MIN), RUNS_MAX)
    if runs != args.runs_per_query:
        print(f"optimize_skill: runs-per-query clamped to {runs}", file=sys.stderr)

    results_dir = workspace / "results"
    log_path = workspace / "optimizer.log"

    cmd = [
        sys.executable, "-m", "scripts.run_loop",
        "--eval-set", str(eval_set),
        "--skill-path", str(skill_dir),
        "--max-iterations", PINNED_MAX_ITERATIONS,
        "--runs-per-query", str(runs),
        "--model", PINNED_MODEL,
        "--report", "none",
        "--results-dir", str(results_dir),
        "--verbose",
    ]

    if args.dry_run:
        print("cwd:", RUN_LOOP_DIR)
        print(" ".join(cmd))
        if installed_twin and args.park_installed:
            print(f"(would park {installed} -> "
                  f"{SKILLS_ROOT.parent / ('.parked-' + name)} for the run)")
        return 0

    if not (RUN_LOOP_DIR / "scripts").is_dir():
        fail(f"skill-creator scripts missing under {RUN_LOOP_DIR}", 2)
    if _run_loop_alive():
        fail("another run_loop is already running — one optimizer at a time "
             "(check `pgrep -af scripts.run_loop`)")
    workspace.mkdir(parents=True, exist_ok=True)

    park_needed = installed_twin and args.park_installed
    if park_needed and not (args.foreground or args._supervised):
        # Detached parking needs a supervisor that outlives this process to
        # restore the skill when the run ends: re-exec self into its own
        # session; the child takes the inline park→run→restore path below.
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()),
                 *argv, "--_supervised"],
                stdout=log, stderr=log, start_new_session=True,
            )
        print(f"✓ supervised optimizer launched for {name} (pid {proc.pid}) — "
              f"'{name}' is PARKED for the duration; the live agent cannot "
              f"trigger it until the run ends and it is restored")
        print(f"  log: {log_path}")
        print(f"  if the run is ever hard-killed, restore manually: "
              f"mv {SKILLS_ROOT.parent / ('.parked-' + name)} {installed}")
        return 0

    parked_at = None
    digest_before = None
    if park_needed:
        import hashlib
        parked_at = SKILLS_ROOT.parent / f".parked-{name}"
        if parked_at.exists():
            fail(f"{parked_at} already exists — a previous parked run did "
                 f"not restore; resolve that first")
        digest_before = hashlib.md5(
            (installed / "SKILL.md").read_bytes()).hexdigest()
        installed.rename(parked_at)
        print(f"parked {installed} -> {parked_at}", flush=True)
        if skill_dir == installed:
            # run_loop must read the skill from its parked location
            cmd[cmd.index(str(installed))] = str(parked_at)

    try:
        if args.foreground or args._supervised:
            rc = subprocess.run(cmd, cwd=RUN_LOOP_DIR).returncode
        else:
            with open(log_path, "ab") as log:
                proc = subprocess.Popen(
                    cmd, cwd=RUN_LOOP_DIR, stdout=log, stderr=log,
                    start_new_session=True,
                )
            print(f"✓ optimizer launched for {name} (pid {proc.pid}, "
                  f"max-iterations {PINNED_MAX_ITERATIONS}, "
                  f"runs-per-query {runs})")
            print(f"  log: {log_path}")
            print(f"  watch: tail -f {log_path}")
            return 0
    finally:
        if parked_at is not None and parked_at.exists():
            import hashlib
            parked_at.rename(installed)
            digest_after = hashlib.md5(
                (installed / "SKILL.md").read_bytes()).hexdigest()
            verdict = "intact" if digest_after == digest_before else \
                f"CHANGED ({digest_before} -> {digest_after})"
            print(f"restored {installed} — SKILL.md {verdict}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
