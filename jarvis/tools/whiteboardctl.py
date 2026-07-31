"""Whiteboard window control.

Same shape as voicectl: the hook lives here so the tool has no import edge
into the face; the server registers a callback that broadcasts "wb_close"
over SSE, and the whiteboard page closes itself on hearing it.

Only pages that opt in by handling the event can be closed this way. The HUD
deliberately does not — it is the approval surface, and the agent must never
hold the lever that dismisses the window gating him.
"""

from __future__ import annotations

from typing import Callable

from . import tool

_on_close: Callable[[], None] | None = None


def on_close(callback: Callable[[], None]) -> None:
    global _on_close
    _on_close = callback


@tool
def whiteboard_close() -> str:
    """Close the whiteboard window, if one is open.

    Use when the owner asks to close the whiteboard / design board. Only the
    whiteboard listens for this — it cannot close any other window.
    """
    if _on_close is None:
        return (
            "Error: the face server is not running in this process, so there "
            "is no whiteboard window to signal."
        )
    _on_close()
    return "Close signal sent — the whiteboard window shuts itself."
