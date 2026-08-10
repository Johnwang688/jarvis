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

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import config

EVICTED_IMAGE = "[screenshot from an earlier step — evicted to save context]"
TRUNCATED = "\n[...truncated to save context]"
SUMMARY_PREFIX = "[Summary of earlier conversation — context, not a new request]\n"

# Spilled results older than this are deleted the first time anything spills in
# a given process. They exist to make one run's truncation recoverable, not to
# be an archive.
SPILL_MAX_AGE_DAYS = 7
_pruned = False


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

    spill_dir: Path | None = None
    """Where truncated results are written whole. None means config.SPILL_DIR."""


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


@dataclass
class TokenMeter:
    """Real prompt tokens for the measured prefix; an estimate only for the rest.

    `estimate_tokens` is chars/4 and counts no tool schemas at all, so it reads
    low by however much the schemas weigh — thousands of tokens on a full
    toolset. Judging `compact_at_tokens` against it means the threshold does not
    mean what it says, and the error grows with the number of tools an agent
    carries. The provider reports the true number with every reply, so the fix
    is to hold on to it and estimate only what has been appended since.

    Every measurement is provisional: it describes a prefix that later gets
    mutated in place (eviction, truncation) or cut (compaction). `discount()`
    covers the first, `invalidate()` the second, and both only have to hold
    until the next reply re-measures.
    """

    measured: int = 0
    measured_len: int = 0

    def observe(self, messages: list[dict[str, Any]], prompt_tokens: int) -> None:
        """Record the true size of `messages`, which must be exactly what was sent."""
        if prompt_tokens > 0:
            self.measured = prompt_tokens
            self.measured_len = len(messages)

    def discount(self, tokens: int) -> None:
        """Shrink the measured prefix after messages were rewritten in place."""
        self.measured = max(0, self.measured - max(0, tokens))

    def invalidate(self) -> None:
        """Forget the measurement — messages it covered have been deleted."""
        self.measured = 0
        self.measured_len = 0

    def count(self, messages: list[dict[str, Any]], policy: ContextPolicy) -> int:
        if self.measured and len(messages) >= self.measured_len:
            return self.measured + estimate_tokens(messages[self.measured_len :], policy)
        return estimate_tokens(messages, policy)


def _spill(body: str, policy: ContextPolicy) -> str | None:
    """Write a result somewhere `read_file` can reach it. None if that failed.

    Truncation used to simply drop the tail, which makes it the one context
    operation with no way back: eviction leaves a placeholder saying what it
    was, compaction leaves a summary, but a cut result left nothing at all — so
    a page fetched at step 4 was unrecoverable at step 20 except by fetching it
    again. Writing it out first turns the cut into a pointer.

    Named by content hash, so re-truncating the same result is idempotent and
    two identical results share one file. The body has already been through
    `dispatch()`'s scrub by the time it is a tool message, so nothing spilled
    here carries a credential the transcript would not have carried anyway.
    """
    global _pruned
    try:
        directory = policy.spill_dir or config.SPILL_DIR
        directory.mkdir(parents=True, exist_ok=True)
        if not _pruned:
            _pruned = True
            cutoff = time.time() - SPILL_MAX_AGE_DAYS * 86400
            for stale in directory.glob("*.txt"):
                try:
                    if stale.stat().st_mtime < cutoff:
                        stale.unlink()
                except OSError:
                    pass
        target = directory / (hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:16] + ".txt")
        if not target.exists():
            target.write_text(body, encoding="utf-8")
        return str(target)
    except OSError:
        return None  # spilling is a bonus, never a reason for a turn to fail


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
        # The pointer goes *before* TRUNCATED so the marker stays the last
        # thing in the message — that suffix is how this pass recognises its
        # own earlier work, and idempotence depends on it.
        spilled = _spill(body, policy)
        pointer = (
            f"\n[full result — {len(body):,} chars — saved to {spilled};"
            " read_file it if you need the rest]"
            if spilled
            else ""
        )
        message["content"] = body[: policy.max_old_result_chars] + pointer + TRUNCATED
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
    meter: TokenMeter | None = None,
) -> ContextStats:
    """Apply the whole policy in order: evict, truncate, then compact if needed.

    `meter` carries the provider's real token counts. Without one this falls
    back to the chars/4 estimate throughout, which is what every synthetic test
    and every caller that has no reply to measure gets.
    """
    stats = ContextStats()
    if not policy.enabled:
        return stats

    def budget() -> int:
        return meter.count(messages, policy) if meter else estimate_tokens(messages, policy)

    estimated_before = estimate_tokens(messages, policy)
    stats.tokens_before = budget()
    stats.images_evicted = evict_images(messages, policy)
    stats.results_truncated = truncate_old_results(messages, policy)

    # Both of those rewrote messages the measurement already counted, so the
    # measured prefix is now too big by roughly what they saved. Estimated
    # savings are the right correction: the two numbers are being compared to
    # each other, not to the wire.
    if meter is not None:
        meter.discount(estimated_before - estimate_tokens(messages, policy))

    if summarize is not None and (force or budget() > policy.compact_at_tokens):
        stats.messages_compacted = compact(messages, policy, summarize)
        # Compaction *deletes*. Whatever was measured no longer describes this
        # transcript at all, and unlike a discount there is no honest way to
        # adjust it — so drop it and estimate until the next reply re-measures.
        if meter is not None and stats.messages_compacted:
            meter.invalidate()

    stats.tokens_after = budget()
    return stats
