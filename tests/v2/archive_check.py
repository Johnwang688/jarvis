"""Projects: rename, edit, archive, restore, permanent delete (decisions part B).

Free and hermetic: real loopback listeners, real Git in temp repos, fake
brains, and a trash rooted in a temp directory with a Recycle Bin stub that
fails the test if it is ever reached for a Linux path. Nothing here reads or
writes the owner's ~/.local/share (`XDG_DATA_HOME` is pointed at a temp dir
for the whole run, and every daemon gets an injected Trash besides).

Each case is written to fail against `main` before this feature: the routes
did not exist (404, not 403/409), `Task.root` did not exist, names were not
numbered, and archived projects did not exist to be hidden.
"""
from __future__ import annotations

import http.client
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import config  # noqa: E402
from jarvis.v2 import projects as P, trash as T, worktrees  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.model import ProviderName as PN, Role, TaskState  # noqa: E402
from jarvis.v2.provider import Brief, Decision, Event, EventKind as K, SessionHandle, Usage  # noqa: E402
from jarvis.v2.router import Router  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402

# One table, mirrored in hud/src/lib/projects.test.ts: the window's preview of
# a numbered name and the backend's decision must agree.
NAME_TABLE = [
    ("site", [], "site"),
    ("site", ["site"], "site (1)"),
    ("  Site  ", ["site"], "Site (1)"),
    ("SITE", ["site", "site (1)"], "SITE (2)"),
    ("site", ["site", "site (2)"], "site (1)"),
    ("site (1)", ["site", "site (1)"], "site (2)"),
    ("e2e-calc", ["E2E-CALC "], "e2e-calc (1)"),
    ("Inbox", ["Inbox"], "Inbox (1)"),
    ("new", ["old"], "new"),
]


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test")
    (path / "README").write_text("base\n")
    git(path, "add", "README")
    git(path, "commit", "-qm", "base")
    return path


def eventually(predicate, timeout=4):
    until = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > until:
            raise AssertionError("condition did not settle")
        time.sleep(0.005)


class Fake:
    """A brain that answers at once, or waits on `hold` when it is set."""

    def __init__(self, name):
        self.name = name
        self.hold: threading.Event | None = None

    def health(self):
        return True, "fake"

    def start(self, thread, brief, permit):
        return SessionHandle(thread.id, self.name, "fake:" + thread.id, SimpleNamespace(brief=brief))

    resume = start

    def usage(self, handle):
        return Usage()

    def send(self, handle, message):
        if self.hold is not None:
            self.hold.wait(5)
        yield Event(K.TEXT, handle.thread_id, {"text": "ok"})
        yield Event(K.USAGE, handle.thread_id, dict(input=1, output=1, cached=0, cost_usd=0.001))
        yield Event(K.TURN_FINISHED, handle.thread_id, {"stop": "end"})

    def interrupt(self, handle):
        pass

    def close(self, handle):
        pass


class FakeControl:
    def __init__(self, stores):
        self.stores = stores

    def start(self, task_id):
        return self.stores.tasks.transition(task_id, TaskState.CLARIFYING)


