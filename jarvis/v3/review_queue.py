"""Drain turn-end references and return due reviews to the caller.

One daemon owns the consumer. A second live consumer may repeat a batch.
"""

from contextlib import ExitStack, contextmanager, suppress
import ctypes
from datetime import datetime
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import struct
import time
from uuid import uuid4

from jarvis import config
from jarvis.credential_patterns import holds_credential
from jarvis.tools.files import credential_folder_judge
from jarvis.tools.secrets import is_credential_name

from .hooks.turn_end import absolute_reference, transcript_reference, valid_session
from .index import open_directory
from .ingest.transcripts import _batch, _fingerprint


SKILL_STEPS = 10
MEMORY_TURNS = 10
MAX_AGE_S = 7 * 86400
_STATE_AGE_S = 30 * 86400
_NS_PER_SECOND = 10**9
_SCAN_BYTES = 16 << 20
_ITEM_BYTES = 64 << 10
_JOURNAL_BYTES = 32 << 20  # Up to two reviews per queue item, with JSON-escaped paths.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_ITEM_KEYS = {"provider", "session_id", "transcript", "cwd", "ended_at"}
_REVIEW_KEYS = {"provider", "session_id", "transcript", "kind", "tool_steps", "user_turns"}
_PENDING = ".pending"
_STATE_NAME = re.compile(r"[0-9a-f]{64}\.json")
_PROGRESS_KEYS = {"skill", "memory", "size", "first_seen", "last_seen"}
_STATX = getattr(ctypes.CDLL(None), "statx", None)
_AT_EMPTY_PATH = 0x1000
_STATX_BTIME = 0x0800
# Receipts are local to this process (including across Consumer instances).
# A restart replays the outstanding batch before advancing to new items.
_returned = set()


class _Unusable(ValueError):
    """An input which must be discarded without reading its contents."""


class _ScanPending(ValueError):
    """More bytes remain in a transcript than this count can read at once."""

    def __init__(self, cursor):
        super().__init__("Transcript exceeds the per-count byte budget.")
        self.cursor = cursor


def _uint(value):
    return type(value) is int and value >= 0


def _steps(row, provider):
    if not isinstance(row, dict) or row.get("isSidechain") or row.get("isMeta"):
        return 0, 0
    if provider == "codex":
        item = row.get("payload")
        if row.get("type") != "response_item" or not isinstance(item, dict) or item.get("isSidechain"):
            return 0, 0
        kind = item.get("type")
        return (int(kind in ("function_call", "custom_tool_call") and isinstance(item.get("name"), str)),
                int(kind == "message" and item.get("role") == "user"))
    role, message = row.get("type"), row.get("message")
    if role not in ("assistant", "user") or not isinstance(message, dict) or message.get("role") != role:
        return 0, 0
    content = message.get("content")
    if isinstance(content, str):
        return 0, int(role == "user")
    if not isinstance(content, list):
        return 0, 0
    blocks = [block for block in content if isinstance(block, dict)]
    if role == "user":
        return 0, int(any(block.get("type") in ("text", "image", "document") for block in blocks))
    return sum(block.get("type") == "tool_use" and isinstance(block.get("name"), str) for block in blocks), 0


@contextmanager
def _regular(directory, name):
    # Inspect before opening for data so a FIFO or device cannot block us.
    fd = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise _Unusable("Expected an owner-controlled regular file with one link.")
        with open(f"/proc/self/fd/{fd}", "rb") as stream:
            yield stream, info
    finally:
        os.close(fd)


def _credential_path(path, judge):
    return (any(is_credential_name(part) for part in path.parts)
            or judge(path) or holds_credential(str(path)))


def _path(value):
    # The no-follow open checks all descendants from the filesystem root.
    # Normalize redundant separators and dots for the corresponding state key.
    return Path("/" + value.lstrip("/"))


def _valid_cursor(cursor):
    return (isinstance(cursor, dict) and {"dev", "ino", "offset", "tools", "turns", "skipping", "sample"} == cursor.keys()
            and all(_uint(cursor[k]) for k in ("dev", "ino", "offset", "tools", "turns"))
            and isinstance(cursor["skipping"], bool) and isinstance(cursor["sample"], str))


