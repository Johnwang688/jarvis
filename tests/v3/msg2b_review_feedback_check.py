"""A proposal's initial relay must not become fresh planner work."""

import unittest

import msg2b_check as fixtures
from jarvis.v3.claude_jobs import JobResult
from planner_check import basis, proposal, reply


class Feedback(unittest.TestCase):
    def test_completed_relays_do_not_generate_follow_up_proposals(self):
        fx = fixtures.PlannerMessages()
        self.addCleanup(fx.doCleanups)
        fx.setUp()
        first = proposal(basis(fx.signal()), "thread.start")
        self.assertEqual(fx.run_items(first).posted, 1)

        for step in range(2):
            # T3 completed the requested relay. The owner has yet to reply.
            fx.now += 300
            thread = fx.ledger.threads()[0]
            fx.ledger.record_status(thread.thread_id, status="completed",
                                    waiting_on_owner=False, at=fx.now)
            ref = f"ledger:{thread.thread_id}"

            def recorded_reply(_job, prompt, **_kwargs):
                items = [proposal(basis(ref, "thread"), "thread.start")] if ref in prompt else []
                return JobResult("planner", "sonnet", ok=True, outcome="ok", text=reply(*items))

            with self.subTest(step=step):
                result = fx.run_planner(recorded_reply)
                self.assertIn(result.outcome, {"ok", "nothing"})
                self.assertEqual(result.posted, 0)


if __name__ == "__main__":
    unittest.main()
