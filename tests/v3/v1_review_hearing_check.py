"""Lifecycle and PCM regressions for the v3 hearing stream."""

from queue import Empty, Queue
from threading import Event
from types import SimpleNamespace
import unittest

import numpy as np

from jarvis.v3.voice.hearing import FRAME_SAMPLES, Hearing


QUIET = np.zeros(FRAME_SAMPLES, dtype=np.float32)
SPEECH = np.linspace(-0.2, 0.2, FRAME_SAMPLES, dtype=np.float32)
SOFT = SPEECH / 20
MARKER = SPEECH * 2
BARRIER = SPEECH * 3
WAIT = 3


class HearingReview(unittest.TestCase):
    def start(self, transcribe=lambda samples, **kwargs: "heard"):
        self.events = Queue()
        detector = SimpleNamespace(is_speech=lambda frame: float(np.max(np.abs(frame)) > 0.05))
        self.hearing = Hearing(
            SimpleNamespace(transcribe=transcribe), detector=detector,
            end_silence_ms=500, on_event=lambda kind, **fields: self.events.put((kind, fields)),
        )
        self.addCleanup(self.hearing.close)

    def feed(self, count, frame=SPEECH):
        for _ in range(count):
            self.hearing.feed(frame)

    def receive(self, kind):
        try:
            while True:
                event, fields = self.events.get(timeout=WAIT)
                if event == kind:
                    return fields
        except Empty:
            self.fail(f"missing {kind}")

    def finals_through_barrier(self):
        self.feed(8, BARRIER)
        self.feed(25, QUIET)
        finals = []
        while not finals or finals[-1] != "barrier":
            finals.append(self.receive("final")["text"])
        return finals

    def test_one_missed_vad_frame_drops_a_short_answer(self):
        self.start()
        self.feed(4)
        self.feed(1, SOFT)
        self.feed(4)
        self.assertTrue(self.hearing.speech_active(), "160 ms of detected speech in a 180 ms answer")
        self.feed(25, QUIET)
        self.assertEqual(self.receive("final")["text"], "heard")

    def test_one_isolated_vad_hit_joins_two_utterances(self):
        def transcribe(samples, **kwargs):
            if np.any(samples == BARRIER[-1]):
                return "barrier"
            if np.any(samples == MARKER[-1]):
                return "joined" if np.any(samples == SPEECH[-1]) else "second"
            return "first"

        self.start(transcribe)
        self.feed(12)
        self.feed(24, QUIET)
        self.feed(1)
        self.feed(15, QUIET)
        self.feed(12, MARKER)
        self.feed(25, QUIET)
        self.assertEqual(self.finals_through_barrier(), ["first", "second", "barrier"])

    def test_final_decode_includes_quiet_word_ending(self):
        def transcribe(samples, **kwargs):
            return "stopped" if np.any(samples == SOFT[-1]) else "stop"

        self.start(transcribe)
        self.feed(12)
        self.feed(3, SOFT)
        self.feed(25, QUIET)
        self.assertEqual(self.receive("final")["text"], "stopped")

    def test_blocked_decode_does_not_retain_minutes_of_ended_audio(self):
        entered, release = Event(), Event()

        def transcribe(samples, **kwargs):
            entered.set()
            if not release.wait(30):
                raise TimeoutError("test did not release decode")
            return "heard"

        self.start(transcribe)
        self.addCleanup(release.set)
        self.feed(8)
        self.feed(25, QUIET)
        self.assertTrue(entered.wait(WAIT))
        for _ in range(512):
            self.feed(8)
            self.feed(25, QUIET)
        with self.hearing._changed:
            retained = sum(len(utterance.frames) for utterance in self.hearing._ended)
        self.assertLess(retained * 20, 60_000, "a blocked decode retained over a minute of PCM")


if __name__ == "__main__":
    unittest.main()
