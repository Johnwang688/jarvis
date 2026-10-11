"""Checks that dispatch() enforces the calling agent's toolset. Free — no API.

`tools._dispatch` used to look a call up in the global `REGISTRY` and nowhere
else, so a model that simply *named* a tool its agent was never offered reached
it: a dangerous one still met the approver, a non-dangerous one just ran. Every
toolset in the codebase was therefore advisory — only true of the schemas sent.
A reviewer of PR #33 showed it with a scripted v2 fast-path turn whose
`write_file` created a file. Since 2026-10-10 the toolset is enforced at
dispatch (tools._toolset_refusal).

What must hold, each against the surface that claims it:

  * a workflow naming `browser_snapshot` / `browser_goto` is refused;
  * a sub-agent naming a tool outside its intersection is refused, alone and
    in a fleet;
  * an attended task naming `task_start` or `desktop_*` is refused;
  * a goal naming `desktop_*` is refused;
  * a v2 fast-path turn naming `write_file`, `edit_file` or `run_command` is
    refused and nothing is written — and a brief's narrower set holds too;
  * a bench's pinned toolset holds (agent-bench naming `fetch_page`);
  * jarvis-mcp's worker binds the list it exposes;
  * the refusal is text keyed to its call id, returned before the arguments
    are read, and the transcript stays wire-valid;
  * a parallel batch with one refused call answers every call id, in order;
  * a deferred group keeps its `load_tools` pointer — only for an agent that
    could load it — and after `load_tools` the tool runs; an agent without the
    group's core cannot load it;
  * a tool an agent was handed explicitly (PR #33's EXPLICIT_ONLY shape) runs
    for that agent and no other;
  * a turn's toolset is bound only while the turn runs (restored after).

And the surface walk, which is what makes "unbound fails open" safe: the only
callers of `dispatch` under `jarvis/` are `Agent._dispatch_one` (reached only
from `_run_turn`, after its bind) and jarvis-mcp's worker, and every dispatch
made by every surface this suite drives — workflow, task, goal, sub-agent,
fleet, fast path, mcp, agent-bench, the CLI, the Discord agent, the face's
voice agent and its designer — ran with a toolset bound, equal to that agent's.

Run against origin/main (34b6ab2), sixteen of the twenty checks fail. The four
that pass there do so by design: the static call-site pin, the deferred
pointer (marked "regression guard"), the conversation-surface binding and the
unbound decision.

`llm.chat` is scripted; tools that would touch the world (browser, desktop,
shell, task spawning, Spotify, the network) are replaced by recorders under
their own names. HOME and every config path are temp dirs.

Run:  PYTHONPATH=. .venv/bin/python tests/dispatch_toolset_check.py
"""

from __future__ import annotations

import ast
import contextlib
import contextvars
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_TMP = tempfile.TemporaryDirectory(prefix="jarvis-dispatch-toolset-")
TMP = Path(_TMP.name)
(TMP / "home").mkdir()
os.environ["HOME"] = str(TMP / "home")  # before config computes any default

from jarvis import config  # noqa: E402

config.ALLOWLIST_PATH = TMP / "allowlist.json"
config.MODELS_PATH = TMP / "models.json"
config.PROVIDER_DEFAULTS_PATH = TMP / "provider_defaults.json"
config.MODEL_CACHE_PATH = TMP / "catalog.json"
config.ROUTING_PATH = TMP / "routing.json"
config.CODEX_CATALOG_PATH = TMP / "codex_catalog.json"
config.DISCORD_GUILD_PATH = TMP / "discord_guild.json"
config.SPILL_DIR = TMP / "spill"
config.SESSIONS_DIR = TMP / "sessions"
config.MEMORY_DIR = TMP / "memory"
config.SKILLS_DIR = TMP / "skills"
config.GOALS_DIR = TMP / "goals"
config.GIT_WORKTREES_DIR = TMP / "worktrees"
config.DESIGNS_DIR = TMP / "designs"
config.VOICES_DIR = TMP / "voices"
config.AVATAR_STATE_PATH = TMP / "avatar.json"
config.AVATAR_ENV = "jarvis"
config.SPOTIFY_TOKEN_PATH = TMP / "spotify_token.json"  # absent: group unavailable
for _d in ("memory", "skills", "sessions", "goals", "spill", "designs"):
    (TMP / _d).mkdir(exist_ok=True)