def _never_recycle(path):
    raise AssertionError(f"the Recycle Bin was reached for {path}")


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "xdg"),
                                      "JARVIS_CLAUDE_CREDENTIALS": str(self.root / "none.json")})
        env.start()
        self.addCleanup(env.stop)
        for name, value in dict(V2_DATA_DIR=self.root / "data", ALLOWLIST_PATH=self.root / "allow.json",
                                MODELS_PATH=self.root / "models.json").items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        self.repo_a = repo(self.root / "a")
        self.repo_b = repo(self.root / "b")
        self.stores = Stores()
        self.providers = {name: Fake(name) for name in PN}
        self.daemon = Daemon(self.stores, self.providers, lambda *_: lambda *_: Decision.DENY, 0)
        self.daemon.runner = FakeControl(self.stores)
        self.recycled = []
        self.daemon.trash = T.Trash(self.stores.root, root=self.root / "Trash", recycle=_never_recycle)
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        self.project = self.stores.projects.create("Calc", str(self.repo_a))
        self.other = self.stores.projects.create("Other", str(self.repo_b))
        self.events = self.daemon.bus.subscribe()

    # -- wire helpers ---------------------------------------------------------

    def request(self, method, path, body=None, status=200, *, owner=False, port=None, origin=None):
        port = port or (self.daemon.face_port if owner else self.daemon.port)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if owner:
            headers["Origin"] = origin or f"http://127.0.0.1:{port}"
        elif origin:
            headers["Origin"] = origin
        try:
            conn.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
            response = conn.getresponse()
            content = response.read()
            self.assertEqual(response.status, status, content[:400])
            return json.loads(content)
        finally:
            conn.close()

    def owner(self, method, path, body=None, status=200):
        return self.request(method, path, body if body is not None else ({} if method == "POST" else None),
                            status, owner=True)

    def drain(self):
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except Exception:
                return out

    def kinds(self):
        return [e["kind"] for e in self.drain()]

    def chat(self, project=None, title=""):
        project = project or self.project
        thread = self.daemon.open_thread(project.id, Role.CHAT, PN.FAST, Brief(Role.CHAT, project.root))
        if title:
            stored = self.stores.threads.get(thread.id)
            stored.title = title
            self.stores.threads.save(stored)
            self.daemon._sessions[thread.id].thread.title = title
        return thread

    def task(self, project=None, state=TaskState.DONE, worktree=True):
        project = project or self.project
        task = self.stores.tasks.create(project.id, "add multiply")
        self.stores.tasks.save(task)
        if worktree:
            task = worktrees.ensure(task, project, self.stores)
        path = {TaskState.DONE: [TaskState.CLARIFYING, TaskState.PLANNED, TaskState.RUNNING,
                                 TaskState.VERIFYING, TaskState.DONE],
                TaskState.BLOCKED: [TaskState.CLARIFYING, TaskState.BLOCKED],
                TaskState.CANCELLED: [TaskState.CANCELLED],
                TaskState.INTAKE: []}[state]
        for step in path:
            task = self.stores.tasks.transition(task.id, step)
        return task

    def impact(self, project=None):
        return self.request("GET", f"/projects/{(project or self.project).id}/impact")

    def archive(self, project=None, status=200):
        project = project or self.project
        token = self.impact(project)["token"]
        return self.owner("POST", f"/projects/{project.id}/archive?expect={token}", status=status)


