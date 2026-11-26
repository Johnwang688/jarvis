"""Review probe: a phone message sent while the running turn waits on an approval never steers it."""

import json
import time
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v3.policy import set_auto
from a6b1_acceptance_check import Model, reply
import phone_auth_check as auth_check
import phone_routes_check as base


ADD = {"folder": "facts", "name": "fixture-note", "description": "Fixture note",
       "body": "Fixture body text.", "source": "owner chat"}


class PhoneSteerReview(unittest.TestCase):
    setUp = base.PhoneRoutesCheck.setUp
    start = base.PhoneRoutesCheck.start
    api = base.PhoneRoutesCheck.api
    window = base.PhoneRoutesCheck.window
    call = auth_check.PhoneAuthCheck.call
    ok = auth_check.PhoneAuthCheck.ok
    pair = auth_check.PhoneAuthCheck.pair
    sign_in = auth_check.PhoneAuthCheck.sign_in
    unlock = auth_check.PhoneAuthCheck.unlock
    me = auth_check.PhoneAuthCheck.me

    def wait(self, predicate, what):
        deadline = time.monotonic() + 10
        while not predicate():
            self.assertLess(time.monotonic(), deadline, what)
            time.sleep(0.01)

    def test_phone_send_during_pending_approval_starts_the_next_turn(self):
        policy_file = self.data / "policy.json"
        self.enterContext(patch.object(config, "V3_POLICY_FILE", policy_file))
        set_auto(policy_file, True)
        model = Model(reply(calls=[("vault_add", ADD)]), reply("Fixture saved reply"),
                      reply(calls=[("vault_add", dict(ADD, name="second-fixture"))]),
                      reply("Fixture following reply"))
        self.start(model)
        self.assertEqual(self.api("chat/send", body={"text": "Fixture first turn"}),
                         (202, {"state": "running", "steered": False}))
        self.wait(lambda: self.broker.pending(), "No approval card appeared")
        self.assertEqual(self.api("chat/send", body={"text": "Fixture phone text"}),
                         (202, {"state": "running", "steered": False}))
        (card,) = self.api("approvals", method="GET")[1]["approvals"]
        self.assertEqual((card["level"], card["why"]), ("L0", "phone"))
        self.unlock(self.cookie)
        self.assertEqual(self.api("approvals/answer",
                                  body={"id": card["id"], "digest": card["digest"], "decision": "approve"}),
                         (200, {"id": card["id"], "state": "approved"}))
        self.wait(lambda: any(row["id"] != card["id"] for row in self.broker.pending()),
                  "The queued phone action did not ask")
        (queued,) = self.api("approvals", method="GET")[1]["approvals"]
        self.assertEqual((queued["level"], queued["why"]), ("L0", "phone"))
        self.assertEqual(self.api("approvals/answer",
                                  body={"id": queued["id"], "digest": queued["digest"], "decision": "deny"}),
                         (200, {"id": queued["id"], "state": "denied"}))
        self.wait(lambda: self.chat.snapshot()["state"] == "idle" and len(model.seen) == 4,
                  "Chat did not finish both turns")
        self.assertNotIn("Fixture phone text", json.dumps(model.seen[:2]))
        self.assertIn({"role": "user", "content": "Fixture phone text"}, model.seen[2]["messages"])


if __name__ == "__main__":
    unittest.main()
