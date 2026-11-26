from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v3.daemon import Daemon
from jarvis.v3.voice import link, wire
from jarvis.v3.voice.hearing import END_SILENCE_MS, FRAME_MS
from jarvis.v3.voice.session import VoiceSession
from jarvis.v3.voice.turns import VoiceTurns
from voice_link_check import Helper, KEY_TEXT
from voice_session_check import Brain, Detector, Transcriber
from voice_wiring_check import CANCELLED, PCM, QUIET, RATE, RAW


class WiringDelta(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        key = root / "fake-pairing"
        key.touch(mode=0o600)
        key.write_bytes(KEY_TEXT)
        self.enterContext(patch.object(config, "voice_link_key", return_value=key))
        self.brain, self.asr = Brain(), Transcriber()
        self.turns = VoiceTurns(self.brain, on_event=lambda *_args, **_fields: None)
        brain = SimpleNamespace(close=lambda: None)
        self.voice = SimpleNamespace(synthesize=lambda _text: (PCM, RATE))
        session = VoiceSession(self.turns, transcriber=self.asr, voices=(self.voice, None),
                               names=tuple, detector=Detector(), follow_up_s=0)
        with patch.object(Daemon, "_new_voice", return_value=(brain, self.turns, session)):
            self.daemon = Daemon(port=0, voice_link_port=0, data_dir=root / "data",
                                 jobs=(), phone_host=None)
        self.addCleanup(self.daemon.stop)
        self.daemon.start()
        self.address = self.daemon._voice_link.address
        self.assertEqual(self.address[0], "127.0.0.1")
        self.assertNotIn(self.address[1], range(8402, 8411))

    def helper(self):
        helper = Helper(self.enterContext(ExitStack()), self.address).authenticate()
        self.assertEqual(helper.read(), (wire.EVENT, {"kind": "state", "state": "resting"}))
        return helper

    def request(self, helper, *, count=1):
        self.asr.lines.append("Jarvis, open the fixture folder.")
        for _ in range(8):
            helper.send(wire.AUDIO, (RAW, CANCELLED))
        for _ in range(END_SILENCE_MS // FRAME_MS):
            helper.send(wire.AUDIO, (QUIET, QUIET))
        self.brain.inputs.wait(lambda rows: rows == ["open the fixture folder."] * count)

    def test_approval_event_and_prompt_reach_paired_helper(self):
        helper = self.helper()
        self.request(helper)
        self.brain.pending = [{
            "id": "fake-card", "digest": "fake-digest", "tool": "vault_add", "expires_in": 15,
            "card": {"title": "Write a fixture note", "rows": []},
        }]
        self.brain.emit("approval_pending")

        events = []
        try:
            while True:
                kind, value = helper.read()
                if kind == wire.AUDIO_OUT:
                    break
                self.assertEqual(kind, wire.EVENT)
                events.append(value)
        except (EOFError, wire.ProtocolError) as error:
            self.fail(f"No valid approval prompt audio reached the helper: {error}")
        asking = next((event for event in events if event["kind"] == "asking"), None)
        self.assertIsNotNone(asking)
        self.assertEqual(asking["id"], "fake-card")
        self.assertEqual(asking.get("expires_in"), 15)

    def test_flush_drops_audio_still_queued_for_the_helper(self):
        entered, release, last_queued, flush_queued = (Event() for _ in range(4))
        send, on_audio, on_event = link._Peer.send, link._Peer.on_audio, link._Peer.on_event

        def pause_audio(peer, data, deadline=None):
            if data[0] == wire.AUDIO_OUT and deadline is None and not entered.is_set():
                entered.set()
                release.wait(3)
            send(peer, data, deadline)

        def observe_audio(peer, sentence, pcm, rate, first, last):
            on_audio(peer, sentence, pcm, rate, first, last)
            if last:
                last_queued.set()

        def observe_event(peer, kind, **fields):
            on_event(peer, kind, **fields)
            if kind == "flush":
                flush_queued.set()

        self.enterContext(patch.object(link._Peer, "send", pause_audio))
        self.enterContext(patch.object(link._Peer, "on_audio", observe_audio))
        self.enterContext(patch.object(link._Peer, "on_event", observe_event))
        helper = self.helper()
        self.request(helper)
        self.brain.emit("reply", text="The first fixture answer is ready.")
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(last_queued.wait(2))
            helper.send(wire.KEY, True)
            self.assertTrue(flush_queued.wait(2))
        finally:
            release.set()

        flags = []
        while True:
            kind, value = helper.read()
            if kind == wire.EVENT and value["kind"] == "flush":
                break
            if kind == wire.AUDIO_OUT:
                flags.append(value[1])
        self.assertFalse(any(flag & wire.LAST for flag in flags), flags)
        helper.send(wire.FLUSHED, value["n"])
        helper.send(wire.PING, b"fixture!")
        while True:
            kind, value = helper.read()
            if kind == wire.PONG:
                self.assertEqual(value, b"fixture!")
                break
            self.assertEqual(kind, wire.EVENT)

    def test_early_played_does_not_discard_the_final_wire_chunk(self):
        release = Event()
        on_audio = link._Peer.on_audio

        def pause_after_first(peer, sentence, pcm, rate, first, last):
            on_audio(peer, sentence, pcm, rate, first, last)
            if first and not last:
                release.wait(3)

        self.enterContext(patch.object(link._Peer, "on_audio", pause_after_first))
        helper = self.helper()
        self.request(helper)
        self.brain.emit("reply", text="The first fixture answer is ready.")
        try:
            while True:
                kind, say = helper.read()
                self.assertEqual(kind, wire.EVENT)
                if say["kind"] == "say":
                    break
            kind, audio = helper.read()
            self.assertEqual((kind, audio[0], audio[1]), (wire.AUDIO_OUT, say["n"], wire.FIRST))
            helper.send(wire.PLAYED, {"id": say["id"]})
            helper.send(wire.PING, b"fixture!")
            self.assertEqual(helper.read(), (wire.PONG, b"fixture!"))
        finally:
            release.set()
        kind, audio = helper.read()
        self.assertEqual(kind, wire.AUDIO_OUT)
        self.assertEqual((audio[0], audio[1]), (say["n"], wire.LAST))


if __name__ == "__main__":
    unittest.main()
