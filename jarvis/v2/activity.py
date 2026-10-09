"""What each thread and task is doing, for the HUD sidebar (2026-10-08).

One status per chat thread, task thread and task, drawn as the dot beside it:

    working      a turn is running (a task: a phase that is not terminal)
    needs_input  blocked on the owner: an approval, an open question, a
                 BLOCKED task, or a CLARIFYING task with a blocking question.
                 Outranks working, because both arrive mid-turn.
    unread       a chat turn finished and the owner has not opened it since
                 (a task: DONE and not yet opened)
    failed       the same, for a turn that ended in an error (a task: FAILED)
    idle         everything else, including an interrupted turn and a
                 cancelled task — the owner stopped those themselves

Only chat threads and tasks are ever unread or failed. A task's own threads
finish turns all through a run, and the task row is where its outcome shows.

The tracker reads the records the daemon already publishes (an observer on
the bus, called synchronously inside `publish`, so nothing can be dropped the
way a full subscription drops), and publishes one `activity` record whenever a
status changes (its ids inside `data`, so no thread- or project-filtered
stream ever carries it). Running turns and open asks are in memory — a daemon restart
ends both. What was last finished and what the owner has seen is in a sidecar,
`threads/<id>/activity.json` and `tasks/<id>/activity.json`, so unread and
failed survive a restart. **No sidecar means idle**: everything that existed
before this shipped starts read, rather than every old thread turning blue.

The owner marks a thread or task read by opening it in the HUD
(`POST /threads/<id>/seen`, `POST /tasks/<id>/seen`); a Discord read does not
count (owner's call, 2026-10-08).
"""
from __future__ import annotations

import json
import logging
import threading

from .model import TERMINAL_STATES, TaskState
from .stores import StoreError, _write_bytes

LOG = logging.getLogger(__name__)

WORKING, NEEDS_INPUT, UNREAD, FAILED, IDLE = "working", "needs_input", "unread", "failed", "idle"

_THREAD_KINDS = frozenset({"user_message", "turn_started", "turn_finished", "approval_requested",
                           "approval_resolved", "question", "question_answered"})
_ACTIVE_TASK = frozenset({TaskState.INTAKE, TaskState.CLARIFYING, TaskState.PLANNED,
                          TaskState.RUNNING, TaskState.VERIFYING})


