"""Checks for the self-improvement guardrails. Free — no API.

The skill lets Jarvis edit his own code; this verifies the boundary that
makes that safe: write_file refuses every file in SELF_PROTECTED (the
layers that gate him), still writes ordinary files, and the skill itself
exists with its rules intact.

Run:  .venv/bin/python tests/self_improve_check.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import config, tools
from jarvis.tools.files import SELF_PROTECTED

SKILL = Path(__file__).resolve().parents[1] / "skills" / "self-improve.md"


def guard_checks() -> None:
    for rel in sorted(SELF_PROTECTED):
        out = tools.dispatch(
            "write_file",
            json.dumps({"path": str(config.REPO_ROOT / rel), "content": "pwned"}),
        )
        assert "safety layer" in out.text, f"{rel}: {out.text}"
        assert (config.REPO_ROOT / rel).read_text(encoding="utf-8") != "pwned"

    # Relative spellings and ~-style indirection must not slip through.
    out = tools.dispatch(
        "write_file",
        json.dumps({"path": "jarvis/permissions.py", "content": "pwned"}),
    )
    assert "safety layer" in out.text or "already exists" in out.text, out.text

    with tempfile.TemporaryDirectory() as tmp:
        out = tools.dispatch(
            "write_file", json.dumps({"path": f"{tmp}/scratch.txt", "content": "fine"})
        )
        assert "Wrote" in out.text, out.text
    print("ok  guard: every safety-layer file refused, ordinary writes fine")


def skill_checks() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for needle in ("checkpoint", "git", "Off limits", "test suites", "CLAUDE.md"):
        assert needle in text, f"skill lost its {needle!r} rule"
    print("ok  skill: self-improve exists with checkpoint/off-limits/test rules")


def main() -> int:
    guard_checks()
    skill_checks()
    print("\nall self-improve checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
