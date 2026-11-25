"""Incrementally ingest the supported session transcripts from local files."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from jarvis import config
from jarvis.tools.secrets import is_env_credential, is_protected
from jarvis.v3.chat import is_private_chat_directory
from jarvis.v3.index import Index, _document_id, _timestamp, open_directory


_READ_BYTES = 64 * 1024
_MAX_LINE = 1024 * 1024
_BATCH_BYTES = 256 * 1024
_BATCH_LINES = 128
_CURSOR_SAMPLE = 4_096
_WRITER_WAIT_S = 30.0
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@dataclass
class Stats:
    files: int = 0
    lines: int = 0
    chunks: int = 0
    skipped: int = 0
    done: bool = True


class _Limit:
    """When this run must stop, and how long it may wait for another writer."""

    def __init__(self, budget_s, stop):
        self._deadline = None if budget_s is None else time.monotonic() + budget_s
        self._stop = stop
        self.busy = False

    def __call__(self) -> bool:
        return (self.busy or (self._stop is not None and self._stop.is_set())
                or (self._deadline is not None and time.monotonic() >= self._deadline))

    def wait_s(self) -> float:
        return _WRITER_WAIT_S if self._deadline is None else min(
            _WRITER_WAIT_S, max(0.0, self._deadline - time.monotonic()))


@dataclass(frozen=True)
class _Cursor:
    dev: int
    ino: int
    offset: int
    digest: bytes
    skipping: bool

    def values(self):
        return self.dev, self.ino, self.offset, self.digest, self.skipping


def _read_stamp(value):
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return stamp.timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return _timestamp(value) or None


def _text(content, kinds):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(block["text"] for block in content if isinstance(block, dict)
                     and block.get("type") in kinds and isinstance(block.get("text"), str))


def _claude(row, stamp):
    role = row.get("type")
    message = row.get("message")
    if role not in ("user", "assistant") or not isinstance(message, dict) or message.get("role") != role:
        return
    if row.get("isMeta"):       # the CLI's own reminders, stored under the user's role
        return
    content = message.get("content")
    if isinstance(content, str):
        yield role, content, stamp
    elif isinstance(content, list):
        # Preserve text around tool blocks in transcript order.
        text = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                text.append(block["text"])
                continue
            if kind == "tool_use" and role == "assistant" and isinstance(block.get("name"), str):
                tool = block["name"]
            elif kind == "tool_result" and role == "user":
                tool = _text(block.get("content"), ("text",))
            else:
                continue
            if text:
                yield role, "\n".join(text), stamp
                text.clear()
            yield "tool", tool, stamp
        if text:
            yield role, "\n".join(text), stamp


def _codex(row, stamp):
    item = row.get("payload")
    if row.get("type") != "response_item" or not isinstance(item, dict) or item.get("isSidechain"):
        return
    kind = item.get("type")
    if kind == "message":
        role = item.get("role")
        if role in ("user", "assistant"):
            yield role, _text(item.get("content"), ("input_text", "output_text", "text")), stamp
    elif kind in ("function_call", "custom_tool_call"):
        if isinstance(item.get("name"), str):
            yield "tool", item["name"], stamp
    elif kind in ("function_call_output", "custom_tool_call_output"):
        yield "tool", _text(item.get("output"), ("input_text", "output_text", "text")), stamp


def _jarvis(row, stamp):
    role = row.get("role")
    if role in ("you", "user"):
        role = "user"
    elif role in ("jarvis", "assistant"):
        role = "assistant"
    if role in ("user", "assistant", "tool") and isinstance(row.get("text"), str):
        yield role, row["text"], stamp


_PARSERS = {"claude": _claude, "codex": _codex, "jarvis": _jarvis}


@contextmanager
def _regular(directory, name):
    if is_protected(name):
        raise OSError("Expected a non-credential file.")
    fd = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or is_env_credential(Path(f"/proc/self/fd/{directory}") / name)):
            raise OSError("Expected a regular transcript file.")
        with open(f"/proc/self/fd/{fd}", "rb") as handle:
            yield handle, info
    finally:
        os.close(fd)


def _jarvis_meta(directory):
    try:
        with _regular(directory, "meta.json") as (handle, _):
            raw = handle.read(_MAX_LINE + 1)
            if len(raw) <= _MAX_LINE:
                record = json.loads(raw)
                if isinstance(record, dict):
                    return record
    except (OSError, ValueError, RecursionError):
        pass
    return {}


def _fingerprint(handle, offset):
    if not offset:
        return b""
    fd = handle.fileno()
    tail_start = max(0, offset - _CURSOR_SAMPLE)
    tail = os.pread(fd, offset - tail_start, tail_start)
    head = os.pread(fd, min(tail_start, _CURSOR_SAMPLE), 0) if tail_start else b""
    if len(tail) != offset - tail_start or len(head) != min(tail_start, _CURSOR_SAMPLE):
        raise OSError("Transcript changed while being read.")
    return hashlib.blake2s(head + tail, digest_size=16).digest()


def _batch(handle, end, offset, skipping, stopped):
    """Read a bounded batch, leaving an incomplete small line for the next run.

    For a known oversized line, the cursor can advance even before a newline
    arrives. The next batch continues discarding that line in bounded reads.
    """
    handle.seek(offset)
    start = position = offset
    line = bytearray()
    rows = []
    while position < end and not stopped():
        piece = handle.readline(min(_READ_BYTES, end - position))
        if not piece:
            break
        position += len(piece)
        complete = piece.endswith(b"\n")
        if not skipping:
            if len(line) + len(piece) - complete > _MAX_LINE:
                skipping = True
                line.clear()
            else:
                line.extend(piece)
        if complete:
            rows.append(None if skipping else bytes(line))
            line.clear()
            skipping = False
            offset = position
        elif skipping:
            offset = position
        if len(rows) >= _BATCH_LINES or (position - start >= _BATCH_BYTES and (complete or skipping)):
            return offset, skipping, rows, position < end
    return offset, skipping, rows, stopped() and position < end


def _file(index, source, root, relative, directory, stats, stopped):
    doc_id = _document_id(source, str(root / relative))
    stats.files += 1
    while not stopped():
        # The file is opened inside the write transaction, so another process
        # cannot advance its cursor while this batch reads or resets it.
        try:
            with index._transaction(write=True, wait_s=stopped.wait_s()) as db, _regular(directory, relative.name) as (handle, info):
                row = db.execute("""SELECT dev, ino, offset, digest, skipping FROM cursors
                                    WHERE file_key=?""", (doc_id,)).fetchone()
                old = _Cursor(*row) if row is not None else None
                valid = (old is not None and (old.dev, old.ino) == (info.st_dev, info.st_ino)
                         and old.offset <= info.st_size and old.digest == _fingerprint(handle, old.offset))
                offset = old.offset if valid else 0
                skipping = bool(valid and old.skipping)
                if valid and offset == info.st_size:
                    return
                end, discard, lines, more = _batch(handle, info.st_size, offset, skipping, stopped)
                if valid and end == offset:
                    return

                cursor = _Cursor(info.st_dev, info.st_ino, end, _fingerprint(handle, end), discard)
                if not valid and old is not None:
                    db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
                    db.execute("DELETE FROM docs WHERE id=?", (doc_id,))
                messages = []
                skipped = 0
                for line in lines:
                    try:
                        item = json.loads(line.decode("utf-8")) if line is not None else None
                    except (ValueError, RecursionError):
                        item = None
                    parsed = []
                    if isinstance(item, dict) and not item.get("isSidechain"):
                        stamp = _read_stamp(item.get("timestamp", item.get("t")))
                        parsed = [message for message in _PARSERS[source](item, stamp) if message[1]]
                    messages.extend(parsed)
                    skipped += not parsed

                chunks = 0
                if messages:
                    meta = _jarvis_meta(directory) if source == "jarvis" else {}
                    title = meta.get("title")
                    if not isinstance(title, str) or not title.strip():
                        title = str(relative.parent if source == "jarvis" else relative)
                    ts = max((message[2] for message in messages if message[2] is not None), default=info.st_mtime)
                    chunks = index._append(db, doc_id, source, str(relative), title,
                                           _read_stamp(meta.get("updated")) or ts, messages)
                db.execute("""INSERT OR REPLACE INTO cursors(file_key, dev, ino, offset, digest, skipping)
                              VALUES (?, ?, ?, ?, ?, ?)""", (doc_id, *cursor.values()))
        except OSError:
            stats.skipped += 1
            return
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode & 0xFF != sqlite3.SQLITE_BUSY:
                raise
            stopped.busy = True     # another writer holds the index: this run ends, the next goes on
            return

        stats.lines += len(lines)
        stats.skipped += skipped
        stats.chunks += chunks
        if not more:
            return


def _walk(index, source, root, relative, directory, stats, stopped, *, private=False):
    # Keep the parent fd open during recursion. Every entry is opened against
    # its pinned parent, so a rename cannot redirect the walk through a link.
    if private and not is_private_chat_directory(directory):
        stats.skipped += 1
        return
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if stopped():
                    return
                name = entry.name
                if is_protected(name):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if (source == "jarvis" and relative.parts) or (source == "claude" and name == "subagents"):
                            continue
                        child = os.open(name, _DIR_FLAGS, dir_fd=directory)
                        try:
                            _walk(index, source, root, relative / name, child, stats, stopped, private=private)
                        finally:
                            os.close(child)
                    elif entry.is_file(follow_symlinks=False):
                        if source == "jarvis":
                            eligible = len(relative.parts) == 1 and name == "log.jsonl"
                        else:
                            eligible = name.endswith(".jsonl")
                        if eligible:
                            _file(index, source, root, relative / name, directory, stats, stopped)
                except OSError:
                    stats.skipped += 1
    except OSError:
        stats.skipped += 1


def ingest(index: Index, *, claude=None, codex=None, jarvis=None, chats=None,
           budget_s=None, stop=None) -> Stats:
    """Resume each transcript and atomically commit its chunks and cursor."""
    stopped = _Limit(budget_s, stop)
    roots = (
        ("claude", Path.home() / ".claude" / "projects" if claude is None else Path(claude), False),
        ("codex", Path.home() / ".codex" / "sessions" if codex is None else Path(codex), False),
        ("jarvis", Path(config.SESSIONS_DIR if jarvis is None else jarvis), False),
        ("jarvis", Path(config.V3_DIR) / "chats" if chats is None else Path(chats), True),
    )
    stats = Stats()
    for source, path, private in roots:
        if stopped():
            break
        root = Path(os.path.abspath(path.expanduser()))
        if any(is_protected(part) for part in root.parts):
            continue
        try:
            with open_directory(root) as directory:
                _walk(index, source, root, Path(), directory, stats, stopped, private=private)
        except FileNotFoundError:
            continue
        except OSError:
            stats.skipped += 1
    stats.done = not stopped()
    return stats