class Activity:
    def __init__(self, stores, publish):
        self.stores = stores
        self._publish = publish
        self._lock = threading.RLock()
        self._running: set[str] = set()
        self._asks: dict[str, set[str]] = {}        # thread id -> open approval/question ids
        self._meta: dict[str, tuple] = {}           # thread id -> (role, task_id, project_id)
        self._shown: dict[tuple[str, str], str] = {}  # last status published per (kind, id)

    # -- reading ------------------------------------------------------------

    def _sidecar(self, store, object_id) -> dict:
        try:
            path = store.path(object_id).with_name("activity.json")
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError, UnicodeError, StoreError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, store, object_id, data: dict) -> None:
        try:
            _write_bytes(store.path(object_id).with_name("activity.json"),
                         json.dumps(data, sort_keys=True).encode())
        except StoreError:
            LOG.warning("Cannot write activity for %s", object_id, exc_info=True)

    def _thread_meta(self, thread_id):
        meta = self._meta.get(thread_id)
        if meta is None:
            try:
                thread = self.stores.threads.get(thread_id)
            except StoreError:
                thread = None
            if thread is None:
                return None
            role = getattr(thread.role, "value", thread.role)
            meta = self._meta[thread_id] = (role, thread.task_id, thread.project_id)
        return meta

    def thread_status(self, thread_id: str) -> str:
        with self._lock:
            if self._asks.get(thread_id):
                return NEEDS_INPUT
            if thread_id in self._running:
                return WORKING
            meta = self._thread_meta(thread_id)
            if meta is None or meta[0] != "chat":
                return IDLE
            side = self._sidecar(self.stores.threads, thread_id)
            last = side.get("last")
            if not isinstance(last, dict) or not last.get("turn_id") or last["turn_id"] == side.get("seen"):
                return IDLE
            stop = last.get("stop")
            return FAILED if stop == "error" else IDLE if stop == "interrupted" else UNREAD

    def task_status(self, task, *, side: dict | None = None) -> str:
        with self._lock:
            state = TaskState(getattr(task.state, "value", task.state))
            asking = any(self._asks.get(tid) for tid, meta in self._meta.items()
                         if meta[1] == task.id)
            if asking or state == TaskState.BLOCKED or (
                    state == TaskState.CLARIFYING and task.spec.blocked_on()):
                return NEEDS_INPUT
            if state in _ACTIVE_TASK:
                return WORKING
            if state in (TaskState.DONE, TaskState.FAILED):
                side = self._sidecar(self.stores.tasks, task.id) if side is None else side
                if side.get("terminal") == state.value and not side.get("seen"):
                    return FAILED if state == TaskState.FAILED else UNREAD
            return IDLE

    def snapshot(self) -> dict:
        """`GET /activity`: every thread and task that is not idle."""
        threads, tasks = {}, {}
        for thread_id in self.stores.threads.ids():
            status = self.thread_status(thread_id)
            if status != IDLE:
                threads[thread_id] = status
        for task_id in self.stores.tasks.ids():
            try:
                task = self.stores.tasks.get(task_id)
            except StoreError:
                continue
            if task is not None:
                status = self.task_status(task)
                if status != IDLE:
                    tasks[task_id] = status
        return {"threads": threads, "tasks": tasks}

    # -- the owner reading --------------------------------------------------

    def seen_thread(self, thread_id: str) -> str:
        with self._lock:
            side = self._sidecar(self.stores.threads, thread_id)
            last = side.get("last")
            if isinstance(last, dict) and last.get("turn_id") and side.get("seen") != last["turn_id"]:
                side["seen"] = last["turn_id"]
                self._write(self.stores.threads, thread_id, side)
            self._changed_thread(thread_id)
            return self.thread_status(thread_id)

    def seen_task(self, task) -> str:
        with self._lock:
            side = self._sidecar(self.stores.tasks, task.id)
            if side.get("terminal") and not side.get("seen"):
                side["seen"] = True
                self._write(self.stores.tasks, task.id, side)
            self._changed_task(task)
            return self.task_status(task)

    # -- the bus ------------------------------------------------------------

    def observe(self, record: dict) -> None:
        """Called inside `EventBus.publish`. Never raises into the publisher."""
        try:
            kind = record.get("kind")
            if kind == "task_status_changed":
                self._task_record(record)
            elif kind in _THREAD_KINDS and record.get("thread_id"):
                self._thread_record(kind, record)
        except Exception:
            LOG.exception("activity: cannot follow a %s record", record.get("kind"))

    def _thread_record(self, kind, record):
        thread_id = record["thread_id"]
        data = record.get("data") or {}
        with self._lock:
            if kind in ("user_message", "turn_started"):
                self._running.add(thread_id)
            elif kind == "turn_finished":
                self._running.discard(thread_id)
                # Whatever was still open belonged to the turn that just ended.
                self._asks.pop(thread_id, None)
                meta = self._thread_meta(thread_id)
                if meta is not None and meta[0] == "chat" and record.get("turn_id"):
                    side = self._sidecar(self.stores.threads, thread_id)
                    side["last"] = {"turn_id": record["turn_id"], "stop": data.get("stop") or "end",
                                    "at": record.get("at")}
                    self._write(self.stores.threads, thread_id, side)
            elif kind in ("approval_requested", "question"):
                # A record with no broker code is the gate being consulted,
                # never a question for the owner (CLAUDE.md, 2026-10-08).
                if kind == "approval_requested" and not data.get("code"):
                    return
                if data.get("req_id"):
                    self._asks.setdefault(thread_id, set()).add(str(data["req_id"]))
            elif kind in ("approval_resolved", "question_answered"):
                asks = self._asks.get(thread_id)
                if asks is not None:
                    asks.discard(str(data.get("req_id")))
                    if not asks:
                        self._asks.pop(thread_id, None)
            self._changed_thread(thread_id)
            meta = self._thread_meta(thread_id)
            if meta is not None and meta[1] and kind in ("approval_requested", "approval_resolved",
                                                         "question", "question_answered", "turn_finished"):
                try:
                    task = self.stores.tasks.get(meta[1])
                except StoreError:
                    task = None
                if task is not None:
                    self._changed_task(task)

    def _task_record(self, record):
        task_id = record.get("task_id")
        if not task_id:
            return
        with self._lock:
            try:
                task = self.stores.tasks.get(task_id)
            except StoreError:
                return
            if task is None:
                return
            state = TaskState(getattr(task.state, "value", task.state))
            if state in TERMINAL_STATES:
                side = self._sidecar(self.stores.tasks, task_id)
                if side.get("terminal") != state.value:
                    # The first time this task is seen terminal: unread now.
                    self._write(self.stores.tasks, task_id, {"terminal": state.value, "seen": False})
            self._changed_task(task)

    def _changed_thread(self, thread_id):
        meta = self._thread_meta(thread_id)
        self._emit("thread", thread_id, meta[2] if meta else None, self.thread_status(thread_id))

    def _changed_task(self, task):
        self._emit("task", task.id, task.project_id, self.task_status(task))

    def _emit(self, kind, object_id, project_id, status):
        key = (kind, object_id)
        # No default: after a restart nothing has been published yet, and the
        # HUD may hold a status from `GET /activity` that this change ends.
        if self._shown.get(key) == status:
            return
        self._shown[key] = status
        # The ids ride in `data`, never at the top level: every stream
        # filtered by thread or project (the runner's turn wait, `?thread=`)
        # keeps exactly the records it had before this kind existed.
        self._publish({"kind": "activity",
                       "data": {"of": kind, "id": object_id, "project_id": project_id, "status": status}})
