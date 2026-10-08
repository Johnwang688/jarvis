"""B1: the guild setup, project channels and their lifecycle — free, offline.

Every Discord call lands on `FakeDiscord`, a small in-memory server (guild,
roles, the bot's member record, channels); `httpx.request` itself raises, so a
call that escaped the fake would fail the suite rather than reach Discord. The
guild file and the token bundle live in a temp directory, never the owner's
`~/.config/jarvis`.

The checks that matter most, each written to fail against the code before B1:

* **D4.** An owner rename moves the channel at once; a rename that did not
  come from the owner raises an approval and sends **nothing** to Discord
  before the yes — and nothing at all on a deny.
* **D3.** No DELETE, ever: the fake fails the test on one, and the adapter has
  no method that could send it.
* **The Inbox** is linked to #ungrouped by the daemon and cannot be relinked,
  unlinked or given a second channel.
* **Setup** creates nothing before a "y", reports missing and excess
  permissions (Administrator loudly), and writes the guild file mode 600.
"""
from __future__ import annotations

from copy import deepcopy
import io
import json
import os
from pathlib import Path
import queue
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from jarvis import config
from jarvis.v2.approvals import PendingApprovals
from jarvis.v2.bus import EventBus
from jarvis.v2.discord import guild as guildmod
from jarvis.v2.discord import linker as linker_mod
from jarvis.v2.discord import perms
from jarvis.v2.discord import setup as setup_mod
from jarvis.v2.discord.gateway import DiscordRouter
from jarvis.v2.discord.linker import ChannelLinker, LinkError, slug, topic
from jarvis.v2.discord.reporter import Reporter, read_sidecar
from jarvis.v2.discord.rest import DiscordRest
from jarvis.v2.model import TaskState
from jarvis.v2.provider import Decision
from jarvis.v2.router import Router
from jarvis.v2.stores import Stores

TOKEN = "synthetic-discord-secret-never-in-content"
APP = "700000000000000001"
BOT = "700000000000000002"
OWNER = "700000000000000003"
GUILD = "800000000000000001"
OTHER_GUILD = "800000000000000099"
CATEGORY = "810000000000000001"
ARCHIVE = "810000000000000002"
UNGROUPED = "810000000000000003"
BOT_ROLE = "820000000000000001"


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        time.sleep(0.005)


class FakeDiscord:
    """An in-memory Discord: just enough server for setup and the linker."""

    def __init__(self, *, role_permissions=perms.REQUIRED):
        self.calls = []
        self.next_id = 900000000000000000
        self.fail = {}          # (method, path prefix) -> (status, body) or Exception
        self.guilds = [{"id": GUILD, "name": "Jarvis HQ"}]
        self.guild = {"id": GUILD, "owner_id": OWNER,
                      "roles": [{"id": GUILD, "permissions": "0"},
                                {"id": BOT_ROLE, "permissions": str(role_permissions)}]}
        self.member = {"user": {"id": BOT}, "roles": [BOT_ROLE]}
        self.channels: dict[str, dict] = {}

    def add(self, channel_id, name, *, kind=0, parent=None, guild=GUILD, overwrites=()):
        self.channels[channel_id] = {"id": channel_id, "name": name, "type": kind,
                                     "guild_id": guild, "parent_id": parent,
                                     "permission_overwrites": list(overwrites)}
        return channel_id

    def configured(self):
        self.add(CATEGORY, "Jarvis", kind=4)
        self.add(ARCHIVE, "Jarvis Archive", kind=4)
        self.add(UNGROUPED, "ungrouped", parent=CATEGORY)

    def _new_id(self):
        self.next_id += 1
        return str(self.next_id)

    def __call__(self, method, url, headers, timeout, **kwargs):
        assert headers == {"Authorization": f"Bot {TOKEN}"}
        assert TOKEN not in repr(kwargs)
        path = url.removeprefix(config.DISCORD_API)
        call = {"method": method, "path": path, "timeout": timeout, **deepcopy(kwargs)}
        self.calls.append(call)
        # D3: nothing may ever send one. Fail loudly, not quietly.
        assert method != "DELETE", f"a DELETE was sent to {path}"
        for (m, prefix), outcome in list(self.fail.items()):
            if m == method and path.startswith(prefix):
                if isinstance(outcome, Exception):
                    raise outcome
                status, body = outcome
                return httpx.Response(status, json=body)
        body = kwargs.get("json")
        if method == "GET" and path == "/users/@me":
            return httpx.Response(200, json={"id": BOT, "username": "jarvis"})
        if method == "GET" and path == "/users/@me/guilds":
            return httpx.Response(200, json=self.guilds)
        if method == "GET" and path == "/oauth2/applications/@me":
            return httpx.Response(200, json={"id": APP})
        if method == "GET" and path == f"/guilds/{GUILD}":
            return httpx.Response(200, json=self.guild)
        if method == "GET" and path == f"/guilds/{GUILD}/members/{BOT}":
            return httpx.Response(200, json=self.member)
        if method == "GET" and path == f"/guilds/{GUILD}/channels":
            return httpx.Response(200, json=[dict(c) for c in self.channels.values()
                                             if c["guild_id"] == GUILD])
        if method == "POST" and path == f"/guilds/{GUILD}/channels":
            new = self.add(self._new_id(), body["name"], kind=body.get("type", 0),
                           parent=body.get("parent_id"))
            if "topic" in body:
                self.channels[new]["topic"] = body["topic"]
            return httpx.Response(201, json=self.channels[new])
        if path.startswith("/channels/"):
            rest = path.removeprefix("/channels/").split("/")
            channel = self.channels.get(rest[0])
            if len(rest) == 1 and method == "GET":
                if channel is None:
                    return httpx.Response(404, json={"message": "Unknown Channel", "code": 10003})
                return httpx.Response(200, json=channel)
            if len(rest) == 1 and method == "PATCH":
                if channel is None:
                    return httpx.Response(404, json={"message": "Unknown Channel", "code": 10003})
                channel.update({k: v for k, v in body.items()})
                return httpx.Response(200, json=channel)
            if rest[1:] == ["messages"] and method == "POST":
                return httpx.Response(200, json={"id": self._new_id()})
            if rest[1:] == ["threads"] and method == "POST":
                return httpx.Response(201, json={"id": self._new_id()})
            if rest[1:2] == ["thread-members"] and method == "PUT":
                return httpx.Response(204)
        return httpx.Response(404, json={"message": "not in the fake", "code": 0})

    def named(self, method, path=None):
        return [c for c in self.calls if c["method"] == method
                and (path is None or c["path"] == path)]

    def posts(self, channel):
        return [c["json"]["content"] for c in self.calls if c["method"] == "POST"
                and c["path"] == f"/channels/{channel}/messages" and "json" in c]


