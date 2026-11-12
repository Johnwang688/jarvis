"""Opening a chat's native sub-agents in the owner's HUD panes, on the chat's
word (the owner's ask, 2026-11-12: "I can tell Jarvis to open the sub-agents
in the panels and then it can open").

`hud_open_subagents` (`tools/hud_subagents.py`) reaches this service and
nothing else. It publishes one bus record, `subagent_open`, carrying the
calling chat's thread id and the sids of that chat's own sub-agents, ids
only. Each HUD window then places them itself (`App.tsx` `openSubagent`,
`byAgent`): in an existing drawn pane without unsaved work, never the pane
showing this chat, never the pane the owner has focused, never moving focus;
else a split; else it says there is no room. What a pane showed before is
kept in its spec, so the owner switching it back gets it again.

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
    def __init__(self, daemon):
        self.daemon = daemon
        self.closed = True

    def _refuse_caller(self, caller) -> tuple[str | None, Any]:
        """Under the daemon lock: `decisions.OwnerPosts._refuse_caller`'s
        checks (the live session, its exact turn, no relayed turn, not
        stopped, an owner chat, Claude or Codex, the owner at the desk)."""
        from .model import ProviderName, Role
        daemon = self.daemon
        if self.closed or daemon._stopping:
            return STOPPING, None
        session = daemon._sessions.get(caller["thread_id"])
        if (session is None or session.worker is None or session.handle is None
                or session.turn_id != caller["turn_id"]):
            return NOT_AT_DESK, None
        if getattr(session, "relay_turn", None) is not None:
            return NOT_AT_DESK, None
        if session.cancelled.is_set() or session.closing or session.retired or session.lost:
            return "Error: this turn was stopped, so it opens nothing more.", None
        if session.brief.role != Role.CHAT or session.brief.task_id is not None:
            return NOT_AT_DESK, None
        if session.thread.provider not in (ProviderName.CLAUDE, ProviderName.CODEX):
            return NOT_AT_DESK, None
        if session.desk.slot.get("present") is not True:
            return NOT_AT_DESK, None
        return None, session

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
        from .model import utcnow
        self.daemon.bus.publish({"kind": "subagent_open", "thread_id": thread_id, "at": utcnow(),
                                 "data": {"sids": [r["sid"] for r in chosen]}})
        names = ", ".join(r.get("name") or r["sid"] for r in chosen)
        return (f"Asked the HUD to open {len(chosen)} sub-agent view(s): {names}. Each window puts "
                "them in its existing panes that hold no unsaved work (never the pane showing "
                "this chat or the one the owner is in), splits when none is free, and says so "
                "when there is no room. Nothing comes back to this chat.")

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
