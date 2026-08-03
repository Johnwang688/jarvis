"""Per-run state that tools need but `dispatch()` cannot pass them.

A tool is a plain function called as `func(**arguments)` — deliberately, so the
schema generator stays honest and a tool is testable on its own. That leaves no
channel for things that belong to *the agent currently running*: its working
plan, its approver, its cancel check, how deep it is in a sub-agent chain.

These live in `ContextVar`s, bound by `Agent.run_turn` at the top of every turn.
That works because dispatch is synchronous and runs on the same thread as the
`run_turn` that bound it: a fresh thread (a workflow, one of the face's request
threads) starts with an empty context and binds its own, so two agents running
at once never see each other's state.

Everything here fails **closed**. An unbound approver denies rather than
approves — a sub-agent that somehow ran without inheriting a human gate must not
be a way to run dangerous tools unguarded (the same rule that makes
`dispatch(approve=None)` the face's one forbidden mistake).
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Callable

# The working plan, as a one-key mutable dict so a tool can rewrite it in place
# and the agent that owns it sees the change on its next step.
_PLAN: ContextVar[dict[str, str] | None] = ContextVar("jarvis_plan", default=None)

# The running agent's approver, so a sub-agent inherits the same human gate.
_APPROVE: ContextVar[Callable[..., bool] | None] = ContextVar("jarvis_approve", default=None)

# The running agent's cancel check, so cancelling a turn also stops sub-agents.
_SHOULD_STOP: ContextVar[Callable[[], bool] | None] = ContextVar(
    "jarvis_should_stop", default=None
)

# How many sub-agents deep we are. The backstop against a spawn loop.
_DEPTH: ContextVar[int] = ContextVar("jarvis_depth", default=0)

# The running agent's own tool names. A sub-agent's toolset is intersected with
# this, so a child can never reach a tool its parent was not given — which is
# what keeps a background workflow (no browser tools, because they would
# interleave on the one shared Playwright page) from getting to the browser by
# spawning a child that has them.
_TOOLS: ContextVar[frozenset[str] | None] = ContextVar("jarvis_tools", default=None)

MAX_DEPTH = 2


def bind(
    plan: dict[str, str] | None = None,
    approve: Callable[..., bool] | None = None,
    should_stop: Callable[[], bool] | None = None,
    depth: int | None = None,
    tool_names: frozenset[str] | set[str] | None = None,
) -> None:
    """Bind the current agent's per-run state. Called by `Agent.run_turn`."""
    if plan is not None:
        _PLAN.set(plan)
    if approve is not None:
        _APPROVE.set(approve)
    if should_stop is not None:
        _SHOULD_STOP.set(should_stop)
    if depth is not None:
        _DEPTH.set(depth)
    if tool_names is not None:
        _TOOLS.set(frozenset(tool_names))


def plan_slot() -> dict[str, str] | None:
    return _PLAN.get()


def approver() -> Callable[..., bool]:
    """The inherited approver, or a denier if nothing is bound (fail closed)."""
    found = _APPROVE.get()
    return found if found is not None else (lambda *a, **k: False)


def should_stop() -> Callable[[], bool]:
    found = _SHOULD_STOP.get()
    return found if found is not None else (lambda: False)


def depth() -> int:
    return _DEPTH.get()


def parent_tools() -> frozenset[str] | None:
    """The running agent's toolset, or None if nothing is bound."""
    return _TOOLS.get()


def describe() -> dict[str, Any]:
    """For tests and debugging — what is bound right now."""
    return {
        "plan": (_PLAN.get() or {}).get("text", ""),
        "approve_bound": _APPROVE.get() is not None,
        "depth": _DEPTH.get(),
    }
