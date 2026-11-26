"""Exercise v3 voice from paired PCM frames to a fake playback link."""

from collections import deque
from threading import Condition, Event, current_thread
import unittest
from unittest.mock import patch

import numpy as np

from jarvis.v3.voice.engines import EngineChoice
from jarvis.v3.voice.hearing import FRAME_SAMPLES
from jarvis.v3.voice.session import ANSWER_WORDS, VoiceSession
from jarvis.v3.voice.turns import VoiceTurns


WAIT = 3
RATE = 100
QUIET = np.zeros(FRAME_SAMPLES, dtype=np.float32)


def tone(level):
    return np.resize(np.array([level, -level], dtype=np.float32), FRAME_SAMPLES)


RAW, CANCELLED, SOFT = tone(0.4), tone(0.2), tone(0.04)


class Rows:
    def __init__(self):
        self.changed = Condition()
        self.rows = []

    def add(self, row):
        with self.changed:
            self.rows.append(row)
            self.changed.notify_all()

    def wait(self, predicate):
        with self.changed:
            if not self.changed.wait_for(lambda: predicate(self.rows), WAIT):
                raise AssertionError(f"fixture did not reach expected state ({len(self.rows)} rows)")


class Detector:
    def is_speech(self, frame):
        return float(np.std(frame) > 0.01)


class Transcriber:
    def __init__(self):
        self.lines = deque()
        self.calls = Rows()
        self.warmed = Rows()
        self.closed = Event()
        self.fail = False
        self.hold = None

    def warm(self):
        self.warmed.add(current_thread())

    def transcribe(self, samples, *, names):
        hold, self.hold = self.hold, None
        self.calls.add((float(np.max(np.abs(samples))), names))
        if hold is not None and not hold.wait(WAIT):
            raise TimeoutError("fixture decode did not complete")
        if self.fail:
            raise RuntimeError("private fixture engine detail")
        return self.lines.popleft()

    def close(self):
        self.closed.set()


class Voice:
    def __init__(self, name, *, seconds=0.5, hold=None, fail=False):
        self.name, self.seconds, self.hold, self.fail = name, seconds, hold, fail
        self.calls = Rows()
        self.closed = Event()

    def synthesize(self, text):
        self.calls.add(text)
        if self.hold is not None and not self.hold.wait(WAIT):
            raise TimeoutError("fixture warm-up did not complete")
        if self.fail:
            raise RuntimeError("fixture voice failed")
        return np.ones(int(self.seconds * RATE), dtype=np.int16), RATE

    def close(self):
        self.closed.set()


class Brain:
    def __init__(self):
        self.listener = None
        self.turn = None
        self.inputs = Rows()
        self.warmed = Rows()
        self.pending = []
        self.fail = False

    def warm(self):
        self.warmed.add(current_thread())

    def subscribe(self, listener):
        self.listener = listener
        return lambda: None

    def pending_approvals(self, turn):
        return self.pending if turn == self.turn else []

    def send(self, text, *, input_id, **kwargs):
        if self.fail:
            raise RuntimeError("fixture brain failed")
        self.turn = input_id
        self.emit("user", input_id=input_id)
        self.inputs.add(text)

    def stop(self):
        self.emit("stopped")

    def emit(self, kind, **fields):
        self.listener(dict(fields, kind=kind, turn=self.turn))


