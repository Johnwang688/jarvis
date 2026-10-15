"""The owner's HUD terminals (WP-C; HUD plan §2.4, decisions W-2, W-3, W-5).

Free and hermetic. Every daemon here listens on three ephemeral loopback
ports — never 8402, 8403 or 8405, which the owner's daemon holds (8403
appears only as an Origin *string* a socket presents). `JARVIS_TERMINAL_SHELL`
points at `terminal_fake/sh`, a scripted stand-in, so no test starts
the owner's login shell; the one case that runs real bash (to prove the
shipped startup file's marks) runs it with a temp HOME. Every config path
and HOME are temp. The environment checks compare variable *names* and
never print a value; the planted "secrets" are placeholder strings.

Written to fail against `main` before WP-C: `/terminals` did not exist (404
where these expect 403/201/101), and `jarvis.v2.terminals`/`ws` did not exist.
"""
from __future__ import annotations

import base64
import fcntl
import http.client
import inspect
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import struct
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import websocket  # noqa: E402  (websocket-client, already a dependency)

from jarvis import config  # noqa: E402
from jarvis.v2 import devurl, terminals as T, ws as W  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.model import ProviderName as PN  # noqa: E402
from jarvis.v2.provider import Decision, Event, EventKind as K, SessionHandle, Usage  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402
from tests import loadclock  # noqa: E402

FAKE_SHELL = Path(__file__).resolve().parent / "terminal_fake" / "sh"
LIVE_PORTS = {8402, 8403, 8405}
# Any OSC sequence: the marks, and whatever else a system's own startup files
# print (Ubuntu's systemd context marks, OSC 3008, for one).
OSC = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def eventually(predicate, timeout=6.0, what="condition"):
    until = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > until:
            raise AssertionError(f"{what} did not settle")
        time.sleep(0.02)


def dead(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            stat = handle.read()
    except OSError:
        return True
    return stat[stat.rfind(b")") + 2:].split()[0] == b"Z"


def plain(data: bytes) -> str:
    """Output without the marks, as text, with the PTY's CRLF read as LF."""
    return OSC.sub(b"", data).decode("utf-8", "replace").replace("\r\n", "\n")


class Fake:
    def __init__(self, name):
        self.name = name

    def health(self):
        return True, "fake"

    def start(self, thread, brief, permit):
        return SessionHandle(thread.id, self.name, "fake:" + thread.id, SimpleNamespace(brief=brief))

    resume = start

    def usage(self, handle):
        return Usage()

    def send(self, handle, message):
        yield Event(K.TEXT, handle.thread_id, {"text": "ok"})
        yield Event(K.TURN_FINISHED, handle.thread_id, {"stop": "end"})

    def interrupt(self, handle):
        pass

    def close(self, handle):
        pass


class Client:
    """A websocket-client connection that keeps every byte and message."""

    def __init__(self, conn):
        self.conn = conn
        self.output = bytearray()
        self.messages: list[dict] = []
        self.closed = None
        conn.settimeout(0.1)

    def pump(self):
        try:
            opcode, data = self.conn.recv_data()
        except websocket.WebSocketTimeoutException:
            return
        except (websocket.WebSocketConnectionClosedException, OSError):
            self.closed = self.closed or "dropped"
            return
        if opcode == websocket.ABNF.OPCODE_BINARY:
            self.output += data
        elif opcode == websocket.ABNF.OPCODE_TEXT:
            self.messages.append(json.loads(data))
        elif opcode == websocket.ABNF.OPCODE_CLOSE:
            self.closed = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 1005

    def until(self, predicate, timeout=6.0, what="client condition"):
        def step():
            if self.closed is None:
                self.pump()
            return predicate(self)
        return eventually(step, timeout, what)

    def text(self) -> str:
        return plain(bytes(self.output))

    def kinds(self):
        return [m.get("type") for m in self.messages]

    def message(self, kind):
        return next((m for m in self.messages if m.get("type") == kind), None)

    def type(self, line: str):
        self.conn.send_binary((line + "\r").encode())

    def control(self, payload: dict):
        self.conn.send(json.dumps(payload))

    def wait_text(self, needle: str, timeout=6.0):
        return self.until(lambda c: needle in c.text(), timeout, f"output {needle!r}")

    def wait_line(self, line: str, timeout=6.0):
        """`line` as a whole line of output. `echo x` echoes `echo x` first,
        so waiting for "x\n" alone is satisfied before the command has run."""
        return self.wait_text("\n" + line + "\n", timeout)

    def drain(self, seconds: float):
        """Keep reading (and so answering pings) for a while."""
        until = time.monotonic() + seconds
        while time.monotonic() < until and self.closed is None:
            self.pump()

    def close(self):
        try:
            self.conn.close(timeout=1)
        except Exception:
            pass


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / ".hushlogin").write_text("")
        env = patch.dict(os.environ, {"HOME": str(self.home), "XDG_DATA_HOME": str(self.root / "xdg"),
                                      "TMPDIR": str(self.root),
                                      "JARVIS_CLAUDE_CREDENTIALS": str(self.root / "none.json")})
        env.start()
        self.addCleanup(env.stop)
        tempfile.tempdir = None
        self.addCleanup(setattr, tempfile, "tempdir", None)
        for name, value in dict(V2_DATA_DIR=self.root / "data", ALLOWLIST_PATH=self.root / "allow.json",
                                MODELS_PATH=self.root / "models.json",
                                PROVIDER_DEFAULTS_PATH=self.root / "provider_defaults.json",
                                DISCORD_GUILD_PATH=self.root / "guild.json",
                                BROWSER_SETTINGS_PATH=self.root / "browser.json",
                                TERMINAL_SHELL=str(FAKE_SHELL)).items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        self.work = self.root / "work"
        self.work.mkdir()
        self.stores = Stores()
        self.daemon = Daemon(self.stores, {name: Fake(name) for name in PN},
                             lambda *_: lambda *_: Decision.DENY, 0)
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        ports = {self.daemon.port, self.daemon.face_port, self.daemon.workshop_port}
        self.assertFalse(ports & LIVE_PORTS, "a test daemon must never hold a live port")
        self.project = self.stores.projects.create("Calc", str(self.work))
        self.clients: list[Client] = []
        self.addCleanup(self._close_clients)

    def _close_clients(self):
        for client in self.clients:
            client.close()

    # -- HTTP -------------------------------------------------------------------------

    def request(self, method, path, body=None, *, port=None, origin="hud", host=None, status=None,
                content_type="application/json"):
        port = port or self.daemon.face_port
        headers = {}
        if body is not None and content_type:
            headers["Content-Type"] = content_type
        if origin == "hud":
            headers["Origin"] = f"http://127.0.0.1:{port}"
        elif origin is not None:
            headers["Origin"] = origin
        if host is not None:
            headers["Host"] = host
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
            response = conn.getresponse()
            content = response.read()
        finally:
            conn.close()
        if status is not None:
            self.assertEqual(response.status, status, content[:300])
        return response.status, (json.loads(content) if content else None)

    def owner(self, method, path, body=None, status=200):
        if body is None and method in ("POST", "PATCH"):
            body = {}
        return self.request(method, path, body, status=status)[1]

    def open_terminal(self, spec=None, **extra):
        body = {"in": spec if spec is not None else {"project": self.project.id}, **extra}
        return self.owner("POST", "/terminals", body, status=201)

    def ticket(self, tid):
        return self.owner("POST", f"/terminals/{tid}/ticket")["ticket"]

    # -- WebSocket ------------------------------------------------------------------

    def url(self, tid, ticket, port=None, window=None):
        port = port or self.daemon.face_port
        query = f"?ticket={ticket}" if ticket is not None else ""
        if window is not None:
            query += ("&" if query else "?") + "window=" + window
        return f"ws://127.0.0.1:{port}/terminals/{tid}/attach{query}"

    def connect(self, tid, ticket="fresh", *, port=None, origin="hud", host=None, window=None):
        if ticket == "fresh":
            ticket = self.ticket(tid)
        options = {}
        port = port or self.daemon.face_port
        if origin == "hud":
            options["origin"] = f"http://127.0.0.1:{self.daemon.face_port}"
        elif origin is None:
            options["suppress_origin"] = True
        else:
            options["origin"] = origin
        if host is not None:
            options["host"] = host
        conn = websocket.create_connection(self.url(tid, ticket, port, window), timeout=5, **options)
        client = Client(conn)
        self.clients.append(client)
        return client

    def refused(self, tid, ticket="fresh", **kw):
        with self.assertRaises(websocket.WebSocketBadStatusException) as caught:
            self.connect(tid, ticket, **kw)
        return caught.exception.status_code

    def session(self, tid=None):
        """A terminal (made if not given) and an attached client at its prompt."""
        tid = tid or self.open_terminal()["id"]
        client = self.connect(tid)
        client.until(lambda c: "replayed" in c.kinds(), what="replay")
        client.wait_text("fake$ ")
        return tid, client

    def term(self, tid):
        return self.daemon.terminals.get(tid)


# -- who can connect -----------------------------------------------------------------------

class Handshake(Base):
    def test_the_hud_attaches_and_the_status_line_is_http_1_1(self):
        tid = self.open_terminal()["id"]
        sock = raw_connect(self.daemon.face_port, f"/terminals/{tid}/attach?ticket={self.ticket(tid)}",
                           origin=f"http://127.0.0.1:{self.daemon.face_port}")
        self.addCleanup(sock.close)
        status, headers = read_response(sock)
        self.assertEqual(status, b"HTTP/1.1 101 Switching Protocols")
        self.assertIn(b"sec-websocket-accept: " + W.accept_value(sock.key).encode().lower(),
                      headers.lower())
        self.assertNotIn(b"sec-websocket-extensions", headers.lower())   # no compression agreed

    def test_every_origin_but_the_huds_is_refused(self):
        tid = self.open_terminal()["id"]
        face = self.daemon.face_port
        for origin in (None, "null", "http://evil.example", f"http://127.0.0.1:{self.daemon.workshop_port}",
                       "http://127.0.0.1:8403", f"https://127.0.0.1:{face}", f"http://127.0.0.1:{face}.evil",
                       "http://localhost:5173"):
            with self.subTest(origin=origin):
                self.assertEqual(self.refused(tid, origin=origin), 403)

    def test_a_cross_site_fetch_is_refused_even_with_the_right_origin(self):
        tid = self.open_terminal()["id"]
        with self.assertRaises(websocket.WebSocketBadStatusException) as caught:
            websocket.create_connection(self.url(tid, self.ticket(tid)), timeout=5,
                                        origin=f"http://127.0.0.1:{self.daemon.face_port}",
                                        header=["Sec-Fetch-Site: cross-site"])
        self.assertEqual(caught.exception.status_code, 403)

    def test_a_refused_origin_does_not_spend_the_ticket(self):
        tid = self.open_terminal()["id"]
        ticket = self.ticket(tid)
        self.assertEqual(self.refused(tid, ticket, origin="http://evil.example"), 403)
        client = self.connect(tid, ticket)
        client.until(lambda c: "attached" in c.kinds(), what="attach")

    def test_dns_rebinding_host_is_refused(self):
        tid = self.open_terminal()["id"]
        face = self.daemon.face_port
        for host in (f"evil.example:{face}", f"127.0.0.1.nip.io:{face}", "127.0.0.1", f"127.0.0.1:{face + 1}"):
            with self.subTest(host=host):
                self.assertEqual(self.refused(tid, host=host, origin=f"http://{host}"), 403)

    def test_the_api_and_preview_listeners_never_serve_a_terminal(self):
        tid = self.open_terminal()["id"]
        api, preview = self.daemon.port, self.daemon.workshop_port
        self.assertEqual(self.refused(tid, port=api, origin=f"http://127.0.0.1:{api}"), 403)
        self.assertEqual(self.refused(tid, port=api), 403)
        self.assertEqual(self.refused(tid, port=preview, origin=f"http://127.0.0.1:{preview}"), 404)
        # Every route, not just the socket: the API listener is every tool's client.
        for method, path, body in (("GET", "/terminals", None),
                                   ("POST", "/terminals", {"in": "home"}),
                                   ("POST", f"/terminals/{tid}/ticket", {}),
                                   ("PATCH", f"/terminals/{tid}", {"readable": False}),
                                   ("DELETE", f"/terminals/{tid}", None)):
            with self.subTest(method=method, path=path):
                self.request(method, path, body, port=api, status=403)
                self.request(method, path, body, port=api, origin=f"http://127.0.0.1:{api}", status=403)
                self.request(method, path, body, origin=None, status=403)       # the HUD port, no Origin
                self.request(method, path, body, origin="null", status=403)
        self.assertTrue(self.term(tid).readable)
        self.assertEqual(len(self.owner("GET", "/terminals")), 1)

    def test_tickets_are_single_use_short_lived_and_bound_to_one_terminal(self):
        first = self.open_terminal()["id"]
        second = self.open_terminal()["id"]
        self.assertEqual(self.refused(first, None), 403)                          # missing
        self.assertEqual(self.refused(first, ""), 403)
        self.assertEqual(self.refused(first, "not-a-ticket"), 403)
        self.assertEqual(self.refused(first, self.ticket(second)), 403)          # another terminal's
        used = self.ticket(first)
        client = self.connect(first, used)
        client.until(lambda c: "attached" in c.kinds(), what="attach")
        client.close()
        self.assertEqual(self.refused(first, used), 403)                          # reused
        wrong = self.ticket(second)
        self.assertEqual(self.refused(first, wrong), 403)
        self.assertEqual(self.refused(second, wrong), 403)                        # presented once: spent
        self.assertEqual(T.TICKET_TTL_S, 30.0)
        with patch.object(T, "TICKET_TTL_S", 0.2):
            stale = self.ticket(first)
        time.sleep(0.4)
        self.assertEqual(self.refused(first, stale), 403)                         # expired

    def test_a_plain_get_is_not_an_upgrade(self):
        tid = self.open_terminal()["id"]
        status, body = self.request("GET", f"/terminals/{tid}/attach?ticket={self.ticket(tid)}")
        self.assertEqual(status, 400, body)

    def test_the_ticket_never_reaches_the_daemon_log(self):
        tid = self.open_terminal()["id"]
        ticket = self.ticket(tid)
        with capture_logs() as records:
            self.refused(tid, ticket, origin="http://evil.example")
            self.refused(tid, "made-up-ticket-value")
        text = "\n".join(records)
        self.assertIn("/terminals/", text)                                         # the refusal was logged
        self.assertNotIn(ticket, text)
        self.assertNotIn("made-up-ticket-value", text)


