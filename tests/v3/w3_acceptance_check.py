"""Orchestrator-owned acceptance tests for W3 (implementers: do not edit; if one is wrong, stop and say why).

Contract: an owner-only read of the coding threads Jarvis started, for the HUD's T3 panel (A6a's rules apply unchanged).
  jarvis/v3/owner_routes.py
    ROUTES[("GET", "/v3/owner/work")]           a Route with owner=True. It is the ONLY table entry whose path starts with "/v3/owner/work":
                                                 no POST, no sub-path. W3 is read-only (starting or steering a thread is W1b's chat tools).
  jarvis/v3/daemon.py
    Daemon(port=None, data_dir=None, hud_dir=None, *, jobs=None, chat=None, work=None)
       work: a zero-argument callable returning (registry, ledger), called once per request to the route and never at start-up.
             registry.list() -> [Project]  and  ledger.threads() -> [Thread]  (W1a's `jarvis/v3/work.py`; no other method is called,
             and threads() is called with no project filter). None (the default) builds W1a's own Registry and Ledger over the
             daemon's data dir, <data_dir>/work/projects.json and <data_dir>/work/threads.jsonl (lazy import: agentd start-up stays light).
  GET /v3/owner/work -> 200 {"projects": [{"name"}], "threads": [{"thread_id", "project", "title", "status", "waiting_on_owner",
                                                                  "started_at", "updated_at"}]}
       exactly those keys, values copied from the dataclasses unchanged (status is the ledger's word; times are the floats it holds).
       projects: sorted by name, at most 32 (the registry's own limit). threads: every ledger thread, including threads of a
       project since removed, sorted by started_at newest first (ties keep the ledger's order), the newest 50 only (WORK_THREAD_CAP).
       Never in the answer: a project's path, provider, model or base; a thread's branch, brief hash or run id.
       The route reads the registry and ledger only: it never constructs `t3_client.T3` and never starts a process.
  jarvis.v3.work.WorkError from either read -> 503 {"error": "Service Unavailable"} (the daemon's fixed text; no detail from the
       exception). Any other exception -> the daemon's plain 500. A missing registry and ledger -> 200 with two empty lists, and the
       read creates no file or folder.
  Refused like every owner read: no Sec-Fetch-Site/Origin (403), cross-site (403), a foreign Origin (403); the provider is not called.
"""
import http.client
import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from jarvis import config
from jarvis.v3 import daemon, owner_routes, t3_client
from jarvis.v3.work import WorkError

WORK = "/v3/owner/work"
MARKERS = ("/home/user/marker-path", "marker-provider", "marker-model", "marker-base", "jarvis/marker-branch", "ab" * 32, "marker-run")
THREAD_KEYS = {"thread_id", "project", "title", "status", "waiting_on_owner", "started_at", "updated_at"}


@dataclass(frozen=True)
class FakeProject:
    name: str
    path: str = MARKERS[0]
    provider: str = MARKERS[1]
    model: str = MARKERS[2]
    base: str | None = MARKERS[3]


@dataclass(frozen=True)
class FakeThread:
    thread_id: str
    project: str
    title: str
    started_at: float
    status: str = "started"
    waiting_on_owner: bool = False
    updated_at: float = 0.0
    branch: str | None = MARKERS[4]
    brief_sha256: str = MARKERS[5]
    run_id: str = MARKERS[6]


class FakeRegistry:
    def __init__(self, projects=(), error=None):
        self.projects, self.error, self.calls = list(projects), error, 0

    def list(self):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.projects)

    def __getattr__(self, name):                       # get/add/remove: a read route must not reach them
        raise AssertionError(f"the route called registry.{name}")


class FakeLedger:
    def __init__(self, threads=(), error=None):
        self.rows, self.error, self.calls = list(threads), error, 0

    def threads(self, project=None):
        self.calls += 1
        if project is not None:
            raise AssertionError("the route filtered by project")
        if self.error:
            raise self.error
        return list(self.rows)

    def __getattr__(self, name):                       # record_start/record_status/get
        raise AssertionError(f"the route called ledger.{name}")


