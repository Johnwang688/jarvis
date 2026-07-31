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

Every failure mode denies:

  - no window connected      -> deny immediately; there is nothing to ask on
  - nobody answers in time   -> deny on timeout
  - window closed while up   -> deny when the last viewer goes
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
    ):
        self._broadcast = broadcast
        self._viewers = viewers
        self.timeout_s = timeout_s
        self._announce = announce
        self._on_always = on_always
        self._pending: dict[str, Pending] = {}
        self._lock = threading.Lock()
        self.decisions: list[dict[str, Any]] = []

    # -- the agent side ----------------------------------------------------

    def approver(self) -> Callable[[Any, dict], bool]:
        """The callable to hand to `Agent(approve=...)`."""
        return lambda tool, args: self.request(tool.name, args)

    def request(self, tool_name: str, args: dict[str, Any]) -> bool:
        if self._viewers() == 0:
            return self._record(tool_name, args, False, "no-window", 0.0)

        item = Pending(
            id=secrets.token_urlsafe(9),
            tool=tool_name,
            args=dict(args),
            asked_at=time.monotonic(),
        )
        with self._lock:
            self._pending[item.id] = item

        self._broadcast(
            "approval",
            {
                "id": item.id,
                "tool": item.tool,
                "args": _for_display(item.args),
                "timeout_s": round(self.timeout_s),
            },
        )
        item.event.wait(self.timeout_s)

        with self._lock:
            self._pending.pop(item.id, None)
        waited = time.monotonic() - item.asked_at
        if not item.event.is_set():
            # Nobody answered. Take the card down so the window does not keep
            # offering a button that no longer decides anything.
            self._broadcast("approval_closed", {"id": item.id, "resolution": "timeout"})
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

    def deny_all(self, resolution: str = "cancelled") -> int:
        """Release every waiter with a denial — window gone, or shutting down."""
        with self._lock:
            items = [i for i in self._pending.values() if not i.event.is_set()]
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
