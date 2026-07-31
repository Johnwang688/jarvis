"""Voice output control (mute/unmute for the face).

The flag lives here rather than in the face so the tool has no import edge
into the server (face → tools is the allowed direction). The face reads
is_muted() before synthesizing TTS and registers on_change() to broadcast
the new state to the window, so the MUTE button and the spoken command
("Jarvis, mute yourself") stay in sync.
"""

from __future__ import annotations

from typing import Annotated, Callable

from . import tool

_muted = False
_on_change: Callable[[bool], None] | None = None


def is_muted() -> bool:
    return _muted


def set_muted(muted: bool) -> None:
    global _muted
    _muted = bool(muted)
    if _on_change is not None:
        try:
            _on_change(_muted)
        except Exception:
            pass


def on_change(callback: Callable[[bool], None]) -> None:
    global _on_change
    _on_change = callback


@tool
def set_voice_mute(
    muted: Annotated[bool, "True to mute Jarvis's spoken replies, False to unmute"],
) -> str:
    """Mute or unmute Jarvis's spoken voice in the face window.

    Use when the owner says "mute yourself" / "unmute yourself". Replies
    still appear as text in the window; only the audio stops.
    """
    set_muted(muted)
    return "Voice output muted." if muted else "Voice output unmuted."