def _snapshot(stream, info, offset):
    return [info.st_dev, info.st_ino, offset, _fingerprint(stream, offset).hex()]


def _matches_snapshot(stream, info, snapshot):
    dev, ino, offset, sample = snapshot
    return ((info.st_dev, info.st_ino) == (dev, ino) and offset <= info.st_size
            and _fingerprint(stream, offset).hex() == sample)


def _valid_snapshot(snapshot):
    return (isinstance(snapshot, list) and len(snapshot) == 4
            and all(_uint(value) and value < 1 << 64 for value in snapshot[:3])
            and isinstance(snapshot[3], str)
            and (re.fullmatch(r"[0-9a-f]{32}", snapshot[3]) if snapshot[2] else snapshot[3] == ""))


@contextmanager
def _open_transcript(path, judge):
    if _credential_path(path, judge):
        raise _Unusable("Credential path.")
    with open_directory(path.parent) as directory, _regular(directory, path.name) as opened:
        yield opened


def _scan(provider, stream, info, cursor=None):
    offset = tools = turns = 0
    skipping = False
    if (_valid_cursor(cursor) and (info.st_dev, info.st_ino) == (cursor["dev"], cursor["ino"])
            and cursor["offset"] <= info.st_size
            and _fingerprint(stream, cursor["offset"]).hex() == cursor["sample"]):
        offset, tools, turns, skipping = (cursor[k] for k in ("offset", "tools", "turns", "skipping"))
    end = min(info.st_size, offset + _SCAN_BYTES)
    while offset < end:
        position, skipping, lines, _ = _batch(stream, end, offset, skipping, lambda: False)
        for line in lines:
            if line is None:
                continue
            try:
                row = json.loads(line.decode("utf-8"))
            except (ValueError, RecursionError):
                continue
            steps, users = _steps(row, provider)
            tools += steps
            turns += users
        if position == offset:
            break  # The final small line has no newline yet.
        offset = position
    if end < info.st_size:
        raise _ScanPending({"dev": info.st_dev, "ino": info.st_ino, "offset": offset,
                            "tools": tools, "turns": turns, "skipping": skipping,
                            "sample": _fingerprint(stream, offset).hex()})
    return tools, turns, offset


def count(provider, transcript) -> tuple[int, int]:
    """Count a stable-size snapshot. A large snapshot needs consumer ticks."""
    if provider not in ("claude", "codex"):
        return 0, 0
    try:
        path = os.fspath(transcript)
        if not transcript_reference(path):
            return 0, 0
        with _open_transcript(_path(path), credential_folder_judge()) as (stream, info):
            tools, turns, _ = _scan(provider, stream, info)
            return tools, turns
    except (OSError, _Unusable):
        return 0, 0


def _read_json(directory, name, limit):
    with _regular(directory, name) as (stream, info):
        if info.st_size <= limit:
            raw = stream.read(limit + 1)
            if len(raw) <= limit:
                return json.loads(raw)
    raise _Unusable("Oversized queue or state file.")


def _write_json(directory, name, data):
    raw = json.dumps(data).encode("ascii")
    if name == _PENDING and len(raw) > _JOURNAL_BYTES:
        raise ValueError("Review journal exceeds its byte budget.")
    temporary = f".state-{uuid4().hex}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(fd, 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(fd)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


def _remove(directory, name):
    with suppress(FileNotFoundError):
        os.unlink(name, dir_fd=directory)


def _stamp(item):
    if not isinstance(item, dict) or item.keys() != _ITEM_KEYS:
        raise _Unusable("Wrong queue shape.")
    provider, transcript = item["provider"], item["transcript"]
    if (not valid_session(item["session_id"]) or not absolute_reference(item["cwd"])
            or not ((provider == "codex" and transcript is None)
                    or (provider == "claude" and transcript_reference(transcript)))
            or not isinstance(item["ended_at"], str)):
        raise _Unusable("Malformed queue item.")
    stamp = datetime.fromisoformat(item["ended_at"])
    if stamp.tzinfo is None:
        raise _Unusable("Queue timestamp needs a timezone.")
    return stamp.timestamp()