class Names(Base):
    def test_unique_name_table(self):
        for wanted, taken, expected in NAME_TABLE:
            with self.subTest(wanted=wanted, taken=taken):
                self.assertEqual(P.unique_name(wanted, taken), expected)

    def test_create_and_edit_number_collisions_and_publish(self):
        made = self.request("POST", "/projects", {"name": " calc ", "root": str(self.repo_b)}, 201)
        self.assertEqual(made["name"], "calc (1)")
        self.assertEqual(self.request("POST", "/projects", {"name": "CALC", "root": str(self.repo_b)}, 201)["name"],
                         "CALC (2)")
        # The Inbox's name is reserved even before the Inbox exists.
        self.assertEqual(self.request("POST", "/projects", {"name": "inbox", "root": str(self.repo_b)}, 201)["name"],
                         "inbox (1)")
        self.request("POST", "/projects", {"name": "x", "root": str(self.root / "missing")}, 400)
        created = [e for e in self.drain() if e["kind"] == "project_created"]
        self.assertEqual(len(created), 3)
        self.assertEqual(created[0]["project_id"], made["id"])

        url = f"/projects/{self.other.id}"
        renamed = self.request("PATCH", url, {"name": "  calc  "})
        self.assertEqual(renamed["name"], "calc (3)")
        updated = [e for e in self.drain() if e["kind"] == "project_updated"]
        self.assertEqual(updated[-1]["data"]["changed"], ["name"])
        # A no-change PATCH answers and publishes nothing.
        self.request("PATCH", url, {"name": "calc (3)"})
        self.assertNotIn("project_updated", self.kinds())
        # Archived names still count: a new project cannot take one.
        self.archive(self.project)
        self.assertEqual(self.request("POST", "/projects", {"name": "Calc", "root": str(self.repo_b)}, 201)["name"],
                         "Calc (4)")

    def test_existing_duplicates_stay_editable(self):
        twin = self.stores.projects.create("Calc", str(self.repo_b))      # the owner's e2e-calc pair
        self.assertEqual(self.request("PATCH", f"/projects/{twin.id}", {"profile": "ask"})["name"], "Calc")
        self.assertEqual(self.request("PATCH", f"/projects/{twin.id}", {"name": "calc"})["name"], "calc")
        self.assertEqual(self.request("PATCH", f"/projects/{twin.id}", {"name": "Fresh"})["name"], "Fresh")

    def test_thread_rename_numbers_within_project_and_survives_a_turn(self):
        first, second = self.chat(), self.chat()
        elsewhere = self.chat(self.other, title="plan")
        self.assertEqual(self.request("PATCH", f"/threads/{first.id}", {"title": " plan "})["title"], "plan")
        self.assertEqual(self.request("PATCH", f"/threads/{second.id}", {"title": "Plan"})["title"], "Plan (1)")
        self.assertEqual(self.stores.threads.get(elsewhere.id).title, "plan")   # other project untouched
        updated = [e for e in self.drain() if e["kind"] == "thread_updated"]
        self.assertEqual([e["data"]["title"] for e in updated], ["plan", "Plan (1)"])
        # The live session writes its own Thread at the end of a turn; the
        # rename must survive that save.
        self.request("POST", f"/threads/{first.id}/send", {"text": "hi"}, 202)
        eventually(lambda: self.daemon._sessions[first.id].worker is None)
        self.assertEqual(self.stores.threads.get(first.id).title, "plan")
        self.request("PATCH", f"/threads/{first.id}", {"title": "  "}, 400)
        # Rename and move are separate requests (the move route is unchanged).
        self.request("PATCH", f"/threads/{first.id}", {"title": "a", "project_id": self.other.id}, 400)


class RootChange(Base):
    def test_task_pins_its_root_through_a_root_change(self):
        thread = self.chat()
        task = self.task(state=TaskState.BLOCKED)                # started, unfinished
        self.assertEqual(task.root, str(self.repo_a))
        self.assertTrue(Path(task.worktree).is_relative_to(self.repo_a))
        self.request("PATCH", f"/projects/{self.project.id}", {"root": str(self.root / "nope")}, 400)
        moved = self.request("PATCH", f"/projects/{self.project.id}", {"root": str(self.repo_b)})
        self.assertEqual(moved["root"], str(self.repo_b))
        # The existing thread keeps its folder; a new one opens in the new root.
        self.assertEqual(self.request("GET", "/threads")[0]["cwd"], str(self.repo_a))
        fresh = self.chat(self.stores.projects.get(self.project.id))
        self.assertEqual(self.stores.threads.get(fresh.id).cwd, str(self.repo_b))
        self.assertEqual(self.stores.threads.get(thread.id).cwd, str(self.repo_a))
        # The task still works, commits and is removed in the original repo.
        task = self.stores.tasks.get(task.id)
        again = worktrees.ensure(task, self.stores.projects.get(self.project.id), self.stores)
        self.assertEqual(again.worktree, task.worktree)
        work = Path(task.worktree)
        (work / "multiply.py").write_text("def multiply(a, b):\n    return a * b\n")
        git(work, "add", "multiply.py")
        git(work, "commit", "-qm", "multiply")
        git(self.repo_a, "merge", "-q", task.branch)
        self.assertIn(task.branch, git(self.repo_a, "branch"))
        worktrees.remove(self.stores.tasks.get(task.id), self.stores)
        self.assertFalse(work.exists())
        self.assertNotIn(task.branch, git(self.repo_a, "branch"))
        self.assertTrue((self.repo_a / "multiply.py").exists())
        self.assertFalse((self.repo_b / ".jarvis").exists())          # nothing went to the new root

    def test_a_legacy_task_is_pinned_before_the_root_moves(self):
        task = self.task(state=TaskState.BLOCKED)
        task.root = None                                         # recorded before the field existed
        self.stores.tasks.save(task)
        not_started = self.task(state=TaskState.INTAKE, worktree=False)
        impact = self.impact()
        self.assertEqual({t["id"] for t in impact["on_root"]["tasks"]}, {task.id, not_started.id})
        self.request("PATCH", f"/projects/{self.project.id}", {"root": str(self.repo_b)})
        self.assertEqual(self.stores.tasks.get(task.id).root, str(self.repo_a))
        # A task that has not started yet starts under the new root.
        self.assertIsNone(self.stores.tasks.get(not_started.id).root)
        started = worktrees.ensure(self.stores.tasks.get(not_started.id),
                                   self.stores.projects.get(self.project.id), self.stores)
        self.assertEqual(started.root, str(self.repo_b))


