"""A read-only look at a currently visible owner HUD Browser pane."""
from typing import Annotated

from jarvis import runtime, untrusted
from jarvis.tools import EXPLICIT_ONLY, PRIVATE_RESULT, ToolResult, tool
from ..browser.look import look_for_tool


@tool
def hud_browser_view(
    pane: Annotated[str, "Visible Browser pane slot number, or empty for the last owner-focused Browser pane; not a tab ID."] = "",
    whole_page: Annotated[bool, "Read rendered page text beyond the viewport without scrolling; image and refs stay viewport-only."] = False,
) -> ToolResult:
    """Look at the owner's visible HUD Browser page without changing it. Returns
    fenced untrusted text/interactive refs and an optional viewport image.
    Never reads form values; form and embedded-frame pixels are masked. A
    credential hit refuses everything. Canvas/image or inaccessible DOM text
    cannot be credential-scanned. Direct pages use a fresh logged-out load,
    not the owner's exact view, only with existing network permission. Works
    only in the owner's HUD chat; obeys the pane's Jarvis can see switch."""
    if not runtime.at_desk() or runtime.depth()!=0:
        return ToolResult("Refused: hud_browser_view works only in a chat the owner is typing in at the HUD (not from Discord, a task, a schedule or a sub-agent)")
    if not isinstance(pane,str) or (pane and pane not in tuple(str(i) for i in range(1,9))) or not isinstance(whole_page,bool):
        return ToolResult("Refused: pane must be a visible slot from1 to8 or empty, and whole_page must be true or false")
    result=look_for_tool(pane,whole_page)
    if result.refused:return ToolResult("Refused: "+result.refused)
    if not runtime.at_desk() or runtime.depth()!=0 or runtime.should_stop()():
        return ToolResult("Refused: the owner is no longer at the HUD; look again from the HUD")
    return ToolResult(untrusted.fence(result.text,result.source,notes=list(result.notes)),
                      result.image if runtime.desk_images() else None)


EXPLICIT_ONLY.add("hud_browser_view")
PRIVATE_RESULT.add("hud_browser_view")
