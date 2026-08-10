"""Checks for typed sub-agents and the fleet. Free — `llm.chat` is stubbed.

What must hold:
  1. every type's tools exist, and none of them is dangerous
  2. every child runs on the orchestrator model — a type never carries one
  3. a type can only narrow: its toolset is intersected with the parent's
  4. the fleet actually runs concurrently, and reports in call order
  5. **the browser type never runs beside another browser child** — one
     Playwright page, one shared action budget
  6. depth cap, cancellation, and a raising child all still report

Run:  .venv/bin/python tests/fleet_check.py
"""

from __future__ import annotations

import contextlib
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import agents, config, llm, runtime, tools  # noqa: E402
from jarvis.tools import subagent  # noqa: E402


@contextlib.contextmanager
def stub_llm(delay=0.0, tracker=None):
    """Every child answers immediately, optionally after `delay` seconds."""
    real = llm.chat

    def fake(model, messages, tools=None, **kwargs):
        if tracker is not None:
            tracker.enter(messages[0]["content"])
        if delay:
            time.sleep(delay)
        if tracker is not None:
            tracker.leave(messages[0]["content"])
        return llm.Reply(
            message={"content": f"answer from {model}"},
            finish_reason="stop", model=model, latency_s=0.0,
        )

    llm.chat = fake
    try:
        yield
    finally:
        llm.chat = real


class Tracker:
    """Records peak concurrency, per kind of child, by system-prompt marker."""

    def __init__(self):
        self.lock = threading.Lock()
        self.live: dict[str, int] = {}
        self.peak: dict[str, int] = {}
        self.models: list[str] = []

    def _kind(self, system: str) -> str:
        for name in agents.TYPES:
            if agents.TYPES[name].system[:40] in system:
                return name
        return "?"

    def enter(self, system: str) -> None:
        kind = self._kind(system)
        with self.lock:
            self.live[kind] = self.live.get(kind, 0) + 1
            self.peak[kind] = max(self.peak.get(kind, 0), self.live[kind])

    def leave(self, system: str) -> None:
        kind = self._kind(system)
        with self.lock:
            self.live[kind] -= 1


@contextlib.contextmanager
def bound(tool_names=None, approve=None, stop=None, depth=0):
    token_plan = runtime.bind(
        plan={"text": ""},
        approve=approve if approve is not None else (lambda *a, **k: True),
        should_stop=stop or (lambda: False),
        depth=depth,
        tool_names=tool_names,
    )
    try:
        yield
    finally:
        pass


def catalogue_checks() -> None:
    for name, agent_type in agents.TYPES.items():
        assert agent_type.tools, f"{name} has no tools"
        for tool_name in agent_type.tools:
            assert tool_name in tools.REGISTRY, f"{name} names a missing tool: {tool_name}"
            assert not tools.REGISTRY[tool_name].dangerous, (
                f"{name} carries a dangerous tool ({tool_name}) — a child's dangerous "
                "call goes to the parent's approver, and the owner would be answering "
                "for a request they never saw framed"
            )
        assert name in agents.catalogue(), f"{name} missing from the catalogue"
    # The one type that holds a singleton.
    assert not agents.get("browser").concurrent_safe
    assert all(agents.get(n).concurrent_safe for n in agents.TYPES if n != "browser")
    assert agents.get("no-such-type").name == agents.DEFAULT, "unknown type must fall back"
    print(f"ok  types: {len(agents.TYPES)} types, all tools real, none dangerous")


def model_checks() -> None:
    """Every child is Luna. A type carries a brief, never a model."""
    assert config.TIERS["subagent"] == config.TIERS["orchestrator"], (
        "sub-agents must run on the orchestrator model"
    )
    for agent_type in agents.TYPES.values():
        assert not hasattr(agent_type, "model"), f"{agent_type.name} carries a model"

    seen: list[str] = []
    real = llm.chat

    def fake(model, messages, tools=None, **kwargs):
        seen.append(model)
        return llm.Reply(message={"content": "done"}, finish_reason="stop",
                         model=model, latency_s=0.0)

    llm.chat = fake
    try:
        with bound(tool_names=set(subagent.SUBAGENT_TOOLS)):
            for name in agents.TYPES:
                tools.dispatch("run_subagent", json.dumps({"task": "x", "type": name}))
    finally:
        llm.chat = real
    assert seen and all(m == config.TIERS["orchestrator"] for m in seen), set(seen)
    print(f"ok  model: all {len(seen)} children ran on {config.TIERS['orchestrator']}")


