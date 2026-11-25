"""An owner request must finish while another writer holds the skill store."""

from concurrent.futures import ThreadPoolExecutor, wait
import http.client
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from jarvis.v3 import owner_routes
from jarvis.v3.daemon import Daemon
from jarvis.v3.skill_store import SkillStore
from jarvis.v3.tools.skills import SkillTools


class OwnerContentionCheck(unittest.TestCase):
    def test_pin_does_not_wait_on_a_writer_that_arrives_after_lookup(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.dict("os.environ", {"HOME": str(root)}))
        store = SkillStore(root / "skills", ledger=root / "skill-ledger")
        skills = SkillTools(store, session="codex:fixture")
        self.assertIn("Skill change saved.", skills.skill_manage(
            "create", "sort-notes", description="Sort notes.", body="# Notes\n\nExample notes.\n",
        ))
        before = store.records()
        rid = before[-1]["id"]
        skill = store.skill_dir("sort-notes") / "SKILL.md"
        before_text = skill.read_bytes()
        daemon = Daemon(port=0, data_dir=root, jobs=[])
        daemon.start()
        self.addCleanup(daemon.stop)

        def pin():
            conn = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=5)
            try:
                conn.request("POST", "/v3/owner/learned/pin", json.dumps({"id": rid, "on": True}),
                             {"Origin": f"http://127.0.0.1:{daemon.port}", "Content-Type": "application/json"})
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()

        looked_up, writer_locked = Event(), Event()
        lookup = owner_routes._learned_target

        def lookup_then_wait(*args, **kwargs):
            result = lookup(*args, **kwargs)
            looked_up.set()
            if not writer_locked.wait(3):
                raise AssertionError("The competing writer never acquired the store lock.")
            return result

        with ThreadPoolExecutor(max_workers=1) as worker, patch.object(
            owner_routes, "_learned_target", side_effect=lookup_then_wait,
        ):
            request = worker.submit(pin)
            self.assertTrue(looked_up.wait(3))
            with store._operation(initialize=False):
                writer_locked.set()
                done, _ = wait([request], timeout=2.5)
                self.assertIn(request, done, "The owner request waits for a competing writer.")
                self.assertEqual(request.result(), (503, {"error": "Service Unavailable"}))
        self.assertEqual(store.records(), before)
        self.assertEqual(skill.read_bytes(), before_text)


if __name__ == "__main__":
    unittest.main()
