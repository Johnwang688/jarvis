"""Free PR C checks: every chat is a Discord thread (plan §4, decisions C1).

Fakes only — a scripted provider, a fake Discord transport, a fake gateway
listener — and `httpx.request` raises on any call, so nothing here can reach
Discord or a model. A real v2 daemon runs on ephemeral ports so the HUD side
(send, move, transcript) goes through its real routes.

The ones worth keeping are written to bite:
  - the HUD's line on Discord is the *typed* words: an inlined file's contents
    never leave, a credential value is scrubbed, images are a note;
  - a message typed in Discord runs the same chat and is never echoed back;
  - a voice note runs a turn but never approves or answers anything;
  - a stale copy of a thread saved over a new surface keeps the surface;
  - nothing is ever deleted on Discord, and progress is the log, not the bus.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import http.client
import itertools
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from jarvis import config
from jarvis import discord_gateway as v1gw
from jarvis.tools import secrets as secrets_mod
from jarvis.v2 import projects as P
from jarvis.v2.approvals import ApprovalRequest, PendingApprovals
from jarvis.v2.daemon import Daemon
from jarvis.v2.discord import mirror as M
from jarvis.v2.discord.gateway import DiscordRouter
from jarvis.v2.discord.mirror import ChatMirror
from jarvis.v2.discord.rest import DiscordRest
from jarvis.v2.model import ProviderName, Role
from jarvis.v2.provider import Decision, Event, EventKind as K, SessionHandle, Usage
from jarvis.v2.router import Router
from jarvis.v2.stores import Stores, StoreError

TOKEN = "synthetic-discord-secret-never-in-content"
SECRET = "sk-or-v1-" + "f" * 64          # a credential-shaped value the scrub knows
BOT, OWNER, GUILD = "100000000000000001", "100000000000000002", "100000000000000003"
SCHOOL_CHANNEL = "700000000000000001"
TEST_CHANNEL = "700000000000000002"
UNGROUPED = "700000000000000009"
DM_CHANNEL = "700000000000000099"
_ids = itertools.count(800000000000000000)


def wait_for(predicate, timeout=5.0, what="condition"):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"{what} did not become true")
        time.sleep(0.01)


class FakeTransport:
    """Discord as far as the mirror can tell. The token only in the header."""

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.fail = []                      # [(method, path predicate, status, json, times)]
        self.violations = []

    def __call__(self, method, url, headers, timeout, **kwargs):
        if headers != {"Authorization": f"Bot {TOKEN}"}:
            self.violations.append("unexpected headers")
        if TOKEN in repr(kwargs):
            self.violations.append("the bot token reached a request body")
        path = url.removeprefix(config.DISCORD_API)
        with self.lock:
            self.calls.append({"method": method, "path": path, **deepcopy(kwargs)})
            for rule in list(self.fail):
                fmethod, match, status, body, times = rule
                if fmethod == method and match(path):
                    if times is not None:
                        rule_index = self.fail.index(rule)
                        if times <= 1:
                            self.fail.pop(rule_index)
                        else:
                            self.fail[rule_index] = (fmethod, match, status, body, times - 1)
                    return httpx.Response(status, json=body)
        return httpx.Response(200, json={"id": str(next(_ids))})

    # -- views -------------------------------------------------------------

    def posts(self, channel=None):
        out = []
        with self.lock:
            calls = list(self.calls)
        for call in calls:
            parts = call["path"].split("/")
            if call["method"] == "POST" and len(parts) == 4 and parts[1] == "channels" \
                    and parts[3] == "messages":
                if channel is None or parts[2] == channel:
                    body = call["json"] if "json" in call else json.loads(
                        call["data"]["payload_json"])
                    body = dict(body)
                    if "files" in call:
                        body["_files"] = [(f[1][0], f[1][1]) for f in call["files"]]
                    out.append(body)
        return out

    def texts(self, channel):
        return [p.get("content") or "" for p in self.posts(channel)]

    def made(self):
        """Every thread made: (parent channel, kind, response id order)."""
        with self.lock:
            return [c for c in self.calls if c["method"] == "POST" and c["path"].endswith("/threads")]

    def patches(self, channel):
        with self.lock:
            return [c.get("json") for c in self.calls
                    if c["method"] == "PATCH" and c["path"] == f"/channels/{channel}"]


class FakeListener:
    def __init__(self, handler):
        self.handler = handler
        self.bot_id, self.owner_id = BOT, OWNER

    def start(self):
        pass

    def stop(self):
        pass

    def feed(self, message):
        self.handler(message, self.bot_id, self.owner_id)


class Scripted:
    """A provider whose turns are scripted per message.

    `self.plan` maps a message's text to a list of steps: ("text", str),
    ("tool", name), ("thinking", str), ("delta", str), ("error", message),
    ("question", text) — which blocks the turn until `answer` is called — or
    ("wait",) which blocks until `release` is set."""

    name = ProviderName.FAST

    def __init__(self):
        self.plan = {}
        self.default = [("text", "the answer")]
        self.messages = []
        self.answers = []
        self.answered = threading.Event()
        self.release = threading.Event()
        self.stop = "end"

    def health(self):
        return True, "fake"

    def start(self, thread, brief, permit):
        return SessionHandle(thread.id, self.name, "fake:" + thread.id, {})

    resume = start

    def usage(self, handle):
        return Usage()

    def send(self, handle, message):
        self.messages.append(message)
        tid = handle.thread_id
        yield Event(K.TURN_STARTED, tid)
        stop = self.stop
        for step in self.plan.get(message.typed or message.text, self.default):
            kind = step[0]
            if kind == "text":
                yield Event(K.TEXT, tid, {"text": step[1]})
            elif kind == "delta":
                yield Event(K.TEXT_DELTA, tid, {"text": step[1]})
            elif kind == "thinking":
                yield Event(K.THINKING, tid, {"text": step[1]})
            elif kind == "tool":
                yield Event(K.TOOL_STARTED, tid, {"call_id": step[1], "name": step[1], "args": {}})
                yield Event(K.TOOL_FINISHED, tid, {"call_id": step[1], "name": step[1],
                                                   "ok": True, "summary": ""})
            elif kind == "error":
                yield Event(K.ERROR, tid, {"message": step[1], "fatal": False})
                stop = "error"
            elif kind == "question":
                self.answered.clear()
                self.awaiting = "q-1"
                yield Event(K.QUESTION, tid, {"req_id": "q-1", "text": step[1],
                                              "options": ["npm", "pnpm"]})
                self.answered.wait(5)
                yield Event(K.TEXT, tid, {"text": f"using {self.answers[-1][1]}"})
            elif kind == "wait":
                self.release.wait(5)
        yield Event(K.TURN_FINISHED, tid, {"stop": stop})

    def interrupt(self, handle):
        self.release.set()

    def answer(self, handle, req_id, decision):
        # Like the Codex provider: an unknown or already-answered request raises.
        if req_id != getattr(self, "awaiting", None):
            raise ValueError("Unknown or resolved request")
        self.awaiting = None
        self.answers.append((req_id, decision))
        self.answered.set()

    def close(self, handle):
        pass


def dm(content="", **extra):
    body = {"id": str(next(_ids)), "channel_id": DM_CHANNEL, "content": content,
            "author": {"id": OWNER}, "mentions": []}
    body.update(extra)
    return body


def guild_message(content, channel, **extra):
    body = {"id": str(next(_ids)), "channel_id": channel, "guild_id": GUILD,
            "content": content, "author": {"id": OWNER}, "mentions": []}
    body.update(extra)
    return body


class Harness(unittest.TestCase):
    rate_posts = 1000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        token_path = self.root / "discord_token.json"
        token_path.write_text(json.dumps({"bot_token": TOKEN, "owner_id": OWNER}))
        guild_path = self.root / "discord_guild.json"
        guild_path.write_text(json.dumps({
            "guild_id": GUILD, "category_id": "700000000000000100",
            "archive_category_id": "700000000000000101", "ungrouped_channel_id": UNGROUPED}))
        for name, value in (("DISCORD_TOKEN_PATH", token_path),
                            ("DISCORD_GUILD_PATH", guild_path),
                            ("ALLOWLIST_PATH", self.root / "allowlist.json")):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        network = patch.object(httpx, "request", side_effect=AssertionError("live network"))
        network.start()
        self.addCleanup(network.stop)
        # The transcript scrub redacts known credential values; this suite's
        # one known value is SECRET.
        known = patch.object(secrets_mod, "secret_values", return_value=[SECRET])
        known.start()
        self.addCleanup(known.stop)
        download = patch.object(v1gw, "_download", side_effect=AssertionError("live download"))
        self.download = download.start()
        self.addCleanup(download.stop)

        self.now = [1000.0]
        self.stores = Stores(self.root / "state")
        self.transport = FakeTransport()
        self.addCleanup(lambda: self.assertEqual(self.transport.violations, []))
        self.rest = DiscordRest(self.transport)
        self.provider = Scripted()
        holder = {}
        self.approvals = PendingApprovals(
            timeout_s=5.0,
            on_request=lambda request: holder["daemon"]._approval_requested(request),
            on_resolve=lambda request, decision, resolution:
                holder["daemon"]._approval_resolved(request, decision, resolution))
        self.daemon = Daemon(self.stores, {ProviderName.FAST: self.provider},
                             lambda thread_id, brief: (lambda *_a: Decision.ALLOW),
                             0, self.approvals)
        holder["daemon"] = self.daemon
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        self.router = Router(self.stores)
        self.spoken = []
        self.boot()

        self.school = self.stores.projects.create("School", str(self.root),
                                                  discord_channel_id=SCHOOL_CHANNEL)
        self.test = self.stores.projects.create("test", str(self.root),
                                                discord_channel_id=TEST_CHANNEL)
        inbox = self.stores.projects.inbox()
        inbox.discord_channel_id = UNGROUPED
        self.stores.projects.save(inbox)
        self.inbox = inbox

    def boot(self):
        """A mirror and a gateway over the daemon, as `DiscordSurface` wires them."""
        self.mirror = ChatMirror(self.daemon, self.rest, dm_channel=lambda: DM_CHANNEL,
                                 speak=self._speak, router=self.router,
                                 clock=lambda: self.now[0], rate_posts=self.rate_posts)
        self.gateway = DiscordRouter(
            self.daemon, self.stores, self.router, self.approvals, None, self.rest,
            FakeListener, announce=lambda text: None, dm_channel=lambda: DM_CHANNEL,
            mirror=self.mirror)
        self.gateway.guild_id = GUILD
        self.mirror.start()
        self.gateway.start()
        self.addCleanup(self.shutdown)
        self.listener = self.gateway.listener

    def shutdown(self):
        self.gateway.stop()
        self.mirror.close()

    def _speak(self, text):
        self.spoken.append(text)
        return ("jarvis-reply.wav", b"RIFF-fake-audio")

    # -- helpers -----------------------------------------------------------

    def request(self, method, path, body=None, status=200):
        conn = http.client.HTTPConnection("127.0.0.1", self.daemon.port, timeout=5)
        try:
            conn.request(method, path, json.dumps(body).encode() if body is not None else None,
                         {"Content-Type": "application/json"} if body is not None else {})
            response = conn.getresponse()
            content = response.read()
            self.assertEqual(response.status, status, content[:300])
            return json.loads(content) if content else None
        finally:
            conn.close()

    def open_chat(self, project=None):
        project = project or self.school
        return self.daemon.open_thread(project.id, Role.CHAT, ProviderName.FAST, {})

    def hud_send(self, chat, text, **body):
        turn = self.request("POST", f"/threads/{chat.id}/send", {"text": text, **body}, 202)
        return turn["turn_id"]

    def idle(self, chat_id):
        def done():
            with self.daemon._lock:
                session = self.daemon._sessions.get(chat_id)
                return session is None or session.worker is None
        wait_for(done, what="the turn to finish")

    def place(self, chat_id):
        def found():
            thread = self.stores.threads.get(chat_id)
            return thread.surface if thread and thread.surface else None
        surface = wait_for(found, what="the chat's surface")
        return surface.split(":", 1)[1] if surface.startswith("discord:") else surface

    def settled(self, chat_id):
        """Until the mirror has posted everything this chat's log holds."""
        def done():
            side = M.read_sidecar(self.stores, chat_id)
            log = self.stores.threads.read_log(chat_id)
            return int(side.get("mirrored_through") or 0) >= len(log) \
                and self.mirror._pending_count() == 0
        wait_for(done, what="the mirror to catch up")

    def chats(self, project_id=None):
        return [t for t in self.stores.threads.list()
                if t.role == Role.CHAT and (project_id is None or t.project_id == project_id)]

    def all_methods(self):
        with self.transport.lock:
            return {c["method"] for c in self.transport.calls}


