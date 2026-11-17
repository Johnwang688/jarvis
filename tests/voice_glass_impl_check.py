"""VA-7 implementation checks with headless Tk and injected Windows APIs."""
from __future__ import annotations

import ctypes
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from windows import voiceui


class Canvas:
    def __init__(self, window, **options):
        self.options, self.items = options, {}

    def pack(self):
        pass

    def create(self, kind, coords, options):
        ident = len(self.items) + 1
        self.items[ident] = dict(kind=kind, coords=coords, state="normal", **options)
        return ident

    def create_polygon(self, *coords, **options):
        return self.create("polygon", coords, options)

    def create_line(self, *coords, **options):
        # State may be supplied by the caller.
        state = options.pop("state", "normal")
        ident = self.create("line", coords, options)
        self.items[ident]["state"] = state
        return ident

    def create_rectangle(self, *coords, **options):
        return self.create("rectangle", coords, options)

    def create_text(self, *coords, **options):
        return self.create("text", coords, options)

    def itemconfigure(self, ident, **options):
        self.items[ident].update(options)

    def coords(self, ident, *coords):
        self.items[ident]["coords"] = coords

    def bbox(self, ident):
        return (20, 64, 420, 78)

    def configure(self, **options):
        self.options.update(options)


class Native:
    def __init__(self, result=1):
        self.composition, self.decorations = [], []
        self.hwnd = 0x10000004D  # Exercise the pointer-sized HWND signatures.
        self.user32 = SimpleNamespace(
            GetParent=Mock(return_value=self.hwnd), GetWindowLongW=Mock(return_value=0x20),
            SetWindowLongW=Mock(), ShowWindow=Mock(), SetWindowPos=Mock(),
            SetWindowCompositionAttribute=Mock(side_effect=lambda hwnd, data: self.compose(hwnd, data, result)))
        self.dwmapi = SimpleNamespace(DwmSetWindowAttribute=Mock(side_effect=self.decorate))

    def compose(self, hwnd, data, result):
        data = data._obj
        accent = ctypes.cast(data.Data, ctypes.POINTER(voiceui.ACCENT_POLICY)).contents
        self.composition.append((hwnd, data.Attribute, accent.AccentState,
                                 accent.GradientColor, data.SizeOfData))
        return result

    def decorate(self, hwnd, attribute, value, size):
        self.decorations.append((hwnd, attribute, value._obj.value, size))
        return 0


