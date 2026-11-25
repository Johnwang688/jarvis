"""I3c details reads through the window and the authenticated phone listener."""

import json
import unittest
from unittest.mock import patch

import phone_routes_check as phone
from phone_listener_check import request

from jarvis import config
from jarvis.v3 import owner_routes, phone_routes
from jarvis.v3.proposals import Draft, ProposalStore, action_sha256


OWNER = "/v3/owner/proposal"
TAINT = "Based on untrusted text"
MARKER = "fixture-argument-hidden-from-card"
ASK = "Start the fixture coding thread?"
PROJECT = "fixture-project"
FOLDER = "/fixture/coding"
AGENT = "fixture-provider / fixture-model"
TITLE = "Inspect the fixture recovery path"
BRIEF = ("Review the fixture state before making changes.\n" * 55
         + "Include the final fixture result.")


def work_draft():
    args = {"project": PROJECT, "title": TITLE, "brief": BRIEF, "worktree": True, "hidden": MARKER}
    return Draft(
        kind="thread.start", level_proposed="L1", basis=[{"type": "thread", "id": "fixture-thread"}],
        brief={"ask": ASK, "rank": 3, "why_now": "The fixture is ready.",
               "context": [{"label": "Fixture thread", "ref": "jarvis:thread:fixture-thread"}]},
        action={"tool": "work_start", "args": args,
                "preview": {"project": PROJECT, "title": TITLE, "brief": BRIEF, "path": FOLDER,
                            "provider": "fixture-provider", "model": "fixture-model",
                            "base": "main", "branch": "fixture-branch"}},
        dedupe_key="thread.start:fixture-project:fixture-thread", project=PROJECT, tainted=True,
    )


