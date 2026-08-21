"""Checks for attended background tasks. Free — no API calls, no network.

What must hold, in order of how much it matters:

  1. A task agent's dangerous call reaches the owner through the broker the
     starting agent held — the card carries the task's origin label, an
     approval runs the command, a denial does not (the flip that
     distinguishes tasks from deny-all workflows).
  2. The captured approver is captured *whole*: a real gate keeps its
     human-backed flag inside a task (an ALLOW-verdict command runs with no
     card), and a task started from an unbound context is deny-all — fail
     closed, exactly like a workflow.
  3. Attribution changes nothing for the conversation agent: with no origin
     bound, the SSE payload has no origin key and the Discord DM body is
     byte-identical to the pre-task shape.
  4. Task state renders into the working-context block only for agents that
     hold the task tools, and the task toolset never contains the browser,
     the desktop, its own family, or the fleet.

`llm.chat` is stubbed (loop_check's Reply shape), `shell._run` is a recorder
(rules_check's pattern), the broker gets approval_check's FakeWindow, and the
DM sender is injected (discord_approvals_check's pattern). ALLOWLIST_PATH is
pointed at a temp file for the whole run — `gate()` consults it.

Run:  .venv/bin/python tests/tasks_check.py
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import agent as agent_mod  # noqa: E402
from jarvis import config, llm, permissions, runtime, tasks, tools, workflows  # noqa: E402
from jarvis.discord_approvals import DiscordApprovals  # noqa: E402
from jarvis.face.approvals import ApprovalBroker  # noqa: E402
from jarvis.goalrunner import goal_tool_names  # noqa: E402
from jarvis.tools import shell  # noqa: E402
from jarvis.tools.subagent import SUBAGENT_TOOLS  # noqa: E402

TASK_TOOL_NAMES = ("task_start", "task_status", "task_log", "task_cancel")


def reply(text="", calls=None):
    message = {"content": text or None}
    if calls:
        message["tool_calls"] = [
            {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": a}}
            for i, (n, a) in enumerate(calls)
        ]
    return llm.Reply(
        message=message,
        finish_reason="tool_calls" if calls else "stop",
        model="stub",
        latency_s=0.0,
    )


@contextlib.contextmanager
def scripted(replies, seen_tool_results=None):
    """Serve `replies` in order from llm.chat; drain tasks before restoring.

    Draining before the restore is what prevents a paid API call: a task
    thread that has not yet reached its chat call must find the stub, never
    the real client.
    """
    queue = list(replies)
    real = llm.chat

    def fake(model, messages, tools=None, **kwargs):
        if seen_tool_results is not None:
            seen_tool_results.extend(
                m.get("content") for m in messages if m.get("role") == "tool"
            )
        return queue.pop(0)

    llm.chat = fake
    try:
        yield
    finally:
        _drain()
        llm.chat = real


@contextlib.contextmanager
def recorded():
    """Nothing executes; shell._run just records what it was handed."""
    ran: list[str] = []
    real = shell._run
    shell._run = lambda command: ran.append(command) or "[exit 0]"
    try:
        yield ran
    finally:
        shell._run = real


class FakeWindow:
    """Stands in for a connected HUD: counts as a viewer, records broadcasts."""

    def __init__(self, connected: int = 1):
        self.connected = connected
        self.sent: list[tuple[str, dict]] = []

    def broadcast(self, kind, data):
        self.sent.append((kind, data))

    def viewers(self):
        return self.connected

    def kinds(self):
        return [k for k, _ in self.sent]

    def last(self, kind):
        return [d for k, d in self.sent if k == kind][-1]


def _await(probe, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            found = probe()
        except (IndexError, KeyError):
            found = None
        if found:
            return found
        time.sleep(0.02)
    raise AssertionError("timed out waiting for a condition")


def _drain(timeout=10.0):
    _await(lambda: tasks.running_count() == 0 or None, timeout) if tasks.running_count() else None


def _start_with(approve, task_text, name=""):
    """Dispatch task_start with `approve` bound in a copied context, so the
    binding never leaks onto this test's own thread."""

    def go():
        if approve is not None:
            runtime.bind(approve=approve)
        return tools.dispatch("task_start", json.dumps({"task": task_text, "name": name}))

    out = contextvars.copy_context().run(go)
    assert "Started tk-" in out.text, out.text
    return out.text.split()[1]


