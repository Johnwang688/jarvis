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

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterable, Iterator

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

# Set only by `unconfined()`: dispatch with nothing bound runs instead of
# refusing. Unbound refuses by default — every real caller binds a toolset
# first, so an unbound dispatch is a bug, and a bug should fail loudly rather
# than quietly open the whole registry.
_UNCONFINED: ContextVar[bool] = ContextVar("jarvis_unconfined", default=False)

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

# Whether the owner is at the desk for this turn, for `terminal_read` (WP-F,
# decisions W-2: "foreground chat threads only, where the owner is present").
# A one-key mutable dict, `{"present": bool}`: the v2 FastPathProvider binds
# it for an owner's chat turn typed in the HUD, and sets `present` False when
# a message from anywhere else (Discord, the escape hatch) steers that turn.
# Unbound — every v1 surface, a workflow, a goal, a task, a script — means
# nobody is, and the tool refuses. Like the proposal slot, `Agent.run_turn`
# never sets it, so a slot the provider bound survives the loop's own bind.
_DESK: ContextVar[dict | None] = ContextVar("jarvis_desk", default=None)

# Which v2 conversation a tool call belongs to (the peers plan's Phase 0,
# 2026-10-10): `{thread_id, project_id, provider, turn_id}`. The v2 fast path
# binds it for its own turn; the daemon's jarvis-mcp bridge binds it for each
# call a Claude or Codex chat makes through its session token. A tool that
# must know who is asking (a peer message, a project proposal) reads it here,
# never from an argument the model wrote. Unbound — every v1 surface, a
# script, a test — is None, and such a tool refuses. `Agent.run_turn` never
# sets it, so a caller the provider bound survives the loop's own bind.
_CALLER: ContextVar[dict | None] = ContextVar("jarvis_caller", default=None)
CALLER_KEYS = ("thread_id", "project_id", "provider", "turn_id")

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
    desk: dict | None = None,
    caller: dict | None = None,
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
    if desk is not None:
        _DESK.set(desk)
    if caller is not None:
        _CALLER.set({key: caller.get(key) for key in CALLER_KEYS})


def bind_caller(thread_id: str, project_id: str | None, provider: str,
                turn_id: str | None) -> None:
    """Bind who is calling, from the four facts that name a v2 turn."""
    bind(caller={"thread_id": thread_id, "project_id": project_id,
                 "provider": provider, "turn_id": turn_id})


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

    `dispatch()` refuses anything outside this set, and with None — nothing
    bound — it refuses everything, unless the code calling it opted out with
    `unconfined()` (see tools._toolset_refusal). Every real caller binds first.
    """
    return _TOOLS.get()


@contextmanager
def tools_bound(names: Iterable[str]) -> Iterator[frozenset[str]]:
    """Bind `names` as the toolset for the duration, then put back what was.

    For a script or a test that calls `tools.dispatch` itself, with no agent
    turn to bind one: name the tools it calls, and dispatch holds it to them
    exactly as it would an agent. Scoped, so the thread is not left holding a
    toolset no agent owns once the block ends.
    """
    held = frozenset(names)
    token = _TOOLS.set(held)
    try:
        yield held
    finally:
        _TOOLS.reset(token)


@contextmanager
def unconfined() -> Iterator[None]:
    """Let `dispatch` run any registered tool on this thread while nothing is
    bound — for a script or a test that really means "every tool".

    The explicit opt-in that makes "unbound refuses" livable (2026-10-11, the
    owner's call on PR #34's follow-up). It is code, not configuration: a
    ContextVar set by this context manager and nothing else, so no environment
    variable, file or argument a model could write reaches it, and nothing under
    `jarvis/` calls it (tests/dispatch_toolset_check.py pins that). It changes
    only the unbound case — a toolset bound inside the block, an agent's turn
    included, is enforced as ever — and it does not follow a new thread, which
    starts with an empty context and so refuses again.
    """
    token = _UNCONFINED.set(True)
    try:
        yield
    finally:
        _UNCONFINED.reset(token)


def is_unconfined() -> bool:
    """True inside `unconfined()`. Read by dispatch only when nothing is bound."""
    return _UNCONFINED.get()


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


def at_desk() -> bool:
    """True only inside a turn the v2 fast path bound as the owner's, typed
    in the HUD, and never steered from elsewhere since. Fails closed."""
    slot = _DESK.get()
    return bool(slot is not None and slot.get("present") is True)


def caller() -> dict | None:
    """The v2 conversation this call belongs to, or None if nothing bound one.

    A copy: what a tool does with it cannot change what the next call sees.
    """
    found = _CALLER.get()
    return dict(found) if found is not None else None


def describe() -> dict[str, Any]:
    """For tests and debugging — what is bound right now."""
    return {
        "plan": (_PLAN.get() or {}).get("text", ""),
        "approve_bound": _APPROVE.get() is not None,
        "depth": _DEPTH.get(),
        "origin": _ORIGIN.get(),
    }
