"""Package D backend/concurrency regressions: real daemon, temporary HOME,
ephemeral ports, fake speech engines and providers; no network/model calls.

Spoken questions follow the selected HUD chat. Only its id is stored separately.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("voice_link_existing", Path(__file__).with_name("voice_link_check.py"))
existing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(existing)

from jarvis import llm, speaker, voice
from jarvis.v2 import chat_reads, projects, voice_link
from jarvis.v2.daemon import Daemon
from jarvis.v2.desk import desk_message
from jarvis.v2.discord.mirror import user_line
from jarvis.v2.model import ProviderName as PN, Role
from jarvis.v2.provider import Event, EventKind as EK, UserMessage
from jarvis.v2.stores import Stores
from windows import voicewire as wire


class Streaming(existing.rac.Fake):
    def __init__(self, name=PN.FAST):
        super().__init__(name)
        self.messages, self.steered, self.desk = [], [], []
        self.entered, self.release = threading.Event(), threading.Event()
        self.block = False
        self.ask_owner = True
        self.interrupted = 0

    def send(self, h, message):
        self.messages.append(message)
        self.desk.append(desk_message(h.native.brief, message))
        self.entered.set()
        yield Event(EK.TURN_STARTED, h.thread_id)
        if self.block:
            # A question/approval wait is not a turn end.
            if self.ask_owner:
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

    def start(self):
        super().start()
        self.chosen = self.d.open_thread(self.d.stores.projects.inbox().id, "chat", "fast", {}, title="Project notes")
        self.select(self.chosen.id)

    def select(self, ident):
        code, body = existing.HudLaunch.post(self, self.d.face_port, "/voice/chat-target", {"thread_id": ident})
        self.assertEqual(code, 200, body)
        return body["thread_id"]

    def voice_thread(self):
        return self.chosen

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
    def test_unknown_model_kind_falls_back_to_original_speech(self):
        self.start()
        c = self.client()
        for kind in ("run_shell", "", 7, None):
            for source, fallback in (("hotkey", "dictate"), ("wake", "ask")):
                with self.subTest(kind=kind, source=source):
                    raw = json.dumps({"kind": kind, "text": "model replacement",
                                      "target": "untrusted target"})
                    expected = {"kind": fallback, "text": "original speech",
                                "target": "literal" if source == "hotkey" else ""}
                    self.assertEqual(voice_link.parse_intent(raw, source, "original speech"), expected)
                    with patch.object(self.link, "route_model", return_value=raw):
                        self.assertEqual(c.route("original speech", source), expected)

    def test_selected_chat_reused_after_restart_and_owner_message(self):
        self.start()
        c = self.client()
        seen = []
        self.assertEqual(c.ask("hello", seen.append), "Hello")
        self.assertEqual(seen, ["Hel", "lo"])
        self.assertEqual(c.ask("again"), "Hello")
        thread = self.voice_thread()
        self.assertEqual(self.d.thread_json(thread)["title"], "Project notes")
        self.assertEqual(len(self.d.stores.threads.list()), 1)
        self.assertEqual(thread.provider, PN.FAST)
        self.assertTrue(self.d.stores.projects.get(thread.project_id).inbox)
        for message in self.fast.messages:
            self.assertEqual((message.origin, message.via, message.desk, message.spoken),
                             ("owner", "voice", False, True))
        self.assertEqual(self.fast.desk, [False, False])
        self.assertEqual(json.loads((self.d.stores.root / "voice-target.json").read_text()), thread.id)
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

    def test_no_target_is_actionable_and_never_creates_a_chat(self):
        self.start()
        c = self.client()
        self.assertEqual(self.link.asker("direct", lambda _: None), "Hello")
        (self.d.stores.root / "voice-target.json").unlink()
        with self.assertRaisesRegex(wire.LinkError, voice_link.ASK_NO_TARGET):
            c.ask("no target")
        self.assertEqual([t.id for t in self.d.stores.threads.list()], [self.chosen.id])
        self.select(self.chosen.id)
        self.assertEqual(c.ask("again in my chat"), "Hello")

    def test_owner_only_target_and_ineligible_chat_clear_previous_selection(self):
        self.start()
        post = existing.HudLaunch.post
        arg = {"thread_id": None}
        self.assertEqual(post(self, self.d.port, "/voice/chat-target", arg)[0], 403)
        self.assertEqual(post(self, self.d.face_port, "/voice/chat-target", arg, origin=False)[0], 403)
        self.assertEqual(post(self, self.d.face_port, "/voice/chat-target", {"thread_id": "../x"})[0], 400)
        self.assertEqual(post(self, self.d.face_port, "/voice/chat-target", {})[0], 400)
        self.assertEqual(self.link.asker("still selected", lambda _: None), "Hello")
        c = self.client()
        for changes in ({"role": Role.IMPLEMENTER}, {"task_id": "abcd1234"},
                        {"archived": "2026-11-16T00:00:00Z"}):
            with self.subTest(changes=changes):
                row = self.d.stores.threads.create(**{"project_id": self.chosen.project_id,
                    "role": Role.CHAT, "provider": PN.FAST, **changes})
                self.d.stores.threads.save(row)
                self.select(self.chosen.id)
                self.assertIsNone(self.select(row.id))
                with self.assertRaisesRegex(wire.LinkError, voice_link.ASK_NO_TARGET):
                    c.ask("never sent")
                self.assertEqual(self.d.stores.threads.read_log(row.id), [])
        self.assertEqual(len(self.fast.messages), 1)

    def test_selected_cli_chat_uses_its_provider_and_shared_history(self):
        self.start()
        c = self.client()
        self.d.voice_chat.select(None)
        for name in (PN.CLAUDE, PN.CODEX):
            with self.subTest(provider=name):
                provider = Streaming(name)
                self.d.providers[name] = provider
                thread = self.d.open_thread(self.chosen.project_id, "chat", name.value, {})
                self.assertEqual(self.select(thread.id), thread.id)
                self.d.send(thread.id, UserMessage("typed first", via="hud", desk=True))
                existing.eventually(lambda: not self.d.thread_json(thread)["running"])
                self.assertEqual(c.ask("spoken next"), "Hello")
                self.assertEqual([m.text for m in provider.messages], ["typed first", "spoken next"])
                records = self.d.stores.threads.read_log(thread.id)
                terminal = [r for r in records if r["kind"] == "turn_finished"]
                self.assertFalse(terminal[0].get("native_voice_reply"))
                self.assertTrue(terminal[1]["native_voice_reply"])
                self.assertEqual([(m.via, m.desk) for m in provider.messages], [("hud", True), ("voice", False)])
        self.assertEqual(self.fast.messages, [])

    def test_archived_deleted_or_stale_project_clears_selection(self):
        self.start()
        c = self.client()
        self.assertEqual(c.ask("first"), "Hello")
        existing.eventually(lambda: not self.d.thread_json(self.chosen)["running"])
        projects.archive_thread(self.d, self.chosen.id)
        with self.assertRaisesRegex(wire.LinkError, voice_link.ASK_NO_TARGET):
            c.ask("archived")
        self.assertIsNone(json.loads((self.d.stores.root / "voice-target.json").read_text()))
        self.assertIsNone(self.select("00000000"))
        project = self.d.stores.projects.create("Other", str(self.root))
        self.d.stores.projects.save(project)
        other = self.d.open_thread(project.id, "chat", "fast", {})
        self.select(other.id)
        project.archived = "2026-11-18T00:00:00Z"
        self.d.stores.projects.save(project)
        with self.assertRaisesRegex(wire.LinkError, voice_link.ASK_NO_TARGET):
            c.ask("project archived")
        self.assertEqual(self.d.stores.threads.read_log(other.id), [])

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

    def test_route_uses_local_decider_and_preserves_original_on_failure(self):
        from jarvis.v2 import voice_intent
        with patch.object(voice_intent, "route") as local, patch.object(llm, "chat") as cloud:
            self.start()
            c = self.client()
            local.assert_not_called()
            local.return_value = {"kind": "open_app", "text": "bring up Slack", "target": "Slack"}
            self.assertEqual(c.route("bring up Slack", "wake"), local.return_value)
            local.assert_called_once_with("bring up Slack", "wake")
            local.side_effect = RuntimeError("private diagnostic")
            for source, kind in (("hotkey", "dictate"), ("wake", "ask")):
                self.assertEqual(c.route("bring up Slack", source),
                                 {"kind": kind, "text": "bring up Slack",
                                  "target": "literal" if source == "hotkey" else ""})
            cloud.assert_not_called()

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
    def test_ask_line_limit_includes_escaping_and_newline_and_keeps_connection(self):
        self.start()
        c = self.client()
        cap = 192
        with patch.object(wire, "MAX_LINE", cap):
            for delta in (False, True):
                for excess in (0, 1):
                    with self.subTest(delta=delta, excess=excess):
                        prefix = '\u2603\n"'
                        reply = {"id": c._id + 1, "text": prefix}
                        reply.update({"event": "delta"} if delta else {"ok": True, "done": True})
                        answer = prefix + "x" * (cap - len(wire.json_line(reply)) + excess)

                        def asker(_text, on_delta):
                            if delta:
                                on_delta(answer)
                                return "end"
                            return answer

                        self.link.asker = asker
                        seen = []
                        if excess:
                            with self.assertRaises(wire.LinkError) as caught:
                                c.ask("hello", seen.append)
                            self.assertEqual(str(caught.exception), voice_link.ASK_FAILED)
                            self.assertEqual(seen, [])
                        else:
                            self.assertEqual(c.ask("hello", seen.append), "end" if delta else answer)
                            self.assertEqual(seen, [answer] if delta else [])
                        c.ping()

    def test_voice_deliver_never_steers_and_default_ask_waits_for_approval(self):
        self.start()
        c = self.client()
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

    def test_steer_replies_to_the_selected_running_turn_without_duplicate_playback(self):
        self.start()
        c = self.client()
        self.fast.block, self.fast.ask_owner = True, False
        turn = self.d.send(self.chosen.id, UserMessage("typed", via="hud", desk=True))
        self.assertTrue(self.fast.entered.wait(2))
        seen = []
        result, done = self.async_ask(c, seen=seen)
        existing.eventually(lambda: bool(self.fast.steered))
        self.fast.release.set()
        self.assertTrue(done.wait(3))
        self.assertEqual(result, ["Hello"])
        self.assertEqual(seen, [])  # a steer is spoken when the provider confirms consumption
        log = self.d.stores.threads.read_log(self.chosen.id)
        self.assertEqual([r["turn_id"] for r in log if r["kind"] == "user"], [turn, turn])
        self.assertTrue(next(r for r in log if r["kind"] == "turn_finished")["native_voice_reply"])

    def test_speech_after_turn_finished_but_before_cleanup_queues_instead_of_steering(self):
        self.start()
        c = self.client()
        finished, release = threading.Event(), threading.Event()
        real_finish = self.d._finish

        def finish(session, event):
            real_finish(session, event)
            finished.set()
            release.wait(3)

        with patch.object(self.d, "_finish", side_effect=finish):
            self.d.send(self.chosen.id, UserMessage("first"))
            self.assertTrue(finished.wait(2))
            result, done = self.async_ask(c)
            try:
                existing.eventually(lambda: bool(self.d._queues.get(self.chosen.id)))
                self.assertEqual(self.fast.steered, [])
            finally:
                release.set()
            self.assertTrue(done.wait(3))
        self.assertEqual(result, ["Hello"])
        self.assertEqual([m.text for m in self.fast.messages], ["first", "hello"])

    def test_interrupt_fallback_preserves_answer_ownership_and_provenance(self):
        from jarvis.v2.desk import SelectedVoiceMessage
        from jarvis.v2.provider import SteerRefused
        self.start()
        c = self.client()
        self.fast.block, self.fast.ask_owner = True, False
        self.d.send(self.chosen.id, UserMessage("first"))
        self.assertTrue(self.fast.entered.wait(2))
        with patch.object(self.fast, "steer", side_effect=SteerRefused("unsupported", fallback="interrupt")):
            self.assertEqual(c.ask("new question"), "Hello")
        self.assertGreaterEqual(self.fast.interrupted, 1)
        self.assertIsInstance(self.fast.messages[-1], SelectedVoiceMessage)
        self.assertTrue(self.fast.messages[-1].text.endswith("new question"))
        rows = self.d.stores.threads.read_log(self.chosen.id)
        self.assertTrue([r for r in rows if r["kind"] == "turn_finished"][-1]["native_voice_reply"])

    def test_queued_ask_is_pinned_when_hud_selects_another_chat(self):
        self.start()
        c = self.client()
        self.fast.block = True
        self.d.send(self.chosen.id, UserMessage("await owner's answer"))
        existing.eventually(lambda: bool(self.d._sessions[self.chosen.id].questions))
        result, done = self.async_ask(c, text="follow up")
        existing.eventually(lambda: bool(self.d._queues.get(self.chosen.id)))
        other = self.d.open_thread(self.chosen.project_id, "chat", "fast", {})
        self.select(other.id)
        self.fast.block = False
        self.fast.release.set()
        self.assertTrue(done.wait(3))
        self.assertEqual(result, ["Hello"])
        self.assertEqual(c.ask("new target"), "Hello")
        def words(thread):
            return [r["data"]["typed"] for r in self.d.stores.threads.read_log(thread.id) if r["kind"] == "user"]
        self.assertEqual(words(self.chosen), ["await owner's answer", "follow up"])
        self.assertEqual(words(other), ["new target"])

    def test_unconsumed_steer_follows_its_new_turn(self):
        self.start()
        c = self.client()
        self.fast.block, self.fast.ask_owner = True, False
        with patch.object(self.fast, "undelivered", create=True, side_effect=lambda _: self.fast.steered[:]):
            self.d.send(self.chosen.id, UserMessage("typed"))
            self.assertTrue(self.fast.entered.wait(2))
            seen = []
            result, done = self.async_ask(c, text="steer which runs later", seen=seen)
            existing.eventually(lambda: bool(self.fast.steered))
            self.fast.release.set()
            self.assertTrue(done.wait(3))
        self.assertEqual(result, ["Hello"])
        self.assertEqual(seen, ["Hel", "lo"])
        self.assertEqual([m.text for m in self.fast.messages], ["typed", "steer which runs later"])
        log = self.d.stores.threads.read_log(self.chosen.id)
        ends = [r for r in log if r["kind"] == "turn_finished"]
        self.assertFalse(ends[0].get("native_voice_reply"))
        self.assertTrue(ends[1]["native_voice_reply"])

    def test_delivery_failure_does_not_leak_playback_ownership(self):
        from jarvis.v2.daemon import DaemonError
        self.start()
        c = self.client()
        with patch.object(self.d, "deliver", side_effect=DaemonError("broken session")):
            with self.assertRaisesRegex(wire.LinkError, voice_link.ASK_FAILED):
                c.ask("never")
        self.assertEqual(c.ask("link still usable"), "Hello")

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