from jarvis import agent as agent_mod  # noqa: E402
from jarvis import goals, llm, runtime, tasks, tools, workflows  # noqa: E402
from jarvis.goalrunner import GoalRunner  # noqa: E402
from jarvis.v2 import mcp  # noqa: E402
from jarvis.v2.model import ProviderName, Role, Thread  # noqa: E402
from jarvis.v2.provider import Brief, Decision, UserMessage  # noqa: E402
from jarvis.v2.providers.fastpath import FastPathProvider  # noqa: E402

NOT_AVAILABLE = "is not available to this agent"


# --- harness ----------------------------------------------------------------


def reply(text="", calls=None):
    message: dict = {"content": text or None}
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


def _first_user_text(messages) -> str:
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if isinstance(content, str) and not content.startswith(agent_mod.CONTEXT_BLOCK_PREFIX):
            return content
    return ""


class Script:
    """llm.chat, scripted per conversation.

    Each plan is keyed by a marker in the conversation's first user message, so
    a parent, its children and a background thread can share one fake. A plan
    that runs out answers "done", which ends any turn.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.plans: dict[str, list] = {}
        self.calls: dict[str, list[tuple[list, set]]] = {}

    def add(self, marker: str, *replies) -> None:
        with self.lock:
            self.plans[marker] = list(replies)
            self.calls[marker] = []

    def __call__(self, model, messages, tools=None, **kwargs):
        first = _first_user_text(messages)
        offered = {spec["function"]["name"] for spec in (tools or [])}
        with self.lock:
            for marker, queue in self.plans.items():
                if marker in first:
                    self.calls[marker].append((list(messages), offered))
                    return queue.pop(0) if queue else reply("done")
        return reply("done")

    def tool_results(self, marker: str) -> list[dict]:
        """The tool messages the marker's conversation had sent back, last call."""
        with self.lock:
            seen = self.calls.get(marker) or []
            if not seen:
                return []
            return [m for m in seen[-1][0] if m.get("role") == "tool"]


SCRIPT = Script()
llm.chat = SCRIPT  # every check in this file; never restored to the real client


RAN: list[tuple[str, frozenset | None]] = []
_RAN_LOCK = threading.Lock()
_OPEN_SCHEMA = {"type": "object", "properties": {}, "required": [], "additionalProperties": True}


@contextlib.contextmanager
def recorders(*names):
    """Replace real tools with recorders under their own names (and their own
    `dangerous` flag), so a check can tell whether a call ran without letting
    it touch the browser, the desktop, a shell, Spotify or the network.
    `RAN` starts empty for each use, so `ran()` counts this block's calls."""
    with _RAN_LOCK:
        RAN.clear()
    saved = {name: tools.REGISTRY[name] for name in names}
    for name, real in saved.items():
        def ran(_name=name, **kwargs):
            with _RAN_LOCK:
                RAN.append((_name, runtime.parent_tools()))
            return f"ran {_name}"

        tools.REGISTRY[name] = tools.Tool(
            name=name, description=real.description, schema=_OPEN_SCHEMA,
            func=ran, dangerous=real.dangerous,
        )
    try:
        yield
    finally:
        tools.REGISTRY.update(saved)


def ran(name: str) -> int:
    with _RAN_LOCK:
        return sum(1 for n, _ in RAN if n == name)


# The surface-walk probe: every dispatch, the toolset bound when it happened,
# and which surface was being driven. Installed for the whole run.
SURFACE = ["(none)"]
PROBE: list[tuple[str, str, frozenset | None]] = []
_REAL_DISPATCH = tools._dispatch


def _probe(name, raw_arguments, approve=None):
    PROBE.append((SURFACE[0], name, runtime.parent_tools()))
    return _REAL_DISPATCH(name, raw_arguments, approve)


tools._dispatch = _probe


def assert_wire_valid(messages) -> None:
    declared, answered = [], []
    for message in messages:
        for call in message.get("tool_calls") or []:
            declared.append(call["id"])
        if message.get("role") == "tool":
            answered.append(message["tool_call_id"])
    assert declared == answered, f"declared {declared} answered {answered}"


