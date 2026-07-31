"""Thin OpenRouter client.

Every provider behind OpenRouter speaks the OpenAI chat-completions shape, so
this is the only place in the codebase that knows about HTTP.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import config


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


def chat(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout: float = 120.0,
    max_retries: int = 3,
) -> Reply:
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

    headers = {
        "Authorization": f"Bearer {config.api_key()}",
        "Content-Type": "application/json",
        "X-Title": "Jarvis",
    }

    last_error: Exception | None = None
    for attempt in range(max_retries):
        started = time.monotonic()
        try:
            response = httpx.post(
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