# -- frames ------------------------------------------------------------------------------------

class Raw:
    """A bare TCP client that speaks the handshake and frames by hand, so a
    test can send what no library would (unmasked, oversize, reserved bits)."""

    def __init__(self, port, path, *, origin, host=None):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.pending = b""
        request = (f"GET {path} HTTP/1.1\r\nHost: {host or f'127.0.0.1:{port}'}\r\n"
                   f"Upgrade: websocket\r\nConnection: keep-alive, Upgrade\r\n"
                   f"Sec-WebSocket-Key: {self.key}\r\nSec-WebSocket-Version: 13\r\n"
                   f"Sec-WebSocket-Extensions: permessage-deflate\r\nOrigin: {origin}\r\n\r\n")
        self.sock.sendall(request.encode())

    def sendall(self, data):
        self.sock.sendall(data)

    def recv(self, n):
        return self.sock.recv(n)

    def settimeout(self, value):
        self.sock.settimeout(value)

    def close(self):
        self.sock.close()


def raw_connect(port, path, *, origin, host=None):
    return Raw(port, path, origin=origin, host=host)


def read_response(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    sock.pending = rest
    status, _, headers = head.partition(b"\r\n")
    return status, headers


def frame(opcode, payload=b"", *, fin=True, mask=True, rsv=0, length=None):
    head = bytes([(0x80 if fin else 0) | rsv | opcode])
    size = len(payload) if length is None else length
    bit = 0x80 if mask else 0
    if size < 126:
        head += bytes([bit | size])
    elif size < 1 << 16:
        head += bytes([bit | 126]) + struct.pack("!H", size)
    else:
        head += bytes([bit | 127]) + struct.pack("!Q", size)
    if not mask:
        return head + payload
    key = secrets.token_bytes(4)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def read_frame(sock, timeout=5.0):
    """(opcode, payload) of the next server frame, or None at EOF."""
    sock.settimeout(timeout)

    def need(n):
        while len(sock.pending) < n:
            chunk = sock.recv(65536)
            if not chunk:
                return None
            sock.pending += chunk
        out, sock.pending = sock.pending[:n], sock.pending[n:]
        return out

    head = need(2)
    if head is None:
        return None
    if head[1] & 0x80:
        raise AssertionError("a server frame must never be masked")
    size = head[1] & 0x7F
    if size == 126:
        size = struct.unpack("!H", need(2))[0]
    elif size == 127:
        size = struct.unpack("!Q", need(8))[0]
    return head[0] & 0x0F, need(size) if size else b""


def frames_until(sock, predicate, timeout=6.0):
    seen = []
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        try:
            got = read_frame(sock, timeout=max(0.05, until - time.monotonic()))
        except socket.timeout:
            break
        if got is None:
            seen.append(None)
            return seen
        seen.append(got)
        if predicate(got):
            return seen
    raise AssertionError(f"no matching frame in {[(f[0], f[1][:40]) if f else f for f in seen]}")


class Frames(Base):
    def attach_raw(self):
        tid = self.open_terminal()["id"]
        sock = raw_connect(self.daemon.face_port, f"/terminals/{tid}/attach?ticket={self.ticket(tid)}",
                           origin=f"http://127.0.0.1:{self.daemon.face_port}")
        self.addCleanup(sock.close)
        status, _ = read_response(sock)
        self.assertEqual(status, b"HTTP/1.1 101 Switching Protocols")
        seen = {"replayed": False, "out": b""}

        def ready(f):
            if f[0] == W.TEXT and b'"replayed"' in f[1]:
                seen["replayed"] = True
            elif f[0] == W.BINARY:
                seen["out"] += f[1]
            return seen["replayed"] and b"fake$ " in seen["out"]
        frames_until(sock, ready)
        return tid, sock

    def closed_with(self, sock, code):
        seen = frames_until(sock, lambda f: f[0] == W.CLOSE)
        self.assertEqual(struct.unpack("!H", seen[-1][1][:2])[0], code)
        self.assertIsNone(read_frame(sock, timeout=4))                         # then the socket goes

    def test_an_unmasked_frame_is_a_protocol_error(self):
        _, sock = self.attach_raw()
        sock.sendall(frame(W.BINARY, b"echo nope\r", mask=False))
        self.closed_with(sock, W.PROTOCOL_ERROR)

    def test_a_reserved_bit_is_a_protocol_error(self):
        _, sock = self.attach_raw()
        sock.sendall(frame(W.BINARY, b"x", rsv=0x40))                            # compression was never agreed
        self.closed_with(sock, W.PROTOCOL_ERROR)

    def test_an_oversize_frame_is_refused_unread(self):
        _, sock = self.attach_raw()
        sock.sendall(frame(W.BINARY, b"", length=W.MAX_MESSAGE + 1)[:14])     # header only: never sent
        self.closed_with(sock, W.TOO_BIG)

    def test_an_oversize_fragmented_message_is_refused(self):
        _, sock = self.attach_raw()
        chunk = b"x" * (40 * 1024)
        sock.sendall(frame(W.BINARY, chunk, fin=False) + frame(W.CONTINUATION, chunk))
        self.closed_with(sock, W.TOO_BIG)

    def test_text_must_be_utf8(self):
        _, sock = self.attach_raw()
        sock.sendall(frame(W.TEXT, b"\xff\xfe"))
        self.closed_with(sock, W.BAD_DATA)

    def test_fragments_are_reassembled_with_a_ping_between_them(self):
        tid, sock = self.attach_raw()
        sock.sendall(frame(W.BINARY, b"ec", fin=False) + frame(W.PING, b"hello")
                     + frame(W.CONTINUATION, b"ho frag", fin=False) + frame(W.CONTINUATION, b"mented\r"))
        frames_until(sock, lambda f: f[0] == W.PONG and f[1] == b"hello")
        eventually(lambda: b"fragmented" in OSC.sub(b"", self.term(tid).history().data)
                   and "echo fragmented" in [s.command for s in self.term(tid).history().spans],
                   what="the reassembled line ran")

    def test_a_continuation_without_a_message_is_a_protocol_error(self):
        _, sock = self.attach_raw()
        sock.sendall(frame(W.CONTINUATION, b"x"))
        self.closed_with(sock, W.PROTOCOL_ERROR)

    def test_the_server_pings_and_drops_a_silent_socket(self):
        self.assertEqual((W.PING_INTERVAL_S, W.PONG_TIMEOUT_S, W.MAX_MESSAGE), (20.0, 60.0, 64 * 1024))
        with patch.object(W, "PING_INTERVAL_S", 0.2), patch.object(W, "PONG_TIMEOUT_S", 0.9):
            tid, sock = self.attach_raw()
            frames_until(sock, lambda f: f[0] == W.PING)
            # Never answering: the server drops the socket and the window detaches.
            frames_until(sock, lambda f: f is None, timeout=5)
            eventually(lambda: not self.term(tid).row()["shown"], what="detached")

    def test_a_close_is_answered(self):
        _, sock = self.attach_raw()
        sock.sendall(frame(W.CLOSE, struct.pack("!H", 1000)))
        self.closed_with(sock, 1000)

    def test_a_close_code_is_echoed_and_an_empty_close_answered_empty(self):
        _, sock = self.attach_raw()
        sock.sendall(frame(W.CLOSE, struct.pack("!H", 4000) + "bye".encode()))
        self.closed_with(sock, 4000)
        _, sock = self.attach_raw()
        sock.sendall(frame(W.CLOSE, b""))
        seen = frames_until(sock, lambda f: f[0] == W.CLOSE)
        self.assertEqual(seen[-1][1], b"")                                     # never a 1005 on the wire

    def test_a_bad_close_frame_is_a_protocol_error(self):
        for body, code in ((struct.pack("!H", 1005), W.PROTOCOL_ERROR),       # may not be sent
                           (struct.pack("!H", 1006), W.PROTOCOL_ERROR),
                           (struct.pack("!H", 1015), W.PROTOCOL_ERROR),
                           (struct.pack("!H", 999), W.PROTOCOL_ERROR),        # out of range
                           (struct.pack("!H", 2000), W.PROTOCOL_ERROR),
                           (struct.pack("!H", 5000), W.PROTOCOL_ERROR),
                           (b"\x03", W.PROTOCOL_ERROR),                       # one byte
                           (struct.pack("!H", 1000) + b"\xff\xfe", W.BAD_DATA)):
            with self.subTest(body=body):
                tid, sock = self.attach_raw()
                sock.sendall(frame(W.CLOSE, body))
                self.closed_with(sock, code)
                self.owner("DELETE", f"/terminals/{tid}")                     # six at most

    def test_control_frames_are_short_and_never_fragmented(self):
        _, sock = self.attach_raw()
        sock.sendall(frame(W.PING, b"p" * 126))                                # over 125 bytes
        self.closed_with(sock, W.PROTOCOL_ERROR)
        _, sock = self.attach_raw()
        sock.sendall(frame(W.PING, b"p", fin=False))                           # fragmented
        self.closed_with(sock, W.PROTOCOL_ERROR)
        _, sock = self.attach_raw()
        sock.sendall(frame(W.PING, b"p" * 125))                                # the limit itself is fine
        frames_until(sock, lambda f: f[0] == W.PONG and f[1] == b"p" * 125)

    def test_nothing_is_queued_after_our_close(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        left.settimeout(0.0)
        sock = W.WebSocket(left)
        sock.start()
        self.assertTrue(sock.send_text("before"))
        sock.close(W.NORMAL)
        self.assertFalse(sock.send_text("after"))
        self.assertFalse(sock.send_binary(b"after"))
        self.assertFalse(sock._put(W.PONG, b"after"))
        right.settimeout(3)
        close = W.encode_frame(W.CLOSE, struct.pack("!H", W.NORMAL))
        got = b""
        while not got.endswith(close):
            chunk = right.recv(4096)
            self.assertTrue(chunk, got)
            got += chunk
        self.assertEqual(got, W.encode_frame(W.TEXT, b"before") + close)
        sock.abort()

    def test_websocket_client_round_trips_and_answers_pings(self):
        with patch.object(W, "PING_INTERVAL_S", 0.2), patch.object(W, "PONG_TIMEOUT_S", 0.8):
            tid, client = self.session()
            client.drain(1.5)                                                   # pinged, answered, kept
            client.type("echo still here")
            client.wait_line("still here")
            self.assertTrue(self.term(tid).row()["shown"])


# -- lifecycle ----------------------------------------------------------------------------------

class Lifecycle(Base):
    def test_creation_listing_and_the_attached_message(self):
        row = self.open_terminal()
        self.assertEqual(row["folder"], str(self.work))
        self.assertEqual(row["project_id"], self.project.id)
        self.assertEqual(row["title"], f"{FAKE_SHELL.name} · Calc")
        self.assertEqual((row["cols"], row["rows"], row["shown"], row["exited"], row["readable"]),
                         (80, 24, False, False, True))
        home = self.open_terminal("home", cols=120, rows=40)
        self.assertEqual((home["folder"], home["project_id"], home["cols"]), (str(self.home), None, 120))
        _, client = self.session(row["id"])
        attached = client.message("attached")
        self.assertEqual(attached["terminal"]["id"], row["id"])
        self.assertTrue(attached["terminal"]["shown"])
        self.assertFalse(attached["terminal"]["integrated"])                  # no command marked yet
        client.type("echo hello")
        client.wait_line("hello")
        eventually(lambda: self.term(row["id"]).history().integrated, what="the C mark")
        listed = {r["id"]: r for r in self.owner("GET", "/terminals")}
        self.assertTrue(listed[row["id"]]["shown"])
        self.assertTrue(listed[row["id"]]["integrated"])
        self.assertFalse(listed[home["id"]]["shown"])

    def test_the_folder_comes_from_ids_never_a_path(self):
        for body, status in (({"in": {"path": "/etc"}}, 400), ({"in": "/etc"}, 400),
                             ({"in": {"project": self.project.id, "thread": "00000000"}}, 400),
                             ({"in": {"project": "00000000"}}, 404), ({}, 400),
                             ({"in": "home", "cwd": "/etc"}, 400),
                             ({"in": "home", "cols": 0}, 400), ({"in": "home", "rows": "24"}, 400)):
            with self.subTest(body=body):
                self.request("POST", "/terminals", body, status=status)
        gone = self.stores.projects.create("Gone", str(self.root / "missing"))
        self.request("POST", "/terminals", {"in": {"project": gone.id}}, status=409)
        self.assertEqual(self.owner("GET", "/terminals"), [])

    def test_a_relative_or_windows_shell_override_is_refused(self):
        for shell in ("terminal_fake/sh", "/mnt/c/Windows/System32/cmd.exe", str(self.root / "nope")):
            with self.subTest(shell=shell), patch.object(config, "TERMINAL_SHELL", shell):
                self.request("POST", "/terminals", {"in": "home"}, status=409)

    def test_the_readable_switch_is_on_by_default_and_owner_only(self):
        tid = self.open_terminal()["id"]
        self.assertTrue(self.owner("GET", "/terminals")[0]["readable"])
        self.assertTrue(self.term(tid).history().readable)
        row = self.owner("PATCH", f"/terminals/{tid}", {"readable": False})
        self.assertFalse(row["readable"])
        self.assertFalse(self.owner("GET", "/terminals")[0]["readable"])
        self.assertFalse(self.term(tid).history().readable)
        for body in ({"readable": "yes"}, {"readable": 1}, {}, {"readable": True, "title": "x"}):
            with self.subTest(body=body):
                self.request("PATCH", f"/terminals/{tid}", body, status=400)
        self.request("PATCH", f"/terminals/{tid}", {"readable": True}, port=self.daemon.port, status=403)
        self.request("PATCH", f"/terminals/{tid}", {"readable": True}, origin=None, status=403)
        self.assertFalse(self.term(tid).readable)
        self.assertTrue(self.owner("PATCH", f"/terminals/{tid}", {"readable": True})["readable"])
        # A new terminal starts readable whatever another one says.
        self.owner("PATCH", f"/terminals/{tid}", {"readable": False})
        self.assertTrue(self.open_terminal()["readable"])

    def test_a_terminal_can_be_made_unreadable_from_its_first_byte(self):
        # WP-G2: a split's copy of a terminal Jarvis may not read is created
        # with the switch off, never readable for the round trip a PATCH takes.
        row = self.open_terminal(readable=False)
        self.assertFalse(row["readable"])
        self.assertFalse(self.term(row["id"]).readable)
        self.assertFalse(self.term(row["id"]).history().readable)
        self.assertTrue(self.open_terminal(readable=True)["readable"])
        for bad in ("no", 0, None, [False]):
            self.owner("POST", "/terminals", {"in": "home", "readable": bad}, status=400)
        # Still the owner's alone, like every terminal route.
        self.request("POST", "/terminals", {"in": "home", "readable": False}, port=self.daemon.port, status=403)

    def test_at_most_six(self):
        self.assertEqual(T.TERMINAL_CAP, 6)
        made = [self.open_terminal()["id"] for _ in range(6)]
        status, body = self.request("POST", "/terminals", {"in": "home"})
        self.assertEqual(status, 409)
        self.assertIn("6 terminals", body["error"])
        self.owner("DELETE", f"/terminals/{made[0]}")
        self.open_terminal()

    def test_takeover_asks_the_holder_and_refuses_after_the_timeout(self):
        self.assertEqual(T.TAKEOVER_TIMEOUT_S, 20.0)
        tid, first = self.session()
        first.type("echo before takeover")
        first.wait_line("before takeover")
        second = self.connect(tid)
        first.until(lambda c: c.message("takeover_request"), what="the holder is asked")
        second.until(lambda c: c.message("waiting"), what="the newcomer waits")
        self.assertNotIn("attached", second.kinds())
        request = first.message("takeover_request")
        self.assertEqual(request["timeout_s"], 20.0)
        first.control({"type": "takeover", "id": "wrong-id", "allow": True})    # ignored
        second.drain(0.3)
        self.assertNotIn("attached", second.kinds())
        first.control({"type": "takeover", "id": request["id"], "allow": True})
        first.until(lambda c: "taken" in c.kinds() and c.closed is not None, what="the holder lets go")
        second.until(lambda c: "replayed" in c.kinds(), what="the newcomer attaches")
        self.assertIn("before takeover", second.text())                        # the ring replayed
        # Keep it: the next window is refused.
        third = self.connect(tid)
        second.until(lambda c: c.message("takeover_request"), what="asked again")
        second.control({"type": "takeover", "id": second.message("takeover_request")["id"], "allow": False})
        third.until(lambda c: c.message("refused") and c.closed is not None, what="refused")
        self.assertIn("kept it", third.message("refused")["reason"])
        # No answer: refused when the time runs out.
        with patch.object(T, "TAKEOVER_TIMEOUT_S", 0.5):
            fourth = self.connect(tid)
            fourth.until(lambda c: c.message("refused") and c.closed is not None, what="timed out")
        self.assertIn("did not answer", fourth.message("refused")["reason"])
        second.type("echo still mine")
        second.wait_line("still mine")

    def test_a_window_that_is_gone_is_not_asked(self):
        tid, first = self.session()
        first.close()
        eventually(lambda: not self.term(tid).row()["shown"], what="detached")
        second = self.connect(tid)
        second.until(lambda c: "replayed" in c.kinds(), what="attached at once")
        self.assertNotIn("waiting", second.kinds())

    def test_the_ring_replays_in_order_capped_at_a_line(self):
        self.assertEqual(T.RING_BYTES, 1 << 20)
        tid, client = self.session()
        client.type("flood 100000")
        client.close()                                                         # nobody watching
        terminal = self.term(tid)

        def finished():
            data = OSC.sub(b"", terminal.history().data)
            return b"line 099999\r\n" in data and data.endswith(b"fake$ ")
        eventually(finished, timeout=30, what="the flood finished")
        again = self.connect(tid)
        again.until(lambda c: "replayed" in c.kinds(), timeout=10, what="replay")
        replay = bytes(again.output)
        self.assertLessEqual(len(replay), 1 << 20)
        self.assertEqual(again.message("attached")["replay"], len(replay))
        lines = plain(replay).split("\n")
        self.assertRegex(lines[0], r"^line \d{6}$")                             # starts on a line
        numbers = [int(line[5:]) for line in lines if re.fullmatch(r"line \d{6}", line)]
        self.assertEqual(numbers, list(range(numbers[0], 100000)))             # in order, none missing
        self.assertGreater(numbers[0], 0)                                       # the oldest were dropped
        self.assertGreater(len(replay), (1 << 20) - 64)

    def test_ring_unit(self):
        ring = T.Ring(cap=100)
        written = b"".join(b"%03d\n" % i for i in range(60))                    # 240 bytes
        for i in range(0, len(written), 7):
            ring.append(written[i:i + 7])
        self.assertEqual(ring.end, 240)
        self.assertEqual(ring.start, 140)
        start, data = ring.snapshot()
        self.assertEqual(data, written[start:])
        self.assertEqual(start, 140)                                            # 140 is already a line start
        self.assertEqual(data[:4], b"035\n")
        ring.append(b"x" * 250)                                                 # one write past the cap
        self.assertEqual((ring.start, ring.end), (390, 490))
        self.assertEqual(ring.snapshot(), (390, b"x" * 100))                    # no newline: kept whole
        small = T.Ring(cap=100)
        small.append(b"abc\ndef")
        self.assertEqual(small.snapshot(), (0, b"abc\ndef"))                    # nothing dropped: not trimmed

    def test_resize_reaches_the_child(self):
        tid, client = self.session()
        client.type("size")
        client.wait_text("24 80\n")
        client.control({"type": "resize", "cols": 132, "rows": 43})
        client.control({"type": "resize", "cols": 99999, "rows": 1})            # out of range: ignored
        client.control({"type": "resize", "cols": "80", "rows": 24})            # not an int: ignored
        eventually(lambda: self.term(tid).cols == 132, what="resized")
        client.type("size")
        client.wait_text("43 132\n")
        self.assertEqual((self.owner("GET", "/terminals")[0]["cols"]), 132)

    def test_exit_is_reported(self):
        tid, client = self.session()
        client.type("exit 3")
        client.until(lambda c: c.message("exit"), what="exit")
        self.assertEqual(client.message("exit")["code"], 3)
        row = self.owner("GET", "/terminals")[0]
        self.assertEqual((row["exited"], row["exit_code"]), (True, 3))
        client.close()
        eventually(lambda: not self.term(tid).row()["shown"], what="detached")
        again = self.connect(tid)                                               # reattach still replays
        again.until(lambda c: c.message("exit"), what="exit replayed")

    def test_close_kills_background_jobs_in_the_session(self):
        tid, client = self.session()
        client.type("bg")
        client.type("bgnohup")
        found = client.until(lambda c: re.findall(r"BGPID (\d+)", c.text())[1:] and
                             re.findall(r"BGPID (\d+)", c.text()), what="two jobs")
        pids = [int(p) for p in found]
        shell = self.term(tid)._proc.pid
        self.assertTrue(all(not dead(p) for p in pids + [shell]))
        self.assertEqual(T.CLOSE_GRACE_S, 3.0)
        with patch.object(T, "CLOSE_GRACE_S", 0.4):
            result = self.owner("DELETE", f"/terminals/{tid}")
        self.assertTrue(result["ok"])
        eventually(lambda: all(dead(p) for p in pids + [shell]), what="the whole session is gone")
        client.until(lambda c: c.closed is not None, what="the window is told")
        self.assertEqual(client.message("exit")["reason"], "closed")
        self.assertEqual(self.owner("GET", "/terminals"), [])
        self.request("DELETE", f"/terminals/{tid}", status=404)

    def test_sighup_goes_out_before_sigkill(self):
        tid, client = self.session()
        client.type("bgnohup")
        found = client.until(lambda c: re.findall(r"BGPID (\d+)", c.text()), what="a job")
        job, shell = int(found[0]), self.term(tid)._proc.pid
        real, sent = os.kill, []

        def spy(pid, sig):
            sent.append((pid, sig))
            return real(pid, sig)
        with patch.object(os, "kill", spy), patch.object(T, "CLOSE_GRACE_S", 0.4):
            self.owner("DELETE", f"/terminals/{tid}")
        eventually(lambda: dead(job) and dead(shell), what="gone")
        to_job = [sig for pid, sig in sent if pid == job]
        self.assertIn(signal.SIGHUP, to_job)
        self.assertIn(signal.SIGKILL, to_job)                                   # it ignored the HUP
        self.assertLess(to_job.index(signal.SIGHUP), to_job.index(signal.SIGKILL))
        self.assertEqual([sig for pid, sig in sent if pid == shell][:1], [signal.SIGHUP])

    def test_signals_the_daemon_ignores_are_default_in_the_shell(self):
        previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)               # as under nohup
        try:
            tid, _ = self.session()
        finally:
            signal.signal(signal.SIGHUP, previous)
        status = Path(f"/proc/{self.term(tid)._proc.pid}/status").read_text()
        ignored = int(re.search(r"SigIgn:\s*([0-9a-f]+)", status).group(1), 16)
        self.assertFalse(ignored & (1 << (signal.SIGHUP - 1)), "the shell inherited SIGHUP ignored")

    def test_two_deletes_at_once_close_it_once(self):
        tid = self.open_terminal()["id"]
        results = []
        workers = [threading.Thread(target=lambda: results.append(
            self.request("DELETE", f"/terminals/{tid}")[0])) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
        self.assertEqual(sorted(results), [200, 404, 404, 404])
        self.open_terminal()                                                    # the daemon is fine

    def test_the_shell_is_its_realpath_under_its_own_name(self):
        bindir = self.root / "bin"
        bindir.mkdir()
        (bindir / "myshell").symlink_to(FAKE_SHELL)
        (bindir / "winshell").symlink_to("/mnt/c/Windows/System32/cmd.exe")
        with patch.object(config, "TERMINAL_SHELL", str(bindir / "myshell")):
            row = self.open_terminal()
        self.assertEqual((row["title"], row["integration"]), ("myshell · Calc", "posix"))
        with patch.object(config, "TERMINAL_SHELL", str(bindir / "myshell")):
            shell = T.resolve_shell()
        self.assertEqual((shell.path, shell.name), (str(FAKE_SHELL), str(bindir / "myshell")))
        prefix, program = T.launcher(shell)
        self.assertEqual(program, str(FAKE_SHELL))                             # the realpath runs,
        if any(part.startswith("--argv0=") for part in prefix):               # under its own name
            self.assertIn(f"--argv0={bindir / 'myshell'}", prefix)
        with patch.object(config, "TERMINAL_SHELL", str(bindir / "winshell")):
            status, body = self.request("POST", "/terminals", {"in": "home"})
        self.assertEqual(status, 409)
        self.assertIn("/mnt/", body["error"])

    def test_without_env_argv0_the_checked_realpath_runs_or_nothing_does(self):
        # PR #25 re-review: an env without --argv0 used to exec the *given*
        # name — a link that can point elsewhere by the time it runs — rather
        # than the realpath that was checked.
        env = shutil.which("env", path="/usr/bin:/bin")
        bindir = self.root / "bin"
        bindir.mkdir()
        (bindir / "sh").symlink_to(FAKE_SHELL)                 # same basename: argv[0] changes nothing
        (bindir / "rsh").symlink_to(FAKE_SHELL)                # another name: it would change what runs
        for signals in (True, False):
            with patch.object(T, "_env_support", (env, signals, False)):
                prefix, program = T.launcher(T.Shell(str(FAKE_SHELL), str(bindir / "sh")))
                self.assertEqual(program, str(FAKE_SHELL))
                self.assertFalse(any(p.startswith("--argv0=") for p in prefix))
                self.assertNotIn(str(bindir / "sh"), prefix)
                with self.assertRaises(T.TerminalError) as caught:
                    T.launcher(T.Shell(str(FAKE_SHELL), str(bindir / "rsh")))
                self.assertIn("argv[0]", str(caught.exception))
        # Through the route: the realpath runs (its pid's exe is the file that
        # was checked, whatever the link says), and a renamed one is a 409.
        with patch.object(T, "_env_support", (env, True, False)), \
                patch.object(config, "TERMINAL_SHELL", str(bindir / "sh")):
            row = self.open_terminal()
            tid, client = self.session(row["id"])
            (bindir / "sh").unlink()                           # the link moves on: nothing changes
            self.assertEqual(Path(f"/proc/{self.term(tid)._proc.pid}/cmdline").read_bytes().split(b"\0")[1],
                             str(FAKE_SHELL).encode())
        with patch.object(T, "_env_support", (env, True, False)), \
                patch.object(config, "TERMINAL_SHELL", str(bindir / "rsh")):
            status, body = self.request("POST", "/terminals", {"in": "home"})
        self.assertEqual(status, 409)
        self.assertIn("argv[0]", body["error"])

    def test_the_startup_file_never_blocks_its_pipe(self):
        # PR #25 re-review: a blocking write of the startup file under
        # Terminals._lock wedged create() — and every terminal route — once
        # the file outgrew the pipe (two pages under pipe-user-pages-soft).
        terminals = T.Terminals()
        for kind in T.RC_PATHS:                                # the shipped files fit, nonce and all
            content = f"__jarvis_nonce='{'0' * 32}'\n".encode() + T.RC_PATHS[kind].read_bytes()
            self.assertLessEqual(len(content), T.RC_PIPE_MAX, kind)
        self.assertEqual(T.RC_PIPE_MAX, 8 * 1024)
        # Too large for the guaranteed capacity: refused before a pipe exists.
        before = set(os.listdir("/proc/self/fd"))
        terminals._rc_text["bash"] = b"#" * T.RC_PIPE_MAX
        with self.assertRaises(T.TerminalError):
            terminals._rc_pipe("0" * 32, "bash")
        # A pipe smaller than the file (one page, as F_SETPIPE_SZ allows): a
        # short write is a refusal, at once, not a hang and not a cut file.
        terminals._rc_text["bash"] = b"#" * 6000
        real_pipe = os.pipe

        def small_pipe():
            read, write = real_pipe()
            fcntl.fcntl(write, fcntl.F_SETPIPE_SZ, 4096)
            return read, write
        started = time.monotonic()
        with patch.object(T.os, "pipe", small_pipe), self.assertRaises(T.TerminalError) as caught:
            terminals._rc_pipe("0" * 32, "bash")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIn("did not fit", str(caught.exception))
        self.assertEqual(set(os.listdir("/proc/self/fd")), before, "a descriptor leaked")
        # And through create(): a 409 with the daemon still answering.
        self.daemon.terminals._rc_text["posix"] = b"#" * (T.RC_PIPE_MAX + 1)
        status, body = self.request("POST", "/terminals", {"in": "home"})
        self.assertEqual(status, 409)
        self.assertIn("too large", body["error"])
        self.assertEqual(self.owner("GET", "/terminals"), [])
        del self.daemon.terminals._rc_text["posix"]
        self.open_terminal()

    def test_marked_says_the_startup_file_is_running(self):
        # PR #25 re-review: `integration` is what was configured. A signed
        # prompt mark is what says it took; the window hears it once.
        terminal = T.Terminal("0000000d", shell="/bin/sh", folder="/", project_id=None, label="~",
                              cols=80, rows=24, nonce="ab" * 16, integration="posix")
        self.addCleanup(terminal.finish)

        class Sock:
            def __init__(self):
                self.texts, self.closed = [], threading.Event()

            def send_binary(self, data):
                return True

            def send_text(self, text):
                self.texts.append(json.loads(text))
                return True
        sock = Sock()
        terminal._attachment = sock
        self.assertFalse(terminal.row()["marked"])
        terminal._output(mark(b"A", nonce="00" * 16) + b"forged$ ")           # the wrong nonce
        terminal._output(b"\x1b]133;A\x07plain$ ")                             # no nonce at all
        self.assertFalse(terminal.row()["marked"])
        self.assertEqual(sock.texts, [])
        signed = mark(b"A", nonce="ab" * 16)
        terminal._output(b"$ " + signed[:5])                                   # split across chunks
        terminal._output(signed[5:] + b"sh$ ")
        terminal._output(mark(b"A", nonce="ab" * 16) + b"sh$ ")
        self.assertTrue(terminal.row()["marked"])
        self.assertEqual(sock.texts, [{"type": "marked"}])                     # once
        # The fake shell marks its prompt: the listing and the attach say so.
        tid, client = self.session()
        eventually(lambda: self.term(tid).row()["marked"], what="marked")
        client.until(lambda c: c.message("attached")["terminal"]["marked"] or "marked" in c.kinds(),
                     what="the window told")
        self.assertTrue(self.owner("GET", "/terminals")[0]["marked"])

    def test_a_program_that_does_not_read_never_stalls_the_socket(self):
        self.assertEqual(T.INPUT_CAP, 256 * 1024)
        with patch.object(T, "INPUT_CAP", 32 * 1024), patch.object(W, "PING_INTERVAL_S", 0.3), \
                patch.object(W, "PONG_TIMEOUT_S", 2.0):
            tid, client = self.session()
            client.type("deaf")
            client.wait_text("DEAF")
            for _ in range(64):                                                 # 1 MiB it never reads
                client.conn.send_binary(b"x" * 16384)
            client.until(lambda c: c.message("input_dropped"), what="the paste is refused")
            dropped = client.message("input_dropped")
            self.assertTrue(dropped["latched"])
            self.assertIn("not reading", dropped["reason"])
            client.control({"type": "resize", "cols": 101, "rows": 33})         # control still lands
            eventually(lambda: self.term(tid).cols == 101, what="resized while stuck")
            client.drain(2.5)                                                   # pings still answered
            self.assertIsNone(client.closed)
            self.assertTrue(self.term(tid).row()["shown"])
            # Latched: a lone Ctrl-C still goes in; resuming waits for the
            # queue to drain, which a program that never reads never lets it.
            before = client.kinds().count("input_dropped")
            client.conn.send_binary(b"\x03")
            client.control({"type": "input_resume"})
            client.until(lambda c: c.message("input_resume_refused"), what="resume refused")
            self.assertEqual(client.kinds().count("input_dropped"), before)    # the ^C was taken
            self.assertNotIn("input_resumed", client.kinds())

    def test_a_dropped_paste_is_never_spliced(self):
        # The reviewer's shape: `sleep 1; cat > file` under a 768 KiB paste in
        # 16 KiB frames 40 ms apart. Without the latch the frames after the
        # first drop were taken again once room freed: a prefix, a hole, then
        # a later chunk, cut mid-line.
        out = self.root / "pasted.bin"
        paste = b"".join(b"%07d\n" % i for i in range(768 * 1024 // 8))
        with patch.object(T, "INPUT_CAP", 32 * 1024):
            tid, client = self.session()
            client.type(f"lateread {out}")
            client.wait_text("LATE")
            for i in range(0, len(paste), 16384):
                client.conn.send_binary(paste[i:i + 16384])
                time.sleep(0.04)
            eventually(out.exists, timeout=20, what="the program saved what it read")
            got = out.read_bytes()
            client.until(lambda c: c.message("input_dropped"), what="a frame dropped (else no test)")
            self.assertTrue(0 < len(got) < len(paste), len(got))
            self.assertTrue(paste.startswith(got), "the program got a spliced paste")
            # Typing waits for an explicit resume, after the queue drained.
            client.wait_line(f"READ {len(got)}")
            client.type("echo ignored")
            client.control({"type": "input_resume"})
            client.until(lambda c: "input_resumed" in c.kinds(), what="resumed")
            client.output.clear()
            client.type("echo typed again")
            client.wait_line("typed again")
            self.assertNotIn("ignored", client.text())

    def test_ctrl_c_throws_away_queued_input_but_never_a_ctrl_c(self):
        terminal = T.Terminal("0000000c", shell="/bin/sh", folder="/", project_id=None, label="~",
                              cols=80, rows=24, nonce="0" * 32)
        self.addCleanup(terminal.finish)
        with patch.object(T, "INPUT_CAP", 100):
            self.assertTrue(terminal.queue_input(b"a" * 60))
            self.assertFalse(terminal.queue_input(b"b" * 60))                   # over the cap: refused
            self.assertTrue(terminal.queue_input(b"\x03"))
            self.assertEqual(list(terminal._inbox), [b"\x03"])
            self.assertTrue(terminal.queue_input(b"\x03"))                      # two quick ones: both
            self.assertEqual(list(terminal._inbox), [b"\x03", b"\x03"])
            self.assertTrue(terminal.queue_input(b"c" * 90))
            self.assertTrue(terminal.queue_input(b"\x03"))                      # keeps one waiting
            self.assertEqual(list(terminal._inbox), [b"\x03", b"\x03"])
            self.assertEqual(terminal._in_bytes, 2)
            self.assertFalse(terminal.input_drained())

    def test_daemon_stop_ends_every_terminal(self):
        pids, clients = [], []
        for _ in range(2):
            tid, client = self.session()
            client.type("bgnohup")
            found = client.until(lambda c: re.findall(r"BGPID (\d+)", c.text()), what="a job")
            pids += [int(found[0]), self.term(tid)._proc.pid]
            clients.append(client)
        started = time.monotonic()
        self.daemon.stop()
        self.assertLess(time.monotonic() - started, 4.0)
        eventually(lambda: all(dead(p) for p in pids), what="every session is gone")
        for client in clients:
            client.until(lambda c: c.closed is not None, what="told")
            self.assertEqual(client.message("exit")["reason"], "ended")
        with self.assertRaises(T.TerminalError):
            self.daemon.terminals.create(str(self.work), project_id=None, label="~")


# -- what a terminal runs with ------------------------------------------------------------

class Environment(Base):
    PLANTED = {"OPENROUTER_API_KEY": "placeholder-not-a-key", "HF_HUB_OFFLINE": "1",
               "ANTHROPIC_API_KEY": "placeholder", "CLAUDE_CODE_OAUTH_TOKEN": "placeholder",
               "CODEX_HOME": "/nowhere", "OPENAI_API_KEY": "placeholder", "JARVIS_ANYTHING": "x",
               "PYTHONPATH": "/nowhere", "BASH_ENV": "/nowhere/rc", "PROMPT_COMMAND": "true",
               "LC_PLANTED_BY_DOTENV": "placeholder"}
    FORBIDDEN = set(PLANTED) | {"VIRTUAL_ENV"}

    def setUp(self):
        super().setUp()
        repo = self.root / "repo"
        repo.mkdir()
        (repo / ".env").write_text("LC_PLANTED_BY_DOTENV=placeholder\nOPENROUTER_API_KEY=placeholder\n")
        venv, other = self.root / "venv", self.root / "uvvenv"
        (venv / "bin").mkdir(parents=True)
        (other / "bin").mkdir(parents=True)
        (other / "pyvenv.cfg").write_text("home = /usr/bin\n")
        self.venv_bins = [str(venv / "bin"), str(other / "bin")]
        planted = dict(self.PLANTED, VIRTUAL_ENV=str(venv),
                       PATH=":".join(self.venv_bins + [os.environ.get("PATH", "/usr/bin:/bin")]))
        guard = patch.dict(os.environ, planted)
        guard.start()
        self.addCleanup(guard.stop)
        guard = patch.object(config, "REPO_ROOT", repo)
        guard.start()
        self.addCleanup(guard.stop)

    def names(self, client):
        found = client.until(lambda c: re.search(r"NAMES (.*?) END", c.text()), what="names")
        return set(found.group(1).split())

    def test_the_shell_gets_a_clean_environment_by_name(self):
        _, client = self.session()
        client.type("names")
        names = self.names(client)
        self.assertFalse(names & self.FORBIDDEN, sorted(names & self.FORBIDDEN))
        self.assertFalse({n for n in names if n.startswith(("JARVIS_", "ANTHROPIC_", "CLAUDE_", "CODEX_"))})
        self.assertTrue({"HOME", "PATH", "TERM", "SHELL", "LANG"} <= names, sorted(names))
        self.assertNotIn("ENV", names)                                          # read, then unset
        for entry, answer in [(e, "no") for e in self.venv_bins] + [("/usr/bin", "yes")]:
            client.output.clear()
            client.type(f"pathhas {entry}")
            client.wait_text(f"PATHHAS {answer}\n")

    def test_clean_environment_unit(self):
        env = T.clean_environment("/bin/bash", env_file=config.REPO_ROOT / ".env")
        self.assertFalse(set(env) & self.FORBIDDEN, sorted(set(env) & self.FORBIDDEN))
        self.assertEqual((env["TERM"], env["SHELL"], env["HOME"]), ("xterm-256color", "/bin/bash", str(self.home)))
        self.assertFalse(set(env["PATH"].split(":")) & set(self.venv_bins))
        self.assertEqual(T.shell_command("/bin/bash", "bash", "/rc"),
                         (["/bin/bash", "--rcfile", "/rc", "-i"], {}))
        self.assertEqual(T.shell_command("/usr/bin/dash", "posix", "/rc"),
                         (["/usr/bin/dash", "-i"], {"ENV": "/rc"}))
        self.assertEqual(T.shell_command("/usr/bin/zsh", "none", None), (["/usr/bin/zsh", "-l"], {}))
        self.assertEqual([T.shell_kind(s) for s in ("/usr/bin/bash", "/usr/bin/dash", "/bin/sh",
                                                    "/bin/busybox", "/usr/bin/zsh", "/usr/bin/fish")],
                         ["bash", "posix", "posix", "posix", "none", "none"])
        # Judged on the name it is called by too: bash called sh reads $ENV.
        self.assertEqual([T.shell_kind(path, name) for path, name in (
            ("/usr/bin/bash", "/usr/bin/rbash"), ("/usr/bin/bash", "/bin/sh"),
            ("/usr/bin/zsh", "/bin/sh"), ("/usr/bin/zsh", "/usr/bin/zsh"))],
            ["bash", "posix", "posix", "none"])


# -- what never leaves memory --------------------------------------------------------------

class capture_logs:
    """Every record the daemon's process logs, formatted, at DEBUG and up —
    except the test's own WebSocket client library's."""

    def __enter__(self):
        self.records: list[str] = []
        outer = self

        class Keep(logging.Handler):
            def emit(self, record):
                if not record.name.startswith("websocket"):
                    outer.records.append(record.getMessage())

        self.handler = Keep(level=logging.DEBUG)
        self.root = logging.getLogger()
        self.level = self.root.level
        self.root.addHandler(self.handler)
        self.root.setLevel(logging.DEBUG)
        return self.records

    def __exit__(self, *exc):
        self.root.removeHandler(self.handler)
        self.root.setLevel(self.level)


class Leaks(Base):
    def test_output_reaches_no_bus_no_log_and_no_file(self):
        import contextvars
        from jarvis import runtime, tools
        from jarvis.v2.tools import terminal_read  # noqa: F401  (registers the tool)
        marker = "zq-terminal-marker-" + secrets.token_hex(6)
        events = self.daemon.bus.subscribe()

        def read():
            ctx = contextvars.copy_context()
            ctx.run(runtime.bind, desk={"present": True}, depth=0,
                    tool_names={"terminal_read"})  # dispatch refuses unbound (PR #42)
            return ctx.run(tools.dispatch, "terminal_read",
                           json.dumps({"terminal": tid, "lines": 20})).text

        with capture_logs() as records:
            tid, client = self.session()
            client.type(f"echo {marker}")
            client.wait_line(marker)
            # Jarvis reads it (WP-F): the tool's answer holds the output,
            # the bus and the log never do.
            self.assertIn(marker, read())
            client.control({"type": "resize", "cols": 100, "rows": 30})
            self.owner("PATCH", f"/terminals/{tid}", {"readable": False})
            client.close()
            again = self.connect(tid)
            again.until(lambda c: marker in c.text(), what="replay")            # it is in the ring
            self.owner("DELETE", f"/terminals/{tid}")
        drained = []
        while True:
            try:
                drained.append(events.get_nowait())
            except Exception:
                break
        # Lifecycle ids only, never output: one `terminal_attached` per attach
        # (two here), each exactly {terminal_id, at} — and one `terminal_read`
        # for the read, exactly {terminal_id, lines, at, refused}.
        self.assertEqual(sorted(r["kind"] for r in drained),
                         ["terminal_attached"] * 2 + ["terminal_read"], drained)
        for record in drained:
            self.assertEqual(set(record), {"kind", "data"})
            keys = ({"terminal_id", "at"} if record["kind"] == "terminal_attached"
                    else {"terminal_id", "lines", "at", "refused"})
            self.assertEqual(set(record["data"]), keys)
            self.assertEqual(record["data"]["terminal_id"], tid)
            self.assertRegex(record["data"]["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
        self.assertNotIn(marker, json.dumps(drained))
        text = "\n".join(records)
        self.assertIn(f"terminal {tid} opened", text)                           # the capture worked
        self.assertIn(f"terminal {tid} closed", text)
        self.assertNotIn(marker, text)
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(marker.encode(), path.read_bytes(), path)


_PROMPT_MARK = re.compile(rb"\]133;B;jarvis=[0-9a-f]+\x1b\\")


class DevUrls(Base):
    """Live terminal banners join the memory-only registry (AO-1b-ii).

    The attached owner's socket carries the cleaned URL; the bus carries ids.
    With auto_open off, no output reaches the sniffer or registry.
    """
    WINDOW = "a" * 32

    def connect(self, tid, ticket="fresh", *, window=WINDOW, **kw):
        return super().connect(tid, ticket, window=window, **kw)

    def setUp(self):
        super().setUp()
        # One frame a second per terminal is devurl_check's to prove; here
        # several servers announce themselves in quick succession.
        guard = patch.object(devurl, "RATE_GAP_S", 0.0)
        guard.start()
        self.addCleanup(guard.stop)
        from jarvis.v2 import devservers
        guard = patch.object(devservers, "SOURCE_RATE_S", 0.0)
        guard.start()
        self.addCleanup(guard.stop)

    @staticmethod
    def dev_urls(client):
        return [m for m in client.messages if m.get("type") == "dev_url"]

    def wait_dev_url(self, client, url):
        client.until(lambda c: any(m.get("url") == url for m in self.dev_urls(c)),
                     what=f"dev_url {url}")

    @staticmethod
    def prompts(client) -> int:
        return len(_PROMPT_MARK.findall(bytes(client.output)))

    def at_prompt(self, client, count=1):
        client.until(lambda c: self.prompts(c) >= count, what="a prompt")

    def run_line(self, client, line, expect=None):
        """Type a line once the shell is at its prompt, and wait for the next
        prompt: typed ahead, the tty would echo it inside the running
        command's output, where it is output."""
        self.at_prompt(client)
        before = self.prompts(client)
        client.type(line)
        self.at_prompt(client, before + 1)
        expect = line.partition(" ")[2] if expect is None else expect
        self.assertIn("\n" + expect + "\n", client.text())

    def session(self, tid=None):
        tid, client = super().session(tid)
        self.at_prompt(client)
        return tid, client

    def test_it_reaches_only_the_window_attached_to_that_terminal(self):
        tid1, one = self.session()
        tid2, two = self.session()
        one.type("echo   Local:   http://localhost:24601/")
        self.wait_dev_url(one, "http://localhost:24601/")
        # After the bytes that carried it: the line is already on screen.
        self.assertIn("  Local:   http://localhost:24601/\n", one.text())
        one.drain(0.3)
        two.drain(0.3)
        frame, = self.dev_urls(one)
        self.assertEqual(set(frame), {"type", "url", "id"})
        self.assertEqual(frame["url"], "http://localhost:24601/")
        entry = self.daemon.devservers._entry(frame["id"])
        self.assertEqual((entry.state, entry.window), ("claimed", self.WINDOW))
        self.assertEqual(entry.source, {"kind": "terminal", "terminal_id": tid1})
        self.assertEqual(self.dev_urls(two), [])
        self.assertNotIn("dev_url", json.dumps(self.owner("GET", "/terminals")))

    def test_the_prompt_and_the_typed_line_are_never_read(self):
        tid, client = self.session()
        # The first command (before any C mark) and a later one (after).
        self.run_line(client, "# Local: http://localhost:24602/", "fake: #: command not found")
        self.run_line(client, "curl http://localhost:24603", "fake: curl: command not found")
        self.run_line(client, "#  * Running on http://127.0.0.1:24604", "fake: #: command not found")
        self.run_line(client, "echo  Local: http://localhost:24605/")
        self.wait_dev_url(client, "http://localhost:24605/")
        client.drain(0.3)
        self.assertEqual([m["url"] for m in self.dev_urls(client)], ["http://localhost:24605/"])

    def test_jarvis_ports_are_never_sent(self):
        tid, client = self.session()
        for port in sorted({self.daemon.face_port, self.daemon.port, self.daemon.workshop_port,
                            8402, 8403, 8404, 8405, 8406}):
            for host in ("localhost", "127.0.0.1", "[::1]", "0.0.0.0"):
                self.run_line(client, f"echo  Local: http://{host}:{port}/")
        self.run_line(client, "echo  Local: http://127.0.0.1:24606/")
        self.wait_dev_url(client, "http://localhost:24606/")                  # sent as localhost
        self.assertEqual([m["url"] for m in self.dev_urls(client)], ["http://localhost:24606/"])

    def test_only_id_state_source_time_reach_the_bus_no_url_log_or_file(self):
        marker = "zq-devurl-" + secrets.token_hex(6)
        url = f"http://localhost:24607/{marker}/"
        events = self.daemon.bus.subscribe()
        with capture_logs() as records:
            tid, client = self.session()
            client.type(f"echo  Local: {url}")
            self.wait_dev_url(client, url)
            self.owner("PATCH", "/browser/settings", {"auto_open": True})
            self.owner("DELETE", f"/terminals/{tid}")
        drained = []
        while True:
            try:
                drained.append(events.get_nowait())
            except Exception:
                break
        self.assertEqual([r["kind"] for r in drained],
                         ["terminal_attached", "dev_server", "dev_server_settings"], drained)
        self.assertEqual(drained[2], {"kind": "dev_server_settings", "data": {"auto_open": True}})
        record = drained[1]
        self.assertEqual(set(record), {"kind", "data"})
        self.assertEqual(set(record["data"]), {"id", "state", "source", "at"})
        self.assertEqual(record["data"]["state"], "claimed")
        self.assertEqual(record["data"]["source"], {"kind": "terminal", "terminal_id": tid})
        self.assertNotIn(marker, json.dumps(drained))
        self.assertNotIn("24607", json.dumps(drained))
        self.assertNotIn(self.WINDOW, json.dumps(drained))
        text = "\n".join(records)
        self.assertIn(f"terminal {tid} closed", text)                           # the capture worked
        self.assertIn("browser settings changed by the owner: auto_open on", text)
        self.assertNotIn(marker, text)
        self.assertNotIn("24607", text)
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(marker.encode(), path.read_bytes(), path)

    def test_with_auto_open_off_the_sniffer_is_never_fed(self):
        self.assertTrue(self.owner("GET", "/browser/settings")["auto_open"])
        self.assertEqual(self.owner("PATCH", "/browser/settings", {"auto_open": False}),
                         {"lan": False, "local_servers": False, "auto_open": False,
                           "sound": True, "agents_may_drive": True})
        fed = []
        real = devurl.DevUrlSniffer.feed

        def counting(sniffer, data):
            fed.append(len(data))
            return real(sniffer, data)
        with patch.object(devurl.DevUrlSniffer, "feed", counting):
            tid, client = self.session()
            self.run_line(client, "echo  Local: http://localhost:24608/")
            client.drain(0.3)
            self.assertEqual(self.dev_urls(client), [])
            self.assertEqual(fed, [], "no byte of output reached the sniffer")
            self.assertEqual(self.owner("GET", "/dev-servers")["servers"], [])
            self.assertFalse(self.owner("GET", "/browser/settings")["auto_open"])
            # On again: the next announcement is read.
            self.owner("PATCH", "/browser/settings", {"auto_open": True})
            self.run_line(client, "echo  Local: http://localhost:24609/")
            self.wait_dev_url(client, "http://localhost:24609/")
        self.assertTrue(fed)
        self.assertEqual([m["url"] for m in self.dev_urls(client)], ["http://localhost:24609/"])

    def test_a_replay_or_a_second_announcement_never_sends_it_again(self):
        tid, client = self.session()
        self.run_line(client, "echo  Local: http://localhost:24610/")
        self.wait_dev_url(client, "http://localhost:24610/")
        client.close()
        again = self.connect(tid)
        again.until(lambda c: "replayed" in c.kinds() and "24610" in c.text(), what="the replay")
        self.at_prompt(again)
        # The same server again (a restart): the same origin, and Puma's
        # second line for it.
        self.run_line(again, "echo  Local: http://127.0.0.1:24610/")
        self.run_line(again, "echo * Listening on http://[::1]:24610")
        self.run_line(again, "echo  Local: http://localhost:24611/")
        self.wait_dev_url(again, "http://localhost:24611/")
        self.assertEqual([m["url"] for m in self.dev_urls(again)], ["http://localhost:24611/"])

    def test_the_switch_is_the_owners(self):
        path = "/browser/settings"
        defaults = {"lan": False, "local_servers": False, "auto_open": True,
                    "sound": True, "agents_may_drive": True}
        self.assertEqual(self.owner("GET", path), defaults)
        for kw in ({"port": self.daemon.port}, {"port": self.daemon.workshop_port},
                   {"origin": f"http://127.0.0.1:{self.daemon.workshop_port}"},
                   {"origin": "http://evil.example"}, {"origin": "null"}, {"origin": None},
                   {"host": f"evil.example:{self.daemon.face_port}"}):
            with self.subTest(**{k: str(v) for k, v in kw.items()}):
                status, _ = self.request("PATCH", path, {"auto_open": False}, **kw)
                self.assertIn(status, (403, 404))
                status, _ = self.request("GET", path, None, **kw)
                self.assertIn(status, (403, 404))
        for bad in ({"auto_open": "no"}, {"auto_open": 0}, {"bogus": True}, {}, [True]):
            with self.subTest(bad=bad):
                self.assertEqual(self.request("PATCH", path, bad)[0], 400)
        self.assertEqual(self.request("PATCH", path + "?x=1", {"auto_open": False})[0], 400)
        self.assertEqual(self.request("POST", path, {"auto_open": False})[0], 404)
        self.assertEqual(self.request("GET", "/browser/other")[0], 404)
        self.assertEqual(self.owner("GET", path), defaults)
        self.assertFalse((self.root / "browser.json").exists(), "a test daemon writes no file")

    def test_one_set_of_switches_for_every_reader(self):
        # The reconciliation with BR-1a (#69): the routes, the engine, the
        # gate's per-connection reads, `/status` and the terminals all ask the
        # daemon's one `BrowserSettings`. No Chrome starts.
        settings, host = self.daemon.browser_settings, self.daemon.browser
        self.assertIs(host._settings, settings)
        tid = self.open_terminal()["id"]
        term = self.term(tid)
        self.assertTrue(term._dev_on())
        self.assertFalse(host.loopback_open(24699))
        changed = self.owner("PATCH", "/browser/settings",
                             {"lan": True, "local_servers": True, "auto_open": False})
        self.assertEqual(changed, {"lan": True, "local_servers": True, "auto_open": False,
                                   "sound": True, "agents_may_drive": True})
        self.assertEqual((settings.value("lan"), settings.value("local_servers")), (True, True))
        self.assertTrue(host.loopback_open(24699), "the gate's per-connection read")
        self.assertFalse(term._dev_on(), "the terminals' read")
        status = self.request("GET", "/status", port=self.daemon.port, origin=None)[1]["browser"]
        self.assertEqual((status["lan"], status["local_servers"]), (True, True))
        self.assertTrue(self.owner("GET", "/browser")["settings"]["lan"])
        self.assertEqual(self.request("PATCH", "/browser/settings", {"local_servers": "yes"})[0], 400)
        self.assertTrue(settings.value("local_servers"), "a refused PATCH changed nothing")

    def test_standalone_without_registry_keeps_legacy_signed_mark_behavior(self):
        terminal = T.Terminal("0000000e", shell="/bin/sh", folder="/", project_id=None, label="~",
                              cols=80, rows=24, nonce=NONCE, integration="posix")
        self.addCleanup(terminal.finish)
        terminal._sniffer = devurl.DevUrlSniffer(lambda: set())
        terminal._dev_on = lambda: True
        line = b"  Local: http://localhost:24612/\r\n"
        terminal._output(line)                                                  # no window

        class Sock:
            def __init__(self):
                self.sent, self.closed = [], threading.Event()

            def send_binary(self, data):
                self.sent.append(("binary", bytes(data)))
                return True

            def send_text(self, text):
                self.sent.append(("text", json.loads(text)))
                return True
        sock = Sock()
        terminal._attachment = sock
        # The typed line between B and C, and the C mark split across chunks.
        c_mark = mark(b"C;cmdline_url=npm%20run%20dev")
        terminal._output(mark(b"A") + b"$ " + mark(b"B") + b"echo Local: http://localhost:24613/\r\n"
                         + c_mark[:9])
        terminal._output(c_mark[9:] + line[:12])
        terminal._output(line[12:] + mark(b"D;0"))
        texts = [item for kind, item in sock.sent if kind == "text"]
        self.assertEqual(texts, [{"type": "marked"},                           # the signed A
                                 {"type": "dev_url", "url": "http://localhost:24612/"}])
        self.assertEqual(sock.sent[-1][0], "text", "after the bytes that carried it")
        # A mark without this terminal's nonce moves nothing.
        terminal._output(mark(b"A", nonce="00" * 16) + b"  Local: http://localhost:24614/\r\n")
        self.assertEqual(sock.sent[-1], ("text", {"type": "dev_url", "url": "http://localhost:24614/"}))

    def test_a_frame_the_socket_refuses_is_not_remembered(self):
        # PR #70 review: the URL used to be recorded before `send_text`, whose
        # False was ignored, so a socket that was closing or behind cost the
        # server its one frame for good.
        terminal = T.Terminal("0000000c", shell="/bin/sh", folder="/", project_id=None, label="~",
                              cols=80, rows=24, nonce=NONCE, integration="posix")
        self.addCleanup(terminal.finish)
        terminal._sniffer = devurl.DevUrlSniffer(lambda: set())
        terminal._dev_on = lambda: True
        frames, accept = [], [False]

        def send_text(text):
            if accept[0]:
                frames.append(json.loads(text))
            return accept[0]
        terminal._attachment = SimpleNamespace(send_binary=lambda d: True, send_text=send_text,
                                               closed=threading.Event())
        line = b"  Local: http://localhost:24616/\r\n"
        terminal._output(line)                                                 # refused
        self.assertEqual(frames, [])
        self.assertEqual(len(terminal._sniffer._recent), 0, "not remembered")
        accept[0] = True
        terminal._output(line)                                                 # the restart's
        self.assertEqual(frames, [{"type": "dev_url", "url": "http://localhost:24616/"}])
        terminal._output(line)
        self.assertEqual(len(frames), 1, "and remembered once sent")

    def test_unattached_live_pty_banner_is_retained_for_owner_claim(self):
        tid, client = self.session()
        client.close()
        terminal = self.term(tid)
        until = time.monotonic() + 3
        while terminal._attachment is not None and time.monotonic() < until:
            time.sleep(.01)
        self.assertIsNone(terminal._attachment)
        events = self.daemon.bus.subscribe()
        self.addCleanup(self.daemon.bus.unsubscribe, events)
        # Real fake-shell PTY output with no socket: path retained, query/fragment stripped.
        self.assertTrue(terminal.queue_input(b"echo  Local: http://localhost:24620/app/?secret=hidden#frag\n"))
        until = time.monotonic() + 3
        while not self.daemon.devservers.listing()["servers"] and time.monotonic() < until:
            time.sleep(.01)
        server, = self.owner("GET", "/dev-servers")["servers"]
        self.assertEqual(server["url"], "http://localhost:24620/app/")
        self.assertEqual((server["state"], server["claimed"]), ("up", False))
        record = events.get(timeout=1)
        self.assertEqual(set(record), {"kind", "data"})
        self.assertEqual(record["kind"], "dev_server")
        self.assertEqual(set(record["data"]), {"id", "state", "source", "at"})
        self.assertEqual(record["data"]["state"], "up")
        self.assertEqual(record["data"]["source"], {"kind": "terminal", "terminal_id": tid})
        for forbidden in ("24620", "/app/", "hidden", self.WINDOW):
            self.assertNotIn(forbidden, json.dumps(record))
        self.assertTrue(events.empty(), "one record for unattached registration")
        for kw in ({"origin": None}, {"origin": "http://evil.example"},
                   {"port": self.daemon.port}, {"port": self.daemon.workshop_port},
                   {"host": f"evil.example:{self.daemon.face_port}"}):
            self.assertIn(self.request("GET", "/dev-servers", **kw)[0], (403, 404))
        result = self.owner("POST", f"/dev-servers/{server['id']}/claim",
                            {"window": self.WINDOW, "shows": True, "focused_at": 1})
        self.assertEqual(result, {"won": True, "url": server["url"]})
        again = self.connect(tid)
        again.until(lambda c: "replayed" in c.kinds(), what="replay")
        again.drain(.2)
        self.assertEqual(self.dev_urls(again), [])

    def test_repeat_banner_revives_retained_source_without_claim_or_second_frame(self):
        watch = self.daemon.devservers
        now = [1000.0]
        watch.clock = lambda: now[0]
        with patch.object(watch, "_ensure_thread"):
            for index, prior in enumerate(("up", "placed", "dismissed", "claimed")):
                with self.subTest(prior=prior):
                    terminal = T.Terminal(f"{index + 10:08x}", shell="/bin/sh", folder="/",
                                          project_id=None, label="~", cols=80, rows=24, nonce=NONCE)
                    self.addCleanup(terminal.finish)
                    terminal._sniffer = devurl.DevUrlSniffer(lambda: set())
                    terminal._dev_on = lambda: True
                    terminal._dev_announce = watch.announce_terminal
                    terminal._dev_release = watch.terminal_delivery_failed
                    terminal._dev_send = watch.deliver_terminal
                    port = 24630 + index
                    line = f"  Local: http://localhost:{port}/first/\r\n".encode()
                    terminal._output(line)             # unattached: retained and deduplicated
                    entry = watch._entries[port]
                    ident = entry.id
                    with watch._lock:
                        entry.state = prior
                        now[0] += 5
                        watch._liveness(set(), now[0])
                        watch._liveness(set(), now[0])
                    self.assertFalse(entry.listening)
                    frames = []
                    terminal._attachment_window = self.WINDOW
                    terminal._attachment = SimpleNamespace(
                        send_binary=lambda d: True,
                        send_text=lambda t: frames.append(json.loads(t)) or True,
                        closed=threading.Event())
                    now[0] += 10
                    terminal._output(f"  Local: http://localhost:{port}/new-path/\r\n".encode())
                    self.assertTrue(entry.listening, "a fresh repeated banner revives the registry")
                    self.assertEqual(entry.id, ident)
                    self.assertEqual(entry.path, "/first/")
                    self.assertEqual(entry.state, "up" if prior == "claimed" else prior)
                    self.assertEqual(entry.source, {"kind": "terminal", "terminal_id": terminal.id})
                    self.assertIsNone(entry.window)
                    self.assertIsNone(entry.delivery)
                    self.assertEqual(entry.first_seen, now[0])
                    self.assertEqual(frames, [], "registry refresh never preclaims or places again")
                    with watch._lock:
                        watch._liveness(set(), now[0])
                        watch._liveness(set(), now[0])
                    self.assertTrue(entry.listening, "the restart gets its startup grace")

    def test_repeat_after_tombstone_expiry_registers_unclaimed_under_normal_gates(self):
        from jarvis.v2 import devservers
        watch = self.daemon.devservers
        now = [1000.0]
        watch.clock = lambda: now[0]
        terminal = T.Terminal("0000000e", shell="/bin/sh", folder="/", project_id=None,
                              label="~", cols=80, rows=24, nonce=NONCE)
        self.addCleanup(terminal.finish)
        terminal._sniffer = devurl.DevUrlSniffer(lambda: set())
        terminal._dev_on = lambda: True
        terminal._dev_announce = watch.announce_terminal
        terminal._dev_release = watch.terminal_delivery_failed
        terminal._dev_send = watch.deliver_terminal
        line = b"  Local: http://localhost:24635/app/\r\n"
        with patch.object(watch, "_ensure_thread"):
            terminal._output(line)
            entry = watch._entries[24635]
            ident = entry.id
            with watch._lock:
                now[0] += 5
                watch._liveness(set(), now[0])
                watch._liveness(set(), now[0])
            now[0] += devservers.DOWN_KEEP_S + 1
            frames = []
            terminal._attachment_window = self.WINDOW
            terminal._attachment = SimpleNamespace(
                send_binary=lambda d: True, send_text=lambda t: frames.append(json.loads(t)) or True,
                closed=threading.Event())
            with patch.object(watch, "enabled", return_value=False):
                terminal._output(line)
                self.assertIs(watch._entries[24635], entry, "off creates or revives nothing")
                self.assertFalse(entry.listening)
            with patch.object(devservers, "ENTRIES_MAX", 0):
                terminal._output(line)
                self.assertEqual(watch.listing()["servers"], [], "entry cap still applies")
            with patch.object(devservers, "SOURCE_RATE_S", 1000):
                watch._rates[("terminal", terminal.id)] = now[0]
                terminal._output(line)
                self.assertEqual(watch.listing()["servers"], [], "source rate still applies")
            with patch.object(devservers, "GLOBAL_RATE_MAX", 0):
                terminal._output(line)
                self.assertEqual(watch.listing()["servers"], [], "global rate still applies")
            terminal._output(line)
            self.assertIn(24635, watch._entries, "a fresh post-expiry banner is discoverable")
            fresh = watch._entries[24635]
            self.assertNotEqual(fresh.id, ident)
            self.assertTrue(fresh.listening)
            self.assertEqual((fresh.state, fresh.window, fresh.delivery), ("up", None, None))
            self.assertEqual(fresh.source, {"kind": "terminal", "terminal_id": terminal.id})
            self.assertEqual(watch.listing()["servers"][0]["url"], "http://localhost:24635/app/")
            self.assertEqual(frames, [], "no duplicate frame even with an attached window")

    def test_duplicate_from_a_different_terminal_cannot_revive_the_first_source(self):
        watch = self.daemon.devservers
        now = [1000.0]
        watch.clock = lambda: now[0]
        with patch.object(watch, "_ensure_thread"):
            ident = watch.announce_terminal("first", "http://localhost:24634/first/")
            entry = watch._entry(ident)
            with watch._lock:
                entry.state = "dismissed"
                now[0] += 5
                watch._liveness(set(), now[0])
                watch._liveness(set(), now[0])
            terminal = T.Terminal("0000000f", shell="/bin/sh", folder="/", project_id=None,
                                  label="~", cols=80, rows=24, nonce=NONCE)
            self.addCleanup(terminal.finish)
            terminal._sniffer = devurl.DevUrlSniffer(lambda: set())
            terminal._sniffer.sent("http://localhost:24634/other/")
            terminal._dev_on = lambda: True
            terminal._dev_announce = watch.announce_terminal
            terminal._output(b"  Local: http://localhost:24634/other/\r\n")
            self.assertFalse(entry.listening)
            self.assertEqual((entry.state, entry.prior_state), ("down", "dismissed"))
            self.assertEqual(entry.source, {"kind": "terminal", "terminal_id": "first"})
            self.assertEqual(entry.path, "/first/")

    def test_legacy_attach_still_sends_url_but_leaves_entry_unclaimed(self):
        tid = self.open_terminal()["id"]
        client = self.connect(tid, window=None)
        self.at_prompt(client)
        self.run_line(client, "echo  Local: http://localhost:24621/")
        self.wait_dev_url(client, "http://localhost:24621/")
        self.assertEqual(self.dev_urls(client), [{"type": "dev_url", "url": "http://localhost:24621/"}])
        entry, = self.owner("GET", "/dev-servers")["servers"]
        self.assertEqual((entry["state"], entry["claimed"]), ("up", False))

    def test_attach_window_is_strict_and_bad_query_does_not_spend_ticket(self):
        tid = self.open_terminal()["id"]
        ticket = self.ticket(tid)
        for window in ("", "a" * 16, "A" * 32, "g" * 32, "a" * 33,
                       self.WINDOW + "&window=" + self.WINDOW):
            self.assertEqual(self.refused(tid, ticket, window=window), 400)
        for kw in ({"origin": None}, {"origin": "null"}, {"origin": "http://evil.example"},
                   {"host": f"evil.example:{self.daemon.face_port}"}):
            self.assertEqual(self.refused(tid, ticket, **kw), 403)
        client = self.connect(tid, ticket)
        client.until(lambda c: "replayed" in c.kinds(), what="valid unchanged ticket")

    def test_takeover_binds_banners_to_new_socket_window(self):
        tid, first = self.session()
        second = self.connect(tid, window="b" * 32)
        second.until(lambda c: "waiting" in c.kinds(), what="takeover waiting")
        first.until(lambda c: "takeover_request" in c.kinds(), what="takeover request")
        request = next(m for m in first.messages if m["type"] == "takeover_request")
        first.conn.send(json.dumps({"type": "takeover", "id": request["id"], "allow": True}))
        second.until(lambda c: "replayed" in c.kinds(), what="takeover replay")
        self.run_line(second, "echo  Local: http://localhost:24622/")
        self.wait_dev_url(second, "http://localhost:24622/")
        frame, = self.dev_urls(second)
        self.assertEqual(self.daemon.devservers._entry(frame["id"]).window, "b" * 32)
        first.drain(.1)
        self.assertEqual(self.dev_urls(first), [])

    def test_registry_callback_can_reenter_terminal_and_failed_send_releases_only_its_claim(self):
        terminal = T.Terminal("0000000d", shell="/bin/sh", folder="/", project_id=None, label="~",
                              cols=80, rows=24, nonce=NONCE)
        self.addCleanup(terminal.finish)
        watch = self.daemon.devservers
        other = watch.announce_terminal("other", "http://localhost:24623/", window=self.WINDOW)
        terminal._sniffer = devurl.DevUrlSniffer(lambda: set())
        terminal._dev_on = lambda: bool(terminal.history())
        def announce(*a, **kw):
            terminal.history()                    # would deadlock under terminal._lock
            return watch.announce_terminal(*a, **kw)
        terminal._dev_announce = announce
        terminal._dev_release = watch.terminal_delivery_failed
        terminal._dev_send = watch.deliver_terminal
        terminal._attachment_window = self.WINDOW
        terminal._attachment = SimpleNamespace(send_binary=lambda d: True, send_text=lambda t: False,
                                               closed=threading.Event())
        worker = threading.Thread(target=terminal._output,
                                  args=(b"  Local: http://localhost:24624/\r\n",), daemon=True)
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive(), "registry callbacks must not hold the terminal lock")
        entry = next(e for e in watch._entries.values() if e.port == 24624)
        self.assertEqual((entry.state, entry.window), ("up", None))
        self.assertEqual(watch._entry(other).state, "claimed")
        self.assertEqual(len(terminal._sniffer._recent), 0)
        frames = []
        terminal._attachment.send_text = lambda t: frames.append(json.loads(t)) or True
        terminal._output(b"  Local: http://localhost:24624/\r\n")
        self.assertEqual(frames, [{"type": "dev_url", "url": "http://localhost:24624/", "id": entry.id}])
        self.assertEqual((entry.state, entry.window), ("claimed", self.WINDOW))

    def test_attachment_change_during_registration_never_sends_a_stale_frame(self):
        terminal = T.Terminal("0000000a", shell="/bin/sh", folder="/", project_id=None, label="~",
                              cols=80, rows=24, nonce=NONCE)
        self.addCleanup(terminal.finish)
        watch = self.daemon.devservers
        terminal._sniffer = devurl.DevUrlSniffer(lambda: set())
        terminal._dev_on = lambda: True
        first_frames, second_frames = [], []
        first = SimpleNamespace(send_binary=lambda d: True,
                                send_text=lambda t: first_frames.append(json.loads(t)) or True,
                                closed=threading.Event())
        second = SimpleNamespace(send_binary=lambda d: True,
                                 send_text=lambda t: second_frames.append(json.loads(t)) or True,
                                 closed=threading.Event())
        terminal._attachment, terminal._attachment_window = first, self.WINDOW
        def announce(*a, **kw):
            ident = watch.announce_terminal(*a, **kw)
            terminal.detach(first)
            self.assertIsNone(terminal.attach(second, 0, window="b" * 32))
            return ident
        terminal._dev_announce = announce
        terminal._dev_release = watch.terminal_delivery_failed
        terminal._dev_send = watch.deliver_terminal
        terminal._output(b"  Local: http://localhost:24625/\r\n")
        self.assertEqual(first_frames, [])
        self.assertFalse(any(m["type"] == "dev_url" for m in second_frames))
        entry, = watch._entries.values()
        self.assertEqual((entry.state, entry.window), ("up", None))
        self.assertEqual(len(terminal._sniffer._recent), 0)

    def test_a_claim_lost_before_queueing_never_sends_its_preclaimed_frame(self):
        terminal = T.Terminal("00000009", shell="/bin/sh", folder="/", project_id=None, label="~",
                              cols=80, rows=24, nonce=NONCE)
        self.addCleanup(terminal.finish)
        watch = self.daemon.devservers
        terminal._sniffer = devurl.DevUrlSniffer(lambda: set())
        terminal._dev_on = lambda: True
        frames = []
        terminal._attachment_window = self.WINDOW
        terminal._attachment = SimpleNamespace(send_binary=lambda d: True,
                                               send_text=lambda t: frames.append(json.loads(t)) or True,
                                               closed=threading.Event())
        def announce(*a, **kw):
            ident = watch.announce_terminal(*a, **kw)
            watch.window_ended(self.WINDOW)
            self.assertTrue(watch.claim(ident, "b" * 32, True, 0)[1]["won"])
            return ident
        terminal._dev_announce = announce
        terminal._dev_release = watch.terminal_delivery_failed
        terminal._dev_send = watch.deliver_terminal
        terminal._output(b"  Local: http://localhost:24626/\r\n")
        self.assertEqual(frames, [])
        entry, = watch._entries.values()
        self.assertEqual((entry.state, entry.window), ("claimed", "b" * 32))
        self.assertEqual(len(terminal._sniffer._recent), 0)

    def test_floods_stay_well_above_what_xterm_draws(self):
        # PR #70 review: 50 MB of "Network: http://192.168…" lines went through
        # `_output` at 8.9 MB/s with the sniffer (543 without). xterm draws
        # ~14 MB/s; every flood must stay at twice that, through `_output`
        # with a synchronous fake socket that takes everything. Measure calling-thread
        # CPU throughput so concurrent suites cannot spend this budget by descheduling
        # it, at this machine's idle speed (`tests/loadclock.py`: they slow its CPU
        # too); also check elapsed wall time when it returns. Best of three runs each.
        # With colour taken off the bulk path (`_PLAIN` without its SGR branch) the
        # colour flood runs at ~7 MB/CPU-s, and fails here under load as alone.
        mb = 1 << 20
        floor = 28.0                    # MB/CPU-s at idle speed: the guard's strength
        loadclock.pinned()
        floods = {
            "network URLs": b"  Network: http://192.168.1.20:5173/assets/app.js 200 OK\r\n",
            "plain logs": b"2026-10-12 10:00:00 INFO worker-3 processed job 12345 in 4.2 ms\r\n",
            "loopback access log": b"GET http://localhost:3000/api/items?page=2 200 12ms\r\n",
            "colour": b"\x1b[32mok\x1b[0m test \x1b[1mname\x1b[22m passed in \x1b[33m3ms\x1b[0m\r\n",
            "announcements": b"  Local:   http://localhost:3000/\r\n",
        }
        sock = SimpleNamespace(send_binary=lambda d: True, send_text=lambda t: True,
                               closed=threading.Event())
        for name, line in floods.items():
            data = line * (12 * mb // len(line))
            best = 0.0
            for _ in range(3):
                if best > floor:
                    break
                terminal = T.Terminal("0000000b", shell="/bin/sh", folder="/", project_id=None,
                                      label="~", cols=80, rows=24, nonce=NONCE, integration="none")
                terminal._attachment = sock
                terminal._sniffer = devurl.DevUrlSniffer(lambda: set())
                terminal._dev_on = lambda: True
                wall = time.perf_counter()
                started = time.thread_time()
                for i in range(0, len(data), 65536):
                    terminal._output(data[i:i + 65536])
                elapsed = time.thread_time() - started
                self.assertGreater(elapsed, 0, "CPU clock must measure the 12 MB workload")
                self.assertLess(time.perf_counter() - wall, 20.0, f"{name}: elapsed wall limit")
                held = loadclock.judge(elapsed, len(data) / mb / floor)
                best = max(best, len(data) / mb / held.idle)
                terminal.finish()
            with self.subTest(flood=name):
                self.assertGreater(best, floor, f"{name}: {best:.1f} MB/CPU-s at idle speed; last run {held}")

    def test_a_sniffer_that_fails_never_breaks_the_terminal(self):
        terminal = T.Terminal("0000000f", shell="/bin/sh", folder="/", project_id=None, label="~",
                              cols=80, rows=24, nonce=NONCE, integration="posix")
        self.addCleanup(terminal.finish)

        class Broken(devurl.DevUrlSniffer):
            def feed(self, data):
                raise RuntimeError("zq-sniffer-text " + data.decode())
        terminal._sniffer = Broken(lambda: set())
        terminal._dev_on = lambda: True
        sent = []
        terminal._attachment = SimpleNamespace(send_binary=lambda d: sent.append(d) or True,
                                               send_text=lambda t: sent.append(t) or True,
                                               closed=threading.Event())
        with capture_logs() as records:
            terminal._output(b"  Local: http://localhost:24615/\r\n")
            terminal._output(b"more\r\n")
        self.assertEqual(sent, [b"  Local: http://localhost:24615/\r\n", b"more\r\n"])
        self.assertIsNone(terminal._sniffer)
        self.assertIn("dev-server detection stopped (RuntimeError)", "\n".join(records))
        self.assertNotIn("zq-sniffer-text", "\n".join(records))


class NoTool(Base):
    """No tool, MCP tool, fast-path tool or Discord verb reaches a terminal —
    but one: `terminal_read` (WP-F), which imports exactly `read_for_tool`."""

    NEEDLES = re.compile(r"""['"]/terminals|\.terminals\b|import\s+terminals\b|\bterminals\s+import|"""
                         r"""jarvis\.v2\.terminals|\bTerminals\(|\bterminals\.(route|serve|create)""")
    # vex.py (VX-1) creates program terminals, from its own argv tables, behind
    # owner-only routes; tests/v2/vex_check.py holds it to that.
    # vex_connection.py is the VEX USB-presence half split out of vex.py
    # (5b0ac8c); only vex.py imports it, and no tool reaches either.
    ALLOWED = {"jarvis/v2/daemon.py", "jarvis/v2/hud_api.py", "jarvis/v2/terminals.py",
               "jarvis/v2/vex.py", "jarvis/v2/vex_connection.py"}
    # The one reader, and the one line it may hold.
    READER = "jarvis/v2/tools/terminal_read.py"
    READER_IMPORT = "from ..terminals import read_for_tool\n"
    # What `read_for_tool` and everything it calls must never do.
    WRITES = ("queue_input", "_write(", "resize(", ".control(", "attach(", "ticket(", ".create(",
              "self.close(", "terminals.close(", "terminal.close(", "hangup", "spawn(", "send_text",
              "send_binary", "readable =", "redeem(", "os.write", "_terminals.pop", "serve(", "finish(")

    def _without_reader_import(self, text: str) -> str:
        self.assertEqual(text.count(self.READER_IMPORT), 1, "the reader imports read_for_tool once")
        return text.replace(self.READER_IMPORT, "")

    def _without_browser_readable_call(self, text: str) -> str:
        # BR5 and BR9b each get one boolean source-readability fact, never a terminal
        # handle/output/control capability. Pin receiver and argument exactly;
        # the rest of the module still passes the ordinary terminal needles.
        import ast
        call = "self.daemon.terminals.readable_source(source.terminal_id)"
        tree = ast.parse(text)
        receivers = [ast.unparse(node) for node in ast.walk(tree)
                     if isinstance(node, ast.Attribute) and node.attr == "terminals"]
        self.assertEqual(receivers, ["self.daemon.terminals"])
        calls = [ast.unparse(node) for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "readable_source"]
        self.assertEqual(calls, [call])
        self.assertEqual(text.count(call), 1)
        return text.replace(call, "True")

    def test_no_tool_is_named_for_a_terminal(self):
        from jarvis import tools
        from jarvis.v2 import mcp, session_tools
        from jarvis.v2.providers import fastpath
        reachable = (set(fastpath.FAST_TOOLS) | set(mcp.MCP_TOOLS)
                     | set(session_tools.SESSION_TOOLS) | set(tools.REGISTRY))
        self.assertTrue({"task_propose", "schedule_delete"} <= reachable)
        self.assertEqual(sorted(n for n in reachable if "terminal" in n.lower()), ["terminal_read"])
        # The standalone MCP server excludes it; the authenticated session
        # catalog offers only this same reader, never a terminal-write tool.
        self.assertNotIn("terminal_read", mcp.MCP_TOOLS)
        self.assertFalse(tools.REGISTRY["terminal_read"].dangerous)

    def test_nothing_but_the_daemon_and_its_routes_reaches_the_module(self):
        repo = Path(__file__).resolve().parents[2]
        offenders = []
        for path in sorted((repo / "jarvis").rglob("*.py")):
            rel = path.relative_to(repo).as_posix()
            if rel in self.ALLOWED:
                continue
            text = path.read_text(encoding="utf-8")
            if rel == self.READER:
                text = self._without_reader_import(text)
            if rel in {"jarvis/v2/browser/look.py", "jarvis/v2/browser/drive.py"}:
                text = self._without_browser_readable_call(text)
            if self.NEEDLES.search(text):
                offenders.append(rel)
        self.assertEqual(offenders, [])
        # And the tool modules the agent actually runs, by import.
        from jarvis import tools
        from jarvis.v2 import mcp
        from jarvis.v2.providers import fastpath
        modules = {mcp, fastpath}
        for name in set(fastpath.FAST_TOOLS) | set(mcp.MCP_TOOLS):
            func = getattr(tools.REGISTRY.get(name), "func", None)
            if func is not None:
                modules.add(inspect.getmodule(func))
        for module in modules:
            text = inspect.getsource(module)
            if module.__name__ == "jarvis.v2.tools.terminal_read":
                text = self._without_reader_import(text)
                # It reaches the terminals through one call of that one function.
                self.assertEqual(len(re.findall(r"\bread_for_tool\(", text)), 1)
            self.assertIsNone(self.NEEDLES.search(text), module.__name__)

    def test_the_reader_only_reads(self):
        reader = [T.read_for_tool, T.Terminals.readable_source, T.Terminals.read, T.Terminals._resolve,
                  T.Terminals._read_published, T.Terminal.history, T._secret_values,
                  T._env_values, T._folder_env_files]
        for func in reader:
            source = inspect.getsource(func)
            for verb in self.WRITES:
                self.assertNotIn(verb, source, f"{func.__qualname__} calls {verb}")

    def test_the_needles_would_catch_a_tool_that_reached_in(self):
        for text in ('client.post("/terminals", ...)', "daemon.terminals.create(x)",
                     "from jarvis.v2 import terminals", "from .terminals import Terminals"):
            self.assertIsNotNone(self.NEEDLES.search(text), text)
        self.assertIsNone(self.NEEDLES.search("from .model import TERMINAL_STATES"))


# -- shell integration -------------------------------------------------------------------

NONCE = "0123456789abcdef0123456789abcdef"


def mark(params, nonce=NONCE, st=b"\x1b\\"):
    return b"\x1b]133;" + params + (b";jarvis=" + nonce.encode() if nonce else b"") + st


def stream():
    """A two-command session, with the offsets each command's output lies at."""
    parts = [mark(b"A"), b"$ ", mark(b"B"), b"env | grep X\r\n",
             mark(b"C;cmdline_url=env%20%7C%20grep%20X"), b"X=secret-ish\r\n", mark(b"D;0"),
             mark(b"A"), b"$ ", mark(b"B", st=b"\x07"), b"echo hi\r\n",
             mark(b"C;cmdline_url=echo%20hi", st=b"\x07"),
             mark(b"C;cmdline_url=forged"), b"",                    # (replaced below: wrong nonce)
             b"hi\r\n", mark(b"D;1"), mark(b"A"), b"$ "]
    parts[12] = mark(b"C;cmdline_url=forged", nonce="f" * 32) + mark(b"C;cmdline_url=bare", nonce="")
    return b"".join(parts)


class Marks(unittest.TestCase):
    def spans(self, chunks):
        marks = T.Marks(NONCE)
        for chunk in chunks:
            marks.feed(chunk)
        return marks

    def check(self, marks, data):
        spans = list(marks.spans)
        self.assertTrue(marks.integrated)
        self.assertEqual([s.command for s in spans], ["env | grep X", "echo hi"])
        self.assertEqual([s.exit_code for s in spans], [0, 1])
        self.assertEqual(data[spans[0].start:spans[0].end], b"X=secret-ish\r\n")
        out = data[spans[1].start:spans[1].end]
        self.assertEqual(OSC.sub(b"", out), b"hi\r\n")                         # forged marks: ignored
        self.assertIsNotNone(spans[0].prompt)

    def test_whole_and_split_at_every_byte(self):
        data = stream()
        self.check(self.spans([data]), data)
        self.check(self.spans([data[i:i + 1] for i in range(len(data))]), data)
        for cut in range(1, len(data)):
            with self.subTest(cut=cut):
                self.check(self.spans([data[:cut], data[cut:]]), data)

    def test_marks_without_the_nonce_mean_nothing(self):
        marks = self.spans([mark(b"C;cmdline_url=x", nonce="f" * 32), b"out", mark(b"D;0", nonce="")])
        self.assertEqual(list(marks.spans), [])
        self.assertFalse(marks.integrated)

    def test_overlong_aborted_and_garbage_marks_do_not_derail_parsing(self):
        signed = b";jarvis=" + NONCE.encode()
        data = (b"\x1b]133;C;cmdline_url=" + b"a" * (T.MARK_CAP + 10) + signed + b"\x1b\\"
                + b"\x1b]133;C;cmdline_url=x" + signed + b"\x1bXjunk"
                + b"\x1b]133;C;cmdline_url=y" + signed + b"\x18"
                + mark(b"C;cmdline_url=ok") + b"output\n" + mark(b"D;0"))
        for chunks in ([data], [data[i:i + 5] for i in range(0, len(data), 5)]):
            marks = self.spans(chunks)
            self.assertEqual([(s.command, s.exit_code) for s in marks.spans], [("ok", 0)])

    def test_prune_and_history_keep_only_what_the_ring_holds(self):
        marks = self.spans([stream()])
        marks.prune(10_000)
        self.assertEqual(list(marks.spans), [])

    def test_evicted_spans_leave_their_bytes_unattributed(self):
        self.assertEqual(T.SPAN_CAP, 2000)
        data = b""
        with patch.object(T, "SPAN_CAP", 3):
            marks = T.Marks(NONCE)
            for i in range(5):
                chunk = (mark(b"A") + b"$ " + mark(b"C;cmdline_url=c%d" % i) + b"out%d\n" % i
                         + mark(b"D;0"))
                marks.feed(chunk)
                data += chunk
        self.assertEqual([s.command for s in marks.spans], ["c2", "c3", "c4"])
        # c0 and c1 are gone, but their output may still be in the ring: every
        # byte before the end of c1's output is unattributed now.
        self.assertEqual(marks.spans_from, data.index(b"out1\n") + len(b"out1\n"))
        self.assertLessEqual(marks.spans_from, marks.spans[0].start)


class SessionReuse(unittest.TestCase):
    """A session id outlives its processes only as a number: once the last
    member is gone it can lead somebody else's session. A fake /proc."""

    def setUp(self):
        self.stats: dict[int, tuple] = {}
        self.kills: list[tuple[int, int]] = []
        for patcher in (patch.object(T, "_proc_stat", lambda pid: self.stats.get(pid)),
                        patch.object(T, "_proc_pids", lambda: list(self.stats)),
                        patch.object(os, "kill", lambda pid, sig: self.kills.append((pid, sig)))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def terminal(self, start=1000):
        terminal = T.Terminal("0000000d", shell="/bin/sh", folder="/", project_id=None, label="~",
                              cols=80, rows=24, nonce="0" * 32)
        terminal._proc = SimpleNamespace(pid=4242, poll=lambda: 0, wait=lambda timeout=None: 0,
                                         returncode=0)
        terminal._start_time = start
        return terminal

    def test_live_members_are_found_by_session(self):
        self.stats.update({4242: (b"S", 4242, 1000), 4300: (b"S", 4242, 2000),
                           4301: (b"Z", 4242, 2001), 4400: (b"S", 4400, 3000)})
        terminal = self.terminal()
        self.assertEqual(terminal._members(), [4242, 4300])                    # never the zombie
        terminal.hangup("closed")
        self.assertEqual({pid for pid, _ in self.kills}, {4242, 4300})
        terminal.finish()

    def test_a_reused_session_number_is_never_signalled(self):
        # The shell is gone; its number now leads an unrelated session.
        self.stats.update({4242: (b"S", 4242, 5555), 4243: (b"S", 4242, 5556)})
        terminal = self.terminal(start=1000)
        self.assertEqual(terminal._members(), [])
        terminal.hangup("closed")
        terminal.finish()
        self.assertEqual(self.kills, [])

    def test_a_session_seen_empty_stays_empty(self):
        terminal = self.terminal()
        self.assertEqual(terminal._members(), [])                               # nothing left
        # Later a session with that number (and, by bad luck, that start
        # time) appears: still never signalled.
        self.stats.update({4242: (b"S", 4242, 1000), 4243: (b"S", 4242, 1001)})
        self.assertEqual(terminal._members(), [])
        terminal.hangup("closed")
        terminal.finish()
        self.assertEqual(self.kills, [])


class FakeShellMarks(Base):
    def test_spans_from_the_fake_shell_and_forged_marks_ignored(self):
        tid, client = self.session()
        for line in ("echo one", "forge", "echo two"):
            client.type(line)
        # Typed ahead, so no line of output starts after a newline the tty
        # echoed: wait for the spans themselves.
        terminal = self.term(tid)
        eventually(lambda: len(terminal.history().spans) == 3
                   and terminal.history().spans[-1].exit_code == 0, what="the last D mark")
        history = terminal.history()
        self.assertTrue(history.integrated)
        self.assertEqual(history.spans_from, 0)
        self.assertIsNotNone(history.prompt)
        self.assertEqual([s.command for s in history.spans], ["echo one", "forge", "echo two"])

        def out(span):
            return OSC.sub(b"", history.data[span.start - history.start:span.end - history.start])
        self.assertEqual(out(history.spans[0]), b"one\r\n")
        self.assertEqual(out(history.spans[1]), b"forged output\r\n")
        self.assertTrue(history.readable)


class OtherShells(Base):
    """A shell that reads no $ENV (zsh, fish, …) runs as a plain login shell:
    no startup file, so no `sudo -k` and no marks — and the listing says so."""

    def test_a_shell_without_env_runs_unintegrated(self):
        fish = self.root / "bin" / "fish"
        fish.parent.mkdir()
        shutil.copy(FAKE_SHELL, fish)                          # a copy named fish, not a link
        fish.chmod(0o755)
        with patch.object(config, "TERMINAL_SHELL", str(fish)):
            row = self.open_terminal()
        self.assertEqual((row["integration"], row["title"]), ("none", "fish · Calc"))
        tid, client = self.session(row["id"])
        client.type("names")
        names = client.until(lambda c: re.search(r"NAMES (.*?) END", c.text()), what="names")
        self.assertNotIn("ENV", names.group(1).split())
        client.type("echo plain")
        client.wait_line("plain")
        history = self.term(tid).history()
        self.assertFalse(history.integrated)
        self.assertIsNone(history.prompt)
        self.assertEqual(history.spans, ())
        self.assertFalse(list(self.root.rglob("rc-*")), "a startup file on disk")


# A child process hunting for the nonce everywhere a program could ordinarily
# look: its environment, its parent shell's argv and initial environment
# (/proc), every file either of those names, and every pipe or file the shell
# holds open (/proc/<shell>/fd — where dash kept its deleted startup file
# while ~/.profile ran). It is run twice: from ~/.profile, while the startup
# file is being read, and later as a command. It writes what it found to a
# file for the test to search; nothing is printed.
HUNT = r'''
import os, sys
dump = sys.argv[1]
parent = os.getppid()
seen = ["\n".join(f"{k}={v}" for k, v in os.environ.items())]
for name in ("environ", "cmdline"):
    try:
        with open(f"/proc/{parent}/{name}", "rb") as handle:
            seen.append(handle.read().decode("latin-1"))
    except OSError:
        pass
paths = set()
for text in list(seen):
    for token in text.replace("\0", "\n").replace("=", "\n").replace(":", "\n").split("\n"):
        if token.startswith("/"):
            paths.add(token)
fd_dir = f"/proc/{parent}/fd"
try:
    for name in os.listdir(fd_dir):
        try:
            link = os.readlink(os.path.join(fd_dir, name))
        except OSError:
            continue
        if link.startswith("pipe:") or (link.startswith("/") and not link.startswith("/dev/")):
            paths.add(os.path.join(fd_dir, name))
except OSError:
    pass
for path in sorted(paths):
    try:
        if path.startswith(fd_dir) or os.path.isfile(path):
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            try:
                seen.append(os.read(fd, 1 << 16).decode("latin-1"))
            except BlockingIOError:
                pass
            finally:
                os.close(fd)
    except OSError:
        pass
with open(dump + ".part", "w") as handle:
    handle.write("\n".join(seen))
os.replace(dump + ".part", dump)
'''


class NonceHunt:
    """Mixed into the real-shell cases: the nonce must be out of a child's
    reach — while the startup file is read and afterwards — and on no disk."""

    def profile(self):
        """A ~/.profile that exports PS1 and runs the hunt mid-startup."""
        self.hunter = self.root / "hunt.py"
        self.hunter.write_text(HUNT)
        self.early = self.root / "hunt-profile.txt"
        (self.home / ".profile").write_text(
            f"export PS1='custom$ '\npython3 {self.hunter} {self.early}\n")

    def hunt(self, client, terminal):
        nonce = terminal._marks._nonce
        late = self.root / "hunt.txt"
        client.type(f"python3 {self.hunter} {late}")
        eventually(late.exists, timeout=15, what="the hunt")
        eventually(self.early.exists, timeout=15, what="the hunt from ~/.profile")
        for dump in (self.early, late):
            found = dump.read_text(encoding="latin-1")
            # Booleans only: a failure must not print what the child read.
            self.assertTrue("HOME=" in found, f"{dump.name} read nothing")     # it really looked
            self.assertFalse(nonce.decode() in found, f"a child found the nonce ({dump.name})")
        for path in self.root.rglob("*"):                                       # TMPDIR is in here
            if path.is_file():
                self.assertFalse(nonce in path.read_bytes(), f"the nonce is on disk ({path.name})")


class RealBashStartupFile(NonceHunt, Base):
    """The shipped startup file, in real bash, with a temp HOME and no
    profile files of the owner's but one that exports PS1 and hunts: the
    marks WP-F depends on, `sudo -k`, a clean environment by name, and a
    nonce no program in the terminal can find."""

    def setUp(self):
        super().setUp()
        bindir = self.root / "bin"
        bindir.mkdir()
        (bindir / "bash").symlink_to("/bin/bash")              # named bash: --rcfile mode
        (bindir / "rbash").symlink_to("/bin/bash")             # named rbash: restricted
        self.bindir = bindir
        self.profile()
        guard = patch.object(config, "TERMINAL_SHELL", str(bindir / "bash"))
        guard.start()
        self.addCleanup(guard.stop)
        guard = patch.dict(os.environ, {"OPENROUTER_API_KEY": "placeholder-not-a-key",
                                        "HF_HUB_OFFLINE": "1", "VIRTUAL_ENV": str(self.root / "venv")})
        guard.start()
        self.addCleanup(guard.stop)

    def run_lines(self, client, terminal, lines):
        for line in lines:
            count = len(terminal.history().spans)
            client.type(line)
            eventually(lambda: len(terminal.history().spans) > count
                       and terminal.history().spans[-1].end is not None, timeout=10,
                       what=f"{line!r} finished")

    def test_bash_emits_marks_and_never_caches_sudo(self):
        tid = self.open_terminal()["id"]
        terminal = self.term(tid)
        self.assertEqual(terminal.row()["integration"], "bash")
        client = self.connect(tid)
        eventually(lambda: b"133;B;jarvis=" in terminal.history().data, timeout=15,
                   what="the first prompt")
        self.assertFalse(terminal.history().integrated)
        self.run_lines(client, terminal, [
            "echo hi there", "false", "alias sudo", "printf '%s\\n' 'a;b'",
            "HISTCONTROL=ignorespace", " echo hidden from history",
            "env | cut -d= -f1 | sort | tr '\\n' ' '; echo", "stty size"])
        history = terminal.history()
        self.assertTrue(history.integrated)
        spans = history.spans

        def out(span):
            return plain(history.data[span.start - history.start:span.end - history.start])
        commands = [s.command for s in spans]
        self.assertEqual(commands[:5], ["echo hi there", "false", "alias sudo",
                                        "printf '%s\\n' 'a;b'", "HISTCONTROL=ignorespace"])
        self.assertEqual(commands[5], "echo hidden from history")              # $BASH_COMMAND fallback
        self.assertEqual([s.exit_code for s in spans[:3]], [0, 1, 0])
        self.assertEqual(out(spans[0]).strip(), "hi there")
        self.assertIn("sudo='sudo -k'", out(spans[2]))
        self.assertEqual(out(spans[3]).strip(), "a;b")
        names = set(out(spans[6]).split())
        self.assertIn("HOME", names)
        self.assertFalse(names & {"OPENROUTER_API_KEY", "HF_HUB_OFFLINE", "VIRTUAL_ENV",
                                  "__jarvis_nonce", "ENV", "PS1"}, sorted(names))
        self.assertEqual(out(spans[7]).strip(), "24 80")
        nonce = re.search(rb"jarvis=([0-9a-f]{32})", history.data).group(1)
        self.assertNotIn(nonce.decode(), out(spans[6]))
        self.hunt(client, terminal)
        # A foreground job other than the shell reads as busy (the HUD asks before closing).
        eventually(lambda: not terminal.row()["busy"], what="idle")
        client.type("sleep 3")
        eventually(lambda: terminal.row()["busy"], what="busy")

    def test_marked_only_once_the_startup_file_runs(self):
        # `integration` is what was configured; `marked` is what took. A
        # profile that `exec`s another shell configures "bash" and gets no
        # marks and no `sudo -k` — the HUD says so from `marked` (PR #25
        # re-review).
        tid = self.open_terminal()["id"]
        eventually(lambda: self.term(tid).row()["marked"], timeout=15, what="the first signed prompt")
        if not os.access("/usr/bin/dash", os.X_OK):
            self.skipTest("no dash on this machine")
        (self.home / ".profile").write_text("exec /usr/bin/dash -i\n")
        row = self.open_terminal()
        self.assertEqual((row["integration"], row["marked"]), ("bash", False))
        client = self.connect(row["id"])
        # Typed ahead of dash's prompt, so a line may follow "$ ": the
        # arithmetic is what proves it ran (the echoed line holds no "42").
        client.type("echo started-$((40+2))")
        client.wait_text("started-42\n", timeout=10)
        client.type("alias sudo; echo done-$((1+1))")
        client.wait_text("done-2\n", timeout=10)
        self.assertNotIn("sudo -k", client.text())
        listed = next(r for r in self.owner("GET", "/terminals") if r["id"] == row["id"])
        self.assertEqual((listed["integration"], listed["marked"], listed["integrated"]), ("bash", False, False))
        self.assertNotIn("marked", client.kinds())

    def test_a_shell_keeps_the_name_it_was_given(self):
        # rbash is a link to bash; launched as its realpath under its own
        # name, it must still be restricted.
        with patch.object(config, "TERMINAL_SHELL", str(self.bindir / "rbash")):
            row = self.open_terminal()
        self.assertEqual((row["title"], row["integration"]), ("rbash · Calc", "bash"))
        terminal = self.term(row["id"])
        client = self.connect(row["id"])
        eventually(lambda: b"133;B;jarvis=" in terminal.history().data, timeout=15,
                   what="the first prompt")
        client.type("cd /")
        client.wait_text("restricted", timeout=10)
        client.type('echo "zero=$0"')
        client.wait_line(f"zero={self.bindir / 'rbash'}")


class RealDashStartupFile(NonceHunt, Base):
    """The POSIX startup file, in real dash (temp HOME, a ~/.profile that
    exports PS1 and hunts while the startup file is still open): it parses —
    a POSIX shell reads every line of it — it keeps sudo from caching, its
    prompt marks are well formed and carry the nonce, it marks no command,
    and the nonce is out of a child's reach and on no disk."""

    def test_dash_reads_the_posix_file(self):
        if not os.access("/usr/bin/dash", os.X_OK):
            self.skipTest("no dash on this machine")
        self.profile()
        with patch.object(config, "TERMINAL_SHELL", "/usr/bin/dash"):
            tid = self.open_terminal()["id"]
        terminal = self.term(tid)
        self.assertEqual(terminal.row()["integration"], "posix")
        client = self.connect(tid)
        eventually(lambda: terminal.history().prompt is not None, timeout=15,
                   what="a prompt mark, parsed")
        nonce = terminal._marks._nonce
        data = terminal.history().data
        self.assertIn(b"\x1b]133;A;jarvis=" + nonce + b"\x07custom$ \x1b]133;B;jarvis=" + nonce
                      + b"\x07", data)                                         # well formed, whole
        client.type("alias sudo")
        client.wait_text("sudo -k", timeout=10)
        client.output.clear()
        client.type("env | cut -d= -f1 | sed 's/^/N:/'; echo DONE-$((40+2))")
        client.wait_text("DONE-42\n")
        names = set(re.findall(r"^N:(\S+)$", client.text(), re.M))
        self.assertIn("HOME", names)
        # dash cannot un-export PS1 (the ~/.profile here exports it), so PS1
        # names the nonce instead of holding it: the hunt below proves it.
        self.assertFalse(names & {"ENV", "__jarvis_nonce"}, sorted(names))
        self.assertNotIn("Syntax error", client.text())
        self.hunt(client, terminal)
        history = terminal.history()
        self.assertFalse(history.integrated)
        self.assertEqual(history.spans, ())


if __name__ == "__main__":
    unittest.main(verbosity=2)