def thread(n, project="alpha", **kw):
    return FakeThread(f"thread-{n:03d}", project, f"Fixture title {n}", 1_700_000_000.0 + n, updated_at=1_700_000_500.5 + n, **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.enterContext(patch.object(config, "V3_DIR", self.tmp / "v3"))
        self.registry, self.ledger, self.provided = FakeRegistry(), FakeLedger(), 0

    def provider(self):
        self.provided += 1
        return self.registry, self.ledger

    def start(self, **kw):
        kw.setdefault("work", self.provider)
        self.d = daemon.Daemon(port=0, data_dir=self.tmp / "v3", hud_dir=self.tmp / "hud3", jobs=[], **kw)
        self.d.start()
        self.addCleanup(self.d.stop)
        self.host = f"127.0.0.1:{self.d.port}"

    def get(self, headers=None, method="GET", path=WORK):
        conn = http.client.HTTPConnection("127.0.0.1", self.d.port, timeout=10)
        conn.request(method, path, headers={"Host": self.host, **({"Sec-Fetch-Site": "same-origin"} if headers is None else headers)})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp, raw


class Shape(Base):
    def test_exact_fields_and_no_leaks(self):
        self.registry.projects = [FakeProject("beta"), FakeProject("alpha", base=None)]
        self.ledger.rows = [thread(1, status="running", waiting_on_owner=True), thread(2, project="gone", status="completed")]
        self.start()
        resp, raw = self.get()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "application/json")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        body = json.loads(raw)
        self.assertEqual(set(body), {"projects", "threads"})
        self.assertEqual(body["projects"], [{"name": "alpha"}, {"name": "beta"}])
        self.assertEqual(body["threads"], [
            {"thread_id": "thread-002", "project": "gone", "title": "Fixture title 2", "status": "completed", "waiting_on_owner": False,
             "started_at": 1_700_000_002.0, "updated_at": 1_700_000_502.5},
            {"thread_id": "thread-001", "project": "alpha", "title": "Fixture title 1", "status": "running", "waiting_on_owner": True,
             "started_at": 1_700_000_001.0, "updated_at": 1_700_000_501.5},
        ])
        self.assertTrue(all(set(row) == THREAD_KEYS for row in body["threads"]))
        for marker in MARKERS:
            self.assertNotIn(marker.encode(), raw, marker)
        self.assertEqual((self.provided, self.registry.calls, self.ledger.calls), (1, 1, 1))

    def test_newest_first_and_capped(self):
        rows = [thread(n) for n in range(60)]
        self.ledger.rows = rows[30:] + rows[:30]                       # the route sorts; it does not trust the order it is given
        self.registry.projects = [FakeProject(f"p{n:02d}") for n in range(40, 0, -1)]
        self.start()
        body = json.loads(self.get()[1])
        self.assertEqual([row["thread_id"] for row in body["threads"]], [f"thread-{n:03d}" for n in range(59, 9, -1)])
        self.assertEqual([p["name"] for p in body["projects"]], [f"p{n:02d}" for n in range(1, 33)])

    def test_ties_keep_the_ledger_order(self):
        self.ledger.rows = [FakeThread("thread-b", "alpha", "B", 5.0), FakeThread("thread-a", "alpha", "A", 5.0), FakeThread("thread-c", "alpha", "C", 9.0)]
        self.start()
        self.assertEqual([row["thread_id"] for row in json.loads(self.get()[1])["threads"]], ["thread-c", "thread-b", "thread-a"])

    def test_empty(self):
        self.start()
        resp, raw = self.get()
        self.assertEqual((resp.status, json.loads(raw)), (200, {"projects": [], "threads": []}))


