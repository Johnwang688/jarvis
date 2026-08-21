"""Attended background tasks: delegated work whose approvals reach the owner.

The sibling of workflows.py, with the premise inverted. A workflow agent gets
a deny-all approver *because* nobody is watching a background thread. A task
is started from a conversation — the owner is reachable on that surface by
definition — so the task agent runs with the approver **captured from the
starting agent's runtime** (the run_subagent precedent): the same
`permissions.gate(broker.approver())` the conversation agent holds, human-backed
flag and all. A dangerous tool raises the HUD card or Discord DM exactly as it
would in the foreground, attributed to the task that asked; a parent whose
approver is deny-all (a workflow, an unbound context) yields a deny-all task,
fail closed.

Design constraints, deliberate:

  - No browser tools: the Playwright session is one shared page in this same
    process, and a task browsing beside the conversation agent would corrupt
    both. (Goals get the browser because the daemon's runner is serial.)
  - No desktop tools: driving an app steals the Windows foreground.
  - No task/workflow/fleet tools inside a task: recursion from an unwatched
    thread is a spend amplifier; run_subagent stays because it is synchronous
    and inherits this task's approver and toolset.
  - The registry is in-memory. A restart forgets finished tasks — the lasting
    output is whatever the task wrote to files or memory.
  - Task state renders into the working-context block (`block()`), so the
    conversation agent always sees what is running and never has to invent an
    answer to "how is that task going?".
"""

from __future__ import annotations

import itertools
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import config, runtime, tools

MAX_CONCURRENT = 3
MAX_LOG_LINES = 400
BLOCK_MAX_TASKS = 10  # newest shown in the working-context block

# Tools a task agent never gets, beyond the browser_*/desktop_* prefixes:
# its own family and workflows (no recursion from an unwatched thread),
# run_fleet (six children from a background thread is a spend amplifier),
# the window/voice/avatar levers (they act on a surface the task does not
# own), and goal_report (armed only inside goal slices; a task is not one).
_EXCLUDED = {
    "task_start",
    "task_status",
    "task_log",
    "task_cancel",
    "workflow_start",
    "workflow_status",
    "workflow_log",
    "run_fleet",
    "set_voice_mute",
    "whiteboard_close",
    "set_avatar",
    "goal_report",
}


def task_tool_names() -> list[str]:
    """The task toolset, computed against the live registry per start.

    Dangerous tools are *in* — that is the feature: they gate through the
    captured approver instead of being auto-denied.
    """
    return [
        name
        for name in tools.REGISTRY
        if not name.startswith("browser_")
        and not name.startswith("desktop_")
        and name not in _EXCLUDED
    ]


TASK_SYSTEM = config.SYSTEM_PROMPT + (
    "\n\nYou are running as a background task while the conversation with the "
    "owner continues elsewhere. The owner is reachable: a dangerous tool "
    "raises an authorization card or DM attributed to this task, so use the "
    "tools the job actually needs rather than working around them — but batch "
    "approval-needing steps sensibly, and if one is denied, note it in your "
    "report instead of retrying it. Work the task to completion, save durable "
    "output to files or memory, and finish with a concise report of what you "
    "did and where the results are — it is read back with task_log."
)


@dataclass
class Task:
    id: str
    name: str
    task: str
    origin: str  # the sanitized label shown on approval surfaces
    status: str = "running"  # running | done | failed | cancelled
    started: float = field(default_factory=time.time)
    finished: float | None = None
    log: list[str] = field(default_factory=list)
    result: str = ""
    cost_usd: float = 0.0
    steps: int = 0
    cancel: threading.Event = field(default_factory=threading.Event)

    def add_log(self, line: str) -> None:
        self.log.append(f"[{time.strftime('%H:%M:%S')}] {line}")
        del self.log[:-MAX_LOG_LINES]


_registry: dict[str, Task] = {}
_lock = threading.Lock()
_ids = itertools.count(1)

# Optional surface hook (the face broadcasts SSE, the daemon logs). Never a
# reason for a task to fail — every call is exception-swallowed.
_notify: Callable[[str, dict[str, Any]], None] = lambda kind, data: None


def set_notify(fn: Callable[[str, dict[str, Any]], None] | None) -> None:
    global _notify
    _notify = fn or (lambda kind, data: None)


def _tell(data: dict[str, Any]) -> None:
    try:
        _notify("task", data)
    except Exception:
        pass