class OutboundChecks(Harness):
    def test_no_thread_until_the_first_owner_message_then_exactly_one(self):
        chat = self.open_chat()
        time.sleep(0.2)
        self.assertEqual(self.transport.made(), [], "opening a chat made a thread")
        self.assertIsNone(self.stores.threads.get(chat.id).surface)
        self.hud_send(chat, "hello")
        place = self.place(chat.id)
        self.idle(chat.id)
        self.hud_send(chat, "and again")
        self.idle(chat.id)
        self.settled(chat.id)
        made = self.transport.made()
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0]["path"], f"/channels/{SCHOOL_CHANNEL}/threads")
        self.assertEqual(self.transport.texts(place),
                         ["You (HUD): hello", "the answer", "You (HUD): and again",
                          "the answer"])
        # The owner is added, so the thread shows in their list.
        self.assertTrue(any(c["method"] == "PUT" and c["path"].endswith(f"/thread-members/{OWNER}")
                            for c in self.transport.calls))

    def test_name_falls_back_to_id_and_excerpt_then_follows_hud_renames(self):
        chat = self.open_chat()
        long = "please summarise chapter four of the biology reading for tomorrow morning"
        self.hud_send(chat, long)
        place = self.place(chat.id)
        name = self.transport.made()[0]["json"]["name"]
        self.assertEqual(name, f"{chat.id} · {long[:50].rstrip()}")
        self.assertLessEqual(len(name), 100)
        self.idle(chat.id)
        P.rename_thread(self.daemon, chat.id, "Biology " + "x" * 200)
        wait_for(lambda: self.transport.patches(place), what="the rename")
        renamed = self.transport.patches(place)[-1]
        self.assertEqual(set(renamed), {"name"})
        self.assertTrue(renamed["name"].startswith("Biology x"))
        self.assertLessEqual(len(renamed["name"]), 100)
        # A titled chat's new thread is named by its title.
        titled = self.open_chat()
        P.rename_thread(self.daemon, titled.id, "Essay plan")
        self.hud_send(titled, "start")
        self.place(titled.id)
        self.assertEqual(self.transport.made()[-1]["json"]["name"], "Essay plan")

    def test_the_hud_line_is_the_typed_words_never_a_file_and_scrubbed(self):
        chat = self.open_chat()
        secret_file = base64.b64encode(f"notes\nKEY={SECRET}\n".encode()).decode()
        image = base64.b64encode(b"\x89PNG fake").decode()
        self.hud_send(chat, f"read this, my key is {SECRET}",
                      attachments=[{"name": "notes.md", "mime": "text/markdown",
                                    "data_b64": secret_file}],
                      images=[{"b64": image, "mime": "image/png"},
                              {"b64": image, "mime": "image/png"}])
        place = self.place(chat.id)
        self.idle(chat.id)
        self.settled(chat.id)
        line = self.transport.texts(place)[0]
        self.assertTrue(line.startswith("You (HUD): read this, my key is"), line)
        self.assertIn("[attached: notes.md]", line)
        self.assertIn("[2 images, in the HUD]", line)
        self.assertNotIn("notes\n", line, "a file's contents reached Discord")
        self.assertNotIn("attached file", line)
        everything = json.dumps(self.transport.calls)
        self.assertNotIn(SECRET, everything, "a credential value reached Discord")
        # The provider still got the whole assembled message.
        self.assertIn("[attached file: notes.md]", self.provider.messages[-1].text)
        # Dictation is labelled; the record says so too.
        self.hud_send(chat, "this was spoken", spoken=True)
        self.idle(chat.id)
        self.settled(chat.id)
        self.assertIn("You (HUD, voice): this was spoken", self.transport.texts(place))

    def test_replies_split_at_2000_one_footer_never_deltas_or_thinking(self):
        long = "\n".join(f"line {i} " + "y" * 90 for i in range(60))
        self.provider.plan["work"] = [("thinking", "private musing"), ("delta", "half a wor"),
                                      ("tool", "read_file"), ("tool", "grep_files"),
                                      ("tool", "read_file"), ("tool", "edit_file"),
                                      ("tool", "run_tests"), ("text", long)]
        chat = self.open_chat()
        self.hud_send(chat, "work")
        place = self.place(chat.id)
        self.idle(chat.id)
        self.settled(chat.id)
        texts = self.transport.texts(place)[1:]
        self.assertGreater(len(texts), 2)
        self.assertTrue(all(len(t) <= 2000 for t in texts))
        footer = "· 5 tools: read_file, grep_files +2"
        self.assertEqual(sum(footer in t for t in texts), 1)
        self.assertTrue(texts[-1].endswith(footer), texts[-1][-80:])
        joined = " ".join(texts).replace(footer, "")
        self.assertEqual(joined.split(), long.split())
        self.assertNotIn("private musing", json.dumps(self.transport.calls))
        self.assertNotIn("half a wor", json.dumps(self.transport.calls))
        # Every mirrored post is silent; nothing here pinged.
        self.assertTrue(all(p.get("flags") == 4096 for p in self.transport.posts(place)))
        self.assertFalse(any("<@" in t for t in self.transport.texts(place)))

    def test_a_failed_turn_and_an_interrupted_one_say_so_by_class_only(self):
        self.provider.plan["boom"] = [("text", "partial"),
                                      ("error", f"RuntimeError: leaked {SECRET} in a path")]
        chat = self.open_chat()
        self.hud_send(chat, "boom")
        place = self.place(chat.id)
        self.idle(chat.id)
        self.settled(chat.id)
        self.assertEqual(self.transport.texts(place)[-1], "partial\n· turn failed (RuntimeError)")
        self.assertNotIn("leaked", json.dumps(self.transport.calls))
        self.provider.plan["slow"] = [("wait",), ("text", "late")]
        self.provider.release.clear()
        self.hud_send(chat, "slow")
        wait_for(lambda: any("slow" in t for t in self.transport.texts(place)))
        self.daemon.interrupt(chat.id)
        self.idle(chat.id)
        self.settled(chat.id)
        self.assertTrue(self.transport.texts(place)[-1].endswith("· interrupted"))

    def test_four_posts_per_five_seconds_per_thread(self):
        self.mirror.rate_posts = M.RATE_POSTS
        self.provider.default = [("text", "\n".join("z" * 1500 for _ in range(6)))]
        chat = self.open_chat()
        self.hud_send(chat, "a lot")
        place = self.place(chat.id)
        self.idle(chat.id)
        wait_for(lambda: len(self.transport.posts(place)) == 4, what="four posts")
        time.sleep(0.6)
        self.assertEqual(len(self.transport.posts(place)), 4, "the window was not held")
        self.now[0] += 5.1
        self.settled(chat.id)
        self.assertEqual(len(self.transport.posts(place)), 7)

    def test_above_twelve_pending_the_rest_collapse_into_one_line(self):
        self.mirror.rate_posts = M.RATE_POSTS
        words = [f"para{i} " + "q" * 1900 for i in range(20)]
        self.provider.default = [("text", "\n".join(words))]
        chat = self.open_chat()
        self.hud_send(chat, "flood")
        place = self.place(chat.id)
        self.idle(chat.id)
        for _ in range(6):
            self.now[0] += 5.1
            time.sleep(0.6)
        self.settled(chat.id)
        posts = self.transport.posts(place)
        # The user line, then 12 for the reply: 11 chunks and one collapsed.
        self.assertEqual(len(posts), 13, [p.get("content", "")[:30] for p in posts])
        last = posts[-1]
        self.assertEqual(last["content"], "(9 more messages, open the HUD)")
        name, body = last["_files"][0]
        self.assertEqual(name, "reply.txt")
        self.assertIn(b"para19", body)
        self.assertNotIn(b"para10 ", body)
        shown = " ".join(p.get("content", "") for p in posts[1:12])
        self.assertIn("para10", shown)


