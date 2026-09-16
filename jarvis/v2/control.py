"""TaskControl — the verbs a surface may apply to a task (design §8.1 row 1,
§10.1, §11.2). Fixed interface: WP11's runner implements it, WP10b's Discord
routing and the HUD call it, tests fake it.

Every method is synchronous and returns quickly: a steer is queued for the
task's next step boundary, a cancel lands at the next boundary (invariant 3 —
a turn is never cut mid-tool), a resume requeues. None of them blocks on a
model. Failures raise `ControlError` with a sentence the surface can show.
"""
from __future__ import annotations

from typing import Protocol

from .model import ProviderName, Task


class ControlError(Exception):
    """A verb that cannot be applied: unknown task, wrong state, bad provider."""


class TaskControl(Protocol):
    def start(self, task_id: str) -> Task:
        """INTAKE → CLARIFYING: open the orchestrator and begin the spec."""

    def steer(self, task_id: str, text: str, *, spoken: bool = False) -> None:
        """Queue owner text for the orchestrator's next step. Spoken text is
        conversation and may steer; it never answers an approval (v1 rule)."""

    def answer_question(self, task_id: str, index: int, text: str) -> Task:
        """Answer an open clarifying question by index; the task proceeds when
        no blocking question remains."""

    def cancel(self, task_id: str) -> Task:
        """Cancel at the next step boundary; a proposal still in its grace
        window is withdrawn before any CLI session starts."""

    def resume(self, task_id: str, *, provider: ProviderName | None = None) -> Task:
        """Requeue a BLOCKED/FAILED task; with `provider`, restart the
        orchestrator there from durable state (design §8.4)."""

    def status(self, task_id: str) -> Task:
        """The task as the runner sees it, status record current."""

    def list_tasks(self, project_id: str | None = None, *, active_only: bool = True) -> list[Task]: ...
