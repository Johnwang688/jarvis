"""L3 checkpoint regressions with a real feed and synthetic sources."""

from datetime import datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from jarvis.v3 import learning_digest
from jarvis.v3.feed import Feed
from jarvis.v3.skill_store import SkillStore
from jarvis.v3.vault import Vault


class DigestStateCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def digest(self, day):
        now = datetime(2030, 1, day, 21, tzinfo=ZoneInfo("America/Chicago"))
        feed = Feed(self.root / "feed", clock=lambda: now)
        return self.run_digest(feed, now=now.timestamp()), feed

    def run_digest(self, feed, *, now=None):
        return learning_digest.run(
            feed=feed, now=now, state_dir=self.root / "digest",
            store=SkillStore(self.root / "skills", ledger=self.root / "ledger", links=[], owner_roots=[]),
            vault=Vault(self.root / "vault"), reviewer_dir=self.root / "reviewer",
        )

    def add_memory(self, name):
        Vault(self.root / "vault").add("facts", name, "Fixture fact", "Example notes.", "owner")

    def test_sensitive_memory_name_is_withheld_in_feed_and_checkpoint(self):
        self.assertEqual(self.digest(1)[0].outcome, "baseline")
        name = "fixture-private-value-123x"
        self.add_memory(name)
        with patch.dict("os.environ", {"L3_FIXTURE_API_KEY": name}):
            result, feed = self.digest(2)
            self.assertEqual(result.outcome, "posted")
            brief = feed.get(result.brief_id)
            self.assertNotIn(name, json.dumps(brief))
            state_dir = self.root / "digest"
            summary = learning_digest.summary_line(
                now=datetime(2030, 1, 2, 21, tzinfo=ZoneInfo("America/Chicago")).timestamp(),
                state_dir=state_dir,
            )
            self.assertEqual(summary, {"label": "Learning digest: 1 item", "ref": f"brief:{result.brief_id}"})
            self.assertEqual(self.digest(3)[0].outcome, "empty")
        self.assertNotIn(name, (state_dir / "state.json").read_text())

    def test_replaced_sidecar_does_not_skip_its_first_complete_event(self):
        vault = self.root / "vault"
        vault.mkdir(mode=0o700)
        flags = vault / ".flags.jsonl"

        def row(name):
            return json.dumps({"name": "facts/" + name, "rule": "imperative"}) + "\n"

        flags.write_text(row("old-fact"))
        self.assertEqual(self.digest(1)[0].outcome, "baseline")
        flags.rename(vault / ".flags.previous.jsonl")
        flags.write_text(row("new-fact") + row("next-fact"))

        result, feed = self.digest(2)
        self.assertEqual(result.outcome, "posted")
        self.assertEqual([line["ref"] for line in feed.get(result.brief_id)["context"]],
                         ["memory:facts/new-fact", "memory:facts/next-fact"])
        self.assertEqual(self.digest(3)[0].outcome, "empty")

if __name__ == "__main__":
    unittest.main()
