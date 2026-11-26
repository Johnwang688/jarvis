"""Exercise fallback and delivery windows with no model or T3."""

from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from jarvis.v3 import daily_briefs, learning_digest
from jarvis.v3.feed import Feed
from jarvis.v3.messages import MessageService
from jarvis.v3.skill_store import SkillStore
from jarvis.v3.vault import Vault

NOW = datetime(2030, 1, 2, 22, 59, 59, tzinfo=ZoneInfo("America/Chicago"))


class DeliveryCheck(unittest.TestCase):
    def setup_digest(self, kind, messages):
        root = Path(self.enterContext(TemporaryDirectory()))
        feed = Feed(root / "feed", clock=lambda: NOW)
        state = root / "state"
        runner = Mock()
        if kind == "morning":
            budget = Mock()
            budget.admit.return_value.ok = False

            def run(now=None):
                return daily_briefs.run(
                    "morning", feed=feed, state_dir=state, now=now, budget=budget,
                    calendar=lambda *_: [{"id": "fixture-event", "title": "Lab", "start": NOW}],
                    run_job=runner, messages=messages,
                )
        else:
            store = SkillStore(root / "skills", ledger=root / "ledger")
            vault = Vault(root / "vault")

            def run(now=None):
                return learning_digest.run(
                    feed=feed, state_dir=state, now=now, store=store, vault=vault,
                    reviewer_dir=root / "reviewer", messages=messages,
                )

            self.assertEqual(run((NOW - timedelta(days=1)).timestamp()).outcome, "baseline")
            vault.add("facts", "sample-fact", "Fixture description.", "Synthetic note.", "owner")
        self.addCleanup(runner.assert_not_called)
        return run, feed, state / "state.json"

    def check_fallback(self, run, feed, state, kind):
        now = NOW.timestamp()
        self.assertEqual(run(now).outcome, "posted")
        self.assertEqual(run(now).outcome, "already")
        self.assertEqual(len(feed.briefs()), 1)
        saved = json.loads(state.read_text())
        self.assertEqual(saved["morning" if kind == "morning" else "day"], "2030-01-02")
        if kind == "learning":
            self.assertEqual(saved["memory"], ["facts/sample-fact"])
            self.assertEqual(saved["last_brief"]["id"], feed.briefs()[0]["id"])

    def test_send_exception_posts_one_card(self):
        for kind in ("morning", "learning"):
            with self.subTest(kind=kind):
                messages = Mock()
                messages.send.side_effect = RuntimeError("Fixture send failure")
                run, feed, state = self.setup_digest(kind, messages)
                self.check_fallback(run, feed, state, kind)
                messages.send.assert_called_once()

    def test_builder_exception_posts_one_card(self):
        for kind, module in (("morning", daily_briefs), ("learning", learning_digest)):
            with self.subTest(kind=kind):
                messages = Mock()
                run, feed, state = self.setup_digest(kind, messages)
                with patch.object(module, "clean_source", side_effect=RuntimeError("Fixture build failure")):
                    self.check_fallback(run, feed, state, kind)
                messages.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
