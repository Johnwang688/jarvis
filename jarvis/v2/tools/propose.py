"""`task_propose` — the one tool the fast path gains, and its whole escape.

Design §8.1: the fast path has no tool that can change anything, so a turn that
misjudges a request as chat can only *answer badly* — it cannot half-do work.
That boundary is only tolerable because there is a door: when a request needs a
file edit, a command, a browser, more than one page, or a deliverable, the model
says so in the transcript by calling this, and the Router opens a task on a CLI
provider.

It is an act *in* the transcript rather than prose the provider greps for. That
is the v1 `goal_report` lesson (and the step-budget one behind it): state the
harness reads out of free text is state the model will confabulate, and state
the harness produces without telling the model is state the model invents a
story about. A tool call is neither.

The slot is a ContextVar (`runtime.proposal_slot`), not a module global like
`goalctl`'s: goal runs are one serial worker, chat is not — the HUD, Discord and
the daemon can each hold a fast-path handle at once, and a module global would
let one conversation's proposal be read back by another.
"""

from __future__ import annotations

from typing import Annotated

from ... import runtime
from ...tools import tool

# Long enough for a real brief with acceptance criteria, short enough that the
# model writes a task rather than pasting the conversation into one.
MAX_BRIEF_CHARS = 4000


@tool
def task_propose(
    brief: Annotated[
        str,
        "What the task should achieve, in the owner's terms: the goal, the "
        "deliverable, and how you would know it is done. Write it for someone "
        "who has not read this conversation.",
    ],
    project: Annotated[
        str,
        "Which project it belongs to, if the owner named one. Leave empty to "
        "let the router place it.",
    ] = "",
    provider: Annotated[
        str,
        "'claude' or 'codex' if the owner asked for one by name. Leave empty "
        "otherwise — the router picks.",
    ] = "",
) -> str:
    """Hand this request to a real task instead of answering it here.

    Call this when the request needs any one of: editing a file, running a
    command, a browser, more than one page fetched, or producing a deliverable
    (a file, a PR, a report) — and also when it simply will not fit in this
    conversation's short step budget. Any one of those is enough; you do not
    need all of them.

    Do not call it for a question you can answer from memory, the transcript,
    one read or one fetch, for a control request (mute, avatar, music), or for
    ordinary conversation.

    Calling it twice in one turn replaces the earlier proposal, so correcting
    yourself is free. After calling it, tell the owner in one line what you
    opened and that it will ask if anything is unclear.
    """
    slot = runtime.proposal_slot()
    if slot is None:
        return (
            "Error: nothing here can open a task — this conversation is not "
            "running on the fast path. Answer directly, or tell the owner what "
            "needs doing."
        )
    text = brief.strip()
    if not text:
        return "Error: a proposal needs a brief saying what the task should achieve."
    if len(text) > MAX_BRIEF_CHARS:
        text = text[:MAX_BRIEF_CHARS] + "\n[...brief truncated]"

    # Replace rather than append: one turn proposes at most one task, so a
    # second call is a correction. Written into the caller's own dict in place
    # so the provider's reference stays live.
    slot.clear()
    slot.update(
        {
            "brief": text,
            "project": project.strip() or None,
            "provider": provider.strip().lower() or None,
        }
    )
    where = f" in {slot['project']}" if slot["project"] else ""
    who = f" on {slot['provider']}" if slot["provider"] else ""
    return (
        f"Task proposed{where}{who}. Tell the owner in one line what it will do; "
        "it opens after this reply and will ask if anything is unclear."
    )
