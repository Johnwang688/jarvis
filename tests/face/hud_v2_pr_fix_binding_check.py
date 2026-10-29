"""Verifier counterexample: a fix summary must be bound to its Merge OK.

Run after building hud/dist, with the same isolated HOME and browser cache
as hud_v2_phone_pr_check.py. Uses a real phone, passkey and PR merge actor;
only gh, the model and the fix projection are fake. No live Jarvis ports.
"""
from __future__ import annotations

import copy
from contextlib import ExitStack
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.face.hud_v2_phone_pr_check import Phone, Shim, _load  # noqa: E402
from tests.face.hud_v2_check import until  # noqa: E402


class FixSummaryBinding(unittest.TestCase):
    def test_merge_rejects_fix_summary_that_landed_after_the_phone_drew(self):
        from playwright.sync_api import sync_playwright
        from jarvis.v2.pr_summary import card_key

        with sync_playwright() as pw, ExitStack() as cleanups:
            browser = pw.chromium.launch(headless=True)
            cleanups.callback(browser.close)
            phone = Phone(browser, until)
            cleanups.callback(phone.close)
            phone.sign_in()
            shim = Shim()
            cleanups.callback(shim.close)
            pmc = _load("pr_merge_check", ROOT / "tests/v2/pr_merge_check.py")
            prs = pmc.Prs(shim, phone.daemon)
            prs.poll()
            row = copy.deepcopy(prs.row())
            row["detail"]["fix"] = {
                "state": "ready", "task_id": "t0000037abcdef",
                "from_head": "9" * 40, "commits": 1, "ci_changed": False,
                "note": "fixed — merge?", "summary": None,
                "summary_state": "pending", "count": 1, "max": 3,
            }
            phone.show([row])
            shown = phone.row(pmc.ROW)
            slot = shown.locator('[data-testid="m-pr-fix-summary"]')
            self.assertTrue(phone.pump(lambda: slot.count() == 1))
            self.assertIn("summarizing…", slot.inner_text())
            drawn_card = row["detail"]["card"]
            self.assertEqual(card_key(row["detail"]), drawn_card)

            # The asynchronous interdiff summary lands on the daemon before
            # the phone receives its inbox_changed event. Same head, facts,
            # ordinary PR summary and action. This is PM-E's READY projection.
            unseen = "The fix removes the backup instead of retrying the upload."
            source = phone.daemon.inbox.prs
            with source._lock:
                detail = source._rows[pmc.ROW]["detail"]
                detail["fix"].update(summary=unseen, summary_state="ready",
                                      note=f"fixed: {unseen} — merge?")
                detail["card"] = card_key(detail)
            self.assertNotIn(unseen, slot.inner_text())

            before = len(phone.posts)
            shown.locator('[data-testid="m-pr-act-merge"]').click()
            self.assertTrue(phone.pump(lambda: any(
                path == "/m/api/inbox/pr" or
                (path == "/m/api/inbox/pr/options" and status == 409)
                for path, _body, status in phone.posts[before:]
            )))
            sent = [(path, status) for path, _body, status in phone.posts[before:]
                    if path.startswith("/m/api/inbox/pr")]
            self.assertFalse(
                bool(prs.run.acts()),
                f"unseen fix summary authorized a merge; phone responses: {sent}",
            )


if __name__ == "__main__":
    unittest.main()