def toolset_checks() -> None:
    names = tasks.task_tool_names()
    assert set(names) <= set(tools.REGISTRY), "task toolset names a tool that does not exist"
    assert "run_command" in names and "gmail_send" in names, "dangerous tools are the point"
    assert "run_subagent" in names, "synchronous delegation stays available"
    for name in names:
        assert not name.startswith("browser_"), name
        assert not name.startswith("desktop_"), name
    for tool_name in TASK_TOOL_NAMES + ("workflow_start", "workflow_log", "run_fleet"):
        assert tool_name not in names, f"{tool_name} must not be reachable from a task"

    for tool_name in TASK_TOOL_NAMES:
        assert tool_name in tools.REGISTRY, tool_name
        assert not tools.REGISTRY[tool_name].dangerous, tool_name
        assert tool_name not in workflows.SAFE_TOOLS, "workflows must not start tasks"
        assert tool_name not in SUBAGENT_TOOLS, "sub-agents must not start tasks"
        assert tool_name not in goal_tool_names(), "goals must not start tasks"
    assert tools.parallelizable("task_status") and tools.parallelizable("task_log")
    assert not tools.parallelizable("task_start") and not tools.parallelizable("task_cancel")

    # Before any task exists: nothing to say, nowhere.
    assert tasks.block() == "", tasks.block()
    assert "No background tasks" in tasks.listing()

    # The label lands verbatim on the authorization surfaces, so a model-chosen
    # name cannot fake a second line or break the DM's formatting.
    label = tasks._label("tk-9", "evil`\nname\t" + "x" * 100)
    assert "\n" not in label and "`" not in label.replace('"', ""), label
    assert len(label) < 90, label
    assert tasks._label("tk-9", "") == "task tk-9"
    print("ok  toolset: exclusions hold, task tools unreachable from background contexts")


def lifecycle_checks() -> None:
    with scripted([reply(text="wrote the summary to notes.md")]):
        tid = _start_with(None, "summarize the notes", name="notes")
        tsk = _await(lambda: tasks.get(tid) if tasks.get(tid).status != "running" else None)
    assert tsk.status == "done", tsk.status
    assert tsk.result == "wrote the summary to notes.md"
    assert any("started:" in line for line in tsk.log), tsk.log

    status = tools.dispatch("task_status", "{}").text
    assert tid in status and "[done]" in status, status
    log = tools.dispatch("task_log", json.dumps({"task_id": tid})).text
    assert "Final report" in log, log
    bad = tools.dispatch("task_log", json.dumps({"task_id": "tk-999"})).text
    assert "Error" in bad, bad
    already = tools.dispatch("task_cancel", json.dumps({"task_id": tid})).text
    assert "already done" in already, already
    print("ok  lifecycle: start through dispatch, status/log/cancel answer honestly")


def attribution_card_checks() -> None:
    """The headline: a task's dangerous call raises the card, attributed."""
    win = FakeWindow(connected=1)
    broker = ApprovalBroker(win.broadcast, win.viewers, timeout_s=10, announce=lambda m: None)
    gated = permissions.gate(broker.approver())
    ask_cmd = json.dumps({"command": "rm /tmp/tasks-check-scratch.txt", "reason": "cleanup"})

    with recorded() as ran:
        with scripted([reply(calls=[("run_command", ask_cmd)]), reply(text="removed it")]):
            tid = _start_with(gated, "clean the scratch file up", name="scratch cleanup")
            card = _await(lambda: win.last("approval"))
            assert card["tool"] == "run_command", card
            assert card["origin"].startswith("task tk-"), card
            assert "scratch cleanup" in card["origin"], card
            assert broker.resolve(card["id"], True)
            _await(lambda: tasks.get(tid).status != "running" or None)
        assert ran == ["rm /tmp/tasks-check-scratch.txt"], ran
        assert tasks.get(tid).status == "done"
        assert broker.decisions[-1]["origin"] == card["origin"], broker.decisions[-1]

        # And a denial does not run it — while the task still finishes whole.
        ran.clear()
        win.sent.clear()
        seen: list[str] = []
        with scripted(
            [reply(calls=[("run_command", ask_cmd)]), reply(text="fine, left it alone")],
            seen_tool_results=seen,
        ):
            tid = _start_with(gated, "clean the scratch file up", name="scratch cleanup")
            card = _await(lambda: win.last("approval"))
            assert broker.resolve(card["id"], False)
            _await(lambda: tasks.get(tid).status != "running" or None)
        assert not ran, ran
        assert tasks.get(tid).status == "done"
        assert any("declined" in (r or "") for r in seen), seen
    print("ok  attribution: the card names the task; approve runs it, deny does not")


