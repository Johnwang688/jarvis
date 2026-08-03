"""Synthetic checks for interrupting a turn in flight. Free — no API calls.

You can see what Jarvis heard the moment STT lands, so the useful next power
is taking it back before he answers a misheard question. That means the agent
loop can now be stopped mid-turn, and the risk is invariant 3: an assistant
message carrying tool_calls MUST be followed by a tool result for each id, or
the next request 400s. So cancellation is only ever checked between steps, and
this pins that down by stubbing llm.chat — no network, no cost, exact control
over where the cancel lands.

  1. cancel before the first step: nothing is sent, transcript stays clean
  2. cancel after a tool round: every tool_call still has its result, and the
     conversation keeps working afterwards
  3. a cancel never fires mid-tool-loop, even when set while a tool is running
  4. the /cancel route sets the flag, and refuses cross-origin

The other way a turn ends without a reply is the step budget running out, so
5 covers that exit path from the same stub harness: it must leave the notice
*in the transcript*, not only in the returned Turn, or the next turn's model
has no idea it was cut off and confabulates an explanation.

Run:  .venv/bin/python tests/face/cancel_check.py
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import agent as agent_mod  # noqa: E402
from jarvis import llm  # noqa: E402
from jarvis.face import server as face_server  # noqa: E402

PORT = 8476
BASE = f"http://127.0.0.1:{PORT}"


def reply(content=None, tool_calls=None) -> llm.Reply:
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return llm.Reply(message=message, finish_reason="stop", model="stub", latency_s=0.01)


def call(call_id: str, name: str, arguments: str) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


class StubLLM:
    """Replays a canned script of assistant turns, counting the calls."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.last_messages: list[dict] = []

    def __call__(self, model, messages, **kwargs):
        self.calls += 1
        self.last_messages = list(messages)
        return self.script.pop(0) if self.script else reply("done")


def check_transcript(agent) -> None:
    """Every assistant tool_call id has exactly one tool message behind it."""
    messages = agent.messages
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            continue
        want = [c["id"] for c in msg["tool_calls"]]
        got = []
        for follower in messages[i + 1:]:
            if follower.get("role") != "tool":
                break
            got.append(follower.get("tool_call_id"))
        assert got == want, f"tool results {got} do not match calls {want}"
    assert messages[-1].get("role") in ("user", "tool", "assistant"), messages[-1]


def agent_checks() -> None:
    original = llm.chat
    try:
        # 1. cancelled before it starts — the model is never even asked
        stub = StubLLM([])
        llm.chat = agent_mod.llm.chat = stub
        jarvis = agent_mod.Agent(should_stop=lambda: True, tool_names=["get_datetime"])
        turn = jarvis.run_turn("what time is it")
        assert turn.cancelled and turn.steps == 0, turn
        assert stub.calls == 0, "a cancelled turn must not spend a request"
        assert jarvis.messages[-1] == {"role": "user", "content": "what time is it"}
        check_transcript(jarvis)
        print("ok  cancel: before the first step — no request sent, transcript clean")

        # 2. cancelled after a tool round: the tool result is still there
        flag = threading.Event()
        stub = StubLLM([
            reply(content="Let me check.", tool_calls=[call("c1", "get_datetime", "{}")]),
            reply(content="It is late."),
        ])
        llm.chat = agent_mod.llm.chat = stub
        jarvis = agent_mod.Agent(should_stop=flag.is_set, tool_names=["get_datetime"])
        jarvis.on_event = lambda kind, data: flag.set() if kind == "tool_end" else None
        turn = jarvis.run_turn("what time is it")
        assert turn.cancelled, turn
        assert stub.calls == 1, f"stopped one step in, not {stub.calls}"
        assert turn.tool_calls == [("get_datetime", "{}")], turn.tool_calls
        check_transcript(jarvis)
        print("ok  cancel: after a tool ran — every tool_call kept its result")

        # ...and the conversation still works on the very next turn
        flag.clear()
        jarvis.on_event = lambda kind, data: None
        stub.script = [reply("Half past four.")]
        turn = jarvis.run_turn("sorry, I meant the date")
        assert not turn.cancelled and turn.text == "Half past four.", turn
        check_transcript(jarvis)
        print("ok  cancel: the next turn runs normally on the resumed transcript")

        # 3. a cancel raised mid-tool-loop still completes that loop
        flag = threading.Event()
        stub = StubLLM([
            reply(tool_calls=[call("a", "get_datetime", "{}"),
                              call("b", "get_datetime", "{}")]),
        ])
        llm.chat = agent_mod.llm.chat = stub
        jarvis = agent_mod.Agent(should_stop=flag.is_set, tool_names=["get_datetime"])
        # fires while the FIRST of two parallel calls is still running
        jarvis.on_event = lambda kind, data: flag.set() if kind == "tool_start" else None
        turn = jarvis.run_turn("twice please")
        assert turn.cancelled, turn
        assert len(turn.tool_calls) == 2, "both parallel calls must finish"
        check_transcript(jarvis)
        print("ok  cancel: raised mid-tool-loop, both parallel results still returned")

        # 5. the step budget running out is announced in the transcript, not
        #    just in the returned Turn — the model must be able to see it
        stub = StubLLM([])
        stub.script = [reply(tool_calls=[call(f"t{i}", "get_datetime", "{}")])
                       for i in range(3)]
        llm.chat = agent_mod.llm.chat = stub
        jarvis = agent_mod.Agent(max_steps=3, tool_names=["get_datetime"])
        turn = jarvis.run_turn("keep going")
        assert turn.stopped_early and turn.steps == 3, turn
        assert "stopped after 3 steps" in turn.text, turn.text
        last = jarvis.messages[-1]
        assert last["role"] == "assistant" and last["content"] == turn.text, last
        assert not last.get("tool_calls"), "the stop notice asks for nothing"
        check_transcript(jarvis)
        print("ok  budget: exhausted — the stop notice lands in the transcript")

        # ...and the model can then answer for itself on the next turn
        stub.script = [reply("I ran out of steps before I could reply.")]
        turn = jarvis.run_turn("why did you stop?")
        seen = stub.last_messages
        assert any(m.get("content") == "[stopped after 3 steps without finishing]"
                   for m in seen), "the next request must carry the stop notice"
        check_transcript(jarvis)
        print("ok  budget: the next turn is sent the notice, so it can say why")

        # the conversation surfaces take the default; only they rely on it
        assert agent_mod.Agent(tool_names=["get_datetime"]).max_steps == 30
        print("ok  budget: default step budget is 30")
    finally:
        llm.chat = agent_mod.llm.chat = original


def route_checks() -> None:
    server = face_server.create_server(PORT)
    try:
        face_server._cancel.clear()
        assert _post("/cancel", origin="https://evil.example") == 403
        assert not face_server._cancel.is_set(), "a refused cancel must not set the flag"
        assert _post("/cancel") == 200
        assert face_server._cancel.is_set()
        face_server._cancel.clear()
        assert _post("/cancel", origin=f"http://127.0.0.1:{PORT}") == 200
        assert face_server._cancel.is_set()
        print("ok  route: /cancel sets the flag, same-origin only")
    finally:
        face_server._cancel.clear()
        server.shutdown()
        server.server_close()


def _post(path: str, origin: str | None = None) -> int:
    request = urllib.request.Request(
        BASE + path, data=b"{}", method="POST",
        headers={"Content-Type": "application/json", **({"Origin": origin} if origin else {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def main() -> int:
    agent_checks()
    route_checks()
    print("all cancel checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
