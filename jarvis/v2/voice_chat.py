"""The dedicated Voice chat. Only its id is stored here; conversation uses
the daemon's normal chat history. No model/provider starts until first ask.
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time

from .model import ProviderName, Role
from .provider import UserMessage
from .stores import _write_bytes


class VoiceChat:
    def __init__(self, daemon):
        self.daemon = daemon
        self._lock = threading.Lock()

    def _thread(self):
        d = self.daemon
        path = d.stores.root / "voice-chat.json"
        thread = None
        try:
            ident = json.loads(path.read_text(encoding="ascii"))
            if isinstance(ident, str) and re.fullmatch(r"[0-9a-f]{8}", ident):
                thread = d.stores.threads.get(ident)
        except (OSError, ValueError):
            pass
        if thread is not None:
            project = d.stores.projects.get(thread.project_id)
            if (thread.archived or project is None or project.archived
                    or thread.role != Role.CHAT or thread.task_id is not None
                    or thread.provider != ProviderName.FAST):
                thread = None
        if thread is None:
            project = d.stores.projects.inbox()
            # Recover a creation interrupted before the reference was saved.
            thread = next((t for t in d.stores.threads.list(project_id=project.id)
                           if t.title == "Voice" and not t.archived
                           and t.role == Role.CHAT and t.task_id is None
                           and t.provider == ProviderName.FAST), None)
            if thread is None:
                thread = d.open_thread(project.id, "chat", "fast", {})
                d.stores.threads.set_title(thread.id, "Voice", "owner")
                # open_thread shares this object with the live session. Publish
                # the fixed title as well as saving it, as a HUD rename does.
                thread.title, thread.title_source = "Voice", "owner"
                from .projects import _title_changed
                _title_changed(d, thread)
            _write_bytes(path, (json.dumps(thread.id) + "\n").encode("ascii"))
        return thread

    def ask(self, text, on_delta, *, cancelled=lambda: False, timeout=180.0):
        from .daemon import DaemonError
        deadline = time.monotonic() + timeout

        def check():
            if cancelled():
                raise ConnectionError()
            if time.monotonic() >= deadline:
                raise TimeoutError()

        # Serialize Voice questions, including a previous agent's in-flight ask.
        # A disconnected waiter releases promptly; no daemon/link lock is held.
        while not self._lock.acquire(timeout=0.1):
            check()
        q = None
        try:
            check()
            thread = self._thread()
            check()
            q = self.daemon.bus.subscribe({"thread_id": thread.id})
            message = UserMessage(text, origin="owner", via="voice", desk=False, spoken=True)
            while True:
                check()
                try:
                    turn = self.daemon.send(thread.id, message)
                    break
                except DaemonError:
                    # `send` never steers; wait behind an existing turn, including
                    # one held for an approval on the HUD/phone. Other conflicts
                    # fail instead of hiding a broken session for three minutes.
                    state = self.daemon.thread_json(thread)
                    if not (state.get("running") or state.get("queued")):
                        raise
                    try:
                        q.get(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
                    except queue.Empty:
                        pass
            deltas, full = [], []
            while True:
                check()
                try:
                    event = q.get(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
                except queue.Empty:
                    continue
                if q.dropped:
                    raise RuntimeError()  # never silently return a partial stream
                if event.get("kind") == "shutdown":
                    raise ConnectionError()
                if event.get("turn_id") != turn:
                    continue
                kind, data = event.get("kind"), event.get("data") or {}
                if kind in ("text_delta", "text"):
                    piece = data.get("text")
                    if not isinstance(piece, str):
                        raise ValueError()
                    if kind == "text_delta":
                        deltas.append(piece)
                        on_delta(piece)
                    else:
                        full.append(piece)
                elif kind == "error" and data.get("fatal", True):
                    raise RuntimeError()
                elif kind == "turn_finished":
                    if data.get("stop") in ("error", "cancelled", "interrupted"):
                        raise RuntimeError()
                    return "".join(full or deltas)
        finally:
            if q is not None:
                self.daemon.bus.unsubscribe(q)
            self._lock.release()
