"""Verifier counterexample: a fix summary must be bound to its Merge OK.

Run after building hud/dist, with the same isolated HOME and browser cache
as hud_v2_phone_pr_check.py. Uses a real phone, passkey and PR merge actor;
only gh, the model and the fix projection are fake. No live Jarvis ports.

WS-5c (PX-5): the Merge is a gated action. The first tap is on a locked phone
(unlock/options, the passkey, unlock, then the act); the second is a stale
client's (its window was ended at Jarvis, as by Lock now in another tab), so
the act's 423 is followed by one unlock and ONE resend of the same request.
Each time the check waits for the FINAL answer on `/m/api/inbox/pr`, the
stale-card refusal, never the first 423, and holds the drawn head and card and
"nothing merged".
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
        from jarvis.v2.pr_merge import CHANGED

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
            # The phone draws the row with the fix card pending: its card is
            # the card over that detail (the daemon's `enrich` sets it so).
            row["detail"]["card"] = card_key(row["detail"])
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

            drawn = {"id": pmc.ROW, "action": "merge", "head": row["detail"]["head"], "card": drawn_card}

            def final_refusal(before):
                """The act's last answer: a 409 on /m/api/inbox/pr (a 423 is never final)."""
                return any(path == "/m/api/inbox/pr" and status == 409 for path, _b, status in phone.posts[before:])

            def acts_sent(before):
                return [(body, status) for path, body, status in phone.posts[before:] if path == "/m/api/inbox/pr"]

            def final_error(before):
                """The final answer's own words (Opus L6: not any 409, the stale-card refusal)."""
                answers = [a for path, _st, a in phone.answers[before:] if path == "/m/api/inbox/pr"]
                return answers[-1].get("error") if answers and isinstance(answers[-1], dict) else None

            def notice():
                return phone.row(pmc.ROW).locator('[data-testid="m-pr-notice"]').inner_text()

            # 1. A locked phone: Face ID first, then the act, which refuses the unseen card.
            before = len(phone.posts)
            shown.locator('[data-testid="m-pr-act-merge"]').click()
            self.assertTrue(phone.pump(lambda: final_refusal(before)))
            trail = [path for path, _b, _s in phone.posts[before:]]
            self.assertEqual(trail, ["/m/api/unlock/options", "/m/api/unlock", "/m/api/inbox/pr"])
            sent = acts_sent(before)
            self.assertEqual([st for _b, st in sent], [409])
            self.assertEqual({k: v for k, v in sent[0][0].items() if k != "op"}, drawn)
            self.assertFalse(bool(prs.run.acts()), f"unseen fix summary authorized a merge; phone responses: {sent}")
            self.assertEqual(final_error(before), CHANGED)
            self.assertTrue(phone.pump(lambda: notice() == "Changed since you read it."), notice())

            # 2. A stale client: the phone draws the old card again and believes its window open (it is: the
            # unlock above); the daemon's card moves again, unseen; and as the act arrives the window is ended at
            # Jarvis, as Lock now in another tab would. The act is 423, then one unlock and ONE resend.
            phone.show([row])
            shown = phone.row(pmc.ROW)
            self.assertTrue(phone.pump(lambda: shown.locator('[data-testid="m-pr-act-merge"]').count() == 1
                                       and "summarizing…" in shown.locator('[data-testid="m-pr-fix-summary"]').inner_text()))
            with source._lock:
                detail = source._rows[pmc.ROW]["detail"]
                detail["fix"].update(summary=unseen, summary_state="ready", note=f"fixed: {unseen} — merge?")
                detail["card"] = card_key(detail)
            self.assertEqual(phone.page.locator('[data-testid="m-lock-pill"]').get_attribute("data-state"), "unlocked")
            auth = phone.daemon.remote_auth
            locked = []

            def lock_now_elsewhere(method, path):
                if method == "POST" and path == "/m/api/inbox/pr" and not locked:
                    locked.append(True)
                    for digest, srow in list(auth._sessions.items()):
                        auth.lock({"token_sha256": digest, "id": srow["id"]})
            phone.before_forward = lock_now_elsewhere
            before = len(phone.posts)
            shown.locator('[data-testid="m-pr-act-merge"]').click()
            self.assertTrue(phone.pump(lambda: final_refusal(before)),
                            str([(p_, s_, b_.get("error") if isinstance(b_, dict) else b_) for p_, b_, s_ in phone.posts[before:]]))
            trail = [path for path, _b, _s in phone.posts[before:]]
            self.assertEqual(trail, ["/m/api/inbox/pr", "/m/api/unlock/options", "/m/api/unlock", "/m/api/inbox/pr"])
            sent = acts_sent(before)
            self.assertEqual([st for _b, st in sent], [423, 409])
            # The one resend is the same frozen request: same head, card and op token.
            self.assertEqual(sent[1][0], sent[0][0])
            self.assertEqual({k: v for k, v in sent[1][0].items() if k != "op"}, drawn)
            self.assertEqual(final_error(before), CHANGED)
            self.assertTrue(phone.pump(lambda: notice() == "Changed since you read it."), notice())
            phone.page.wait_for_timeout(600)
            self.assertEqual(len(acts_sent(before)), 2, "the stale refusal was sent again")
            self.assertFalse(bool(prs.run.acts()), f"unseen fix summary authorized a merge; phone responses: {sent}")


if __name__ == "__main__":
    unittest.main()
