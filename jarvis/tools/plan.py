"""The working plan — a checklist that survives everything else.

On a long task the only thing holding the work together used to be the
transcript, and the transcript is exactly what context management eats: old
tool results get truncated, old stretches get replaced by a cheap-tier summary.
So the further a run went, the less the model could see of what it had set out
to do. It would finish step 40 having quietly dropped the third of the four
things it was asked for.

The fix is to keep the plan somewhere pruning cannot reach. `messages[0]` is
that place — the system message is rebuilt from source every step and neither
pruning nor compaction touches index 0 — and the skills index already proved
the pattern works.

The plan is *rewritten whole* rather than patched item by item. One tool, one
argument, no ids to keep in sync, and re-stating the whole checklist each time
is itself the act of re-reading it.
"""

from __future__ import annotations

from typing import Annotated

from .. import runtime
from . import tool

MAX_PLAN_CHARS = 2000


def block() -> str:
    """The plan, rendered for the system message. Empty when nothing is set."""
    slot = runtime.plan_slot()
    text = (slot or {}).get("text", "").strip()
    if not text:
        return ""
    return (
        "## Working plan\n"
        "This is your own plan for the task in progress, and it is the one part "
        "of this conversation that cannot be lost to context pruning. Keep it "
        "current with plan_write as you finish steps or learn the plan was "
        "wrong.\n\n" + text
    )


@tool
def plan_write(
    plan: Annotated[
        str,
        "The full plan as a markdown checklist, e.g. '- [x] read the spec\\n- [ ] write the tests'",
    ],
) -> str:
    """Record or update your plan for a multi-step task.

    Use this when a task needs more than a few steps: write the checklist
    before starting, then rewrite it as steps finish or the plan changes. It
    stays visible to you for the whole task even after older messages are
    pruned or summarized away, so it is the reliable place to keep track of
    what is done and what is left.

    Pass the complete checklist every time — it replaces the previous one.
    """
    slot = runtime.plan_slot()
    if slot is None:
        return (
            "Error: no plan slot bound for this run. plan_write only works "
            "inside an agent turn."
        )
    text = plan.strip()
    if len(text) > MAX_PLAN_CHARS:
        text = text[:MAX_PLAN_CHARS] + "\n[...plan truncated]"
    slot["text"] = text
    if not text:
        return "Plan cleared."
    done = sum(1 for line in text.splitlines() if line.strip().startswith(("- [x]", "- [X]")))
    left = sum(1 for line in text.splitlines() if line.strip().startswith("- [ ]"))
    return f"Plan updated: {done} done, {left} remaining."
