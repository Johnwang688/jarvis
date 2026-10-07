"""Domain model — design §4. Fixed interface for WP1 (stores) and everything above.

Rules that hold for every type here:
- Plain dataclasses, JSON-serializable through `to_json()` / `from_json()`;
  no behaviour beyond validation. Stores live in stores.py (WP1).
- Ids are opaque strings minted by the store; never derived from titles.
- Timestamps are ISO-8601 UTC strings (`utcnow()`), never floats.
- Every enum is a str subclass so it round-trips through JSON unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProviderName(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"
    FAST = "fast"


class Role(str, Enum):
    ORCHESTRATOR = "orchestrator"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    RESEARCHER = "researcher"
    CHAT = "chat"            # fast-path conversation threads


class PermissionProfile(str, Enum):
    AUTO = "auto"            # design §6.2
    ASK = "ask"
    STRICT = "strict"


class TaskState(str, Enum):
    INTAKE = "intake"
    CLARIFYING = "clarifying"
    PLANNED = "planned"
    RUNNING = "running"
    VERIFYING = "verifying"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset({TaskState.DONE, TaskState.FAILED, TaskState.CANCELLED})

# Legal transitions, design §10.1. A store MUST refuse anything else.
TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.INTAKE:     frozenset({TaskState.CLARIFYING, TaskState.CANCELLED}),
    TaskState.CLARIFYING: frozenset({TaskState.PLANNED, TaskState.BLOCKED, TaskState.CANCELLED}),
    TaskState.PLANNED:    frozenset({TaskState.RUNNING, TaskState.BLOCKED, TaskState.CANCELLED}),
    TaskState.RUNNING:    frozenset({TaskState.VERIFYING, TaskState.BLOCKED, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.VERIFYING:  frozenset({TaskState.RUNNING, TaskState.DONE, TaskState.BLOCKED, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.BLOCKED:    frozenset({TaskState.CLARIFYING, TaskState.PLANNED, TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.DONE:       frozenset(),
    TaskState.FAILED:     frozenset(),
    TaskState.CANCELLED:  frozenset(),
}


@dataclass
class Routing:
    """role -> provider chain, design §8.2. Empty means 'use the next layer up'."""
    chains: dict[str, list[str]] = field(default_factory=dict)      # Role -> [ProviderName...]
    models: dict[str, dict[str, str]] = field(default_factory=dict)  # Role -> {provider: "model/effort"}
    no_new_work: float | None = None                                  # fraction of window, §8.3


@dataclass
class Project:
    id: str
    name: str
    root: str                                   # absolute path; may be non-git
    created: str = field(default_factory=utcnow)
    profile: PermissionProfile = PermissionProfile.AUTO
    routing: Routing = field(default_factory=Routing)
    discord_channel_id: str | None = None
    extra_dirs: list[str] = field(default_factory=list)
    always_ask: list[str] = field(default_factory=list)   # additions only, §6 layer 2
    inbox: bool = False                                    # the one project for unplaced chat


@dataclass
class Thread:
    """A conversation on one provider. design §4."""
    id: str
    project_id: str
    role: Role
    provider: ProviderName
    provider_session_id: str | None = None      # Claude session id, "codex:<id>", or None for fast
    task_id: str | None = None
    title: str = ""
    created: str = field(default_factory=utcnow)
    updated: str = field(default_factory=utcnow)
    turns: int = 0
    cost_usd: float = 0.0
    tokens: int = 0                             # work_tokens, §8.3
    model: str | None = None
    effort: str | None = None
    migrated_from: str | None = None            # v1 session id, if any
    # The folder the thread was opened in. Fixed for its life: a move between
    # projects re-labels the thread, it never re-roots it (design §18).
    cwd: str | None = None


@dataclass
class OpenQuestion:
    text: str
    blocking: bool
    options: list[str] = field(default_factory=list)
    answer: str | None = None
    assumed: str | None = None                  # what was assumed when not blocking


@dataclass
class Spec:
    goal: str = ""
    deliverable: str = ""
    acceptance: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    questions: list[OpenQuestion] = field(default_factory=list)

    def blocked_on(self) -> list[OpenQuestion]:
        return [q for q in self.questions if q.blocking and q.answer is None]


@dataclass
class RoutingDecision:
    role: Role
    provider: ProviderName
    reason: str                                 # "project table", "claude over threshold", ...
    at: str = field(default_factory=utcnow)


@dataclass
class Status:
    """The runner-maintained status record, design §10.3. Never model-written."""
    phase: TaskState = TaskState.INTAKE
    step: int = 0
    steps: int = 0
    started: str | None = None
    elapsed_s: float = 0.0
    cost_usd: float = 0.0
    tokens: int = 0
    last_tool: str | None = None
    last_file: str | None = None
    open_question: str | None = None
    routing: list[RoutingDecision] = field(default_factory=list)


@dataclass
class Report:
    """design §10.4. Rendered per surface by the Reporter; capped there, not here."""
    done: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    verified: str = ""
    open: list[str] = field(default_factory=list)
    next: str = ""
    cost: dict[str, float] = field(default_factory=dict)   # provider -> usd (or tokens if no $)


@dataclass
class Task:
    id: str
    project_id: str
    brief: str
    state: TaskState = TaskState.INTAKE
    created: str = field(default_factory=utcnow)
    updated: str = field(default_factory=utcnow)
    spec: Spec = field(default_factory=Spec)
    plan: list[str] = field(default_factory=list)
    status: Status = field(default_factory=Status)
    report: Report | None = None
    thread_ids: list[str] = field(default_factory=list)      # orchestrator first
    worktree: str | None = None
    branch: str | None = None
    discord_thread_id: str | None = None
    profile: PermissionProfile | None = None                # None -> project's
    provider_override: ProviderName | None = None           # "use codex", §8.2 step 1
    ceilings: dict[str, float] = field(default_factory=dict)  # usd / hours / tokens
    migrated_from: str | None = None                        # v1 goal id, if any


# --- serialization ----------------------------------------------------------

def to_json(obj: Any) -> dict:
    """asdict with enums flattened to their values."""
    def _flat(v):
        if isinstance(v, Enum):
            return v.value
        if isinstance(v, dict):
            return {k: _flat(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_flat(x) for x in v]
        return v
    return _flat(asdict(obj))


_ENUM_FIELDS = {
    "state": TaskState, "phase": TaskState, "role": Role, "provider": ProviderName,
    "profile": PermissionProfile, "provider_override": ProviderName,
}
_NESTED = {
    "routing": Routing, "spec": Spec, "status": Status, "report": Report,
    "questions": OpenQuestion,
}


def from_json(cls, data: dict):
    """Inverse of to_json for every dataclass above (one level of nesting per field)."""
    kw = {}
    for k, v in data.items():
        if v is None:
            kw[k] = None
        elif k in _ENUM_FIELDS:
            kw[k] = _ENUM_FIELDS[k](v)
        elif k == "routing" and cls is Task or (k == "routing" and cls is Status):
            kw[k] = [from_json(RoutingDecision, x) for x in v]
        elif k in _NESTED and isinstance(v, dict):
            kw[k] = from_json(_NESTED[k], v)
        elif k == "questions":
            kw[k] = [from_json(OpenQuestion, x) for x in v]
        else:
            kw[k] = v
    return cls(**kw)
