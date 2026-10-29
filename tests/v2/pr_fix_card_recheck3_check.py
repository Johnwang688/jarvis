"""PR #340 / 740c0546: fresh-read changes before the next inbox refresh.

Expected failures: an old OK must not authorize newly read title, safety
warnings, or action explanations. Real summaries/source/action actor; fake gh
and model from pr_merge_check. No product patches, network, or live ports.
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


class FreshReadCard(PM.Case):
    def reject_unseen(self, prs, drawn, action, message):
        # Only the actor's mandatory fresh read sees the new facts: no poll
        # or summary callback has refreshed the cached inbox row yet.
        self.assertEqual(prs.detail(), drawn)
        with self.assertRaises(pr_merge.Refused, msg=message) as caught:
            prs.act(action, head=drawn["head"], card=drawn["card"])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(prs.run.acts(), [])

    def test_fresh_safety_warning_change_on_already_flagged_pr(self):
        prs = self.prs()
        prs.poll()
        old = prs.summaries.facts_for(PM.REPO, PM.N)
        path = ".github/workflows/ci.yml"
        prs.facts = replace(old, files=(replace(old.files[0], path=path,
                            flags=pr_facts.path_flags(path)),),
                            patches=(replace(old.patches[0], path=path),))
        prs.summaries._on_facts(prs.facts)
        drawn = copy.deepcopy(prs.detail())
        self.assertTrue(drawn["flags"]["flagged"])
        path = "jarvis/v2/pr_merge.py"
        prs.facts = replace(prs.facts, base_oid="a" * 40,
                            files=(replace(old.files[0], path=path,
                                   flags=pr_facts.path_flags(path)),),
                            patches=(replace(old.patches[0], path=path),))
        self.assertTrue(pr_facts.flagged(prs.facts))
        self.assertNotEqual(S.flags_detail(prs.facts), drawn["flags"])
        self.assertEqual([p.as_dict() for p in pr_facts.facts_parts(prs.facts)], drawn["facts"])
        self.assertEqual(list(pr_facts.decide(prs.facts).actions), drawn["actions"])
        self.reject_unseen(prs, drawn, "merge", "fresh unseen safety warning accepted Merge OK")

    def test_fresh_action_explanation_change_before_poll(self):
        prs = self.prs()
        prs.poll()
        old = prs.summaries.facts_for(PM.REPO, PM.N)
        check = pr_facts.Check("unit tests", "failing", False, "github-actions")
        prs.facts = replace(old, fork=True, merge_state="UNSTABLE", checks="failing", failing=(check,))
        prs.set_view(isCrossRepository=True, mergeStateStatus="UNSTABLE",
                     statusCheckRollup=PM.PF.failing_rollup("unit tests"))
        prs.poll()
        prs.summaries._on_facts(prs.facts)
        PM.eventually(lambda: prs.detail()["summary_state"] == S.READY)
        drawn = copy.deepcopy(prs.detail())
        self.assertEqual(drawn["why"], "checks fail; from a fork: no Fix or Update")
        prs.facts = replace(prs.facts, failing=(replace(check, scanner=None, app=None),))
        fresh = pr_facts.decide(prs.facts)
        self.assertIn("could not be read in full", fresh.why)
        self.assertNotEqual(fresh.why, drawn["why"])
        self.assertEqual(list(fresh.actions), drawn["actions"])
        self.assertEqual([p.as_dict() for p in pr_facts.facts_parts(prs.facts)], drawn["facts"])
        self.reject_unseen(prs, drawn, "merge_as_is", "fresh unseen why accepted Merge as is OK")

    def test_fresh_title_change_before_poll(self):
        prs = self.prs()
        drawn = copy.deepcopy(prs.poll())
        # The gh view read by the actor changes; the list poll has not run.
        prs.set_view(title="Remove backups after the upload")
        fresh = prs.read(PM.REPO, PM.N)
        self.assertNotEqual(fresh.title, drawn["title"])
        self.assertEqual([p.as_dict() for p in pr_facts.facts_parts(fresh)], drawn["facts"])
        self.reject_unseen(prs, drawn, "merge", "fresh unseen title accepted Merge OK")


if __name__ == "__main__":
    unittest.main()
