"""I2 restart and state-limit regressions."""

import unittest
from unittest.mock import patch

from i2_acceptance_check import Base, NOW, cal
from jarvis.v3 import triggers


class Durability(Base):
    def test_calendar_move_back_after_interrupted_checkpoint_is_detected(self):
        initial = cal("planning")
        moved = dict(initial, start="2027-01-20T17:00:00Z", end="2027-01-20T18:00:00Z")
        self.put("calendar", [initial])
        self.tick(("calendar",))
        self.put("calendar", [moved])
        atomic = triggers._atomic

        def interrupt_checkpoint(directory, name, data):
            if name == "state.json":
                raise OSError("interrupted checkpoint")
            return atomic(directory, name, data)

        with patch.object(triggers, "_atomic", side_effect=interrupt_checkpoint):
            with self.assertRaisesRegex(OSError, "interrupted checkpoint"):
                self.tick(("calendar",), at=NOW + 300)
        [first] = self.log()
        self.assertEqual(first["summary_fields"]["start"], moved["start"])

        self.put("calendar", [initial])
        self.tick(("calendar",), at=NOW + 600)
        restored = self.log(after=first["seq"])
        self.assertEqual(len(restored), 1)
        fields = restored[0]["summary_fields"]
        self.assertEqual((fields["change"], fields["previous_start"], fields["start"]),
                         ("moved", moved["start"], initial["start"]))

    def test_unexpired_edge_history_at_capacity_does_not_stop_new_signals(self):
        # Scale the byte cap down while keeping the real per-kind and TTL rules.
        cap = 2048
        self.put("calendar", [])
        with patch.object(triggers, "_STATE_LIMIT", cap):
            self.tick(("calendar",))
            for number in range(1, 31):
                event_id = f"teamplanningworkshop{number:02d}_20270120T150000Z"
                self.put("calendar", [cal(event_id)])
                self.tick(("calendar",), at=NOW + number * 300)

        self.assertEqual(self.log()[-1]["summary_fields"]["event"], event_id)
        self.assertLessEqual((self.root / "state.json").stat().st_size, cap)


if __name__ == "__main__":
    unittest.main()
