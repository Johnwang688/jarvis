"""Tools for delegating work to attended background tasks.

Lazy imports: jarvis.tasks constructs Agents, and importing it at module load
would cycle back into the tools package mid-initialization (the workflows
pattern).

task_start captures the *calling agent's* approver from runtime — the
run_subagent precedent — so a task started from a conversation surface can
raise authorization cards/DMs there, while one started from any deny-all
context (a workflow, an unbound thread) stays deny-all, fail closed.
"""

from __future__ import annotations

from typing import Annotated

from .. import runtime
from . import tool


@tool
def task_start(
    task: Annotated[str, "Complete instructions: the job, absolute paths, and where to put results"],
    name: Annotated[str, "Short label for status listings and approval cards"] = "",
) -> str:
    """Run a job in the background on a full-capability agent while this
    conversation continues.

    Use it for self-contained work with a clear deliverable — a refactor, a
    report, a batch of file changes, sending mail the owner asked for — so the
    conversation stays free. Unlike workflow_start, the task agent CAN use
    tools that need the owner's approval: those raise an authorization card or
    DM naming the task, so do not avoid delegating work just because part of
    it needs a yes. It has no browser and no desktop tools. Give it complete
    instructions; it cannot see this conversation. Check on it with
    task_status / task_log; stop it with task_cancel.
    """
    from .. import tasks

    approve = runtime.approver()  # the calling agent's own gate; denies if unbound
    try:
        tsk = tasks.start(task, name=name, approve=approve)
    except RuntimeError as exc:
        return f"Error: {exc}"
    return (
        f"Started {tsk.id} ({tsk.name!r}) in the background. "
        f"Check it with task_status or task_log."
    )


@tool
def task_status() -> str:
    """List this session's background tasks with status, runtime, and cost."""
    from .. import tasks

    return tasks.listing()


@tool
def task_log(
    task_id: Annotated[str, "Task id from task_status, e.g. tk-1"],
    tail_lines: Annotated[int, "How many recent log lines"] = 30,
) -> str:
    """Read a background task's progress log and, once finished, its final report."""
    from .. import tasks

    tsk = tasks.get(task_id)
    if tsk is None:
        return f"Error: no task {task_id!r}. Check task_status."
    parts = [f"{tsk.id} [{tsk.status}] {tsk.name!r}", *tsk.log[-max(1, int(tail_lines)):]]
    if tsk.status != "running" and tsk.result:
        parts.append(f"\nFinal report:\n{tsk.result}")
    return "\n".join(parts)


@tool
def task_cancel(
    task_id: Annotated[str, "Task id from task_status, e.g. tk-1"],
) -> str:
    """Stop a running background task at its next step boundary.

    An authorization card the task already raised stays up until the owner
    answers it or it times out — cancelling the task does not answer questions
    for the owner.
    """
    from .. import tasks

    tsk = tasks.get(task_id)
    if tsk is None:
        return f"Error: no task {task_id!r}. Check task_status."
    if tsk.status != "running":
        return f"{tsk.id} is already {tsk.status}."
    tsk.cancel.set()
    return (
        f"Cancelling {tsk.id} — it stops at the next step boundary. "
        f"Check task_status for the final state."
    )
