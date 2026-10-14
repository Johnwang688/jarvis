"""Read-aloud voices per chat thread (voice plan V7, decisions V-8 and V-9).

Every chat pane the HUD has open is read aloud, each in its own voice, through
one shared queue. This module is the daemon's half: which voice a thread has,
and whether the owner muted it. It lives here, and not in the HUD, so that

- a thread keeps its voice across reloads, restarts and windows (sticky), and
- two windows opening the same thread cannot each invent a different voice
  (the daemon assigns under one lock, then publishes `thread_voice` on the bus).

The store is `config.SPEECH_VOICES_PATH` (`~/.config/jarvis/speech.json`):
`{"threads": {"<thread id>": {"voice": "<catalog name>", "muted": false}}}`.
It is not a `Thread` field, because the live session saves `Thread` too and
would write a stale copy back. Entries for threads that no longer exist are
pruned on every write.

Assignment: the first thread takes the voice the owner hears now (the picker's
choice, else the avatar's); later ones take the next unused voice of an ordered
pool -- English Kokoro `a*` then `b*`, then Pocket builtins when pocket-tts is
installed (the non-English builtins are left out: they would read English badly).
When every voice is taken the least-used one is reused. A stored voice that is
no longer installed is treated as unassigned. Nothing here calls a model, speaks
or reads audio; a voice is only ever a name that `voice.catalog()` lists.

Routes (owner-only, the HUD listener, like the other pickers):

    GET  /speech/voices            -> {"threads": {id: {voice, muted}}, "pool": [names]}
    POST /speech/voices {thread_id}                  assign if unassigned (idempotent)
    POST /speech/voices {thread_id, voice: "<name>"} choose a voice ("" = automatic again)
    POST /speech/voices {thread_id, muted: bool}     mute or unmute that chat
"""
from __future__ import annotations

import json
import os
import re
import stat
import threading
from pathlib import Path

from jarvis import config

_lock = threading.Lock()
_THREAD_ID = re.compile(r"[0-9a-f]{8}")
#: Pocket builtins that speak another language: a pool voice that reads English badly.
_NOT_ENGLISH = frozenset({"estelle", "giovanni", "juergen", "lola", "rafael"})
#: A file this large is not ours (a few hundred threads are a few tens of KB).
_MAX_BYTES = 1_000_000


def _path() -> Path:
    return Path(config.SPEECH_VOICES_PATH)