class Harness(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        token = self.root / "discord_token.json"
        token.write_text(json.dumps({"bot_token": TOKEN, "owner_id": OWNER}))
        self.guild_path = self.root / "config" / "discord_guild.json"
        for name, value in (("DISCORD_TOKEN_PATH", token),
                            ("DISCORD_GUILD_PATH", self.guild_path),
                            ("ALLOWLIST_PATH", self.root / "config" / "allowlist.json")):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        network = patch.object(httpx, "request", side_effect=AssertionError("live network"))
        network.start()
        self.addCleanup(network.stop)
        pace = patch.object(linker_mod, "CREATE_INTERVAL", 0.0)
        pace.start()
        self.addCleanup(pace.stop)
        self.fake = FakeDiscord()
        self.rest = DiscordRest(self.fake)
        self.addCleanup(self.rest.close)
        self.stores = Stores(self.root / "state")
        self.approvals = PendingApprovals(timeout_s=3.0)
        self.bus = EventBus()
        self.daemon = SimpleNamespace(stores=self.stores, bus=self.bus, approvals=self.approvals,
                                      _lock=threading.RLock())
        self.events = self.bus.subscribe()
        (self.root / "school").mkdir()
        (self.root / "calc").mkdir()
        self.school = self.stores.projects.create("School", str(self.root / "school"))
        self.calc = self.stores.projects.create("e2e-calc", str(self.root / "calc"))
        self.linker = None

    def configure(self):
        self.fake.configured()
        guildmod.write(guildmod.GuildConfig(GUILD, CATEGORY, ARCHIVE, UNGROUPED))

    def make(self, *, start=False):
        self.linker = ChannelLinker(self.daemon, self.rest, approvals=self.approvals)
        self.addCleanup(self.linker.close)
        if start:
            self.linker.start()
        return self.linker

    @staticmethod
    def serve(linker):
        """Start the workers without the start-up crowding pass, so a test's
        own `check_crowding()` is the only one (no race with the worker)."""
        linker._crowding_due = False
        return linker.start()

    def published(self, kind=None):
        out = []
        while True:
            try:
                record = self.events.get_nowait()
            except queue.Empty:
                return [r for r in out if kind is None or r.get("kind") == kind]
            out.append(record)

    def patches(self, channel):
        return [c["json"] for c in self.fake.named("PATCH", f"/channels/{channel}")]


# ---------------------------------------------------------------------------


class SlugAndPermissionTables(unittest.TestCase):
    def test_slug_table(self):
        table = [
            ("School", "abcd1234", (), "school"),
            ("e2e-calc", "abcd1234", (), "e2e-calc"),
            ("e2e-calc (1)", "abcd1234", (), "e2e-calc-1"),
            ("  Robotics  Club!! ", "abcd1234", (), "robotics-club"),
            ("Café Über", "abcd1234", (), "cafe-uber"),
            ("日本語", "abcd1234", (), "project-abcd1234"),
            ("!!!", "abcd1234", (), "project-abcd1234"),
            ("", "abcd1234", (), "project-abcd1234"),
            ("School", "abcd1234", ("school",), "school-abcd"),
            ("a" * 200, "abcd1234", (), "a" * 90),
            ("a" * 200, "abcd1234", ("a" * 90,), "a" * 85 + "-abcd"),
            ("x-" * 60, "abcd1234", (), ("x-" * 45).strip("-")),
        ]
        for name, pid, taken, expected in table:
            with self.subTest(name=name, taken=taken):
                got = slug(name, pid, taken)
                self.assertEqual(got, expected)
                self.assertLessEqual(len(got), 90)
                self.assertRegex(got, r"^[a-z0-9-]+$")
                self.assertFalse(got.startswith("-") or got.endswith("-"))

    def test_topic(self):
        project = SimpleNamespace(name="School", id="abcd1234")
        self.assertEqual(topic(project), "Jarvis project · School · abcd1234")

    def test_required_integer_and_names(self):
        self.assertEqual(perms.REQUIRED, 309237763088)
        self.assertEqual(perms.missing(perms.REQUIRED), [])
        self.assertEqual(perms.missing(0), [
            "Manage Channels", "View Channel", "Send Messages", "Embed Links", "Attach Files",
            "Read Message History", "Create Public Threads", "Send Messages in Threads"])
        self.assertEqual(perms.excess(perms.REQUIRED), [])
        self.assertEqual(perms.excess(perms.ADMINISTRATOR | perms.KICK_MEMBERS),
                         ["Administrator", "Kick Members"])
        self.assertEqual(perms.excess(str(perms.MANAGE_ROLES | perms.MANAGE_GUILD
                                          | perms.MANAGE_WEBHOOKS | perms.BAN_MEMBERS)),
                         ["Manage Roles", "Manage Server", "Manage Webhooks", "Ban Members"])
        self.assertEqual(perms.bits("309237763088"), perms.REQUIRED)
        self.assertEqual(perms.bits("garbage"), 0)

    def test_invite_url(self):
        self.assertEqual(
            perms.invite_url(APP, GUILD),
            f"https://discord.com/oauth2/authorize?client_id={APP}"
            "&scope=bot+applications.commands&permissions=309237763088"
            f"&guild_id={GUILD}&disable_guild_select=true")
        from jarvis.tools import discord as v1
        self.assertIn("permissions=309237763088", v1.invite_url(APP))

    def test_effective_follows_discords_algorithm(self):
        g = "100"
        guild = {"id": g, "owner_id": "1"}
        roles = [{"id": g, "permissions": str(perms.VIEW_CHANNEL)},
                 {"id": "r1", "permissions": str(perms.SEND_MESSAGES | perms.EMBED_LINKS)},
                 {"id": "r2", "permissions": str(perms.ATTACH_FILES)},
                 {"id": "admin", "permissions": str(perms.ADMINISTRATOR)}]
        member = {"user": {"id": "bot"}, "roles": ["r1", "r2"]}
        V, S, E, A = (perms.VIEW_CHANNEL, perms.SEND_MESSAGES, perms.EMBED_LINKS,
                      perms.ATTACH_FILES)

        def ch(*overwrites):
            return {"permission_overwrites": [
                {"id": i, "type": t, "allow": str(a), "deny": str(d)} for i, t, a, d in overwrites]}

        table = [
            ("base: @everyone OR roles", None, V | S | E | A),
            ("no overwrites", ch(), V | S | E | A),
            ("@everyone deny view hides everything", ch((g, 0, 0, V)), 0),
            ("a role allow beats the @everyone deny", ch((g, 0, 0, V), ("r2", 0, V, 0)),
             V | S | E | A),
            ("role denies before role allows: allow wins", ch(("r1", 0, 0, S), ("r2", 0, S, 0)),
             V | S | E | A),
            ("the member overwrite is last", ch(("r1", 0, S, 0), ("bot", 1, 0, S)), V),
            ("no Send Messages loses Embed and Attach", ch(("r1", 0, 0, S)), V),
            ("an allow adds a permission", ch(("bot", 1, perms.MANAGE_CHANNELS, 0)),
             V | S | E | A | perms.MANAGE_CHANNELS),
        ]
        for label, channel, expected in table:
            with self.subTest(label):
                self.assertEqual(perms.effective(guild, roles, member, channel), expected)
        admin = {"user": {"id": "bot"}, "roles": ["admin"]}
        self.assertEqual(perms.effective(guild, roles, admin, ch((g, 0, 0, V))), perms.ALL,
                         "Administrator ignores every channel overwrite")
        owner = {"user": {"id": "1"}, "roles": []}
        self.assertEqual(perms.effective(guild, roles, owner), perms.ALL)
        self.assertEqual(perms.granted(guild, roles, admin), V | perms.ADMINISTRATOR)


# ---------------------------------------------------------------------------


class LinkerChecks(Harness):
    def test_unconfigured_is_a_state_not_an_error_and_writes_nothing(self):
        linker = self.make()
        self.assertEqual(linker.view(self.school.id)["state"], "unconfigured")
        with self.assertRaises(LinkError) as caught:
            linker.create(self.school.id)
        self.assertIn("jarvis auth discord-guild", str(caught.exception))
        self.assertFalse(linker.ensure_inbox())
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(linker.status()["guild"], {"configured": False, "id": None})

    def test_create_names_places_and_records_the_channel(self):
        self.configure()
        linker = self.make()
        view = linker.create(self.school.id)
        post = self.fake.named("POST", f"/guilds/{GUILD}/channels")[-1]["json"]
        self.assertEqual(post, {"name": "school", "type": 0, "parent_id": CATEGORY,
                                "topic": f"Jarvis project · School · {self.school.id}"})
        self.assertNotIn("permission_overwrites", post)
        saved = self.stores.projects.get(self.school.id)
        self.assertEqual(saved.discord_channel_origin, "created")
        self.assertEqual(view["state"], "linked_ok")
        self.assertEqual(view["origin"], "created")
        self.assertEqual(view["category"], "Jarvis")
        self.assertEqual(view["channel_id"], saved.discord_channel_id)
        event = self.published("project_updated")[-1]
        self.assertEqual(event["data"]["changed"], ["discord_channel_id"])
        self.assertEqual(event["data"]["by"], "owner")
        # A second project with a clashing slug is told apart by its id.
        clash = self.stores.projects.create("school", str(self.root / "calc"))
        linker.create(clash.id)
        self.assertEqual(self.fake.named("POST", f"/guilds/{GUILD}/channels")[-1]["json"]["name"],
                         f"school-{clash.id[:4]}")
        with self.assertRaises(LinkError) as caught:
            linker.create(self.school.id)
        self.assertEqual(caught.exception.status, 409)

    def test_link_validation_table(self):
        self.configure()
        linker = self.make()
        good = self.fake.add("830000000000000001", "school-notes", parent=CATEGORY)
        self.fake.add("830000000000000002", "voice", kind=2, parent=CATEGORY)
        self.fake.add("830000000000000003", "elsewhere", guild=OTHER_GUILD)
        self.fake.add("830000000000000004", "locked", parent=CATEGORY, overwrites=[
            {"id": BOT_ROLE, "type": 0, "allow": "0", "deny": str(perms.SEND_MESSAGES_IN_THREADS)}])
        self.fake.add("830000000000000005", "taken", parent=CATEGORY)
        self.calc.discord_channel_id = "830000000000000005"
        self.stores.projects.save(self.calc)
        self.fake.fail[("GET", "/channels/830000000000000006")] = (
            403, {"message": "Missing Access", "code": 50001})
        archived = self.stores.projects.create("Old", str(self.root / "school"),
                                               archived="2026-01-01T00:00:00+00:00")
        gone = self.stores.projects.create("Gone", str(self.root / "nowhere"))
        inbox = self.stores.projects.inbox()
        table = [
            (self.school, "school", 400, "digits"),
            (self.school, "123", 400, "digits"),
            (self.school, "839999999999999999", 400, "no channel"),
            (self.school, "830000000000000006", 400, "cannot see"),
            (self.school, "830000000000000003", 400, "another server"),
            (self.school, "830000000000000002", 400, "not a text channel"),
            (self.school, "830000000000000005", 409, "e2e-calc"),
            (self.school, "830000000000000004", 400, "Send Messages in Threads"),
            (self.school, UNGROUPED, 400, "Inbox"),
            (self.school, CATEGORY, 400, "category"),
            (inbox, good, 409, "ungrouped"),
            (archived, good, 409, "archived"),
            (gone, good, 409, "does not exist"),
        ]
        for project, channel, status, words in table:
            with self.subTest(project=project.name, channel=channel):
                with self.assertRaises(LinkError) as caught:
                    linker.link(project.id, channel)
                self.assertEqual(caught.exception.status, status, str(caught.exception))
                self.assertIn(words, str(caught.exception))
        self.assertIsNone(self.stores.projects.get(self.school.id).discord_channel_id)
        view = linker.link(self.school.id, good)
        self.assertEqual((view["state"], view["origin"], view["name"]),
                         ("linked_ok", "linked", "school-notes"))
        # Linking never renames or moves the owner's channel.
        self.assertEqual(self.patches(good), [])

    def test_unlink_keeps_the_channel(self):
        self.configure()
        linker = self.make()
        channel = linker.create(self.school.id)["channel_id"]
        view = linker.unlink(self.school.id)
        self.assertEqual(view["state"], "unlinked")
        project = self.stores.projects.get(self.school.id)
        self.assertIsNone(project.discord_channel_id)
        self.assertIsNone(project.discord_channel_origin)
        self.assertIn(channel, self.fake.channels)
        self.assertEqual(self.patches(channel), [])
        event = self.published("project_updated")[-1]
        self.assertEqual(event["data"]["previous"], {"discord_channel_id": channel})

    def test_view_states(self):
        self.configure()
        linker = self.make()
        self.assertEqual(linker.view(self.school.id)["state"], "unlinked")
        cases = [
            ("830000000000000010", lambda c: None, "linked_ok"),
            ("830000000000000011", lambda c: self.fake.channels.pop(c), "not_found"),
            ("830000000000000012", lambda c: self.fake.fail.__setitem__(
                ("GET", f"/channels/{c}"), (403, {"code": 50001, "message": "x"})), "no_access"),
            ("830000000000000013", lambda c: self.fake.channels[c].update(guild_id=OTHER_GUILD),
             "wrong_guild"),
            ("830000000000000014", lambda c: self.fake.channels[c].update(permission_overwrites=[
                {"id": BOT, "type": 1, "allow": "0", "deny": str(perms.ATTACH_FILES)}]),
             "missing_permissions"),
            ("830000000000000015", lambda c: self.fake.fail.__setitem__(
                ("GET", f"/channels/{c}"), httpx.ConnectError("down")), "unreachable"),
            ("830000000000000016", lambda c: self.fake.fail.__setitem__(
                ("GET", f"/channels/{c}"), (429, {"retry_after": 30.0, "message": "slow"})),
             "unreachable"),
        ]
        for channel, breakit, state in cases:
            with self.subTest(state):
                self.fake.add(channel, "c", parent=CATEGORY)
                project = self.stores.projects.get(self.school.id)
                project.discord_channel_id, project.discord_channel_origin = channel, "linked"
                self.stores.projects.save(project)
                breakit(channel)
                view = linker.view(self.school.id, refresh=True)
                self.assertEqual(view["state"], state)
                if state == "missing_permissions":
                    self.assertEqual(view["missing"], ["Attach Files"])
        # Every read was held to 5 s, and a 429 was not slept out.
        reads = [c for c in self.fake.calls if c["method"] == "GET"]
        self.assertTrue(reads and all(c["timeout"] == 5.0 for c in reads))
        # Cached for 60 s: a second read sends nothing.
        before = len(self.fake.calls)
        linker.view(self.school.id)
        self.assertEqual(len(self.fake.calls), before)
        # The folder going missing is its own state.
        (self.root / "school").rmdir()
        self.assertEqual(linker.view(self.school.id, refresh=True)["state"], "folder_missing")
        with self.assertRaises(LinkError) as caught:
            linker.link(self.school.id, "830000000000000010")
        self.assertIn("does not exist", str(caught.exception))

    def test_backfill_creates_one_a_second_for_live_projects_with_folders(self):
        self.configure()
        linker = self.make()
        archived = self.stores.projects.create("Old", str(self.root / "school"),
                                               archived="2026-01-01T00:00:00+00:00")
        missing = self.stores.projects.create("Gone", str(self.root / "nowhere"))
        self.stores.projects.inbox()
        result = linker.backfill()
        self.assertEqual(result["created"], 2)
        self.assertEqual({r["name"] for r in result["results"]}, {"School", "e2e-calc"})
        self.assertEqual(result["skipped"], [{"project_id": missing.id, "name": "Gone",
                                              "reason": "folder missing"}])
        self.assertEqual(len(self.fake.named("POST", f"/guilds/{GUILD}/channels")), 2)
        self.assertIsNone(self.stores.projects.get(archived.id).discord_channel_id)
        self.assertIsNone(self.stores.projects.inbox().discord_channel_id,
                          "backfill never touches the Inbox: the daemon links it")
        # A second backfill has nothing left to do.
        self.assertEqual(linker.backfill()["created"], 0)

    def test_backfill_is_paced(self):
        self.configure()
        linker = self.make()
        stamps = []
        original = self.rest.create_channel

        def stamped(*a, **k):
            stamps.append(time.monotonic())
            return original(*a, **k)

        self.rest.create_channel = stamped
        for n in range(2):
            (self.root / f"p{n}").mkdir()
            self.stores.projects.create(f"p{n}", str(self.root / f"p{n}"))
        with patch.object(linker_mod, "CREATE_INTERVAL", 0.1):
            linker.backfill()
        self.assertEqual(len(stamps), 4)
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertTrue(all(g >= 0.09 for g in gaps), gaps)

    def test_inbox_is_linked_to_ungrouped_and_cannot_be_relinked(self):
        self.configure()
        linker = self.make()
        self.assertTrue(linker.ensure_inbox())
        inbox = self.stores.projects.inbox()
        self.assertEqual((inbox.discord_channel_id, inbox.discord_channel_origin),
                         (UNGROUPED, "created"))
        self.assertFalse(linker.ensure_inbox(), "linking it twice is a no-op")
        self.fake.add("830000000000000020", "spare", parent=CATEGORY)
        for attempt in (lambda: linker.link(inbox.id, "830000000000000020"),
                        lambda: linker.unlink(inbox.id),
                        lambda: linker.create(inbox.id)):
            with self.assertRaises(LinkError) as caught:
                attempt()
            self.assertIn("ungrouped", str(caught.exception))
        self.assertEqual(self.stores.projects.inbox().discord_channel_id, UNGROUPED)
        with self.assertRaises(LinkError):
            linker.link(self.school.id, UNGROUPED)
        # Nothing was created, renamed or moved for the Inbox.
        self.assertEqual(self.fake.named("POST", f"/guilds/{GUILD}/channels"), [])
        self.assertEqual(self.patches(UNGROUPED), [])

    def test_the_worker_links_the_inbox_once_the_guild_file_appears(self):
        """No restart: setup writes the file while the daemon runs."""
        with patch.object(linker_mod, "TICK_S", 0.02):
            linker = self.make(start=True)
            time.sleep(0.1)
            self.assertIsNone(self.stores.projects.list(inbox=True)[0].discord_channel_id
                              if self.stores.projects.list(inbox=True) else None)
            self.configure()
            wait_for(lambda: self.stores.projects.list(inbox=True)
                     and self.stores.projects.list(inbox=True)[0].discord_channel_id == UNGROUPED)
            wait_for(lambda: linker.status()["permissions"] is not None)
            self.assertEqual(linker.status()["permissions"]["missing"], [])
            self.assertFalse(linker.status()["permissions"]["administrator"])


class LifecycleChecks(Harness):
    def linked(self):
        self.configure()
        linker = self.make()
        channel = linker.create(self.school.id)["channel_id"]
        self.published()
        return linker, channel

    def rename(self, name, by):
        project = self.stores.projects.get(self.school.id)
        before = project.name
        project.name = name
        self.stores.projects.save(project)
        return {"kind": "project_updated", "project_id": project.id,
                "data": {"project_id": project.id, "changed": ["name"], "by": by,
                         "project": {}, "previous": {"name": before}}}

    def test_an_owner_rename_moves_the_channel_at_once(self):
        linker, channel = self.linked()
        linker._handle(self.rename("School 2027", "owner"))
        self.assertEqual(self.patches(channel), [{"name": "school-2027"}])
        self.assertEqual(self.approvals.pending(), [])

    def test_an_api_rename_asks_first_and_sends_nothing_before_yes(self):
        linker, channel = self.linked()
        self.serve(linker)
        linker._handle(self.rename("Homework", "api"))
        wait_for(lambda: self.approvals.pending())
        request = self.approvals.pending()[0]
        self.assertEqual(request.tool, "discord_channel")
        self.assertEqual(request.origin, "Jarvis housekeeping")
        self.assertFalse(request.allowlistable, "never Always")
        self.assertEqual(request.args, {"action": "rename", "channel": channel,
                                        "from": "school", "to": "homework",
                                        "why": "project Homework was renamed outside the HUD"})
        self.assertEqual(linker.status()["linker"]["awaiting_approval"], 1)
        time.sleep(0.05)
        self.assertEqual(self.patches(channel), [], "nothing before the yes")
        self.approvals.resolve(request.req_id, Decision.ALLOW)
        wait_for(lambda: self.patches(channel))
        self.assertEqual(self.patches(channel), [{"name": "homework"}])
        wait_for(lambda: linker.status()["linker"]["awaiting_approval"] == 0)

    def test_a_denied_or_unanswered_rename_does_nothing(self):
        linker, channel = self.linked()
        self.serve(linker)
        linker._handle(self.rename("Homework", "api"))
        wait_for(lambda: self.approvals.pending())
        self.approvals.resolve(self.approvals.pending()[0].req_id, Decision.DENY)
        wait_for(lambda: linker.status()["linker"]["awaiting_approval"] == 0)
        time.sleep(0.05)
        self.assertEqual(self.patches(channel), [])
        # Timeout: the broker denies, and nothing is sent.
        short = PendingApprovals(timeout_s=0.05)
        linker.approvals = short
        decision = linker.ask_now({"action": "rename", "project_id": self.school.id,
                                   "channel": channel, "to": "homework",
                                   "args": {"action": "rename"}})
        self.assertEqual(decision, Decision.DENY)
        self.assertEqual(self.patches(channel), [])

    def test_a_rate_limited_rename_is_kept_pending_and_retried(self):
        linker, channel = self.linked()
        self.fake.fail[("PATCH", f"/channels/{channel}")] = (
            429, {"retry_after": 300.0, "message": "slow down"})
        linker._handle(self.rename("Homework", "owner"))
        status = linker.status()["linker"]
        self.assertEqual((status["state"], status["pending_renames"]), ("degraded", 1))
        self.assertTrue(linker.view(self.school.id, refresh=True)["rename_pending"])
        del self.fake.fail[("PATCH", f"/channels/{channel}")]
        linker._retry_renames()                  # not due yet
        self.assertEqual(linker.status()["linker"]["pending_renames"], 1)
        linker._wall = lambda: time.time() + 301
        linker._retry_renames()
        self.assertEqual(linker.status()["linker"]["pending_renames"], 0)
        self.assertEqual(self.fake.channels[channel]["name"], "homework")

    def test_archive_restore_and_delete(self):
        linker, channel = self.linked()
        linker._handle({"kind": "project_archived", "project_id": self.school.id,
                        "data": {"project_id": self.school.id, "name": "School"}})
        moved = self.patches(channel)[-1]
        self.assertEqual(moved, {"parent_id": ARCHIVE})
        for forbidden in ("lock_permissions", "permission_overwrites"):
            self.assertNotIn(forbidden, moved)
        self.assertIn("Jarvis Archive", self.fake.posts(channel)[-1])
        self.assertIn("kept", self.fake.posts(channel)[-1])
        # Restored under a renumbered name: moved back and renamed, one PATCH.
        project = self.stores.projects.get(self.school.id)
        project.name = "School (1)"
        self.stores.projects.save(project)
        linker._handle({"kind": "project_restored", "project_id": self.school.id,
                        "data": {"project_id": self.school.id, "name": "School (1)"}})
        self.assertEqual(self.patches(channel)[-1], {"parent_id": CATEGORY, "name": "school-1"})
        self.assertEqual(self.fake.posts(channel)[-1], "Restored.")
        # Restored with the same name: moved back only.
        linker._handle({"kind": "project_restored", "project_id": self.school.id,
                        "data": {"project_id": self.school.id}})
        self.assertEqual(self.patches(channel)[-1], {"parent_id": CATEGORY})
        # Deleted: a note, and the channel stays.
        linker._handle({"kind": "project_deleted", "project_id": self.school.id,
                        "data": {"project_id": self.school.id, "name": "School (1)",
                                 "discord_channel_id": channel}})
        self.assertIn("kept", self.fake.posts(channel)[-1])
        self.assertIn(channel, self.fake.channels)

    def test_no_delete_anywhere(self):
        self.assertFalse([n for n in dir(DiscordRest) if "delete" in n.lower()])
        root = Path(__file__).resolve().parents[2] / "jarvis" / "v2" / "discord"
        for path in sorted(root.glob("*.py")):
            self.assertNotIn('"DELETE"', path.read_text(encoding="utf-8"), path.name)
        # And over a whole lifecycle, the fake (which asserts on one) saw none.
        linker, channel = self.linked()
        linker.unlink(self.school.id)
        linker.link(self.school.id, channel)
        linker._handle({"kind": "project_deleted", "project_id": self.school.id,
                        "data": {"name": "School", "discord_channel_id": channel}})
        self.assertFalse([c for c in self.fake.calls if c["method"] == "DELETE"])

    def test_moving_a_channel_never_touches_its_permissions(self):
        linker, channel = self.linked()
        with self.assertRaises(TypeError):
            self.rest.modify_channel(channel, lock_permissions=True)
        with self.assertRaises(TypeError):
            self.rest.modify_channel(channel, permission_overwrites=[])


class CrowdingChecks(Harness):
    def crowd(self, n, parent=CATEGORY, first=840000000000000000):
        for i in range(n):
            self.fake.add(str(first + i), f"filler-{i}", parent=parent)

    def idle(self, project, days=40):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                              time.gmtime(time.time() - days * 86400))
        project.created = stamp
        self.stores.projects.save(project)

    def test_45_channels_asks_once_to_archive_the_idle_ones(self):
        self.configure()
        linker = self.make()
        school = linker.create(self.school.id)["channel_id"]
        calc = linker.create(self.calc.id)["channel_id"]
        self.idle(self.stores.projects.get(self.school.id))
        # 3 Jarvis channels (ungrouped, school, calc) + 41 = 44: not crowded yet.
        self.crowd(41)
        self.assertEqual(linker.check_crowding(), [])
        self.crowd(1, first=850000000000000000)
        self.serve(linker)
        self.assertEqual(linker.check_crowding(), ["crowded"])
        self.assertEqual(linker.check_crowding(), [], "never asked twice at once")
        wait_for(lambda: self.approvals.pending())
        request = self.approvals.pending()[0]
        self.assertEqual(request.tool, "discord_channel")
        self.assertFalse(request.allowlistable)
        self.assertEqual(request.args["action"], "archive")
        self.assertEqual(request.args["channel"], "#school", "only the idle project")
        self.assertIn("Move these 1 to Jarvis Archive?", request.args["why"])
        self.assertIn("45 of 50", request.args["why"])
        self.assertEqual(self.patches(school), [])
        self.approvals.resolve(request.req_id, Decision.ALLOW)
        wait_for(lambda: self.patches(school))
        self.assertEqual(self.patches(school), [{"parent_id": ARCHIVE}])
        self.assertEqual(self.patches(calc), [], "a project with recent work stays")
        self.assertIsNotNone(self.stores.projects.get(self.school.id).discord_channel_id,
                             "the project stays linked and active")

    def test_a_full_archive_asks_for_jarvis_archive_2(self):
        self.configure()
        linker = self.make()
        self.crowd(45, parent=ARCHIVE)
        self.serve(linker)
        self.assertEqual(linker.check_crowding(), ["archive_full"])
        wait_for(lambda: self.approvals.pending())
        request = self.approvals.pending()[0]
        self.assertEqual(request.args["action"], "create_category")
        self.assertEqual(request.args["to"], "Jarvis Archive 2")
        before = len(self.fake.named("POST", f"/guilds/{GUILD}/channels"))
        time.sleep(0.05)
        self.assertEqual(len(self.fake.named("POST", f"/guilds/{GUILD}/channels")), before,
                         "no category before the yes")
        self.approvals.resolve(request.req_id, Decision.ALLOW)
        wait_for(lambda: len(self.fake.named("POST", f"/guilds/{GUILD}/channels")) > before)
        made = self.fake.named("POST", f"/guilds/{GUILD}/channels")[-1]["json"]
        self.assertEqual(made, {"name": "Jarvis Archive 2", "type": 4})
        overflow = self.fake.next_id
        wait_for(lambda: linker.archive_target(linker.config()) == str(overflow))
        # The next archive goes to the new category, and it persists.
        channel = linker.create(self.school.id)["channel_id"]
        linker._handle({"kind": "project_archived", "project_id": self.school.id, "data": {}})
        self.assertEqual(self.patches(channel)[-1], {"parent_id": str(overflow)})
        again = ChannelLinker(self.daemon, self.rest, approvals=self.approvals)
        self.assertEqual(again.archive_target(again.config()), str(overflow))

    def test_a_denied_crowding_ask_moves_nothing(self):
        self.configure()
        linker = self.make()
        school = linker.create(self.school.id)["channel_id"]
        self.idle(self.stores.projects.get(self.school.id))
        self.crowd(44)
        self.serve(linker)
        self.assertEqual(linker.check_crowding(), ["crowded"])
        wait_for(lambda: self.approvals.pending())
        self.approvals.resolve(self.approvals.pending()[0].req_id, Decision.DENY)
        wait_for(lambda: linker.status()["linker"]["awaiting_approval"] == 0)
        self.assertEqual(self.patches(school), [])
        self.assertEqual(linker.check_crowding(), [], "not re-asked for a day")


