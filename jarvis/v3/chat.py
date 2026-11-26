"""Owner chat state, private session selection and event polling."""

from collections import deque
from contextlib import contextmanager
from copy import deepcopy
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from uuid import uuid4

from jarvis import config

from . import t3_client
from ._paths import open_directory


MAX_TEXT = 16_000
MAX_INBOX = 8
EVENT_LIMIT = 256
LISTENER_QUEUE_LIMIT = 256
HISTORY_LIMIT = 50
HISTORY_MESSAGE_LIMIT = 64_000
ERROR_MESSAGE = "The chat turn could not complete."
LOG = logging.getLogger(__name__)
CALENDAR_TOOLS = frozenset({"calendar_list", "calendar_free"})
CALENDAR_WRITES = frozenset({"calendar_create", "calendar_update", "calendar_delete"})
GATED_TOOLS = frozenset({"vault_add", "vault_patch", "vault_archive"}) | CALENDAR_WRITES
WORK_TOOLS = frozenset({"work_projects", "work_threads", "work_show", "message_list", "message_read"})
WORK_GATED = frozenset({"work_start", "work_send", "delegate"})
PC_GATED = frozenset({"app_open", "folder_open", "link_open"})
# Results that never carry text written outside this session. Thread titles and learned
# skill names can come from an earlier session that read outside text, so they are not here.
CLEAN_RESULT_TOOLS = frozenset({
    "get_datetime", "calendar_free", "work_projects", "brief_post",
}) | GATED_TOOLS | WORK_GATED | PC_GATED
TOOL_NAMES = frozenset({
    "get_datetime", "vault_list", "vault_read", "vault_search", "index_search",
    "skill_view", "skill_names", "brief_post", *CALENDAR_TOOLS,
})
_SESSION_ID = re.compile(r"[a-f0-9]{32}")
_SESSION_FILES = ("meta.json", "messages.json", "log.jsonl")
_ROLES = {"you": "user", "jarvis": "assistant"}


class ChatBusy(RuntimeError):
    """The requested action conflicts with the current chat state."""


class ChatInboxFull(ChatBusy):
    """The turn already has the maximum number of waiting messages."""


class ChatStorageError(RuntimeError):
    """The chat's private storage could not be used safely."""


def calendar_available():
    return Path(config.GOOGLE_TOKEN_PATH).is_file()


def _t3_available():
    try:
        t3_client.resolve()
    except Exception:
        return False
    return True


def _work_available(work):
    try:
        return work is not None and bool(work.projects()) and _t3_available()
    except Exception:
        return False


def available_tools(names=TOOL_NAMES, *, gated=False, work=None):
    if gated:
        names = names | GATED_TOOLS | PC_GATED
    work_available = _work_available(work)
    if work_available:
        names = names | WORK_TOOLS
        if gated:
            names = names | (WORK_GATED - {"delegate"})
    if (gated and callable(getattr(work, "plan_delegate", None))
            and (work_available or _t3_available())):
        names = names | {"delegate"}
    return sorted(names if calendar_available() else names - (CALENDAR_TOOLS | CALENDAR_WRITES))


def is_private_chat_directory(fd):
    info = os.fstat(fd)
    return info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700


def _private_directory(fd):
    if not is_private_chat_directory(fd):
        raise ChatStorageError("Chat directories must be owned by the user with mode 0700.")


def _check_files(path):
    # v1 uses these fixed temporary names for atomic replacement. Reject
    # special files and links before handing the pinned directory to v1.
    for name in (*_SESSION_FILES, "meta.json.tmp", "messages.json.tmp"):
        try:
            info = (path / name).lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ChatStorageError("Invalid chat session file.")


