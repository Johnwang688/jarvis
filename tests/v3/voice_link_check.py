"""Exercise the daemon link with fake sessions, private files, and loopback TCP."""

import ast
from contextlib import ExitStack
import hashlib
import hmac
import io
import json
import logging
import os
from pathlib import Path
import select
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen

import numpy as np

from jarvis import config
from jarvis.v3.daemon import Daemon
from jarvis.v3.voice import link as transport, wire


KEY_TEXT = b"a1" * 32
KEY = bytes.fromhex(KEY_TEXT.decode())
HELLO = {"features": ["test-audio"], "canceller": True, "version": "example-3"}
DISTINCT_WORDS = "example speech should stay inside the voice link"
DISTINCT_PCM = b"distinct-pcm" * 53 + b"test"


def until(check, timeout=3):
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for voice link state")
        time.sleep(0.01)


class Session:
    def __init__(self, *, on_event=None, on_audio=None):
        self.on_event, self.on_audio = on_event, on_audio
        self.calls = []

    def start(self):
        self.calls.append(("start",))

    def stop(self):
        self.calls.append(("stop",))

    def status(self):
        return {}

    def feed(self, raw, cancelled):
        self.calls.append(("feed", raw, cancelled))

    def key(self, down):
        self.calls.append(("key", down))

    def played(self, ident):
        self.calls.append(("played", ident))

    def playback_stopped(self):
        self.calls.append(("flushed",))


