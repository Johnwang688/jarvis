"""`load_tools` — pull a deferred tool group into the running conversation.

The other half of `tools.ToolGroup` (see its docstring for why groups exist).
An agent is offered each available group's `core` tools plus a one-line pointer
in its working-context block; this is what turns the pointer into the rest of
the schemas, for the life of that agent.

The channel is `runtime.loaded_groups()` — a mutable set the agent owns and
binds per turn, exactly like the working plan. A tool cannot be handed the
agent that called it (`dispatch()` calls `func(**arguments)` and nothing else),
so the agent reads the set back on its next step and rebuilds its tool specs.

It fails closed the way everything in runtime.py does: with nothing bound —
which is what a tool running outside a turn looks like — there is no set to
write into, so the call reports that instead of silently succeeding.
"""

from __future__ import annotations

from typing import Annotated

from .. import runtime
from . import GROUPS, REGISTRY, tool


@tool
def load_tools(
    group: Annotated[str, "The tool group to load, e.g. 'spotify'"],
) -> str:
    """Load a group of tools listed as available in your working context.

    Their schemas are left out of the conversation until asked for, so this is
    how you reach them. Call it once — they stay for the rest of the
    conversation, and the tools it adds are listed in the reply.
    """
    name = (group or "").strip().lower()
    entry = GROUPS.get(name)
    if entry is None:
        known = ", ".join(sorted(g.name for g in GROUPS.values() if g.is_available()))
        return f"Error: no tool group called {group!r}. Available: {known or 'none'}."
    if not entry.is_available():
        return (
            f"Error: the '{name}' tools need setting up first — tell the owner "
            f"rather than retrying. ({entry.summary})"
        )

    loaded = runtime.loaded_groups()
    if loaded is None:
        return (
            "Error: tool groups cannot be loaded from here (no agent context is "
            "bound). Report this rather than working around it."
        )

    # Loading widens the toolset `dispatch()` enforces, so it is gated the way
    # `tools.loadable` is: only an agent already holding the group's core may
    # expand into the rest. An agent handed an explicit toolset without them
    # (a sub-agent, a bench, anything narrowed on purpose) must not be able to
    # widen it by asking — before 2026-10-10 this wrote any available group
    # into the set and `Agent._sync_tools` folded its tools in.
    held = runtime.current_tools()
    if held is not None and not set(entry.core) <= held:
        return (
            f"Error: the '{name}' tools are not available to this agent, so they "
            "cannot be loaded here. Say what you need and let the user decide."
        )

    added = [n for n in entry.extra if n in REGISTRY]
    if name in loaded:
        return f"The '{name}' tools are already loaded: {', '.join(added)}."

    # A plain set mutated in place, so the agent that owns it sees the change
    # on its next step — the working plan's mechanism, and for the same reason.
    loaded.add(name)
    return (
        f"Loaded the '{name}' tools: {', '.join(added)}.\n"
        "They are available from your next step onwards."
    )