def _history(path):
    result = deque(maxlen=HISTORY_LIMIT)
    try:
        with (path / "log.jsonl").open(encoding="utf-8") as log:
            for line in log:
                row = json.loads(line)
                role, body, stamp = row["role"], row["text"], row["t"]
                if role not in _ROLES or not isinstance(body, str) or not _timestamp(stamp):
                    raise ValueError("Invalid chat log record.")
                message = {"role": _ROLES[role], "text": body[:HISTORY_MESSAGE_LIMIT], "t": stamp}
                if auto := row.get("auto"):
                    message["auto"] = auto
                result.append(message)
    except FileNotFoundError:
        pass
    return list(result)


def _timestamp(value):
    return type(value) in (int, float) and -math.inf < value < math.inf


def _history_outside_text(messages):
    """Return the taint of a valid transcript, or None if it cannot be resumed."""
    if not isinstance(messages, list) or not messages:
        return None
    outside_text = False
    pending = {}
    for message in messages:
        if not isinstance(message, dict):
            return None
        role, body = message.get("role"), message.get("content")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(body, str) or not isinstance(call_id, str) or call_id not in pending:
                return None
            name = pending.pop(call_id)
            if name not in CLEAN_RESULT_TOOLS or message.get("name", name) != name:
                outside_text = True
        elif pending:
            return None
        elif role == "assistant" and "tool_calls" in message:
            calls = message["tool_calls"]
            if not isinstance(calls, list) or not calls or (body is not None and not isinstance(body, str)):
                return None
            for call in calls:
                function = call["function"]
                call_id = call["id"]
                if (not isinstance(call_id, str) or call_id in pending or call["type"] != "function"
                        or not isinstance(function["name"], str) or not isinstance(function["arguments"], str)):
                    return None
                pending[call_id] = function["name"]
        elif role not in ("user", "assistant") or not isinstance(body, str):
            return None
    return None if pending else outside_text


def _read_meta(path, session_id):
    meta = json.loads((path / "meta.json").read_bytes())
    if (not isinstance(meta, dict) or meta.get("id") != session_id
            or not _timestamp(meta.get("updated")) or not _timestamp(meta.get("created"))
            or not _timestamp(meta.get("cost_usd")) or type(meta.get("turns")) is not int
            or meta["turns"] < 0 or not isinstance(meta.get("title"), str)):
        raise ValueError("Invalid chat session metadata.")
    return meta


_BAD_SESSION = (ChatStorageError, OSError, ValueError, TypeError, KeyError, RecursionError)