class Archive(Base):
    def test_archive_hides_pauses_and_refuses_new_work(self):
        thread = self.chat(title="desk")
        done = self.task()
        schedule = self.request("POST", "/schedules", dict(project_id=self.project.id, brief="nightly",
                                                            every_s=60), 201)
        projects_before = len(self.stores.projects.list())
        impact = self.impact()
        self.assertEqual(impact["chat_threads"]["count"], 1)
        self.assertEqual(impact["tasks"], {"total": 1, "finished": 1, "active": []})
        self.assertEqual([w["task_id"] for w in impact["worktrees"]], [done.id])
        self.assertEqual(len(self.stores.projects.list()), projects_before)   # /impact made no Inbox
        url = f"/projects/{self.project.id}/archive"
        self.owner("POST", url, status=400)                                  # expect is required
        self.owner("POST", url + "?expect=stale", status=409)
        # Not from a tool's client: the API listener, or no Origin, is refused.
        self.request("POST", url + f"?expect={impact['token']}", {}, 403)
        self.request("POST", url + f"?expect={impact['token']}", {}, 403, port=self.daemon.face_port)
        self.request("POST", url + f"?expect={impact['token']}", {}, 403, owner=True,
                     origin="http://127.0.0.1:1")
        result = self.owner("POST", url + f"?expect={impact['token']}")
        self.assertEqual(result["paused_schedules"], [schedule["id"]])
        self.assertNotIn(thread.id, self.daemon._sessions)                   # idle session closed
        self.assertIn("project_archived", self.kinds())
        # Hidden from every list and every placement …
        self.assertNotIn(self.project.id, [p["id"] for p in self.request("GET", "/projects")])
        self.assertEqual(self.request("GET", "/threads"), [])
        self.assertEqual(self.request("GET", "/tasks"), [])
        self.assertEqual(self.request("GET", "/schedules"), [])
        router = Router(self.stores)
        self.assertIsNone(router.place("calc"))
        self.assertIsNone(router.place(None, SimpleNamespace(thread_id=thread.id, project_id=None, surface="")))
        # … and refuses new work, while every record is kept.
        self.request("POST", f"/threads/{thread.id}/send", {"text": "hello?"}, 409)
        self.request("POST", "/threads", {"project_id": self.project.id, "role": "chat"}, 409)
        self.request("POST", "/tasks", {"project_id": self.project.id, "brief": "more"}, 409)
        self.request("PATCH", f"/projects/{self.project.id}", {"profile": "ask"}, 409)
        self.request("PATCH", f"/threads/{thread.id}", {"project_id": self.other.id}, 409)
        self.request("PATCH", f"/threads/{self.chat(self.other).id}", {"project_id": self.project.id}, 409)
        saved = self.daemon.schedules.get(schedule["id"])
        self.assertFalse(saved["enabled"])
        self.assertIsNone(self.daemon.schedules.fire(schedule["id"]))
        self.request("POST", f"/schedules/{schedule['id']}/run-now", {}, 409)
        self.assertIsNotNone(self.stores.threads.get(thread.id))
        self.assertEqual(self.stores.tasks.get(done.id).state, TaskState.DONE)
        view = self.request("GET", "/archive")
        self.assertEqual([p["id"] for p in view["projects"]], [self.project.id])
        self.assertEqual([t["id"] for t in view["projects"][0]["threads"]], [thread.id])
        self.assertEqual(self.request("GET", f"/threads/{thread.id}/transcript")["messages"], [])
        self.owner("POST", url + f"?expect={impact['token']}", status=409)    # already archived

        # Restore: visible again, schedule running again.
        restored = self.owner("POST", f"/projects/{self.project.id}/restore")
        self.assertIsNone(restored["archived"])
        self.assertIn(self.project.id, [p["id"] for p in self.request("GET", "/projects")])
        self.assertTrue(self.daemon.schedules.get(schedule["id"])["enabled"])
        self.assertNotIn("paused_by_archive", self.daemon.schedules.get(schedule["id"]))
        self.request("POST", f"/threads/{thread.id}/send", {"text": "back"}, 202)
        self.assertIn("project_restored", self.kinds())

    def test_restore_numbers_a_name_taken_meanwhile(self):
        self.archive()
        clash = self.stores.projects.get(self.other.id)
        clash.name = "calc"                                      # written behind the API's back
        self.stores.projects.save(clash)
        self.assertEqual(self.owner("POST", f"/projects/{self.project.id}/restore")["name"], "Calc (1)")

    def test_refused_while_work_is_unfinished_or_a_turn_runs(self):
        self.owner("POST", f"/projects/{self.stores.projects.inbox().id}/archive?expect=x", status=409)
        blocked = self.task(state=TaskState.BLOCKED)
        impact = self.impact()
        self.assertEqual([t["id"] for t in impact["tasks"]["active"]], [blocked.id])
        refused = self.archive(status=409)
        self.assertIn(blocked.id, refused["error"])
        self.stores.tasks.transition(blocked.id, TaskState.CANCELLED)
        thread = self.chat(title="busy one")
        hold = threading.Event()
        self.providers[PN.FAST].hold = hold
        self.request("POST", f"/threads/{thread.id}/send", {"text": "long"}, 202)
        try:
            self.assertEqual(self.impact()["running_turns"][0]["title"], "busy one")
            refused = self.archive(status=409)
            self.assertIn("busy one", refused["error"])
        finally:
            hold.set()
        eventually(lambda: self.daemon._sessions[thread.id].worker is None)
        self.archive()


