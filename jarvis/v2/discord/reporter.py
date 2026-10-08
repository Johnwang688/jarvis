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
* Its project has no channel (B1 links them; until then every project), or the
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
**Linking (B1).** When the channel linker links a project (`project_updated`
with `changed: ["discord_channel_id"]`), that project's tasks are reconciled
at once: an active or blocked task gets its thread and card in the new
channel, and one whose old thread was deleted gets a fresh one — the DM is
then the safety net only if that channel breaks too. A live task whose thread
is in **another** channel (the project was unlinked and linked elsewhere)
gets a new thread in the new one, "Continued from <#old>", and the old thread
— kept, never deleted — says "Moved to <#new>" (O-C5, applied to tasks).

**Never backwards.** The sidecar's `seen_updated` is the newest
`Task.updated` delivered; an event snapshot older than that is skipped, so a
reconcile that ran ahead of queued snapshots cannot be undone by them.

`status()` is what `GET /discord` shows and what a `discord_status` SSE event
carries on every change: ok, degraded (with a reason) or down.
"""
from __future__ import annotations

from collections import Counter
import copy
import hashlib
import json
import logging
import queue
import threading
import time

from jarvis.tools.secrets import scrub

from ..model import TERMINAL_STATES, Task, TaskState, from_json, utcnow
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
BROKEN_TTL_S = 3600.0           # a broken-place entry ages out; a new failure re-adds it
LIST_BACKOFF_MAX_S = 300.0
KINDS = frozenset({"task_created", "task_status_changed", "task_updated", "task_question"})
# B1: a project gaining (or losing) its channel. Only the linker's own
# `changed: ["discord_channel_id"]` events are taken; every other project edit
# is ignored here.
LINK_KIND = "project_updated"
# Milestones that need the owner: followed by the ping line in a guild, and the
# only ones that go to the DM safety net.
ATTENTION = frozenset({"question", "blocked", "failed", "done"})
BROKEN = frozenset({UNKNOWN_CHANNEL, MISSING_ACCESS, MISSING_PERMISSIONS})
# The two that mean "the bot is not allowed there": the owner is told by DM.
ALERT_CODES = frozenset({MISSING_ACCESS, MISSING_PERMISSIONS})
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


def _clean(text):
    """Everything this poster sends goes through the transcript scrub first:
    a credential value from a protected file never reaches Discord."""
    return scrub(text) if isinstance(text, str) and text else text


def _scrubbed(task: Task) -> Task:
    """A copy of the snapshot with every text field Discord could be shown
    scrubbed *before* rendering, so the render caps (400, 1500, 1024, 6000)
    still hold after a value is replaced by the longer redaction marker."""
    task = copy.deepcopy(task)
    task.brief = _clean(task.brief)
    status = task.status
    status.open_question = _clean(status.open_question)
    status.last_tool = _clean(status.last_tool)
    status.last_file = _clean(status.last_file)
    for question in task.spec.questions:
        question.text = _clean(question.text)
        question.options = [_clean(option) for option in question.options]
    if task.report is not None:
        report = task.report
        report.done = [_clean(x) for x in report.done]
        report.changed = [_clean(x) for x in report.changed]
        report.open = [_clean(x) for x in report.open]
        report.verified = _clean(report.verified)
        report.next = _clean(report.next)
    return task


def _created(task):
    return _stamp(task.created)


def _stamp(value):
    """An ISO stamp as a datetime, or None. `Task.updated` mixes seconds and
    microseconds precision, so stamps are compared parsed, never as text."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