def _read() -> dict[str, dict]:
    """The stored map, tolerating anything: a missing, odd, oversized or corrupt
    file is empty. Only a regular file is read (never a FIFO or a device a
    symlink points at), at most `_MAX_BYTES` of it, and nested-to-death JSON
    (a RecursionError) is just another corrupt file."""
    try:
        # O_NONBLOCK: opening a FIFO must not wait for a writer.
        fd = os.open(_path(), os.O_RDONLY | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return {}
            with os.fdopen(fd, "rb", closefd=False) as f:
                data = f.read(_MAX_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > _MAX_BYTES:
            return {}
        raw = json.loads(data.decode("utf-8"))
        threads = raw.get("threads") if isinstance(raw, dict) else None
        out: dict[str, dict] = {}
        if isinstance(threads, dict):
            for tid, entry in threads.items():
                if isinstance(tid, str) and _THREAD_ID.fullmatch(tid) and isinstance(entry, dict):
                    voice = entry.get("voice")
                    out[tid] = {"voice": voice if isinstance(voice, str) and voice else None,
                                "muted": entry.get("muted") is True}
        return out
    except Exception:       # OSError, ValueError, UnicodeError, RecursionError ...
        return {}


def _write(threads: dict[str, dict]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"threads": threads}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def pool() -> list[str]:
    """The voices to hand out, in order."""
    from jarvis import pocket, voice

    names = [n for n in sorted(voice.available_voices()) if n[:1] in ("a", "b")]
    if pocket.available():
        names += [f"pocket:{n}" for n in pocket.BUILTINS if n not in _NOT_ENGLISH]
    return names


def _installed() -> set[str]:
    from jarvis import voice

    return {entry["name"] for entry in voice.catalog()}


def _first_voice() -> str | None:
    """What the owner hears now: the picker's choice, else the avatar's."""
    from jarvis import voice

    try:
        name = voice.selected_voice()
    except Exception:
        return None
    return name if name in _installed() else None


def _pick(threads: dict[str, dict], installed: set[str]) -> str | None:
    """The next voice for a thread that has none."""
    used: dict[str, int] = {}
    for entry in threads.values():
        v = entry.get("voice")
        if v:
            used[v] = used.get(v, 0) + 1
    if not used:
        first = _first_voice()
        if first:
            return first
    candidates = [n for n in pool() if n in installed]
    if not candidates:
        return _first_voice()
    for name in candidates:
        if name not in used:
            return name
    return min(candidates, key=lambda n: used.get(n, 0))   # all taken: the least used, first in pool order


def _alive(daemon, thread_id: str) -> bool:
    """Does the thread exist? A store that cannot answer keeps its entry."""
    from .stores import StoreError

    try:
        return daemon.stores.threads.get(thread_id) is not None
    except StoreError:
        return True
    except Exception:
        return True


def view(daemon) -> dict:
    """The map as the HUD reads it: threads with an installed voice, and the pool."""
    installed = _installed()
    with _lock:
        threads = _read()
    return {
        "threads": {tid: {"voice": e["voice"] if e["voice"] in installed else None, "muted": e["muted"]}
                    for tid, e in threads.items()},
        "pool": [n for n in pool() if n in installed],
    }


def assign(daemon, thread_id: str) -> tuple[dict, bool]:
    """Give `thread_id` a voice if it has none that is installed. Idempotent: a
    sticky voice never moves. Returns the record and whether the stored entry
    changed (when no voice can be found nothing is stored, and nothing changes)."""
    installed = _installed()
    with _lock:
        threads = _read()
        before = threads.get(thread_id)
        entry = dict(before) if before else {"voice": None, "muted": False}
        if entry["voice"] not in installed:
            entry["voice"] = _pick({k: v for k, v in threads.items() if k != thread_id}, installed)
        # An entry that would be {voice: None, muted: False} is the same as none at all.
        changed = entry != (before or {"voice": None, "muted": False})
        if changed:
            threads[thread_id] = entry
            _prune(daemon, threads, keep=thread_id)
            _write(threads)
            _publish(daemon, thread_id, entry)
    return {"thread_id": thread_id, **entry}, changed


def set_voice(daemon, thread_id: str, name: str) -> tuple[dict, bool]:
    """The owner's choice. "" returns the thread to automatic assignment."""
    installed = _installed()
    name = name.strip()
    if name and name not in installed:
        raise LookupError("unknown voice")
    with _lock:
        threads = _read()
        before = threads.get(thread_id)
        entry = dict(before) if before else {"voice": None, "muted": False}
        entry["voice"] = name or _pick({k: v for k, v in threads.items() if k != thread_id}, installed)
        threads[thread_id] = entry
        changed = entry != before
        if changed:
            _prune(daemon, threads, keep=thread_id)
            _write(threads)
            _publish(daemon, thread_id, entry)
    return {"thread_id": thread_id, **entry}, changed


def set_muted(daemon, thread_id: str, muted: bool) -> tuple[dict, bool]:
    with _lock:
        threads = _read()
        before = threads.get(thread_id)
        entry = dict(before) if before else {"voice": None, "muted": False}
        entry["muted"] = muted
        threads[thread_id] = entry
        changed = entry != before
        if changed:
            _prune(daemon, threads, keep=thread_id)
            _write(threads)
            _publish(daemon, thread_id, entry)
    return {"thread_id": thread_id, **entry}, changed


def _publish(daemon, thread_id: str, entry: dict) -> None:
    """Tell every window, still holding the lock: two changes cannot reach the bus
    in a different order from the one they were stored in."""
    daemon.bus.publish({"kind": "thread_voice", "data": {"thread_id": thread_id, **entry}})


def forget(thread_id: str) -> None:
    """A deleted thread's entry goes (best effort: pruning on the next write catches a miss)."""
    try:
        with _lock:
            threads = _read()
            if threads.pop(thread_id, None) is not None:
                _write(threads)
    except Exception:
        pass


def _prune(daemon, threads: dict[str, dict], keep: str) -> None:
    """Drop entries of threads that were deleted."""
    for tid in [t for t in threads if t != keep and not _alive(daemon, t)]:
        del threads[tid]


def route(handler, daemon, parts, query):
    """`/speech/voices`, or None."""
    if parts != ["speech", "voices"]:
        return None
    from .daemon import APIError, _object
    from .hud_api import fail
    from .projects import owner_only

    owner_only(handler, daemon)
    _object(query, ())
    if handler.command == "GET":
        return 200, view(daemon)
    if handler.command != "POST":
        return None
    body = _object(handler._body(), ("thread_id", "voice", "muted"), ("thread_id",))
    thread_id = body["thread_id"]
    if not isinstance(thread_id, str) or not _THREAD_ID.fullmatch(thread_id):
        fail(400, "thread_id must be a thread id")
    if daemon.stores.threads.get(thread_id) is None:
        fail(404, "no such thread")
    if "voice" in body and "muted" in body:
        fail(400, "set the voice or the mute, not both")
    try:
        if "voice" in body:
            if not isinstance(body["voice"], str):
                fail(400, "voice must be a string")
            record, _ = set_voice(daemon, thread_id, body["voice"])
        elif "muted" in body:
            if not isinstance(body["muted"], bool):
                fail(400, "muted must be a boolean")
            record, _ = set_muted(daemon, thread_id, body["muted"])
        else:
            record, _ = assign(daemon, thread_id)
    except LookupError as exc:
        raise APIError(400, str(exc)) from exc
    return 200, record
