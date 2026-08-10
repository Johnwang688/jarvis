"""Thin OpenRouter client.

Every provider behind OpenRouter speaks the OpenAI chat-completions shape, so
this is the only place in the codebase that knows about HTTP.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import config

# One keep-alive connection pool for every OpenRouter call (Client is
# thread-safe). Bare httpx.post reconnected per request, which taxed every
# agent step and — audibly — every streamed TTS sentence with a fresh
# TCP+TLS handshake. With the pool, the chat call that produced the reply
# leaves a warm connection for the TTS that speaks it.
# keepalive_expiry is raised from the 5s default: voice turns are minutes
# apart, and an expired pool means the first call of every turn pays
# DNS+TCP+TLS again — on this machine that setup path is usually ~60ms but
# has stalled for seconds when the resolver/route misbehaves (Tailscale DNS
# interception is a suspect). transport retries=2 retries the *connect*
# phase quickly on exactly those blips.
_client = httpx.Client(
    timeout=120.0,
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10,
                        keepalive_expiry=300.0),
    transport=httpx.HTTPTransport(retries=2),
)


@dataclass
class Reply:
    """One assistant turn, normalized."""

    message: dict[str, Any]
    finish_reason: str
    model: str
    latency_s: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.message.get("content") or ""

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return self.message.get("tool_calls") or []


class LLMError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    """The caller asked to stop while the model was still generating.

    Raised only from a streaming read, and only *before* any tool has run — so
    the transcript is whole when it surfaces and the assistant turn is simply
    never appended. That is what makes mid-generation cancellation safe under
    invariant 3.
    """


def _accumulate(chunk: dict[str, Any], state: dict[str, Any], on_delta) -> None:
    """Fold one SSE chunk into the reply being assembled.

    Tool calls arrive in pieces and are keyed by `index`, not by id: the id and
    function name usually come in the first fragment for that index and the
    arguments dribble in across later ones, so they have to be stitched by
    position rather than matched by name.
    """
    if chunk.get("model"):
        state["model"] = chunk["model"]
    if chunk.get("usage"):
        state["usage"] = chunk["usage"]

    for choice in chunk.get("choices") or []:
        if choice.get("finish_reason"):
            state["finish_reason"] = choice["finish_reason"]
        delta = choice.get("delta") or {}

        piece = delta.get("content")
        if piece:
            state["content"].append(piece)
            if on_delta is not None:
                on_delta(piece)

        for call in delta.get("tool_calls") or []:
            index = call.get("index", 0)
            slot = state["tool_calls"].setdefault(
                index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            )
            if call.get("id"):
                slot["id"] = call["id"]
            if call.get("type"):
                slot["type"] = call["type"]
            function = call.get("function") or {}
            if function.get("name"):
                slot["function"]["name"] += function["name"]
            if function.get("arguments"):
                slot["function"]["arguments"] += function["arguments"]


def _stream_once(
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
    on_delta,
    should_stop,
) -> tuple[Reply | None, Exception | None, bool]:
    """One streaming attempt. Returns (reply, error, anything_emitted).

    `anything_emitted` is what decides whether a failure may be retried: once
    text has gone out to a surface, replaying the call would duplicate it.
    """
    state: dict[str, Any] = {
        "content": [], "tool_calls": {}, "finish_reason": "", "model": "", "usage": {},
    }
    emitted = False
    started = time.monotonic()
    try:
        with _client.stream(
            "POST", config.OPENROUTER_URL, json={**payload, "stream": True},
            headers=headers, timeout=timeout,
        ) as response:
            if response.status_code >= 400:
                response.read()
                return None, LLMError(f"HTTP {response.status_code}: {response.text[:500]}"), False
            for line in response.iter_lines():
                # Checked per line, which is what buys mid-generation
                # cancellation: the owner talking over a long answer no longer
                # waits for the whole model call to finish first.
                if should_stop is not None and should_stop():
                    raise Cancelled()
                line = line.strip()
                # OpenRouter sends `: OPENROUTER PROCESSING` keepalive comments.
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if "error" in chunk and not chunk.get("choices"):
                    err = chunk["error"]
                    message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    return None, LLMError(f"provider error: {message[:300]}"), emitted
                before = len(state["content"])
                _accumulate(chunk, state, on_delta)
                emitted = emitted or len(state["content"]) > before
    except Cancelled:
        raise
    except httpx.HTTPError as exc:
        return None, exc, emitted

    message: dict[str, Any] = {"content": "".join(state["content"]) or None}
    if state["tool_calls"]:
        message["tool_calls"] = [state["tool_calls"][i] for i in sorted(state["tool_calls"])]
    usage = state["usage"] or {}
    return (
        Reply(
            message=message,
            finish_reason=state["finish_reason"] or "stop",
            model=state["model"] or payload.get("model", ""),
            latency_s=time.monotonic() - started,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            cost_usd=float(usage.get("cost") or 0.0),
            raw={"streamed": True, "usage": usage},
        ),
        None,
        emitted,
    )


def chat(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    timeout: float = 120.0,
    max_retries: int = 3,
    provider: str | None = None,
    on_delta: Any = None,
    should_stop: Any = None,
    stream: bool | None = None,
) -> Reply:
    """One chat completion.

    Streams by default (`config.STREAM`). The return type is identical either
    way, so nothing that calls this had to change — what streaming buys is
    `should_stop` being answerable *while the model is still generating*
    instead of only between steps, and `on_delta` for a surface that wants to
    render text as it arrives.
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": config.MAX_TOKENS if max_tokens is None else max_tokens,
        # Ask OpenRouter to bill-report each call so the bench can compare cost.
        "usage": {"include": True},
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    # Optional routing pin — see config.CHAT_PROVIDER for why this exists and
    # why it refuses fallbacks. Off unless asked for, so default behaviour is
    # unchanged: OpenRouter picks.
    pinned = provider if provider is not None else config.CHAT_PROVIDER
    if pinned:
        payload["provider"] = {"order": [pinned], "allow_fallbacks": False}

    headers = {
        "Authorization": f"Bearer {config.api_key()}",
        "Content-Type": "application/json",
        "X-Title": "Jarvis",
    }

    last_error: Exception | None = None

    if stream if stream is not None else config.STREAM:
        for attempt in range(max_retries):
            reply, error, emitted = _stream_once(payload, headers, timeout, on_delta, should_stop)
            if reply is not None:
                return reply
            last_error = error
            # Once bytes have reached a surface, replaying the call would
            # duplicate what the owner already saw, so a mid-stream failure is
            # not retryable. A failure before the first token is.
            if emitted:
                raise LLMError(f"{model} failed mid-stream: {last_error}")
            time.sleep(2**attempt)
        # Every streaming attempt failed before producing anything. Fall through
        # to the non-streaming path once: a provider that cannot stream should
        # degrade to a slower answer, not to no answer.

    for attempt in range(max_retries):
        started = time.monotonic()
        try:
            response = _client.post(
                config.OPENROUTER_URL, json=payload, headers=headers, timeout=timeout
            )
        except httpx.HTTPError as exc:
            last_error = exc
            time.sleep(2**attempt)
            continue

        elapsed = time.monotonic() - started

        if response.status_code in (408, 429) or response.status_code >= 500:
            last_error = LLMError(f"HTTP {response.status_code}: {response.text[:300]}")
            time.sleep(2**attempt)
            continue
        if response.status_code >= 400:
            raise LLMError(f"HTTP {response.status_code}: {response.text[:500]}")

        body = response.json()
        # OpenRouter can return HTTP 200 with an error object instead of choices
        # (e.g. the routed provider rejected the request shape).
        if "error" in body and "choices" not in body:
            err = body["error"]
            message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise LLMError(f"provider error: {message[:300]}")
        if "choices" not in body:
            raise LLMError(f"Malformed response: {str(body)[:500]}")

        choice = body["choices"][0]
        usage = body.get("usage") or {}
        return Reply(
            message=choice.get("message") or {},
            finish_reason=choice.get("finish_reason") or "stop",
            model=body.get("model", model),
            latency_s=elapsed,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            cost_usd=float(usage.get("cost") or 0.0),
            raw=body,
        )

    raise LLMError(f"{model} failed after {max_retries} attempts: {last_error}")


