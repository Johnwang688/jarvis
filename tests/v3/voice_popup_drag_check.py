"""Drag the voice pop-up with the Linux fake and exercise its position file."""

from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from voice_helper_check import FakeDevices, FakePopup
from voice_popup_check import FakeWin32, SAMPLES, drawing, popup
from windows import v3voice_main as main


class Drag(unittest.TestCase):
    def open(self, *, anchor=None):
        self.fake = FakeWin32()
        self.changes, self.clicks = [], []
        self.window = popup.Popup(self.clicks.append, anchor=anchor,
                                  on_anchor_change=self.changes.append, win32=self.fake)
        self.addCleanup(self.window.close)

    def paint(self, state):
        self.window.set(state)
        return self.fake.until("paint")

    def assert_anchored(self, paint, anchor, work, scale):
        (x, y), (width, height), _ = paint
        left, top, right, bottom = work
        self.assertAlmostEqual(x + width / 2, left + anchor[0] * (right - left), delta=.5)
        self.assertAlmostEqual(y + height, bottom - anchor[1] * scale, delta=.5)
        self.assertGreaterEqual(x, left)
        self.assertGreaterEqual(y, top)
        self.assertLessEqual(x + width, right)
        self.assertLessEqual(y + height, bottom)

    def button_events(self, state, position):
        _, scale = self.fake.work_scale
        left, top, right, bottom = dict(drawing._layout(state, scale, 470, ()).buttons)["approve"]
        x, y = position[0] + (left + right) // 2, position[1] + (top + bottom) // 2
        return (0, popup.WM_LBUTTONDOWN, x, y), (0, popup.WM_LBUTTONUP, x, y)

    def test_no_anchor_matches_default_and_stationary_press_has_no_effect(self):
        self.open()
        for work, scale in (((0, 0, 1600, 900), 1), ((-1601, 20, 0, 900), 1.25),
                            ((1600, -300, 4200, 1300), 2)):
            with self.subTest(work=work, scale=scale):
                self.fake.work_scale = work, scale
                state = replace(SAMPLES["resting"], t=scale)
                position, size, _ = self.paint(state)
                self.assertEqual(position, popup.placement(work, size, scale))
                self.fake.sync()
                self.assertEqual(self.fake.hit_test(), popup.HTCAPTION)
                self.fake.drag(self.fake.rect)
                self.fake.sync()
                self.assertEqual(self.changes, [])
                self.assertEqual(self.fake.rect[:2], position)

    def test_all_draggable_states_and_repaint_with_other_sizes_monitor_and_notice(self):
        for kind in ("resting", "listening", "working", "speaking", "finished", "problem"):
            with self.subTest(kind=kind):
                self.open()
                _, (width, height), _ = self.paint(SAMPLES[kind])
                self.fake.sync()
                self.assertEqual(self.fake.hit_test(), popup.HTCAPTION)
                work, scale = (500, 100, 2100, 1050), 1.25
                rect = (840, 650 - height, 840 + width, 650)
                self.fake.drag(rect, work_scale=(work, scale))
                dropped = self.fake.until("paint", lambda row: abs(row[0][0] - 840) < 100)
                anchor = ((840 + width / 2 - 500) / 1600, 320)
                self.assertEqual(self.changes, [anchor])
                self.assert_anchored(dropped, anchor, work, scale)

                work, scale = (-2100, -300, -300, 1100), 2
                self.fake.work_scale = work, scale
                state = SAMPLES["resting" if kind != "resting" else "problem"]
                size = drawing.size(state, scale=scale)
                self.window.set(state)
                repaint = self.fake.until("paint", lambda row: row[1] == size)
                self.assert_anchored(repaint, anchor, work, scale)
                self.window.notice(SAMPLES["finished"])
                stacked = self.fake.until("paint", lambda row: row[1][1] > size[1])
                self.assert_anchored(stacked, anchor, work, scale)
                self.assertEqual(self.changes, [anchor])
                self.window.close()

    def test_enabled_buttons_reject_movement_and_still_accept_a_physical_pair(self):
        self.open()
        state = SAMPLES["asking"]
        position, _, _ = self.paint(state)
        self.fake.sync()
        self.assertEqual(self.fake.hit_test(), popup.HTCLIENT)
        rect = self.fake.rect
        down, up = self.button_events(state, position)
        self.fake.mouse(down)
        self.fake.drag(tuple(value + 200 for value in rect))
        self.fake.mouse((up[0], up[1], up[2] + 200, up[3] + 200))
        self.fake.sync()
        self.assertEqual(self.changes, [])
        self.assertEqual(self.clicks, [])
        self.assertEqual(self.fake.rect, rect)
        self.fake.mouse(down, up)
        self.fake.sync()
        self.assertEqual(self.clicks, [{"id": state.id, "digest": state.digest, "decision": "approve"}])

        self.paint(replace(state, expires=0))
        self.fake.sync()
        self.assertEqual(self.fake.hit_test(), popup.HTCAPTION)
        self.fake.drag(tuple(value - 200 for value in rect))
        self.fake.sync()
        self.assertEqual(len(self.changes), 1)

    def test_approve_press_survives_notice_at_default_and_anchored_positions(self):
        state = SAMPLES["needs_click"]
        banner = replace(SAMPLES["finished"], line="Done", sub="")
        for name, anchor, work_height, side in (
                ("default", None, 800, "above"),
                ("default with little room above", None, 200, "below"),
                ("top", (.5, 800), 800, "below"),
                ("bottom", (.5, 0), 800, "above"),
                ("middle", (.5, 400), 800, "above"),
                ("short work area", (.5, 75), 150, None)):
            with self.subTest(placement=name):
                self.open(anchor=anchor)
                with closing(self.window), patch.object(popup, "monotonic", return_value=100.0) as now:
                    self.fake.work_scale = (0, 0, 1200, work_height), 1
                    position, size, _ = self.paint(state)
                    self.fake.sync()
                    target = self.window._shown[0]
                    down, up = self.button_events(state, position)
                    self.fake.mouse(down)
                    self.fake.sync()
                    self.window.notice(banner)
                    stack_at, stack_size, _ = self.fake.until("paint")
                    self.fake.sync()
                    self.assertEqual(self.window._shown[0], target)
                    if side is None:
                        self.assertEqual((stack_at, stack_size), (position, size))
                    else:
                        stack_extra = drawing.size(banner)[1] + 8
                        self.assertEqual(stack_size, (size[0], size[1] + stack_extra))
                        expected_y = position[1] - stack_extra if side == "above" else position[1]
                        self.assertEqual(stack_at, (position[0], expected_y))
                        self.assertGreaterEqual(stack_at[1], 0)
                        self.assertLessEqual(stack_at[1] + stack_size[1], work_height)
                    self.fake.mouse(up)
                    self.fake.sync()
                    self.assertEqual(self.clicks, [{"id": state.id, "digest": state.digest,
                                                   "decision": "approve"}])
                    now.return_value += 6
                    self.fake.wake()
                    paint_at, paint_size, _ = self.fake.until("paint")
                    self.fake.sync()
                    self.assertEqual((paint_at, paint_size), (position, size))
                    self.assertEqual(self.window._shown[0], target)

    def test_approval_published_during_move_needs_a_fresh_press_at_the_drop(self):
        self.open()
        _, (width, height), _ = self.paint(SAMPLES["resting"])
        state = SAMPLES["needs_click"]
        approval_width, approval_height = drawing.size(state, scale=1.5)
        rect = (-1300, 450 - height, -1300 + width, 450)
        expected_position = (-1300 + (width - approval_width) // 2, 450 - approval_height)
        down, up = self.button_events(state, expected_position)

        def publish_during_move():
            self.fake.handler(*down)
            self.window.set(state)
            self.fake.handler(*up)

        self.fake.drag(rect, during_move=publish_during_move)
        position, _, _ = self.fake.until("paint")
        self.assertAlmostEqual(position[0], expected_position[0], delta=1)
        self.assertEqual(position[1], expected_position[1])
        self.fake.mouse(up)
        self.fake.sync()
        self.assertEqual(self.clicks, [])
        self.assertEqual(self.fake.hit_test(), popup.HTCLIENT)
        self.fake.mouse(*self.button_events(state, position))
        self.fake.sync()
        self.assertEqual(self.clicks, [{"id": state.id, "digest": state.digest, "decision": "approve"}])

    def test_drop_within_scaled_default_radius_clears_saved_anchor(self):
        self.open(anchor=(.3, 180))
        _, size, _ = self.paint(SAMPLES["resting"])
        work, scale = self.fake.work_scale
        x, y = popup.placement(work, size, scale)
        width, height = size
        self.fake.drag((x + 23, y + 23, x + 23 + width, y + 23 + height))
        self.assertEqual(self.fake.until("paint")[0], (x, y))
        self.assertEqual(self.changes, [None])

    def test_large_monitor_anchor_clamps_panel_and_keeps_notice_inside_smaller_monitor(self):
        self.open(anchor=(.98, 1250))
        work, scale = (-800, -50, -240, 350), 1
        self.fake.work_scale = work, scale
        self.paint(SAMPLES["problem"])
        self.window.notice(SAMPLES["finished"])
        (_, height) = drawing.size(SAMPLES["problem"], scale=scale)
        (x, y), (stack_width, stack_height), _ = self.fake.until("paint", lambda row: row[1][1] > height)
        self.assertEqual((x + stack_width, y), (work[2], work[1]))
        self.assertLessEqual(y + stack_height, work[3])

        work = (-200, 0, 0, 80)
        self.fake.work_scale = work, scale
        (x, y), (width, height), _ = self.paint(replace(SAMPLES["problem"], line="Another problem"))
        self.assertGreaterEqual(x, work[0])
        self.assertGreaterEqual(y, work[1])
        self.assertLessEqual(x + width, work[2])
        self.assertLessEqual(y + height, work[3])

    def test_drag_with_notice_below_saves_panel_bottom_center(self):
        self.open(anchor=(.5, 800))
        work, scale = (0, 0, 1200, 800), 1
        self.fake.work_scale = work, scale
        panel_at, (panel_width, panel_height), _ = self.paint(SAMPLES["resting"])
        self.window.notice(SAMPLES["finished"])
        (x, y), (width, height), _ = self.fake.until("paint")
        self.assertEqual(y, panel_at[1])
        self.assertLess(x, panel_at[0])
        self.fake.drag((x + 100, y + 50, x + width + 100, y + height + 50))
        self.fake.until("paint")
        self.assertEqual(self.changes, [((panel_at[0] + 100 + panel_width / 2) / 1200,
                                         800 - panel_at[1] - 50 - panel_height)])

    def test_real_and_fake_adapters_define_popup_methods(self):
        methods = {"open", "monitor", "paint", "hide", "watch_mouse", "is_popup_at",
                   "wake", "wait", "close", "set_draggable", "take_move"}
        for adapter in (FakeWin32, popup._Win32):
            self.assertEqual(methods, {name for name in methods if callable(getattr(adapter, name, None))})
        self.assertIn("SetWindowPos", popup._Win32.open.__code__.co_names)
        self.assertEqual(popup.WM_MOVING, 0x0216)
        self.assertEqual((popup.SWP_NOSIZE, popup.SWP_NOZORDER, popup.SWP_NOACTIVATE),
                         (0x0001, 0x0004, 0x0010))


class PositionFile(unittest.TestCase):
    def setUp(self):
        self.path = Path(self.enterContext(TemporaryDirectory())) / "popup-position.json"
        self.logger = Mock()

    def test_helper_uses_log_folder_for_load_save_and_reset(self):
        main._save_anchor(self.path, (.25, 144), self.logger)
        with main.log_file(self.path.parent) as logger:
            helper = main.Helper("127.0.0.1", 12345, self.path.with_name("example.key"), logger=logger,
                                 devices=FakeDevices(), make_canceller=lambda: None,
                                 make_client=Mock(), make_key=Mock(), make_popup=FakePopup)
            self.assertEqual(helper._popup.anchor, (.25, 144))
            helper._popup.on_anchor_change((.75, 212.5))
            self.assertEqual(main._load_anchor(self.path), (.75, 212.5))
            helper._popup.on_anchor_change(None)
            self.assertFalse(self.path.exists())

    def test_missing_unreadable_and_invalid_files(self):
        self.assertIsNone(main._load_anchor(self.path))
        with patch.object(Path, "read_text", side_effect=PermissionError):
            self.assertIsNone(main._load_anchor(self.path))
        self.path.write_bytes(b"\xff")
        self.assertIsNone(main._load_anchor(self.path))
        valid = {"version": 1, "fraction": .5, "distance": 120}
        for broken in ("{", "[]", "null", json.dumps({}),
                       *(json.dumps(valid | {"version": value}) for value in (2, True, 1., "1")),
                       *(json.dumps(valid | {key: value})
                         for key, values in (("fraction", (-.01, 1.01)), ("distance", (-1, 1e9, 10**400)))
                         for value in (*values, "0.5", True, [], {}, None, float("nan"),
                                       float("inf"), -float("inf")))):
            with self.subTest(broken=broken):
                self.path.write_text(broken, encoding="utf-8")
                self.assertIsNone(main._load_anchor(self.path))

    def test_failed_atomic_replace_keeps_previous_file_and_logs_once(self):
        main._save_anchor(self.path, (.25, 144), self.logger)
        with patch.object(Path, "replace", side_effect=PermissionError):
            main._save_anchor(self.path, (.75, 212.5), self.logger)
        self.logger.warning.assert_called_once_with("Could not save the voice pop-up position.")
        self.assertEqual(main._load_anchor(self.path), (.25, 144))
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


if __name__ == "__main__":
    unittest.main()
