"""Free WP10b checks: fake listener, fake control, fake REST transport, no network.

The rules under test are the ones that decide what a typed "yes" authorizes, so
the suite is written to fail if any of them is relaxed: owner-only and
never-bots, the mention lifted *only* inside an owned channel or thread, a code
answerable *only* where it was posted, a spoken answer authorizing nothing, and
a bare yes with two open questions refusing to guess.

`config.ALLOWLIST_PATH` is pointed at a temp file for the whole run — an ALWAYS
here writes a real allowlist entry, and a suite that writes into live machine
state is how `apt` once landed on the owner's real allowlist.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

import httpx

from jarvis import config
from jarvis import discord_gateway as v1gw
from jarvis.v2 import daemon as daemon_mod
from jarvis.v2.approvals import ApprovalRequest, PendingApprovals
from jarvis.v2.control import ControlError
from jarvis.v2.daemon import Daemon
from jarvis.v2.discord.gateway import (ChannelReply, DiscordRouter, _V2Listener, _listing,
                                       _split)
from jarvis.v2.model import OpenQuestion
from jarvis.v2.discord.rest import DiscordRest
from jarvis.v2.model import ProviderName, Role, TaskState
from jarvis.v2.provider import Decision, Event, EventKind as K, SessionHandle, Usage
from jarvis.v2.router import Router
from jarvis.v2.stores import Stores

TOKEN = "synthetic-discord-secret-never-in-content"
BOT = "botid"
OWNER = "ownerid"
GUILD = "guild1"
PROJECT_CHANNEL = "chan-project"
TASK_THREAD = "chan-task"
OTHER_THREAD = "chan-other-task"
PLAIN_CHANNEL = "chan-plain"
DM_CHANNEL = "chan-dm"


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        time.sleep(0.005)


class FakeTransport:
    """Every REST call lands here; the token may appear only in the header."""

    def __init__(self):
        self.calls = []
        # Standing answers: (predicate(method, path), response).
        self.rules = []

    def __call__(self, method, url, headers, timeout, **kwargs):
        assert headers == {"Authorization": f"Bot {TOKEN}"}
        assert TOKEN not in repr(kwargs)
        path = url.removeprefix(config.DISCORD_API)
        self.calls.append({"method": method, "path": path, **deepcopy(kwargs)})
        for matches, response in self.rules:
            if matches(method, path):
                return response
        return httpx.Response(200, json={"id": str(len(self.calls))})


def payload(call):
    return call["json"] if "json" in call else json.loads(call["data"]["payload_json"])


class FakeListener:
    def __init__(self, handler):
        self.handler = handler
        self.started = False
        self.stopped = False
        self.bot_id, self.owner_id = BOT, OWNER

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def feed(self, message):
        self.handler(message, self.bot_id, self.owner_id)


class FakeControl:
    """Records the verbs WP11's runner will receive; acts on nothing."""

    def __init__(self, stores):
        self.stores = stores
        self.calls = []
        self.fail = None

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self.fail:
            raise ControlError(self.fail)

    def start(self, task_id):
        self._record("start", task_id)
        return self.stores.tasks.get(task_id)

    def steer(self, task_id, text, *, spoken=False):
        self._record("steer", task_id, text, spoken=spoken)

    def answer_question(self, task_id, index, text):
        self._record("answer_question", task_id, index, text)

    def cancel(self, task_id):
        self._record("cancel", task_id)
        return self.stores.tasks.get(task_id)

    def resume(self, task_id, *, provider=None):
        self._record("resume", task_id, provider=provider)
        return self.stores.tasks.get(task_id)

    def status(self, task_id):
        self._record("status", task_id)
        return self.stores.tasks.get(task_id)

    def list_tasks(self, project_id=None, *, active_only=True):
        return self.stores.tasks.list()

    def named(self, name):
        return [call for call in self.calls if call[0] == name]


class FakeFast:
    """The fast path, scripted: one TEXT then TURN_FINISHED (+ optional proposal)."""

    name = ProviderName.FAST

    def __init__(self):
        self.reply = "the fast path answered"
        self.proposal = None
        self.messages = []

    def health(self):
        return True, "fake"

    def start(self, thread, brief, permit):
        return SessionHandle(thread.id, self.name, "fake:" + thread.id, {})

    resume = start

    def usage(self, handle):
        return Usage()

    def send(self, handle, message):
        self.messages.append(message)
        yield Event(K.TURN_STARTED, handle.thread_id)
        yield Event(K.TEXT, handle.thread_id, {"text": self.reply})
        data = {"stop": "end"}
        if self.proposal:
            data["proposal"] = dict(self.proposal)
        yield Event(K.TURN_FINISHED, handle.thread_id, data)

    def interrupt(self, handle):
        pass

    def answer(self, handle, req_id, decision):
        pass

    def close(self, handle):
        pass


class GateFast(FakeFast):
    """Announces each gate consultation before `permit` decides, as the
    Claude hook (every tool use) and Codex (every escalation) do."""

    def __init__(self):
        super().__init__()
        self.calls = []

    def start(self, thread, brief, permit):
        return SessionHandle(thread.id, self.name, "fake:" + thread.id,
                             {"permit": permit, "brief": brief})

    resume = start

    def send(self, handle, message):
        yield Event(K.TURN_STARTED, handle.thread_id)
        command = message.text.removeprefix("run:")
        req_id = f"gate-{len(self.calls)}"
        yield Event(K.APPROVAL_REQUESTED, handle.thread_id,
                    {"req_id": req_id, "tool": "Bash", "args": {"command": command},
                     "command": command})
        decision = Decision(handle.native["permit"]("Bash", {"command": command},
                                                    handle.native["brief"]))
        self.calls.append((command, decision))
        yield Event(K.APPROVAL_RESOLVED, handle.thread_id,
                    {"req_id": req_id, "decision": decision.value})
        yield Event(K.TURN_FINISHED, handle.thread_id, {"stop": "end"})


def message(content="", channel=DM_CHANNEL, *, guild=GUILD, author=OWNER,
            bot=False, mention=False, **extra):
    body = {"channel_id": channel, "content": content,
            "author": {"id": author, "bot": bot},
            "mentions": [{"id": BOT}] if mention else []}
    if guild is not None:
        body["guild_id"] = guild
    body.update(extra)
    return body


def dm(content="", **kwargs):
    return message(content, channel=DM_CHANNEL, guild=None, **kwargs)


class DiscordRoutingChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        token_path = root / "discord_token.json"
        token_path.write_text(json.dumps({"bot_token": TOKEN, "owner_id": OWNER}))
        for name, value in (("DISCORD_TOKEN_PATH", token_path),
                            # Never the owner's real guild file (B1).
                            ("DISCORD_GUILD_PATH", root / "discord_guild.json"),
                            ("ALLOWLIST_PATH", root / "allowlist.json")):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        network = patch.object(httpx, "request", side_effect=AssertionError("live network"))
        network.start()
        self.addCleanup(network.stop)

        self.stores = Stores(root / "state")
        self.transport = FakeTransport()
        self.rest = DiscordRest(self.transport)
        self.provider = FakeFast()
        holder = {}
        self.approvals = PendingApprovals(
            timeout_s=3.0,
            on_request=lambda request: holder["daemon"]._approval_requested(request),
            on_resolve=lambda request, decision, resolution:
                holder["daemon"]._approval_resolved(request, decision, resolution))
        self.daemon = Daemon(self.stores, {ProviderName.FAST: self.provider},
                             lambda thread_id, brief: (lambda *_a: Decision.ALLOW),
                             0, self.approvals)
        holder["daemon"] = self.daemon
        self.addCleanup(self.daemon.stop)
        self.control = FakeControl(self.stores)
        self.router = Router(self.stores)
        self.surface = DiscordRouter(
            self.daemon, self.stores, self.router, self.approvals, self.control,
            self.rest, FakeListener, announce=lambda text: None,
            dm_channel=lambda: DM_CHANNEL, turn_timeout_s=5)
        self.addCleanup(self.surface.stop)
        self.listener = self.surface.listener
        # Chats are posted by the mirror (PR C); its 4-per-5-s pacing is
        # discord_mirror_check's subject, not this suite's.
        self.surface.mirror.rate_posts = 1000
        self.surface.start()

        self.project = self.stores.projects.create(
            "Jarvis", str(root), discord_channel_id=PROJECT_CHANNEL)
        self.task = self.stores.tasks.create(self.project.id, "migrate the skills folder",
                                             discord_thread_id=TASK_THREAD)
        self.stores.tasks.save(self.task)

    # -- helpers -----------------------------------------------------------

    def posts(self, channel=None):
        out = []
        for call in self.transport.calls:
            if not call["path"].endswith("/messages"):
                continue
            where = call["path"].split("/")[2]
            if channel is None or where == channel:
                out.append((where, payload(call)))
        return out

    def texts(self, channel=None):
        return [body.get("content", "") for _, body in self.posts(channel)]

    def chats(self, project_id=None):
        return [t for t in self.stores.threads.list()
                if t.role == Role.CHAT and (project_id is None or t.project_id == project_id)]

    def chat_place(self, project_id, before=()):
        """The Discord thread of the newest chat opened in a project (PR C)."""
        wait_for(lambda: [t for t in self.chats(project_id)
                          if t.id not in before and t.surface])
        chat = [t for t in self.chats(project_id) if t.id not in before and t.surface][-1]
        return chat, chat.surface.split(":", 1)[1]

    def idle(self, thread_id):
        """Until the daemon has no turn running on this chat."""
        def done():
            with self.daemon._lock:
                session = self.daemon._sessions.get(thread_id)
                return session is None or session.worker is None
        wait_for(done)

    def ask(self, tool="Bash", command="pnpm run deploy --prod", task_id=None):
        """Ask for an approval from another thread, as a provider would."""
        result = {}
        request = ApprovalRequest(tool=tool, args={"command": command},
                                  command=command, task_id=task_id, origin="task test")
        thread = threading.Thread(
            target=lambda: result.update(decision=self.approvals.ask(request)), daemon=True)
        posted = len(self.posts())
        thread.start()
        wait_for(lambda: bool(self.approvals.pending()))
        wait_for(lambda: len(self.posts()) > posted)
        # Wait for *this* request to have been posted and its channel recorded,
        # not merely for some approval post to exist: with two asks open, the
        # looser condition is already true when the second one is still in the air.
        wait_for(lambda: request.req_id in self.surface._approval_channels)
        # ...and its post recorded (the map the buttons and S1's restart
        # strip both read), which now happens before the ping line goes out.
        wait_for(lambda: request.req_id in self.surface._approval_messages)
        return request, result, thread

    # -- who is heard ------------------------------------------------------

    def test_only_the_owner_and_never_a_bot(self):
        self.listener.feed(dm("hello", author="stranger"))
        self.listener.feed(dm("hello", author=OWNER, bot=True))
        self.listener.feed(message("hello", channel=TASK_THREAD, author="stranger"))
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.control.calls, [])

    def test_mention_is_lifted_only_where_jarvis_owns_the_channel(self):
        self.listener.feed(message("what is up", channel=TASK_THREAD))
        self.assertEqual([call[1][0] for call in self.control.named("steer")], [self.task.id])

        # A top-level message in a project channel is a new chat in its own
        # thread (PR C, O-C1): the reply is posted there, not in the channel.
        self.listener.feed(message("what is up", channel=PROJECT_CHANNEL, id="900001"))
        _chat, place = self.chat_place(self.project.id)
        wait_for(lambda: self.texts(place))
        self.assertIn("fast path answered", self.texts(place)[0])
        self.assertEqual(self.texts(PROJECT_CHANNEL), [])

        # A channel Jarvis does not own is not a place (plan §4.1): ignored,
        # with a mention or without.
        before = len(self.transport.calls)
        self.listener.feed(message("what is up", channel=PLAIN_CHANNEL))
        self.listener.feed(message("what is up", channel=PLAIN_CHANNEL, mention=True))
        time.sleep(0.2)
        self.assertEqual(len(self.transport.calls), before)

    # -- approvals ---------------------------------------------------------

    def test_approval_posts_to_the_task_thread_with_the_whole_command(self):
        command = "pnpm run deploy --prod && gh release create v9"
        request, result, worker = self.ask(command=command, task_id=self.task.id)
        channel, body = self.posts(TASK_THREAD)[0]
        self.assertEqual(channel, TASK_THREAD)
        self.assertIn(command, body["content"])
        self.assertIn(request.code, body["content"])

        self.listener.feed(message(f"yes {request.code}", channel=TASK_THREAD))
        worker.join(2)
        self.assertEqual(result["decision"], Decision.ALLOW)
        self.assertTrue(any("Authorized" in t for t in self.texts(TASK_THREAD)))

    def test_approval_without_a_task_goes_to_the_owner_dm(self):
        request, result, worker = self.ask(command="rm -rf build")
        self.assertEqual(self.posts(DM_CHANNEL)[0][0], DM_CHANNEL)
        self.assertIn("rm -rf build", self.posts(DM_CHANNEL)[0][1]["content"])
        self.listener.feed(dm(f"no {request.code}"))
        worker.join(2)
        self.assertEqual(result["decision"], Decision.DENY)

    # -- auto mode is quiet (2026-10-08) ------------------------------------

    def test_auto_settled_tool_calls_never_reach_discord(self):
        """Ten tool calls that the policy settles on its own send no DM, no
        thread post, no ping and no resolution post. Fails against the daemon
        that forwarded every provider gate event as an approval_requested."""
        from jarvis.v2.model import PermissionProfile
        from jarvis.v2.provider import Brief, UserMessage

        gate = GateFast()
        self.daemon.providers[ProviderName.FAST] = gate
        brief = Brief(role=Role.IMPLEMENTER, cwd=self.tmp.name,
                      profile=PermissionProfile.AUTO, task_id=self.task.id)
        thread = self.daemon.open_thread(self.project.id, Role.IMPLEMENTER,
                                         ProviderName.FAST, brief)
        before = len(self.transport.calls)
        questions = self.daemon.bus.subscribe(lambda r: r.get("kind") in (
            "approval_requested", "approval_resolved"))
        self.addCleanup(self.daemon.bus.unsubscribe, questions)
        for index in range(10):
            self.daemon.send(thread.id, UserMessage(f"run:echo {index}"))
            wait_for(lambda: len(gate.calls) == index + 1)
            wait_for(lambda: self.daemon._sessions[thread.id].worker is None
                     or not self.daemon._sessions[thread.id].worker.is_alive())
        self.assertEqual([d for _, d in gate.calls], [Decision.ALLOW] * 10)
        time.sleep(0.3)        # the watcher thread has had its chance to post
        self.assertEqual(self.transport.calls[before:], [])
        self.assertEqual(self.approvals.pending(), [])
        self.assertTrue(questions.empty(), "no approval event reached the bus at all")

    def test_a_question_without_a_broker_code_is_never_posted(self):
        """The gateway's own guard: only the broker asks the owner, and every
        broker question carries a code. A provider-shaped record that reaches
        the bus anyway posts nothing, and its resolution posts nothing."""
        before = len(self.transport.calls)
        self.daemon.bus.publish({"kind": "approval_requested", "thread_id": "t",
                                 "data": {"req_id": "gate-1", "tool": "Bash",
                                          "args": {"command": "ls"}, "command": "ls",
                                          "task_id": self.task.id}})
        self.daemon.bus.publish({"kind": "approval_resolved", "thread_id": "t",
                                 "data": {"req_id": "gate-1", "decision": "allow"}})
        time.sleep(0.3)
        self.assertEqual(self.transport.calls[before:], [])
        # ...while a real question still reaches the owner, once.
        request, result, worker = self.ask(command="rm -rf build")
        self.assertEqual(len(self.posts(DM_CHANNEL)), 1)
        self.listener.feed(dm(f"no {request.code}"))
        worker.join(2)
        self.assertEqual(result["decision"], Decision.DENY)

    # -- PR A: silent posts, the ping line, the DM fallback -----------------

    def test_a_thread_approval_is_silent_and_followed_by_the_ping_line(self):
        """D1: the approval post itself never notifies; one `<@owner>` line
        right after it does. The buttons stay on the approval, not the ping."""
        request, result, worker = self.ask(task_id=self.task.id)
        wait_for(lambda: len(self.posts(TASK_THREAD)) == 2)
        (_, approval), (_, ping) = self.posts(TASK_THREAD)
        self.assertEqual(approval["flags"], 4096)
        self.assertEqual(approval["allowed_mentions"], {"parse": []})
        self.assertIn("components", approval)
        self.assertEqual(ping, {"content": f"<@{OWNER}>",
                                "allowed_mentions": {"parse": [], "users": [OWNER]}})
        posted = [c for c in self.transport.calls if c["path"].endswith("/messages")]
        self.assertEqual(self.surface._approval_messages[request.req_id][1],
                         str(self.transport.calls.index(posted[0]) + 1))
        self.approvals.shutdown()
        worker.join(2)

    def test_a_dm_approval_has_no_ping_line_and_no_silent_flag(self):
        request, result, worker = self.ask(command="rm -rf build")
        time.sleep(0.05)
        [(_, body)] = self.posts(DM_CHANNEL)
        self.assertNotIn("flags", body)
        self.assertFalse([c for c in self.transport.calls if f"<@{OWNER}>" in json.dumps(c)])
        self.approvals.shutdown()
        worker.join(2)

    def test_a_failed_thread_post_asks_in_the_dm_and_only_the_dm_answers(self):
        self.transport.rules.append((
            lambda method, path: method == "POST" and path == f"/channels/{TASK_THREAD}/messages",
            httpx.Response(403, json={"code": 50013, "message": "Missing Permissions"})))
        request, result, worker = self.ask(task_id=self.task.id)
        self.assertEqual(self.surface._approval_channels[request.req_id], DM_CHANNEL)
        channel, message_id, code = self.surface._approval_messages[request.req_id]
        self.assertEqual((channel, code), (DM_CHANNEL, request.code))
        # Persisted with S1's map, so a restart strips these buttons too.
        saved = json.loads(self.surface._posts_path.read_text())
        self.assertEqual(saved[request.req_id], [DM_CHANNEL, message_id, request.code])
        self.assertIn(request.code, self.posts(DM_CHANNEL)[0][1]["content"])
        self.assertNotIn("flags", self.posts(DM_CHANNEL)[0][1])
        self.assertFalse([c for c in self.transport.calls if f"<@{OWNER}>" in json.dumps(c)])
        # Asked in the DM now: the thread's yes resolves nothing.
        self.transport.rules.clear()
        self.listener.feed(message(f"yes {request.code}", channel=TASK_THREAD))
        self.assertTrue(self.approvals.pending())
        self.listener.feed(dm(f"yes {request.code}"))
        worker.join(2)
        self.assertEqual(result["decision"], Decision.ALLOW)

    def test_review6_an_approval_shows_the_whole_command_minus_secret_values(self):
        envdir = Path(self.tmp.name) / "envdir"
        envdir.mkdir()
        secret = "sk-test-PRA-approval-0123456789"
        (envdir / ".env").write_text(f"FAKE_API_KEY={secret}\n")
        from jarvis.tools import secrets
        command = f"curl -H 'Authorization: Bearer {secret}' https://api.example/deploy"
        with patch.object(secrets, "_search_dirs", return_value=[envdir]):
            request, result, worker = self.ask(command=command, task_id=self.task.id)
        body = self.posts(TASK_THREAD)[0][1]["content"]
        self.assertNotIn(secret, repr(self.transport.calls))
        self.assertIn("curl -H 'Authorization: Bearer ", body)        # the rest stays readable
        self.assertIn("https://api.example/deploy", body)
        self.assertIn("redacted", body)
        self.approvals.shutdown()
        worker.join(2)

    def test_approval_finds_the_thread_in_the_sidecar(self):
        """Bug 1: the sidecar is read first, so a task record that lost its
        thread id still has its approvals asked in its thread."""
        task = self.stores.tasks.create(self.project.id, "sidecar only")
        self.stores.tasks.save(task)
        sidecar = self.stores.tasks.path(task.id).with_name("discord.json")
        sidecar.write_text(json.dumps({"discord_thread_id": OTHER_THREAD}))
        request, result, worker = self.ask(task_id=task.id)
        self.assertEqual(self.surface._approval_channels[request.req_id], OTHER_THREAD)
        self.approvals.shutdown()
        worker.join(2)

    def test_replies_in_a_guild_are_silent_and_in_the_dm_are_not(self):
        self.listener.feed(message("status", channel=TASK_THREAD))
        self.listener.feed(dm("projects"))
        thread_posts = self.posts(TASK_THREAD)
        dm_posts = self.posts(DM_CHANNEL)
        self.assertTrue(thread_posts and dm_posts)
        self.assertTrue(all(body.get("flags") == 4096 for _, body in thread_posts))
        self.assertTrue(all("flags" not in body for _, body in dm_posts))

    def test_a_code_typed_anywhere_else_resolves_nothing(self):
        other = self.stores.tasks.create(self.project.id, "other", discord_thread_id=OTHER_THREAD)
        self.stores.tasks.save(other)
        request, result, worker = self.ask(task_id=self.task.id)
        for channel, guild in ((OTHER_THREAD, GUILD), (PROJECT_CHANNEL, GUILD),
                               (DM_CHANNEL, None)):
            self.listener.feed(message(f"yes {request.code}", channel=channel, guild=guild))
        # A guild channel Jarvis does not own is not a place at all (PR C,
        # plan §4.1): with a mention or without, nothing there is read.
        self.listener.feed(message(f"yes {request.code}", channel=PLAIN_CHANNEL))
        self.listener.feed(message(f"yes {request.code}", channel=PLAIN_CHANNEL, mention=True))
        self.assertEqual(self.texts(PLAIN_CHANNEL), [])

        self.assertTrue(self.approvals.pending(), "the request must still be open")
        self.assertFalse(self.control.named("steer"))
        for channel in (OTHER_THREAD, PROJECT_CHANNEL, DM_CHANNEL):
            self.assertTrue(any("asked elsewhere" in t for t in self.texts(channel)), channel)
        self.approvals.shutdown()
        worker.join(2)
        self.assertEqual(result["decision"], Decision.DENY)

    def test_a_spoken_yes_authorizes_nothing_and_steers_instead(self):
        request, result, worker = self.ask(task_id=self.task.id)
        note = {"attachments": [{"content_type": "audio/ogg", "url": "u", "size": 10,
                                 "waveform": "x"}], "flags": v1gw.VOICE_MESSAGE_FLAG}
        with patch.object(v1gw, "_download", return_value=b"audio"), \
             patch("jarvis.voice.stt", return_value=f"yes {request.code}"):
            self.listener.feed(message("", channel=TASK_THREAD, **note))
        self.assertTrue(self.approvals.pending(), "a transcription may never authorize")
        steers = self.control.named("steer")
        self.assertEqual([call[1][1] for call in steers], [f"yes {request.code}"])
        self.assertTrue(steers[0][2]["spoken"])
        self.approvals.shutdown()
        worker.join(2)
        self.assertEqual(result["decision"], Decision.DENY)

    def test_a_bare_yes_with_two_open_requests_asks_for_the_code(self):
        first, first_result, first_worker = self.ask(command="one", task_id=self.task.id)
        second, second_result, second_worker = self.ask(command="two", task_id=self.task.id)
        self.listener.feed(message("yes", channel=TASK_THREAD))
        self.assertEqual(len(self.approvals.pending()), 2)
        asked = [t for t in self.texts(TASK_THREAD) if "Which one?" in t]
        self.assertTrue(asked)
        self.assertIn(first.code, asked[0])
        self.assertIn(second.code, asked[0])

        self.listener.feed(message(f"yes {second.code}", channel=TASK_THREAD))
        second_worker.join(2)
        self.assertEqual(second_result["decision"], Decision.ALLOW)
        self.approvals.shutdown()
        first_worker.join(2)
        self.assertEqual(first_result["decision"], Decision.DENY)

    def test_a_bare_yes_with_one_open_request_answers_it(self):
        request, result, worker = self.ask(task_id=self.task.id)
        self.listener.feed(message("yes", channel=TASK_THREAD))
        worker.join(2)
        self.assertEqual(result["decision"], Decision.ALLOW)

    def test_always_mints_an_allowlist_entry_through_the_broker(self):
        request, result, worker = self.ask(command="pnpm build", task_id=self.task.id)
        self.listener.feed(message(f"always {request.code}", channel=TASK_THREAD))
        worker.join(2)
        self.assertEqual(result["decision"], Decision.ALLOW)
        entries = json.loads(config.ALLOWLIST_PATH.read_text())
        self.assertEqual(entries, [{"tool": "run_command", "prefix": "pnpm"}])

    def test_resolution_is_announced_once_in_the_channel_that_was_asked(self):
        request, result, worker = self.ask(task_id=self.task.id)
        self.listener.feed(message(f"no {request.code}", channel=TASK_THREAD))
        worker.join(2)
        wait_for(lambda: any("answered here" in t for t in self.texts(TASK_THREAD)))
        # The map is one-shot: the same code is now unknown everywhere.
        self.listener.feed(message(f"yes {request.code}", channel=TASK_THREAD))
        self.assertTrue(any("No open authorization" in t for t in self.texts(TASK_THREAD)))

    # -- S1: buttons, standing rules, nudges, answers -----------------------

    def test_approval_posts_carry_approve_and_deny_and_never_always(self):
        request, result, worker = self.ask(task_id=self.task.id)
        _, body = self.posts(TASK_THREAD)[0]
        buttons = [b for row in body["components"] for b in row["components"]]
        self.assertEqual([b["label"] for b in buttons], ["Approve", "Deny"])
        self.assertEqual([b["custom_id"] for b in buttons],
                         [f"jv:a:{request.code}", f"jv:d:{request.code}"])
        self.assertNotIn(request.req_id, json.dumps(body))      # the broker id stays home
        self.assertIn(f"/always {request.code}", body["content"])
        message_id = self.surface._approval_messages[request.req_id][1]
        self.assertEqual(self.surface._approval_messages[request.req_id],
                         (TASK_THREAD, message_id, request.code))
        self.approvals.shutdown()
        worker.join(2)

    def test_a_resolution_anywhere_strips_the_buttons(self):
        request, result, worker = self.ask(task_id=self.task.id)
        message_id = self.surface._approval_messages[request.req_id][1]
        self.approvals.resolve(request.req_id, Decision.DENY)        # the HUD card, say
        worker.join(2)
        path = f"/channels/{TASK_THREAD}/messages/{message_id}"
        wait_for(lambda: any(c["path"] == path for c in self.transport.calls))
        edit = next(c for c in self.transport.calls if c["path"] == path)
        self.assertEqual((edit["method"], edit["json"]["components"]), ("PATCH", []))
        self.assertNotIn("content", edit["json"])                  # the command stays readable
        self.assertNotIn(request.req_id, self.surface._approval_messages)
        # The on-disk copy (for stripping after a restart) forgets it too.
        posts = Path(self.stores.root) / "discord" / "approval-posts.json"
        wait_for(lambda: json.loads(posts.read_text()) == {})

    def test_always_is_refused_where_no_standing_rule_can_be_made(self):
        request, result, worker = self.ask(command="git status && rm -rf build",
                                           task_id=self.task.id)
        self.assertNotIn("/always", self.posts(TASK_THREAD)[0][1]["content"])
        self.listener.feed(message(f"always {request.code}", channel=TASK_THREAD))
        self.assertTrue(self.approvals.pending(), "a refused always must not approve")
        self.assertIn("can't become a standing rule", self.texts(TASK_THREAD)[-1])
        self.assertFalse(config.ALLOWLIST_PATH.exists())
        self.approvals.shutdown()
        worker.join(2)
        self.assertEqual(result["decision"], Decision.DENY)

    def test_every_keyword_reply_ends_with_its_slash_command(self):
        cases = [("steer: use the other parser", "/steer"), ("cancel", "/cancel"),
                 ("status", "/status"), ("projects", "/project list"),
                 ("tasks", "/status"), (f"resume {self.task.id}", "/resume")]
        for text, slash in cases:
            with self.subTest(text=text):
                before = len(self.posts(TASK_THREAD))
                self.listener.feed(message(text, channel=TASK_THREAD))
                body = self.posts(TASK_THREAD)[before][1]
                self.assertTrue(body["content"].endswith(f"(next time: `{slash}`)"), body)
        request, result, worker = self.ask(task_id=self.task.id)
        self.listener.feed(message(f"no {request.code}", channel=TASK_THREAD))
        worker.join(2)
        self.assertTrue(any(t.startswith("Denied.") and t.endswith("(next time: `/no`)")
                            for t in self.texts(TASK_THREAD)))
        self.listener.feed(dm("task: think about lunch"))
        self.assertTrue(self.texts(DM_CHANNEL)[-1].endswith("(next time: `/task`)"))
        # Plain conversation is not a keyword and is never nudged.
        self.listener.feed(dm("what is up"))
        wait_for(lambda: "fast path answered" in self.texts(DM_CHANNEL)[-1])
        self.assertNotIn("next time", self.texts(DM_CHANNEL)[-1])

    def _clarifying(self, options=("pnpm", "npm")):
        self.stores.tasks.transition(self.task.id, TaskState.CLARIFYING)
        task = self.stores.tasks.get(self.task.id)
        task.spec.questions = [OpenQuestion("Which runner?", False, assumed="pytest"),
                               OpenQuestion("Which package manager?", True, list(options))]
        self.stores.tasks.save(task)
        return task

    def test_typed_text_in_a_clarifying_thread_answers_its_open_question(self):
        self._clarifying()
        self.listener.feed(message("pnpm, please", channel=TASK_THREAD))
        self.assertEqual(self.control.named("answer_question"),
                         [("answer_question", (self.task.id, 1, "pnpm, please"), {})])
        self.assertEqual(self.control.named("steer"), [])
        self.assertIn("Answered", self.texts(TASK_THREAD)[-1])
        # `steer:` is still an explicit steer, not an answer.
        self.listener.feed(message("steer: also run lint", channel=TASK_THREAD))
        self.assertEqual(len(self.control.named("steer")), 1)

    def test_a_voice_note_never_answers_a_question(self):
        self._clarifying()
        note = {"attachments": [{"content_type": "audio/ogg", "url": "u", "size": 10,
                                 "waveform": "x"}], "flags": v1gw.VOICE_MESSAGE_FLAG}
        with patch.object(v1gw, "_download", return_value=b"audio"), \
             patch("jarvis.voice.stt", return_value="pnpm"):
            self.listener.feed(message("", channel=TASK_THREAD, **note))
        self.assertEqual(self.control.named("answer_question"), [])
        self.assertEqual(self.control.named("steer"), [])
        self.assertIn("voice note", self.texts(TASK_THREAD)[-1])

    def test_without_an_open_question_plain_text_still_steers(self):
        self.listener.feed(message("try the other parser", channel=TASK_THREAD))
        self.assertEqual(self.control.named("answer_question"), [])
        self.assertEqual(self.control.named("steer")[0][1], (self.task.id, "try the other parser"))

    # -- verbs -------------------------------------------------------------

    def test_steer_cancel_status_resume_projects_tasks(self):
        self.listener.feed(message("steer: use the other parser", channel=TASK_THREAD))
        self.assertEqual(self.control.named("steer")[0][1],
                         (self.task.id, "use the other parser"))

        self.listener.feed(message("cancel", channel=TASK_THREAD))
        self.assertEqual(self.control.named("cancel")[0][1], (self.task.id,))

        self.listener.feed(message("status", channel=TASK_THREAD))
        self.assertEqual(self.control.named("status")[0][1], (self.task.id,))
        embeds = [body for _, body in self.posts(TASK_THREAD) if "embeds" in body]
        self.assertIn(self.task.id, embeds[0]["embeds"][0]["title"])

        self.listener.feed(dm(f"resume {self.task.id} on codex"))
        self.assertEqual(self.control.named("resume")[0][1:],
                         ((self.task.id,), {"provider": ProviderName.CODEX}))

        self.listener.feed(dm("projects"))
        self.assertIn("Jarvis", self.texts(DM_CHANNEL)[-1])
        self.listener.feed(dm("tasks"))
        self.assertIn(self.task.id, self.texts(DM_CHANNEL)[-1])

    def test_a_control_error_is_shown_rather_than_raised(self):
        self.control.fail = "That task is already done."
        self.listener.feed(message("cancel", channel=TASK_THREAD))
        self.assertIn("already done", self.texts(TASK_THREAD)[-1])

    # -- intake ------------------------------------------------------------

    def test_task_intake_in_a_project_channel_creates_and_starts(self):
        self.listener.feed(message("task: rewrite the parser", channel=PROJECT_CHANNEL))
        started = self.control.named("start")
        self.assertEqual(len(started), 1)
        task = self.stores.tasks.get(started[0][1][0])
        self.assertEqual((task.project_id, task.brief), (self.project.id, "rewrite the parser"))
        self.assertIn(f"Opened task {task.id} in Jarvis", self.texts(PROJECT_CHANNEL)[-1])

    def test_task_intake_in_a_dm_uses_the_inbox_unless_a_project_is_named(self):
        self.listener.feed(dm("task: think about lunch"))
        inbox_task = self.stores.tasks.get(self.control.named("start")[0][1][0])
        self.assertTrue(self.stores.projects.get(inbox_task.project_id).inbox)

        self.listener.feed(dm("task: in Jarvis: rewrite the parser"))
        placed = self.stores.tasks.get(self.control.named("start")[1][1][0])
        self.assertEqual((placed.project_id, placed.brief),
                         (self.project.id, "rewrite the parser"))

        self.listener.feed(dm("task: in Nowhere: do something"))
        self.assertIn("don't know a project", self.texts(DM_CHANNEL)[-1])
        self.assertEqual(len(self.control.named("start")), 2)

    def test_an_archived_projects_old_task_thread_opens_and_starts_nothing(self):
        """Decisions B1: an archived project takes no work, from any door.
        Its old task thread used to place `task: X` in it and start it."""
        from jarvis.v2.model import utcnow
        self.stores.tasks.transition(self.task.id, TaskState.CANCELLED)
        project = self.stores.projects.get(self.project.id)
        project.archived = utcnow()
        self.stores.projects.save(project)
        before = {t.id for t in self.stores.tasks.list()}
        for channel in (TASK_THREAD, PROJECT_CHANNEL):
            for text in ("task: add a divide button", "steer: use the other parser",
                         "resume " + self.task.id, "what is up", "yes"):
                with self.subTest(channel=channel, text=text):
                    self.listener.feed(message(text, channel=channel))
                    self.assertIn("archived", self.texts(channel)[-1])
        self.assertEqual({t.id for t in self.stores.tasks.list()}, before)
        self.assertEqual(self.control.calls, [])
        self.assertEqual(self.provider.messages, [])                     # no fast-path turn either
        # A stranger in the same thread is still not heard at all.
        posted = len(self.posts())
        self.listener.feed(message("task: x", channel=TASK_THREAD, author="stranger"))
        self.assertEqual(len(self.posts()), posted)

    def test_intake_into_a_project_archived_meanwhile_says_so(self):
        # The race past placement: the store refuses, the owner is told.
        from jarvis.v2.model import utcnow
        project = self.stores.projects.get(self.project.id)
        self.stores.tasks.transition(self.task.id, TaskState.CANCELLED)
        project.archived = utcnow()
        self.stores.projects.save(project)
        live = self.stores.projects.get(self.project.id)
        live.archived = None
        before = {t.id for t in self.stores.tasks.list()}
        # A stale, unarchived copy; verbs take a reply sink, not a channel id (S1).
        self.surface._intake("rewrite the parser", ChannelReply(self.surface, PROJECT_CHANNEL),
                             live)
        self.assertIn("opened nothing", self.texts(PROJECT_CHANNEL)[-1])
        self.assertEqual({t.id for t in self.stores.tasks.list()}, before)
        self.assertEqual(self.control.named("start"), [])

    # -- chat --------------------------------------------------------------

    def test_dm_and_channel_chat_each_reach_a_fast_path_thread(self):
        self.listener.feed(dm("what did we decide about the ledger?"))
        wait_for(lambda: self.texts(DM_CHANNEL))
        self.listener.feed(message("and in here?", channel=PROJECT_CHANNEL, mention=True,
                                   id="900002"))
        first, place = self.chat_place(self.project.id)
        wait_for(lambda: self.texts(place))
        threads = self.stores.threads.list()
        self.assertEqual(sorted(t.project_id for t in threads),
                         sorted([self.stores.projects.inbox().id, self.project.id]))
        self.assertTrue(all(t.role == Role.CHAT and t.provider == ProviderName.FAST
                            for t in threads))
        dm_chat = next(t for t in threads if t.project_id != self.project.id)
        self.assertEqual(dm_chat.surface, "dm")
        # The DM conversation is one chat: a second DM reuses it.
        self.idle(dm_chat.id)
        self.listener.feed(dm("and that other thing?"))
        wait_for(lambda: len(self.texts(DM_CHANNEL)) == 2)
        self.assertEqual(len(self.stores.threads.list()), 2)
        # A second top-level message in a project channel is a second chat
        # (O-C1); a message inside the first chat's thread stays in it.
        self.idle(first.id)
        self.listener.feed(message("still here?", channel=place, id="900003"))
        wait_for(lambda: len(self.texts(place)) == 2)
        self.assertEqual(len(self.stores.threads.list()), 2)
        self.listener.feed(message("a new subject", channel=PROJECT_CHANNEL, id="900004"))
        second, other_place = self.chat_place(self.project.id, before={first.id})
        self.assertNotEqual(other_place, place)
        self.assertEqual(len(self.stores.threads.list()), 3)

    def test_a_proposal_on_turn_finished_is_handed_to_the_router_once(self):
        handed = []
        original = self.router.on_turn_finished

        def counted(event, incoming=None):
            handed.append(event)
            return original(event, incoming)

        self.router.on_turn_finished = counted
        self.provider.proposal = {"brief": "convert the skills folder", "project": "Jarvis"}
        self.listener.feed(message("could you convert the skills folder",
                                   channel=PROJECT_CHANNEL, id="900005"))
        # With no task runner on this daemon, the chat mirror hands the
        # proposal to the router and posts its sentence in the chat's thread.
        first, place = self.chat_place(self.project.id)
        wait_for(lambda: any("Opened task" in t for t in self.texts(place)))
        self.assertEqual(len(handed), 1)
        opened = [t for t in self.stores.tasks.list() if t.id != self.task.id]
        self.assertEqual(len(opened), 1)
        # Replaying the same turn opens nothing: the router is idempotent per
        # turn id, so a runner that also consumes the event cannot double-open.
        self.assertEqual(original(handed[0], None),
                         f"Opened task {opened[0].id} in Jarvis: convert the skills folder. "
                         f"It will ask if anything is unclear.")
        self.assertEqual(len([t for t in self.stores.tasks.list() if t.id != self.task.id]), 1)

        # A turn that proposes nothing never reaches the router at all.
        self.provider.proposal = None
        self.idle(first.id)
        self.listener.feed(message("and again", channel=place, id="900006"))
        wait_for(lambda: len(self.texts(place)) >= 3)
        self.assertEqual(len(handed), 1)

    def test_cancel_inside_the_grace_window_withdraws_a_proposal(self):
        self.provider.proposal = {"brief": "convert the skills folder", "project": "Jarvis"}
        self.listener.feed(message("convert the skills folder", channel=PROJECT_CHANNEL,
                                   id="900007"))
        _chat, place = self.chat_place(self.project.id)
        wait_for(lambda: any("Opened task" in t for t in self.texts(place)))
        opened = [t for t in self.stores.tasks.list() if t.id != self.task.id][0]

        self.listener.feed(message(f"cancel {opened.id}", channel=PROJECT_CHANNEL))
        self.assertEqual(self.control.named("cancel"), [])
        self.assertEqual(self.stores.tasks.get(opened.id).state, TaskState.CANCELLED)
        self.assertIn("Withdrew", self.texts(PROJECT_CHANNEL)[-1])

    # -- limits and hygiene ------------------------------------------------

    def test_chat_splits_at_2000_and_only_approvals_are_attached_whole(self):
        self.provider.reply = "\n".join(f"line {i} " + "x" * 80 for i in range(120))
        self.listener.feed(dm("say a lot"))
        wait_for(lambda: " ".join(self.texts(DM_CHANNEL)).split() == self.provider.reply.split())
        chunks = self.texts(DM_CHANNEL)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 2000 for chunk in chunks))
        # Splitting moves whitespace at the seams and nothing else: no word of
        # the reply may be dropped, and none may be invented.
        self.assertEqual(" ".join(chunks).split(), self.provider.reply.split())

        request, result, worker = self.ask(command="echo " + "y" * 2500,
                                           task_id=self.task.id)
        attached = [call for call in self.transport.calls if "data" in call]
        self.assertEqual(len(attached), 1)
        body = attached[0]["files"][0][1][1].decode("utf-8")
        self.assertIn("y" * 2500, body)
        self.approvals.shutdown()
        worker.join(2)
        self.assertEqual(result["decision"], Decision.DENY)

    def test_listings_state_what_they_could_not_show(self):
        text = _listing("Projects", [f"row {i} " + "z" * 60 for i in range(200)])
        self.assertLessEqual(len(text), 2000)
        self.assertIn("more.", text)
        self.assertEqual(_listing("Active tasks", []), "Active tasks: none.")
        self.assertEqual(_split("short"), ["short"])

    def test_no_credential_value_reaches_any_request_body(self):
        self.listener.feed(dm("hello"))
        wait_for(lambda: self.texts(DM_CHANNEL))
        request, result, worker = self.ask(task_id=self.task.id)
        self.listener.feed(message(f"yes {request.code}", channel=TASK_THREAD))
        worker.join(2)
        for call in self.transport.calls:
            self.assertNotIn(TOKEN, json.dumps(payload(call)))
            self.assertNotIn(TOKEN, repr(call.get("files", "")))

    def test_an_unreadable_message_never_raises_at_the_listener(self):
        self.listener.feed({"channel_id": DM_CHANNEL})       # no author at all
        self.listener.feed(dm("   "))                        # nothing to say
        self.assertEqual(self.posts(), [])


