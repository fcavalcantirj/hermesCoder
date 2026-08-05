#!/usr/bin/env python3
"""Sanctioned skill installer for hermesCoder — the ONLY agent-side path
into the skill directories.

The box's permission model allowlists narrow, auditable executables (the
delegate_coder.py pattern) instead of generic tools: the agent may run THIS
script, not `cp`/`rsync` into ~/.claude/skills. The script refuses anything
that isn't a validator-clean skill directory, so "installable" and
"validated" are the same fact.

Usage:
    install_skill.py <source-dir> [--name NAME]

- <source-dir> must contain SKILL.md and pass skill-creator's
  quick_validate.py.
- NAME defaults to the source directory's basename with any trailing
  "-skill" suffix stripped (…/launch-repo-skill → launch-repo).
- Installs (rsync-style replace) into BOTH ~/.claude/skills/<name> (what the
  brain loads — proven gotcha) and ~/.hermescoder/skills/<name> (staging,
  matching deploy/box-bootstrap.sh).
- Refuses source dirs that live inside either destination tree (no
  self-copy loops) and names that escape the skills dirs (path traversal).

Exit codes: 0 installed; 1 usage/refusal; 2 validation failed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
DESTS = (HOME / ".claude/skills", HOME / ".hermescoder/skills")
VALIDATOR = HOME / ".claude/skills/skill-creator/scripts/quick_validate.py"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def fail(msg: str, code: int = 1) -> "NoReturn":  # noqa: F821
    print(f"install_skill: {msg}", file=sys.stderr)
    sys.exit(code)


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--name")]
    name = None
    for a in argv:
        if a.startswith("--name="):
            name = a.split("=", 1)[1]
    if len(args) != 1:
        fail("usage: install_skill.py <source-dir> [--name=NAME]")
    src = Path(args[0]).expanduser().resolve()

    if not (src / "SKILL.md").is_file():
        fail(f"{src} has no SKILL.md — not a skill directory")
    for dest_root in DESTS:
        if src.is_relative_to(dest_root):
            fail(f"{src} is inside {dest_root} — refusing self-copy")

    if name is None:
        name = src.name.removesuffix("-skill")
    if not NAME_RE.match(name):
        fail(f"invalid skill name {name!r} (lowercase kebab-case only)")

    if not VALIDATOR.is_file():
        fail(f"validator missing at {VALIDATOR}")
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), str(src)],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or "valid" not in (result.stdout + result.stderr).lower():
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        fail(f"{src} failed skill validation — not installing", 2)

    for dest_root in DESTS:
        dest = dest_root / name
        dest_root.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        print(f"installed {name} -> {dest}")
    print(f"✓ {name}: validated and installed to both skill dirs "
          "(loads on the next session)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