class ChatService:
    def __init__(self, root, *, completion=None, clock=time.time, feed_root=None,
                 approvals=None, calendar=None, work=None):
        self.root = Path(root).expanduser().absolute()
        self._feed_root = self.root.parent / "feed" if feed_root is None else Path(feed_root)
        self._completion = completion
        self._clock = clock
        self.approvals = approvals
        self._calendar = calendar
        self.work = work
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._stop = threading.Event()
        self._worker = None
        self._state = "idle"
        self._session_id = None
        self._outside_text = False
        self._turn_id = None
        self._phone_input = False
        self._turn_auto = []
        self._selected = False
        self._seq = 0
        self._events = deque(maxlen=EVENT_LIMIT)
        self._listeners = {}
        self._finished_turns = deque(maxlen=EVENT_LIMIT)
        self._prompt = None
        self._delivered = []
        self._inbox = []
        self._queued = []
        self._taken = 0
        self._pending_followup = []
        self._draft = ""
        self._last_error = None
        self._last_activity = None

    def subscribe(self, callback):
        """Receive ordered event dictionaries with session and turn IDs.

        Streaming deltas and other listener-only notifications have no
        polling sequence number. Each listener runs on its own thread outside
        the chat lock. Unsubscribing discards queued events. A callback already
        in progress may finish.
        """
        ready = threading.Condition(self._lock)
        with self._lock:
            if self._closed.is_set():
                raise ChatBusy("Chat is closed.")
            self._listeners[ready] = deque()
            try:
                threading.Thread(target=self._listen, args=(ready, callback),
                                 name="jarvis-v3-chat-listener", daemon=True).start()
            except RuntimeError:
                self._unsubscribe(ready)
                raise

        def unsubscribe():
            with self._lock:
                self._unsubscribe(ready)

        return unsubscribe

    def _unsubscribe(self, ready):
        pending = self._listeners.pop(ready, None)
        if pending is not None:
            pending.clear()
        ready.notify()

    def _listen(self, ready, callback):
        while True:
            with ready:
                ready.wait_for(lambda: ready not in self._listeners or self._listeners[ready])
                pending = self._listeners.get(ready)
                if pending is None:
                    return
                row = pending.popleft()
            event = deepcopy(row)
            with self._lock:
                if ready not in self._listeners:
                    return
            try:
                callback(event)
            except BaseException:
                with self._lock:
                    self._unsubscribe(ready)
                LOG.warning("A chat event listener failed and was removed.")
                return

    def _publish(self, row, **fields):
        if self._listeners:
            event = {**row, **fields, "turn": self._turn_id, "session_id": self._session_id}
            for ready, pending in tuple(self._listeners.items()):
                if len(pending) >= LISTENER_QUEUE_LIMIT:
                    self._unsubscribe(ready)
                    LOG.warning("A chat event listener overflowed and was removed.")
                else:
                    pending.append(event)
                    ready.notify()

    @contextmanager
    def _directory(self, *, create=False):
        try:
            with open_directory(self.root, create=create) as fd:
                _private_directory(fd)
                yield fd
        except OSError as exc:
            if isinstance(exc, FileNotFoundError) and not create:
                raise
            raise ChatStorageError("Chat storage is unavailable.") from None

    @contextmanager
    def _session(self, root_fd, sid):
        fd = os.open(sid, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            _private_directory(fd)
            path = Path(f"/proc/self/fd/{fd}")
            _check_files(path)
            yield path
        finally:
            os.close(fd)

    def _select(self, root_fd):
        if self._selected:
            return
        self._selected = True
        # Each v1 metadata replacement updates its directory mtime. Rank by
        # that mtime so a corrupt newest session does not silently select an
        # older conversation.
        newest = None
        with os.scandir(root_fd) as entries:
            for entry in entries:
                if not _SESSION_ID.fullmatch(entry.name):
                    continue
                try:
                    candidate = (entry.stat(follow_symlinks=False).st_mtime_ns, entry.name)
                except FileNotFoundError:
                    continue
                if newest is None or candidate > newest:
                    newest = candidate
        if newest is not None:
            _, sid = newest
            try:
                with self._session(root_fd, sid) as path:
                    meta = _read_meta(path, sid)
                    history_taint = False
                    if meta["turns"]:
                        history_taint = _history_outside_text(json.loads((path / "messages.json").read_bytes()))
                        if history_taint is None or not _history(path):
                            return
                    elif any((path / name).exists() for name in _SESSION_FILES[1:]):
                        # A crash may leave part of the first turn on disk.
                        return
                self._session_id = sid
                # Old transcripts may have lost results to compaction, Stop or a failed turn.
                saved_taint = meta.get("outside_text", meta["turns"] > 0)
                self._outside_text = history_taint or saved_taint is not False
            except _BAD_SESSION:
                pass

    def _new_session(self, root_fd):
        sid = uuid4().hex
        os.mkdir(sid, mode=0o700, dir_fd=root_fd)
        now = self._clock()
        with self._session(root_fd, sid) as path, (path / "meta.json").open("x", encoding="utf-8") as meta:
            json.dump({"id": sid, "created": now, "updated": now, "turns": 0,
                       "cost_usd": 0.0, "title": "", "surface": "v3-chat", "outside_text": False}, meta)
        self._session_id = sid
        self._outside_text = False
        self._selected = True
        return sid

    def snapshot(self):
        with self._lock:
            history = []
            try:
                with self._directory() as root_fd:
                    self._select(root_fd)
                    if self._session_id is not None:
                        with self._session(root_fd, self._session_id) as path:
                            history = _history(path)
            except FileNotFoundError:
                if self._session_id is not None:
                    raise ChatStorageError("Chat session is unavailable.") from None
            return {"state": self._state, "session_id": self._session_id, "seq": self._seq,
                    "draft": self._draft, "tools": available_tools(gated=self.approvals is not None, work=self.work),
                    "history": history, "last_error": self._last_error, "outside_text": self._outside_text,
                    "replay": self._replay(history),
                    "approvals": self._approval_count()}

    def _replay(self, history):
        # Replay complete buffered turns over the corresponding suffix of
        # saved messages. An unfinished turn has no messages on disk yet.
        events = []
        history_size = len(history)
        first = self._events[0]["seq"] if self._events else self._seq + 1
        start = self._prompt["seq"] if self._prompt is not None else self._seq + 1
        for turn_start, saved in reversed(self._finished_turns):
            if turn_start < first or saved > history_size:
                break
            start = turn_start
            history_size -= saved
        if self._prompt is not None and self._prompt["seq"] < first:
            events.append(deepcopy(self._prompt))
        events.extend(deepcopy(row) for row in self._events if row["seq"] >= start)
        for row in events:
            if row["kind"] == "reply":
                row["text"] = row["text"][:HISTORY_MESSAGE_LIMIT]
        replay = {"history_prefix": history_size, "events": events}
        saved = []
        for index, message in enumerate(history):
            auto = message.pop("auto", [])
            if index < history_size:
                saved.extend(dict(event, after=index) for event in auto)
        if saved:
            replay["saved"] = saved
        return replay

    def _approval_count(self):
        return 0 if self.approvals is None else len(self.approvals.pending())

    def approval_snapshot(self):
        with self._lock:
            return {"session_id": self._session_id,
                    "approvals": [] if self.approvals is None else self.approvals.pending()}

    def _emit(self, kind, *, stamp=None, listener_fields=None, **fields):
        self._seq += 1
        row = {"seq": self._seq, "t": self._clock() if stamp is None else stamp, "kind": kind, **fields}
        self._events.append(row)
        self._publish(row, **(listener_fields or {}))
        return row

    def events(self, after):
        with self._lock:
            return {"state": self._state, "seq": self._seq, "session_id": self._session_id,
                    "outside_text": self._outside_text,
                    "gap": bool(self._events and after < self._events[0]["seq"] - 1),
                    "events": [deepcopy(row) for row in self._events if row["seq"] > after],
                    "approvals": self._approval_count()}

    def send(self, text, *, steer=True, input_id=None, fresh_after=None, phone=False) -> dict:
        """Accept a message with optional listener correlation and idle rollover."""
        if (not isinstance(text, str) or len(text) > MAX_TEXT or not text.strip()
                or "\0" in text or re.search(r"[\ud800-\udfff]", text)):
            raise ValueError("Invalid chat message.")
        with self._lock:
            if self._closed.is_set() or self._state == "stopping":
                raise ChatBusy("Chat is stopping or closed.")
            fresh = fresh_after is not None and self._idle_seconds() >= fresh_after
            self._last_activity = self._clock()
            if self._state == "running":
                if len(self._inbox) + len(self._queued) + len(self._pending_followup) >= MAX_INBOX:
                    raise ChatInboxFull("The chat steering queue is full.")
                if steer:
                    self._phone_input |= phone
                    row = self._emit("steer", text=text, listener_fields={"input_id": input_id})
                    self._inbox.append(dict(row, input_id=input_id))
                    return {"state": "running", "steered": True}
                row = self._emit("steer", text=text, queued=True, listener_fields={"input_id": input_id})
                self._queued.append(dict(row, phone=phone))
                return {"state": "running", "steered": False}

            with self._directory(create=True) as root_fd:
                self._select(root_fd)
                sid = self._reset_session(root_fd) if fresh else (self._session_id or self._new_session(root_fd))
            inputs = [text]
            self._begin(inputs, waiting=[], input_id=input_id, phone=phone)
            self._worker = threading.Thread(target=self._run, args=(sid, inputs),
                                            name="jarvis-v3-chat", daemon=True)
            try:
                self._worker.start()
            except RuntimeError:
                self._worker = None
                self._error()
                raise
            return {"state": "running", "steered": False}

    def _begin(self, inputs, *, waiting, input_id=None, phone=False):
        self._stop.clear()
        self._turn_id = uuid4().hex
        self._phone_input = phone or any(row.get("phone") for row in waiting)
        self._turn_auto = []
        self._taken = 0
        self._delivered = []
        self._pending_followup = waiting
        self._draft = ""
        self._last_error = None
        self._state = "running"
        fields = {"inputs": [{key: row[key] for key in ("seq", "text")} for row in waiting]} if waiting else {}
        self._prompt = self._emit("user", text="\n\n".join(inputs),
                                  listener_fields={"input_id": input_id}, **fields)

    def _complete_turn(self, saved):
        if self._prompt is not None:
            self._finished_turns.append((self._prompt["seq"], saved))
            self._prompt = None

    def _error(self, saved=0):
        self._state = "error"
        self._draft = ""
        self._last_error = ERROR_MESSAGE
        self._emit("error", message=self._last_error, **self._discard_steers())
        self._complete_turn(saved)

    def _take_steering(self):
        with self._lock:
            if self._stop.is_set():
                return []
            self._taken = len(self._inbox)
            return [(row["text"], None, "") if row["input_id"] is not None else row["text"]
                    for row in self._inbox]

    def _admit_completion(self):
        with self._lock:
            if self._stop.is_set() or self._closed.is_set():
                return None
            delivered = self._inbox[:self._taken]
            del self._inbox[:self._taken]
            self._taken = 0
            self._pending_followup = []
            self._delivered.extend(delivered)
            if delivered:
                self._publish({"kind": "steering_delivered", "inputs": [row["seq"] for row in delivered]})
            return [row["text"] for row in delivered]

    def _on_event(self, kind, **fields):
        with self._lock:
            if self._closed.is_set():
                return
            if self._stop.is_set() and kind != "auto":
                # A tool may already be returning when Stop is requested.
                # Listeners can account for it without adding to HUD replay.
                if kind == "tool_end":
                    self._publish({"kind": kind, **fields})
                return
            if kind == "delta":
                self._draft += fields["text"]
                self._publish({"kind": kind, **fields})
            elif kind in ("approval_pending", "step_end"):
                self._publish({"kind": kind, **fields})
            elif kind == "auto":
                self._turn_auto.append(self._emit(kind, **fields))
            else:
                listener_fields = {key: fields.pop(key) for key in ("outcome", "changes_state")
                                   if key in fields}
                self._emit(kind, listener_fields=listener_fields, **fields)

    def pending_approvals(self, turn):
        return [] if self.approvals is None else self.approvals.pending(turn_id=turn)

    @contextmanager
    def _tool_write(self, *, allow_stopped=False):
        with self._lock:
            yield not self._closed.is_set() and (allow_stopped or not self._stop.is_set())

    def _drain_waiting(self):
        waiting = sorted((*self._inbox, *self._queued), key=lambda row: row["seq"])
        self._inbox.clear()
        self._queued.clear()
        return waiting

    def _discard_steers(self):
        discarded = [row["seq"] for row in (*self._pending_followup, *self._drain_waiting())]
        self._taken = 0
        self._pending_followup = []
        return {"dropped": len(discarded), "discarded": discarded}

    def _finish_stopped(self, saved=0):
        self._draft = ""
        self._state = "idle"
        self._emit("stopped", **self._discard_steers())
        self._complete_turn(saved)

    def _record_outside_text(self, session):
        with self._lock:
            self._outside_text = True
            session.meta["outside_text"] = True
            if not self._closed.is_set():
                session.save_meta()

    def _run(self, sid, inputs):
        while True:
            try:
                from .fastpath import run_turn

                with self._directory() as root_fd, self._session(root_fd, sid) as path:
                    meta = _read_meta(path, sid)
                    meta["outside_text"] = self._outside_text
                    session = run_turn(
                        sid, path, meta, text="\n\n".join(inputs), data_dir=self.root.parent,
                        feed_root=self._feed_root, clock=self._clock, completion=self._completion,
                        stop=lambda: self._stop.is_set() or self._closed.is_set(),
                        take_steering=self._take_steering,
                        admit_completion=self._admit_completion, emit=self._on_event,
                        tool_write=self._tool_write, record_outside_text=self._record_outside_text,
                        phone_input=lambda: self._phone_input,
                        turn_id=self._turn_id,
                        approvals=self.approvals, calendar=self._calendar, work=self.work,
                    )
                    with self._lock:
                        if self._closed.is_set():
                            return
                        self._last_activity = self._clock()
                        self._draft = ""
                        turn = session.turn
                        stopped = self._stop.is_set() or (turn is not None and turn.cancelled)
                        # Failed or stopped follow-ups may never have reached the provider.
                        unfinished = stopped or turn is None
                        recorded = inputs[len(self._pending_followup):] if unfinished else inputs
                        saved = 0
                        reply_stamp = self._clock()
                        if recorded:
                            saved = session.save(recorded, stopped=stopped, prompt=self._prompt,
                                                 delivered=self._delivered, reply_stamp=reply_stamp,
                                                 auto=self._turn_auto)
                        if stopped:
                            self._finish_stopped(saved)
                            return
                        if turn is None:
                            self._error(saved)
                            return
                        self._emit("reply", stamp=reply_stamp, text=turn.text)
                        self._complete_turn(saved)
                        waiting = self._drain_waiting()
                        if not waiting:
                            self._state = "idle"
                            return
                        inputs = [row["text"] for row in waiting]
                        self._begin(inputs, waiting=waiting)
            except BaseException:
                # This worker is the exception boundary for the turn. A
                # provider failure never escapes in a thread traceback.
                with self._lock:
                    if self._closed.is_set():
                        return
                    self._last_activity = self._clock()
                    # A storage failure can leave a partial append. Fall back
                    # to disk rather than overlaying turns at an unknown seam.
                    self._finished_turns.clear()
                    if self._stop.is_set():
                        self._finish_stopped()
                    else:
                        self._error()
                return

    def stop(self):
        with self._lock:
            self._request_stop()
            if self._state == "error" and not self._closed.is_set():
                last = self._events[-1]
                if last["kind"] == "error" and last["dropped"]:
                    # A late Stop acknowledges the messages already dropped by this error.
                    self._emit("stopped", dropped=last["dropped"], error_seq=last["seq"])
            return {"state": self._state}

    def _request_stop(self):
        if self._state == "running":
            self._state = "stopping"
            self._stop.set()
            if self.approvals is not None:
                self.approvals.cancel(self._turn_id)

    def new_session(self):
        with self._lock:
            if self._state != "idle" or self._closed.is_set():
                raise ChatBusy("Chat is not idle.")
            with self._directory(create=True) as root_fd:
                sid = self._reset_session(root_fd)
            return {"session_id": sid}

    def _reset_session(self, root_fd):
        sid = self._new_session(root_fd)
        self._inbox.clear()
        self._queued.clear()
        self._events.clear()
        self._finished_turns.clear()
        return sid

    def idle_seconds(self):
        with self._lock:
            return self._idle_seconds()

    def _idle_seconds(self):
        if self._state in ("running", "stopping"):
            return 0.0
        if self._last_activity is None:
            return math.inf
        return max(0.0, self._clock() - self._last_activity)

    def close(self, grace_s=5.0):
        with self._lock:
            self._closed.set()
            for ready in tuple(self._listeners):
                self._unsubscribe(ready)
            self._request_stop()
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(grace_s)
        with self._lock:
            if self._state == "stopping":
                self._finish_stopped()
        return worker is None or not worker.is_alive()
