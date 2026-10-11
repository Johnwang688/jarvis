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

# The running agent's own tool names — its *current* toolset, groups expanded
# by `load_tools` included (`Agent._sync_tools` re-binds it). Two readers, one
# rule: `dispatch()` refuses any tool not in it (tools._toolset_refusal — the
# toolset is a boundary, not only a list of schemas sent), and a sub-agent's
# toolset is intersected with it, so a child can never reach a tool its parent
# was not given — which is what keeps a background workflow (no browser tools,
# because they would interleave on the one shared Playwright page) from getting
# to the browser by spawning a child that has them, or by naming one.
_TOOLS: ContextVar[frozenset[str] | None] = ContextVar("jarvis_tools", default=None)

# Who is asking, shown on the approval surfaces (HUD card, Discord DM). Bound
# once by the surface that owns the thread — a background task's runner binds
# its label before run_turn — and inherited by children through copy_context,
# so a sub-agent spawned by a task attributes its asks to the task. "" (the
# default, and the conversation agent's value) renders nothing anywhere.
_ORIGIN: ContextVar[str] = ContextVar("jarvis_origin", default="")

# Tool groups this agent has pulled in, as a mutable set so `load_tools` can
# add to it in place and the agent that owns it rebuilds its tool specs on the
# next step — the working plan's mechanism, for the same reason.
_LOADED: ContextVar[set[str] | None] = ContextVar("jarvis_loaded_groups", default=None)

# Where `task_propose` (jarvis/v2/tools/propose.py) puts the task the fast path
# wants opened — a one-key mutable dict, the working plan's mechanism for the
# working plan's reason. The v2 FastPathProvider binds a slot it owns before
# `run_turn` and reads it back after, so the proposal rides the turn's own
# thread and two concurrent chat handles cannot see each other's.
#
# It is deliberately *not* set by `Agent.run_turn`: the v1 loop knows nothing
# about proposals, and `bind()` only writes what it is passed, so a slot bound
# by the provider survives the loop's own bind on the same thread.
_PROPOSAL: ContextVar[dict | None] = ContextVar("jarvis_proposal", default=None)

MAX_DEPTH = 2


def bind(
    plan: dict[str, str] | None = None,
    approve: Callable[..., bool] | None = None,
    should_stop: Callable[[], bool] | None = None,
    depth: int | None = None,
    tool_names: frozenset[str] | set[str] | None = None,
    origin: str | None = None,
    loaded_groups: set[str] | None = None,
    proposal: dict | None = None,
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
    if origin is not None:
        _ORIGIN.set(origin)
    if loaded_groups is not None:
        _LOADED.set(loaded_groups)
    if proposal is not None:
        _PROPOSAL.set(proposal)


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
    """The running agent's toolset, or None if nothing is bound.

    Named for its first reader: to a sub-agent being built, the agent running
    now is its parent. `current_tools()` is the same value, named for dispatch.
    """
    return _TOOLS.get()


def current_tools() -> frozenset[str] | None:
    """The tools the agent running now may call, or None if no agent is bound.

    `dispatch()` refuses anything outside this set. None — a script or a test
    calling `dispatch` directly — is the one case it does not enforce, and it
    is reached by no real path (see tools._toolset_refusal).
    """
    return _TOOLS.get()


def restore_tools(previous: frozenset[str] | None) -> None:
    """Put the toolset back to what it was before a turn bound its own.

    `bind()` cannot do this — it skips None, and None (nothing bound) is the
    usual thing to restore. Called by `Agent.run_turn` when a turn ends, so
    "bound" means "inside an agent's turn" and never "an agent ran here once".
    """
    _TOOLS.set(previous)


def origin() -> str:
    """Who is asking, for the approval surfaces. "" when nothing bound one."""
    return _ORIGIN.get()


def loaded_groups() -> set[str] | None:
    """The running agent's loaded tool groups, or None if nothing is bound.

    None means "there is no agent to load into", which `load_tools` reports
    rather than papering over — a tool group loaded into nothing would look
    like it worked and then not be there.
    """
    return _LOADED.get()


def proposal_slot() -> dict | None:
    """The fast path's task-proposal slot, or None if no provider bound one.

    None means "nothing here can open a task", which `task_propose` reports
    rather than papering over — a proposal written into nothing would look
    like it worked and then not exist.
    """
    return _PROPOSAL.get()


def describe() -> dict[str, Any]:
    """For tests and debugging — what is bound right now."""
    return {
        "plan": (_PLAN.get() or {}).get("text", ""),
        "approve_bound": _APPROVE.get() is not None,
        "depth": _DEPTH.get(),
        "origin": _ORIGIN.get(),
    }
