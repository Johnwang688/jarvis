from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from jarvis.v3 import review_queue


TOOL = b'{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","name":"Read"}]}}'
NOW = datetime.fromisoformat("2030-01-01T00:01:00Z").timestamp()


class ConsumerRoundTwo(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.projects = self.home / ".claude/projects"
        self.projects.mkdir(parents=True)
        self.queue = self.home / "review-queue"
        self.queue.mkdir(mode=0o700)
        self.transcript = self.projects / "example-session.jsonl"
        self.transcript.write_bytes((TOOL + b"\n") * review_queue.SKILL_STEPS)
        self.consumer = review_queue.Consumer(
            self.queue, self.home / "state", clock=lambda: NOW,
            claude_projects=self.projects, codex_sessions=self.home / ".codex/sessions",
        )
        self.sequence = 0

    def enqueue(self, path):
        self.sequence += 1
        (self.queue / f"{self.sequence}.json").write_text(json.dumps({
            "provider": "claude", "session_id": "example-session",
            "transcript": str(path), "cwd": str(self.home),
            "ended_at": f"2030-01-01T00:00:{self.sequence:02d}Z",
        }))

    def test_shorter_in_place_rewrite_due_even_with_same_tool_count(self):
        self.transcript.write_bytes((TOOL + b" " * 100 + b"\n") * review_queue.SKILL_STEPS)
        before = self.transcript.stat()
        self.enqueue(self.transcript)
        self.assertEqual([r["tool_steps"] for r in self.consumer.tick()], [review_queue.SKILL_STEPS])
        self.assertEqual(self.consumer.tick(), [])

        self.transcript.write_bytes((TOOL.replace(b'"Read"', b'"Edit"') + b"\n") * review_queue.SKILL_STEPS)
        after = self.transcript.stat()
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertLess(after.st_size, before.st_size)
        self.enqueue(self.transcript)

        self.assertEqual([r["tool_steps"] for r in self.consumer.tick()], [review_queue.SKILL_STEPS])

    def test_older_notification_does_not_discard_due_rotated_transcript(self):
        self.enqueue(self.transcript)
        self.assertEqual([r["tool_steps"] for r in self.consumer.tick()], [review_queue.SKILL_STEPS])
        self.assertEqual(self.consumer.tick(), [])

        replacement = self.projects / "example-session-continued.jsonl"
        replacement.write_bytes((TOOL.replace(b'"Read"', b'"Edit"') + b"\n") * review_queue.SKILL_STEPS)
        self.enqueue(self.transcript)
        self.enqueue(replacement)

        self.assertEqual([r["transcript"] for r in self.consumer.tick()], [str(replacement)])

    def test_late_notification_does_not_review_old_generation_again(self):
        self.enqueue(self.transcript)
        self.assertEqual([r["transcript"] for r in self.consumer.tick()], [str(self.transcript)])
        self.assertEqual(self.consumer.tick(), [])
        replacement = self.projects / "example-session-continued.jsonl"
        replacement.write_bytes((TOOL.replace(b'"Read"', b'"Edit"') + b"\n") * review_queue.SKILL_STEPS)
        self.enqueue(replacement)
        self.assertEqual([r["transcript"] for r in self.consumer.tick()], [str(replacement)])
        self.assertEqual(self.consumer.tick(), [])

        for path in (self.transcript, replacement, self.transcript, replacement):
            with self.subTest(path=path.name):
                self.enqueue(path)
                self.assertEqual(self.consumer.tick(), [])
                self.assertEqual(self.consumer.tick(), [])

    def test_trailing_slash_does_not_turn_a_non_file_reference_into_a_file(self):
        self.enqueue(f"{self.transcript}/")

        self.assertEqual(self.consumer.tick(), [])


if __name__ == "__main__":
    unittest.main()
