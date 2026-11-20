"""Deliver a paired Windows voice question to the driver or selected HUD chat."""
from __future__ import annotations

import json
import queue
import re
import threading
import time
import uuid

from .desk import SelectedVoiceMessage
from .model import utcnow
from .stores import StoreError, _write_bytes
from .thread_model import is_chat


class NoVoiceTarget(Exception):
    pass


DRIVER_STILL_WORKING = "Still working. Check the driver chat."


class VoiceChat:
    def __init__(self, daemon):
        self.daemon = daemon
        self._selection_lock = threading.Lock()
        self._ask_lock = threading.Lock()
        self._playback_lock = threading.Lock()
        self._message = None
        self._turn = None

    def _eligible_thread(self, ident):
        if not isinstance(ident, str) or not re.fullmatch("[0-9a-f]{8}", ident):
            return None
        stores = self.daemon.stores
        try:
            thread = stores.threads.get(ident)
            if thread is None or thread.archived or not is_chat(thread):
                return None
            project = stores.projects.get(thread.project_id)
            return thread if project is not None and not project.archived else None
        except (StoreError, ValueError, OSError):
            return None

    @property
    def _path(self):
        # Never implicitly adopt a previous version's separate Voice conversation.
        return self.daemon.stores.root / "voice-target.json"

    def select(self, ident):
        with self._selection_lock:
            target = ident if self._eligible_thread(ident) else None
            _write_bytes(self._path, (json.dumps(target) + "\n").encode("ascii"))
            return {"thread_id": target}

    def _selected(self):
        with self._selection_lock:
            try:
                ident = json.loads(self._path.read_text(encoding="ascii"))
            except (OSError, ValueError):
                raise NoVoiceTarget() from None
            if not self._eligible_thread(ident):
                if ident is not None:
                    _write_bytes(self._path, b"null\n")
                raise NoVoiceTarget()
            return ident

    def selected(self):
        """Snapshot the chat for a local Windows shortcut, which can also run by itself."""
        try:
            return self._selected()
        except (NoVoiceTarget, OSError, StoreError):
            return None

    def note_action(self, thread_id, kind, target):
        """Record a successful native action without sending a model turn."""
        if kind in ("open_app", "switch_app"):
            label = target if re.fullmatch(r"[\w .+#&'()-]{1,80}", target) else "the app"
            verb = "opened" if kind == "open_app" else "switched to"
            text = f"Jarvis voice {verb} {label}."
        elif kind == "search":
            text = "Jarvis voice opened a web search."
        elif kind == "navigate":
            text = "Jarvis voice sent a website to the browser."
        elif kind == "read_selection":
            text = "Jarvis voice read the selected text aloud."
        else:
            return False
        with self.daemon._lock:
            # The grant can start after the link receives the receipt.
            if self.daemon.desktop_driver.thread_id() is not None:
                return False
            thread = self._eligible_thread(thread_id)
            if not thread:
                return False
            record = {"kind": "notice", "at": utcnow(), "event_id": uuid.uuid4().hex,
                      "thread_id": thread.id, "project_id": thread.project_id,
                      "data": {"text": text}}
            self.daemon.stores.threads._append(thread.id, "log.jsonl", record)
            self.daemon.bus.publish(record)
        return True

    def owns(self, message):
        with self._playback_lock:
            return message is self._message and message is not None

    def assign(self, message, thread_id, turn_id):
        # Called under the daemon lock before start/steer or requeue. Matching
        # the actual object prevents a different via="voice" caller claiming it.
        with self._playback_lock:
            if message is self._message and message is not None:
                self._turn = (thread_id, turn_id) if turn_id else None

    def speaks(self, thread_id, turn_id):
        with self._playback_lock:
            return turn_id is not None and self._turn == (thread_id, turn_id)

    def ask(self, text, on_delta, *, cancelled=lambda: False, timeout=180.0, on_reset=None):
        deadline = time.monotonic() + timeout

        def check():
            if cancelled():
                raise ConnectionError()
            if time.monotonic() >= deadline:
                raise TimeoutError()

        while not self._ask_lock.acquire(timeout=0.1):
            check()
        q = None
        try:
            check()
            driver = self.daemon.desktop_driver
            thread_id = driver.thread_id()
            grant = driver.grant(thread_id)
            if grant is None:
                thread_id = self._selected()
            if not self._eligible_thread(thread_id):
                raise NoVoiceTarget()
            q = self.daemon.bus.subscribe({"thread_id": thread_id})
            message = SelectedVoiceMessage(text)
            with self._playback_lock:
                self._message = message
            try:
                result = self.daemon.deliver(thread_id, message)
            except Exception:
                if not self._eligible_thread(thread_id):
                    raise NoVoiceTarget() from None
                raise
            if result["status"] == "dropped":
                raise RuntimeError("voice message was stopped")
            turn = result.get("queued_turn_id") or result["turn_id"]
            message_id = result.get("message_id")
            steered = result.get("mode") == "native"
            deltas, full = [], []
            proposing = False
            receipt_deadline = None
            while True:
                try:
                    check()
                except TimeoutError:
                    if grant is None or driver.grant(thread_id) is not grant:
                        raise
                    # Playback is a subscriber. Ending it leaves the delivered
                    # turn (or queued message) under the chat's own controls.
                    if on_reset:
                        on_reset()
                    return DRIVER_STILL_WORKING
                if receipt_deadline is not None and time.monotonic() >= receipt_deadline:
                    return "I couldn't confirm that task was opened."
                try:
                    event = q.get(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
                except queue.Empty:
                    continue
                if q.dropped:
                    raise RuntimeError()  # never silently return a partial stream
                if event.get("kind") == "shutdown":
                    raise ConnectionError()
                data = event.get("data") or {}
                if (event.get("kind") == "proposal_reply" and data.get("turn_id") == turn
                        and (proposing or receipt_deadline is not None)):
                    if on_reset:
                        on_reset()
                    if re.fullmatch(r"[0-9a-f]{8}", str(data.get("task_id") or "")):
                        return "I opened a task for that."
                    return data.get("reply") or "I couldn't open that task."
                kind = event.get("kind")
                if kind == "queue_cleared" and message_id and any(
                    row.get("message_id") == message_id for row in data.get("messages", ())
                ):
                    raise RuntimeError("voice message was stopped")
                if kind == "steer_queued" and message_id and data.get("message_id") == message_id:
                    turn = event["turn_id"]
                    steered = proposing = False
                    deltas.clear()
                    full.clear()
                    continue
                if event.get("turn_id") != turn or not event.get("native_voice_reply"):
                    continue
                if kind == "tool_started":
                    deltas.clear()
                    full.clear()
                    if on_reset:
                        on_reset()
                    if data.get("name") == "task_propose":
                        proposing = True
                if kind in ("text_delta", "text"):
                    piece = data.get("text")
                    if not isinstance(piece, str):
                        raise ValueError()
                    if kind == "text_delta":
                        deltas.append(piece)
                        if not proposing and not steered:
                            on_delta(piece)
                    else:
                        full.append(piece)
                elif kind == "error" and data.get("fatal", True):
                    raise RuntimeError()
                elif kind == "turn_finished":
                    if data.get("stop") in ("error", "cancelled", "interrupted"):
                        raise RuntimeError()
                    if data.get("proposal"):
                        receipt_deadline = min(deadline, time.monotonic() + 5)
                    else:
                        # A live steer can be returned by the provider and run
                        # later. Speak only after that turn actually accepted it.
                        if steered and full:
                            return full[-1]
                        return "".join(full or deltas)
        finally:
            with self._playback_lock:
                self._message = self._turn = None
            if q is not None:
                self.daemon.bus.unsubscribe(q)
            self._ask_lock.release()