class GlassOverlay(unittest.TestCase):
    def overlay(self, result=1, platform="nt"):
        native = Native(result)
        window = Mock()
        window.winfo_id.return_value = 42
        window.winfo_screenwidth.return_value = 1920
        window.winfo_screenheight.return_value = 1080
        tk = SimpleNamespace(Toplevel=Mock(return_value=window), Canvas=Canvas)
        root = SimpleNamespace(after=Mock())
        # Patch only voiceui's OS reference, leaving pathlib and the runner alone.
        with patch.object(voiceui, "os", SimpleNamespace(name=platform)), \
                patch.dict("sys.modules", tkinter=tk), \
                self.assertLogs("jarvis.voice", level="INFO") as logs:
            overlay = voiceui.Overlay(root, user32=native.user32, dwmapi=native.dwmapi)
        return overlay, window, native, tk, logs.output

    def test_acrylic_exposes_blur_with_sheen_and_no_panel(self):
        overlay, window, native, tk, logs = self.overlay()
        self.assertTrue(overlay.glass)
        self.assertEqual(overlay.canvas.items[overlay.panel]["state"], "hidden")
        sheen = overlay.canvas.items[overlay.sheen]
        self.assertEqual((sheen["state"], sheen["width"]), ("normal", 1))
        self.assertEqual(overlay.canvas.options["bg"], voiceui.GLASS_KEY)
        window.configure.assert_called_once_with(bg=voiceui.GLASS_KEY)
        window.attributes.assert_any_call("-transparentcolor", voiceui.GLASS_KEY)
        window.attributes.assert_any_call("-alpha", 1.0)
        self.assertIn("pop-up glass: acrylic", logs[0])
        self.assertEqual(native.composition, [(native.hwnd, 19, 4, voiceui.GLASS_TINT, 16)])
        self.assertIn((native.hwnd, 33, 2, 4), native.decorations)
        self.assertIn((native.hwnd, 34, 0x007B6A3E, 4), native.decorations)
        # A resize/state transition must not reapply acrylic.
        overlay.set_state("answering", "A response")
        overlay._tick()
        self.assertEqual(len(native.composition), 1)
        self.assertEqual(overlay.canvas.items[overlay.sheen]["coords"], (18, 1, 622, 1))

    def test_refused_acrylic_draws_translucent_rounded_fallback(self):
        overlay, window, native, tk, logs = self.overlay(0)
        self.assertFalse(overlay.glass)
        panel = overlay.canvas.items[overlay.panel]
        self.assertEqual((panel["state"], panel["fill"], panel["outline"], panel["smooth"]),
                         ("normal", "#16212a", "#3a4a56", True))
        self.assertNotEqual(panel["fill"], voiceui.GLASS_KEY)
        self.assertEqual(overlay.canvas.items[overlay.sheen]["state"], "hidden")
        window.attributes.assert_any_call("-alpha", 0.88)
        self.assertIn("pop-up glass: tinted fallback", logs[0])

    def test_both_paths_keep_native_nonactivation_on_show_resize_and_hide(self):
        for result in (0, 1):
            with self.subTest(acrylic=bool(result)):
                overlay, window, native, tk, logs = self.overlay(result)
                tk.Toplevel.assert_called_once_with(overlay.root, takefocus=False)
                window.overrideredirect.assert_called_once_with(True)
                window.attributes.assert_any_call("-topmost", True)
                native.user32.SetWindowLongW.assert_called_once_with(
                    native.hwnd, -20, 0x20 | voiceui.WS_EX_NOACTIVATE | voiceui.WS_EX_TOOLWINDOW)
                for state, width in (("listening", 440), ("answering", 640)):
                    overlay.set_state(state, "Example")
                    overlay._tick()
                    self.assertEqual((overlay.width, overlay.height), (width, 96))
                    native.user32.SetWindowPos.assert_called_with(
                        native.hwnd, -1, (1920-width)//2, 904, width, 96,
                        voiceui.SWP_NOACTIVATE | voiceui.SWP_SHOWWINDOW)
                    native.user32.ShowWindow.assert_called_with(native.hwnd, voiceui.SW_SHOWNOACTIVATE)
                overlay.set_state("hidden")
                overlay._tick()
                native.user32.ShowWindow.assert_called_with(native.hwnd, 0)
                self.assertEqual(overlay.canvas.items[overlay.label]["text"], "")
                window.deiconify.assert_not_called()
                window.focus_set.assert_not_called()
                window.focus_force.assert_not_called()

    def test_state_colours_reach_every_bar_and_label_on_both_paths(self):
        expected = {"listening": "#6fc3df", "transcribing": "#fbbf24", "thinking": "#fbbf24",
                    "answering": "#6fc3df", "done": "#6fc3df", "error": "#fb7185"}
        for result in (0, 1):
            overlay, window, native, tk, logs = self.overlay(result)
            for state, colour in expected.items():
                with self.subTest(acrylic=bool(result), state=state):
                    overlay.set_state(state)
                    overlay._tick()
                    self.assertEqual(voiceui.state_colour(state), colour)
                    self.assertEqual([overlay.canvas.items[i]["fill"] for i in overlay.bar_ids],
                                     [colour] * 24)
                    self.assertEqual(overlay.canvas.items[overlay.label]["fill"],
                                     voiceui.RED if state == "error" else voiceui.INK)

    def test_non_windows_constructs_headless_without_native_access(self):
        overlay, window, native, tk, logs = self.overlay(platform="posix")
        self.assertIsNone(overlay.user32)
        self.assertFalse(overlay.glass)
        native.user32.GetParent.assert_not_called()
        native.user32.SetWindowCompositionAttribute.assert_not_called()
        overlay.set_state("listening")
        overlay._tick()
        window.deiconify.assert_called_once_with()


class NativeFailure(unittest.TestCase):
    def test_missing_or_raising_composition_never_escapes(self):
        for error in (AttributeError("missing"), OSError("unsupported"), RuntimeError("failure")):
            with self.subTest(error=error):
                user32 = SimpleNamespace(SetWindowCompositionAttribute=Mock(side_effect=error))
                self.assertFalse(voiceui.acrylic(77, voiceui.GLASS_TINT, user32=user32, dwmapi=None))
        self.assertFalse(voiceui.acrylic(77, voiceui.GLASS_TINT, user32=object(), dwmapi=None))

    def test_dwm_refusal_or_missing_api_does_not_disable_acrylic(self):
        for dwmapi in (object(), SimpleNamespace(DwmSetWindowAttribute=Mock(return_value=-1)),
                       SimpleNamespace(DwmSetWindowAttribute=Mock(side_effect=OSError("unsupported")))):
            with self.subTest(dwmapi=dwmapi):
                native = Native()
                self.assertTrue(voiceui.acrylic(77, voiceui.GLASS_TINT,
                                               user32=native.user32, dwmapi=dwmapi))

    def test_tint_channels_and_native_structure_layout(self):
        self.assertEqual(voiceui.GLASS_TINT & 0xFFFFFF, 0x2A2116)
        self.assertTrue(0x40 <= voiceui.GLASS_TINT >> 24 <= 0xE0)
        self.assertEqual(ctypes.sizeof(voiceui.ACCENT_POLICY), 16)
        self.assertEqual(voiceui.ACCENT_POLICY.GradientColor.offset, 8)
        self.assertEqual(voiceui.WINDOWCOMPOSITIONATTRIBDATA._fields_,
                         [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p),
                          ("SizeOfData", ctypes.c_size_t)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
