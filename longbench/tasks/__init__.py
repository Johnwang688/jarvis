"""The long-bench task registry."""

from __future__ import annotations

from ..spec import Task
from . import audit, sweep, thread

TASKS: list[Task] = [
    Task(
        name="sweep",
        tests="a migration too wide for one window, then the spec changes",
        plan=sweep.plan,
        materialize=sweep.materialize,
        prompt=sweep.prompt,
        grade=sweep.grade,
        max_turns=200,
        followups=sweep.followups,
    ),
    Task(
        name="audit",
        tests="the whole estate read, and a findings list that only grows",
        plan=audit.plan,
        materialize=audit.materialize,
        prompt=audit.prompt,
        grade=audit.grade,
        max_turns=200,
    ),
    Task(
        name="thread",
        tests="a forty-step chain with a rule, a salt and two broken files in it",
        plan=thread.plan,
        materialize=thread.materialize,
        prompt=thread.prompt,
        grade=thread.grade,
        max_turns=60,
    ),
]

BY_NAME = {task.name: task for task in TASKS}
