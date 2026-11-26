import unittest

from voice_popup_check import FakeWin32, SAMPLES, popup


class Window(unittest.TestCase):
    def test_resting_waits_without_a_redraw_deadline(self):
        fake = FakeWin32()
        window = popup.Popup(win32=fake)
        self.addCleanup(window.close)
        fake.until("wait")
        window.set(SAMPLES["resting"])
        fake.until("paint")
        self.assertIsNone(fake.until("wait"))


if __name__ == "__main__":
    unittest.main()
