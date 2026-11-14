"""What the owner's HUD window says it showed, for a chat that asked for panes
(2026-11-13, `docs/plans/2026-11-13-agent-pane-grid-decisions.md`).

`hud_open_subagents` (`subagent_panes.py`) publishes one bus record per ask.
Before this, nothing came back, and a chat that asked for eleven sub-agent
panes could only say "the HUD doesn't report back". Now each ask carries a
request id (random, made here) and the window that places it posts counts
back to the owner-only route below: how many it showed, how many it had no
room for. The service waits for the first window's answer for at most
`REPORT_WAIT_S`, on the monotonic clock, never under the daemon lock.

    POST /agent-panes/report   {window, request_id, placed, not_placed}

The route is the owner's alone (`projects.owner_only`: the HUD listener and
the HUD's own page asking, so no agent's HTTP client reaches it), on a strict
loopback Host, from a live owner window (one holding the event stream it
named in `?window=`, as `/browser/front` requires). It carries counts and
ids only: no URL, name or word. The first window's answer wins; a second
gets 409, an id nothing waits for 404, counts that do not add up to the ask
400. An answer decides nothing: it is only what the chat is told.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass, field

from jarvis import config

# How long a chat's call waits for a window's answer.
REPORT_WAIT_S = 3.0
# How often the wait looks again at whether the daemon is stopping.
_POLL_S = 0.25
# Asks waiting at once (each a call blocked at most REPORT_WAIT_S).
PENDING_MAX = 32
# The most panes one ask names: the HUD draws twelve and one shows the chat.
COUNT_MAX = 11

_REQUEST = re.compile(r"[0-9a-f]{32}")
_WINDOW = re.compile(r"[0-9a-f]{32}")
KEYS = ("window", "request_id", "placed", "not_placed")


STOPPED_TURN = "Error: this turn was stopped, so it opens nothing more."


def refuse_desk_caller(daemon, closed: bool, caller: dict, not_at_desk: str, stopping: str):
    """Under the daemon lock, for every tool that opens owner panes
    (`hud_open_subagents`, `hud_open_browser`): `(refusal, None)` or
    `(None, session)`. As `decisions.OwnerPosts._refuse_caller`: the live
    session, its exact turn, no relayed turn, not stopped, an owner chat (no
    task), Claude or Codex, the owner at the desk."""
    from .model import ProviderName, Role
    if closed or daemon._stopping:
        return stopping, None
    session = daemon._sessions.get(caller["thread_id"])
    if (session is None or session.worker is None or session.handle is None
            or session.turn_id != caller["turn_id"]):
        return not_at_desk, None
    if getattr(session, "relay_turn", None) is not None:
        return not_at_desk, None
    if session.cancelled.is_set() or session.closing or session.retired or session.lost:
        return STOPPED_TURN, None
    if session.brief.role != Role.CHAT or session.brief.task_id is not None:
        return not_at_desk, None
    if session.thread.provider not in (ProviderName.CLAUDE, ProviderName.CODEX):
        return not_at_desk, None
    if session.desk.slot.get("present") is not True:
        return not_at_desk, None
    return None, session


class ReportRefused(Exception):
    """A fixed refusal: no URL, name or count from the body in its text."""

    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


@dataclass
class _Waiting:
    count: int
    event: threading.Event = field(default_factory=threading.Event)
    answer: dict | None = None


class PaneReports:
    def __init__(self, daemon, *, clock=time.monotonic):
        self.daemon = daemon
        self.clock = clock
        self._lock = threading.Lock()
        self._waiting: dict[str, _Waiting] = {}

    def windows(self) -> int | None:
        """How many owner windows hold an event stream now, or None when the
        daemon cannot say (then the call waits as if one did)."""
        count = getattr(self.daemon, "hud_window_count", None)
        return count() if callable(count) else None

    def expect(self, count: int) -> str | None:
        """A new request id for an ask of `count` panes, or None when too many
        asks already wait (the ask is then published without one, and the
        call does not wait)."""
        if type(count) is not int or not 1 <= count <= COUNT_MAX:
            return None
        request_id = secrets.token_hex(16)
        with self._lock:
            if len(self._waiting) >= PENDING_MAX:
                return None
            self._waiting[request_id] = _Waiting(count)
        return request_id

    def wait(self, request_id: str | None, timeout: float = REPORT_WAIT_S) -> dict | None:
        """The first window's answer, `{placed, not_placed}`, or None when none
        came within `timeout` (or the daemon is stopping). The id is forgotten
        either way, so a late answer gets 404."""
        with self._lock:
            waiting = self._waiting.get(request_id) if request_id else None
        if waiting is None:
            return None
        deadline = self.clock() + timeout
        try:
            while not waiting.event.is_set():
                left = deadline - self.clock()
                if left <= 0 or getattr(self.daemon, "_stopping", False):
                    break
                waiting.event.wait(min(left, _POLL_S))
        finally:
            with self._lock:
                self._waiting.pop(request_id, None)
        return dict(waiting.answer) if waiting.answer is not None else None

    def forget(self, request_id: str | None) -> None:
        with self._lock:
            self._waiting.pop(request_id, None)

    def report(self, body) -> dict:
        """A window's answer (the route, after the owner checks)."""
        if not isinstance(body, dict) or set(body) != set(KEYS):
            raise ReportRefused(400, "a report is {window, request_id, placed, not_placed}")
        window, request_id = body["window"], body["request_id"]
        placed, not_placed = body["placed"], body["not_placed"]
        if not isinstance(window, str) or not _WINDOW.fullmatch(window):
            raise ReportRefused(400, "invalid window")
        if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
            raise ReportRefused(400, "invalid request id")
        for value in (placed, not_placed):
            if type(value) is not int or not 0 <= value <= COUNT_MAX:
                raise ReportRefused(400, f"placed and not_placed are whole numbers from 0 to {COUNT_MAX}")
        live = getattr(self.daemon, "hud_window_live", None)
        if not callable(live) or not live(window):
            raise ReportRefused(409, "a live owner window is required")
        with self._lock:
            waiting = self._waiting.get(request_id)
            if waiting is None:
                raise ReportRefused(404, "no ask is waiting for that report")
            if waiting.answer is not None:
                raise ReportRefused(409, "another window already answered")
            if placed + not_placed != waiting.count:
                raise ReportRefused(400, "placed and not_placed must add up to the panes asked for")
            waiting.answer = {"placed": placed, "not_placed": not_placed}
            waiting.event.set()
        return {"ok": True}


def route(handler, daemon, parts, query):
    """`/agent-panes…`, or None."""
    if not parts or parts[0] != "agent-panes":
        return None
    from .daemon import APIError, _object
    from .projects import owner_only
    # Before anything else: the owner's window, on the HUD listener, on loopback.
    owner_only(handler, daemon)
    if not config.own_host_header(handler.headers.get("Host", ""), daemon.face_port):
        raise APIError(403, "untrusted Host")
    if parts != ["agent-panes", "report"]:
        raise APIError(404, "route not found")
    if handler.command != "POST":
        raise APIError(405, "a report is sent with POST")
    _object(query, ())
    reports = getattr(daemon, "agent_pane_reports", None)
    if reports is None:
        raise APIError(503, "Jarvis is starting")
    try:
        return 200, reports.report(handler._body())
    except ReportRefused as refusal:
        raise APIError(refusal.status, str(refusal)) from None
