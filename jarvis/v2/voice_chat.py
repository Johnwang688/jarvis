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


IDLE_REFUSAL_S = 0.5


class VoiceChat:
    def __init__(self, daemon):
        self.daemon = daemon
        self._lock = threading.Lock()

    def _thread(self):
        d = self.daemon
        path = d.stores.root / "voice-chat.json"
        project = d.stores.projects.inbox()

        def is_voice(thread):
            return (thread is not None and thread.title == "Voice"
                    and thread.project_id == project.id and not project.archived
                    and not thread.archived and thread.role == Role.CHAT
                    and thread.task_id is None and thread.provider == ProviderName.FAST)

        thread = None
        try:
            ident = json.loads(path.read_text(encoding="ascii"))
            if isinstance(ident, str) and re.fullmatch(r"[0-9a-f]{8}", ident):
                thread = d.stores.threads.get(ident)
        except (OSError, ValueError):
            pass
        if not is_voice(thread):
            # Recover a creation interrupted before the reference was saved.
            thread = next((t for t in d.stores.threads.list(project_id=project.id)
                           if is_voice(t)), None)
            if thread is None:
                thread = self._create(project)
                d.stores.threads.set_title(thread.id, "Voice", "owner")
                # The thread shares this object with the live session. Publish
                # the fixed title as well as saving it, as a HUD rename does.
                thread.title, thread.title_source = "Voice", "owner"
                from .projects import _title_changed
                _title_changed(d, thread)
            _write_bytes(path, (json.dumps(thread.id) + "\n").encode("ascii"))
        return thread

    def _create(self, project):
        # Titled at creation: its first metadata write identifies it as Voice
        # even if the process dies before set_title or the id file.
        return self.daemon.open_thread(project.id, "chat", "fast", {}, title="Voice")

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
            idle_since = None
            while True:
                check()
                try:
                    turn = self.daemon.send(thread.id, message)
                    break
                except DaemonError:
                    # `send` never steers; wait behind an existing turn, including
                    # one held for an approval on the HUD/phone. Other conflicts
                    # fail instead of hiding a broken session for three minutes.
                    # Between one turn finishing and a queued message's turn
                    # starting the thread can look idle for a moment while
                    # `send` still refuses: only a refusal that stays idle for
                    # IDLE_REFUSAL_S is a real conflict.
                    state = self.daemon.thread_json(thread)
                    if state.get("running") or state.get("queued"):
                        idle_since = None
                    else:
                        now = time.monotonic()
                        idle_since = now if idle_since is None else idle_since
                        if now - idle_since >= IDLE_REFUSAL_S:
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
