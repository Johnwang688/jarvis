"""Native sub-agents' record (SV-1, 2026-10-15).

A Claude chat's own Agent children (and, since SV-2, a Codex chat's child
agents) are not the orchestrator: what they say is not Jarvis's reply. The provider hands them
over as two kinds of their own (`EventKind.SUBAGENT`, `SUBAGENT_EVENT`), and
this module is where they land instead of the thread's `log.jsonl`:

- **memory**: per thread, the children (`CHILD_LIMIT`, oldest finished out)
  and, per child, its last `ENTRY_LIMIT` transcript entries — a text, a tool
  call folded with its outcome, a reviewer's decline, the CLI's closing
  summary — each text capped at `TEXT_CAP`. A thread's entries together are
  held to `THREAD_BYTES` of their JSON (the oldest entry of any of its
  children goes first), so a thread costs bytes, not just a count. At most
  `THREAD_LIMIT` threads are held; an older one is read back from disk when
  it is asked for.
- **disk**: the same records in the thread's own `subagents.jsonl` (0600),
  beside its log, so a restart keeps them. Past `FILE_CAP` the file is
  rewritten as a snapshot of what memory holds (atomically, under the
  record's lock), and since memory is held to `THREAD_BYTES`, a quarter of
  the cold read's `LOAD_BYTES`, a cold read always rebuilds all of it.
  Nothing that reads `log.jsonl` — the transcript route, the Discord mirror,
  titles, a task's result, the raw `/threads/{id}/log` route on the API
  listener — ever sees a child's words.
- **the bus**: one `subagent` record per change, with ids, a status, times
  and counts, never a word a child wrote or a name the model chose
  (`/events` is open to any local client on the API listener).
- **routes** (`hud_api`): the owner's alone, `GET /threads/{id}/subagents`,
  `GET /threads/{id}/subagents/{sid}/transcript` and (SV-4) `POST
  /threads/{id}/subagents/{sid}/stop`.

**The owner's Stop on one child (SV-4).** The record says `stop: "stopping"`
from the moment the daemon hands the request to the provider until the CLI's
own end arrives (Claude's `killed`/`stopped`, the Codex child's `turn/
completed` `interrupted`), which clears it. Nothing here infers an end from a
stop: if the CLI has said nothing after `STOP_CONFIRM_S`, the record says
`stop: "unconfirmed"` — the request went out and the child may still be
running — and the owner may press Stop again. Between turns nobody reads the
CLI (SV-6), so a child stopped then is confirmed only by the next message's
reading. Ids, a state and a time: the bus may carry it.

A tool entry carries its label (computed by the daemon outside its lock, as
for the parent's calls) and outcome, never its arguments or result summary
(T-4/T-9). `hud_browser_act` is "Browser action" here too. A reviewer's
decline of a child's call is kept as the tool's name alone: it never reaches
the escape hatch (`hatch.py`), which offers the owner to run a declined
command and feeds its output to the orchestrator — the wrong session for a
child's command (SV-1, recorded in `docs/hud-api.md`).

A child is live (`running`, or `backgrounded`: the CLI's default, a child
that outlives its turn) until the CLI says otherwise. An ended Codex child is
live again only on its provider's own `started` from the session that ran it
(a child given another turn); any other live word after an end is stale.

A child's words (`text`, `result`) and its brief (SV-3: Claude's
`task_started.prompt`; Codex sends none) are scrubbed here, the same way for
both providers (`redact.scrub_text`), before memory, the file or a route sees
them. The brief is served only by the owner's transcript route, never the
list, the bus or the thread's log. Jarvis ends one itself
only when the session that ran it is gone (`end_session`: closed, lost,
reconnected for a model switch) or, read back from disk, when no session with
its key is live — a restart, after which the CLI that ran it is gone too.
Either way the end is written and published once.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
import json
import logging
import os
import re
import threading
import uuid
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
LOAD_BYTES = 4 << 20                 # a cold read takes at most the file's last 4 MiB
THREAD_BYTES = LOAD_BYTES // 4       # one thread's entries in memory (and so in a snapshot)
FILE_CAP = LOAD_BYTES                # past this the file is rewritten as a snapshot
ENTRY_OVERHEAD = 64                  # an entry's later fields (outcome, finished_at)
# A child's brief (SV-3, the owner's decision: shown collapsed at the top of
# its pane): `TEXT_CAP` characters, scrubbed as its words are, and at most
# `BRIEF_BYTES` of JSON, so `CHILD_LIMIT` briefs are at most 1 MiB beside the
# entries' `THREAD_BYTES` and a snapshot still fits a cold read.
BRIEF_BYTES = 16 << 10
NAME_CAP = 40
REASON_CAP = 80
SID = re.compile(r"(?:cl|cx):[A-Za-z0-9_-]{1,128}")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
STATUSES = ("running", "backgrounded", "done", "failed", "stopped")
STOP_STATES = ("stopping", "unconfirmed")
STOP_CONFIRM_S = 15.0                # the CLI's word on an owner's Stop, before "unconfirmed"
LIVE = ("running", "backgrounded")
OPS = ("started", "progress", "finished")
OUTCOMES = ("ok", "failed", "denied")
GONE = "the session that ran it ended"
_BROWSER_ACT = ("hud_browser_act", "mcp__jarvis__hud_browser_act")
_CHILD_FIELDS = ("key", "turn_id", "status", "task_id", "parent_sid", "depth", "background",
                 "name", "kind", "last_tool", "tokens", "tool_uses", "duration_ms",
                 "stop_reason", "started_at", "ended_at", "total", "declined", "brief",
                 "stop", "stop_at")


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


def _words(text: str) -> str:
    """A child's words for its record, scrubbed the same way whichever
    provider ran it (`redact.scrub_text`: Jarvis's credential values, the
    known key shapes, bearer tokens, JWTs, jarvis-mcp tokens), then capped. A
    scrub that fails withholds them (SV-2 review)."""
    from .redact import scrub_text
    try:
        return scrub_text(text[:TEXT_CAP * 2])[:TEXT_CAP]
    except Exception:  # noqa: BLE001 — unscrubbable text is not shown
        return "[withheld]"


def _brief(value: Any) -> str:
    """The orchestrator's brief to a child, for its record: scrubbed and
    capped as a child's words are (`_words`), then held to `BRIEF_BYTES` of
    JSON. Empty for anything that is not text."""
    if not isinstance(value, str) or not value.strip():
        return ""
    text = _words(value)
    while text and len(json.dumps(text, ensure_ascii=False).encode()) > BRIEF_BYTES:
        text = text[:len(text) * 9 // 10]
    return text


def _public(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if not k.startswith("_")}


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
    entries: deque = field(default_factory=deque)
    total: int = 0                     # entries ever made (the deque keeps the last)
    declined: int = 0                  # calls a reviewer declined
    brief: str = ""                    # the orchestrator's brief to it (SV-3): the transcript route's alone
    stop: str | None = None            # the owner's Stop (SV-4): "stopping" | "unconfirmed", while live
    stop_at: str | None = None         # when that Stop was asked for
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
        if self.declined:
            out["declined"] = self.declined
        self._stop_fields(out)
        return out

    def _stop_fields(self, out: dict) -> None:
        if self.stop in STOP_STATES and self.status in LIVE:
            out["stop"] = self.stop
            if self.stop_at:
                out["stop_requested_at"] = self.stop_at

    def bus(self, op: str) -> dict:
        """What the bus may carry: ids, status, times, counts. No words."""
        out = {"sid": self.sid, "op": op, "status": self.status, "depth": self.depth,
               "background": self.background, "started_at": self.started_at,
               "entries": self.total}
        for key in ("task_id", "parent_sid", "ended_at"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.declined:
            out["declined"] = self.declined
        self._stop_fields(out)
        return out


@dataclass
class _Thread:
    children: OrderedDict = field(default_factory=OrderedDict)
    size: int = 0                      # bytes of every kept entry
    n: int = 0                         # entries ever appended, across its children


class SubagentRecords:
    def __init__(self, stores, publish: Callable[[dict], None],
                 live_key: Callable[[str], str | None] = lambda _thread_id: None):
        self.stores = stores
        self.publish = publish
        # The key of the live session on a thread (None: none is): what tells
        # a child read back from disk whether anything can still be running it.
        self.live_key = live_key
        # One lock for memory and the file: a line is written in the order
        # memory took it, and a snapshot never races an append.
        self._lock = threading.RLock()
        self._threads: OrderedDict[str, _Thread] = OrderedDict()
        self._outbox: list[tuple[str, dict]] = []   # bus records, sent outside the lock
        # The owner's Stops awaiting the CLI's word (SV-4): (thread, sid) ->
        # (token, timer). The token tells a late timer from the current one.
        self._stops: dict[tuple[str, str], tuple[int, threading.Timer]] = {}
        self._stop_seq = 0
        self._closed = False

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
            brief = _brief(data.get("brief"))
            if brief:
                # The owner's to read in the pane (SV-3): kept in memory and
                # the thread's own file, never in `bus()` or `summary()`.
                out["brief"] = brief
            return {"kind": "subagent", "sid": sid, "data": out}
        if event.kind != EventKind.SUBAGENT_EVENT:
            return None
        inner = data.get("event") if isinstance(data.get("event"), dict) else {}
        kind, body = inner.get("kind"), inner.get("data") if isinstance(inner.get("data"), dict) else {}
        if kind == "text":
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                return None
            entry = {"role": "assistant", "text": _words(text)}
        elif kind == "result":
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                return None
            status = body.get("status") if body.get("status") in STATUSES else None
            entry = {"role": "result", "text": _words(text), "status": status}
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
        elif kind == "reviewer_declined":
            # The tool's name only: never its command, arguments or the
            # reviewer's words about them. Never the escape hatch's either.
            tool = body.get("tool") if isinstance(body.get("tool"), str) else ""
            entry = {"role": "declined",
                     "name": "hud_browser_act" if tool in _BROWSER_ACT else clean_line(tool, NAME_CAP)}
        else:
            # Thinking and anything newer: not kept.
            return None
        return {"kind": "subagent_event", "sid": sid, "data": entry}

    def record(self, thread, turn_id: str | None, key: str | None, prepared: dict) -> None:
        """Keep one prepared record: memory, the thread's file, the bus."""
        with self._lock:
            self._keep(thread.id, turn_id, key, prepared)
        self._flush()

    def end_session(self, thread, key: str | None, reason: str = GONE,
                    keep: frozenset | set = frozenset()) -> None:
        """The session `key` is gone, and with it its CLI: its live children
        (but those in `keep`, which the provider still runs) are stopped,
        said once, on the bus and on disk."""
        with self._lock:
            state = self._state(thread.id)
            live = [c.sid for c in state.children.values()
                    if c.status in LIVE and c.key == key and c.sid not in keep]
            for sid in live:
                self._keep(thread.id, None, key, {"kind": "subagent", "sid": sid, "data": {
                    "sid": sid, "op": "finished", "status": "stopped",
                    "stop_reason": clean_line(reason, REASON_CAP)}})
        self._flush()

    # -- the owner's Stop (SV-4) -------------------------------------------------

    def begin_stop(self, thread, sid: str, key: str | None) -> tuple[str, int]:
        """Mark child `sid` "stopping" before its provider is asked, so a
        second press is not a second request. -> (outcome, token): outcome is
        "started" (go on and ask the provider), "stopping" (already on its
        way: nothing to do), "unknown", "ended", or "elsewhere" (it ran under
        another session than `key`, now gone)."""
        token = 0
        with self._lock:
            child = self._state(thread.id).children.get(sid)
            if child is None:
                outcome = "unknown"
            elif child.status not in LIVE:
                outcome = "ended"
            elif key is None or child.key != key:
                outcome = "elsewhere"
            elif child.stop == "stopping":
                outcome = "stopping"
            else:
                self._stop_seq += 1
                token = self._stop_seq
                self._cancel_timer(thread.id, sid)
                self._stops[(thread.id, sid)] = (token, None)
                self._keep(thread.id, None, child.key, self._stop_line(sid, "stopping"))
                outcome = "started"
        self._flush()
        return outcome, token

    def stop_sent(self, thread, sid: str, token: int) -> None:
        """The provider took the request: wait `STOP_CONFIRM_S` for the
        CLI's word, then say "unconfirmed" if none came."""
        with self._lock:
            held = self._stops.get((thread.id, sid))
            if self._closed or held is None or held[0] != token:
                return
            timer = threading.Timer(STOP_CONFIRM_S, self._unconfirmed, (thread.id, sid, token))
            timer.daemon = True
            self._stops[(thread.id, sid)] = (token, timer)
            timer.start()

    def stop_failed(self, thread, sid: str, token: int, *, unconfirmed: bool) -> None:
        """The provider refused (nothing was sent: the mark goes) or could
        not say whether the CLI took it (`unconfirmed`)."""
        with self._lock:
            held = self._stops.get((thread.id, sid))
            if held is None or held[0] != token:
                return
            self._cancel_timer(thread.id, sid)
            child = self._state(thread.id).children.get(sid)
            if child is not None and child.status in LIVE and child.stop == "stopping":
                self._keep(thread.id, None, child.key,
                           self._stop_line(sid, "unconfirmed" if unconfirmed else None))
        self._flush()

    def _unconfirmed(self, thread_id: str, sid: str, token: int) -> None:
        with self._lock:
            held = self._stops.get((thread_id, sid))
            if self._closed or held is None or held[0] != token:
                return
            del self._stops[(thread_id, sid)]
            state = self._threads.get(thread_id)
            child = state.children.get(sid) if state is not None else None
            if child is not None and child.status in LIVE and child.stop == "stopping":
                self._keep(thread_id, None, child.key, self._stop_line(sid, "unconfirmed"))
        self._flush()

    def _cancel_timer(self, thread_id: str, sid: str) -> None:
        held = self._stops.pop((thread_id, sid), None)
        if held is not None and held[1] is not None:
            held[1].cancel()

    @staticmethod
    def _stop_line(sid: str, stop: str | None) -> dict:
        return {"kind": "subagent", "sid": sid, "data": {"sid": sid, "op": "progress", "stop": stop}}

    def close(self) -> None:
        """The daemon is stopping: no Stop timer outlives it."""
        with self._lock:
            self._closed = True
            for thread_id, sid in list(self._stops):
                self._cancel_timer(thread_id, sid)

    def _keep(self, thread_id: str, turn_id, key, prepared: dict) -> None:
        """Under `_lock`: apply, write, and queue the bus record."""
        line = {**prepared, "turn_id": turn_id, "key": key, "at": utcnow()}
        state = self._state(thread_id)
        child, op = self._apply(state, line)
        if child is None:
            return
        if child.stop != "stopping" and (thread_id, child.sid) in self._stops \
                and self._stops[(thread_id, child.sid)][1] is not None:
            # The CLI's word came (or the child ended): no timer is needed.
            self._cancel_timer(thread_id, child.sid)
        self._write(thread_id, state, line)
        self._outbox.append((thread_id, {"turn_id": turn_id, "at": line["at"],
                                         "data": child.bus(op)}))

    def _flush(self) -> None:
        """Publish what was queued, outside the lock."""
        with self._lock:
            out, self._outbox = self._outbox, []
        for thread_id, record in out:
            try:
                thread = self.stores.threads.get(thread_id)
                project_id = getattr(thread, "project_id", None)
            except Exception:  # noqa: BLE001
                project_id = None
            self.publish({"kind": "subagent", "thread_id": thread_id, "project_id": project_id,
                          **record})

    # -- memory ----------------------------------------------------------------

    def _apply(self, state: _Thread, line: dict) -> tuple[_Child | None, str]:
        sid, data, at = line.get("sid"), line.get("data") or {}, line.get("at") or ""
        if not valid_sid(sid):
            return None, ""
        children = state.children
        if line.get("kind") == "snapshot":
            return self._restore(state, sid, data), "progress"
        child = children.get(sid)
        if child is None:
            if len(children) >= CHILD_LIMIT:
                done = next((s for s, c in children.items() if c.status not in LIVE), None)
                if done is None:
                    return None, ""
                self._forget(state, done)
            child = children[sid] = _Child(sid=sid, key=line.get("key"),
                                           turn_id=line.get("turn_id"), started_at=at)
        if line.get("kind") == "subagent":
            op = data.get("op") if data.get("op") in OPS else "progress"
            was_live = child.status in LIVE
            for key in ("task_id", "parent_sid", "tokens", "tool_uses", "duration_ms"):
                if data.get(key) is not None:
                    setattr(child, key, data[key])
            for key in ("name", "kind", "last_tool", "brief"):
                if data.get(key) and isinstance(data[key], str):
                    setattr(child, key, data[key])
            if data.get("depth"):
                child.depth = data["depth"]
            if "background" in data and data.get("op") != "finished":
                child.background = data["background"] is True
            status = data.get("status")
            if status in STATUSES:
                if status in LIVE and not was_live:
                    if (op == "started" and child.sid.startswith("cx:")
                            and child.key is not None and child.key == line.get("key")):
                        # Codex's own word that an ended child runs again (a
                        # child given more to do, SV-2), from the session
                        # that ran it: live again. Codex only: Claude has no
                        # such turn, so for it every later live word is stale.
                        child.ended_at = child.stop_reason = None
                    else:
                        # A child once ended is not revived by a stale word;
                        # only the CLI's later finish replaces an inferred stop.
                        status = child.status
                child.status = status
            if op == "finished" and child.status not in LIVE:
                child.ended_at = at
                child.stop_reason = data.get("stop_reason")
            if "stop" in data:
                # Only the record's own lines carry it (`prepare` never does).
                stop = data.get("stop") if data.get("stop") in STOP_STATES else None
                if stop == "stopping" or stop is None:
                    child.stop_at = at if stop else None
                child.stop = stop
            if child.status not in LIVE or (child.status in LIVE and not was_live):
                # Ended (or live again, a Codex child's new turn): no Stop is pending.
                child.stop = child.stop_at = None
            return child, op
        entry = dict(data)
        if entry.get("role") == "tool_finished":
            start = child.calls.pop(entry.get("call_id"), None)
            if start is None:
                return None, ""
            start["outcome"] = entry.get("outcome")
            start["finished_at"] = at
            return child, "progress"
        if entry.get("role") == "declined":
            child.declined += 1
        child.total += 1
        entry["seq"] = child.total
        entry["at"] = at
        self._append(state, child, entry)
        return child, "progress"

    def _append(self, state: _Thread, child: _Child, entry: dict) -> None:
        """An entry, inside both bounds: `ENTRY_LIMIT` for its child, then
        `THREAD_BYTES` for its thread (the oldest entry of any child first)."""
        state.n += 1
        entry["_n"] = state.n
        entry["_bytes"] = len(json.dumps(_public(entry), ensure_ascii=False).encode()) + ENTRY_OVERHEAD
        if len(child.entries) >= ENTRY_LIMIT:
            self._drop(state, child)
        child.entries.append(entry)
        state.size += entry["_bytes"]
        if entry.get("role") == "tool" and entry.get("outcome") == "running":
            child.calls[entry["call_id"]] = entry
        while state.size > THREAD_BYTES:
            oldest = min((c for c in state.children.values() if c.entries),
                         key=lambda c: c.entries[0]["_n"], default=None)
            if oldest is None:
                break
            self._drop(state, oldest)

    @staticmethod
    def _drop(state: _Thread, child: _Child) -> None:
        gone = child.entries.popleft()
        state.size -= gone.get("_bytes", 0)
        if gone.get("role") == "tool":
            child.calls.pop(gone.get("call_id"), None)

    @staticmethod
    def _forget(state: _Thread, sid: str) -> None:
        child = state.children.pop(sid)
        state.size -= sum(e.get("_bytes", 0) for e in child.entries)

    def _restore(self, state: _Thread, sid: str, data: dict) -> _Child | None:
        """A snapshot line: the child exactly as memory held it."""
        if sid in state.children:
            self._forget(state, sid)
        elif len(state.children) >= CHILD_LIMIT:
            return None
        child = _Child(sid=sid)
        for name in _CHILD_FIELDS:
            if name in data:
                setattr(child, name, data[name])
        if child.status not in STATUSES:
            child.status = "stopped"
        if not isinstance(child.brief, str):
            child.brief = ""
        if child.stop not in STOP_STATES or child.status not in LIVE:
            child.stop = child.stop_at = None
        state.children[sid] = child
        for entry in data.get("entries") or ():
            if isinstance(entry, dict):
                self._append(state, child, dict(entry))
        return child

    # -- the file --------------------------------------------------------------

    def _path(self, thread_id: str):
        return self.stores.threads.path(thread_id).with_name(FILE)

    def _write(self, thread_id: str, state: _Thread, line: dict) -> None:
        """Under `_lock`: append one line (the file created 0600); past
        `FILE_CAP`, rewrite the file as a snapshot of memory."""
        try:
            path = self._path(thread_id)
            if not path.parent.is_dir():
                return                     # the thread is gone
            data = (json.dumps(line, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, data)
                size = os.fstat(fd).st_size
            finally:
                os.close(fd)
            if size > FILE_CAP:
                self._compact(path, state)
        except Exception:  # noqa: BLE001 — the view is not worth a failed turn
            LOG.warning("Cannot keep a sub-agent record for %s", thread_id, exc_info=True)

    def _compact(self, path, state: _Thread) -> None:
        """Replace the file with one snapshot line per child: what memory
        holds, which is at most `THREAD_BYTES` of entries. Written beside it
        (0600) and renamed over it, so a reader sees the old file or the
        new one, never half of either."""
        tmp = path.with_name(f".{FILE}.{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                for child in state.children.values():
                    data = {name: getattr(child, name) for name in _CHILD_FIELDS}
                    data["entries"] = [_public(e) for e in child.entries]
                    line = {"kind": "snapshot", "sid": child.sid, "at": child.started_at,
                            "data": data}
                    os.write(fd, (json.dumps(line, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001
            LOG.warning("Cannot compact %s", path, exc_info=True)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # -- reading ---------------------------------------------------------------

    def _state(self, thread_id: str) -> _Thread:
        """Under `_lock`: the thread's record, read back from its file on
        first use; an older thread makes room (least recently used). A child
        read back live with no session of its key left to run it is stopped,
        and that is written and published like any other end."""
        state = self._threads.get(thread_id)
        if state is not None:
            self._threads.move_to_end(thread_id)
            return state
        state = _Thread()
        for line in self._read(thread_id):
            self._apply(state, line)
        self._threads[thread_id] = state
        while len(self._threads) > THREAD_LIMIT:
            self._threads.popitem(last=False)
        key = self.live_key(thread_id)
        stale = [c for c in state.children.values()
                 if c.status in LIVE and (key is None or c.key != key)]
        for child in stale:
            # The CLI that ran it is gone (a restart, or a session closed
            # while this thread was out of memory).
            self._keep(thread_id, None, child.key, {"kind": "subagent", "sid": child.sid, "data": {
                "sid": child.sid, "op": "finished", "status": "stopped", "stop_reason": GONE}})
        return state

    def _read(self, thread_id: str) -> list[dict]:
        try:
            with self._path(thread_id).open("rb") as handle:
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

    def list(self, thread_id: str, *, keys: bool = False) -> list[dict]:
        """The thread's children, oldest first. `keys`: each row also says,
        as `_key`, which session ran it (for the daemon, never a route)."""
        with self._lock:
            out = [{**child.summary(), "_key": child.key} if keys else child.summary()
                   for child in self._state(thread_id).children.values()]
        self._flush()
        return out

    def transcript(self, thread_id: str, sid: str, limit: int = ENTRY_LIMIT, *,
                   keys: bool = False) -> dict | None:
        with self._lock:
            child = self._state(thread_id).children.get(sid)
            found = None
            if child is not None:
                entries = list(child.entries)[-max(1, min(limit, ENTRY_LIMIT)):]
                messages = []
                for entry in entries:
                    message = {k: v for k, v in _public(entry).items()
                               if k != "status" or entry.get("role") == "result"}
                    if message.get("role") == "tool" and message.get("outcome") == "running" \
                            and child.status not in LIVE:
                        message["outcome"] = "stopped"
                    messages.append(message)
                first = messages[0]["seq"] if messages else child.total + 1
                summary = {**child.summary(), "_key": child.key} if keys else child.summary()
                found = {"subagent": summary, "messages": messages,
                         "truncated": first > 1}
                if child.brief:
                    found["brief"] = child.brief
        self._flush()
        return found
