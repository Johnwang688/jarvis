"""VA-2/4/5 package D acceptance tests (orchestrator-owned; the implementer may not edit this file).

Plan: docs/plans/2026-11-16-voice-anywhere-va245.md ("Wire protocol additions").
Hermetic: ephemeral loopback ports, temporary key files, fake transcribers,
router, asker and speaker (never a model, never the network).

Run: PYTHONPATH=. .venv/bin/python tests/v2/voice_link_va245_check.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "windows"))

import voicewire  # noqa: E402

from jarvis.v2 import daemon, voice_link  # noqa: E402

KEY = bytes(range(32))
CLIP = voicewire.wav_bytes(b"\x01\x00" * 8000)


class Calls:
    def __init__(self):
        self.hotkey, self.wake, self.routed, self.asked, self.said = [], [], [], [], []
        self.route_reply = '{"kind":"dictate","text":"hello","target":""}'
        self.route_error = None
        self.wake_error = None
        self.ask_error = None

    def transcribe(self, wav):
        self.hotkey.append(wav)
        return {"text": "hotkey words", "backend": "local"}

    def transcribe_wake(self, wav):
        self.wake.append(wav)
        if self.wake_error:
            raise self.wake_error
        return {"text": "wake words", "backend": "local"}

    def route_model(self, text, source):
        self.routed.append((text, source))
        if self.route_error:
            raise self.route_error
        return self.route_reply

    def asker(self, text, on_delta):
        self.asked.append(text)
        if self.ask_error:
            raise self.ask_error
        on_delta("Hel")
        on_delta("lo")
        return "Hello"

    def speaker(self, text):
        self.said.append(text)
        return b"RIFF-fake-wav"


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        os.chmod(d, 0o700)
        key = d / "voice-link.key"
        key.write_text(KEY.hex() + "\n")
        os.chmod(key, 0o600)
        self.calls = c = Calls()
        self.link = voice_link.VoiceLink(None, port=0, key_path=key, transcribe=c.transcribe,
                                         transcribe_wake=c.transcribe_wake, route_model=c.route_model,
                                         asker=c.asker, speaker=c.speaker)
        self.assertTrue(self.link.start(), self.link.status)
        self.addCleanup(self.link.stop)
        self.client = voicewire.Client("127.0.0.1", self.link.port, KEY, timeout=5.0)
        self.addCleanup(self.client.close)
        self.client.connect()


class Dictate(Case):
    def test_source_selects_the_transcriber(self):
        self.assertEqual(self.client.dictate(CLIP)["text"], "hotkey words")
        self.assertEqual(self.client.dictate(CLIP, source="hotkey")["text"], "hotkey words")
        self.assertEqual(self.client.dictate(CLIP, source="wake")["text"], "wake words")
        self.assertEqual((len(self.calls.hotkey), len(self.calls.wake)), (2, 1))

    def test_unknown_source_is_refused_and_transcribes_nothing(self):
        with self.assertRaises(voicewire.LinkError):
            self.client.dictate(CLIP, source="phone")
        self.assertEqual((self.calls.hotkey, self.calls.wake), ([], []))

    def test_wake_speaker_refusal_is_the_fixed_sentence(self):
        self.calls.wake_error = voice_link.NotOwner("voiceprint score 0.12 for /home/johnw/x")
        with self.assertRaises(voicewire.LinkError) as caught:
            self.client.dictate(CLIP, source="wake")
        self.assertIn(voice_link.NOT_OWNER, str(caught.exception))
        self.assertNotIn("/home/johnw", str(caught.exception))
        self.assertEqual(self.client.dictate(CLIP)["text"], "hotkey words")   # link still usable


class Route(Case):
    def test_a_valid_model_answer_is_the_intent(self):
        self.calls.route_reply = '{"kind":"open_app","target":"Slack","text":"jarvis open slack"}'
        got = self.client.route("jarvis open slack", "hotkey")
        self.assertEqual(got, {"kind": "open_app", "target": "Slack", "text": "jarvis open slack"})
        self.assertEqual(self.calls.routed, [("jarvis open slack", "hotkey")])

    def test_fallback_on_garbage_unknown_kind_or_error(self):
        for reply, error in (("not json at all", None), ('{"kind":"run_shell","target":"rm -rf ~"}', None),
                             ('{"kind":7}', None), (None, RuntimeError("model timeout"))):
            self.calls.route_reply, self.calls.route_error = reply, error
            with self.subTest(reply=reply, error=error):
                self.assertEqual(self.client.route("open the pod bay doors", "hotkey"),
                                 {"kind": "dictate", "text": "open the pod bay doors", "target": ""})
                self.assertEqual(self.client.route("what time is it", "wake"),
                                 {"kind": "ask", "text": "what time is it", "target": ""})

    def test_unknown_route_source_is_refused(self):
        with self.assertRaises(voicewire.LinkError):
            self.client.route("hi", "phone")
        self.assertEqual(self.calls.routed, [])


class ParseIntent(unittest.TestCase):
    def test_fenced_json_and_caps_and_control_characters(self):
        raw = 'Sure!\n```json\n{"kind":"search","text":"black\\u0007 holes","target":"' + "x" * 200 + '"}\n```'
        got = voice_link.parse_intent(raw, "hotkey", "search black holes")
        self.assertEqual(got["kind"], "search")
        self.assertEqual(got["text"], "black holes")
        self.assertEqual(len(got["target"]), 80)

    def test_every_kind_is_accepted_and_nothing_else(self):
        for kind in ("dictate", "ask", "open_app", "switch_app", "search", "read_selection"):
            self.assertEqual(voice_link.parse_intent(json.dumps({"kind": kind, "text": "t", "target": "a"}),
                                                     "hotkey", "orig")["kind"], kind)
        self.assertEqual(voice_link.parse_intent('{"kind":"exec","text":"t"}', "wake", "orig"),
                         {"kind": "ask", "text": "orig", "target": ""})

    def test_missing_text_falls_back_to_the_original_words(self):
        got = voice_link.parse_intent('{"kind":"open_app","target":"Notepad"}', "hotkey", "open notepad")
        self.assertEqual(got, {"kind": "open_app", "text": "open notepad", "target": "Notepad"})

    def test_text_is_capped(self):
        got = voice_link.parse_intent(json.dumps({"kind": "dictate", "text": "a" * 5000}), "hotkey", "x")
        self.assertEqual(len(got["text"]), 2000)


class Ask(Case):
    def test_deltas_stream_then_the_full_answer(self):
        seen = []
        self.assertEqual(self.client.ask("what is two plus two", on_delta=seen.append), "Hello")
        self.assertEqual(seen, ["Hel", "lo"])
        self.assertEqual(self.calls.asked, ["what is two plus two"])
        self.assertEqual(self.client.dictate(CLIP)["text"], "hotkey words")   # stream fully consumed

    def test_failure_is_a_fixed_sentence(self):
        self.calls.ask_error = RuntimeError("provider error at /home/johnw/.config")
        with self.assertRaises(voicewire.LinkError) as caught:
            self.client.ask("hello")
        self.assertNotIn("/home/johnw", str(caught.exception))

    def test_empty_question_is_refused_without_asking(self):
        with self.assertRaises(voicewire.LinkError):
            self.client.ask("   ")
        self.assertEqual(self.calls.asked, [])


class Say(Case):
    def test_returns_the_speakers_wav(self):
        self.assertEqual(self.client.say("Hello there"), b"RIFF-fake-wav")
        self.assertEqual(self.calls.said, ["Hello there"])

    def test_text_is_capped(self):
        self.client.say("a" * 9000)
        self.assertEqual(len(self.calls.said[0]), voice_link.SAY_CHARS)
        self.assertLessEqual(voice_link.SAY_CHARS, 4000)


class Boundary(unittest.TestCase):
    def test_new_ops_need_the_handshake(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        os.chmod(d, 0o700)
        key = d / "voice-link.key"
        key.write_text(KEY.hex() + "\n")
        os.chmod(key, 0o600)
        c = Calls()
        link = voice_link.VoiceLink(None, port=0, key_path=key, transcribe=c.transcribe,
                                    transcribe_wake=c.transcribe_wake, route_model=c.route_model,
                                    asker=c.asker, speaker=c.speaker)
        self.assertTrue(link.start())
        self.addCleanup(link.stop)
        s = socket.create_connection(("127.0.0.1", link.port), timeout=5)
        self.addCleanup(s.close)
        f = s.makefile("rwb")
        for op in ({"op": "route", "id": 1, "text": "x", "source": "hotkey"},
                   {"op": "ask", "id": 2, "text": "x"}, {"op": "say", "id": 3, "text": "x"}):
            f.write((json.dumps(op) + "\n").encode())
        f.flush()
        try:
            self.assertEqual(f.readline(), b"")
        except OSError:
            pass
        self.assertEqual((c.routed, c.asked, c.said), ([], [], []))

    def test_voice_messages_never_steer(self):
        self.assertIn("voice", daemon.VIAS)
        self.assertIn("voice", daemon.NEVER_STEER)

    def test_new_windows_modules_are_self_protected(self):
        from jarvis.tools.files import SELF_PROTECTED
        for name in ("voicetype", "voiceactions", "voiceflow", "voiceui", "voicewake"):
            self.assertIn(f"windows/{name}.py", SELF_PROTECTED)

    def test_defaults_exist(self):
        link = voice_link.VoiceLink(None, port=0, key_path=Path("/nonexistent"))
        for name in ("transcribe", "transcribe_wake", "route_model", "asker", "speaker"):
            self.assertTrue(callable(getattr(link, name)), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
