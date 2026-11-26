"""Exercise the local Voice panel API with a paired fake helper and engines."""

from contextlib import ExitStack
import http.client
import json
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import numpy as np

import phone_auth_check
from phone_listener_check import HOST, ORIGIN, request as phone_request
import voice_wiring_check
from voice_link_check import HELLO, KEY_TEXT, Helper, until
from voice_session_check import Detector, Rows, Transcriber
from voice_wiring_check import CHUNK, RATE, REPLY

from jarvis import config, llm
from jarvis.v3 import phone_auth, phone_routes
from jarvis.v3.daemon import Daemon
from jarvis.v3.voice import settings, wire
from jarvis.v3.voice.engines import EngineChoice, KokoroVoice
from jarvis.v3.voice.session import ECHO_TAIL_S, VoiceSession


VOICE = "/v3/owner/voice"
SETTINGS = VOICE + "/settings"
DISTINCTIVE = "FixtureUtteranceMarker"
BAD = (
    {"unknown": 1}, {"speed": "1.5"}, {"speed": False}, {"speed": None}, {"speed": []},
    {"follow_up_s": {}}, {"follow_up_s": True}, {"speed": float("nan")},
    {"speed": float("inf")}, {"speed": 0.79}, {"speed": 2.01},
    {"follow_up_s": -0.01}, {"follow_up_s": 30.01},
    {"speed": 1.7, "follow_up_s": -1},
)
TRACE_FIELDS = {
    "heard_at", "text", "tools", "approval_asked", "sentence_count", "silence_ms", "decode_ms",
    "first_audio_ms", "first_say_s", "end_s",
}


class SettingsCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())) / "data"
        self.path = self.root / "voice/settings.json"

    def test_defaults_round_trip_and_modes(self):
        with patch.multiple(config, V3_KOKORO_SPEED=1.35, V3_FOLLOW_UP_S=8):
            with self.assertLogs(settings.LOG, level="INFO"):
                self.assertEqual(settings.load(self.root), settings.VoiceSettings(1.35, 8))
        for speed, follow in ((0.8, 0), (2.0, 30), (1.65, 12.5)):
            chosen = settings.defaults().patched({"speed": speed, "follow_up_s": follow})
            settings.save(self.root, chosen)
            self.assertEqual(settings.load(self.root), chosen)
            self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_invalid_patch_and_failed_replace_leave_old_file(self):
        original = settings.VoiceSettings(1.35, 8)
        settings.save(self.root, original)
        previous = self.path.read_bytes()
        for patch_body in (*BAD, []):
            with self.subTest(body=patch_body), self.assertRaises(ValueError):
                original.patched(patch_body)
        with patch.object(settings.os, "replace", side_effect=OSError("fixture rename failure")):
            with self.assertRaises(OSError):
                settings.save(self.root, settings.VoiceSettings(1.7, 12))
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_missing_oversized_nonregular_and_invalid_files_use_defaults(self):
        settings.save(self.root, settings.defaults())
        for raw in (None, b" " * 4097, b"{", b'{"speed":true}', b'{"follow_up_s":NaN}',
                    b'{"extra":8}', b"[" * 2000 + b"]" * 2000, b"\xff"):
            with self.subTest(raw=None if raw is None else raw[:20]):
                if raw is None:
                    self.path.unlink()
                else:
                    self.path.write_bytes(raw)
                with self.assertLogs(settings.LOG, level="INFO") as log:
                    self.assertEqual(settings.load(self.root), settings.defaults())
                self.assertEqual(log.records[0].getMessage(), "Voice settings could not be loaded. Using defaults.")
        with patch.object(settings.os, "open", side_effect=PermissionError("fixture permission")):
            with self.assertLogs(settings.LOG, level="INFO"):
                self.assertEqual(settings.load(self.root), settings.defaults())
        self.path.unlink()
        self.path.mkdir()
        with self.assertLogs(settings.LOG, level="INFO"):
            self.assertEqual(settings.load(self.root), settings.defaults())

    def test_final_symlink_is_neither_read_nor_written_through(self):
        settings.save(self.root, settings.defaults())
        target = self.root / "fixture.json"
        self.path.rename(target)
        target.write_text('{"speed":2,"follow_up_s":30}', encoding="utf-8")
        before = target.read_bytes()
        self.path.symlink_to(target)
        with self.assertLogs(settings.LOG, level="INFO"):
            self.assertEqual(settings.load(self.root), settings.defaults())
        chosen = settings.VoiceSettings(1.5, 10)
        settings.save(self.root, chosen)
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse(self.path.is_symlink())
        self.assertEqual(settings.load(self.root), chosen)

    def test_data_directory_may_have_a_linked_parent(self):
        chosen = settings.VoiceSettings(1.5, 10)
        settings.save(self.root, chosen)
        alias = self.root.parent / "fixture-parent"
        alias.symlink_to(self.root.parent, target_is_directory=True)
        linked = alias / self.root.name
        self.assertEqual(settings.load(linked), chosen)
        settings.save(linked, settings.defaults())
        self.assertEqual(settings.load(self.root), settings.defaults())