def _codex_matches(directory, session, depth=0):
    with os.scandir(directory) as entries:
        for entry in entries:
            name = entry.name
            if is_credential_name(name):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth < 3:
                        child = os.open(name, _DIR_FLAGS, dir_fd=directory)
                        try:
                            for match in _codex_matches(child, session, depth + 1):
                                yield Path(name) / match
                        finally:
                            os.close(child)
                elif (name == f"{session}.jsonl" or name.endswith(f"-{session}.jsonl")) and entry.is_file(follow_symlinks=False):
                    yield Path(name)
            except OSError:
                continue


def _birthtime(stream):
    # Linux's statx ABI includes birth time; Python's fstat on Linux does not.
    # Query the pinned descriptor. Use first_seen if the filesystem lacks it.
    if _STATX is not None:
        data = ctypes.create_string_buffer(256)
        if _STATX(stream.fileno(), b"", _AT_EMPTY_PATH, _STATX_BTIME, data) == 0:
            mask, = struct.unpack_from("=I", data)
            if mask & _STATX_BTIME:
                seconds, nanos = struct.unpack_from("=qI", data, 0x50)
                return seconds * _NS_PER_SECOND + nanos
    return None


def _state_name(provider, path, stream, info):
    identity = (provider, str(path), info.st_dev, info.st_ino, _birthtime(stream))
    return sha256(json.dumps(identity).encode("ascii")).hexdigest() + ".json"


def _valid_progress(value):
    return (isinstance(value, dict) and _PROGRESS_KEYS <= value.keys() <= _PROGRESS_KEYS | {"scan", "snapshot"}
            and all(_uint(value[key]) for key in _PROGRESS_KEYS)
            and ("scan" not in value or _valid_cursor(value["scan"]))
            and ("snapshot" not in value or _valid_snapshot(value["snapshot"])))


def _legacy_progress(value):
    if not (isinstance(value, dict)
            and {"skill", "memory"} <= value.keys() <= {"skill", "memory", "scan", "file"}
            and _uint(value["skill"]) and _uint(value["memory"])):
        return False
    file_id = value.get("file", [])
    return "file" not in value or (isinstance(file_id, list) and len(file_id) == 2 and all(map(_uint, file_id)))


def _initial_progress(seen_at):
    return {"skill": 0, "memory": 0, "size": 0, "first_seen": seen_at, "last_seen": seen_at}


def _state(directory, name):
    try:
        value = _read_json(directory, name, _ITEM_BYTES)
        if _valid_progress(value):
            return value
    except (FileNotFoundError, ValueError, RecursionError):
        pass
    return None


def _prune(directory, seen_at):
    cutoff = seen_at - _STATE_AGE_S * _NS_PER_SECOND
    removed = False
    for name in os.listdir(directory):
        if not _STATE_NAME.fullmatch(name):
            continue
        try:
            progress = _state(directory, name)
            last_seen = (progress["last_seen"] if progress is not None
                         else os.stat(name, dir_fd=directory, follow_symlinks=False).st_mtime_ns)
            if last_seen <= cutoff:
                _remove(directory, name)
                removed = True
        except OSError:
            continue
    if removed:
        os.fsync(directory)


def _valid_review(item):
    return (isinstance(item, dict) and _REVIEW_KEYS <= item.keys() <= _REVIEW_KEYS | {"progress", "state", "snapshot"}
            and ("snapshot" not in item or _valid_snapshot(item["snapshot"]))
            and ((isinstance(item.get("state"), str) and _STATE_NAME.fullmatch(item["state"])
                  and _valid_progress(item.get("progress"))) if "state" in item
                 else ("progress" not in item or _legacy_progress(item["progress"])))
            and item["kind"] in ("skill", "memory")
            and item["provider"] in ("claude", "codex") and valid_session(item["session_id"])
            and transcript_reference(item["transcript"])
            and _uint(item["tool_steps"]) and _uint(item["user_turns"]))


_DEFAULT_COUNT = count


