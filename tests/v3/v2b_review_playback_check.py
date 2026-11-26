import unittest

import numpy as np

from jarvis.v3.voice.hearing import FRAME_SAMPLES
from v2b_review_admission_check import Fixture
from voice_session_check import CANCELLED, tone


class PlaybackReview(unittest.TestCase):
    def test_unacknowledged_flush_keeps_buffered_echo_out(self):
        f = Fixture(self)
        f.voice.seconds = 12
        f.utter("Jarvis, check the fixture.")
        f.brain.emit("reply", text="Jarvis, repeat the fixture request.")
        f.wait_action("delivered", 0)
        f.session.key(True)
        f.wait_event("flush", n=1)
        f.session.key(False)
        f.fence()
        f.now += 2.1
        self.check_echo_rejected(f)

    def test_played_timeout_does_not_trust_raw_while_playback_is_late(self):
        f = Fixture(self)
        f.utter("Jarvis, check the fixture.")
        f.brain.emit("reply", text="Jarvis, repeat the fixture request.")
        f.wait_action("delivered", 0)
        f.now += 3.5
        f.fence()
        self.assertEqual(f.session.status()["counters"]["playback_timeouts"], 1)
        f.now += 0.31
        self.check_echo_rejected(f)

    def check_echo_rejected(self, f):
        def decode(samples, *, names):
            return ("Jarvis, repeat the fixture request." if max(samples) > 0.7
                    else "Quiet room discussion")

        f.asr.transcribe = decode
        previous = f.actions.rows.count("speech_end")
        f.frames(8, tone(0.8), tone(0.04))
        f.end()
        f.wait_action("speech_end", previous)
        self.assertEqual(f.brain.inputs.rows, ["check the fixture."])

    def test_key_and_stop_interrupt_between_sentences(self):
        for control in ("key", "stop"):
            with self.subTest(control=control):
                f = Fixture(self)
                f.utter("Jarvis, check the fixture.")
                f.brain.emit("delta", text="The first fixture sentence is ready. "
                             "The second fixture sentence is ready. The third is still forming")
                f.wait_action("delivered", 0)
                f.played(f.audio.rows[0])
                self.assertEqual(len(set(f.audio.rows)), 1)
                if control == "key":
                    f.session.key(True)
                    f.session.key(False)
                    f.fence()
                else:
                    f.utter("stop")
                f.brain.emit("reply", text="")
                f.fence()
                self.assertEqual(sum(kind == "say" for kind, _ in f.events.rows), 1)
                self.assertIn(("flush", {"n": 1}), f.events.rows)

    def test_capture_keeps_raw_prefix_when_playback_starts_mid_utterance(self):
        f = Fixture(self)
        f.utter("Jarvis, check the fixture.")
        previous = f.actions.rows.count("speech_end")
        decoded = []

        def decode(samples, *, names):
            decoded.append(samples.copy())
            return "Jarvis, open the fixture folder."

        f.asr.transcribe = decode
        f.frames(8, tone(0.7), CANCELLED)
        f.brain.emit("reply", text="Here is a fixture answer.")
        f.audio.wait(bool)
        f.frames(8, tone(0.9), CANCELLED)
        f.end()
        f.wait_action("speech_end", previous)
        voiced = np.abs(decoded[-1][decoded[-1] != 0])
        np.testing.assert_allclose(voiced, np.repeat([0.7, 0.2], 8 * FRAME_SAMPLES))

    def test_empty_key_press_does_not_drop_active_wake_request(self):
        f = Fixture(self)

        def decode(samples, *, names):
            return ("Jarvis, check the fixture." if max(samples) > 0.7
                    else "Jarvis, open the fixture folder.")

        f.asr.transcribe = decode
        f.frames(8)
        f.session.key(True)
        f.session.key(False)
        f.fence()
        f.end()
        f.frames(8, tone(0.8), CANCELLED)
        f.end()
        f.brain.inputs.wait(lambda rows: "check the fixture." in rows)
        self.assertEqual(f.brain.inputs.rows, ["open the fixture folder.", "check the fixture."])


if __name__ == "__main__":
    unittest.main()
