"""The daemon's in-memory grant to one owner HUD chat."""
from __future__ import annotations

from dataclasses import dataclass
from threading import Condition

from jarvis import config

REFUSED = "Refused: Windows window driving is off for this chat."
DRIVER_BRIEF = (
    "[Desktop driving instructions]\n"
    "Use only the hud_window tools for windows. Do not load a skill for this. "
    "List windows first with hud_window_list. Take a snapshot before acting and act by ref. "
    "To open an app, use the Start or Search button on the taskbar (an explorer.exe window). "
    "Give every acting call a purpose. Set risky to \"yes\" for anything that sends, submits, "
    "pays, deletes, installs or changes system settings, and whenever unsure. "
    "Terminals are see-only here. Use the terminal tool. "
    "A refusal is final. Do not look for another route to the same protected window. "
    "If a step is refused twice, stop and tell the owner."
)


@dataclass(eq=False)
class _Grant:
    thread_id: str
    brief_pending: bool = True


class DesktopDriver:
    def __init__(self, lock=None):
        self.enabled = config.desktop_drive()
        self.changed = Condition(lock)
        self._grant: _Grant | None = None

    def thread_id(self):
        with self.changed:
            return self._grant.thread_id if self._grant is not None else None

    def grant(self, thread_id):
        with self.changed:
            if (self.enabled and config.desktop_drive() and self._grant is not None
                    and self._grant.thread_id == thread_id):
                return self._grant
            return None

    def set(self, thread_id, on):
        with self.changed:
            if not on:
                return self.clear(thread_id) is not None
            if not self.enabled or not config.desktop_drive():
                raise ValueError("Desktop driving is disabled on this daemon.")
            if self._grant is not None:
                if self._grant.thread_id != thread_id:
                    raise ValueError("Another chat is driving the desktop. Turn it off in that chat first.")
                return False
            self._grant = _Grant(thread_id)
            self.changed.notify_all()
            return True

    def clear(self, thread_id=None):
        with self.changed:
            grant = self._grant
            if grant is None or (thread_id is not None and grant.thread_id != thread_id):
                return None
            self._grant = None
            self.changed.notify_all()
            return grant.thread_id

    def take_brief(self, thread_id, expected):
        with self.changed:
            grant = self.grant(thread_id)
            # Lead, after review: the brief goes with EVERY owner desk turn
            # while the chat drives. Sending it once lost it for good when
            # the provider rejected that turn at its start.
            if grant is None or grant is not expected:
                return None
            return DRIVER_BRIEF