class Reporter:
    """Subscribes on construction; the worker reconciles, then takes events.

    REST is caller-owned. `dm_channel` is a callable returning the owner's DM
    channel id (the router's cached one); without it the safety net has
    nowhere to go and says so in its counters. close(flush=True) unsubscribes,
    drains what is queued and sends due card edits, bounded at 2 s.
    """

    def __init__(self, stores, rest, bus, *, dm_channel=None, reconcile: bool = True,
                 clock=time.monotonic, wall=time.time, started_at: str | None = None):
        self.stores, self.rest, self.bus = stores, rest, bus
        # Tasks created before this stamp existed before the poster did, and
        # only those are seeded silently. One created after it — during an
        # outage, or while its events were dropped — is reported, late.
        # Seconds resolution, so a task from the same second counts as new:
        # posting late is the safe side, losing a question is not.
        self.started_at = started_at or utcnow()
        self._dm_channel = dm_channel
        self._clock, self._wall = clock, wall
        self.counters = Counter()
        self._status_lock = threading.Lock()
        self.subscription = bus.subscribe(
            lambda r: r.get("kind") in KINDS or (
                r.get("kind") == LINK_KIND
                and "discord_channel_id" in ((r.get("data") or {}).get("changed") or ())))
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
        self._broken: dict[str, tuple[int, float]] = {}   # channel -> (code, wall time)
        self._alerted: dict[str, float] = {}
        self._list_backoff = 0
        self._parent_unknown: set[str] = set()     # relinks whose old parent is unknown, warned
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
        now = self._wall()
        # status() reads this from the HTTP thread: copy, and skip aged entries.
        broken = {channel: code for channel, (code, at) in dict(self._broken).items()
                  if now - at < BROKEN_TTL_S}
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
        permanent_create = op == "create_thread" and not _transient(exc)
        if channel is not None and (code in BROKEN or permanent_create):
            self._broken[str(channel)] = (code or facts["status"] or 0, self._wall())
            if code in ALERT_CODES:
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
        dm = self._dm()
        if not dm:
            return                       # not stamped: the next failure tries again
        why = ("the bot cannot see it" if code == MISSING_ACCESS
               else "the bot is missing a permission there")
        try:
            self.rest.post(dm, content=(
                f"I can't post in <#{channel}>: {why} (Discord {code}). Task updates that "
                "need you come to this DM until it is fixed; the HUD's Discord light shows "
                "it too."))
            self._alerted[str(channel)] = now   # only once it was actually sent
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
            # The DM could not be found just now (the router swallowed why):
            # retry by reconcile, so a terminal Done/Failed is not lost for want
            # of a later event.
            self._count("dm_unavailable")
            self._schedule_reconcile(self._clock() + RETRY_AFTER_FAILURE)
            return False
        prefix = _clean(f"[{_cap(project.name, 60)} · task {task.id}] ")
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
                files = (("report.txt", _clean(report.overflow)),)
        content = _clean(content)
        if thread:
            ok, _message, exc = self._post(kind, thread, content=content, files=files,
                                           ping=kind in ATTENTION)
            if ok:
                return True
            if exc is None or _transient(exc):
                return False                    # transient: a reconcile retries it
            # Refused for good — the place is gone, locked or forbidden, or the
            # post itself was refused: an attention milestone still reaches the
            # owner through the DM; a quiet one is settled.
            if getattr(exc, "code", None) == UNKNOWN_CHANNEL:
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
        """Best-effort: a sidecar that cannot be written costs at worst a late
        repeat, never the post that was already made."""
        try:
            _write_bytes(sidecar_path(self.stores, task_id),
                         json.dumps(sidecar, sort_keys=True).encode("utf-8"))
        except (StoreError, OSError, TypeError, ValueError) as exc:
            self._count("sidecar_errors")
            LOG.warning("Discord sidecar for task %s not saved (%s)", task_id,
                        type(exc).__name__)

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
        DM. Any other refusal — 10003/50001/50013, 50024 wrong channel type,
        30033 too many threads, a bare 404 — is for good, and that is the
        safety net's case."""
        if sidecar.get("thread_gone"):
            return None, False
        current = self.stores.tasks.get(task.id)
        rethread = bool(sidecar.get("rethread"))
        thread = sidecar.get("discord_thread_id") or (
            current.discord_thread_id if current and not rethread else None)
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
        name = _cap(_clean(f"{task.id} · {' '.join(task.brief.split())}"), 100)
        channel = project.discord_channel_id
        ok, thread, exc = self._call("create_thread", self.rest.create_thread, channel, name,
                                     channel=channel)
        if not ok:
            return None, exc is None or _transient(exc)
        self._count("threads")
        # The id goes on the task record first (write-once: TaskStore.save never
        # wipes it), then the sidecar, best-effort — so the narrowest possible
        # window loses a thread Discord already made. Reload under the store
        # lock; never overwrite newer runner status.
        try:
            with _lock:
                current = self.stores.tasks.get(task.id)
                if current is not None and (rethread or not current.discord_thread_id):
                    # A rethread (B1) replaces the gone id: the gateway places
                    # owner messages by the id on the task record.
                    current.discord_thread_id = thread
                    self.stores.tasks.save(current)
        except StoreError as exc:
            LOG.warning("Discord thread id not saved on task %s (%s)", task.id,
                        type(exc).__name__)
        sidecar["discord_thread_id"] = thread
        # The parent channel, so a relink to another channel can tell that a
        # live task's thread is in the old one (item 7, O-C5 for tasks).
        sidecar["channel_id"] = str(channel)
        sidecar.pop("rethread", None)
        self._save(task.id, sidecar)
        self._call("add_owner", self.rest.add_owner, thread, channel=thread)
        moved_from = sidecar.pop("moved_from", None)
        if moved_from:
            self._moved(task, sidecar, str(moved_from), str(thread))
        return thread, False

    def _moved(self, task, sidecar, old, new):
        """The project moved to another channel while this task was live: the
        new thread opens with "Continued from <#old>" (which stands in for
        Started), and the old thread — kept as it is, never deleted — says
        "Moved to <#new>". Both quiet, both best-effort: a link that fails
        costs a link, never the task's posts."""
        ok, message_id, _ = self._post("continued", new, content=_clean(
            f"Continued from <#{old}> (task {task.id}: the project moved channel)."))
        if ok:
            sidecar["started_message_id"] = message_id
        self._save(task.id, sidecar)
        # No `channel=`: a refusal in the old place is not this task's broken
        # channel any more, so it must not turn the light amber.
        if self._call("moved_note", self.rest.post, old, silent=True,
                      content=_clean(f"Moved to <#{new}>: this task's updates continue "
                                     "there."))[0]:
            self._count("posts")

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
        # Never let an older snapshot move the sidecar backwards. A reconcile
        # posts from disk while older event snapshots can still be queued
        # behind it; replaying one would rewind `phase` and make the next pass
        # post Verified, Done and the owner ping a second time. `updated` moves
        # strictly forward on every transition, so a snapshot older than the
        # newest one already delivered has nothing left to say.
        stamp, seen = _stamp(task.updated), _stamp(sidecar.get("seen_updated"))
        if not reconcile and stamp is not None and seen is not None and stamp < seen:
            self._count("stale_snapshots")
            return
        if stamp is not None and (seen is None or stamp > seen):
            sidecar["seen_updated"] = task.updated
        task = _scrubbed(task)
        project = copy.copy(project)
        project.name = _clean(project.name)
        if questions:
            questions = [{**q, "text": _clean(q.get("text")),
                          "options": [_clean(o) for o in q.get("options") or []]}
                         for q in questions if isinstance(q, dict)]
        phase = task.status.phase
        channelled = bool(project.discord_channel_id)
        if reconcile and "phase" not in sidecar and self._preexisting(task):
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
                                                 content=_clean(milestone("started", task)))
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
                if phase in TERMINAL_STATES:
                    # Nothing more will be posted there: a broken-thread entry
                    # for it must not hold the light amber.
                    for place in (thread, sidecar.get("discord_thread_id")):
                        if place and self._broken.pop(str(place), None) is not None:
                            self._changed()
        question = task.status.open_question
        # Two questions in one phase share a `Task.updated` stamp, so the
        # stamp cannot order them: an event snapshot posts (or clears) a
        # question only while it is still the one open on disk. A reconcile
        # reads the disk, so it always is.
        current = question
        if not reconcile:
            try:
                record = self.stores.tasks.get(task.id)
            except StoreError:
                record = None
            current = _clean(record.status.open_question) if record is not None else None
        if question != current:
            self._count("stale_questions")
        elif phase == TaskState.CLARIFYING and question and question != sidecar.get("open_question"):
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
        if record.get("kind") == LINK_KIND:
            self._relinked(record)
            return
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

    def _relinked(self, record):
        """A project was linked to a channel, or unlinked (B1). Its own
        reconcile runs now, so an active or blocked task that lived on the DM
        safety net gets its thread and card in the new channel at once —
        including one whose old thread Discord had deleted (`thread_gone`).
        The DM stays the safety net only for a channel that later breaks."""
        data = record.get("data") or {}
        project_id = record.get("project_id") or data.get("project_id")
        previous = (data.get("previous") or {}).get("discord_channel_id")
        if previous and self._broken.pop(str(previous), None) is not None:
            self._changed()
        if not project_id:
            return
        project = self.stores.projects.get(project_id)
        if project is None:
            return
        self._count("relinks")
        for task in self.stores.tasks.list(project_id=project_id):
            if self._stop.is_set() or self.breaker_open():
                self._schedule_reconcile(self._open_until or self._clock())
                return
            if task.state in TERMINAL_STATES:
                continue
            sidecar = read_sidecar(self.stores, task.id)
            live = None if sidecar.get("thread_gone") else (
                sidecar.get("discord_thread_id") or task.discord_thread_id)
            # A sidecar from before the parent was recorded: its thread was
            # made in the channel this link replaced, or Discord says where.
            parent = sidecar.get("channel_id") or previous
            if live and not parent and project.discord_channel_id:
                ok, info, _ = self._call("get_thread", self.rest.get_channel, live)
                parent = (info or {}).get("parent_id") if ok and isinstance(info, dict) else None
                if not parent and task.id not in self._parent_unknown:
                    # Its posts stay in the old thread: say so, once per task.
                    self._parent_unknown.add(task.id)
                    self._count("parent_unknown")
                    LOG.warning("Discord relink: task %s's thread has no known parent channel; "
                                "its updates stay in that thread", task.id)
            if (project.discord_channel_id and live and parent
                    and str(parent) != str(project.discord_channel_id)):
                # Relinked to another channel while the task is live: a new
                # thread there, linked both ways; the old one is kept as is.
                for key in ("discord_thread_id", "started_message_id",
                            "discord_status_message_id", "embed_sha", "channel_id"):
                    sidecar.pop(key, None)
                self._drop_pending(str(live), task.id)
                sidecar["rethread"] = True
                sidecar["moved_from"] = str(live)
                # An open question is asked again in the new thread, which is
                # where the owner's typed answer is now routed.
                sidecar.pop("open_question", None)
                sidecar["retired_threads"] = sorted(
                    set(sidecar.get("retired_threads") or []) | {str(live)})
                self._count("moved_tasks")
                self._save(task.id, sidecar)
            elif project.discord_channel_id and sidecar.get("thread_gone"):
                # A fresh thread in the new channel; the gone one is kept on
                # record only as history.
                gone = sidecar.get("discord_thread_id") or task.discord_thread_id
                for key in ("thread_gone", "discord_thread_id", "started_message_id",
                            "discord_status_message_id", "embed_sha"):
                    sidecar.pop(key, None)
                sidecar["rethread"] = True
                if gone:
                    sidecar["retired_threads"] = sorted(
                        set(sidecar.get("retired_threads") or []) | {str(gone)})
                self._save(task.id, sidecar)
            try:
                self._deliver(task, project, sidecar, reconcile=True)
            except Exception as exc:
                LOG.warning("Discord relink of a task failed (%s)", type(exc).__name__)

    def _reconcile(self):
        self.reconciling = True
        try:
            self._reconcile_tasks()
        finally:
            self.reconciling = False

    def _preexisting(self, task) -> bool:
        """Created before this poster started (so it may be seeded silently)."""
        from datetime import datetime
        created = _created(task)
        try:
            started = datetime.fromisoformat(self.started_at)
        except (TypeError, ValueError):
            return True
        return created is not None and created < started

    def _reconcile_tasks(self):
        self._reconcile_at = None
        self._count("reconciles")
        try:
            ids = self.stores.tasks.ids()
        except (StoreError, OSError) as exc:
            # Keep taking live events; try the pass again with back-off.
            delay = min(RETRY_AFTER_FAILURE * (2 ** self._list_backoff), LIST_BACKOFF_MAX_S)
            self._list_backoff += 1
            self._schedule_reconcile(self._clock() + delay)
            LOG.warning("Discord reconcile could not list tasks (%s)", type(exc).__name__)
            return
        self._list_backoff = 0
        for task_id in ids:
            if self._stop.is_set() or self._closing or self.breaker_open():
                if self.breaker_open():
                    self._reconcile_at = self._open_until
                return
            try:
                task = self.stores.tasks.get(task_id)
            except StoreError as exc:
                # One unreadable record never stops the pass (or the breaker
                # from closing behind it).
                self._count("unreadable_tasks")
                LOG.warning("Discord reconcile skipped unreadable task %s (%s)", task_id,
                            type(exc).__name__)
                continue
            if task is None:
                continue
            try:
                project = self.stores.projects.get(task.project_id)
                if project is None:
                    continue
                sidecar = read_sidecar(self.stores, task.id)
                if (task.state in TERMINAL_STATES and "phase" not in sidecar
                        and self._preexisting(task)):
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
            # or not the pass had anything to send — and the failure count and
            # back-off go with it, so the HUD light returns to ok rather than
            # staying "degraded" on a failure that is over.
            self._open_until = None
            self._failures = 0
            self._backoff = 0
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
                        # Never before the breaker re-closes: a reconcile due
                        # "now" with the breaker open is a busy loop of
                        # get(timeout=0).
                        self._schedule_reconcile(max(self._clock(), self._open_until or 0.0))
                    if self.breaker_open():
                        # Whatever this event says is on disk; the reconcile at
                        # the end of the back-off catches up. Once the back-off
                        # has run out, events are handled again whether or not
                        # that reconcile could finish.
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
