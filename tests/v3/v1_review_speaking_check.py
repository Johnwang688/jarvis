"""A stop between the queue check and a PCM handoff suppresses the chunk."""

from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from jarvis.v3.voice.speaking import Speaking


class SpeakingReview(unittest.TestCase):
    def test_stop_before_callback_entry_prevents_audio_handoff(self):
        slicing, release, stopped, played = (Event() for _ in range(4))
        pcm = np.zeros(320, dtype=np.int16)
        audio_after_stop = []
        dropped = []

        class PausedPCM:
            def __len__(self):
                return len(pcm)

            def __getitem__(self, key):
                slicing.set()
                if not release.wait(10):
                    raise TimeoutError("test did not release the PCM handoff")
                return pcm[key]

        original_play = Speaking._play

        def play(speaker, sentence):
            sentence.audio = PausedPCM(), 16_000
            try:
                return original_play(speaker, sentence)
            finally:
                played.set()

        def on_audio(id, samples, sample_rate, first):
            if stopped.is_set():
                audio_after_stop.append(id)

        with patch.object(Speaking, "_play", play):
            speaker = Speaking(SimpleNamespace(synthesize=lambda text: (pcm, 16_000)),
                               on_audio=on_audio, on_done=lambda id: None)

            def stop():
                dropped.extend(speaker.stop())
                stopped.set()

            stopper = Thread(target=stop, daemon=True)
            try:
                speaker.say("old", "First sentence.")
                self.assertTrue(slicing.wait(3), "delivery never reached the handoff")
                stopper.start()
                stopped_before_release = stopped.wait(3)
                release.set()
                self.assertTrue(played.wait(3))
                self.assertTrue(stopped.wait(3))
                if stopped_before_release:
                    self.assertEqual(dropped, ["old"])
                self.assertEqual(audio_after_stop, [])
            finally:
                release.set()
                speaker.close()


if __name__ == "__main__":
    unittest.main()
