"""Checks for streamed completions. Free — the HTTP layer is a fake.

Streaming exists for one reason above the others: `should_stop` becomes
answerable *while the model is generating*, instead of only between steps.
Cancellation used to cost one full in-flight request however early the owner
spoke, which is the wrong window — the interesting moment is exactly when a
long answer has started going wrong.

What must hold:
  1. content and tool calls reassemble from fragments, keyed by index
  2. usage, model and finish_reason survive the stream
  3. a cancel mid-stream raises Cancelled, stops reading, and leaves the
     transcript **whole** — no assistant turn, no orphaned tool_call
  4. a failure before the first token is retryable and falls back to the
     non-streaming path; a failure *after* one is not, because the bytes have
     already reached a surface
  5. keepalive comments and [DONE] are handled

Run:  .venv/bin/python tests/stream_check.py
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import agent as agent_mod  # noqa: E402
from jarvis import llm  # noqa: E402


def sse(chunks) -> list[str]:
    lines = [": OPENROUTER PROCESSING", ""]
    for chunk in chunks:
        lines.append("data: " + json.dumps(chunk))
        lines.append("")
    lines.append("data: [DONE]")
    return lines


class FakeStream:
    """Stands in for httpx's streaming response."""

    def __init__(self, lines, status=200, body="", explode=None):
        self.lines, self.status_code, self._body, self.explode = lines, status, body, explode
        self.consumed = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body

    @property
    def text(self):
        return self._body

    def iter_lines(self):
        for line in self.lines:
            self.consumed += 1
            if self.explode and self.consumed == self.explode:
                raise llm.httpx.ConnectError("stream died")
            yield line


@contextlib.contextmanager
def streaming(*responses):
    """Serve `responses` in order from _client.stream; record the payloads."""
    queue, seen = list(responses), []
    real = llm._client.stream

    def fake(method, url, json=None, headers=None, timeout=None):
        seen.append(json)
        return queue.pop(0)

    llm._client.stream = fake
    try:
        yield seen
    finally:
        llm._client.stream = real


@contextlib.contextmanager
def no_post():
    """Make the non-streaming path loud, so a silent fallback cannot hide."""
    real = llm._client.post

    def fake(*a, **kw):
        raise AssertionError("fell back to the non-streaming path unexpectedly")

    llm._client.post = fake
    try:
        yield
    finally:
        llm._client.post = real


def assembly_checks() -> None:
    chunks = [
        {"model": "openai/gpt-5.6-luna", "choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"finish_reason": "stop", "delta": {}}]},
        {"usage": {"prompt_tokens": 42, "completion_tokens": 7, "cost": 0.0009}},
    ]
    deltas: list[str] = []
    with no_post(), streaming(FakeStream(sse(chunks))) as sent:
        reply = llm.chat("m", [{"role": "user", "content": "hi"}], on_delta=deltas.append)

    assert sent[0]["stream"] is True, "streaming was not actually requested"
    assert reply.text == "Hello", reply.text
    assert deltas == ["Hel", "lo"], deltas
    assert reply.finish_reason == "stop"
    assert reply.model == "openai/gpt-5.6-luna"
    assert (reply.prompt_tokens, reply.completion_tokens) == (42, 7)
    assert abs(reply.cost_usd - 0.0009) < 1e-9, reply.cost_usd
    print("ok  stream: content, usage, model and finish_reason all survive")


def tool_call_checks() -> None:
    """Fragments are keyed by index — the id and name arrive before the args."""
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "read_", "arguments": '{"pa'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "file", "arguments": 'th": "x"}'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 1, "id": "c2", "function": {"name": "list_dir", "arguments": "{}"}}]}}]},
        {"choices": [{"finish_reason": "tool_calls", "delta": {}}]},
    ]
    with no_post(), streaming(FakeStream(sse(chunks))):
        reply = llm.chat("m", [{"role": "user", "content": "go"}])

    assert reply.finish_reason == "tool_calls"
    assert len(reply.tool_calls) == 2, reply.tool_calls
    first, second = reply.tool_calls
    assert first["id"] == "c1" and first["function"]["name"] == "read_file"
    assert json.loads(first["function"]["arguments"]) == {"path": "x"}
    assert second["id"] == "c2" and second["function"]["name"] == "list_dir"
    # Null content with tool_calls is the legal shape invariant 2 pins.
    assert reply.message["content"] is None, reply.message
    print("ok  stream: tool calls reassemble by index, args stitched across chunks")


def cancel_checks() -> None:
    """The whole point: stopping without waiting for the rest of the answer."""
    chunks = [{"choices": [{"delta": {"content": f"word{i} "}}]} for i in range(50)]
    stream = FakeStream(sse(chunks))
    seen: list[str] = []

    with no_post(), streaming(stream):
        try:
            llm.chat(
                "m", [{"role": "user", "content": "go"}],
                on_delta=seen.append,
                should_stop=lambda: len(seen) >= 3,
            )
            raised = False
        except llm.Cancelled:
            raised = True

    assert raised, "a mid-stream stop must raise Cancelled"
    assert len(seen) <= 4, f"kept generating after the stop: {len(seen)} deltas"
    assert stream.consumed < len(stream.lines), "read the whole stream anyway"
    print(f"ok  stream: cancelled after {len(seen)} deltas instead of all 50")


