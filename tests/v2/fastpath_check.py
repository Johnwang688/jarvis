"""Checks for the v2 FastPathProvider. Free — `llm.chat` is faked, no key needed.

Three things are under test, in descending order of how badly they fail:

  **The structural boundary (§8.1).** The fast path must hold no tool that can
  change anything. A mutating tool slipping into `FAST_TOOLS` does not raise —
  it just lets a chat turn edit a file — so the exclusions are asserted by name
  and by prefix, and a `Brief` may narrow the set but never widen it.

  **The proposal channel (§8.1).** A turn that needs real work must come out
  the other end as a proposal: explicitly through `task_propose`, or implicitly
  because the step budget ran out. The budget rule is the one worth having a
  test for — v1's exhaustion handoff pointed at a task instead of at the owner.

  **The event translation (§5.2).** One v1 `on_event` stream becomes the one
  `Event` stream every v2 surface reads. Order matters (deltas during the turn,
  not after it), and so does the transcript being wire-valid after a cancel —
  invariant 3, which is why v1 only cancels at a step boundary.

Every path that could touch the owner's real machine state is pointed at a temp
directory first: sessions, the allowlist, the model roster and its catalog
cache, skills, and the avatar pin. A suite must not read live machine state any
more than it may write it.

Run:  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python tests/v2/fastpath_check.py
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import config  # noqa: E402

_TMP = tempfile.TemporaryDirectory(prefix="jarvis-fastpath-check-")
_ROOT = Path(_TMP.name)
config.SESSIONS_DIR = _ROOT / "sessions"
config.ALLOWLIST_PATH = _ROOT / "allowlist.json"
config.MODELS_PATH = _ROOT / "models.json"
config.PROVIDER_DEFAULTS_PATH = _ROOT / "provider_defaults.json"
config.MODEL_CACHE_PATH = _ROOT / "catalog.json"
config.SKILLS_DIR = _ROOT / "skills"
config.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
config.AVATAR_ENV = "jarvis"

from jarvis import llm, tools  # noqa: E402
from jarvis.v2.model import PermissionProfile, ProviderName, Role, Thread  # noqa: E402
from jarvis.v2.provider import (  # noqa: E402
    Brief,
    BriefRefused,
    Decision,
    EventKind,
    UserMessage,
)
from jarvis.v2.providers import fastpath  # noqa: E402
from jarvis.v2.providers.fastpath import FastPathProvider  # noqa: E402

CWD = str(_ROOT)


# --- harness ----------------------------------------------------------------


def reply(text="", calls=None, finish="stop"):
    """One llm.Reply, in the shape `loop_check` uses."""
    message: dict = {"content": text or None}
    if calls:
        message["tool_calls"] = [
            {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": a}}
            for i, (n, a) in enumerate(calls)
        ]
    return llm.Reply(
        message=message,
        finish_reason="tool_calls" if calls else finish,
        model="stub",
        latency_s=0.0,
        cost_usd=0.001,
    )


@contextlib.contextmanager
def scripted(fake):
    """Install `fake` as llm.chat for the duration."""
    real = llm.chat
    llm.chat = fake
    try:
        yield
    finally:
        llm.chat = real


def allow_all(name, args, brief):
    return Decision.ALLOW


def thread(tid="t1", session_id=None):
    return Thread(
        id=tid,
        project_id="p1",
        role=Role.CHAT,
        provider=ProviderName.FAST,
        provider_session_id=session_id,
    )


def brief(**kw):
    kw.setdefault("role", Role.CHAT)
    kw.setdefault("cwd", CWD)
    return Brief(**kw)


def drain(provider, handle, text="go", images=None):
    return list(provider.send(handle, UserMessage(text=text, images=images or [])))


def kinds(events):
    return [e.kind for e in events]


def one(events, kind):
    found = [e for e in events if e.kind is kind]
    assert len(found) == 1, f"expected exactly one {kind}, got {len(found)}"
    return found[0]


def assert_wire_valid(messages) -> None:
    """Every declared tool_call has exactly one result, in order (invariant 3)."""
    declared, answered = [], []
    for message in messages:
        for call in message.get("tool_calls") or []:
            declared.append(call["id"])
        if message.get("role") == "tool":
            answered.append(message["tool_call_id"])
    assert declared == answered, f"declared {declared} answered {answered}"


# --- the boundary -----------------------------------------------------------


def boundary_checks() -> None:
    for name in fastpath.FAST_TOOLS:
        assert name in tools.REGISTRY, f"FAST_TOOLS names a tool that does not exist: {name}"
        assert not tools.REGISTRY[name].dangerous, f"{name} is dangerous and on the fast path"

    # Named in the brief, one by one, because a regression here is silent.
    for name in (
        "write_file", "edit_file", "run_command", "run_readonly", "run_subagent",
        "run_fleet", "plan_write", "load_tools", "delegate",
        "task_start", "task_status", "task_log", "task_cancel",
        "workflow_start", "workflow_status", "workflow_log",
    ):
        assert name not in fastpath.FAST_TOOLS, f"{name} must not be on the fast path"
    for name in fastpath.FAST_TOOLS:
        assert not name.startswith(("browser_", "desktop_", "cad_", "gmail_", "discord_")), name
    # S1: no fast-path tool may reach the Discord API, however it is named later.
    assert "discord_" in fastpath.FORBIDDEN_PREFIXES, fastpath.FORBIDDEN_PREFIXES

    # Not one registered tool that is dangerous is reachable, whatever else moves.
    dangerous = {n for n, t in tools.REGISTRY.items() if t.dangerous}
    assert not (dangerous & fastpath.FAST_TOOLS), dangerous & fastpath.FAST_TOOLS

    assert "task_propose" in fastpath.FAST_TOOLS, "the door out must be in the toolset"
    print("ok  boundary: FAST_TOOLS exists, nothing dangerous, every excluded name absent")


def validation_checks() -> None:
    """The import-time guard has to actually bite — invariant 10's failure mode
    is a toolset that shrinks with nothing said."""
    saved = fastpath.FAST_TOOLS
    try:
        fastpath.FAST_TOOLS = saved | {"no_such_tool_anywhere"}
        try:
            fastpath._validate_toolset()
        except RuntimeError as exc:
            assert "no_such_tool_anywhere" in str(exc), exc
        else:
            raise AssertionError("a missing tool did not fail loudly")

        fastpath.FAST_TOOLS = saved | {"run_command"}
        try:
            fastpath._validate_toolset()
        except RuntimeError as exc:
            assert "run_command" in str(exc), exc
        else:
            raise AssertionError("a dangerous tool did not fail loudly")
    finally:
        fastpath.FAST_TOOLS = saved
    fastpath._validate_toolset()
    print("ok  boundary: a missing or dangerous entry fails loudly at import")


def brief_checks() -> None:
    provider = FastPathProvider()

    try:
        provider.start(thread(), brief(profile=PermissionProfile.STRICT), allow_all)
    except BriefRefused as exc:
        assert "strict" in str(exc).lower(), exc
    else:
        raise AssertionError("a strict brief was accepted")

    try:
        provider.start(thread(), brief(mcp_servers={"x": {}}), allow_all)
    except BriefRefused:
        pass
    else:
        raise AssertionError("an MCP brief was accepted")

    # The one that matters: a brief may narrow the fast path, never widen it.
    try:
        provider.start(thread(), brief(allowed_tools=["read_file", "run_command"]), allow_all)
    except BriefRefused as exc:
        assert "run_command" in str(exc), exc
    else:
        raise AssertionError("a Brief widened the fast path")

    handle = provider.start(thread(), brief(allowed_tools=["read_file"]), allow_all)
    names = {s["function"]["name"] for s in handle.native.agent.tool_specs}
    assert names == {"read_file"}, names
    provider.close(handle)
    print("ok  brief: STRICT and MCP refused, a subset narrows, a superset is refused")


def health_checks() -> None:
    provider = FastPathProvider()
    saved = os.environ.get("OPENROUTER_API_KEY")
    try:
        os.environ.pop("OPENROUTER_API_KEY", None)
        ok, why = provider.health()
        assert not ok and "OPENROUTER_API_KEY" in why, (ok, why)

        os.environ["OPENROUTER_API_KEY"] = "not-a-real-key"
        ok, why = provider.health()
        assert ok, (ok, why)
        # Never the value, never its length.
        assert "not-a-real-key" not in why and "14" not in why, why
    finally:
        if saved is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = saved
    print("ok  health: set-ness only, both ways, and the key never appears in the reason")


# --- the event stream -------------------------------------------------------


def sequence_checks() -> None:
    """A scripted two-step turn: one tool call, then the answer."""
    provider = FastPathProvider()
    handle = provider.start(thread("seq"), brief(), allow_all)

    def fake(model, messages, tools=None, on_delta=None, **kw):
        if not any(m.get("role") == "tool" for m in messages):
            return reply("checking the clock", [("get_datetime", "{}")])
        for piece in ("it is ", "late"):
            if on_delta:
                on_delta(piece)
        return reply("it is late")

    with scripted(fake):
        events = drain(provider, handle, "what time is it")

    assert kinds(events) == [
        EventKind.TURN_STARTED,
        EventKind.USAGE,
        EventKind.THINKING,
        EventKind.TOOL_STARTED,
        EventKind.TOOL_FINISHED,
        # The deltas arrive *inside* the second model call, so they precede its
        # USAGE — which is the whole point of the queue.
        EventKind.TEXT_DELTA,
        EventKind.TEXT_DELTA,
        EventKind.USAGE,
        EventKind.TEXT,
        EventKind.TURN_FINISHED,
    ], kinds(events)

    started = one(events, EventKind.TOOL_STARTED)
    finished = one(events, EventKind.TOOL_FINISHED)
    assert started.data["name"] == "get_datetime" and started.data["args"] == {}, started.data
    assert started.data["call_id"] == finished.data["call_id"], "call_id must pair the two"
    assert finished.data["ok"] is True and finished.data["summary"], finished.data
    assert len(finished.data["summary"]) <= fastpath.SUMMARY_CHARS

    assert one(events, EventKind.THINKING).data["text"] == "checking the clock"
    assert one(events, EventKind.TEXT).data["text"] == "it is late"
    assert [e.thread_id for e in events] == ["seq"] * len(events)

    # Every delta lands before the finished text: the HUD draws a reply while
    # it is being written, which only works if these are live.
    positions = [i for i, e in enumerate(events) if e.kind is EventKind.TEXT_DELTA]
    assert max(positions) < kinds(events).index(EventKind.TEXT), kinds(events)

    done = one(events, EventKind.TURN_FINISHED)
    assert done.data == {"stop": "end", "proposal": None}, done.data
    assert_wire_valid(handle.native.agent.messages)
    provider.close(handle)
    print("ok  events: exact kind sequence, paired call ids, deltas before the text")


def unparseable_args_checks() -> None:
    provider = FastPathProvider()
    handle = provider.start(thread("args"), brief(), allow_all)

    def fake(model, messages, tools=None, on_delta=None, **kw):
        if not any(m.get("role") == "tool" for m in messages):
            return reply("", [("get_datetime", "{not json")])
        return reply("recovered")

    with scripted(fake):
        events = drain(provider, handle)

    started = one(events, EventKind.TOOL_STARTED)
    assert started.data["args"] == {"raw": "{not json"}, started.data
    # v1 returns a tool failure as text (invariant 4); the stream says so.
    assert one(events, EventKind.TOOL_FINISHED).data["ok"] is False
    provider.close(handle)
    print("ok  events: unparseable args are reported as raw, a failed tool is not ok")


# --- the proposal channel ---------------------------------------------------


def propose_checks() -> None:
    provider = FastPathProvider()
    handle = provider.start(thread("prop"), brief(), allow_all)

    def fake(model, messages, tools=None, on_delta=None, **kw):
        if not any(m.get("role") == "tool" for m in messages):
            return reply(
                "",
                [
                    (
                        "task_propose",
                        json.dumps({"brief": "convert the skills folder", "project": "jarvis"}),
                    )
                ],
            )
        return reply("Opened a task for that.")

    with scripted(fake):
        events = drain(provider, handle, "please convert the skills folder")

    done = one(events, EventKind.TURN_FINISHED)
    assert done.data["stop"] == "end", done.data
    assert done.data["proposal"] == {
        "brief": "convert the skills folder",
        "project": "jarvis",
        "provider": None,
    }, done.data
    provider.close(handle)
    print("ok  proposal: task_propose reaches TURN_FINISHED with its project")


def propose_replace_checks() -> None:
    provider = FastPathProvider()
    handle = provider.start(thread("prop2"), brief(), allow_all)
    calls = {"n": 0}

    def fake(model, messages, tools=None, on_delta=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return reply("", [("task_propose", json.dumps({"brief": "first idea"}))])
        if calls["n"] == 2:
            return reply(
                "",
                [("task_propose", json.dumps({"brief": "second idea", "provider": "Codex"}))],
            )
        return reply("done")

    with scripted(fake):
        events = drain(provider, handle)

    proposal = one(events, EventKind.TURN_FINISHED).data["proposal"]
    assert proposal == {"brief": "second idea", "project": None, "provider": "codex"}, proposal
    provider.close(handle)

    # And outside a fast-path turn it refuses rather than pretending.
    result = tools.dispatch("task_propose", json.dumps({"brief": "x"}))
    assert result.text.startswith("Error:"), result.text
    print("ok  proposal: a second call replaces the first; unbound, it refuses")


def exhaustion_checks() -> None:
    """The budget rule: running out of steps becomes a proposal, not a stop."""
    provider = FastPathProvider()
    handle = provider.start(thread("burn"), brief(), allow_all)

    def fake(model, messages, tools=None, on_delta=None, **kw):
        if tools is None:  # the v1 handoff call: tool-free, at the wall
            return reply("DONE: nothing. OPEN: the migration. NEXT: read the spec.")
        return reply("", [("get_datetime", "{}")])

    with scripted(fake):
        events = drain(provider, handle, "migrate everything")

    turn_calls = [e for e in events if e.kind is EventKind.TOOL_STARTED]
    assert len(turn_calls) == fastpath.FAST_MAX_STEPS, len(turn_calls)

    done = one(events, EventKind.TURN_FINISHED)
    assert done.data["stop"] == "max_turns", done.data
    proposal = done.data["proposal"]
    assert proposal["brief"].startswith("[fast path ran out of steps] "), proposal
    assert "NEXT: read the spec." in proposal["brief"], proposal
    assert proposal["project"] is None and proposal["provider"] is None, proposal
    assert_wire_valid(handle.native.agent.messages)
    provider.close(handle)
    print("ok  proposal: step exhaustion becomes a proposal carrying the handoff")


# --- interruption and failure ----------------------------------------------


def interrupt_checks() -> None:
    provider = FastPathProvider()
    handle = provider.start(thread("stop"), brief(), allow_all)

    # Interrupt from inside a tool, so the cancel lands at the next step
    # boundary with the tool result already in the transcript — which is the
    # property invariant 3 is about.
    real = tools.REGISTRY["get_datetime"].func

    def interrupting():
        provider.interrupt(handle)
        return "the time is now"

    tools.REGISTRY["get_datetime"].func = interrupting

    def fake(model, messages, tools=None, on_delta=None, **kw):
        return reply("", [("get_datetime", "{}")])

    try:
        with scripted(fake):
            events = drain(provider, handle)
    finally:
        tools.REGISTRY["get_datetime"].func = real

    done = one(events, EventKind.TURN_FINISHED)
    assert done.data["stop"] == "interrupted", done.data
    assert EventKind.TEXT not in kinds(events), kinds(events)
    messages = handle.native.agent.messages
    assert_wire_valid(messages)
    assert messages[-1]["role"] in ("tool", "user"), messages[-1]["role"]

    # A cancel aimed at the previous turn must not kill its replacement.
    def answer(model, messages, tools=None, on_delta=None, **kw):
        return reply("still here")

    with scripted(answer):
        events = drain(provider, handle, "are you there")
    assert one(events, EventKind.TURN_FINISHED).data["stop"] == "end"
    assert one(events, EventKind.TEXT).data["text"] == "still here"
    provider.close(handle)
    print("ok  interrupt: stop=interrupted, transcript wire-valid, next turn unaffected")


def steering_checks() -> None:
    """2026-10-08: a message steered into a running fast-path turn reaches the
    model at its next step boundary as `[owner steering] …` — after every
    result of the batch it interrupted (invariant 3), never inside it; one that
    arrives as the final answer is being written is handed back undelivered;
    and a second send is refused without being fatal."""
    from jarvis.v2.provider import SteerRefused

    provider = FastPathProvider()
    handle = provider.start(thread("steer"), brief(), allow_all)
    try:
        provider.steer(handle, UserMessage(text="nothing is running"))
        raise AssertionError("a steer with no turn running must be refused")
    except SteerRefused as exc:
        assert exc.fallback == "queue", exc.fallback

    real = tools.REGISTRY["get_datetime"].func
    steered = UserMessage(text="use UTC", images=[{"b64": "YWJj", "mime": "image/png"}])

    def steering_tool():
        # From inside the batch: the steer must wait for the batch to finish.
        if not getattr(steering_tool, "done", False):
            steering_tool.done = True
            provider.steer(handle, steered)
        return "the time is now"

    tools.REGISTRY["get_datetime"].func = steering_tool
    seen = []

    def fake(model, messages, tools=None, on_delta=None, **kw):
        seen.append([dict(m) for m in messages])
        if len(seen) == 1:
            return reply("", [("get_datetime", "{}"), ("get_datetime", "{}")])
        return reply("done, in UTC")

    try:
        with scripted(fake):
            events = drain(provider, handle, "what time is it")
    finally:
        tools.REGISTRY["get_datetime"].func = real

    assert one(events, EventKind.TEXT).data["text"] == "done, in UTC"
    assert one(events, EventKind.TURN_FINISHED).data["stop"] == "end"
    second = seen[1]
    roles = [m["role"] for m in second]
    at = next(i for i, m in enumerate(second) if m["role"] == "user"
              and isinstance(m.get("content"), list)
              and m["content"][0].get("text") == "[owner steering] use UTC")
    assert roles[at - 3:at] == ["assistant", "tool", "tool"], roles
    assert [p["type"] for p in second[at]["content"]] == ["text", "image_url"], second[at]["content"]
    # Invariant 7: the working-context block is still lifted to the tail,
    # behind the steering message, and there is one of it.
    from jarvis.agent import CONTEXT_BLOCK_PREFIX
    blocks = [i for i, m in enumerate(second)
              if isinstance(m.get("content"), str) and m["content"].startswith(CONTEXT_BLOCK_PREFIX)]
    assert len(blocks) <= 1 and all(i == len(second) - 1 and i > at for i in blocks), (blocks, at)
    assert_wire_valid(handle.native.agent.messages)
    assert provider.undelivered(handle) == [], "a delivered steer is not handed back"
    print("ok  steering: delivered at the next step boundary, after the whole batch, with its image")

    # Steered while the final answer is being written: never seen by this
    # turn, so handed back for the daemon to run next.
    late = UserMessage(text="and in French")

    def answering(model, messages, tools=None, on_delta=None, **kw):
        provider.steer(handle, late)
        return reply("it is noon")

    with scripted(answering):
        events = drain(provider, handle, "and now?")
    assert one(events, EventKind.TEXT).data["text"] == "it is noon"
    left = provider.undelivered(handle)
    assert len(left) == 1 and left[0] is late, left
    assert provider.undelivered(handle) == [], "handed back once"
    assert not any("and in French" in str(m.get("content")) for m in handle.native.agent.messages)
    try:
        provider.steer(handle, UserMessage(text="after the turn"))
        raise AssertionError("a steer after the turn must be refused")
    except SteerRefused:
        pass
    print("ok  steering: one that misses the final answer is handed back, not lost")

    # A second send while a turn runs is refused — and not fatal, because a
    # fatal error makes the daemon drop the session that is still running.
    gate = threading.Event()

    def slow(model, messages, tools=None, on_delta=None, **kw):
        gate.wait(5)
        return reply("first finished")

    with scripted(slow):
        first = provider.send(handle, UserMessage(text="one"))
        assert next(first).kind is EventKind.TURN_STARTED
        second_events = list(provider.send(handle, UserMessage(text="two")))
        gate.set()
        rest = list(first)
    assert kinds(second_events) == [EventKind.ERROR], second_events
    assert second_events[0].data["fatal"] is False, second_events[0].data
    assert one(rest, EventKind.TEXT).data["text"] == "first finished"
    provider.close(handle)
    print("ok  steering: a concurrent send is refused, never fatal, and the running turn finishes")


def error_checks() -> None:
    provider = FastPathProvider()
    handle = provider.start(thread("boom"), brief(), allow_all)

    def fake(model, messages, tools=None, on_delta=None, **kw):
        raise RuntimeError("provider exploded")

    with scripted(fake):
        events = drain(provider, handle)

    assert kinds(events)[-1] is EventKind.ERROR, kinds(events)
    assert EventKind.TURN_FINISHED not in kinds(events), kinds(events)
    error = one(events, EventKind.ERROR)
    assert error.data["fatal"] is True and "provider exploded" in error.data["message"], error.data
    provider.close(handle)
    print("ok  error: a raising llm.chat yields ERROR(fatal) and the generator terminates")


def skill_checks() -> None:
    """`UserMessage.skill` (S1): the body rides the turn; refusals never run one."""
    from jarvis.v2 import commands

    for name, meta, body in (("brief-me", "", "Say the date, then the weather."),
                             ("board", "jarvis-only: true\n", "Open the whiteboard."),
                             ("huge", "", "y" * (commands.SKILL_BODY_MAX + 1))):
        (config.SKILLS_DIR / name).mkdir(exist_ok=True)
        (config.SKILLS_DIR / name / "SKILL.md").write_text(
            f"---\nname: {name}\n{meta}description: Use for {name}\n---\n{body}\n")
    provider = FastPathProvider()
    handle = provider.start(thread("skill"), brief(), allow_all)
    seen = []

    def fake(model, messages, tools=None, on_delta=None, **kw):
        seen.extend(m["content"] for m in messages if m.get("role") == "user")
        return reply("done")

    with scripted(fake):
        events = list(provider.send(handle, UserMessage(text="keep it short", skill="brief-me")))
    assert one(events, EventKind.TURN_FINISHED).data["stop"] == "end", kinds(events)
    # The session titler also calls llm.chat; the turn's own message is the one
    # that *starts* with the skill header.
    sent = next((s for s in seen if isinstance(s, str) and s.startswith("[The owner")), str(seen))
    assert '[The owner invoked the skill "brief-me". Follow it.]' in sent, sent
    assert "Say the date, then the weather." in sent, sent
    assert sent.rstrip().endswith("[Request]\nkeep it short"), sent

    for name, needle in (("board", "only runs in the v1 loop"), ("huge", "limit"),
                         ("nope", "don't have a skill")):
        calls = len(seen)
        with scripted(fake):
            events = list(provider.send(handle, UserMessage(text="x", skill=name)))
        assert len(seen) == calls, f"{name}: a refused skill must not reach the model"
        error = one(events, EventKind.ERROR)
        assert error.data["fatal"] is False and needle in error.data["message"], error.data
        assert one(events, EventKind.TURN_FINISHED).data["stop"] == "error"
    provider.close(handle)
    print("ok  skill: the body is injected with the request; jarvis-only, oversized and "
          "unknown skills are refused before the model")


def close_checks() -> None:
    provider = FastPathProvider()
    handle = provider.start(thread("close"), brief(), allow_all)
    provider.close(handle)
    provider.close(handle)  # idempotent
    events = drain(provider, handle)
    assert kinds(events) == [EventKind.ERROR] and events[0].data["fatal"], events
    assert provider.usage(handle).cost_usd == 0.0
    print("ok  close: idempotent, a send afterwards errors rather than hanging")


# --- session, approvals, usage, isolation -----------------------------------


def resume_checks() -> None:
    provider = FastPathProvider()
    first = provider.start(thread("keep"), brief(), allow_all)

    def fake(model, messages, tools=None, on_delta=None, **kw):
        return reply("the codeword is ossifrage")

    with scripted(fake):
        drain(provider, first, "remember a codeword")
    session_id = first.provider_session_id
    provider.close(first)

    second = provider.resume(thread("keep", session_id=session_id), brief(), allow_all)
    assert second.provider_session_id == session_id
    transcript = json.dumps(second.native.agent.messages)
    assert "ossifrage" in transcript and "remember a codeword" in transcript, transcript[:400]

    # A session id that no longer resolves degrades to a fresh one rather than
    # refusing to talk.
    third = provider.resume(thread("keep", session_id="no-such-session"), brief(), allow_all)
    assert third.provider_session_id != "no-such-session"
    assert len(third.native.agent.messages) == 1, third.native.agent.messages
    provider.close(second)
    provider.close(third)
    print("ok  resume: a second handle sees the first's transcript; a lost id degrades")


def approver_checks() -> None:
    """No FAST_TOOLS entry is dangerous, so the adapter is exercised through a
    test-only widening: the boundary and the gate are separate properties, and
    the gate has to be right for the day a dangerous tool is added."""
    ran = {"n": 0}

    def boom() -> str:
        ran["n"] += 1
        return "it ran"

    tools.REGISTRY["fastpath_test_danger"] = tools.Tool(
        name="fastpath_test_danger",
        description="stub",
        schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        func=boom,
        dangerous=True,
    )

    class Widened(FastPathProvider):
        def _toolset(self, brief):
            return ["get_datetime", "fastpath_test_danger"]

    asked: list[tuple[str, dict]] = []

    def permit(name, args, brief):
        asked.append((name, args))
        return Decision.DENY

    def fake(model, messages, tools=None, on_delta=None, **kw):
        if not any(m.get("role") == "tool" for m in messages):
            return reply("", [("fastpath_test_danger", "{}")])
        return reply("declined, then")

    try:
        provider = Widened()
        handle = provider.start(thread("gate"), brief(), permit)
        assert getattr(handle.native.agent.approve, "jarvis_human_backed", False), (
            "the approver must keep v1's human-backed flag"
        )
        with scripted(fake):
            events = drain(provider, handle)
        assert asked == [("fastpath_test_danger", {})], asked
        assert ran["n"] == 0, "DENY must mean the tool never runs"
        assert one(events, EventKind.TOOL_FINISHED).data["ok"] is False
        provider.close(handle)

        allowed = Widened().start(thread("gate2"), brief(), allow_all)
        with scripted(fake):
            drain(Widened(), allowed)
        assert ran["n"] == 1, "ALLOW must let it run"
    finally:
        tools.REGISTRY.pop("fastpath_test_danger", None)
    print("ok  approver: permit sees the tool name, DENY refuses, ALLOW runs, flag kept")


def answer_checks() -> None:
    provider = FastPathProvider()
    handle = provider.start(thread("ans"), brief(), allow_all)
    try:
        provider.answer(handle, "r1", Decision.ALLOW)
    except ValueError:
        pass
    else:
        raise AssertionError("answer() must refuse — the fast path asks nothing")
    provider.close(handle)
    print("ok  answer: refused, because the fast path raises no approvals")


def usage_checks() -> None:
    provider = FastPathProvider()
    handle = provider.start(thread("cost"), brief(), allow_all)

    def fake(model, messages, tools=None, on_delta=None, **kw):
        if not any(m.get("role") == "tool" for m in messages):
            return reply("", [("get_datetime", "{}")])
        return reply("done")

    with scripted(fake):
        first = drain(provider, handle)
        second = drain(provider, handle, "again")

    # Each USAGE carries the *increment*, so summing them is correct — v1's
    # cost event is cumulative within a turn and summing that would double it.
    # Two steps in the first turn, one in the second: three model calls, three
    # USAGE events, and the handle's total is their sum across both turns.
    deltas = [e.data["cost_usd"] for e in first + second if e.kind is EventKind.USAGE]
    assert len(deltas) == 3, deltas
    assert all(d > 0 for d in deltas), deltas
    total = provider.usage(handle).cost_usd
    assert abs(total - sum(deltas)) < 1e-9, (total, deltas)
    assert abs(total - 0.003) < 1e-9, total
    assert provider.usage(handle).work_tokens == 0
    provider.close(handle)
    print("ok  usage: USAGE deltas sum to the handle's total")


def isolation_checks() -> None:
    """Two concurrent handles share neither transcript nor per-run state.

    One script, shared, dispatching on the calling agent's own user message:
    `llm.chat` is a module global, so two threads each installing their own
    closure is a race whose loser silently runs the winner's script — the
    flakiness `longhorizon_check` already paid for.
    """
    provider = FastPathProvider()
    handles = {tag: provider.start(thread(tag), brief(), allow_all) for tag in ("alpha", "beta")}
    seen: dict[str, list] = {}
    barrier = threading.Barrier(2, timeout=10)

    def script(model, messages, tools=None, on_delta=None, **kw):
        tag = messages[1]["content"]
        if not any(m.get("role") == "tool" for m in messages):
            return reply("", [("task_propose", json.dumps({"brief": f"task for {tag}"}))])
        barrier.wait()  # both proposals written before either is read back
        return reply(f"opened for {tag}")

    def drive(tag: str) -> None:
        seen[tag] = drain(provider, handles[tag], tag)

    with scripted(script):
        threads = [threading.Thread(target=drive, args=(t,)) for t in handles]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

    assert set(seen) == {"alpha", "beta"}, seen
    for tag, events in seen.items():
        other = "beta" if tag == "alpha" else "alpha"
        proposal = one(events, EventKind.TURN_FINISHED).data["proposal"]
        assert proposal["brief"] == f"task for {tag}", proposal
        transcript = json.dumps(handles[tag].native.agent.messages)
        assert tag in transcript and other not in transcript, transcript[:400]
    for handle in handles.values():
        provider.close(handle)
    print("ok  isolation: two concurrent handles share no proposal and no transcript")


def _roster(selected: str = "", models=("other/model",)) -> None:
    """Write the (temp) roster directly: the default plus `models`."""
    config.MODELS_PATH.write_text(json.dumps({
        "models": [config.TIERS["orchestrator"], *models], "selected": selected, "efforts": {}}))


def model_checks() -> None:
    """Decisions A1/A5: a default handle follows the global picker on every
    turn; a pinned one does not; a change lands at a turn boundary only."""
    seen: list[tuple[str, object]] = []
    gate = threading.Event()
    hold = threading.Event()

    def fake(model, messages, tools=None, on_delta=None, **kw):
        if tools is None:
            return reply("a title")   # the session titler (cheap tier), not the turn
        seen.append((model, kw.get("effort")))
        if hold.is_set() and not any(m.get("role") == "tool" for m in messages):
            gate.set()        # turn 1 is in its first call: the model changes now
            return reply("", [("get_datetime", "{}")])
        return reply("done")

    provider = FastPathProvider()
    _roster("")
    default = provider.start(thread("dflt"), brief(), allow_all)
    pinned = provider.start(thread("pin"), brief(model="other/model"), allow_all)
    with scripted(fake):
        drain(provider, default)
        assert seen[-1][0] == config.TIERS["orchestrator"], seen
        _roster("other/model")                     # the owner picks another model globally
        drain(provider, default)
        assert seen[-1][0] == "other/model", f"a default handle must follow the global choice: {seen}"
        seen.clear()
        _roster(config.TIERS["orchestrator"])
        drain(provider, pinned)
        assert seen and all(m == "other/model" for m, _ in seen), f"a pin must ignore the global: {seen}"

        # set_model during turn 1: every call of turn 1 stays on the old model.
        seen.clear()
        hold.set()
        changer = threading.Thread(target=lambda: (gate.wait(5),
                                                   provider.set_model(pinned, "third/model", "low")))
        changer.start()
        first = drain(provider, pinned)
        changer.join(5)
        hold.clear()
        assert len(seen) == 2 and all(m == "other/model" for m, _ in seen), \
            f"a change mid-turn must not move that turn's calls: {seen}"
        usage = [e for e in first if e.kind is EventKind.USAGE]
        assert usage and usage[-1].data["provider_reported"]["model"] == "other/model", usage
        seen.clear()
        second = drain(provider, pinned)
        assert seen == [("third/model", "low")], f"the next turn runs the new pair: {seen}"
        assert one(second, EventKind.USAGE).data["provider_reported"]["model"] == "third/model"

        # effort None keeps v1's per-model resolution (models.effort_for).
        seen.clear()
        provider.set_model(pinned, "third/model", None)
        drain(provider, pinned)
        from jarvis import models
        assert seen == [("third/model", models.effort_for("third/model"))], seen
        # "" sends no effort at all (a model with no reasoning control).
        seen.clear()
        provider.set_model(pinned, "third/model", "")
        drain(provider, pinned)
        assert seen == [("third/model", None)], seen
    for h in (default, pinned):
        provider.close(h)
    try:
        provider.set_model(pinned, "x/y", None)
    except ValueError:
        pass
    else:
        raise AssertionError("set_model on a closed handle must refuse")
    _roster("")
    print("ok  model: default follows the global per turn, a pin does not, a change waits for the next turn")


def guarded_text(path: Path) -> str:
    import inspect
    from jarvis.tools import files
    text = path.read_text()
    if path.resolve() == Path(files.__file__).resolve():
        body = inspect.getsource(files._protected_state)
        assert body in text, "files._protected_state moved; the exemption must follow it"
        text = text.replace(body, "")
    return text


def no_self_switch_checks() -> None:
    """The agent has no lever on its own model or provider (decisions A1).

    Only the owner changes them, through PATCH /threads/{id}. A tool that
    could would let a thread pick itself something cheaper — or dearer — with
    nobody asked, so this is asserted by name and by what the code calls.
    """
    from jarvis.v2 import mcp
    reachable = set(fastpath.FAST_TOOLS) | set(mcp.MCP_TOOLS)
    for name in sorted(reachable):
        assert not any(word in name for word in ("model", "provider", "effort")), name
    for name, tool in tools.REGISTRY.items():
        assert not any(word in name for word in ("set_model", "thread_model", "set_provider",
                                                 "provider_default")), name
    roots = [Path(fastpath.__file__).parents[1] / "tools", Path(tools.__file__).parent]
    for root in roots:
        for path in root.rglob("*.py"):
            # `files._protected_state` names the state files only to refuse
            # writes to them; the same exemption models_check makes.
            text = guarded_text(path)
            for needle in ("set_thread_model", "thread_model", ".set_model(",
                           # The roster and the global default: every write.
                           "models.select(", "models.set_effort(", "models.remove(", "models.add(",
                           "models_mod.select(", "models_mod.set_effort(", "models_mod.remove(",
                           "models_mod.add(", "models._save(", "MODELS_PATH",
                           # The Claude/Codex chat default (2026-10-08).
                           "set_provider_default(", "_save_defaults(", "PROVIDER_DEFAULTS_PATH",
                           "provider_defaults", "/thread-models"):
                assert needle not in text, f"{path} reaches {needle}"
    print("ok  guard: no fast-path, MCP or registered tool can change a thread's model, "
          "provider or a provider's default")


def main() -> int:
    boundary_checks()
    validation_checks()
    brief_checks()
    health_checks()
    sequence_checks()
    unparseable_args_checks()
    propose_checks()
    propose_replace_checks()
    exhaustion_checks()
    interrupt_checks()
    steering_checks()
    error_checks()
    skill_checks()
    close_checks()
    resume_checks()
    approver_checks()
    answer_checks()
    usage_checks()
    isolation_checks()
    model_checks()
    no_self_switch_checks()
    print("\nall fast-path checks passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        _TMP.cleanup()
