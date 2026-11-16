"""VA-1 acceptance tests (orchestrator-owned; the implementer may not edit this file).

Plan: docs/plans/2026-11-15-voice-anywhere-plan.md, package VA-1. The Windows
voice agent dials into the daemon's voice link, both sides prove they hold the
paired key before any audio moves, and the daemon transcribes a push-to-talk
clip with the desk's own dictation.

Free and hermetic: ephemeral loopback ports only (never 8402-8409 live
listeners), temporary key files, a fake transcriber (never a model, never the
network).

Run: PYTHONPATH=. .venv/bin/python tests/v2/voice_link_va1_check.py
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
import wave

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "windows"))

import voicewire  # noqa: E402  (windows/voicewire.py: pure, no Windows imports)

from jarvis import config  # noqa: E402
from jarvis.v2 import voice_link  # noqa: E402


KEY = bytes(range(32))
OTHER = bytes(range(1, 33))


def write_key(directory: Path, key: bytes = KEY, mode: int = 0o600) -> Path:
    path = directory / "voice-link.key"
    path.write_text(key.hex() + "\n")
    os.chmod(path, mode)
    return path


def pcm(seconds: float) -> bytes:
    return b"\x01\x00" * int(16000 * seconds)


class FakeTranscriber:
    def __init__(self, text="hello world", backend="local"):
        self.calls: list[bytes] = []
        self.text, self.backend = text, backend

    def __call__(self, wav_bytes: bytes) -> dict:
        self.calls.append(wav_bytes)
        return {"text": self.text, "backend": self.backend}


class LinkCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        os.chmod(self.dir, 0o700)
        self.key_path = write_key(self.dir)
        self.fake = FakeTranscriber()
        self.link = voice_link.VoiceLink(None, port=0, key_path=self.key_path, transcribe=self.fake)
        self.assertTrue(self.link.start(), getattr(self.link, "status", None))
        self.addCleanup(self.link.stop)
        self.addCleanup(self.tmp.cleanup)

    def client(self, key: bytes = KEY) -> "voicewire.Client":
        c = voicewire.Client("127.0.0.1", self.link.port, key, timeout=5.0)
        self.addCleanup(c.close)
        return c


# -- the pure Windows-side module ----------------------------------------------

class WireTests(unittest.TestCase):
    def test_proofs_bind_role_and_order(self):
        a, d = voicewire.new_nonce(), voicewire.new_nonce()
        self.assertNotEqual(a, d)
        self.assertGreaterEqual(len(a), 32)
        dp = voicewire.daemon_proof(KEY, a, d)
        ap = voicewire.agent_proof(KEY, d, a)
        self.assertNotEqual(dp, ap)
        self.assertNotEqual(dp, voicewire.daemon_proof(KEY, d, a))
        self.assertNotEqual(dp, voicewire.daemon_proof(OTHER, a, d))
        self.assertEqual(dp, voicewire.daemon_proof(KEY, a, d))

    def test_load_key(self):
        with tempfile.TemporaryDirectory() as t:
            good = write_key(Path(t))
            self.assertEqual(voicewire.load_key(good), KEY)
            for bad in ("abc", "zz" * 32, KEY.hex()[:-2], KEY.hex() + "00"):
                p = Path(t) / "bad.key"
                p.write_text(bad)
                with self.assertRaises(voicewire.KeyFileError):
                    voicewire.load_key(p)
            with self.assertRaises(voicewire.KeyFileError):
                voicewire.load_key(Path(t) / "missing.key")

    def test_wav_is_16k_mono_16bit_and_daemon_accepts_it(self):
        data = voicewire.wav_bytes(pcm(1.0))
        with wave.open(io.BytesIO(data)) as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()), (16000, 1, 2, 16000))
        from jarvis.v2 import remote_stt
        remote_stt.check_wav(data)   # raises when the desk's dictation would refuse it


class HotkeyTests(unittest.TestCase):
    def m(self):
        return voicewire.HotkeyMachine(tap_ms=300, max_ms=120_000)

    def test_hold_then_release_records_while_held(self):
        h = self.m()
        self.assertEqual(h.feed(0, False, False), [])
        self.assertEqual(h.feed(10, True, False), ["start"])
        self.assertEqual(h.feed(500, True, False), [])
        self.assertEqual(h.feed(900, False, False), ["stop"])
        self.assertEqual(h.feed(1000, False, False), [])

    def test_tap_toggles_until_the_next_press(self):
        h = self.m()
        self.assertEqual(h.feed(0, True, False), ["start"])
        self.assertEqual(h.feed(100, False, False), [])          # a tap: still recording
        self.assertEqual(h.feed(5000, False, True), [])          # typing elsewhere does not cancel a toggle
        self.assertEqual(h.feed(6000, True, False), ["stop"])    # next press stops
        self.assertEqual(h.feed(6100, False, False), [])         # its release starts nothing
        self.assertEqual(h.feed(7000, True, False), ["start"])

    def test_chord_cancels_and_needs_a_release(self):
        h = self.m()
        self.assertEqual(h.feed(0, True, False), ["start"])
        self.assertEqual(h.feed(50, True, True), ["cancel"])     # Right Ctrl + C is a shortcut, not dictation
        self.assertEqual(h.feed(100, True, False), [])
        self.assertEqual(h.feed(200, False, False), [])
        self.assertEqual(h.feed(300, True, False), ["start"])

    def test_other_key_already_down_never_starts(self):
        h = self.m()
        self.assertEqual(h.feed(0, True, True), [])
        self.assertEqual(h.feed(100, True, False), [])
        self.assertEqual(h.feed(200, False, False), [])
        self.assertEqual(h.feed(300, True, False), ["start"])

    def test_max_length_stops(self):
        h = self.m()
        h.feed(0, True, False)
        self.assertEqual(h.feed(120_000, True, False), ["stop"])
        self.assertEqual(h.feed(130_000, False, False), [])
        h.feed(140_000, True, False)
        h.feed(140_100, False, False)                           # toggled
        self.assertEqual(h.feed(260_000, False, False), ["stop"])


# -- daemon side and interop -----------------------------------------------------

class InteropTests(LinkCase):
    def test_paired_agent_gets_the_transcript(self):
        c = self.client()
        c.connect()
        clip = voicewire.wav_bytes(pcm(1.0))
        got = c.dictate(clip)
        self.assertEqual(got, {"text": "hello world", "backend": "local"})
        self.assertEqual(self.fake.calls, [clip])
        self.assertEqual(c.dictate(clip)["text"], "hello world")   # the connection stays usable

    def test_wrong_key_agent_is_told_not_jarvis_and_nothing_is_transcribed(self):
        c = self.client(OTHER)
        with self.assertRaises(voicewire.NotJarvis):
            c.connect()
        self.assertTrue(issubclass(voicewire.NotJarvis, voicewire.LinkError))
        self.assertEqual(self.fake.calls, [])

    def _raw_hello(self):
        s = socket.create_connection(("127.0.0.1", self.link.port), timeout=5)
        self.addCleanup(s.close)
        f = s.makefile("rwb")
        f.write((json.dumps({"hello": "jarvis-voice-agent", "v": 1, "nonce_a": voicewire.new_nonce()}) + "\n").encode())
        f.flush()
        reply = json.loads(f.readline())
        return s, f, reply

    def _closed(self, f) -> bool:
        try:
            return f.readline() == b""
        except OSError:
            return True

    def test_bad_agent_proof_is_dropped_before_any_request(self):
        s, f, reply = self._raw_hello()
        self.assertIn("proof", reply)
        f.write((json.dumps({"proof": "00" * 32}) + "\n").encode())
        f.write((json.dumps({"op": "dictate", "id": 1, "wav_b64": "AAAA"}) + "\n").encode())
        f.flush()
        self.assertTrue(self._closed(f))
        self.assertEqual(self.fake.calls, [])

    def test_request_without_handshake_is_dropped(self):
        s = socket.create_connection(("127.0.0.1", self.link.port), timeout=5)
        self.addCleanup(s.close)
        f = s.makefile("rwb")
        f.write((json.dumps({"op": "dictate", "id": 1, "wav_b64": "AAAA"}) + "\n").encode())
        f.flush()
        self.assertTrue(self._closed(f))
        self.assertEqual(self.fake.calls, [])

    def test_silent_connection_times_out(self):
        s = socket.create_connection(("127.0.0.1", self.link.port), timeout=15)
        self.addCleanup(s.close)
        start = time.monotonic()
        self.assertEqual(s.makefile("rb").readline(), b"")
        self.assertLess(time.monotonic() - start, voice_link.HANDSHAKE_TIMEOUT_S + 3)
        self.assertLessEqual(voice_link.HANDSHAKE_TIMEOUT_S, 10)

    def test_oversize_and_malformed_clips_are_refused_without_transcribing(self):
        c = self.client()
        c.connect()
        too_long = voicewire.wav_bytes(pcm(voicewire.MAX_CLIP_S + 1))
        with self.assertRaises(voicewire.LinkError):
            c.dictate(too_long)
        with self.assertRaises(voicewire.LinkError):
            c.dictate(b"RIFF not a wav")
        self.assertEqual(self.fake.calls, [])

    def test_a_transcriber_failure_is_a_sentence_not_a_crash(self):
        def boom(_wav):
            raise RuntimeError("engine said /home/johnw/secret path")
        self.link.transcribe = boom
        c = self.client()
        c.connect()
        with self.assertRaises(voicewire.LinkError) as caught:
            c.dictate(voicewire.wav_bytes(pcm(0.5)))
        self.assertNotIn("/home/johnw", str(caught.exception))
        self.link.transcribe = self.fake
        self.assertEqual(c.dictate(voicewire.wav_bytes(pcm(0.5)))["text"], "hello world")

    def test_listens_on_loopback_only(self):
        self.assertEqual(self.link.address[0], "127.0.0.1")


class ImpostorTests(unittest.TestCase):
    """A process that took the port while the daemon was down cannot get audio."""

    def test_client_sends_no_audio_to_a_server_without_the_key(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        received = bytearray()

        def impostor():
            conn, _ = server.accept()
            f = conn.makefile("rwb")
            hello = json.loads(f.readline())
            f.write((json.dumps({"hello": "jarvis-voice-link", "v": 1, "nonce_d": voicewire.new_nonce(),
                                 "proof": voicewire.daemon_proof(OTHER, hello["nonce_a"], "x")}) + "\n").encode())
            f.flush()
            conn.settimeout(2)
            try:
                while chunk := conn.recv(65536):
                    received.extend(chunk)
            except OSError:
                pass
            conn.close()

        t = threading.Thread(target=impostor, daemon=True)
        t.start()
        c = voicewire.Client("127.0.0.1", port, KEY, timeout=5.0)
        with self.assertRaises(voicewire.NotJarvis):
            c.connect()
        with self.assertRaises(voicewire.LinkError):
            c.dictate(voicewire.wav_bytes(pcm(0.5)))
        c.close()
        t.join(5)
        server.close()
        self.assertNotIn(b"wav_b64", bytes(received))
        self.assertNotIn(b"proof", bytes(received))


class KeyFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        os.chmod(self.dir, 0o700)

    def refused(self, key_path: Path):
        link = voice_link.VoiceLink(None, port=0, key_path=key_path, transcribe=FakeTranscriber())
        self.addCleanup(link.stop)
        self.assertFalse(link.start())
        self.assertIsNone(link.address)
        self.assertTrue(link.status)

    def test_missing_key_does_not_listen(self):
        self.refused(self.dir / "absent.key")

    def test_group_or_world_readable_key_does_not_listen(self):
        self.refused(write_key(self.dir, mode=0o644))

    def test_symlinked_key_does_not_listen(self):
        real = write_key(self.dir)
        link = self.dir / "link.key"
        link.symlink_to(real)
        self.refused(link)

    def test_setup_writes_one_key_to_both_places(self):
        from jarvis import voice_link_setup
        wsl = self.dir / "wsl" / "voice-link.key"
        win = self.dir / "win" / "voice-link.key"
        voice_link_setup.write_keys(wsl, win)
        self.assertEqual(voicewire.load_key(wsl), voicewire.load_key(win))
        self.assertEqual(os.stat(wsl).st_mode & 0o077, 0)
        self.assertEqual(os.stat(wsl.parent).st_mode & 0o077, 0)
        first = voicewire.load_key(wsl)
        voice_link_setup.write_keys(wsl, win)          # re-pairing makes a new key
        self.assertNotEqual(voicewire.load_key(wsl), first)


class BoundaryTests(unittest.TestCase):
    def test_port_is_a_control_port(self):
        self.assertEqual(config.VOICE_LINK_PORT, int(os.environ.get("JARVIS_VOICE_LINK_PORT", "8409")))
        self.assertIn(config.VOICE_LINK_PORT, config.control_ports())
        self.assertIn(8409, config.DEFAULT_CONTROL_PORTS)

    def test_key_folder_is_a_credential_folder(self):
        folder = str(Path(config.VOICE_LINK_KEY).expanduser().parent)
        self.assertIn(folder, config.credential_dirs())

    def test_key_file_is_refused_by_name_on_both_sides(self):
        from jarvis.tools import secrets
        self.assertEqual(Path(config.VOICE_LINK_KEY).name, "voice-link.key")
        self.assertIn("voice-link.key", secrets.EXACT_NAMES)
        self.assertTrue(secrets.is_protected(Path(config.VOICE_LINK_KEY).expanduser()))
        self.assertTrue(secrets.is_protected("/mnt/c/Users/johnw/.jarvis-bridge/voice-link.key"))

    def test_link_and_agent_code_are_self_protected(self):
        from jarvis.tools.files import SELF_PROTECTED
        for path in ("jarvis/v2/voice_link.py", "windows/voicewire.py", "windows/voice_agent.py",
                     "jarvis/voice_link_setup.py"):
            self.assertIn(path, SELF_PROTECTED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
