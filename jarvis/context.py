"""Context management: keeping long agentic runs from drowning in stale data.

Two different pressures need two different fixes, and conflating them is the
usual mistake:

  Images — a screenshot costs ~1.5k tokens and only the newest one or two
           matter for deciding the next action. Old ones are pure weight, so
           they are *evicted*: the image part is swapped for a placeholder.

  Text   — tool results (page dumps, search results) pile up. Recent ones stay
           whole, older ones get truncated, and only if the transcript is still
           oversized does the oldest stretch get summarized into a single note.

Summarizing is the last resort, not the first: it costs an API call and loses
detail, while eviction is free and loses nothing that mattered.

One invariant holds the whole thing together: **pruning never deletes a
message.** Every `tool` message must keep the `tool_call_id` its assistant
message referenced, or the next request is rejected. Compaction does delete, so
it only ever cuts at a real user-message boundary — that is the one place where
no assistant/tool group can be split in half.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

EVICTED_IMAGE = "[screenshot from an earlier step — evicted to save context]"
TRUNCATED = "\n[...truncated to save context]"
SUMMARY_PREFIX = "[Summary of earlier conversation — context, not a new request]\n"


@dataclass
class ContextPolicy:
    keep_images: int = 2
    """How many of the most recent images survive intact."""

    keep_full_results: int = 4
    """How many of the most recent tool results stay untruncated."""

    max_old_result_chars: int = 1500
    """Older tool results are cut to this length."""

    image_token_cost: int = 1500
    """Rough per-image token cost, for budget estimation."""

    compact_at_tokens: int = 60_000
    """Summarize the oldest history once the estimate crosses this."""

    keep_recent_messages: int = 14
    """Never compact away the most recent N messages."""

    enabled: bool = True


@dataclass
class ContextStats:
    images_evicted: int = 0
    results_truncated: int = 0
    messages_compacted: int = 0
    tokens_before: int = 0
    tokens_after: int = 0

    @property
    def saved(self) -> int:
        return max(0, self.tokens_before - self.tokens_after)

    def __bool__(self) -> bool:
        return bool(self.images_evicted or self.results_truncated or self.messages_compacted)


def _parts(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    return content if isinstance(content, list) else []


def is_live_image(part: dict[str, Any]) -> bool:
    return part.get("type") in ("image_url", "input_image", "image")


def estimate_tokens(messages: list[dict[str, Any]], policy: ContextPolicy) -> int:
    """Rough token count: ~4 chars per token for text, flat cost per image."""
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content) // 4
        else:
            for part in _parts(message):
                if is_live_image(part):
                    total += policy.image_token_cost
                else:
                    total += len(str(part.get("text", ""))) // 4
        for call in message.get("tool_calls") or []:
            total += len(str(call.get("function", {}).get("arguments", ""))) // 4
    return total


def evict_images(messages: list[dict[str, Any]], policy: ContextPolicy) -> int:
    """Replace all but the newest `keep_images` images with a text placeholder."""
    seen = 0
    evicted = 0
    for message in reversed(messages):
        parts = _parts(message)
        for i, part in enumerate(parts):
            if not is_live_image(part):
                continue
            seen += 1
            if seen > policy.keep_images:
                parts[i] = {"type": "text", "text": EVICTED_IMAGE}
                evicted += 1
    return evicted


def truncate_old_results(messages: list[dict[str, Any]], policy: ContextPolicy) -> int:
    """Shorten older tool results, keeping the most recent ones intact."""
    seen = 0
    truncated = 0
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        seen += 1
        if seen <= policy.keep_full_results:
            continue
        body = message.get("content")
        if not isinstance(body, str) or len(body) <= policy.max_old_result_chars:
            continue
        if body.endswith(TRUNCATED):  # already handled on an earlier pass
            continue
        message["content"] = body[: policy.max_old_result_chars] + TRUNCATED
        truncated += 1
    return truncated


def find_cut_point(messages: list[dict[str, Any]], keep_recent: int) -> int:
    """Index of the newest safe compaction boundary, or 0 if there is none.

    Two things make an index safe, and "looks like a user message" is only the
    first of them.

    The subtle one: an assistant's tool group is *not* a contiguous run of
    `tool` messages. An image-producing tool returns its picture as a separate
    `user` message wedged in behind its `tool` message (invariant 5), so a turn
    with two parallel renders lays out as

        assistant(call_1, call_2) · tool(call_1) · user(image) · tool(call_2)

    and that inner `user(image)` is, on the face of it, a perfectly good cut
    point — no `tool_call_id`, right role. Cutting there deletes the assistant
    message and `call_1`'s result while leaving `call_2`'s behind, pointing at
    an id nothing declares any more, and the next request 400s. It only fires
    past `compact_at_tokens`, so it is invisible until a run is long enough to
    matter.

    So the real test is whether every tool_call declared so far has already been
    answered. We walk forward tracking the outstanding ids and only accept a
    boundary where none are pending.

    Index 1 is never a candidate: that is the session's opening request, the one
    statement of what all this work is *for*, and compaction used to eat it
    first — leaving the model taking direction from a cheap-tier paraphrase of
    its own goal. `compact()` keeps it.
    """
    limit = len(messages) - keep_recent
    pending: set[str] = set()
    cut = 0
    for i, message in enumerate(messages):
        role = message.get("role")
        # Judged *before* this message is folded in: the boundary sits in front
        # of it, so what matters is whether everything earlier is settled.
        if 1 < i < limit and not pending and role == "user" and "tool_call_id" not in message:
            cut = i
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                if call.get("id"):
                    pending.add(call["id"])
        elif role == "tool":
            pending.discard(message.get("tool_call_id", ""))
    return cut


def compact(
    messages: list[dict[str, Any]],
    policy: ContextPolicy,
    summarize: Callable[[str], str],
) -> int:
    """Replace the oldest stretch of history with a summary. Returns messages removed.

    Compacts `messages[2:cut]`, not `[1:cut]`: index 0 is the system message and
    index 1 is the original request. Both are pinned — see `find_cut_point`.
    """
    cut = find_cut_point(messages, policy.keep_recent_messages)
    if cut < 3:  # nothing between the pinned goal and the boundary
        return 0

    transcript = []
    for message in messages[2:cut]:
        role = message.get("role", "?")
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "[image]") if not is_live_image(p) else "[image]" for p in content
            )
        text = (content or "").strip()
        for call in message.get("tool_calls") or []:
            text += f" [called {call.get('function', {}).get('name')}]"
        if text:
            transcript.append(f"{role}: {text[:1200]}")

    if not transcript:
        return 0

    summary = summarize("\n".join(transcript))
    removed = cut - 2
    # A plain user message, not a system one: mid-conversation system messages
    # are not supported across every model OpenRouter serves.
    messages[2:cut] = [{"role": "user", "content": SUMMARY_PREFIX + summary}]
    return removed


def manage(
    messages: list[dict[str, Any]],
    policy: ContextPolicy,
    summarize: Callable[[str], str] | None = None,
    force: bool = False,
) -> ContextStats:
    """Apply the whole policy in order: evict, truncate, then compact if needed."""
    stats = ContextStats()
    if not policy.enabled:
        return stats

    stats.tokens_before = estimate_tokens(messages, policy)
    stats.images_evicted = evict_images(messages, policy)
    stats.results_truncated = truncate_old_results(messages, policy)

    if summarize is not None and (force or estimate_tokens(messages, policy) > policy.compact_at_tokens):
        stats.messages_compacted = compact(messages, policy, summarize)

    stats.tokens_after = estimate_tokens(messages, policy)
    return stats