class Threads(Base):
    def test_thread_archive_restore_and_delete_through_the_trash(self):
        thread = self.chat(title="scratch")
        self.request("POST", f"/threads/{thread.id}/send", {"text": "keep me"}, 202)
        eventually(lambda: self.daemon._sessions[thread.id].worker is None)
        task = self.task(worktree=False)
        worker = self.daemon.open_thread(self.project.id, Role.IMPLEMENTER, PN.FAST,
                                         Brief(Role.IMPLEMENTER, str(self.repo_a), task_id=task.id))
        self.owner("POST", f"/threads/{worker.id}/archive", status=409)          # task threads go with the task
        self.owner("DELETE", f"/threads/{thread.id}", status=409)                # not archived yet
        self.request("POST", f"/threads/{thread.id}/archive", {}, 403)          # not from a tool's client
        self.owner("POST", f"/threads/{thread.id}/archive")
        self.assertNotIn(thread.id, [t["id"] for t in self.request("GET", "/threads")])
        self.assertEqual([t["id"] for t in self.request("GET", "/archive")["threads"]], [thread.id])
        self.request("POST", f"/threads/{thread.id}/send", {"text": "no"}, 409)
        self.request("PATCH", f"/threads/{thread.id}", {"title": "no"}, 409)
        # An archived thread's title still counts within its project …
        other = self.chat()
        self.assertEqual(self.request("PATCH", f"/threads/{other.id}", {"title": "scratch"})["title"],
                         "scratch (1)")
        # … and restore renumbers it if the name was taken behind the API's back.
        self.daemon.close_thread(other.id)
        clash = self.stores.threads.get(other.id)
        clash.title = "SCRATCH"
        self.stores.threads.save(clash)
        self.assertEqual(self.owner("POST", f"/threads/{thread.id}/restore")["title"], "scratch (1)")
        self.owner("POST", f"/threads/{thread.id}/archive")
        folder = self.stores.threads.path(thread.id).parent
        result = self.owner("DELETE", f"/threads/{thread.id}")
        self.assertEqual(result["trash"]["where"], "linux")
        self.assertIsNone(self.stores.threads.get(thread.id))
        self.assertFalse(folder.exists())
        entries = self.daemon.trash.entries()
        self.assertEqual(len(entries), 1)
        trashed = self.root / "Trash" / "files" / entries[0]["name"]
        self.assertIn("keep me", (trashed / "threads" / thread.id / "log.jsonl").read_text())
        self.assertTrue(entries[0]["path"].startswith(str(self.stores.root)))
        self.assertIn("thread_deleted", self.kinds())
        self.assertFalse((self.root / "xdg" / "Trash").exists())               # only the injected trash


