"""Owner chat state, private session selection and event polling."""

from collections import deque
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from uuid import uuid4

from jarvis import config

from ._paths import open_directory


MAX_TEXT = 16_000
MAX_INBOX = 8
EVENT_LIMIT = 256
HISTORY_LIMIT = 50
HISTORY_MESSAGE_LIMIT = 64_000
ERROR_MESSAGE = "The chat turn could not complete."
CALENDAR_TOOLS = frozenset({"calendar_list", "calendar_free"})
CALENDAR_WRITES = frozenset({"calendar_create", "calendar_update", "calendar_delete"})
GATED_TOOLS = frozenset({"vault_add", "vault_patch", "vault_archive"}) | CALENDAR_WRITES
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
    """The turn already has the maximum number of waiting steers."""


class ChatStorageError(RuntimeError):
    """The chat's private storage could not be used safely."""


def calendar_available():
    return Path(config.GOOGLE_TOKEN_PATH).is_file()


def available_tools(names=TOOL_NAMES, *, gated=False):
    if gated:
        names = names | GATED_TOOLS
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
                result.append({"role": _ROLES[role], "text": body[:HISTORY_MESSAGE_LIMIT], "t": stamp})
    except FileNotFoundError:
        pass
    return list(result)


def _timestamp(value):
    return type(value) in (int, float) and -math.inf < value < math.inf