def wait_for(predicate, what: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def deny(tool, args) -> bool:
    return False


# --- surfaces ---------------------------------------------------------------


def workflow_checks() -> None:
    SURFACE[0] = "workflow"
    SCRIPT.add(
        "[wf-browser]",
        reply(calls=[("browser_snapshot", "{}"),
                     ("browser_goto", json.dumps({"url": "https://example.com"}))]),
        reply("done"),
    )
    with recorders("browser_snapshot", "browser_goto"):
        wf = workflows.start("[wf-browser] look at a page")
        wait_for(lambda: wf.status != "running", "the workflow")
    assert wf.status == "done", (wf.status, wf.result)
    assert ran("browser_snapshot") == 0 and ran("browser_goto") == 0, RAN
    results = SCRIPT.tool_results("[wf-browser]")
    assert [m["tool_call_id"] for m in results] == ["c0", "c1"], results
    for message, name in zip(results, ("browser_snapshot", "browser_goto")):
        assert NOT_AVAILABLE in message["content"] and name in message["content"], message
    offered = SCRIPT.calls["[wf-browser]"][0][1]
    assert not any(n.startswith("browser_") for n in offered), "never offered either"
    print("ok  workflow: naming browser_snapshot / browser_goto is refused, never run")


def subagent_checks() -> None:
    SURFACE[0] = "sub-agent"
    target = TMP / "subagent-wrote.txt"
    SCRIPT.add(
        "[wf-parent]",
        reply(calls=[("run_subagent", json.dumps(
            {"task": "[child-browser] open the page", "type": "browser"}))]),
        reply(calls=[("run_subagent", json.dumps(
            {"task": "[child-explorer] note something", "type": "explorer"}))]),
        reply("parent done"),
    )
    SCRIPT.add("[child-browser]",
               reply(calls=[("browser_goto", json.dumps({"url": "https://example.com"}))]),
               reply("child done"))
    SCRIPT.add("[child-explorer]",
               reply(calls=[("write_file", json.dumps({"path": str(target), "content": "x"}))]),
               reply("child done"))
    with recorders("browser_goto"):
        # A workflow parent: no browser of its own, so a browser-type child is
        # intersected down to none — and must not get one by naming it. The
        # explorer child's parent *does* hold write_file; the explorer type
        # does not, so the intersection excludes it.
        wf = workflows.start("[wf-parent] delegate twice")
        wait_for(lambda: wf.status != "running", "the parent workflow")
    assert wf.status == "done", (wf.status, wf.result)
    assert ran("browser_goto") == 0, RAN
    assert not target.exists(), "an explorer child wrote a file it was never given"
    for marker, name in (("[child-browser]", "browser_goto"), ("[child-explorer]", "write_file")):
        results = SCRIPT.tool_results(marker)
        assert len(results) == 1 and results[0]["tool_call_id"] == "c0", results
        assert NOT_AVAILABLE in results[0]["content"] and name in results[0]["content"], results
    print("ok  sub-agent: a tool outside the child's intersection is refused (browser, write_file)")


def fleet_checks() -> None:
    from jarvis import __main__ as cli

    SURFACE[0] = "fleet (CLI parent)"
    paths = {key: TMP / f"fleet-{key}.txt" for key in ("a", "b")}
    jobs = [{"task": f"[fleet-{key}] note something", "type": "explorer"} for key in paths]
    SCRIPT.add("[cli-fleet]", reply(calls=[("run_fleet", json.dumps({"jobs": json.dumps(jobs)}))]),
               reply("fleet done"))
    for key, path in paths.items():
        SCRIPT.add(f"[fleet-{key}]",
                   reply(calls=[("write_file", json.dumps({"path": str(path), "content": key})),
                                ("run_command", json.dumps({"command": f"touch {path}"}))]),
                   reply("child done"))
    parent = cli._make_agent(None, None)
    with recorders("run_command"):
        turn = parent.run_turn("[cli-fleet] map two things at once")
    assert turn.text == "fleet done", turn.text
    assert ran("run_command") == 0, RAN
    assert not any(path.exists() for path in paths.values()), "a fleet child wrote a file"
    for key in paths:
        results = SCRIPT.tool_results(f"[fleet-{key}]")
        assert [m["tool_call_id"] for m in results] == ["c0", "c1"], results
        assert all(NOT_AVAILABLE in m["content"] for m in results), results
    assert_wire_valid(parent.messages)
    fleet_threads = [held for surface, name, held in PROBE
                     if surface == SURFACE[0] and name in ("write_file", "run_command")]
    assert len(fleet_threads) == 4 and all(h is not None for h in fleet_threads), fleet_threads
    print("ok  fleet: concurrent children are each held to their own set (write_file, run_command)")


def task_checks() -> None:
    SURFACE[0] = "attended task"
    asked = []

    def approve(tool, args):  # would say yes — so only the toolset can refuse
        asked.append(tool.name)
        return True

    SCRIPT.add(
        "[task-spawn]",
        reply(calls=[("task_start", json.dumps({"task": "recurse"})),
                     ("desktop_open", json.dumps({"app": "settings"})),
                     ("desktop_snapshot", "{}")]),
        reply("done"),
    )
    with recorders("task_start", "desktop_open", "desktop_snapshot"):
        tsk = tasks.start("[task-spawn] do a job", approve=approve)
        wait_for(lambda: tsk.status != "running", "the task")
    assert tsk.status == "done", (tsk.status, tsk.result)
    assert not [n for n, _ in RAN if n in ("task_start", "desktop_open", "desktop_snapshot")], RAN
    assert not asked, f"a refused tool reached the approver: {asked}"
    results = SCRIPT.tool_results("[task-spawn]")
    assert [m["tool_call_id"] for m in results] == ["c0", "c1", "c2"], results
    assert all(NOT_AVAILABLE in m["content"] for m in results), results
    print("ok  attended task: naming task_start or desktop_* is refused before any approver")


def goal_checks() -> None:
    SURFACE[0] = "goal"
    goal = goals.create("[goal-desk] tidy up")
    runner = GoalRunner(approver=deny, notify=lambda text: None, announce=lambda text: None)
    agent = runner._default_agent(goal)
    held = {spec["function"]["name"] for spec in agent.tool_specs}
    assert "goal_report" in held and not any(n.startswith("desktop_") for n in held)
    SCRIPT.add("[goal-desk]",
               reply(calls=[("desktop_click", json.dumps({"app": "settings", "ref": "e1"}))]),
               reply("done"))
    with recorders("desktop_click"):
        agent.run_turn("[goal-desk] continue")
    assert ran("desktop_click") == 0, RAN
    results = [m for m in agent.messages if m.get("role") == "tool"]
    assert len(results) == 1 and NOT_AVAILABLE in results[0]["content"], results
    assert_wire_valid(agent.messages)
    print("ok  goal: naming desktop_click is refused")


def _thread(tid):
    return Thread(id=tid, project_id="p1", role=Role.CHAT, provider=ProviderName.FAST)


def fastpath_checks() -> None:
    SURFACE[0] = "v2 fast path"
    provider = FastPathProvider()
    existing = TMP / "fast-existing.txt"
    existing.write_text("original\n", encoding="utf-8")
    created = TMP / "fast-created.txt"

    handle = provider.start(_thread("fp1"), Brief(role=Role.CHAT, cwd=str(TMP)),
                            lambda name, args, brief: Decision.ALLOW)
    SCRIPT.add(
        "[fast-write]",
        reply(calls=[
            ("read_file", json.dumps({"path": str(existing)})),
            ("write_file", json.dumps({"path": str(created), "content": "pwned"})),
            ("edit_file", json.dumps({"path": str(existing), "old_string": "original",
                                      "new_string": "pwned"})),
            ("run_command", json.dumps({"command": f"touch {created}"})),
        ]),
        reply("done"),
    )
    with recorders("run_command"):
        list(provider.send(handle, UserMessage(text="[fast-write] change the file")))
    agent = handle.native.agent
    assert not created.exists(), "a fast-path turn created a file"
    assert existing.read_text(encoding="utf-8") == "original\n", "a fast-path turn edited a file"
    assert ran("run_command") == 0, RAN
    results = [m for m in agent.messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in results] == ["c0", "c1", "c2", "c3"], results
    assert "original" in results[0]["content"], "read_file is the fast path's own"
    for message, name in zip(results[1:], ("write_file", "edit_file", "run_command")):
        assert NOT_AVAILABLE in message["content"] and name in message["content"], message
    assert_wire_valid(agent.messages)

    # A brief may narrow the set; the narrower set is the boundary too.
    narrow = provider.start(_thread("fp2"),
                            Brief(role=Role.CHAT, cwd=str(TMP), allowed_tools=["get_datetime"]),
                            lambda name, args, brief: Decision.ALLOW)
    SCRIPT.add("[fast-narrow]",
               reply(calls=[("memory_write", json.dumps({"name": "x", "content": "y"}))]),
               reply("done"))
    list(provider.send(narrow, UserMessage(text="[fast-narrow] remember this")))
    assert not any(config.MEMORY_DIR.iterdir()), "a narrowed brief wrote memory"
    results = [m for m in narrow.native.agent.messages if m.get("role") == "tool"]
    assert len(results) == 1 and NOT_AVAILABLE in results[0]["content"], results
    print("ok  v2 fast path: write_file / edit_file / run_command refused, nothing written; "
          "a brief's narrower set holds")


def agentbench_checks() -> None:
    from jarvis import agentbench

    SURFACE[0] = "agent-bench"
    task = next(t for t in agentbench.TASKS if t.tools == agentbench.LOCAL_TOOLS)
    marker = task.turns[0][:60]
    SCRIPT.add(marker, reply(calls=[("fetch_page", json.dumps({"url": "https://example.com"}))]),
               reply("done"))
    with recorders("fetch_page"):
        agentbench.run_task("stub", task)
    assert ran("fetch_page") == 0, "a bench's pinned toolset let the model reach the network"
    results = SCRIPT.tool_results(marker)
    assert results and NOT_AVAILABLE in results[0]["content"], results
    print(f"ok  agent-bench: the pinned toolset holds ('{task.name}' naming fetch_page)")


def mcp_checks() -> None:
    SURFACE[0] = "jarvis-mcp"
    refused = mcp.call_tool("write_file", {"path": str(TMP / "mcp.txt"), "content": "x"})
    assert refused["isError"] and not (TMP / "mcp.txt").exists(), refused
    with recorders("get_datetime"):
        result = mcp.call_tool("get_datetime", {})
    assert result["content"][0]["text"] == "ran get_datetime", result
    with _RAN_LOCK:
        bound = [held for name, held in RAN if name == "get_datetime"][-1]
    assert bound == frozenset(mcp.available_tools()), bound
    print("ok  jarvis-mcp: refuses what it does not expose, and binds the list it does")


def conversation_surface_checks() -> None:
    """The conversation agents — CLI, Discord, the face and its designer — hold
    the default registry, so the check here is the binding, not a refusal: a
    dispatch from each must see exactly that agent's toolset."""
    from jarvis import __main__ as cli
    from jarvis import discord_agent, sessions
    from jarvis.face import server

    class Broker:
        def approver(self, remote=False):
            return deny

    server.set_session(sessions.new("face"))
    builders = {
        "CLI chat": lambda: cli._make_agent(None, None),
        "Discord agent": lambda: discord_agent.DiscordResponder(Broker(), None)._default_agent(),
        "face voice agent": server._get_agent,
        "face designer": server._get_designer,
    }
    for label, build in builders.items():
        SURFACE[0] = label
        agent = build()
        marker = f"[surface-{label}]"
        SCRIPT.add(marker, reply(calls=[("get_datetime", "{}"), ("spotify_pause", "{}")]),
                   reply("done"))
        with recorders("spotify_pause"):
            agent.run_turn(f"{marker} what time is it")
        held = frozenset(spec["function"]["name"] for spec in agent.tool_specs)
        seen = [h for surface, name, h in PROBE if surface == label]
        assert seen and all(h == held for h in seen), (label, seen)
        assert ran("spotify_pause") == 0, (label, RAN)
    print("ok  conversation surfaces: CLI, Discord, face and designer each dispatch bound to "
          "their own toolset")


# --- the refusal itself -----------------------------------------------------


def refusal_shape_checks() -> None:
    SURFACE[0] = "direct agent"
    agent = agent_mod.Agent(tool_names=["get_datetime"], approve=deny)
    SCRIPT.add("[shape]",
               reply(calls=[("write_file", "{this is not json"), ("teleport", "{}")]),
               reply("done"))
    agent.run_turn("[shape] go")
    results = [m for m in agent.messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in results] == ["c0", "c1"], results
    refusal, unknown = results[0]["content"], results[1]["content"]
    # Refused before the arguments were read: no JSON error, the tool named,
    # and the agent's own tools listed rather than the registry.
    assert NOT_AVAILABLE in refusal and "write_file" in refusal, refusal
    assert "not valid JSON" not in refusal, refusal
    assert "Your tools: get_datetime." in refusal, refusal
    assert "no tool named 'teleport'" in unknown and "Available: get_datetime" in unknown, unknown
    assert "read_file" not in unknown, "an unknown name must not list the whole registry"
    assert_wire_valid(agent.messages)

    # A large toolset is listed capped, and says so. In a copy of the context,
    # so this thread is not left bound to a toolset no agent owns.
    big = sorted(tools.default_names())
    cap = getattr(tools, "REFUSAL_LIST_CAP", 40)  # getattr: this file also runs against main
    assert len(big) > cap, len(big)

    def capped():
        runtime.bind(tool_names=big[1:])
        return tools.dispatch(big[0], "{}").text

    text = contextvars.copy_context().run(capped)
    assert NOT_AVAILABLE in text and f"and {len(big) - 1 - cap} more" in text, text
    print("ok  refusal: text keyed to its call id, before argument parsing, own tools listed "
          "(capped); transcript wire-valid")


def parallel_checks() -> None:
    SURFACE[0] = "parallel batch"
    note = TMP / "parallel.txt"
    note.write_text("alpha\n", encoding="utf-8")
    agent = agent_mod.Agent(tool_names=["read_file", "grep_files", "get_datetime"], approve=deny)
    assert all(tools.parallelizable(n) for n in ("read_file", "list_dir", "get_datetime"))
    SCRIPT.add("[parallel]",
               reply(calls=[("read_file", json.dumps({"path": str(note)})),
                            ("list_dir", json.dumps({"path": str(TMP)})),
                            ("get_datetime", "{}")]),
               reply("done"))
    with recorders("list_dir"):
        agent.run_turn("[parallel] three at once")
    assert ran("list_dir") == 0, RAN
    results = [m for m in agent.messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in results] == ["c0", "c1", "c2"], results
    assert "alpha" in results[0]["content"], results[0]
    assert NOT_AVAILABLE in results[1]["content"] and "list_dir" in results[1]["content"], results[1]
    assert NOT_AVAILABLE not in results[2]["content"], results[2]
    assert_wire_valid(agent.messages)
    workers = [h for surface, name, h in PROBE if surface == SURFACE[0]]
    assert len(workers) == 3 and all(h is not None for h in workers), workers
    print("ok  parallel: one refused call in a batch, every id answered in call order, "
          "each worker bound")


# --- deferred groups and explicit tools ---------------------------------------


@contextlib.contextmanager
def spotify_connected():
    """Spotify's group available (a token bundle exists), for the duration."""
    config.SPOTIFY_TOKEN_PATH.write_text(json.dumps({"refresh_token": "x"}), encoding="utf-8")
    try:
        yield
    finally:
        config.SPOTIFY_TOKEN_PATH.unlink(missing_ok=True)


def deferred_pointer_checks() -> None:
    """(regression guard) The pointer, then load_tools, then the tool runs."""
    SURFACE[0] = "deferred group"
    with spotify_connected():
        names = tools.default_names()
        assert "load_tools" in names and "spotify_play" in names and "spotify_pause" not in names
        agent = agent_mod.Agent(tool_names=names, approve=deny)
        SCRIPT.add("[deferred]",
                   reply(calls=[("spotify_pause", "{}")]),
                   reply(calls=[("load_tools", json.dumps({"group": "spotify"}))]),
                   reply(calls=[("spotify_pause", "{}")]),
                   reply("done"))
        with recorders("spotify_pause"):
            agent.run_turn("[deferred] pause the music")
    results = [m["content"] for m in agent.messages if m.get("role") == "tool"]
    assert "load_tools('spotify')" in results[0] and "not loaded" in results[0], results[0]
    assert "Loaded the 'spotify' tools" in results[1], results[1]
    assert results[2] == "ran spotify_pause" and ran("spotify_pause") == 1, results
    assert_wire_valid(agent.messages)
    print("ok  deferred (regression guard): load_tools pointer, then the tool runs after "
          "load_tools")


def deferred_no_pointer_checks() -> None:
    """No pointer for an agent that cannot load: the fast path's shape holds
    the group's core but not load_tools."""
    SURFACE[0] = "deferred group"
    with spotify_connected():
        fast_shape = agent_mod.Agent(tool_names=["spotify_play", "spotify_status"], approve=deny)
        SCRIPT.add("[deferred-noload]", reply(calls=[("spotify_pause", "{}")]), reply("done"))
        with recorders("spotify_pause"):
            fast_shape.run_turn("[deferred-noload] pause")
    text = [m["content"] for m in fast_shape.messages if m.get("role") == "tool"][0]
    assert NOT_AVAILABLE in text and "load_tools" not in text, text
    assert ran("spotify_pause") == 0, RAN
    print("ok  deferred: no load_tools pointer for an agent that cannot call load_tools")


def deferred_widening_checks() -> None:
    """An agent holding load_tools but not the group's core cannot widen into
    it: load_tools refuses, and the group's tools stay refused after."""
    SURFACE[0] = "deferred group"
    with spotify_connected():
        narrow = agent_mod.Agent(tool_names=["read_file", "load_tools"], approve=deny)
        SCRIPT.add("[deferred-widen]",
                   reply(calls=[("load_tools", json.dumps({"group": "spotify"}))]),
                   reply(calls=[("spotify_search", json.dumps({"query": "x"}))]),
                   reply("done"))
        with recorders("spotify_search"):
            narrow.run_turn("[deferred-widen] find a song")
    results = [m["content"] for m in narrow.messages if m.get("role") == "tool"]
    assert "not available to this agent" in results[0] and "Loaded" not in results[0], results
    assert NOT_AVAILABLE in results[1], results[1]
    assert ran("spotify_search") == 0, RAN
    assert not any(s["function"]["name"].startswith("spotify_") for s in narrow.tool_specs)
    print("ok  deferred: load_tools refuses a group whose core the agent was not given")


def deferred_sync_checks() -> None:
    """The second half of that rule: a group written into the agent's loaded
    set some other way is not folded into its toolset without its core."""
    with spotify_connected():
        narrow = agent_mod.Agent(tool_names=["read_file", "load_tools"], approve=deny)
        narrow.loaded_groups.add("spotify")
        narrow._sync_tools()
    assert not any(s["function"]["name"].startswith("spotify_") for s in narrow.tool_specs), \
        sorted(s["function"]["name"] for s in narrow.tool_specs)
    print("ok  deferred: _sync_tools folds in no group whose core the agent lacks")


def explicit_tool_checks() -> None:
    """PR #33's shape: a registered tool no surface gets by default
    (`tools.EXPLICIT_ONLY`), handed to one agent explicitly."""
    SURFACE[0] = "explicit tool"
    name = "zz_explicit_probe"
    calls = []
    tools.REGISTRY[name] = tools.Tool(
        name=name, description="probe", schema=_OPEN_SCHEMA,
        func=lambda **kw: calls.append(1) or "probe ran",
    )
    explicit = getattr(tools, "EXPLICIT_ONLY", None)
    if explicit is not None:
        explicit.add(name)
    try:
        everyone = [n for n in tools.default_names() if n != name]
        handed = agent_mod.Agent(tool_names=["get_datetime", name], approve=deny)
        other = agent_mod.Agent(tool_names=everyone, approve=deny)
        for agent, marker in ((handed, "[explicit-handed]"), (other, "[explicit-other]")):
            SCRIPT.add(marker, reply(calls=[(name, "{}")]), reply("done"))
            agent.run_turn(f"{marker} read it")
        handed_result = [m["content"] for m in handed.messages if m.get("role") == "tool"][0]
        other_result = [m["content"] for m in other.messages if m.get("role") == "tool"][0]
        assert handed_result == "probe ran", handed_result
        assert NOT_AVAILABLE in other_result, other_result
        assert len(calls) == 1, calls
    finally:
        tools.REGISTRY.pop(name, None)
        if explicit is not None:
            explicit.discard(name)
    print("ok  explicit tool: runs for the agent handed it, refused for one that was not")


def unbound_checks() -> None:
    """The decision, pinned: nothing bound means no agent is calling, so
    dispatch does not enforce — a fresh thread's empty context, as a script or
    a test has. Safe only because no real path is unbound (walk below)."""
    SURFACE[0] = "unbound (expected)"
    out = {}

    def direct():
        out["held"] = runtime.parent_tools()
        out["text"] = tools.dispatch("get_datetime", "{}").text

    worker = threading.Thread(target=direct)
    worker.start()
    worker.join()
    assert out["held"] is None and NOT_AVAILABLE not in out["text"], out
    print("ok  unbound: a direct call with no agent bound is not enforced (documented)")


def restore_checks() -> None:
    """"Bound" means inside an agent's turn: a turn puts the toolset back as
    it found it, so a thread that ran one does not go on holding that agent's
    set (a later direct call would be judged against an agent no longer
    running), and a turn run inside another's context hands its set back."""
    SURFACE[0] = "restore"
    out = {}

    def fresh_thread():
        agent = agent_mod.Agent(tool_names=["get_datetime"], approve=deny)
        SCRIPT.add("[restore-fresh]", reply("done"))
        agent.run_turn("[restore-fresh] hi")
        out["after"] = runtime.parent_tools()

    worker = threading.Thread(target=fresh_thread)
    worker.start()
    worker.join()
    assert out["after"] is None, f"a finished turn left its toolset bound: {out['after']}"

    def nested():
        runtime.bind(tool_names={"read_file", "get_datetime"})
        inner = agent_mod.Agent(tool_names=["get_datetime"], approve=deny)
        SCRIPT.add("[restore-nested]", reply("done"))
        inner.run_turn("[restore-nested] hi")
        return runtime.parent_tools()

    after = contextvars.copy_context().run(nested)
    assert after == frozenset({"read_file", "get_datetime"}), after
    print("ok  restore: a turn's toolset is bound only while the turn runs")


# --- the surface walk -------------------------------------------------------


def _calls_named(tree: ast.AST, attr: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == attr) or (
                isinstance(func, ast.Name) and func.id == attr
            ):
                yield node


def _enclosing(tree: ast.AST) -> dict[int, str]:
    """line -> innermost enclosing def name."""
    owner: dict[int, str] = {}

    def visit(node, name):
        for child in ast.iter_child_nodes(node):
            inner = name
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner = child.name
            for line in range(getattr(child, "lineno", 0), getattr(child, "end_lineno", 0) + 1):
                owner[line] = inner
            visit(child, inner)

    visit(tree, "<module>")
    return owner


def static_walk_checks() -> None:
    """Every caller of dispatch in the code (jarvis/ and the bench packages
    beside it), and how Agent reaches it."""
    sites = set()
    sources = [p for package in ("jarvis", "longbench", "swecompare")
               for p in sorted((REPO / package).rglob("*.py"))]
    for path in sources:
        rel = path.relative_to(REPO).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = _enclosing(tree)
        for attr in ("dispatch", "_dispatch"):
            for call in _calls_named(tree, attr):
                func = call.func
                receiver = func.value.id if isinstance(func, ast.Attribute) and isinstance(
                    func.value, ast.Name) else None
                if isinstance(func, ast.Attribute) and receiver != "tools":
                    continue  # some other object's .dispatch()
                sites.add((rel, owner.get(call.lineno, "?"), attr))
        # Nothing may call a tool's function around dispatch. (`args.func` is
        # argparse's subcommand handler in the CLI, not a tool.)
        for call in _calls_named(tree, "func"):
            receiver = call.func.value if isinstance(call.func, ast.Attribute) else None
            if isinstance(receiver, ast.Name) and receiver.id == "args":
                continue
            assert rel == "jarvis/tools/__init__.py", f"{rel}:{call.lineno} calls .func() directly"
    expected = {
        ("jarvis/tools/__init__.py", "dispatch", "_dispatch"),
        ("jarvis/agent.py", "_dispatch_one", "dispatch"),
        ("jarvis/v2/mcp.py", "run", "dispatch"),
    }
    assert sites == expected, f"dispatch callers changed: {sorted(sites ^ expected)}"

    agent_tree = ast.parse((REPO / "jarvis" / "agent.py").read_text(encoding="utf-8"))
    owner = _enclosing(agent_tree)
    one = {owner[c.lineno] for c in _calls_named(agent_tree, "_dispatch_one")}
    calls = {owner[c.lineno] for c in _calls_named(agent_tree, "_dispatch_calls")}
    assert one == {"_dispatch_calls"} and calls == {"_run_turn"}, (one, calls)
    run_turn = next(n for n in ast.walk(agent_tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "_run_turn")
    binds = [c for c in _calls_named(run_turn, "bind")
             if any(k.arg == "tool_names" for k in c.keywords)]
    dispatches = list(_calls_named(run_turn, "_dispatch_calls"))
    assert binds and min(b.lineno for b in binds) < min(d.lineno for d in dispatches)
    print("ok  static walk: dispatch is called only by Agent._dispatch_one (from _run_turn, "
          "after its bind) and jarvis-mcp's worker")


def dynamic_walk_checks() -> None:
    by_surface: dict[str, int] = {}
    for surface, name, held in PROBE:
        if surface == "unbound (expected)":
            assert held is None
            continue
        assert held is not None, f"{surface}: {name} dispatched with no toolset bound"
        by_surface[surface] = by_surface.get(surface, 0) + 1
    wanted = {"workflow", "sub-agent", "fleet (CLI parent)", "attended task", "goal",
              "v2 fast path", "agent-bench", "jarvis-mcp", "CLI chat", "Discord agent",
              "face voice agent", "face designer", "parallel batch", "deferred group"}
    missing = wanted - by_surface.keys()
    assert not missing, f"surfaces that never dispatched: {missing}"
    total = sum(by_surface.values())
    print(f"ok  dynamic walk: {total} dispatches across {len(by_surface)} surfaces, every one "
          "with a toolset bound")


def main() -> int:
    static_walk_checks()
    refusal_shape_checks()
    parallel_checks()
    workflow_checks()
    subagent_checks()
    fleet_checks()
    task_checks()
    goal_checks()
    fastpath_checks()
    agentbench_checks()
    mcp_checks()
    deferred_pointer_checks()
    deferred_no_pointer_checks()
    deferred_widening_checks()
    deferred_sync_checks()
    explicit_tool_checks()
    conversation_surface_checks()
    unbound_checks()
    restore_checks()
    dynamic_walk_checks()
    print("\nall dispatch toolset checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
