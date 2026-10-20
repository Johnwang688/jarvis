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
from copy import deepcopy
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import agent as agent_mod  # noqa: E402
from jarvis import config as _skills_config  # noqa: E402
_skills_config.CLAUDE_SKILLS_DIR = None  # never read the owner's real ~/.claude/skills
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
        seen.append(deepcopy(json))
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


def provider_pin_checks() -> None:
    """A pin is one tag or an ordered comma list; fallbacks never leave it."""
    assert llm.provider_order("") == [] and llm.provider_order(None) == []
    assert llm.provider_order("deepinfra") == ["deepinfra"]
    assert llm.provider_order("deepinfra, novita ,z-ai,") == ["deepinfra", "novita", "z-ai"]

    chunks = [{"choices": [{"delta": {"content": "x"}}]},
              {"choices": [{"finish_reason": "stop", "delta": {}}]}]
    # explicit argument beats the config default, and an ordered list goes out as `order`
    with no_post(), streaming(FakeStream(sse(chunks))) as sent:
        llm.chat("m", [{"role": "user", "content": "hi"}], provider="deepinfra,novita,z-ai")
    assert sent[0]["provider"] == {"order": ["deepinfra", "novita", "z-ai"], "allow_fallbacks": False}, sent[0].get("provider")
    # the config default is used when no argument is given, and "" sends nothing
    real = llm.config.CHAT_PROVIDER
    try:
        llm.config.CHAT_PROVIDER = "novita"
        with no_post(), streaming(FakeStream(sse(chunks))) as sent:
            llm.chat("m", [{"role": "user", "content": "hi"}])
        assert sent[0]["provider"] == {"order": ["novita"], "allow_fallbacks": False}
        llm.config.CHAT_PROVIDER = ""
        with no_post(), streaming(FakeStream(sse(chunks))) as sent:
            llm.chat("m", [{"role": "user", "content": "hi"}])
        assert "provider" not in sent[0], sent[0].get("provider")
    finally:
        llm.config.CHAT_PROVIDER = real
    print("ok  provider pin: single tag, ordered list, config default, and no pin")


def _breakpoints(messages) -> list[int]:
    return [i for i, message in enumerate(messages)
            if any("cache_control" in part for part in (message.get("content") or [])
                   if isinstance(part, dict))]


def _plain_messages(messages):
    """Undo wire-only cache annotations for transcript prefix comparisons."""
    plain = deepcopy(messages)
    for message in plain:
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                part.pop("cache_control", None)
            if len(content) == 1 and content[0].get("type") == "text":
                message["content"] = content[0]["text"]
    return plain