class Delete(Base):
    def test_delete_moves_records_to_the_trash_and_leaves_disk_alone(self):
        (self.repo_a / "sentinel.txt").write_text("the owner's file\n")
        thread = self.chat(title="history")
        done = self.task()
        self.request("POST", "/schedules", dict(project_id=self.project.id, brief="n", every_s=60), 201)
        self.owner("DELETE", f"/projects/{self.project.id}?expect={self.impact()['token']}", status=409)
        self.archive()
        impact = self.impact()
        self.owner("DELETE", f"/projects/{self.project.id}", status=400)
        self.owner("DELETE", f"/projects/{self.project.id}?expect=stale", status=409)
        self.request("DELETE", f"/projects/{self.project.id}?expect={impact['token']}", None, 403)
        result = self.owner("DELETE", f"/projects/{self.project.id}?expect={impact['token']}")
        self.assertEqual(result["removed"], {"threads": 1, "tasks": 1, "schedules": 1})
        self.assertEqual(result["left_on_disk"], [done.worktree])
        self.assertEqual(result["trash"]["where"], "linux")
        # Every record is gone from the stores …
        self.assertIsNone(self.stores.projects.get(self.project.id))
        self.assertIsNone(self.stores.threads.get(thread.id))
        self.assertIsNone(self.stores.tasks.get(done.id))
        self.assertEqual(self.daemon.schedules.list(), [])
        # … the flat project store kept its other records (the rmtree trap) …
        self.assertIsNotNone(self.stores.projects.get(self.other.id))
        # … nothing of the owner's was touched …
        self.assertTrue((self.repo_a / "sentinel.txt").exists())
        self.assertTrue(Path(done.worktree).is_dir())
        self.assertIn(done.branch, git(self.repo_a, "branch"))
        # … and everything is in the trash, as one entry with a manifest.
        entries = self.daemon.trash.entries()
        self.assertEqual(len(entries), 1)
        bundle = self.root / "Trash" / "files" / entries[0]["name"]
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertEqual(manifest["project"]["id"], self.project.id)
        self.assertTrue((bundle / "projects" / f"{self.project.id}.json").exists())
        self.assertTrue((bundle / "threads" / thread.id / "thread.json").exists())
        self.assertTrue((bundle / "tasks" / done.id / "journal.jsonl").exists())
        info = (self.root / "Trash" / "info" / (entries[0]["name"] + ".trashinfo")).read_text()
        self.assertTrue(info.startswith("[Trash Info]\nPath=/"))
        self.assertIn("project_deleted", self.kinds())
        self.assertFalse((self.stores.root / "deleting").exists() and any((self.stores.root / "deleting").iterdir()))

    def test_a_crash_left_staging_folder_goes_to_the_trash_later(self):
        stale = self.stores.root / "deleting" / "20260101T000000-project-deadbeef-abc123"
        (stale / "projects").mkdir(parents=True)
        (stale / "projects" / "deadbeef.json").write_text("{}")
        fresh = self.stores.root / "deleting" / "now-project-feedface-abc123"
        fresh.mkdir()
        old = time.time() - 3600
        os.utime(stale, (old, old))
        self.assertEqual(P.recover_staging(self.daemon), 1)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())                         # maybe a delete in progress
        self.assertEqual([e["name"] for e in self.daemon.trash.entries()], [stale.name])

    def test_a_windows_data_root_goes_to_the_recycle_bin(self):
        recycled = []

        def recycle(path):
            recycled.append(path)
            import shutil
            shutil.rmtree(path)                                   # what the Recycle Bin does to it here

        self.daemon.trash = T.Trash(self.stores.root, root=self.root / "Trash", recycle=recycle,
                                    is_windows=lambda p: True)
        thread = self.chat()
        self.owner("POST", f"/threads/{thread.id}/archive")
        result = self.owner("DELETE", f"/threads/{thread.id}")
        self.assertEqual(result["trash"]["where"], "windows")
        self.assertEqual(len(recycled), 1)
        self.assertTrue(recycled[0].is_relative_to(self.stores.root / "deleting"))
        self.assertFalse((self.root / "Trash").exists())


