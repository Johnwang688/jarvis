"""`hud_open_browser`: open public web pages in new Remote Browser panes of the
owner's HUD (`jarvis/v2/browser_open.py`; decisions D2–D4 of
`docs/plans/2026-11-13-agent-pane-grid-decisions.md`).

Bound as `hud_open_subagents` is, checked twice: here first
(`runtime.at_desk()` and `runtime.depth() == 0`), then by the service under
the daemon lock against the live session and the exact turn. Claude and
Codex owner chats are offered it through the session catalog
(`session_tools.py`): foreground-only and desk-only, not read-only, so
`permit` decides (`jarvis-session-delegate`). `EXPLICIT_ONLY`, so no v1
surface holds it; the fast path never does; never in `mcp_catalog.MCP_TOOLS`.
Not `PARALLEL_SAFE`, not dangerous: it asks the owner's window to show
public pages, judged first, which no agent may then look at or drive until
the owner navigates them himself; it admits no loopback port.
"""
from __future__ import annotations

from typing import Annotated

from jarvis import runtime
from jarvis.tools import EXPLICIT_ONLY, tool

from ..browser_open import NOT_AT_DESK, NOT_FROM_A_SUB_AGENT, NOT_IN_CHAT, installed


@tool
def hud_open_browser(
    urls: Annotated[list[str], "The public web pages to open, 1 to 11 full http(s) URLs, each in its "
                               "own new Browser pane"],
) -> str:
    """Open public web pages in the owner's HUD, each in its own Remote
    Browser pane, when the owner asks to see them there. Only public
    http(s) pages: a loopback, home-network or Jarvis address refuses the
    whole call. The HUD uses existing panes that hold no unsaved work (never
    the one showing this chat or the one the owner is in), then lays the
    window out as a grid, and never moves the owner's focus. The result says
    how many opened. Only during a turn the owner started at the HUD in this
    chat."""
    if not runtime.at_desk():
        return NOT_AT_DESK
    if runtime.depth() != 0:
        return NOT_FROM_A_SUB_AGENT
    who = runtime.caller()
    service = installed()
    if who is None or service is None:
        return NOT_IN_CHAT
    return service.open(who, urls)


EXPLICIT_ONLY.add("hud_open_browser")