def cache_control_checks() -> None:
    """Claude caches nonblank text at stable boundaries on a wire copy."""
    messages = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
                {"role": "tool", "tool_call_id": "c1", "content": "result"},
                {"role": "assistant", "tool_calls": [{"id": "c2"}]},
                {"role": "user", "content": "volatile"}]
    tools = [{"type": "function", "function": {"name": "test", "parameters": {"type": "object"}}}]
    chunks = [{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]

    class Body:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    def request(model, transcript, tail, stream, **kwargs):
        before = deepcopy(transcript)
        posts = []

        def fake_post(url, json=None, **kwargs):
            posts.append(deepcopy(json))
            return Body()

        with patch.object(llm._client, "post", fake_post), streaming(FakeStream(sse(chunks))) as sent:
            reply = llm.chat(model, transcript, tools=tools, stream=stream, volatile_tail=tail, **kwargs)
        assert reply.text == "ok"
        assert len(sent) == int(stream) and len(posts) == int(not stream)
        assert transcript == before, "wire annotations mutated the caller"
        assert isinstance(transcript[0]["content"], str)
        return (sent if stream else posts)[0]

    baseline = None
    for model in ("anthropic/claude-haiku-5.5", "~anthropic/claude-sonnet-latest",
                  "openai/gpt-6-luna", "openrouter/free", "google/gemini-3.7-flash",
                  "other/anthropic/claude", "anthropic-not/claude", ""):
        claude = model.startswith(("anthropic/", "~anthropic/"))
        for stream in (True, False):
            payload = request(model, messages, 1, stream)
            assert "cache_control" not in payload
            assert payload["tools"] == tools
            if claude:
                assert _breakpoints(payload["messages"]) == [0, 3]
                assert json.dumps(payload).count('"cache_control"') == 2
                for i in (0, 3):
                    assert payload["messages"][i]["content"][-1]["cache_control"] == {"type": "ephemeral"}
                assert _plain_messages(payload["messages"]) == messages
                if baseline is None:
                    baseline = payload["messages"]
                assert payload["messages"] == baseline
            else:
                assert '"cache_control"' not in json.dumps(payload)
                assert payload["messages"] == messages

    model = "anthropic/claude-haiku-5.5"
    # Default tail=0 caches the newest message; all rebuilt trailing messages
    # are skipped when there is more than one.
    assert _breakpoints(request(model, messages, 0, False)["messages"]) == [0, 5]
    assert _breakpoints(request(model, messages, 3, False)["messages"]) == [0, 1]
    assert _breakpoints(request(model, [messages[0], messages[-1]], 1, False)["messages"]) == [0]
    assert _breakpoints(request(model, [messages[0]], 0, False)["messages"]) == [0]
    assert _breakpoints(request(model, messages[:3], 0, False)["messages"]) == [0, 1]

    parts = [{"role": "system", "content": "sys"},
             {"role": "user", "content": [{"type": "text", "text": "first"},
                                            {"type": "text", "text": "last"}]},
             messages[-1]]
    wire = request(model, parts, 1, False)["messages"]
    assert "cache_control" not in wire[1]["content"][0]
    assert wire[1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert _breakpoints(wire) == [0, 1]

    # Only marked dicts/parts are copied; unmarked ones retain their identities.
    wire = llm._cache_messages(model, parts, 1)
    assert wire is not parts and wire[0] is not parts[0] and wire[1] is not parts[1]
    assert wire[2] is parts[2]
    assert wire[1]["content"] is not parts[1]["content"]
    assert wire[1]["content"][0] is parts[1]["content"][0]
    assert wire[1]["content"][-1] is not parts[1]["content"][-1]

    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,ZmFrZQ=="}}
    carrier = {"role": "user", "content": [image]}
    cases = [
        # An image carrier at the tail walks back to the preceding tool result.
        ([messages[0], messages[3], carrier, messages[-1]], (), [0, 1]),
        # An attachment keeps its text markable, even before an image.
        ([messages[0], {"role": "user", "content": [
            {"type": "text", "text": "look at this"}, image]}, messages[-1]], (), [0, 1]),
        # Steering also skips any blank text after the image.
        ([messages[0], {"role": "user", "content": [
            {"type": "text", "text": "[owner steering] look"}, image,
            {"type": "text", "text": "  \n"}]}, messages[-1]], (), [0, 1]),
        # An image-only middle point walks back, independently of the tail.
        ([messages[0], messages[3], carrier, messages[1], messages[-1]], (2,), [0, 1, 3]),
        ([messages[0], messages[3], {"role": "tool", "content": " \n\t"},
          {"role": "assistant", "content": "tool preamble", "tool_calls": [{"id": "c2"}]},
          messages[-1]], (), [0, 1]),
        ([messages[0], messages[3],
          {"role": "assistant", "content": "empty calls", "tool_calls": []},
          messages[-1]], (), [0, 1]),
    ]
    for transcript, points, expected in cases:
        for stream in (True, False):
            wire = request(model, transcript, 1, stream, cache_points=points)["messages"]
            assert _breakpoints(wire) == expected
            for message in wire:
                content = message.get("content")
                if isinstance(content, list):
                    for part in content:
                        if "cache_control" in part:
                            assert part["type"] == "text" and part["text"].strip()
            assert _plain_messages(wire) == _plain_messages(transcript)
    mixed = cases[2][0]
    wire = llm._cache_messages(model, mixed, 1)
    assert wire[1]["content"][0] is not mixed[1]["content"][0]
    assert wire[1]["content"][1] is image
    assert "cache_control" not in image

    many = [{"role": "user", "content": str(i)} for i in range(10)]
    many[0]["role"] = "system"
    # Drop oldest middle boundaries after walking/deduping, regardless of order.
    for stream in (True, False):
        wire = request(model, many, 1, stream, cache_points=(7, 1, 6, 2, 6, 0, 8))["messages"]
        assert _breakpoints(wire) == [0, 6, 7, 8]
    for stream in (True, False):
        wire = request(model, many, 1, stream, cache_points=(1, 2, 3), cache=False)["messages"]
        assert wire == many and not _breakpoints(wire)
    assert not _breakpoints(llm._cache_messages(model, [
        {"role": "system", "content": " "}, carrier], 0))

    posts = []
    before = deepcopy(messages)

    def fake_post(url, json=None, **kwargs):
        posts.append(deepcopy(json))
        return Body()

    with patch.object(llm._client, "post", fake_post), patch.object(llm.time, "sleep"), \
            streaming(FakeStream([], status=500, body="unavailable")) as sent:
        llm.chat(model, messages, stream=True, max_retries=1, volatile_tail=1)
    assert len(sent) == len(posts) == 1
    assert sent[0]["messages"] == posts[0]["messages"] == baseline
    assert "cache_control" not in sent[0] and "cache_control" not in posts[0]
    assert messages == before
    print("ok  caching: explicit stable breakpoints, Claude only, no mutation, all paths")


def agent_cache_prefix_checks() -> None:
    """Real tool dispatch changes the plan tail, but preserves cached prefixes."""
    def tool_reply(cid, tool_name, **arguments):
        call = {"index": 0, "id": cid, "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(arguments)}}
        return FakeStream(sse([{"choices": [{"delta": {"tool_calls": [call]},
                                             "finish_reason": "tool_calls"}]}]))

    def text_reply(text, reason="stop"):
        return FakeStream(sse([{"choices": [{"delta": {"content": text},
                                             "finish_reason": reason}]}]))

    with tempfile.TemporaryDirectory() as raw, \
            patch.object(llm.config, "SKILLS_DIR", Path(raw) / "skills"), \
            patch.object(llm.config, "MODEL_CACHE_PATH", Path(raw) / "catalog.json"), \
            patch.object(llm.config, "AVATAR_STATE_PATH", Path(raw) / "avatar.json"), \
            patch.object(llm.config, "AVATAR_ENV", "jarvis"), \
            patch.object(llm.config, "STREAM", True):
        skill = llm.config.SKILLS_DIR / "test"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\ndescription: Test skill\n---\nFollow the plan.\n")
        agent = agent_mod.Agent(model="anthropic/claude-haiku-5.5",
                               tool_names=["skill_read", "plan_write"], max_steps=8)
        responses = [tool_reply("c1", "plan_write", plan="- [ ] read skill"),
                     tool_reply("c2", "skill_read", name="test"),
                     tool_reply("c3", "plan_write", plan="- [x] read skill"),
                     text_reply("done"), text_reply("second done")]
        with no_post(), streaming(*responses) as sent:
            assert agent.run_turn("follow the plan").text == "done"
            assert agent.run_turn("what next?").text == "second done"
        assert len(sent) == 5
        plains = [_plain_messages(p["messages"]) for p in sent]
        assert len({p[-1]["content"] for p in plains}) > 1, "context did not change"
        assert "- [x] read skill" in plains[-1][-1]["content"]
        for payload, plain in zip(sent, plains):
            messages = payload["messages"]
            points = _breakpoints(messages)
            assert len(points) == 2 and points[0] == 0, "missing explicit stable breakpoints"
            assert points[1] == len(messages) - 2
            assert plain[-1]["content"].startswith(agent_mod.CONTEXT_BLOCK_PREFIX)
            assert messages[-1] == _plain_messages(messages)[-1], "context block marked"
            assert "cache_control" not in payload
        for current, following in zip(sent, sent[1:]):
            end = _breakpoints(current["messages"])[1] + 1
            assert _plain_messages(current["messages"][:end]) == _plain_messages(following["messages"][:end]), \
                "cached prefix changed on the next request"
        assert '"cache_control"' not in json.dumps(agent.messages)
        assert all(isinstance(m.get("content"), str) or m.get("content") is None
                   for m in agent.messages)

        # A token-cap continuation uses the same transcript call and exclusion.
        with no_post(), streaming(text_reply("cut", "length"), text_reply("finished")) as sent:
            assert agent.run_turn("continue answer").text == "cutfinished"
        assert len(sent) == 2
        assert all(_breakpoints(p["messages"])[-1] == len(p["messages"]) - 2 for p in sent)

        # At exhaustion the handoff prompt follows the context block, so the
        # identity check returns zero rather than skipping that stable prompt.
        agent.max_steps = 1
        posts = []

        def handoff_post(url, json=None, **kwargs):
            posts.append(deepcopy(json))
            assert agent._volatile_tail() == 0
            return type("Body", (), {"status_code": 200, "json": staticmethod(
                lambda: {"choices": [{"message": {"content": "handoff"}}]})})()

        with patch.object(llm._client, "post", handoff_post), streaming(
                tool_reply("c4", "plan_write", plan="- [ ] more work")):
            assert "handoff" in agent.run_turn("more work").text
        assert len(posts) == 1
        assert _breakpoints(posts[0]["messages"])[-1] == len(posts[0]["messages"]) - 1
        for message, plain in zip(posts[0]["messages"], _plain_messages(posts[0]["messages"])):
            if isinstance(plain.get("content"), str) and plain["content"].startswith(agent_mod.CONTEXT_BLOCK_PREFIX):
                assert message == plain, "handoff context block marked"
    print("ok  caching: real Agent prefixes across three tool steps and a second turn, continuation, handoff")


def agent_long_cache_prefix_checks() -> None:
    """Separate windows retain a stable prefix and reuse a longer one when possible."""
    with tempfile.TemporaryDirectory() as raw, \
            patch.object(llm.config, "SKILLS_DIR", Path(raw) / "skills"), \
            patch.object(llm.config, "SPILL_DIR", Path(raw) / "spill"), \
            patch.object(llm.config, "AVATAR_STATE_PATH", Path(raw) / "avatar.json"), \
            patch.object(llm.config, "AVATAR_ENV", "jarvis"), \
            patch.object(llm.config, "STREAM", True):
        skill = llm.config.SKILLS_DIR / "test"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\ndescription: Large-result test\n---\nFollow the plan.\n")
        agent = agent_mod.Agent(model="anthropic/claude-haiku-5.5",
                               tool_names=["skill_read", "plan_write"], max_steps=18)
        assert agent.policy == agent_mod.context.ContextPolicy()
        responses = []
        for step in range(1, 17):
            call = {"index": 0, "id": f"long{step}", "type": "function",
                    "function": {"name": "skill_read", "arguments": '{"name":"test"}'}}
            responses.append(FakeStream(sse([{"choices": [{"delta": {"tool_calls": [call]},
                                                            "finish_reason": "tool_calls"}]}])))
        responses.append(FakeStream(sse([{"choices": [{"delta": {"content": "done"},
                                                        "finish_reason": "stop"}]}])))

        def dispatch(call):
            step = int(call["id"][4:])
            return agent_mod.tools.ToolResult(text=f"result {step}: " + "R" * 4000,
                                             image_b64="ZmFrZQ==" if step in (2, 9, 14) else None)

        with patch.object(agent, "_dispatch_one", dispatch), no_post(), streaming(*responses) as sent:
            assert agent.run_turn("sixteen large tool results, with sparse images").text == "done"
        assert len(sent) == 17
        plains = [_plain_messages(payload["messages"]) for payload in sent]
        assert all(plain[-1]["content"].startswith(agent_mod.CONTEXT_BLOCK_PREFIX) for plain in plains), \
            [(i, plain[-1]["content"][:100]) for i, plain in enumerate(plains)]
        assert any(agent_mod.context.TRUNCATED in m.get("content", "")
                   for m in plains[-1] if m["role"] == "tool")
        assert any(p.get("text") == agent_mod.context.EVICTED_IMAGE
                   for m in sent[-1]["messages"] for p in agent_mod.context._parts(m))
        middle_count = four_count = longer_reused = longer_invalidated = 0
        for current, following in zip(sent, sent[1:]):
            points = _breakpoints(current["messages"])
            assert 0 in points and len(points) <= 4
            if len(points) >= 3:
                middle_count += 1
                end = points[1] + 1
                assert _plain_messages(current["messages"][:end]) == \
                    _plain_messages(following["messages"][:end]), "earliest middle cached prefix changed"
            if len(points) == 4:
                four_count += 1
                end = points[2] + 1
                before = _plain_messages(current["messages"][:end])
                after = _plain_messages(following["messages"][:end])
                if before == after:
                    longer_reused += 1
                    # The extra point saves actual result text beyond the older
                    # point; merely adding a nearby empty/image block is no fix.
                    extra = before[points[1] + 1:]
                    assert sum(len(m.get("content", "")) for m in extra
                               if m["role"] == "tool") > 1500
                else:
                    longer_invalidated += 1
                    # The other window may still rewrite this longer prefix;
                    # the earlier point above remains the stable fallback.
                    assert all(old["role"] in ("tool", "user")
                               for old, new in zip(before, after) if old != new)
        assert middle_count >= 5, "long turn never acquired a useful middle breakpoint"
        assert four_count >= 4, "sparse images held back the independent tool boundary"
        assert longer_reused >= 3, "extra boundary never reused a longer result prefix"
        assert longer_invalidated >= 1, "fixture never exercised the other window crossing a boundary"
        assert '"cache_control"' not in json.dumps(agent.messages)

        # The boundary follows the actual policy, counting images, not carriers.
        agent.messages = [{"role": "system", "content": "sys"},
                          {"role": "tool", "content": "old"},
                          {"role": "user", "content": [
                              {"type": "input_image"}, {"type": "image"}]},
                          {"role": "tool", "content": "new"}]
        assert agent._cache_points() == (1,)
        agent.policy.keep_full_results = 1
        assert agent._cache_points() == (1, 2)
        agent.policy.keep_images = 3
        assert agent._cache_points() == (2,)
        agent.policy.keep_full_results = 3
        assert agent._cache_points() == ()
        agent.policy.keep_full_results = agent.policy.keep_images = 0
        assert agent._cache_points() == ()
        # Both raw window boundaries can walk back to the same text block.
        agent.policy.keep_full_results = agent.policy.keep_images = 1
        assert agent._cache_points() == (1, 2)
        wire = llm._cache_messages(agent.model, agent.messages, 0, agent._cache_points())
        assert _breakpoints(wire) == [0, 1, 3]
        agent.policy.enabled = False
        assert agent._cache_points() == ()
    print("ok  caching: real Agent, sixteen 4k results, independent image/tool windows and four points")


def summarize_cache_checks() -> None:
    agent = agent_mod.Agent(tool_names=[])
    calls = []

    def fake(model, messages, **kwargs):
        calls.append(kwargs)
        return llm.Reply(message={"content": "summary"},
                         model=model, latency_s=0.0,
                         finish_reason="length" if len(calls) == 1 else "stop")

    with patch.object(llm, "chat", fake):
        assert agent._summarize("large transcript") == "summary"
    assert len(calls) == 2 and all(call.get("cache") is False for call in calls)
    print("ok  caching: compaction and its retry opt out")


def main() -> int:
    llm.config.MODELS_PATH = Path(tempfile.mkdtemp()) / "models.json"
    cache_control_checks()
    agent_cache_prefix_checks()
    agent_long_cache_prefix_checks()
    summarize_cache_checks()
    assembly_checks()
    tool_call_checks()
    cancel_checks()
    agent_cancel_checks()
    retry_checks()
    fallback_checks()
    opt_out_checks()
    provider_pin_checks()
    print("\nall streaming checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