class Failures(Base):
    def test_work_error_is_a_fixed_503(self):
        detail = "malformed marker-detail /home/user/secret"
        for registry, ledger in ((FakeRegistry(error=WorkError(detail)), FakeLedger([thread(1)])),
                                 (FakeRegistry([FakeProject("alpha")]), FakeLedger(error=WorkError(detail)))):
            self.registry, self.ledger = registry, ledger
            if not hasattr(self, "d"):
                self.start()
            resp, raw = self.get()
            self.assertEqual((resp.status, json.loads(raw)), (503, {"error": "Service Unavailable"}))
            self.assertNotIn(b"marker-detail", raw)
            self.assertNotIn(b"thread-001", raw)

    def test_other_errors_are_a_plain_500(self):
        self.registry = FakeRegistry(error=OSError("marker-detail /home/user/secret"))
        self.start()
        resp, raw = self.get()
        self.assertEqual((resp.status, json.loads(raw)), (500, {"error": "Internal Server Error"}))

    def test_t3_is_never_touched(self):
        self.registry.projects, self.ledger.rows = [FakeProject("alpha")], [thread(1, status="running")]
        self.start()
        with patch.object(t3_client.T3, "__init__", side_effect=AssertionError("T3 constructed")), \
             patch("subprocess.Popen", side_effect=AssertionError("process started")), \
             patch("subprocess.run", side_effect=AssertionError("process started")):
            resp, raw = self.get()
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(json.loads(raw)["threads"]), 1)


class Default(Base):
    def test_missing_files_read_as_empty_and_create_nothing(self):
        self.start(work=None)
        resp, raw = self.get()
        self.assertEqual((resp.status, json.loads(raw)), (200, {"projects": [], "threads": []}))
        self.assertFalse((self.tmp / "v3" / "work").exists())

    def test_default_reads_the_data_dirs_registry(self):
        self.start(work=None)
        folder = self.tmp / "v3" / "work"
        folder.mkdir(mode=0o700)
        path = folder / "projects.json"
        path.write_text("{not json marker-detail")
        path.chmod(0o600)
        resp, raw = self.get()
        self.assertEqual((resp.status, json.loads(raw)), (503, {"error": "Service Unavailable"}))

    def test_the_provider_is_not_called_at_start_up(self):
        self.start()
        self.assertEqual(self.provided, 0)


class Owner(Base):
    def test_refused_without_the_window(self):
        self.registry.projects, self.ledger.rows = [FakeProject("alpha")], [thread(1)]
        self.start()
        foreign = {"Origin": "http://example.invalid"}
        for headers in ({}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}, {"Sec-Fetch-Site": "none"}, foreign,
                        {"Origin": "null"}, {"Sec-Fetch-Site": "same-origin", "Upgrade": "websocket"}):
            resp, raw = self.get(headers)
            self.assertEqual(resp.status, 403, headers)
            self.assertNotIn(b"thread-001", raw)
        self.assertEqual(self.provided, 0)
        resp, raw = self.get({"Origin": f"http://{self.host}"})
        self.assertEqual(resp.status, 200)
        resp, raw = self.get(method="HEAD")
        self.assertEqual((resp.status, raw), (200, b""))

    def test_get_only_and_in_the_table(self):
        self.start()
        route = owner_routes.ROUTES[("GET", WORK)]
        self.assertIs(route.owner, True)
        self.assertEqual({key for key in owner_routes.ROUTES if key[1].startswith(WORK)}, {("GET", WORK)})
        self.assertNotIn(("GET", WORK), owner_routes.PUBLIC)
        conn = http.client.HTTPConnection("127.0.0.1", self.d.port, timeout=10)
        conn.request("POST", WORK, body=b"{}", headers={"Host": self.host, "Origin": f"http://{self.host}", "Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertIn(resp.status, (404, 405))
        for path in (WORK + "/", WORK + "?project=alpha", WORK + "/alpha"):
            self.assertEqual(self.get(path=path)[0].status, 404, path)
        self.assertEqual(self.provided, 0)


if __name__ == "__main__":
    unittest.main()
