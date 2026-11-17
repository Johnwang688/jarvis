"""VA-7 glass acceptance (orchestrator-owned): the voice pop-up in aurora glass.

Owner (2026-11-17): the pop-up should follow the HUD's liquid/aurora glass,
with real Windows 11 acrylic blur, falling back to a tinted panel if Windows
refuses. The HUD keeps amber for pending and red for errors fixed; the accent
is the HUD's cyan. Looks are checked live by the lead; these pin the calls.
"""
from __future__ import annotations

import ctypes
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows import voiceui  # noqa: E402


class FakeUser32:
    def __init__(self, result=1):
        self.result, self.calls = result, []

    def SetWindowCompositionAttribute(self, hwnd, data):
        struct = data._obj
        accent = ctypes.cast(struct.Data, ctypes.POINTER(voiceui.ACCENT_POLICY)).contents
        self.calls.append((hwnd, struct.Attribute, accent.AccentState, accent.GradientColor,
                           struct.SizeOfData))
        return self.result


class FakeDwm:
    def __init__(self):
        self.calls = []

    def DwmSetWindowAttribute(self, hwnd, attr, value, size):
        self.calls.append((hwnd, attr, value._obj.value, size))
        return 0


class Acrylic(unittest.TestCase):
    def test_requests_acrylic_blur_and_round_corners(self):
        u, d = FakeUser32(), FakeDwm()
        self.assertTrue(voiceui.acrylic(77, voiceui.GLASS_TINT, user32=u, dwmapi=d))
        (hwnd, attribute, state, tint, size), = u.calls
        self.assertEqual((hwnd, attribute, state), (77, 19, 4))  # WCA_ACCENT_POLICY, ACRYLIC
        self.assertEqual(tint, voiceui.GLASS_TINT)
        self.assertEqual(size, ctypes.sizeof(voiceui.ACCENT_POLICY))
        self.assertIn((77, 33, 2, 4), d.calls)  # DWMWA_WINDOW_CORNER_PREFERENCE = ROUND

    def test_refusal_or_missing_api_falls_back(self):
        self.assertFalse(voiceui.acrylic(77, voiceui.GLASS_TINT, user32=FakeUser32(0),
                                         dwmapi=FakeDwm()))
        self.assertFalse(voiceui.acrylic(77, voiceui.GLASS_TINT, user32=object(),
                                         dwmapi=FakeDwm()))

    def test_tint_is_a_translucent_abgr(self):
        alpha = (voiceui.GLASS_TINT >> 24) & 0xFF
        self.assertTrue(0x40 <= alpha <= 0xE0, hex(voiceui.GLASS_TINT))


class Palette(unittest.TestCase):
    def test_hud_accent_and_fixed_pending_error_colours(self):
        self.assertEqual(voiceui.ACCENT.lower(), "#6fc3df")
        self.assertEqual(voiceui.state_colour("listening").lower(), "#6fc3df")
        for pending in ("transcribing", "thinking"):
            self.assertEqual(voiceui.state_colour(pending).lower(), voiceui.AMBER.lower())
        self.assertEqual(voiceui.state_colour("error").lower(), voiceui.RED.lower())
        self.assertEqual(voiceui.AMBER.lower(), "#fbbf24")
        self.assertEqual(voiceui.RED.lower(), "#fb7185")


if __name__ == "__main__":
    unittest.main()
