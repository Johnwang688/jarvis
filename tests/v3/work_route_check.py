"""The owner work read against fake providers and isolated work files."""

import http.client
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from jarvis.v3.daemon import Daemon
from jarvis.v3.work import Ledger, Project, Registry, Thread, WorkError


WORK = "/v3/owner/work"
UNAVAILABLE = {"error": "Service Unavailable"}


class WorkRoute(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def start(self, name="v3", *, work=None):
        daemon = Daemon(port=0, data_dir=self.root / name, hud_dir=self.root / "hud", jobs=[], work=work)
        daemon.start()
        self.addCleanup(daemon.stop)
        return daemon.port

    def request(self, port, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request(method, WORK, headers={"Sec-Fetch-Site": "same-origin"})
            response = conn.getresponse()
            return response.status, response.headers, response.read()
        finally:
            conn.close()

    def read(self, port):
        status, _, raw = self.request(port)
        return status, json.loads(raw)

    def seed(self, data_dir, project, thread_id):
        folder = self.root / f"checkout-{project}"
        folder.mkdir()
        root = self.root / data_dir / "work"
        registry = Registry(root / "projects.json")
        registry.add(project, folder, provider="fixture-provider", model="fixture-model")
        Ledger(root / "threads.jsonl").record_start(
            thread_id=thread_id, run_id="fixture-run", project=project, title="Recorded title",
            branch=None, brief_sha256="a" * 64, at=10,
        )
        return registry

    def test_head_has_get_headers_and_no_body(self):
        port = self.start()
        status, headers, raw = self.request(port)
        head_status, head_headers, head_raw = self.request(port, "HEAD")
        self.assertEqual((status, head_status, head_raw), (200, 200, b""))
        for name in ("Content-Type", "Content-Length", "Cache-Control", "X-Frame-Options"):
            self.assertEqual(head_headers[name], headers[name])
        self.assertEqual(int(head_headers["Content-Length"]), len(raw))

    def test_thread_still_lists_after_its_project_is_removed(self):
        port = self.start()
        self.seed("v3", "alpha", "recorded").remove("alpha")
        status, body = self.read(port)
        self.assertEqual((status, body["projects"]), (200, []))
        self.assertEqual([(row["thread_id"], row["project"]) for row in body["threads"]], [("recorded", "alpha")])

    def test_thread_cap_at_and_above_the_limit(self):
        ledger = Mock(spec=Ledger)
        registry = Mock(spec=Registry, **{"list.return_value": []})
        port = self.start(work=lambda: (registry, ledger))
        for size in (50, 51):
            with self.subTest(size=size):
                ledger.threads.return_value = [
                    Thread(f"thread-{n}", "alpha", "Fixture title", n, None, "a" * 64, "started", False, n)
                    for n in range(size)
                ]
                status, body = self.read(port)
                self.assertEqual(status, 200)
                self.assertEqual([row["thread_id"] for row in body["threads"]],
                                 [f"thread-{n}" for n in range(size - 1, size - 51, -1)])

    def test_ledger_failure_does_not_return_projects(self):
        registry = Mock(spec=Registry, **{"list.return_value": [Project("alpha", "/fixture", "p", "m", None)]})
        ledger = Mock(spec=Ledger, **{"threads.side_effect": WorkError("fixture ledger error")})
        port = self.start(work=lambda: (registry, ledger))
        self.assertEqual(self.read(port), (503, UNAVAILABLE))
        registry.list.assert_called_once_with()
        ledger.threads.assert_called_once_with()

    def test_provider_work_error_is_a_503(self):
        provider = Mock(side_effect=WorkError("fixture provider error"))
        port = self.start(work=provider)
        self.assertEqual(self.read(port), (503, UNAVAILABLE))
        provider.assert_called_once_with()

    def test_each_daemon_uses_its_own_work_files(self):
        ports = (self.start("one"), self.start("two"))
        self.seed("one", "alpha", "first")
        self.seed("two", "beta", "second")
        for port, project, thread_id in zip(ports, ("alpha", "beta"), ("first", "second"), strict=True):
            with self.subTest(project=project):
                status, body = self.read(port)
                self.assertEqual((status, body["projects"]), (200, [{"name": project}]))
                self.assertEqual([row["thread_id"] for row in body["threads"]], [thread_id])

    def test_symlinked_registry_is_refused(self):
        port = self.start()
        root = self.root / "v3" / "work"
        root.mkdir(mode=0o700)
        target = self.root / "registry-fixture.json"
        target.write_text('{"v":1,"projects":{}}', encoding="utf-8")
        target.chmod(0o600)
        (root / "projects.json").symlink_to(target)
        self.assertEqual(self.read(port), (503, UNAVAILABLE))


if __name__ == "__main__":
    unittest.main()
