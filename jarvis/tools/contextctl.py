"""Tools that operate on the active agent conversation."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

from .. import context
from . import ToolResult, tool


@dataclass
class ActiveContext:
    messages: list[dict[str, Any]]
    policy: context.ContextPolicy
    summarize: Callable[[str], str]
    on_event: Callable[[str, Any], None]


_active: ContextVar[ActiveContext | None] = ContextVar("active_context", default=None)


def bind(active: ActiveContext):
    return _active.set(active)


def reset(token) -> None:
    _active.reset(token)


def compact_now() -> str:
    active = _active.get()
    if active is None:
        return "Error: no active agent conversation is available."
    stats = context.manage(
        active.messages, active.policy, summarize=active.summarize, force=True
    )
    active.on_event("context", stats)
    return f"Compacted; saved {stats.saved:,} tokens."


@tool
def compact_context() -> ToolResult:
    """Compact the current conversation using the same summarizer as automatic compaction."""
    return ToolResult(compact_now())
