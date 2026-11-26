"""Paired loopback client. The only shared Windows/daemon code is the wire codec."""

from collections import deque
from copy import copy
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import secrets
import select
import socket
import threading
import time


_spec = importlib.util.spec_from_file_location(
    "v3voice_wire", Path(__file__).resolve().parents[1] / "jarvis" / "v3" / "voice" / "wire.py")
wire = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wire)

FRAME_SECONDS = 0.02
MAX_AUDIO = 50
MIN_BACKOFF = 0.5
MAX_BACKOFF = 10
QUICK_DISCONNECT_S = 5
STABLE_SESSION_S = 30
VERSION = "3"
WRONG_PEER = "That wasn't Jarvis"


class WrongPeer(Exception):
    pass


@dataclass(frozen=True)
class Card:
    id: str
    digest: str
    expires_at: float

    def accepts(self, ident, digest):
        return (ident == self.id and digest == self.digest
                and time.monotonic() < self.expires_at)


class _Reader:
    """Apply one absolute deadline to a complete line or frame."""

    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline
        self.buffer = bytearray()

    def _fill(self, size):
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.sock], [], [], remaining)[0]:
                raise TimeoutError()
            try:
                data = self.sock.recv(min(size, 8192))
            except BlockingIOError:
                continue
            if not data:
                raise EOFError()
            self.buffer.extend(data)
            return

    def _take(self, size):
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def readline(self, size):
        while True:
            newline = self.buffer.find(b"\n", 0, size)
            if newline >= 0:
                return self._take(newline + 1)
            if len(self.buffer) >= size:
                return self._take(size)
            self._fill(size - len(self.buffer))

    def read(self, size):
        while len(self.buffer) < size:
            self._fill(size - len(self.buffer))
        return self._take(size)


class _Outbox:
    """One producer queue per connection. Its caller holds the client lock."""

    def __init__(self):
        self.items = deque()
        self.audio = self.dropped = 0

    def put(self, kind, payload):
        if kind == wire.AUDIO:
            if self.audio == MAX_AUDIO:
                for index, (queued, _) in enumerate(self.items):
                    if queued == wire.AUDIO:
                        del self.items[index]
                        self.dropped += 1
                        break
            else:
                self.audio += 1
        self.items.append((kind, payload))

    def pop(self):
        kind, payload = self.items.popleft()
        self.audio -= kind == wire.AUDIO
        return kind, payload

    def discard_clicks(self):
        self.items = deque(item for item in self.items if item[0] != wire.CLICK)


