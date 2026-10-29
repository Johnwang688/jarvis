"""#340 follow-up: the tap-time re-check when the row already cut its warnings.

The row lists at most 20 flagged safety files and counts the rest in `more`.
A fresh read whose first 20 are the same but whose total is higher has a
warning the owner never saw; one with the same total is unchanged.
Real source and facts reader; fake gh/model. No live ports.
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
from jarvis.v2 import pr_merge, pr_summary as S

setUpModule = PM.setUpModule
tearDownModule = PM.tearDownModule


class CutRow(PM.Case):
    def files(self, paths):
        return [{"path": p, "additions": 1, "deletions": 1} for p in paths]

    def drawn_with(self, paths):
        view = PM.PF.view(files=self.files(paths), changedFiles=len(paths),
                          additions=len(paths), deletions=len(paths))
        run = PM.GhRun(view=json.dumps(view), diff=PM.PF.diff_of(*[(p, "-x\n+y") for p in paths]))
        prs = self.prs(view=view, run=run)
        return prs, copy.deepcopy(prs.poll())

    def move_base(self, prs, paths):
        prs.set_view(files=self.files(paths), changedFiles=len(paths), additions=len(paths),
                     deletions=len(paths), baseRefOid="a" * 40)
        prs.run.answers["diff"] = PM.PF.diff_of(*[(p, "-x\n+y") for p in paths])

    def test_a_higher_total_behind_a_cut_row_is_a_change(self):
        paths = [f".github/workflows/check-{i:02}.yml" for i in range(24)] + ["app.txt"]
        prs, drawn = self.drawn_with(paths)
        self.assertEqual((len(drawn["flags"]["items"]), drawn["flags"]["more"]), (20, 4))
        # Same file count and totals (the card's facts do not move): one more
        # safety path, after the first 20.
        self.move_base(prs, paths[:-1] + [".github/workflows/zz-new.yml"])
        fresh = prs.read(PM.REPO, PM.N, budget=pr_merge.READ_BUDGET_S)
        self.assertEqual(S.flags_detail(fresh)["items"], drawn["flags"]["items"])
        self.assertEqual(S.flags_detail(fresh)["more"], 5)
        with self.assertRaises(pr_merge.Refused) as caught:
            prs.act("merge", head=drawn["head"], card=drawn["card"])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(prs.run.acts(), [])

    def test_the_same_total_behind_a_cut_row_is_unchanged(self):
        paths = [f".github/workflows/check-{i:02}.yml" for i in range(25)]
        prs, drawn = self.drawn_with(paths)
        self.move_base(prs, paths)
        fresh = prs.read(PM.REPO, PM.N, budget=pr_merge.READ_BUDGET_S)
        decision = prs.actions.judge(fresh, "merge", drawn["head"], drawn["card"],
                                     PM.ROW, via="hud", limited=False)
        self.assertIn("merge", decision.actions)


if __name__ == "__main__":
    unittest.main()