class ApprovalAndQuestionChecks(Harness):
    def ask(self, chat_id, command="pnpm run deploy --prod"):
        result = {}
        request = ApprovalRequest(tool="Bash", args={"command": command}, command=command,
                                  thread_id=chat_id, origin="chat")
        worker = threading.Thread(target=lambda: result.update(
            decision=self.approvals.ask(request)), daemon=True)
        worker.start()
        wait_for(lambda: request.req_id in self.gateway._approval_messages,
                 what="the approval post")
        return request, result, worker

    def test_a_chat_approval_lands_in_its_thread_with_buttons_and_a_ping(self):
        chat = self.open_chat()
        self.hud_send(chat, "deploy it")
        place = self.place(chat.id)
        self.idle(chat.id)
        request, result, worker = self.ask(chat.id)
        posts = self.transport.posts(place)
        asked = next(p for p in posts if request.code in (p.get("content") or ""))
        self.assertEqual(asked.get("flags"), 4096)
        labels = [b["label"] for row in asked["components"] for b in row["components"]]
        self.assertEqual(labels, ["Approve", "Deny"])
        wait_for(lambda: f"<@{OWNER}>" in self.transport.texts(place), what="the ping line")
        self.assertEqual(self.transport.texts(DM_CHANNEL), [], "a chat approval went to the DM")
        # Answered where it was asked, typed.
        self.listener.feed(guild_message(f"yes {request.code}", place))
        worker.join(3)
        self.assertEqual(result["decision"], Decision.ALLOW)

    def test_an_approval_at_the_first_message_waits_for_the_thread(self):
        self.provider.plan["first"] = [("wait",), ("text", "done")]
        self.provider.release.clear()
        chat = self.open_chat()
        self.hud_send(chat, "first")
        request, result, worker = self.ask(chat.id, "make build")
        place = self.place(chat.id)
        self.assertTrue(any(request.code in t for t in self.transport.texts(place)))
        self.approvals.resolve(request.req_id, Decision.DENY)
        self.provider.release.set()
        worker.join(3)
        self.idle(chat.id)

    def test_a_dm_chat_approval_is_asked_in_the_dm_without_a_ping(self):
        self.listener.feed(dm("hi"))
        chat = wait_for(lambda: [t for t in self.chats() if t.surface == "dm"])[0]
        self.idle(chat.id)
        request, result, worker = self.ask(chat.id, "rm -rf build")
        self.assertTrue(any(request.code in t for t in self.transport.texts(DM_CHANNEL)))
        self.assertNotIn(f"<@{OWNER}>", self.transport.texts(DM_CHANNEL))
        self.listener.feed(dm(f"no {request.code}"))
        worker.join(3)
        self.assertEqual(result["decision"], Decision.DENY)

    def test_a_codex_question_pings_and_the_next_typed_message_answers_it(self):
        self.provider.plan["set it up"] = [("question", "Which package manager?")]
        chat = self.open_chat()
        self.hud_send(chat, "set it up")
        place = self.place(chat.id)
        wait_for(lambda: any("Which package manager?" in t for t in self.transport.texts(place)))
        question = next(t for t in self.transport.texts(place) if "Which package" in t)
        self.assertIn("1. npm", question)
        wait_for(lambda: f"<@{OWNER}>" in self.transport.texts(place), what="the ping")
        # A voice note never answers.
        with patch.object(self.gateway, "_hear", return_value="pnpm"):
            self.listener.feed(guild_message("", place, attachments=[{
                "url": "https://cdn.example/v.ogg", "content_type": "audio/ogg", "size": 10,
                "waveform": "AA=="}], flags=1 << 13))
        self.assertEqual(self.provider.answers, [])
        self.assertTrue(any("can't take a voice note" in t for t in self.transport.texts(place)))
        self.listener.feed(guild_message("pnpm", place))
        wait_for(lambda: self.provider.answers, what="the answer")
        self.assertEqual(self.provider.answers, [("q-1", "pnpm")])
        self.idle(chat.id)
        self.settled(chat.id)
        self.assertIn("using pnpm", self.transport.texts(place)[-1])
        # It was an answer, not a second turn.
        self.assertEqual(len(self.provider.messages), 1)


