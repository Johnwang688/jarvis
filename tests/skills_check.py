"""Synthetic checks for the skills tools and the always-visible index. Free.

The index is the Claude-Code-style two-tier design: every skill's name +
trigger description is injected into skill-armed agents' system message each
turn; the full body still loads only via skill_read.

Run:  .venv/bin/python tests/skills_check.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jarvis.agent as agent_mod
from jarvis import config, tools
from jarvis.tools import skills as skills_mod

REPO_SKILLS = Path(__file__).resolve().parents[1] / "skills"


def crud_checks() -> None:
    out = tools.dispatch("skill_list", "{}")
    assert "No skills" in out.text, out.text

    out = tools.dispatch(
        "skill_write",
        json.dumps(
            {
                "name": "test-skill",
                "description": "a test workflow",
                "instructions": "1. do the thing\n2. report",
            }
        ),
    )
    assert "saved" in out.text, out.text

    out = tools.dispatch("skill_list", "{}")
    assert "test-skill: a test workflow" in out.text, out.text

    out = tools.dispatch("skill_read", json.dumps({"name": "test-skill"}))
    assert "do the thing" in out.text and "a test workflow" in out.text, out.text

    out = tools.dispatch("skill_write", json.dumps(
        {"name": "test-skill", "description": "v2", "instructions": "3. improved"}
    ))
    assert "updated" in out.text, out.text
    assert "improved" in tools.dispatch("skill_read", json.dumps({"name": "test-skill"})).text

    out = tools.dispatch("skill_read", json.dumps({"name": "nope"}))
    assert "Error" in out.text and "skill_list" in out.text, out.text
    out = tools.dispatch("skill_write", json.dumps(
        {"name": "Bad Name!", "description": "x", "instructions": "y"}
    ))
    assert "Error" in out.text, out.text
    print("ok  skills: write/list/read/update round-trip, bad inputs refused")


def starter_checks() -> None:
    """The checked-in starter skills must parse and carry descriptions."""
    names = {p.stem for p in REPO_SKILLS.glob("*.md")}
    assert {
        "email-triage",
        "morning-briefing",
        "skill-creator",
        "whiteboard",
    } <= names, names
    for path in REPO_SKILLS.glob("*.md"):
        description, body = skills_mod._parse(path.read_text(encoding="utf-8"))
        assert description, f"{path.name} has no description"
        assert body, f"{path.name} has no instructions"
    print("ok  skills: starter skills parse with descriptions")


def index_checks() -> None:
    assert skills_mod.index() == "", "index of a missing/empty library must be empty"

    tools.dispatch("skill_write", json.dumps(
        {"name": "alpha", "description": "Use when testing alpha", "instructions": "steps"}
    ))
    out = skills_mod.index()
    assert "SKILLS AVAILABLE" in out and "- alpha: Use when testing alpha" in out, out

    # Editing a file in place must refresh the cache (content, not just count).
    tools.dispatch("skill_write", json.dumps(
        {"name": "alpha", "description": "Use when testing alpha v2", "instructions": "steps"}
    ))
    assert "alpha v2" in skills_mod.index()

    for i in range(40):
        tools.dispatch("skill_write", json.dumps(
            {"name": f"bulk-{i:02d}", "description": f"Use for bulk case {i}", "instructions": "x"}
        ))
    out = skills_mod.index()
    shown = out.count("\n- ") + out.count("SKILLS AVAILABLE\n- ")  # entry lines
    assert "more — run skill_list" in out, "cap must point at skill_list"
    assert len(out) < 3000, f"index too large: {len(out)} chars"
    print("ok  index: renders, refreshes on edit, caps with a skill_list tail")


def injection_checks() -> None:
    seen: list[str] = []

    def fake_chat(model, messages, tools=None, **kwargs):
        seen.append(messages[0]["content"])
        return types.SimpleNamespace(
            message={"content": "ok"}, tool_calls=[], text="ok", cost_usd=0.0, latency_s=0.0
        )

    real = agent_mod.llm.chat
    agent_mod.llm.chat = fake_chat
    try:
        armed = agent_mod.Agent(approve=lambda t, a: False)
        armed.run_turn("hello")
        assert "SKILLS AVAILABLE" in seen[-1] and "- alpha:" in seen[-1]

        # A skill saved mid-conversation appears on the next turn.
        tools.dispatch("skill_write", json.dumps(
            {"name": "brand-new", "description": "Use when new", "instructions": "y"}
        ))
        armed.run_turn("again")
        assert "brand-new" in seen[-1], "mid-session skill missing from next turn"

        unarmed = agent_mod.Agent(
            approve=lambda t, a: False, tool_names=["read_file", "write_file"]
        )
        unarmed.run_turn("hello")
        assert "SKILLS AVAILABLE" not in seen[-1], "index leaked to a skill-less agent"
    finally:
        agent_mod.llm.chat = real
    print("ok  injection: index in skill-armed system messages, fresh each turn, absent otherwise")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        config.SKILLS_DIR = Path(tmp) / "skills"
        crud_checks()
    with tempfile.TemporaryDirectory() as tmp:
        config.SKILLS_DIR = Path(tmp) / "skills"
        index_checks()
        injection_checks()
    starter_checks()
    print("\nall skills checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