class GatewayAndReporterChecks(Harness):
    def test_a_configured_guild_places_only_its_own_messages(self):
        self.configure()
        router = DiscordRouter.__new__(DiscordRouter)
        router.stores = self.stores
        router._guild_pinned = False
        project = self.stores.projects.get(self.school.id)
        project.discord_channel_id = "830000000000000030"
        self.stores.projects.save(project)
        here = {"guild_id": GUILD}
        self.assertEqual(router._locate(here, "830000000000000030")[0], "project")
        self.assertEqual(router._locate({"guild_id": OTHER_GUILD}, "830000000000000030")[0],
                         "other", "another server is never a Jarvis place")
        self.assertEqual(router._locate({}, "dm")[0], "dm")
        self.assertEqual(router.guild_id, GUILD)
        self.guild_path.unlink()
        self.assertIsNone(router.guild_id)
        self.assertEqual(router._locate({"guild_id": OTHER_GUILD}, "830000000000000030")[0],
                         "project", "unconfigured: as before B1")

    def test_linking_a_project_gives_its_blocked_task_a_thread(self):
        """The safety net steps aside: the blocked task, which had only the
        DM, gets its thread and card in the new channel when it is linked."""
        self.configure()
        task = self.stores.tasks.create(self.calc.id, "compute the thing")
        self.stores.tasks.save(task)
        for state in (TaskState.CLARIFYING, TaskState.BLOCKED):
            self.stores.tasks.transition(task.id, state)
        task = self.stores.tasks.get(task.id)
        reporter = Reporter(self.stores, self.rest, self.bus, dm_channel=lambda: "dm",
                            reconcile=False, started_at="2000-01-01T00:00:00+00:00")
        self.addCleanup(reporter.close)
        # Before the link: the blocked milestone went to the DM.
        reporter._deliver(task, self.stores.projects.get(self.calc.id),
                          read_sidecar(self.stores, task.id))
        threads = [c for c in self.fake.calls if c["path"].endswith("/threads")]
        self.assertEqual(threads, [])
        linker = self.make()
        channel = linker.create(self.calc.id)["channel_id"]
        wait_for(lambda: [c for c in self.fake.calls
                          if c["path"] == f"/channels/{channel}/threads"])
        wait_for(lambda: self.stores.tasks.get(task.id).discord_thread_id)
        sidecar = read_sidecar(self.stores, task.id)
        wait_for(lambda: read_sidecar(self.stores, task.id).get("discord_status_message_id"))
        self.assertEqual(self.stores.tasks.get(task.id).discord_thread_id,
                         read_sidecar(self.stores, task.id)["discord_thread_id"])
        self.assertFalse(sidecar.get("thread_gone"))

    def test_a_gone_thread_is_replaced_when_the_project_is_relinked(self):
        self.configure()
        task = self.stores.tasks.create(self.calc.id, "compute the thing",
                                        discord_thread_id="839999999999999990")
        self.stores.tasks.save(task)
        self.stores.tasks.transition(task.id, TaskState.CLARIFYING)
        sidecar_path = self.stores.tasks.path(task.id).with_name("discord.json")
        sidecar_path.write_text(json.dumps({"discord_thread_id": "839999999999999990",
                                            "thread_gone": True, "phase": "clarifying"}))
        reporter = Reporter(self.stores, self.rest, self.bus, dm_channel=lambda: "dm",
                            reconcile=False)
        self.addCleanup(reporter.close)
        linker = self.make()
        channel = linker.create(self.calc.id)["channel_id"]
        wait_for(lambda: [c for c in self.fake.calls
                          if c["path"] == f"/channels/{channel}/threads"])
        wait_for(lambda: self.stores.tasks.get(task.id).discord_thread_id != "839999999999999990")
        sidecar = read_sidecar(self.stores, task.id)
        self.assertNotIn("thread_gone", sidecar)
        self.assertEqual(sidecar["retired_threads"], ["839999999999999990"])


