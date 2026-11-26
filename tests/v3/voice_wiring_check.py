"""Run the daemon's voice path against a paired loopback helper and fake engines."""

from contextlib import ExitStack
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import urlopen

import numpy as np

from jarvis import config
from jarvis.v3.daemon import Daemon
from jarvis.v3.voice import wire
from jarvis.v3.voice.hearing import END_SILENCE_MS, FRAME_MS
from jarvis.v3.voice.session import VoiceSession
from jarvis.v3.voice.turns import VoiceTurns
from voice_link_check import HELLO, KEY_TEXT, Helper, until
from voice_session_check import Brain, Detector, Transcriber


RATE = 16_000
CHUNK = wire.audio_chunk_samples(RATE)
PCM = np.arange(2 * CHUNK, dtype="<i2")
RAW = np.resize(np.array([12_000, -12_000], dtype="<i2"), wire.MIC_BYTES // 2).tobytes()
CANCELLED = np.resize(np.array([6_000, -6_000], dtype="<i2"), wire.MIC_BYTES // 2).tobytes()
QUIET = bytes(wire.MIC_BYTES)
REPLY = "The fixture answer is ready."


class ScriptedBrain(Brain):
    def send(self, text, *, input_id, **kwargs):
        super().send(text, input_id=input_id, **kwargs)
        self.emit("reply", text=REPLY)

    def status(self):
        return {"brain": "fixture", "state": "idle", "turns": len(self.inputs.rows)}

    def close(self):
        self.listener = None


class VoiceWiringCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.key = self.root / "fake-pairing"
        self.enterContext(patch.object(config, "voice_link_key", return_value=self.key))
        self.brain, self.asr = ScriptedBrain(), Transcriber()
        self.baseline = set(threading.enumerate())

    def start(self):
        turns = VoiceTurns(self.brain, on_event=lambda *_args, **_fields: None)
        voice = SimpleNamespace(name="fixture-voice", synthesize=lambda _text: (PCM, RATE))
        session = VoiceSession(turns, transcriber=self.asr, voices=(voice, None), names=tuple,
                               detector=Detector(), follow_up_s=0)
        with patch.object(Daemon, "_new_voice", return_value=(self.brain, turns, session)):
            daemon = Daemon(port=0, voice_link_port=0, data_dir=self.root / "data", jobs=(), phone_host=None)
        self.addCleanup(daemon.stop)
        daemon.start()
        self.assertIs(daemon._voice_session, session)
        self.assertIsNone(session._worker)
        self.assertIsNone(session._on_event)
        self.assertIsNone(session._on_audio)
        self.assertEqual(self.brain.warmed.rows, [])
        self.assertEqual(self.asr.warmed.rows, [])
        return daemon, session

    def voice_status(self, daemon):
        with urlopen(f"http://127.0.0.1:{daemon.port}/v3/status", timeout=2) as response:
            return json.load(response)["voice"]

    def event(self, helper, **fields):
        self.assertEqual(helper.read(), (wire.EVENT, fields))

    def state(self, helper, state):
        self.event(helper, kind="state", state=state)

    def frames(self, helper, count, raw=RAW, cancelled=CANCELLED):
        for _ in range(count):
            helper.send(wire.AUDIO, (raw, cancelled))

    def reply(self, helper, request):
        events = []
        while True:
            kind, event = helper.read()
            self.assertEqual(kind, wire.EVENT)
            if event["kind"] == "say":
                break
            events.append(event)
        self.assertIn({"kind": "heard", "text": request}, events)
        self.assertIn({"kind": "state", "state": "speaking"}, events)
        self.assertEqual(event["text"], REPLY)
        for flags, samples in ((wire.FIRST, PCM[:CHUNK]), (wire.LAST, PCM[CHUNK:])):
            self.assertEqual(helper.read(), (wire.AUDIO_OUT, (event["n"], flags, RATE, samples.tobytes())))
        return event["id"]

    def test_paired_audio_played_key_and_reconnect(self):
        self.key.write_bytes(KEY_TEXT)
        self.key.chmod(0o600)
        daemon, session = self.start()
        link = daemon._voice_link
        self.assertEqual(self.voice_status(daemon)["link"], {"listening": True, "connected": False})
        self.assertEqual(link.address[0], "127.0.0.1")
        self.assertNotIn(link.port, range(8402, 8411))
        with ExitStack() as first:
            helper = Helper(first, link.address).authenticate()
            self.state(helper, "resting")
            helper.send(wire.PING, b"example!")
            self.assertEqual(helper.read(), (wire.PONG, b"example!"))
            self.assertEqual(self.voice_status(daemon)["link"], {
                "listening": True, "connected": True, "helper_version": HELLO["version"],
            })

            self.asr.lines.append("Ordinary fixture speech.")
            self.frames(helper, 8)
            self.frames(helper, END_SILENCE_MS // FRAME_MS, QUIET, QUIET)
            until(lambda: session.status()["counters"]["ignored"] == 1)

            request = "Check the fixture."
            self.asr.lines.append(f"Jarvis, {request}")
            self.frames(helper, 8)
            self.frames(helper, END_SILENCE_MS // FRAME_MS, QUIET, QUIET)
            sentence = self.reply(helper, request)
            status = self.voice_status(daemon)
            self.assertEqual((status["state"], status["counters"]["played"]), ("speaking", 0))
            self.assertGreater(self.asr.calls.rows[-1][0], 0)
            helper.send(wire.PLAYED, {"id": sentence})
            self.state(helper, "resting")
            self.assertEqual(self.voice_status(daemon)["counters"]["played"], 1)

            helper.send(wire.KEY, True)
            self.state(helper, "listening")
            request = "This request uses the key."
            self.asr.lines.append(request)
            self.frames(helper, 8)
            helper.send(wire.KEY, False)
            helper.send(wire.PLAYED, {"id": self.reply(helper, request)})
            self.state(helper, "resting")
            self.assertEqual(self.brain.inputs.rows, ["Check the fixture.", request])

            helper.send(wire.KEY, True)
            self.state(helper, "listening")
            worker, stale_event = session._worker, session._on_event

        until(lambda: not link.status()["connected"] and session._on_event is None
              and session.status()["state"] == "resting")
        self.assertIsNone(session._on_audio)
        self.assertFalse(session._active)
        with ExitStack() as second:
            helper = Helper(second, link.address).authenticate()
            self.state(helper, "resting")
            self.assertIs(link._session, session)
            self.assertIs(session._worker, worker)
            stale_event("notice", title="Old helper", text="Old fixture event.")
            helper.send(wire.PING, b"example2")
            self.assertEqual(helper.read(), (wire.PONG, b"example2"))
            self.assertEqual(len(self.asr.warmed.rows), 1)
            self.assertEqual(len(self.brain.warmed.rows), 1)
            daemon.stop()
            helper.disconnected()
        self.assertIsNone(session._on_event)
        self.assertIsNone(session._on_audio)
        self.assertTrue(self.asr.closed.is_set())
        until(lambda: set(threading.enumerate()) <= self.baseline)

    def test_missing_or_unusable_key_leaves_daemon_running(self):
        for reason in ("key_missing", "key_invalid"):
            with self.subTest(reason=reason):
                if reason == "key_invalid":
                    self.key.write_bytes(b"invalid fake key")
                    self.key.chmod(0o600)
                daemon, session = self.start()
                self.assertIsNone(daemon._voice_link.address)
                self.assertIsNone(daemon._voice_link._acceptor)
                voice = self.voice_status(daemon)
                self.assertEqual((voice["brain"], voice["brain_state"], voice["state"]),
                                 ("fixture", "idle", "resting"))
                self.assertEqual(voice["link"], {"listening": False, "connected": False, "reason": reason})
                self.assertIsNone(session._worker)
                daemon.stop()
        until(lambda: set(threading.enumerate()) <= self.baseline)


if __name__ == "__main__":
    unittest.main()
