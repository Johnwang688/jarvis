"""A full dedupe checkpoint must stay quiet once a backlog has drained."""

import unittest
from unittest.mock import patch

from i2_acceptance_check import Base, NOW
from jarvis.v3 import triggers


class DeltaEviction(Base):
    def test_active_mail_does_not_reemit_in_a_loop_at_the_state_cap(self):
        cap = 2048
        message_ids = {f"18f751000000{number:04x}" for number in range(40)}
        for mid in sorted(message_ids):
            self.index.add(mid, mid, NOW - 7200)

        with patch.object(triggers, "_STATE_LIMIT", cap):
            self.assertEqual(self.tick(("mail",))["emitted"], 20)
            self.assertEqual(self.tick(("mail",), at=NOW + 300)["emitted"], 20)
            backlog = self.log()
            self.assertEqual({row["summary_fields"]["message"] for row in backlog}, message_ids)
            for offset in (600, 900, 1200):
                with self.subTest(offset=offset):
                    self.assertLessEqual((self.root / "state.json").stat().st_size, cap)
                    self.assertEqual(self.tick(("mail",), at=NOW + offset)["emitted"], 0)


if __name__ == "__main__":
    unittest.main()
