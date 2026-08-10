"""Avatar control — "Jarvis, become the fox."

Thin wrapper over `avatars.py` so the tool has no import edge into the face
(face → tools is the allowed direction); the server registers
`avatars.on_change` and broadcasts, exactly as it does for mute.

Deliberately *not* dangerous. What it moves is a name, two wake phrases and a
picture: no file is written outside `avatars/`, no command runs, and the art
is sanitized and rendered in an `<img>`, so a defaced avatar cannot reach the
approval card it sits next to. The owner can always undo it from the HUD's
AVATAR row or `jarvis avatar jarvis`. It also cannot *create* an avatar —
only select one that is already on disk.
"""

from __future__ import annotations

from typing import Annotated

from .. import avatars
from . import tool


@tool
def avatar_list() -> str:
    """List the avatars available, and say which one is active.

    An avatar is a name, a set of wake phrases, and a face drawn in the HUD.
    """
    here = avatars.active()
    lines = []
    for av in avatars.available():
        mark = "*" if av.slug == here.slug else " "
        wake = ", ".join(w.lower() for w in av.wake_labels())
        note = f" — {av.description}" if av.description else ""
        lines.append(f"{mark} {av.slug}: {av.name} (wake: {wake}){note}")
    lines.append("\n* = active. Change it with set_avatar.")
    return "\n".join(lines)


@tool
def set_avatar(
    slug: Annotated[str, "the avatar's slug, as listed by avatar_list"],
) -> str:
    """Switch to a different avatar — a different name, wake word, and face.

    Use when the owner asks you to become someone else, change your name, or
    change your face ("be the fox", "go back to Jarvis"). The change takes
    effect immediately in any open window and lasts until it is changed again.
    Call avatar_list first if you are not sure of the slug.
    """
    try:
        av = avatars.set_active(slug)
    except LookupError:
        have = ", ".join(a.slug for a in avatars.available())
        return f"No avatar named {slug!r}. Available: {have}"
    wake = ", ".join(w.lower() for w in av.wake_labels())
    return f"Now presenting as {av.name} (slug {av.slug}); wake phrases: {wake}."