def speech(
    text: str,
    model: str,
    voice: str | None = None,
    instructions: str | None = None,
    speed: float | None = None,
    response_format: str = "mp3",
    provider: str | None = None,
    timeout: float = 60.0,
    max_retries: int = 3,
) -> bytes:
    """Text-to-speech via OpenRouter's audio endpoint. Returns raw audio bytes.

    voice, instructions, and speed are provider-specific; omit them to get
    the model's defaults. `provider` PINS routing — no fallbacks: the same
    model can be orders of magnitude slower on another host, and OpenRouter
    falls back on transient blips, which is how "prefer the fast provider"
    still produced 11s chunks. Transient errors on the pinned host are
    covered by this function's own retry loop instead.
    """
    payload: dict[str, Any] = {
        "model": model,
        "input": text,
        "response_format": response_format,
    }
    if voice:
        payload["voice"] = voice
    if instructions:
        payload["instructions"] = instructions
    if speed and speed != 1.0:
        payload["speed"] = speed
    if provider:
        payload["provider"] = {"order": [provider], "allow_fallbacks": False}

    headers = {
        "Authorization": f"Bearer {config.api_key()}",
        "Content-Type": "application/json",
        "X-Title": "Jarvis",
    }

    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            response = _client.post(
                config.OPENROUTER_SPEECH_URL, json=payload, headers=headers, timeout=timeout
            )
        except httpx.HTTPError as exc:
            last_error = exc
            time.sleep(2**attempt)
            continue

        if response.status_code in (408, 429) or response.status_code >= 500:
            last_error = LLMError(f"HTTP {response.status_code}: {response.text[:300]}")
            time.sleep(2**attempt)
            continue
        if response.status_code >= 400:
            raise LLMError(f"HTTP {response.status_code}: {response.text[:500]}")

        if not response.content:
            raise LLMError("speech endpoint returned an empty body")
        return response.content

    raise LLMError(f"{model} speech failed after {max_retries} attempts: {last_error}")