def remote_dm_checks() -> None:
    """No window: the task's ask goes out as a DM carrying the origin line."""
    sent: list[str] = []
    channel = DiscordApprovals(announce=lambda m: None, send=lambda text: sent.append(text) or "42")
    channel.timeout_s = 10.0  # bound a hang; the class default is 600s
    broker = ApprovalBroker(
        lambda kind, data: None, lambda: 0, timeout_s=10, announce=lambda m: None, remote=channel
    )
    channel.bind(broker)
    gated = permissions.gate(broker.approver(remote=True))
    ask_cmd = json.dumps({"command": "rm /tmp/tasks-check-remote.txt", "reason": "cleanup"})

    with recorded() as ran:
        with scripted([reply(calls=[("run_command", ask_cmd)]), reply(text="removed")]):
            tid = _start_with(gated, "clean up remotely", name="remote cleanup")
            _await(lambda: sent)
            body = sent[0]
            assert '\nfor task tk-' in body, body
            assert "remote cleanup" in body, body
            answer = channel.handle_reply("42", "yes")
            assert answer is not None and "Authorized" in answer, answer
            _await(lambda: tasks.get(tid).status != "running" or None)
        assert ran == ["rm /tmp/tasks-check-remote.txt"], ran
    assert broker.decisions[-1].get("origin", "").startswith("task tk-"), broker.decisions[-1]
    print("ok  remote: the DM names the task, and a typed yes runs it")


def conversation_unchanged_checks() -> None:
    """No origin bound -> the surfaces look exactly as they did before tasks."""
    win = FakeWindow(connected=1)
    broker = ApprovalBroker(win.broadcast, win.viewers, timeout_s=5, announce=lambda m: None)
    result: list[bool] = []
    waiter = threading.Thread(
        target=lambda: result.append(broker.request("run_command", {"command": "ls"}))
    )
    waiter.start()
    card = _await(lambda: win.last("approval"))
    assert "origin" not in card, card
    broker.resolve(card["id"], False)
    waiter.join(timeout=5)
    assert result == [False]
    assert "origin" not in broker.decisions[-1], broker.decisions[-1]

    # The DM body must be byte-identical to the pre-task shape — this check
    # fails if the origin line is ever added unconditionally.
    sent: list[str] = []
    channel = DiscordApprovals(announce=lambda m: None, send=lambda t: sent.append(t) or "7")
    import types

    item = types.SimpleNamespace(id="r1", tool="run_command", args={"command": "ls"})
    assert channel.ask(item)
    body = sent[0]
    code = re.search(r"code `([a-z0-9]{4})`", body).group(1)
    expected = (
        f"**AUTHORIZATION NEEDED** · code `{code}`\n"
        f"`run_command`\n```\n  command: ls\n```\n"
        f"Reply **yes** to allow, **no** to deny, **always** to allow this "
        f"and stop asking. Expires in 10 minutes, and anything "
        f"else I hear counts as neither."
    )
    assert body == expected, body
    print("ok  unchanged: no origin bound -> no origin key, DM body byte-identical")


def fail_closed_checks() -> None:
    """A task started where no approver was bound is deny-all, like a workflow."""
    ask_cmd = json.dumps({"command": "rm /tmp/tasks-check-unbound.txt", "reason": "no"})
    seen: list[str] = []
    with recorded() as ran:
        with scripted(
            [reply(calls=[("run_command", ask_cmd)]), reply(text="could not")],
            seen_tool_results=seen,
        ):
            tid = _start_with(None, "try something dangerous", name="unbound")
            _await(lambda: tasks.get(tid).status != "running" or None)
        assert not ran, ran
        assert any("declined" in (r or "") for r in seen), seen
    print("ok  fail-closed: an unbound context yields a deny-all task")


def flag_capture_checks() -> None:
    """The gate is captured whole: ALLOW-verdict commands auto-run in a task
    with no card (jarvis_human_backed survived), and the same command under a
    deny-all capture is declined (the flag is absent, so ALLOW is not honoured)."""
    win = FakeWindow(connected=1)
    broker = ApprovalBroker(win.broadcast, win.viewers, timeout_s=5, announce=lambda m: None)
    gated = permissions.gate(broker.approver())
    allow_cmd = json.dumps({"command": "git commit -m x", "reason": "save"})

    with recorded() as ran:
        with scripted([reply(calls=[("run_command", allow_cmd)]), reply(text="committed")]):
            tid = _start_with(gated, "commit the work", name="commit")
            _await(lambda: tasks.get(tid).status != "running" or None)
        assert ran == ["git commit -m x"], ran
        assert "approval" not in win.kinds(), win.sent

        ran.clear()
        seen: list[str] = []
        with scripted(
            [reply(calls=[("run_command", allow_cmd)]), reply(text="no")],
            seen_tool_results=seen,
        ):
            tid = _start_with(None, "commit the work", name="commit")
            _await(lambda: tasks.get(tid).status != "running" or None)
        assert not ran, ran
        assert any("declined" in (r or "") for r in seen), seen
    print("ok  flag capture: ALLOW auto-runs under a real gate, never under deny-all")


