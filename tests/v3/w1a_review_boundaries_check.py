import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from jarvis import config
from jarvis.v3 import t3_client, work


class WorkBoundaries(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        home = root / "home"
        home.mkdir()
        self.enterContext(patch.dict(os.environ, {"HOME": str(home), "JARVIS_T3_BIN": str(root / "fake-t3")}))
        state = root / "state"
        self.enterContext(patch.object(config, "V3_DIR", state))
        self.ledger = work.Ledger(state / "work" / "threads.jsonl")
        self.registry = work.Registry(state / "work" / "projects.json")
        self.t3 = Mock(spec=t3_client.T3)
        self.t3.start.return_value = t3_client.Run("thread-1", "run-1", "running")
        self.service = work.WorkService(t3=self.t3, clock=lambda: 1, token=lambda _: "a1b2")
        self.project = root / "project"
        self.project.mkdir()
        self.registry.add("demo", self.project, provider="codex", model="fake-model", base="main")

    def plan(self):
        return self.service.plan_start("demo", "Fix example", "fake private brief")

    def test_t3_response_text_in_identifier_fields_stays_private(self):
        marker = "fake private brief"
        plan = self.plan()
        t3 = t3_client.T3()
        service = work.WorkService(t3=t3, clock=lambda: 1)
        for field in ("threadId", "runId"):
            with self.subTest(field=field):
                self.ledger.path.unlink(missing_ok=True)
                response = {"threadId": "thread-1", "runId": "run-1", "status": "running", field: marker}
                with patch.object(t3, "_invoke", return_value=(0, json.dumps(response).encode())):
                    try:
                        rendered = repr(service.start(plan))
                    except work.WorkError as exc:
                        rendered = str(exc)
                if self.ledger.path.exists():
                    rendered += self.ledger.path.read_text()
                self.assertNotIn(marker, rendered)

    def test_ledger_write_error_does_not_echo_t3_response_text(self):
        marker = "fake private brief"
        self.t3.start.return_value = t3_client.Run(marker, "run-1", "running")
        plan = self.plan()
        with patch.object(work.os, "write", side_effect=OSError("fake disk failure")):
            with self.assertRaises(work.WorkError) as caught:
                self.service.start(plan)
        self.assertNotIn(marker, str(caught.exception))

    def test_duplicate_t3_id_cannot_return_a_conflicting_thread(self):
        self.service.start(self.plan())
        self.registry.add("other", self.project, provider="codex", model="fake-model")
        plan = self.service.plan_start("other", "Fix other", "fake brief")
        try:
            thread = self.service.start(plan)
        except work.WorkError:
            return
        self.assertEqual(thread, self.ledger.get(thread.thread_id))



if __name__ == "__main__":
    unittest.main()