def _label(task_id: str, name: str) -> str:
    """The attribution string. The name is model-chosen text that lands
    verbatim on the authorization surfaces, so it must not be able to fake a
    second line or break the DM's formatting: collapse whitespace, strip
    backticks, cap the length."""
    clean = re.sub(r"\s+", " ", name.replace("`", "")).strip()[:60]
    return f'task {task_id} "{clean}"' if clean else f"task {task_id}"


def _deny(tool, args) -> bool:
    return False  # a task started with no approver must not run gated tools


def running_count() -> int:
    with _lock:
        return sum(1 for t in _registry.values() if t.status == "running")


def start(
    task: str,
    name: str = "",
    approve: Callable[..., bool] | None = None,
    model: str | None = None,
) -> Task:
    if running_count() >= MAX_CONCURRENT:
        raise RuntimeError(f"already {MAX_CONCURRENT} tasks running — wait or check them")

    tid = f"tk-{next(_ids)}"
    tsk = Task(id=tid, name=name or task[:40], task=task, origin=_label(tid, name))
    approve = approve or _deny  # never hand an Agent approve=None
    with _lock:
        _registry[tsk.id] = tsk

    def on_event(kind: str, data) -> None:
        if kind == "tool_start":
            tool_name, raw = data
            tsk.add_log(f"→ {tool_name}({str(raw)[:120]})")
        elif kind == "interim_text" and data:
            tsk.add_log(f"note: {str(data)[:200]}")

    def run() -> None:
        # Lazy import: agent.py imports this module for block(), so importing
        # it at module level would cycle.
        from . import agent as agent_mod

        # This thread starts with an empty context; bind the label (and the
        # approver, for anything on this thread reading runtime outside a
        # turn) before the first run_turn. run_turn's own bind passes no
        # origin, so this survives every step and reaches the broker on the
        # thread request() blocks — and copy_context carries it into any
        # sub-agent the task spawns.
        runtime.bind(approve=approve, origin=tsk.origin)
        tsk.add_log(f"started: {tsk.task[:200]}")
        _tell({"id": tsk.id, "name": tsk.name, "status": "running"})
        try:
            agent = agent_mod.Agent(
                model=model,
                system=TASK_SYSTEM,
                tool_names=task_tool_names(),
                max_steps=config.MAX_STEPS,
                approve=approve,
                should_stop=tsk.cancel.is_set,
                on_event=on_event,
            )
            turn = agent.run_turn(tsk.task)
            tsk.result = turn.text
            tsk.cost_usd = turn.cost_usd
            tsk.steps = turn.steps
            tsk.status = "cancelled" if turn.cancelled else "done"
            tsk.add_log(f"{tsk.status}: {turn.steps} step(s), ${turn.cost_usd:.4f}")
        except Exception as exc:
            tsk.status = "failed"
            tsk.result = f"{type(exc).__name__}: {exc}"
            tsk.add_log(f"failed: {tsk.result}")
        finally:
            tsk.finished = time.time()
            _tell(
                {
                    "id": tsk.id,
                    "name": tsk.name,
                    "status": tsk.status,
                    "steps": tsk.steps,
                    "cost_usd": round(tsk.cost_usd, 4),
                }
            )

    threading.Thread(target=run, daemon=True, name=tsk.id).start()
    return tsk


def get(task_id: str) -> Task | None:
    with _lock:
        return _registry.get(task_id)


def listing() -> str:
    with _lock:
        items = list(_registry.values())
    if not items:
        return "No background tasks this session."
    lines = []
    for tsk in items:
        elapsed = (tsk.finished or time.time()) - tsk.started
        lines.append(
            f"- {tsk.id} [{tsk.status}] {tsk.name!r} · {elapsed:.0f}s"
            + (
                f" · {tsk.steps} step(s) · ${tsk.cost_usd:.4f}"
                if tsk.status != "running"
                else ""
            )
        )
    return "\n".join(lines)


def block() -> str:
    """The working-context section. "" when there is nothing to say."""
    with _lock:
        items = list(_registry.values())[-BLOCK_MAX_TASKS:]
    if not items:
        return ""
    lines = ["## Background tasks"]
    for tsk in items:
        elapsed = (tsk.finished or time.time()) - tsk.started
        lines.append(
            f"- {tsk.id} [{tsk.status}] {tsk.name!r} · {elapsed:.0f}s"
            + (
                f" · {tsk.steps} step(s) · ${tsk.cost_usd:.4f}"
                if tsk.status != "running"
                else ""
            )
        )
    lines.append(
        "When one finishes, read its report with task_log before telling the "
        "owner the results."
    )
    return "\n".join(lines)
