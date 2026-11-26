import unittest

from voice_popup_check import FakeWin32, SAMPLES, drawing, popup


class Events(unittest.TestCase):
    def test_non_object_events_are_ignored(self):
        previous = SAMPLES["listening"]
        for event in (None, [], "asking", 4):
            with self.subTest(event=event):
                self.assertIs(drawing.state_from_event(previous, event), previous)

    def test_oversized_json_integer_duration_cannot_crash_or_enable_buttons(self):
        event = {"kind": "asking", "sentence": "Apply the change?", "expires_in": 10**400}
        fake = FakeWin32()
        window = popup.Popup(win32=fake)
        self.addCleanup(window.close)
        state = drawing.state_from_event(SAMPLES["working"], event)
        window.set(state, expires_in=event["expires_in"])
        self.assertFalse(state.buttons)


if __name__ == "__main__":
    unittest.main()
