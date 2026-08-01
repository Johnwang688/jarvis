"""Sub-agents: hand a bounded job to a fresh context and keep only the answer.

`delegate()` in agent.py is the cost lever — cheap tier, single shot, no tools.
This is the *context* lever, which is a different problem. Every tool result a
run produces stays in the orchestrator's transcript forever: fetch four pages
to answer one question and the 40k tokens of page dump outlive the answer by
the whole rest of the session, crowding out the things that still matter and
dragging the compactor in early.

A sub-agent runs the job on its own transcript and returns only its final text.
The parent's context grows by a paragraph instead of by four pages. That is the
single biggest lever on how far a run can go before it starts forgetting.

Deliberate choices:

  - **Synchronous.** The parent blocks while the child runs. That is what makes
    it safe to give a sub-agent the browser, which `workflows.py` cannot do —
    background workflows interleave on one shared Playwright page, but a child
    that runs to completion inside the parent's step never overlaps with it.
  - **The human gate is inherited, not bypassed.** The child dispatches with
    the parent's approver (`runtime.approver()`, which denies when nothing is
    bound). A sub-agent must never be a way to reach a dangerous tool that the
    parent would have had to ask about — background workflows get a deny-all
    approver, so a workflow's children are denied too.
  - **Cancellation reaches through.** The child inherits the parent's
    `should_stop`, so cancelling a turn stops the whole tree at the next step
    boundary rather than leaving an orphan grinding through its budget.
  - **Depth-capped.** `runtime.MAX_DEPTH` stops a spawn loop from forking
    forever at real API cost.
"""

from __future__ import annotations

import contextvars
from typing import Annotated

from .. import runtime
from . import REGISTRY, tool

# What a sub-agent may use. Everything read-heavy — the whole point is that
# bulky output dies with the child — plus the browser, which is safe here
# precisely because the call is synchronous. No plan_write (the plan belongs to
# the parent), no workflow tools, and nothing dangerous: a child's dangerous
# call would go to the parent's approver, and the owner would be answering for
# a request they never saw framed.
SUBAGENT_TOOLS = [
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
    "run_readonly",
    "skill_list",
    "skill_read",
    "browser_goto",
    "browser_snapshot",
    "browser_screenshot",
    "browser_click",
    "browser_type",
]

SUBAGENT_SYSTEM = """You are a sub-agent handling one bounded job for the main agent.

You have your own fresh context. Nothing you read here is visible to whoever
asked — only your final message comes back. So do the work, then write a reply
that stands on its own: the findings, the specific values, the file paths or
URLs that matter. Do not describe your process, and do not say you are done
without saying what you found.

If the job turns out to be impossible or the answer is genuinely not there, say
that plainly and say what you tried. A wrong confident answer is worse than a
clear dead end."""


def _child_tools() -> list[str]:
    """SUBAGENT_TOOLS, narrowed to what is registered and to the parent's own set.

    The intersection is the load-bearing part. Without it a background workflow
    — which is denied browser tools because workflows share one Playwright page
    and would interleave on it — could reach the browser just by spawning a
    child that has them.
    """
    parent = runtime.parent_tools()
    return [
        n
        for n in SUBAGENT_TOOLS
        if n in REGISTRY and (parent is None or n in parent)
    ]


@tool
def run_subagent(
    task: Annotated[
        str,
        "The job, stated so it can be done with no other context: what to find "
        "or do, where to look, and what to report back",
    ],
    max_steps: Annotated[int, "Tool-calling steps the sub-agent may take (default 12)"] = 12,
) -> str:
    """Hand a self-contained job to a sub-agent and get back just its answer.

    Use this when finishing something would flood your own context with material
    you only need the conclusion of — reading through a large codebase, checking
    several pages to answer one question, digging through a long document. The
    sub-agent's intermediate work is discarded; you get its final report.

    Give it everything it needs in the task description: it cannot see this
    conversation. It has read, search, and browsing tools, and cannot run
    anything that would need the user's approval.
    """
    # Imported here, not at module scope: tools/__init__ imports this module
    # while it is still initializing, and agent.py imports tools.
    from .. import agent as agent_mod

    if runtime.depth() >= runtime.MAX_DEPTH:
        return (
            f"Error: sub-agent depth limit ({runtime.MAX_DEPTH}) reached. "
            "Do this part of the work directly rather than delegating further."
        )

    stop = runtime.should_stop()
    if stop():
        return "Cancelled before the sub-agent started."

    child = agent_mod.Agent(
        system=SUBAGENT_SYSTEM,
        tool_names=_child_tools(),
        max_steps=max(1, min(int(max_steps), 30)),
        approve=runtime.approver(),  # inherited; denies when unbound
        should_stop=stop,
        depth=runtime.depth() + 1,
    )
    # In a *copy* of the current context, because the child's own run_turn
    # binds its plan, depth and toolset over the same ContextVars we are
    # standing in. Run it directly and the parent would come back from this
    # call holding the child's empty plan and the child's depth — the plan
    # would vanish from the parent's system message mid-task, which is the
    # exact failure this whole change exists to prevent. The copy is discarded
    # on return, so the parent's bindings survive untouched.
    turn = contextvars.copy_context().run(child.run_turn, task)

    if turn.cancelled:
        return "The sub-agent was cancelled before it finished."
    note = f"\n\n[sub-agent: {turn.steps} step(s), ${turn.cost_usd:.4f}]"
    if turn.stopped_early:
        return (
            "The sub-agent ran out of steps before finishing. Partial result:\n"
            + (turn.text or "(nothing reported)")
            + note
        )
    return (turn.text or "(the sub-agent returned nothing)") + note
