"""A small RFC 6455 WebSocket server endpoint, hand-rolled (HUD plan §2.4, decision 14).

Server side only, and only what the HUD's terminal needs: no extensions (so
no compression), no subprotocols, client frames must be masked, a message is
at most 64 KiB, and the server pings every 20 s and drops a socket that has
said nothing for 60 s. `websocket-client`, already a dependency, is the test
client; nothing here is a client.

It rides the daemon's stdlib `BaseHTTPRequestHandler`: the route validates
the request (Origin, Host, ticket — that is `terminals.py`'s job, not this
file's), then `upgrade()` writes the `101` itself, because the handler speaks
HTTP/1.0 and a browser refuses `HTTP/1.0 101`. From then on the handler's
thread is this socket's reader, and one writer thread per socket sends what
other threads queue, so a terminal's output never waits on a browser: a
socket that falls more than `MAX_PENDING` bytes behind is dropped, and the
window reattaches and replays the terminal's ring.

Nothing in here logs a payload. The only log lines are why a socket closed.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import queue
import select
import socket
import struct
import threading
import time

LOG = logging.getLogger(__name__)

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 64 * 1024          # the HUD sends pastes in 16 KiB chunks
MAX_PENDING = 4 * 1024 * 1024    # queued to one browser before it is dropped
PING_INTERVAL_S = 20.0
PONG_TIMEOUT_S = 60.0
TICK_S = 0.25
CLOSE_WAIT_S = 2.0

TEXT, BINARY, CLOSE, PING, PONG, CONTINUATION = 0x1, 0x2, 0x8, 0x9, 0xA, 0x0
_DATA = (TEXT, BINARY)
_CONTROL = (CLOSE, PING, PONG)

# Close codes (RFC 6455 §7.4.1).
NORMAL, GOING_AWAY, PROTOCOL_ERROR, BAD_DATA, POLICY, TOO_BIG, TRY_AGAIN = (
    1000, 1001, 1002, 1007, 1008, 1009, 1013)


class HandshakeError(Exception):
    """A request that is not a valid WebSocket opening handshake (HTTP 400)."""


def _tokens(value: str) -> set[str]:
    return {t.strip().lower() for t in (value or "").split(",") if t.strip()}


def handshake_key(headers) -> str:
    """The client's `Sec-WebSocket-Key`, once the request is a well-formed
    version-13 upgrade; HandshakeError otherwise. Extensions and
    subprotocols the client offers are ignored, so none is ever agreed."""
    if "websocket" not in _tokens(headers.get("Upgrade", "")):
        raise HandshakeError("expected a WebSocket upgrade")
    if "upgrade" not in _tokens(headers.get("Connection", "")):
        raise HandshakeError("expected Connection: Upgrade")
    if headers.get("Sec-WebSocket-Version", "").strip() != "13":
        raise HandshakeError("only WebSocket version 13 is supported")
    key = headers.get("Sec-WebSocket-Key", "").strip()
    try:
        if len(base64.b64decode(key, validate=True)) != 16:
            raise ValueError
    except ValueError:
        raise HandshakeError("invalid Sec-WebSocket-Key") from None
    return key


def accept_value(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def upgrade(handler, key: str, **options) -> "WebSocket":
    """Answer `101 Switching Protocols` on a request already validated by the
    route, and hand the connection to a WebSocket.

    The status line is written by hand: `send_response` would say HTTP/1.0,
    which browsers reject. The handler's 2-second socket timeout (`setup`) is
    lifted here — the socket goes non-blocking, and this module waits with
    `select` from now on — and any bytes the handler's buffered reader has
    already pulled off the socket are carried over, so a client that sends a
    frame hard on the heels of its handshake loses nothing.
    """
    handler.wfile.write(
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + accept_value(key).encode() + b"\r\n\r\n")
    handler.wfile.flush()
    handler._streaming = True
    handler.close_connection = True
    sock = handler.connection
    sock.settimeout(0.0)
    carried = b""
    try:
        carried = handler.rfile.peek(MAX_MESSAGE) or b""     # buffered only: never blocks
        carried = handler.rfile.read(len(carried)) if carried else b""
    except (BlockingIOError, OSError, ValueError):
        carried = b""
    ws = WebSocket(sock, initial=carried, **options)
    ws.start()
    return ws


def encode_frame(opcode: int, payload: bytes = b"") -> bytes:
    """A server frame: final, unmasked (servers never mask)."""
    head = bytes([0x80 | opcode])
    size = len(payload)
    if size < 126:
        head += bytes([size])
    elif size < 1 << 16:
        head += bytes([126]) + struct.pack("!H", size)
    else:
        head += bytes([127]) + struct.pack("!Q", size)
    return head + payload


def _unmask(data: bytes, mask: bytes) -> bytes:
    """XOR with the repeating 4-byte key, as one big-integer operation
    rather than a Python loop per byte (64 KiB is a paste)."""
    if not data:
        return b""
    size = len(data)
    key = (mask * (size // 4 + 1))[:size]
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(size, "big")


class _ProtocolError(Exception):
    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


class WebSocket:
    """One server-side connection. `receive()` runs on the caller's thread
    (the HTTP handler's); `send_*` and `close` may be called from any thread
    and never block on the network."""

    def __init__(self, sock: socket.socket, *, initial: bytes = b"",
                 ping_interval: float | None = None, pong_timeout: float | None = None,
                 max_pending: int | None = None, max_message: int | None = None,
                 clock=time.monotonic):
        self._sock = sock
        self._buffer = bytearray(initial)
        self.ping_interval = PING_INTERVAL_S if ping_interval is None else ping_interval
        self.pong_timeout = PONG_TIMEOUT_S if pong_timeout is None else pong_timeout
        self.max_pending = MAX_PENDING if max_pending is None else max_pending
        self.max_message = MAX_MESSAGE if max_message is None else max_message
        self._clock = clock
        self._queue: queue.Queue = queue.Queue()
        self._pending = 0
        self._pending_lock = threading.Lock()
        self._heard = clock()
        self._pinged = clock()
        self.closed = threading.Event()        # no more frames will be read or written
        self._close_queued = False
        self._close_sent_at: float | None = None
        self._peer_closed = False
        self.close_code: int | None = None
        self.close_reason = ""
        self._writer = threading.Thread(target=self._write_loop, name="jarvis-ws-writer", daemon=True)

    def start(self) -> None:
        self._writer.start()

    # -- sending (any thread) -------------------------------------------------

    def send_text(self, text: str) -> bool:
        return self._enqueue(TEXT, text.encode("utf-8"))

    def send_binary(self, data: bytes) -> bool:
        return self._enqueue(BINARY, bytes(data))

    def _enqueue(self, opcode: int, payload: bytes) -> bool:
        """Queue a data frame. False when the socket is closing, or when it is
        so far behind that it is dropped here (the window reattaches)."""
        if self.closed.is_set() or self._close_queued:
            return False
        with self._pending_lock:
            if self._pending + len(payload) > self.max_pending:
                slow = True
            else:
                slow = False
                self._pending += len(payload)
        if slow:
            LOG.info("WebSocket dropped: the browser fell too far behind")
            self.abort()
            return False
        self._queue.put((opcode, payload))
        return True

    def close(self, code: int = NORMAL, reason: str = "") -> None:
        """Send a close frame after what is already queued, then drop the
        socket once the browser answers or CLOSE_WAIT_S passes."""
        if self.closed.is_set() or self._close_queued:
            return
        self._close_queued = True
        self.close_code, self.close_reason = code, reason
        self._queue.put((CLOSE, struct.pack("!H", code) + reason.encode("utf-8")[:120]))

    def abort(self) -> None:
        """Drop the connection now, unblocking both threads."""
        if self.closed.is_set():
            return
        self.closed.set()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._queue.put(None)

    def wait_closed(self, timeout: float | None = None) -> bool:
        return self.closed.wait(timeout)

    # -- the writer thread ------------------------------------------------------

    def _write_loop(self) -> None:
        try:
            while not self.closed.is_set():
                try:
                    item = self._queue.get(timeout=TICK_S)
                except queue.Empty:
                    item = None
                self._timers()
                if item is None or self.closed.is_set():
                    continue
                opcode, payload = item
                self._send_all(encode_frame(opcode, payload))
                if opcode in _DATA:
                    with self._pending_lock:
                        self._pending -= len(payload)
                if opcode == CLOSE:
                    self._close_sent_at = self._clock()
                    if self._peer_closed:
                        self.abort()
        except OSError:
            self.abort()

    def _timers(self) -> None:
        now = self._clock()
        if self._close_sent_at is not None and now - self._close_sent_at > CLOSE_WAIT_S:
            self.abort()
            return
        if now - self._heard > self.pong_timeout:
            LOG.info("WebSocket dropped: no pong for %ds", int(self.pong_timeout))
            self.abort()
            return
        if not self._close_queued and now - self._pinged >= self.ping_interval:
            self._pinged = now
            self._send_all(encode_frame(PING, b"jarvis"))

    def _send_all(self, data: bytes) -> None:
        view = memoryview(data)
        while view and not self.closed.is_set():
            try:
                sent = self._sock.send(view)
            except (BlockingIOError, InterruptedError):
                sent = 0
            if sent:
                view = view[sent:]
                continue
            # The browser is not reading. Keep checking whether it has gone
            # silent for good rather than block on it.
            select.select([], [self._sock], [], TICK_S)
            if self._clock() - self._heard > self.pong_timeout:
                raise OSError("peer stopped reading")

    # -- reading (the handler's thread) -------------------------------------------

    def _fill(self, need: int) -> bool:
        """Read until the buffer holds `need` bytes. False on EOF or close."""
        while len(self._buffer) < need:
            if self.closed.is_set():
                return False
            try:
                ready, _, _ = select.select([self._sock], [], [], TICK_S)
            except (OSError, ValueError):
                return False
            if not ready:
                continue
            try:
                chunk = self._sock.recv(65536)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                return False
            if not chunk:
                return False
            self._buffer += chunk
        return True

    def _take(self, n: int) -> bytes:
        data = bytes(self._buffer[:n])
        del self._buffer[:n]
        return data

    def _frame(self):
        """One frame as (fin, opcode, payload); None on EOF."""
        if not self._fill(2):
            return None
        b0, b1 = self._buffer[0], self._buffer[1]
        fin, rsv, opcode = b0 & 0x80, b0 & 0x70, b0 & 0x0F
        masked, size = b1 & 0x80, b1 & 0x7F
        if rsv:
            raise _ProtocolError(PROTOCOL_ERROR, "no extension was negotiated")
        if not masked:
            raise _ProtocolError(PROTOCOL_ERROR, "client frames must be masked")
        if opcode not in _DATA + _CONTROL + (CONTINUATION,):
            raise _ProtocolError(PROTOCOL_ERROR, "unknown opcode")
        if opcode in _CONTROL and (not fin or size > 125):
            raise _ProtocolError(PROTOCOL_ERROR, "bad control frame")
        header = 2
        if size == 126:
            if not self._fill(4):
                return None
            size = struct.unpack("!H", bytes(self._buffer[2:4]))[0]
            header = 4
        elif size == 127:
            if not self._fill(10):
                return None
            size = struct.unpack("!Q", bytes(self._buffer[2:10]))[0]
            if size >> 63:
                raise _ProtocolError(PROTOCOL_ERROR, "invalid length")
            header = 10
        if size > self.max_message:
            raise _ProtocolError(TOO_BIG, "message too big")
        if not self._fill(header + 4 + size):
            return None
        self._take(header)
        mask = self._take(4)
        payload = _unmask(self._take(size), mask)
        self._heard = self._clock()
        return bool(fin), opcode, payload

    def receive(self):
        """The next message as (TEXT | BINARY, bytes), or None once the
        connection has closed. Pings, pongs and the close handshake are
        answered here; a protocol violation closes with its code."""
        parts: list[bytes] = []
        kind = None
        size = 0
        try:
            while True:
                frame = self._frame()
                if frame is None:
                    self.abort()
                    return None
                fin, opcode, payload = frame
                if opcode == PING:
                    self._queue.put((PONG, payload))
                    continue
                if opcode == PONG:
                    continue
                if opcode == CLOSE:
                    self._peer_closed = True
                    if len(payload) == 1:
                        raise _ProtocolError(PROTOCOL_ERROR, "bad close frame")
                    if not self._close_queued:
                        self.close(struct.unpack("!H", payload[:2])[0] if payload else NORMAL)
                    elif self._close_sent_at is not None:
                        self.abort()
                    return None
                if opcode == CONTINUATION:
                    if kind is None:
                        raise _ProtocolError(PROTOCOL_ERROR, "continuation without a message")
                elif kind is not None:
                    raise _ProtocolError(PROTOCOL_ERROR, "new message inside a fragmented one")
                else:
                    kind = opcode
                size += len(payload)
                if size > self.max_message:
                    raise _ProtocolError(TOO_BIG, "message too big")
                parts.append(payload)
                if not fin:
                    continue
                message = b"".join(parts)
                if kind == TEXT:
                    try:
                        message.decode("utf-8")
                    except UnicodeDecodeError:
                        raise _ProtocolError(BAD_DATA, "text is not UTF-8") from None
                return kind, message
        except _ProtocolError as exc:
            LOG.info("WebSocket closed: %s", exc.reason)
            self._peer_closed = True
            self.close(exc.code, exc.reason)
            return None
