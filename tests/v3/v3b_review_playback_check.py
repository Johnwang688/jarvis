import threading
import unittest
from unittest.mock import Mock

import numpy as np

from voice_helper_check import FakeDevices
from windows import v3voice_audio as audio
from windows.v3voice_client import wire


class PlaybackBoundaries(unittest.TestCase):
    def test_flush_discards_speech_already_buffered_by_player(self):
        devices = FakeDevices()
        flushed = threading.Event()
        playback = audio.Playback(devices, played=Mock(), flushed=flushed.set)
        pending = []
        played_after_flush = []

        def buffer_write(samples):
            if np.any(samples):
                pending.append(samples)
                if len(pending) == 3:
                    playback.flush()

        def drain(cancelled):
            if cancelled():
                return False
            played_after_flush.extend(pending)
            pending.clear()
            return True

        devices.before_write = buffer_write
        devices.drain = drain
        speech = b"\x00\x20" * (6 * audio.DEVICE_STEP)
        playback.put(0, wire.FIRST | wire.LAST, audio.DEVICE_RATE, speech)
        playback.start()
        try:
            self.assertTrue(flushed.wait(2))
            self.assertLessEqual(len(played_after_flush), 1, "flush drained queued speech audibly")
        finally:
            playback.close()

    def test_playback_backlog_is_bounded_while_device_write_is_blocked(self):
        devices = FakeDevices()
        entered = threading.Event()
        resume = threading.Event()

        def slow_write(_):
            entered.set()
            resume.wait(3)

        devices.before_write = slow_write
        playback = audio.Playback(devices, played=Mock(), flushed=Mock())
        playback.start()
        try:
            self.assertTrue(entered.wait(2))
            rate = 24000
            chunk = b"\x00\x20" * (rate // 5)
            for _ in range(300):
                playback.put(0, 0, rate, chunk)
            with playback._lock:
                self.assertLess(len(playback._queue), 300, "all 60 seconds of audio queued")
        finally:
            resume.set()
            playback.close()


if __name__ == "__main__":
    unittest.main()
