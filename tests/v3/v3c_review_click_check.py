from dataclasses import replace
from threading import Event
import unittest
from unittest.mock import patch

from voice_popup_check import FakeWin32, SAMPLES, drawing, popup


class PausedPaint(FakeWin32):
    def __init__(self):
        super().__init__()
        self.paint_started = Event()
        self.resume_paint = Event()
        self.pause_next = False

    def paint(self, position, dimensions, data):
        if self.pause_next:
            self.pause_next = False
            self.paint_started.set()
            if not self.resume_paint.wait(2):
                raise AssertionError("test did not release paint")
        super().paint(position, dimensions, data)


def approve_pair(state, position):
    left, top, right, bottom = dict(drawing._layout(state, 1.5, 470, ()).buttons)["approve"]
    x, y = position[0] + (left + right) // 2, position[1] + (top + bottom) // 2
    return (0, popup.WM_LBUTTONDOWN, x, y), (0, popup.WM_LBUTTONUP, x, y)


class Click(unittest.TestCase):
    def test_click_queued_before_replacement_is_painted_cannot_answer_it(self):
        fake = PausedPaint()
        received = []
        self.enterContext(patch.object(popup, "monotonic", return_value=100.0))
        window = popup.Popup(received.append, win32=fake)
        self.addCleanup(window.close)
        self.addCleanup(fake.resume_paint.set)
        old = replace(SAMPLES["asking"], line="Apply the first change?")
        new = replace(old, line="Apply the other change?")
        window.set(old, expires_in=30)
        position, _, _ = fake.until("paint")
        fake.sync()
        fake.pause_next = True
        window.set(new, expires_in=30)
        self.assertTrue(fake.paint_started.wait(2))
        # The old frame is still visible. Both physical events wait for the pump.
        fake.mouse(*approve_pair(old, position))
        fake.resume_paint.set()
        self.assertEqual(fake.until("paint")[0], position)
        fake.sync()
        self.assertEqual(received, [])

    def test_zero_remaining_cannot_be_clicked_with_set_state(self):
        fake = FakeWin32()
        received = []
        window = popup.Popup(received.append, win32=fake)
        self.addCleanup(window.close)
        state = replace(SAMPLES["asking"], expires=0)
        window.set(state)
        position, _, _ = fake.until("paint")
        fake.mouse(*approve_pair(SAMPLES["asking"], position))
        fake.sync()
        self.assertEqual((received, fake.watch), ([], False))


if __name__ == "__main__":
    unittest.main()