class TrashUnit(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.owned = self.root / "data"
        self.owned.mkdir()
        self.now = 1_800_000_000.0
        self.trash = T.Trash(self.owned, root=self.root / "Trash", recycle=_never_recycle,
                             clock=lambda: self.now, retention=30)

    def item(self, name, where=None):
        path = (where or self.owned) / name
        path.mkdir(parents=True)
        (path / "f").write_text(name)
        return path

    def test_windows_paths(self):
        self.assertTrue(T.is_windows_path("/mnt/c/Users/x"))
        self.assertTrue(T.is_windows_path("/mnt/d"))
        self.assertFalse(T.is_windows_path("/home/johnw/.local/share/jarvis"))
        self.assertFalse(T.is_windows_path("/mnt/wslg/x"))
        self.assertEqual(T.windows_path("/mnt/c/Users/o'neil/a b"), "C:\\Users\\o'neil\\a b")
        self.assertEqual(T.windows_path("/mnt/d"), "D:\\")
        # The default recycler builds a quoted literal, never interpolated code.
        with patch.object(T.shutil, "which", return_value=None):
            with self.assertRaises(T.TrashError):
                T.recycle_windows(Path("/mnt/c/never"))

    def test_put_names_info_and_collisions(self):
        a = self.trash.put(self.item("one"))
        b = self.trash.put(self.item("one"))
        self.assertEqual((a["name"], b["name"]), ("one", "one.2"))
        info = (self.root / "Trash" / "info" / "one.trashinfo").read_text()
        self.assertIn(f"Path={self.owned}/one\n", info)
        self.assertRegex(info, r"DeletionDate=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\n")
        with self.assertRaises(T.TrashError):
            self.trash.put(Path("relative"))

    def test_purge_and_empty_touch_only_jarvis_entries(self):
        self.trash.put(self.item("old"))
        foreign = self.trash.put(self.item("theirs", self.root / "desktop"))   # the owner's own file
        self.now += 10 * 86400
        self.trash.put(self.item("young"))
        self.assertEqual([e["name"] for e in self.trash.entries()], ["old", "young"])
        self.now += 21 * 86400                                   # old: 31 days, young: 21
        self.assertEqual(self.trash.purge(), 1)
        self.assertEqual([e["name"] for e in self.trash.entries()], ["young"])
        self.assertFalse((self.root / "Trash" / "files" / "old").exists())
        self.assertEqual(self.trash.empty(), 1)
        self.assertEqual(self.trash.entries(), [])
        self.assertTrue((self.root / "Trash" / "files" / foreign["name"]).exists())
        self.assertTrue((self.root / "Trash" / "info" / (foreign["name"] + ".trashinfo")).exists())

    def test_retention_setting(self):
        for raw, days in (("", 30.0), ("7", 7.0), ("0", 30.0), ("-1", 30.0), ("soon", 30.0)):
            with self.subTest(raw=raw), patch.dict(os.environ, {"JARVIS_TRASH_DAYS": raw}):
                self.assertEqual(T.retention_days(), days)

    def test_home_trash_follows_xdg(self):
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "x")}):
            self.assertEqual(T.home_trash(), self.root / "x" / "Trash")
            self.assertEqual(T.Trash(self.owned).root, self.root / "x" / "Trash")