class InboundChecks(Harness):
    def started(self, chat):
        self.hud_send(chat, "hello")
        place = self.place(chat.id)
        self.idle(chat.id)
        self.settled(chat.id)
        return place

    def test_typed_in_discord_runs_the_same_chat_and_is_never_echoed(self):
        chat = self.open_chat()
        place = self.started(chat)
        before = len(self.transport.posts(place))
        self.listener.feed(guild_message("what about chapter five?", place))
        wait_for(lambda: len(self.provider.messages) == 2)
        message = self.provider.messages[-1]
        self.assertEqual((message.via, message.typed), ("discord", "what about chapter five?"))
        self.idle(chat.id)
        self.settled(chat.id)
        new = self.transport.texts(place)[before:]
        self.assertEqual(new, ["the answer"], "the owner's own message was echoed")
        self.assertEqual(len(self.chats()), 1)
        transcript = self.request("GET", f"/threads/{chat.id}/transcript")["messages"]
        mine = [m for m in transcript if m["role"] == "user"]
        self.assertEqual(mine[-1], {**mine[-1], "text": "what about chapter five?",
                                    "via": "discord"})
        self.assertNotIn("via", mine[0])

    def test_a_duplicate_message_id_runs_once_and_bots_are_never_heard(self):
        chat = self.open_chat()
        place = self.started(chat)
        message = guild_message("once", place)
        self.listener.feed(message)
        self.listener.feed(dict(message))
        self.idle(chat.id)
        self.listener.feed(guild_message("from a bot", place, author={"id": "5", "bot": True}))
        self.listener.feed(guild_message("from me as a bot", place,
                                         author={"id": OWNER, "bot": True}))
        self.listener.feed(guild_message("a stranger", place, author={"id": "6"}))
        time.sleep(0.3)
        self.assertEqual([m.typed for m in self.provider.messages], ["hello", "once"])

    def test_a_message_during_a_running_turn_is_queued_up_to_three(self):
        self.provider.plan["long"] = [("wait",), ("text", "finally")]
        self.provider.release.clear()
        chat = self.open_chat()
        self.hud_send(chat, "long")
        place = self.place(chat.id)
        for word in ("one", "two", "three", "four"):
            self.listener.feed(guild_message(word, place))
        texts = self.transport.texts(place)
        wait_for(lambda: self.transport.texts(place).count(M.QUEUED_TEXT) == 3)
        self.assertEqual(self.transport.texts(place).count(M.FULL_TEXT), 1)
        self.assertTrue(all(p.get("flags") == 4096 for p in self.transport.posts(place)))
        del texts
        self.provider.release.set()
        wait_for(lambda: [m.typed for m in self.provider.messages] ==
                 ["long", "one", "two", "three"], timeout=10, what="the queue to drain")
        self.idle(chat.id)

    def test_a_voice_note_runs_a_turn_speaks_back_and_approves_nothing(self):
        chat = self.open_chat()
        place = self.started(chat)
        request = ApprovalRequest(tool="Bash", args={"command": "make deploy"},
                                  command="make deploy", thread_id=chat.id, origin="chat")
        result = {}
        worker = threading.Thread(target=lambda: result.update(
            decision=self.approvals.ask(request)), daemon=True)
        worker.start()
        wait_for(lambda: request.req_id in self.gateway._approval_messages)
        with patch.object(self.gateway, "_hear", return_value="yes"):
            self.listener.feed(guild_message("", place, attachments=[{
                "url": "https://cdn.example/v.ogg", "content_type": "audio/ogg", "size": 10,
                "waveform": "AA=="}], flags=1 << 13))
        wait_for(lambda: len(self.provider.messages) == 2)
        self.assertTrue(self.provider.messages[-1].spoken)
        self.assertTrue(self.approvals.pending(), "a voice note approved something")
        self.idle(chat.id)
        self.settled(chat.id)
        audio = [p for p in self.transport.posts(place) if p.get("_files")]
        self.assertEqual(audio[-1]["_files"][0][0], "jarvis-reply.wav")
        self.assertEqual(self.spoken, ["the answer"])
        self.approvals.resolve(request.req_id, Decision.DENY)
        worker.join(3)

    def test_discord_files_follow_the_hud_rules(self):
        chat = self.open_chat()
        place = self.started(chat)
        files = {"https://cdn.example/a.txt": b"plain notes",
                 "https://cdn.example/p.png": b"\x89PNG"}
        self.download.side_effect = lambda url: files[url]
        self.listener.feed(guild_message("look", place, attachments=[
            {"url": "https://cdn.example/a.txt", "filename": "a.txt",
             "content_type": "text/plain", "size": 11},
            {"url": "https://cdn.example/p.png", "filename": "p.png",
             "content_type": "image/png", "size": 4},
            {"url": "https://cdn.example/.env", "filename": ".env",
             "content_type": "text/plain", "size": 4},
            {"url": "https://cdn.example/x.pdf", "filename": "x.pdf",
             "content_type": "application/pdf", "size": 4},
            {"url": "https://cdn.example/big.txt", "filename": "big.txt",
             "content_type": "text/plain", "size": 5 * 1024 * 1024}]))
        wait_for(lambda: len(self.provider.messages) == 2)
        message = self.provider.messages[-1]
        self.assertIn("[attached file: a.txt]", message.text)
        self.assertIn("plain notes", message.text)
        self.assertEqual(len(message.images), 1)
        self.assertIn(".env holds live credentials", message.text)
        self.assertIn("x.pdf: only images and text files", message.text)
        self.assertIn("big.txt is over 4MB", message.text)
        fetched = [c.args[0] for c in self.download.call_args_list]
        self.assertEqual(sorted(fetched), ["https://cdn.example/a.txt",
                                           "https://cdn.example/p.png"],
                         "a refused attachment was downloaded")
        self.assertEqual(message.attachments, ["a.txt", "p.png"])
        self.idle(chat.id)

    def test_a_top_level_message_starts_a_new_chat_threaded_under_it(self):
        message = guild_message("plan my week", SCHOOL_CHANNEL)
        self.listener.feed(message)
        chat = wait_for(lambda: self.chats(self.school.id))[0]
        made = self.transport.made()[0]
        self.assertEqual(made["path"],
                         f"/channels/{SCHOOL_CHANNEL}/messages/{message['id']}/threads")
        self.assertEqual(made["json"]["name"], f"{chat.id} · plan my week")
        place = self.place(chat.id)
        self.idle(chat.id)
        self.settled(chat.id)
        self.assertEqual(self.transport.texts(place), ["the answer"])
        self.assertEqual(self.transport.texts(SCHOOL_CHANNEL), [])
        self.listener.feed(guild_message("anything in the inbox?", UNGROUPED))
        inbox_chat = wait_for(lambda: self.chats(self.inbox.id))[0]
        self.assertTrue(self.place(inbox_chat.id))
        self.assertTrue(self.transport.made()[-1]["path"].startswith(f"/channels/{UNGROUPED}/"))
        self.idle(inbox_chat.id)

    def test_the_dm_conversation_persists_across_a_restart(self):
        self.listener.feed(dm("remember the ledger"))
        chat = wait_for(lambda: [t for t in self.chats() if t.surface == "dm"])[0]
        self.assertEqual(chat.project_id, self.inbox.id)
        self.idle(chat.id)
        self.settled(chat.id)
        # A DM reply notifies; nothing lands in #ungrouped.
        self.assertEqual(self.transport.posts(DM_CHANNEL)[-1].get("flags"), None)
        self.assertEqual(self.transport.texts(UNGROUPED), [])
        # A HUD turn in the DM chat is posted to the DM, silently.
        self.hud_send(chat, "from the desk")
        self.idle(chat.id)
        self.settled(chat.id)
        quiet = self.transport.posts(DM_CHANNEL)[-2:]
        self.assertEqual([p["content"] for p in quiet], ["You (HUD): from the desk", "the answer"])
        self.assertTrue(all(p.get("flags") == 4096 for p in quiet))
        # Restart the Discord side: the same chat is found by its surface.
        self.shutdown()
        self.boot()
        self.listener.feed(dm("still there?"))
        wait_for(lambda: len(self.provider.messages) == 3)
        self.assertEqual([t.id for t in self.chats() if t.surface == "dm"], [chat.id])
        self.assertEqual(len(self.chats()), 1)
        self.idle(chat.id)


