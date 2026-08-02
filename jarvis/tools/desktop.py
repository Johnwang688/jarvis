"""Desktop app control, via the Windows bridge.

Same discipline as the browser tools, because it is the same problem: read
the app as text, act by ref, re-read. Refs come from the accessibility tree,
so there is no coordinate arithmetic and a ref that has gone stale fails
loudly instead of clicking whatever moved into its place.

Every tool takes an app *name* from `config.DESKTOP_APPS` and nothing else —
no window titles, no handles, no paths. See that registry for why.
"""

from __future__ import annotations

from typing import Annotated

from ..desktop import SESSION, DesktopError
from . import ToolResult, tool


@tool
def desktop_status() -> str:
    """Check whether the Windows desktop bridge is connected, and list the apps
    Jarvis is allowed to drive."""
    try:
        return SESSION.status()
    except DesktopError as exc:
        return f"Error: {exc}"


@tool
def desktop_open(
    app: Annotated[str, "Registered app name, e.g. 'settings' or 'claude'"],
) -> str:
    """Open (or focus) a registered desktop app and read its window as text.

    Starts the app if it is not already running, brings it to the foreground,
    and returns the same ref-tagged tree desktop_snapshot gives — so this is
    usually the only call needed to begin.
    """
    try:
        return SESSION.open(app)
    except DesktopError as exc:
        return f"Error: {exc}"


@tool
def desktop_snapshot(
    app: Annotated[str, "Registered app name"],
) -> str:
    """Read the app's current window as text: interactive elements with refs,
    plus labels and values.

    Take a fresh snapshot after anything that changes the window — refs are
    reassigned every time.
    """
    try:
        return SESSION.snapshot(app)
    except DesktopError as exc:
        return f"Error: {exc}"


@tool
def desktop_click(
    app: Annotated[str, "Registered app name"],
    ref: Annotated[str, "Element ref from the latest snapshot, e.g. e12"],
) -> str:
    """Click an element by its snapshot ref."""
    try:
        return SESSION.click(app, ref)
    except DesktopError as exc:
        return f"Error: {exc}"


@tool
def desktop_type(
    app: Annotated[str, "Registered app name"],
    ref: Annotated[str, "Element ref of the text field"],
    text: Annotated[str, "Text to enter (replaces what is there)"],
    submit: Annotated[bool, "Press Enter afterwards"] = False,
) -> str:
    """Type into a text field, optionally submitting."""
    try:
        return SESSION.type_text(app, ref, text, submit)
    except DesktopError as exc:
        return f"Error: {exc}"


@tool
def desktop_key(
    app: Annotated[str, "Registered app name"],
    keys: Annotated[
        str,
        "Keys to send, uiautomation syntax: {Enter}, {Esc}, {Tab}, {Ctrl}n, "
        "{Alt}{F4}, {Down}. Literal text is typed as-is.",
    ],
) -> str:
    """Send keystrokes to the app window (shortcuts, Escape, arrow keys).

    Use this for things with no clickable element — dismissing a dialog,
    keyboard shortcuts, moving through a list. The window is focused first,
    so the keys always land in the target app.
    """
    try:
        return SESSION.send_keys(app, keys)
    except DesktopError as exc:
        return f"Error: {exc}"


@tool
def desktop_screenshot(
    app: Annotated[str, "Registered app name"],
    marked: Annotated[bool, "Overlay each element's ref (e1, e2…) as a badge"] = True,
) -> ToolResult:
    """Take a screenshot of the app window. Requires a vision-capable model.

    The text snapshot is the primary channel and is cheaper and more precise;
    reach for this when layout, rendering, or something the tree does not
    expose actually matters.
    """
    try:
        image, size, marks = SESSION.screenshot_b64(app, marked)
    except DesktopError as exc:
        return ToolResult(f"Error: {exc}")
    note = f" {marks} element(s) marked." if marked else ""
    return ToolResult(f"Screenshot captured ({size:,} bytes).{note}", image_b64=image)
