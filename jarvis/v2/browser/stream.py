"""A Browser tab's stream and the owner's input to it (BR-1b; plan §2 "The
stream", §8, §11; amendment 1 §B.8–§B.9).

**One socket per tab per window**, on the terminal's RFC 6455 endpoint
(`jarvis/v2/ws.py`) and its pattern: every route is `projects.owner_only`
plus a strict loopback Host; the socket needs a **single-use, 30 s ticket
bound to one tab** (`Tickets`), minted by an owner-only JSON POST; a tab
another window shows is taken only after that window says yes (refused
after 20 s with no answer); and every attach publishes `browser_attached`
with exactly `{tab_id, at}` so the HUD can tell an attach that was not its
own. Not a boundary against a program running as the owner, as for
terminals.

**Frames** (daemon → window, binary): the tab's screencast, one JPEG per
binary message behind an 8-byte header — the frame's id (uint32), then the
viewport it shows in CSS px, width and height (uint16 each), all big-endian.
**Latest only, one in flight**: a frame goes out only when the window has
acked the last one (`{t:"ack", id}` reads "up to here", as BR-2's
`FramePipe` acks the newest frame it handled); what arrives meanwhile waits
in a single slot, each newer frame replacing it. A slow window shows the
newest picture and never builds a backlog: the socket's queue holds at most
one frame. A tab no window streams sends nothing.

**Input** (window → daemon, text JSON, BR-2's `remote.ts` shapes):
`{t:"key", e}` → `Input.dispatchKeyEvent`, `{t:"mouse", e}` →
`Input.dispatchMouseEvent` (the wheel included), `{t:"text", text}` →
`Input.insertText`, and `{t:"dialog", id, accept, text?}` answering a dialog.
**Every message is validated here, its shape and its bounds, whatever the
window mapped** (`parse`): an unknown field, type, key, button or modifier
bit, a coordinate off a 4096-px viewport, a wheel past 5000 px, text past
16 KiB or holding a control character other than tab and line ends — refused
with `{t:"bad", reason}` (a fixed sentence, never the content).

**The hold** (plan §8.3, amendment H3): while **any** approval is pending
(`daemon.approvals.pending()`), nothing reaches the page. It is checked when
a message arrives and again on the Browser's thread immediately before each
CDP event is sent, and **nothing held is ever replayed**: every message
received up to the moment a hold was seen is dropped, even one that waited
in the queue until after the card went (`_drop_through`). The window hears
`{t:"held"}`. Frames keep streaming, view-only. This is the daemon's twin of
BR-2's client hold, and the real layer: the HUD's is a courtesy.

**A page that falls behind gets the newest pointer, not a backlog**
(2026-10-14). Chrome answers a pointer move, a drag or a wheel only at its
next frame (16.7 ms, measured, Chrome 155), and the pump sends one event at
a time, so a window on a 160 Hz display queued moves faster than they went
out and every click waited behind them. A move or a wheel still in the
queue is merged with the next one of the same kind (`_merged`): a move by
the newer position, a wheel at the same point by its summed deltas, within
WHEEL_MAX. Only an event nobody has taken yet is merged, so a page that
keeps up sees every event, and nothing else is ever reordered or merged —
a press, a release, a key or text between two moves keeps both.

**The owner's input pauses an agent** (amendment §B.9): every input that
counts as taking over (keys but a lone modifier, text, the wheel, a pointer
press or release, a drag, a dialog or file-chooser answer — never a plain
pointer move or a socket's own control messages, BR-2's `isTakeover`) is
stamped (`owner_input_at`, for H4's five quiet seconds) and, while an agent
holds the tab, pauses it **before** the event is queued, telling the window
`{t:"agent", kind:"paused", paused:true, by:"owner"}`. Resume (refused under
a card) and Stop come over the socket. No agent drives yet (BR-9b); the
`{t:"agent"}` channel — `driver`, `action`, `paused`, `list`, the shapes in
BR-2's `drive.ts` — is here for the banner to read, the list (50 at most)
kept in memory and sent only to this tab's socket.

**Nothing leaves memory**: no frame, URL, title, typed text, dialog text or
file name reaches the bus or the log. The bus gets `browser_attached` (an id
and a time); the log gets lifecycle lines with ids and classes.
"""

from __future__ import annotations

import hmac
import json
import logging
import math
import secrets
import struct
import threading
import time
from collections import deque

from .. import ws

LOG = logging.getLogger("jarvis.v2.browser")

