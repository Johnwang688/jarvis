"""Free checks for `tests/loadclock.py`, the CPU-time judge that the cost,
flood and throughput bounds share (QA-LOAD). Its clamps are what keep a
busy machine from excusing a regression, so each is checked on a fake
reference clock: no real load, no timing."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import loadclock as L  # noqa: E402


def timings(*seconds):
    """A fake `cpu` whose reference runs take `seconds` in turn."""
    it = iter(seconds)
    return lambda fn, *a, **kw: (next(it), None)


class Judge(unittest.TestCase):
    def test_constants_are_pinned(self):
        L.pinned()
        with patch.object(L, "MAX_SLOWDOWN", 4.0), self.assertRaises(AssertionError):
            L.pinned()
        with patch.object(L, "REFERENCE_IDLE_S", 0.02), self.assertRaises(AssertionError):
            L.pinned()

    def test_a_run_under_its_bound_is_never_scaled_or_timed_again(self):
        with patch.object(L, "slowdown", side_effect=AssertionError("timed the reference")):
            held = L.judge(0.05, 0.1)
        self.assertEqual((held.cpu, held.slow, held.idle), (0.05, 1.0, 0.05))

    def test_a_miss_is_judged_at_the_present_speed(self):
        with patch.object(L, "cpu", timings(0.021, 0.02, 0.022)):
            held = L.judge(0.15, 0.1)
        self.assertAlmostEqual(held.slow, 2.0)          # the fastest, 20 ms against 10
        self.assertAlmostEqual(held.idle, 0.075)
        self.assertIn("2.00x slow", str(held))

    def test_a_paused_reference_run_never_loosens_the_bound(self):
        with patch.object(L, "cpu", timings(0.5, 0.012, 0.3)):
            self.assertAlmostEqual(L.slowdown(), 1.2)

    def test_a_machine_slower_than_the_cap_excuses_no_more(self):
        # 10x slower: still only 3x excused, so a run three times its bound fails.
        with patch.object(L, "cpu", timings(0.1, 0.1, 0.1)):
            held = L.judge(0.31, 0.1)
        self.assertEqual(held.slow, L.MAX_SLOWDOWN)
        self.assertGreater(held.idle, 0.1)

    def test_a_fast_moment_never_tightens_past_the_bound(self):
        with patch.object(L, "cpu", timings(0.004, 0.004, 0.004)):
            self.assertEqual(L.slowdown(), 1.0)

    def test_the_reference_is_real_work(self):
        took, value = L.cpu(L.reference)
        self.assertGreater(value, 50)
        self.assertGreater(took, 0)


if __name__ == "__main__":
    unittest.main()