class Brain(voice_wiring_check.ScriptedBrain):
    def __init__(self, *_args, **_kwargs):
        super().__init__()
        self.tools = False

    def send(self, text, *, input_id, **kwargs):
        if not self.tools:
            return super().send(text, input_id=input_id, **kwargs)
        self.turn = input_id
        self.emit("user", input_id=input_id)
        self.inputs.add(text)
        for name, outcome in (("fake_lookup", "ok"), ("fake_update", "refused")):
            self.emit("tool_end", name=name, outcome=outcome, changes_state=False,
                      arguments={"target": "example.invalid"}, result="fixture-private-result")
        self.emit("reply", text=REPLY)


class PanelCheck(unittest.TestCase):
    call = phone_auth_check.PhoneAuthCheck.call
    ok = phone_auth_check.PhoneAuthCheck.ok
    pair = phone_auth_check.PhoneAuthCheck.pair
    sign_in = phone_auth_check.PhoneAuthCheck.sign_in
    frames = voice_wiring_check.VoiceWiringCheck.frames
    event = voice_wiring_check.VoiceWiringCheck.event
    state = voice_wiring_check.VoiceWiringCheck.state

    def setUp(self):
        phone_auth_check.PhoneAuthCheck.setUp(self)
        self.data = self.root / "data"
        self.key = self.root / "fake-pairing"
        self.key.write_bytes(KEY_TEXT)
        self.key.chmod(0o600)
        self.asr = Transcriber()
        self.syntheses = Rows()
        self.enterContext(patch.multiple(config, V3_VOICE="kokoro-cpu", V3_KOKORO_SPEED=1.35,
                                         V3_FOLLOW_UP_S=8, voice_link_key=lambda: self.key))
        self.enterContext(patch.object(llm, "chat", side_effect=AssertionError("live model call")))
        self.enterContext(patch("jarvis.v3.voice.claude_brain.ClaudeVoiceBrain", Brain))
        self.enterContext(patch("jarvis.v3.voice.engines.pick_transcriber",
                                new=lambda: EngineChoice(self.asr, "fixture-asr", "fixture transcriber ready")))
        self.enterContext(patch("jarvis.v3.voice.session.VoiceSession",
                                new=lambda *args, **kwargs: VoiceSession(*args, detector=Detector(), **kwargs)))
        self.enterContext(patch.object(KokoroVoice, "_load", return_value=self))

    def create(self, text, *, speed, **_kwargs):
        self.syntheses.add((text, speed))
        return np.full(2 * CHUNK, 0.5, dtype=np.float32), RATE

    def start(self, *, phone=False):
        self.daemon = Daemon(port=0, voice_link_port=0, phone_host=HOST if phone else None,
                             data_dir=self.data, hud_dir=self.root / "hud", jobs=())
        self.addCleanup(self.daemon.stop)
        self.daemon.start()
        self.assertNotIn(self.daemon.port, range(8402, 8411))
        if phone:
            self.auth = self.daemon._phone_server.auth
            self.auth.mono = lambda: self.clock.now

    def request(self, path=VOICE, *, method="GET", body=None, owner=True):
        data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if owner:
            headers["Origin"] = f"http://127.0.0.1:{self.daemon.port}"
        client = http.client.HTTPConnection("127.0.0.1", self.daemon.port, timeout=2)
        try:
            client.request(method, path, body=data, headers=headers)
            response = client.getresponse()
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def update(self, body, *, owner=True):
        return self.request(SETTINGS, method="POST", body=body, owner=owner)

    def test_live_speed_follow_up_and_restart(self):
        self.start()
        with ExitStack() as stack:
            helper = Helper(stack, self.daemon._voice_link.address).authenticate()
            self.state(helper, "resting")
            self.assertEqual(self.update({"speed": 1.75, "follow_up_s": 13}), (
                200, {"settings": settings.VoiceSettings(1.75, 13).snapshot()},
            ))
            text = "Check the next sentence."
            self.asr.lines.append(text)
            helper.send(wire.KEY, True)
            self.state(helper, "listening")
            self.frames(helper, 8)
            helper.send(wire.KEY, False)
            while True:
                kind, fields = helper.read()
                if kind == wire.EVENT and fields["kind"] == "say":
                    sentence = fields["id"]
                    break
            while True:
                kind, payload = helper.read()
                if kind == wire.AUDIO_OUT and payload[1] & wire.LAST:
                    break
            self.assertEqual(self.syntheses.rows[-1], (REPLY, 1.75))
            helper.send(wire.PLAYED, {"id": sentence})
            session = self.daemon._voice_session
            until(lambda: session._follow_from > 0 and session._follow_until > session._follow_from)
            self.assertAlmostEqual(session._follow_until - session._follow_from, 13 - ECHO_TAIL_S)
        self.daemon.stop()
        self.start()
        self.assertEqual(self.request()[1]["settings"], settings.VoiceSettings(1.75, 13).snapshot())
        self.assertEqual(self.daemon._voice_session._fast.speed, 1.75)
        self.assertEqual(self.daemon._voice_session._follow_up_s, 13)

    def test_reply_projects_latest_twenty_and_helper_fields(self):
        self.start()
        turns = self.daemon._voice_turns
        self.daemon._voice_brain.tools = True
        now = time.time()
        for index in range(23):
            turns.heard(f"Fixture {index} " + "x" * 350, silence_ms=510, decode_ms=42)
        # Project the trace even if a tool event later acquires private fields.
        trace = turns.trace()
        trace[0]["tools"][0].update(arguments={"url": "example.invalid"}, result="fixture-private-result")
        self.enterContext(patch.object(turns, "trace", return_value=trace))
        with ExitStack() as stack:
            helper = Helper(stack, self.daemon._voice_link.address).authenticate()
            self.state(helper, "resting")
            helper.send(wire.LEVEL, 67)
            helper.send(wire.PING, b"example!")
            self.assertEqual(helper.read(), (wire.PONG, b"example!"))
            status, reply = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(set(reply), {"state", "transcriber", "voice", "detector", "gpu_warm",
                                      "counters", "helper", "brain", "settings", "turns"})
        for engine in ("transcriber", "voice"):
            self.assertEqual(set(reply[engine]), {"name", "reason"})
        self.assertEqual(reply["helper"], {"connected": True, "listening": True, "version": HELLO["version"],
                                           "canceller": True, "level": 67})
        self.assertEqual(reply["brain"], {"name": "fixture", "state": "idle", "turns": 23})
        self.assertEqual(reply["settings"], settings.VoiceSettings(1.35, 8).snapshot())
        self.assertEqual(len(reply["turns"]), 20)
        for index, row in zip(range(22, 2, -1), reply["turns"], strict=True):
            self.assertEqual(set(row), TRACE_FIELDS)
            self.assertEqual(row["text"], (f"Fixture {index} " + "x" * 350)[:300])
            self.assertGreaterEqual(row["heard_at"], now - 1)
            self.assertLessEqual(row["heard_at"], time.time() + 1)
            self.assertEqual(row["tools"], [{"name": "fake_lookup", "ok": True},
                                            {"name": "fake_update", "ok": False}])
            self.assertEqual((row["silence_ms"], row["decode_ms"]), (510, 42))
        encoded = json.dumps(reply)
        for private in ("example.invalid", "127.0.0.1", "fixture-private-result", KEY_TEXT.decode().strip()):
            self.assertNotIn(private, encoded)

    def test_post_validation_has_no_side_effect_and_partial_updates_merge(self):
        self.start()
        self.assertEqual(self.update({"speed": 1.6})[0], 200)
        self.assertEqual(self.update({"follow_up_s": 0})[1]["settings"], settings.VoiceSettings(1.6, 0).snapshot())
        path = self.data / "voice/settings.json"
        previous = path.read_bytes()
        for body in (*BAD, b'{"speed":1e309}', b"[]", b"null"):
            with self.subTest(body=body):
                status, response = self.update(body)
                self.assertEqual(status, 400)
                self.assertIsInstance(response.get("message", response.get("error")), str)
                self.assertEqual(path.read_bytes(), previous)
                self.assertEqual((self.daemon._voice_session._fast.speed, self.daemon._voice_session._follow_up_s), (1.6, 0))
        with patch.object(settings.os, "replace", side_effect=OSError("fixture rename failure")):
            self.assertEqual(self.update({"speed": 2})[0], 503)
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(self.update({})[1]["settings"], settings.VoiceSettings(1.6, 0).snapshot())

    def test_owner_only_and_phone_has_no_voice_trace(self):
        self.start(phone=True)
        self.daemon._voice_turns.heard(DISTINCTIVE)
        self.assertIn(DISTINCTIVE, json.dumps(self.request()[1]))
        self.assertEqual(self.request(owner=False), self.request("/v3/owner/whoami", owner=False))
        self.assertEqual(self.update({"speed": 1.5}, owner=False),
                         self.request("/v3/owner/ping", method="POST", body={}, owner=False))
        self.assertFalse((self.data / "voice/settings.json").exists())
        self.assertNotIn(DISTINCTIVE, json.dumps(self.request("/v3/status", owner=False)[1]))
        self.pair()
        cookie = self.sign_in().split(";", 1)[0]
        routes = list(phone_routes.PHONE_ROUTES) + [
            *[("POST", "/m/api/" + name) for name in phone_auth._OPEN | (phone_auth._SESSION_WRITES - {"logout"})],
            ("GET", "/m/api/me"), ("GET", "/m/"), ("GET", "/m/pair"), ("GET", "/m/manifest.webmanifest"),
            ("GET", "/m/icons/orb-32.png"), ("GET", "/m/assets/missing.js"),
            ("GET", "/m/api/voice"), ("POST", "/m/api/voice/settings"),
            ("GET", VOICE), ("POST", SETTINGS), ("POST", "/m/api/logout"),
        ]
        for method, path in routes:
            with self.subTest(method=method, path=path):
                status, _, raw = phone_request(self.daemon.phone_port, method, path, origin=ORIGIN,
                                               headers={"Cookie": cookie}, body={} if method == "POST" else None)
                self.assertNotIn(DISTINCTIVE.encode(), raw)
                if "voice" in path:
                    self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
