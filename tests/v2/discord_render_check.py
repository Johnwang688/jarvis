"""Offline Discord rendering checks; every REST request uses a fake transport."""
from __future__ import annotations

from copy import deepcopy
import json
import logging
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from jarvis import config
from jarvis.tools import discord as v1
from jarvis.v2.bus import EventBus
from jarvis.v2 import worktrees
from jarvis.v2.discord import DiscordError, DiscordRest, Reporter
from jarvis.v2.discord import reporter as reporter_mod
from jarvis.v2.discord.render import approval_text, milestone, report_text, status_embed
from jarvis.v2.discord.rest import DiscordHTTPError
from jarvis.v2.model import (OpenQuestion, Project, ProviderName, Report, Role,
                             RoutingDecision, Status, Task, TaskState, to_json)
from jarvis.v2.stores import Stores

TOKEN = "synthetic-discord-secret-never-in-content"
DM = "dm-channel"


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.responses = []
        # Standing answers, checked first: (predicate(method, path), make_response).
        self.rules = []

    def __call__(self, method, url, headers, timeout, **kwargs):
        assert headers == {"Authorization": f"Bot {TOKEN}"}
        assert timeout == 30
        assert TOKEN not in repr(kwargs)
        call = {"method": method, "path": url.removeprefix(config.DISCORD_API),
                "at": time.monotonic(), **deepcopy(kwargs)}
        self.calls.append(call)
        for matches, make in list(self.rules):
            if matches(method, call["path"]):
                response = make()
                if isinstance(response, Exception):
                    raise response
                return response
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
        # Never the owner's real guild file (B1).
        guild = patch.object(config, "DISCORD_GUILD_PATH", Path(self.tmp.name) / "discord_guild.json")
        guild.start()
        self.addCleanup(guild.stop)
        # Accidental use of the default client must never reach the network.
        self.network = patch.object(httpx, "request", side_effect=AssertionError("live network"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.fake = FakeTransport()
        self.rest = DiscordRest(self.fake)
        self.addCleanup(self.rest.close)
        # Thread creates are paced at one a second in production; the pacing
        # check sets its own interval.
        pace = patch("jarvis.v2.discord.reporter.CREATE_INTERVAL", 0.0)
        pace.start()
        self.addCleanup(pace.stop)

    def test_exact_rest_endpoints(self):
        embed = {"title": "Status"}
        self.assertEqual(self.rest.create_channel("g", "project"), "1")
        self.assertEqual(self.rest.create_thread("c", "task"), "2")
        self.assertEqual(self.rest.post("t", "hi", embed), "3")
        self.rest.edit("t", "m", "bye", embed)
        self.rest.unarchive("t")
        self.rest.add_owner("t")
        self.assertEqual(self.rest.post("t", "quiet", silent=True), "7")
        self.assertEqual(self.rest.ping_owner("t"), "8")
        expected = [
            ("POST", "/guilds/g/channels", {"name": "project", "type": 0}),
            # A week idle before Discord archives a task's thread (plan §1).
            ("POST", "/channels/c/threads", {"name": "task", "type": 11,
                                              "auto_archive_duration": 10080}),
            ("POST", "/channels/t/messages", {"content": "hi", "embeds": [embed],
                                               "allowed_mentions": {"parse": []}}),
            ("PATCH", "/channels/t/messages/m", {"content": "bye", "embeds": [embed],
                                                  "allowed_mentions": {"parse": []}}),
            ("PATCH", "/channels/t", {"archived": False}),
            ("PUT", "/channels/t/thread-members/owner", None),
            # SUPPRESS_NOTIFICATIONS: every guild post but the ping line (D1).
            ("POST", "/channels/t/messages", {"content": "quiet", "flags": 4096,
                                               "allowed_mentions": {"parse": []}}),
            # The ping line: exactly the mention, mentioning the owner alone.
            ("POST", "/channels/t/messages", {"content": "<@owner>",
                                               "allowed_mentions": {"parse": [],
                                                                    "users": ["owner"]}}),
        ]
        self.assertEqual([(c["method"], c["path"],
                           payload(c) if ("json" in c or "data" in c) else None)
                          for c in self.fake.calls], expected)
        self.rest.edit("t", "m", embed=embed)
        self.assertNotIn("content", payload(self.fake.calls[-1]))

    def test_never_a_delete(self):
        """Decisions D3: channels are archived, never deleted — so the adapter
        has no way to send a DELETE, and nothing in the surface spells one."""
        self.assertFalse([name for name in dir(self.rest) if "delete" in name.lower()])
        package = Path(__file__).resolve().parents[2] / "jarvis" / "v2" / "discord"
        for path in package.glob("*.py"):
            self.assertNotIn('"DELETE"', path.read_text(encoding="utf-8"), path.name)

    def test_codes_and_a_long_429_raises_instead_of_sleeping(self):
        self.fake.responses = [httpx.Response(403, json={"code": 50013, "message": "Missing"})]
        with self.assertRaises(DiscordHTTPError) as caught:
            self.rest.post("t", "hi")
        self.assertEqual((caught.exception.status, caught.exception.code), (403, 50013))
        # Over 10 s: raise with the wait, sleep nothing — the caller defers.
        self.fake.responses = [httpx.Response(429, json={"retry_after": 30.5})]
        with patch.object(self.rest, "_sleep") as sleep:
            with self.assertRaises(DiscordHTTPError) as caught:
                self.rest.post("t", "hi")
        sleep.assert_not_called()
        self.assertEqual((caught.exception.status, caught.exception.retry_after), (429, 30.5))
        # At most 10 s is slept *in total* for one call (plan §1), not 10 s a try.
        self.fake.responses = [httpx.Response(429, json={"retry_after": 4})] * 4
        with patch.object(self.rest, "_sleep") as sleep:
            with self.assertRaises(DiscordHTTPError) as caught:
                self.rest.post("t", "hi")
        self.assertEqual([call.args for call in sleep.call_args_list], [(4.0,), (4.0,)])
        self.assertEqual(caught.exception.retry_after, 4.0)
        self.fake.responses = [httpx.Response(429, json={"retry_after": 10})] * 2
        with patch.object(self.rest, "_sleep") as sleep:
            with self.assertRaises(DiscordHTTPError):
                self.rest.post("t", "hi")
        self.assertEqual(sleep.call_count, 1)

    def test_a_rate_limit_wait_is_cut_short_by_close(self):
        rest = DiscordRest(self.fake)
        self.fake.responses = [httpx.Response(429, json={"retry_after": 9})]
        failure = {}

        def call():
            try:
                rest.post("t", "hi")
            except DiscordError as exc:
                failure["exc"] = exc
        worker = threading.Thread(target=call)
        started = time.monotonic()
        worker.start()
        wait_for(lambda: len(self.fake.calls) == 1)
        rest.close()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn("closed", str(failure["exc"]))
        self.assertEqual(len(self.fake.calls), 1)

    def test_edit_reopens_an_archived_thread_only_when_told_to(self):
        self.rest.edit("t", "m", embed={"title": "x"})
        self.assertEqual([(c["method"], c["path"]) for c in self.fake.calls],
                         [("PATCH", "/channels/t/messages/m")])
        self.fake.calls.clear()
        self.fake.responses = [httpx.Response(400, json={"code": 50083, "message": "archived"}),
                               httpx.Response(200, json={}), httpx.Response(200, json={})]
        self.rest.edit("t", "m", embed={"title": "x"})
        self.assertEqual([(c["method"], c["path"]) for c in self.fake.calls],
                         [("PATCH", "/channels/t/messages/m"), ("PATCH", "/channels/t"),
                          ("PATCH", "/channels/t/messages/m")])
        self.assertEqual(self.fake.calls[1]["json"], {"archived": False})
        # Any other refusal is not a reason to reopen anything.
        self.fake.calls.clear()
        self.fake.responses = [httpx.Response(403, json={"code": 50013, "message": "no"})]
        with self.assertRaises(DiscordHTTPError):
            self.rest.edit("t", "m", embed={"title": "x"})
        self.assertEqual(len(self.fake.calls), 1)

    def test_multipart_and_retry_preserve_file(self):
        self.fake.responses = [httpx.Response(429, json={"retry_after": 0.125}),
                               httpx.Response(200, json={"id": "message"})]
        with patch.object(self.rest, "_sleep") as sleep:
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
        with patch.object(self.rest, "_sleep") as sleep:
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
        self.assertIn("/yes C42", rendered)
        self.assertIn("/always C42", rendered)
        self.assertNotIn("/always", approval_text("shell", {"command": "a && b"}, "C44",
                                                  "task abc", allowlistable=False))
        self.assertIn("Origin: task abc", rendered)
        self.rest.post("t", rendered)
        call = self.fake.calls[-1]
        self.assertLessEqual(len(payload(call)["content"]), 2000)
        self.assertEqual(call["files"][0][1][1].decode(), rendered)
        short = approval_text("shell", {"command": "pwd"}, "C43", "task abc")
        self.rest.post("t", short)
        self.assertEqual(payload(self.fake.calls[-1])["content"], short)
        self.assertNotIn("files", self.fake.calls[-1])

    # -- the update poster (PR A) -------------------------------------------

    def reporter(self, channel="project-channel", *, dm=DM, reconcile=True, **kwargs):
        self.stores = Stores(Path(self.tmp.name) / "stores")
        self.project = self.stores.projects.create("Jarvis", "/tmp", discord_channel_id=channel)
        self.task = self.stores.tasks.create(self.project.id, "Implement task")
        self.stores.tasks.save(self.task)
        self.bus = getattr(self, "bus", None) or EventBus()
        self.rep = Reporter(self.stores, self.rest, self.bus,
                            dm_channel=(lambda: dm) if dm else None, reconcile=reconcile,
                            **kwargs)
        self.addCleanup(self.rep.close)
        self.drained()
        return self.rep

    def publish(self, kind="task_status_changed", task=None, data=None):
        task = task or self.task
        self.bus.publish({"kind": kind, "task_id": task.id, "project_id": task.project_id,
                          "data": data if data is not None else to_json(task)})

    def move(self, *phases, task=None, publish=True):
        """Drive the task through real store transitions, publishing each
        snapshot the way the runner does."""
        task = task or self.task
        for phase in phases:
            task = self.stores.tasks.transition(task.id, phase)
            if publish:
                self.publish(task=task)
        if task.id == self.task.id:
            self.task = task
        return task

    def drained(self):
        """Idle: nothing queued or in hand, no reconcile running or due now."""
        def idle():
            rep = self.rep
            due = (rep._reconcile_at is not None and not rep._closing
                   and rep._reconcile_at <= rep._clock() + 0.2 and not rep.breaker_open())
            return rep.subscription.unfinished_tasks == 0 and not rep.reconciling and not due
        wait_for(idle)
        time.sleep(0.01)
        wait_for(idle)

    def posts(self, channel=None):
        return [c for c in self.fake.calls if c["method"] == "POST"
                and c["path"].endswith("/messages")
                and (channel is None or c["path"] == f"/channels/{channel}/messages")]

    def contents(self, channel=None):
        return [payload(c).get("content") for c in self.posts(channel)]

    def edits(self):
        return [c for c in self.fake.calls if c["method"] == "PATCH" and "/messages/" in c["path"]]

    def creates(self):
        return [c for c in self.fake.calls if c["path"].endswith("/threads")]

    def sidecar(self, task=None):
        return reporter_mod.read_sidecar(self.stores, (task or self.task).id)

    def thread_id(self, task=None):
        return self.sidecar(task).get("discord_thread_id")

    def test_no_thread_during_intake_then_one_at_clarifying(self):
        """D6: a task is not under way in intake (a proposal inside its grace
        window can still be withdrawn), so its thread waits for clarifying."""
        self.reporter()
        self.publish("task_created")
        self.publish("task_updated")
        self.drained()
        self.assertEqual(self.fake.calls, [])
        self.move(TaskState.CLARIFYING)
        self.drained()
        create, member, started, card = self.fake.calls
        self.assertEqual((create["method"], create["path"]), ("POST", "/channels/project-channel/threads"))
        self.assertEqual(create["json"]["auto_archive_duration"], 10080)
        self.assertEqual(create["json"]["name"], f"{self.task.id} · Implement task")
        thread = self.thread_id()
        self.assertEqual((member["method"], member["path"]),
                         ("PUT", f"/channels/{thread}/thread-members/owner"))
        self.assertTrue(payload(started)["content"].startswith("Started task"))
        self.assertIn("embeds", payload(card))
        for quiet in (started, card):
            self.assertEqual(payload(quiet)["flags"], 4096)
            self.assertEqual(payload(quiet)["allowed_mentions"], {"parse": []})
        self.assertEqual(self.stores.tasks.get(self.task.id).discord_thread_id, thread)

    def test_a_proposal_withdrawn_in_intake_never_gets_a_thread(self):
        self.reporter()
        self.move(TaskState.CANCELLED)
        self.drained()
        self.assertEqual(self.fake.calls, [])

    def test_created_once_sidecar_and_restart_adoption(self):
        self.reporter()
        self.move(TaskState.CLARIFYING)
        self.drained()
        task = self.stores.tasks.get(self.task.id)
        self.assertIsNotNone(task.discord_thread_id)
        sidecar = self.sidecar()
        self.assertEqual(sidecar["discord_thread_id"], task.discord_thread_id)
        self.assertEqual(sidecar["discord_status_message_id"], "4")
        self.assertEqual(sidecar["phase"], "clarifying")
        self.assertEqual(sidecar["embed_sha"],
                         reporter_mod.embed_sha(status_embed(task, self.project)))
        self.assertNotIn("discord_status_message_id", to_json(task))
        self.publish()
        self.drained()
        self.rep.close()
        self.rep = Reporter(self.stores, self.rest, self.bus, dm_channel=lambda: DM)
        self.addCleanup(self.rep.close)
        self.publish()
        self.drained()
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(len(self.posts()), 2)

    def test_the_thread_id_is_read_from_the_sidecar_first(self):
        """Bug 1's other half: even if the task record lost the id, the sidecar
        still has it, so no second thread is made."""
        self.reporter()
        self.move(TaskState.CLARIFYING)
        self.drained()
        thread = self.thread_id()
        path = self.stores.tasks.path(self.task.id)
        raw = json.loads(path.read_text())
        raw["discord_thread_id"] = None
        path.write_text(json.dumps(raw))
        self.move(TaskState.PLANNED)
        self.drained()
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(reporter_mod.thread_for(self.stores, self.stores.tasks.get(self.task.id)),
                         thread)

    def test_adopt_existing_thread(self):
        self.reporter()
        self.task.discord_thread_id = "existing-thread"
        self.stores.tasks.save(self.task)
        self.move(TaskState.CLARIFYING)
        self.drained()
        self.assertEqual(self.creates(), [])
        self.assertEqual(len(self.posts()), 2)
        self.assertTrue(all(c["path"] == "/channels/existing-thread/messages" for c in self.fake.calls))
        self.assertEqual(self.thread_id(), "existing-thread")

    def test_five_changes_one_second_last_state_timer(self):
        self.reporter()
        self.move(TaskState.CLARIFYING)
        self.drained()
        started = time.monotonic()
        for step in range(1, 6):
            self.task.status.step = step
            self.publish()
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
        # Edit first: a live thread is never reopened pre-emptively.
        self.assertFalse([c for c in self.fake.calls if c.get("json") == {"archived": False}])
        # The card never pings: an edit carries no mention at all.
        self.assertEqual(payload(edit)["allowed_mentions"], {"parse": []})

    def test_same_channel_edit_spacing_and_independent_channels(self):
        # Accelerate the interval here; the preceding test checks the real 5 s.
        with patch("jarvis.v2.discord.reporter.EDIT_INTERVAL", 0.08):
            self.reporter()
            tasks = []
            for channel in ("shared", "shared", "other"):
                task = self.stores.tasks.create(self.project.id, "Another task", discord_thread_id=channel)
                self.stores.tasks.save(task)
                tasks.append(self.move(TaskState.CLARIFYING, task=task))
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

    def test_milestones_ping_lines_and_report_attachment(self):
        """D1: the milestone is silent; a separate `<@owner>` line follows it
        for blocked, failed and done, and for nothing quiet."""
        self.reporter()
        self.move(TaskState.CLARIFYING)
        self.drained()
        # Queue every snapshot before consumption; latest disk state is irrelevant.
        for phase in (TaskState.BLOCKED, TaskState.RUNNING, TaskState.VERIFYING, TaskState.DONE):
            self.task.status.phase = phase
            if phase == TaskState.DONE:
                self.task.report = Report(done=["x" * 5000], verified="reviewer abc: passed")
            self.publish()
        self.drained()
        posts = self.posts()[2:]
        self.assertEqual([(payload(c).get("content") or "").split(":")[0] for c in posts],
                         ["Blocked", "<@owner>", "Verified", "Done", "<@owner>"])
        for call in posts:
            body = payload(call)
            if body["content"] == "<@owner>":
                self.assertEqual(body["allowed_mentions"], {"parse": [], "users": ["owner"]})
                self.assertNotIn("flags", body)
            else:
                self.assertEqual(body["flags"], 4096)
                self.assertEqual(body["allowed_mentions"], {"parse": []})
                self.assertLessEqual(len(body["content"]), 2000)
        done = posts[3]
        self.assertLessEqual(len(payload(done)["content"]), 400 + 1 + 1500)
        self.assertIn(b"x" * 5000, done["files"][0][1][1])
        self.assertEqual(done["files"][0][1][0], "report.txt")
        self.publish()
        self.drained()
        self.assertEqual(len(self.posts()), 7)

    def test_question_with_choices_from_task_question_once(self):
        self.reporter()
        task = self.move(TaskState.CLARIFYING)
        self.drained()
        question = OpenQuestion("Which target?", True, options=["staging", "prod"])
        task.spec.questions = [question]
        task.status.open_question = question.text
        self.stores.tasks.save(task)
        # The runner's order: the saved snapshot, then the question event.
        self.publish(task=task)
        self.publish("task_question", task=task, data={
            "questions": [to_json(question)], "task": to_json(task)})
        self.drained()
        posts = self.posts()[2:]
        self.assertEqual(len(posts), 2, [payload(c) for c in posts])
        body = payload(posts[0])["content"]
        self.assertTrue(body.startswith("Question (blocking): Which target?"))
        self.assertIn("Choices: staging / prod", body)
        self.assertIn("/answer", body)
        self.assertEqual(payload(posts[1])["content"], "<@owner>")
        # Answered in the HUD; the next question is posted, the old one never again.
        task.spec.questions[0].answer = "staging"
        task.spec.questions.append(OpenQuestion("Which region?", True))
        task.status.open_question = "Which region?"
        self.stores.tasks.save(task)
        self.publish(task=task)
        self.drained()
        self.assertTrue(self.contents()[-2].startswith("Question (blocking): Which region?"))
        self.assertEqual(len(self.posts()), 6)

    def test_cancelled_is_a_quiet_post(self):
        """D8: a cancelled task says so in its thread, and does not ping."""
        self.reporter()
        self.move(TaskState.CLARIFYING, TaskState.CANCELLED)
        self.drained()
        last = self.posts()[-1]
        self.assertEqual(payload(last)["content"], "Cancelled.")
        self.assertEqual(payload(last)["flags"], 4096)
        self.assertNotIn("<@owner>", self.contents())

    def test_failed_and_no_provider_chatter(self):
        self.reporter()
        self.move(TaskState.CLARIFYING)
        self.task.status.phase = TaskState.FAILED
        self.publish("task_updated")
        for kind in ("tool_started", "tool_finished", "text_delta", "thinking", "text", "usage",
                     "approval_requested", "question"):
            self.bus.publish({"kind": kind, "task_id": self.task.id, "data": {"text": "noise"}})
        self.drained()
        contents = self.contents()
        self.assertTrue(contents[-2].startswith("Failed"))
        self.assertEqual(contents[-1], "<@owner>")
        self.assertEqual(len(contents), 4)

    def test_failed_edit_retries_next_snapshot(self):
        with patch("jarvis.v2.discord.reporter.EDIT_INTERVAL", 0.02), \
                patch("jarvis.v2.discord.reporter.RETRY_AFTER_FAILURE", 60):
            self.reporter()
            self.move(TaskState.CLARIFYING)
            self.drained()
            self.fake.responses = [httpx.ConnectError(TOKEN)]
            self.task.status.step = 1
            with self.assertLogs("jarvis.v2.discord.reporter", logging.WARNING) as logs:
                self.publish()
                self.drained()
                wait_for(lambda: self.rep.counters["errors"] == 1)
            self.assertNotIn(TOKEN, "\n".join(logs.output))
            self.publish()
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
            self.move(TaskState.CLARIFYING)
            self.drained()
        saved = self.stores.tasks.get(self.task.id)
        self.assertEqual(saved.status.step, 9)
        self.assertIsNotNone(saved.discord_thread_id)

    def test_bug1_a_stale_save_in_worktrees_ensure_keeps_the_thread_id(self):
        """Bug 1, reproduced where it happened: `ensure` holds the task across
        `git worktree add` and then saves it whole. The poster made the thread
        meanwhile; the stale save must not wipe its id."""
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        for args in (("init", "--template=", "-b", "main"), ("config", "user.name", "T"),
                     ("config", "user.email", "t@example.invalid"),
                     ("config", "commit.gpgsign", "false"), ("config", "core.hooksPath", "/dev/null"),
                     ("commit", "--allow-empty", "-m", "init")):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        stores = Stores(Path(self.tmp.name) / "bug1")
        project = stores.projects.create("Repo", str(repo), discord_channel_id="c")
        task = stores.tasks.create(project.id, "Fix the thing")
        stores.tasks.save(task)
        stale = stores.tasks.transition(task.id, TaskState.CLARIFYING)
        real_git = worktrees._git

        def git(cwd, *args):
            out = real_git(cwd, *args)
            if args[:2] == ("worktree", "add"):
                # The Reporter, on its own thread, records the new thread now.
                fresh = stores.tasks.get(task.id)
                fresh.discord_thread_id = "thread-1"
                stores.tasks.save(fresh)
            return out
        with patch.object(worktrees, "_git", side_effect=git):
            worktrees.ensure(stale, project, stores)
        saved = stores.tasks.get(task.id)
        self.assertIsNotNone(saved.worktree)
        self.assertEqual(saved.discord_thread_id, "thread-1")

    def test_safety_net_dm_without_a_channel(self):
        """Before B1 no project has a channel: attention milestones reach the
        owner's DM, prefixed, unflagged and with no ping line; quiet ones and
        the card are not posted at all."""
        self.reporter(channel=None)
        self.move(TaskState.CLARIFYING)
        self.drained()
        self.assertEqual(self.fake.calls, [])
        self.move(TaskState.BLOCKED, TaskState.RUNNING, TaskState.VERIFYING)
        self.task.report = Report(done=["shipped"], verified="ok")
        self.stores.tasks.save(self.task)
        self.move(TaskState.DONE)
        self.drained()
        self.assertEqual({c["path"] for c in self.fake.calls}, {f"/channels/{DM}/messages"})
        contents = self.contents(DM)
        prefix = f"[Jarvis · task {self.task.id}] "
        self.assertEqual([c.removeprefix(prefix).split(":")[0] for c in contents],
                         ["Blocked", "Done"])
        self.assertTrue(all(c.startswith(prefix) for c in contents))
        self.assertIn("DONE      shipped", contents[-1])
        for call in self.posts(DM):
            self.assertNotIn("flags", payload(call))
        self.assertEqual(self.rep.counters["dm"], 2)

    def test_a_broken_channel_falls_back_to_the_dm_and_alerts_once(self):
        self.fake.rules.append((lambda m, p: p == "/channels/project-channel/threads",
                                lambda: httpx.Response(403, json={
                                    "code": 50013, "message": f"no {TOKEN}"})))
        self.reporter()
        with self.assertLogs("jarvis.v2.discord.reporter", logging.WARNING) as logs:
            self.move(TaskState.CLARIFYING, TaskState.BLOCKED)
            self.drained()
        dm = self.contents(DM)
        self.assertEqual(len(dm), 2, dm)
        self.assertIn("<#project-channel>", dm[0])          # the alert, once
        self.assertIn("50013", dm[0])
        self.assertTrue(dm[1].startswith(f"[Jarvis · task {self.task.id}] Blocked"))
        status = self.rep.status()
        self.assertEqual(status["state"], "degraded")
        self.assertIn("project-channel", status["reason"])
        self.assertEqual(status["last_error"]["op"], "create_thread")
        self.assertEqual((status["last_error"]["status"], status["last_error"]["code"]), (403, 50013))
        text = "\n".join(logs.output)
        self.assertIn("create_thread", text)
        self.assertIn("403", text)
        self.assertIn("50013", text)
        for secret in (TOKEN, "/channels/", "discord.com", config.DISCORD_API):
            self.assertNotIn(secret, text)
        # Still broken a moment later: no second alert inside 24 h.
        self.move(TaskState.RUNNING, TaskState.FAILED)
        self.drained()
        self.assertEqual(sum("<#project-channel>" in c for c in self.contents(DM)), 1)
        self.assertTrue(self.contents(DM)[-1].startswith(f"[Jarvis · task {self.task.id}] Failed"))

    def test_a_deleted_thread_is_not_recreated_and_the_dm_takes_over(self):
        self.reporter()
        self.move(TaskState.CLARIFYING)
        self.drained()
        thread = self.thread_id()
        self.fake.rules.append((lambda m, p: p == f"/channels/{thread}/messages",
                                lambda: httpx.Response(404, json={"code": 10003, "message": "x"})))
        self.move(TaskState.BLOCKED)
        self.drained()
        self.assertTrue(self.sidecar()["thread_gone"])
        self.assertTrue(self.contents(DM)[-1].startswith(f"[Jarvis · task {self.task.id}] Blocked"))
        self.move(TaskState.RUNNING, TaskState.FAILED)
        self.drained()
        self.assertEqual(len(self.creates()), 1)
        self.assertTrue(self.contents(DM)[-1].startswith(f"[Jarvis · task {self.task.id}] Failed"))
        self.assertIsNone(reporter_mod.thread_for(self.stores, self.stores.tasks.get(self.task.id)))

    def test_reconcile_seeds_silently_and_never_backfills_terminal_tasks(self):
        """A first start over existing tasks: no DM storm, a thread and a card
        only for active tasks in a project with a channel, one create a
        second, and nothing at all for a finished task."""
        stores = Stores(Path(self.tmp.name) / "stores")
        quiet = stores.projects.create("School", "/tmp")
        loud = stores.projects.create("Jarvis", "/tmp", discord_channel_id="project-channel")
        made = []
        for project, path in ((quiet, (TaskState.CLARIFYING, TaskState.BLOCKED)),
                              (loud, (TaskState.CLARIFYING, TaskState.PLANNED, TaskState.RUNNING)),
                              (loud, (TaskState.CLARIFYING,)),
                              (loud, (TaskState.CLARIFYING, TaskState.PLANNED, TaskState.RUNNING,
                                      TaskState.VERIFYING, TaskState.DONE)),
                              (loud, ())):
            task = stores.tasks.create(project.id, "Old work")
            stores.tasks.save(task)
            for phase in path:
                task = stores.tasks.transition(task.id, phase)
            made.append(task)
        self.stores, self.bus = stores, EventBus()
        with patch("jarvis.v2.discord.reporter.CREATE_INTERVAL", 0.3):
            # These tasks existed before the poster started (the stamp is
            # seconds-resolution, so the test says so rather than sleeping).
            self.rep = Reporter(stores, self.rest, self.bus, dm_channel=lambda: DM,
                                started_at="2999-01-01T00:00:00+00:00")
            self.addCleanup(self.rep.close)
            wait_for(lambda: self.rep.counters["reconciles"] == 1
                     and len([c for c in self.fake.calls if "embeds" in json.dumps(c.get("json", {}))
                              or "payload_json" in c.get("data", {})]) == 2, timeout=4)
            self.drained()
        creates = self.creates()
        self.assertEqual(len(creates), 2)
        self.assertGreaterEqual(creates[1]["at"] - creates[0]["at"], 0.29)
        self.assertEqual(self.posts(DM), [])
        for call in self.posts():
            self.assertIn("embeds", payload(call))          # cards only: no Started, no ping
        self.assertNotIn("<@owner>", json.dumps([payload(c) for c in self.posts()]))
        blocked, running, clarifying, done, intake = made
        self.assertTrue(self.sidecar(blocked)["seeded"])
        self.assertEqual(self.sidecar(blocked)["phase"], "blocked")
        self.assertIsNone(self.thread_id(done))
        self.assertTrue(self.sidecar(done)["seeded"])
        self.assertEqual(self.sidecar(intake)["phase"], "intake")
        self.assertNotIn("seeded", self.sidecar(intake))
        # Seeded tasks carry on normally: the next real transition is posted.
        self.task = running
        self.move(TaskState.VERIFYING, TaskState.DONE)
        self.drained()
        self.assertEqual([c.split(":")[0] for c in self.contents(self.thread_id(running))[1:]],
                         ["Verified", "Done", "<@owner>"])
        # The intake task gets its "Started" when it really starts.
        self.task = intake
        self.move(TaskState.CLARIFYING)
        self.drained()
        self.assertTrue(self.contents(self.thread_id(intake))[0].startswith("Started task"))

    def test_reconcile_edits_a_stale_card_once(self):
        self.reporter()
        self.move(TaskState.CLARIFYING)
        self.drained()
        self.rep.close()
        posts, edits = len(self.posts()), len(self.edits())
        task = self.stores.tasks.get(self.task.id)
        task.status.step = 3
        self.stores.tasks.save(task)
        with patch("jarvis.v2.discord.reporter.EDIT_INTERVAL", 0.05):
            self.rep = Reporter(self.stores, self.rest, self.bus, dm_channel=lambda: DM)
            self.addCleanup(self.rep.close)
            wait_for(lambda: self.rep.counters["edits"] == 1)
            time.sleep(0.2)
        self.assertEqual(len(self.edits()), edits + 1)
        self.assertEqual(len(self.posts()), posts)
        self.assertEqual(self.sidecar()["embed_sha"],
                         reporter_mod.embed_sha(status_embed(task, self.project)))
        # Unchanged since: a further reconcile edits nothing.
        self.rep._schedule_reconcile(self.rep._clock())
        self.publish("task_updated", data={})
        self.drained()
        time.sleep(0.2)
        self.assertEqual(len(self.edits()), edits + 1)

    def test_dropped_events_trigger_a_reconcile_that_catches_up(self):
        """The Done snapshot itself is evicted from a full queue; the drop
        count grows, the reconcile reads the disk, and Done is still posted."""
        self.bus = EventBus(capacity=2)
        gate, entered = threading.Event(), threading.Event()
        original = self.rest.post

        def slow(*args, **kwargs):
            entered.set()
            gate.wait(3)
            return original(*args, **kwargs)
        self.reporter(channel=None)
        other = self.stores.tasks.create(self.project.id, "Unrelated")
        self.stores.tasks.save(other)
        with patch.object(self.rest, "post", side_effect=slow):
            self.move(TaskState.CLARIFYING, TaskState.BLOCKED)     # the worker parks in post
            self.assertTrue(entered.wait(3))
            self.move(TaskState.RUNNING, TaskState.VERIFYING, TaskState.DONE)
            for _ in range(2):                                      # evict Done too
                self.publish("task_updated", task=other)
            self.assertGreaterEqual(self.rep.subscription.dropped, 3)
            gate.set()
            wait_for(lambda: self.rep.counters["reconciles"] >= 2)
            self.drained()
        contents = self.contents(DM)
        self.assertEqual([c.split("] ")[1].split(":")[0] for c in contents], ["Blocked", "Done"])
        self.assertEqual(self.rep.status()["dropped"], self.rep.subscription.dropped)

    def test_breaker_opens_after_three_failures_and_reconciles_after_back_off(self):
        now = [1000.0]
        self.fake.rules.append((lambda m, p: True, lambda: httpx.ConnectError(TOKEN)))
        events = self.bus = EventBus()
        seen = events.subscribe(lambda r: r.get("kind") == "discord_status")
        self.reporter(clock=lambda: now[0])
        with self.assertLogs("jarvis.v2.discord.reporter", logging.WARNING):
            self.move(TaskState.CLARIFYING)
            self.drained()
            self.assertEqual(self.rep.status()["state"], "degraded")
            for _ in range(2):
                self.publish()
                self.drained()
        self.assertTrue(self.rep.breaker_open())
        self.assertEqual(self.rep.status()["state"], "down")
        self.assertEqual(self.rep._open_until, 1030.0)
        calls = len(self.fake.calls)
        self.publish()
        self.drained()
        self.assertEqual(len(self.fake.calls), calls, "the open breaker short-circuits")
        # Past the back-off, the reconcile tries again; failing, it waits 60 s.
        now[0] = 1031.0
        nudge = {"kind": "task_updated", "task_id": "00000000", "data": {}}  # no such task
        with self.assertLogs("jarvis.v2.discord.reporter", logging.WARNING):
            self.bus.publish(dict(nudge))
            wait_for(lambda: self.rep.counters["reconciles"] >= 2)
            self.drained()
        self.assertEqual(self.rep._open_until, 1031.0 + 60)
        # Discord is back: the next reconcile makes the thread and the light goes green.
        self.fake.rules.clear()
        now[0] = 1100.0
        self.bus.publish(dict(nudge))
        wait_for(lambda: self.rep.counters["reconciles"] >= 3)
        self.drained()
        self.assertFalse(self.rep.breaker_open())
        self.assertEqual(self.rep.status()["state"], "ok")
        self.assertEqual(len(self.creates()), 5)        # 3 + 1 failed, then the one that worked
        self.assertIsNotNone(self.thread_id())
        states = []
        while not seen.empty():
            states.append(seen.get_nowait()["data"]["state"])
        self.assertEqual(states, ["degraded", "down", "ok"])
        for state in ("down",):
            self.assertIn(state, states)

    def test_close_flushes_due_edits_and_is_idempotent(self):
        self.reporter()
        self.move(TaskState.CLARIFYING)
        self.drained()
        self.task.status.step = 4
        self.publish()
        self.drained()
        self.assertEqual(self.edits(), [])               # due in 5 s
        started = time.monotonic()
        self.rep.close(flush=True)
        self.assertLess(time.monotonic() - started, 2.6)
        self.assertEqual(len(self.edits()), 1)
        self.rep.close(flush=True)
        self.assertFalse(self.rep._worker.is_alive())

    def test_status_is_states_and_counts_only(self):
        self.reporter()
        self.fake.rules.append((lambda m, p: True, lambda: httpx.Response(
            500, json={"message": f"https://discord.com/api/channels/1 {TOKEN}"})))
        with self.assertLogs("jarvis.v2.discord.reporter", logging.WARNING):
            self.move(TaskState.CLARIFYING)
            self.drained()
        status = self.rep.status()
        self.assertEqual(set(status), {"state", "reason", "counters", "dropped", "last_error"})
        self.assertEqual(set(status["last_error"]), {"op", "status", "code", "at"})
        self.assertNotIn(TOKEN, json.dumps(status))
        self.assertNotIn("discord.com", json.dumps(status))

    def test_transport_exception_swallowed_and_continues(self):
        with patch("jarvis.v2.discord.reporter.RETRY_AFTER_FAILURE", 60):
            self.reporter()
            self.fake.responses = [httpx.ConnectError("headers contain " + TOKEN)]
            with self.assertLogs("jarvis.v2.discord.reporter", logging.WARNING) as logs:
                self.move(TaskState.CLARIFYING)
                self.drained()
            self.assertEqual(self.rep.counters["errors"], 1)
            self.assertNotIn(TOKEN, "\n".join(logs.output))
            self.publish()
            self.drained()
            self.assertEqual(len(self.posts()), 2)
            self.assertTrue(self.rep._worker.is_alive())

    def test_partial_creation_reuses_thread_and_started(self):
        with patch("jarvis.v2.discord.reporter.RETRY_AFTER_FAILURE", 60):
            self.reporter()
            self.fake.responses = [httpx.Response(200, json={"id": "thread"}),
                                   httpx.Response(204),
                                   httpx.Response(200, json={"id": "started"}),
                                   httpx.ConnectError(TOKEN)]
            with self.assertLogs("jarvis.v2.discord.reporter", logging.WARNING):
                self.move(TaskState.CLARIFYING)
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


    # -- review fixes (PR #6 review) ---------------------------------------

    def trip_breaker(self, now):
        """Three transient failures on the DM path: the breaker opens."""
        self.move(TaskState.CLARIFYING, TaskState.BLOCKED)
        self.drained()
        for _ in range(3):
            if self.rep.breaker_open():
                break
            self.publish()
            self.drained()
        self.assertTrue(self.rep.breaker_open())

    def nudge(self):
        self.bus.publish({"kind": "task_updated", "task_id": "00000000", "data": {}})

    def test_review1_a_task_created_during_an_outage_still_gets_its_question(self):
        """Its task_created was dropped while the breaker was open; the
        reconcile after the back-off must report it, not seed it silently."""
        now = [1000.0]
        failing = [True]
        self.fake.rules.append((lambda m, p: failing[0], lambda: httpx.ConnectError("x")))
        self.bus = EventBus()
        with self.assertLogs("jarvis.v2.discord.reporter", logging.WARNING):
            self.reporter(channel=None, clock=lambda: now[0],
                          started_at="2000-01-01T00:00:00+00:00")
            self.trip_breaker(now)
        other = self.stores.tasks.create(self.project.id, "New work")
        self.stores.tasks.save(other)
        self.publish("task_created", task=other)
        other = self.stores.tasks.transition(other.id, TaskState.CLARIFYING)
        other.status.open_question = "Which database?"
        other.spec.questions.append(OpenQuestion("Which database?", True, options=["pg", "sqlite"]))
        self.stores.tasks.save(other)
        self.publish(task=other)
        self.drained()
        failing[0] = False
        now[0] = 2000.0
        self.nudge()
        wait_for(lambda: any("Which database?" in (c or "") for c in self.contents(DM)))
        self.drained()
        question = next(c for c in self.contents(DM) if "Which database?" in c)
        self.assertIn("Choices: pg / sqlite", question)
        self.assertNotIn("seeded", self.sidecar(other))

    def test_review2_an_unreadable_task_never_wedges_the_breaker(self):
        now = [1000.0]
        failing = [True]
        self.fake.rules.append((lambda m, p: failing[0], lambda: httpx.ConnectError("x")))
        self.reporter(channel=None, clock=lambda: now[0])
        bad = self.stores.tasks.create(self.project.id, "Bad")
        self.stores.tasks.save(bad)
        self.stores.tasks.path(bad.id).write_text("{not json")
        with self.assertLogs("jarvis.v2.discord.reporter", logging.WARNING) as logs:
            self.trip_breaker(now)
            failing[0] = False
            now[0] = 5000.0
            self.nudge()
            wait_for(lambda: self.rep.counters["unreadable_tasks"] >= 1)
            self.drained()
        self.assertIn(bad.id, "\n".join(logs.output))
        self.assertIsNone(self.rep._open_until)
        self.assertEqual(self.rep.status()["state"], "ok")
        self.move(TaskState.RUNNING, TaskState.FAILED)
        self.drained()
        self.assertTrue(any("Failed" in c for c in self.contents(DM)), self.contents(DM))

    def test_review2_events_are_handled_once_the_back_off_has_run_out(self):
        """Even when the reconcile cannot list tasks at all."""
        now = [1000.0]
        failing = [True]
        self.fake.rules.append((lambda m, p: failing[0], lambda: httpx.ConnectError("x")))
        self.reporter(channel=None, clock=lambda: now[0])
        with self.assertLogs("jarvis.v2.discord.reporter", logging.WARNING):
            self.trip_breaker(now)
            failing[0] = False
            now[0] = 5000.0
            with patch.object(self.stores.tasks, "ids", side_effect=OSError("disk")):
                self.move(TaskState.RUNNING, TaskState.FAILED)
                self.drained()
        self.assertTrue(any("Failed" in c for c in self.contents(DM)), self.contents(DM))
        self.assertGreater(self.rep._reconcile_at, now[0])      # retried with back-off

    def test_review3_a_permanent_create_refusal_sends_attention_to_the_dm(self):
        self.fake.rules.append((lambda m, p: p.endswith("/threads"),
                                lambda: httpx.Response(400, json={"code": 50024, "message": "x"})))
        self.reporter()
        with self.assertLogs("jarvis.v2.discord.reporter", logging.WARNING):
            self.move(TaskState.CLARIFYING, TaskState.BLOCKED)
            self.drained()
        self.assertTrue(self.contents(DM)[-1].startswith(f"[Jarvis · task {self.task.id}] Blocked"))
        self.assertEqual(self.rep.status()["state"], "degraded")

    def test_review3_a_refused_attention_post_falls_back_to_the_dm(self):
        """A locked thread refuses the post with a plain 403: the owner still
        hears about it, in the DM."""
        self.reporter()
        self.move(TaskState.CLARIFYING)
        self.drained()
        thread = self.thread_id()
        self.fake.rules.append((lambda m, p: p == f"/channels/{thread}/messages",
                                lambda: httpx.Response(403, json={"message": "locked"})))
        with self.assertLogs("jarvis.v2.discord.reporter", logging.WARNING):
            self.move(TaskState.BLOCKED)
            self.drained()
        self.assertTrue(self.contents(DM)[-1].startswith(f"[Jarvis · task {self.task.id}] Blocked"))
        self.assertEqual(self.sidecar()["phase"], "blocked")

    def test_review4_an_unreachable_dm_is_retried_without_a_new_event(self):
        answer = [None]
        with patch("jarvis.v2.discord.reporter.RETRY_AFTER_FAILURE", 1.0):
            self.reporter(channel=None)
            self.rep._dm_channel = lambda: answer[0]
            self.move(TaskState.CLARIFYING, TaskState.BLOCKED, TaskState.FAILED)
            self.drained()
            self.assertEqual(self.contents(DM), [])
            self.assertIsNotNone(self.rep._reconcile_at)
            answer[0] = DM
            wait_for(lambda: self.contents(DM))
            self.drained()
        self.assertTrue(self.contents(DM)[-1].startswith(f"[Jarvis · task {self.task.id}] Failed"))

    def test_review5_a_broken_thread_entry_clears_when_its_task_finishes(self):
        self.reporter()
        self.move(TaskState.CLARIFYING)
        self.drained()
        thread = self.thread_id()
        rule = (lambda m, p: p == f"/channels/{thread}/messages",
                lambda: httpx.Response(403, json={"code": 50013, "message": "x"}))
        self.fake.rules.append(rule)
        with self.assertLogs("jarvis.v2.discord.reporter", logging.WARNING):
            self.move(TaskState.PLANNED, TaskState.RUNNING, TaskState.FAILED)
            self.drained()
        self.fake.rules.remove(rule)
        self.assertEqual(self.rep.status()["state"], "ok")

    def test_review5_a_broken_entry_ages_out(self):
        wall = [1_000_000.0]
        self.fake.rules.append((lambda m, p: p.endswith("/threads"),
                                lambda: httpx.Response(403, json={"code": 50013, "message": "x"})))
        self.reporter(wall=lambda: wall[0])
        with self.assertLogs("jarvis.v2.discord.reporter", logging.WARNING):
            self.move(TaskState.CLARIFYING)
            self.drained()
        self.assertEqual(self.rep.status()["state"], "degraded")
        wall[0] += 3601
        self.assertEqual(self.rep.status()["state"], "ok")

    def test_review6_nothing_secret_reaches_discord(self):
        envdir = Path(self.tmp.name) / "envdir"
        envdir.mkdir()
        secret = "sk-test-PRA-0123456789abcdef"
        (envdir / ".env").write_text(f"FAKE_API_KEY={secret}\n")
        from jarvis.tools import secrets
        with patch.object(secrets, "_search_dirs", return_value=[envdir]):
            self.reporter()
            task = self.stores.tasks.get(self.task.id)
            task.brief = f"deploy with {secret}"
            self.stores.tasks.save(task)
            task = self.move(TaskState.CLARIFYING)
            question = OpenQuestion(f"Use {secret}?", True, options=[secret, "no"])
            task.spec.questions = [question]
            task.status.open_question = question.text
            task.status.last_file = f"/tmp/{secret}"
            self.stores.tasks.save(task)
            self.publish(task=task)
            self.drained()
            self.task = self.stores.tasks.get(self.task.id)
            self.task.spec.questions[0].answer = "no"
            self.task.status.open_question = None
            self.task.report = Report(done=[f"used {secret} " + "x" * 3000], verified=secret)
            self.stores.tasks.save(self.task)
            self.move(TaskState.PLANNED, TaskState.RUNNING, TaskState.VERIFYING, TaskState.DONE)
            self.drained()
            self.rep.close(flush=True)
        wire = repr(self.fake.calls)
        self.assertNotIn(secret, wire)
        self.assertIn("redacted", wire)
        self.assertTrue(any(c.get("files") for c in self.posts()), "the report overflow was attached")

    def test_review8_missing_access_alerts_too_and_a_failed_alert_is_retried(self):
        dm_posts = [0]

        def dm_rule(method, path):
            if path != f"/channels/{DM}/messages":
                return False
            dm_posts[0] += 1
            return dm_posts[0] == 1                        # the first alert fails
        self.fake.rules.append((dm_rule, lambda: httpx.Response(500, json={"message": "x"})))
        self.fake.rules.append((lambda m, p: p.endswith("/threads"),
                                lambda: httpx.Response(403, json={"code": 50001, "message": "x"})))
        with patch("jarvis.v2.discord.reporter.RETRY_AFTER_FAILURE", 60):
            self.reporter()
            with self.assertLogs("jarvis.v2.discord.reporter", logging.WARNING):
                self.move(TaskState.CLARIFYING)
                self.drained()
                self.assertEqual(self.rep.counters["alerts"], 0)
                self.move(TaskState.BLOCKED)
                self.drained()
                self.assertEqual(self.rep.counters["alerts"], 1)
                self.move(TaskState.RUNNING)                 # still broken: no third try
                self.drained()
        alerts = [c for c in self.contents(DM) if "<#project-channel>" in c]
        # The first was refused (500) and not stamped, so the next failure
        # sent it again; after that one landed, 24 h of quiet.
        self.assertEqual(len(alerts), 2, self.contents(DM))
        self.assertEqual(self.rep.counters["alerts"], 1)
        self.assertIn("cannot see it", alerts[0])
        self.assertIn("50001", alerts[0])

    def test_review10_the_thread_id_survives_a_failed_sidecar_write(self):
        self.reporter()
        real = reporter_mod._write_bytes

        def fail_sidecar(path, data):
            if path.name == "discord.json":
                raise OSError("disk full")
            return real(path, data)
        with patch.object(reporter_mod, "_write_bytes", side_effect=fail_sidecar), \
                self.assertLogs("jarvis.v2.discord.reporter", logging.WARNING):
            self.move(TaskState.CLARIFYING)
            self.drained()
            thread = self.stores.tasks.get(self.task.id).discord_thread_id
            self.assertIsNotNone(thread)
            self.move(TaskState.PLANNED)
            self.drained()
        self.assertEqual(len(self.creates()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
