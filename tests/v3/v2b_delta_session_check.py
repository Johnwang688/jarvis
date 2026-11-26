from threading import Event
import unittest

from v2b_review_admission_check import Fixture
from voice_session_check import tone


class SessionDelta(unittest.TestCase):
    def test_wake_window_includes_speech_after_wake_before_decode_returns(self):
        fixture = Fixture(self)
        release = fixture.asr.hold = Event()
        self.addCleanup(release.set)
        fixture.asr.lines.append("Jarvis")
        fixture.frames(8)
        fixture.end()
        fixture.asr.calls.wait(bool)

        fixture.asr.lines.append("Open the fixture folder.")
        fixture.frames(8)
        fixture.end()
        release.set()
        fixture.wait_action("speech_end", 1)
        self.assertEqual(fixture.brain.inputs.rows, ["Open the fixture folder."])

    def test_talk_key_stop_retires_remaining_followup_window(self):
        fixture = Fixture(self)
        fixture.utter("Jarvis, open the fixture folder.")
        fixture.finish_reply()
        fixture.now += 0.4
        fixture.utter("Tell me more about the fixture.")
        self.assertTrue(fixture.turns.can_interrupt())

        fixture.session.key(True)
        fixture.session.key(False)
        fixture.fence()
        self.assertIn(("flush", {"n": 1}), fixture.events.rows)
        self.assertFalse(fixture.turns.can_interrupt())
        fixture.utter("Room discussion about the fixture.")

        self.assertEqual(fixture.brain.inputs.rows, ["open the fixture folder.",
                                                   "Tell me more about the fixture."])

    def test_stopping_while_listening_retires_the_followup_window(self):
        for how in ("key", "stop word"):
            with self.subTest(how=how):
                fixture = Fixture(self)
                fixture.utter("Jarvis, open the fixture folder.")
                fixture.finish_reply()
                fixture.now += 0.4
                if how == "key":
                    previous = fixture.actions.rows.count("speech_end")
                    fixture.session.key(True)
                    fixture.session.key(False)
                    fixture.wait_action("speech_end", previous)
                else:
                    fixture.utter("stop")
                fixture.utter("Room discussion about the fixture.")
                self.assertEqual(fixture.brain.inputs.rows, ["open the fixture folder."])

    def test_followup_window_uses_confirmation_time_when_worker_lags(self):
        fixture = Fixture(self)
        fixture.utter("Jarvis, open the fixture folder.")
        fixture.brain.emit("reply", text="Here is the fixture answer.")
        fixture.wait_action("delivered", 0)
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        handle = fixture.session._handle

        def pause(action, *args):
            if action == "review_gate":
                entered.set()
                release.wait(3)
            handle(action, *args)

        fixture.session._handle = pause
        fixture.session._post("review_gate")
        self.assertTrue(entered.wait(3))
        try:
            fixture.session.played(fixture.audio.rows[0])
            fixture.now += 0.4
            fixture.asr.lines.append("Tell me more about the fixture.")
            fixture.frames(8, cancelled=None)
            fixture.end()
        finally:
            release.set()
        fixture.wait_action("speech_end", 1)
        self.assertEqual(fixture.brain.inputs.rows, ["open the fixture folder.",
                                                   "Tell me more about the fixture."])

    def test_speech_started_during_reply_cannot_use_an_earlier_followup_window(self):
        fixture = Fixture(self)
        fixture.utter("Jarvis, open the fixture folder.")
        fixture.finish_reply()
        fixture.now += 0.4
        fixture.utter("Tell me more about the fixture.")
        fixture.brain.emit("reply", text="Here is another fixture answer.")
        fixture.wait_action("delivered", 1)

        fixture.asr.lines.append("Room discussion about the fixture.")
        fixture.frames(8)
        fixture.played(fixture.audio.rows[-1])
        fixture.end()
        fixture.wait_action("speech_end", 2)
        self.assertEqual(fixture.brain.inputs.rows, ["open the fixture folder.",
                                                   "Tell me more about the fixture."])

    def test_played_before_last_chunk_does_not_make_echo_trusted(self):
        fixture = Fixture(self)
        fixture.voice.seconds = 1
        chunks = []

        def audio(sentence, pcm, rate, first, last):
            chunks.append((first, last))
            if first:
                fixture.session.played(sentence)

        fixture.session.attach(fixture.session._on_event, audio)
        fixture.utter("Jarvis, open the fixture folder.")
        fixture.brain.emit("reply", text="Here is the fixture answer.")
        fixture.wait_action("delivered", 0)
        self.assertEqual(chunks[0], (True, False))
        self.assertEqual(chunks[-1], (False, True))
        fixture.now += 0.4

        def decode(samples, *, names):
            if max(samples) > 0.7:
                return "Jarvis, repeat the fixture answer."
            return "Room discussion about the fixture."

        fixture.asr.transcribe = decode
        fixture.frames(8, tone(0.8), tone(0.04))
        fixture.end()
        fixture.wait_action("speech_end", 1)
        self.assertEqual(fixture.brain.inputs.rows, ["open the fixture folder."])

    def test_repeated_flush_ack_does_not_unmask_newly_flushed_audio(self):
        fixture = Fixture(self)
        fixture.voice.seconds = 12

        def play_and_stop(request, delivered):
            fixture.utter(f"Jarvis, {request}")
            fixture.brain.emit("reply", text="Jarvis, repeat the fixture request.")
            fixture.wait_action("delivered", delivered)
            previous = fixture.actions.rows.count("speech_end")
            fixture.session.key(True)
            fixture.session.key(False)
            fixture.wait_action("speech_end", previous)
            fixture.fence()
            return [fields["n"] for kind, fields in fixture.events.rows if kind == "flush"][-1]

        first = play_and_stop("open the fixture folder.", 0)
        fixture.session.playback_stopped(first)
        fixture.fence()
        fixture.now += 0.4
        second = play_and_stop("check the fixture folder.", 1)
        self.assertGreater(second, first)
        # A second acknowledgement of the first flush arrives before the
        # helper confirms the second flush.
        fixture.session.playback_stopped(first)
        fixture.fence()
        fixture.now += 0.4

        def decode(samples, *, names):
            if max(samples) > 0.7:
                return "Jarvis, repeat the fixture request."
            return "Room discussion about the fixture."

        fixture.asr.transcribe = decode
        previous = fixture.actions.rows.count("speech_end")
        fixture.frames(8, tone(0.8), tone(0.04))
        fixture.end()
        fixture.wait_action("speech_end", previous)
        self.assertEqual(fixture.brain.inputs.rows, ["open the fixture folder.",
                                                   "check the fixture folder."])

        fixture.session.playback_stopped(second)
        fixture.now += 0.4
        fixture.session.playback_stopped(second)  # A duplicate cannot restart the tail.
        fixture.frames(8, tone(0.8), tone(0.04))
        fixture.end()
        fixture.wait_action("speech_end", previous + 1)
        self.assertEqual(fixture.brain.inputs.rows, ["open the fixture folder.",
                                                   "check the fixture folder.",
                                                   "repeat the fixture request."])


if __name__ == "__main__":
    unittest.main()
