"""Windows desktop tools for the selected, top-level owner chat."""
from __future__ import annotations

import re
from typing import Annotated

from jarvis import runtime
from jarvis.desktop import DesktopError, SESSION
from jarvis.tools import EXPLICIT_ONLY, PRIVATE_RESULT, tool
from windows.desktopwire import APPS, NAV_KEYS, launch_name

HUD_DESKTOP_TOOLS = frozenset({
    "hud_desktop_status", "hud_desktop_open", "hud_desktop_snapshot",
    "hud_desktop_click", "hud_desktop_type", "hud_desktop_key",
})
UNAVAILABLE = "Refused: desktop control requires a current owner HUD or paired voice chat turn."


def current():
    who = runtime.caller() or {}
    return bool(who.get("thread_id") and who.get("turn_id")) and runtime.hud_desktop_access()


def _run(op, app=None, **fields):
    if not current():
        return UNAVAILABLE
    if app is not None:
        if not isinstance(app, str):
            return "Refused: use a registered Windows app name."
        app = app.strip().casefold()
        if op == "open" and app not in APPS:
            if launch_name(app) is None:
                return "Refused: use a normal Windows Start app display name, without a path or elevation."
            op = "launch_app"
        elif app not in APPS:
            return "Refused: only Settings and Claude Desktop support UI controls. Use hud_desktop_open for installed Start apps."
        fields["app"] = app
    if "ref" in fields and (not isinstance(fields["ref"], str)
                            or not re.fullmatch(r"[0-9a-f]{16}-e[1-9][0-9]{0,3}", fields["ref"])):
        return "Refused: use the complete element ref from this chat's latest snapshot."
    try:
        result = SESSION.request(op, require_pair=True, current=current,
                                 scope=runtime.caller()["thread_id"], **fields)
    except (OSError, DesktopError) as exc:
        return f"Error: {exc}"
    if op == "ping":
        return ("Paired Windows desktop bridge is connected. Read and control: "
                + ", ".join(sorted(APPS)) + ". Launch other installed apps by exact Windows Start display name.")
    if op == "launch_app":
        return f"Sent the Windows launch request for {result['name']}."
    if op in ("open", "snapshot"):
        return result["snapshot"]
    return "Action completed. Take a fresh snapshot to see the result."


@tool
def hud_desktop_status() -> str:
    """Check the paired, non-admin Windows desktop bridge and its supported apps."""
    return _run("ping")


@tool(dangerous=True)
def hud_desktop_open(app: Annotated[str, "Registered app or exact Windows Start display name, e.g. settings or Chrome"]) -> str:
    """Open a Windows app with the owner's approval. Settings and Claude also return their accessible controls."""
    return _run("open", app)


@tool(dangerous=True)
def hud_desktop_snapshot(app: Annotated[str, "settings or claude"]) -> str:
    """Focus and read a registered Windows app's accessible controls with the owner's approval."""
    return _run("snapshot", app)


@tool(dangerous=True)
def hud_desktop_click(app: Annotated[str, "settings or claude"],
                      ref: Annotated[str, "Complete element ref from this chat's latest snapshot"]) -> str:
    """Click a Windows app element by snapshot ref after the owner approves."""
    return _run("click", app, ref=ref)


@tool(dangerous=True)
def hud_desktop_type(app: Annotated[str, "settings or claude"],
                     ref: Annotated[str, "Complete editable field ref from this chat's latest snapshot"],
                     text: Annotated[str, "Literal text to enter, up to 2000 characters"],
                     submit: Annotated[bool, "Press Enter after typing"] = False) -> str:
    """Replace a Windows text field's content, optionally submitting, after the owner approves."""
    if not isinstance(text, str) or len(text) > 2000 or any(ord(ch) < 32 for ch in text) or type(submit) is not bool:
        return "Refused: supply a single line up to 2000 characters and a boolean submit flag."
    return _run("type", app, ref=ref, text=text, submit=submit)


@tool(dangerous=True)
def hud_desktop_key(app: Annotated[str, "settings or claude"],
                    key: Annotated[str, "One navigation key: enter, escape, tab, up, down, left, right, home or end"]) -> str:
    """Send a single navigation key to a registered Windows window after the owner approves."""
    keys = NAV_KEYS.get(key.strip().casefold()) if isinstance(key, str) else None
    if not keys:
        return "Refused: use a single supported navigation key."
    return _run("key", app, keys=keys)


EXPLICIT_ONLY.update(HUD_DESKTOP_TOOLS)
PRIVATE_RESULT.update(HUD_DESKTOP_TOOLS - {"hud_desktop_status"})