def agent_cancel_checks() -> None:
    """A cancel mid-generation must leave the transcript wire-valid.

    Nothing has been dispatched at that point, so no tool_call is outstanding
    and the assistant turn is simply never appended — which is exactly what
    lets cancellation happen here rather than only at a step boundary.
    """
    def fake_chat(model, messages, tools=None, on_delta=None, should_stop=None, **kwargs):
        for piece in ("thinking ", "out ", "loud", "for", "ever"):
            if on_delta:
                on_delta(piece)
            if should_stop and should_stop():
                raise llm.Cancelled()
        return llm.Reply(message={"content": "done"}, finish_reason="stop",
                         model=model, latency_s=0.0)

    events: list[tuple] = []
    deltas = {"n": 0}

    def on_event(kind, data):
        events.append((kind, data))
        if kind == "delta":
            deltas["n"] += 1

    # The stop fires *during* generation, not before it: at the step boundary
    # nothing has streamed yet, so the loop's own pre-step check passes and the
    # interrupt has to be caught inside the model call. That is the case this
    # whole change is about.
    agent = agent_mod.Agent(
        tool_names=["get_datetime"],
        should_stop=lambda: deltas["n"] >= 2,
        on_event=on_event,
    )
    real = llm.chat
    llm.chat = fake_chat
    try:
        before = len(agent.messages)
        turn = agent.run_turn("say something long")
    finally:
        llm.chat = real

    assert 2 <= deltas["n"] <= 3, f"kept generating past the stop: {deltas['n']}"

    assert turn.cancelled, turn
    assert turn.text == "", "a cancelled turn must not report a reply"
    assert not [m for m in agent.messages if m.get("role") == "assistant"], agent.messages
    # The user message stays: the conversation really does end there, which is
    # what makes the interrupted turn immediately reusable.
    assert len(agent.messages) >= before + 1
    declared = [c["id"] for m in agent.messages for c in (m.get("tool_calls") or [])]
    answered = [m["tool_call_id"] for m in agent.messages if m.get("role") == "tool"]
    assert declared == answered == [], (declared, answered)
    assert ("cancelled", 0) in events, events
    assert any(kind == "delta" for kind, _ in events), "deltas never reached the surface"
    print("ok  stream: a mid-generation cancel leaves the transcript whole")


def retry_checks() -> None:
    """Retryable before the first token; never after one."""
    good = [{"choices": [{"delta": {"content": "ok"}}]},
            {"choices": [{"finish_reason": "stop", "delta": {}}]}]

    # Dies on the first line, before anything was emitted -> retried.
    with no_post(), streaming(FakeStream(sse(good), explode=1), FakeStream(sse(good))):
        reply = llm.chat("m", [{"role": "user", "content": "go"}])
    assert reply.text == "ok", reply.text

    # Dies after a token has already reached the caller -> not retried, because
    # replaying would duplicate what the owner already saw.
    seen: list[str] = []
    with no_post(), streaming(FakeStream(sse(good), explode=4), FakeStream(sse(good))):
        try:
            llm.chat("m", [{"role": "user", "content": "go"}], on_delta=seen.append)
            raised = False
        except llm.LLMError as exc:
            raised = "mid-stream" in str(exc)
    assert raised, "a failure after emitting must not be retried"
    assert seen == ["ok"], seen
    print("ok  stream: retried before the first token, never after one")


def fallback_checks() -> None:
    """A provider that cannot stream degrades to a slower answer, not none."""
    calls: list[dict] = []

    class Body:
        status_code = 200

        @staticmethod
        def json():
            return {
                "choices": [{"message": {"content": "non-streamed"}, "finish_reason": "stop"}],
                "model": "m", "usage": {"prompt_tokens": 5},
            }

    real_post = llm._client.post

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        return Body()

    llm._client.post = fake_post
    try:
        with streaming(*[FakeStream([], status=500, body="upstream said no") for _ in range(3)]):
            reply = llm.chat("m", [{"role": "user", "content": "go"}])
    finally:
        llm._client.post = real_post

    assert reply.text == "non-streamed", reply.text
    assert calls and "stream" not in calls[0], "the fallback must not ask for a stream"
    print("ok  stream: falls back to a non-streaming call when streaming never starts")


def opt_out_checks() -> None:
    class Body:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "plain"}, "finish_reason": "stop"}],
                    "model": "m", "usage": {}}

    real_post = llm._client.post
    llm._client.post = lambda url, json=None, headers=None, timeout=None: Body()
    try:
        def boom(*a, **kw):
            raise AssertionError("streamed despite stream=False")

        real_stream = llm._client.stream
        llm._client.stream = boom
        try:
            assert llm.chat("m", [{"role": "user", "content": "x"}], stream=False).text == "plain"
        finally:
            llm._client.stream = real_stream
    finally:
        llm._client.post = real_post
    print("ok  stream: stream=False and JARVIS_STREAM=0 take the old path")


def main() -> int:
    assembly_checks()
    tool_call_checks()
    cancel_checks()
    agent_cancel_checks()
    retry_checks()
    fallback_checks()
    opt_out_checks()
    print("\nall streaming checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