class Helper:
    def __init__(self, stack, address, *, small_buffer=False):
        self.sock = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        self.sock.settimeout(2)
        if small_buffer:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        self.sock.connect(address)
        self.stream = stack.enter_context(self.sock.makefile("rb"))

    def challenge(self, nonce=None):
        self.a = wire.new_nonce() if nonce is None else nonce
        self.sock.sendall(wire.json_line(wire.helper_hello(self.a)))
        return wire.read_json(self.stream)

    def authenticate(self, *, hello=True):
        k3 = wire.derive_k3(KEY)
        reply = self.challenge()
        self.d = wire.check_daemon_hello(k3, self.a, reply)
        self.ks = wire.derive_ks(k3, self.a, self.d)
        self.up = wire.FrameWriter(self.ks, wire.UP)
        self.down = wire.FrameReader(self.ks, wire.DOWN)
        helper_proof = wire.helper_proof(k3, self.a, self.d)
        self.proofs = reply["proof"], helper_proof["proof"]
        proof = wire.json_line(helper_proof)
        # Immediate HELLO can share a TCP write with the proof.
        if hello:
            proof += self.up.encode(wire.HELLO, wire.encode_payload(wire.HELLO, HELLO))
        self.sock.sendall(proof)
        return self

    def send(self, kind, value):
        self.sock.sendall(self.up.encode(kind, wire.encode_payload(kind, value)))

    def read(self):
        return self.down.read(self.stream)

    def disconnected(self):
        try:
            while self.sock.recv(65536):
                pass
        except ConnectionResetError:
            pass


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.stack = self.enterContext(ExitStack())
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}))
        self.path = self.home / "fake-pairing"

    def write_key(self):
        self.path.write_bytes(KEY_TEXT + b"\n")
        self.path.chmod(0o600)

    def make_link(self, *, factory=Session):
        self.link = transport.VoiceLink(factory, port=0, key_path=self.path)
        self.addCleanup(self.link.close)
        return self.link

    def start_link(self):
        self.write_key()
        link = self.make_link()
        self.assertTrue(link.start())
        self.session = link._session
        return link

    def helper(self):
        return Helper(self.stack, self.link.address)

    def connect(self, **kwargs):
        peer = self.helper().authenticate(**kwargs)
        until(lambda: self.link.status()["connected"])
        return peer

    def test_key_file(self):
        session = Session()
        link = self.make_link(factory=session)
        self.assertFalse(link.start())
        self.assertEqual(link.status(), {"listening": False, "connected": False, "reason": "key_missing"})
        for label, data, mode in (("group", KEY_TEXT, 0o640), ("world", KEY_TEXT, 0o604),
                                  ("short", KEY_TEXT[:-1], 0o600), ("junk", KEY_TEXT + b"\nx", 0o600)):
            with self.subTest(label=label):
                self.path.write_bytes(data)
                self.path.chmod(mode)
                self.assertFalse(link.start())
                self.assertEqual(link.status()["reason"], "key_invalid")
                self.assertIsNone(link.address)
        target = self.home / "fake-target"
        self.write_key()
        self.path.rename(target)
        self.path.symlink_to(target)
        self.assertFalse(link.start())
        self.assertEqual(link.status()["reason"], "key_invalid")
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        self.assertFalse(link.start())
        self.assertEqual(link.status()["reason"], "key_invalid")
        self.path.unlink()
        self.write_key()
        self.assertTrue(link.start())
        self.assertIs(link._session, session)
        self.assertEqual(link.address[0], "127.0.0.1")
        self.assertNotIn(link.port, range(8402, 8411))
        self.assertEqual(link.status(), {"listening": True, "connected": False})

    def test_key_inode_and_mode_are_rechecked_after_open(self):
        self.write_key()
        link = self.make_link()
        real_open = os.open

        def replace_then_open(path, flags):
            self.path.rename(self.home / "old-fake-pairing")
            self.write_key()
            return real_open(path, flags)

        with patch.object(transport.os, "open", side_effect=replace_then_open):
            self.assertFalse(link.start())
        self.assertEqual(link.status()["reason"], "key_invalid")

        def chmod_then_open(path, flags):
            self.path.chmod(0o644)
            return real_open(path, flags)

        with patch.object(transport.os, "open", side_effect=chmod_then_open):
            self.assertFalse(link.start())
        self.assertEqual(link.status()["reason"], "key_invalid")

    def test_wrong_reflected_and_replayed_proofs(self):
        self.start_link()
        k3 = wire.derive_k3(KEY)
        nonce = "12" * 32
        first = self.helper()
        reply = first.challenge(nonce)
        previous_d = wire.check_daemon_hello(k3, nonce, reply)
        replay = wire.helper_proof(k3, nonce, previous_d)
        first.sock.close()
        # Each proof is refused before any session method gets invoked.
        for variant in ("wrong", "reflected", "replay"):
            with self.subTest(variant=variant):
                peer = self.helper()
                reply = peer.challenge(nonce)
                d = wire.check_daemon_hello(k3, nonce, reply)
                self.assertNotEqual(d, previous_d)
                if variant == "wrong":
                    proof = wire.helper_proof(wire.derive_k3(b"b" * 32), nonce, d)
                elif variant == "reflected":
                    proof = {"proof": reply["proof"]}
                else:
                    proof = replay
                peer.sock.sendall(wire.json_line(proof))
                peer.disconnected()
        self.assertEqual(self.session.calls, [])

    def test_peer_nonce_cannot_equal_the_daemon_nonce(self):
        self.start_link()
        with patch.object(wire, "new_nonce", return_value="af" * 32):
            peer = self.helper()
            peer.sock.sendall(wire.json_line(wire.helper_hello("AF" * 32)))
            self.assertEqual(peer.sock.recv(1), b"")
        self.assertEqual(self.session.calls, [])

    def test_bad_handshake_lines_and_frame_before_proof(self):
        self.start_link()
        for invalid in (b" " * wire.MAX_LINE + b"\n", b"not-json\n", b"[]\n",
                        b'{"hello":"jarvis-voice3-helper","hello":"duplicate","v":3}\n'):
            with self.subTest(size=len(invalid)):
                peer = self.helper()
                peer.sock.sendall(invalid)
                self.assertEqual(peer.sock.recv(1), b"")
        peer = self.helper()
        reply = peer.challenge()
        k3 = wire.derive_k3(KEY)
        d = wire.check_daemon_hello(k3, peer.a, reply)
        up = wire.FrameWriter(wire.derive_ks(k3, peer.a, d), wire.UP)
        peer.sock.sendall(up.encode(wire.HELLO, wire.encode_payload(wire.HELLO, HELLO))
                          + wire.json_line(wire.helper_proof(k3, peer.a, d)))
        peer.disconnected()
        self.assertEqual(self.session.calls, [])

    def test_absolute_handshake_deadline_and_pending_limit(self):
        self.assertEqual((wire.HANDSHAKE_TIMEOUT_S, wire.MAX_UNAUTHENTICATED), (5, 4))
        self.start_link()
        start = time.monotonic()
        silent, trickle, *rest = [self.helper() for _ in range(4)]
        # Waiting for all four accepted sockets makes the fifth check independent of scheduling.
        until(lambda: self.link._pending == 4)
        self.helper().disconnected()
        rest[0].challenge()  # A completed first line does not restart the clock.
        trickle.sock.sendall(b"{")
        halfway = time.monotonic() + 2.5
        while time.monotonic() < halfway:
            trickle.sock.sendall(b" ")
            time.sleep(0.25)
        self.assertFalse(select_ready(silent.sock, 0))
        late = time.monotonic() + 3.5
        trickle_closed = False
        while time.monotonic() < late:
            try:
                trickle.sock.sendall(b" ")
                if not select_ready(trickle.sock, 0.15):
                    continue
                trickle.disconnected()
            except (BrokenPipeError, ConnectionResetError):
                pass
            trickle_closed = True
            break
        self.assertTrue(trickle_closed)
        self.assertGreaterEqual(time.monotonic() - start, 4.8)
        self.assertLess(time.monotonic() - start, 6.2)
        for peer in (silent, *rest):
            peer.disconnected()
        self.assertEqual(self.session.calls, [])
        self.connect()

    def test_only_an_authenticated_newcomer_replaces_the_active_helper(self):
        self.start_link()
        old = self.connect()
        until(lambda: self.link.status().get("helper_version") == HELLO["version"])
        pending = self.helper()
        pending.challenge()
        old.send(wire.PING, b"abcdefgh")
        self.assertEqual(old.read(), (wire.PONG, b"abcdefgh"))
        self.assertEqual(self.session.calls, [("start",)])
        new = self.helper().authenticate()
        new.send(wire.PING, b"ABCDEFGH")
        self.assertEqual(new.read(), (wire.PONG, b"ABCDEFGH"))
        old.disconnected()
        self.assertEqual(self.session.calls, [("start",), ("stop",), ("start",)])
        self.link.close()
        self.assertEqual(self.session.calls[-1], ("stop",))
        self.assertEqual(len(self.session.calls), 4)

    def test_invalid_frames_close_and_stop_once(self):
        self.start_link()
        payload = wire.encode_payload(wire.HELLO, HELLO)
        for i, variant in enumerate(("MAC", "skipped", "repeated", "type", "large control", "large audio",
                                     "HELLO missing", "HELLO repeated", "bad KEY"), 1):
            with self.subTest(variant=variant):
                peer = self.connect(hello=False)
                hello = peer.up.encode(wire.HELLO, payload)
                if variant == "MAC":
                    data = hello[:-1] + bytes((hello[-1] ^ 1,))
                elif variant == "skipped":
                    data = peer.up.encode(wire.HELLO, payload)
                elif variant == "repeated":
                    data = hello + hello
                elif variant in ("type", "large control", "large audio"):
                    kind, size = {"type": (0xfa, 0), "large control": (wire.HELLO, wire.MAX_CONTROL + 1),
                                  "large audio": (wire.AUDIO, wire.MAX_AUDIO + 1)}[variant]
                    data = struct.pack(">BII", kind, size, 0)  # Refuse before waiting for a body.
                elif variant == "HELLO missing":
                    data = wire.FrameWriter(peer.ks, wire.UP).encode(wire.KEY, b"\x01")
                elif variant == "HELLO repeated":
                    data = hello + peer.up.encode(wire.HELLO, payload)
                else:
                    data = hello + peer.up.encode(wire.KEY, b"\x02")
                peer.sock.sendall(data)
                peer.disconnected()
                until(lambda: len(self.session.calls) == i * 2)
                self.assertEqual(self.session.calls, [("start",), ("stop",)] * i)

    def test_incoming_audio_controls_and_private_exchange(self):
        with self.assertLogs(level=logging.WARNING) as captured:
            logging.getLogger(__name__).warning("Voice link test log capture is active.")
            self.start_link()
            peer = self.connect()
            raw = np.arange(-160, 160, dtype="<i2")
            cancelled = -raw
            peer.send(wire.AUDIO, (raw.tobytes(), cancelled.tobytes()))
            peer.send(wire.AUDIO, (DISTINCT_PCM, None))
            peer.send(wire.KEY, True)
            peer.send(wire.KEY, False)
            peer.send(wire.PLAYED, {"id": "fake-sentence"})
            peer.send(wire.FLUSHED, None)
            peer.send(wire.CLICK, {"id": "fake-card", "digest": "fake-digest", "decision": "approve"})
            peer.send(wire.LEVEL, 51)
            peer.send(wire.PING, b"example!")
            self.assertEqual(peer.read(), (wire.PONG, b"example!"))
            self.assertEqual(self.link.status(), {"listening": True, "connected": True, "level": 51,
                                                  "helper_version": HELLO["version"]})
            self.assertEqual(self.link.clicks, 1)
            _, first, second = self.session.calls[1]
            self.assertEqual(first.dtype, np.dtype("int16"))
            np.testing.assert_array_equal(first, raw)
            np.testing.assert_array_equal(second, cancelled)
            _, first, second = self.session.calls[2]
            self.assertEqual(first.tobytes(), DISTINCT_PCM)
            self.assertIsNone(second)
            self.assertEqual(self.session.calls[3:], [("key", True), ("key", False),
                                                      ("played", "fake-sentence"), ("flushed",)])
            self.session.on_event("say", id="example", text=DISTINCT_WORDS)
            self.session.on_audio("example", np.frombuffer(DISTINCT_PCM, dtype="<i2"), 16000, True)
            self.assertEqual(peer.read()[1]["text"], DISTINCT_WORDS)
            self.assertEqual(peer.read()[1][3], DISTINCT_PCM)
            self.link.close()
        for secret in (DISTINCT_WORDS.encode(), DISTINCT_PCM, KEY_TEXT, KEY, wire.derive_k3(KEY), peer.ks,
                       *(value.encode() for value in (peer.a, peer.d, *peer.proofs))):
            self.assertNotIn(secret, "\n".join(captured.output).encode())
            for file in self.home.rglob("*"):
                if file.is_file() and file != self.path:
                    self.assertNotIn(secret, file.read_bytes())
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(list(self.home.iterdir()), [self.path])
        self.assertEqual(self.path.read_bytes(), KEY_TEXT + b"\n")

    def test_session_exception_is_logged_without_peer_data(self):
        self.start_link()
        peer = self.connect()
        with patch.object(self.session, "played", side_effect=RuntimeError(DISTINCT_WORDS)):
            with self.assertLogs(transport.LOG, "WARNING") as captured:
                peer.send(wire.PLAYED, {"id": DISTINCT_WORDS})
                peer.disconnected()
        self.assertEqual([entry.message for entry in captured.records],
                         ["Voice link connection stopped after an internal error."])
        until(lambda: self.session.calls == [("start",), ("stop",)])

    def test_level_is_rate_limited(self):
        self.start_link()
        peer = self.connect()
        for value in range(10):
            peer.send(wire.LEVEL, value)
        peer.send(wire.PING, b"abcdefgh")
        self.assertEqual(peer.read(), (wire.PONG, b"abcdefgh"))
        self.assertEqual(self.link.status()["level"], 9)
        peer.send(wire.LEVEL, 10)
        peer.disconnected()
        until(lambda: self.session.calls == [("start",), ("stop",)])

    def test_outgoing_events_chunking_flags_and_index_reset(self):
        self.start_link()
        peer = self.connect()
        self.assertEqual(self.link._active.sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY), 1)
        self.session.on_event("notice", title="🟦" * 900, text="🟩" * 900)
        kind, notice = peer.read()
        self.assertEqual(kind, wire.EVENT)
        self.assertTrue(0 < len(notice["text"]) <= wire.MAX_TEXT)
        self.assertTrue(0 < len(notice["title"]) <= wire.MAX_TEXT)
        pcm = np.arange(7300, dtype="<i2")
        self.session.on_event("say", id="fake-one", text="a" * 1000)
        self.session.on_audio("fake-one", pcm, 16000, True)
        self.session.on_event("say", id="fake-two", text="second")
        self.session.on_audio("fake-two", pcm[:3200], 16000, True, last=False)
        self.session.on_audio("fake-two", pcm[3200:3210], 16000, False, last=True)
        self.assertEqual(peer.read(), (wire.EVENT, {"kind": "say", "id": "fake-one", "text": "a" * 600, "n": 0}))
        for start, stop, flags in ((0, 3200, wire.FIRST), (3200, 6400, 0), (6400, 7300, wire.LAST)):
            self.assertEqual(peer.read(), (wire.AUDIO_OUT, (0, flags, 16000, pcm[start:stop].tobytes())))
        self.assertEqual(peer.read()[1]["n"], 1)
        self.assertEqual(peer.read()[1][:3], (1, wire.FIRST, 16000))
        self.assertEqual(peer.read()[1][:3], (1, wire.LAST, 16000))
        new = self.helper().authenticate()
        new.send(wire.PING, b"new-wire")
        self.assertEqual(new.read(), (wire.PONG, b"new-wire"))
        self.session.on_event("say", id="fake-one", text="new helper")
        self.assertEqual(new.read()[1]["n"], 0)

    def test_slow_readers_overflow_each_queue_without_blocking_callbacks(self):
        self.assertEqual((wire.MAX_QUEUED_AUDIO_S, wire.MAX_QUEUED_EVENTS), (2, 256))
        self.start_link()
        init = transport._Peer.__init__

        def small_buffer(peer, sock):
            init(peer, sock)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)

        for kind in ("audio", "events"):
            with self.subTest(kind=kind), patch.object(transport._Peer, "__init__", small_buffer):
                peer = Helper(self.stack, self.link.address, small_buffer=True).authenticate()
                until(lambda: self.link.status().get("helper_version") == HELLO["version"])
                count = len(self.session.calls)
                if kind == "audio":
                    self.session.on_event("say", id="fake-stall", text="audio")
                    pcm = np.zeros(3200, dtype=np.int16)

                def produce():
                    for _ in range(512):
                        if kind == "audio":
                            self.session.on_audio("fake-stall", pcm, 16000, False, last=False)
                        else:
                            self.session.on_event("partial", text="x" * 600)

                runner = threading.Thread(target=produce, daemon=True)
                start = time.monotonic()
                runner.start()
                runner.join(0.5)
                self.assertFalse(runner.is_alive())
                self.assertLess(time.monotonic() - start, 0.5)
                until(lambda: len(self.session.calls) == count + 1)
                self.assertEqual(self.session.calls[-1], ("stop",))
                peer.disconnected()

    def test_liveness_deadline_uses_complete_frames(self):
        self.assertEqual(wire.LIVENESS_S, 8)
        self.start_link()
        peer = self.connect()
        peer.send(wire.PING, b"deadline")
        self.assertEqual(peer.read(), (wire.PONG, b"deadline"))
        start = time.monotonic()
        peer.sock.settimeout(10)
        peer.sock.sendall(b"\x07")  # An incomplete frame cannot renew the lease.
        peer.disconnected()
        self.assertGreaterEqual(time.monotonic() - start, 7.8)
        self.assertLess(time.monotonic() - start, 9.5)
        until(lambda: self.session.calls == [("start",), ("stop",)])

    def test_liveness_closes_socket_while_session_call_is_busy(self):
        self.start_link()
        entered, release = threading.Event(), threading.Event()

        def feed(*_):
            entered.set()
            release.wait(3)

        with patch.object(wire, "LIVENESS_S", 0.25), patch.object(self.session, "feed", side_effect=feed):
            peer = self.connect()
            try:
                peer.send(wire.AUDIO, (DISTINCT_PCM, None))
                self.assertTrue(entered.wait(1))
                peer.disconnected()
                self.assertEqual(self.session.calls, [("start",)])
            finally:
                release.set()
            until(lambda: self.session.calls == [("start",), ("stop",)])

    def test_close_joins_all_threads_during_traffic(self):
        self.start_link()
        active = self.connect()
        pending = self.helper()
        pending.challenge()
        with ExitStack() as stack:
            ready = threading.Event()

            def send_keys():
                try:
                    while True:
                        active.send(wire.KEY, True)
                        ready.set()
                except OSError:
                    pass

            sender = threading.Thread(target=send_keys, daemon=True)
            sender.start()
            stack.callback(sender.join, 3)
            self.assertTrue(ready.wait(2))
            until(lambda: ("key", True) in self.session.calls)
            self.link.close()
            active.disconnected()
            pending.disconnected()
        self.assertFalse(sender.is_alive())
        self.assertEqual(self.session.calls[-1], ("stop",))
        self.assertEqual(self.session.calls.count(("stop",)), 1)
        snapshot = self.session.calls.copy()
        self.session.on_event("heard", text="after close")
        self.session.on_audio("fake", np.zeros(320, dtype=np.int16), 16000, True)
        self.link.close()
        self.assertEqual(self.session.calls, snapshot)
        self.assertFalse([t.name for t in threading.enumerate() if t.name.startswith("jarvis-voice-")])


