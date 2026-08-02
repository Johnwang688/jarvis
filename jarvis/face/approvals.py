"""Human approval for dangerous tools, routed through the face.

The CLI gates `dangerous=True` tools with a y/N prompt on stdin. The face has
no stdin, so it hard-denied every one of them — safe, but it left Jarvis
read-only in the window the owner actually uses.

This is the same gate, moved to the HUD. `dispatch()` calls the approver
synchronously from the agent thread, so `request()` **blocks that thread**
while the card is up: the pending decision goes out over SSE, the window POSTs
/approve, and the waiting thread wakes with the answer. The server is
threaded, so the deciding request runs on a different thread than the one it
unblocks — that is the whole trick, and it is why the face server must stay a
ThreadingHTTPServer.

A request can also be put to the owner *remotely* — see discord_approvals.py,
which DMs the same question and resolves it the same way. The broker stays
ignorant of Discord: it holds a `remote` channel with ask/close, and whichever
surface answers first wins, because the id can only be resolved once.

Every failure mode denies:

  - nowhere to ask it        -> deny immediately (no window, no remote)
  - nobody answers in time   -> deny on timeout
  - window closed while up   -> deny when the last viewer goes, unless the
                                question also went out remotely (the owner is
                                not at the window by definition)
  - shutdown                 -> deny

The request id is a one-shot 72-bit token. An approval therefore cannot be
replayed, cannot be guessed by a drive-by POST from another page, and cannot
be applied to a later request than the one the owner actually read.

What this does *not* defend against: any local process that can read the SSE
stream can also approve, because the face binds to 127.0.0.1 and has no user
auth. That means anything already running as the owner — which could just run
the command itself instead. The agent's own escape route is the one that
mattered, and it is closed elsewhere: `config.is_face_origin()` makes the
browser and web tools refuse the face's origin, so Jarvis cannot open his own
HUD and click his own button.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

TIMEOUT_S = 120.0
MAX_ARG_CHARS = 600  # what the card shows; the tool still gets the full value


@dataclass
class Pending:
    id: str
    tool: str
    args: dict[str, Any]
    asked_at: float
    event: threading.Event = field(default_factory=threading.Event)
    allowed: bool = False
    resolution: str = "timeout"
    remote: bool = False
    """The question also went out over the remote channel (a Discord DM)."""


def _for_display(args: dict[str, Any]) -> dict[str, str]:
    shown = {}
    for key, value in args.items():
        text = value if isinstance(value, str) else str(value)
        if len(text) > MAX_ARG_CHARS:
            text = text[:MAX_ARG_CHARS] + f"… [+{len(text) - MAX_ARG_CHARS} chars]"
        shown[key] = text
    return shown


class ApprovalBroker:
    """Blocks the agent thread until a window answers, or denies trying."""

    def __init__(
        self,
        broadcast: Callable[[str, Any], None],
        viewers: Callable[[], int],
        timeout_s: float = TIMEOUT_S,
        announce: Callable[[str], None] = print,
        on_always: Callable[[str, dict], None] | None = None,
        remote: Any = None,
    ):
        self._broadcast = broadcast
        self._viewers = viewers
        self.timeout_s = timeout_s
        self._announce = announce
        self._on_always = on_always
        # Optional away-from-desk channel: .ask(item) -> delivered?,
        # .close(id, resolution), .timeout_s. See discord_approvals.py.
        self._remote = remote
        self._pending: dict[str, Pending] = {}
        self._lock = threading.Lock()
        self.decisions: list[dict[str, Any]] = []

    # -- the agent side ----------------------------------------------------

    def approver(self, remote: bool = False) -> Callable[[Any, dict], bool]:
        """The callable to hand to `Agent(approve=...)`.

        `remote=True` marks an agent whose owner is, by construction, not at
        the window — the Discord agent. Its questions always go out over the
        remote channel as well, because a card in a HUD nobody is looking at
        is just a slower denial.
        """
        return lambda tool, args: self.request(tool.name, args, prefer_remote=remote)

    def request(
        self, tool_name: str, args: dict[str, Any], prefer_remote: bool = False
    ) -> bool:
        item = Pending(
            id=secrets.token_urlsafe(9),
            tool=tool_name,
            args=dict(args),
            asked_at=time.monotonic(),
        )
        with self._lock:
            self._pending[item.id] = item

        watching = self._viewers() > 0
        # Ask remotely *first* so the card can carry the real deadline: a
        # window that withdraws its buttons at 120s while the DM is still
        # live would leave the owner unable to answer from either place.
        if self._remote is not None and (prefer_remote or not watching):
            try:
                item.remote = bool(self._remote.ask(item))
            except Exception as exc:  # a broken channel must not grant anything
                self._announce(f"[approval] remote ask failed: {type(exc).__name__}: {exc}")

        if not watching and not item.remote:
            with self._lock:
                self._pending.pop(item.id, None)
            return self._record(tool_name, args, False, "nowhere-to-ask", 0.0)

        timeout = self._remote.timeout_s if item.remote else self.timeout_s
        if watching:
            self._broadcast(
                "approval",
                {
                    "id": item.id,
                    "tool": item.tool,
                    "args": _for_display(item.args),
                    "timeout_s": round(timeout),
                    "remote": item.remote,
                },
            )
        item.event.wait(timeout)

        with self._lock:
            self._pending.pop(item.id, None)
        waited = time.monotonic() - item.asked_at
        if not item.event.is_set():
            # Nobody answered. Take the card down so the window does not keep
            # offering a button that no longer decides anything.
            self._broadcast("approval_closed", {"id": item.id, "resolution": "timeout"})
        if item.remote and self._remote is not None:
            try:
                self._remote.close(item.id, item.resolution)
            except Exception:
                pass  # closing is courtesy; the decision already stands
        return self._record(item.tool, item.args, item.allowed, item.resolution, waited)

    # -- the window side ---------------------------------------------------

    def resolve(self, request_id: str, allowed: bool, always: bool = False) -> bool:
        """Answer a pending request. False if the id is unknown or already used.

        `always` (only meaningful with allowed=True) additionally records a
        persistent allowlist entry via the on_always hook — the owner's
        explicit "stop asking me about this one".
        """
        with self._lock:
            item = self._pending.get(request_id)
            if item is None or item.event.is_set():
                return False
            item.allowed = bool(allowed)
            item.resolution = "approved" if item.allowed else "denied"
            if always and item.allowed and self._on_always is not None:
                try:
                    self._on_always(item.tool, item.args)
                    item.resolution = "approved-always"
                except Exception:
                    pass  # the approval itself must still go through
            item.event.set()
        self._broadcast(
            "approval_closed", {"id": request_id, "resolution": item.resolution}
        )
        return True

    def deny_all(self, resolution: str = "cancelled", include_remote: bool = True) -> int:
        """Release every waiter with a denial — window gone, or shutting down.

        `include_remote=False` spares questions that also went out as a DM:
        the window closing says nothing about whether the owner is going to
        answer on their phone, and denying there would make the remote path
        useless the moment the HUD is shut.
        """
        with self._lock:
            items = [
                i
                for i in self._pending.values()
                if not i.event.is_set() and (include_remote or not i.remote)
            ]
            for item in items:
                item.allowed = False
                item.resolution = resolution
                item.event.set()
        for item in items:
            self._broadcast(
                "approval_closed", {"id": item.id, "resolution": resolution}
            )
        return len(items)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    # -- record ------------------------------------------------------------

    def _record(
        self, tool: str, args: dict, allowed: bool, resolution: str, waited: float
    ) -> bool:
        entry = {
            "tool": tool,
            "args": _for_display(args),
            "allowed": allowed,
            "resolution": resolution,
            "waited_s": round(waited, 1),
        }
        self.decisions.append(entry)
        summary = " ".join(f"{k}={v!r}" for k, v in entry["args"].items())
        self._announce(f"[approval] {resolution}: {tool} {summary[:200]}")
        return allowed
