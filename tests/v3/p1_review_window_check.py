"""P1 window and budget regressions. All jobs and transcript data are fixtures."""

from datetime import datetime
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from jarvis.v3 import budget as budget_module, consolidate
from jarvis.v3.budget import Budget
from jarvis.v3.claude_jobs import JobResult
from jarvis.v3.index import Index
from jarvis.v3.ingest.transcripts import ingest


DAY = 86_400
NIGHT = datetime(2030, 1, 16, 3, 30, tzinfo=ZoneInfo("America/Chicago")).timestamp()


class WindowReview(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.db = self.root / "index" / "index.sqlite"
        self.state = self.root / "consolidate"
        self.prompts = []

    def budget(self, now, *, cap=2):
        return Budget(self.root / "budget.jsonl", now=lambda: now, daily_cap=cap,
                      usage=lambda: [{"used_percent": 10, "resets_at": now + DAY}])

    def job(self, name, prompt, *, model, **kwargs):
        self.prompts.append(prompt)
        return JobResult(name, model, ok=True, outcome="ok", input_tokens=200)

    def night(self, now=NIGHT, *, budget=None):
        with Index(self.db) as index:
            return consolidate.run(index=index, budget=budget or self.budget(now),
                                   now=now, state_dir=self.state, run_job=self.job)

    @staticmethod
    def add(index, title, when):
        index.add("gmail", title, title=title, ts=when, meta={"from_owner": 1},
                  messages=[("owner", "A synthetic project note.", when)])

    def test_owner_overflow_reaches_a_later_night(self):
        """HIGH: the oldest owner document is permanently skipped after a capped success."""
        with Index(self.db) as index:
            self.add(index, "Overflow fixture", NIGHT - 1000)
            for n in range(consolidate.MAX_DOCS):
                self.add(index, f"Newer fixture {n}", NIGHT - n)
        first = self.night()
        self.assertEqual((first.outcome, first.docs, first.left_out),
                         ("ok", consolidate.MAX_DOCS, 1))
        self.assertNotIn("Overflow fixture", self.prompts[0])
        self.night(NIGHT + DAY)
        self.assertEqual(len(self.prompts), 2, "the omitted document never reached a job")
        self.assertIn("Overflow fixture", self.prompts[1])
        self.assertNotIn("Newer fixture", self.prompts[1])

    def test_late_transcript_on_an_existing_index_reaches_a_later_night(self):
        """HIGH: an indexed owner message can be lost if ingest trails consolidation."""
        previous_index = Index
        if filename := os.environ.get("P1_ORIGIN_INDEX"):
            original = ModuleType("p1_origin_index")
            path = Path(filename)
            exec(compile(path.read_bytes(), str(path), "exec"), original.__dict__)
            previous_index = original.Index

        roots = {name: self.root / name for name in ("claude", "codex", "jarvis")}
        roots["claude"].mkdir()
        transcript = roots["claude"] / "Delayed transcript fixture.jsonl"

        def append(body, timestamp):
            with transcript.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": "user", "timestamp": timestamp,
                                         "message": {"role": "user", "content": body}}) + "\n")

        append("First synthetic message.", NIGHT - 60)
        with previous_index(self.db) as index:
            self.assertEqual(ingest(index, **roots).chunks, 1)
            self.add(index, "Inclusive upper bound fixture", NIGHT)
        with Index(self.db) as index:
            rows = index.documents_between(since=NIGHT - 60, until=NIGHT)
            self.assertEqual([row["title"] for row in rows], ["Inclusive upper bound fixture"])
            self.assertEqual(ingest(index, **roots).chunks, 0)
        append("Late indexed synthetic message.", NIGHT - 1)
        self.assertEqual(self.night().outcome, "ok")

        with Index(self.db) as index:
            self.assertEqual(ingest(index, **roots).chunks, 1)
            self.assertEqual(index.discover("synthetic message")[0]["ts"], NIGHT - 1)
        self.night(NIGHT + DAY)
        self.assertEqual(len(self.prompts), 2, "the newly indexed message never reached a job")
        self.assertIn(transcript.name, self.prompts[1])

    def test_documents_created_after_clock_correction_survive(self):
        """MED: a wall-clock correction can backdate new documents behind the checkpoint."""
        with Index(self.db) as index:
            self.add(index, "Before correction fixture", NIGHT + 299)
        self.assertEqual(self.night(NIGHT + 300).outcome, "ok")
        with Index(self.db) as index:
            self.add(index, "After correction fixture", NIGHT + 1)
        self.night(NIGHT + DAY)
        self.assertEqual(len(self.prompts), 2, "the corrected timestamp was never reconsidered")
        self.assertIn("After correction fixture", self.prompts[1])

    def test_failure_before_runner_does_not_spend_budget(self):
        """LOW: a job setup error takes a budget slot before the runner is called."""
        with Index(self.db) as index:
            self.add(index, "Setup fixture", NIGHT)
        budget = self.budget(NIGHT)
        with (patch.dict(os.environ, {"JARVIS_VAULT": "~fake-vault-user/vault"}),
              patch("pwd.getpwnam", side_effect=KeyError("fake-vault-user"))):
            self.assertEqual(self.night(budget=budget).outcome, "error")
        self.assertEqual(self.prompts, [])
        self.assertEqual(budget.used()["total"], 0)

    def test_transient_budget_record_failure_cannot_allow_an_extra_job(self):
        """MED: a job that consumed tokens does not count if the ledger write fails."""
        with Index(self.db) as index:
            self.add(index, "Budget fixture", NIGHT)
        with patch.object(budget_module, "_append", side_effect=OSError("Fixture ledger error")):
            self.assertEqual(self.night(budget=self.budget(NIGHT, cap=1)).outcome, "error")
        self.assertEqual(len(self.prompts), 0)
        self.night(budget=self.budget(NIGHT, cap=1))
        self.assertEqual(len(self.prompts), 1, "retry did not use the available slot")


if __name__ == "__main__":
    unittest.main()
