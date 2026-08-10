"""Checks for the agent loop's dispatch and stop-reason handling. Free — no API.

`llm.chat` is stubbed so every reply shape can be produced on cue. Two things
are under test, both added 2026-08-09:

  Parallel dispatch — independent tool calls run together. The subtle half is
  not the speed, it is that ContextVars are per-thread and `runtime.py` fails
  closed: a pool thread that did not inherit the parent's bindings would hold
  an unbound approver, which *denies*. A gated tool would then start refusing
  itself purely because it ran beside another one, which is the kind of bug
  that only shows up in the wild.

  finish_reason == "length" — a reply cut off at the token ceiling. It used to
  be indistinguishable from a finished one, so a sentence that stopped
  mid-word was returned as the answer.

Both must leave the transcript wire-valid: every tool_call answered exactly
once, in order (invariants 3 and 5).

Run:  .venv/bin/python tests/loop_check.py
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import agent as agent_mod  # noqa: E402
from jarvis import llm, runtime, tools  # noqa: E402

EMPTY_SCHEMA = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


def reply(text="", calls=None, finish="stop", prompt_tokens=0):
    message = {"content": text or None}
    if calls:
        message["tool_calls"] = [
            {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": a}}
            for i, (n, a) in enumerate(calls)
        ]
    return llm.Reply(
        message=message,
        finish_reason=finish if not calls else ("length" if finish == "length" else "tool_calls"),
        model="stub",
        latency_s=0.0,
        prompt_tokens=prompt_tokens,
    )


@contextlib.contextmanager
def scripted(replies):
    """Serve `replies` in order from llm.chat."""
    queue = list(replies)
    real = llm.chat

    def fake(model, messages, tools=None, **kwargs):
        return queue.pop(0)

    llm.chat = fake
    try:
        yield
    finally:
        llm.chat = real


@contextlib.contextmanager
def stub_tools(**funcs):
    """Swap real tools for stubs under their own (parallel-safe) names."""
    saved = {name: tools.REGISTRY.get(name) for name in funcs}
    for name, func in funcs.items():
        tools.REGISTRY[name] = tools.Tool(
            name=name, description="stub", schema=EMPTY_SCHEMA, func=func
        )
    try:
        yield
    finally:
        for name, original in saved.items():
            if original is None:
                tools.REGISTRY.pop(name, None)
            else:
                tools.REGISTRY[name] = original


def assert_wire_valid(messages) -> None:
    """Every declared tool_call has exactly one result, and nothing is orphaned."""
    declared, answered = [], []
    for message in messages:
        for call in message.get("tool_calls") or []:
            declared.append(call["id"])
        if message.get("role") == "tool":
            answered.append(message["tool_call_id"])
    assert declared == answered, f"declared {declared} answered {answered}"


# --- parallel dispatch -----------------------------------------------------


def grouping_checks() -> None:
    def calls(*names):
        return [{"function": {"name": n, "arguments": "{}"}} for n in names]

    groups = agent_mod._call_groups(calls("read_file", "read_file", "grep_files"))
    assert [len(g) for g in groups] == [3], groups

    # The ordering property: a write between two reads has to break the batch,
    # or the second read could observe the file before the write landed.
    groups = agent_mod._call_groups(calls("read_file", "write_file", "read_file"))
    assert [len(g) for g in groups] == [1, 1, 1], groups
    assert [g[0][0] for g in groups] == [0, 1, 2], "indices must stay in call order"

    groups = agent_mod._call_groups(calls("read_file", "read_file", "run_command", "grep_files"))
    assert [len(g) for g in groups] == [2, 1, 1], groups
    print("ok  grouping: safe runs batch, a write splits them, order preserved")


def allowlist_checks() -> None:
    for name in tools.PARALLEL_SAFE:
        assert name in tools.REGISTRY, f"PARALLEL_SAFE names a tool that does not exist: {name}"
        assert not tools.REGISTRY[name].dangerous, f"{name} is dangerous and parallel-safe"
    for name in tools.REGISTRY:
        if name.startswith(("browser_", "desktop_")) or name == "run_subagent":
            assert not tools.parallelizable(name), f"{name} must never run in parallel"

    # Danger is checked at the gate, not only in the list: marking an
    # allowlisted tool dangerous must take it out of parallel play, so two
    # approval prompts can never race for the owner's attention.
    entry = tools.REGISTRY["read_file"]
    entry.dangerous = True
    try:
        assert not tools.parallelizable("read_file")
    finally:
        entry.dangerous = False
    print("ok  allowlist: no drift, nothing dangerous, no browser/desktop/subagent")


def concurrency_checks() -> None:
    def slow():
        time.sleep(0.3)
        return "tick"

    agent = agent_mod.Agent(tool_names=["get_datetime", "memory_list", "skill_list"])
    with stub_tools(get_datetime=slow, memory_list=slow, skill_list=slow):
        with scripted([
            reply(calls=[("get_datetime", "{}"), ("memory_list", "{}"), ("skill_list", "{}")]),
            reply(text="done"),
        ]):
            started = time.monotonic()
            turn = agent.run_turn("go")
            elapsed = time.monotonic() - started

    assert turn.text == "done"
    assert elapsed < 0.6, f"three 0.3s tools took {elapsed:.2f}s — ran serially"
    assert_wire_valid(agent.messages)
    print(f"ok  concurrency: 3x0.3s tools finished in {elapsed:.2f}s (serial would be 0.9s)")


def ordering_checks() -> None:
    def make(value):
        def func():
            time.sleep(0.05 * value)  # finish out of submission order on purpose
            return f"result-{value}"
        return func

    agent = agent_mod.Agent(tool_names=["get_datetime", "memory_list", "skill_list"])
    with stub_tools(get_datetime=make(3), memory_list=make(2), skill_list=make(1)):
        with scripted([
            reply(calls=[("get_datetime", "{}"), ("memory_list", "{}"), ("skill_list", "{}")]),
            reply(text="ok"),
        ]):
            agent.run_turn("go")

    results = [m["content"] for m in agent.messages if m.get("role") == "tool"]
    assert results == ["result-3", "result-2", "result-1"], results
    assert_wire_valid(agent.messages)
    print("ok  ordering: results match call order even when they finish out of order")


def propagation_checks() -> None:
    """The one that matters: pool threads must inherit the parent's bindings."""
    seen: list[tuple] = []
    lock = threading.Lock()

    def probe():
        state = (
            runtime.plan_slot() is not None,
            runtime.approver() is not None,
            runtime.depth(),
            threading.current_thread().name,
        )
        with lock:
            seen.append(state)
        return "probed"

    approver_calls: list = []
    agent = agent_mod.Agent(
        tool_names=["get_datetime", "memory_list"],
        approve=lambda tool, args: approver_calls.append(tool.name) or True,
        depth=1,
    )
    with stub_tools(get_datetime=probe, memory_list=probe):
        with scripted([
            reply(calls=[("get_datetime", "{}"), ("memory_list", "{}")]),
            reply(text="ok"),
        ]):
            agent.run_turn("go")

    assert len(seen) == 2, seen
    for has_plan, has_approver, depth, thread in seen:
        assert has_plan, "plan slot did not reach the worker thread"
        assert has_approver, "approver did not reach the worker — it would deny"
        assert depth == 1, f"depth did not propagate: {depth}"
    assert any(t.startswith("jarvis-tool") for *_, t in seen), "did not actually run on a pool"

    # The plan is one dict shared by reference, so a worker sees the owner's.
    agent.plan["text"] = "- [ ] step"
    assert agent.plan["text"] == "- [ ] step"
    print("ok  propagation: plan, approver and depth all reach pool threads")