TICKET_TTL_S = 30.0
TAKEOVER_TIMEOUT_S = 20.0
INPUT_QUEUE_MAX = 256               # input messages waiting for the Browser's thread
TEXT_MAX_BYTES = 16 * 1024          # one `{t:"text"}` (BR-2's PASTE_CHUNK, as UTF-8)
DIALOG_TEXT_MAX = 4096              # a prompt's answer
FRAME_MAX_BYTES = 8 * 1024 * 1024   # BR-2's FRAME_MAX_BYTES: a larger frame is never sent
SOCKET_PENDING = 12 * 1024 * 1024   # one frame plus control messages; past it the socket drops
HELD_NOTICE_S = 1.0                 # `held` at most this often while input is dropped
VIEW_MAX = 4096                     # BR-2's VIEW_MAX, CSS px each way
MAX_DEVICE_PIXELS = 4096 * 4096
WHEEL_MAX = 5000
AGENT_LIST_MAX = 50
AGENT_LINGER_S = 30.0
AGENT_TEXT_CAP = 60
FRAME_HEADER = struct.Struct("!IHH")

TOKEN = __import__("re").compile(r"[0-9a-f]{8}")


class Bad(Exception):
    """An input or control message of the wrong shape: a fixed sentence."""


# -- validation (pure) -----------------------------------------------------------------

_KEY_TYPES = frozenset({"keyDown", "rawKeyDown", "keyUp"})
_MOUSE_TYPES = frozenset({"mousePressed", "mouseReleased", "mouseMoved", "mouseWheel"})
_BUTTONS = frozenset({"none", "left", "middle", "right", "back", "forward"})
_KEY_FIELDS = frozenset({"type", "modifiers", "key", "code", "windowsVirtualKeyCode",
                         "nativeVirtualKeyCode", "text", "unmodifiedText", "autoRepeat",
                         "isKeypad", "location"})
_MOUSE_FIELDS = frozenset({"type", "x", "y", "modifiers", "button", "buttons", "clickCount",
                           "deltaX", "deltaY", "pointerType"})
# BR-2's MODIFIER_KEYS: a lone modifier is never the owner taking over.
MODIFIER_KEYS = frozenset({"Control", "Alt", "Shift", "Meta", "AltGraph", "OS", "Super", "Hyper",
                           "Fn", "FnLock"})
KEY_NAME_MAX = 32
KEY_TEXT_MAX = 8


def _is_int(value, lo: int, hi: int) -> bool:
    return type(value) is int and lo <= value <= hi


