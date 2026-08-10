"""Sub-agents: hand a bounded job to a fresh context and keep only the answer.

`delegate()` in agent.py is the cost lever — cheap tier, single shot, no tools.
This is the *context* lever, which is a different problem. Every tool result a
run produces stays in the orchestrator's transcript forever: fetch four pages
to answer one question and the 40k tokens of page dump outlive the answer by
the whole rest of the session, crowding out the things that still matter and
dragging the compactor in early.

A sub-agent runs the job on its own transcript and returns only its final text.
The parent's context grows by a paragraph instead of by four pages.

Two tools. `run_subagent` sends one child and blocks. `run_fleet` sends several
at once and blocks until all of them are back — the same context saving,
multiplied, for the case where a question genuinely splits into independent
pieces. Both pick from the named types in `agents.py`.

Deliberate choices, in the order they matter:

  - **Every child runs on the orchestrator model** (owner's call, 2026-08-09).
    A type carries a brief, a toolset and a budget — never a model.
  - **The human gate is inherited, not bypassed.** Children dispatch with the
    parent's approver (`runtime.approver()`, which denies when unbound), so a
    workflow's deny-all approver yields deny-all children. No type carries a
    dangerous tool, which is the first line of defence.
  - **A type can only narrow.** Its toolset is intersected with the union of
    all types' tools and then with the parent's own, so no type is a route to
    something the parent could not use.
  - **Cancellation reaches through**, and **depth is capped** at
    `runtime.MAX_DEPTH`.
  - **The browser type never runs concurrently.** `browser.SESSION` is a
    module-level singleton with one page and one 120-action budget; two
    children on it would interleave and corrupt both. `run_fleet` runs
    concurrent-unsafe jobs one at a time, which is what makes it safe to run
    everything else together — the caveat CLAUDE.md has flagged since
    sub-agents were introduced, now enforced rather than only noted.
"""

from __future__ import annotations

import contextvars
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated

from .. import agents, config, runtime
from . import REGISTRY, tool

# Every tool any type can reach. Kept as a module-level name because it is what
# longhorizon_check asserts over: nothing in here is dangerous, and everything
# in here exists.
SUBAGENT_TOOLS = sorted(
    {name for agent_type in agents.TYPES.values() for name in agent_type.tools}
)

MAX_FLEET = 6
"""Most children one run_fleet call may start. A bound on blast radius and
spend — asking for more is an error, not a silent truncation."""


def _child_tools(agent_type: agents.AgentType) -> list[str]:
    """The type's tools, narrowed to what is registered and to the parent's own.

    The intersection with the parent is the load-bearing part. Without it a
    background workflow — denied browser tools because workflows share one
    Playwright page — could reach the browser just by asking for a child of
    the browser type.
    """
    parent = runtime.parent_tools()
    return [
        name
        for name in agent_type.tools
        if name in REGISTRY and (parent is None or name in parent)
    ]


def _run_one(job: agents.Job) -> agents.Job:
    """Run one child to completion. Never raises — a fleet must always report."""
    from .. import agent as agent_mod

    agent_type = agents.get(job.type)
    try:
        child = agent_mod.Agent(
            model=config.TIERS["subagent"],
            system=agent_type.prompt(),
            tool_names=_child_tools(agent_type),
            max_steps=job.extras.get("max_steps") or agent_type.max_steps,
            approve=runtime.approver(),  # inherited; denies when unbound
            should_stop=runtime.should_stop(),
            depth=runtime.depth() + 1,
        )
        # In a *copy* of the current context, because the child's own run_turn
        # binds its plan, depth and toolset over the same ContextVars we are
        # standing in. Run it directly and the parent would come back holding
        # the child's empty plan and the child's depth — the plan would vanish
        # from the parent's system message mid-task, which is the exact failure
        # this whole mechanism exists to prevent. The copy is discarded on
        # return, so the parent's bindings survive untouched.
        turn = contextvars.copy_context().run(child.run_turn, job.task)
    except Exception as exc:
        job.error = f"{type(exc).__name__}: {exc}"
        return job

    job.steps = turn.steps
    job.cost_usd = turn.cost_usd
    if turn.cancelled:
        job.error = "cancelled before it finished"
    elif turn.stopped_early:
        job.result = turn.text or "(nothing reported)"
        job.error = "ran out of steps"
    else:
        job.result = turn.text or "(the sub-agent returned nothing)"
    return job


def _render(job: agents.Job, label: str = "") -> str:
    head = f"[{label or job.type}: {job.steps} step(s), ${job.cost_usd:.4f}]"
    if job.error and not job.result:
        return f"{head}\nFailed — {job.error}."
    if job.error:
        return f"{head} {job.error}. Partial result:\n{job.result}"
    return f"{head}\n{job.result}"


