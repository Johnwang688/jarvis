"""Check records accepted at the proposal boundary."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v3.approval_card import build_card
from jarvis.v3.feed import Feed
from jarvis.v3.proposals import Draft, ProposalError, ProposalStore


class RecordChecks(unittest.TestCase):
    def setUp(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        v3 = home / "v3"
        self.enterContext(patch.dict("os.environ", HOME=str(home)))
        self.enterContext(patch.multiple(config, V3_DIR=v3, V3_POLICY_FILE=v3 / "policy.json"))
        self.store = ProposalStore()
        self.feed = Feed(v3 / "feed")
        self.draft = Draft(
            "thread.send", "L0", [], {"rank": 3, "ask": "fake-private-brief"},
            {"tool": "work_send",
             "args": {"thread_id": "fake-thread-a", "message": "fake-private-message"},
             "preview": {"thread_id": "fake-thread-a", "title": "Fake thread", "project": "demo"}},
            "fake-key", "demo", False,
        )

    def test_approval_card_and_action_name_the_same_target(self):
        action = self.draft.action
        preview = {**action["preview"], "thread_id": "fake-thread-b"}
        draft = replace(self.draft, action={**action, "preview": preview})
        try:
            row = self.store.add(draft)
            if row is None:
                return
            self.store.brief(row["id"], self.feed)
        except ProposalError:
            self.assertEqual(self.feed.briefs(), [])
            return
        stored = self.store.get(row["id"])["action"]
        card = build_card(stored["tool"], stored["args"], stored["preview"])
        self.assertIn({"label": "Thread id", "value": stored["args"]["thread_id"]}, card["rows"])

    def test_draft_repr_omits_payload(self):
        representation = repr(self.draft)
        for marker in ("fake-private-brief", "fake-private-message", "fake-thread-a"):
            with self.subTest(marker=marker):
                self.assertFalse(marker in representation, "Draft repr exposed a private field")


if __name__ == "__main__":
    unittest.main()
