import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

from voice_helper_check import FakeDevices, FakePopup
from windows import v3voice_audio as audio, v3voice_main as main


class CaptureShutdown(unittest.TestCase):
    def test_delayed_capture_workers_do_not_open_devices_after_stop(self):
        devices = FakeDevices()
        capture = audio.Capture(devices)
        initializing = threading.Semaphore(0)
        resume = threading.Event()
        resampler = audio.Resampler

        def delayed_resampler(*args):
            initializing.release()
            if not resume.wait(3):
                raise TimeoutError()
            return resampler(*args)

        stop = threading.Thread(target=capture.stop, daemon=True)
        with patch.object(audio, "Resampler", side_effect=delayed_resampler):
            capture.start()
            try:
                for _ in range(2):
                    self.assertTrue(initializing.acquire(timeout=2))
                stop.start()
                with capture._changed:
                    self.assertTrue(capture._changed.wait_for(lambda: capture._stopping, 2))
            finally:
                resume.set()
                if stop.ident is None:
                    capture.stop()
                else:
                    stop.join(3)
        self.assertFalse(stop.is_alive())
        self.assertEqual(devices.opened[0], 0, "the microphone opened after capture was stopped")

    def test_capture_error_discards_frame_already_removed_by_pump(self):
        transport = Mock()
        helper = main.Helper("127.0.0.1", 12345, None, logger=Mock(),
                             devices=FakeDevices(), make_canceller=lambda: None,
                             make_client=Mock(return_value=transport),
                             make_key=Mock(), make_popup=FakePopup)
        stopped = threading.Event()
        helper._connected = True
        frame = (np.ones(320, dtype="<i2"), None, None)

        def frame_then_error(_):
            helper._capture._end("Audio capture stopped after a device error.")
            return frame

        with helper._capture._changed:
            helper._capture._stopping = False
        def end_pump(_):
            stopped.set()

        frames = iter((frame_then_error, end_pump))
        with patch.object(helper._capture, "get", side_effect=lambda timeout: next(frames)(timeout)), \
                patch.object(stopped, "wait", side_effect=lambda _: stopped.set()):
            helper._capture_loop(stopped)

        self.assertIsNotNone(helper._capture.reason)
        transport.send_audio.assert_not_called()


if __name__ == "__main__":
    unittest.main()
