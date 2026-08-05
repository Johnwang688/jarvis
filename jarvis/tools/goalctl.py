"""Goal run control: how a background goal declares itself finished.

The goal runner cannot grep the model's prose for "I'm done" — state that
lives outside the transcript is state the model will confabulate about, and
state sniffed *from* prose is worse. So finishing a goal is a tool call: it
lands in the transcript like every other action, and the runner consumes the
report at the slice boundary.

The slot is module-level and armed only while the runner is inside a slice
(`arm()`/`disarm()`); the runner is a single serial worker, so there is no
concurrent-goal ambiguity. Any other agent calling goal_report gets a clear
refusal — the tool is registered globally (voicectl's pattern), and outside
a goal run it has nothing to end.
"""

from __future__ import annotations

from typing import Annotated

from . import tool

_armed = False
_report: dict | None = None


def arm() -> None:
    global _armed, _report
    _armed, _report = True, None


def disarm() -> None:
    global _armed
    _armed = False


def consume() -> dict | None:
    """The report from the slice that just ran, cleared on read."""
    global _report
    report, _report = _report, None
    return report


@tool
def goal_report(
    status: Annotated[str, "'done' when the goal is complete, 'blocked' when you cannot proceed without the owner"],
    summary: Annotated[str, "What was accomplished (done) or exactly what is needed to continue (blocked)"],
) -> str:
    """End the current background goal run.

    Call with status 'done' when the goal is genuinely complete — never claim
    completion in prose without calling this. Call with status 'blocked' when
    you are stuck and only the owner can unblock you; the summary is what
    they will read on their phone, so say precisely what you need.
    """
    global _report
    if not _armed:
        return (
            "Error: no background goal is running. This tool ends goal runs; "
            "in a conversation, just answer normally."
        )
    status = status.strip().lower()
    if status not in {"done", "blocked"}:
        return "Error: status must be 'done' or 'blocked'."
    _report = {"status": status, "summary": summary.strip()}
    return f"Goal marked {status}. The owner will be notified."
