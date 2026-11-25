from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jarvis.v3 import review_queue


TOOL = b'{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","name":"Read"}]}}\n'
NOW = datetime.fromisoformat("2030-01-01T00:01:00Z").timestamp()


class ConsumerReview(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}))
        self.projects = self.home / ".claude/projects/example-project"
        self.projects.mkdir(parents=True)
        self.queue = self.home / "review-queue"
        self.queue.mkdir(mode=0o700)
        self.state = self.home / "review-state"
        self.transcript = self.projects / "example-session.jsonl"
        self.transcript.write_bytes(TOOL * 10)
        self.item = 0

    def enqueue(self, path, session=None):
        self.item += 1
        (self.queue / f"{self.item}.json").write_text(json.dumps({
            "provider": "claude", "session_id": session or path.stem,
            "transcript": str(path), "cwd": str(self.home),
            "ended_at": f"2030-01-01T00:00:{self.item:02d}Z",
        }))

    def consumer(self, **kwargs):
        return review_queue.Consumer(self.queue, self.state, clock=lambda: NOW, **kwargs)

    def test_outside_transcript_is_discarded_before_counting(self):
        outside = self.home / "unrelated" / self.transcript.name
        outside.parent.mkdir()
        outside.write_bytes(TOOL * 10)
        self.enqueue(outside)
        self.enqueue(self.transcript)
        counted = []

        def count(provider, path):
            counted.append(path)
            return review_queue.count(provider, path)

        reviews = self.consumer(count=count).tick()

        self.assertEqual(counted, [self.transcript])
        self.assertEqual([r["transcript"] for r in reviews], [str(self.transcript)])

    def test_rotated_transcript_gets_a_fresh_count_baseline(self):
        self.enqueue(self.transcript)
        self.assertEqual([r["tool_steps"] for r in self.consumer().tick()], [10])
        self.assertEqual(self.consumer().tick(), [])

        self.transcript.rename(self.transcript.with_suffix(".previous"))
        self.transcript.write_bytes(TOOL.replace(b'"Read"', b'"Grep"') * 10)
        self.enqueue(self.transcript)

        self.assertEqual([r["tool_steps"] for r in self.consumer().tick()], [10])

    @unittest.skipIf(os.geteuid() == 0, "root can read files with mode 000")
    def test_unreadable_transcript_does_not_block_other_sessions(self):
        unreadable = self.projects / "unreadable.jsonl"
        unreadable.write_bytes(TOOL)
        unreadable.chmod(0)
        self.enqueue(unreadable)
        self.enqueue(self.transcript)

        reviews = self.consumer().tick()

        self.assertEqual([r["session_id"] for r in reviews], [self.transcript.stem])

if __name__ == "__main__":
    unittest.main()