def transcribe(
    audio: bytes,
    model: str,
    filename: str = "audio.webm",
    mime: str = "audio/webm",
    language: str | None = None,
    timeout: float = 60.0,
    max_retries: int = 3,
) -> str:
    """Speech-to-text via OpenRouter's transcription endpoint. Returns the text."""
    data: dict[str, Any] = {"model": model}
    if language:
        data["language"] = language

    headers = {
        "Authorization": f"Bearer {config.api_key()}",
        "X-Title": "Jarvis",
    }

    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            response = _client.post(
                config.OPENROUTER_TRANSCRIPTION_URL,
                data=data,
                files={"file": (filename, audio, mime)},
                headers=headers,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            last_error = exc
            time.sleep(2**attempt)
            continue

        if response.status_code in (408, 429) or response.status_code >= 500:
            last_error = LLMError(f"HTTP {response.status_code}: {response.text[:300]}")
            time.sleep(2**attempt)
            continue
        if response.status_code >= 400:
            raise LLMError(f"HTTP {response.status_code}: {response.text[:500]}")

        body = response.json()
        if "text" not in body:
            raise LLMError(f"Malformed transcription response: {str(body)[:300]}")
        return (body["text"] or "").strip()

    raise LLMError(f"{model} transcription failed after {max_retries} attempts: {last_error}")
