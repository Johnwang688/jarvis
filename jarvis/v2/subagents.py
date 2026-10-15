"""Native sub-agents' record (SV-1, 2026-10-15).

A Claude chat's own Agent children (and, from SV-2, Codex's) are not the
orchestrator: what they say is not Jarvis's reply. The provider hands them
over as two kinds of their own (`EventKind.SUBAGENT`, `SUBAGENT_EVENT`), and
this module is where they land instead of the thread's `log.jsonl`:

- **memory**: per thread, the children (`CHILD_LIMIT`, oldest finished out)
  and, per child, its last `ENTRY_LIMIT` transcript entries — a text, a tool
  call folded with its outcome, the CLI's closing summary — each text capped
  at `TEXT_CAP`. At most `THREAD_LIMIT` threads are held; an older one is
  read back from disk when it is asked for.
- **disk**: the same records in the thread's own `subagents.jsonl`, beside
  its log, so a restart keeps them. A cold read takes at most the file's last
  `LOAD_BYTES`. Nothing that reads `log.jsonl` — the transcript route, the
  Discord mirror, titles, a task's result, the raw `/threads/{id}/log` route
  on the API listener — ever sees a child's words.
- **the bus**: one `subagent` record per change, with ids, a status, times
  and a count, never a word a child wrote or a name the model chose
  (`/events` is open to any local client on the API listener).
- **routes** (`hud_api`): the owner's alone, `GET /threads/{id}/subagents`
  and `GET /threads/{id}/subagents/{sid}/transcript`.

A tool entry carries its label (computed by the daemon outside its lock, as
for the parent's calls) and outcome, never its arguments or result summary
(T-4/T-9). `hud_browser_act` is "Browser action" here too.

A child is live (`running`, or `backgrounded`: the CLI's default, a child
that outlives its turn) until the CLI says otherwise. Jarvis ends one itself
only when the session that ran it is gone (`end_session`: closed, lost,
reconnected for a model switch) or, read back from disk, when no session with
its key is live — a restart, after which the CLI that ran it is gone too.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
import json
import logging
import os
import re
import threading
from typing import Any, Callable

from .approvals import clean_line
from .model import utcnow
from .provider import Event, EventKind

LOG = logging.getLogger(__name__)

FILE = "subagents.jsonl"
THREAD_LIMIT = 32
CHILD_LIMIT = 64
ENTRY_LIMIT = 300
TEXT_CAP = 4000
LOAD_BYTES = 4 << 20
NAME_CAP = 40
REASON_CAP = 80
SID = re.compile(r"(?:cl|cx):[A-Za-z0-9_-]{1,128}")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
STATUSES = ("running", "backgrounded", "done", "failed", "stopped")
LIVE = ("running", "backgrounded")
OPS = ("started", "progress", "finished")
OUTCOMES = ("ok", "failed", "denied")
GONE = "the session that ran it ended"
_BROWSER_ACT = ("hud_browser_act", "mcp__jarvis__hud_browser_act")


def valid_sid(value: Any) -> bool:
    return isinstance(value, str) and SID.fullmatch(value) is not None


def _id(value: Any) -> str | None:
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 1 << 53 else None


def _clean(value: Any, cap: int) -> str:
    """Model-chosen text bound for a surface: scrubbed of credential values,
    one line, capped. A scrub that fails withholds the text."""
    if not isinstance(value, str) or not value:
        return ""
    from jarvis.tools.secrets import scrub
    try:
        value = scrub(value)
    except Exception:  # noqa: BLE001 — unscrubbable text is not shown
        return "[withheld]"
    return clean_line(value, cap)


@dataclass
class _Child:
    sid: str
    key: str | None = None             # the daemon session that ran it
    turn_id: str | None = None         # the turn it started in
    status: str = "running"
    task_id: str | None = None
    parent_sid: str | None = None
    depth: int = 1
    background: bool = False
    name: str = ""
    kind: str = ""
    last_tool: str = ""
    tokens: int | None = None
    tool_uses: int | None = None
    duration_ms: int | None = None
    stop_reason: str | None = None
    started_at: str = ""
    ended_at: str | None = None
    entries: deque = field(default_factory=lambda: deque(maxlen=ENTRY_LIMIT))
    total: int = 0                     # entries ever made (the deque keeps the last)
    calls: dict = field(default_factory=dict)   # call_id -> its entry, while kept

    def summary(self) -> dict:
        out = {"sid": self.sid, "status": self.status, "depth": self.depth,
               "background": self.background, "name": self.name, "kind": self.kind,
               "started_at": self.started_at, "turn_id": self.turn_id,
               "entries": self.total}
        for key in ("task_id", "parent_sid", "ended_at", "stop_reason", "tokens",
                    "tool_uses", "duration_ms"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.last_tool:
            out["last_tool"] = self.last_tool
        return out

    def bus(self, op: str) -> dict:
        """What the bus may carry: ids, status, times, counts. No words."""
        out = {"sid": self.sid, "op": op, "status": self.status, "depth": self.depth,
               "background": self.background, "started_at": self.started_at,
               "entries": self.total}
        for key in ("task_id", "parent_sid", "ended_at"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


class SubagentRecords:
    def __init__(self, stores, publish: Callable[[dict], None],
                 live_key: Callable[[str], str | None] = lambda _thread_id: None):
        self.stores = stores
        self.publish = publish
        # The key of the live session on a thread (None: none is): what tells
        # a child read back from disk whether anything can still be running it.
        self.live_key = live_key
        self._lock = threading.RLock()
        self._threads: OrderedDict[str, OrderedDict[str, _Child]] = OrderedDict()

    # -- writing ---------------------------------------------------------------

    @staticmethod
    def prepare(event: Event, cwd: str | None) -> dict | None:
        """Outside the daemon's lock (the scrub and a tool's label may read
        credential files): the event, cleaned into what is kept. None for
        anything malformed, which is dropped, never shown as the parent's."""
        data = event.data if isinstance(event.data, dict) else {}
        sid = data.get("sid")
        if not valid_sid(sid):
            return None
        if event.kind == EventKind.SUBAGENT:
            op = data.get("op") if data.get("op") in OPS else "progress"
            status = data.get("status") if data.get("status") in STATUSES else None
            depth = _count(data.get("depth"))
            out = {"sid": sid, "op": op, "status": status,
                   "depth": min(depth, 16) if depth else 1,
                   "background": data.get("background") is True,
                   "task_id": _id(data.get("task_id")),
                   "parent_sid": data.get("parent_sid") if valid_sid(data.get("parent_sid")) else None,
                   "name": _clean(data.get("name"), NAME_CAP),
                   "kind": _clean(data.get("kind"), NAME_CAP),
                   "last_tool": _clean(data.get("last_tool"), NAME_CAP),
                   "stop_reason": _clean(data.get("stop_reason"), REASON_CAP) or None}
            for key in ("tokens", "tool_uses", "duration_ms"):
                out[key] = _count(data.get(key))
            return {"kind": "subagent", "sid": sid, "data": out}
        if event.kind != EventKind.SUBAGENT_EVENT:
            return None
        inner = data.get("event") if isinstance(data.get("event"), dict) else {}
        kind, body = inner.get("kind"), inner.get("data") if isinstance(inner.get("data"), dict) else {}
        if kind == "text":
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                return None
            entry = {"role": "assistant", "text": text[:TEXT_CAP]}
        elif kind == "result":
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                return None
            status = body.get("status") if body.get("status") in STATUSES else None
            entry = {"role": "result", "text": text[:TEXT_CAP], "status": status}
        elif kind == "tool_started":
            name = body.get("name") if isinstance(body.get("name"), str) else ""
            call_id = _id(body.get("call_id"))
            if call_id is None:
                return None
            if name in _BROWSER_ACT:
                # Private (BR9b): never its arguments, never a label from them.
                name, label = "hud_browser_act", "Browser action"
            else:
                from .tool_calls import label as tool_label
                try:
                    label = tool_label(name, body.get("args"), cwd)
                except Exception:  # noqa: BLE001
                    label = clean_line(name, NAME_CAP)
            entry = {"role": "tool", "call_id": call_id, "name": clean_line(name, NAME_CAP),
                     "label": label, "outcome": "running"}
        elif kind == "tool_finished":
            call_id = _id(body.get("call_id"))
            if call_id is None:
                return None
            outcome = body.get("outcome")
            entry = {"role": "tool_finished", "call_id": call_id,
                     "outcome": outcome if outcome in OUTCOMES else ("ok" if body.get("ok") is True else "failed")}
        else:
            # Thinking, a reviewer's decline (its tool's outcome says denied)
            # and anything newer: not kept.
            return None
        return {"kind": "subagent_event", "sid": sid, "data": entry}

    def record(self, thread, turn_id: str | None, key: str | None, prepared: dict) -> None:
        """Keep one prepared record: memory, the thread's file, the bus."""
        at = utcnow()
        line = {**prepared, "turn_id": turn_id, "key": key, "at": at}
        with self._lock:
            children = self._children(thread.id)
            child, op = self._apply(children, line)
            if child is None:
                return
            bus = child.bus(op)
        try:
            self.stores.threads._append(thread.id, FILE, line)
        except Exception:  # noqa: BLE001 — the view is not worth a failed turn
            LOG.warning("Cannot keep a sub-agent record for %s", thread.id, exc_info=True)
        self.publish({"kind": "subagent", "thread_id": thread.id, "project_id": thread.project_id,
                      "turn_id": turn_id, "at": at, "data": bus})

    def end_session(self, thread, key: str | None, reason: str = GONE) -> None:
        """The session `key` is gone, and with it its CLI: its live children
        are stopped (said once, on the bus and on disk)."""
        with self._lock:
            children = self._children(thread.id)
            live = [c for c in children.values() if c.status in LIVE and c.key == key]
        for child in live:
            self.record(thread, None, key, {"kind": "subagent", "sid": child.sid, "data": {
                "sid": child.sid, "op": "finished", "status": "stopped",
                "stop_reason": clean_line(reason, REASON_CAP)}})

    def _apply(self, children: OrderedDict, line: dict) -> tuple[_Child | None, str]:
        sid, data, at = line.get("sid"), line.get("data") or {}, line.get("at") or ""
        if not valid_sid(sid):
            return None, ""
        child = children.get(sid)
        if child is None:
            if len(children) >= CHILD_LIMIT:
                done = next((s for s, c in children.items() if c.status not in LIVE), None)
                if done is None:
                    return None, ""
                del children[done]
            child = children[sid] = _Child(sid=sid, key=line.get("key"),
                                           turn_id=line.get("turn_id"), started_at=at)
        if line.get("kind") == "subagent":
            op = data.get("op") if data.get("op") in OPS else "progress"
            was_live = child.status in LIVE
            for key in ("task_id", "parent_sid", "tokens", "tool_uses", "duration_ms"):
                if data.get(key) is not None:
                    setattr(child, key, data[key])
            for key in ("name", "kind", "last_tool"):
                if data.get(key):
                    setattr(child, key, data[key])
            if data.get("depth"):
                child.depth = data["depth"]
            if "background" in data and data.get("op") != "finished":
                child.background = data["background"] is True
            status = data.get("status")
            if status in STATUSES:
                if status in LIVE and not was_live:
                    # A child once ended is not revived by a stale word;
                    # only the CLI's later finish replaces an inferred stop.
                    status = child.status
                child.status = status
            if op == "finished" and child.status not in LIVE:
                child.ended_at = at
                child.stop_reason = data.get("stop_reason")
            return child, op
        entry = dict(data)
        if entry.get("role") == "tool_finished":
            start = child.calls.pop(entry.get("call_id"), None)
            if start is None:
                return None, ""
            start["outcome"] = entry.get("outcome")
            start["finished_at"] = at
            return child, "progress"
        child.total += 1
        entry["seq"] = child.total
        entry["at"] = at
        if len(child.entries) == child.entries.maxlen:
            gone = child.entries[0]
            if gone.get("role") == "tool":
                child.calls.pop(gone.get("call_id"), None)
        child.entries.append(entry)
        if entry.get("role") == "tool":
            child.calls[entry["call_id"]] = entry
        return child, "progress"

    # -- reading ---------------------------------------------------------------

    def _children(self, thread_id: str) -> OrderedDict:
        """Under `_lock`: the thread's children, read back from its file on
        first use. An older thread makes room (least recently used)."""
        children = self._threads.get(thread_id)
        if children is not None:
            self._threads.move_to_end(thread_id)
            return children
        children = OrderedDict()
        for line in self._read(thread_id):
            self._apply(children, line)
        key = self.live_key(thread_id)
        for child in children.values():
            if child.status in LIVE and (key is None or child.key != key):
                # Read back with nothing left to run it: the CLI that did is
                # gone (a restart, or a session closed without a word).
                child.status, child.stop_reason = "stopped", GONE
                child.ended_at = child.ended_at or child.started_at
        self._threads[thread_id] = children
        while len(self._threads) > THREAD_LIMIT:
            self._threads.popitem(last=False)
        return children

    def _read(self, thread_id: str) -> list[dict]:
        try:
            path = self.stores.threads.path(thread_id).with_name(FILE)
            with path.open("rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                start = max(0, size - LOAD_BYTES)
                handle.seek(start)
                raw = handle.read()
        except FileNotFoundError:
            return []
        except Exception:  # noqa: BLE001 — an unreadable record is no record
            LOG.warning("Cannot read sub-agent records for %s", thread_id, exc_info=True)
            return []
        lines = raw.split(b"\n")
        if start:
            lines = lines[1:]                  # a partial first line
        out = []
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out

    def list(self, thread_id: str) -> list[dict]:
        with self._lock:
            return [child.summary() for child in self._children(thread_id).values()]

    def transcript(self, thread_id: str, sid: str, limit: int = ENTRY_LIMIT) -> dict | None:
        with self._lock:
            child = self._children(thread_id).get(sid)
            if child is None:
                return None
            entries = list(child.entries)[-max(1, min(limit, ENTRY_LIMIT)):]
            messages = []
            for entry in entries:
                message = {k: v for k, v in entry.items() if k != "status" or entry.get("role") == "result"}
                if message.get("role") == "tool" and message.get("outcome") == "running" \
                        and child.status not in LIVE:
                    message["outcome"] = "stopped"
                messages.append(message)
            first = messages[0]["seq"] if messages else child.total + 1
            return {"subagent": child.summary(), "messages": messages,
                    "truncated": first > 1}
