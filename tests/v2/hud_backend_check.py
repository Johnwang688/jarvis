"""WP12a: free integration checks, real loopback listeners and Git, fake brains."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import queue
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from jarvis import config, models, avatars, voice
from jarvis.tools import voicectl
from jarvis.v2 import hud_api as H, worktrees
from jarvis.v2.daemon import Daemon, DaemonError
from jarvis.v2.model import ProviderName as P, Role, TaskState, to_json
from jarvis.v2.provider import Brief, Decision, Event, EventKind as K, SessionHandle, Usage
from jarvis.v2.providers.codex import CodexProvider
from jarvis.v2.schedules import Cron, Schedules, ZONE, parse_when, describe
from jarvis.v2.stores import Stores


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True,
                          text=True, check=True).stdout


def eventually(predicate):
    until = time.monotonic() + 4
    while not predicate():
        if time.monotonic() > until:
            raise AssertionError("condition did not settle")
        time.sleep(0.005)


class Fake:
    def __init__(self, name):
        self.name, self.messages, self.report = name, [], None

    def health(self):
        return True, "fake healthy"

    def start(self, thread, brief, permit):
        return SessionHandle(thread.id, self.name, "fake:" + thread.id, SimpleNamespace(brief=brief))

    resume = start

    def usage(self, handle):
        return Usage()

    def send(self, handle, message):
        self.messages.append(message)
        yield Event(K.TEXT_DELTA, handle.thread_id, {"text": "unfinished"})
        yield Event(K.TEXT, handle.thread_id, {"text": "settled"})
        yield Event(K.USAGE, handle.thread_id, dict(input=12, output=4, cached=2, cost_usd=None,
                    provider_reported={"rate_limits": self.report} if self.report else {}))
        yield Event(K.TURN_FINISHED, handle.thread_id, {"stop": "end"})

    def interrupt(self, handle):
        pass

    def close(self, handle):
        pass


class FakeControl:
    """TaskControl verbs exercised by schedules do real store transitions."""
    def __init__(self, stores):
        self.stores, self.started = stores, []

    def start(self, task_id):
        self.started.append(task_id)
        return self.stores.tasks.transition(task_id, TaskState.CLARIFYING)


WHEN_CASES = [
    ("every day at 9", {"cron": "0 9 * * *"}, "every day at 09:00"),
    ("daily at 09:15", {"cron": "15 9 * * *"}, "every day at 09:15"),
    ("weekdays at 8:30", {"cron": "30 8 * * 1-5"}, "weekdays at 08:30"),
    ("weekends at 10am", {"cron": "0 10 * * 0,6"}, "weekends at 10:00"),
    ("mondays at 10", {"cron": "0 10 * * 1"}, "mondays at 10:00"),
    ("every tuesday at 6 pm", {"cron": "0 18 * * 2"}, "tuesdays at 18:00"),
    ("wednesday at 12am", {"cron": "0 0 * * 3"}, "wednesdays at 00:00"),
    ("thursdays at 12 pm", {"cron": "0 12 * * 4"}, "thursdays at 12:00"),
    ("  FRIDAYS  at  23:59 ", {"cron": "59 23 * * 5"}, "fridays at 23:59"),
    ("saturdays at 7:05am", {"cron": "5 7 * * 6"}, "saturdays at 07:05"),
    ("sundays at 1", {"cron": "0 1 * * 0"}, "sundays at 01:00"),
    ("every 2 hours", {"every_s": 7200}, "every 2 hours"),
    ("every 15 minutes", {"every_s": 900}, "every 15 minutes"),
    ("every minute", {"every_s": 60}, "every 1 minute"),
    ("every 30 seconds", {"every_s": 30}, "every 30 seconds"),
    ("every 3 days", {"every_s": 259200}, "every 3 days"),
    ("every 2 weeks", {"every_s": 1209600}, "every 2 weeks"),
    ("0 9 * * 1-5", {"cron": "0 9 * * 1-5"}, "weekdays at 09:00"),
]
WHEN_REFUSALS = ("tomorrow morning", "every few hours", "weekdays at 25:00", "every 0 minutes",
                 "every day at 9:99", "mondays at 0pm", "9 * * *", "0 9 31 2 *")


class Backend(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        self.extra = self.root / "extra"
        self.extra.mkdir()
        self.other = self.root / "other"
        self.other.mkdir()
        (self.project_root / "hello.txt").write_text("hello\n")
        (self.project_root / "index.html").write_text("<h1>preview</h1>")
        (self.project_root / ".env").write_text("SECRET=never-expose-this\n")
        (self.extra / "extra.txt").write_text("extra")
        (self.other / "private.txt").write_text("outside")
        for name in H.SKIP_DIRS:
            if name != ".git":
                directory = self.project_root / name
                directory.mkdir()
                (directory / "hidden").write_text("hidden")
        (self.project_root / "leak").symlink_to(self.other, target_is_directory=True)
        (self.project_root / "alias.txt").symlink_to(self.project_root / ".env")
        git(self.project_root, "init", "-q")
        git(self.project_root, "config", "user.email", "test@example.invalid")
        git(self.project_root, "config", "user.name", "Test")
        git(self.project_root, "add", "hello.txt", "index.html")
        git(self.project_root, "commit", "-qm", "base")
        for name, value in dict(V2_DATA_DIR=self.root / "data", ALLOWLIST_PATH=self.extra / "allowlist.json",
                                MODELS_PATH=self.extra / "models.json", REPO_ROOT=self.project_root,
                                AVATAR_STATE_PATH=self.root / "avatar.json", AVATARS_DIR=self.root / "avatars",
                                AVATAR_ENV="").items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        self.catalog = [models.normalize(dict(id="test/model", name="Test", context_length=10000,
                                             supported_parameters=["tools", "reasoning"],
                                             architecture={"input_modalities": ["text", "image"], "output_modalities": ["text"]},
                                             pricing={"prompt": "0", "completion": "0"}))]
        catalog = patch.object(models, "catalog", return_value=self.catalog)
        catalog.start()
        self.addCleanup(catalog.stop)
        self.stores = Stores()
        self.providers = {name: Fake(name) for name in P}
        self.daemon = Daemon(self.stores, self.providers, lambda *_: lambda *_: Decision.DENY, 0)
        self.daemon.runner = FakeControl(self.stores)
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        self.project = self.stores.projects.create("Test", str(self.project_root), extra_dirs=[str(self.extra)])
        self.second = self.stores.projects.create("Other", str(self.other))
        self.events = self.daemon.bus.subscribe()
        self.url = f"/projects/{self.project.id}"

    def request(self, method, path, body=None, status=200, *, port=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", port or self.daemon.port, timeout=5)
        request_headers = {"Content-Type": "application/json"} if body is not None else {}
        request_headers.update(headers or {})
        data = json.dumps(body).encode() if body is not None else raw
        try:
            conn.request(method, path, data, request_headers)
            response = conn.getresponse()
            content = response.read()
            self.assertEqual(response.status, status, content[:500])
            result = json.loads(content) if response.getheader("Content-Type", "").startswith("application/json") else content
            if status >= 400:
                self.assertEqual(set(result), {"error"})
                self.assertNotIn("never-expose-this", result["error"])
            self.headers = dict(response.getheaders())
            return result
        finally:
            conn.close()

    def getfile(self, name, **kw):
        return self.request("GET", self.url + "/file?" + urlencode({"path": name}), **kw)

    def thread(self, provider=P.FAST, task_id=None):
        return self.daemon.open_thread(self.project.id, Role.CHAT, provider,
                                      Brief(Role.CHAT, str(self.project_root), task_id=task_id))

    def settled(self, thread):
        eventually(lambda: self.daemon._sessions[thread.id].worker is None)

    def events_all(self):
        result = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                return result

    def schedule(self, **extra):
        return self.request("POST", "/schedules", dict(project_id=self.project.id,
                            brief="scheduled work", every_s=60, **extra), status=201)

    def test_fs_dirs_defaults_to_home(self):
        # The picker opens with no path and expects $HOME (WP12c frontend).
        body = self.request("GET", "/fs/dirs")
        self.assertEqual(body["path"], str(Path.home()))

    def test_chat_thread_opens_with_project_and_role_only(self):
        # The HUD's New Thread sends only these two; the daemon defaults the
        # rest (found live 2026-09-16: a silent 400 and nothing on screen).
        body = self.request("POST", "/threads", {"project_id": self.project.id, "role": "chat"}, status=201)
        self.assertEqual(body["provider"], "fast")
        self.assertEqual(body["role"], "chat")
        self.request("POST", "/threads", {"project_id": self.project.id, "role": "implementer"}, status=400)

    def test_listeners_static_and_preview_origin(self):
        for port in (self.daemon.port, self.daemon.face_port):
            self.assertEqual(self.request("GET", "/status", port=port)["version"], 2)
            # Either the built HUD (title pinned for FORBIDDEN_TITLES) or, with no
        # build present, the placeholder naming hud/dist.
        page = self.request("GET", "/", port=port)
        self.assertTrue(b"<title>J.A.R.V.I.S.</title>" in page or b"hud/dist" in page)
        # Simulate a separately built HUD without writing hud/dist.
        dist = self.root / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("built HUD")
        (dist / "assets" / "app.js").write_text("console.log('HUD')")
        with patch.object(H, "HUD_DIST", dist):
            self.assertEqual(self.request("GET", "/"), b"built HUD")
            self.assertIn(b"console", self.request("GET", "/assets/app.js"))
            self.request("GET", "/assets/no.js", status=404)
            self.request("GET", "/assets/%2e%2e/index.html", status=403)
        preview = self.daemon.workshop_port
        prefix = f"/p/{self.project.id}/"
        self.assertEqual(self.request("GET", prefix + "hello.txt", port=preview), b"hello\n")
        self.assertEqual(self.headers["Cache-Control"], "no-store")
        self.assertIn("text/plain", self.headers["Content-Type"])
        self.assertIn(b"preview", self.request("GET", prefix, port=preview))
        for path, code in [("/status", 404), ("/", 404), ("/assets/app.js", 404),
                           (prefix + ".env", 403), (prefix + ".git/config", 403),
                           (prefix + "../other/private.txt", 403),
                           (prefix + "leak/private.txt", 403),
                           (prefix + "http://localhost:8402/", 403),
                           (prefix + "%2F" + str(self.other / "private.txt").lstrip("/"), 403)]:
            self.request("GET", path, port=preview, status=code)
        self.request("PUT", prefix + "hello.txt", {}, port=preview, status=404)
        self.request("GET", f"/p/{self.project.id}/hello.txt", status=404)

    def test_second_listener_collision_rolls_back_startup(self):
        other = Daemon(Stores(self.root / "collision"), {}, lambda *_: None, 0,
                       face_port=self.daemon.face_port)
        self.addCleanup(other.stop)
        with self.assertRaises(DaemonError):
            other.start()
        self.assertIsNone(other._server)
        self.assertEqual(other._listeners, [])
        self.assertEqual(self.request("GET", "/status")["version"], 2)

    def test_host_origin_and_errors(self):
        for headers in ({"Host": "attacker.example"}, {"Origin": f"http://localhost:{self.daemon.workshop_port}"},
                        {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
            self.request("GET", "/status", headers=headers, status=403)
            self.request("POST", "/mute", {"muted": True}, headers=headers, status=403)
        self.request("POST", "/mute", raw=b'{"muted":true}', headers={"Content-Type": "text/plain"}, status=400)
        self.request("POST", "/schedules", raw=b"{", status=400)
        self.request("GET", "/usage?x=1", status=400)
        self.request("GET", self.url + "/tree?depth=1&depth=2", status=400)
        self.request("GET", "/projects/deadbeef/tree", status=404)
        self.request("GET", "/projects/bad/tree", status=400)
        self.request("GET", "/nothing", status=404)
        self.request("POST", "/say", raw=b"", headers={"Content-Length": str(49 * 1024 * 1024)}, status=413)

    def test_tree_depth_scope_and_platform(self):
        (self.project_root / "nested").mkdir()
        (self.project_root / "nested" / "a.txt").write_text("a")
        first = self.request("GET", self.url + "/tree?depth=1")
        self.assertNotIn("nested/a.txt", [x["name"] for x in first["entries"]])
        second = self.request("GET", self.url + "/tree?depth=2")
        names = [x["name"] for x in second["entries"]]
        self.assertIn("nested/a.txt", names)
        self.assertNotIn("leak", names)
        self.assertFalse(set(names) & H.SKIP_DIRS)
        for row in second["entries"]:
            self.assertEqual(set(row), {"name", "kind", "size", "mtime"})
        self.request("GET", self.url + "/tree?depth=-1", status=400)
        self.request("GET", self.url + "/tree?path=missing", status=404)
        self.assertEqual(self.request("GET", self.url + "/platform"), {"platform": "wsl", "note": None})
        self.project.root = "/mnt/c/Users/owner"
        self.stores.projects.save(self.project)
        platform = self.request("GET", self.url + "/platform")
        self.assertEqual(platform["platform"], "windows")
        self.assertIn("9p", platform["note"])

    def test_file_read_write_conflict_and_caps(self):
        value = self.getfile("hello.txt")
        self.assertEqual(set(value), {"path", "content", "mtime", "size", "protected"})
        self.assertEqual(value["content"], "hello\n")
        saved = self.request("PUT", self.url + "/file", dict(path="hello.txt", content="changed", expected_mtime=value["mtime"]))
        self.assertIsInstance(saved["mtime"], float)
        self.assertEqual((self.project_root / "hello.txt").read_text(), "changed")
        self.request("PUT", self.url + "/file", dict(path="hello.txt", content="bad", expected_mtime=value["mtime"]), status=409)
        self.request("PUT", self.url + "/file", dict(path="new.txt", content="new", expected_mtime=None))
        self.assertEqual(self.getfile(str(self.extra / "extra.txt"))["content"], "extra")
        for path in (".env", "alias.txt"):
            self.assertEqual(self.getfile(path), {"protected": True})
            self.request("PUT", self.url + "/file", dict(path=path, content="bad", expected_mtime=None), status=403)
        for path in ("../other/private.txt", "leak/private.txt", str(self.other / "private.txt"), ".git/config"):
            self.getfile(path, status=403)
            self.request("PUT", self.url + "/file", dict(path=path, content="bad", expected_mtime=None), status=403)
        self.getfile("missing", status=404)
        self.request("GET", self.url + "/file", status=400)
        self.request("PUT", self.url + "/file", {"path": "hello.txt"}, status=400)
        self.request("PUT", self.url + "/file", dict(path="hello.txt", content=4, expected_mtime=0), status=400)
        self.request("PUT", self.url + "/file", dict(path="hello.txt", content="x" * (H.FILE_CAP + 1), expected_mtime=0), status=413)
        (self.project_root / "big").write_bytes(b"x" * (H.FILE_CAP + 1))
        self.getfile("big", status=413)
        self.assertFalse(list(self.project_root.glob(".*.tmp")))

    def test_write_protects_permission_code_and_config(self):
        for name in ("jarvis/tools/files.py", "jarvis/tools/secrets.py"):
            path = self.project_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("guard")
            self.request("PUT", self.url + "/file", dict(path=name, content="bad", expected_mtime=path.stat().st_mtime), status=403)
        for name in ("allowlist.json", "models.json", "routing.json"):
            path = self.extra / name
            path.write_text("{}")
            self.request("PUT", self.url + "/file", dict(path=str(path), content="bad", expected_mtime=path.stat().st_mtime), status=403)

    def test_diff_committed_working_tree_rename_delete_and_untracked(self):
        task = self.stores.tasks.create(self.project.id, "edit files")
        task = worktrees.ensure(task, self.project, self.stores)
        root = Path(task.worktree)
        (root / "hello.txt").write_text("committed\n")
        (root / "added.txt").write_text("new\n")
        git(root, "add", ".")
        git(root, "commit", "-qm", "task change")
        (root / "hello.txt").write_text("committed\nworking\n")
        (root / "scratch.txt").write_text("scratch\n")
        (root / ".env").write_text("must never appear")
        (root / "alias").symlink_to(self.other / "private.txt")
        path = f"/tasks/{task.id}/diff"
        result = self.request("GET", path)
        self.assertEqual(result["head"], git(root, "rev-parse", "HEAD").strip())
        self.assertEqual({r["path"] for r in result["files"]}, {"hello.txt", "added.txt", "scratch.txt"})
        self.assertIn("+working", result["patch"])
        self.assertNotIn("must never appear", result["patch"])
        self.assertEqual(self.request("GET", path + "/file?path=hello.txt"), {"before": "hello\n", "after": "committed\nworking\n"})
        self.assertEqual(self.request("GET", path + "/file?path=added.txt")["before"], "")
        for name in (".env", "alias", "../hello.txt"):
            self.request("GET", path + "/file?" + urlencode({"path": name}), status=403)
        self.request("GET", path + "/file?path=missing", status=404)
        git(root, "mv", "index.html", "renamed.html")
        (root / "hello.txt").unlink()
        result = self.request("GET", path)
        statuses = {r["path"]: r["status"] for r in result["files"]}
        self.assertEqual(statuses["renamed.html"], "R")
        self.assertEqual(statuses["hello.txt"], "D")
        self.assertIn("preview", self.request("GET", path + "/file?path=renamed.html")["before"])
        self.assertEqual(self.request("GET", path + "/file?path=hello.txt")["after"], "")
        with patch.object(H, "PATCH_CAP", 40):
            result = self.request("GET", path)
            self.assertTrue(result["truncated"])
            self.assertLessEqual(len(result["patch"].encode()), 40)
        blank = self.stores.tasks.create(self.project.id, "no checkout")
        self.request("GET", f"/tasks/{blank.id}/diff", status=409)
        self.request("GET", "/tasks/deadbeef/diff", status=404)

    def test_transcript_attachments_and_task_views(self):
        task = self.stores.tasks.create(self.project.id, "linked task")
        self.stores.tasks.save(task)
        thread = self.thread(task_id=task.id)
        self.stores.tasks.journal(task.id, "example", detail="yes")
        data = base64.b64encode(b"attached text").decode()
        body = {"text": "review @hello.txt @.env", "attachments": [
            {"name": "note.txt", "mime": "text/plain", "data_b64": data},
            {"name": ".env", "mime": "text/plain", "data_b64": base64.b64encode(b"never-expose-this").decode()},
            {"name": "pic.png", "mime": "image/png", "data_b64": data}]}
        path = f"/threads/{thread.id}"
        self.request("POST", path + "/send", body, status=202)
        self.settled(thread)
        message = self.providers[P.FAST].messages[-1]
        self.assertIn("```\nattached text\n```", message.text)
        self.assertIn("holds live credentials", message.text)
        self.assertNotIn("never-expose-this", message.text)
        self.assertIn("hello", message.text)
        self.assertEqual(len(message.images), 1)
        transcript = self.request("GET", path + "/transcript")["messages"]
        self.assertEqual([r["role"] for r in transcript], ["user", "assistant"])
        self.assertEqual(transcript[-1]["text"], "settled")
        self.assertTrue(all(set(r) == {"role", "text", "at"} and r["at"] for r in transcript))
        self.stores.threads.log(thread.id, "user", "legacy")
        self.assertEqual(self.request("GET", path + "/transcript")["messages"][-1]["text"], "legacy")
        rows = self.request("GET", f"/tasks/{task.id}/threads")
        self.assertEqual(rows[0]["state"], "open")
        self.assertEqual(set(rows[0]), {"thread_id", "role", "provider", "model", "state", "turns", "cost_usd"})
        self.daemon.close_thread(thread.id)
        self.assertEqual(self.request("GET", f"/tasks/{task.id}/threads")[0]["state"], "closed")
        self.assertEqual(self.request("GET", f"/tasks/{task.id}/journal")[0]["event"], "example")
        self.assertEqual(self.request("GET", f"/tasks/{task.id}/journal?after=1"), [])
        self.request("GET", f"/tasks/{task.id}/journal?after=-1", status=400)
        self.request("GET", "/threads/deadbeef/transcript", status=404)
        self.request("POST", path + "/send", {"text": "", "attachments": [{}]}, status=400)

    def test_attachment_limits_scrubbing_and_scope(self):
        make = lambda name, data: dict(name=name, mime="text/plain", data_b64=base64.b64encode(data).decode())
        body = dict(text="@../other/private.txt @leak/private.txt", attachments=[
            make("big.txt", b"x" * (H.ATTACH_CAP + 1)), make("long.txt", b"a" * 100001),
            make("secret.txt", b"credential-value"), dict(name="bad", mime="text/plain", data_b64="%%%")]
            + [make(f"{i}.txt", b"text") for i in range(5)])
        with patch("jarvis.tools.secrets.secret_values", return_value=["credential-value"]):
            message = H.assemble_turn(self.project, body)
        self.assertIn("over 4MB", message.text)
        self.assertIn("[truncated]", message.text)
        self.assertIn("8-per-turn", message.text)
        self.assertIn("unreadable attachment", message.text)
        self.assertIn("outside scope", message.text)
        self.assertNotIn("credential-value", message.text)
        self.assertNotIn("outside\n", message.text)
        protected = H.assemble_turn(self.project, dict(text="inspect", attachments=[make("x" * 210 + "/.env", b"hidden")]))
        self.assertNotIn("hidden", protected.text)
        # Upload-only turns are allowed.
        self.assertIn("text", H.assemble_turn(self.project, dict(text="", attachments=[make("a", b"text")])).text)

    def test_usage_quota_durable_sparse_and_idempotent(self):
        result = self.request("GET", "/usage")["providers"]
        self.assertEqual(set(result), {"claude", "codex", "fast"})
        for row in result.values():
            self.assertEqual(set(row), {"state", "reason", "today", "allowance", "quota"})
            self.assertEqual(row["state"], "available")
            self.assertIsNone(row["quota"])
            self.assertEqual(set(row["today"]), {"work_tokens", "spend_usd", "equivalent_usd"})
        self.providers[P.CODEX].report = {"limitId": "codex", "primary": {"usedPercent": 42, "windowDurationMins": 300, "resetsAt": 1900000000},
                                         "secondary": {"usedPercent": 8, "windowDurationMins": 10080, "resetsAt": 1900001000}}
        thread = self.thread(P.CODEX)
        self.request("POST", f"/threads/{thread.id}/send", {"text": "usage"}, status=202)
        self.settled(thread)
        quota = self.request("GET", "/usage")["providers"]["codex"]["quota"]
        self.assertEqual(quota["windows"][0], {"name": "5h", "used_percent": 42, "resets_at": 1900000000})
        self.assertEqual(quota["windows"][1]["name"], "weekly")
        events = self.events_all()
        self.assertTrue(any(e["kind"] == "usage_updated" and e["data"]["provider"] == "codex" for e in events))
        ledger = self.daemon.router.ledger
        before = ledger.totals(provider="codex")
        record = next(e for e in events if e["kind"] == "usage")
        self.daemon.router.on_event(record)
        self.assertEqual(ledger.totals(provider="codex"), before)
        self.providers[P.CODEX].report = {"limitId": "codex", "primary": {"usedPercent": 45, "resetsAt": None}, "secondary": None}
        self.request("POST", f"/threads/{thread.id}/send", {"text": "update"}, status=202)
        self.settled(thread)
        restored = H.HUDLedger(self.stores)
        self.assertEqual(restored.quota("codex")["windows"][0]["used_percent"], 45)
        self.assertEqual(restored.quota("codex")["windows"][0]["resets_at"], 1900000000)
        self.assertEqual(restored.totals(provider="codex")["work_tokens"], 14)
        self.assertIsNone(restored.quota("claude"))

    def test_schedules_crud_fire_skip_disable_and_run_now(self):
        schedule = self.schedule()
        self.assertEqual(set(schedule), {"id", "project_id", "brief", "cron", "every_s", "enabled", "last_run_at", "last_task_id", "next_run_at", "created"})
        path = "/schedules/" + schedule["id"]
        self.assertTrue((self.stores.root / "schedules" / (schedule["id"] + ".json")).is_file())
        self.assertEqual(self.request("GET", "/schedules"), [schedule])
        self.assertEqual(Schedules(self.daemon).get(schedule["id"]), schedule)
        self.assertIsNone(self.daemon.schedules.fire(schedule["id"]))
        fired = self.request("POST", path + "/run-now", {})
        task_id = fired["last_task_id"]
        self.assertEqual(self.daemon.runner.started, [task_id])
        self.assertEqual(self.stores.tasks.get(task_id).state, TaskState.CLARIFYING)
        self.assertEqual(self.stores.tasks.read_journal(task_id)[0]["event"], "scheduled_by")
        again = self.request("POST", path + "/run-now", {})
        self.assertEqual(again["last_task_id"], task_id)
        self.assertEqual(self.stores.tasks.read_journal(task_id)[-1]["event"], "schedule_skipped")
        self.assertEqual(len(self.daemon.runner.started), 1)
        events = self.events_all()
        self.assertEqual(sum(e["kind"] == "schedule_fired" for e in events), 1)
        self.assertEqual(sum(e["kind"] == "schedule_created" for e in events), 1)
        disabled = self.request("PATCH", path, {"enabled": False})
        self.assertIsNone(disabled["next_run_at"])
        self.request("POST", path + "/run-now", {}, status=409)
        self.daemon.schedules.tick()
        self.assertEqual(len(self.daemon.runner.started), 1)
        self.request("PATCH", path, {"enabled": True})
        self.stores.tasks.transition(task_id, TaskState.CANCELLED)
        due = datetime.fromisoformat(self.daemon.schedules.get(schedule["id"])["next_run_at"]).timestamp()
        self.daemon.schedules.clock = lambda: due
        self.daemon.schedules.tick()
        self.assertEqual(len(self.daemon.runner.started), 2)
        self.request("DELETE", path)
        changes = [e for e in self.events_all() if e["kind"] in {"schedule_updated", "schedule_deleted"}]
        self.assertEqual([e["kind"] for e in changes], ["schedule_updated", "schedule_updated", "schedule_deleted"])
        self.assertTrue(all(e["data"] == {"schedule_id": schedule["id"]} for e in changes))
        self.assertEqual(self.request("GET", "/schedules"), [])
        self.request("PATCH", path, {"brief": "missing"}, status=404)
        self.request("DELETE", path, status=404)
        self.request("POST", path + "/run-now", {}, status=404)
        self.request("POST", "/schedules/bad/run-now", {}, status=400)

    def test_move_chat_preserves_record_log_session_and_subsequent_turn(self):
        thread = self.thread()
        path = f"/threads/{thread.id}"
        self.request("POST", path + "/send", {"text": "before"}, status=202)
        self.settled(thread)
        session = self.daemon._sessions[thread.id]
        session.thread.title = "Travelling conversation"
        self.stores.threads.save(session.thread)
        before = to_json(self.stores.threads.get(thread.id))
        log_path = self.stores.threads.path(thread.id).with_name("log.jsonl")
        log = log_path.read_bytes()
        handle = session.handle
        result = self.request("PATCH", path, {"project_id": self.second.id})
        self.assertEqual(result, before | {"project_id": self.second.id})
        self.assertEqual(log_path.read_bytes(), log)
        self.assertIs(session.handle, handle)
        self.assertEqual(session.thread.project_id, self.second.id)
        moved = [e for e in self.events_all() if e["kind"] == "thread_moved"]
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]["data"], dict(thread_id=thread.id, from_project_id=self.project.id,
                                             to_project_id=self.second.id))
        self.request("POST", path + "/send", {"text": "after"}, status=202)
        self.settled(thread)
        saved = self.stores.threads.get(thread.id)
        self.assertEqual(saved.project_id, self.second.id)
        self.assertEqual(saved.provider_session_id, before["provider_session_id"])
        self.daemon.close_thread(thread.id)
        self.request("PATCH", path, {"project_id": self.project.id})
        self.request("POST", path + "/send", {"text": "resumed"}, status=202)
        self.settled(thread)
        self.assertEqual(self.stores.threads.get(thread.id).project_id, self.project.id)
        self.request("PATCH", path, {"project_id": "deadbeef"}, status=404)
        self.request("PATCH", path, {"project_id": self.second.id, "title": "no"}, status=400)
        self.request("PATCH", "/threads/deadbeef", {"project_id": self.second.id}, status=404)

    def test_move_task_thread_refused(self):
        task = self.stores.tasks.create(self.project.id, "owned thread")
        thread = self.thread(task_id=task.id)
        before = to_json(self.stores.threads.get(thread.id))
        result = self.request("PATCH", f"/threads/{thread.id}", {"project_id": self.second.id}, status=409)
        self.assertEqual(result, {"error": "task threads move with their task"})
        self.assertEqual(to_json(self.stores.threads.get(thread.id)), before)
        self.assertFalse(any(e["kind"] == "thread_moved" for e in self.events_all()))

    def test_directory_picker_scope_and_symlinks(self):
        home = self.root / "home"
        home.mkdir()
        for name in ("Alpha", "zeta", ".hidden"):
            (home / name).mkdir()
        (home / "file.txt").write_text("not a directory")
        (home / "escape").symlink_to(self.other, target_is_directory=True)
        (home / "inside").symlink_to(home / "Alpha", target_is_directory=True)
        (self.other / "back-in").symlink_to(home, target_is_directory=True)
        with patch.object(Path, "home", return_value=home):
            def get(path, status=200):
                return self.request("GET", "/fs/dirs?" + urlencode({"path": str(path)}), status=status)
            self.assertEqual(get(home), dict(path=str(home), parent=None, dirs=["Alpha", "inside", "zeta"]))
            self.assertEqual(get(home / "Alpha")["parent"], str(home))
            for forbidden in (self.other, home / "escape", self.other / "back-in", "/etc", "/mnt", "/mnt/cc", "relative", home / ".."):
                get(forbidden, 403)
            get(home / "missing", 404)
            get(home / "file.txt", 404)
            self.assertIn("dirs", self.request("GET", "/fs/dirs"))  # no path -> home
            # Drive roots need no real mount in the test environment.
            with patch.object(Path, "is_dir", return_value=True), patch.object(Path, "iterdir", return_value=iter([])):
                self.assertEqual(get("/mnt/c"), dict(path="/mnt/c", parent=None, dirs=[]))
                self.assertEqual(get("/mnt/D/work")["parent"], "/mnt/D")

    def test_schedule_preview_and_when_table(self):
        now = datetime(2026, 9, 15, 9, 30, tzinfo=ZONE).timestamp()
        self.daemon.schedules.clock = lambda: now
        for phrase, timing, reading in WHEN_CASES:
            with self.subTest(phrase=phrase):
                self.assertEqual(parse_when(phrase), timing)
                self.assertEqual(describe(**timing), reading)
                response = self.request("POST", "/schedules/preview", timing)
                self.assertEqual(response["describe"], reading)
                self.assertEqual(len(response["next"]), 3)
                previous = now
                for value in response["next"]:
                    at = datetime.fromisoformat(value)
                    self.assertGreater(at.timestamp(), previous)
                    self.assertEqual(at.utcoffset(), at.astimezone(ZONE).utcoffset())
                    if "cron" in timing:
                        self.assertTrue(Cron(timing["cron"]).matches(at))
                        self.assertEqual(at.timestamp(), Cron(timing["cron"]).next(previous))
                    else:
                        self.assertEqual(at.timestamp() - previous, timing["every_s"])
                    previous = at.timestamp()
        for phrase in WHEN_REFUSALS:
            self.assertIsNone(parse_when(phrase), phrase)
        for bad in ({}, {"cron": "bad"}, {"cron": "* * * * *", "every_s": 1}, {"every_s": True},
                    {"every_s": 0}, {"every_s": 1.5}, {"when": "daily"}, {"cron": "0 9 31 2 *"}):
            self.request("POST", "/schedules/preview", bad, status=400)
        self.assertEqual(self.request("GET", "/schedules"), [])

    def test_schedule_preview_dst_and_general_cron_reading(self):
        self.daemon.schedules.clock = lambda: datetime(2026, 3, 7, 3, tzinfo=ZONE).timestamp()
        self.assertEqual(self.request("POST", "/schedules/preview", {"cron": "30 2 * * *"}), {
            "next": ["2026-03-09T02:30:00-05:00", "2026-03-10T02:30:00-05:00", "2026-03-11T02:30:00-05:00"],
            "describe": "every day at 02:30"})
        self.daemon.schedules.clock = lambda: datetime(2026, 11, 1, 0, tzinfo=ZONE).timestamp()
        self.assertEqual(self.request("POST", "/schedules/preview", {"cron": "30 1 * * *"})["next"],
                         ["2026-11-01T01:30:00-05:00", "2026-11-02T01:30:00-06:00", "2026-11-03T01:30:00-06:00"])
        self.assertEqual(describe(cron="*/15 * * * *"),
                         "minute 0, 15, 30, 45; every hour; every month; (every day of month; every day of the week)")
        self.assertIn("day of month 15 or on monday", describe(cron="0 9 15 * 1"))

    def test_schedule_validation(self):
        for update in ({"every_s": 0}, {"every_s": True}, {"cron": "bad"}, {"cron": "* * * * *", "every_s": 5},
                       {"every_s": None}, {"enabled": "yes"}, {"brief": ""}, {"unknown": 1}):
            self.request("POST", "/schedules", dict(project_id=self.project.id, brief="work", every_s=60) | update, status=400)
        self.request("POST", "/schedules", {"project_id": "deadbeef", "brief": "work", "every_s": 60}, status=404)
        cron = self.request("POST", "/schedules", {"project_id": self.project.id, "brief": "daily", "cron": "0 9 * * 1-5"}, status=201)
        self.assertIsNotNone(cron["next_run_at"])
        self.daemon.runner = None
        self.request("POST", f"/schedules/{cron['id']}/run-now", {}, status=409)

    def test_speech_pickers_and_fast_model_only(self):
        with patch.object(voice, "stt", return_value="dictated") as stt:
            self.assertEqual(self.request("POST", "/stt", raw=b"RIFFaudio", headers={"Content-Type": "audio/wav"}), {"text": "dictated"})
            stt.assert_called_once_with(b"RIFFaudio", mime="audio/wav")
            self.request("POST", "/stt", raw=b"text", headers={"Content-Type": "text/plain"}, status=400)
            self.request("POST", "/stt", raw=b"", headers={"Content-Type": "audio/webm"}, status=400)
        for audio, mime in ((b"RIFFwav", "audio/wav"), (b"MP3data", "audio/mpeg")):
            with patch.object(voice, "tts", return_value=audio) as tts:
                self.assertEqual(self.request("POST", "/say", {"text": "x" * 2500, "voice": "test"}), audio)
                self.assertEqual(self.headers["Content-Type"], mime)
                self.assertEqual(len(tts.call_args.args[0]), 2000)
        self.request("POST", "/say", {}, status=400)
        av = self.request("GET", "/avatars")
        self.assertIn("avatars", av)
        self.request("POST", "/avatar", {"slug": "missing"}, status=404)
        self.assertEqual(self.request("POST", "/avatar", {"slug": "jarvis"})["slug"], "jarvis")
        with patch.object(avatars, "svg", return_value='<svg xmlns="http://www.w3.org/2000/svg"/>'):
            self.assertIn(b"<svg", self.request("GET", "/avatar.svg"))
            self.assertIn("default-src 'none'", self.headers["Content-Security-Policy"])
        self.request("GET", "/avatar.svg?slug=missing", status=404)
        with patch.object(voice, "catalog", return_value=[{"name": "test", "backend": "fake", "kind": "builtin"}]), patch.object(voice, "_known", return_value=True):
            self.assertEqual(self.request("GET", "/voices")["voices"][0]["name"], "test")
            self.assertEqual(self.request("POST", "/voice", {"voice": "test"}), {"voice": "test"})
            self.request("POST", "/voice", {"voice": ""})
        with patch.object(voice, "_known", return_value=False):
            self.request("POST", "/voice", {"voice": "missing"}, status=404)
        self.assertTrue(self.request("POST", "/mute", {"muted": True})["muted"])
        self.request("POST", "/mute", {"muted": False})
        route_before = self.request("GET", "/route")
        thread = self.thread(P.CODEX)
        pinned = self.daemon._sessions[thread.id].brief
        self.assertIn("models", self.request("GET", "/models"))
        self.assertEqual(self.request("GET", "/models/catalog")["models"][0]["id"], "test/model")
        self.request("POST", "/models", {"add": "test/model"})
        selected = self.request("POST", "/model", {"model": "test/model"})
        self.assertEqual(selected["current"], "test/model")
        self.assertEqual(models.tier("orchestrator"), "test/model")
        self.assertEqual(self.daemon._sessions[thread.id].brief, pinned)
        self.assertEqual(self.request("GET", "/route")["table"], route_before["table"])
        self.request("POST", "/model", {"model": "missing"}, status=404)
        self.request("POST", "/models", {"add": "test/model", "remove": "test/model"}, status=400)
        self.request("POST", "/models", {"remove": "test/model"})
        self.assertNotEqual(models.selected(), "test/model")
        kinds = {e["kind"] for e in self.events_all()}
        self.assertTrue({"voice", "avatar", "model", "mute"} <= kinds)


class CronChecks(unittest.TestCase):
    def test_table(self):
        at = datetime(2026, 9, 15, 9, 30, tzinfo=ZONE)  # Tuesday
        table = [("* * * * *", True), ("*/15 * * * *", True), ("*/7 * * * *", False),
                 ("30 9 * * *", True), ("0 9 * * 1-5", False), ("30 9 * * 1-5", True),
                 ("0,30 8-10 * 9 2", True), ("31 9 * * *", False), ("30 8 * * *", False),
                 ("30 9 15 9 *", True), ("30 9 14 9 *", False), ("30 9 * 8 *", False),
                 ("30 9 * 9 0", False), ("30 9 * 9 7", False), ("30 9 * 9 1,2", True),
                 ("0-59/10 */3 * * *", True), ("30 9 14 * 2", True), ("30 9 15 * 1", True),
                 ("30 9 14 * 1", False), ("30 9 */2 * *", True), ("0-20,30-40 9 * * *", True),
                 ("30 0-23/2 * * *", False), ("30 9 1-31/2 1-12 0-7", True)]
        for expression, expected in table:
            with self.subTest(expression=expression):
                self.assertEqual(Cron(expression).matches(at), expected)
        for invalid in ("", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *",
                        "* * * 13 *", "* * * * 8", "*/0 * * * *", "* * * * 5-1", "x * * * *", "1,,2 * * * *"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                Cron(invalid)

    def test_dst_and_next(self):
        spring = datetime(2026, 3, 8, 0, tzinfo=ZONE).timestamp()
        next_230 = datetime.fromtimestamp(Cron("30 2 * * *").next(spring), ZONE)
        self.assertEqual((next_230.day, next_230.hour), (9, 2))
        fall = datetime(2026, 11, 1, 0, tzinfo=ZONE).timestamp()
        first = Cron("30 1 * * *").next(fall)
        second = datetime.fromtimestamp(Cron("30 1 * * *").next(first), ZONE)
        self.assertEqual(second.day, 2)  # one fire per repeated wall time
        self.assertEqual(datetime.fromtimestamp(Cron("*/15 * * * *").next(spring), ZONE).minute, 15)
        leap = datetime(2027, 3, 1, tzinfo=timezone.utc).timestamp()
        self.assertEqual(datetime.fromtimestamp(Cron("0 9 29 2 *").next(leap), ZONE).year, 2028)

    def test_codex_notification_adapter(self):
        report = {"primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1900000000}}
        native = SimpleNamespace(brief=SimpleNamespace(model=None), accounting=SimpleNamespace(usage=lambda: Usage(10, 4, 2)))
        handle = SessionHandle("12345678", P.CODEX, "codex:fake", native)
        events = list(CodexProvider()._notification(handle, "account/rateLimits/updated", {"rateLimits": report}))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, K.USAGE)
        self.assertEqual(events[0].data["provider_reported"]["rate_limits"], report)
        self.assertEqual(events[0].data["input"], 10)

    def test_hud_cli(self):
        from jarvis.__main__ import cmd_hud
        with patch("jarvis.v2.daemon.is_running", return_value=True), patch("jarvis.face.server.launch_window") as launch:
            self.assertEqual(cmd_hud(SimpleNamespace(no_window=True)), 0)
            launch.assert_not_called()
            self.assertEqual(cmd_hud(SimpleNamespace(no_window=False)), 0)
            launch.assert_called_once_with(f"http://localhost:{config.FACE_PORT}/")
        with patch("jarvis.v2.daemon.is_running", side_effect=[False, True]), patch("subprocess.Popen") as child:
            self.assertEqual(cmd_hud(SimpleNamespace(no_window=True)), 0)
            self.assertIn("daemon2", child.call_args.args[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