class LifecycleChecks(Harness):
    def started(self, chat, text="hello"):
        self.hud_send(chat, text)
        place = self.place(chat.id)
        self.idle(chat.id)
        self.settled(chat.id)
        return place

    def test_a_move_opens_a_new_thread_links_both_and_renames_the_old(self):
        chat = self.open_chat()
        old = self.started(chat, "essay outline")
        self.request("PATCH", f"/threads/{chat.id}", {"project_id": self.test.id})
        new = wait_for(lambda: (lambda s: s and s.split(":", 1)[1] != old and s.split(":", 1)[1])(
            self.stores.threads.get(chat.id).surface), what="the new surface")
        self.assertTrue(self.transport.made()[-1]["path"].startswith(f"/channels/{TEST_CHANNEL}/"))
        wait_for(lambda: self.transport.texts(new), what="the continued line")
        self.assertEqual(self.transport.texts(new)[0], f"Continued from <#{old}>")
        self.assertIn(f"Moved to test → <#{new}>", self.transport.texts(old))
        name = M.read_sidecar(self.stores, chat.id)["name"]
        wait_for(lambda: self.transport.patches(old), what="the old thread's rename")
        self.assertEqual(self.transport.patches(old)[-1],
                         {"name": f"↪ moved to test · {name}"})
        side = M.read_sidecar(self.stores, chat.id)
        self.assertEqual((side["surface"], side["retired"]), (f"discord:{new}", [old]))
        # The old thread still runs the chat; the reply goes there with a pointer.
        self.listener.feed(guild_message("still here", old))
        wait_for(lambda: len(self.provider.messages) == 2)
        self.idle(chat.id)
        self.settled(chat.id)
        self.assertEqual(self.transport.texts(old)[-1],
                         f"the answer\n↪ this chat continues in <#{new}>")
        # A later HUD rename renames both, the old one keeping its header.
        P.rename_thread(self.daemon, chat.id, "Essay")
        wait_for(lambda: {"name": "↪ moved to test · Essay"} in self.transport.patches(old))
        wait_for(lambda: {"name": "Essay"} in self.transport.patches(new))

    def test_archive_restore_and_delete_post_notes_and_delete_nothing(self):
        chat = self.open_chat()
        place = self.started(chat)
        P.archive_thread(self.daemon, chat.id)
        wait_for(lambda: {"archived": True} in self.transport.patches(place), what="archive")
        self.assertIn("Archived in the HUD. Restore it there to carry on here.",
                      self.transport.texts(place))
        # Typed there now: the fixed reply, and no turn.
        self.listener.feed(guild_message("are you there", place))
        self.assertIn(DiscordRouter.CHAT_ARCHIVED_TEXT, self.transport.texts(place))
        self.assertEqual(len(self.provider.messages), 1)
        P.restore_thread(self.daemon, chat.id)
        wait_for(lambda: "Restored." in self.transport.texts(place))
        P.archive_thread(self.daemon, chat.id)
        P.delete_thread(self.daemon, chat.id)
        wait_for(lambda: any("Deleted in the HUD" in t for t in self.transport.texts(place)))
        self.assertIsNone(self.mirror.locate(place))
        self.assertNotIn("DELETE", self.all_methods(), "Jarvis deleted something on Discord")
        self.assertFalse(M.sidecar_path(self.stores, chat.id).parent.exists(),
                         "the mirror recreated a deleted chat's folder")

    def test_an_archive_the_bot_may_not_do_is_reported(self):
        chat = self.open_chat()
        place = self.started(chat)
        self.transport.fail.append(("PATCH", lambda path: path == f"/channels/{place}", 403,
                                    {"code": 50013, "message": "Missing Permissions"}, None))
        P.archive_thread(self.daemon, chat.id)
        wait_for(lambda: self.mirror.status()["state"] == "degraded", what="the light")
        status = self.mirror.status()
        self.assertIn("Manage Threads", status["reason"])
        self.assertEqual(status["last_error"]["op"], "archive_thread")
        self.assertEqual(status["last_error"]["status"], 403)
        # GET /discord carries it (field by field).
        self.daemon.discord = self.gateway
        body = self.request("GET", "/discord")
        self.assertEqual(body["mirror"]["state"], "degraded")
        self.assertNotIn(TOKEN, json.dumps(body))

    def test_a_thread_deleted_on_discord_unlinks_and_the_next_message_makes_one(self):
        chat = self.open_chat()
        place = self.started(chat)
        self.transport.fail.append(("POST", lambda path: path == f"/channels/{place}/messages",
                                    404, {"code": 10003, "message": "Unknown Channel"}, None))
        self.hud_send(chat, "are you there")
        wait_for(lambda: self.stores.threads.get(chat.id).surface is None, what="unlinking")
        self.idle(chat.id)
        self.hud_send(chat, "again")
        wait_for(lambda: (self.stores.threads.get(chat.id).surface or "").split(":")[-1]
                 not in ("", place), what="a fresh thread")
        self.idle(chat.id)


class RestartChecks(Harness):
    def test_restart_posts_up_to_six_missing_messages(self):
        chat = self.open_chat()
        self.hud_send(chat, "first")
        place = self.place(chat.id)
        self.idle(chat.id)
        self.settled(chat.id)
        self.shutdown()
        for word in ("two", "three", "four"):           # 3 turns = 6 messages
            self.hud_send(chat, word)
            self.idle(chat.id)
        before = len(self.transport.posts(place))
        self.boot()
        self.settled(chat.id)
        self.assertEqual(self.transport.texts(place)[before:],
                         ["You (HUD): two", "the answer", "You (HUD): three", "the answer",
                          "You (HUD): four", "the answer"])

    def test_restart_with_more_than_six_missing_posts_one_catch_up_line(self):
        chat = self.open_chat()
        self.hud_send(chat, "first")
        place = self.place(chat.id)
        self.idle(chat.id)
        self.settled(chat.id)
        self.shutdown()
        for word in ("two", "three", "four", "five"):   # 8 messages
            self.hud_send(chat, word)
            self.idle(chat.id)
        before = len(self.transport.posts(place))
        self.boot()
        self.settled(chat.id)
        self.assertEqual(self.transport.texts(place)[before:],
                         ["(8 messages while Discord was unavailable, see the HUD)"])
        # And it carries on normally afterwards.
        self.hud_send(chat, "six")
        self.idle(chat.id)
        self.settled(chat.id)
        self.assertEqual(self.transport.texts(place)[-2:], ["You (HUD): six", "the answer"])

    def test_chats_from_before_the_mirror_are_not_backfilled(self):
        self.shutdown()
        chat = self.open_chat()
        for word in ("old one", "old two"):
            self.hud_send(chat, word)
            self.idle(chat.id)
        with patch.object(M, "utcnow", return_value="2999-01-01T00:00:00+00:00"):
            self.boot()
        time.sleep(0.3)
        self.assertEqual(self.transport.made(), [])
        self.hud_send(chat, "new message")
        place = self.place(chat.id)
        self.idle(chat.id)
        self.settled(chat.id)
        self.assertEqual(self.transport.texts(place), ["You (HUD): new message", "the answer"])

    def test_progress_is_the_log_not_the_bus(self):
        chat = self.open_chat()
        self.hud_send(chat, "first")
        place = self.place(chat.id)
        self.idle(chat.id)
        self.settled(chat.id)
        # Drop the mirror's subscription on the floor for one turn.
        self.daemon.bus.unsubscribe(self.mirror.subscription)
        self.hud_send(chat, "unheard")
        self.idle(chat.id)
        self.daemon.bus._subscribers[self.mirror.subscription] = (
            lambda r: r.get("kind") in M.KINDS)
        self.hud_send(chat, "heard")
        self.idle(chat.id)
        self.settled(chat.id)
        self.assertEqual(self.transport.texts(place)[2:],
                         ["You (HUD): unheard", "the answer", "You (HUD): heard", "the answer"])