def narrowing_checks() -> None:
    """A type can only narrow — never a route to a tool the parent lacks."""
    # A parent denied the browser (a workflow) must not reach it via the
    # browser type, which is the whole reason the intersection exists.
    with bound(tool_names={"read_file", "grep_files", "run_subagent"}):
        child = subagent._child_tools(agents.get("browser"))
    assert "browser_goto" not in child, child
    assert set(child) <= {"read_file", "grep_files"}, child

    with bound(tool_names=set(subagent.SUBAGENT_TOOLS)):
        child = subagent._child_tools(agents.get("explorer"))
    assert "grep_files" in child and "browser_goto" not in child, child
    print("ok  narrowing: type tools intersected with the parent's, browser unreachable")


def fleet_concurrency_checks() -> None:
    tracker = Tracker()
    jobs = [{"task": f"job {i}", "type": "explorer"} for i in range(4)]
    with stub_llm(delay=0.25, tracker=tracker):
        with bound(tool_names=set(subagent.SUBAGENT_TOOLS)):
            started = time.monotonic()
            out = tools.dispatch("run_fleet", json.dumps({"jobs": json.dumps(jobs)})).text
            elapsed = time.monotonic() - started
    assert elapsed < 0.6, f"four 0.25s children took {elapsed:.2f}s — ran serially"
    assert tracker.peak.get("explorer", 0) > 1, tracker.peak
    assert out.count("answer from") == 4, out
    assert "4 sub-agents" in out, out
    print(f"ok  fleet: 4 children in {elapsed:.2f}s, peak concurrency {tracker.peak['explorer']}")


def browser_is_solo_checks() -> None:
    """The caveat that has been in CLAUDE.md since sub-agents existed.

    browser.SESSION is a module-level singleton with one page and one shared
    120-action budget. Two browser children would interleave on it and corrupt
    each other, so the fleet has to run them one at a time even while it runs
    everything else together.
    """
    tracker = Tracker()
    jobs = [{"task": f"page {i}", "type": "browser"} for i in range(3)]
    jobs += [{"task": f"look {i}", "type": "explorer"} for i in range(3)]
    with stub_llm(delay=0.15, tracker=tracker):
        with bound(tool_names=set(subagent.SUBAGENT_TOOLS)):
            out = tools.dispatch("run_fleet", json.dumps({"jobs": json.dumps(jobs)})).text
    assert tracker.peak.get("browser", 0) == 1, (
        f"two browser children overlapped (peak {tracker.peak.get('browser')}) — "
        "they share one Playwright page"
    )
    assert tracker.peak.get("explorer", 0) > 1, "the safe ones should still run together"
    assert out.count("answer from") == 6, out
    print("ok  fleet: browser children serialised, everything else still concurrent")


def fleet_order_checks() -> None:
    jobs = [{"task": "alpha", "type": "explorer"},
            {"task": "beta", "type": "researcher"},
            {"task": "gamma", "type": "reviewer"}]
    with stub_llm():
        with bound(tool_names=set(subagent.SUBAGENT_TOOLS)):
            out = tools.dispatch("run_fleet", json.dumps({"jobs": json.dumps(jobs)})).text
    positions = [out.index(f"{i + 1}/3 {t}") for i, t in enumerate(("explorer", "researcher", "reviewer"))]
    assert positions == sorted(positions), f"fleet reported out of order: {out}"
    print("ok  fleet: reported in call order, labelled by type")


