"""Thin OpenRouter client.

Every provider behind OpenRouter speaks the OpenAI chat-completions shape, so
this is the only place in the codebase that knows about HTTP.
"""

from __future__ import annotations

import threading
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


_catalog: dict[str, dict[str, Any]] | None = None
_catalog_failed_at: float = 0.0
_catalog_lock = threading.Lock()

# How long a failed catalog fetch is remembered. A failure must not be cached
# the way a success is — the next switch should get a fresh answer — but it
# cannot be retried freely either: rendering the roster menu asks about every
# entry, so an unreachable catalog would otherwise cost one full timeout per
# model before printing a single line.
CATALOG_RETRY_S = 60.0


def catalog(refresh: bool = False) -> dict[str, dict[str, Any]]:
    """OpenRouter's model list, keyed by model id. **Fails soft — {} on error.**

    Returns `{id: {"context_length", "vision", "tools"}}`. This is where model
    capabilities come from, rather than a table in the repo: written-down
    model facts rot silently, and a stale "this model does vision" is a 400
    mid-conversation.

    Because it fails soft, callers must treat a missing id — or an empty
    catalog — as *unknown*, never as *no*. A network blip must not be able to
    veto a switch the owner asked for; the lookup only ever adds safety.
    """
    global _catalog, _catalog_failed_at
    with _catalog_lock:
        if _catalog is not None and not refresh:
            return _catalog
        if not refresh and time.monotonic() - _catalog_failed_at < CATALOG_RETRY_S:
            return {}  # recently unreachable; don't pay the timeout again
        try:
            response = _client.get(config.OPENROUTER_MODELS_URL, timeout=15.0)
            response.raise_for_status()
            entries = response.json()["data"]
        except Exception:
            # Never cached as data — only as "don't ask again just yet".
            _catalog_failed_at = time.monotonic()
            return {}

        parsed: dict[str, dict[str, Any]] = {}
        for entry in entries:
            model_id = entry.get("id")
            if not model_id:
                continue
            architecture = entry.get("architecture") or {}
            modalities = architecture.get("input_modalities") or []
            parameters = entry.get("supported_parameters") or []
            parsed[model_id] = {
                "context_length": entry.get("context_length") or 0,
                "vision": "image" in modalities,
                "tools": "tools" in parameters,
            }
        _catalog = parsed
        return _catalog


def chat(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout: float = 120.0,
    max_retries: int = 3,
    providers: list[str] | None = None,
) -> Reply:
    """`providers` PINS routing to an ordered allowlist of hosts, no fallbacks.

    Same mechanism `speech()` uses, for a different reason: there it was
    latency, here it is *who is serving the model*. An open-weight model can be
    hosted by several companies in several countries, and which one answers is
    a routing decision — so the list is ordered (first choice first) and
    `allow_fallbacks` is off, which bounds the request to these hosts rather
    than merely preferring them. A model with no pin routes normally.
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # Ask OpenRouter to bill-report each call so the bench can compare cost.
        "usage": {"include": True},
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if providers:
        # Falls through the list in order and hard-fails past it. That is the
        # point: a pin that silently reroutes off the allowlist is not a pin.
        payload["provider"] = {"order": list(providers), "allow_fallbacks": False}

    headers = {
        "Authorization": f"Bearer {config.api_key()}",
        "Content-Type": "application/json",
        "X-Title": "Jarvis",
    }

    last_error: Exception | None = None
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