def failure_checks() -> None:
    """A tool that explodes still has to produce a result for its call id."""

    def boom():
        raise RuntimeError("kaboom")

    agent = agent_mod.Agent(tool_names=["get_datetime", "memory_list"])
    with stub_tools(get_datetime=boom, memory_list=lambda: "fine"):
        with scripted([
            reply(calls=[("get_datetime", "{}"), ("memory_list", "{}")]),
            reply(text="recovered"),
        ]):
            turn = agent.run_turn("go")

    results = [m["content"] for m in agent.messages if m.get("role") == "tool"]
    assert "kaboom" in results[0], results
    assert results[1] == "fine", results
    assert turn.text == "recovered"
    assert_wire_valid(agent.messages)
    print("ok  failure: a raising tool answers its call id and the turn continues")


def image_checks() -> None:
    """Parallel image results keep their carrier user message behind them."""

    def shot():
        return tools.ToolResult("rendered", image_b64="AAAA")

    agent = agent_mod.Agent(tool_names=["get_datetime", "memory_list"])
    with stub_tools(get_datetime=shot, memory_list=shot):
        with scripted([
            reply(calls=[("get_datetime", "{}"), ("memory_list", "{}")]),
            reply(text="ok"),
        ]):
            agent.run_turn("go")

    roles = [m["role"] for m in agent.messages]
    # ... assistant(2 calls) · tool · user(image) · tool · user(image) · assistant
    assert roles[-6:-1] == ["assistant", "tool", "user", "tool", "user"], roles
    assert_wire_valid(agent.messages)
    print("ok  images: each parallel image rides behind its own tool message")


