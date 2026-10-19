"""The chat-to-chat service (plan `docs/plans/2026-10-19-chat-to-chat-plan.md`).

An in-process daemon service, installed like `BrowserLook` (decision P-3):
the daemon builds one, `install()`s it in `start()` and `stop()`s it on
shutdown, and the chat tools (`jarvis/v2/tools/chats.py`) reach it only
through `installed()`. There is no HTTP route behind it.

This is its read half (C1): `stores` and `snapshot()`, which is all the four
read tools need. A read takes the daemon lock only for the snapshot of what
is running, never while it reads a log or scrubs (the scrub reads credential
files; plan §3.3). The send half, its queues and replies, is C3's.

SELF_PROTECTED (decision CC-8): what this module hands a tool is what one
chat may see of another.
"""
from __future__ import annotations

import threading
from typing import Any


class ChatRelay:
    """What the chat tools may ask the daemon for."""

    def __init__(self, daemon: Any):
        self.daemon = daemon
        self.closed = False

    @property
    def stores(self):
        return self.daemon.stores

    def snapshot(self) -> dict[str, dict]:
        """Per live thread, what `chat_reads` calls `live` (§3.1): whether a
        turn is running and how many of the owner's messages wait on it.
        Relays do not exist yet, so `relay_from` is None and
        `relays_waiting` 0."""
        daemon = self.daemon
        live: dict[str, dict] = {}
        with daemon._lock:
            for thread_id, session in daemon._sessions.items():
                live[thread_id] = {"running": session.worker is not None, "relay_from": None,
                                   "queued": 0, "relays_waiting": 0}
            for thread_id, waiting in daemon._queues.items():
                if waiting:
                    entry = live.setdefault(thread_id, {"running": False, "relay_from": None,
                                                        "queued": 0, "relays_waiting": 0})
                    entry["queued"] = len(waiting)
        return live

    def install(self) -> None:
        global _installed
        with _installed_lock:
            self.closed = False
            _installed = self

    def stop(self) -> None:
        global _installed
        self.closed = True
        with _installed_lock:
            if _installed is self:
                _installed = None


_installed_lock = threading.Lock()
_installed: ChatRelay | None = None


def installed() -> ChatRelay | None:
    """The running daemon's service, or None (no daemon, or it stopped)."""
    with _installed_lock:
        service = _installed
    return service if service is not None and not service.closed else None