class SetupChecks(Harness):
    def run_setup(self, answers):
        out = io.StringIO()
        answers = list(answers)
        asked = []

        def ask(prompt):
            asked.append(prompt)
            return answers.pop(0) if answers else ""

        code = setup_mod.run(self.rest, ask=ask, out=lambda text: out.write(str(text) + "\n"))
        return code, out.getvalue(), asked

    def creates(self):
        return [c["json"] for c in self.fake.named("POST", f"/guilds/{GUILD}/channels")]

    def test_happy_path_creates_only_after_yes_and_writes_mode_600(self):
        code, text, asked = self.run_setup(["1", "n"])
        self.assertEqual(code, 1)
        self.assertEqual(self.creates(), [], "nothing before a yes")
        self.assertFalse(self.guild_path.exists())
        self.assertIn(f"guild_id={GUILD}&disable_guild_select=true", text)
        self.assertIn("permissions=309237763088", text)
        self.assertIn("scope=bot+applications.commands", text)
        code, text, asked = self.run_setup(["1", "y"])
        self.assertEqual(code, 0, text)
        saved = json.loads(self.guild_path.read_text())
        self.assertEqual(self.creates(), [
            {"name": "Jarvis", "type": 4},
            {"name": "Jarvis Archive", "type": 4},
            {"name": "ungrouped", "type": 0, "parent_id": saved["category_id"],
             "topic": setup_mod.UNGROUPED_TOPIC}])
        self.assertEqual(set(saved), {"guild_id", "category_id", "archive_category_id",
                                      "ungrouped_channel_id"})
        self.assertEqual(saved["guild_id"], GUILD)
        self.assertEqual(stat.S_IMODE(os.stat(self.guild_path).st_mode), 0o600)
        self.assertNotIn(TOKEN, text)
        # A second run reuses what exists: nothing new is created.
        n = len(self.creates())
        code, text, _ = self.run_setup(["1", "y"])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.creates()), n)
        self.assertIn("use existing category Jarvis", text)
        self.assertEqual(json.loads(self.guild_path.read_text()), saved)

    def test_missing_permissions_block_and_excess_warn(self):
        self.fake.guild["roles"][1]["permissions"] = str(
            perms.REQUIRED & ~perms.MANAGE_CHANNELS & ~perms.ATTACH_FILES | perms.KICK_MEMBERS)
        code, text, asked = self.run_setup(["1", "q"])
        self.assertEqual(code, 1)
        self.assertIn("MISSING (blocking): Manage Channels, Attach Files", text)
        self.assertIn("Kick Members", text)
        self.assertNotIn("ADMINISTRATOR", text)
        self.assertEqual(self.creates(), [])
        self.assertFalse(self.guild_path.exists())
        # Fixed while it waits: Enter checks again and goes on.
        out = io.StringIO()

        def ask(prompt):
            if prompt.startswith("Press Enter"):
                self.fake.guild["roles"][1]["permissions"] = str(perms.REQUIRED)
                return ""
            return "y" if prompt.startswith("Go ahead") else "1"

        code = setup_mod.run(self.rest, ask=ask, out=lambda t: out.write(str(t) + "\n"))
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("everything required is on", out.getvalue())

    def test_administrator_gets_the_loud_warning(self):
        self.fake.guild["roles"][1]["permissions"] = str(perms.ADMINISTRATOR)
        code, text, _ = self.run_setup(["1", "n"])
        self.assertIn("WARNING: the bot has ADMINISTRATOR", text)
        self.assertIn("full server takeover", text)
        self.assertIn("Remove Administrator", text)
        self.assertNotIn("MISSING", text, "Administrator grants everything required")

    def test_no_bundle_and_no_guilds(self):
        config.DISCORD_TOKEN_PATH.unlink()
        code, text, _ = self.run_setup([])
        self.assertEqual(code, 1)
        self.assertIn("jarvis auth discord", text)
        config.DISCORD_TOKEN_PATH.write_text(json.dumps({"bot_token": TOKEN, "owner_id": OWNER}))
        self.fake.guilds = []
        code, text, _ = self.run_setup([])
        self.assertEqual(code, 1)
        self.assertIn("in no server", text)
        self.assertEqual(self.fake.named("POST"), [])

    def test_the_cli_offers_it(self):
        from jarvis import __main__ as cli
        with patch.object(setup_mod, "run", return_value=0) as ran:
            self.assertEqual(cli.cmd_auth(SimpleNamespace(service="discord-guild",
                                                          client_json=None, redo=False)), 0)
        ran.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
