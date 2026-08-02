"""Synthetic checks for session memory. Free — no API calls.

Covers the store (round-trip, image stripping, titles, the log surviving
compaction), the index injected into context, the read-only tools, and the
Agent binding that makes every surface persist for free.

Run:  .venv/bin/python tests/sessions_check.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jarvis.agent as agent_mod
from jarvis import config, context, sessions, tools

sessions.AUTO_TITLE = False  # titling is a cheap-tier call; tests stay free


def _turn(text: str = "ok", cost: float = 0.001):
    return types.SimpleNamespace(text=text, cost_usd=cost, steps=1)


def store_checks() -> None:
    session = sessions.new("chat")
    assert session.turns == 0 and session.title == "untitled"

    messages = [
        {"role": "system", "content": "SYSTEM PROMPT + volatile index"},
        {"role": "user", "content": "how do I wire the VEX c-channel"},
        {"role": "assistant", "content": "like so"},
    ]
    session.record("how do I wire the VEX c-channel", _turn("like so"), messages)

    assert session.turns == 1, session.meta
    assert session.title.startswith("how do I wire"), session.title

    reloaded = sessions.load(session.id)
    assert reloaded is not None
    restored = reloaded.restore_messages()
    # The system message is rebuilt from today's prompt, never restored: it
    # carries the skills/sessions indexes, which must not be frozen to disk.
    assert len(restored) == 2 and restored[0]["role"] == "user", restored
    assert "SYSTEM PROMPT" not in json.dumps(restored)
    print("ok  store: record → reload round-trip, system message not persisted")


def image_checks() -> None:
    session = sessions.new("chat")
    big = "A" * 40_000
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "look"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "here"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{big}"}},
            ],
        },
    ]
    session.record("look", _turn(), messages)

    saved = (session.path / "messages.json").read_text(encoding="utf-8")
    assert big not in saved, "image payload persisted — session files would balloon"
    assert context.EVICTED_IMAGE in saved
    # The live transcript still holds the image; only the copy on disk drops it.
    assert messages[2]["content"][1]["type"] == "image_url"
    print("ok  store: image payloads stripped on save, live transcript untouched")


def log_checks() -> None:
    """The log is the durable record — compaction cannot take it away."""
    session = sessions.new("chat")
    messages = [{"role": "system", "content": "s"}]
    for i in range(3):
        messages.append({"role": "user", "content": f"question about kokoro {i}"})
        session.record(f"question about kokoro {i}", _turn(f"answer {i}"), messages)

    # Simulate compaction eating the whole history, then one more turn.
    messages[1:] = [{"role": "user", "content": "[Summary of earlier conversation]"}]
    messages.append({"role": "user", "content": "and now something else"})
    session.record("and now something else", _turn("fine"), messages)

    assert len(session.restore_messages()) == 2, "transcript should be compacted"
    out = sessions.search("kokoro")
    assert out.count("kokoro") >= 3, out
    assert session.id in out
    assert "No session mentions" in sessions.search("nothing-here-at-all")
    assert "Error" in sessions.search("")
    print("ok  log: survives compaction of the transcript, and greps")


def index_checks() -> None:
    first = sessions.new("chat")
    first.record("the VEX arm design", _turn(), [{"role": "system", "content": "s"}])
    second = sessions.new("face")
    second.record("tts latency chase", _turn(), [{"role": "system", "content": "s"}])

    block = sessions.index(current=second.id)
    assert "RECENT SESSIONS" in block
    assert first.id in block and second.id in block
    assert "← this conversation" in block.split(second.id, 1)[1].split("\n")[0]
    assert "the VEX arm design" in block

    # Empty store renders nothing at all, like the skills index.
    with tempfile.TemporaryDirectory() as empty:
        config.SESSIONS_DIR = Path(empty) / "sessions"
        assert sessions.index() == ""
    print("ok  index: recent titles render, current one marked, empty store empty")


def tool_checks() -> None:
    session = sessions.new("chat")
    session.record("kokoro on cpu was the fix", _turn("noted"), [{"role": "system", "content": "s"}])

    out = tools.dispatch("session_list", "{}")
    assert session.id in out.text and "kokoro" in out.text

    out = tools.dispatch("session_search", json.dumps({"query": "kokoro"}))
    assert "kokoro on cpu" in out.text

    out = tools.dispatch("session_read", json.dumps({"session_id": session.id}))
    assert "you: kokoro on cpu was the fix" in out.text and "jarvis: noted" in out.text

    out = tools.dispatch("session_read", json.dumps({"session_id": "nope"}))
    assert "Error" in out.text and "session_list" in out.text

    # summary() is the only tool that costs a model call — stub it.
    calls = []

    def fake_chat(model, messages, **kwargs):
        calls.append(model)
        return types.SimpleNamespace(text="They fixed TTS by going local.", cost_usd=0.0)

    real = sessions.llm.chat
    sessions.llm.chat = fake_chat
    try:
        out = tools.dispatch("session_summary", json.dumps({"session_id": session.id}))
        assert "going local" in out.text and session.id in out.text
        # Cached: same turn count, so no second call.
        tools.dispatch("session_summary", json.dumps({"session_id": session.id}))
        assert len(calls) == 1, calls
        # A new turn invalidates it.
        session.record("more", _turn(), [{"role": "system", "content": "s"}])
        tools.dispatch("session_summary", json.dumps({"session_id": session.id}))
        assert len(calls) == 2, calls
    finally:
        sessions.llm.chat = real

    assert all(not tools.REGISTRY[n].dangerous for n in
               ("session_list", "session_read", "session_search", "session_summary"))
    print("ok  tools: list/search/read/summary, summary cached per turn count, none dangerous")


def agent_checks() -> None:
    seen: list[list] = []

    def fake_chat(model, messages, tools=None, **kwargs):
        seen.append([dict(m) for m in messages])
        return types.SimpleNamespace(
            message={"content": "sure"}, tool_calls=[], text="sure", cost_usd=0.002, latency_s=0.1
        )

    real = agent_mod.llm.chat
    agent_mod.llm.chat = fake_chat
    try:
        session = sessions.new("chat")
        first = agent_mod.Agent(approve=lambda t, a: False, session=session)
        first.run_turn("remember the sandbox pin")
        assert session.turns == 1 and session.meta["cost_usd"] == 0.002

        # A new Agent on the same session picks the conversation back up.
        resumed = agent_mod.Agent(approve=lambda t, a: False, session=sessions.load(session.id))
        assert [m["role"] for m in resumed.messages] == ["system", "user", "assistant"]
        resumed.run_turn("what did I say?")
        assert seen[-1][1]["content"] == "remember the sandbox pin"
        assert "RECENT SESSIONS" in seen[-1][0]["content"], "index missing for a session agent"
        assert "← this conversation" in seen[-1][0]["content"]

        # An agent without the session tools must not be shown the index.
        blind = agent_mod.Agent(approve=lambda t, a: False, tool_names=["read_file"])
        blind.run_turn("hi")
        assert "RECENT SESSIONS" not in seen[-1][0]["content"]

        # No session bound: nothing is written, and the loop still runs.
        before = len(sessions.all_sessions())
        agent_mod.Agent(approve=lambda t, a: False, tool_names=["read_file"]).run_turn("hi")
        assert len(sessions.all_sessions()) == before
    finally:
        agent_mod.llm.chat = real
    print("ok  agent: turns persist, a second Agent resumes them, index only when armed")


def cancel_checks() -> None:
    """A cancelled turn still saves — that is what makes it recoverable."""
    real = agent_mod.llm.chat
    agent_mod.llm.chat = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call"))
    try:
        session = sessions.new("chat")
        agent = agent_mod.Agent(
            approve=lambda t, a: False, session=session, should_stop=lambda: True
        )
        turn = agent.run_turn("wait, no")
        assert turn.cancelled and session.turns == 1
        assert sessions.load(session.id).restore_messages()[-1]["content"] == "wait, no"
    finally:
        agent_mod.llm.chat = real
    print("ok  cancel: an interrupted turn is saved, transcript ends at the user message")


def main() -> int:
    original = config.SESSIONS_DIR
    try:
        for check in (
            store_checks,
            image_checks,
            log_checks,
            index_checks,
            tool_checks,
            agent_checks,
            cancel_checks,
        ):
            with tempfile.TemporaryDirectory() as tmp:
                config.SESSIONS_DIR = Path(tmp) / "sessions"
                config.SESSIONS_DIR.mkdir(parents=True)
                check()
    finally:
        config.SESSIONS_DIR = original
    print("\nall session checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