class Consumer:
    def __init__(self, queue=None, state=None, *, clock=time.time, codex_sessions=None,
                 claude_projects=None, count=count):
        self.queue = Path(config.V3_REVIEW_QUEUE_DIR if queue is None else queue)
        self.state = Path(config.V3_DIR / "review-state" if state is None else state)
        self.clock = clock
        self.codex_sessions = Path(os.path.abspath(
            Path.home() / ".codex/sessions" if codex_sessions is None else codex_sessions))
        self.claude_projects = Path(os.path.abspath(
            Path.home() / ".claude/projects" if claude_projects is None else claude_projects))
        self.count = count
        self._incremental = count is _DEFAULT_COUNT

    def _transcript(self, item, judge):
        if item["provider"] == "claude":
            return _path(item["transcript"])
        if _credential_path(self.codex_sessions, judge):
            raise _Unusable("Credential path.")
        with open_directory(self.codex_sessions) as root:
            match = max(_codex_matches(root, item["session_id"]), default=None)
        if match is None:
            raise FileNotFoundError
        return self.codex_sessions / match

    def _open(self, provider, path, judge):
        root = self.claude_projects if provider == "claude" else self.codex_sessions
        # The lexical check is sufficient because _open_transcript opens
        # every component without following links, all the way from '/'.
        if path == root or ".." in path.parts or not path.is_relative_to(root):
            raise _Unusable("Transcript is outside the provider's session folder.")
        return _open_transcript(path, judge)

    @staticmethod
    def _deliver(state, token, reviews, received, accept):
        if accept is None:
            if token is not None:
                _returned.add((os.getpid(), token))
            return [{key: review[key] for key in _REVIEW_KEYS} for review in reviews]
        accept(reviews, received)
        # The downstream write has synced before we remove the journal. If
        # interrupted here, delivery repeats before the downstream job runs.
        if token is not None:
            _remove(state, _PENDING)
            os.fsync(state)
        return []

    def _resume(self, state, judge, seen_at, accept):
        try:
            pending = _read_json(state, _PENDING, _JOURNAL_BYTES)
        except FileNotFoundError:
            return None
        # Refuse a corrupt journal rather than advancing past undelivered reviews.
        if (not isinstance(pending, dict) or not {"token", "reviews"} <= pending.keys() <= {"token", "reviews", "received"}
                or not valid_session(pending["token"]) or not isinstance(pending["reviews"], list)
                or ("received" in pending and not _uint(pending["received"]))
                or not all(_valid_review(review) for review in pending["reviews"])):
            raise ValueError("Invalid review journal.")
        received = pending.get("received")
        if received is None and accept is not None:
            # Older journals have the original count time in their progress.
            times = [review["progress"]["last_seen"] for review in pending["reviews"] if "state" in review]
            published = os.stat(_PENDING, dir_fd=state, follow_symlinks=False).st_mtime_ns
            received = min(times) if times else min(seen_at, published)
        receipt = os.getpid(), pending["token"]
        if receipt in _returned:
            _remove(state, _PENDING)
            os.fsync(state)
            _returned.remove(receipt)
            return None
        reviews = []
        for review in pending["reviews"]:
            provider, transcript = review["provider"], _path(review["transcript"])
            try:
                with self._open(provider, transcript, judge) as (stream, info):
                    name = _state_name(provider, transcript, stream, info)
                    snapshot = review.get("snapshot")
                    if accept is not None and snapshot is not None and not _matches_snapshot(stream, info, snapshot):
                        continue
            except (OSError, _Unusable):
                continue
            if "state" in review:
                if name != review["state"]:
                    continue  # A different file now occupies the journal's path.
                progress = review["progress"]
                current = _state(state, name)
                if current is not None and current["first_seen"] != progress["first_seen"]:
                    continue
            else:
                # Older journals still supply the path needed to replay a
                # review. Session-keyed baseline files lacked that path.
                legacy = review.get("progress", {})
                if "file" in legacy and legacy["file"] != [info.st_dev, info.st_ino]:
                    continue
                progress = _state(state, name) or _initial_progress(seen_at)
                kind = review["kind"]
                progress[kind] = review["tool_steps" if kind == "skill" else "user_turns"]
                progress["size"] = info.st_size
            progress["last_seen"] = seen_at
            _write_json(state, name, progress)
            reviews.append(review)
        return self._deliver(state, pending["token"], reviews, received, accept)

    def _items(self, queue, now):
        items = []
        # Take a snapshot of names. A hook publishing during a count is left
        # for the next tick, even if it belongs to the session being counted.
        for name in os.listdir(queue):
            if not name.endswith(".json"):
                continue
            try:
                item = _read_json(queue, name, _ITEM_BYTES)
                ended = _stamp(item)
                if now - ended <= MAX_AGE_S:
                    items.append((ended, name, item))
                    continue
            except (FileNotFoundError, ValueError, OverflowError, RecursionError):
                pass
            # A directory with a JSON suffix is outside the queue protocol.
            with suppress(IsADirectoryError):
                _remove(queue, name)
        return sorted(items)

    def _drain(self, queue, state, judge, now, seen_at, accept):
        seen, deferred, consumed = set(), set(), []
        updates, reviews = {}, []
        for _, name, item in self._items(queue, now):
            provider = item["provider"]
            with ExitStack() as stack:
                try:
                    transcript = self._transcript(item, judge)
                    stream, info = stack.enter_context(self._open(provider, transcript, judge))
                except (OSError, _Unusable):
                    consumed.append(name)
                    continue
                key = _state_name(provider, transcript, stream, info)
                if key in seen:
                    consumed.append(name)
                    continue
                if key in deferred:
                    continue
                progress = _state(state, key) or _initial_progress(seen_at)
                progress["last_seen"] = seen_at
                if info.st_size < progress["size"]:
                    progress.update(skill=0, memory=0, size=0)
                try:
                    previous = progress.get("snapshot")
                    if accept is not None and previous is not None and not _matches_snapshot(stream, info, previous):
                        # A truncate and regrow can leave the same inode, size and counts.
                        progress.update(skill=0, memory=0, size=0)
                        progress.pop("scan", None)
                        progress.pop("snapshot")
                    if self._incremental:
                        tools, turns, offset = _scan(provider, stream, info, progress.get("scan"))
                    else:
                        tools, turns = self.count(provider, transcript)
                        offset = info.st_size
                    if tools < progress["skill"] or turns < progress["memory"]:
                        progress.update(skill=0, memory=0, size=0)
                    due = [(kind, total) for kind, total, threshold in (
                        ("skill", tools, SKILL_STEPS), ("memory", turns, MEMORY_TURNS),
                    ) if total - progress[kind] >= threshold]
                    snapshot = _snapshot(stream, info, offset) if due or accept is not None else None
                except _ScanPending as exc:
                    progress["scan"] = exc.cursor
                    _write_json(state, key, progress)
                    deferred.add(key)
                    continue
                except (OSError, _Unusable):
                    consumed.append(name)
                    continue
            seen.add(key)
            progress.pop("scan", None)
            if accept is not None:
                progress["snapshot"] = snapshot
            for kind, total in due:
                progress[kind] = total
                progress["size"] = info.st_size
                reviews.append({"kind": kind, "provider": provider, "session_id": item["session_id"],
                                "transcript": str(transcript), "tool_steps": tools, "user_turns": turns,
                                "state": key, "progress": progress, "snapshot": snapshot})
            updates[key] = progress
            consumed.append(name)

        token = uuid4().hex if reviews else None
        if token is not None:
            _write_json(state, _PENDING, {"token": token, "reviews": reviews, "received": seen_at})
        for key, progress in updates.items():
            _write_json(state, key, progress)
        for name in consumed:
            _remove(queue, name)
        if consumed:
            os.fsync(queue)
        return self._deliver(state, token, reviews, seen_at, accept)

    def tick(self):
        """Return a batch. Handle it before polling again in this process."""
        return self._tick(accept=None)

    def handoff(self, accept):
        """Call accept(reviews, received_ns) under the queue lock before acknowledging."""
        self._tick(accept)

    def _tick(self, accept):
        with open_directory(self.state, create=True) as state:
            fcntl.flock(state, fcntl.LOCK_EX)
            judge = credential_folder_judge()
            now = self.clock()
            seen_at = int(now * _NS_PER_SECOND)
            pending = self._resume(state, judge, seen_at, accept)
            if pending is not None:
                return pending
            _prune(state, seen_at)
            with ExitStack() as stack:
                try:
                    queue = stack.enter_context(open_directory(self.queue))
                except FileNotFoundError:
                    return self._deliver(state, None, [], seen_at, accept)
                return self._drain(queue, state, judge, now, seen_at, accept)
