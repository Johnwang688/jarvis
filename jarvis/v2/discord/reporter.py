"""The update poster (design §11.3, plan §1 "PR A"): one bus subscriber that
turns the runner's task snapshots into Discord posts.

Runner contract: task_status_changed (or task_updated, task_created) uses the
daemon's shape: {kind, task_id, project_id, data: to_json(task)};
task_question carries {data: {questions, task}}. Snapshots preserve
intermediate transitions even when disk has advanced. Only runner lifecycle
records are consumed, never provider prose or events.

**Where a task's posts go.**

* Its project has a channel: a thread in it, created at the first snapshot
  whose phase is not `intake` (decisions D6, so a proposal withdrawn inside its
  grace window never makes one), auto-archiving after a week idle, with the
  owner added. "Started", then one status card edited in place, then the
  milestones.
* Its project has no channel yet (every project, until B1 links them), or the
  channel or thread is broken (Discord 10003 unknown channel, 50001/50013
  missing access or permissions): the **attention** milestones — question,
  blocked, failed, done with its report — go to the owner's DM, prefixed
  `[<project> · task <id>]`. The DM is the safety net, never the main road
  (decisions O1). A permission failure also DMs the owner once per channel
  per 24 h, so the cause is not only on the HUD light.

**What notifies (decisions D1).** In a guild, every post carries
SUPPRESS_NOTIFICATIONS — the card, its edits, Started, Verified, Cancelled and
the milestone text itself. A milestone that needs the owner (question,
blocked, failed, done) is followed by a separate one-line `<@owner>` post, the
only thing that ever pings. A DM notifies by itself, so a DM gets neither.

**Restarts.** The sidecar `tasks/<id>/discord.json` holds the thread id, the
message ids, the last `phase` and `open_question` posted, the card's
`embed_sha` and `thread_gone`; the thread id is read from it first, so a
stale whole-task save elsewhere (bug 1) cannot cost a second thread. The
worker reconciles from disk before it takes its first event: a task this
poster has never seen is **seeded silently** — its current phase recorded,
nothing posted to a DM — and only an active one in a project with a channel
gets a thread and a card; terminal tasks are never backfilled; a card whose
embed changed gets one edit. Creates are paced at one per second. The same
reconcile runs again when the bus drops events, after the circuit breaker
re-closes, and shortly after a transient failure, so a missed milestone is
posted late rather than never: a milestone is marked done only once it was
delivered, or once Discord said the place is gone.

**Failures.** Every REST failure is logged as the operation, the HTTP status
and Discord's code — never the URL, never a message body, never a token.
Three transient failures in a row (transport, 429 too long to sleep, 5xx)
open a breaker: 30 s, then 60 s, doubling up to 5 min, then a reconcile.
`status()` is what `GET /discord` shows and what a `discord_status` SSE event
carries on every change: ok, degraded (with a reason) or down.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import logging
import queue
import threading
import time

from ..model import TERMINAL_STATES, Task, TaskState, from_json
from ..stores import StoreError, _lock, _write_bytes
from .render import _cap, milestone, report_text, status_embed
from .rest import (MISSING_ACCESS, MISSING_PERMISSIONS, UNKNOWN_CHANNEL, describe)

LOG = logging.getLogger(__name__)
EDIT_INTERVAL = 5.0
CREATE_INTERVAL = 1.0           # thread creates, at most one a second
RETRY_AFTER_FAILURE = 5.0       # a transient failure is retried by a reconcile this much later
BREAKER_AFTER = 3
BACKOFF_S = (30.0, 60.0, 120.0, 240.0, 300.0)
FLUSH_S = 2.0
ALERT_EVERY_S = 24 * 3600.0
KINDS = frozenset({"task_created", "task_status_changed", "task_updated", "task_question"})
# Milestones that need the owner: followed by the ping line in a guild, and the
# only ones that go to the DM safety net.
ATTENTION = frozenset({"question", "blocked", "failed", "done"})
BROKEN = frozenset({UNKNOWN_CHANNEL, MISSING_ACCESS, MISSING_PERMISSIONS})
QUESTION_TAIL = " Answer here or with `/answer`."


def sidecar_path(stores, task_id):
    return stores.tasks.path(task_id).with_name("discord.json")


def read_sidecar(stores, task_id) -> dict:
    try:
        data = json.loads(sidecar_path(stores, task_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def thread_for(stores, task) -> str | None:
    """A task's Discord thread: the sidecar first, then the task record.

    None when the thread is known to be gone, so a caller falls back to the DM
    instead of posting into a deleted channel."""
    sidecar = read_sidecar(stores, task.id)
    if sidecar.get("thread_gone"):
        return None
    return sidecar.get("discord_thread_id") or task.discord_thread_id


def embed_sha(embed: dict) -> str:
    return hashlib.sha256(json.dumps(embed, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def _transient(exc) -> bool:
    status = getattr(exc, "status", None)
    return status is None or status == 429 or status >= 500


class Reporter:
    """Subscribes on construction; the worker reconciles, then takes events.

    REST is caller-owned. `dm_channel` is a callable returning the owner's DM
    channel id (the router's cached one); without it the safety net has
    nowhere to go and says so in its counters. close(flush=True) unsubscribes,
    drains what is queued and sends due card edits, bounded at 2 s.
    """

    def __init__(self, stores, rest, bus, *, dm_channel=None, reconcile: bool = True,
                 clock=time.monotonic, wall=time.time):
        self.stores, self.rest, self.bus = stores, rest, bus
        self._dm_channel = dm_channel
        self._clock, self._wall = clock, wall
        self.counters = Counter()
        self._status_lock = threading.Lock()
        self.subscription = bus.subscribe(lambda r: r.get("kind") in KINDS)
        self._stop = threading.Event()
        self._closing = False
        self._flush = False
        self._deadline = None
        self._pending = {}  # channel -> insertion-ordered task -> (message, embed, sha)
        self._due = {}
        self._last_edit = {}
        self._last_create = None
        self._dropped_seen = 0
        self._reconcile_at = self._clock() if reconcile else None
        self.reconciling = False
        # Health: the breaker, the channels Discord says are broken, the last error.
        self._failures = 0
        self._backoff = 0
        self._open_until = None
        self._broken: dict[str, int] = {}
        self._alerted: dict[str, float] = {}
        self.last_error = None
        self._published = ("ok", "")
        self._worker = threading.Thread(target=self._run, name="jarvis-discord-reporter",
                                        daemon=True)
        self._worker.start()

    # -- lifecycle ---------------------------------------------------------

    def close(self, flush: bool = False):
        if self._closing:
            return
        self._closing = True
        self._flush = flush
        self._deadline = self._clock() + FLUSH_S
        self.bus.unsubscribe(self.subscription)
        if not flush:
            self._stop.set()
        self.subscription.offer({"kind": "shutdown"})
        self._worker.join(timeout=FLUSH_S + 0.5)
        self._stop.set()

    def _late(self) -> bool:
        return self._stop.is_set() or (self._deadline is not None
                                       and self._clock() >= self._deadline)

    # -- health ------------------------------------------------------------

    def _count(self, key, n=1):
        with self._status_lock:
            self.counters[key] += n

    def breaker_open(self) -> bool:
        return self._open_until is not None and self._clock() < self._open_until

    def _state(self) -> tuple[str, str]:
        if self._open_until is not None:
            error = self.last_error or {}
            return "down", (f"Discord is failing ({error.get('op', '?')}: HTTP "
                            f"{error.get('status')}, code {error.get('code')}); "
                            "retrying with back-off")
        broken = dict(self._broken)         # status() reads this from the HTTP thread
        if broken:
            channel, code = next(iter(broken.items()))
            why = {UNKNOWN_CHANNEL: "is gone", MISSING_ACCESS: "is not visible to the bot",
                   MISSING_PERMISSIONS: "is missing a bot permission"}.get(code, "is broken")
            more = f" (+{len(broken) - 1} more)" if len(broken) > 1 else ""
            return "degraded", (f"channel {channel} {why} ({code}){more}; "
                                "attention updates go to your DM")
        if self._failures:
            error = self.last_error or {}
            return "degraded", (f"last {error.get('op', '?')} failed (HTTP "
                                f"{error.get('status')}, code {error.get('code')})")
        return "ok", ""

    def status(self) -> dict:
        """For `GET /discord` and the `discord_status` event: states, counts and
        times only — never a token, a URL or a message body."""
        state, reason = self._state()
        with self._status_lock:
            counters = dict(self.counters)
        return {"state": state, "reason": reason, "counters": counters,
                "dropped": self.subscription.dropped,
                "last_error": dict(self.last_error) if self.last_error else None}

    def _changed(self):
        now = self._state()
        if now != self._published:
            self._published = now
            self.bus.publish({"kind": "discord_status", "data": self.status()})

    def _failed(self, op, exc, channel=None):
        facts = describe(exc)
        self._count("errors")
        self.last_error = {"op": op, "status": facts["status"], "code": facts["code"],
                           "at": self._wall()}
        # Never log exception strings or tracebacks: injected transports and
        # Discord bodies may echo a URL or a credential.
        LOG.warning("Discord %s failed (HTTP %s, code %s, %s)", op, facts["status"],
                    facts["code"], facts["error"])
        code = facts["code"]
        if channel is not None and code in BROKEN:
            self._broken[str(channel)] = code
            if code == MISSING_PERMISSIONS:
                self._alert(channel, code)
        if _transient(exc):
            self._failures += 1
            if self._failures >= BREAKER_AFTER:
                delay = BACKOFF_S[min(self._backoff, len(BACKOFF_S) - 1)]
                self._backoff += 1
                self._open_until = self._clock() + delay
                self._reconcile_at = self._open_until
                # Pending edits are re-derived by the reconcile from embed_sha.
                self._pending.clear()
                self._due.clear()
                self._count("breaker_trips")
            else:
                self._schedule_reconcile(self._clock() + RETRY_AFTER_FAILURE)
        self._changed()

    def _ok(self, channel=None):
        self._failures = 0
        self._backoff = 0
        self._open_until = None
        if channel is not None:
            self._broken.pop(str(channel), None)
        self._changed()

    def _schedule_reconcile(self, at):
        if self._reconcile_at is None or at < self._reconcile_at:
            self._reconcile_at = at

    def _call(self, op, fn, *args, channel=None, **kwargs):
        """-> (ok, result, exception). The breaker short-circuits every call."""
        if self.breaker_open():
            return False, None, None
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            self._failed(op, exc, channel)
            return False, None, exc
        self._ok(channel)
        return True, result, None

    def _alert(self, channel, code):
        """One DM per broken channel per 24 h, so the owner learns why."""
        now = self._wall()
        last = self._alerted.get(str(channel))
        if last is not None and now - last < ALERT_EVERY_S:
            return
        self._alerted[str(channel)] = now
        dm = self._dm()
        if not dm:
            return
        try:
            self.rest.post(dm, content=(
                f"I can't post in <#{channel}>: the bot is missing a permission there "
                f"(Discord {code}). Task updates that need you come to this DM until it is "
                "fixed; the HUD's Discord light shows it too."))
            self._count("alerts")
        except Exception as exc:
            facts = describe(exc)
            LOG.warning("Discord permission alert failed (HTTP %s, code %s, %s)",
                        facts["status"], facts["code"], facts["error"])

    # -- posting -----------------------------------------------------------

    def _dm(self):
        if self._dm_channel is None:
            return None
        try:
            channel = self._dm_channel()
        except Exception as exc:
            self._failed("dm_channel", exc)
            return None
        return str(channel) if channel else None

    def _post(self, op, channel, *, ping=False, **kwargs):
        """A guild post: silent, plus the D1 ping line when it needs the owner.
        -> (ok, message id, exception)."""
        ok, message_id, exc = self._call(op, self.rest.post, channel, channel=channel,
                                         silent=True, **kwargs)
        if not ok:
            return False, None, exc
        self._count("posts")
        if ping:
            pinged, _, _ = self._call("ping_owner", self.rest.ping_owner, channel,
                                      channel=channel)
            if pinged:
                self._count("pings")
        return True, message_id, None

    def _to_dm(self, kind, content, files, task, project) -> bool:
        """The safety net. -> settled (delivered, or nothing will ever take it)."""
        if kind not in ATTENTION:
            return True
        dm = self._dm()
        if dm is None:
            if self._dm_channel is None:
                self._count("undelivered")
                return True
            return False
        prefix = f"[{_cap(project.name, 60)} · task {task.id}] "
        ok, _, _ = self._call(f"dm_{kind}", self.rest.post, dm, content=prefix + content,
                              files=files)
        if ok:
            self._count("dm")
        return ok

    def _milestone(self, kind, task, project, sidecar, thread, **ctx) -> bool:
        """Post one milestone where it belongs. -> settled."""
        content = milestone(kind, task, **ctx)
        files = ()
        if kind == "question":
            content = content + QUESTION_TAIL
        if kind == "done" and task.report:
            report = report_text(task.report)
            content += "\n" + report
            if report.overflow is not None:
                files = (("report.txt", report.overflow),)
        if thread:
            ok, _message, exc = self._post(kind, thread, content=content, files=files,
                                           ping=kind in ATTENTION)
            if ok:
                return True
            code = getattr(exc, "code", None)
            if exc is None or _transient(exc):
                return False                    # transient: a reconcile retries it
            if code not in BROKEN:
                # Discord refused this post for good (a 4xx about the post, not
                # the place): retrying it on every snapshot would never land.
                self._count("refused")
                return True
            if code == UNKNOWN_CHANNEL:
                self._gone(task, sidecar)
        return self._to_dm(kind, content, files, task, project)

    def _gone(self, task, sidecar):
        """Discord says this thread no longer exists. Never recreated: the task's
        attention updates use the DM from now on."""
        sidecar["thread_gone"] = True
        thread = sidecar.get("discord_thread_id")
        if thread:
            self._broken.pop(str(thread), None)
        self._count("threads_gone")
        self._save(task.id, sidecar)
        self._changed()

    # -- sidecar -----------------------------------------------------------

    def _save(self, task_id, sidecar):
        _write_bytes(sidecar_path(self.stores, task_id),
                     json.dumps(sidecar, sort_keys=True).encode("utf-8"))

    # -- the thread and its card ------------------------------------------

    def _pace(self):
        if self._last_create is not None:
            wait = self._last_create + CREATE_INTERVAL - self._clock()
            if wait > 0:
                self._stop.wait(wait)
        self._last_create = self._clock()

    def _thread(self, task, project, sidecar, *, create: bool):
        """-> (thread id or None, deferred). `deferred` means a create failed in
        a way worth retrying (transport, 429, 5xx, breaker open): the caller
        holds its milestones for the reconcile rather than sending them to the
        DM. A create Discord refused for good (10003/50001/50013) is not
        deferred — that is the safety net's case."""
        if sidecar.get("thread_gone"):
            return None, False
        current = self.stores.tasks.get(task.id)
        thread = sidecar.get("discord_thread_id") or (current.discord_thread_id if current else None)
        if thread:
            if sidecar.get("discord_thread_id") != thread:
                sidecar["discord_thread_id"] = thread
                self._save(task.id, sidecar)
            return thread, False
        if not create:
            return None, False
        self._pace()
        if self._stop.is_set():
            return None, True
        name = _cap(f"{task.id} · {' '.join(task.brief.split())}", 100)
        channel = project.discord_channel_id
        ok, thread, exc = self._call("create_thread", self.rest.create_thread, channel, name,
                                     channel=channel)
        if not ok:
            return None, exc is None or getattr(exc, "code", None) not in BROKEN
        self._count("threads")
        sidecar["discord_thread_id"] = thread
        self._save(task.id, sidecar)
        # Reload while holding the store lock; never overwrite newer runner status.
        with _lock:
            current = self.stores.tasks.get(task.id)
            if current is not None and not current.discord_thread_id:
                current.discord_thread_id = thread
                self.stores.tasks.save(current)
        self._call("add_owner", self.rest.add_owner, thread, channel=thread)
        return thread, False

    def _card(self, task, project, sidecar, thread):
        embed = status_embed(task, project)
        sha = embed_sha(embed)
        message_id = sidecar.get("discord_status_message_id")
        if not message_id:
            ok, message_id, exc = self._post("post_card", thread, embed=embed)
            if ok:
                sidecar["discord_status_message_id"] = message_id
                sidecar["embed_sha"] = sha
                self._save(task.id, sidecar)
            elif getattr(exc, "code", None) == UNKNOWN_CHANNEL:
                self._gone(task, sidecar)
            return
        if sidecar.get("embed_sha") == sha:
            self._drop_pending(thread, task.id)
            return
        pending = self._pending.setdefault(thread, {})
        pending[task.id] = (message_id, embed, sha)
        self._due.setdefault(thread, max(self._clock() + EDIT_INTERVAL,
                                         self._last_edit.get(thread, 0) + EDIT_INTERVAL))

    def _drop_pending(self, thread, task_id):
        pending = self._pending.get(thread)
        if pending and task_id in pending:
            del pending[task_id]
            if not pending:
                self._pending.pop(thread, None)
                self._due.pop(thread, None)

    # -- one task ----------------------------------------------------------

    def _deliver(self, task: Task, project, sidecar: dict, *, reconcile=False, questions=None):
        phase = task.status.phase
        channelled = bool(project.discord_channel_id)
        if reconcile and "phase" not in sidecar:
            # Never seen by this poster: record where it stands, post nothing
            # to the DM. An active task with a channel gets its thread and card.
            # A task still in intake is not under way: it gets its "Started"
            # like any other when it starts.
            sidecar.update(phase=phase.value, open_question=task.status.open_question)
            if phase != TaskState.INTAKE:
                sidecar["seeded"] = True
            self._save(task.id, sidecar)
            if channelled and phase not in TERMINAL_STATES and phase != TaskState.INTAKE:
                thread, _ = self._thread(task, project, sidecar, create=True)
                if thread:
                    self._card(task, project, sidecar, thread)
            return
        thread, deferred = None, False
        if channelled and phase != TaskState.INTAKE:
            thread, deferred = self._thread(task, project, sidecar,
                                            create=phase not in TERMINAL_STATES)
            if thread and not sidecar.get("seeded") and not sidecar.get("started_message_id"):
                ok, message_id, exc = self._post("started", thread,
                                                 content=milestone("started", task))
                if ok:
                    sidecar["started_message_id"] = message_id
                    self._save(task.id, sidecar)
                elif getattr(exc, "code", None) == UNKNOWN_CHANNEL:
                    self._gone(task, sidecar)
            if thread and not sidecar.get("thread_gone"):
                self._card(task, project, sidecar, thread)
            if sidecar.get("thread_gone"):
                thread = None
        if deferred:
            # The thread could not be made for a reason worth retrying: hold
            # every milestone for the reconcile rather than spill it to the DM.
            self._save(task.id, sidecar)
            return
        previous = sidecar.get("phase")
        if previous != phase.value:
            # Kinds already delivered for this transition, so a retry after a
            # partial failure (Verified sent, Done not) never posts one twice.
            delivered = set(sidecar.get("partial") or []) if sidecar.get(
                "partial_for") == phase.value else set()

            def send(kind, **ctx):
                if kind in delivered:
                    return True
                where = None if sidecar.get("thread_gone") else thread
                if not self._milestone(kind, task, project, sidecar, where, **ctx):
                    return False
                delivered.add(kind)
                sidecar.update(partial_for=phase.value, partial=sorted(delivered))
                return True

            settled = True
            if phase == TaskState.BLOCKED:
                settled = send("blocked")
            elif phase == TaskState.FAILED:
                settled = send("failed")
            elif phase == TaskState.CANCELLED:
                # Quiet, and only where the task has a thread: a cancel never
                # reaches the DM (decisions D8).
                settled = send("cancelled") if thread else True
            elif phase == TaskState.DONE:
                if previous == TaskState.VERIFYING.value and thread:
                    settled = send("verified")
                settled = settled and send("done")
            if settled:
                sidecar["phase"] = phase.value
                sidecar.pop("partial", None)
                sidecar.pop("partial_for", None)
        question = task.status.open_question
        if phase == TaskState.CLARIFYING and question and question != sidecar.get("open_question"):
            options = next((q.options for q in task.spec.questions
                            if q.text == question and q.answer is None), None)
            if options is None and questions:
                options = next((q.get("options") or [] for q in questions
                                if isinstance(q, dict) and q.get("text") == question), [])
            if self._milestone("question", task, project, sidecar,
                               None if sidecar.get("thread_gone") else thread,
                               text=question, options=list(options or [])):
                sidecar["open_question"] = question
        elif not question:
            sidecar["open_question"] = None
        self._save(task.id, sidecar)

    def _handle(self, record):
        task_id = record["task_id"]
        current = self.stores.tasks.get(task_id)
        if current is None:
            return
        data = record.get("data") or {}
        questions = None
        if record.get("kind") == "task_question":
            questions = data.get("questions")
            data = data.get("task") or {}
        task = from_json(Task, data) if isinstance(data, dict) and "id" in data else current
        if task.id != current.id or task.project_id != current.project_id:
            raise ValueError("task snapshot identity mismatch")
        project = self.stores.projects.get(current.project_id)
        if project is None:
            self._count("skipped")
            return
        self._deliver(task, project, read_sidecar(self.stores, task_id), questions=questions)

    def _reconcile(self):
        self.reconciling = True
        try:
            self._reconcile_tasks()
        finally:
            self.reconciling = False

    def _reconcile_tasks(self):
        self._reconcile_at = None
        self._count("reconciles")
        try:
            tasks = self.stores.tasks.list()
        except StoreError as exc:
            LOG.warning("Discord reconcile could not list tasks (%s)", type(exc).__name__)
            return
        for task in tasks:
            if self._stop.is_set() or self._closing or self.breaker_open():
                if self.breaker_open():
                    self._reconcile_at = self._open_until
                return
            try:
                project = self.stores.projects.get(task.project_id)
                if project is None:
                    continue
                sidecar = read_sidecar(self.stores, task.id)
                if task.state in TERMINAL_STATES and "phase" not in sidecar:
                    # Never backfilled: record it so it stays quiet for good.
                    sidecar.update(seeded=True, phase=task.status.phase.value,
                                   open_question=task.status.open_question)
                    self._save(task.id, sidecar)
                    continue
                self._deliver(task, project, sidecar, reconcile=True)
            except Exception as exc:
                LOG.warning("Discord reconcile of a task failed (%s)", type(exc).__name__)
        if self._open_until is not None and not self.breaker_open():
            # A whole pass with the breaker shut: it is closed again, whether
            # or not the pass had anything to send.
            self._open_until = None
            self._changed()

    # -- card edits --------------------------------------------------------

    def _flush_due(self, force=False):
        for channel in list(self._due):
            if self._stop.is_set() or self.breaker_open():
                return
            if force and self._late():
                return
            if not force and self._clock() < self._due[channel]:
                continue
            pending = self._pending[channel]
            task_id = next(iter(pending))
            message_id, embed, sha = pending.pop(task_id)
            ok, _, exc = self._call("edit_card", self.rest.edit, channel, message_id,
                                    embed=embed, channel=channel)
            self._last_edit[channel] = self._clock()
            if ok:
                self._count("edits")
                sidecar = read_sidecar(self.stores, task_id)
                sidecar["embed_sha"] = sha
                self._save(task_id, sidecar)
            elif getattr(exc, "code", None) == UNKNOWN_CHANNEL:
                self._gone(self.stores.tasks.get(task_id) or Task(task_id, "", ""),
                           read_sidecar(self.stores, task_id))
            if channel not in self._pending:
                continue
            if pending:
                self._due[channel] = self._last_edit[channel] + EDIT_INTERVAL
            else:
                self._due.pop(channel, None)
                self._pending.pop(channel, None)

    # -- the worker --------------------------------------------------------

    def _timeout(self):
        times = list(self._due.values())
        if self._reconcile_at is not None and not self._closing:
            times.append(self._reconcile_at)
        if not times:
            return None
        return max(0.0, min(times) - self._clock())

    def _run(self):
        try:
            while not self._stop.is_set():
                if (self._reconcile_at is not None and self._clock() >= self._reconcile_at
                        and not self.breaker_open() and not self._closing):
                    self._reconcile()
                try:
                    record = self.subscription.get(timeout=self._timeout())
                except queue.Empty:
                    self._flush_due()
                    continue
                try:
                    if record["kind"] == "shutdown":
                        if self._flush:
                            self._flush_due(force=True)
                        break
                    dropped = self.subscription.dropped
                    if dropped > self._dropped_seen:
                        # Events were evicted: their state is on disk, so the
                        # reconcile, not the event stream, catches up.
                        self._dropped_seen = dropped
                        self._schedule_reconcile(self._clock())
                    if self._open_until is not None:
                        # The breaker tripped and has not closed: whatever this
                        # event says is on disk, and the post-back-off attempt
                        # is always a reconcile, never this one event.
                        self._schedule_reconcile(self._open_until)
                    elif self._closing and self._late():
                        pass
                    else:
                        self._handle(record)
                except Exception as exc:
                    self._count("errors")
                    LOG.warning("Discord reporter could not handle an event (%s)",
                                type(exc).__name__)
                finally:
                    self.subscription.task_done()
                self._flush_due()
        finally:
            self.bus.unsubscribe(self.subscription)
