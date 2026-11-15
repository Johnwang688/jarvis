"""The working set: the chat threads the owner is working on now (WS-1, plan
`docs/plans/2026-11-14-inbox-working-set-plan.md`, revision 2, D-1 to D-11).

The Inbox becomes a second view of the owner's threads: every chat thread
with recent activity is *in*, and leaves when it has been idle for the idle
limit (default 3 days) or when the owner presses Done. Leaving changes
nothing else: the thread stays in its project, untouched.

**The store (D-1).** `<data root>/inbox/working.json`, inside the inbox's
protected tree (`jarvis/tools/files._inbox_tree`; the file is also named in
`_protected_state_paths`), so no agent write reaches it:

    {"seeded_at": ISO | null, "idle_days": 1..30,
     "threads": {thread_id: {since, last, seq, done_at, done_seq, done_token, reason}}}

A thread is *in* while `done_at` is null. `seq` counts the activity records
on it, bumped under the store lock; `last` is the UTC time of the latest.
`reason` says why it left ("done", "idle", "archived"), null while in.
`idle_days` is the owner's idle limit (an addition to D-1's shape: the
setting lives beside the state it governs, under the same protection).
Every write is one `stores._write_bytes` (a sibling temp file, mode 0600,
then `os.replace`, with no fsync: a crash can leave the file empty or
missing, which reads as an empty, unseeded set that is seeded again), and
a write larger than `_FILE_MAX` is refused (a read refuses one too). A file
that is missing, a link, not regular, too large or not JSON reads as empty,
and a malformed entry is dropped. Membership is not a secret (§9 Q7: it is
derivable from `/threads` + `/activity`); the boundary is on the writes and
on the inbox rows the read carries.

**What joins (D-2).** A synchronous bus observer (as `activity.py`), reading
the producer table `ACTIVITY_KINDS` (plus a `subagent` record whose op is
"started" or "finished", never "progress"). Each record bumps `seq`/`last`
on its `thread_id`, and brings a thread that had left back in. A record
flagged `compaction` (the owner's Compact button) is skipped; so is any
record on a task's thread (D-7: tasks stay in Needs you), an archived
thread, or a thread in an archived project (D-8).

**The invariant (IN-8): a visible chat thread with a linked row of rank 1
to 4 is always in.** Two halves keep it:

  * *Leaving needs eligibility.* A thread leaves only when a fresh
    `inbox.rows()` read shows no row of rank 1 to 4 linked to it by
    `thread_id` (a quiet-hours deferral is rank 4 and counts), and
    `activity.thread_status` is not working or needs_input. A read that
    failed is "not eligible" (fail closed): `rows()` raised, or reported
    (`failures=`) a source that can hold such a row as failed (approvals,
    questions, deferrals, a deferrals file that is not ours, or any name not
    in `_HARMLESS_SOURCES`: task rows carry no chat `thread_id`, and missed,
    decision, check and PR rows are rank 5 or more).
  * *Reconciling brings it back.* Not every new blocking row arrives with a
    thread-scoped record: a quiet-hours deferral is settled by the broker
    and announced only by `inbox_changed`, so it does not bump `seq`, and
    can land while a Done or a sweep is deciding, or just after. Every
    `inbox_changed` schedules a `reconcile` `RECONCILE_S` later, off the
    publish thread (also run at the start of each sweep): one read, and
    every visible chat thread with a linked rank 1 to 4 row that is out (or
    has no entry) is brought in, its `seq` bumped. At most one reconcile
    worker runs; events while it runs make it run once more. The invariant
    heals itself, but `RECONCILE_S` is only the scheduling delay: waiting
    for the decision lock, the source reads and a reconcile whose read
    failed (retried on the next event or sweep) can stretch the time to
    recovery, with no fixed bound. The row is drawn in `needs_you`
    meanwhile.

Two ways to leave:

  * the timer (`sweep`): hourly, plus once `FIRST_SWEEP_S` after start; a
    thread whose `last` is older than the idle limit leaves with reason
    "idle". The scheduler (`timer_factory`) and clocks are injectable.
  * Done (`POST /working/<id>/done {seq}`): `seq` must equal the stored
    one (stale is a 409 with the current entry); then eligibility, and
    `done_at`, `done_seq` and a fresh random `done_token` written in one
    step; then `activity.seen_thread` for the last turn it observed only
    (a reply that finished since stays unread). Undo (`POST
    /working/<id>/undo {token}`) restores the entry only with the stored
    token and before a `UNDO_S` deadline on this process's monotonic clock.

**Locks.** `_decide` serialises the decisions (Done, the sweep, reconcile)
and is held across their inbox read. `_lock` guards the store and is never
held while anything else is called: the observer runs inside `publish`,
sometimes under the daemon's lock, and `inbox.rows()` takes that lock, so a
decision reads the store's `seq` under `_lock`, releases it, reads the rows
and the status, then takes `_lock` again and writes only if `seq` is
unchanged. A message, a turn, an approval and a question each bump `seq`, so
those cannot slip between the read and the write; a deferral can, and the
reconcile above brings its thread back.

**Lifecycle (D-8).** Archiving a thread or its project takes it out
(reason "archived"; its rows show in `needs_you`). Restore does not rejoin.
Delete drops the entry. A move or rename publishes `working_changed` only.
Archive, restore and delete bump a lifecycle generation (per thread, and
per project for a project's events) and clear any `done_token`, so a
change that passed the visibility check before the event (activity, a
reconcile, a seed) is dropped when it commits, and an Undo from before it
is refused. Another project's event does not count.

**Stopping.** `stop` closes the set for good: the timers are cancelled, a
running reconcile or sweep is waited for (`STOP_JOIN_S` each), and every
section that changes membership (activity, reconcile, the sweep's pruning
and exits, seeding, Done, Undo, and the archive, restore and delete
handlers) checks the closed flag under the store lock first, so nothing
changes membership after `stop` returns.

A move bumps the thread's generation too (it changes which project guards
it), without bumping `seq`.

**The signal (D-5).** `working_changed` carries no ids and no data: `/events`
is not owner-checked. Its timing can still hint at membership (it follows a
record on a member thread); accepted under §9 Q7, since membership is
derivable anyway. The HUD reads `GET /working` (owner-only) again.

**Startup (D-11).** `Daemon.__init__` loads the store and registers the
observer; `Daemon.start` seeds once (when `seeded_at` is absent: every
visible chat thread updated within the idle limit, and every one with a
linked rank 1 to 4 row) and arms the timer. A seed whose inbox read failed,
or that could not read one of the threads (told apart from a thread that is
gone or hidden), is incomplete: nothing is added, `seeded_at` stays unset,
and the next sweep tries again. A seed that adds nothing to a store with no file writes
nothing (no `<data>/inbox/` folder is made at an empty start), so
`seeded_at` is then remembered in memory only.

This module is SELF_PROTECTED (`jarvis/tools/files.py`): it decides when a
thread leaves the owner's Inbox. Tests: tests/v2/working_check.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import errno
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from .stores import StoreError, _write_bytes

LOG = logging.getLogger(__name__)

NAME = "working.json"
IDLE_DAYS_DEFAULT = 3
IDLE_DAYS_MIN = 1
IDLE_DAYS_MAX = 30
FIRST_SWEEP_S = 300.0           # the first sweep, after the inbox sources are up
SWEEP_S = 3600.0                # then hourly
RECONCILE_S = 0.25              # an `inbox_changed` burst becomes one reconcile
UNDO_S = 20.0                   # the daemon's Undo window; the HUD's toast shows 15 s
STOP_JOIN_S = 5.0               # how long `stop` waits for a running reconcile or sweep
BLOCKING_MAX_RANK = 4           # IN-8: a linked row of rank 1 to 4 holds a thread in
COUNTED_MAX_RANK = 5            # `waitingCount` (hud/src/lib/inbox.ts)
PENDING_MAX_RANK = 2            # `anyPending`
PRUNE_AFTER = timedelta(days=IDLE_DAYS_MAX)   # an entry out this long is forgotten
_FILE_MAX = 4 << 20
_ID = re.compile(r"^[0-9a-f]{8}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_READ = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)

# D-2's producer table: each is activity on its record's `thread_id` (the
# receiving thread for `relay_in`/`relay_delivered`, the sender for
# `relay_reply`: `chat_relay._record` logs each on that thread). Plus
# `subagent` with op in SUBAGENT_OPS (the parent thread).
ACTIVITY_KINDS = frozenset({
    "user_message", "turn_started", "turn_finished", "error",
    "approval_requested", "question", "question_answered",
    "relay_in", "relay_delivered", "relay_reply",
    "decision_posted", "check_posted",
    "thread_opened",
})
SUBAGENT_OPS = frozenset({"started", "finished"})
# D-8: what takes a thread out, what drops its entry, what only redraws, and
# what only bumps the lifecycle generation.
_LEAVE_KINDS = frozenset({"thread_archived", "project_archived"})
_DROP_KINDS = frozenset({"thread_deleted", "project_deleted"})
_RESTORE_KINDS = frozenset({"thread_restored", "project_restored"})
_REDRAW_KINDS = frozenset({"thread_moved", "thread_updated", "project_updated"})

# The inbox sources (`inbox.rows(failures=...)`) whose failure cannot hide a
# row linked to a chat thread: task rows carry no `thread_id` (D-7), and
# missed, decision, check and PR rows are rank 5 or more. Any other name
# reported fails the read closed.
_HARMLESS_SOURCES = frozenset({"tasks", "missed", "decisions", "checks", "pull requests"})

_ROW_WORDS = {"approval": "an approval", "question": "a question", "deferred": "an approval deferred overnight"}
UNREADABLE = "the Inbox could not be read just now, so nothing leaves it; try again"
_ENTRY_FIELDS = ("since", "last", "seq", "done_at", "done_seq", "done_token", "reason")
_NO_SEEN = object()             # Done could not tell which turn it saw: mark nothing read


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_time(value) -> datetime | None:
    """An ISO time; one without a zone is read as UTC (`model.utcnow` writes
    zoned times; an older record may not)."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        out = datetime.fromisoformat(value)
    except ValueError:
        return None
    return out if out.tzinfo is not None else out.replace(tzinfo=timezone.utc)


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def valid_idle_days(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and IDLE_DAYS_MIN <= value <= IDLE_DAYS_MAX


def _valid_entry(entry) -> bool:
    if not isinstance(entry, dict) or set(entry) != set(_ENTRY_FIELDS):
        return False
    if parse_time(entry["since"]) is None or parse_time(entry["last"]) is None or not _count(entry["seq"]):
        return False
    if entry["done_at"] is None:
        return entry["done_seq"] is None and entry["done_token"] is None and entry["reason"] is None
    return (parse_time(entry["done_at"]) is not None and _count(entry["done_seq"])
            and (entry["done_token"] is None or (isinstance(entry["done_token"], str)
                                                 and _TOKEN.match(entry["done_token"]) is not None))
            and isinstance(entry["reason"], str) and len(entry["reason"]) <= 20)


def row_hidden(row) -> bool:
    """`isHidden` (hud/src/lib/inbox.ts), mirrored: the row says it is hidden
    and the HUD's own rule allows it (rank 5 to 8, never "PR list
    unavailable" or a deferral). A rank 1 to 4 row is never hidden."""
    rank, kind, row_id = row.get("rank"), row.get("kind"), row.get("id")
    if not isinstance(rank, int) or isinstance(rank, bool) or not isinstance(row_id, str):
        return False
    return (bool(row.get("hidden")) and 5 <= rank <= 8 and kind not in ("pr_unavailable", "deferred")
            and not row_id.startswith(("prx:", "defer:")))


def waiting_count(rows) -> int:
    """`waitingCount`, mirrored: visible rows of rank 1 to 5 (D-6)."""
    return sum(1 for r in rows if _rank(r) is not None and 1 <= _rank(r) <= COUNTED_MAX_RANK
               and not row_hidden(r))


def any_pending(rows) -> bool:
    """`anyPending`, mirrored: any row of rank 1 or 2 (amber)."""
    return any(_rank(r) is not None and 1 <= _rank(r) <= PENDING_MAX_RANK for r in rows)


def _rank(row):
    rank = row.get("rank") if isinstance(row, dict) else None
    return rank if isinstance(rank, int) and not isinstance(rank, bool) else None


def blocking(row) -> bool:
    """A row that holds its thread in (IN-8): rank 1 to 4."""
    rank = _rank(row)
    return rank is not None and 1 <= rank <= BLOCKING_MAX_RANK


def _linked(rows) -> set[str]:
    """The thread ids that a rank 1 to 4 row is linked to."""
    out = set()
    for row in rows:
        thread_id = row.get("thread_id") if isinstance(row, dict) else None
        if isinstance(thread_id, str) and _ID.match(thread_id) and blocking(row):
            out.add(thread_id)
    return out


class WorkingSet:
    def __init__(self, daemon, *, clock=None, mono=None, timer_factory=None):
        self.daemon = daemon
        self.root = Path(daemon.stores.root)
        self.path = self.root / "inbox" / NAME
        self.clock = clock or _utcnow
        self.mono = mono or time.monotonic
        self._timer_factory = timer_factory or threading.Timer
        self._lock = threading.RLock()      # the store; never held while calling out
        self._decide = threading.Lock()     # Done, the sweep and reconcile, across their inbox read
        self._threads: dict[str, dict] = {}
        self._seeded_at: str | None = None
        self._idle_days = IDLE_DAYS_DEFAULT
        self._undo: dict[str, tuple[str, float]] = {}   # thread id -> (token, monotonic deadline)
        self._chat: dict[str, bool] = {}                # thread id -> a chat thread (never changes)
        self._gen: dict[str, int] = {}                  # thread id -> lifecycle generation
        self._pgen: dict[str, int] = {}                 # project id -> lifecycle generation
        self._timer = None
        self._reconcile_timer = None
        self._reconciling = False                       # one reconcile worker at a time
        self._reconcile_again = False                   # an event came while it ran: once more
        self._workers: set = set()                      # threads running a reconcile or a sweep
        self._on = False
        self._closed = False                            # `stop` ran: no membership write after it
        self._load()

    # -- the file -------------------------------------------------------------------

    def _read_file(self) -> dict | None:
        try:
            fd = os.open(self.path, _READ)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                return None
            raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _FILE_MAX:
                return None
            chunks, left = [], _FILE_MAX + 1
            while left > 0 and (chunk := os.read(fd, min(1 << 16, left))):
                chunks.append(chunk)
                left -= len(chunk)
        finally:
            os.close(fd)
        data = b"".join(chunks)
        if len(data) > _FILE_MAX:
            return None
        try:
            value = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            return None
        return value if isinstance(value, dict) else None

    def _load(self) -> None:
        try:
            value = self._read_file()
        except Exception as exc:  # noqa: BLE001 — an unreadable file is an empty set
            LOG.warning("Cannot read the working set (%s); it starts empty", type(exc).__name__)
            value = None
        if value is None:
            LOG.warning("The working set file is not one of ours; it starts empty")
            value = {}
        threads = value.get("threads") if isinstance(value.get("threads"), dict) else {}
        seeded = value.get("seeded_at")
        idle = value.get("idle_days")
        with self._lock:
            self._threads = {tid: dict(e) for tid, e in threads.items()
                             if isinstance(tid, str) and _ID.match(tid) and _valid_entry(e)}
            self._seeded_at = seeded if parse_time(seeded) is not None else None
            self._idle_days = idle if valid_idle_days(idle) else IDLE_DAYS_DEFAULT

    def _save(self) -> None:
        """Under `_lock`. Raises StoreError/OSError; callers decide. Never
        writes what a read would refuse (`_FILE_MAX`)."""
        data = json.dumps({"seeded_at": self._seeded_at, "idle_days": self._idle_days,
                           "threads": self._threads},
                          sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        if len(data) > _FILE_MAX:
            LOG.error("The working set is larger than its file may be (%d bytes); it is not written", len(data))
            raise StoreError("the working set is too large to write")
        folder = self.path.parent
        try:
            info = os.lstat(folder)
        except FileNotFoundError:
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = os.lstat(folder)
        if not stat.S_ISDIR(info.st_mode):
            raise StoreError(f"{folder.name} is not a folder")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.chmod(folder, 0o700)
        _write_bytes(self.path, data)

    def _save_quietly(self) -> None:
        try:
            self._save()
        except Exception as exc:  # noqa: BLE001 — memory stands; the next write retries
            LOG.warning("Cannot write the working set (%s)", type(exc).__name__)

    # -- reading ----------------------------------------------------------------------

    @property
    def idle_days(self) -> int:
        with self._lock:
            return self._idle_days

    @property
    def seeded_at(self) -> str | None:
        with self._lock:
            return self._seeded_at

    def entry(self, thread_id: str) -> dict | None:
        """A copy of one entry, without its token, or None."""
        with self._lock:
            e = self._threads.get(thread_id)
            return _public(thread_id, e) if e is not None else None

    def members(self) -> list[str]:
        with self._lock:
            return sorted(tid for tid, e in self._threads.items() if e["done_at"] is None)

    def _thread(self, thread_id):
        try:
            return self.daemon.stores.threads.get(thread_id)
        except (StoreError, ValueError):
            return None

    def _is_chat(self, thread) -> bool:
        known = self._chat.get(thread.id)
        if known is None:
            role = getattr(thread.role, "value", thread.role)
            known = self._chat[thread.id] = role == "chat" and not thread.task_id
        return known

    def _check_visible(self, thread_id):
        """(thread, read): the thread when it is a chat thread (never a
        task's, D-7) that is neither archived nor in an archived project
        (D-8), else None; `read` is False when the thread record could not be
        read (told apart from a thread that is missing or not visible).
        Called with no lock of ours held."""
        try:
            thread = self.daemon.stores.threads.get(thread_id)
        except (StoreError, ValueError):
            return None, False
        if thread is None or not self._is_chat(thread) or thread.archived:
            return None, True
        from .projects import is_archived
        return (None if is_archived(self.daemon.stores, thread.project_id) else thread), True

    def _visible(self, thread_id):
        return self._check_visible(thread_id)[0]

    def _visible_chat(self, thread_id) -> bool:
        return self._visible(thread_id) is not None

    def _generations(self) -> tuple[dict, dict]:
        """Under `_lock`: every lifecycle generation, copied, taken before a
        visibility check."""
        return dict(self._gen), dict(self._pgen)

    def _unchanged(self, before, thread_id, project_id) -> bool:
        """Under `_lock`: no archive, restore or delete of this thread or of
        its project since `before` (a change to another project does not
        count)."""
        threads, projects = before
        return (self._gen.get(thread_id, 0) == threads.get(thread_id, 0)
                and self._pgen.get(project_id, 0) == projects.get(project_id, 0))

    def _bump_project(self, project_id) -> None:
        """Under `_lock`."""
        if isinstance(project_id, str):
            self._pgen[project_id] = self._pgen.get(project_id, 0) + 1

    # -- the bus (D-2, D-8) ---------------------------------------------------------------

    def observe(self, record) -> None:
        """Called inside `EventBus.publish`. Never raises into the publisher,
        and filters on the kind before taking any lock: `activity` records
        are published under the activity tracker's lock."""
        try:
            if not isinstance(record, dict):
                return
            kind = record.get("kind")
            if kind in ACTIVITY_KINDS or kind == "subagent":
                self._activity(kind, record)
            elif kind == "inbox_changed":
                self._arm_reconcile()
            elif kind in _LEAVE_KINDS:
                self._leave_on(kind, record)
            elif kind in _DROP_KINDS:
                self._drop_on(kind, record)
            elif kind in _RESTORE_KINDS:
                self._restore_on(kind, record)
            elif kind in _REDRAW_KINDS:
                self._redraw_on(kind, record)
        except Exception:
            LOG.exception("working set: cannot follow a %s record", record.get("kind"))

    def _activity(self, kind, record) -> None:
        if record.get("compaction"):
            return              # the owner's Compact button is not activity
        thread_id = record.get("thread_id")
        if not isinstance(thread_id, str) or not _ID.match(thread_id):
            return
        if kind == "subagent":
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            if data.get("op") not in SUBAGENT_OPS:
                return
        # The generations before the visibility check: an archive, restore or
        # delete of this thread or its project that lands after the check
        # (and before the write) bumps one, and this record is then dropped
        # rather than rejoining. Another project's event does not count.
        with self._lock:
            before = self._generations()
        thread = self._visible(thread_id)
        if thread is None:
            return
        now = _iso(self.clock())
        with self._lock:
            if self._closed or not self._unchanged(before, thread_id, thread.project_id):
                return
            entry = self._threads.get(thread_id)
            if entry is None:
                self._threads[thread_id] = {"since": now, "last": now, "seq": 1, "done_at": None,
                                            "done_seq": None, "done_token": None, "reason": None}
            else:
                entry["seq"] += 1
                entry["last"] = now
                if entry["done_at"] is not None and entry["seq"] > (entry["done_seq"] or 0):
                    # Back in on new activity (D-4: only a record that bumps
                    # `seq` past `done_seq`, which every new record does).
                    entry.update(since=now, done_at=None, done_seq=None, done_token=None, reason=None)
                    self._undo.pop(thread_id, None)
            self._save_quietly()
        self._changed()

    def _take_out(self, thread_id: str, reason: str, now: str) -> bool:
        """Under `_lock`: one entry out. True when it was in."""
        entry = self._threads.get(thread_id)
        if entry is None or entry["done_at"] is not None:
            return False
        entry.update(done_at=now, done_seq=entry["seq"], done_token=None, reason=reason)
        self._undo.pop(thread_id, None)
        return True

    def _forget_token(self, thread_id: str) -> bool:
        """Under `_lock`: no Undo from before a lifecycle event. True when an
        entry changed."""
        self._undo.pop(thread_id, None)
        entry = self._threads.get(thread_id)
        if entry is not None and entry["done_token"] is not None:
            entry["done_token"] = None
            return True
        return False

    def _project_threads(self, project_id) -> list[str]:
        """Every entry whose thread is in `project_id` (no lock of ours held)."""
        with self._lock:
            known = list(self._threads)
        out = []
        for thread_id in known:
            thread = self._thread(thread_id)
            if thread is not None and thread.project_id == project_id:
                out.append(thread_id)
        return out

    def _leave_on(self, kind, record) -> None:
        now = _iso(self.clock())
        thread_id = record.get("thread_id")
        if kind == "thread_archived":
            with self._lock:
                if isinstance(thread_id, str):
                    self._gen[thread_id] = self._gen.get(thread_id, 0) + 1
            ids = [thread_id]
        else:
            with self._lock:
                self._bump_project(record.get("project_id"))
            ids = self._project_threads(record.get("project_id"))
        changed = moved = False
        with self._lock:
            if self._closed:
                return
            for tid in ids:
                moved |= self._take_out(tid, "archived", now)
                changed |= self._forget_token(tid)
            if changed or moved:
                self._save_quietly()
        if moved:
            self._changed()

    def _restore_on(self, kind, record) -> None:
        """Restore does not rejoin (D-8); it bumps the generation and spends
        any Undo from before the archive."""
        thread_id = record.get("thread_id")
        if kind == "thread_restored":
            with self._lock:
                if isinstance(thread_id, str):
                    self._gen[thread_id] = self._gen.get(thread_id, 0) + 1
            ids = [thread_id]
        else:
            with self._lock:
                self._bump_project(record.get("project_id"))
            ids = self._project_threads(record.get("project_id"))
        changed = False
        with self._lock:
            if self._closed:
                return
            for tid in ids:
                changed |= self._forget_token(tid)
            if changed:
                self._save_quietly()

    def _drop_on(self, kind, record) -> None:
        if kind == "thread_deleted":
            ids = {record.get("thread_id")}
            with self._lock:
                for tid in ids:
                    if isinstance(tid, str):
                        self._gen[tid] = self._gen.get(tid, 0) + 1
        else:
            with self._lock:
                self._bump_project(record.get("project_id"))
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            removed = data.get("removed_thread_ids")
            ids = {i for i in removed if isinstance(i, str)} if isinstance(removed, list) else set()
            with self._lock:
                known = list(self._threads)
            ids |= {tid for tid in known if self._thread(tid) is None}
        changed = False
        with self._lock:
            if self._closed:
                return
            for thread_id in ids:
                if self._threads.pop(thread_id, None) is not None:
                    changed = True
                self._undo.pop(thread_id, None)
            if changed:
                self._save_quietly()
        if changed:
            self._changed()

    def _redraw_on(self, kind, record) -> None:
        if kind == "thread_moved":
            # A move changes which project's generation guards the thread:
            # a change checked before it (in the old project) is dropped
            # when it commits. Not activity: `seq` is not bumped.
            thread_id = record.get("thread_id")
            with self._lock:
                if isinstance(thread_id, str):
                    self._gen[thread_id] = self._gen.get(thread_id, 0) + 1
        members = self.members()
        if kind == "project_updated":
            if members:
                self._changed()
        elif record.get("thread_id") in members:
            self._changed()

    def _changed(self) -> None:
        """No ids, no data (D-5): `/events` is not owner-checked."""
        try:
            self.daemon.bus.publish({"kind": "working_changed"})
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Cannot announce a working set change (%s)", type(exc).__name__)

    # -- the inbox read (D-4) -------------------------------------------------------------

    def _read_rows(self) -> tuple[list[dict], bool]:
        """A fresh `inbox.rows()` (marks ignored, no live checks: neither can
        hold a rank 1 to 4 row) and whether it is whole for this purpose:
        False when it raised or reported a source that can hold such a row
        as failed (`failures=`, per call, so overlapping reads never mix)."""
        from . import inbox as _inbox
        failures: list = []
        try:
            rows = _inbox.rows(self.daemon, with_checks=False, marks="ignore", failures=failures)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Cannot read the inbox for the working set (%s)", type(exc).__name__)
            return [], False
        if not isinstance(rows, list):
            return [], False
        failed = set(failures) - _HARMLESS_SOURCES
        if failed:
            LOG.warning("The working set's inbox read was incomplete (%s)", ", ".join(sorted(failed)))
        return rows, not failed

    def _strict_rows(self) -> list[dict] | None:
        """The read, or None when it is not whole (fail closed)."""
        rows, whole = self._read_rows()
        return rows if whole else None

    def _status(self, thread_id) -> str:
        return self.daemon.activity.thread_status(thread_id)

    def why_not(self, thread_ids, rows=None) -> dict[str, str | None]:
        """Why each thread may not leave now, or None when it may. `rows`:
        an inbox read already made (the view's); otherwise a strict one.
        Called with no lock of ours held."""
        if rows is None:
            rows = self._strict_rows()
        if rows is None:
            return {tid: UNREADABLE for tid in thread_ids}
        wanted = set(thread_ids)
        linked: dict[str, list[dict]] = {}
        for row in rows:
            thread_id = row.get("thread_id") if isinstance(row, dict) else None
            if thread_id in wanted and blocking(row):
                linked.setdefault(thread_id, []).append(row)
        out = {}
        for thread_id in thread_ids:
            held = linked.get(thread_id)
            if held:
                words = _ROW_WORDS.get(held[0].get("kind"), "something")
                out[thread_id] = f"{words} waits on you in this chat"
                continue
            status = self._status(thread_id)
            if status == "working":
                out[thread_id] = "a turn is running in this chat"
            elif status == "needs_input":
                out[thread_id] = "this chat waits on your answer"
            else:
                out[thread_id] = None
        return out

    # -- reconciling (IN-8, self-healing) --------------------------------------------------

    def _arm_reconcile(self) -> None:
        """From the observer: one reconcile `RECONCILE_S` from now, off the
        publish thread. At most one worker at a time: an event that arrives
        while one is armed is folded into it, and one that arrives while one
        runs makes it run once more when it finishes."""
        with self._lock:
            if not self._on or self._closed:
                return
            if self._reconciling:
                self._reconcile_again = True
                return
            if self._reconcile_timer is not None:
                return
            try:
                timer = self._timer_factory(RECONCILE_S, self._reconcile_tick)
                timer.daemon = True
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Cannot arm a working set reconcile (%s)", type(exc).__name__)
                return
            self._reconcile_timer = timer
        try:
            timer.start()
        except Exception as exc:  # noqa: BLE001 — never wedge the slot: the next event retries
            with self._lock:
                if self._reconcile_timer is timer:
                    self._reconcile_timer = None
            LOG.warning("Cannot start a working set reconcile (%s)", type(exc).__name__)

    def _reconcile_tick(self) -> None:
        with self._lock:
            self._reconcile_timer = None
            if not self._on or self._closed or self._reconciling:
                return
            self._reconciling = True
            self._reconcile_again = False
            me = threading.current_thread()
            self._workers.add(me)
        released = False
        try:
            while True:
                try:
                    self.reconcile()
                except Exception:  # noqa: BLE001
                    LOG.exception("working set: a reconcile failed")
                # The last look for a pending event and the release of the
                # worker are one step under the lock: an event after it finds
                # no worker and arms a new timer; one before it is run here.
                with self._lock:
                    if self._reconcile_again and not self._closed:
                        self._reconcile_again = False
                        continue
                    self._reconciling = False
                    self._reconcile_again = False
                    self._workers.discard(me)
                    released = True
                    break
        finally:
            if not released:        # only if the loop itself raised
                with self._lock:
                    self._reconciling = False
                    self._reconcile_again = False
                    self._workers.discard(me)

    def reconcile(self) -> list[str]:
        """Bring in every visible chat thread with a linked rank 1 to 4 row
        that is out or has no entry, bumping its `seq`. Returns the ids
        brought in. A read that is not whole still brings in what it shows
        (bringing in is always safe); the next `inbox_changed` or sweep
        reads again."""
        brought: list[str] = []
        with self._decide:
            rows, _whole = self._read_rows()
            linked = _linked(rows)
            with self._lock:
                before = self._generations()
                wanted = [tid for tid in sorted(linked)
                          if tid not in self._threads or self._threads[tid]["done_at"] is not None]
            visible = {}
            for tid in wanted:
                thread = self._visible(tid)
                if thread is not None:
                    visible[tid] = thread.project_id
            if not visible:
                return []
            now = _iso(self.clock())
            with self._lock:
                if self._closed:
                    return []
                for thread_id, project_id in visible.items():
                    if not self._unchanged(before, thread_id, project_id):
                        continue
                    entry = self._threads.get(thread_id)
                    if entry is None:
                        self._threads[thread_id] = {"since": now, "last": now, "seq": 1, "done_at": None,
                                                    "done_seq": None, "done_token": None, "reason": None}
                    elif entry["done_at"] is not None:
                        entry.update(seq=entry["seq"] + 1, since=now, last=now, done_at=None,
                                     done_seq=None, done_token=None, reason=None)
                        self._undo.pop(thread_id, None)
                    else:
                        continue
                    brought.append(thread_id)
                if brought:
                    self._save_quietly()
        if brought:
            self._changed()
        return brought

    # -- Done and Undo (D-4) ---------------------------------------------------------------

    def done(self, thread_id: str, seq) -> tuple[int, dict]:
        if not isinstance(thread_id, str) or not _ID.match(thread_id):
            return 400, {"error": "id must be eight lowercase hex characters"}
        if not _count(seq):
            return 400, {"error": "seq must be a whole number"}
        with self._decide:
            with self._lock:
                entry = self._threads.get(thread_id)
                if entry is None or entry["done_at"] is not None:
                    return 409, {"error": "this chat is not in your Inbox", "entry": self.entry(thread_id)}
                if entry["seq"] != seq:
                    return 409, {"error": "this chat changed since the Inbox was drawn",
                                 "entry": _public(thread_id, entry)}
            why = self.why_not([thread_id])[thread_id]
            # The last turn as of the check: a turn that finishes after this
            # bumps `seq` (refused below) or comes after the write (it
            # rejoins, and stays unread: seen only marks this one).
            try:
                observed = self.daemon.activity.last_turn(thread_id)
            except Exception:  # noqa: BLE001
                observed = _NO_SEEN
            with self._lock:
                entry = self._threads.get(thread_id)
                if entry is None or entry["done_at"] is not None or entry["seq"] != seq:
                    return 409, {"error": "this chat changed since the Inbox was drawn",
                                 "entry": self.entry(thread_id)}
                if why is not None:
                    return 409, {"error": why, "entry": _public(thread_id, entry)}
                if self._closed:
                    return 503, {"error": "Jarvis is stopping; nothing changed"}
                before = dict(entry)
                token = secrets.token_urlsafe(18)
                entry.update(done_at=_iso(self.clock()), done_seq=seq, done_token=token, reason="done")
                try:
                    self._save()
                except Exception as exc:  # noqa: BLE001 — not done: memory goes back
                    self._threads[thread_id] = before
                    LOG.warning("Cannot write a Done (%s)", type(exc).__name__)
                    return 503, {"error": "the Inbox could not be saved; nothing changed"}
                self._undo[thread_id] = (token, self.mono() + UNDO_S)
                public = _public(thread_id, entry)
        self._changed()
        if observed is not _NO_SEEN:
            try:
                # Once, so a finished chat is not left unread in its project;
                # only the turn this Done saw.
                self.daemon.activity.seen_thread(thread_id, only_turn=observed)
            except Exception as exc:  # noqa: BLE001 — the Done stands
                LOG.warning("Cannot mark a done chat read (%s)", type(exc).__name__)
        return 200, {"thread_id": thread_id, "done": True, "token": token, "undo_s": UNDO_S,
                     "entry": public}

    def undo(self, thread_id: str, token) -> tuple[int, dict]:
        if not isinstance(thread_id, str) or not _ID.match(thread_id):
            return 400, {"error": "id must be eight lowercase hex characters"}
        if not isinstance(token, str) or not _TOKEN.match(token):
            return 400, {"error": "token must be the one Done returned"}
        with self._lock:
            entry = self._threads.get(thread_id)
            held = self._undo.get(thread_id)
            if (entry is None or entry["done_at"] is None or entry["reason"] != "done"
                    or not isinstance(entry["done_token"], str) or held is None
                    or not secrets.compare_digest(entry["done_token"], token)
                    or not secrets.compare_digest(held[0], token)):
                return 409, {"error": "nothing to undo for this chat", "entry": self.entry(thread_id)}
            if not self.mono() < held[1]:
                self._undo.pop(thread_id, None)
                return 409, {"error": "too late to undo", "entry": _public(thread_id, entry)}
            if self._closed:
                return 503, {"error": "Jarvis is stopping; nothing changed"}
            before = dict(entry)
            entry.update(done_at=None, done_seq=None, done_token=None, reason=None)
            try:
                self._save()
            except Exception as exc:  # noqa: BLE001
                self._threads[thread_id] = before
                LOG.warning("Cannot write an Undo (%s)", type(exc).__name__)
                return 503, {"error": "the Inbox could not be saved; nothing changed"}
            self._undo.pop(thread_id, None)
            public = _public(thread_id, entry)
        self._changed()
        return 200, {"thread_id": thread_id, "done": False, "entry": public}

    # -- the timer (D-3) -------------------------------------------------------------------

    def sweep(self) -> list[str]:
        """Finish an incomplete seed, reconcile, then take out every thread
        idle past the limit and eligible. Returns the ids that left."""
        if self.seeded_at is None:
            self.seed()
        self.reconcile()
        left: list[str] = []
        pruned = False
        with self._decide:
            now = self.clock()
            with self._lock:
                if self._closed:
                    return []
                limit = timedelta(days=self._idle_days)
                seqs = {tid: e["seq"] for tid, e in self._threads.items()
                        if e["done_at"] is None and now - parse_time(e["last"]) >= limit}
                for tid in [t for t, e in self._threads.items() if e["done_at"] is not None
                            and now - parse_time(e["done_at"]) >= PRUNE_AFTER]:
                    del self._threads[tid]
                    pruned = True
            why = self.why_not(sorted(seqs)) if seqs else {}
            with self._lock:
                if self._closed:
                    return []
                stamp = _iso(now)
                for thread_id, seq in seqs.items():
                    entry = self._threads.get(thread_id)
                    if why.get(thread_id) is None and entry is not None and entry["seq"] == seq:
                        if self._take_out(thread_id, "idle", stamp):
                            left.append(thread_id)
                if left or pruned:
                    self._save_quietly()
        if left:
            self._changed()
        return sorted(left)

    def start(self) -> None:
        """`Daemon.start`, after the inbox sources: seed once, then arm the
        timer. Never raises."""
        try:
            self.seed()
        except Exception:  # noqa: BLE001
            LOG.exception("working set: seeding failed")
        with self._lock:
            self._on = True
        self._arm(FIRST_SWEEP_S)

    def stop(self) -> None:
        """Closed for good: the timers are cancelled, a running reconcile or
        sweep is waited for (at most `STOP_JOIN_S` each), and every commit
        checks the closed flag under the store lock, so no membership change
        is made after this returns, even by a worker that outlived the wait."""
        with self._lock:
            self._on = False
            self._closed = True
            timers = (self._timer, self._reconcile_timer)
            self._timer = self._reconcile_timer = None
            workers = [w for w in self._workers if w is not threading.current_thread()]
        for timer in timers:
            if timer is not None:
                timer.cancel()
        for worker in workers:
            join = getattr(worker, "join", None)
            if callable(join):
                join(STOP_JOIN_S)

    def _arm(self, delay: float) -> None:
        try:
            timer = self._timer_factory(delay, self._tick)
            timer.daemon = True
            with self._lock:
                if not self._on:
                    return
                old, self._timer = self._timer, timer
            if old is not None:
                old.cancel()
            timer.start()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Cannot arm the working set timer (%s)", type(exc).__name__)

    def _tick(self) -> None:
        with self._lock:
            if not self._on or self._closed:
                return
            me = threading.current_thread()
            self._workers.add(me)
        try:
            self.sweep()
        except Exception:  # noqa: BLE001
            LOG.exception("working set: a sweep failed")
        finally:
            with self._lock:
                self._workers.discard(me)
        self._arm(SWEEP_S)

    # -- seeding (D-11) ---------------------------------------------------------------------

    def seed(self) -> int:
        """Once, when `seeded_at` is absent: every visible chat thread updated
        within the idle limit, and every one with a linked rank 1 to 4 row.
        Returns how many joined. Adds only; never takes a thread out. A seed
        whose inbox read is not whole adds nothing and leaves `seeded_at`
        unset, so the next sweep (or start) tries again."""
        with self._lock:
            if self._seeded_at is not None:
                return 0
            limit = timedelta(days=self._idle_days)
        now = self.clock()
        rows = self._strict_rows()
        if rows is None:
            LOG.warning("The working set's seed could not read the inbox; it will try again")
            return 0
        linked = _linked(rows)
        found: dict[str, tuple[str, str]] = {}
        try:
            ids = self.daemon.stores.threads.ids()
        except Exception:  # noqa: BLE001
            LOG.warning("The working set's seed could not list the threads; it will try again")
            return 0
        with self._lock:
            before = self._generations()
        for thread_id in ids:
            thread, read = self._check_visible(thread_id)
            if not read:
                # Not a thread that is gone or hidden: one that could not be
                # read. The seed is incomplete; the next sweep tries again.
                LOG.warning("The working set's seed could not read a thread; it will try again")
                return 0
            if thread is None:
                continue
            updated = parse_time(getattr(thread, "updated", None))
            recent = updated is not None and now - updated < limit
            if recent or thread_id in linked:
                found[thread_id] = (_iso(updated) if updated is not None else _iso(now), thread.project_id)
        stamp = _iso(now)
        added = 0
        with self._lock:
            if self._seeded_at is not None or self._closed:
                return 0
            for thread_id, (last, project_id) in found.items():
                if not self._unchanged(before, thread_id, project_id):
                    continue        # archived, restored or deleted since the check
                if thread_id not in self._threads:
                    self._threads[thread_id] = {"since": stamp, "last": last, "seq": 0, "done_at": None,
                                                "done_seq": None, "done_token": None, "reason": None}
                    added += 1
            self._seeded_at = stamp
            # Nothing seeded and no file yet: `seeded_at` stays in memory, so
            # an empty start never creates `<data>/inbox/` (whose absence
            # other inbox stores' checks rely on). The next start seeds
            # again, which finds nothing new to add or only what is recent.
            if added or os.path.lexists(self.path):
                self._save_quietly()
        if added:
            self._changed()
        return added

    # -- the setting -------------------------------------------------------------------------

    def settings(self) -> dict:
        return {"idle_days": self.idle_days, "min": IDLE_DAYS_MIN, "max": IDLE_DAYS_MAX,
                "default": IDLE_DAYS_DEFAULT}

    def set_idle_days(self, value) -> dict:
        """The owner's idle limit, 1 to 30 days. ValueError for anything else;
        StoreError/OSError when it cannot be saved (nothing changes)."""
        if not valid_idle_days(value):
            raise ValueError(f"idle_days must be a whole number from {IDLE_DAYS_MIN} to {IDLE_DAYS_MAX}")
        with self._lock:
            before = self._idle_days
            self._idle_days = value
            try:
                self._save()
            except Exception:
                self._idle_days = before
                raise
        self._changed()     # no ids, no data: another window reads the limit again
        return self.settings()

    # -- the read (D-5, D-6) -----------------------------------------------------------------

    def entries(self) -> list[dict]:
        """The visible in-set entries, unordered: membership and each
        entry's `thread_id`, `project_id`, `since`, `last`, `seq` and
        `status`, and nothing else (no `done_*`, no reason, no rows). It reads
        no inbox row (WS-5a: the phone's read has none to attach), writes
        nothing and changes no membership, token or `seq`. An entry whose
        thread is archived, in an archived project, a task's or unreadable is
        left out (D-7, D-8). `view()` builds on it. Called with no lock of
        ours held."""
        with self._lock:
            snapshot = {tid: dict(e) for tid, e in self._threads.items() if e["done_at"] is None}
        out = []
        for thread_id, entry in snapshot.items():
            thread = self._visible(thread_id)
            if thread is None:
                continue
            out.append({"thread_id": thread_id, "project_id": thread.project_id,
                        "since": entry["since"], "last": entry["last"], "seq": entry["seq"],
                        "status": self._status(thread_id)})
        return out

    def view(self) -> dict:
        """`GET /working`: each entry with the rows linked to it, every other
        row in `needs_you`, so every row of one `inbox.rows()` read appears
        exactly once; `count`/`pending` are today's badge rules."""
        from . import inbox as _inbox
        rows = _inbox.rows(self.daemon, marks="flag")
        entries = {e["thread_id"]: {**e, "rows": [], "why_not": None} for e in self.entries()}
        needs_you = []
        for row in rows:
            target = entries.get(row.get("thread_id")) if isinstance(row, dict) else None
            (target["rows"] if target is not None else needs_you).append(row)
        why = self.why_not(list(entries), rows=rows) if entries else {}
        for thread_id, entry in entries.items():
            entry["why_not"] = why.get(thread_id)
        ordered = sorted(entries.values(), key=lambda e: (e["last"], e["thread_id"]), reverse=True)
        return {"entries": ordered, "needs_you": needs_you,
                "count": waiting_count(rows), "pending": any_pending(rows)}


def _public(thread_id: str, entry: dict) -> dict:
    """An entry as a response shows it: never its `done_token`."""
    return {"thread_id": thread_id, "in": entry["done_at"] is None,
            **{k: entry[k] for k in _ENTRY_FIELDS if k != "done_token"}}


# -- the routes (owner-only) -------------------------------------------------------------

def route(handler, daemon, parts, query):
    """`GET /working`, `POST /working/<id>/done {seq}`, `POST
    /working/<id>/undo {token}`, `GET`/`POST /working/settings`; None for any
    other path. `owner_only` runs first on every `/working` path, so the API
    listener and a non-owner page learn nothing."""
    if not parts or parts[0] != "working":
        return None
    from .daemon import APIError, _object
    from .projects import owner_only
    owner_only(handler, daemon)
    working = getattr(daemon, "working", None)
    if working is None:
        raise APIError(503, "the working set is not running")
    method = handler.command
    if parts == ["working"]:
        if method != "GET":
            raise APIError(405, "method not allowed")
        _object(query, ())
        return 200, working.view()
    if parts == ["working", "settings"]:
        _object(query, ())
        if method == "GET":
            return 200, working.settings()
        if method == "POST":
            body = _object(handler._body(), ("idle_days",), ("idle_days",))
            try:
                return 200, working.set_idle_days(body["idle_days"])
            except ValueError as exc:
                raise APIError(400, str(exc)) from None
            except (StoreError, OSError):
                raise APIError(503, "the setting could not be saved") from None
        raise APIError(405, "method not allowed")
    if len(parts) == 3 and parts[2] in ("done", "undo"):
        if method != "POST":
            raise APIError(405, "method not allowed")
        _object(query, ())
        if parts[2] == "done":
            body = _object(handler._body(), ("seq",), ("seq",))
            return working.done(parts[1], body["seq"])
        body = _object(handler._body(), ("token",), ("token",))
        return working.undo(parts[1], body["token"])
    raise APIError(404, "route not found")
