"""One background subscriber, with per-channel timed status edits.

Runner contract: task_status_changed (or task_updated) uses the daemon's
task_created shape: {kind, task_id, project_id, data: to_json(task)}.
Snapshots preserve intermediate transitions even when disk has advanced.
Only runner lifecycle records are consumed, never provider prose/events.
"""
from __future__ import annotations

from collections import Counter
import json
import logging
import queue
import threading
import time

from ..model import Task, TaskState, from_json
from ..stores import _lock, _write_bytes
from .render import _cap, milestone, report_text, status_embed

LOG = logging.getLogger(__name__)
EDIT_INTERVAL = 5.0
KINDS = frozenset({"task_created", "task_status_changed", "task_updated"})


class Reporter:
    """Starts on construction; close() unsubscribes and bounds its worker join.

    REST is caller-owned. counters: skipped (events without project channels),
    errors, threads, posts, edits. subscription.dropped exposes bus overflow.
    Pending edits are discarded on shutdown; later task snapshots refresh them.
    """

    def __init__(self, stores, rest, bus):
        self.stores, self.rest, self.bus = stores, rest, bus
        self.counters = Counter()
        self.subscription = bus.subscribe(lambda r: r.get("kind") in KINDS)
        self._stop = threading.Event()
        self._pending = {}  # channel -> insertion-ordered task -> (message, embed)
        self._due = {}
        self._last_edit = {}
        self._embeds = {}
        self._worker = threading.Thread(target=self._run, name="jarvis-discord-reporter", daemon=True)
        self._worker.start()

    def close(self):
        if self._stop.is_set():
            return
        self.bus.unsubscribe(self.subscription)
        self._stop.set()
        self.subscription.offer({"kind": "shutdown"})
        self._worker.join(timeout=2)

    def _error(self, exc):
        self.counters["errors"] += 1
        # Never log exception strings/tracebacks: injected transports may echo auth.
        LOG.warning("Discord reporter operation failed (%s)", type(exc).__name__)

    def _call(self, operation, *args, **kwargs):
        try:
            result = operation(*args, **kwargs)
            return True, result
        except Exception as exc:
            self._error(exc)
            return False, None

    def _post(self, channel, **kwargs):
        ok, message_id = self._call(self.rest.post, channel, **kwargs)
        if ok:
            self.counters["posts"] += 1
        return message_id

    def _path(self, task_id):
        return self.stores.tasks.path(task_id).with_name("discord.json")

    def _save(self, task_id, sidecar):
        _write_bytes(self._path(task_id), json.dumps(sidecar).encode("utf-8"))

    def _handle(self, record):
        task_id = record["task_id"]
        current = self.stores.tasks.get(task_id)
        if current is None:
            return
        project = self.stores.projects.get(current.project_id)
        if project is None or not project.discord_channel_id:
            self.counters["skipped"] += 1
            return
        data = record.get("data", {})
        task = from_json(Task, data) if "id" in data else current
        if task.id != current.id or task.project_id != current.project_id:
            raise ValueError("task snapshot identity mismatch")
        try:
            sidecar = json.loads(self._path(task_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            sidecar = {}
        channel = current.discord_thread_id
        if channel is None:
            ok, channel = self._call(self.rest.create_thread, project.discord_channel_id,
                                     _cap(f"{task.id} · {task.brief}", 100))
            if not ok:
                return
            self.counters["threads"] += 1
            # Reload while holding the store lock; never overwrite newer runner status.
            with _lock:
                current = self.stores.tasks.get(task_id)
                if current is None:
                    return
                current.discord_thread_id = channel
                self.stores.tasks.save(current)
        if sidecar.get("discord_thread_id") != channel:
            sidecar = {"discord_thread_id": channel}
        if not sidecar.get("started_message_id"):
            message_id = self._post(channel, content=milestone("started", task))
            if message_id:
                sidecar["started_message_id"] = message_id
                self._save(task_id, sidecar)
        embed = status_embed(task, project)
        message_id = sidecar.get("discord_status_message_id")
        if not message_id:
            message_id = self._post(channel, embed=embed)
            if message_id:
                sidecar["discord_status_message_id"] = message_id
                self._embeds[task_id] = embed
                self._save(task_id, sidecar)
        elif self._embeds.get(task_id) != embed:
            pending = self._pending.setdefault(channel, {})
            pending[task_id] = (message_id, embed)
            self._due.setdefault(channel, max(time.monotonic() + EDIT_INTERVAL,
                                               self._last_edit.get(channel, 0) + EDIT_INTERVAL))
            self._embeds[task_id] = embed
        previous = sidecar.get("phase")
        phase = task.status.phase.value
        if previous != phase:
            if phase == TaskState.BLOCKED.value:
                self._post(channel, content=milestone("blocked", task))
            elif phase == TaskState.FAILED.value:
                self._post(channel, content=milestone("failed", task))
            elif phase == TaskState.DONE.value:
                if previous == TaskState.VERIFYING.value:
                    self._post(channel, content=milestone("verified", task))
                content = milestone("done", task)
                files = ()
                if task.report:
                    report = report_text(task.report)
                    content += "\n" + report
                    if report.overflow is not None:
                        files = (("report.txt", report.overflow),)
                self._post(channel, content=content, files=files)
        question = task.status.open_question
        if question and question != sidecar.get("open_question") and phase != TaskState.BLOCKED.value:
            self._post(channel, content=milestone("question", task))
        sidecar.update(phase=phase, open_question=question)
        self._save(task_id, sidecar)

    def _flush_due(self):
        for channel in list(self._due):
            if time.monotonic() < self._due[channel] or self._stop.is_set():
                continue
            pending = self._pending[channel]
            task_id = next(iter(pending))
            message_id, embed = pending.pop(task_id)
            # Editing does not auto-unarchive; posting milestones does.
            ok, _ = self._call(self.rest.unarchive, channel)
            if ok:
                ok, _ = self._call(self.rest.edit, channel, message_id, embed=embed)
            self._last_edit[channel] = time.monotonic()
            if ok:
                self.counters["edits"] += 1
            else:
                # Permit the next identical runner snapshot to retry this state.
                self._embeds.pop(task_id, None)
            if pending:
                self._due[channel] = self._last_edit[channel] + EDIT_INTERVAL
            else:
                del self._due[channel]
                del self._pending[channel]

    def _run(self):
        try:
            while not self._stop.is_set():
                timeout = max(0, min(self._due.values()) - time.monotonic()) if self._due else None
                try:
                    record = self.subscription.get(timeout=timeout)
                except queue.Empty:
                    self._flush_due()
                    continue
                try:
                    if record["kind"] == "shutdown":
                        break
                    self._handle(record)
                except Exception as exc:
                    self._error(exc)
                finally:
                    self.subscription.task_done()
                self._flush_due()
        finally:
            self.bus.unsubscribe(self.subscription)
