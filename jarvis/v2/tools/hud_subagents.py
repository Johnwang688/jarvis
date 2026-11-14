"""`hud_open_subagents`: show this chat's native sub-agents in the owner's
HUD panes (`jarvis/v2/subagent_panes.py`).

Bound as `owner_post` is, checked twice: here first (`runtime.at_desk()`
and `runtime.depth() == 0`), then by the service under the daemon lock
against the live session and the exact turn. Claude and Codex owner chats
are offered it through the session catalog (`session_tools.py`):
foreground-only and desk-only, not read-only, so `permit` decides
(`jarvis-session-delegate`). `EXPLICIT_ONLY`, so no v1 surface holds it;
the fast path never does; never in `mcp_catalog.MCP_TOOLS`. Not
`PARALLEL_SAFE`, not dangerous: it opens read-only views and nothing else.
"""
from __future__ import annotations

from typing import Annotated

from jarvis import runtime
from jarvis.tools import EXPLICIT_ONLY, tool

from ..subagent_panes import NOT_AT_DESK, NOT_FROM_A_SUB_AGENT, NOT_IN_CHAT, installed


@tool
def hud_open_subagents(
    which: Annotated[list[str] | None, "The sub-agents to show, each by its name or id (at most "
                                       "11); leave it out for every one still running"] = None,
) -> str:
    """Show this chat's own sub-agents in the owner's HUD panes, each in its
    own pane, when the owner asks to see them there. Start them in the
    background first so they are still running. The HUD uses existing panes
    that hold no unsaved work (never the one showing this chat or the one the
    owner is in), then lays the window out as a grid for the rest (up to 12
    panes), and never moves the owner's focus. The result says how many it
    showed. Only during a turn the owner started at the HUD in this chat."""
    if not runtime.at_desk():
        return NOT_AT_DESK
    if runtime.depth() != 0:
        return NOT_FROM_A_SUB_AGENT
    who = runtime.caller()
    service = installed()
    if who is None or service is None:
        return NOT_IN_CHAT
    return service.open(who, which)


EXPLICIT_ONLY.add("hud_open_subagents")