class ReviewFixChecks(Harness):
    """The PR #10 review's findings, each written to fail against the code
    the review read."""

    def started(self, chat, text="hello"):
        self.hud_send(chat, text)
        place = self.place(chat.id)
        self.idle(chat.id)
        self.settled(chat.id)
        return place

    def test_a_secret_in_a_reply_reaches_neither_speech_nor_any_post(self):
        self.provider.default = [("text", f"the key is {SECRET} ok")]
        chat = self.open_chat()
        place = self.started(chat)
        self.assertTrue(any("the key is" in t for t in self.transport.texts(place)))
        with patch.object(self.gateway, "_hear", return_value="say the key"):
            self.listener.feed(guild_message("", place, attachments=[{
                "url": "https://cdn.example/v.ogg", "content_type": "audio/ogg", "size": 10,
                "waveform": "AA=="}], flags=1 << 13))
        wait_for(lambda: self.spoken, what="speech")
        self.idle(chat.id)
        self.settled(chat.id)
        self.assertTrue(self.spoken and all(SECRET not in text for text in self.spoken),
                        "an unscrubbed reply was spoken")
        self.assertNotIn(SECRET, repr(self.transport.calls))

    def test_escape_hatch_output_is_never_mirrored(self):
        from jarvis.v2.provider import UserMessage
        chat = self.open_chat()
        place = self.started(chat)
        self.daemon.send(chat.id, UserMessage(
            text="[owner ran: cat diary.txt]\nDear diary, private words", origin="owner-ran"))
        self.idle(chat.id)
        self.settled(chat.id)
        self.assertIn("· system: [owner ran: cat diary.txt] (output in the HUD)",
                      self.transport.texts(place))
        self.assertNotIn("Dear diary", json.dumps(self.transport.calls))

    def test_a_thread_closed_to_the_bot_falls_back_to_the_dm(self):
        chat = self.open_chat()
        place = self.started(chat)
        self.transport.fail.append(("POST", lambda path: path == f"/channels/{place}/messages",
                                    403, {"code": 50013, "message": "Missing Permissions"}, None))
        self.listener.feed(guild_message("are you there?", place))
        wait_for(lambda: self.transport.texts(DM_CHANNEL), what="the DM safety net")
        self.idle(chat.id)
        self.settled(chat.id)
        dm = self.transport.posts(DM_CHANNEL)[-1]
        self.assertEqual(dm["content"], f"[School · chat {chat.id}] the answer")
        self.assertIsNone(dm.get("flags"), "the safety net must notify")

    def test_thread_creation_backs_off_and_honours_retry_after(self):
        creates = lambda: [c for c in self.transport.made()
                           if c["path"] == f"/channels/{SCHOOL_CHANNEL}/threads"]
        unavailable = ("POST", lambda path: path == f"/channels/{SCHOOL_CHANNEL}/threads",
                       503, {"message": "unavailable"}, None)
        self.transport.fail.append(unavailable)
        chat = self.open_chat()
        self.hud_send(chat, "hello")
        wait_for(lambda: creates(), what="a first create")
        time.sleep(1.0)
        self.assertEqual(len(creates()), 1, "creates retried with no back-off")
        self.now[0] += 5.1
        wait_for(lambda: len(creates()) == 2, what="the retry after 5 s")
        self.transport.fail.remove(unavailable)
        limited = ("POST", lambda path: path == f"/channels/{SCHOOL_CHANNEL}/threads",
                   429, {"message": "slow down", "retry_after": 30}, None)
        self.transport.fail.append(limited)
        self.now[0] += 10.1
        wait_for(lambda: len(creates()) == 3, what="the retry after 10 s")
        # The back-off alone would be 20 s now; Discord asked for 30.
        self.now[0] += 21
        time.sleep(0.8)
        self.assertEqual(len(creates()), 3, "Discord's retry_after was not honoured")
        self.transport.fail.remove(limited)
        self.now[0] += 10
        self.place(chat.id)
        self.idle(chat.id)

    def test_an_at_path_typed_in_discord_is_never_read_from_disk(self):
        (self.root / "notes.md").write_text("PRIVATE-NOTES-FROM-DISK")
        chat = self.open_chat()
        place = self.started(chat)
        self.download.side_effect = lambda url: b"attached words"
        self.listener.feed(guild_message("see @notes.md please", place, attachments=[
            {"url": "https://cdn.example/a.txt", "filename": "a.txt",
             "content_type": "text/plain", "size": 14}]))
        wait_for(lambda: len(self.provider.messages) == 2)
        message = self.provider.messages[-1]
        self.assertIn("attached words", message.text)
        self.assertNotIn("PRIVATE-NOTES-FROM-DISK", message.text)
        self.assertEqual(message.attachments, ["a.txt"])
        self.idle(chat.id)

    def test_a_question_answered_in_the_hud_frees_the_next_message(self):
        self.provider.plan["set it up"] = [("question", "Which package manager?"),
                                           ("wait",), ("text", "set up")]
        self.provider.release.clear()
        chat = self.open_chat()
        self.hud_send(chat, "set it up")
        place = self.place(chat.id)
        wait_for(lambda: self.mirror.pending_question(chat.id), what="the question")
        self.request("POST", f"/threads/{chat.id}/answer", {"req_id": "q-1", "text": "npm"})
        wait_for(lambda: self.mirror.pending_question(chat.id) is None,
                 what="the question cleared while the turn still runs")
        self.listener.feed(guild_message("and the tests?", place))
        wait_for(lambda: M.QUEUED_TEXT in self.transport.texts(place))
        self.assertEqual(self.provider.answers, [("q-1", "npm")])
        self.provider.release.set()
        wait_for(lambda: [m.typed for m in self.provider.messages] == ["set it up",
                                                                      "and the tests?"],
                 timeout=10)
        self.idle(chat.id)

    def test_an_answer_that_fails_runs_as_an_ordinary_turn(self):
        chat = self.open_chat()
        place = self.started(chat)
        with self.mirror._lock:
            self.mirror._questions[chat.id] = "q-stale"
        self.listener.feed(guild_message("next thing", place))
        wait_for(lambda: len(self.provider.messages) == 2, what="the turn")
        self.assertEqual(self.provider.messages[-1].typed, "next thing")
        self.idle(chat.id)

    def test_collapse_keeps_a_question_and_its_ping(self):
        quiet = ChatMirror(self.daemon, self.rest, dm_channel=lambda: DM_CHANNEL)
        chat = "abcd1234"
        posts = [M._Post(chat, "900", content=f"line {i}") for i in range(14)]
        posts.append(M._Post(chat, "900", content="Question: which one?", keep=True))
        posts.append(M._Post(chat, "900", action="ping"))
        posts += [M._Post(chat, "900", content=f"after {i}", through=40 + i) for i in range(3)]
        quiet._enqueue(posts)
        box = list(quiet._outbox["900"])
        self.assertIn("Question: which one?", [p.content for p in box])
        self.assertIn("ping", [p.action for p in box])
        collapsed = [p for p in box if (p.content or "").endswith("open the HUD)")]
        self.assertEqual(len(collapsed), 1)
        self.assertEqual(collapsed[0].through, 42)

    def test_the_forbidden_light_says_so_plainly_and_clears(self):
        chat = self.open_chat()
        place = self.started(chat)
        refuse = ("PATCH", lambda path: path == f"/channels/{place}", 403,
                  {"code": 50013, "message": "Missing Permissions"}, None)
        self.transport.fail.append(refuse)
        seen = self.daemon.bus.subscribe({"kind": "discord_status"})
        P.archive_thread(self.daemon, chat.id)
        wait_for(lambda: self.mirror.forbidden, what="the refusal")
        first = seen.get(timeout=2)["data"]["mirror"]
        self.assertIn("Manage Threads", first["reason"],
                      "the first status published must already say why")
        self.assertIn("cannot archive its own chat threads", first["reason"])
        self.transport.fail.remove(refuse)
        P.restore_thread(self.daemon, chat.id)
        P.rename_thread(self.daemon, chat.id, "Works now")
        wait_for(lambda: self.mirror.forbidden is None, what="the light to clear")
        self.assertNotIn("Manage Threads", self.mirror.status()["reason"])

    def test_a_proposal_reply_to_a_dm_message_notifies(self):
        self.listener.feed(dm("plan my week"))
        chat = wait_for(lambda: [t for t in self.chats() if t.surface == "dm"])[0]
        self.idle(chat.id)
        self.settled(chat.id)
        turn = [r for r in self.stores.threads.read_log(chat.id) if r.get("kind") == "user"][-1]
        self.daemon.bus.publish({"kind": "proposal_reply", "thread_id": chat.id,
                                 "project_id": chat.project_id,
                                 "data": {"turn_id": turn["turn_id"],
                                          "reply": "Starting a task for that."}})
        wait_for(lambda: "Starting a task for that." in self.transport.texts(DM_CHANNEL))
        post = next(p for p in self.transport.posts(DM_CHANNEL)
                    if p.get("content") == "Starting a task for that.")
        self.assertIsNone(post.get("flags"), "a reply to a DM message must notify")
        # A HUD turn's proposal in the DM chat stays silent.
        self.hud_send(chat, "from the desk")
        self.idle(chat.id)
        hud_turn = [r for r in self.stores.threads.read_log(chat.id)
                    if r.get("kind") == "user"][-1]
        self.daemon.bus.publish({"kind": "proposal_reply", "thread_id": chat.id,
                                 "project_id": chat.project_id,
                                 "data": {"turn_id": hud_turn["turn_id"],
                                          "reply": "Starting another."}})
        wait_for(lambda: "Starting another." in self.transport.texts(DM_CHANNEL))
        post = next(p for p in self.transport.posts(DM_CHANNEL)
                    if p.get("content") == "Starting another.")
        self.assertEqual(post.get("flags"), 4096)

    def test_a_chat_whose_thread_cannot_start_is_not_left_behind(self):
        message = guild_message("plan my week", SCHOOL_CHANNEL)
        self.transport.fail.append((
            "POST", lambda path: path.endswith(f"/messages/{message['id']}/threads"),
            403, {"code": 50013, "message": "Missing Permissions"}, None))
        self.listener.feed(message)
        wait_for(lambda: any("couldn't open a chat" in t
                             for t in self.transport.texts(SCHOOL_CHANNEL)))
        self.assertEqual(self.chats(self.school.id), [], "an empty chat was left in the HUD")
        self.assertEqual(self.provider.messages, [])

    def test_an_archived_dm_chat_is_retired_and_a_fresh_one_answers(self):
        self.listener.feed(dm("first"))
        old = wait_for(lambda: [t for t in self.chats() if t.surface == "dm"])[0]
        self.idle(old.id)
        self.settled(old.id)
        P.archive_thread(self.daemon, old.id)
        self.listener.feed(dm("second"))
        new = wait_for(lambda: [t for t in self.chats()
                                if t.surface == "dm" and t.id != old.id])[0]
        self.assertEqual(self.stores.threads.get(old.id).surface, "dm:retired")
        self.assertEqual(new.project_id, self.inbox.id)
        wait_for(lambda: len(self.provider.messages) == 2)
        self.assertEqual(self.provider.messages[-1].typed, "second")
        self.idle(new.id)