def _valid_messages(messages):
    if not isinstance(messages, list) or not messages:
        return False
    pending = set()
    for message in messages:
        if not isinstance(message, dict):
            return False
        role, body = message.get("role"), message.get("content")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(body, str) or not isinstance(call_id, str) or call_id not in pending:
                return False
            pending.remove(call_id)
        elif pending:
            return False
        elif role == "assistant" and "tool_calls" in message:
            calls = message["tool_calls"]
            if not isinstance(calls, list) or not calls or (body is not None and not isinstance(body, str)):
                return False
            for call in calls:
                function = call["function"]
                call_id = call["id"]
                if (not isinstance(call_id, str) or call_id in pending or call["type"] != "function"
                        or not isinstance(function["name"], str) or not isinstance(function["arguments"], str)):
                    return False
                pending.add(call_id)
        elif role not in ("user", "assistant") or not isinstance(body, str):
            return False
    return not pending


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
                 approvals=None, calendar=None):
        self.root = Path(root).expanduser().absolute()
        self._feed_root = self.root.parent / "feed" if feed_root is None else Path(feed_root)
        self._completion = completion
        self._clock = clock
        self.approvals = approvals
        self._calendar = calendar
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._stop = threading.Event()
        self._worker = None
        self._state = "idle"
        self._session_id = None
        self._turn_id = None
        self._selected = False
        self._seq = 0
        self._events = deque(maxlen=EVENT_LIMIT)
        self._inbox = []
        self._taken = 0
        self._pending_followup = 0
        self._draft = ""
        self._last_error = None
        self._last_activity = None

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
                    if meta["turns"]:
                        if not _valid_messages(json.loads((path / "messages.json").read_bytes())) or not _history(path):
                            return
                    elif any((path / name).exists() for name in _SESSION_FILES[1:]):
                        # A crash may leave part of the first turn on disk.
                        return
                self._session_id = sid
            except _BAD_SESSION:
                pass

    def _new_session(self, root_fd):
        sid = uuid4().hex
        os.mkdir(sid, mode=0o700, dir_fd=root_fd)
        now = self._clock()
        with self._session(root_fd, sid) as path, (path / "meta.json").open("x", encoding="utf-8") as meta:
            json.dump({"id": sid, "created": now, "updated": now, "turns": 0,
                       "cost_usd": 0.0, "title": "", "surface": "v3-chat"}, meta)
        self._session_id = sid
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
                    "draft": self._draft, "tools": available_tools(gated=self.approvals is not None),
                    "history": history, "last_error": self._last_error,
                    "approvals": self._approval_count()}

    def _approval_count(self):
        return 0 if self.approvals is None else len(self.approvals.pending())

    def _emit(self, kind, **fields):
        self._seq += 1
        self._events.append({"seq": self._seq, "t": self._clock(), "kind": kind, **fields})

    def events(self, after):
        with self._lock:
            return {"state": self._state, "seq": self._seq,
                    "gap": bool(self._events and after < self._events[0]["seq"] - 1),
                    "events": [row.copy() for row in self._events if row["seq"] > after],
                    "approvals": self._approval_count()}

    def send(self, text):
        if (not isinstance(text, str) or len(text) > MAX_TEXT or not text.strip()
                or "\0" in text or re.search(r"[\ud800-\udfff]", text)):
            raise ValueError("Invalid chat message.")
        with self._lock:
            if self._closed.is_set() or self._state == "stopping":
                raise ChatBusy("Chat is stopping or closed.")
            self._last_activity = self._clock()
            if self._state == "running":
                if len(self._inbox) + self._pending_followup >= MAX_INBOX:
                    raise ChatInboxFull("The chat steering queue is full.")
                self._inbox.append(text)
                self._emit("steer", text=text)
                return {"state": "running", "steered": True}

            with self._directory(create=True) as root_fd:
                self._select(root_fd)
                sid = self._session_id or self._new_session(root_fd)
            inputs = [text]
            self._begin(inputs, waiting=0)
            self._worker = threading.Thread(target=self._run, args=(sid, inputs),
                                            name="jarvis-v3-chat", daemon=True)
            try:
                self._worker.start()
            except RuntimeError:
                self._worker = None
                self._error()
                raise
            return {"state": "running", "steered": False}

    def _begin(self, inputs, *, waiting):
        self._stop.clear()
        self._turn_id = uuid4().hex if self.approvals is not None else None
        self._taken = 0
        self._pending_followup = waiting
        self._draft = ""
        self._last_error = None
        self._state = "running"
        self._emit("user", text="\n\n".join(inputs))

    def _error(self):
        self._state = "error"
        self._draft = ""
        self._last_error = ERROR_MESSAGE
        self._emit("error", message=self._last_error, dropped=self._discard_steers())

    def _take_steering(self):
        with self._lock:
            if self._stop.is_set():
                return []
            self._taken = len(self._inbox)
            return self._inbox.copy()

    def _admit_completion(self):
        with self._lock:
            if self._stop.is_set() or self._closed.is_set():
                return None
            delivered = self._inbox[:self._taken]
            del self._inbox[:self._taken]
            self._taken = 0
            self._pending_followup = 0
            return delivered

    def _on_event(self, kind, **fields):
        with self._lock:
            if self._stop.is_set() or self._closed.is_set():
                return
            if kind == "delta":
                self._draft += fields["text"]
            else:
                self._emit(kind, **fields)

    @contextmanager
    def _tool_write(self, *, allow_stopped=False):
        with self._lock:
            yield not self._closed.is_set() and (allow_stopped or not self._stop.is_set())

    def _discard_steers(self):
        dropped = len(self._inbox) + self._pending_followup
        self._inbox.clear()
        self._taken = 0
        self._pending_followup = 0
        return dropped

    def _finish_stopped(self):
        self._draft = ""
        self._state = "idle"
        self._emit("stopped", dropped=self._discard_steers())

    def _run(self, sid, inputs):
        while True:
            try:
                from .fastpath import run_turn

                with self._directory() as root_fd, self._session(root_fd, sid) as path:
                    session = run_turn(
                        sid, path, _read_meta(path, sid), text="\n\n".join(inputs), data_dir=self.root.parent,
                        feed_root=self._feed_root, clock=self._clock, completion=self._completion,
                        stop=lambda: self._stop.is_set() or self._closed.is_set(),
                        take_steering=self._take_steering,
                        admit_completion=self._admit_completion, emit=self._on_event,
                        tool_write=self._tool_write, turn_id=self._turn_id,
                        approvals=self.approvals, calendar=self._calendar,
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
                        recorded = inputs[self._pending_followup:] if unfinished else inputs
                        if recorded:
                            session.save(recorded, stopped=stopped)
                        if stopped:
                            self._finish_stopped()
                            return
                        if turn is None:
                            self._error()
                            return
                        self._emit("reply", text=turn.text)
                        if not self._inbox:
                            self._state = "idle"
                            return
                        inputs, self._inbox = self._inbox, []
                        self._begin(inputs, waiting=len(inputs))
            except BaseException:
                # This worker is the exception boundary for the turn. A
                # provider failure never escapes in a thread traceback.
                with self._lock:
                    if self._closed.is_set():
                        return
                    self._last_activity = self._clock()
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
                sid = self._new_session(root_fd)
            self._inbox.clear()
            return {"session_id": sid}

    def idle_seconds(self):
        with self._lock:
            if self._state in ("running", "stopping"):
                return 0.0
            if self._last_activity is None:
                return math.inf
            return max(0.0, self._clock() - self._last_activity)

    def close(self, grace_s=5.0):
        with self._lock:
            self._closed.set()
            self._request_stop()
            worker = self._worker
        if worker is not None:
            worker.join(grace_s)
        with self._lock:
            if self._state == "stopping":
                self._finish_stopped()
        return worker is None or not worker.is_alive()
