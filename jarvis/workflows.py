"""Background workflows: a task handed to its own agent on its own thread.

The owner (or Jarvis mid-conversation) starts one, keeps talking, and checks
in later via the workflow tools. Design constraints, deliberate:

  - A workflow agent gets SAFE_TOOLS only and a deny-all approver: nobody is
    watching a background thread, so nothing that would need a human answer
    can run there. Dangerous steps come back as "auto-denied — run this in
    the foreground", and the workflow reports that instead of stalling.
  - No browser tools: the Playwright session is one shared page; two agents
    interleaving on it would corrupt both.
  - No desktop tools, for a sharper version of the same reason: driving an app
    means taking the Windows foreground, so a background workflow would fight
    the owner for their own screen and type into whatever they had focused.
  - The registry is in-memory. A restart forgets finished workflows — the
    lasting output is whatever the workflow wrote to files or memory.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field

from . import agent as agent_mod
from . import config

MAX_CONCURRENT = 3
MAX_LOG_LINES = 400

SAFE_TOOLS = [
    "read_file",
    "write_file",
    "list_dir",
    "find_files",
    "get_datetime",
    "web_search",
    "fetch_page",
    "memory_list",
    "memory_read",
    "memory_search",
    "memory_write",
    "run_readonly",
    "skill_list",
    "skill_read",
    # Read-only views of past conversations: a background task that needs to
    # know what was already decided can look it up instead of asking.
    "session_list",
    "session_summary",
    "session_search",
    "session_read",
    # Safe despite being outward-facing: the recipient is pinned to the
    # owner, so a workflow can ping their phone when it finishes.
    "discord_dm_owner",
    # A workflow is the long-horizon surface by definition — nobody is watching,
    # so losing the thread is silent. The plan keeps it honest; sub-agents keep
    # its context from filling with material it only needs the conclusion of.
    # Both are safe here: run_subagent inherits this agent's deny-all approver,
    # and its own toolset excludes everything dangerous.
    "plan_write",
    "run_subagent",
]

WORKFLOW_SYSTEM = config.SYSTEM_PROMPT + (
    "\n\nYou are running as a background workflow: no human is watching, and "
    "you cannot ask for approval — dangerous tools are auto-denied, so plan "
    "around them and note anything that needs the owner. Work the task to "
    "completion, save durable output to files or memory, and finish with a "
    "concise report of what you did and where the results are."
)


@dataclass
class Workflow:
    id: str
    name: str
    task: str
    status: str = "running"  # running | done | failed
    started: float = field(default_factory=time.time)
    finished: float | None = None
    log: list[str] = field(default_factory=list)
    result: str = ""
    cost_usd: float = 0.0
    steps: int = 0

    def add_log(self, line: str) -> None:
        self.log.append(f"[{time.strftime('%H:%M:%S')}] {line}")
        del self.log[:-MAX_LOG_LINES]


_registry: dict[str, Workflow] = {}
_lock = threading.Lock()
_ids = itertools.count(1)


def _deny(tool, args) -> bool:
    return False  # background: nobody to ask — dispatch returns the refusal


def running_count() -> int:
    with _lock:
        return sum(1 for w in _registry.values() if w.status == "running")


def start(task: str, name: str = "", model: str | None = None) -> Workflow:
    if running_count() >= MAX_CONCURRENT:
        raise RuntimeError(f"already {MAX_CONCURRENT} workflows running — wait or check them")

    wf = Workflow(id=f"wf-{next(_ids)}", name=name or task[:40], task=task)
    with _lock:
        _registry[wf.id] = wf

    def on_event(kind: str, data) -> None:
        if kind == "tool_start":
            tool_name, raw = data
            wf.add_log(f"→ {tool_name}({str(raw)[:120]})")
        elif kind == "interim_text" and data:
            wf.add_log(f"note: {str(data)[:200]}")

    def run() -> None:
        wf.add_log(f"started: {wf.task[:200]}")
        try:
            agent = agent_mod.Agent(
                model=model,
                system=WORKFLOW_SYSTEM,
                tool_names=SAFE_TOOLS,
                max_steps=20,
                approve=_deny,
                on_event=on_event,
            )
            turn = agent.run_turn(wf.task)
            wf.result = turn.text
            wf.cost_usd = turn.cost_usd
            wf.steps = turn.steps
            wf.status = "done"
            wf.add_log(f"done: {turn.steps} step(s), ${turn.cost_usd:.4f}")
        except Exception as exc:
            wf.status = "failed"
            wf.result = f"{type(exc).__name__}: {exc}"
            wf.add_log(f"failed: {wf.result}")
        finally:
            wf.finished = time.time()

    threading.Thread(target=run, daemon=True, name=wf.id).start()
    return wf


def get(workflow_id: str) -> Workflow | None:
    with _lock:
        return _registry.get(workflow_id)


def listing() -> str:
    with _lock:
        items = list(_registry.values())
    if not items:
        return "No workflows this session."
    lines = []
    for wf in items:
        elapsed = (wf.finished or time.time()) - wf.started
        lines.append(
            f"- {wf.id} [{wf.status}] {wf.name!r} · {elapsed:.0f}s"
            + (f" · {wf.steps} step(s) · ${wf.cost_usd:.4f}" if wf.status != "running" else "")
        )
    return "\n".join(lines)