# --- finish_reason == "length" ---------------------------------------------


def continuation_checks() -> None:
    agent = agent_mod.Agent(tool_names=["get_datetime"])
    with scripted([
        reply(text="The answer is that the cat sat on the", finish="length"),
        reply(text=" mat, and that is all."),
    ]):
        turn = agent.run_turn("explain")

    assert turn.truncated, "a cut-off reply must be flagged"
    assert turn.text == "The answer is that the cat sat on the mat, and that is all.", turn.text
    assert any(
        agent_mod.CONTINUE_NUDGE == m.get("content") for m in agent.messages
    ), "the truncation was never written into the transcript"
    print("ok  truncation: a cut-off reply is continued and the halves are joined")


def continuation_cap_checks() -> None:
    agent = agent_mod.Agent(tool_names=["get_datetime"])
    with scripted([reply(text="a", finish="length") for _ in range(agent_mod.MAX_CONTINUATIONS + 1)]):
        turn = agent.run_turn("explain")

    assert turn.truncated
    assert turn.text.endswith("[cut off at the token limit]"), turn.text
    assert turn.text.startswith("aaa"), turn.text
    nudges = sum(1 for m in agent.messages if m.get("content") == agent_mod.CONTINUE_NUDGE)
    assert nudges == agent_mod.MAX_CONTINUATIONS, nudges
    print("ok  truncation: continuation is capped and the cut is stated in the reply")


def truncated_tool_call_checks() -> None:
    """A cut-off reply that was mid-tool-call still settles before it is noted."""
    agent = agent_mod.Agent(tool_names=["get_datetime"])
    with stub_tools(get_datetime=lambda: "tick"):
        with scripted([
            reply(calls=[("get_datetime", "{}")], finish="length"),
            reply(text="carried on"),
        ]):
            turn = agent.run_turn("go")

    assert turn.truncated
    assert_wire_valid(agent.messages)
    tool_index = next(i for i, m in enumerate(agent.messages) if m.get("role") == "tool")
    note = agent.messages[tool_index + 1]
    assert note["role"] == "user" and "cut off mid-argument" in note["content"], agent.messages
    print("ok  truncation: the mid-tool-call note lands after the results, not before")


def untruncated_checks() -> None:
    agent = agent_mod.Agent(tool_names=["get_datetime"])
    with scripted([reply(text="all done")]):
        turn = agent.run_turn("go")
    assert not turn.truncated and turn.text == "all done"
    assert len(agent.messages) == 3, agent.messages  # system, user, assistant
    print("ok  truncation: an ordinary reply gains no note and no extra call")


def main() -> int:
    grouping_checks()
    allowlist_checks()
    concurrency_checks()
    ordering_checks()
    propagation_checks()
    failure_checks()
    image_checks()
    continuation_checks()
    continuation_cap_checks()
    truncated_tool_call_checks()
    untruncated_checks()
    print("\nall loop checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
