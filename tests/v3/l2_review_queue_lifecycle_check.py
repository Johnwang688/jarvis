"""Reviewer durability and ingestion checks using local files and a fake model."""

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from jarvis import config
from jarvis.v3 import review_queue, reviewer
from jarvis.v3.budget import Budget
from jarvis.v3.claude_jobs import JobResult
from jarvis.v3.index import Index
from jarvis.v3.ingest.transcripts import ingest


class Crash(BaseException):
    pass


class ReviewerLifecycle(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.data = self.root / "v3"
        self.enterContext(patch.object(config, "V3_DIR", self.data))
        self.projects = self.root / ".claude/projects"
        self.projects.mkdir(parents=True)
        self.transcript = self.projects / "example.jsonl"
        self.queue = self.data / "review-queue"
        self.queue.mkdir(mode=0o700, parents=True)
        self.index = self.enterContext(Index(self.data / "index.sqlite"))
        self.now = 1_895_000_000
        self.run_job = Mock(return_value=JobResult("reviewer", "haiku", ok=True, outcome="ok"))

    def append_turns(self, start, end):
        with self.transcript.open("a", encoding="utf-8") as stream:
            for number in range(start, end):
                row = {"type": "user", "cwd": str(self.root), "message": {
                    "role": "user", "content": f"Fixture turn {number}."}}
                stream.write(json.dumps(row) + "\n")

    def enqueue(self):
        notice = {"provider": "claude", "session_id": "example",
                  "transcript": str(self.transcript), "cwd": str(self.root),
                  "ended_at": datetime.fromtimestamp(self.now, timezone.utc).isoformat()}
        (self.queue / "notice.json").write_text(json.dumps(notice), encoding="utf-8")

    def ingest(self):
        ingest(self.index, claude=self.projects, codex=self.root / "no-codex", jarvis=self.root / "no-jarvis")

    def budget(self, cap=40):
        return Budget(self.data / "budget.jsonl", caps={"reviewer": cap}, now=lambda: self.now,
                      usage=lambda: [{"used_percent": 10, "resets_at": self.now + 3600}])

    def tick(self, budget=None):
        consumer = review_queue.Consumer(self.queue, self.data / "review-state",
                                         clock=lambda: self.now, claude_projects=self.projects)
        runner = reviewer.Reviewer(consumer=consumer, budget=self.budget() if budget is None else budget,
                                   index=self.index, run_job=self.run_job, clock=lambda: self.now)
        return [row["outcome"] for row in runner.tick()]

    def restart_and_tick(self):
        self.now += 300
        # A real restart keeps the journal and loses only process-local receipts.
        with patch.object(review_queue, "_returned", set()):
            return self.tick()

    def test_success_is_not_replayed_on_every_restart(self):
        self.append_turns(0, 10)
        self.ingest()
        self.enqueue()
        self.assertEqual(self.restart_and_tick(), ["ok"])
        self.assertEqual([self.restart_and_tick() for _ in range(3)], [[], [], []])
        self.run_job.assert_called_once()

    def test_failed_review_cannot_restart_its_attempt_count(self):
        self.append_turns(0, 10)
        self.ingest()
        self.enqueue()
        self.run_job.return_value = JobResult("reviewer", "haiku")
        outcomes = [self.restart_and_tick() for _ in range(5)]
        self.assertEqual(outcomes, [["error"], ["error"], ["error"], [], []])

    def test_appending_turns_preserves_pending_attempts_and_receipt(self):
        self.append_turns(0, 10)
        self.ingest()
        self.enqueue()
        self.run_job.return_value = JobResult("reviewer", "haiku")
        received = self.now
        self.assertEqual(self.tick(), ["error"])

        self.now += 300
        self.append_turns(10, 20)
        self.ingest()
        self.enqueue()
        self.assertEqual(self.restart_and_tick(), ["error"])
        backlog = json.loads((self.data / "reviewer/backlog.json").read_text())
        row, = backlog["pending"]
        self.assertEqual((row["attempts"], row["received"], row["review"]["user_turns"]), (2, received, 20))

    def test_regrown_transcript_can_be_reviewed_after_prior_completion(self):
        self.append_turns(0, 10)
        self.ingest()
        self.enqueue()
        self.assertEqual(self.tick(), ["ok"])

        self.transcript.write_text("", encoding="utf-8")
        self.append_turns(10, 20)
        self.ingest()
        self.enqueue()
        self.assertEqual(self.restart_and_tick(), ["ok"])
        self.assertEqual(self.restart_and_tick(), [])
        self.assertEqual(self.run_job.call_count, 2)

    def test_model_spend_survives_crash_before_result_recording(self):
        self.append_turns(0, 10)
        self.ingest()
        self.enqueue()
        budget = self.budget(cap=1)
        with patch.object(budget, "record", side_effect=Crash), self.assertRaises(Crash):
            self.tick(budget)
        self.run_job.assert_called_once()

        with patch.object(review_queue, "_returned", set()):
            self.assertEqual(self.tick(self.budget(cap=1)), ["skipped-budget"])
        self.run_job.assert_called_once()

    def test_replayed_handoff_keeps_its_original_age(self):
        self.append_turns(0, 10)
        self.ingest()
        self.enqueue()
        with patch.object(reviewer, "_atomic_write", side_effect=Crash), self.assertRaises(Crash):
            self.tick()
        self.run_job.assert_not_called()

        self.now += 7 * 86_400
        outcomes = self.restart_and_tick()
        self.assertIn(outcomes, ([], ["expired"]))
        self.run_job.assert_not_called()

    def test_existing_indexed_prefix_does_not_make_new_turns_ready(self):
        self.append_turns(0, 9)
        self.ingest()
        self.append_turns(9, 10)
        self.enqueue()
        before = self.tick()
        self.ingest()
        after = self.tick()
        self.assertEqual((before, after), ([], ["ok"]))
        self.run_job.assert_called_once()


if __name__ == "__main__":
    unittest.main()