def _is_num(value, lo: float, hi: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and lo <= value <= hi


def _control(ch: str, allowed: str) -> bool:
    c = ord(ch)
    return (c < 0x20 and ch not in allowed) or 0x7F <= c <= 0x9F


def _clean_text(value, *, cap_bytes: int, allowed: str, what: str) -> str:
    if not isinstance(value, str):
        raise Bad(f"{what} must be a string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise Bad(f"{what} is not valid text") from None
    if size > cap_bytes:
        raise Bad(f"{what} is too long")
    if any(_control(ch, allowed) for ch in value):
        raise Bad(f"{what} holds a control character")
    return value


def _key(e) -> dict:
    if not isinstance(e, dict) or e.keys() - _KEY_FIELDS:
        raise Bad("a key event has the wrong shape")
    if e.get("type") not in _KEY_TYPES:
        raise Bad("a key event's type is not one the page takes")
    if not _is_int(e.get("modifiers", 0), 0, 15):
        raise Bad("a key event's modifiers are out of range")
    key = e.get("key")
    if not isinstance(key, str) or not 1 <= len(key) <= KEY_NAME_MAX or any(_control(c, "") for c in key):
        raise Bad("a key event's key is not a key")
    code = e.get("code", "")
    if not isinstance(code, str) or len(code) > KEY_NAME_MAX or not code.isascii() or not (code == "" or code.isalnum()):
        raise Bad("a key event's code is not a key code")
    for name in ("windowsVirtualKeyCode", "nativeVirtualKeyCode"):
        if name in e and not _is_int(e[name], 0, 255):
            raise Bad("a key event's key code is out of range")
    for name in ("text", "unmodifiedText"):
        if name in e:
            text = e[name]
            if not isinstance(text, str) or len(text) > KEY_TEXT_MAX:
                raise Bad("a key event's text is too long")
            # A key types one character, or Enter's "\r".
            if text != "\r" and any(_control(c, "") for c in text):
                raise Bad("a key event's text holds a control character")
            try:
                text.encode("utf-8")
            except UnicodeEncodeError:
                raise Bad("a key event's text is not valid text") from None
    for name in ("autoRepeat", "isKeypad"):
        if name in e and type(e[name]) is not bool:
            raise Bad("a key event's flags must be booleans")
    if "location" in e and not _is_int(e["location"], 0, 3):
        raise Bad("a key event's location is out of range")
    out = {"type": e["type"], "modifiers": e.get("modifiers", 0), "key": key, "code": code}
    for name in ("windowsVirtualKeyCode", "nativeVirtualKeyCode", "text", "unmodifiedText",
                 "autoRepeat", "isKeypad", "location"):
        if name in e:
            out[name] = e[name]
    return out


def _mouse(e) -> dict:
    if not isinstance(e, dict) or e.keys() - _MOUSE_FIELDS:
        raise Bad("a pointer event has the wrong shape")
    kind = e.get("type")
    if kind not in _MOUSE_TYPES:
        raise Bad("a pointer event's type is not one the page takes")
    # BR-2's OFF_PAGE is (-1, -1): where a press a card cut off is let go.
    if not _is_num(e.get("x"), -1, VIEW_MAX) or not _is_num(e.get("y"), -1, VIEW_MAX):
        raise Bad("a pointer event is off the page")
    if not _is_int(e.get("modifiers", 0), 0, 15):
        raise Bad("a pointer event's modifiers are out of range")
    button = e.get("button", "none")
    if button not in _BUTTONS:
        raise Bad("a pointer event's button is not a button")
    if not _is_int(e.get("buttons", 0), 0, 31):
        raise Bad("a pointer event's buttons are out of range")
    if not _is_int(e.get("clickCount", 0), 0, 3):
        raise Bad("a pointer event's click count is out of range")
    if e.get("pointerType", "mouse") != "mouse":
        raise Bad("a pointer event must be a mouse's")
    wheel = kind == "mouseWheel"
    if wheel != ("deltaX" in e and "deltaY" in e) or (not wheel and ("deltaX" in e or "deltaY" in e)):
        raise Bad("only a wheel event carries deltas, and it carries both")
    out = {"type": kind, "x": e["x"], "y": e["y"], "modifiers": e.get("modifiers", 0), "button": button,
           "buttons": e.get("buttons", 0), "clickCount": e.get("clickCount", 0), "pointerType": "mouse"}
    if wheel:
        if not _is_num(e["deltaX"], -WHEEL_MAX, WHEEL_MAX) or not _is_num(e["deltaY"], -WHEEL_MAX, WHEEL_MAX):
            raise Bad("a wheel event scrolls too far")
        out["deltaX"], out["deltaY"] = e["deltaX"], e["deltaY"]
    return out


def takes_over(method: str, params: dict) -> bool:
    """BR-2's `isTakeover` for one input event: a pointer move with no button
    held and a lone modifier are not the owner taking over; every other key,
    text, the wheel, a press, a release and a drag are."""
    if method == "Input.dispatchKeyEvent":
        return params.get("key") not in MODIFIER_KEYS
    if method == "Input.dispatchMouseEvent" and params.get("type") == "mouseMoved":
        return params.get("buttons", 0) != 0
    return True


def _merged(older: "Msg", newer: "Msg") -> "Msg | None":
    """`newer` taking the place of a still-queued `older` (the module's
    "newest pointer, not a backlog"), or None: a pointer move by a move with
    the same buttons and modifiers, a wheel by a wheel at the same point
    whose summed deltas stay within WHEEL_MAX."""
    if older.method != "Input.dispatchMouseEvent" or newer.method != older.method:
        return None
    a, b = older.params, newer.params
    if a["type"] != b["type"] or any(a.get(k) != b.get(k) for k in ("button", "buttons", "modifiers")):
        return None
    if b["type"] == "mouseMoved":
        return newer
    if b["type"] != "mouseWheel" or (a["x"], a["y"]) != (b["x"], b["y"]):
        return None
    dx, dy = a["deltaX"] + b["deltaX"], a["deltaY"] + b["deltaY"]
    if abs(dx) > WHEEL_MAX or abs(dy) > WHEEL_MAX:
        return None
    return Msg(newer.t, newer.method, {**b, "deltaX": dx, "deltaY": dy}, **newer.fields)


class Msg:
    """One parsed message from the window."""

    __slots__ = ("t", "method", "params", "fields")

    def __init__(self, t: str, method: str | None = None, params: dict | None = None, **fields):
        self.t = t
        self.method = method
        self.params = params or {}
        self.fields = fields

    @property
    def input(self) -> bool:
        return self.method is not None

    @property
    def takeover(self) -> bool:
        if self.t == "dialog":
            return True
        return self.method is not None and takes_over(self.method, self.params)


def _fields(m: dict, allowed: tuple, required: tuple = ()) -> None:
    if m.keys() - {"t", *allowed} or set(required) - m.keys():
        raise Bad("a message has the wrong fields")


def parse(raw: bytes) -> Msg:
    """One text message from the window, validated; `Bad` otherwise."""
    try:
        m = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise Bad("a message is not JSON") from None
    if not isinstance(m, dict) or not isinstance(m.get("t"), str):
        raise Bad("a message has no type")
    t = m["t"]
    if t in ("find", "find_cancel", "cursor", "cursor_cancel"):
        from .owner_tools import parse_message, OwnerBad
        try:
            fields = parse_message(m)
        except OwnerBad as exc:
            raise Bad(str(exc)) from None
        return Msg(t, **{key: value for key, value in fields.items() if key != "t"})
    if t == "key":
        _fields(m, ("e",), ("e",))
        return Msg(t, "Input.dispatchKeyEvent", _key(m["e"]))
    if t == "mouse":
        _fields(m, ("e",), ("e",))
        return Msg(t, "Input.dispatchMouseEvent", _mouse(m["e"]))
    if t == "text":
        _fields(m, ("text",), ("text",))
        text = _clean_text(m["text"], cap_bytes=TEXT_MAX_BYTES, allowed="\t\n\r", what="text")
        if not text:
            raise Bad("text is empty")
        return Msg(t, "Input.insertText", {"text": text})
    if t == "dialog":
        _fields(m, ("id", "accept", "text"), ("id", "accept"))
        if not isinstance(m["id"], str) or not TOKEN.fullmatch(m["id"]) or type(m["accept"]) is not bool:
            raise Bad("a dialog answer has the wrong shape")
        text = None
        if "text" in m:
            text = _clean_text(m["text"], cap_bytes=DIALOG_TEXT_MAX, allowed="\t", what="a dialog's answer")
        return Msg(t, id=m["id"], accept=m["accept"], text=text)
    if t == "auth":
        _fields(m, ("id", "cancel", "username", "password"), ("id", "cancel"))
        if (not isinstance(m["id"], str) or not __import__("re").fullmatch(r"[0-9a-f]{32}", m["id"])
                or type(m["cancel"]) is not bool):
            raise Bad("an authentication answer has the wrong shape")
        if m["cancel"]:
            if "username" in m or "password" in m:
                raise Bad("an authentication answer has the wrong shape")
            return Msg(t, id=m["id"], cancel=True)
        from .auth_state import credential_fields, AuthRefused
        try:
            username, password = credential_fields(m.get("username"), m.get("password"))
        except AuthRefused:
            raise Bad("authentication fields are invalid") from None
        return Msg(t, id=m["id"], cancel=False, username=username, password=password)
    if t == "ack":
        _fields(m, ("id",), ("id",))
        if not _is_int(m["id"], 0, 0xFFFFFFFF):
            raise Bad("an ack names no frame")
        return Msg(t, id=m["id"])
    if t == "resize":
        _fields(m, ("width", "height", "deviceScaleFactor"), ("width", "height"))
        width, height = m["width"], m["height"]
        scale = m.get("deviceScaleFactor", 1)
        if not _is_int(width, 1, VIEW_MAX) or not _is_int(height, 1, VIEW_MAX) or not _is_num(scale, 0.25, 8):
            raise Bad("a viewport is out of range")
        if width * height * scale * scale > MAX_DEVICE_PIXELS * 1.0001:
            raise Bad("a viewport holds too many pixels")
        return Msg(t, width=width, height=height, scale=float(scale))
    if t == "visible":
        _fields(m, ("visible",), ("visible",))
        if type(m["visible"]) is not bool:
            raise Bad("visible must be a boolean")
        return Msg(t, visible=m["visible"])
    if t == "takeover":
        _fields(m, ("id", "allow"), ("id", "allow"))
        if not isinstance(m["id"], str) or type(m["allow"]) is not bool:
            raise Bad("a takeover answer has the wrong shape")
        return Msg(t, id=m["id"], allow=m["allow"])
    if t in ("resume", "stop", "selection", "focus", "blur", "ping", "pong"):
        _fields(m, ())
        return Msg(t)
    raise Bad("unknown message")


# -- tickets ------------------------------------------------------------------------------

class Tickets:
    """Single-use, 30 s, one tab each (the terminal's rule)."""

    def __init__(self, clock=time.monotonic):
        self._lock = threading.Lock()
        self._tickets: dict[str, tuple[str, float, int | None]] = {}
        self._clock = clock

    def _expire(self, now: float) -> None:
        for ticket in [k for k, (_, until, _) in self._tickets.items() if until <= now]:
            del self._tickets[ticket]

    def mint(self, tab_id: str, page_identity: int | None = None) -> str:
        ticket = secrets.token_urlsafe(32)
        with self._lock:
            now = self._clock()
            self._expire(now)
            self._tickets[ticket] = (tab_id, now + TICKET_TTL_S, page_identity)
        return ticket

    def redeem(self, ticket, tab_id: str, page_identity: int | None = None) -> bool:
        """Spent by being presented, whatever the answer."""
        if not isinstance(ticket, str) or not ticket:
            return False
        with self._lock:
            now = self._clock()
            entry = self._tickets.pop(ticket, None)
            self._expire(now)
        return (entry is not None and entry[1] > now and entry[2] == page_identity
                and hmac.compare_digest(entry[0], tab_id))

    def revoke(self, tab_id: str) -> None:
        with self._lock:
            for ticket in [k for k, row in self._tickets.items() if row[0] == tab_id]:
                self._tickets.pop(ticket, None)

    def clear(self) -> None:
        with self._lock:
            self._tickets.clear()


# -- one tab's stream -----------------------------------------------------------------------

class _Takeover:
    def __init__(self, holder):
        self.id = secrets.token_hex(4)
        self.holder = holder
        self.allow: bool | None = None
        self.event = threading.Event()


def _clean(text, cap: int) -> str:
    from ..approvals import clean_line
    return clean_line(text if isinstance(text, str) else "", cap)


class TabStream:
    """One tab's attachment, frames, input queue, replayed state and agent
    channel. Thread-safe: the socket's reader thread, the Browser's thread
    and the routes all call in. `wake` is how a queued input reaches the
    Browser's thread (the host sets it)."""

    def __init__(self, tab_id: str, *, held=lambda: False, wake=lambda: None,
                 clock=time.monotonic, blocked=lambda: False):
        self.tab_id = tab_id
        self._held = held
        self.input_blocked = blocked
        self._blocked_notice = False
        self.wake = wake
        self._clock = clock
        self._lock = threading.RLock()
        self._sock = None
        self.gen = 0
        self._takeover: _Takeover | None = None
        self.closed = False
        # Frames.
        self.visible = True
        self._next_frame = 0
        self._inflight: int | None = None
        self._waiting: tuple | None = None
        self._last: tuple | None = None         # the newest picture, for a window that attaches
        self.frames_sent = 0
        self.frames_replaced = 0
        self.frames_dropped = 0
        self.viewport: tuple[int, int, float] | None = None
        # (viewport, wider one) while the host lays the page out wider than
        # the window's viewport (host.py `_fit`); one assignment, so the
        # reader thread never sees half of it.
        self.fitted: tuple | None = None
        # Input.
        self._seq = 0
        self._drop_through = 0
        self._queue: deque = deque()
        self._held_notice = -math.inf
        self.dropped = 0
        self.merged = 0                         # moves and wheels merged into a newer one
        # How many holds this tab has seen: a press delivered before one is
        # "cut off", and its release is never a click (host `_deliver`).
        self.hold_count = 0
        # State a reattaching window is told again (the owner's window only).
        self._replay: dict[str, dict] = {}
        # The agent channel (amendment §B.9; BR-9b drives it).
        self.paused = False
        self.paused_by: str | None = None
        self.driver: tuple[str, str | None] | None = None
        self.actions: deque = deque(maxlen=AGENT_LIST_MAX)
        self.last_action_at: float | None = None
        self.owner_input_at: float | None = None
        self.owner_input_serial = 0

    # -- sending --------------------------------------------------------------------

    def attached(self):
        with self._lock:
            return self._sock

    def holds(self, sock) -> bool:
        return self._sock is sock

    def _send(self, sock, message: dict) -> None:
        if sock is not None:
            sock.send_text(json.dumps(message))

    def send(self, message: dict, *, gen: int | None = None) -> None:
        """To the attached window, if any (and only if it is still `gen`)."""
        with self._lock:
            if gen is not None and gen != self.gen:
                return
            self._send(self._sock, message)

    def send_to(self, sock, message: dict) -> None:
        with self._lock:
            if self._sock is sock:
                self._send(sock, message)

    def remember(self, key: str, message: dict | None, *, send: bool = True) -> None:
        """State a window is told now and again on every attach (a location,
        an open dialog or file chooser); None forgets it."""
        with self._lock:
            if message is None:
                self._replay.pop(key, None)
                return
            self._replay[key] = message
            if send:
                self._send(self._sock, message)

    # -- attaching --------------------------------------------------------------------

    def _install(self, sock, tab_row: dict) -> None:
        """Under `_lock`: `sock` is the tab's window from now on."""
        self._sock = sock
        self.gen += 1
        self._inflight = None
        self._waiting = None
        self._queue.clear()
        self.visible = True
        self._held_notice = -math.inf           # each window hears its first hold
        self._send(sock, {"t": "attached", "tab": tab_row})
        for message in self._agent_replay():
            self._send(sock, message)
        for message in list(self._replay.values()):
            self._send(sock, message)
        # The picture as it stands: a page that does not repaint sends no
        # new frame, and a window taking the tab over must not stay blank.
        if self._last is not None:
            self._send_frame(sock, self._numbered(self._last))

    def attach(self, sock, tab_row: dict, timeout: float = TAKEOVER_TIMEOUT_S) -> str | None:
        """None when attached; otherwise why not. A tab another window shows
        asks that window first."""
        with self._lock:
            if self.closed:
                return "this tab is closing"
            holder = self._sock
            if holder is None or holder.closed.is_set():
                self._install(sock, tab_row)
                return None
            if self._takeover is not None:
                return "another window is already asking for this tab"
            request = self._takeover = _Takeover(holder)
        holder.send_text(json.dumps({"t": "takeover_request", "id": request.id, "timeout_s": timeout}))
        sock.send_text(json.dumps({"t": "waiting", "timeout_s": timeout}))
        request.event.wait(timeout)
        with self._lock:
            if self._takeover is request:
                self._takeover = None
            if self.closed:
                return "this tab is closing"
            current = self._sock
            gone = current is not holder or holder.closed.is_set()
            if request.allow is True or gone:
                if current is holder:
                    self._sock = None
                    holder.send_text(json.dumps({"t": "taken"}))
                    holder.close(ws.NORMAL, "taken by another window")
                if self._sock is not None:
                    return "another window has this tab"
                self._install(sock, tab_row)
                return None
        if request.allow is False:
            return "the window showing this tab kept it"
        return f"the window showing this tab did not answer in {int(timeout)} s"

    def detach(self, sock) -> bool:
        """`sock` goes. True when it was the tab's window."""
        with self._lock:
            was = self._sock is sock
            if was:
                self._sock = None
                self.gen += 1
                self._inflight = None
                self._waiting = None
                self._queue.clear()
            request = self._takeover
            if request is not None and request.holder is sock:
                request.event.set()             # nobody left to ask
        return was

    def answer_takeover(self, sock, msg: Msg) -> None:
        with self._lock:
            request = self._takeover
            if request is not None and request.holder is sock and msg.fields["id"] == request.id:
                request.allow = msg.fields["allow"]
                request.event.set()

    def close(self, reason: str = "closed") -> None:
        """The tab closed, or the Browser stopped: tell the window and let it go."""
        with self._lock:
            if self.closed:
                return
            self.closed = True
            sock, self._sock = self._sock, None
            self.gen += 1
            self._queue.clear()
            self._waiting = None
            self._last = None
            request = self._takeover
        if request is not None:
            request.event.set()
        if sock is not None:
            sock.send_text(json.dumps({"t": "closed", "reason": reason}))
            sock.close(ws.GOING_AWAY if reason == "ended" else ws.NORMAL, reason)

    # -- frames (the Browser's thread offers, the reader thread acks) ------------------------

    def streaming(self) -> bool:
        with self._lock:
            return self._sock is not None and self.visible and not self.closed

    def set_visible(self, sock, visible: bool) -> bool:
        with self._lock:
            if self._sock is not sock:
                return False
            was, self.visible = self.visible, visible
            if not visible:
                self._waiting = None
            elif not was and self._inflight is None and self._last is not None:
                self._send_frame(sock, self._numbered(self._last))
            return True

    def _send_frame(self, sock, frame: tuple) -> None:
        fid, jpeg, width, height = frame
        self._inflight = fid
        self.frames_sent += 1
        sock.send_binary(FRAME_HEADER.pack(fid, width, height) + jpeg)

    def page_viewport(self) -> tuple[int, int, float] | None:
        """The viewport the page is laid out at, in page CSS px: the
        window's, or the fitted one while it is for that same window
        viewport (a resize not yet applied reads as the new size)."""
        viewport, fitted = self.viewport, self.fitted
        if fitted is not None and fitted[0] == viewport:
            return fitted[1]
        return viewport

    def forget_picture(self) -> None:
        """The screencast stopped: the newest picture may no longer be the
        page (it can change unwatched), so a window attaching waits for
        the screencast's own first frame instead."""
        with self._lock:
            self._last = None

    def _numbered(self, picture: tuple) -> tuple:
        """Under `_lock`: a picture as the next frame."""
        self._next_frame = self._next_frame + 1 if self._next_frame < 0xFFFFFFFF else 1
        return (self._next_frame, *picture)

    def offer_frame(self, jpeg: bytes, width: int, height: int) -> None:
        with self._lock:
            if len(jpeg) > FRAME_MAX_BYTES:
                self.frames_dropped += 1
                return
            picture = (jpeg, max(0, min(65535, width)), max(0, min(65535, height)))
            self._last = picture
            sock = self._sock
            if sock is None or not self.visible or self.closed:
                return
            frame = self._numbered(picture)
            if self._inflight is None:
                self._send_frame(sock, frame)
            else:
                if self._waiting is not None:
                    self.frames_replaced += 1
                self._waiting = frame

    def ack(self, sock, frame_id: int) -> None:
        """The window drew (or gave up on) frames up to `frame_id`."""
        with self._lock:
            if sock is not self._sock or self._inflight is None or frame_id < self._inflight:
                return
            self._inflight = None
            frame, self._waiting = self._waiting, None
            if frame is not None and self.visible:
                self._send_frame(sock, frame)

    # -- input (the reader thread queues, the Browser's thread takes) -------------------------

    def _held_notice_due(self) -> bool:
        now = self._clock()
        if now - self._held_notice >= HELD_NOTICE_S:
            self._held_notice = now
            return True
        return False

    def discard_document_input(self) -> None:
        """Retire received old-document input without recording an approval.

        An expected navigation may finish its agent action on the fresh
        document. Its queue cutoff must not look like an intervening card.
        """
        with self._lock:
            self._drop_through = self._seq
            self._queue.clear()

    def hold_through(self, *, notify: bool = True) -> None:
        """A hold was seen: everything received so far is dropped, now and
        whenever it would have been delivered — never replayed after the card."""
        with self._lock:
            self._drop_through = self._seq
            self._queue.clear()
            self.hold_count += 1
            if notify and self._held_notice_due():
                self._send(self._sock, {"t": "held"})

    def owner_input(self) -> None:
        """The owner's input to this tab: stamped, and an agent at work is
        paused before the input is delivered (amendment §B.9)."""
        now = self._clock()
        with self._lock:
            self.owner_input_at = now
            self.owner_input_serial += 1
            if not self.paused and self._agent_active(now):
                self.paused, self.paused_by = True, "owner"
                self._send(self._sock, self._paused_message())

    def drop_blocked_input(self) -> None:
        """One notice per blocked spell; dropped events are never queued."""
        with self._lock:
            self.dropped += 1
            if not self._blocked_notice:
                self._blocked_notice = True
                self._send(self._sock, {"t": "input_dropped",
                                       "reason": "the page has a dialog open or an input still unanswered; this input was dropped"})

    def enqueue(self, sock, msg: Msg) -> None:
        """One input event from the window: held, refused for want of room,
        or queued for the Browser's thread."""
        held = self._held()
        with self._lock:
            if sock is not self._sock:
                return
            self._seq += 1
            if held:
                self._drop_through = self._seq
                self._queue.clear()
                self.hold_count += 1
                self.dropped += 1
                if self._held_notice_due():
                    self._send(sock, {"t": "held"})
                return
            if self.input_blocked():
                self.drop_blocked_input()
                return
            self._blocked_notice = False
            if len(self._queue) >= INPUT_QUEUE_MAX:
                self.dropped += 1
                self._send(sock, {"t": "input_dropped",
                                  "reason": "the page is not keeping up; this input was dropped"})
                return
            entry = (self._seq, self.gen, msg)
        if msg.takeover:
            self.owner_input()
        with self._lock:
            if sock is not self._sock or entry[1] != self.gen or entry[0] <= self._drop_through:
                return
            last = self._queue[-1] if self._queue else None
            merged = _merged(last[2], msg) if last is not None and last[1] == entry[1] else None
            if merged is not None:
                self._queue[-1] = (entry[0], entry[1], merged)
                self.merged += 1
            else:
                self._queue.append(entry)
        self.wake()

    def take(self):
        """The next queued input still deliverable, or None."""
        with self._lock:
            while self._queue:
                seq, gen, msg = self._queue.popleft()
                if gen == self.gen and seq > self._drop_through and not self.closed:
                    return seq, gen, msg
            return None

    def deliverable(self, seq: int, gen: int) -> bool:
        with self._lock:
            return gen == self.gen and seq > self._drop_through and not self.closed

    # -- the agent channel (amendment §B.9) -----------------------------------------------------

    def _agent_active(self, now: float) -> bool:
        if self.driver is None:
            return False
        if any(a.get("outcome") in ("running", "asking") for a in self.actions):
            return True
        return self.last_action_at is not None and now - self.last_action_at <= AGENT_LINGER_S

    def agent_active(self) -> bool:
        with self._lock:
            return self._agent_active(self._clock())

    def _paused_message(self) -> dict:
        return {"t": "agent", "kind": "paused", "paused": self.paused,
                "by": self.paused_by if self.paused else None}

    def _driver_message(self) -> dict:
        thread_id, provider = self.driver if self.driver else (None, None)
        return {"t": "agent", "kind": "driver", "thread_id": thread_id, "provider": provider}

    def _agent_replay(self) -> list[dict]:
        out = []
        if self.driver is not None:
            out.append(self._driver_message())
        out.append(self._paused_message())
        out.append({"t": "agent", "kind": "list", "actions": [dict(a) for a in self.actions]})
        return out

    def agent_driver(self, thread_id: str | None, provider: str | None = None) -> None:
        """BR-9b: who drives this tab (None when it lets go)."""
        with self._lock:
            self.driver = (thread_id, provider) if thread_id else None
            message = self._driver_message()
            self._send(self._sock, message)

    def agent_action(self, action: dict) -> None:
        """BR-9b: one action, sent when it starts, while it asks and when it
        ends (the same id each time). The target and text are cleaned and
        capped at 60 here, and only ever go to this tab's socket."""
        row = {"id": str(action.get("id", ""))[:64], "at": action.get("at"),
               "action": str(action.get("action", ""))[:16],
               "target": _clean(action.get("target", ""), AGENT_TEXT_CAP),
               "text": _clean(action.get("text", ""), AGENT_TEXT_CAP),
               "outcome": str(action.get("outcome", ""))[:16]}
        for name in ("page_identity", "document_generation"):
            value = action.get(name)
            if type(value) is int and value >= 0:
                row[name] = value
        if isinstance(action.get("rect"), dict) and row["outcome"] in ("running", "asking"):
            row["rect"] = {k: action["rect"].get(k) for k in ("x", "y", "width", "height")}
        with self._lock:
            for i, existing in enumerate(self.actions):
                if existing["id"] == row["id"]:
                    self.actions[i] = row
                    break
            else:
                self.actions.append(row)
            self.last_action_at = self._clock()
            self._send(self._sock, {"t": "agent", "kind": "action", **row})

    def pause(self, by: str) -> None:
        with self._lock:
            if self.paused:
                return
            self.paused, self.paused_by = True, by
            self._send(self._sock, self._paused_message())

    def resume(self) -> None:
        with self._lock:
            if not self.paused:
                return
            self.paused, self.paused_by = False, None
            self._send(self._sock, self._paused_message())

    def owner_state(self) -> dict:
        """What BR-9b's gate reads for H3/H4: paused, by whom, and how long
        since the owner's last input (None when never)."""
        now = self._clock()
        with self._lock:
            quiet = None if self.owner_input_at is None else now - self.owner_input_at
            return {"paused": self.paused, "by": self.paused_by, "owner_quiet_s": quiet,
                    "agent_active": self._agent_active(now), "shown": self._sock is not None,
                    "visible": self.visible, "input_serial": self.owner_input_serial,
                    "attachment_generation": self.gen, "hold_count": self.hold_count}


# -- a socket session (the HTTP handler's thread) -----------------------------------------------

def serve(stream: TabStream, sock, *, tab_row: dict, on_attached, on_detached, handle) -> None:
    """Attach `sock` to `stream` and read it until it closes. `handle(msg)`
    acts on each valid message (the host's), `on_attached` and
    `on_detached` start and stop the tab's screencast."""
    try:
        refused = stream.attach(sock, tab_row, TAKEOVER_TIMEOUT_S)
        if refused is not None:
            sock.send_text(json.dumps({"t": "refused", "reason": refused}))
            sock.close(ws.POLICY, "refused")
            return
        on_attached()
        while True:
            message = sock.receive()
            if message is None:
                break
            kind, data = message
            if not stream.holds(sock):
                break
            if kind == ws.BINARY:
                stream.send_to(sock, {"t": "bad", "reason": "only the daemon sends binary messages"})
                continue
            try:
                msg = parse(data)
            except Bad as exc:
                stream.send_to(sock, {"t": "bad", "reason": str(exc)})
                continue
            handle(sock, msg)
    except Exception as exc:  # noqa: BLE001 - a session problem never takes the daemon down
        LOG.warning("browser tab %s session failed (%s)", stream.tab_id, type(exc).__name__)
    finally:
        if stream.detach(sock):
            on_detached()
        sock.close()
        sock.wait_closed(ws.CLOSE_WAIT_S + 0.5)
        sock.abort()
