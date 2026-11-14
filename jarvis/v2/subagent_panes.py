"""Opening a chat's native sub-agents in the owner's HUD panes, on the chat's
word (the owner's ask, 2026-11-12: "I can tell Jarvis to open the sub-agents
in the panels and then it can open").

`hud_open_subagents` (`tools/hud_subagents.py`) reaches this service and
nothing else. It publishes one bus record, `subagent_open`, carrying the
calling chat's thread id, the sids of that chat's own sub-agents and a
random request id, ids only. Each HUD window then places them itself, as one
batch (`App.tsx` `placeAgentBatch`, `lib/agentpanes.ts`; 2026-11-13,
`docs/plans/2026-11-13-agent-pane-grid-decisions.md`): in existing drawn
panes without unsaved work, never the pane showing this chat, never the pane
the owner has focused, never moving focus; then the window laid out once as
a balanced grid; what does not fit is counted. What a pane showed before is
kept in its spec, so the owner switching it back gets it again. The window
posts back how many it showed (`agent_panes.py`), and the call waits up to
three seconds for the first answer, never under the daemon lock.

What it cannot do: name another chat's sub-agents (the sids come from the
caller's own thread's records), open anything but a read-only sub-agent
view, close a pane, or take focus. Bound as `owner_post` is: a turn the
owner started at the HUD in this Claude or Codex chat, the conversation
itself (not a sub-agent), checked by the tool and again here under the
daemon lock against the live session and its exact turn.
"""
from __future__ import annotations

import threading
from typing import Any

from .agent_panes import REPORT_WAIT_S, PaneReports, refuse_desk_caller
from .subagents import LIVE, valid_sid

NOT_IN_CHAT = "Error: hud_open_subagents works only inside a Jarvis HUD chat."
NOT_AT_DESK = ("Error: sub-agents can be opened in panes only during a turn the owner started "
               "at the HUD in this Claude or Codex chat (not Discord, the DM, a schedule, a "
               "task, a sub-agent or a turn another chat's message started).")
NOT_FROM_A_SUB_AGENT = ("Error: a sub-agent cannot open panes; only the conversation itself "
                        "can.")
STOPPING = "Error: Jarvis is stopping, so no panes are opened."

# The HUD draws at most 12 panes; one shows this chat.
OPEN_MAX = 11
NAME_MAX = 200


def _same(text: str) -> str:
    return " ".join(text.split()).casefold()


