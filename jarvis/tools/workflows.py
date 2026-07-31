"""Tools for starting and monitoring background workflows.

Lazy imports: jarvis.workflows constructs Agents, and importing it at module
load would cycle back into the tools package mid-initialization.
"""

from __future__ import annotations

from typing import Annotated

from . import tool


@tool
def workflow_start(
    task: Annotated[str, "Complete instructions for the background agent"],
    name: Annotated[str, "Short label for status listings"] = "",
) -> str:
    """Run a task in the background while the conversation continues.

    The workflow agent has file, web, memory, and read-only shell tools; it
    cannot use the browser and cannot run anything needing owner approval
    (those are auto-denied). Give it complete instructions, including where
    to put the results. Check on it with workflow_status / workflow_log.
    """
    from .. import workflows

    try:
        wf = workflows.start(task, name=name)
    except RuntimeError as exc:
        return f"Error: {exc}"
    return f"Started {wf.id} ({wf.name!r}). Check it with workflow_status or workflow_log."


@tool
def workflow_status() -> str:
    """List this session's workflows with status, runtime, and cost."""
    from .. import workflows

    return workflows.listing()


@tool
def workflow_log(
    workflow_id: Annotated[str, "Workflow id from workflow_status, e.g. wf-1"],
    tail_lines: Annotated[int, "How many recent log lines"] = 30,
) -> str:
    """Read a workflow's progress log and, once finished, its final report."""
    from .. import workflows

    wf = workflows.get(workflow_id)
    if wf is None:
        return f"Error: no workflow {workflow_id!r}. Check workflow_status."
    parts = [f"{wf.id} [{wf.status}] {wf.name!r}", *wf.log[-max(1, int(tail_lines)):]]
    if wf.status != "running" and wf.result:
        parts.append(f"\nFinal report:\n{wf.result}")
    return "\n".join(parts)
