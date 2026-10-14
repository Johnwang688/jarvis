"""Explicit-only Browser input tool for an owner HUD chat turn."""
from typing import Annotated
from jarvis import runtime
from jarvis.tools import EXPLICIT_ONLY, PRIVATE_RESULT, tool
from ..browser.drive import act_for_tool
from ..browser.drive_state import mark_attempt


@tool
def hud_browser_act(
    action: Annotated[str, "One of click, type, press, scroll, select, upload, navigate, back, forward."],
    pane: Annotated[str, "Visible Browser pane slot 1–8 or empty for the owner-focused pane; never a tab ID."] = "",
    target: Annotated[str, "Interactive eN ref from this turn's latest guarded Remote look; required for click/type/select/upload."] = "",
    text: Annotated[str, "Exact replacement text, unique option label, destination URL, named key, or scroll direction as appropriate."] = "",
    file: Annotated[str, "Upload only: permitted file inside this chat's project; approval binds its exact bytes."] = "",
) -> str:
    """Perform one reviewed action in the owner's visible Remote Browser pane.
    The owner's global and pane switches and all desk/action guards must permit it.
    No coordinate clicks or arbitrary JavaScript. Input yields to owner activity;
    private HUD approval may be required and never authorizes future actions.
    Typing replaces the field; press accepts only the supported named navigation
    keys (not shortcuts), and scroll accepts up/down/top/bottom. A fresh look is
    required when target or document changes. This is available only to the
    owner's HUD chat, never tasks, Discord or sub-agents. Native calls share the gate.
    """
    if not runtime.at_desk() or runtime.depth()!=0:
        return "Refused: Browser driving requires the owner's HUD chat."
    mark_attempt(pane)
    result=act_for_tool(action,pane,target,text,file)
    prefix={"acted":"Acted: ","refused":"Refused: ","asked":"Asked: ","stopped":"Stopped: "}[result.outcome]
    return prefix+result.text


EXPLICIT_ONLY.add("hud_browser_act")
PRIVATE_RESULT.add("hud_browser_act")
