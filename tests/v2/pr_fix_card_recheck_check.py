"""PR #340 re-check: replay an OK after a different fix starts at the same head.

Hermetic, using pr_fix_check's real fix lifecycle and pr_merge_check's fake gh.
Run with PYTHONPATH=$PWD TMPDIR=/var/tmp and an isolated HOME.
"""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pr_fix_check as FX
import pr_merge_check as PM
from jarvis.v2 import pr_merge
from jarvis.v2.model import TaskState


setUpModule = FX.setUpModule
tearDownModule = FX.tearDownModule


class FixCardRecheck(FX.Fixing):
    def actor(self, facts):
        run = PM.GhRun()
        actor = pr_merge.PrActions(self.daemon, run=run, gh=PM.PF.GH,
                                   read=lambda *a, **kw: facts)
        return actor, run

    def test_old_merge_as_is_cannot_authorize_a_different_fix_attempt(self):
        first = self.start()["task_id"]
        drawn = copy.deepcopy(self.row()["detail"])
        self.assertEqual(drawn["actions"], ["merge_as_is"])
        self.assertEqual((drawn["fix"]["state"], drawn["fix"]["count"]), ("running", 1))

        # A second surface cancels the first task and starts another, without
        # changing the PR head. The first surface misses both inbox events.
        self.fixes.cancel_fix(self.pr_id)
        second = self.start()["task_id"]
        current = self.row()["detail"]
        self.assertNotEqual(first, second)
        self.assertEqual((current["fix"]["state"], current["fix"]["count"]), ("running", 2))
        self.assertEqual(current["head"], drawn["head"])
        self.assertEqual(current["actions"], drawn["actions"])

        actor, run = self.actor(FX.F())
        with self.assertRaises(pr_merge.Refused,
                               msg="stale Merge as is accepted a different fix task and attempt count"):
            actor.act("merge_as_is", self.pr_id, drawn["head"], drawn["card"], via="hud")
        self.assertEqual(run.acts(), [], "no gh merge may run for an unseen fix task")

    def test_unchanged_live_fix_card_matches_after_fix_action_is_removed(self):
        self.start()
        drawn = self.row()["detail"]
        self.assertEqual(drawn["actions"], ["merge_as_is"])
        actor, _run = self.actor(FX.F())
        decision = actor.judge(FX.F(), "merge_as_is", drawn["head"], drawn["card"],
                               self.pr_id, via="hud", limited=False)
        self.assertIn("merge_as_is", decision.actions)

    def test_unchanged_capped_fix_card_matches_after_fix_action_is_removed(self):
        for _ in range(3):
            task_id = self.start()["task_id"]
            self.end(task_id, TaskState.CANCELLED)
        drawn = self.row()["detail"]
        self.assertEqual(drawn["actions"], ["merge_as_is"])
        self.assertEqual(drawn["fix"]["count"], 3)
        actor, _run = self.actor(FX.F())
        decision = actor.judge(FX.F(), "merge_as_is", drawn["head"], drawn["card"],
                               self.pr_id, via="phone", limited=False)
        self.assertIn("merge_as_is", decision.actions)

    def test_unchanged_ready_fix_cards_match_on_phone_and_desk(self):
        task_id = self.start()["task_id"]
        self.end(task_id, TaskState.DONE)
        self.compares[(FX.HEAD, FX.HEAD2)] = {
            "ancestor": True, "commits": 1, "subjects": ["fix retries"]}
        facts = FX.F(head=FX.HEAD2, checks="passing", failing=(), merge_state="CLEAN")
        self.put(facts)
        self.fixes.on_facts(facts)
        PM.eventually(lambda: self.row()["detail"]["fix"]["summary_state"] == "ready")

        # Use the real fix projection for both ready and still-pending summaries.
        key = FX.X.fix_key(FX.REPO, FX.N, FX.HEAD, FX.HEAD2)
        for summary_state in ("ready", "pending"):
            with self.fixes._lock:
                self.fixes._fixed[key] = (summary_state, "Fixed the retry count." if summary_state == "ready" else "")
            self.prs.refresh([self.pr_id])
            drawn = self.row()["detail"]
            self.assertEqual(drawn["fix"]["state"], "ready")
            for via in ("hud", "phone"):
                with self.subTest(summary_state=summary_state, via=via):
                    actor, _run = self.actor(facts)
                    decision = actor.judge(facts, "merge", drawn["head"], drawn["card"],
                                           self.pr_id, via=via, limited=False)
                    self.assertIn("merge", decision.actions)


if __name__ == "__main__":
    unittest.main()
