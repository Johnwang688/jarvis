"""The hotkey capture borrows the wake listener's microphone stream.

Found live on 2026-11-16: with the wake listener's WASAPI stream running,
starting a second stream on the same microphone fails inside PortAudio
(-9999, WdmSyncIoctl 0x490), so every Right-Ctrl press showed "Microphone
unavailable". These checks use fakes; the live receipt is in the PR.
"""
from __future__ import annotations

from array import array
from pathlib import Path
import queue
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy  # Keep audio's import outside patch.dict(sys.modules) fakes.

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows import voice_agent, voicewake


def bare_agent():
    a = voice_agent.Agent.__new__(voice_agent.Agent)
    a.done, a.online = threading.Event(), threading.Event()
    a.online.set()
    a.recording, a.flow_running = threading.Event(), threading.Event()
    a.generation, a.generation_lock = 1, threading.Lock()
    a.capture_lock = threading.Lock()
    a.capture, a.tapped, a.chunks, a.frames, a.audio_bad = None, False, [], 0, False
    a.capture_online = False
    a.clips = queue.Queue(maxsize=4)
    a.overlay, a.player = Mock(), voice_agent.Player()
    a.hud_in_front = Mock(return_value=False)
    a.wake = None
    return a


def listener(busy=lambda: False, active=True):
    w = voicewake.WakeListener(lambda wav: None, lambda rms: None, busy)
    w._stream = SimpleNamespace(active=active, close=lambda: None)
    return w


def refusing_sd():
    """A sounddevice whose second stream cannot start, as on the owner's PC."""
    stream = Mock()
    stream.start.side_effect = RuntimeError("Error starting stream: -9999")
    return SimpleNamespace(
        query_hostapis=lambda: [{"name": "WASAPI", "default_input_device": 0}],
        WasapiSettings=Mock(), InputStream=Mock(return_value=stream))


PCM = array("h", [1000] * 1600)


class Tap(unittest.TestCase):
    def test_tap_needs_a_live_stream(self):
        w = listener()
        w._stream = None
        self.assertFalse(w.tap(print))
        self.assertFalse(listener(active=False).tap(print))
        stopped = listener()
        stopped._done.set()
        self.assertFalse(stopped.tap(print))
        self.assertIsNone(stopped._tap)

    def test_tap_forwards_frames_even_while_busy_and_wake_still_drops_them(self):
        w = listener(busy=lambda: True)
        got = []
        self.assertTrue(w.tap(lambda indata, frames, timing, status: got.append(frames)))
        epoch = w._epoch
        w._audio(PCM, 1600, None, None)
        self.assertEqual(got, [1600])
        self.assertEqual(w._epoch, epoch + 1, "the wake word must not hear hotkey speech")
        self.assertTrue(w._audio_queue.empty())

    def test_untap_and_close_end_the_loan(self):
        w, got = listener(), []
        w.tap(lambda *args: got.append(1))
        w.tap(None)
        w._audio(PCM, 1600, None, None)
        self.assertEqual(got, [])
        w.tap(lambda *args: got.append(1))
        w._close_resources()
        self.assertIsNone(w._tap)

    def test_a_failing_tap_does_not_break_the_wake_stream(self):
        w = listener()
        w.tap(Mock(side_effect=ValueError("boom")))
        w._audio(PCM, 1600, None, None)  # must not raise into PortAudio
        self.assertFalse(w._audio_queue.empty())


class Capture(unittest.TestCase):
    def test_hotkey_borrows_the_wake_stream_instead_of_opening_a_second(self):
        a = bare_agent()
        a.wake = listener(busy=a.busy)
        sd = refusing_sd()
        with patch.dict(sys.modules, {"sounddevice": sd}):
            a.start_capture()
            a.wake._audio(PCM, 1600, None, None)
            a.finish_capture()
        sd.InputStream.assert_not_called()
        self.assertIsNone(a.wake._tap, "release must hand the stream back")
        self.assertFalse(a.tapped)
        generation, wav, source = a.clips.get_nowait()
        self.assertEqual(source, "hotkey")
        self.assertGreater(len(wav), 3200)

    def test_without_a_live_wake_stream_the_hotkey_opens_its_own(self):
        for wake in (None, listener(active=False)):
            with self.subTest(wake=wake):
                a = bare_agent()
                a.wake = wake
                stream = Mock()
                sd = refusing_sd()
                sd.InputStream = Mock(return_value=stream)
                with patch.dict(sys.modules, {"sounddevice": sd}):
                    a.start_capture()
                    a.audio(PCM, 1600, None, None)
                    a.finish_capture()
                sd.InputStream.assert_called_once()
                stream.close.assert_called_once()
                self.assertEqual(a.clips.qsize(), 1)

    def test_a_cancelled_borrowed_capture_queues_nothing(self):
        a = bare_agent()
        a.wake = listener(busy=a.busy)
        with patch.dict(sys.modules, {"sounddevice": refusing_sd()}):
            a.start_capture()
            a.wake._audio(PCM, 1600, None, None)
            a.finish_capture(cancel=True)
        self.assertEqual(a.clips.qsize(), 0)
        self.assertIsNone(a.wake._tap)


if __name__ == "__main__":
    unittest.main()