class BugbotQuestionChecks(Harness):
    """Cursor Bugbot findings on the question path (2026-10-08), each written
    to fail against the code it read."""

    def asking(self, chat, text="set it up"):
        self.provider.plan[text] = [("question", "Which package manager?")]
        self.hud_send(chat, text)
        wait_for(lambda: any(r.get("kind") == "question"
                             for r in self.stores.threads.read_log(chat.id)),
                 what="the question in the log")

    def test_files_with_no_text_never_answer_and_run_as_a_turn(self):
        """D7: only typed words answer. A files-only message used to answer
        the question with "" and lose its files."""
        chat = self.open_chat()
        self.asking(chat)
        place = self.place(chat.id)
        wait_for(lambda: self.mirror.pending_question(chat.id), what="the question")
        self.download.side_effect = lambda url: b"attached words"
        self.listener.feed(guild_message("", place, attachments=[
            {"url": "https://cdn.example/a.txt", "filename": "a.txt",
             "content_type": "text/plain", "size": 14}]))
        wait_for(lambda: M.QUEUED_TEXT in self.transport.texts(place), what="queued")
        self.assertEqual(self.provider.answers, [])
        self.assertEqual(self.mirror.pending_question(chat.id), "q-1")
        self.listener.feed(guild_message("pnpm", place))
        wait_for(lambda: self.provider.answers, what="the typed answer")
        self.assertEqual(self.provider.answers, [("q-1", "pnpm")])
        wait_for(lambda: len(self.provider.messages) == 2, what="the files as a turn")
        self.assertEqual(self.provider.messages[-1].attachments, ["a.txt"])
        self.assertIn("attached words", self.provider.messages[-1].text)
        self.idle(chat.id)

    def test_a_question_with_nowhere_to_go_is_kept_and_posted_later(self):
        """No DM channel at the moment it is asked: recorded anyway, so the
        typed answer still answers, and posted once the DM is back."""
        reachable = [True]
        self.mirror._dm_channel = lambda: DM_CHANNEL if reachable[0] else None
        self.listener.feed(dm("hi"))
        chat = wait_for(lambda: [t for t in self.chats() if t.surface == "dm"])[0]
        self.idle(chat.id)
        self.settled(chat.id)
        reachable[0] = False
        self.asking(chat)
        wait_for(lambda: self.mirror.pending_question(chat.id), what="the question recorded")
        self.assertFalse(any("Which package" in t for t in self.transport.texts(DM_CHANNEL)))
        reachable[0] = True
        self.now[0] += M.RETRY_S + 0.1
        wait_for(lambda: any("Which package" in t for t in self.transport.texts(DM_CHANNEL)),
                 what="the question posted once the DM is back")
        self.assertNotIn(f"<@{OWNER}>", self.transport.texts(DM_CHANNEL), "a DM never pings")
        self.listener.feed(dm("pnpm"))
        wait_for(lambda: self.provider.answers, what="the answer")
        self.assertEqual(self.provider.answers, [("q-1", "pnpm")])
        self.idle(chat.id)
        self.settled(chat.id)
        asked = [t for t in self.transport.texts(DM_CHANNEL) if "Which package" in t]
        self.assertEqual(len(asked), 1, "posted more than once")

    def test_a_dropped_question_event_is_recovered_from_the_log(self):
        chat = self.open_chat()
        self.hud_send(chat, "hello")
        place = self.place(chat.id)
        self.idle(chat.id)
        self.settled(chat.id)
        # The bus event is lost; only the log has the question. (The sync for
        # the user message may already find it there — that is the fix too.)
        with patch.object(self.mirror, "_question", lambda thread, data: None):
            self.asking(chat)
            time.sleep(0.1)
        self.mirror.subscription.dropped += 1             # an eviction: catch up
        self.mirror._kick()
        wait_for(lambda: any("Which package" in t for t in self.transport.texts(place)),
                 what="the question recovered from the log")
        wait_for(lambda: f"<@{OWNER}>" in self.transport.texts(place), what="its D1 ping")
        self.assertEqual(self.mirror.pending_question(chat.id), "q-1")
        self.listener.feed(guild_message("pnpm", place))
        wait_for(lambda: self.provider.answers, what="the answer")
        self.idle(chat.id)
        self.settled(chat.id)
        self.assertEqual(len([t for t in self.transport.texts(place) if "Which package" in t]), 1)
        self.assertEqual(self.transport.texts(place).count(f"<@{OWNER}>"), 1)

    def test_a_question_from_before_this_process_is_not_resurrected(self):
        chat = self.open_chat()
        self.hud_send(chat, "hello")
        place = self.place(chat.id)
        self.idle(chat.id)
        self.settled(chat.id)
        # A question its provider (and daemon) did not survive.
        self.stores.threads._append(chat.id, "log.jsonl", {
            "kind": "question", "turn_id": "a-turn-from-before", "thread_id": chat.id,
            "at": "2026-01-01T00:00:00+00:00",
            "data": {"req_id": "q-old", "text": "Which old thing?", "options": []}})
        with patch.object(self.mirror, "_open_question",
                          wraps=self.mirror._open_question) as looked:
            self.mirror.subscription.dropped += 1
            self.mirror._kick()
            wait_for(lambda: looked.called, what="the sync to look at it")
        time.sleep(0.1)
        self.assertFalse(any("Which old thing" in t for t in self.transport.texts(place)))
        self.assertIsNone(self.mirror.pending_question(chat.id))


