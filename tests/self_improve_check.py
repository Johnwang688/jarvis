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

# Agent Skills layout since v2 WP8: skills/<name>/SKILL.md.
SKILL = Path(__file__).resolve().parents[1] / "skills" / "self-improve" / "SKILL.md"


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

    # Every write path, not just the first one. edit_file (2026-08-09) can
    # reach these files as precisely as write_file can and does far less to
    # give itself away — a one-line replacement in permissions.py disarms the
    # gate without changing the shape of the file. Any future write tool has
    # to be added here too.
    for rel in sorted(SELF_PROTECTED):
        path = config.REPO_ROOT / rel
        before = path.read_text(encoding="utf-8")
        tools.dispatch("read_file", json.dumps({"path": str(path)}))
        anchor = next(ln for ln in before.split("\n") if ln.startswith(("import ", "from ")))
        out = tools.dispatch(
            "edit_file",
            json.dumps({"path": str(path), "old_string": anchor, "new_string": "# pwned"}),
        )
        assert "safety layer" in out.text, f"{rel}: {out.text}"
        assert path.read_text(encoding="utf-8") == before, f"{rel} was modified by edit_file"

    with tempfile.TemporaryDirectory() as tmp:
        out = tools.dispatch(
            "write_file", json.dumps({"path": f"{tmp}/scratch.txt", "content": "fine"})
        )
        assert "Wrote" in out.text, out.text
    print("ok  guard: every safety-layer file refused, ordinary writes fine")


STATE_PATHS = ("ALLOWLIST_PATH", "MODELS_PATH", "PROVIDER_DEFAULTS_PATH", "DISCORD_GUILD_PATH",
               "CODEX_CATALOG_PATH",
               "ROUTING_PATH")


def state_checks() -> None:
    """The gate's data and the model/routing state (2026-10-08, PR #15
    review): which model a v2 thread runs on is no more his to rewrite than
    the allowlist. Every path is a temp file here, never the owner's."""
    saved = {name: getattr(config, name) for name in STATE_PATHS}
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        for name in STATE_PATHS:
            setattr(config, name, tmp / name.lower() / f"{name.lower()}.json")
        # routing.json is protected as the allowlist's sibling, as in v2.
        config.ROUTING_PATH = config.ALLOWLIST_PATH.with_name("routing.json")
        try:
            for name in STATE_PATHS:
                path = getattr(config, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"model": "claude-opus-5-5"}', encoding="utf-8")
                read = tools.dispatch("read_file", json.dumps({"path": str(path)}))
                assert "claude-opus-5-5" in read.text, f"{name} must stay readable: {read.text}"
                out = tools.dispatch("edit_file", json.dumps({
                    "path": str(path), "old_string": "claude-opus-5-5", "new_string": "claude-haiku-4-5"}))
                assert "safety layer" in out.text, f"{name}: {out.text}"
                out = tools.dispatch("write_file", json.dumps({"path": str(path), "content": "{}"}))
                assert "safety layer" in out.text, f"{name}: {out.text}"
                assert "claude-opus-5-5" in path.read_text(encoding="utf-8"), f"{name} was rewritten"
        finally:
            for name, value in saved.items():
                setattr(config, name, value)
    print("ok  guard: the allowlist, roster, chat defaults, guild file and routing are refused, "
          "still readable")


def skill_checks() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for needle in ("checkpoint", "git", "Off limits", "test suites", "CLAUDE.md"):
        assert needle in text, f"skill lost its {needle!r} rule"
    print("ok  skill: self-improve exists with checkpoint/off-limits/test rules")


def main() -> int:
    guard_checks()
    state_checks()
    skill_checks()
    print("\nall self-improve checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
