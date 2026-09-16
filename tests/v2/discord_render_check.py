"""Offline Discord rendering checks; every REST request uses a fake transport."""
from __future__ import annotations

from copy import deepcopy
import json
import logging
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from jarvis import config
from jarvis.tools import discord as v1
from jarvis.v2.bus import EventBus
from jarvis.v2.discord import DiscordError, DiscordRest, Reporter
from jarvis.v2.discord.render import approval_text, milestone, report_text, status_embed
from jarvis.v2.model import (Project, ProviderName, Report, Role, RoutingDecision,
                             Status, Task, TaskState, to_json)
from jarvis.v2.stores import Stores

TOKEN = "synthetic-discord-secret-never-in-content"


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.responses = []

    def __call__(self, method, url, headers, timeout, **kwargs):
        assert headers == {"Authorization": f"Bot {TOKEN}"}
        assert timeout == 30
        assert TOKEN not in repr(kwargs)
        call = {"method": method, "path": url.removeprefix(config.DISCORD_API),
                "at": time.monotonic(), **deepcopy(kwargs)}
        self.calls.append(call)
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return httpx.Response(200, json={"id": str(len(self.calls))})


def payload(call):
    return call["json"] if "json" in call else json.loads(call["data"]["payload_json"])


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("background operation did not finish")
        time.sleep(0.005)


class DiscordChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        token_path = Path(self.tmp.name) / "discord_token.json"
        token_path.write_text(json.dumps({"bot_token": TOKEN, "owner_id": "owner"}))
        self.bundle = patch.object(config, "DISCORD_TOKEN_PATH", token_path)
        self.bundle.start()
        self.addCleanup(self.bundle.stop)
        # Accidental use of the default client must never reach the network.
        self.network = patch.object(httpx, "request", side_effect=AssertionError("live network"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.fake = FakeTransport()
        self.rest = DiscordRest(self.fake)
        self.addCleanup(self.rest.close)

    def test_exact_rest_endpoints(self):
        embed = {"title": "Status"}
        self.assertEqual(self.rest.create_channel("g", "project"), "1")
        self.assertEqual(self.rest.create_thread("c", "task"), "2")
        self.assertEqual(self.rest.post("t", "hi", embed), "3")
        self.rest.edit("t", "m", "bye", embed)
        self.rest.unarchive("t")
        expected = [
            ("POST", "/guilds/g/channels", {"name": "project", "type": 0}),
            ("POST", "/channels/c/threads", {"name": "task", "type": 11}),
            ("POST", "/channels/t/messages", {"content": "hi", "embeds": [embed],
                                               "allowed_mentions": {"parse": []}}),
            ("PATCH", "/channels/t/messages/m", {"content": "bye", "embeds": [embed],
                                                  "allowed_mentions": {"parse": []}}),
            ("PATCH", "/channels/t", {"archived": False}),
        ]
        self.assertEqual([(c["method"], c["path"], payload(c)) for c in self.fake.calls], expected)
        self.rest.edit("t", "m", embed=embed)
        self.assertNotIn("content", payload(self.fake.calls[-1]))

    def test_multipart_and_retry_preserve_file(self):
        self.fake.responses = [httpx.Response(429, json={"retry_after": 0.125}),
                               httpx.Response(200, json={"id": "message"})]
        with patch("jarvis.v2.discord.rest.time.sleep") as sleep:
            self.assertEqual(self.rest.post("t", "diff", files=(("diff.txt", "λ\n+code"),)), "message")
        sleep.assert_called_once_with(0.125)
        for call in self.fake.calls:
            self.assertEqual((call["method"], call["path"]), ("POST", "/channels/t/messages"))
            self.assertEqual(payload(call), {"content": "diff", "allowed_mentions": {"parse": []},
                                             "attachments": [{"id": 0, "filename": "diff.txt"}]})
            self.assertEqual(call["files"], [("files[0]", ("diff.txt", "λ\n+code".encode(),
                                                           "text/plain; charset=utf-8"))])

    def test_retry_header_and_fail_text(self):
        self.fake.responses = [httpx.Response(429, json={}, headers={"Retry-After": "0.25"})]
        with patch("jarvis.v2.discord.rest.time.sleep") as sleep:
            self.rest.unarchive("t")
        sleep.assert_called_once_with(0.25)
        for status in (400, 403, 500):
            response = httpx.Response(status, json={"message": "no"})
            self.fake.responses = [response]
            with self.assertRaises(DiscordError) as caught:
                self.rest.post("t", "hi")
            self.assertEqual(str(caught.exception), v1._fail(response))
        self.fake.responses = [httpx.Response(429, json={"retry_after": "nan"})]
        with self.assertRaises(DiscordError):
            self.rest.unarchive("t")

    def test_transport_and_http_errors_do_not_expose_auth(self):
        for failure in (httpx.ConnectError(TOKEN), httpx.Response(403, json={"message": TOKEN})):
            self.fake.responses = [failure]
            with self.assertRaises(DiscordError) as caught:
                self.rest.post("t", "hello")
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_httpx_mock_transport_multipart(self):
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"id": "wire"})
        rest = DiscordRest(httpx.MockTransport(handle))
        self.addCleanup(rest.close)
        self.assertEqual(rest.post("t", files=(("a.txt", "full body"),)), "wire")
        request = requests[0]
        self.assertEqual(request.method, "POST")
        self.assertTrue(str(request.url).endswith("/channels/t/messages"))
        self.assertIn("multipart/form-data; boundary=", request.headers["Content-Type"])
        self.assertIn(b'name="payload_json"', request.content)
        self.assertIn(b'name="files[0]"; filename="a.txt"', request.content)
        self.assertIn(b"full body", request.content)
        self.assertNotIn(TOKEN.encode(), request.content)

    def test_runner_status_only(self):
        project = Project("project", "Jarvis", "/tmp")
        task = Task("task", project.id, "untrusted brief", status=Status(
            phase=TaskState.RUNNING, step=2, steps=7, elapsed_s=3661, cost_usd=1.25,
            tokens=1234, last_tool="edit_file", last_file="app.py", open_question="Which target?",
            routing=[RoutingDecision(Role.ORCHESTRATOR, ProviderName.CLAUDE, "project table"),
                     RoutingDecision(Role.IMPLEMENTER, ProviderName.CODEX, "claude over threshold")]))
        expected = {"Phase": "running", "Step": "2/7", "Elapsed": "01:01:01",
                    "Cost": "$1.2500 · 1,234 tokens", "Last action": "edit_file · app.py",
                    "Open question": "Which target?", "Routing":
                    "orchestrator: claude (project table) · implementer: codex (claude over threshold)"}
        before = status_embed(task, project)
        self.assertEqual(before["title"], "Jarvis · Task task")
        self.assertEqual({f["name"]: f["value"] for f in before["fields"]}, expected)
        task.spec.goal = "THE MODEL CLAIMS DONE " * 1000
        task.brief = "changed"
        task.plan = ["changed"]
        task.report = Report(done=["changed"])
        task.state = TaskState.FAILED
        self.assertEqual(before, status_embed(task, project))

    def test_render_limits(self):
        long = "x" * 5000
        task = Task("task", "project", long, status=Status(
            last_tool=long, last_file=long, open_question=long,
            routing=[RoutingDecision(Role.IMPLEMENTER, ProviderName.CODEX, long)]))
        for kind in ("started", "question", "approval", "blocked", "verified", "done", "failed"):
            result = milestone(kind, task, text=long, tool=long, code=long, options=[long])
            self.assertEqual(len(result), 400, kind)
            self.assertTrue(result.endswith("…"), kind)
        embed = status_embed(task, Project("project", long, "/tmp"))
        self.assertLessEqual(len(embed["title"]), 256)
        self.assertLessEqual(len(embed.get("description", "")), 4096)
        self.assertLessEqual(len(embed["fields"]), 25)
        for field in embed["fields"]:
            self.assertLessEqual(len(field["value"]), 1024)
        self.assertLessEqual(len(embed["title"]) + sum(len(f["name"]) + len(f["value"])
                                                     for f in embed["fields"]), 6000)

    def test_report_boundary_and_overflow(self):
        report = Report(done=["a"], changed=["app.py"], verified="reviewer abc: pass",
                        open=["none"], next="ship", cost={"codex": 1.25})
        short = report_text(report)
        self.assertIsInstance(short, str)
        self.assertIsNone(short.overflow)
        self.assertEqual([line.split()[0] for line in short.splitlines()],
                         ["DONE", "CHANGED", "VERIFIED", "OPEN", "NEXT", "COST"])
        report.done[0] += "x" * (1500 - len(short))
        self.assertEqual(len(report_text(report)), 1500)
        self.assertIsNone(report_text(report).overflow)
        report.done[0] += "tail"
        rendered = report_text(report)
        self.assertEqual(len(rendered), 1500)
        self.assertTrue(rendered.endswith("…"))
        self.assertIn("tail", rendered.overflow)
        self.rest.post("t", rendered)
        call = self.fake.calls[-1]
        self.assertEqual(payload(call)["content"], rendered)
        self.assertEqual(call["files"][0][1][1].decode(), rendered.overflow)

    def test_full_approval_attached(self):
        command = "printf '" + "x" * 5000 + "'\nsecond command"
        rendered = approval_text("shell", {"command": command, "cwd": "/tmp"}, "C42", "task abc")
        self.assertIn(command, rendered)
        self.assertIn("yes/no C42", rendered)
        self.assertIn("Origin: task abc", rendered)
        self.rest.post("t", rendered)
        call = self.fake.calls[-1]
        self.assertLessEqual(len(payload(call)["content"]), 2000)
        self.assertEqual(call["files"][0][1][1].decode(), rendered)
        short = approval_text("shell", {"command": "pwd"}, "C43", "task abc")
        self.rest.post("t", short)
        self.assertEqual(payload(self.fake.calls[-1])["content"], short)
        self.assertNotIn("files", self.fake.calls[-1])

    def reporter(self, channel="project-channel"):
        self.stores = Stores(Path(self.tmp.name) / "stores")
        self.project = self.stores.projects.create("Jarvis", "/tmp", discord_channel_id=channel)
        self.task = self.stores.tasks.create(self.project.id, "Implement task")
        self.stores.tasks.save(self.task)
        self.bus = EventBus()
        self.rep = Reporter(self.stores, self.rest, self.bus)
        self.addCleanup(self.rep.close)
        return self.rep

    def publish(self, kind="task_created", task=None):
        task = task or self.task
        self.bus.publish({"kind": kind, "task_id": task.id, "project_id": task.project_id,
                          "data": to_json(task)})

    def drained(self):
        wait_for(lambda: self.rep.subscription.unfinished_tasks == 0)

    def posts(self):
        return [c for c in self.fake.calls if c["method"] == "POST" and c["path"].endswith("/messages")]

    def edits(self):
        return [c for c in self.fake.calls if c["method"] == "PATCH" and "/messages/" in c["path"]]

    def test_created_once_sidecar_and_restart_adoption(self):
        self.reporter()
        self.publish()
        self.drained()
        task = self.stores.tasks.get(self.task.id)
        self.assertIsNotNone(task.discord_thread_id)
        sidecar_path = self.stores.tasks.path(task.id).with_name("discord.json")
        sidecar = json.loads(sidecar_path.read_text())
        self.assertEqual(sidecar["discord_status_message_id"], "3")
        self.assertNotIn("discord_status_message_id", to_json(task))
        self.publish()
        self.drained()
        self.rep.close()
        self.rep = Reporter(self.stores, self.rest, self.bus)
        self.addCleanup(self.rep.close)
        self.publish()
        self.drained()
        self.assertEqual(len([c for c in self.fake.calls if c["path"].endswith("/threads")]), 1)
        self.assertEqual(len(self.posts()), 2)

    def test_adopt_existing_thread(self):
        self.reporter()
        self.task.discord_thread_id = "existing-thread"
        self.stores.tasks.save(self.task)
        self.publish()
        self.drained()
        self.assertEqual(len(self.posts()), 2)
        self.assertTrue(all(c["path"] == "/channels/existing-thread/messages" for c in self.fake.calls))

    def test_five_changes_one_second_last_state_timer(self):
        self.reporter()
        self.publish()
        self.drained()
        started = time.monotonic()
        for step in range(1, 6):
            self.task.status.step = step
            self.publish("task_status_changed")
            time.sleep(0.15)
        self.drained()
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.edits(), [])
        wait_for(lambda: self.rep.counters["edits"] == 1, timeout=6)
        self.assertEqual(len(self.edits()), 1)
        edit = self.edits()[0]
        self.assertGreaterEqual(edit["at"] - started, 5)
        self.assertEqual({f["name"]: f["value"] for f in payload(edit)["embeds"][0]["fields"]}["Step"], "5/0")
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(self.fake.calls[-2]["json"], {"archived": False})

    def test_same_channel_edit_spacing_and_independent_channels(self):
        # Accelerate the interval here; the preceding test checks the real 5 s.
        with patch("jarvis.v2.discord.reporter.EDIT_INTERVAL", 0.08):
            self.reporter()
            tasks = []
            for channel in ("shared", "shared", "other"):
                task = self.stores.tasks.create(self.project.id, "Another task", discord_thread_id=channel)
                self.stores.tasks.save(task)
                tasks.append(task)
                self.publish(task=task)
            self.drained()
            for task in tasks:
                task.status.step = 1
                self.publish("task_updated", task)
            self.drained()
            wait_for(lambda: self.rep.counters["edits"] == 3)
            shared = [c for c in self.edits() if "/shared/" in c["path"]]
            other = [c for c in self.edits() if "/other/" in c["path"]]
            self.assertEqual(len(shared), 2)
            self.assertGreaterEqual(shared[1]["at"] - shared[0]["at"], 0.08)
            self.assertLess(other[0]["at"], shared[1]["at"])

    def test_milestones_snapshots_and_report_attachment(self):
        self.reporter()
        self.publish()
        self.drained()
        # Queue every snapshot before consumption; latest disk state is irrelevant.
        for phase in (TaskState.CLARIFYING, TaskState.BLOCKED, TaskState.RUNNING,
                      TaskState.VERIFYING, TaskState.DONE):
            self.task.status.phase = phase
            if phase == TaskState.DONE:
                self.task.report = Report(done=["x" * 5000], verified="reviewer abc: passed")
            self.publish("task_status_changed")
        self.drained()
        contents = [payload(c).get("content", "") for c in self.posts()]
        self.assertEqual([s.split(":")[0] for s in contents[2:]], ["Blocked", "Verified", "Done"])
        done = self.posts()[-1]
        self.assertLessEqual(len(payload(done)["content"]), 2000)
        self.assertIn(b"x" * 5000, done["files"][0][1][1])
        self.publish("task_status_changed")
        self.drained()
        self.assertEqual(len(self.posts()), 5)

    def test_failed_and_question_no_provider_chatter(self):
        self.reporter()
        self.publish()
        self.drained()
        self.task.status.open_question = "Target environment?"
        self.publish("task_status_changed")
        self.task.status.phase = TaskState.FAILED
        self.publish("task_updated")
        for kind in ("tool_started", "tool_finished", "text_delta", "thinking", "text", "usage",
                     "approval_requested", "question"):
            self.bus.publish({"kind": kind, "task_id": self.task.id, "data": {"text": "noise"}})
        self.drained()
        contents = [payload(c).get("content", "") for c in self.posts()]
        self.assertTrue(contents[-2].startswith("Question"))
        self.assertTrue(contents[-1].startswith("Failed"))
        self.assertEqual(len(contents), 4)

    def test_failed_edit_retries_next_snapshot(self):
        with patch("jarvis.v2.discord.reporter.EDIT_INTERVAL", 0.02):
            self.reporter()
            self.publish()
            self.drained()
            self.fake.responses = [httpx.Response(200, json={}), httpx.ConnectError(TOKEN)]
            self.task.status.step = 1
            with self.assertLogs("jarvis.v2.discord.reporter", logging.WARNING) as logs:
                self.publish("task_status_changed")
                self.drained()
                wait_for(lambda: self.rep.counters["errors"] == 1)
            self.assertNotIn(TOKEN, "\n".join(logs.output))
            self.publish("task_status_changed")
            self.drained()
            wait_for(lambda: self.rep.counters["edits"] == 1)
            self.assertEqual(len(self.posts()), 2)
            self.assertEqual(len(self.edits()), 2)

    def test_thread_save_preserves_concurrent_runner_update(self):
        self.reporter()
        original = self.rest.create_thread
        def create(channel, name):
            # Simulate a runner writing while the Discord HTTP call is in flight.
            task = self.stores.tasks.get(self.task.id)
            task.status.step = 9
            self.stores.tasks.save(task)
            return original(channel, name)
        with patch.object(self.rest, "create_thread", side_effect=create):
            self.publish()
            self.drained()
        saved = self.stores.tasks.get(self.task.id)
        self.assertEqual(saved.status.step, 9)
        self.assertIsNotNone(saved.discord_thread_id)

    def test_missing_channel_silent_count(self):
        self.reporter(channel=None)
        with patch("jarvis.v2.discord.reporter.LOG.warning") as log:
            self.publish()
            self.drained()
        self.assertEqual(self.rep.counters["skipped"], 1)
        self.assertEqual(self.fake.calls, [])
        log.assert_not_called()

    def test_transport_exception_swallowed_and_continues(self):
        self.reporter()
        self.fake.responses = [httpx.ConnectError("headers contain " + TOKEN)]
        with self.assertLogs("jarvis.v2.discord.reporter", logging.WARNING) as logs:
            self.publish()
            self.drained()
        self.assertEqual(self.rep.counters["errors"], 1)
        self.assertNotIn(TOKEN, "\n".join(logs.output))
        self.publish()
        self.drained()
        self.assertEqual(len(self.posts()), 2)
        self.assertTrue(self.rep._worker.is_alive())

    def test_partial_creation_reuses_thread_and_started(self):
        self.reporter()
        self.fake.responses = [httpx.Response(200, json={"id": "thread"}),
                               httpx.Response(200, json={"id": "started"}),
                               httpx.ConnectError(TOKEN)]
        with self.assertLogs("jarvis.v2.discord.reporter", logging.WARNING):
            self.publish()
            self.drained()
        self.publish()
        self.drained()
        self.assertEqual(self.rep.counters["threads"], 1)
        self.assertEqual(len([c for c in self.posts() if "content" in payload(c)]), 1)
        self.assertEqual(self.rep.counters["posts"], 2)

    def test_bus_shutdown_stops_reporter(self):
        self.reporter()
        self.bus.close()
        wait_for(lambda: not self.rep._worker.is_alive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
