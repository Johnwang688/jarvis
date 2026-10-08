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
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from jarvis import config, models, avatars, voice
from jarvis.tools import voicectl
from jarvis.v2 import hud_api as H, worktrees
from jarvis.v2.daemon import APIError, Daemon, DaemonError
from jarvis.v2.model import PermissionProfile, ProviderName as P, Role, TaskState, to_json
from jarvis.v2.provider import (Brief, BriefRefused, Decision, Event, EventKind as K, SessionHandle,
                               SessionLost, Usage)
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


class ModelFake(Fake):
    """A Fake that records what it was opened with and every model change,
    and can hold a turn open so a PATCH can land in the middle of one."""
    def __init__(self, name):
        super().__init__(name)
        self.opened, self.changes, self.permits = [], [], []
        self.hold = None
        self.entered = threading.Event()

    def start(self, thread, brief, permit):
        self.opened.append(("resume" if thread.provider_session_id else "start", brief))
        self.permits.append(permit)
        return SessionHandle(thread.id, self.name, "fake:" + thread.id, SimpleNamespace(brief=brief))

    resume = start

    def set_model(self, handle, model, effort):
        self.changes.append((model, effort))

    def send(self, handle, message):
        if self.hold is not None:
            yield Event(K.TURN_STARTED, handle.thread_id, {})
            self.entered.set()
            self.hold.wait(4)
        yield from super().send(handle, message)


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
                                AVATAR_ENV="",
                                # Never the owner's real Discord files: the guild
                                # file decides `GET /discord`'s answer (B1).
                                DISCORD_GUILD_PATH=self.root / "discord_guild.json",
                                DISCORD_TOKEN_PATH=self.root / "discord_token.json").items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        self.catalog = [models.normalize(dict(id="test/model", name="Test", context_length=10000,
                                             supported_parameters=["tools", "reasoning"],
                                             architecture={"input_modalities": ["text", "image"], "output_modalities": ["text"]},
                                             pricing={"prompt": "0", "completion": "0"})),
                        models.normalize(dict(id="test/thinker", name="Thinker", context_length=10000,
                                             supported_parameters=["tools", "reasoning"],
                                             reasoning={"supported_efforts": ["high", "medium", "low"]},
                                             architecture={"input_modalities": ["text"], "output_modalities": ["text"]},
                                             pricing={"prompt": "0.000001", "completion": "0.000002"}))]
        catalog = patch.object(models, "catalog", return_value=self.catalog)
        catalog.start()
        self.addCleanup(catalog.stop)
        # The catalog cache is never the owner's: effort ladders come from the
        # fixture above, never ~/.cache/jarvis.
        cache =patch.object(config, "MODEL_CACHE_PATH", self.root / "catalog.json")
        cache.start()
        self.addCleanup(cache.stop)
        info = patch.object(models, "cached_info",
                            side_effect=lambda model_id: next((m for m in self.catalog if m.id == model_id), None))
        info.start()
        self.addCleanup(info.stop)
        routing = patch.object(config, "ROUTING_PATH", self.root / "routing.json")
        routing.start()
        self.addCleanup(routing.stop)
        always = patch.object(config, "V2_ALWAYS_ASK", self.root / "always-ask.json")
        always.start()
        self.addCleanup(always.stop)
        # The owner's real Claude login must not be read, and the suite must
        # not call the usage endpoint. A missing file is "not reported".
        creds = patch.dict("os.environ", {
            "JARVIS_CLAUDE_CREDENTIALS": str(self.root / "missing-claude-credentials.json"),
        })
        creds.start()
        self.addCleanup(creds.stop)
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

    def test_discord_status_route_holds_states_never_secrets(self):
        """`GET /discord` (S1): connected + the command sync, nothing else."""
        with patch.object(config, "DISCORD_TOKEN_PATH", self.root / "absent.json"):
            self.assertEqual(self.request("GET", "/discord"),
                             {"connected": False, "commands": {"state": "off", "count": 0,
                                                               "synced_at": None, "error": None},
                              "reporter": None,
                              # B1: no guild file, no linker, no permission check.
                              "guild": {"configured": False, "id": None},
                              "linker": None, "permissions": None})

        class Surface:
            def status(self):
                return {"connected": True, "token": "synthetic-leak",
                        "commands": {"state": "ok", "count": 11, "synced_at": 1.0,
                                     "error": None, "path": "/webhooks/1/synthetic-leak"}}

        self.daemon.discord = Surface()
        body = self.request("GET", "/discord")
        self.assertEqual(body, {"connected": True, "commands": {
            "state": "ok", "count": 11, "synced_at": 1.0, "error": None}, "reporter": None,
            "guild": {"configured": False, "id": None}, "linker": None, "permissions": None})
        self.assertNotIn("synthetic-leak", json.dumps(body))

        class WithReporter(Surface):
            """PR A: the update poster's health, field by field — a stray key,
            a URL in an odd field or a non-integer counter never passes."""
            def status(self):
                status = super().status()
                status["reporter"] = {
                    "state": "degraded", "reason": "channel 1 is missing a bot permission (50013)",
                    "counters": {"posts": 4, "dm": 1, "leak": "synthetic-leak"},
                    "dropped": 2, "url": "https://discord.com/synthetic-leak",
                    "last_error": {"op": "create_thread", "status": 403, "code": 50013,
                                   "at": 2.5, "message": "synthetic-leak"}}
                return status

        self.daemon.discord = WithReporter()
        body = self.request("GET", "/discord")
        self.assertEqual(body["reporter"], {
            "state": "degraded", "reason": "channel 1 is missing a bot permission (50013)",
            "counters": {"posts": 4, "dm": 1}, "dropped": 2,
            "last_error": {"op": "create_thread", "status": 403, "code": 50013, "at": 2.5}})
        self.assertNotIn("synthetic-leak", json.dumps(body))

        class WithLinker(Surface):
            """B1: guild, linker and permissions, field by field."""
            def status(self):
                status = super().status()
                status.update(
                    guild={"configured": True, "id": "800000000000000001", "token": "synthetic-leak"},
                    linker={"state": "degraded", "reason": "1 channel rename(s) pending",
                            "pending_renames": 1, "awaiting_approval": 2,
                            "url": "https://discord.com/synthetic-leak"},
                    permissions={"missing": ["Attach Files"], "excess": ["Administrator"],
                                 "administrator": True, "checked_at": 3.0,
                                 "body": "synthetic-leak"})
                return status

        self.daemon.discord = WithLinker()
        body = self.request("GET", "/discord")
        self.assertEqual(body["guild"], {"configured": True, "id": "800000000000000001"})
        self.assertEqual(body["linker"], {"state": "degraded", "reason": "1 channel rename(s) pending",
                                          "pending_renames": 1, "pending_moves": 0,
                                          "awaiting_approval": 2})
        self.assertEqual(body["permissions"], {"missing": ["Attach Files"], "excess": ["Administrator"],
                                               "administrator": True, "checked_at": 3.0})
        self.assertNotIn("synthetic-leak", json.dumps(body))
        # A start that failed (review fix 7): red with the class, not "pending".
        self.daemon.discord, self.daemon.discord_error = None, "RuntimeError"
        body = self.request("GET", "/discord")
        self.assertEqual(body["commands"], {"state": "failed", "count": 0, "synced_at": None,
                                            "error": "RuntimeError"})
        self.assertEqual(body["reporter"]["state"], "down")
        self.assertIn("RuntimeError", body["reporter"]["reason"])
        self.daemon.discord_error = None
        self.request("GET", "/discord", headers={"Sec-Fetch-Site": "cross-site"}, status=403)
        self.request("GET", "/discord?x=1", status=400)

    # -- B1: project channels -------------------------------------------------

    def owner(self, method, path, body=None, status=200):
        """The HUD's own request: its listener and its Origin."""
        port = self.daemon.face_port
        return self.request(method, path, body, status, port=port,
                            headers={"Origin": f"http://127.0.0.1:{port}"})

    def discord_linker(self):
        from tests.v2.discord_linker_check import (ARCHIVE, CATEGORY, GUILD, TOKEN, UNGROUPED,
                                                   FakeDiscord)
        from jarvis.v2.discord import guild as guildmod
        from jarvis.v2.discord import linker as linker_mod
        from jarvis.v2.discord.linker import ChannelLinker
        from jarvis.v2.discord.rest import DiscordRest
        token = self.root / "discord_token.json"
        token.write_text(json.dumps({"bot_token": TOKEN, "owner_id": "1"}))
        for name, value in (("DISCORD_TOKEN_PATH", token),
                            ("DISCORD_GUILD_PATH", self.root / "discord_guild.json")):
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        pace = patch.object(linker_mod, "CREATE_INTERVAL", 0.0)
        pace.start()
        self.addCleanup(pace.stop)
        network = patch("httpx.request", side_effect=AssertionError("live network"))
        network.start()
        self.addCleanup(network.stop)
        fake = FakeDiscord()
        fake.configured()
        guildmod.write(guildmod.GuildConfig(GUILD, CATEGORY, ARCHIVE, UNGROUPED))
        rest = DiscordRest(fake)
        self.addCleanup(rest.close)
        linker = ChannelLinker(self.daemon, rest, approvals=self.daemon.approvals)
        self.addCleanup(linker.close)
        self.daemon.discord = SimpleNamespace(linker=linker, status=lambda: {
            "connected": True, "commands": {}, **linker.status()})
        self.addCleanup(setattr, self.daemon, "discord", None)
        return fake, linker

    def test_project_discord_routes_are_owner_only(self):
        fake, _ = self.discord_linker()
        path = f"/projects/{self.project.id}/discord"
        # Reading the light is not an action; acting is the owner's alone.
        self.assertEqual(self.request("GET", path)["state"], "unlinked")
        for port, headers in ((self.daemon.port, {}),
                              (self.daemon.port, {"Origin": f"http://127.0.0.1:{self.daemon.port}"}),
                              (self.daemon.face_port, {})):
            for target, body in ((path, {"action": "create"}), ("/discord/backfill", {})):
                with self.subTest(port=port, headers=headers, target=target):
                    self.request("POST", target, body, 403, port=port, headers=headers)
        self.assertEqual(fake.named("POST"), [], "a refused request reached Discord")
        self.assertIsNone(self.stores.projects.get(self.project.id).discord_channel_id)

    def test_project_discord_create_link_unlink_and_validation(self):
        from tests.v2.discord_linker_check import CATEGORY, OTHER_GUILD, UNGROUPED
        fake, _ = self.discord_linker()
        path = f"/projects/{self.project.id}/discord"
        for body, status, words in (
                ({}, 400, "missing fields: action"),
                ({"action": "delete"}, 400, "create, link or unlink"),
                ({"action": "link"}, 400, "channel_id"),
                ({"action": "create", "channel_id": "1"}, 400, "no channel_id"),
                ({"action": "link", "channel_id": "nope"}, 400, "digits"),
                ({"action": "link", "channel_id": 830000000000000001}, 400, "digits"),
                ({"action": "link", "channel_id": "839999999999999999"}, 400, "no channel"),
                ({"action": "link", "channel_id": UNGROUPED}, 400, "Inbox"),
                ({"action": "unlink", "extra": 1}, 400, "unknown fields")):
            with self.subTest(body=body):
                self.assertIn(words, self.owner("POST", path, body, status)["error"])
        fake.add("830000000000000001", "notes", guild=OTHER_GUILD)
        self.assertIn("another server", self.owner(
            "POST", path, {"action": "link", "channel_id": "830000000000000001"}, 400)["error"])
        fake.add("830000000000000002", "test-notes", parent=CATEGORY)
        self.events_all()
        view = self.owner("POST", path, {"action": "link", "channel_id": "830000000000000002"})
        self.assertEqual((view["state"], view["origin"], view["name"]),
                         ("linked_ok", "linked", "test-notes"))
        updated = [e for e in self.events_all() if e["kind"] == "project_updated"]
        self.assertEqual(updated[-1]["data"]["changed"], ["discord_channel_id"])
        # The same channel cannot be linked to a second project (409).
        self.assertIn("already linked", self.owner(
            "POST", f"/projects/{self.second.id}/discord",
            {"action": "link", "channel_id": "830000000000000002"}, 409)["error"])
        self.assertEqual(self.owner("POST", path, {"action": "unlink"})["state"], "unlinked")
        self.assertIn("830000000000000002", fake.channels, "unlink keeps the channel")
        view = self.owner("POST", path, {"action": "create"})
        self.assertEqual((view["state"], view["origin"]), ("linked_ok", "created"))
        self.assertEqual(self.request("GET", path)["channel_id"], view["channel_id"])
        self.request("GET", "/projects/deadbeef/discord", status=404)
        self.owner("POST", "/projects/deadbeef/discord", {"action": "create"}, 404)

    def test_backfill_route(self):
        self.discord_linker()
        result = self.owner("POST", "/discord/backfill", {})
        self.assertEqual(result["created"], 2)
        self.assertEqual({r["name"] for r in result["results"]}, {"Test", "Other"})
        self.assertTrue(all(self.stores.projects.get(p).discord_channel_origin == "created"
                            for p in (self.project.id, self.second.id)))
        self.assertEqual(self.owner("POST", "/discord/backfill", {})["created"], 0)
        self.owner("POST", "/discord/backfill", {"x": 1}, 400)
        status = self.request("GET", "/discord")
        self.assertEqual(status["guild"]["configured"], True)
        self.assertIn(status["linker"]["state"], ("ok", "degraded"))

    def test_without_a_surface_the_light_says_why_and_actions_refuse(self):
        path = f"/projects/{self.project.id}/discord"
        with patch.object(config, "DISCORD_GUILD_PATH", self.root / "absent.json"):
            self.assertEqual(self.request("GET", path)["state"], "unconfigured")
            self.assertIn("not running", self.owner("POST", path, {"action": "create"}, 409)["error"])

    def test_projects_refuse_the_channel_field_and_tag_who_renamed(self):
        for method, path, body in (
                ("POST", "/projects", {"name": "x", "root": str(self.other),
                                       "discord_channel_id": "1"}),
                ("PATCH", self.url, {"discord_channel_id": "1"}),
                ("PATCH", self.url, {"discord_channel_origin": "linked"})):
            with self.subTest(method=method, body=body):
                error = self.owner(method, path, body, 400)["error"]
                self.assertEqual(error, "link a channel from the project dialog")
        self.assertIsNone(self.stores.projects.get(self.project.id).discord_channel_id)
        self.events_all()
        self.owner("PATCH", self.url, {"name": "Renamed by me"})
        self.request("PATCH", self.url, {"name": "Renamed by a script"})
        updated = [e["data"] for e in self.events_all() if e["kind"] == "project_updated"]
        self.assertEqual([(e["by"], e["previous"]) for e in updated],
                         [("owner", {"name": "Test"}), ("api", {"name": "Renamed by me"})])

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
        # A `/skill` turn (S1) shows as the owner typed it.
        self.stores.threads._append(thread.id, "log.jsonl", {
            "kind": "user", "at": "2026-10-07T00:00:00Z", "thread_id": thread.id,
            "data": {"text": "keep it short", "skill": "morning-briefing"}})
        self.assertEqual(self.request("GET", path + "/transcript")["messages"][-1]["text"],
                         "/skill morning-briefing keep it short")
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

    def test_claude_subscription_quota_is_reported_not_invented(self):
        token = "oat-hud-test-token-value"
        path = self.root / "claude-credentials.json"
        path.write_text(json.dumps({"claudeAiOauth": {"accessToken": token, "expiresAt": 1}}))
        body = {
            "five_hour": {"utilization": 74.0, "resets_at": "2026-10-07T01:00:00Z"},
            "seven_day": {"utilization": 12.0, "resets_at": "2026-10-12T01:00:00Z"},
            "seven_day_opus": {"utilization": 99.0, "resets_at": "2026-10-12T01:00:00Z"},
        }
        seen = []

        def fake_fetch(got):
            seen.append(got)
            return body

        with patch.dict("os.environ", {"JARVIS_CLAUDE_CREDENTIALS": str(path)}), \
             patch.object(H, "fetch_claude_usage", fake_fetch):
            usage = self.request("GET", "/usage")
            rendered = json.dumps(usage)
            self.assertNotIn(token, rendered)
            self.assertEqual(seen, [token])
            windows = usage["providers"]["claude"]["quota"]["windows"]
            self.assertEqual([w["name"] for w in windows], ["5h", "week"])
            self.assertEqual(windows[0]["used_percent"], 74.0)
            self.assertEqual(windows[1]["used_percent"], 12.0)
            self.assertNotIn("opus", rendered)
            # A 429 keeps the last good reading rather than blanking the meters.
            ledger = self.daemon.router.ledger
            ledger._claude_quota_at = 0
            with patch.object(H, "fetch_claude_usage", side_effect=H.ClaudeUsageUnavailable("429")):
                self.assertEqual(ledger.quota("claude")["windows"][0]["used_percent"], 74.0)
            # A 401 is a dead login: null, not yesterday's numbers.
            ledger._claude_quota_at = 0
            with patch.object(H, "fetch_claude_usage", return_value=None):
                self.assertIsNone(ledger.quota("claude"))
        # No credential file, no call.
        called = []
        with patch.object(H, "fetch_claude_usage", lambda got: called.append(got)):
            self.assertIsNone(H.HUDLedger(self.stores).quota("claude"))
        self.assertEqual(called, [])

    def test_schedules_crud_fire_skip_disable_and_run_now(self):
        schedule = self.schedule()
        self.assertEqual(set(schedule), {"id", "project_id", "brief", "cron", "every_s", "enabled",
                                          "last_run_at", "last_task_id", "next_run_at", "created", "describe"})
        self.assertEqual(schedule["describe"], "every 1 minute")
        stored = {k: v for k, v in schedule.items() if k != "describe"}
        self.assertEqual(Schedules(self.daemon).get(schedule["id"]), stored)
        path = "/schedules/" + schedule["id"]
        self.assertTrue((self.stores.root / "schedules" / (schedule["id"] + ".json")).is_file())
        self.assertEqual(self.request("GET", "/schedules"), [schedule])
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

    def test_thread_cwd_backfilled_from_saved_brief(self):
        thread = self.thread()
        record = self.stores.threads.get(thread.id)
        record.cwd = None                       # a thread opened before the field existed
        self.stores.threads.save(record)
        listed = {t["id"]: t for t in self.request("GET", "/threads")}
        self.assertEqual(listed[thread.id]["cwd"], str(self.project_root))
        self.assertIsNone(self.stores.threads.get(thread.id).cwd)   # listing wrote nothing

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
        brief_path = self.stores.threads.path(thread.id).with_name("brief.json")
        brief = brief_path.read_bytes()
        self.assertEqual(before["cwd"], str(self.project_root))
        result = self.request("PATCH", path, {"project_id": self.second.id})
        # A move re-labels the thread; it never re-roots it. The saved brief
        # (cwd, profile, always_ask) is untouched and the wire still reports
        # the folder the thread was opened in.
        self.assertEqual(brief_path.read_bytes(), brief)
        listed = {t["id"]: t for t in self.request("GET", "/threads")}
        self.assertEqual(listed[thread.id]["cwd"], str(self.project_root))
        self.assertEqual(listed[thread.id]["project_id"], self.second.id)
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
        self.request("POST", path + "/send", {"text": "resumed in the new project"}, status=202)
        self.settled(thread)
        # Resumed from the saved brief: still the original folder.
        self.assertEqual(self.daemon._sessions[thread.id].handle.native.brief.cwd, str(self.project_root))
        self.assertEqual(brief_path.read_bytes(), brief)
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

    def test_picker_body_keys_are_the_contract(self):
        """A6: the HUD sent {id}, {name} and {mute}; the daemon read model,
        voice and muted, so every click reset the setting it meant to change.
        A wrong key is now refused by name rather than read as 'absent'."""
        self.request("POST", "/models", {"add": "test/model"})
        self.request("POST", "/model", {"model": "test/model"})
        self.assertEqual(models.selected(), "test/model")
        for path, body in (("/model", {"id": "test/model"}), ("/model", {"id": "test/model", "effort": "high"}),
                           ("/voice", {"name": ""}), ("/mute", {"mute": True}),
                           ("/mute", {"muted": "yes"}), ("/models", {"id": "test/model"})):
            error = self.request("POST", path, body, status=400)["error"]
            self.assertTrue("unknown fields" in error or "missing fields" in error or "boolean" in error, error)
        # None of those refusals moved the selection back to the default.
        self.assertEqual(models.selected(), "test/model")
        self.assertFalse(voicectl.is_muted())
        # Effort goes to /models {model, effort}, and does not move the selection.
        self.request("POST", "/models", {"model": "test/model", "effort": ""})
        self.assertEqual(models.selected(), "test/model")

    # -- the model a chat thread runs on (decisions 2026-10-06, part A) ------

    def model_fakes(self):
        fakes = {name: ModelFake(name) for name in P}
        self.daemon.providers.update(fakes)
        return fakes

    def chat(self, provider="fast", status=201, **brief):
        return self.request("POST", "/threads", {"project_id": self.project.id, "role": "chat",
                                                 "provider": provider, "brief": brief}, status=status)

    def send_and_settle(self, thread_id, text="hi"):
        self.request("POST", f"/threads/{thread_id}/send", {"text": text}, status=202)
        eventually(lambda: self.daemon._sessions[thread_id].worker is None)

    def brief_bytes(self, thread_id):
        return self.stores.threads.path(thread_id).with_name("brief.json").read_bytes()

    def test_an_archived_thread_or_one_in_an_archived_project_keeps_its_model(self):
        # Read-only until restored, the rule rename and move already follow.
        from jarvis.v2 import projects
        self.model_fakes()
        self.request("POST", "/models", {"add": "test/thinker"})
        tid = self.chat()["id"]
        projects.archive_thread(self.daemon, tid)
        self.events_all()
        for body in ({"model": "test/thinker"}, {"effort": "low"}, {"model": None}):
            self.assertEqual(self.request("PATCH", f"/threads/{tid}", body, status=409)["error"],
                             "restore the thread before changing its model", body)
        with self.assertRaises(APIError) as refused:
            self.daemon.set_thread_model(tid, {"model": "test/thinker"})
        self.assertEqual(refused.exception.status, 409)
        stored = self.stores.threads.get(tid)
        self.assertEqual((stored.model, stored.effort), (None, None), "nothing is written")
        self.assertEqual([e["kind"] for e in self.events_all()
                          if e["kind"] in ("thread_updated", "model_set")], [], "and nothing is announced")
        self.assertFalse([r for r in self.stores.threads.read_log(tid) if r.get("kind") == "model_set"])
        # A live thread in an archived project is refused the same way.
        projects.restore_thread(self.daemon, tid)
        token = projects.impact(self.daemon, self.project.id)["token"]
        projects.archive_project(self.daemon, self.project.id, token)
        self.assertEqual(self.request("PATCH", f"/threads/{tid}", {"model": "test/thinker"}, status=409)["error"],
                         "restore the thread before changing its model")
        self.assertIsNone(self.stores.threads.get(tid).model)
        # Restored, the change goes through.
        projects.restore_project(self.daemon, self.project.id)
        self.assertEqual(self.request("PATCH", f"/threads/{tid}", {"model": "test/thinker"})["model"],
                         "test/thinker")

    def test_a_projects_routing_models_are_held_to_cli_models_on_write(self):
        # The rule routing.json and /route already follow (`router._cli_model`).
        before = len(self.stores.projects.list())
        root = str(self.other)
        for models, reason in (
                ({"implementer": {"codex": "gpt-9/high"}}, "is not a codex model Jarvis knows"),
                ({"reviewer": {"claude": "claude-haiku-4-5/high"}}, "has no effort control"),
                ({"reviewer": {"claude": "claude-opus-5-5/turbo"}}, "invalid model/effort"),
                ({"orchestrator": {"codex": "gpt-5.5"}}, "expected model/effort"),
                ({"orchestrator": {"fast": "x/high"}}, "must map claude/codex"),
                ({"janitor": {"codex": "gpt-5.5/high"}}, "role must be")):
            error = self.request("POST", "/projects", {"name": "routed", "root": root,
                                                       "routing": {"models": models}}, status=400)["error"]
            self.assertIn(reason, error, models)
        self.assertEqual(len(self.stores.projects.list()), before, "a refused project is never created")
        good = {"implementer": {"codex": "gpt-5.5/high", "claude": "roster/default"},
                "reviewer": {"claude": "claude-sonnet-5-5/max"}}
        made = self.request("POST", "/projects", {"name": "routed", "root": root,
                                                  "routing": {"models": good}}, status=201)
        self.assertEqual(made["routing"]["models"], good)
        # PATCH is held to the same rule, and a refusal changes nothing.
        error = self.request("PATCH", f"/projects/{made['id']}", {"routing": {"models": {
            "implementer": {"claude": "claude-opus-5-5/xhigh", "codex": "gpt-5.6-luna/max"}}}}, status=400)["error"]
        self.assertIn("routing.models.implementer.codex", error)
        self.assertIn("does not offer effort 'max'", error)
        self.assertEqual(self.stores.projects.get(made["id"]).routing.models, good)
        self.request("PATCH", f"/projects/{made['id']}", {"routing": {"models": {
            "implementer": {"claude": "claude-opus-5-5/xhigh"}}}})
        self.assertEqual(self.stores.projects.get(made["id"]).routing.models,
                         {"implementer": {"claude": "claude-opus-5-5/xhigh"}})
        # A project saved before the check stays editable in every other way.
        legacy = self.stores.projects.get(made["id"])
        legacy.routing.models = {"implementer": {"codex": "gpt-9/high"}}
        self.stores.projects.save(legacy)
        self.assertEqual(self.request("PATCH", f"/projects/{made['id']}", {"name": "renamed"})["name"], "renamed")

    def test_open_thread_checks_the_model_before_anything_exists(self):
        fakes = self.model_fakes()
        self.request("POST", "/models", {"add": "test/thinker"})
        body = self.chat(model="test/thinker", effort="low")
        self.assertEqual((body["model"], body["effort"]), ("test/thinker", "low"))
        self.assertEqual((fakes[P.FAST].opened[-1][1].model, fakes[P.FAST].opened[-1][1].effort),
                         ("test/thinker", "low"))
        before = len(self.stores.threads.list())
        for brief, reason in (({"model": "test/model-gone"}, "supports tool calling"),
                              ({"model": "test/thinker", "effort": "max"}, "does not offer 'max'"),
                              ({"model": "test/thinker", "effort": "turbo"}, "not a reasoning effort")):
            self.assertIn(reason, self.chat(status=400, **brief)["error"])
        self.request("POST", "/models", {"remove": "test/thinker"})
        self.assertIn("not on your model roster", self.chat(status=400, model="test/thinker")["error"])
        self.assertEqual(len(self.stores.threads.list()), before, "a refused model leaves no thread behind")
        self.assertIn("not a Claude model", self.chat("claude", status=400, model="test/thinker")["error"])

    def test_claude_and_codex_chat_threads_are_full_agents_behind_the_gate(self):
        """A1: a Claude or Codex chat thread runs in the thread's folder under
        the project's profile, its tool calls through the §6 permit — the
        same `build_permit` every task worker gets — and its approvals reach
        the owner through the daemon's broker."""
        fake = ModelFake(P.CLAUDE)
        gate = Daemon(Stores(self.root / "gate"), {P.CLAUDE: fake, P.CODEX: ModelFake(P.CODEX)}, None, 0)
        self.addCleanup(gate.stop)
        project = gate.stores.projects.create("Gated", str(self.project_root), always_ask=["make deploy"])
        thread = gate.open_thread(project.id, Role.CHAT, P.CLAUDE, {})
        mode, brief = fake.opened[-1]
        self.assertEqual(mode, "start")
        self.assertEqual((brief.model, brief.effort), ("claude-opus-5-5", "high"), "A5: Opus 5.5 at high")
        self.assertIsNone(thread.model, "a default thread stores no pin")
        self.assertEqual(brief.cwd, str(self.project_root))
        self.assertEqual(brief.profile, project.profile)
        self.assertEqual(brief.always_ask, ["make deploy"])
        self.assertIsNone(brief.allowed_tools, "a full agent: the provider's whole native toolset")
        self.assertIn("chat thread", brief.system_append)
        saved = json.loads(gate.stores.threads.path(thread.id).with_name("brief.json").read_text())
        self.assertIsNone(saved["model"], "the saved brief keeps the choice (default), not a resolution")
        permit = fake.permits[-1]
        # Layer 1: never approvable, never asked of anyone.
        self.assertEqual(permit("Bash", {"command": "sudo rm -rf /"}, brief), Decision.DENY)
        self.assertEqual(gate.approvals.pending(), [])
        # Layer 2: the project's always-ask addition reaches the owner's broker.
        answer = {}
        asker = threading.Thread(target=lambda: answer.setdefault(
            "d", permit("Bash", {"command": "make deploy"}, brief)), daemon=True)
        asker.start()
        eventually(lambda: gate.approvals.pending())
        pending = gate.approvals.pending()[0]
        self.assertEqual((pending.thread_id, pending.command), (thread.id, "make deploy"))
        gate.resolve_approval(pending.req_id, Decision.DENY)
        asker.join(4)
        self.assertEqual(answer["d"], Decision.DENY)
        # Codex cannot enforce always-ask additions (codex_config.validate),
        # so a Codex chat thread in this project is refused before any
        # record exists; in a project without them it gets the same brief.
        with self.assertRaises(APIError) as refused:
            gate.open_thread(project.id, Role.CHAT, P.CODEX, {})
        self.assertIn("always-ask", str(refused.exception))
        self.assertEqual(gate.providers[P.CODEX].opened, [])
        plain = gate.stores.projects.create("Plain", str(self.project_root))
        codex = gate.open_thread(plain.id, Role.CHAT, P.CODEX, {})
        _, codex_brief = gate.providers[P.CODEX].opened[-1]
        self.assertEqual((codex_brief.model, codex_brief.effort), ("gpt-6-astra", "xhigh"),
                         "A5: Codex's routing default")
        self.assertEqual(codex_brief.profile, PermissionProfile.AUTO)
        self.assertIsNone(codex.model)

    def test_a_provider_the_project_cannot_use_leaves_no_thread(self):
        """Codex runs auto only and cannot enforce always-ask commands;
        Claude and the fast path have no strict mode. Each is refused with
        the reason before a record exists, and a provider that still
        refuses at start leaves nothing behind either."""
        fakes = self.model_fakes()
        ask = self.stores.projects.create("Ask", str(self.project_root), profile=PermissionProfile.ASK)
        gated = self.stores.projects.create("Gated", str(self.project_root), always_ask=["make deploy"])
        strict = self.stores.projects.create("Strict", str(self.project_root),
                                             profile=PermissionProfile.STRICT)
        before = {t.id for t in self.stores.threads.list()}
        for project, provider, reason in ((ask, "codex", "only run a project on the auto profile"),
                                          (gated, "codex", "always-ask commands (make deploy)"),
                                          (strict, "claude", "cannot run a strict project"),
                                          (strict, "fast", "cannot run a strict project"),
                                          (strict, "codex", "auto profile")):
            with self.subTest(project=project.name, provider=provider):
                error = self.request("POST", "/threads", {"project_id": project.id, "role": "chat",
                                                          "provider": provider, "brief": {}}, status=400)
                self.assertIn(reason, error["error"])
        self.assertEqual({t.id for t in self.stores.threads.list()}, before)
        self.assertEqual(self.stores.threads._pending, {})
        self.assertEqual([f.opened for f in fakes.values()], [[], [], []], "no provider was started")
        # The ones a project can use still open.
        self.request("POST", "/threads", {"project_id": ask.id, "role": "chat", "provider": "claude",
                                          "brief": {}}, status=201)
        self.request("POST", "/threads", {"project_id": gated.id, "role": "chat", "provider": "fast",
                                          "brief": {}}, status=201)
        # A refusal the pre-check cannot see (the provider's own, at start)
        # removes the never-started record too.
        known = {t.id for t in self.stores.threads.list()}

        def refuse(thread, brief, permit):
            raise BriefRefused("claude session failed to start: login expired")
        fakes[P.CLAUDE].start = refuse
        self.assertIn("login expired", self.chat("claude", status=409)["error"])
        self.assertEqual({t.id for t in self.stores.threads.list()}, known)
        self.assertEqual(self.stores.threads._pending, {})
        self.assertFalse([p for p in (self.root / "data").rglob("thread.json")
                          if p.parent.name not in known], "no thread directory is left on disk")

    def test_thread_models_say_which_profiles_each_provider_runs(self):
        body = self.request("GET", "/thread-models")["providers"]
        self.assertEqual({p: (body[p]["profiles"], body[p]["always_ask"]) for p in body},
                         {"fast": (["auto", "ask"], True), "claude": (["auto", "ask"], True),
                          "codex": (["auto"], False)})

    def test_a_caller_brief_cannot_loosen_the_project(self):
        """A Claude or Codex chat thread is a full agent; the brief fields
        that fence it are the project's, whatever a caller sends."""
        fakes = self.model_fakes()
        project = self.stores.projects.create("Fenced", str(self.project_root),
                                              profile=PermissionProfile.ASK, always_ask=["make deploy"])
        before = {t.id for t in self.stores.threads.list()}
        for brief, field in (({"profile": "auto"}, "profile"),
                             ({"always_ask": []}, "always_ask"),
                             ({"always_ask": ["rm"]}, "always_ask"),
                             ({"cwd": str(self.other)}, "cwd"),
                             ({"cwd": "/"}, "cwd"),
                             ({"mcp_servers": {"git": {"command": "git-mcp"}}}, "mcp_servers")):
            with self.subTest(brief=brief):
                error = self.request("POST", "/threads", {"project_id": project.id, "role": "chat",
                                                          "provider": "claude", "brief": brief}, status=400)
                self.assertIn("cannot loosen the project's " + field, error["error"])
        self.assertEqual({t.id for t in self.stores.threads.list()}, before)
        self.assertEqual(fakes[P.CLAUDE].opened, [])
        # The project's own values, or stricter ones, are accepted.
        self.request("POST", "/threads", {"project_id": project.id, "role": "chat", "provider": "claude",
                                          "brief": {"cwd": str(self.project_root), "profile": "ask",
                                                    "always_ask": ["make deploy"]}}, status=201)
        _, brief = fakes[P.CLAUDE].opened[-1]
        self.assertEqual((brief.cwd, brief.profile, brief.always_ask, brief.mcp_servers),
                         (str(self.project_root), PermissionProfile.ASK, ["make deploy"], {}))
        self.request("POST", "/threads", {"project_id": project.id, "role": "chat", "provider": "fast",
                                          "brief": {"always_ask": ["make deploy", "git push"]}}, status=201)
        self.assertEqual(fakes[P.FAST].opened[-1][1].always_ask, ["make deploy", "git push"])

    def test_patch_model_applies_from_the_next_message_and_never_touches_the_brief(self):
        fakes = self.model_fakes()
        self.request("POST", "/models", {"add": "test/thinker"})
        thread = self.chat()
        tid = thread["id"]
        saved = self.brief_bytes(tid)
        self.send_and_settle(tid)
        self.events_all()
        body = self.request("PATCH", f"/threads/{tid}", {"model": "test/thinker"})
        self.assertEqual((body["model"], body["effort"]), ("test/thinker", None))
        stored = self.stores.threads.get(tid)
        self.assertEqual((stored.model, stored.effort), ("test/thinker", None))
        self.assertEqual(self.daemon._sessions[tid].thread.model, "test/thinker")
        self.assertEqual(self.brief_bytes(tid), saved, "brief.json is never rewritten")
        self.assertEqual(fakes[P.FAST].changes, [], "nothing changes until the next message")
        published = [e for e in self.events_all() if e["kind"] in ("thread_updated", "model_set")]
        self.assertEqual([e["kind"] for e in published], ["model_set", "thread_updated"])
        self.assertEqual(published[1]["data"]["effective_effort"], "high")
        # The record plus the envelope a rename's thread_updated also carries.
        self.assertEqual(published[1]["data"]["thread_id"], tid)
        self.assertEqual(published[1]["data"]["changed"], ["model", "effort"])
        self.assertEqual(published[1]["data"]["id"], tid)
        self.send_and_settle(tid)
        self.assertEqual(fakes[P.FAST].changes, [("test/thinker", "high")])
        usage = [r for r in self.stores.threads.read_log(tid) if r.get("kind") == "usage"]
        self.assertEqual((usage[0]["data"]["model"], usage[-1]["data"]["model"]),
                         (models.tier("orchestrator"), "test/thinker"), "each turn records its model")
        # Effort alone; then back to default; each a system line.
        self.request("PATCH", f"/threads/{tid}", {"effort": "low"})
        self.request("PATCH", f"/threads/{tid}", {"model": None})
        self.assertEqual((self.stores.threads.get(tid).model, self.stores.threads.get(tid).effort), (None, None))
        lines = [m for m in self.request("GET", f"/threads/{tid}/transcript")["messages"] if m["role"] == "system"]
        self.assertEqual(len(lines), 3, lines)
        self.assertIn("test/thinker · high (from the next message)", lines[0]["text"])
        self.assertIn("test/thinker · low", lines[1]["text"])
        self.assertIn("default", lines[2]["text"])
        self.assertEqual(self.brief_bytes(tid), saved)
        # Refusals, with the reason; none changes anything.
        self.assertIn("does not offer", self.request("PATCH", f"/threads/{tid}",
                      {"model": "test/thinker", "effort": "xhigh"}, status=400)["error"])
        self.assertIn("supports tool calling", self.request("PATCH", f"/threads/{tid}",
                      {"model": "nope/nope"}, status=400)["error"])
        # Rename, move and model change are three mutually exclusive shapes.
        for mixed in ({"model": "test/thinker", "project_id": self.second.id},
                      {"effort": "low", "title": "renamed"},
                      {"title": "renamed", "project_id": self.second.id},
                      {"title": "renamed", "model": None, "project_id": self.second.id}):
            self.assertEqual(self.request("PATCH", f"/threads/{tid}", mixed, status=400)["error"],
                             "rename, move and model change are separate requests", mixed)
        self.assertIn("missing fields", self.request("PATCH", f"/threads/{tid}", {}, status=400)["error"])
        self.assertNotEqual(self.stores.threads.get(tid).title, "renamed")
        self.assertEqual(self.stores.threads.get(tid).project_id, thread["project_id"])
        self.request("PATCH", f"/threads/{tid}", {"provider": "claude"}, status=400)
        self.assertEqual(self.stores.threads.get(tid).model, None)

    def test_a_default_thread_follows_a_global_change_between_turns(self):
        fakes = self.model_fakes()
        self.request("POST", "/models", {"add": "test/thinker"})
        tid = self.chat()["id"]
        self.send_and_settle(tid)
        self.assertEqual(fakes[P.FAST].changes, [])
        self.request("POST", "/model", {"model": "test/thinker"})   # the global picker
        self.send_and_settle(tid)
        self.assertEqual(fakes[P.FAST].changes, [("test/thinker", "high")],
                         "an open default thread must pick up the global choice at its next turn")
        lines = [m["text"] for m in self.request("GET", f"/threads/{tid}/transcript")["messages"]
                 if m["role"] == "system"]
        self.assertEqual(lines, ["model → test/thinker · high (follows the default)"])
        self.send_and_settle(tid)
        self.assertEqual(len(fakes[P.FAST].changes), 1, "no change, no call")

    def test_an_effort_alone_keeps_a_default_thread_on_the_default_model(self):
        """A4 amendment (2026-10-07): an effort-only change on a default
        thread stores the effort and leaves `model` None; the thread follows
        every later global change, with the effort re-clamped to each new
        default and dropped for one with no reasoning control. Only an
        explicit model pins."""
        fakes = self.model_fakes()
        self.request("POST", "/models", {"add": "test/thinker"})
        self.request("POST", "/models", {"add": "test/model"})
        tid = self.chat()["id"]
        saved = self.brief_bytes(tid)
        first = models.tier("orchestrator")       # not in the fixture: its ladder is unknown
        self.send_and_settle(tid)
        self.events_all()
        # An unknown ladder takes any level, as the v1 rule does.
        body = self.request("PATCH", f"/threads/{tid}", {"effort": "max"})
        self.assertEqual((body["model"], body["effort"]), (None, "max"), "the model stays default")
        stored = self.stores.threads.get(tid)
        self.assertEqual((stored.model, stored.effort), (None, "max"))
        self.assertEqual(self.daemon._sessions[tid].thread.model, None)
        updated = [e for e in self.events_all() if e["kind"] == "thread_updated"]
        self.assertEqual((updated[-1]["data"]["model"], updated[-1]["data"]["effective_model"],
                          updated[-1]["data"]["effective_effort"]), (None, first, "max"))
        self.send_and_settle(tid)
        self.assertEqual(fakes[P.FAST].changes, [(first, "max")])
        # The global default moves to a model whose ladder stops at high: the
        # thread follows it, and the stored effort is clamped down to it.
        self.request("POST", "/model", {"model": "test/thinker"})
        self.send_and_settle(tid)
        self.assertEqual(fakes[P.FAST].changes[-1], ("test/thinker", "high"))
        self.assertEqual((self.stores.threads.get(tid).model, self.stores.threads.get(tid).effort),
                         (None, "max"), "the owner's choice is kept, not overwritten by the clamp")
        # An effort chosen on that default is checked against what was offered.
        self.assertIn("does not offer 'max'", self.request(
            "PATCH", f"/threads/{tid}", {"effort": "max"}, status=400)["error"])
        self.request("PATCH", f"/threads/{tid}", {"effort": "low"})
        self.assertEqual(self.stores.threads.get(tid).model, None)
        self.send_and_settle(tid)
        self.assertEqual(fakes[P.FAST].changes[-1], ("test/thinker", "low"))
        # A default with no reasoning control gets no effort at all.
        self.request("POST", "/model", {"model": "test/model"})
        self.send_and_settle(tid)
        self.assertEqual(fakes[P.FAST].changes[-1], ("test/model", None))
        self.assertIn("no reasoning effort", self.request(
            "PATCH", f"/threads/{tid}", {"effort": "low"}, status=400)["error"])
        # Back to a default with a ladder: the stored effort applies again.
        self.request("POST", "/model", {"model": "test/thinker"})
        self.send_and_settle(tid)
        self.assertEqual(fakes[P.FAST].changes[-1], ("test/thinker", "low"))
        lines = [m["text"] for m in self.request("GET", f"/threads/{tid}/transcript")["messages"]
                 if m["role"] == "system"]
        self.assertEqual(lines[0], f"effort → max (default model: {first})")
        self.assertIn("model → test/thinker · high (follows the default)", lines)
        self.assertIn("effort → low (default model: test/thinker)", lines)
        self.assertIn("model → test/model (follows the default)", lines)
        self.assertEqual(lines[-1], "model → test/thinker · low (follows the default)")
        # Only an explicit model pins; it starts on its own default effort.
        body = self.request("PATCH", f"/threads/{tid}", {"model": "test/thinker"})
        self.assertEqual((body["model"], body["effort"]), ("test/thinker", None))
        self.request("POST", "/model", {"model": "test/model"})
        self.send_and_settle(tid)
        self.assertEqual(fakes[P.FAST].changes[-1], ("test/thinker", "high"), "a pin ignores the default")
        # Back to the default clears the effort as well as the pin.
        self.request("PATCH", f"/threads/{tid}", {"effort": "low"})
        body = self.request("PATCH", f"/threads/{tid}", {"model": None})
        self.assertEqual((body["model"], body["effort"]), (None, None))
        self.assertEqual(self.brief_bytes(tid), saved, "brief.json is never rewritten")

    def test_an_effort_without_a_model_opens_a_thread_that_follows_the_default(self):
        fakes = self.model_fakes()
        self.request("POST", "/models", {"add": "test/thinker"})
        self.request("POST", "/model", {"model": "test/thinker"})
        body = self.chat(effort="low")
        self.assertEqual((body["model"], body["effort"]), (None, "low"))
        _, brief = fakes[P.FAST].opened[-1]
        self.assertEqual((brief.model, brief.effort), ("test/thinker", "low"),
                         "the provider starts on the default, at the chosen effort")
        tid = body["id"]
        self.request("POST", "/models", {"add": "test/model"})
        self.request("POST", "/model", {"model": "test/model"})
        self.send_and_settle(tid)
        self.assertEqual(fakes[P.FAST].changes, [("test/model", None)])
        # Claude: an effort alone keeps the thread on Opus, the default.
        body = self.chat("claude", effort="max")
        self.assertEqual((body["model"], body["effort"]), (None, "max"))
        _, brief = fakes[P.CLAUDE].opened[-1]
        self.assertEqual((brief.model, brief.effort), ("claude-opus-5-5", "max"))

    def test_a_pin_survives_a_global_change_and_a_resume(self):
        fakes = self.model_fakes()
        self.request("POST", "/models", {"add": "test/thinker"})
        tid = self.chat()["id"]
        saved = self.brief_bytes(tid)
        self.request("PATCH", f"/threads/{tid}", {"model": "test/thinker", "effort": "medium"})
        self.request("POST", "/models", {"add": "test/model"})
        self.request("POST", "/model", {"model": "test/model"})
        self.request("POST", "/models", {"remove": "test/thinker"})       # A3: stays pinned
        self.daemon.close_thread(tid)
        self.send_and_settle(tid)
        mode, brief = fakes[P.FAST].opened[-1]
        self.assertEqual((mode, brief.model, brief.effort), ("resume", "test/thinker", "medium"))
        self.assertEqual(self.brief_bytes(tid), saved)
        self.assertEqual(fakes[P.FAST].changes, [], "the resume opened on the pin; nothing to change")
        # An effort change on the off-roster pin is still allowed (A3).
        self.request("PATCH", f"/threads/{tid}", {"effort": "low"})

    def test_patch_mid_turn_lands_on_the_next_turn(self):
        fakes = self.model_fakes()
        self.request("POST", "/models", {"add": "test/thinker"})
        tid = self.chat()["id"]
        fakes[P.FAST].hold = threading.Event()
        self.request("POST", f"/threads/{tid}/send", {"text": "long"}, status=202)
        eventually(fakes[P.FAST].entered.is_set)
        self.request("PATCH", f"/threads/{tid}", {"model": "test/thinker"})
        self.assertEqual(fakes[P.FAST].changes, [])
        fakes[P.FAST].hold.set()
        eventually(lambda: self.daemon._sessions[tid].worker is None)
        fakes[P.FAST].hold = None
        self.assertEqual(self.stores.threads.get(tid).model, "test/thinker",
                         "the turn's own saves must not write the old choice back")
        self.send_and_settle(tid)
        self.assertEqual(fakes[P.FAST].changes, [("test/thinker", "high")])

    def test_a_refused_switch_rolls_back_and_the_message_goes_on_the_old_model(self):
        """A provider that cannot move a thread onto its new model must not
        leave the record naming a model it is not running: every later
        message would retry the switch and fail. The owner reads why."""
        fakes = self.model_fakes()
        claude = fakes[P.CLAUDE]
        tid = self.chat("claude")["id"]
        self.send_and_settle(tid)

        def refuse(handle, model, effort):
            claude.changes.append((model, effort))
            raise BriefRefused("claude-sonnet-5-5 is not on this plan")
        claude.set_model = refuse
        self.request("PATCH", f"/threads/{tid}", {"model": "claude-sonnet-5-5", "effort": "low"})
        sent = len(claude.messages)
        self.events_all()
        self.send_and_settle(tid, "still there?")
        self.assertEqual(claude.changes, [("claude-sonnet-5-5", "low")])
        self.assertEqual(len(claude.messages), sent + 1, "the message is sent, on the old model")
        self.assertEqual(claude.messages[-1].text, "still there?")
        stored = self.stores.threads.get(tid)
        self.assertEqual((stored.model, stored.effort), (None, None),
                         "the record is rolled back to what the provider still runs")
        lines = [m["text"] for m in self.request("GET", f"/threads/{tid}/transcript")["messages"]
                 if m["role"] == "system"]
        self.assertEqual(lines[-1], "switch to claude-sonnet-5-5 · low refused: claude-sonnet-5-5 is "
                                    "not on this plan; still on claude-opus-5-5 · high")
        updated = [e for e in self.events_all() if e["kind"] == "thread_updated"]
        self.assertEqual(updated[-1]["data"]["model"], None, "the HUD hears the rollback")
        usage = [r for r in self.stores.threads.read_log(tid) if r.get("kind") == "usage"]
        self.assertEqual(usage[-1]["data"]["model"], "claude-opus-5-5")
        self.send_and_settle(tid, "again")
        self.assertEqual(len(claude.changes), 1, "a refused switch is not retried on every message")
        self.assertEqual(len(claude.messages), sent + 2)

    def test_a_default_that_moves_under_a_provider_that_cannot_switch_pins_the_thread(self):
        fast = Fake(P.FAST)                     # no set_model at all
        self.daemon.providers[P.FAST] = fast
        self.request("POST", "/models", {"add": "test/thinker"})
        tid = self.chat()["id"]
        self.send_and_settle(tid)
        was = models.tier("orchestrator")
        self.request("POST", "/model", {"model": "test/thinker"})   # the global picker moves
        self.send_and_settle(tid, "after the move")
        self.assertEqual(fast.messages[-1].text, "after the move", "the message still goes")
        stored = self.stores.threads.get(tid)
        self.assertEqual(stored.model, was, "pinned to what it runs, so the switch is not retried")
        lines = [m["text"] for m in self.request("GET", f"/threads/{tid}/transcript")["messages"]
                 if m["role"] == "system"]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("cannot change model mid-thread", lines[0])
        self.send_and_settle(tid, "once more")
        self.assertEqual(len([m for m in self.request("GET", f"/threads/{tid}/transcript")["messages"]
                              if m["role"] == "system"]), 1, "no second refusal")

    def test_a_switch_that_loses_the_session_resumes_it_on_the_next_message(self):
        fakes = self.model_fakes()
        claude = fakes[P.CLAUDE]
        tid = self.chat("claude")["id"]
        self.send_and_settle(tid)

        def lose(handle, model, effort):
            raise SessionLost("reconnecting the old model failed too")
        claude.set_model = lose
        self.request("PATCH", f"/threads/{tid}", {"model": "claude-sonnet-5-5"})
        sent = len(claude.messages)
        self.request("POST", f"/threads/{tid}/send", {"text": "lost"}, status=202)
        eventually(lambda: tid not in self.daemon._sessions)
        self.assertEqual(len(claude.messages), sent, "nothing is sent on a closed session")
        log = self.stores.threads.read_log(tid)
        self.assertTrue(any(r.get("kind") == "error" and "send again to resume" in r["data"]["message"]
                            for r in log), "the turn ends with an error that says what to do")
        stored = self.stores.threads.get(tid)
        self.assertEqual((stored.model, stored.effort), (None, None))
        self.request("POST", f"/threads/{tid}/send", {"text": "back"}, status=202)
        eventually(lambda: tid in self.daemon._sessions and self.daemon._sessions[tid].worker is None)
        mode, brief = claude.opened[-1]
        self.assertEqual((mode, brief.model, brief.effort), ("resume", "claude-opus-5-5", "high"),
                         "the next message resumes cleanly, on the old model")
        self.assertEqual(claude.messages[-1].text, "back")

    def test_patch_refuses_task_threads_and_providers_that_cannot_switch(self):
        self.model_fakes()
        task = self.stores.tasks.create(self.project.id, "work")
        thread = self.thread(task_id=task.id)
        self.request("PATCH", f"/threads/{thread.id}", {"model": "test/model"}, status=409)
        self.daemon.providers[P.CODEX] = Fake(P.CODEX)
        codex = self.thread(P.CODEX)
        self.assertIn("cannot change model", self.request(
            "PATCH", f"/threads/{codex.id}", {"model": "gpt-5.5"}, status=409)["error"])

    def test_thread_models_lists_each_providers_own_models(self):
        body = self.request("GET", "/thread-models")["providers"]
        self.assertEqual(set(body), {"fast", "claude", "codex"})
        self.assertEqual(body["claude"]["default"], "claude-opus-5-5")
        self.assertEqual(body["claude"]["default_effort"], "high")
        self.assertEqual(body["fast"]["default"], models.tier("orchestrator"))
        self.assertIn("gpt-5.6-sol", [m["id"] for m in body["codex"]["models"]])


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
