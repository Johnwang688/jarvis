"""Synthetic checks for background workflows. Free — llm.chat is faked.

What must hold:
  1. a started workflow runs on its own thread to done, with a log and result
  2. the workflow tools (start/status/log) work through dispatch
  3. dangerous tools are auto-denied in workflow context (deny-all approver)
  4. the safe toolset stays safe: no browser, no approval-needing tools
  5. the concurrency cap refuses a fourth simultaneous workflow

Run:  .venv/bin/python tests/workflows_check.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jarvis.agent as agent_mod
from jarvis import tools, workflows


def _reply(text="all done"):
    return types.SimpleNamespace(
        message={"content": text}, tool_calls=[], text=text, cost_usd=0.0001, latency_s=0.01
    )


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def lifecycle_checks() -> None:
    real = agent_mod.llm.chat
    agent_mod.llm.chat = lambda *a, **k: _reply("research complete, saved to notes.md")
    try:
        out = tools.dispatch(
            "workflow_start", json.dumps({"task": "research things", "name": "research"})
        )
        assert "Started wf-" in out.text, out.text
        wf_id = out.text.split()[1]

        assert _wait(lambda: workflows.get(wf_id).status == "done"), "never finished"
        wf = workflows.get(wf_id)
        assert wf.result == "research complete, saved to notes.md"
        assert any("started" in line for line in wf.log)
        assert any("done" in line for line in wf.log)

        out = tools.dispatch("workflow_status", "{}")
        assert wf_id in out.text and "[done]" in out.text, out.text
        out = tools.dispatch("workflow_log", json.dumps({"workflow_id": wf_id}))
        assert "Final report" in out.text and "research complete" in out.text, out.text
        out = tools.dispatch("workflow_log", json.dumps({"workflow_id": "wf-999"}))
        assert "Error" in out.text, out.text
    finally:
        agent_mod.llm.chat = real
    print("ok  workflows: start -> background run -> done; status and log tools work")


def safety_checks() -> None:
    dangerous = {name for name, t in tools.REGISTRY.items() if t.dangerous}
    assert not dangerous & set(workflows.SAFE_TOOLS), "dangerous tool in SAFE_TOOLS"
    assert not {t for t in workflows.SAFE_TOOLS if t.startswith("browser_")}, "browser leaked in"

    out = tools.dispatch(
        "run_command",
        json.dumps({"command": "echo hi", "reason": "test"}),
        approve=workflows._deny,
    )
    assert "declined" in out.text, out.text
    print("ok  workflows: toolset is safe; dangerous calls auto-denied")


def concurrency_checks() -> None:
    release = threading.Event()
    real = agent_mod.llm.chat

    def blocking_chat(*a, **k):
        release.wait(10)
        return _reply()

    agent_mod.llm.chat = blocking_chat
    try:
        while workflows.running_count() < workflows.MAX_CONCURRENT:
            workflows.start("wait around")
        try:
            workflows.start("one too many")
            raise AssertionError("fourth concurrent workflow was allowed")
        except RuntimeError as exc:
            assert "wait" in str(exc), exc
        release.set()
        # Drain BEFORE restoring the real client: a worker that has not yet
        # reached its chat call must still land on the fake, or this test
        # makes a real (paid) API request.
        assert _wait(lambda: workflows.running_count() == 0), "workers never drained"
    finally:
        release.set()
        agent_mod.llm.chat = real
    print("ok  workflows: concurrency cap enforced and drains cleanly")


def main() -> int:
    lifecycle_checks()
    safety_checks()
    concurrency_checks()
    print("\nall workflow checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
