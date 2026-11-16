"""Package D backend/concurrency regressions: real daemon, temporary HOME,
ephemeral ports, fake speech engines and providers; no network/model calls.

Implementation notes: voice uses send (never steer), subscribes before sending,
and filters the returned turn id. Its durable reference stores only the chat id.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("voice_link_existing", Path(__file__).with_name("voice_link_check.py"))
existing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(existing)

from jarvis import llm, models, speaker, voice
from jarvis.v2 import chat_reads, projects, voice_link
from jarvis.v2.daemon import Daemon
from jarvis.v2.desk import desk_message
from jarvis.v2.discord.mirror import user_line
from jarvis.v2.model import ProviderName as PN
from jarvis.v2.provider import Event, EventKind as EK, UserMessage
from jarvis.v2.stores import Stores
from windows import voicewire as wire


class Streaming(existing.rac.Fake):
    def __init__(self):
        super().__init__(PN.FAST)
        self.messages, self.steered, self.desk = [], [], []
        self.entered, self.release = threading.Event(), threading.Event()
        self.block = False
        self.interrupted = 0

    def send(self, h, message):
        self.messages.append(message)
        self.desk.append(desk_message(h.native.brief, message))
        self.entered.set()
        yield Event(EK.TURN_STARTED, h.thread_id)
        if self.block:
            # A question/approval wait is not a turn end.
            yield Event(EK.QUESTION, h.thread_id, {"req_id": "owner-only", "text": "Continue?", "options": []})
            self.release.wait(5)
        # Other turns' events must never enter the voice answer.
        yield Event(EK.TEXT_DELTA, h.thread_id, {"text": "Hel"})
        yield Event(EK.TEXT_DELTA, h.thread_id, {"text": "lo"})
        yield Event(EK.TEXT, h.thread_id, {"text": "Hello"})
        yield Event(EK.TURN_FINISHED, h.thread_id, {"stop": "end"})

    def steer(self, h, message):
        self.steered.append(message)
        return True

    def interrupt(self, h):
        self.interrupted += 1
        self.release.set()


class Base(existing.Base):
    def setUp(self):
        super().setUp()
        self.fast = Streaming()
        self.d.providers[PN.FAST] = self.fast
        self.addCleanup(self.fast.release.set)

    def voice_thread(self):
        return next(t for t in self.d.stores.threads.list() if t.title == "Voice")

    def async_ask(self, client, text="hello", seen=None):
        result, done = [], threading.Event()

        def work():
            try:
                result.append(client.ask(text, on_delta=seen.append if seen is not None else None))
            except wire.LinkError as exc:
                result.append(exc)
            finally:
                done.set()
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        return result, done


class Backends(Base):
    def test_voice_created_once_reused_after_restart_and_owner_message(self):
        self.start()
        c = self.client()
        seen = []
        self.assertEqual(c.ask("hello", seen.append), "Hello")
        self.assertEqual(seen, ["Hel", "lo"])
        self.assertEqual(c.ask("again"), "Hello")
        thread = self.voice_thread()
        self.assertEqual(self.d.thread_json(thread)["title"], "Voice")
        self.assertEqual(len(self.d.stores.threads.list()), 1)
        self.assertEqual(thread.provider, PN.FAST)
        self.assertTrue(self.d.stores.projects.get(thread.project_id).inbox)
        for message in self.fast.messages:
            self.assertEqual((message.origin, message.via, message.desk, message.spoken),
                             ("owner", "voice", False, True))
        self.assertEqual(self.fast.desk, [False, False])
        self.assertEqual(json.loads((self.d.stores.root / "voice-chat.json").read_text()), thread.id)
        self.d.stop()
        other = Daemon(Stores(self.d.stores.root), {p: self.fast if p == PN.FAST else existing.rac.Fake(p) for p in PN},
                       lambda *_: lambda *_: existing.rac.Decision.DENY, 0, remote_host=None)
        self.addCleanup(other.stop)
        other.start()
        c2 = wire.Client("127.0.0.1", other.voice_link.port, existing.KEY, timeout=5)
        self.addCleanup(c2.close)
        c2.connect()
        self.assertEqual(c2.ask("after restart"), "Hello")
        self.assertEqual([t.id for t in other.stores.threads.list()], [thread.id])

    def test_reference_recovery_and_direct_asker_interface(self):
        self.start()
        self.assertEqual(self.link.asker("direct", lambda _: None), "Hello")
        thread = self.voice_thread()
        (self.d.stores.root / "voice-chat.json").unlink()
        self.assertEqual(self.client().ask("recover"), "Hello")
        self.assertEqual([t.id for t in self.d.stores.threads.list()], [thread.id])

    def test_archived_or_deleted_voice_is_replaced(self):
        self.start()
        c = self.client()
        self.assertEqual(c.ask("first"), "Hello")
        old = self.voice_thread()
        existing.eventually(lambda: not self.d.thread_json(old)["running"])
        projects.archive_thread(self.d, old.id)
        self.assertEqual(c.ask("second"), "Hello")
        active = [t for t in self.d.stores.threads.list() if not t.archived]
        self.assertEqual(len(active), 1)
        self.assertNotEqual(active[0].id, old.id)
        # Simulate the owner's permanent deletion of the archived chat reference.
        self.d.close_thread(active[0].id)
        self.d.stores.threads._delete(active[0].id)
        self.assertEqual(c.ask("third"), "Hello")
        self.assertNotEqual(json.loads((self.d.stores.root / "voice-chat.json").read_text()), active[0].id)

    def test_enrolled_nonowner_wake_refused_hotkey_and_guest_transcribe(self):
        import numpy as np

        class Embedding:
            def embed(self, samples, rate):
                return np.asarray([0.0, 1.0])
        engine = speaker.Engine(lambda _: Embedding(), schedule=False)
        self.addCleanup(speaker.set_engine, speaker.set_engine(engine))
        speaker._write_voiceprint(speaker.Voiceprint(engine.model, 2, [1.0, 0.0], 0.7, 3, 3, "test"))
        self.start()
        c = self.client()
        clip = existing.stc.wav()
        with patch.object(speaker, "voiced_window", side_effect=lambda samples, rate: (samples, 1.0)):
            with self.assertRaisesRegex(wire.LinkError, voice_link.NOT_OWNER.replace(".", r"\.")):
                c.dictate(clip, "wake")
            self.assertEqual(speaker._last["result"], "rejected")
            self.assertEqual(self.backend.calls, [])
            self.assertEqual(c.dictate(clip)["text"], self.words)
            speaker.store().write("guest.json", b'{"guest":true}')
            self.assertEqual(c.dictate(clip, "wake")["text"], self.words)
        self.assertEqual(self.cloud.calls, [])

    def test_default_say_owner_voice_local_only_and_capped(self):
        self.start()
        c = self.client()
        wav = wire.wav_bytes(b"\x01\x00" * 160)
        with patch.object(voice, "tts", return_value=wav) as tts:
            self.assertEqual(c.say("hello"), wav)
            tts.assert_called_once_with("hello", local_only=True)
            c.say("x" * 9000)
            self.assertEqual(tts.call_args.args, ("x" * voice_link.SAY_CHARS,))
        with patch.object(voice, "tts", side_effect=voice.LocalTTSUnavailable("private /secret")):
            with self.assertRaisesRegex(wire.LinkError, "The text could not be spoken"):
                c.say("hello")

    def test_router_lazy_safe_default_small_uncached_completion(self):
        from types import SimpleNamespace
        with patch.object(llm, "chat", return_value=SimpleNamespace(content='{"kind":"ask"}')) as chat, \
                patch.object(models, "tier", return_value="anthropic/claude-haiku-5.5"):
            self.start()
            chat.assert_not_called()
            c = self.client()
            self.assertEqual(c.route("Jarvis hello", "wake")["kind"], "ask")
            args, kw = chat.call_args
            self.assertEqual(args[0], "anthropic/claude-haiku-5.5")
            for kind in voice_link.KINDS:
                self.assertIn(kind, args[1][0]["content"])
            self.assertEqual((kw["tools"], kw["temperature"], kw["timeout"], kw["cache"], kw["max_retries"]),
                             (None, 0, 4.0, False, 0))
            self.assertLessEqual(kw["max_tokens"], 1000)
            chat.reset_mock()
            with patch.object(models, "tier", return_value="deepseek/blocked"):
                self.assertEqual(c.route("hello", "hotkey"), {"kind": "dictate", "text": "hello", "target": ""})
            chat.assert_not_called()

    def test_voice_labels_and_desk_predicate(self):
        self.start()
        self.client().ask("hello")
        thread = self.voice_thread()
        rows = self.d.stores.threads.read_log(thread.id)
        data = next(r["data"] for r in rows if r["kind"] == "user")
        self.assertEqual(user_line(data), "You (voice): hello")
        row = chat_reads._owner_row(data, None, set())
        self.assertEqual(row.role, "owner")
        self.assertIn("via voice", row.marks)


class Concurrency(Base):
    def test_voice_deliver_never_steers_and_default_ask_waits_for_approval(self):
        self.start()
        c = self.client()
        c.ask("create chat")
        thread = self.voice_thread()
        self.fast.block = True
        self.fast.entered.clear()
        self.d.send(thread.id, UserMessage("a running turn"))
        self.assertTrue(self.fast.entered.wait(2))
        existing.eventually(lambda: bool(self.d._sessions[thread.id].questions))
        queued = self.d.deliver(thread.id, UserMessage("voice queues", via="voice", spoken=True))
        self.assertEqual(queued["status"], "queued")
        seen = []
        result, done = self.async_ask(c, seen=seen)
        self.d.bus.publish({"kind": "text_delta", "thread_id": thread.id,
                            "turn_id": "wrong-turn", "data": {"text": "wrong"}})
        self.assertFalse(done.wait(0.15))
        self.assertEqual(seen, [])
        self.assertEqual(self.fast.steered, [])
        self.assertEqual(self.fast.interrupted, 0)
        self.fast.release.set()
        self.assertTrue(done.wait(3))
        self.assertEqual(result, ["Hello"])
        self.assertEqual(seen, ["Hel", "lo"])

    def test_replaced_ask_stops_stream_and_new_ask_gets_its_own_turn(self):
        self.start()
        c = self.client()
        self.fast.block = True
        old_seen = []
        old, old_done = self.async_ask(c, seen=old_seen)
        self.assertTrue(self.fast.entered.wait(2))
        replacement = self.client()
        replacement.ping()
        new_seen = []
        new, new_done = self.async_ask(replacement, text="replacement", seen=new_seen)
        self.assertTrue(old_done.wait(2))
        self.assertIsInstance(old[0], wire.LinkError)
        self.fast.release.set()
        self.assertTrue(new_done.wait(3))
        self.assertEqual(new, ["Hello"])
        self.assertEqual(old_seen, [])
        self.assertEqual(new_seen, ["Hel", "lo"])
        self.assertEqual([m.text for m in self.fast.messages], ["hello", "replacement"])

    def test_timeout_fixed_and_link_usable(self):
        self.start()
        c = self.client()
        self.fast.block = True
        with patch.object(voice_link, "ASK_TIMEOUT_S", 0.15):
            with self.assertRaisesRegex(wire.LinkError, "The voice answer took too long"):
                c.ask("hello")
        c.ping()
        self.fast.release.set()

    def test_slow_router_and_tts_do_not_hold_link_lock(self):
        self.start()
        for op in ("route", "say"):
            with self.subTest(op=op):
                entered, release, finished = threading.Event(), threading.Event(), threading.Event()

                def backend(*args):
                    entered.set()
                    release.wait(3)
                    return '{"kind":"ask"}' if op == "route" else b"RIFF-fake"

                setattr(self.link, "route_model" if op == "route" else "speaker", backend)
                c = self.client()

                def work():
                    try:
                        c.route("hi", "wake") if op == "route" else c.say("hi")
                    except wire.LinkError:
                        pass
                    finally:
                        finished.set()
                worker = threading.Thread(target=work, daemon=True)
                worker.start()
                self.assertTrue(entered.wait(2))
                replacement = self.client()
                replacement.ping()
                release.set()
                self.assertTrue(finished.wait(2))
                worker.join(2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