def fleet_input_checks() -> None:
    with bound(tool_names=set(subagent.SUBAGENT_TOOLS)):
        assert "not valid JSON" in tools.dispatch("run_fleet", json.dumps({"jobs": "{["})).text
        assert "non-empty" in tools.dispatch("run_fleet", json.dumps({"jobs": "[]"})).text
        too_many = json.dumps([{"task": "x"} for _ in range(subagent.MAX_FLEET + 1)])
        out = tools.dispatch("run_fleet", json.dumps({"jobs": too_many})).text
        assert f"at most {subagent.MAX_FLEET}" in out, out
        assert "has no task" in tools.dispatch(
            "run_fleet", json.dumps({"jobs": json.dumps([{"type": "explorer"}])})
        ).text
    print("ok  fleet: bad JSON, empty, oversized and task-less jobs all refused")


def resilience_checks() -> None:
    """A child that explodes still reports, and depth/cancel still bite."""
    real = llm.chat
    calls = {"n": 0}

    def flaky(model, messages, tools=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("provider exploded")
        return llm.Reply(message={"content": "fine"}, finish_reason="stop",
                         model=model, latency_s=0.0)

    llm.chat = flaky
    try:
        with bound(tool_names=set(subagent.SUBAGENT_TOOLS)):
            jobs = [{"task": f"j{i}", "type": "explorer"} for i in range(3)]
            out = tools.dispatch("run_fleet", json.dumps({"jobs": json.dumps(jobs)})).text
    finally:
        llm.chat = real
    assert out.count("fine") == 2 and "exploded" in out, out
    assert "3 sub-agents" in out, "a fleet must report every job, including the failed one"

    with stub_llm():
        with bound(tool_names=set(subagent.SUBAGENT_TOOLS), depth=runtime.MAX_DEPTH):
            assert "depth limit" in tools.dispatch(
                "run_subagent", json.dumps({"task": "x"})).text
            assert "depth limit" in tools.dispatch(
                "run_fleet", json.dumps({"jobs": '[{"task":"x"}]'})).text

        with bound(tool_names=set(subagent.SUBAGENT_TOOLS), stop=lambda: True):
            assert "Cancelled" in tools.dispatch("run_subagent", json.dumps({"task": "x"})).text
            assert "Cancelled" in tools.dispatch(
                "run_fleet", json.dumps({"jobs": '[{"task":"x"}]'})).text
    print("ok  fleet: a raising child still reports; depth cap and cancel both bite")


def approver_checks() -> None:
    """Children inherit the parent's approver — a deny-all parent, deny-all child."""
    seen: list = []

    def deny(tool, args):
        seen.append(tool.name)
        return False

    with stub_llm():
        with bound(tool_names=set(subagent.SUBAGENT_TOOLS), approve=deny):
            # The child is constructed with runtime.approver(); assert identity
            # rather than trying to make a non-dangerous toolset ask.
            captured = {}
            real_run = subagent._run_one

            def spy(job):
                captured["approver"] = runtime.approver()
                return real_run(job)

            subagent._run_one = spy
            try:
                tools.dispatch("run_subagent", json.dumps({"task": "x"}))
            finally:
                subagent._run_one = real_run
    assert captured["approver"] is deny, "the child did not inherit the parent's approver"
    print("ok  fleet: the parent's approver is what a child dispatches with")


def workflow_exclusion_checks() -> None:
    """A background workflow may send one child, but not a fleet of six.

    run_subagent is in SAFE_TOOLS because it is synchronous and inherits the
    deny-all approver. run_fleet multiplies spend by six from a thread nobody
    is watching, which is a different question.
    """
    from jarvis import workflows

    assert "run_subagent" in workflows.SAFE_TOOLS
    assert "run_fleet" not in workflows.SAFE_TOOLS, (
        "run_fleet must stay out of background workflows — six concurrent "
        "children with nobody watching is a spend amplifier"
    )
    print("ok  workflows: one child allowed, a fleet is not")


def main() -> int:
    catalogue_checks()
    model_checks()
    narrowing_checks()
    fleet_concurrency_checks()
    browser_is_solo_checks()
    fleet_order_checks()
    fleet_input_checks()
    resilience_checks()
    approver_checks()
    workflow_exclusion_checks()
    print("\nall fleet checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