class ProposalRouteCheck(unittest.TestCase):
    # Reuse the real RemoteAuth session fixture without inheriting its tests.
    call = phone.PhoneRoutesCheck.call
    ok = phone.PhoneRoutesCheck.ok
    pair = phone.PhoneRoutesCheck.pair
    sign_in = phone.PhoneRoutesCheck.sign_in
    unlock = phone.PhoneRoutesCheck.unlock
    me = phone.PhoneRoutesCheck.me
    api = phone.PhoneRoutesCheck.api
    window = phone.PhoneRoutesCheck.window

    def setUp(self):
        phone.PhoneRoutesCheck.setUp(self)
        self.enterContext(patch.object(config, "V3_POLICY_FILE", self.data / "policy.json"))
        self.store = ProposalStore(self.data / "fixture-proposals")
        phone.PhoneRoutesCheck.start(self, proposals=lambda: self.store)

    def post(self, draft=None):
        proposal = self.store.add(work_draft() if draft is None else draft)
        self.assertIsNotNone(proposal)
        return proposal, self.store.brief(proposal["id"], self.feed)

    def details(self, bid):
        return self.window("POST", OWNER, body={"id": bid})

    def test_card_has_the_exact_projection_and_full_work_start_rows(self):
        proposal, bid = self.post()
        status, payload = self.details(bid)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"proposal": {
            "kind": "thread.start", "level": "L2", "why": proposal["why"], "tainted": True,
            "basis": [{"type": "thread", "id": "fixture-thread"}], "state": "briefed",
            "action": {"tool": "work_start", "card": {
                "title": "Start a coding thread",
                "rows": [{"label": label, "value": value} for label, value in (
                    ("Project", PROJECT), ("Folder", FOLDER), ("Agent", AGENT),
                    ("Runs in", "A new worktree from main on branch fixture-branch"),
                    ("Thread title", TITLE), ("Brief", BRIEF),
                )],
            }},
        }})
        self.assertNotIn(MARKER, json.dumps(payload))
        self.assertEqual(self.feed.get(bid)["context"][0], {"label": TAINT, "ref": "jarvis:tainted"})

    def test_exact_post_only_route_checks_the_owner_before_lookup(self):
        self.assertEqual([key for key in owner_routes.ROUTES if key[1] == OWNER], [("POST", OWNER)])
        self.assertTrue(owner_routes.ROUTES["POST", OWNER].owner)
        host = f"127.0.0.1:{self.daemon.port}"
        with patch.object(self.feed, "get", wraps=self.feed.get) as lookup:
            status, _, _ = request(self.daemon.port, "POST", OWNER, host=host, body={"id": "a" * 32})
            self.assertEqual(status, 403)
            for method in ("GET", "HEAD", "PUT"):
                status, headers, _ = request(self.daemon.port, method, OWNER, host=host,
                                             headers={"Sec-Fetch-Site": "same-origin"})
                self.assertIn(status, (404, 405))
                if status == 405:
                    self.assertEqual(headers["Allow"], "POST")
            lookup.assert_not_called()
        self.assertEqual(self.api("proposal", method="GET")[0], 404)

    def test_unknown_and_unlinked_briefs_and_invalid_bodies(self):
        self.assertEqual(self.details("a" * 32)[0], 404)
        brief = self.feed.post({"kind": "news", "ask": "Fixture news", "rank": 5})
        self.assertEqual(self.details(brief["id"])[0], 404)
        for body in ({}, {"id": 1}, {"id": brief["id"], "extra": MARKER}):
            with self.subTest(body=body):
                self.assertEqual(self.window("POST", OWNER, body=body), (400, {"error": "Bad Request"}))

    def test_changed_action_and_wrong_brief_are_conflicts(self):
        proposal, bid = self.post()
        reference = self.feed.get(bid)["proposal_id"]
        wrong = self.feed.post({"kind": "news", "ask": "A different fixture brief",
                                "rank": 5, "proposal_id": reference})
        self.assertEqual(self.details(wrong["id"]), (409, {"error": "Conflict"}))
        rows = [json.loads(line) for line in self.store.path.read_text().splitlines()]
        rows[0]["action"]["args"]["hidden"] = "altered-fixture-argument"
        self.assertEqual(rows[0]["action_sha256"], proposal["action_sha256"])
        self.assertNotEqual(action_sha256(rows[0]), proposal["action_sha256"])
        self.store.path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.assertEqual(self.details(bid), (409, {"error": "Conflict"}))

    def test_ask_only_has_a_null_action(self):
        draft = Draft(kind="ask", level_proposed="L1", basis=[],
                      brief={"ask": "Review the fixture?", "rank": 5}, action=None,
                      dedupe_key="ask:fixture", project=None, tainted=False)
        proposal, bid = self.post(draft)
        self.assertEqual(self.details(bid), (200, {"proposal": {
            "kind": "ask", "level": proposal["level"], "why": proposal["why"],
            "tainted": False, "basis": [], "state": "briefed", "action": None,
        }}))
        self.assertEqual(self.feed.get(bid)["kind"], "news")

    def test_phone_reads_while_locked_and_does_not_slide_unlock(self):
        _, bid = self.post()
        body = {"id": bid}
        method = phone_routes.PHONE_ROUTES["POST", "/m/api/proposal"]
        self.assertEqual(method.tier(body), "read")
        self.assertIs(method.owner_route, owner_routes.ROUTES["POST", OWNER])
        self.assertEqual(self.call("proposal", body=body)[0], 401)
        self.assertEqual(self.me(self.cookie)["unlock_s"], 0)
        self.assertEqual(self.api("proposal", body=body), self.details(bid))
        status, snapshot = self.api("feed", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["briefs"][0]["proposal_id"], self.feed.get(bid)["proposal_id"])
        self.assertEqual(self.api("feed/answer", body={**body, "action": "approve"}), phone.LOCKED)
        self.assertEqual(self.feed.get(bid)["state"], "open")
        self.assertEqual(self.me(self.cookie)["unlock_s"], 0)
        self.unlock(self.cookie)
        self.clock.now += 123
        self.assertEqual(self.api("proposal", body=body)[0], 200)
        self.assertEqual(self.me(self.cookie)["unlock_s"], 3477)


if __name__ == "__main__":
    unittest.main()
