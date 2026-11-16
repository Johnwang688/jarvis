"""VA-1 desk path, lifecycle and adversarial connection checks; free/hermetic.

Real Daemon, ephemeral ports, temporary HOME/data, fake local engine and cloud
spy. The Windows UI/device code needs a separate manual Windows check.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("voice_test_stt", Path(__file__).with_name("remote_stt_check.py"))
stc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stc)
rac = stc.rnc.rac

from jarvis import config, llm, rules, speaker, stt_local  # noqa: E402
from jarvis.v2 import hud_api, remote_stt, voice_link  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402
from windows import voicewire as wire  # noqa: E402

KEY = bytes(range(32))


def eventually(predicate):
    stc.rnc.eventually(predicate)


class HotkeyBoundary(unittest.TestCase):
    def test_shortcut_cancels_when_it_coincides_with_the_clip_cap(self):
        machine = wire.HotkeyMachine()
        self.assertEqual(machine.feed(0, True, False), ["start"])
        self.assertEqual(machine.feed(120_000, True, True), ["cancel"])
        self.assertEqual(machine.feed(120_100, True, False), [])
        self.assertEqual(machine.feed(120_200, False, False), [])
        self.assertEqual(machine.feed(120_300, True, False), ["start"])


class Base(rac.Base):
    def setUp(self):
        super().setUp()
        self.settings = self.root / "stt.json"
        for name, value in {"VOICE_LINK_PORT": 0, "STT_SETTINGS_PATH": self.settings,
                            "SPEAKER_DIR": self.root / "speaker", "STT_BACKEND_DEFAULT": "local",
                            "STT_CLOUD_FALLBACK_DEFAULT": False}.items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        self.key = config.voice_link_key()
        self.key.parent.mkdir(mode=0o700, parents=True)
        self.key.write_text(KEY.hex() + "\n")
        self.key.chmod(0o600)
        self.words = "private spoken " + wire.new_nonce()[:12]
        self.backend = stc.LocalBackend(self.words)
        self.addCleanup(stt_local.set_engine, stt_local.set_engine(
            stt_local.Engine(lambda threads: self.backend, schedule=False)))
        self.cloud = stc.Cloud()
        guard = patch.object(llm, "transcribe", self.cloud)
        guard.start()
        self.addCleanup(guard.stop)
        self.d = Daemon(Stores(), {name: rac.Fake(name) for name in rac.PN},
                        lambda *_: lambda *_: rac.Decision.DENY, 0, remote_host=None)
        self.addCleanup(self.d.stop)

    def start(self):
        self.d.start()
        self.link = self.d.voice_link
        self.assertIsNotNone(self.link.address)
        self.assertFalse({self.d.port, self.d.face_port, self.d.workshop_port, self.link.port} & set(range(8402, 8410)))

    def client(self):
        c = wire.Client("127.0.0.1", self.link.port, KEY, timeout=5)
        self.addCleanup(c.close)
        c.connect()
        return c

    def raw(self):
        s = socket.create_connection(self.link.address, timeout=3)
        self.addCleanup(s.close)
        f = s.makefile("rwb")
        self.addCleanup(f.close)
        return s, f

    def ping(self, c):
        wire.send_json(c._stream, {"op": "ping", "id": 901})
        self.assertEqual(wire.read_json(c._stream), {"id": 901, "ok": True})


class DeskPath(Base):
    def test_physical_gesture_skips_nonmatching_speaker_and_uses_desk_path(self):
        self.start()
        c = self.client()
        data = stc.wav(0.5)
        real = hud_api.dictate
        calls = []

        def spy(daemon, clip, mime, *, budget, verify=True):
            calls.append((daemon, clip, mime, budget, verify))
            return real(daemon, clip, mime, budget=budget, verify=verify)
        verdict = dict(speaker._last)
        with patch.object(speaker, "verify", side_effect=speaker.NotOwner("not the enrolled speaker")) as verify, \
                patch.object(hud_api, "dictate", spy):
            self.assertEqual(c.dictate(data), {"text": self.words, "backend": "local"})
        verify.assert_not_called()
        self.assertEqual(verdict, speaker._last)
        self.assertEqual(calls, [(self.d, data, "audio/wav", remote_stt.BUDGET_S, False)])
        self.assertTrue(self.d._stt_watching)
        self.assertEqual(self.cloud.calls, [])

    def test_local_only_never_uploads_even_when_engine_fails_or_missing(self):
        self.start()
        c = self.client()
        self.settings.write_text(json.dumps({"backend": "local", "cloud_fallback": False}))
        data = stc.wav(0.5)
        self.assertEqual(c.dictate(data)["backend"], "local")
        self.backend.error = RuntimeError("private engine detail")
        with self.assertRaises(wire.LinkError):
            c.dictate(data)
        stt_local.set_engine(None)
        with patch.object(stt_local, "installed", return_value=(False, "not installed")):
            with self.assertRaises(wire.LinkError):
                c.dictate(data)
        self.assertEqual(self.cloud.calls, [])

    def test_cloud_only_when_allowed_and_reply_scrub_and_cap(self):
        self.start()
        c = self.client()
        self.settings.write_text(json.dumps({"backend": "cloud", "cloud_fallback": True}))
        self.assertEqual(c.dictate(stc.wav(0.5)), {"text": "cloud words", "backend": "cloud"})
        self.assertEqual(len(self.cloud.calls), 1)
        self.backend.text = "ghp_" + "Zq7" * 12
        self.settings.write_text(json.dumps({"backend": "local", "cloud_fallback": False}))
        self.assertNotIn(self.backend.text, c.dictate(stc.wav(0.5))["text"])
        self.backend.text = "x" * (remote_stt.TEXT_CHARS + 50)
        self.assertEqual(len(c.dictate(stc.wav(0.5))["text"]), remote_stt.TEXT_CHARS)

    def test_no_audio_or_words_written_or_logged(self):
        self.start()
        c = self.client()
        data = stc.wav(0.5, seed=52928)
        needle = data[1000:1064]
        records = []

        class Keep(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))
        logger = logging.getLogger()
        keep = Keep()
        previous_level, previous_disable = logger.level, logging.root.manager.disable
        logger.setLevel(logging.DEBUG)
        logger.addHandler(keep)
        logging.disable(logging.NOTSET)
        try:
            self.assertEqual(c.dictate(data)["text"], self.words)
            self.backend.error = RuntimeError(self.words)
            with self.assertRaises(wire.LinkError):
                c.dictate(data)
            with self.assertRaises(wire.LinkError):
                c.dictate(data[:100])
        finally:
            logger.removeHandler(keep)
            logger.setLevel(previous_level)
            logging.disable(previous_disable)
        for path in self.root.rglob("*"):
            if path.is_file():
                content = path.read_bytes()
                self.assertNotIn(needle, content, str(path))
                self.assertNotIn(self.words.encode(), content, str(path))
        logged = "\n".join(records)
        for sensitive in (self.words, "RIFF", needle.hex()):
            self.assertNotIn(sensitive, logged)
        self.assertEqual(self.cloud.calls, [])

    def test_120_seconds_allowed_phone_still_60(self):
        self.start()
        data = wire.wav_bytes(b"\x01\x00" * (16000 * 120))
        c = self.client()
        self.link.transcribe = lambda _: {"text": "long clip", "backend": "local"}
        self.assertEqual(c.dictate(data)["text"], "long clip")
        with self.assertRaises(remote_stt._Refused):
            remote_stt.check_wav(data)


class Connections(Base):
    def test_authenticated_replacement_and_unauthenticated_nonreplacement(self):
        self.start()
        old = self.client()
        self.ping(old)  # server has completed authentication
        _, f = self.raw()
        a = wire.new_nonce()
        wire.send_json(f, {"hello": "jarvis-voice-agent", "v": 1, "nonce_a": a})
        self.assertIn("proof", wire.read_json(f))
        self.ping(old)
        wire.send_json(f, {"proof": "00" * 32})
        self.assertEqual(f.readline(), b"")
        self.ping(old)
        new = self.client()
        self.ping(new)
        with self.assertRaises(wire.LinkError):
            old.dictate(stc.wav(0.1))
        self.assertEqual(new.dictate(stc.wav(0.1))["text"], self.words)

    def test_replacement_does_not_wait_for_slow_transcription(self):
        self.start()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow(_):
            entered.set()
            release.wait(5)
            return {"text": "done", "backend": "local"}
        self.link.transcribe = slow
        old = self.client()
        outcome = []
        def run():
            try:
                outcome.append(old.dictate(stc.wav(0.1)))
            except wire.LinkError:
                outcome.append("closed")
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(3))
        new = self.client()
        self.ping(new)
        release.set()
        worker.join(3)
        self.assertEqual(outcome, ["closed"])

    def test_unauthenticated_cap_and_handshake_whole_deadline(self):
        with patch.object(voice_link, "HANDSHAKE_TIMEOUT_S", 0.4):
            self.start()
            active = self.client()
            self.ping(active)
            silent = [self.raw() for _ in range(voice_link.MAX_UNAUTHENTICATED)]
            eventually(lambda: self.link._pending == voice_link.MAX_UNAUTHENTICATED)
            extra, _ = self.raw()
            self.assertEqual(extra.recv(1), b"")
            self.ping(active)
            # Trickle a partial line; activity must not extend the deadline.
            started = time.monotonic()
            silent[0][0].sendall(b'{"hello":')
            time.sleep(0.15)
            silent[0][0].sendall(b'"jarvis')
            self.assertEqual(silent[0][1].readline(), b"")
            self.assertLess(time.monotonic() - started, 0.8)
            eventually(lambda: self.link._pending == 0)
            self.ping(active)

    def test_request_shapes_and_oversize_line_never_transcribe(self):
        self.start()
        c = self.client()
        with patch.object(self.link, "transcribe") as transcribe:
            for payload in ({"op": "other", "id": 1}, {"op": "dictate", "id": 2, "wav_b64": "☃"},
                            {"op": "dictate", "id": 3, "wav_b64": []}, {"op": "ping", "id": True}):
                wire.send_json(c._stream, payload)
                self.assertIs(wire.read_json(c._stream)["ok"], False)
            c._stream.write(b"x" * (wire.MAX_LINE + 1))
            c._stream.flush()
            self.assertIs(wire.read_json(c._stream)["ok"], False)
            transcribe.assert_not_called()

    def test_deeply_nested_json_is_refused_without_thread_exception(self):
        self.start()
        c = self.client()
        self.ping(c)
        errors = []
        with patch.object(threading, "excepthook", lambda args: errors.append(args.exc_value)):
            c._stream.write(b'{"id":1,"nested":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}\n")
            c._stream.flush()
            self.assertFalse(wire.read_json(c._stream)["ok"])
            c.close()
            eventually(lambda: not self.link._connections)
        self.assertEqual(errors, [])

    def test_stop_closes_active_and_pending(self):
        self.start()
        c = self.client()
        self.ping(c)
        _, pending = self.raw()
        eventually(lambda: self.link._pending == 1)
        self.d.stop()
        self.assertIsNone(self.link.address)
        self.assertEqual(pending.readline(), b"")
        with self.assertRaises(wire.LinkError):
            c.dictate(stc.wav(0.1))


class Lifecycle(Base):
    def test_daemon_without_key_starts_no_link_or_model(self):
        self.key.unlink()
        with patch.object(voice_link, "VoiceLink") as link:
            self.d.start()
            link.assert_not_called()
        self.assertEqual(self.cloud.calls, [])
        self.assertEqual(self.backend.calls, [])
        self.assertFalse(hasattr(self.d, "voice_link"))

    def test_pairing_cli_is_never_auto_approved(self):
        for command in ("jarvis voice-link setup", "python -m jarvis voice-link setup",
                        "/home/johnw/projects/Jarvis/.venv/bin/jarvis voice-link setup"):
            self.assertNotEqual(rules.decide(command).decision, rules.ALLOW, command)

    def test_setup_cli_reuses_bridge_venv_and_writes_launcher(self):
        from types import SimpleNamespace
        from jarvis import desktop_setup, voice_link_setup
        from jarvis.__main__ import build_parser, cmd_voice_link
        from jarvis.tools import secrets as secret_rules
        bridge = self.root / "Users" / "johnw" / ".jarvis-bridge"
        exe = bridge / "venv" / "Scripts" / "python.exe"
        exe.parent.mkdir(parents=True)
        exe.touch()
        commands = []
        def run(argv, **kwargs):
            commands.append(argv)
            return SimpleNamespace(returncode=0, stdout=str(bridge) + "\n", stderr="")
        parser = build_parser()
        args = parser.parse_args(["voice-link", "setup"])
        self.assertIs(args.func, cmd_voice_link)
        with patch.object(desktop_setup, "_find_python", return_value="python.exe"), \
                patch.object(config, "windows_users_root", return_value=str(self.root / "Users")), \
                patch.object(desktop_setup, "_win_path", return_value=r"\\wsl.localhost\Ubuntu\repo\windows\voice_agent.py"), \
                patch.object(voice_link_setup.subprocess, "run", run), \
                patch.object(desktop_setup.console, "print"):
            self.assertEqual(cmd_voice_link(args), 0)
        self.assertEqual(commands[1], [str(exe), "-m", "pip", "install", "--upgrade", "sounddevice", "numpy"])
        self.assertEqual(wire.load_key(self.key), wire.load_key(bridge / "voice-link.key"))
        launcher = (bridge / "run-voice.cmd").read_text()
        self.assertIn("127.0.0.1 0", launcher)
        self.assertIn("voice_agent.py", launcher)
        self.assertIn("voice-link.key", launcher)
        self.assertNotIn(wire.load_key(self.key).hex(), launcher)
        # Even temporary key copies must retain a credential name on Windows.
        staged = voice_link_setup._stage(bridge / "voice-link.key", b"temporary key")
        self.assertTrue(secret_rules.is_protected(staged))
        staged.unlink()
        self.assertEqual(list(bridge.glob(".env.voice-link-*")), [])

    def test_key_format_and_file_kinds_fail_closed_and_dynamic_data_root(self):
        self.assertEqual(config.voice_link_key().parent, Path(config.V2_DATA_DIR) / "voice-link")
        self.assertIn(str(self.key.parent), config.credential_dirs())
        self.key.unlink()
        os.mkfifo(self.key)
        link = voice_link.VoiceLink(None, port=0)
        self.assertFalse(link.start())
        self.assertIsNone(link.address)
        self.key.unlink()
        for value in (KEY.hex() + "\r\n", KEY.hex() + "\n\n", " " + KEY.hex(), KEY.hex() + " "):
            self.key.write_text(value)
            with self.assertRaises(wire.KeyFileError):
                wire.load_key(self.key)
        a, d = wire.new_nonce(), wire.new_nonce()
        expected = hmac.new(KEY, ("daemon|" + a + "|" + d).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(wire.daemon_proof(KEY, a, d), expected)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
