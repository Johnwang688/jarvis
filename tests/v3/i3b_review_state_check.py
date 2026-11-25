import json
import unittest
from datetime import datetime
from unittest.mock import patch

from planner_check import basis, proposal, reply
import planner_check as fixture
from jarvis.tools import secrets
from jarvis.v3 import planner


class StateReview(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(secrets, "secret_values", return_value=[]))
        fixture.PlannerCheck.setUp(self)

    register = fixture.PlannerCheck.register
    signal = fixture.PlannerCheck.signal
    signal_at = fixture.PlannerCheck.signal_at
    recorded = fixture.PlannerCheck.recorded
    run_planner = fixture.PlannerCheck.run_planner
    inputs = fixture.PlannerCheck.inputs

    def test_retry_obeys_a_new_budget_refusal(self):
        self.signal()
        runner = self.recorded("{", reply())
        replies = runner.side_effect

        def respond(*_args, **_kwargs):
            self.usage = 75
            return next(replies)

        runner.side_effect = respond
        result = self.run_planner(runner)
        runner.assert_called_once()
        self.assertEqual(result.outcome, "skipped-usage")
        self.assertEqual(self.admit.call_count, 2)
        self.assertEqual(self.store.proposals(), [])

    def test_local_window_across_spring_dst_boundary(self):
        state = planner.DueState(ledger_changed=True)
        for day in (7, 8):
            for hour, minute, expected in ((7, 59, False), (8, 0, True),
                                           (21, 59, True), (22, 0, False)):
                with self.subTest(day=day, hour=hour, minute=minute):
                    instant = datetime(2026, 3, day, hour, minute, tzinfo=fixture.CHICAGO)
                    self.assertEqual(planner.due(instant.timestamp(), None, state), expected)

    def test_recovered_briefs_count_against_current_run_cap(self):
        ref = basis(self.signal())
        for index in range(3):
            item = proposal(ref, "thread.start")
            item["action"]["project"] = self.register(f"fixture-{index}").name
            draft, = planner.parse(reply(item), self.inputs())
            self.assertIsNotNone(self.store.add(draft, run=f"fixture-{index}"))
        fresh = proposal(ref, "thread.start")
        self.assertEqual(self.run_planner(self.recorded(reply(fresh))).posted, 3)
        self.assertEqual(len(self.store.proposals("briefed")), 3)

    def test_run_cap_counts_new_and_recovered_briefs_together(self):
        ref = basis(self.signal())
        pending, = planner.parse(reply(proposal(ref, "thread.start")), self.inputs())
        self.assertIsNotNone(self.store.add(pending, run="fixture-previous"))
        items = []
        for index in range(3):
            item = proposal(ref, "thread.start")
            item["action"]["project"] = self.register(f"fixture-{index}").name
            items.append(item)
        self.assertEqual(self.run_planner(self.recorded(reply(*items))).posted, 3)
        self.assertEqual(len(self.store.proposals("briefed")), 3)


if __name__ == "__main__":
    unittest.main()