class ReviewQuestionChecks(Harness):
    """PR #12 review: a question posted once however the log and the bus race,
    and an answered one never resurrected."""

    asking = BugbotQuestionChecks.asking

    def test_f1_a_sync_that_recovered_it_first_does_not_post_it_again(self):
        chat = self.open_chat()
        self.hud_send(chat, "hello")
        place = self.place(chat.id)
        self.idle(chat.id)
        self.settled(chat.id)
        # The daemon logs the question before publishing it: a sync (the
        # user_message's) runs between the two and recovers it from the log.
        original = self.mirror._handle

        def handle(record):
            if record.get("kind") == "question":
                with self.mirror._lock:
                    self.mirror._dirty.add(chat.id)
                self.mirror._process_dirty()
            return original(record)
        self.mirror._handle = handle
        self.asking(chat)
        wait_for(lambda: any("Which package" in t for t in self.transport.texts(place)))
        time.sleep(0.3)
        self.mirror._kick()
        time.sleep(0.3)
        self.assertEqual(len([t for t in self.transport.texts(place) if "Which package" in t]), 1)
        self.assertEqual(self.transport.texts(place).count(f"<@{OWNER}>"), 1)
        self.listener.feed(guild_message("pnpm", place))
        wait_for(lambda: self.provider.answers, what="the answer")
        self.idle(chat.id)

    def test_f1_the_natural_order_posts_it_once(self):
        chat = self.open_chat()
        self.hud_send(chat, "hello")
        place = self.place(chat.id)
        self.idle(chat.id)
        self.settled(chat.id)
        original = self.mirror._handle

        def handle(record):
            if record.get("kind") == "user_message":
                wait_for(lambda: any(r.get("kind") == "question"
                                     for r in self.stores.threads.read_log(chat.id)))
            return original(record)
        self.mirror._handle = handle
        self.asking(chat)
        wait_for(lambda: any("Which package" in t for t in self.transport.texts(place)))
        time.sleep(0.5)
        self.assertEqual(len([t for t in self.transport.texts(place) if "Which package" in t]), 1)
        self.assertEqual(self.transport.texts(place).count(f"<@{OWNER}>"), 1)
        self.listener.feed(guild_message("pnpm", place))
        wait_for(lambda: self.provider.answers, what="the answer")
        self.idle(chat.id)

    def test_m3d_a_question_answered_in_the_hud_is_not_posted_once_a_place_appears(self):
        """Never posted (no DM at the time), answered in the HUD, the turn
        still running: when the DM comes back, nothing is asked."""
        reachable = [True]
        self.mirror._dm_channel = lambda: DM_CHANNEL if reachable[0] else None
        self.listener.feed(dm("hi"))
        chat = wait_for(lambda: [t for t in self.chats() if t.surface == "dm"])[0]
        self.idle(chat.id)
        self.settled(chat.id)
        reachable[0] = False
        self.provider.plan["set it up"] = [("question", "Which package manager?"),
                                           ("wait",), ("text", "set up")]
        self.provider.release.clear()
        self.hud_send(chat, "set it up")
        wait_for(lambda: self.mirror.pending_question(chat.id), what="the question recorded")
        self.request("POST", f"/threads/{chat.id}/answer", {"req_id": "q-1", "text": "npm"})
        wait_for(lambda: self.mirror.pending_question(chat.id) is None, what="answered")
        reachable[0] = True
        with patch.object(self.mirror, "_open_question",
                          wraps=self.mirror._open_question) as looked:
            self.now[0] += M.RETRY_S + 0.1
            self.mirror._kick()
            wait_for(lambda: looked.called, what="the sync to look again")
        time.sleep(0.2)
        self.assertFalse(any("Which package" in t for t in self.transport.texts(DM_CHANNEL)),
                         "an answered question was posted")
        self.assertIsNone(self.mirror.pending_question(chat.id))
        self.provider.release.set()
        self.idle(chat.id)


class WriterChecks(unittest.TestCase):
    # The chat mirror makes, renames and archives *threads*, never channels:
    # it calls none of create_channel/modify_channel (B1's CHANNEL_WRITERS
    # grep in discord_linker_check), and only it and REST touch threads.
    THREAD_WRITERS = {"jarvis/v2/discord/mirror.py"}

    def test_only_the_mirror_renames_or_archives_chat_threads(self):
        import re
        root = Path(__file__).resolve().parents[2]
        # REST calls only: `projects.rename_thread` is the HUD's own rename.
        pattern = re.compile(r"\brest\.(rename_thread|archive_thread|start_thread_from_message)\s*\(")
        found = set()
        for path in (root / "jarvis").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if pattern.search(text):
                found.add(path.relative_to(root).as_posix())
            if path.name == "mirror.py":
                self.assertNotRegex(text, r"\b(create_channel|modify_channel)\s*\(")
        # The gateway starts a chat through the mirror's `start_chat`; nothing
        # else, and no tool, reaches these calls.
        self.assertTrue(found)
        self.assertLessEqual(found, self.THREAD_WRITERS, found - self.THREAD_WRITERS)

    def test_rest_has_no_delete(self):
        self.assertFalse([name for name in dir(DiscordRest) if "delete" in name.lower()])


class StoreChecks(unittest.TestCase):
    def test_save_keeps_the_surface_against_a_stale_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            stores = Stores(Path(tmp))
            project = stores.projects.create("School", tmp)
            thread = stores.threads.create(project.id, Role.CHAT, ProviderName.FAST)
            stores.threads.save(thread)
            stale = stores.threads.get(thread.id)
            stores.threads.set_surface(thread.id, "discord:123456")
            stale.turns = 3
            stores.threads.save(stale)
            stored = stores.threads.get(thread.id)
            self.assertEqual((stored.surface, stored.turns), ("discord:123456", 3))
            # A copy holding an older surface (before a move) cannot put it back.
            older = stores.threads.get(thread.id)
            stores.threads.set_surface(thread.id, "discord:654321")
            stores.threads.save(older)
            self.assertEqual(stores.threads.get(thread.id).surface, "discord:654321")
            # Only set_surface writes it, and only the known shapes.
            for bad in ("discord:", "discord:abc", "slack:1", "dm:x", 7):
                with self.assertRaises(StoreError):
                    stores.threads.set_surface(thread.id, bad)
            fresh = stores.threads.create(project.id, Role.CHAT, ProviderName.FAST)
            fresh.surface = "dm"
            stores.threads.save(fresh)
            self.assertIsNone(stores.threads.get(fresh.id).surface)
            with self.assertRaises(StoreError):
                stores.threads.create(project.id, Role.CHAT, ProviderName.FAST, surface="dm")

    def test_user_message_event_and_log_record_carry_the_same_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            stores = Stores(Path(tmp))
            provider = Scripted()
            daemon = Daemon(stores, {ProviderName.FAST: provider},
                            lambda thread_id, brief: (lambda *_a: Decision.ALLOW), 0)
            self.addCleanup(daemon.stop)
            project = stores.projects.create("School", tmp)
            chat = daemon.open_thread(project.id, Role.CHAT, ProviderName.FAST, {})
            seen = daemon.bus.subscribe({"kind": "user_message"})
            from jarvis.v2.provider import UserMessage
            daemon.send(chat.id, UserMessage("inlined text", [{"b64": "eA==", "mime": "image/png"}],
                                             via="discord", typed="typed",
                                             attachments=["a.txt"], discord_message_id="9",
                                             discord_channel_id="8"))
            event = seen.get(timeout=2)
            fields = {"text": "inlined text", "typed": "typed", "via": "discord",
                      "origin": "owner", "images": 1, "attachments": ["a.txt"],
                      "spoken": False, "discord_message_id": "9", "discord_channel_id": "8"}
            self.assertEqual(event["data"], fields)
            self.assertEqual((event["thread_id"], event["project_id"]), (chat.id, project.id))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                rows = [r for r in stores.threads.read_log(chat.id) if r.get("kind") == "user"]
                if rows:
                    break
                time.sleep(0.01)
            self.assertEqual(rows[0]["data"], fields)
            self.assertEqual(rows[0]["turn_id"], event["turn_id"])
            deadline = time.monotonic() + 2
            while daemon._sessions[chat.id].worker is not None and time.monotonic() < deadline:
                time.sleep(0.01)
            daemon.stop()
            # A message with no via is the owner's from the HUD, or a system one.
            self.assertEqual(M.user_line({"text": "x", "via": None}), "You (HUD): x")
            self.assertIsNone(M.user_line({"text": "x", "via": "discord"}))
            self.assertIsNone(M.user_line({"text": "x", "via": "dm"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
