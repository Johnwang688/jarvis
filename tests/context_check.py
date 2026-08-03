"""Synthetic checks for context management. Free — no API, fully repeatable.

CLAUDE.md has claimed since the beginning that there is a test for
`find_cut_point`. There was not, which is how the orphan bug below lived in the
compaction path unnoticed: it only fires past compact_at_tokens, so nothing
short ever reached it.

What must hold:
  1. pruning never deletes a message, and never orphans a tool_call_id
  2. a cut is never taken inside an assistant's tool group — including the case
     where an image result splits that group with a bare `user` message
  3. the original request (messages[1]) survives compaction
  4. image eviction keeps the newest N and replaces the rest in place
  5. truncation shortens old results only, and is idempotent
  6. manage() leaves a transcript that is still wire-valid

Run:  .venv/bin/python tests/context_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import context


def _image_msg(tag: str = "AAA") -> dict:
    return {
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{tag}"}}],
    }


def _assert_wire_valid(messages: list[dict]) -> None:
    """Every tool message must answer a tool_call some surviving assistant declared.

    This is the check the API performs for us, expensively, in production.
    """
    declared = {
        call.get("id")
        for m in messages
        if m.get("role") == "assistant"
        for call in (m.get("tool_calls") or [])
    }
    orphans = [
        m.get("tool_call_id")
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id") not in declared
    ]
    assert not orphans, f"orphaned tool_call_ids (next request would 400): {orphans}"


def orphan_checks() -> None:
    """The bug: two parallel image-producing calls, cut taken between them.

    agent.py appends an image result as its own `user` message right behind the
    `tool` message it belongs to (invariant 5). With two renders in one
    assistant turn the layout is

        assistant(call_1, call_2) · tool(call_1) · user(image) · tool(call_2)

    and that inner user message has the right role and no tool_call_id, so the
    old cut-point rule accepted it — deleting the assistant message and call_1's
    result while leaving call_2's behind.

    The split group is placed *last* on purpose. The two rules only disagree
    when that inner image message is the newest candidate — put anything safe
    after it and both rules pick that instead, and the test passes against the
    bug. (It did, the first time it was written.)
    """
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "render the assembly two ways"},
        # A complete group, so there is a legitimate boundary to fall back to.
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "x", "function": {"name": "cad_status", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "x", "name": "cad_status", "content": "sandbox ready"},
        {"role": "user", "content": "good, now the renders"},
        # The split group: image results wedged between two tool results.
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_1", "function": {"name": "cad_render", "arguments": "{}"}},
                {"id": "call_2", "function": {"name": "cad_render", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "cad_render", "content": "iso view"},
        _image_msg("A"),
        {"role": "tool", "tool_call_id": "call_2", "name": "cad_render", "content": "top view"},
        _image_msg("B"),
    ]

    cut = context.find_cut_point(messages, keep_recent=2)
    assert cut != 7, "cut landed on the image message inside the tool group"
    assert cut == 4, f"expected the fallback boundary at 4, got {cut}"

    removed = context.compact(
        messages, context.ContextPolicy(keep_recent_messages=2), summarize=lambda t: "SUMMARY"
    )
    assert removed > 0, "nothing was compacted, so the wire-valid check proves nothing"
    _assert_wire_valid(messages)
    print(f"ok  context: parallel image results never orphan a tool_call ({removed} removed)")


def cut_point_checks() -> None:
    # A cut is never taken while any tool_call is outstanding.
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "goal"},
        {"role": "user", "content": "second"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "a", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        _image_msg(),  # mid-group, unanswered call still pending
        {"role": "tool", "tool_call_id": "a", "name": "read_file", "content": "x"},
        {"role": "user", "content": "next"},
    ] + [{"role": "user", "content": f"f{i}"} for i in range(3)]

    cut = context.find_cut_point(messages, keep_recent=3)
    assert cut != 4, "accepted a cut with a tool_call still pending"

    # No safe boundary at all -> 0, and compact() declines rather than guessing.
    tiny = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "goal"},
        {"role": "assistant", "content": "hi"},
    ]
    assert context.find_cut_point(tiny, keep_recent=3) == 0
    assert context.compact(tiny, context.ContextPolicy(), summarize=lambda t: "S") == 0
    assert len(tiny) == 3, "compact() mutated a transcript it should have declined"
    print("ok  context: cut points only at settled user boundaries")


def goal_anchor_checks() -> None:
    """The original request survives. It used to be the first thing deleted."""
    goal = "build the VEX drivetrain and report the wheelbase"
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": goal},
    ]
    for i in range(12):
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": f"c{i}", "function": {"name": "read_file", "arguments": "{}"}}],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "name": "read_file", "content": "x" * 200})
        messages.append({"role": "user", "content": f"step {i}"})

    removed = context.compact(
        messages, context.ContextPolicy(keep_recent_messages=6), summarize=lambda t: "SUMMARY"
    )
    assert removed > 0, "nothing was compacted, so this proves nothing"
    assert messages[1]["content"] == goal, f"the original request was compacted away: {messages[1]}"
    assert messages[2]["content"].startswith(context.SUMMARY_PREFIX), messages[2]
    # The summary must not duplicate the goal we just pinned.
    _assert_wire_valid(messages)
    print(f"ok  context: original request pinned through compaction ({removed} removed)")


def eviction_checks() -> None:
    messages = [{"role": "system", "content": "sys"}]
    for tag in "ABCDE":
        messages.append(_image_msg(tag))

    before = len(messages)
    evicted = context.evict_images(messages, context.ContextPolicy(keep_images=2))
    assert evicted == 3, evicted
    assert len(messages) == before, "eviction deleted a message"
    live = [m for m in messages if any(context.is_live_image(p) for p in context._parts(m))]
    assert len(live) == 2, "wrong number of images survived"
    # The newest two are the ones kept.
    assert "base64,D" in str(messages[-2]) and "base64,E" in str(messages[-1])
    assert context.EVICTED_IMAGE in str(messages[1]), "evicted image lost its placeholder"
    # Idempotent: a second pass finds nothing left to evict.
    assert context.evict_images(messages, context.ContextPolicy(keep_images=2)) == 0
    print("ok  context: images evicted in place, newest kept, idempotent")


def truncation_checks() -> None:
    policy = context.ContextPolicy(keep_full_results=2, max_old_result_chars=100)
    messages = [{"role": "system", "content": "sys"}]
    for i in range(5):
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "name": "t", "content": "y" * 500})

    n = context.truncate_old_results(messages, policy)
    assert n == 3, n
    assert len(messages[-1]["content"]) == 500, "a recent result was truncated"
    assert messages[1]["content"].endswith(context.TRUNCATED)
    assert context.truncate_old_results(messages, policy) == 0, "not idempotent"
    print("ok  context: old results truncated, recent ones intact, idempotent")


def manage_checks() -> None:
    """The whole policy end to end leaves a wire-valid transcript."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "the goal"},
    ]
    for i in range(30):
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": f"a{i}", "function": {"name": "browser_screenshot", "arguments": "{}"}},
                    {"id": f"b{i}", "function": {"name": "fetch_page", "arguments": "{}"}},
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"a{i}", "name": "browser_screenshot", "content": "shot"})
        messages.append(_image_msg(f"IMG{i}"))
        messages.append({"role": "tool", "tool_call_id": f"b{i}", "name": "fetch_page", "content": "z" * 4000})
        messages.append({"role": "user", "content": f"carry on {i}"})

    policy = context.ContextPolicy(compact_at_tokens=5_000, keep_recent_messages=10)
    stats = context.manage(messages, policy, summarize=lambda t: "SUMMARY")
    assert stats.images_evicted and stats.results_truncated and stats.messages_compacted, stats
    assert stats.saved > 0, stats
    assert messages[1]["content"] == "the goal"
    _assert_wire_valid(messages)
    print(
        f"ok  context: manage() {stats.tokens_before}->{stats.tokens_after} tokens, "
        f"{stats.messages_compacted} compacted, still wire-valid"
    )


def main() -> int:
    orphan_checks()
    cut_point_checks()
    goal_anchor_checks()
    eviction_checks()
    truncation_checks()
    manage_checks()
    print("\nall context checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
