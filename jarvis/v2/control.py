"""TaskControl — the verbs a surface may apply to a task (design §8.1 row 1,
§10.1, §11.2). Fixed interface: WP11's runner implements it, WP10b's Discord
routing and the HUD call it, tests fake it.

Every method is synchronous and returns quickly: a steer is queued for the
task's next step boundary, a cancel lands at the next boundary (invariant 3 —
a turn is never cut mid-tool), a resume requeues. None of them blocks on a
model. Failures raise `ControlError` with a sentence the surface can show.
"""
from __future__ import annotations

from typing import Any, Protocol, Sequence

from .model import ProviderName, Task, TaskState


class ControlError(Exception):
    """A verb that cannot be applied: unknown task, wrong state, bad provider."""


def answer_refusal(task_id: str, state: TaskState | str, questions: Sequence[Any],
                   index: Any, text: Any) -> str | None:
    """Why `answer_question` refuses this answer, or None if it takes it.

    The runner's rule, kept in one place because the HUD's test mock calls it
    too (a mock that took any body is how the HUD sent `{question, answer}`
    for weeks, 2026-10-11): only a CLARIFYING task takes an answer; `index` is
    the question's place in `spec.questions` — every question, answered and
    assumable ones included, never its place among the open ones; and the
    answer is not blank."""
    state = TaskState(state)
    if state != TaskState.CLARIFYING:
        return f"Task {task_id} is {state.value}; it is not waiting on a question."
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(questions):
        return f"Task {task_id} has no question {index}."
    if not isinstance(text, str) or not text.strip():
        return "An answer needs some text."
    return None


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
        """Requeue a BLOCKED task; with `provider`, restart the orchestrator
        there from durable state (design §8.4). FAILED is terminal in
        `model.TRANSITIONS` and is refused with a sentence."""

    def status(self, task_id: str) -> Task:
        """The task as the runner sees it, status record current."""

    def list_tasks(self, project_id: str | None = None, *, active_only: bool = True) -> list[Task]: ...