class VoiceSessionCheck(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.events, self.audio, self.turn_events = Rows(), Rows(), Rows()
        self.last_chunks = Rows()
        self.brain, self.asr = Brain(), Transcriber()
        self.cpu = Voice("fixture-cpu")
        self.hints = ["Fixture app"]
        self.callback_locks = []
        self.answer = []
        self.turns = VoiceTurns(self.brain, clock=lambda: self.now,
                                on_event=lambda kind, **fields: self.turn_events.add((kind, fields)))
        self.addCleanup(self.turns.close)

    def open(self, *, best=None, start=True, follow=8, answer=None, transcriber=None):
        self.session = VoiceSession(
            self.turns, transcriber=self.asr if transcriber is None else transcriber,
            voices=(self.cpu, best), names=lambda: self.hints, on_event=self.event,
            on_audio=self.chunk, clock=lambda: self.now, detector=Detector(),
            follow_up_s=follow, on_answer=answer,
        )
        self.addCleanup(self.session.close)
        if start:
            self.session.start()
            self.wait_state("resting")
            if transcriber is None:
                self.asr.warmed.wait(bool)

    def record_locks(self):
        self.callback_locks.append((
            self.session._changed._is_owned(), self.session._hearing._changed._is_owned(),
            self.session._speaking._changed._is_owned(),
        ))
        self.session.status()  # The real link can inspect status from a callback.

    def event(self, kind, **fields):
        self.record_locks()
        self.events.add((kind, fields))

    def chunk(self, id, pcm, rate, first, last):
        self.record_locks()
        self.audio.add((id, len(pcm), rate, first))
        if last:
            self.last_chunks.add(id)

    def played(self, id):
        self.last_chunks.wait(lambda rows: id in rows)
        self.session.played(id)

    def of_kind(self, kind):
        return [fields for name, fields in self.events.rows if name == kind]

    def wait_kind(self, kind, count=1):
        self.events.wait(lambda rows: sum(name == kind for name, _ in rows) >= count)
        return self.of_kind(kind)[count - 1]

    def wait_state(self, state, count=1):
        self.events.wait(lambda rows: sum(kind == "state" and fields["state"] == state
                                          for kind, fields in rows) >= count)

    def input(self, text):
        self.brain.inputs.wait(lambda rows: text in rows)

    def frames(self, count, raw=RAW, cancelled=CANCELLED):
        for _ in range(count):
            self.now += 0.02
            self.session.feed(raw, cancelled)

    def utterance(self, text, *, raw=RAW, cancelled=CANCELLED):
        self.asr.lines.append(text)
        self.frames(8, raw, cancelled)
        self.frames(25, QUIET, None if cancelled is None else QUIET)

    def reply(self, text="The fixture answer is ready."):
        count = len(self.of_kind("say")) + 1
        self.brain.emit("reply", text=text)
        id = self.wait_kind("say", count)["id"]
        self.audio.wait(lambda rows: any(row[0] == id for row in rows))
        return id

    def begin_reply(self):
        self.utterance("Jarvis, check the fixture.")
        self.input("check the fixture.")
        return self.reply()

    def advance(self, seconds):
        self.now += seconds
        self.session.feed(QUIET, QUIET)

    def worker_fence(self):
        checked = Event()
        handle = self.session._handle

        def observe(action, *args):
            handle(action, *args)
            if action == "test_fence":
                checked.set()

        with patch.object(self.session, "_handle", side_effect=observe):
            self.session._post("test_fence")
            self.assertTrue(checked.wait(WAIT))

    def test_wake_prefix_and_room_speech_privacy(self):
        self.open()
        for text, expected in (("jarvis open chrome", "open chrome"),
                               ("Hey, Jarvis, what time is it?", "what time is it?")):
            self.utterance(text)
            self.input(expected)
        self.utterance("okay Jarvis!")
        self.wait_state("listening")
        self.utterance("open the fixture folder")
        self.input("open the fixture folder")
        marker = "private fixture conversation"
        with self.assertLogs("jarvis.v3.voice", level="WARNING") as captured:
            # Force a decode failure as a positive control for log capture.
            self.asr.fail = True
            self.frames(8)
            self.frames(25, QUIET, QUIET)
            self.wait_kind("error")
            self.asr.fail = False
            self.utterance(marker)
            self.assertEqual(self.wait_kind("ignored"), {"count": 1})
            self.utterance("We mentioned Jarvis in the " + marker)
            self.assertEqual(self.wait_kind("ignored", 2), {"count": 2})
            self.utterance("Jarvis’s output stayed in the " + marker)
            self.assertEqual(self.wait_kind("ignored", 3), {"count": 3})
        self.assertNotIn(marker, str(captured.output))
        self.assertNotIn("private fixture engine detail", str(captured.output))
        self.assertNotIn(marker, str(self.events.rows))
        self.assertNotIn(marker, str(self.turns.trace()))
        self.assertEqual([row["text"] for row in self.of_kind("heard")],
                         ["open chrome", "what time is it?", "open the fixture folder"])

    def test_wake_partial_opens_listening_and_later_partials_show(self):
        self.open()
        self.asr.lines.extend(["some room words", "okay hey jarvis open", "jarvis open the",
                               "Jarvis, open the folder."])
        hearing = self.session._hearing
        self.frames(25)
        self.asr.calls.wait(lambda rows: len(rows) == 1)
        with hearing._changed:
            self.assertTrue(hearing._changed.wait_for(lambda: hearing._job is None, WAIT))
        self.frames(25)
        self.assertEqual(self.wait_kind("partial")["text"], "open")
        self.wait_state("listening")
        self.frames(25)
        self.assertEqual(self.wait_kind("partial", 2)["text"], "open the")
        self.frames(25, QUIET, QUIET)
        self.input("open the folder.")
        self.assertNotIn("some room words", str(self.events.rows))

    def test_final_without_wake_withdraws_the_provisional_partial(self):
        self.open()
        self.asr.lines.extend(("Jarvis, open", "Private fixture discussion"))
        self.frames(25)
        self.assertEqual(self.wait_kind("partial"), {"text": "open"})
        self.frames(25, QUIET, QUIET)
        self.assertEqual(self.wait_kind("partial", 2), {"text": ""})
        self.wait_kind("ignored")
        self.assertEqual(self.brain.inputs.rows, [])

    def test_wake_only_window_expires(self):
        self.open()
        self.utterance("Jarvis")
        self.wait_state("listening")
        self.advance(6.01)
        self.wait_state("resting", 2)
        self.utterance("look out the window")
        self.wait_kind("ignored")
        self.assertEqual(self.brain.inputs.rows, [])

    def test_delayed_wake_decode_cannot_open_an_expired_window(self):
        self.open()
        release = self.asr.hold = Event()
        self.addCleanup(release.set)
        ended = Event()
        handle = self.session._handle

        def observe(action, *args):
            handle(action, *args)
            if action == "hearing" and args[0] == "speech_end":
                ended.set()

        with patch.object(self.session, "_handle", side_effect=observe):
            self.utterance("Jarvis")
            self.asr.calls.wait(bool)
            self.advance(7)
            release.set()
            self.assertTrue(ended.wait(WAIT))
        self.assertEqual(self.session.status()["state"], "resting")

    def test_stream_selection_and_echo_tail(self):
        self.open(follow=0)
        self.frames(30, RAW, QUIET)
        self.assertFalse(self.session._hearing.speech_active())
        self.assertFalse(self.asr.calls.rows)
        id = self.begin_reply()
        self.assertAlmostEqual(self.asr.calls.rows[0][0], 0.4)
        self.hints[:] = ["Second fixture app"]
        self.utterance("ordinary room words")
        self.wait_kind("ignored")
        self.assertAlmostEqual(self.asr.calls.rows[1][0], 0.2)
        # The wake name is first and is never the only or the last name in the prompt.
        self.assertEqual(self.asr.calls.rows[1][1],
                         ("Jarvis", "Second fixture app", "Codex", "Claude", "Chrome", "VS Code"))
        self.played(id)
        self.wait_state("resting", 2)
        self.utterance("ordinary tail words")
        self.wait_kind("ignored", 2)
        self.assertAlmostEqual(self.asr.calls.rows[2][0], 0.2)
        self.utterance("ordinary later words")
        self.wait_kind("ignored", 3)
        self.assertAlmostEqual(self.asr.calls.rows[3][0], 0.4)
        self.utterance("Jarvis, again", raw=(RAW * 32768).astype(np.int16), cancelled=None)
        self.input("again")
        self.assertAlmostEqual(self.asr.calls.rows[4][0], 0.4, places=4)
        self.reply()
        self.utterance("ordinary room words again", cancelled=None)
        self.wait_kind("ignored", 4)
        self.assertAlmostEqual(self.asr.calls.rows[5][0], 0.4)

    def test_follow_up_cannot_admit_unnamed_audio_in_an_uncancelled_tail(self):
        self.open()
        id = self.begin_reply()
        self.played(id)
        self.wait_state("listening")
        self.utterance("An unnamed fixture utterance", cancelled=None)
        self.wait_kind("ignored")
        self.assertEqual(self.brain.inputs.rows, ["check the fixture."])
        self.utterance("A follow-up after the echo tail", cancelled=None)
        self.input("A follow-up after the echo tail")

    def test_flush_timeout_covers_buffered_duration_after_echo_tail_budget(self):
        self.cpu.seconds = 12
        self.open(follow=0)
        self.begin_reply()
        self.audio.wait(lambda rows: sum(row[1] for row in rows) == self.cpu.seconds * RATE)
        self.session.key(True)
        self.wait_kind("flush")
        self.session.key(False)
        self.advance(2.1)
        self.worker_fence()
        self.frames(30, QUIET, QUIET)
        self.utterance("Ordinary fixture discussion", raw=tone(0.8), cancelled=SOFT)
        self.wait_kind("ignored")
        self.assertAlmostEqual(self.asr.calls.rows[-1][0], 0.04)

    def test_key_ends_without_silence_and_flushes_immediately(self):
        self.open()
        self.begin_reply()
        speaking = self.session._speaking
        with (patch.object(speaking, "stop", wraps=speaking.stop) as stop,
              patch.object(self.turns, "interrupt", wraps=self.turns.interrupt) as interrupt):
            self.session.key(True)
            flush = self.wait_kind("flush")
            self.wait_state("listening")
            self.session.playback_stopped(flush["n"])
            self.asr.lines.append("open the folder")
            self.frames(2)
            self.session.key(False)
            self.input("open the folder")
            interrupt.assert_called_once()
            stop.assert_called_once()
        self.assertEqual(self.turns.trace()[0]["silence_ms"], 0)

    def test_key_utterance_is_bounded_by_release(self):
        self.open()
        with patch("jarvis.v3.voice.hearing.MAX_UTTERANCE_MS", 40):
            self.session.key(True)
            self.asr.lines.append("the complete fixture request")
            self.frames(2)
            self.frames(1, tone(0.75), CANCELLED)
            self.assertFalse(self.asr.calls.rows)
            self.session.key(False)
            self.input("the complete fixture request")
        self.assertEqual(self.asr.calls.rows[0][0], 0.75)

    def test_silent_key_press_preserves_speech_before_vad_confirms_onset(self):
        self.open()
        self.asr.lines.append("Jarvis, open the folder")
        self.frames(4)
        self.session.key(True)
        self.frames(2, QUIET, QUIET)
        self.session.key(False)
        self.frames(4)
        self.frames(25, QUIET, QUIET)
        self.input("open the folder")
        self.assertEqual(len(self.asr.calls.rows), 1)

    def test_follow_up_needs_level_and_deadline(self):
        self.open()
        id = self.begin_reply()
        self.played(id)
        self.wait_state("listening")
        self.advance(0.31)
        self.utterance("a very quiet fixture", raw=SOFT, cancelled=SOFT)
        self.wait_kind("ignored")
        self.utterance("tell me more", cancelled=tone(0.08))  # About 8 dB below the first request.
        self.input("tell me more")
        self.assertAlmostEqual(self.asr.calls.rows[-1][0], 0.4)
        id = self.reply()
        self.played(id)
        self.wait_state("listening", 2)
        self.advance(8.01)
        self.wait_state("resting", 2)
        self.utterance("the conversation expired")
        self.wait_kind("ignored", 2)
        self.utterance("Jarvis, continue")
        self.input("continue")

    def test_follow_up_uses_onset_when_playback_or_window_ends_mid_capture(self):
        self.open()
        id = self.begin_reply()
        self.asr.lines.append("Too early for a follow-up")
        self.frames(8)
        self.played(id)
        self.wait_state("listening")
        self.frames(25, QUIET, QUIET)
        self.wait_kind("ignored")
        self.advance(7.4)
        self.utterance("A follow-up that started in time")
        self.input("A follow-up that started in time")
        self.assertEqual(self.brain.inputs.rows, ["check the fixture.", "A follow-up that started in time"])

    def test_only_stop_cancel_or_wake_interrupts_playback(self):
        self.cpu.seconds = 12
        self.open()
        for index, (command, request) in enumerate([
                ("Jarvis stop", None), ("stop", None), ("cancel", None), ("jarvis open the folder", "open the folder"),
        ], 1):
            self.utterance(f"Jarvis check fixture number {index}")
            self.input(f"check fixture number {index}")
            self.reply()
            with (patch.object(self.session._speaking, "stop", wraps=self.session._speaking.stop) as stop,
                  patch.object(self.turns, "interrupt", wraps=self.turns.interrupt) as interrupt):
                self.utterance("someone talking across the room")
                self.wait_kind("ignored", index)
                self.assertEqual(len(self.of_kind("flush")), index - 1)
                self.utterance(command)
                flush = self.wait_kind("flush", index)
                self.wait_kind("heard", index * 2)
                if request:
                    self.input(request)
                interrupt.assert_called_once()
                stop.assert_called_once()
            self.session.playback_stopped(flush["n"])

    def test_asking_names_and_handled_answer_are_private(self):
        handled = Event()

        def answer(text):
            self.answer.append((text, self.of_kind("heard")[:]))
            handled.set()
            return True

        self.open(answer=answer)
        self.utterance("Jarvis check the fixture")
        self.input("check the fixture")
        self.brain.pending = [{"id": "fake-card", "digest": "fake-digest", "tool": "vault_add",
                               "expires_in": 900,
                               "card": {"title": "Write a fixture note", "rows": []}}]
        self.brain.emit("approval_pending")
        self.wait_state("asking")
        self.assertEqual(self.wait_kind("asking"), {
            "id": "fake-card", "digest": "fake-digest", "tool": "vault_add", "sentence": "Write a fixture note. Approve?",
            "expires_in": 600,
        })
        self.audio.wait(bool)
        completed = Event()
        complete = self.session._complete

        def confirm_played(*args):
            complete(*args)
            completed.set()

        with patch.object(self.session, "_complete", side_effect=confirm_played):
            self.played(self.audio.rows[0][0])
            self.assertTrue(completed.wait(WAIT))
        self.utterance("yes")  # Begins in the echo tail and finishes after it.
        self.wait_kind("ignored")
        self.assertEqual(self.answer, [])
        self.utterance("Jarvis yes")
        self.assertTrue(handled.wait(WAIT))
        self.assertEqual(self.answer, [("Jarvis yes", [{"text": "check the fixture"}])])
        self.assertTrue(set(ANSWER_WORDS) <= set(self.asr.calls.rows[-1][1]))
        self.assertEqual(len(self.of_kind("heard")), 1)
        self.assertEqual(self.brain.inputs.rows, ["check the fixture"])
        self.assertEqual(self.of_kind("flush"), [])
        self.brain.pending.clear()
        self.brain.emit("tool_end", name="vault_add", outcome="ok", changes_state=True)
        self.wait_state("working", 2)

    def test_cpu_first_then_gpu_without_waiting(self):
        release = Event()
        best = Voice("fixture-gpu", hold=release)
        self.open(best=best)
        self.addCleanup(release.set)
        best.calls.wait(bool)
        id = self.begin_reply()
        self.assertEqual(self.cpu.calls.rows, ["The fixture answer is ready."])
        self.assertFalse(self.session.status()["gpu_warm"])
        release.set()
        self.session._warmers[-1].join(WAIT)
        self.assertTrue(self.session.status()["gpu_warm"])
        self.played(id)
        self.wait_state("listening")
        self.utterance("Jarvis another fixture question")
        self.input("another fixture question")
        self.reply("The GPU answer is ready.")
        self.assertEqual(best.calls.rows, ["The voice is ready.", "The GPU answer is ready."])

    def test_key_interrupts_synthesis_during_voice_switch(self):
        warm, synthesize = Event(), Event()
        best = Voice("fixture-gpu", hold=warm)
        self.cpu.hold = synthesize
        self.open(best=best)
        self.addCleanup(warm.set)
        self.addCleanup(synthesize.set)
        self.utterance("Jarvis, start the first fixture")
        self.input("start the first fixture")
        self.brain.emit("reply", text="The first fixture answer.")
        interrupted = self.wait_kind("say")["id"]
        self.cpu.calls.wait(bool)
        warm.set()
        self.session._warmers[-1].join(WAIT)
        self.assertTrue(self.session.status()["gpu_warm"])
        self.session.key(True)
        self.wait_kind("flush")
        self.session.key(False)
        synthesize.set()
        self.utterance("Jarvis, start the second fixture")
        self.input("start the second fixture")
        self.reply("The second fixture answer.")
        self.assertEqual(best.calls.rows, ["The voice is ready.", "The second fixture answer."])
        self.assertNotIn(interrupted, [row[0] for row in self.audio.rows])

    def test_failed_gpu_warmup_keeps_cpu(self):
        best = Voice("fixture-gpu", fail=True)
        self.open(best=best)
        self.session._warmers[-1].join(WAIT)
        self.begin_reply()
        self.assertEqual(self.session.status()["voice"],
                         {"name": "fixture-cpu", "reason": "best voice warm-up failed"})
        self.assertFalse(self.session.status()["gpu_warm"])
        self.assertEqual(len(best.calls.rows), 1)
        self.assertEqual(len(self.cpu.calls.rows), 1)

    def test_only_voice_synthesizes_on_demand(self):
        self.cpu = None
        best = Voice("fixture-gpu")
        self.open(best=best)
        self.assertEqual(best.calls.rows, [])
        self.begin_reply()
        self.assertEqual(best.calls.rows, ["The fixture answer is ready."])

    def test_played_and_deadline_trace_ack_and_errors(self):
        self.open(follow=0)
        delivered = Rows()
        emit_done = self.session._speaking._on_done

        def done(id):
            emit_done(id)
            delivered.add(id)

        with (patch.object(self.session._speaking, "_on_done", side_effect=done),
              patch.object(self.turns, "spoken", wraps=self.turns.spoken) as spoken):
            self.utterance("Jarvis give a fixture answer")
            self.input("give a fixture answer")
            self.brain.emit("tool_start")
            self.assertEqual(self.wait_kind("earcon"), {"name": "ack"})
            first = self.reply()
            delivered.wait(lambda rows: first in rows)
            self.played(first)
            self.wait_state("resting", 2)
            spoken.assert_called_once_with(first)
            trace = self.turns.trace()[0]
            self.assertEqual(trace["silence_ms"], 500)
            self.assertEqual(trace["decode_ms"], 0)
            self.assertAlmostEqual(trace["first_audio_ms"], 500)

            self.utterance("Jarvis give another fixture answer")
            self.input("give another fixture answer")
            second = self.reply()
            delivered.wait(lambda rows: second in rows)
            self.advance(2.49)
            self.worker_fence()
            self.assertEqual(spoken.call_count, 1)
            self.advance(0.02)
            self.wait_state("resting", 3)
            self.assertEqual(spoken.call_args_list[-1].args, (second,))
            self.assertEqual(self.session.status()["counters"]["playback_timeouts"], 1)

            self.cpu.fail = True
            self.utterance("Jarvis try a third fixture")
            self.input("try a third fixture")
            self.brain.emit("reply", text="This sentence will fail to synthesize.")
            self.assertEqual(self.wait_kind("error"), {"reason": "speech_failed"})
            self.wait_state("resting", 4)
            self.brain.fail = True
            self.utterance("Jarvis try a failed turn")
            self.assertEqual(self.wait_kind("error", 2), {"reason": "turn_failed"})
            self.wait_state("resting", 5)

    def test_lifecycle_is_lazy_restartable_and_callbacks_are_unlocked(self):
        self.open(start=False)
        self.assertEqual((self.asr.warmed.rows, self.brain.warmed.rows, self.events.rows), ([], [], []))
        self.assertIsNone(self.session._worker)
        self.session.feed(RAW, CANCELLED)
        self.session.start()
        self.wait_state("resting")
        self.brain.warmed.wait(bool)
        self.asr.warmed.wait(bool)
        self.assertIsNot(self.asr.warmed.rows[0], current_thread())
        self.assertIsNot(self.brain.warmed.rows[0], current_thread())
        self.frames(5)
        self.session.stop()
        self.session.start()
        self.wait_state("resting", 2)
        self.frames(3)
        self.assertFalse(self.session._hearing.speech_active())
        self.frames(12, QUIET, QUIET)
        self.begin_reply()
        self.session.close()
        self.assertTrue(self.asr.closed.is_set() and self.cpu.closed.is_set())
        threads = [self.session._worker, *self.session._warmers, *self.session._hearing._workers,
                   *self.session._speaking._workers]
        self.assertTrue(all(not worker.is_alive() for worker in threads))
        self.assertTrue(all(not any(locks) for locks in self.callback_locks))

    def test_unavailable_has_safe_reason(self):
        self.open(transcriber=lambda: EngineChoice(None, None, "missing fake model"))
        self.wait_state("unavailable")
        self.assertEqual(self.wait_kind("error"), {"reason": "transcriber_unavailable"})
        self.assertEqual(self.session.status()["reason"], "transcriber_unavailable")

    def test_reconnect_discards_an_inflight_transcript(self):
        self.open()
        release = self.asr.hold = Event()
        self.addCleanup(release.set)
        self.utterance("Jarvis stale fixture request")
        self.asr.calls.wait(bool)
        self.session.stop()
        self.session.start()
        self.wait_state("resting", 2)
        self.utterance("Jarvis current fixture request")
        release.set()
        self.input("current fixture request")
        self.assertEqual(self.of_kind("heard"), [{"text": "current fixture request"}])
        self.assertEqual(self.turns.trace()[0]["text"], "current fixture request")

    def test_audio_callback_can_interrupt_its_own_delivery(self):
        self.open()
        old_chunk = self.session._on_audio

        def chunk(id, pcm, rate, first, last):
            old_chunk(id, pcm, rate, first, last)
            if first:
                self.session.key(True)

        with patch.object(self.session, "_on_audio", side_effect=chunk):
            self.begin_reply()
            self.wait_kind("flush")
            self.wait_state("listening")
        self.assertEqual(len(self.audio.rows), 1)
        self.session.key(False)

    def test_callback_reconnect_prevents_submission_from_old_link(self):
        self.open()
        old_event = self.session._on_event

        def event(kind, **fields):
            if kind == "heard":
                self.session.stop()
                self.session.start()
            old_event(kind, **fields)

        with patch.object(self.session, "_on_event", side_effect=event):
            self.utterance("Jarvis old link fixture")
            self.wait_kind("heard")
            self.wait_state("resting", 2)
        self.assertEqual(self.brain.inputs.rows, [])
        self.utterance("Jarvis new link fixture")
        self.input("new link fixture")


if __name__ == "__main__":
    unittest.main()
