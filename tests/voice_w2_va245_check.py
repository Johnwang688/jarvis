"""VA-2/4/5 package W2 acceptance tests: overlay and wake listener, pure parts
(orchestrator-owned; the implementer may not edit this file).

Plan: docs/plans/2026-11-16-voice-anywhere-va245.md ("Windows interfaces").
The modules must import on Linux: Windows-only and heavy imports (ctypes.windll,
tkinter, sounddevice, sherpa_onnx, numpy) happen inside the glue, never at import.

Run: PYTHONPATH=. .venv/bin/python tests/voice_w2_va245_check.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "windows"))

import voiceui  # noqa: E402
import voicewake  # noqa: E402

MODEL = ROOT / "hud" / "vendor" / "kws-zipformer-gigaspeech-3.3M"


class Overlay(unittest.TestCase):
    def test_states(self):
        for state in ("hidden", "listening", "transcribing", "thinking", "answering", "done", "error"):
            self.assertIn(state, voiceui.STATES)

    def test_bars_length_padding_order_and_scale(self):
        self.assertEqual(voiceui.bars([], 4), [0.0, 0.0, 0.0, 0.0])
        b = voiceui.bars([0.05], 4)
        self.assertEqual(b[:3], [0.0, 0.0, 0.0])
        self.assertGreater(b[3], 0.0)
        b = voiceui.bars([0.0, 0.01, 0.05, 0.5], 4)
        self.assertEqual(b[0], 0.0)
        self.assertTrue(b[0] < b[1] < b[2] <= b[3] == 1.0, b)
        self.assertEqual(len(voiceui.bars([0.1] * 100, 24)), 24)
        self.assertTrue(all(0.0 <= x <= 1.0 for x in voiceui.bars([5.0, -1.0, 0.2], 3)))

    def test_bottom_middle(self):
        self.assertEqual(voiceui.place(1920, 1080, 400, 80), (760, 920))
        self.assertEqual(voiceui.place(2560, 1440, 500, 120), (1030, 1240))


class Endpoint(unittest.TestCase):
    FRAME = 320  # 20 ms at 16 kHz

    def run_frames(self, ep, rms, ms):
        out = None
        for _ in range(ms // 20):
            out = ep.feed(rms, self.FRAME)
            if out:
                return out
        return out

    def test_ends_after_trailing_silence(self):
        ep = voicewake.Endpointer(rate=16000, silence_ms=800, max_ms=15000)
        self.assertIsNone(self.run_frames(ep, 0.001, 200))
        self.assertIsNone(self.run_frames(ep, 0.1, 1000))
        self.assertIsNone(self.run_frames(ep, 0.001, 600))
        self.assertEqual(self.run_frames(ep, 0.001, 400), "end")

    def test_no_speech_times_out(self):
        ep = voicewake.Endpointer(rate=16000, silence_ms=800, max_ms=15000)
        self.assertEqual(self.run_frames(ep, 0.001, 5000), "timeout")

    def test_long_speech_hits_the_cap(self):
        ep = voicewake.Endpointer(rate=16000, silence_ms=800, max_ms=15000)
        self.assertEqual(self.run_frames(ep, 0.1, 16000), "max")

    def test_short_pause_inside_speech_does_not_end(self):
        ep = voicewake.Endpointer(rate=16000, silence_ms=800, max_ms=15000)
        self.run_frames(ep, 0.1, 500)
        self.assertIsNone(self.run_frames(ep, 0.001, 400))
        self.assertIsNone(self.run_frames(ep, 0.1, 500))


class Keywords(unittest.TestCase):
    def test_hud_pieces_and_threshold(self):
        self.assertEqual(voicewake.keywords_text(["▁JA", "R", "VI", "S"]),
                         "▁JA R VI S :1.0 #0.25 @JARVIS\n")


class Model(unittest.TestCase):
    def test_the_vendored_model_verifies(self):
        ok, reason = voicewake.verify_model(MODEL)
        self.assertTrue(ok, reason)

    def test_a_changed_or_missing_file_does_not(self):
        with tempfile.TemporaryDirectory() as t:
            copy = Path(t) / "kws"
            shutil.copytree(MODEL, copy)
            with open(copy / "joiner.int8.onnx", "r+b") as f:
                f.seek(100)
                f.write(b"\x00\x01\x02")
            ok, reason = voicewake.verify_model(copy)
            self.assertFalse(ok)
            self.assertTrue(reason)
            (copy / "tokens.txt").unlink()
            self.assertFalse(voicewake.verify_model(copy)[0])

    def test_listener_will_not_start_on_a_bad_model(self):
        with tempfile.TemporaryDirectory() as t:
            listener = voicewake.WakeListener(lambda wav: None, lambda rms: None, lambda: False,
                                              model_dir=Path(t))
            self.assertFalse(listener.start())
            self.assertTrue(listener.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