class OwnerOnly(Base):
    """B11: only the owner, in the HUD, can archive or delete. No tool can."""

    FORBIDDEN_NAME = re.compile(r"archive|restore|trash|purge|(delete|remove|drop)_?(project|thread)", re.I)
    FORBIDDEN_SOURCE = ("/archive", "/restore", "/trash", "archive_project", "archive_thread",
                        "restore_project", "restore_thread", "delete_project", "delete_thread",
                        "projects.route", "trash.put", ".empty(")

    def test_no_tool_reaches_archive_or_delete(self):
        from jarvis import tools
        from jarvis.v2 import mcp
        from jarvis.v2.providers import fastpath
        import jarvis.v2.tools as v2tools
        import jarvis.v2.tools.propose as propose
        import jarvis.v2.tools.schedules as schedule_tools
        v2_names = {name for name, entry in tools.REGISTRY.items()
                    if getattr(getattr(entry, "func", None), "__module__", "").startswith("jarvis.v2.tools")}
        reachable = set(fastpath.FAST_TOOLS) | set(mcp.MCP_TOOLS) | v2_names
        self.assertTrue({"task_propose", "schedule_delete"} <= reachable, reachable)
        for name in sorted(reachable):
            self.assertIsNone(self.FORBIDDEN_NAME.search(name), name)
        # And nothing a tool runs names the routes or the functions behind them.
        sources = [v2tools, propose, schedule_tools, mcp, fastpath]
        for name in reachable:
            entry = tools.REGISTRY.get(name)
            func = getattr(entry, "func", None)
            if func is not None:
                sources.append(inspect.getmodule(func))
        from jarvis.v2.discord import gateway
        sources.append(gateway)                                  # Discord has no such verb either
        for module in {m for m in sources if m is not None}:
            text = inspect.getsource(module)
            for needle in self.FORBIDDEN_SOURCE:
                self.assertNotIn(needle, text, f"{module.__name__} names {needle}")

    def test_the_routes_refuse_every_client_but_the_hud(self):
        thread = self.chat()
        token = self.impact()["token"]
        attempts = [("POST", f"/projects/{self.project.id}/archive?expect={token}", {}),
                    ("POST", f"/projects/{self.project.id}/restore", {}),
                    ("DELETE", f"/projects/{self.project.id}?expect={token}", None),
                    ("POST", f"/threads/{thread.id}/archive", {}),
                    ("POST", f"/threads/{thread.id}/restore", {}),
                    ("DELETE", f"/threads/{thread.id}", None),
                    ("POST", "/trash/empty", {})]
        for method, path, body in attempts:
            with self.subTest(path=path):
                # The API listener is where every tool's HTTP client goes
                # (config.DAEMON_PORT); the HUD's listener without its Origin
                # is a script, not the window.
                self.request(method, path, body, 403)
                self.request(method, path, body, 403, port=self.daemon.face_port)
        self.assertIsNone(self.stores.projects.get(self.project.id).archived)


if __name__ == "__main__":
    unittest.main(verbosity=2)