class ListenerChecks(unittest.TestCase):
    """The v1 socket behaviour the subclass must not lose."""

    def test_intents_off_stops_without_retrying(self):
        said = []
        attempts = []

        def create_connection(url, timeout=None):
            attempts.append(url)
            raise v1gw.GatewayClosed(4014, "Disallowed intent(s)")

        fake = types.ModuleType("websocket")
        fake.create_connection = create_connection
        fake.WebSocketTimeoutException = TimeoutError
        listener = _V2Listener(lambda *_: None, announce=said.append)
        listener._token = TOKEN
        with patch.dict(sys.modules, {"websocket": fake}):
            listener._serve()
        self.assertEqual(len(attempts), 1)
        self.assertIn("Message Content Intent", said[0])
        self.assertNotIn(TOKEN, " ".join(said))

    def test_every_message_reaches_the_router_which_owns_the_gate(self):
        seen = []
        listener = _V2Listener(lambda message, bot, owner: seen.append(message),
                               announce=lambda text: None)
        listener._token = TOKEN
        listener.owner_id = OWNER
        frames = [{"op": 10, "d": {"heartbeat_interval": 60000}},
                  {"t": "READY", "d": {"user": {"id": BOT, "username": "jarvis"}}},
                  {"t": "MESSAGE_CREATE", "d": message("hi", channel=PLAIN_CHANNEL)}]

        class FakeWS:
            def __init__(self):
                self.sent = []

            def send(self, payload):
                self.sent.append(json.loads(payload))

            def settimeout(self, value):
                pass

            def close(self):
                pass

        ws = FakeWS()
        fake = types.ModuleType("websocket")
        fake.WebSocketTimeoutException = TimeoutError

        def recv(_ws):
            if frames:
                return frames.pop(0)
            listener._stop.set()
            raise TimeoutError

        with patch.dict(sys.modules, {"websocket": fake}), \
             patch.object(v1gw, "_recv_json", recv):
            listener._session(ws)
        wait_for(lambda: seen)
        self.assertEqual(seen[0]["channel_id"], PLAIN_CHANNEL)   # unmentioned, undropped
        self.assertEqual(ws.sent[0]["op"], 2)
        self.assertEqual(ws.sent[0]["d"]["token"], TOKEN)


