"""Delegated work notices with a real ledger and local fake voice engines."""

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from jarvis import config
from jarvis.v2.quiet import QuietHours
from jarvis.v3 import feed, t3_client, work
from jarvis.v3.daemon import Daemon
from jarvis.v3.work_watch import Notice, Result, Watcher
from windows.v3voice_render import PopupState, state_from_event
import voice_session_check as voice
from w2_acceptance_check import Clock, FakeT3


class WorkNotices(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.clock = Clock()
        self.ledger = work.Ledger(self.root / "work" / "threads.jsonl")
        self.feed = feed.Feed(self.root / "feed")
        self.t3 = FakeT3()

    def start(self, ident, status, *, delegated=True, message=False):
        mark = {"delegated": True, "origin": "voice"} if delegated else {}
        self.ledger.record_start(thread_id=ident, project="example", title=f"Task {ident}", branch=None,
                                 run_id=f"run-{ident}", brief_sha256="0" * 64, at=self.clock() - 20,
                                 message=message, **mark)
        self.t3.set(ident, status, status == "waiting")

    def watcher(self, announce, **kwargs):
        return Watcher(ledger=self.ledger, t3=self.t3, feed=self.feed, clock=self.clock,
                       announce=announce, **kwargs)

    def test_only_delegated_categories_once_even_after_restart_or_reentering_wait(self):
        statuses = {"done": "completed", "failure": "failed", "stop": "interrupted", "wait": "waiting"}
        for ident, status in statuses.items():
            self.start(ident, status)
        for ident in ("ordinary", "planner", "executor", "message"):
            self.start(ident, "completed", delegated=False, message=ident == "message")
        notices = []

        def announce(notice):
            notices.append((notice, self.ledger.get(notice.thread_id).status, len(self.feed.briefs())))

        watcher = self.watcher(announce)
        self.assertEqual(watcher.run(), Result("ok", 8, 8, 7, 0))
        expected = [
            Notice("done", "Task done", "finished"), Notice("failure", "Task failure", "stopped", failed=True),
            Notice("stop", "Task stop", "stopped"), Notice("wait", "Task wait", "waiting"),
        ]
        self.assertCountEqual([notice for notice, _, _ in notices], expected)
        for notice, status, posted in notices:
            self.assertEqual(status, statuses[notice.thread_id])
            self.assertGreater(posted, 0)
        watcher.run()
        self.watcher(announce).run()
        self.clock.now += 10
        self.t3.set("wait", "running")
        self.watcher(announce).run()
        self.clock.now += 10
        self.t3.set("wait", "waiting", True)
        self.assertEqual(self.watcher(announce).run(), Result("ok", 1, 1, 1, 0))
        self.assertEqual(len(notices), 4)
        self.t3.set("wait", "completed")
        self.watcher(announce).run()
        self.assertEqual(notices[-1][0], Notice("wait", "Task wait", "finished"))

    def test_written_notice_precedes_announcement_and_exception_changes_nothing(self):
        self.start("done", "completed")
        messages = Mock()
        messages.send.return_value = SimpleNamespace(outcome="sent")
        delivered = []
        messages.send.side_effect = lambda *a, **kw: (
            delivered.append("message") or messages.send.return_value)

        def announce(notice):
            delivered.append(notice)
            raise RuntimeError("fixture private text")

        with self.assertLogs("jarvis.v3.work_watch", "WARNING") as captured:
            self.assertEqual(self.watcher(announce, messages=messages).run(), Result("ok", 1, 1, 1, 0))
        self.assertEqual(delivered, ["message", Notice("done", "Task done", "finished")])
        self.assertEqual(captured.output, [
            "WARNING:jarvis.v3.work_watch:A work announcement could not be queued."
        ])
        self.assertEqual(self.watcher(announce, messages=messages).run(), Result("ok", 0, 0, 0, 0))

    def test_daemon_wires_watcher_only_when_it_has_voice(self):
        with patch.object(config, "V3_DIR", self.root), patch.object(t3_client, "T3", return_value=self.t3):
            for enabled in (False, True):
                with self.subTest(enabled=enabled):
                    ident = "with-voice" if enabled else "without-voice"
                    self.start(ident, "completed")
                    session = Mock() if enabled else None
                    with patch.object(Daemon, "_new_voice", return_value=(None, None, session)), \
                            patch("jarvis.v3.work_watch.Watcher", wraps=Watcher) as built:
                        daemon = Daemon(port=0, voice_link_port=0, data_dir=self.root, phone_host=None)
                        daemon._scheduler._jobs["work-watch"].run()
                    announce = built.call_args.kwargs["announce"]
                    if enabled:
                        self.assertTrue(callable(announce))
                        session.announce.assert_called_once_with(Notice(ident, f"Task {ident}", "finished"))
                    else:
                        self.assertIsNone(announce)


class VoiceNotices(unittest.TestCase):
    notice = Notice("thread-example", 'Build "fixture"', "finished")
    prefix = '[Jarvis just told the owner: task "Build \'fixture\'" finished. Thread id: thread-example.]'

    def fixture(self, *, hour=12, minute=0, start=True):
        f = voice.VoiceSessionCheck()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.open(start=start)
        root = self.enterContext(TemporaryDirectory())
        f.session._quiet = QuietHours(root, clock=lambda: datetime(2026, 1, 1, hour, minute)).is_quiet
        return f

    def play_notice(self, f):
        f.session.announce(self.notice)
        ident = f.wait_kind("say")["id"]
        f.played(ident)
        f.wait_state("resting", 2)
        f.advance(0.31)

    def count(self, f, name):
        return f.session.status()["counters"][name]

    def key_request(self, f, text):
        f.session.key(True)
        f.asr.lines.append(text)
        f.frames(8)
        f.session.key(False)

    def test_sentences_titles_and_return_to_resting(self):
        cases = (
            (self.notice, "Finished", "Build 'fixture'", "finished"),
            (Notice("thread-example", "Line​ one\nline “two”", "stopped", True),
             "Failed", "Line one line 'two'", "failed"),
            (Notice("thread-example", "x" * 80, "stopped"), "Stopped", "x" * 60, "stopped"),
            (Notice("thread-example", "\n‮ ​", "waiting"),
             "Waiting on you", "a background task", "is waiting on you"),
        )
        for notice, label, title, phrase in cases:
            with self.subTest(label=label):
                f = self.fixture()
                f.session.announce(notice)
                sentence = f'Your task "{title}" {phrase}.'
                self.assertEqual(f.wait_kind("notice"), {"title": label, "text": title})
                say = f.wait_kind("say")
                self.assertEqual(say["text"], sentence)
                f.played(say["id"])
                f.wait_state("resting", 2)
                self.assertEqual(f.cpu.calls.rows, [sentence])
                self.assertEqual(self.count(f, "announced"), 1)

    def test_quiet_hour_boundaries(self):
        for hour, minute, quiet in ((23, 0, True), (2, 0, True), (6, 59, True),
                                    (7, 0, False), (22, 59, False)):
            with self.subTest(hour=hour, minute=minute):
                f = self.fixture(hour=hour, minute=minute)
                f.session.announce(self.notice)
                f.wait_kind("notice")
                if quiet:
                    self.assertEqual(f.wait_kind("earcon"), {"name": "notice"})
                    self.assertEqual(f.cpu.calls.rows, [])
                    self.assertEqual(self.count(f, "announced_quietly"), 1)
                else:
                    f.wait_kind("say")
                    self.assertEqual(self.count(f, "announced"), 1)

    def conversation(self, stage, add_notice):
        f = self.fixture()
        if stage == "listening":
            f.utterance("Jarvis")
            f.wait_state("listening")
        else:
            f.utterance("Jarvis, check the fixture")
            f.input("check the fixture")
            f.wait_state("working")
        if stage in ("speaking", "follow-up"):
            ident = f.reply()
            f.wait_state("speaking")
            f.last_chunks.wait(lambda rows: ident in rows)
            if stage == "follow-up":
                f.played(ident)
                f.wait_state("listening")
        if stage == "asking":
            f.brain.pending = [{"id": "card", "digest": "example-digest", "tool": "fixture",
                                "expires_in": 900, "card": {"title": "Fixture approval", "rows": []}}]
            f.brain.emit("approval_pending")
            f.wait_kind("asking")
            f.last_chunks.wait(lambda rows: "card" in rows)
        if add_notice:
            f.session.announce(self.notice)
            self.assertEqual(f.wait_kind("earcon"), {"name": "notice"})
            self.assertEqual(f.of_kind("notice"), [{"title": "Finished", "text": "Build 'fixture'"}])
        f.worker_fence()
        events = [(kind, {k: v for k, v in fields.items() if k not in {"id", "turn"}})
                  for kind, fields in f.events.rows if kind not in {"notice", "earcon"}]
        return events, [row[1:] for row in f.audio.rows], f.cpu.calls.rows

    def test_busy_conversation_keeps_its_events_and_audio(self):
        for stage in ("listening", "working", "speaking", "asking", "follow-up"):
            with self.subTest(stage=stage):
                self.assertEqual(self.conversation(stage, True), self.conversation(stage, False))

    def test_disconnected_notices_are_counted_and_never_replayed(self):
        f = self.fixture(start=False)
        f.session.announce(self.notice)
        self.assertEqual(self.count(f, "announcements_dropped"), 1)
        f.session.start()
        f.wait_state("resting")
        f.worker_fence()
        self.assertEqual(f.of_kind("notice"), [])
        f.session.detach()
        f.session.announce(self.notice)
        self.assertEqual(self.count(f, "announcements_dropped"), 2)

    def test_burst_has_only_three_speech_candidates_and_overflow_still_chimes(self):
        f = self.fixture()
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        handle = f.session._handle

        def block(action, *args):
            if action == "pause_fixture":
                entered.set()
                release.wait(voice.WAIT)
            else:
                handle(action, *args)

        with patch.object(f.session, "_handle", side_effect=block):
            f.session._post("pause_fixture")
            self.assertTrue(entered.wait(voice.WAIT))
            for index in range(6):
                f.session.announce(Notice(f"thread-{index}", f"Task {index}", "finished"))
            release.set()
            f.wait_kind("earcon", 5)
        self.assertEqual([row["text"] for row in f.of_kind("notice")], [f"Task {i}" for i in range(6)])
        self.assertEqual([say["text"] for say in f.of_kind("say")], ['Your task "Task 0" finished.'])
        self.assertEqual(self.count(f, "announced_quietly"), 5)
        self.assertEqual(self.count(f, "announcements_dropped"), 3)

    def test_interruptions_use_the_normal_speech_path(self):
        for interruption in ("Jarvis", "stop", "key"):
            with self.subTest(interruption=interruption):
                f = self.fixture()
                f.session.announce(self.notice)
                ident = f.wait_kind("say")["id"]
                f.last_chunks.wait(lambda rows: ident in rows)
                if interruption == "key":
                    f.session.key(True)
                else:
                    f.utterance(interruption)
                f.wait_kind("flush")
                f.session.played(ident)
                f.worker_fence()
                self.assertEqual(self.count(f, "played"), 0)
                self.assertIsNone(f.session._notice_follow)

    def test_wake_context_is_one_turn_and_keeps_voice_text_clean(self):
        f = self.fixture()
        self.play_notice(f)
        f.utterance("What happened?")
        self.assertEqual(f.wait_kind("ignored"), {"count": 1})
        self.assertEqual(self.count(f, "ignored"), 1)
        self.assertEqual(f.of_kind("heard"), [])
        self.assertEqual(f.brain.inputs.rows, [])
        self.assertEqual(f.session.status()["state"], "resting")
        f.utterance("Jarvis, What happened?")
        f.input(self.prefix + "\n\nWhat happened?")
        heard = f.wait_kind("heard")
        self.assertEqual(heard, {"text": "What happened?"})
        self.assertEqual(state_from_event(PopupState(), dict(heard, kind="heard")).line, "What happened?")
        self.assertEqual(f.turns.trace()[0]["text"], "What happened?")
        self.key_request(f, "What else?")
        f.input("What else?")
        self.assertEqual(f.brain.inputs.rows, [self.prefix + "\n\nWhat happened?", "What else?"])

    def test_wake_and_talk_key_context_deadline(self):
        for mode, age, prefixed in (("wake", 59, True), ("key", 59, True),
                                    ("wake", 61, False), ("key", 61, False)):
            with self.subTest(mode=mode, age=age):
                f = self.fixture()
                self.play_notice(f)
                f.advance(age)
                if mode == "key":
                    self.key_request(f, "What happened?")
                else:
                    f.utterance("Jarvis, What happened?")
                request = self.prefix + "\n\nWhat happened?" if prefixed else "What happened?"
                f.input(request)


if __name__ == "__main__":
    unittest.main()