def context_block_checks() -> None:
    """Task state reaches only agents that hold the task tools."""
    armed = agent_mod.Agent(approve=lambda tool, args: False)
    with scripted([reply(text="hello")]):
        armed.run_turn("hi")
    state = agent_mod.durable_state(armed.messages)
    assert "## Background tasks" in state, "task section missing for a full-toolset agent"
    assert "tk-1" in state, state
    blocks = [
        m
        for m in armed.messages
        if isinstance(m.get("content"), str)
        and m["content"].startswith(agent_mod.CONTEXT_BLOCK_PREFIX)
    ]
    assert len(blocks) == 1, f"{len(blocks)} context blocks"

    unarmed = agent_mod.Agent(tool_names=["read_file"], approve=lambda tool, args: False)
    with scripted([reply(text="hello")]):
        unarmed.run_turn("hi")
    assert "Background tasks" not in agent_mod.durable_state(unarmed.messages), (
        "an agent without task tools must not see the task section"
    )
    print("ok  context block: tasks render for armed agents only, exactly one block")


def concurrency_checks() -> None:
    release = threading.Event()
    real = llm.chat

    def blocking_chat(model, messages, tools=None, **kwargs):
        release.wait(10)
        return reply(text="waited")

    llm.chat = blocking_chat
    try:
        while tasks.running_count() < tasks.MAX_CONCURRENT:
            tasks.start("wait around")
        try:
            tasks.start("one too many")
            raise AssertionError("the cap did not hold")
        except RuntimeError as exc:
            assert "wait" in str(exc)
        out = tools.dispatch("task_start", json.dumps({"task": "also too many"}))
        assert "Error" in out.text, out.text
    finally:
        release.set()
        # Drain before restoring, so a worker that has not yet reached its
        # chat call cannot make a paid API request.
        _await(lambda: tasks.running_count() == 0 or None)
        llm.chat = real
    print("ok  concurrency: the cap holds and surfaces as a tool error")


def cancel_checks() -> None:
    # A cancel lands at the next step boundary.
    release = threading.Event()
    real = llm.chat
    calls = {"n": 0}

    def latched_chat(model, messages, tools=None, **kwargs):
        calls["n"] += 1
        release.wait(10)
        return reply(calls=[("get_datetime", "{}")])

    llm.chat = latched_chat
    try:
        tsk = tasks.start("run until told to stop")
        _await(lambda: calls["n"] or None)
        out = tools.dispatch("task_cancel", json.dumps({"task_id": tsk.id}))
        assert "next step boundary" in out.text, out.text
        release.set()
        _await(lambda: tsk.status != "running" or None)
        assert tsk.status == "cancelled", tsk.status
    finally:
        release.set()
        _await(lambda: tasks.running_count() == 0 or None)
        llm.chat = real

    # Cancelling a task does not answer its open authorization for the owner.
    win = FakeWindow(connected=1)
    broker = ApprovalBroker(win.broadcast, win.viewers, timeout_s=10, announce=lambda m: None)
    gated = permissions.gate(broker.approver())
    ask_cmd = json.dumps({"command": "rm /tmp/tasks-check-cancel.txt", "reason": "x"})
    with recorded() as ran:
        with scripted([reply(calls=[("run_command", ask_cmd)]), reply(text="never sent")]):
            tid = _start_with(gated, "ask and hang", name="cancel me")
            card = _await(lambda: win.last("approval"))
            tools.dispatch("task_cancel", json.dumps({"task_id": tid}))
            time.sleep(0.2)
            assert broker.pending_count == 1, "cancel must not resolve the owner's question"
            assert broker.resolve(card["id"], False)
            _await(lambda: tasks.get(tid).status != "running" or None)
        assert tasks.get(tid).status == "cancelled", tasks.get(tid).status
        assert not ran, ran
    print("ok  cancel: stops at the boundary, never answers a pending card")


def origin_inheritance_checks() -> None:
    def child_of_task():
        runtime.bind(origin='task tk-9 "probe"')
        return contextvars.copy_context().run(runtime.origin)

    assert contextvars.copy_context().run(child_of_task) == 'task tk-9 "probe"'
    assert runtime.origin() == "", "an origin leaked onto the test thread"
    assert runtime.describe()["origin"] == ""
    print("ok  origin: inherited through copy_context, never leaked across contexts")


def main() -> int:
    # Never read (or write) the owner's real allowlist from a test: gate()
    # consults it, so a stray entry would silently change what this suite
    # measures — and the ALWAYS path writes to it.
    tmp = tempfile.TemporaryDirectory()
    config.ALLOWLIST_PATH = Path(tmp.name) / "allowlist.json"

    toolset_checks()
    lifecycle_checks()
    attribution_card_checks()
    remote_dm_checks()
    conversation_unchanged_checks()
    fail_closed_checks()
    flag_capture_checks()
    context_block_checks()
    concurrency_checks()
    cancel_checks()
    origin_inheritance_checks()
    print("\nall task checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
