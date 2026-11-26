"""Check approval placement when a notice appears beside an enabled approval."""

import unittest
from unittest.mock import patch

from voice_popup_check import FakeWin32, SAMPLES, drawing, popup


class ApprovalPlacement(unittest.TestCase):
    def open_popup(self, work, scale, anchor):
        self.fake = FakeWin32()
        self.fake.work_scale = work, scale
        self.changes = []
        self.window = popup.Popup(anchor=anchor, on_anchor_change=self.changes.append,
                                  win32=self.fake)
        self.addCleanup(self.window.close)

    def publish(self, state):
        self.window.set(state)
        paint = self.fake.until("paint")
        self.fake.sync()
        return paint

    def approve_center(self, state, position, scale):
        buttons = dict(drawing._layout(state, scale, 470, ()).buttons)
        left, top, right, bottom = buttons["approve"]
        return position[0] + (left + right) // 2, position[1] + (top + bottom) // 2

    def test_stacked_notice_keeps_enabled_approve_at_its_painted_position(self):
        self.open_popup((0, 0, 1200, 800), 1, (.5, 800))
        state = SAMPLES["needs_click"]
        with patch.object(self.fake, "paint", wraps=self.fake.paint) as paint:
            position, (panel_width, panel_height), _ = self.publish(state)
            panel_pixels = paint.call_args.args[2]
            self.window.notice(SAMPLES["finished"])
            stack_position, (stack_width, stack_height), _ = self.fake.until("paint")
            self.fake.sync()
            stack_pixels = paint.call_args.args[2]
        (_, panel_position, scale), _, _ = self.window._shown
        self.assertEqual(stack_position, panel_position)
        self.assertEqual(stack_width, panel_width)
        self.assertGreater(stack_height, panel_height)
        self.assertLessEqual(stack_position[1] + stack_height, 800)
        self.assertEqual(stack_pixels[:len(panel_pixels)], panel_pixels)
        self.assertEqual(scale, 1)
        self.assertEqual(self.approve_center(state, panel_position, 1),
                         self.approve_center(state, position, 1))

if __name__ == "__main__":
    unittest.main()