@tool
def run_subagent(
    task: Annotated[
        str,
        "The job, stated so it can be done with no other context: what to find "
        "or do, where to look, and what to report back",
    ],
    type: Annotated[
        str,
        "Which kind of sub-agent to send. One of: "
        + ", ".join(agents.TYPES)
        + ". Defaults to generalist.",
    ] = agents.DEFAULT,
    max_steps: Annotated[int, "Override the type's own step budget"] = 0,
) -> str:
    """Hand a self-contained job to a sub-agent and get back just its answer.

    Use this when finishing something would flood your own context with
    material you only need the conclusion of — reading through a large
    codebase, checking several pages to answer one question, digging through a
    long document. The sub-agent's intermediate work is discarded; you get its
    final report.

    Give it everything it needs in the task description: it cannot see this
    conversation. It cannot run anything that would need the user's approval.

    Types:
    """
    if runtime.depth() >= runtime.MAX_DEPTH:
        return (
            f"Error: sub-agent depth limit ({runtime.MAX_DEPTH}) reached. "
            "Do this part of the work directly rather than delegating further."
        )
    if runtime.should_stop()():
        return "Cancelled before the sub-agent started."

    job = agents.Job(task=task, type=type)
    if max_steps:
        job.extras["max_steps"] = max(1, min(int(max_steps), 30))
    return _render(_run_one(job))


@tool
def run_fleet(
    jobs: Annotated[
        str,
        'JSON list of jobs, each {"task": "...", "type": "explorer"}. '
        "Each task must stand on its own — they cannot see each other or this "
        f"conversation. At most {MAX_FLEET}.",
    ],
) -> str:
    """Send several sub-agents at once and get all their answers back together.

    Use when a question genuinely splits into independent pieces — four
    subsystems to map, three sources to check, several files to review. They
    run at the same time, so this costs about as much wall-clock as the slowest
    one rather than the sum, and only the answers come back into your context.

    Do not use it for steps that depend on each other: they cannot see each
    other's results. Run those one at a time.

    Types:
    """
    if runtime.depth() >= runtime.MAX_DEPTH:
        return (
            f"Error: sub-agent depth limit ({runtime.MAX_DEPTH}) reached. "
            "Do this work directly rather than delegating further."
        )
    if runtime.should_stop()():
        return "Cancelled before the fleet started."

    try:
        raw = json.loads(jobs)
    except json.JSONDecodeError as exc:
        return f"Error: jobs was not valid JSON ({exc})."
    if not isinstance(raw, list) or not raw:
        return 'Error: jobs must be a non-empty JSON list of {"task": ..., "type": ...}.'
    if len(raw) > MAX_FLEET:
        return f"Error: at most {MAX_FLEET} jobs at once, got {len(raw)}. Send fewer."

    parsed: list[agents.Job] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not str(item.get("task", "")).strip():
            return f"Error: job {index} has no task."
        parsed.append(
            agents.Job(task=str(item["task"]), type=str(item.get("type", agents.DEFAULT)))
        )

    # Concurrent-unsafe types hold a singleton (today: the browser's one page
    # and its shared action budget), so they run alone. Everything else goes
    # together.
    solo = [j for j in parsed if not agents.get(j.type).concurrent_safe]
    together = [j for j in parsed if agents.get(j.type).concurrent_safe]

    if together:
        contexts = [contextvars.copy_context() for _ in together]
        with ThreadPoolExecutor(
            max_workers=min(len(together), MAX_FLEET), thread_name_prefix="jarvis-fleet"
        ) as pool:
            futures = [
                pool.submit(ctx.run, _run_one, job)
                for ctx, job in zip(contexts, together)
            ]
            for job, future in zip(together, futures):
                try:
                    future.result()
                except Exception as exc:  # a fleet must always report every job
                    job.error = f"{type(exc).__name__}: {exc}"
    for job in solo:
        if runtime.should_stop()():
            job.error = "cancelled before it started"
            continue
        _run_one(job)

    # Reported in the order the caller asked for them, not the order they
    # finished — `_run_one` mutates each Job in place, so `parsed` is already
    # the answer set.
    total = sum(j.cost_usd for j in parsed)
    body = "\n\n".join(
        _render(job, label=f"{index + 1}/{len(parsed)} {job.type}")
        for index, job in enumerate(parsed)
    )
    return f"{body}\n\n[fleet: {len(parsed)} sub-agents, ${total:.4f} total]"


# The type catalogue is appended at import rather than written into each
# docstring, so adding a type updates what the model is told in one place.
for _fn in (run_subagent, run_fleet):
    REGISTRY[_fn.__name__].description = (
        REGISTRY[_fn.__name__].description.rstrip() + "\n" + agents.catalogue()
    )
