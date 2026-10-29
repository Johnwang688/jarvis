"""Final verifier for #340 / 6eb03c1a: fresh-read warning and budget edges.

Real source, summary projection, facts reader and merge actor; fake gh/model.
No product patches or live ports. The defect tests must fail on 6eb03c1a.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pr_merge_check as PM
from jarvis.v2 import inbox_prs, pr_facts, pr_merge, pr_summary as S

setUpModule = PM.setUpModule
tearDownModule = PM.tearDownModule


class FinalReadEdges(PM.Case):
    def prs_with_files(self, paths, *, diff=None, title="Add the thing"):
        view = PM.PF.view(title=title,
                          files=[{"path": p, "additions": 1, "deletions": 1} for p in paths],
                          changedFiles=len(paths), additions=len(paths), deletions=len(paths))
        run = PM.GhRun(view=json.dumps(view), diff=diff if diff is not None else
                       PM.PF.diff_of(*[(p, "-x\n+y") for p in paths]))
        return self.prs(view=view, run=run)

    def judge_unchanged(self, prs, drawn):
        fresh = prs.read(PM.REPO, PM.N, budget=pr_merge.READ_BUDGET_S)
        self.assertEqual(fresh.head, drawn["head"])
        self.assertEqual(prs.detail(), drawn, "the stored row has not changed")
        try:
            return prs.actions.judge(fresh, "merge", drawn["head"], drawn["card"],
                                     PM.ROW, via="hud", limited=False)
        except pr_merge.Refused as exc:
            self.fail(f"unchanged row refused: {exc.status} {exc.word}")

    def test_missing_diff_completeness_alone_must_not_refuse(self):
        # A lock patch contributes zero readable patches in both budgets.
        # The only fresh warning difference is incompleteness, not a path.
        prs = self.prs_with_files(["package-lock.json"])
        drawn = copy.deepcopy(prs.poll())
        self.assertFalse(drawn["flags"]["flagged"])
        prs.run.answers["diff"] = inbox_prs.GhFailed("timeout")
        fresh = prs.read(PM.REPO, PM.N, budget=pr_merge.READ_BUDGET_S)
        self.assertEqual([p.as_dict() for p in pr_facts.facts_parts(fresh)], drawn["facts"])
        self.assertEqual(S.flags_detail(fresh)["items"], drawn["flags"]["items"])
        self.assertTrue(S.flags_detail(fresh)["incomplete"])
        self.judge_unchanged(prs, drawn)

    def test_shorter_read_must_not_rewrite_the_shown_summary_coverage(self):
        # A diff-only warning from a renamed-away CI file was already shown.
        # The fresh read has the identical view, but cannot fetch that diff.
        diff = ("diff --git a/.github/workflows/old.yml b/app.txt\n"
                "similarity index 80%\nrename from .github/workflows/old.yml\n"
                "rename to app.txt\n--- a/.github/workflows/old.yml\n+++ b/app.txt\n"
                "@@ -1 +1 @@\n-x\n+y\n")
        prs = self.prs_with_files(["app.txt"], diff=diff)
        drawn = copy.deepcopy(prs.poll())
        self.assertEqual(drawn["flags"]["items"],
                         [{"name": ".github/workflows/old.yml", "families": [pr_facts.FLAG_CI]}])
        prs.run.answers["diff"] = inbox_prs.GhFailed("timeout")
        fresh = prs.read(PM.REPO, PM.N, budget=pr_merge.READ_BUDGET_S)
        self.assertEqual(S.flags_detail(fresh)["items"], [])
        old_parts = {p["key"]: p for p in drawn["facts"]}
        new_parts = {p.key: p.as_dict() for p in pr_facts.facts_parts(fresh)}
        self.assertNotIn("read", old_parts)
        self.assertEqual(new_parts.pop("read")["text"], "summary read 0 of 1 files")
        self.assertEqual(new_parts, old_parts)
        self.judge_unchanged(prs, drawn)

    def test_new_warning_beyond_twenty_must_invalidate_merge_ok(self):
        paths = [f".github/workflows/check-{i:02}.yml" for i in range(20)] + ["app.txt"]
        prs = self.prs_with_files(paths)
        drawn = copy.deepcopy(prs.poll())
        self.assertEqual((len(drawn["flags"]["items"]), drawn["flags"]["more"]), (20, 0))
        # The base moves on the same branch: same PR head and aggregate
        # counts, but one more changed safety path. No list poll/callback.
        paths[-1] = ".github/workflows/new.yml"
        prs.set_view(files=[{"path": p, "additions": 1, "deletions": 1} for p in paths],
                     changedFiles=21, additions=21, deletions=21, baseRefOid="a" * 40)
        prs.run.answers["diff"] = PM.PF.diff_of(*[(p, "-x\n+y") for p in paths])
        fresh = prs.read(PM.REPO, PM.N, budget=pr_merge.READ_BUDGET_S)
        flags = S.flags_detail(fresh)
        self.assertEqual(flags["items"], drawn["flags"]["items"])
        self.assertEqual(flags["more"], 1)
        self.assertEqual(fresh.head, drawn["head"])
        self.assertEqual([p.as_dict() for p in pr_facts.facts_parts(fresh)], drawn["facts"])
        self.assertEqual(prs.detail(), drawn)
        with self.assertRaises(pr_merge.Refused,
                               msg="unseen 21st safety warning accepted Merge OK") as caught:
            prs.act("merge", head=drawn["head"], card=drawn["card"])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(prs.run.acts(), [])

    def test_cleaned_title_matches_on_both_reads(self):
        raw = "  Fix\tCafe\u0301\u200b \U0001f680 upload  "
        prs = self.prs_with_files(["app.txt"], title=raw)
        drawn = copy.deepcopy(prs.poll())
        self.assertNotEqual(drawn["title"], raw)
        self.assertEqual(drawn["title"], inbox_prs.clean_title(raw))
        self.assertIn("merge", self.judge_unchanged(prs, drawn).actions)

    def test_diff_only_warning_missing_from_short_read_can_still_match(self):
        # A binary rename keeps model coverage at zero in both reads.
        diff = ("diff --git a/.github/workflows/old.yml b/app.bin\n"
                "similarity index 100%\nrename from .github/workflows/old.yml\n"
                "rename to app.bin\nBinary files a/.github/workflows/old.yml and b/app.bin differ\n")
        prs = self.prs_with_files(["app.bin"], diff=diff)
        drawn = copy.deepcopy(prs.poll())
        self.assertEqual(drawn["flags"]["items"][0]["name"], ".github/workflows/old.yml")
        prs.run.answers["diff"] = inbox_prs.GhFailed("timeout")
        self.assertIn("merge", self.judge_unchanged(prs, drawn).actions)


if __name__ == "__main__":
    unittest.main()