class SubagentPanes:
    def __init__(self, daemon, reports: PaneReports | None = None):
        self.daemon = daemon
        self.closed = True
        # The daemon's one report waiter (agent_panes.py), shared with any
        # other ask for panes; a stub daemon in a test gets its own.
        self.reports = reports or getattr(daemon, "agent_pane_reports", None) or PaneReports(daemon)

    def _refuse_caller(self, caller) -> tuple[str | None, Any]:
        """Under the daemon lock: `decisions.OwnerPosts._refuse_caller`'s
        checks (the live session, its exact turn, no relayed turn, not
        stopped, an owner chat, Claude or Codex, the owner at the desk).
        Shared with `hud_open_browser` (`agent_panes.refuse_desk_caller`)."""
        return refuse_desk_caller(self.daemon, self.closed, caller, NOT_AT_DESK, STOPPING)

    def open(self, caller: dict | None, which: list | None) -> str:
        """Ask every HUD window to show this chat's sub-agents `which` (each a
        sid or a name; None: every one still running, else every one this
        turn started). Returns what was asked, or a sentence starting
        "Error: " (and then nothing was published)."""
        if (not isinstance(caller, dict) or not isinstance(caller.get("thread_id"), str)
                or not isinstance(caller.get("turn_id"), str)):
            return NOT_IN_CHAT
        if which is not None:
            if (not isinstance(which, list) or not which or len(which) > OPEN_MAX
                    or not all(isinstance(w, str) and w.strip() and len(w) <= NAME_MAX for w in which)):
                return (f"Error: which is a list of 1 to {OPEN_MAX} sub-agent names or ids (each "
                        f"at most {NAME_MAX} characters), or left out for every running one.")
        with self.daemon._lock:
            why_not, session = self._refuse_caller(caller)
            if why_not is not None:
                return why_not
            thread_id = session.thread.id
        # Top-level children only: a nested one is drawn in its parent's pane.
        rows = [r for r in self.daemon.subagents.list(thread_id)
                if valid_sid(r.get("sid")) and not r.get("parent_sid")]
        if not rows:
            return ("Error: this chat has no sub-agents to open. Start them first (in the "
                    "background, so they are still running), then call this.")
        if which is None:
            chosen = [r for r in rows if r.get("status") in LIVE]
            if not chosen:
                chosen = [r for r in rows if r.get("turn_id") == caller["turn_id"]]
            if not chosen:
                return ("Error: none of this chat's sub-agents is running or was started this "
                        "turn; name the ones to open.")
        else:
            chosen, missing = [], []
            for wanted in which:
                key = wanted.strip()
                hit = next((r for r in rows if r["sid"] == key), None)
                if hit is None:
                    # The newest of that name: a name can repeat across turns.
                    hit = next((r for r in reversed(rows) if _same(r.get("name") or "") == _same(key)), None)
                if hit is None:
                    missing.append(key)
                elif hit not in chosen:
                    chosen.append(hit)
            if missing:
                names = ", ".join(sorted({r.get("name") or r["sid"] for r in rows})[:20])
                return (f"Error: this chat has no sub-agent called {', '.join(missing)[:300]}. "
                        f"Its sub-agents: {names}.")
        chosen = chosen[-OPEN_MAX:]
        names = ", ".join(r.get("name") or r["sid"] for r in chosen)
        return self._publish_and_wait(thread_id, [r["sid"] for r in chosen], names)

    def _publish_and_wait(self, thread_id: str, sids: list, names: str) -> str:
        """Publish the ask with a request id, then wait (never under the daemon
        lock) for the first window's counts (`agent_panes.PaneReports`)."""
        from .model import utcnow
        reports = self.reports
        n = len(sids)
        windows = reports.windows()
        request_id = reports.expect(n) if windows != 0 else None
        data = {"sids": list(sids)}
        if request_id is not None:
            data["request_id"] = request_id
        try:
            self.daemon.bus.publish({"kind": "subagent_open", "thread_id": thread_id, "at": utcnow(),
                                     "data": data})
        except Exception:
            reports.forget(request_id)
            raise
        asked = f"Asked the HUD to open {n} sub-agent view(s): {names}."
        if windows == 0:
            return f"{asked} No HUD window is open, so none is shown."
        answer = reports.wait(request_id)
        if answer is None:
            return (f"{asked} No HUD window replied within {REPORT_WAIT_S:g} seconds (a card or "
                    f"Settings may be open there), so whether all {n} fit is not known.")
        placed, not_placed = answer["placed"], answer["not_placed"]
        if not_placed == 0:
            return f"Shown {placed} of {n} sub-agent(s) in HUD panes: {names}."
        return (f"Shown {placed} of {n} sub-agent(s) in HUD panes; no room for {not_placed} (the "
                "window is too small for more panes, or the rest hold the owner's work).")

    # -- install / stop (as decisions.OwnerPosts) ---------------------------------------

    def install(self) -> None:
        global _installed
        with _installed_lock:
            self.closed = False
            _installed = self

    def stop(self) -> None:
        global _installed
        self.closed = True
        with _installed_lock:
            if _installed is self:
                _installed = None


_installed_lock = threading.Lock()
_installed: SubagentPanes | None = None


def installed() -> SubagentPanes | None:
    """The running daemon's service, or None (no daemon, or it stopped)."""
    with _installed_lock:
        service = _installed
    return service if service is not None and not service.closed else None
