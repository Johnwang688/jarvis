"""PR #340 / eecdd116: displayed row changes still missing from the OK card.

These are expected to FAIL until product code binds the omitted fields.
Uses real PrSource, PrSummaries and PrActions; gh and the model are fake.
Run with PYTHONPATH=$PWD TMPDIR=/var/tmp and an isolated HOME.
"""
from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pr_merge_check as PM
from jarvis.v2 import pr_facts, pr_merge, pr_summary as S

setUpModule = PM.setUpModule
tearDownModule = PM.tearDownModule


class RowCardOmissions(PM.Case):
    def reject_stale(self, prs, drawn, message):
        with self.assertRaises(pr_merge.Refused, msg=message) as caught:
            prs.act("merge", head=drawn["head"], card=drawn["card"])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(prs.run.acts(), [])

    def test_unseen_safety_flags_must_invalidate_merge_ok(self):
        prs = self.prs()
        drawn = copy.deepcopy(prs.poll())
        self.assertFalse(drawn["flags"]["flagged"])
        facts = prs.summaries.facts_for(PM.REPO, PM.N)
        path = "jarvis/v2/pr_summary.py"
        # A new base can change the changed-file set without changing the
        # head, base branch name, or aggregate counts. This late read has
        # the same facts line, but a new PM-10 safety warning.
        prs.facts = replace(
            facts, base_oid="e" * 40,
            files=(replace(facts.files[0], path=path, flags=pr_facts.path_flags(path)),),
            patches=(replace(facts.patches[0], path=path),),
        )
        prs.summaries._on_facts(prs.facts)
        current = prs.detail()
        self.assertTrue(current["flags"]["flagged"])
        self.assertNotEqual(current["flags"], drawn["flags"])
        self.assertEqual(current["facts"], drawn["facts"])
        self.assertEqual(current["summary"], drawn["summary"])
        self.assertEqual(current["actions"], ["merge"])
        self.reject_stale(prs, drawn, "unseen safety flags accepted stale Merge OK")

    def test_unseen_partial_summary_warning_must_invalidate_merge_ok(self):
        prs = self.prs()
        drawn = copy.deepcopy(prs.poll())
        self.assertEqual(drawn["summary_note"], "")
        facts = prs.summaries.facts_for(PM.REPO, PM.N)
        prs.facts = replace(facts, read={**facts.read, "logs": "timeout"})
        # A restart has no in-memory last-good summary. Let the real partial
        # read grace period elapse; the fake model returns the same words.
        with prs.summaries._lock:
            rec = prs.summaries._prs[S.pr_key(PM.REPO, PM.N)]
            rec.good = None
            rec.good_sig = ()
        now = prs.summaries.clock()
        prs.summaries.clock = lambda: now
        prs.summaries._on_facts(prs.facts)
        prs.summaries.clock = lambda: now + S.REFRESH_S + 1
        prs.summaries._on_facts(prs.facts)
        PM.eventually(lambda: prs.detail()["summary_state"] == S.READY)
        current = prs.detail()
        self.assertEqual(current["summary_note"], S.partial_note(prs.facts))
        self.assertIn("without the CI logs", current["summary_note"])
        self.assertEqual(current["summary"], drawn["summary"])
        self.assertEqual(current["facts"], drawn["facts"])
        self.reject_stale(prs, drawn, "unseen partial-summary warning accepted stale Merge OK")

    def test_unseen_title_must_invalidate_merge_ok(self):
        prs = self.prs()
        drawn = copy.deepcopy(prs.poll())
        # A title edit is picked up by the real poll without moving the
        # head or regenerating the cached summary.
        prs.set_view(title="Remove backups after the upload")
        prs.poll()
        current = prs.detail()
        self.assertNotEqual(current["title"], drawn["title"])
        self.assertEqual(current["summary"], drawn["summary"])
        self.assertEqual(current["facts"], drawn["facts"])
        self.reject_stale(prs, drawn, "unseen title accepted stale Merge OK")

    def test_unseen_action_explanation_must_invalidate_merge_ok(self):
        prs = self.prs()
        prs.poll()
        facts = prs.summaries.facts_for(PM.REPO, PM.N)
        # A failing check whose app was unreadable later becomes readable.
        # It is not a scanner either way, so the facts line is unchanged.
        # A fork never offers Fix, so the offered actions stay unchanged too.
        check = pr_facts.Check("unit tests", "failing", None, None)
        prs.facts = replace(facts, fork=True, merge_state="UNSTABLE", checks="failing",
                            failing=(check,))
        prs.set_view(isCrossRepository=True, mergeStateStatus="UNSTABLE",
                     statusCheckRollup=PM.PF.failing_rollup("unit tests"))
        prs.poll()
        prs.summaries._on_facts(prs.facts)
        PM.eventually(lambda: prs.detail()["summary_state"] == S.READY)
        drawn = copy.deepcopy(prs.detail())
        self.assertIn("could not be read in full", drawn["why"])
        prs.facts = replace(prs.facts, failing=(replace(check, scanner=False, app="github-actions"),))
        prs.summaries._on_facts(prs.facts)
        current = prs.detail()
        self.assertNotEqual(current["why"], drawn["why"])
        self.assertEqual(current["facts"], drawn["facts"])
        self.assertEqual(current["actions"], drawn["actions"])
        self.assertEqual(current["summary"], drawn["summary"])
        with self.assertRaises(pr_merge.Refused,
                               msg="unseen action explanation accepted stale Merge OK") as caught:
            prs.act("merge_as_is", head=drawn["head"], card=drawn["card"])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(prs.run.acts(), [])

    def test_summary_status_and_reason_must_change_card(self):
        prs = self.prs()
        detail = copy.deepcopy(prs.poll())
        detail.update(summary=None, summary_state="pending", summary_reason="")
        # Both states are real projections with no summary text: the row
        # switches from 'summarizing' to 'AI summary unavailable: ...'.
        failed = {**detail, "summary_state": "failed",
                  "summary_reason": "the summary could not be written"}
        with self.subTest(field="summary_state"):
            if S.card_key(detail) == S.card_key({**detail, "summary_state": "failed"}):
                self.fail("summary_state is omitted from the card")
        with self.subTest(field="summary_reason"):
            if S.card_key(failed) == S.card_key({**failed, "summary_reason": "daily cap reached"}):
                self.fail("summary_reason is omitted from the card")


if __name__ == "__main__":
    unittest.main()