def select_ready(sock, timeout):
    return bool(select.select([sock], [], [], timeout)[0])


class WireTests(unittest.TestCase):
    def test_wire_uses_only_standard_library(self):
        tree = ast.parse(Path(wire.__file__).read_bytes())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertTrue(all(alias.name.split(".")[0] in sys.stdlib_module_names for alias in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                self.assertIn(node.module.split(".")[0], sys.stdlib_module_names)

    def test_round_trip_every_frame_and_event_kind(self):
        up = [(wire.HELLO, HELLO), (wire.AUDIO, (b"a" * 640, b"b" * 640)),
              (wire.AUDIO, (b"c" * 640, None)), (wire.KEY, True), (wire.KEY, False),
              (wire.PLAYED, {"id": 42}), (wire.FLUSHED, None),
              (wire.CLICK, {"id": "example", "digest": "fake-digest", "decision": "deny"}),
              (wire.PING, b"example!"), (wire.LEVEL, 255)]
        events = [dict(kind="state", state="unavailable", reason="example"), dict(kind="partial", text="example"),
                  dict(kind="heard", text="example"), dict(kind="say", id="example", n=9, text="example"),
                  dict(kind="asking", id="example", digest="fake-digest", tool="test-tool", sentence="example",
                       expires_in=10), dict(kind="earcon", name="example"), dict(kind="flush"),
                  dict(kind="error", reason="example"), dict(kind="notice", title="example", text="example")]
        down = [(wire.EVENT, event) for event in events]
        down += [(wire.AUDIO_OUT, (9, wire.FIRST | wire.LAST, 16000, b"\x00\x01")), (wire.PONG, b"example!")]
        for direction, pairs in ((wire.UP, up), (wire.DOWN, down)):
            writer = wire.FrameWriter(KEY, direction)
            reader = wire.FrameReader(KEY, direction)
            for kind, value in pairs:
                with self.subTest(direction=direction, kind=kind):
                    frame = writer.encode(kind, wire.encode_payload(kind, value))
                    self.assertEqual(reader.read(io.BytesIO(frame)), (kind, value))

    def test_handshake_and_frame_are_compatible_with_formulas(self):
        a, d = "ab" * 32, "CD" * 32

        def digest(key, data):
            return hmac.new(key, data, hashlib.sha256).digest()

        k3 = digest(KEY, b"jarvis-voice-link-v3/1")
        self.assertEqual(wire.derive_k3(KEY), k3)
        self.assertEqual(wire.check_helper_hello(wire.helper_hello(a)), a)
        reply = wire.daemon_hello(k3, a, d)
        self.assertEqual(reply["proof"], digest(k3, f"daemon|{a}|{d}".encode()).hex())
        self.assertEqual(wire.check_daemon_hello(k3, a, reply), d)
        proof = wire.helper_proof(k3, a, d)
        self.assertEqual(proof["proof"], digest(k3, f"helper|{d}|{a}".encode()).hex())
        wire.check_helper_proof(k3, a, d, proof)
        with self.assertRaises(wire.ProtocolError):
            wire.check_daemon_hello(wire.derive_k3(b"b" * 32), a, reply)
        ks = digest(k3, f"session|{a}|{d}".encode())
        self.assertEqual(wire.derive_ks(k3, a, d), ks)
        frame = struct.pack(">BII", 7, 8, 0) + b"12345678"
        self.assertEqual(wire.FrameWriter(ks, wire.UP).encode(wire.PING, b"12345678"),
                         frame + digest(ks, b"U" + frame)[:8])


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}))
        key_path = self.home / "fake-pairing"
        key_path.write_bytes(KEY_TEXT)
        key_path.chmod(0o600)
        self.key = self.enterContext(patch.object(config, "voice_link_key", return_value=key_path))

    def daemon(self, **kwargs):
        instance = Daemon(port=0, data_dir=self.home / "data", jobs=(), phone_host=None, **kwargs)
        self.addCleanup(instance.stop)
        return instance

    def status(self, instance):
        with urlopen(f"http://127.0.0.1:{instance.port}/v3/status", timeout=3) as result:
            return json.load(result)["voice"]["link"]

    def test_placeholder_does_not_read_the_key_or_bind(self):
        self.key.return_value.unlink()
        instance = self.daemon()
        with patch.object(transport, "_load_key", side_effect=AssertionError("pairing file read")):
            instance.start()
        self.assertIsNone(instance._voice_link.address)
        self.assertEqual(self.status(instance), {"listening": False, "connected": False,
                                                 "reason": "session_unavailable"})

    def test_bind_failure_keeps_daemon_running(self):
        sock = self.enterContext(socket.socket())
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        instance = self.daemon(voice_link_port=sock.getsockname()[1])
        self.enterContext(patch.object(instance, "_new_voice_session", side_effect=Session))
        with self.assertLogs(transport.LOG, "WARNING") as got:
            instance.start()
        self.assertEqual(got.records[0].message, "Voice link could not bind its port.")
        self.assertEqual(self.status(instance), {"listening": False, "connected": False, "reason": "bind_failed"})

    def test_start_and_stop_order_when_close_fails(self):
        instance = self.daemon()
        order = []

        def start_phone(_):
            self.assertGreater(instance.port, 0)
            order.append("phone")

        def new_session(**kwargs):
            order.append("link")
            return Session(**kwargs)

        self.enterContext(patch.object(instance, "_start_phone", side_effect=start_phone))
        self.enterContext(patch.object(instance, "_new_voice_session", side_effect=new_session))
        instance.start()
        self.assertEqual(order, ["phone", "link"])
        peer = Helper(self.enterContext(ExitStack()), instance._voice_link.address).authenticate()
        peer.send(wire.PING, b"12345678")
        self.assertEqual(peer.read(), (wire.PONG, b"12345678"))
        self.assertEqual(self.status(instance), {"listening": True, "connected": True,
                                                 "helper_version": HELLO["version"]})
        original_close = instance._voice_link.close

        def broken_close():
            order.append("link close")
            original_close()
            raise RuntimeError(DISTINCT_WORDS)

        self.enterContext(patch.object(instance._voice_link, "close", side_effect=broken_close))
        self.enterContext(patch.object(instance._voice_turns, "close", side_effect=lambda: order.append("turns")))
        self.enterContext(patch.object(instance._voice_brain, "close", side_effect=lambda: order.append("brain")))
        with self.assertLogs("jarvis.v3.daemon", "WARNING") as got:
            instance.stop()
        self.assertEqual(got.records[0].message, "Could not close the voice link at stop.")
        self.assertEqual(order, ["phone", "link", "link close", "turns", "brain"])
        self.assertIsNone(instance._server)
        peer.disconnected()


if __name__ == "__main__":
    unittest.main()