class FakeRunner:
    """Records when it was served and stopped, and whether the Reporter was
    already on the bus at that moment."""

    def __init__(self, bus, log):
        self.bus, self.log = bus, log
        self.served = 0
        self.reporter_listening = None

    def serve(self, *, recover=True):
        self.served += 1
        self.log.append("serve")
        with self.bus._lock:
            subscribers = list(self.bus._subscribers.values())
        probe = {"kind": "task_status_changed"}
        self.reporter_listening = any(callable(f) and f(probe) for f in subscribers)

    def stop(self, timeout=5.0):
        self.log.append("runner.stop")


class SurfaceChecks(unittest.TestCase):
    """PR A wiring: one DiscordSurface, started and stopped in order, twice-safe."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        token_path = root / "discord_token.json"
        token_path.write_text(json.dumps({"bot_token": TOKEN, "owner_id": OWNER}))
        for name, value in (("DISCORD_TOKEN_PATH", token_path),
                            # Never the owner's real guild file (B1).
                            ("DISCORD_GUILD_PATH", root / "discord_guild.json"),
                            ("ALLOWLIST_PATH", root / "allowlist.json")):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        network = patch.object(httpx, "request", side_effect=AssertionError("live network"))
        network.start()
        self.addCleanup(network.stop)
        self.stores = Stores(root / "state")
        self.daemon = Daemon(self.stores, {ProviderName.FAST: FakeFast()},
                             lambda thread_id, brief: (lambda *_a: Decision.ALLOW), 0)
        self.addCleanup(self.daemon.stop)
        self.log = []
        self.daemon.runner = FakeRunner(self.daemon.bus, self.log)
        self.transport = FakeTransport()
        log = self.log

        class Listener(FakeListener):
            def start(self):
                log.append("gateway.start")

            def stop(self):
                log.append("gateway.stop")
        self.listener = Listener

    def surface(self):
        from jarvis.v2.discord.surface import DiscordSurface
        surface = DiscordSurface(self.daemon, control=FakeControl(self.stores),
                                 rest=DiscordRest(self.transport), router=Router(self.stores),
                                 listener_factory=self.listener, dm_channel=lambda: DM_CHANNEL,
                                 announce=lambda text: None, sync_commands=False)
        self.addCleanup(surface.stop)
        return surface

    def test_start_order_reporter_then_serve_then_gateway_and_idempotent(self):
        surface = self.surface()
        self.assertIsNone(surface.reporter)
        surface.start()
        surface.start()
        self.assertEqual(self.log, ["serve", "gateway.start"])
        self.assertTrue(self.daemon.runner.reporter_listening,
                        "the Reporter must be subscribed before serve() recovers tasks")
        self.assertIsNotNone(surface.reporter)
        original = surface.reporter.close
        surface.reporter.close = lambda flush=False: (self.log.append(f"reporter.close:{flush}"),
                                                      original(flush=flush))
        surface.stop()
        surface.stop()
        self.assertEqual(self.log[2:], ["gateway.stop", "runner.stop", "reporter.close:True"])
        self.assertFalse(surface.reporter._worker.is_alive())

    def test_start_discord_attaches_once_and_reports_the_reporter(self):
        from jarvis.v2.hud_api import discord_status
        kwargs = dict(rest=DiscordRest(self.transport), listener_factory=self.listener,
                      dm_channel=lambda: DM_CHANNEL, announce=lambda text: None,
                      sync_commands=False)
        surface = daemon_mod.start_discord(self.daemon, FakeControl(self.stores), **kwargs)
        self.addCleanup(surface.stop)
        self.assertIs(self.daemon.discord, surface)
        self.assertIs(daemon_mod.start_discord(self.daemon, **kwargs), surface)
        self.assertEqual(self.daemon.runner.served, 1)
        status = discord_status(self.daemon)
        self.assertEqual(status["reporter"]["state"], "ok")
        self.assertEqual(set(status["reporter"]),
                         {"state", "reason", "counters", "dropped", "last_error"})

    def test_a_surface_that_cannot_start_leaves_the_runner_serving(self):
        class Broken(FakeListener):
            def start(self):
                raise RuntimeError("no socket")
        with self.assertLogs("jarvis.v2.daemon", "WARNING"):
            surface = daemon_mod.start_discord(
                self.daemon, FakeControl(self.stores), rest=DiscordRest(self.transport),
                listener_factory=Broken, dm_channel=lambda: DM_CHANNEL,
                announce=lambda text: None, sync_commands=False)
        self.assertIsNone(surface)
        self.assertIsNone(self.daemon.discord)
        self.assertNotIn("runner.stop", self.log)
        # Recorded by class name, so GET /discord shows red rather than "pending".
        self.assertEqual(self.daemon.discord_error, "RuntimeError")
        from jarvis.v2.hud_api import discord_status
        status = discord_status(self.daemon)
        self.assertEqual(status["reporter"]["state"], "down")
        self.assertEqual(status["commands"], {"state": "failed", "count": 0,
                                              "synced_at": None, "error": "RuntimeError"})
        self.assertNotIn("no socket", json.dumps(status))


class LogLeakChecks(unittest.TestCase):
    """httpx logs every request's full URL at INFO, and an interaction reply's
    URL holds a live interaction token (found on the live daemon). The daemon's
    logging setup holds those libraries to WARNING."""

    APP, ITOKEN = "123456789012345678", "aW50ZXJhY3Rpb24tdG9rZW4tc3ludGhldGlj"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        token_path = Path(self.tmp.name) / "discord_token.json"
        token_path.write_text(json.dumps({"bot_token": TOKEN, "owner_id": OWNER}))
        patcher = patch.object(config, "DISCORD_TOKEN_PATH", token_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        import logging
        self.saved = {name: logging.getLogger(name).level for name in daemon_mod.QUIET_LOGGERS}
        root = logging.getLogger()
        self.root_handlers = list(root.handlers)
        self.root_level = root.level

        def restore():
            for name, level in self.saved.items():
                logging.getLogger(name).setLevel(level)
            root.handlers[:] = self.root_handlers
            root.setLevel(self.root_level)
        self.addCleanup(restore)

    def test_no_interaction_token_or_webhook_path_reaches_the_log(self):
        import logging
        records = []

        class Keep(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        keep = Keep(level=logging.DEBUG)
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).addHandler(keep)
            self.addCleanup(logging.getLogger(name).removeHandler, keep)
        root = logging.getLogger()
        root.handlers[:] = [logging.NullHandler()]        # basicConfig adds nothing here
        root.setLevel(logging.INFO)                      # the daemon's level
        daemon_mod.configure_logging()

        rest = DiscordRest(httpx.MockTransport(lambda request: httpx.Response(200, json={"id": "9"})))
        self.addCleanup(rest.close)
        rest.followup(self.APP, self.ITOKEN, content="Done.")
        rest.edit_original(self.APP, self.ITOKEN, content="Done.")
        text = "\n".join(records)
        self.assertNotIn(self.ITOKEN, text)
        self.assertNotIn("/webhooks/", text)
        self.assertNotIn(TOKEN, text)
        for name in daemon_mod.QUIET_LOGGERS:
            self.assertGreaterEqual(logging.getLogger(name).getEffectiveLevel(), logging.WARNING)


class DaemonStartupChecks(unittest.TestCase):
    def test_no_bundle_means_no_discord_and_no_remote_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(config, "DISCORD_TOKEN_PATH", Path(tmp) / "absent.json"):
                self.assertFalse(daemon_mod.discord_connected())

    def test_a_bundle_is_detected_without_the_token_leaving_the_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "discord_token.json"
            path.write_text(json.dumps({"bot_token": TOKEN, "owner_id": OWNER}))
            with patch.object(config, "DISCORD_TOKEN_PATH", path):
                self.assertTrue(daemon_mod.discord_connected())

    def test_verbs_before_the_runner_exists_answer_rather_than_crash(self):
        control = daemon_mod._NoControl()
        with self.assertRaises(ControlError):
            control.cancel("deadbeef")
        self.assertEqual(control.list_tasks(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