def _interrupt(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _send(sock, data, deadline):
    pending = memoryview(data)
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([], [sock], [], remaining)[1]:
            raise TimeoutError()
        try:
            sent = sock.send(pending)
        except BlockingIOError:
            continue
        if not sent:
            raise EOFError()
        pending = pending[sent:]


class Client:
    def __init__(self, host, port, key_path, *, canceller, on_connect, on_disconnect,
                 on_event, on_audio, on_state):
        if host != "127.0.0.1":
            raise ValueError("Voice requires the loopback address.")
        self._address = (host, port)
        with Path(key_path).open("rb") as stream:
            self._key = wire.derive_k3(wire.parse_key(stream.read(66)))
        self._hello = wire.encode_payload(wire.HELLO, {
            "features": ["audio", "playback", "talk_key", "popup", "level"],
            "canceller": bool(canceller), "version": VERSION,
        })
        self._connect, self._disconnect = on_connect, on_disconnect
        self._event, self._audio, self._state = on_event, on_audio, on_state
        self._changed = threading.Condition()
        self._stop = threading.Event()
        self._sock = self._outbox = None
        self._card = None
        self._live = 0.0
        self._pings = {}

    def close(self):
        self._stop.set()
        with self._changed:
            if self._sock is not None:
                _interrupt(self._sock)
            self._changed.notify_all()

    def send(self, kind, value):
        with self._changed:
            if self._outbox is None or self._stop.is_set():
                return False
            payload = wire.encode_payload(kind, value)
            if kind == wire.CLICK and (
                    self._card is None or not self._card.accepts(value["id"], value["digest"])):
                return False
            self._outbox.put(kind, payload)
            self._changed.notify()
            return True

    def set_card(self, card):
        with self._changed:
            self._card = card
            if self._outbox is not None:
                self._outbox.discard_clicks()
                self._changed.notify()

    def send_audio(self, raw, cancelled):
        with self._changed:
            if self._outbox is None or self._stop.is_set():
                return
            self._outbox.put(wire.AUDIO, wire.encode_payload(
                wire.AUDIO, (raw.astype("<i2", copy=False).tobytes(),
                             None if cancelled is None else cancelled.astype("<i2", copy=False).tobytes())))
            self._changed.notify()

    def _handshake(self, sock, reader):
        nonce = wire.new_nonce()
        _send(sock, wire.json_line(wire.helper_hello(nonce)), reader.deadline)
        try:
            remote = wire.check_daemon_hello(self._key, nonce, wire.read_json(reader))
        except wire.ProtocolError:
            raise WrongPeer() from None
        _send(sock, wire.json_line(wire.helper_proof(self._key, nonce, remote)), reader.deadline)
        return wire.derive_ks(self._key, nonce, remote)

    def _write_click(self, sock, frames, payload, card, outbox, deadline):
        if card is None:
            return frames
        # Reserve the sequence number only if this frame reaches the socket.
        candidate = copy(frames)
        data = candidate.encode(wire.CLICK, payload)
        limit = min(card.expires_at, deadline)
        while True:
            remaining = limit - time.monotonic()
            if remaining <= 0 or not select.select([], [sock], [], remaining)[1]:
                return frames
            with self._changed:
                if (self._outbox is not outbox or self._stop.is_set() or self._card is not card
                        or time.monotonic() >= limit):
                    return frames
                try:
                    sent = sock.send(data)
                except BlockingIOError:
                    continue
                if not sent:
                    raise EOFError()
                break
        # Once any bytes have been written, complete the frame for the peer.
        if sent < len(data):
            _send(sock, data[sent:], deadline)
        return candidate

    def _write(self, sock, frames, outbox):
        now = time.monotonic()
        ping_at, audio_at, level_at = now + wire.HEARTBEAT_S, now, now
        try:
            while True:
                with self._changed:
                    while self._outbox is outbox and not self._stop.is_set():
                        now = time.monotonic()
                        if now >= self._live:
                            return
                        if now >= ping_at:
                            kind, payload = wire.PING, secrets.token_bytes(8)
                            self._pings = {token: expiry for token, expiry in self._pings.items() if expiry > now}
                            self._pings[payload] = now + wire.LIVENESS_S
                            ping_at = now + wire.HEARTBEAT_S
                            break
                        wake = min(ping_at, self._live)
                        if outbox.items:
                            kind = outbox.items[0][0]
                            due = {wire.AUDIO: audio_at, wire.LEVEL: level_at}.get(kind, now)
                            if now >= due:
                                kind, payload = outbox.pop()
                                break
                            wake = min(wake, due)
                        self._changed.wait(max(0, wake - now))
                    else:
                        return
                    deadline = self._live
                    card = self._card
                if kind == wire.CLICK:
                    frames = self._write_click(sock, frames, payload, card, outbox, deadline)
                else:
                    _send(sock, frames.encode(kind, payload), deadline)
                if kind == wire.AUDIO:
                    # Pace by schedule, not by sleeping one frame at a time: Windows timers are coarse
                    # (a 20 ms wait takes about 31 ms), which sent 32 frames a second and dropped the rest
                    # (measured on real Windows). Catch up by at most 200 ms.
                    audio_at = max(audio_at + FRAME_SECONDS, now - 0.2)
                elif kind == wire.LEVEL:
                    level_at = time.monotonic() + 0.105
        except (OSError, EOFError, wire.ProtocolError):
            pass
        finally:
            _interrupt(sock)

    def _read(self, reader, frames):
        while not self._stop.is_set():
            kind, value = frames.read(reader)
            now = time.monotonic()
            if now >= reader.deadline:
                raise TimeoutError()
            reader.deadline = now + wire.LIVENESS_S
            with self._changed:
                self._live = reader.deadline
                if kind == wire.PONG and self._pings.pop(value, None) is None:
                    raise wire.ProtocolError("Unexpected voice link PONG.")
                self._changed.notify()
            if kind == wire.EVENT:
                # The frozen codec supports extension fields on notice events.
                # Translate the exact reserved envelope after frame authentication.
                if value == {"kind": "notice", "title": "", "text": "", "helper_action": "quit"}:
                    value = {"kind": "quit"}
                self._event(value)
            elif kind == wire.AUDIO_OUT:
                self._audio(*value)

    def _session(self, sock, deadline):
        reader = _Reader(sock, deadline)
        ks = self._handshake(sock, reader)
        authenticated_at = time.monotonic()
        writer = wire.FrameWriter(ks, wire.UP)
        _send(sock, writer.encode(wire.HELLO, self._hello), deadline)
        outbox = _Outbox()
        with self._changed:
            if self._stop.is_set():
                return 0
            self._outbox = outbox
            self._live = reader.deadline = time.monotonic() + wire.LIVENESS_S
            self._pings.clear()
        thread = threading.Thread(target=self._write, args=(sock, writer, outbox), daemon=True)
        thread.start()
        try:
            self._connect()
            self._read(reader, wire.FrameReader(ks, wire.DOWN))
        except wire.ProtocolError:
            self._state("Voice link rejected a frame.")
        except (OSError, EOFError):
            pass
        finally:
            duration = time.monotonic() - authenticated_at
            with self._changed:
                self._outbox = None
                self._card = None
                self._changed.notify_all()
            _interrupt(sock)
            thread.join()
            self._disconnect(outbox.dropped)
        return duration

    def run(self):
        delay = MIN_BACKOFF
        while not self._stop.is_set():
            duration = 0
            deadline = time.monotonic() + wire.HANDSHAKE_TIMEOUT_S
            try:
                with socket.create_connection(self._address, wire.HANDSHAKE_TIMEOUT_S) as sock:
                    with self._changed:
                        self._sock = sock
                        if self._stop.is_set():
                            return
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    sock.setblocking(False)
                    duration = self._session(sock, deadline)
            except WrongPeer:
                self._state(WRONG_PEER)
                return
            except (OSError, EOFError):
                pass
            finally:
                with self._changed:
                    self._sock = None
            if duration >= STABLE_SESSION_S:
                delay = MIN_BACKOFF
            if self._stop.wait(delay):
                return
            if duration <= QUICK_DISCONNECT_S:
                delay = min(2 * delay, MAX_BACKOFF)
