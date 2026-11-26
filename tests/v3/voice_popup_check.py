"""Exercise the v3 voice pop-up with Pillow and a fake message pump only."""

import ast
from colorsys import rgb_to_hsv
from contextlib import closing
from dataclasses import replace
import os
from pathlib import Path
from queue import Empty, Queue
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from time import monotonic_ns
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops

from windows import v3voice_popup as popup, v3voice_render as drawing
from windows.v3voice_problems import MICROPHONE, PROBLEMS, UNKNOWN


ROOT = Path(__file__).resolve().parents[2]
SAMPLES = {state.kind: state for state in popup._sample_states()}


def hues(image):
    """Allow ±18° for red and 28–57° for amber, excluding dim/desaturated pixels."""
    colors = set()
    for r, g, b, alpha in struct.iter_unpack("4B", image.tobytes()):
        if alpha < 215:
            continue
        hue, saturation, value = rgb_to_hsv(r / 255, g / 255, b / 255)
        if saturation < .26 or value < .35:
            continue
        angle = hue * 360
        if angle < 18 or angle > 342:
            colors.add("red")
        elif 28 <= angle <= 57:
            colors.add("amber")
        elif 175 <= angle <= 215:
            colors.add("cyan")
    return colors


class Render(unittest.TestCase):
    def test_every_state_at_three_dpi_scales(self):
        for state in SAMPLES.values():
            for scale in (1, 1.5, 2):
                with self.subTest(kind=state.kind, scale=scale):
                    image = drawing.render(state, scale=scale)
                    width, height = image.size
                    self.assertEqual(image.mode, "RGBA")
                    self.assertEqual(image.size, drawing.size(state, scale=scale))
                    alpha = image.getchannel("A")
                    self.assertTrue(all(alpha.getpixel((x, y)) == 0
                                        for x in (0, width - 1) for y in (0, height - 1)))
                    if state.kind == "hidden":
                        self.assertIsNone(alpha.getbbox())
                    else:
                        self.assertGreater(alpha.getpixel((width // 2, height // 2)), 190)

    def test_expected_hue_in_panel_and_orb(self):
        for state in SAMPLES.values():
            with self.subTest(kind=state.kind):
                image = drawing.render(state)
                colors = hues(image)
                expected = {"amber"} if state.kind in drawing.APPROVALS else (
                    {"red"} if state.kind == "problem" else set())
                self.assertEqual(colors & {"red", "amber"}, expected)
                if state.kind != "hidden":
                    x, y, diameter = drawing._layout(state, 1, 470, ()).orb
                    orb = image.crop((x, y, x + diameter, y + diameter))
                    self.assertIn(next(iter(expected), "cyan"), hues(orb))

    def test_ring_rotation_wave_level_and_resting_pill(self):
        state = SAMPLES["listening"]
        still = drawing.render(state)
        moved = drawing.render(replace(state, t=1.2))
        x, y, diameter = drawing._layout(state, 1, 470, ()).orb
        ring_box = x, y, x + diameter, y + diameter
        self.assertNotEqual(still.crop(ring_box).tobytes(), moved.crop(ring_box).tobytes())
        quiet = drawing.render(replace(state, level=0))
        wave_box = 403, 12, 456, 58
        self.assertNotEqual(still.crop(wave_box).tobytes(), quiet.crop(wave_box).tobytes())
        pill = SAMPLES["resting"]
        self.assertEqual(drawing.size(pill), (250, 38))
        self.assertEqual(drawing._layout(pill, 1, 470, ()).orb[2], 22)
        self.assertEqual(drawing._layout(SAMPLES["hidden"], 1, 470, ()).orb[2], 0)

    def test_long_line_wraps_and_stays_inside_its_column(self):
        state = drawing.PopupState("listening", line="\u202e\0" + "a long phrase " * 1000)
        layout = drawing._layout(state, 2, 470, ())
        self.assertEqual(len(layout.lines), 2)
        self.assertTrue(layout.lines[-1].endswith("…"))
        self.assertNotIn("\u202e", "".join(layout.lines))
        self.assertNotIn("\0", "".join(layout.lines))
        image = drawing.render(state, scale=2).convert("RGB")
        # Retain the geometry but remove only the two lines to locate their ink.
        with patch.object(drawing, "_layout", return_value=replace(layout, lines=())):
            blank = drawing.render(state, scale=2).convert("RGB")
        left, top, right, bottom = ImageChops.difference(image, blank).getbbox()
        self.assertGreaterEqual(left, 71 * 2 - 2)
        self.assertLessEqual(right, (470 - 15 - 51 - 12) * 2)
        self.assertGreater(top, layout.body[1] * 2 + 13 * 2)
        self.assertLess(bottom, image.height - 10)
        fallback = drawing.render(state, font_paths=("example.invalid/missing-font.ttf",))
        self.assertEqual(fallback.size, drawing.size(state))

    def test_longest_problem_hint_fits_and_is_drawn_at_each_scale(self):
        state = drawing.state_from_event(drawing.PopupState(), {"kind": "error", "reason": "brain_api_key_env"})
        self.assertEqual(state.sub, "Voice uses your Claude sign-in. Remove the key and restart the agent.")
        for scale in (1, 1.5, 2):
            with self.subTest(scale=scale):
                layout = drawing._layout(state, scale, 470, ())
                self.assertEqual(" ".join(layout.sub), state.sub)
                self.assertEqual(len(layout.sub), 2)
                image = drawing.render(state, scale=scale).convert("RGB")
                with patch.object(drawing, "_layout", return_value=replace(layout, sub=())):
                    blank = drawing.render(state, scale=scale).convert("RGB")
                left, top, right, bottom = ImageChops.difference(image, blank).getbbox()
                self.assertGreaterEqual(left, round(71 * scale) - 1)
                self.assertGreaterEqual(top, round((layout.body[1] + 18 + 19 * len(layout.lines)) * scale))
                self.assertLessEqual(right, round((470 - 15) * scale))
                self.assertLess(bottom, image.height - round(10 * scale))

    def test_layout_limits_combining_marks_and_replaces_missing_glyphs(self):
        line = "A" + "\u0301" * 25 + "\U0010ffff" + "B" + "\u0301" * 3
        state = replace(SAMPLES["asking"], line=line)
        layout = drawing._layout(state, 2, 470, ("DejaVuSans.ttf",))
        self.assertEqual(layout.lines, ("A\u0301\u0301 B\u0301\u0301",))

    def test_text_ink_is_clipped_to_its_block_after_downsampling(self):
        state = replace(SAMPLES["asking"], line="A")
        scale = 1.5
        layout = drawing._layout(state, scale, 470, ())
        self.assertEqual(len(layout.lines), 1)
        with patch.object(drawing, "_layout", return_value=replace(layout, lines=())):
            blank = drawing.render(state, scale=scale).convert("RGB")
        # Give the renderer deliberately oversized ink even after layout.
        line = "A" + "\u0301" * 25 + "A" * 100
        with patch.object(drawing, "_layout", return_value=replace(layout, lines=(line,))):
            frame = drawing.render(state, scale=scale).convert("RGB")
        left, top, right, bottom = ImageChops.difference(blank, frame).getbbox()
        x, y = layout.body
        self.assertGreaterEqual(left, round(x * scale))
        self.assertGreaterEqual(top, round((y + 15) * scale))
        self.assertLessEqual(right, round((x + layout.body_width) * scale))
        self.assertLessEqual(bottom, round((y + 34) * scale))

    def test_button_centers_edges_and_neighbors_match_ink(self):
        state = SAMPLES["asking"]
        for scale in (1, 1.5, 2):
            with self.subTest(scale=scale):
                width = 436
                image = drawing.render(state, scale=scale, width=width)
                no_buttons = replace(state, buttons=False)
                layout = drawing._layout(state, scale, width, ())
                with patch.object(drawing, "_layout", return_value=replace(layout, buttons=())):
                    bare = drawing.render(state, scale=scale, width=width)
                for action, (left, top, right, bottom) in layout.buttons:
                    with self.subTest(action=action):
                        x, y = (left + right) // 2, (top + bottom) // 2
                        # Midpoints exercise all four edges away from the rounded corners.
                        for point in ((x, y), (left, y), (right - 1, y), (x, top), (x, bottom - 1)):
                            self.assertEqual(drawing.hit_test(state, *point, scale=scale, width=width), action)
                            self.assertNotEqual(image.getpixel(point), bare.getpixel(point))
                        for point in ((left - 1, y), (right, y), (x, top - 1), (x, bottom)):
                            self.assertIsNone(drawing.hit_test(state, *point, scale=scale, width=width))
                        self.assertIsNone(drawing.hit_test(no_buttons, x, y, scale=scale, width=width))
                        self.assertIsNone(drawing.hit_test(SAMPLES["working"], x, y, scale=scale, width=width))

    def test_countdown_width_tracks_fraction(self):
        for scale in (1, 1.5, 2):
            for fraction in (0, .25, .62, 1):
                with self.subTest(scale=scale, fraction=fraction):
                    image = drawing.render(replace(SAMPLES["asking"], expires=fraction), scale=scale)
                    row = image.height - round(2.5 * scale)
                    bright = [x for x in range(image.width) if image.getpixel((x, row))[0] > 220]
                    if fraction == 0:
                        self.assertEqual(bright, [])
                    elif fraction == 1:
                        self.assertGreater(len(bright), image.width - 36 * scale)
                    else:
                        self.assertAlmostEqual(bright[-1] + 1, image.width * fraction, delta=2)

    def test_import_boundary_and_png_cli(self):
        tree = ast.parse((ROOT / "windows/v3voice_render.py").read_bytes())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.module == "v3voice_problems":
                    self.assertIn(node.level, (0, 1))
                else:
                    self.assertEqual(node.level, 0)
                names = [node.module]
            else:
                continue
            for name in names:
                self.assertIn(name.partition(".")[0], sys.stdlib_module_names | {"PIL", "numpy", "v3voice_problems"})

        with TemporaryDirectory() as folder, TemporaryDirectory() as home:
            subprocess.run([sys.executable, str(ROOT / "windows/v3voice_popup.py"), "--render-dir", folder],
                           env={**os.environ, "HOME": home}, capture_output=True, check=True, timeout=10)
            previews = {file.stem: file for file in Path(folder).glob("*.png")}
            self.assertEqual(previews.keys(), SAMPLES.keys() | {"listening-1", "listening-2", "listening-3"})
            for name, path in previews.items():
                with Image.open(path) as image:
                    self.assertEqual(image.mode, "RGBA")
                    self.assertEqual(image.size, drawing.size(SAMPLES[name.split("-")[0]]))
            self.assertEqual(len({previews[f"listening-{i}"].read_bytes() for i in range(1, 4)}), 3)


class Events(unittest.TestCase):
    def test_state_event_and_unknown_states(self):
        hidden = drawing.PopupState()
        for state, expected in (("resting", "resting"), ("listening", "listening"), ("working", "working"),
                                ("speaking", "speaking"), ("asking", "asking"), ("unavailable", "problem")):
            with self.subTest(state=state):
                result = drawing.state_from_event(hidden, {"kind": "state", "state": state})
                self.assertEqual(result.kind, expected)
                self.assertFalse(result.buttons)
        listening = drawing.state_from_event(hidden, {"kind": "partial", "text": "open the"})
        self.assertIs(drawing.state_from_event(listening, {"kind": "state", "state": "listening"}), listening)
        self.assertIs(drawing.state_from_event(listening, {"kind": "state", "state": "other"}), listening)
        self.assertEqual(drawing.state_from_event(listening, {"kind": "state", "state": "hidden"}), hidden)

    def test_event_text_approval_and_audio_only_events(self):
        previous = drawing.PopupState(level=.6, t=4)
        rows = (
            ({"kind": "partial", "text": "open\x00\t the\x1b\u202e folder"}, "Listening", "open the folder"),
            ({"kind": "heard", "text": "Open the folder"}, "Working", "Open the folder"),
            ({"kind": "say", "text": "It is open"}, "Jarvis", "It is open"),
            ({"kind": "asking", "sentence": "Approve the action?", "expires_in": 60}, "Approve?", "Approve the action?"),
            ({"kind": "asking", "tool": "example", "expires_in": .5, "voice_allowed": False}, "Needs a click", "example"),
            ({"kind": "notice", "title": "Practice", "text": "Task complete"}, "Finished · Practice", "Task complete"),
        )
        for event, label, line in rows:
            with self.subTest(kind=event["kind"], label=label):
                result = drawing.state_from_event(previous, event)
                self.assertEqual((result.label, result.line), (label, line))
                self.assertEqual((result.level, result.t), (.6, 4))
                if event["kind"] == "asking":
                    self.assertTrue(result.buttons)
                    self.assertEqual(result.expires, 1)
                    self.assertIs(drawing.state_from_event(result, {"kind": "say", "text": line}), result)
        for kind in ("earcon", "flush", "unknown"):
            self.assertIs(drawing.state_from_event(previous, {"kind": kind, "name": "ack"}), previous)
        expired = drawing.state_from_event(previous, {"kind": "asking", "expires_in": 0})
        self.assertEqual((expired.expires, expired.buttons), (0, False))
        oversized = drawing.state_from_event(previous, {"kind": "say", "text": "a" * 10000})
        self.assertEqual(oversized.line, "a" * 600 + "…")

    def test_problem_reasons_use_the_table_without_drawing_codes(self):
        previous = drawing.PopupState()
        for event in ({"kind": "error"}, {"kind": "state", "state": "unavailable"}):
            for reason, expected in (*PROBLEMS.items(), ("brain_closed", UNKNOWN),
                                     *((f"made_up_{i}", UNKNOWN) for i in range(30))):
                with self.subTest(event=event, reason=reason):
                    state = drawing.state_from_event(previous, dict(event, reason=reason))
                    self.assertEqual(state.kind, "problem")
                    self.assertEqual((state.label, state.line, state.sub), expected)
                    layout = drawing._layout(state, 1, 470, ())
                    self.assertNotIn(reason, " ".join((*layout.label, *layout.lines, *layout.sub)).lower())
            sentence = "Audio device unavailable."
            state = drawing.state_from_event(previous, dict(event, reason=sentence), device_error=True)
            self.assertEqual((state.label, state.line, state.sub), (MICROPHONE[0], sentence, MICROPHONE[2]))
            state = drawing.state_from_event(previous, event, device_error=True)
            self.assertEqual((state.label, state.line, state.sub), MICROPHONE)
            state = drawing.state_from_event(previous, dict(event, reason=sentence))
            self.assertEqual((state.label, state.line, state.sub), UNKNOWN)

    def test_malformed_fields_and_approval_duration_bounds(self):
        previous = SAMPLES["working"]
        for event in ({"kind": []}, {"kind": "state", "state": {}}, {"kind": "say"},
                      {"kind": "partial", "text": []}, {"kind": "notice", "text": False},
                      {"kind": "asking", "expires_in": "30"}, {"kind": "asking", "id": {}}):
            with self.subTest(event=event):
                self.assertIs(drawing.state_from_event(previous, event), previous)
        asking = {"kind": "asking", "sentence": "Approve?", "id": "card", "digest": "digest"}
        for seconds in (-1, 0, float("nan"), float("inf"), 600.1, 600):
            with self.subTest(seconds=seconds):
                state = drawing.state_from_event(previous, dict(asking, expires_in=seconds))
                active = seconds == 600
                self.assertEqual((state.kind, state.buttons, state.expires), ("asking", active, float(active)))
                self.assertEqual((state.id, state.digest), ("card", "digest"))


class FakeWin32:
    def __init__(self):
        self.events = Queue()
        self.calls = Queue()
        self.work_scale = (-1600, -50, 0, 850), 1.5
        self.on_top = True
        self.watch = False
        self.draggable = False
        self.rect = self.move = None

    def open(self, handler):
        self.owner = threading.get_ident()
        self._handler = handler

    def handler(self, *event):
        self._handler(*event, monotonic_ns())

    def monitor(self):
        return self.work_scale

    def paint(self, position, dimensions, data):
        x, y = position
        width, height = dimensions
        self.rect = x, y, x + width, y + height
        self.painted_at = monotonic_ns()
        self.calls.put(("paint", (position, dimensions, len(data))))

    def set_draggable(self, enabled):
        self.draggable = enabled

    def hit_test(self):
        return popup.HTCAPTION if self.draggable else popup.HTCLIENT

    def take_move(self):
        move, self.move = self.move, None
        return move

    def drag(self, rect, *, work_scale=None, during_move=None):
        def delivered():
            if self.hit_test() != popup.HTCAPTION or self.rect[:2] == rect[:2]:
                return
            if during_move is not None:
                during_move()
            self.rect = rect
            if work_scale is not None:
                self.work_scale = work_scale
            self.move = rect, *self.work_scale

        self.events.put(delivered)

    def hide(self):
        self.calls.put(("hide", None))

    def watch_mouse(self, enabled):
        self.watch = enabled

    def is_popup_at(self, x, y):
        return self.on_top

    def wake(self):
        self.events.put(None)

    def wait(self, timeout):
        self.calls.put(("wait", timeout))
        try:
            event = self.events.get(timeout=timeout)
        except Empty:
            return True
        if event:
            event()
        return True

    def close(self):
        self.watch = False
        self.calls.put(("close", threading.get_ident()))

    def until(self, kind, predicate=lambda value: True):
        for _ in range(100):
            name, value = self.calls.get(timeout=2)
            if name == kind and predicate(value):
                return value
        raise AssertionError(f"missing {kind} call")

    def mouse(self, *events):
        event_time = monotonic_ns()

        def delivered():
            for event in events:
                self._handler(*event, event_time)

        self.events.put(delivered)

    def sync(self):
        done = threading.Event()
        self.events.put(done.set)
        if not done.wait(2):
            raise AssertionError("message pump did not process the test event")


class Window(unittest.TestCase):
    def test_click_flags_and_bgra_conversion(self):
        for flags in (popup.LLMHF_INJECTED, popup.LLMHF_LOWER_IL_INJECTED, 3):
            self.assertFalse(popup.physical_click(flags, popup.WM_LBUTTONUP))
        self.assertTrue(popup.physical_click(0, popup.WM_LBUTTONUP))
        self.assertFalse(popup.physical_click(0, popup.WM_LBUTTONDOWN))
        self.assertFalse(popup.physical_click(0, 0x0205))  # WM_RBUTTONUP
        image = Image.new("RGBA", (2, 1))
        image.putdata([(200, 100, 50, 128), (255, 100, 20, 0)])
        self.assertEqual(popup.premultiplied_bgra(image), bytes((25, 50, 100, 128, 0, 0, 0, 0)))

    def test_thread_placement_redraw_hide_and_close(self):
        fake = FakeWin32()
        window = popup.Popup(win32=fake)
        self.addCleanup(window.close)
        self.assertIsNone(fake.until("wait"))
        state = SAMPLES["working"]
        worker = threading.Thread(target=window.set, args=(state,))
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        dimensions = drawing.size(state, scale=1.5)
        expected = (-1153, 730 - dimensions[1])
        self.assertEqual(fake.until("paint"), (expected, dimensions, dimensions[0] * dimensions[1] * 4))
        self.assertLessEqual(fake.until("wait"), 1 / 30)
        self.assertEqual(fake.until("paint")[0], expected)  # redraw without set()

        fake.work_scale = (1920, 40, 4480, 1440), 2
        state = SAMPLES["resting"]
        window.set(state)
        dimensions = (500, 76)
        position, _, _ = fake.until("paint", lambda entry: entry[1] == dimensions)
        self.assertEqual(position, (2950, 1204))
        fake.sync()
        with patch.object(drawing, "render", wraps=drawing.render) as render:
            window.set(replace(state))
            fake.sync()
            render.assert_not_called()
        window.set(SAMPLES["hidden"])
        fake.until("hide")
        self.assertIsNone(fake.until("wait"))
        window.close()
        self.assertEqual(fake.until("close"), fake.owner)
        self.assertIsNone(window.error)
        self.assertFalse(window._thread.is_alive())

    def test_only_a_physical_pair_on_the_current_button_delivers(self):
        fake = FakeWin32()
        received = []
        window = popup.Popup(received.append, win32=fake)
        self.addCleanup(window.close)
        state = SAMPLES["asking"]
        window.set(state)
        position, _, _ = fake.until("paint")
        fake.sync()
        self.assertTrue(fake.watch)
        buttons = dict(drawing._layout(state, 1.5, 470, ()).buttons)
        left, top, right, bottom = buttons["approve"]
        x, y = position[0] + (left + right) // 2, position[1] + (top + bottom) // 2
        down, up = (0, popup.WM_LBUTTONDOWN, x, y), (0, popup.WM_LBUTTONUP, x, y)
        fake.mouse(up, down, (popup.LLMHF_INJECTED, *up[1:]), up)
        fake.mouse((popup.LLMHF_LOWER_IL_INJECTED, *down[1:]), up)
        fake.mouse(down, (0, popup.WM_LBUTTONUP, position[0] + 5, y))
        fake.sync()
        self.assertEqual(received, [])
        fake.on_top = False
        fake.mouse(down, up)
        fake.sync()
        self.assertEqual(received, [])
        fake.on_top = True
        fake.mouse(down)
        fake.sync()
        new = replace(state, line="A new approval?", id="replacement", digest="replacement-digest")
        window.set(new)
        fake.until("paint")
        fake.mouse(up)
        fake.sync()
        self.assertEqual(received, [])
        fake.mouse(down, up, down, up)
        fake.sync()
        self.assertEqual(received, [{"id": new.id, "digest": new.digest, "decision": "approve"}])
        window.set(SAMPLES["hidden"])
        fake.until("hide")
        self.assertFalse(fake.watch)

    def test_approval_countdown_keeps_the_target_until_expiry(self):
        fake = FakeWin32()
        received = []
        state = SAMPLES["needs_click"]
        decision = {"id": state.id, "digest": state.digest, "decision": "deny"}
        with patch.object(popup, "monotonic", return_value=100.0) as now:
            with closing(popup.Popup(received.append, win32=fake)) as window:
                window.set(state, expires_in=3)
                position, _, _ = fake.until("paint")
                left, top, right, bottom = dict(drawing._layout(state, 1.5, 470, ()).buttons)["deny"]
                x, y = position[0] + (left + right) // 2, position[1] + (top + bottom) // 2
                down, up = (0, popup.WM_LBUTTONDOWN, x, y), (0, popup.WM_LBUTTONUP, x, y)
                fake.mouse(down)
                fake.sync()
                now.return_value = 101.0
                fake.wake()
                fake.until("paint")
                fake.mouse(up)
                fake.sync()
                self.assertEqual(received, [decision])

                window.set(state, expires_in=3)
                fake.until("paint")
                fake.mouse(down)
                fake.sync()
                now.return_value = 104.0
                fake.mouse(up)
                fake.sync()
                self.assertEqual(received, [decision])
                self.assertFalse(fake.watch)

    def test_notice_leaves_the_approval_target_and_deadline_in_place(self):
        fake = FakeWin32()
        received = []
        state = SAMPLES["asking"]
        with patch.object(popup, "monotonic", return_value=100.0) as now:
            with closing(popup.Popup(received.append, win32=fake)) as window:
                window.set(state, expires_in=10)
                position, size, _ = fake.until("paint")
                left, top, right, bottom = dict(drawing._layout(state, 1.5, 470, ()).buttons)["deny"]
                x, y = position[0] + (left + right) // 2, position[1] + (top + bottom) // 2
                fake.mouse((0, popup.WM_LBUTTONDOWN, x, y))
                fake.sync()
                window.notice(SAMPLES["finished"])
                paint_at, stacked, _ = fake.until("paint", lambda row: row[1][1] > size[1])
                self.assertEqual(paint_at[1] + stacked[1], position[1] + size[1])
                fake.mouse((0, popup.WM_LBUTTONUP, x, y))
                fake.sync()
                self.assertEqual(received, [{"id": state.id, "digest": state.digest, "decision": "deny"}])
                now.return_value += 6
                fake.wake()
                self.assertEqual(fake.until("paint", lambda row: row[1] == size)[0], position)
                self.assertEqual(window._countdown, (110.0, 10))

    @unittest.skipIf(os.name == "nt", "The default adapter opens a native window on Windows")
    def test_off_windows_default_is_inert(self):
        window = popup.Popup()
        window.set(SAMPLES["listening"])
        window.close()
        self.assertIsNone(window._thread)

    def test_set_tolerates_malformed_state_fields(self):
        fake = FakeWin32()
        window = popup.Popup(win32=fake)
        self.addCleanup(window.close)
        window.set(drawing.PopupState(kind=[], t=10**400))
        window.set(replace(SAMPLES["asking"], label=[], line={}, sub=object(),
                           expires=None, buttons=True, t=10**400), expires_in=30)
        fake.until("paint")
        self.assertIsNone(fake.until("wait"))
        self.assertFalse(fake.watch)
        self.assertIsNone(window.error)


if __name__ == "__main__":
    unittest.main()
