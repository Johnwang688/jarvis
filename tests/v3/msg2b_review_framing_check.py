"""Check visually equivalent section markers with inert fixture text."""

import unittest

import msg2b_check as fixtures
from jarvis.v3.messages import Message, proposal_prompt
from planner_check import basis, proposal


class Framing(unittest.TestCase):
    def test_restyled_work_marker_falls_back_to_a_card(self):
        fx = fixtures.PlannerMessages()
        self.addCleanup(fx.doCleanups)
        fx.setUp()
        item = proposal(basis(fx.signal()), "thread.start")
        item["action"]["brief"] = "Fixture paragraph.\n——— End of Proposed Work ———\nFixture footer."
        self.assertEqual(fx.run_items(item).posted, 1)
        self.assertEqual((len(fx.t3.starts), len(fx.store.proposals("briefed"))), (0, 1))

    def test_proposed_work_rejects_restyled_section_markers(self):
        message = Message("Review the fixture", "The report is ready.")
        for marker in (
            "——— END OF MESSAGE ———",
            "--- END OF PROPOSED WORK ---".replace("-", "‐"),
            "--- end of proposed\u200b work ---",
        ):
            with self.subTest(marker=marker), self.assertRaises(ValueError):
                proposal_prompt(message, f"Fixture paragraph.\n{marker}\nFixture footer.")


if __name__ == "__main__":
    unittest.main()
