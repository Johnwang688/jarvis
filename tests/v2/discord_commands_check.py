"""Free S1 checks: slash commands, autocomplete, buttons, modals, registration.

No network: `httpx.request` raises "live network" for the whole run, and every
REST call lands in a fake transport that also plays Discord's interaction
callback and webhook endpoints. Nothing registers against a real application.

What is under test is what decides who can make Jarvis act, so each case is
written to fail if a rule is relaxed: the owner gate on *every* interaction
(autocomplete included), our application id, contexts 0/1 only, places
enforced on the server, the deferral sent before any store write or control
call, an approval answerable only where it was asked (and a button only from
the very message it is on), `/always` only where a standing rule can exist,
and no token — bot or interaction — in any body, log line or exception.

`config.ALLOWLIST_PATH`, `config.SKILLS_DIR` and the token bundle all point at
a temp directory: a suite must not read or write the owner's machine state.
"""
from __future__ import annotations

from copy import deepcopy
import itertools
import json
import logging
from pathlib import Path
import re
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis import config  # noqa: E402
from jarvis import discord_gateway as v1gw  # noqa: E402
from jarvis.v2 import commands as registry  # noqa: E402
from jarvis.v2 import hud_api  # noqa: E402
from jarvis.v2.approvals import ApprovalRequest, PendingApprovals  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.discord import commands as dcommands  # noqa: E402
from jarvis.v2.discord.gateway import DiscordRouter, _V2Listener  # noqa: E402
from jarvis.v2.discord.interactions import InteractionReply  # noqa: E402
from jarvis.v2.discord.rest import DiscordError, DiscordRest  # noqa: E402
from jarvis.v2.model import OpenQuestion, ProviderName, TaskState, utcnow  # noqa: E402
from jarvis.v2.provider import Decision  # noqa: E402
from jarvis.v2.router import Router  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402

from discord_routing_check import FakeControl, FakeFast, wait_for  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
TOKEN = "synthetic-bot-secret-never-in-content"
ITOKEN = "synthetic-interaction-token-"
APP = "424242"
BOT = "botid"
OWNER = "1001"
STRANGER = "2002"
GUILD = "777"
PROJECT_CHANNEL = "5001"
TASK_THREAD = "5002"
OTHER_THREAD = "5003"
PLAIN_CHANNEL = "5004"
DM_CHANNEL = "5005"
CALLBACK = re.compile(r"^/interactions/(\d+)/([^/]+)/callback$")
WEBHOOK = re.compile(r"^/webhooks/(\d+)/([^/]+)(/messages/@original)?$")

_ids = itertools.count(900000)


class FakeTransport:
    """Discord, as far as S1 can tell. The bot token may appear only in the
    header; an interaction token only in a URL path."""

    def __init__(self):
        self.calls = []
        self.remote_commands = []
        self.fail = {}              # (method, path regex) -> (status, json or path -> json)
        self.lock = threading.Lock()
        self.violations = []

    def __call__(self, method, url, headers, timeout, **kwargs):
        # An assertion raised in here would be swallowed by DiscordRest (it
        # reports transport failures by class), so violations are collected
        # and every test asserts the list is empty when it ends.
        if headers != {"Authorization": f"Bot {TOKEN}"}:
            self.violations.append("unexpected headers")
        if TOKEN in repr(kwargs):
            self.violations.append("the bot token reached a request body")
        if ITOKEN in repr(kwargs):
            self.violations.append("an interaction token reached a request body")
        path = url.removeprefix(config.DISCORD_API)
        with self.lock:
            self.calls.append({"method": method, "path": path, **deepcopy(kwargs)})
        for (fail_method, pattern), (status, body) in self.fail.items():
            if fail_method == method and re.search(pattern, path):
                return httpx.Response(status, json=body(path) if callable(body) else body)
        if path.startswith("/applications/") and method == "GET":
            return httpx.Response(200, json=deepcopy(self.remote_commands))
        if path.startswith("/applications/") and method == "PUT":
            self.remote_commands = deepcopy(kwargs["json"])
            return httpx.Response(200, json=self.remote_commands)
        if path == "/oauth2/applications/@me":
            return httpx.Response(200, json={"id": APP})
        if CALLBACK.match(path):
            return httpx.Response(204)
        return httpx.Response(200, json={"id": str(next(_ids))})

    # -- views -------------------------------------------------------------

    def callbacks(self):
        return [c["json"] for c in self.calls if CALLBACK.match(c["path"])]

    def webhook(self):
        return [c for c in self.calls if WEBHOOK.match(c["path"])]

    def channel_posts(self, channel=None):
        out = []
        for call in self.calls:
            if call["method"] == "POST" and call["path"].endswith("/messages") \
                    and call["path"].startswith("/channels/"):
                where = call["path"].split("/")[2]
                if channel is None or where == channel:
                    out.append(_body(call))
        return out

    def puts(self):
        return [c for c in self.calls if c["method"] == "PUT"]


def _body(call):
    return call["json"] if "json" in call else json.loads(call["data"]["payload_json"])


class FakeListener:
    def __init__(self, handler):
        self.handler = handler
        self.bot_id, self.owner_id, self.application_id = BOT, OWNER, APP
        self.on_interaction = self.on_ready = None

    def start(self):
        pass

    def stop(self):
        pass

    def feed(self, message):
        self.handler(message, self.bot_id, self.owner_id)

    def interact(self, payload):
        self.on_interaction(payload)

    def ready(self):
        self.on_ready(self)


def interaction(kind, data=None, *, channel=DM_CHANNEL, guild=None, user=OWNER,
                context="auto", app=APP, message=None):
    n = next(_ids)
    body = {"id": str(n), "token": f"{ITOKEN}{n}", "type": kind, "application_id": app,
            "channel_id": channel, "data": data or {}}
    if context == "auto":
        context = 0 if guild else 1
    if context is not None:
        body["context"] = context
    if guild:
        body["guild_id"] = guild
        body["member"] = {"user": {"id": user}}
    else:
        body["user"] = {"id": user}
    if message is not None:
        body["message"] = message
    return body


def slash(name, options=None, sub=None, **kwargs):
    opts = [{"name": k, "type": 4 if isinstance(v, int) and not isinstance(v, bool) else
             5 if isinstance(v, bool) else 3, "value": v} for k, v in (options or {}).items()]
    if sub:
        opts = [{"type": 1, "name": sub, "options": opts}]
    return interaction(2, {"name": name, "type": 1, "options": opts}, **kwargs)


def complete(name, focused, value="", others=None, **kwargs):
    opts = [{"name": k, "type": 3, "value": v} for k, v in (others or {}).items()]
    opts.append({"name": focused, "type": 3, "value": value, "focused": True})
    return interaction(4, {"name": name, "type": 1, "options": opts}, **kwargs)


def button(custom_id, message_id, **kwargs):
    return interaction(3, {"custom_id": custom_id, "component_type": 2},
                       message={"id": str(message_id)}, **kwargs)


def modal_submit(custom_id, brief, **kwargs):
    return interaction(5, {"custom_id": custom_id, "components": [
        {"type": 1, "components": [{"type": 4, "custom_id": "brief", "value": brief}]}]},
        **kwargs)


def in_guild(channel):
    return {"channel": channel, "guild": GUILD}


class Harness(unittest.TestCase):
    sync_commands = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        token_path = root / "discord_token.json"
        token_path.write_text(json.dumps({"bot_token": TOKEN, "owner_id": OWNER}))
        skills = root / "skills"
        for name, meta, body in (
                ("morning-briefing", "", "Brief the owner on the day."),
                ("whiteboard", "jarvis-only: true\n", "Open the board."),
                ("huge", "", "x" * (registry.SKILL_BODY_MAX + 1))):
            (skills / name).mkdir(parents=True)
            (skills / name / "SKILL.md").write_text(
                f"---\nname: {name}\n{meta}description: Use for {name}\n---\n{body}\n")
        for name, value in (("DISCORD_TOKEN_PATH", token_path),
                            # Never the owner's real guild file (B1).
                            ("DISCORD_GUILD_PATH", root / "discord_guild.json"),
                            ("ALLOWLIST_PATH", root / "allowlist.json"),
                            ("SKILLS_DIR", skills)):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        network = patch.object(httpx, "request", side_effect=AssertionError("live network"))
        network.start()
        self.addCleanup(network.stop)

        self.now = [1000.0]
        self.stores = Stores(root / "state")
        self.transport = FakeTransport()
        self.addCleanup(lambda: self.assertEqual(self.transport.violations, []))
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
            dm_channel=lambda: DM_CHANNEL, turn_timeout_s=5,
            sync_commands=self.sync_commands, clock=lambda: self.now[0])
        self.daemon.discord = self.surface
        self.addCleanup(self.surface.stop)
        self.listener = self.surface.listener
        self.surface.start()
        self.listener.ready()

        self.project = self.stores.projects.create(
            "Jarvis", str(root), discord_channel_id=PROJECT_CHANNEL)
        self.task = self.stores.tasks.create(self.project.id, "migrate the skills folder",
                                             discord_thread_id=TASK_THREAD)
        self.stores.tasks.save(self.task)

    # -- helpers -----------------------------------------------------------

    def run_slash(self, *args, **kwargs):
        self.listener.interact(slash(*args, **kwargs))

    def last_callback(self):
        return self.transport.callbacks()[-1]

    def refused(self, text=None):
        callback = self.last_callback()
        self.assertEqual(callback["type"], 4, callback)
        self.assertEqual(callback["data"]["flags"], 64)
        if text:
            self.assertIn(text, callback["data"]["content"])
        return callback["data"]["content"]

    def edits(self):
        return [_body(c) for c in self.transport.webhook() if c["path"].endswith("@original")]

    def said(self):
        """Everything the interaction path said, in order (edits + followups)."""
        return [(_body(c).get("content") or "") for c in self.transport.webhook()]

    def ask(self, tool="Bash", command="pnpm run deploy --prod", task_id=None):
        result = {}
        request = ApprovalRequest(tool=tool, args={"command": command},
                                  command=command, task_id=task_id, origin="task test")
        worker = threading.Thread(
            target=lambda: result.update(decision=self.approvals.ask(request)), daemon=True)
        worker.start()
        wait_for(lambda: request.req_id in self.surface._approval_messages)
        return request, result, worker

    def clarifying(self, task=None):
        task = task or self.task
        self.stores.tasks.transition(task.id, TaskState.CLARIFYING)
        task = self.stores.tasks.get(task.id)
        task.spec.questions = [OpenQuestion("Which runner?", False, assumed="pytest"),
                               OpenQuestion("Which package manager?", True, ["pnpm", "npm"]),
                               OpenQuestion("Which node?", True, ["20", "22"])]
        self.stores.tasks.save(task)
        return task

    def archive(self, project):
        for task in self.stores.tasks.list():
            if task.project_id == project.id and task.state == TaskState.INTAKE:
                self.stores.tasks.transition(task.id, TaskState.CANCELLED)
        project = self.stores.projects.get(project.id)
        project.archived = utcnow()
        self.stores.projects.save(project)


# -- registration ----------------------------------------------------------------


class RegistrationChecks(unittest.TestCase):
    def test_the_payload_passes_every_discord_limit_and_is_global(self):
        payload = dcommands.to_discord()
        dcommands.validate(payload)
        self.assertEqual(sorted(c["name"] for c in payload),
                         ["always", "answer", "cancel", "no", "project", "resume", "skill",
                          "status", "steer", "task", "yes"])
        for command in payload:
            self.assertEqual(command["contexts"], [0, 1], command["name"])
            self.assertEqual(command["integration_types"], [0])
            self.assertEqual(command["default_member_permissions"], "0")
        project = next(c for c in payload if c["name"] == "project")
        self.assertEqual([o["name"] for o in project["options"]], ["list"])   # no S1 stubs

    def test_validate_catches_each_limit(self):
        def broken(change):
            payload = dcommands.to_discord()
            change(payload)
            with self.assertRaises(ValueError):
                dcommands.validate(payload)

        broken(lambda p: p[0].update(description="x" * 101))
        broken(lambda p: p[0].update(name="Has Spaces"))
        broken(lambda p: p[0].update(contexts=[0, 1, 2]))
        broken(lambda p: p[0].update(integration_types=[0, 1]))
        broken(lambda p: p[0].update(default_member_permissions="8"))
        broken(lambda p: p[0].update(options=[{"type": 3, "name": f"o{i}",
                                               "description": "d"} for i in range(26)]))
        broken(lambda p: p[0].update(options=[{"type": 3, "name": "a", "description": "d"},
                                              {"type": 3, "name": "b", "description": "d",
                                               "required": True}]))
        broken(lambda p: p[0].update(options=[{"type": 3, "name": f"o{i}",
                                               "description": "d" * 100,
                                               "choices": [{"name": "c" * 100, "value": "v"}]}
                                              for i in range(20)]))
        broken(lambda p: p.extend(deepcopy(p[0]) for _ in range(100)))

    def test_the_registry_is_static_whatever_is_on_disk(self):
        before = json.dumps(dcommands.to_discord())
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "agent-wrote-this" / "SKILL.md"
            skill.parent.mkdir()
            skill.write_text("---\nname: agent-wrote-this\ndescription: x\n---\nbody\n")
            stores = Stores(Path(tmp) / "state")
            stores.projects.create("NewProject", tmp)
            with patch.object(config, "SKILLS_DIR", Path(tmp)):
                self.assertIn("agent-wrote-this", dict(registry.invocable_skills()))
                after = json.dumps(dcommands.to_discord())
        self.assertEqual(before, after)
        self.assertNotIn("agent-wrote-this", after)
        self.assertNotIn("NewProject", after)

    def test_the_hud_never_gets_yes_no_or_always(self):
        names = {c["name"] for c in registry.describe("hud")}
        self.assertFalse(names & {"yes", "no", "always"})
        self.assertTrue({"task", "status", "cancel", "steer", "skill"} <= names)

    def test_applications_api_is_named_in_one_module_only(self):
        allowed = REPO / "jarvis" / "v2" / "discord" / "commands.py"
        # v1's human-only setup reads the application object (its owner id).
        # It cannot register anything, and it is the only other spelling.
        exceptions = {(REPO / "jarvis" / "tools" / "discord.py", "/oauth2/applications/@me")}
        offenders = []
        for path in (REPO / "jarvis").rglob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if "/applications/" not in line or path == allowed:
                    continue
                if any(path == p and needle in line for p, needle in exceptions):
                    continue
                offenders.append(f"{path.relative_to(REPO)}: {line.strip()}")
        self.assertEqual(offenders, [])
        self.assertIn("/applications/", allowed.read_text())

    def test_nothing_in_the_discord_surface_can_send_a_delete(self):
        for path in (REPO / "jarvis" / "v2" / "discord").glob("*.py"):
            self.assertNotIn('"DELETE"', path.read_text(encoding="utf-8"), path.name)


class SyncChecks(Harness):
    sync_commands = True

    def test_first_ready_syncs_once_with_one_bulk_put_and_never_deletes(self):
        # setUp's READY found an empty remote list: one PUT with the full set.
        self.assertEqual(len(self.transport.puts()), 1)
        self.assertEqual(self.transport.puts()[0]["json"], dcommands.to_discord())
        self.assertEqual(self.surface.commands.state, "ok")
        self.assertEqual(self.surface.commands.count, 11)
        before = len(self.transport.calls)
        self.listener.ready()                                   # a reconnect's READY
        self.assertEqual(len(self.transport.calls), before)
        self.assertFalse(any(c["method"] == "DELETE" for c in self.transport.calls))

    def test_equal_lists_send_no_put_and_any_drift_sends_exactly_one(self):
        remote = dcommands.to_discord()
        for i, command in enumerate(remote):                    # what Discord adds
            command.update(id=str(i), version="1", application_id=APP,
                           name_localizations=None, description_localizations=None,
                           dm_permission=True, nsfw=False)
        self.transport.remote_commands = remote
        result = dcommands.sync(self.rest, APP)
        self.assertEqual((result.state, result.put, result.changed), ("ok", False, 0))
        self.assertEqual(len(self.transport.puts()), 1)          # only setUp's

        drifted = deepcopy(remote)
        drifted[3]["description"] = "an old description"
        self.transport.remote_commands = drifted
        result = dcommands.sync(self.rest, APP)
        self.assertEqual((result.state, result.put, result.changed), ("ok", True, 1))
        self.assertEqual(len(self.transport.puts()), 2)
        self.assertEqual(self.transport.puts()[-1]["json"], dcommands.to_discord())

    def test_a_failed_put_is_a_state_not_a_retry_loop(self):
        self.transport.remote_commands = []
        self.transport.fail[("PUT", r"^/applications/")] = (500, {"message": "boom " + TOKEN})
        result = dcommands.sync(self.rest, APP)
        self.assertEqual(result.state, "failed")
        self.assertEqual(len(self.transport.puts()), 2)          # setUp's + exactly one
        self.assertNotIn(TOKEN, result.error)
        self.assertNotIn(TOKEN, json.dumps(result.to_json()))

    def test_get_discord_reports_the_sync_without_any_token(self):
        status = hud_api.discord_status(self.daemon)
        self.assertEqual(status["connected"], True)
        self.assertEqual(status["commands"]["state"], "ok")
        self.assertEqual(status["commands"]["count"], 11)
        self.assertEqual(set(status["commands"]), {"state", "count", "synced_at", "error"})
        self.assertNotIn(TOKEN, json.dumps(status))
        self.assertNotIn(ITOKEN, json.dumps(status))
        self.daemon.discord = None
        self.assertEqual(hud_api.discord_status(self.daemon)["commands"]["state"], "pending")

    def test_the_cli_checks_without_writing_and_syncs_on_request(self):
        self.transport.remote_commands = []
        said = []
        self.assertEqual(dcommands.main("check", rest=self.rest, out=said.append), 1)
        self.assertEqual(len(self.transport.puts()), 1)          # check wrote nothing
        self.assertIn("scope=bot+applications.commands", "\n".join(said))
        self.assertIn("Interactions Endpoint URL", "\n".join(said))
        self.assertEqual(dcommands.main("sync", rest=self.rest, out=said.append), 0)
        self.assertEqual(len(self.transport.puts()), 2)
        self.assertEqual(dcommands.main("check", rest=self.rest, out=said.append), 0)
        self.assertIn("up to date (11)", said[-1])
        self.assertNotIn(TOKEN, "\n".join(said))


class ListenerChecks(unittest.TestCase):
    def test_ready_records_the_application_and_interactions_reach_a_worker(self):
        seen, ready = [], []
        listener = _V2Listener(lambda *_: None, announce=lambda text: None,
                               on_interaction=seen.append, on_ready=ready.append)
        listener._token = TOKEN
        frames = [{"op": 10, "d": {"heartbeat_interval": 60000}},
                  {"t": "READY", "d": {"user": {"id": BOT, "username": "jarvis"},
                                       "application": {"id": APP}}},
                  {"t": "INTERACTION_CREATE", "d": {"id": "1", "type": 2}}]

        class FakeWS:
            def send(self, payload):
                pass

            def settimeout(self, value):
                pass

            def close(self):
                pass

        fake = types.ModuleType("websocket")
        fake.WebSocketTimeoutException = TimeoutError

        def recv(_ws):
            if frames:
                return frames.pop(0)
            listener._stop.set()
            raise TimeoutError

        with patch.dict(sys.modules, {"websocket": fake}), \
             patch.object(v1gw, "_recv_json", recv):
            listener._session(FakeWS())
        wait_for(lambda: seen and ready)
        self.assertEqual(listener.application_id, APP)
        self.assertEqual(seen[0]["id"], "1")


# -- the gate ----------------------------------------------------------------------


class GateChecks(Harness):
    def test_a_stranger_is_refused_everywhere_and_nothing_is_read_or_written(self):
        tasks_before = {t.id for t in self.stores.tasks.list()}
        for where in ({}, in_guild(TASK_THREAD), in_guild(PROJECT_CHANNEL)):
            self.listener.interact(slash("task", {"brief": "do a thing"}, user=STRANGER, **where))
            self.refused("Only the owner")
            self.listener.interact(slash("status", user=STRANGER, **where))
            self.refused("Only the owner")
            self.listener.interact(complete("task", "project", "Jar", user=STRANGER, **where))
            self.assertEqual(self.last_callback(), {"type": 8, "data": {"choices": []}})
        request, result, worker = self.ask(task_id=self.task.id)
        message_id = self.surface._approval_messages[request.req_id][1]
        self.listener.interact(button(f"jv:a:{request.code}", message_id, user=STRANGER,
                                      **in_guild(TASK_THREAD)))
        self.refused("Only the owner")
        self.assertTrue(self.approvals.pending())
        self.assertEqual(self.control.calls, [])
        self.assertEqual({t.id for t in self.stores.tasks.list()}, tasks_before)
        self.assertEqual(self.transport.webhook(), [])          # nothing deferred, nothing said
        self.approvals.shutdown()
        worker.join(2)

    def test_another_application_is_dropped_without_an_answer(self):
        before = len(self.transport.calls)
        self.listener.interact(slash("status", app="999"))
        self.listener.interact(complete("task", "project", app="999"))
        self.assertEqual(len(self.transport.calls), before)
        self.assertEqual(self.control.calls, [])

    def test_only_guild_and_bot_dm_contexts_and_only_the_configured_guild(self):
        self.listener.interact(slash("status", context=2))
        self.refused("only work in your DM")
        self.listener.interact(slash("status", context=None))
        self.refused("only work in your DM")
        self.listener.interact(slash("status", context=0))              # guild context, no guild
        self.refused("only work in your DM")
        self.surface.guild_id = "888"
        self.listener.interact(slash("status", **in_guild(PROJECT_CHANNEL)))
        self.refused("only work in your DM")
        self.surface.guild_id = GUILD
        self.listener.interact(slash("status", **in_guild(PROJECT_CHANNEL)))
        self.assertEqual(self.last_callback()["type"], 5)
        self.assertEqual(self.control.calls, [])

    def test_a_channel_jarvis_does_not_own_takes_no_command(self):
        for name, options in (("status", {}), ("task", {"brief": "x"}), ("yes", {}),
                              ("project", {})):
            sub = "list" if name == "project" else None
            self.listener.interact(slash(name, options, sub=sub, **in_guild(PLAIN_CHANNEL)))
            self.refused("isn't a Jarvis place")
        self.listener.interact(complete("task", "project", **in_guild(PLAIN_CHANNEL)))
        self.assertEqual(self.last_callback()["data"]["choices"], [])
        self.assertEqual(self.stores.tasks.list(), [self.stores.tasks.get(self.task.id)])

    def test_an_archived_projects_channel_and_threads_refuse_every_command(self):
        self.archive(self.project)
        before = {t.id for t in self.stores.tasks.list()}
        for channel in (PROJECT_CHANNEL, TASK_THREAD):
            self.listener.interact(slash("task", {"brief": "x"}, **in_guild(channel)))
            self.refused("archived")
            self.listener.interact(slash("steer", {"text": "x"}, **in_guild(channel)))
            self.refused("archived")
        self.assertEqual({t.id for t in self.stores.tasks.list()}, before)
        self.assertEqual(self.control.calls, [])

    def test_unknown_commands_options_and_types_are_refused(self):
        self.listener.interact(slash("frobnicate"))
        self.refused("out of date")
        self.listener.interact(slash("project", sub="delete"))
        self.refused("out of date")
        self.listener.interact(slash("steer", {"text": "x", "colour": "red"},
                                     **in_guild(TASK_THREAD)))
        self.refused("out of date")
        self.listener.interact(slash("steer", {}, **in_guild(TASK_THREAD)))
        self.refused("needs `text`")
        self.listener.interact(slash("steer", {"text": 5}, **in_guild(TASK_THREAD)))
        self.refused("wrong type")
        self.listener.interact(slash("resume", {"provider": "fast"}, **in_guild(TASK_THREAD)))
        self.refused("must be one of")
        self.assertEqual(self.control.calls, [])

    def test_places_are_enforced_on_the_server(self):
        self.listener.interact(slash("steer", {"text": "go faster"}))           # a DM
        self.refused("Which task?")
        self.listener.interact(slash("cancel", **in_guild(PROJECT_CHANNEL)))
        self.refused("Which task?")
        self.listener.interact(slash("answer", {"text": "pnpm"}, **in_guild(TASK_THREAD)))
        self.refused("no open question")
        self.assertEqual(self.control.calls, [])

    def test_a_malformed_id_still_gets_exactly_one_answer(self):
        """`Store.path` raises on anything but 8 hex characters; that must be an
        "I don't know" reply, never a silent "application did not respond"."""
        cases = ((slash("status", {"task": "zzzz"}), "don't know a task"),
                 (slash("cancel", {"task": "foo"}), "don't know a task"),
                 (slash("steer", {"text": "x", "task": "ABCDEF12"}), "don't know a task"),
                 (complete("answer", "text", others={"task": "zz"}), None),
                 (complete("answer", "question", others={"task": "1234567g"}), None),
                 (modal_submit("jv:task:zzzzzzzz::", "a brief"), "archived or gone"))
        for payload, text in cases:
            with self.subTest(payload=payload["data"]):
                before = len(self.transport.callbacks())
                self.listener.interact(payload)
                self.assertEqual(len(self.transport.callbacks()), before + 1)
                if text is None:
                    self.assertEqual(self.last_callback(), {"type": 8, "data": {"choices": []}})
                else:
                    self.refused(text)
        self.assertEqual(self.control.calls, [])

    def test_a_button_without_context_derives_it_and_a_command_never_does(self):
        request, result, worker = self.ask(task_id=self.task.id)
        message_id = self.surface._approval_messages[request.req_id][1]
        # Guild: derived from guild_id; the press then fails on its own merits.
        self.listener.interact(button("jv:a:zzzz", message_id, context=None,
                                      **in_guild(TASK_THREAD)))
        self.refused("does not match")
        # A channel Discord types as a DM.
        dm_press = button("jv:a:zzzz", "1", context=None)
        dm_press["channel"] = {"id": DM_CHANNEL, "type": 1}
        self.listener.interact(dm_press)
        self.refused("already answered")
        # A group DM (type 3) is never derived into anything.
        group = button("jv:a:zzzz", "1", context=None, channel="9999")
        group["channel"] = {"id": "9999", "type": 3}
        self.listener.interact(group)
        self.refused("only work in your DM")
        # A command with no context still fails closed.
        self.listener.interact(slash("status", context=None, **in_guild(TASK_THREAD)))
        self.refused("only work in your DM")
        self.assertTrue(self.approvals.pending())
        self.approvals.shutdown()
        worker.join(2)

    def test_autocomplete_bypasses_are_rechecked(self):
        self.listener.interact(slash("skill", {"name": "nope"}))
        self.refused("don't have a skill")
        self.listener.interact(slash("skill", {"name": "whiteboard"}))
        self.refused("only runs in the v1 loop")
        self.listener.interact(slash("task", {"brief": "x", "skill": "whiteboard"}))
        self.refused("only runs in the v1 loop")
        self.listener.interact(slash("task", {"brief": "x", "project": "Nowhere"}))
        self.refused("don't know a live project")
        other = self.stores.projects.create("Old", self.tmp.name)
        self.archive(other)
        self.listener.interact(slash("task", {"brief": "x", "project": other.id}))
        self.refused("don't know a live project")
        self.listener.interact(slash("cancel", {"task": "deadbeef"}))
        self.refused("don't know a task")
        self.listener.interact(slash("steer", {"text": "x", "task": "../../etc"}))
        self.refused("don't know a task")
        self.assertEqual(self.control.calls, [])
        self.assertEqual(len(self.stores.tasks.list()), 1)


# -- timing ------------------------------------------------------------------------


class TimingChecks(Harness):
    def deferred_already(self):
        return any(c.get("type") == 5 for c in self.transport.callbacks())

    def test_the_deferral_goes_out_before_any_control_call_or_store_write(self):
        """Every action command: the type 5 is on the wire before the first
        control call, store write, broker resolution or chat-thread open."""
        order = []

        def watch(owner, name):
            original = getattr(owner, name)

            def wrapped(*args, **kwargs):
                order.append((name, self.deferred_already()))
                return original(*args, **kwargs)
            setattr(owner, name, wrapped)

        self.clarifying()                                       # for /answer
        blocked = self.stores.tasks.create(self.project.id, "stuck",
                                           discord_thread_id=OTHER_THREAD)
        self.stores.tasks.save(blocked)
        self.stores.tasks.transition(blocked.id, TaskState.CLARIFYING)
        self.stores.tasks.transition(blocked.id, TaskState.BLOCKED)
        request, result, worker = self.ask(task_id=self.task.id)

        for name in ("steer", "cancel", "start", "resume", "answer_question"):
            watch(self.control, name)
        watch(self.stores.tasks, "create")
        watch(self.approvals, "resolve")
        watch(self.daemon, "open_thread")

        cases = (("steer", slash("steer", {"text": "x"}, **in_guild(TASK_THREAD))),
                 ("resolve", slash("yes", **in_guild(TASK_THREAD))),
                 ("answer_question", slash("answer", {"text": "pnpm"}, **in_guild(TASK_THREAD))),
                 ("resume", slash("resume", **in_guild(OTHER_THREAD))),
                 ("open_thread", slash("skill", {"name": "morning-briefing"})),
                 ("steer", slash("skill", {"name": "morning-briefing"}, **in_guild(TASK_THREAD))),
                 ("cancel", slash("cancel", **in_guild(TASK_THREAD))),
                 ("create", slash("task", {"brief": "write the tests"})))
        for first, payload in cases:
            with self.subTest(first=first):
                self.transport.calls.clear()
                start = len(order)
                self.listener.interact(payload)
                seen = order[start:]
                self.assertTrue(seen and seen[0][0] == first, seen)
                self.assertTrue(all(ok for _, ok in seen), seen)
        worker.join(2)
        self.assertEqual(result["decision"], Decision.ALLOW)

    def test_refusals_are_private_and_actions_are_public(self):
        self.listener.interact(slash("steer", {"text": "x"}))
        self.refused()
        self.listener.interact(slash("steer", {"text": "x"}, **in_guild(TASK_THREAD)))
        self.assertEqual(self.transport.callbacks()[-1], {"type": 5})     # public deferral
        self.assertIn("Noted", self.edits()[-1]["content"])
        self.listener.interact(slash("status", **in_guild(TASK_THREAD)))
        self.assertEqual(self.transport.callbacks()[-1], {"type": 5, "data": {"flags": 64}})
        self.assertIn(self.task.id, self.edits()[-1]["embeds"][0]["title"])

    def test_autocomplete_answers_at_once_with_at_most_25_choices(self):
        for i in range(30):
            self.stores.projects.create(f"project {i:02}", self.tmp.name)
        self.listener.interact(complete("task", "project", "proj"))
        callback = self.last_callback()
        self.assertEqual(callback["type"], 8)
        self.assertEqual(len(callback["data"]["choices"]), 25)
        self.assertEqual(self.transport.webhook(), [])

    def test_past_the_token_window_the_reply_becomes_a_channel_post(self):
        def slow(task_id, text, *, spoken=False):
            self.now[0] += 15 * 60
        self.control.steer = slow
        self.listener.interact(slash("steer", {"text": "x"}, **in_guild(TASK_THREAD)))
        self.assertEqual(self.edits(), [])
        self.assertIn("Noted", self.transport.channel_posts(TASK_THREAD)[-1]["content"])

    def test_a_dead_token_falls_back_to_a_channel_post(self):
        self.transport.fail[("PATCH", r"@original$")] = (404, {"message": "Unknown Webhook"})
        self.listener.interact(slash("steer", {"text": "x"}, **in_guild(TASK_THREAD)))
        self.assertIn("Noted", self.transport.channel_posts(TASK_THREAD)[-1]["content"])

    def test_a_private_reply_never_falls_back_to_a_public_post(self):
        # /project list, with a token that is dead by the time it answers.
        self.transport.fail[("PATCH", r"@original$")] = (404, {"message": "Unknown Webhook"})
        self.listener.interact(slash("project", sub="list"))
        posts = self.transport.channel_posts(DM_CHANNEL)
        self.assertEqual([p["content"] for p in posts],
                         ["That reply expired — run `/project list` again."])
        del self.transport.fail[("PATCH", r"@original$")]

        # /status, answered after the 14-minute window.
        def slow(task_id):
            self.now[0] += 15 * 60
            return self.stores.tasks.get(task_id)
        self.control.status = slow
        self.listener.interact(slash("status", **in_guild(TASK_THREAD)))
        posts = self.transport.channel_posts(TASK_THREAD)
        self.assertEqual([p.get("content") for p in posts],
                         ["That reply expired — run `/status` again."])
        self.assertFalse(any("embeds" in p for p in posts))
        everything = json.dumps(self.transport.channel_posts())
        self.assertNotIn("migrate the skills folder", everything)
        self.assertNotIn(self.tmp.name, everything)

    def test_a_public_reply_still_falls_back_to_a_channel_post(self):
        self.transport.fail[("PATCH", r"@original$")] = (404, {"message": "Unknown Webhook"})
        self.listener.interact(slash("steer", {"text": "x"}, **in_guild(TASK_THREAD)))
        self.assertIn("Noted", self.transport.channel_posts(TASK_THREAD)[-1]["content"])

    def test_an_unexpected_failure_says_so_and_never_done(self):
        def broken(task_id, text, *, spoken=False):
            raise RuntimeError("internal detail " + TOKEN)
        self.control.steer = broken
        with self.assertLogs("jarvis.v2", level="WARNING") as logs:
            self.listener.interact(slash("steer", {"text": "x"}, **in_guild(TASK_THREAD)))
        self.assertEqual([e.get("content") for e in self.edits()],
                         ["That failed (`RuntimeError`); it may not have run."])
        self.assertNotIn("internal detail", "\n".join(logs.output))
        self.assertNotIn(TOKEN, "\n".join(logs.output))
        self.assertIn("RuntimeError", "\n".join(logs.output))

    def test_an_oversized_skill_is_refused_before_any_deferral(self):
        self.listener.interact(slash("skill", {"name": "huge"}))
        self.refused("limit")
        self.assertFalse(self.deferred_already())
        self.assertEqual(self.provider.messages, [])

    def test_a_deferral_that_says_nothing_still_stops_thinking(self):
        reply = InteractionReply(self.rest, APP, slash("status"), fallback=self.surface._post,
                                 clock=lambda: self.now[0])
        reply.defer()
        reply.finish()
        self.assertEqual(self.edits()[-1]["content"], "Done.")


# -- approvals, answers, skills, tasks ---------------------------------------------


class VerbChecks(Harness):
    def test_yes_resolves_only_a_code_asked_in_this_chat(self):
        request, result, worker = self.ask(task_id=self.task.id)
        self.listener.interact(slash("yes", {"code": "zzzz"}, **in_guild(TASK_THREAD)))
        self.refused("No open authorization with code `zzzz`")
        for where in ({}, in_guild(PROJECT_CHANNEL)):
            self.listener.interact(slash("yes", {"code": request.code}, **where))
            self.refused("asked elsewhere")
        self.listener.interact(slash("yes", **in_guild(PROJECT_CHANNEL)))
        self.refused("No open authorization was asked in this chat")
        self.assertTrue(self.approvals.pending())
        self.listener.interact(slash("yes", **in_guild(TASK_THREAD)))      # the one open here
        worker.join(2)
        self.assertEqual(result["decision"], Decision.ALLOW)
        self.assertEqual(self.transport.callbacks()[-1], {"type": 5})
        self.assertTrue(any("Authorized" in text for text in self.said()))
        self.listener.interact(slash("yes", {"code": request.code}, **in_guild(TASK_THREAD)))
        self.refused("No open authorization")                              # one-shot

    def test_two_open_here_and_no_code_asks_which(self):
        first, first_result, first_worker = self.ask(command="one", task_id=self.task.id)
        second, second_result, second_worker = self.ask(command="two", task_id=self.task.id)
        self.listener.interact(slash("no", **in_guild(TASK_THREAD)))
        text = self.refused("Which one?")
        self.assertIn(first.code, text)
        self.assertIn(second.code, text)
        self.listener.interact(slash("no", {"code": second.code.upper()}, **in_guild(TASK_THREAD)))
        second_worker.join(2)
        self.assertEqual(second_result["decision"], Decision.DENY)
        self.approvals.shutdown()
        first_worker.join(2)

    def test_a_dm_fallback_approval_is_refused_from_the_thread(self):
        request, result, worker = self.ask(command="rm -rf build")          # no task: DM
        self.assertEqual(self.surface._approval_channels[request.req_id], DM_CHANNEL)
        self.listener.interact(slash("yes", {"code": request.code}, **in_guild(TASK_THREAD)))
        self.refused("asked elsewhere")
        self.listener.interact(complete("yes", "code", **in_guild(TASK_THREAD)))
        self.assertEqual(self.last_callback()["data"]["choices"], [])
        self.listener.interact(complete("yes", "code"))                      # the DM
        self.assertEqual([c["value"] for c in self.last_callback()["data"]["choices"]],
                         [request.code])
        self.listener.interact(slash("no", {"code": request.code}))
        worker.join(2)
        self.assertEqual(result["decision"], Decision.DENY)

    def test_always_is_refused_on_a_request_that_cannot_be_a_standing_rule(self):
        request, result, worker = self.ask(command="git status && rm -rf build",
                                           task_id=self.task.id)
        self.listener.interact(complete("always", "code", **in_guild(TASK_THREAD)))
        self.assertEqual(self.last_callback()["data"]["choices"], [])
        self.listener.interact(slash("always", {"code": request.code}, **in_guild(TASK_THREAD)))
        self.refused("can't become a standing rule")
        self.assertTrue(self.approvals.pending())
        self.assertFalse(config.ALLOWLIST_PATH.exists())
        escaped = ApprovalRequest(tool="Bash", args={"command": "pnpm build"},
                                  command="pnpm build", task_id=self.task.id,
                                  allowlistable=False)
        self.assertFalse(self.surface.allowlistable(escaped)[0])
        self.approvals.shutdown()
        worker.join(2)

    def test_always_mints_one_stem_through_the_broker(self):
        request, result, worker = self.ask(command="pnpm build", task_id=self.task.id)
        self.listener.interact(slash("always", **in_guild(TASK_THREAD)))
        worker.join(2)
        self.assertEqual(result["decision"], Decision.ALLOW)
        self.assertEqual(json.loads(config.ALLOWLIST_PATH.read_text()),
                         [{"tool": "run_command", "prefix": "pnpm"}])

    def test_buttons_match_owner_message_and_channel_then_vanish(self):
        other = self.stores.tasks.create(self.project.id, "other", discord_thread_id=OTHER_THREAD)
        self.stores.tasks.save(other)
        request, result, worker = self.ask(task_id=self.task.id)
        channel, message_id, code = self.surface._approval_messages[request.req_id]
        self.assertEqual((channel, code), (TASK_THREAD, request.code))
        # A press whose custom id disagrees with the message it is on.
        self.listener.interact(button("jv:a:zzzz", message_id, **in_guild(TASK_THREAD)))
        self.refused("does not match")
        # The right message, pressed from another channel's copy of the id.
        self.listener.interact(button(f"jv:a:{code}", message_id, **in_guild(OTHER_THREAD)))
        self.assertIn("does not match", self.last_callback()["data"]["content"])
        # A message that never carried an approval.
        self.listener.interact(button(f"jv:a:{code}", "123", **in_guild(TASK_THREAD)))
        self.refused("already answered")
        self.assertTrue(self.approvals.pending())

        self.listener.interact(button(f"jv:d:{code}", message_id, **in_guild(TASK_THREAD)))
        worker.join(2)
        self.assertEqual(result["decision"], Decision.DENY)
        self.assertEqual(self.transport.callbacks()[-1], {"type": 6})
        strip = f"/channels/{TASK_THREAD}/messages/{message_id}"
        wait_for(lambda: any(c["path"] == strip and c["method"] == "PATCH"
                             for c in self.transport.calls))
        edit = next(c for c in self.transport.calls if c["path"] == strip)
        self.assertEqual(edit["json"]["components"], [])
        self.assertIn("Denied", self.transport.channel_posts(TASK_THREAD)[-2]["content"]
                      + self.transport.channel_posts(TASK_THREAD)[-1]["content"])
        # A stale tap after resolution.
        self.listener.interact(button(f"jv:a:{code}", message_id, **in_guild(TASK_THREAD)))
        self.refused("already answered")

    def test_button_posts_are_remembered_and_stripped_after_a_restart(self):
        path = Path(self.stores.root) / "discord" / "approval-posts.json"
        request, result, worker = self.ask(task_id=self.task.id)
        channel, message_id, code = self.surface._approval_messages[request.req_id]
        self.assertEqual(json.loads(path.read_text()),
                         {request.req_id: [channel, message_id, code]})
        # The process dies with the request open: shutdown denies it without a
        # resolution event, so the map on disk still names the post when the
        # next daemon starts.
        self.approvals.shutdown()
        worker.join(2)
        self.assertEqual(result["decision"], Decision.DENY)
        self.assertIn(request.req_id, json.loads(path.read_text()))
        self.transport.calls.clear()
        again = DiscordRouter(self.daemon, self.stores, self.router, self.approvals,
                              self.control, self.rest, FakeListener,
                              announce=lambda text: None, sync_commands=False)
        self.addCleanup(again.stop)
        again.start()
        strip = f"/channels/{channel}/messages/{message_id}"
        wait_for(lambda: any(c["path"] == strip for c in self.transport.calls))
        edit = next(c for c in self.transport.calls if c["path"] == strip)
        self.assertEqual((edit["method"], edit["json"]["components"]), ("PATCH", []))
        wait_for(lambda: json.loads(path.read_text()) == {})
        self.assertFalse(any(c["method"] == "DELETE" for c in self.transport.calls))

    def test_answer_and_plain_text_share_one_answer_path(self):
        self.clarifying()
        self.listener.interact(complete("answer", "question", **in_guild(TASK_THREAD)))
        self.assertEqual([c["value"] for c in self.last_callback()["data"]["choices"]],
                         ["1", "2"])
        self.listener.interact(complete("answer", "text", others={"question": "2"},
                                        **in_guild(TASK_THREAD)))
        self.assertEqual([c["value"] for c in self.last_callback()["data"]["choices"]],
                         ["20", "22"])
        self.listener.interact(slash("answer", {"text": "22", "question": "2"},
                                     **in_guild(TASK_THREAD)))
        self.listener.interact(slash("answer", {"text": "x", "question": "0"},
                                     **in_guild(TASK_THREAD)))
        self.refused("no open question `0`")
        self.listener.interact(slash("answer", {"text": "pnpm", "task": self.task.id}))
        self.listener.feed({"channel_id": TASK_THREAD, "guild_id": GUILD, "content": "npm",
                            "author": {"id": OWNER}, "mentions": []})
        self.assertEqual([call[1] for call in self.control.named("answer_question")],
                         [(self.task.id, 2, "22"), (self.task.id, 1, "pnpm"),
                          (self.task.id, 1, "npm")])
        self.assertEqual(self.control.named("steer"), [])

    def test_skill_runs_as_a_chat_turn_with_the_skill_named(self):
        self.listener.interact(complete("skill", "name"))
        names = [c["value"] for c in self.last_callback()["data"]["choices"]]
        self.assertIn("morning-briefing", names)
        self.assertNotIn("whiteboard", names)
        self.listener.interact(slash("skill", {"name": "morning-briefing",
                                               "request": "keep it short"}))
        wait_for(lambda: self.provider.messages)
        message = self.provider.messages[-1]
        self.assertEqual((message.skill, message.text), ("morning-briefing", "keep it short"))
        self.assertIn("the fast path answered", self.edits()[-1]["content"])
        thread = self.stores.threads.list()[0]
        logged = [r for r in self.stores.threads.read_log(thread.id) if r.get("kind") == "user"]
        self.assertEqual(logged[-1]["data"], {"text": "keep it short",
                                              "skill": "morning-briefing"})

    def test_skill_in_a_task_thread_is_a_steer(self):
        self.listener.interact(slash("skill", {"name": "morning-briefing", "request": "today"},
                                     **in_guild(TASK_THREAD)))
        self.assertEqual(self.control.named("steer")[0][1],
                         (self.task.id, 'Use the "morning-briefing" skill for: today'))
        self.assertEqual(self.provider.messages, [])

    def test_task_opens_with_project_skill_and_provider(self):
        other = self.stores.projects.create("Robotics", self.tmp.name)
        self.listener.interact(complete("task", "project", "rob"))
        self.assertEqual(self.last_callback()["data"]["choices"],
                         [{"name": "Robotics", "value": other.id}])
        self.listener.interact(slash("task", {"brief": "tune the arm", "project": other.id,
                                              "skill": "morning-briefing",
                                              "provider": "codex"}))
        opened = self.stores.tasks.get(self.control.named("start")[0][1][0])
        self.assertEqual(opened.project_id, other.id)
        self.assertEqual(opened.provider_override, ProviderName.CODEX)
        self.assertIn('Use the "morning-briefing" skill.', opened.brief)
        self.assertIn(f"Opened task {opened.id} in Robotics", self.edits()[-1]["content"])

    def test_task_in_a_thread_opens_in_the_same_project_and_a_dm_in_the_inbox(self):
        self.listener.interact(slash("task", {"brief": "second job"}, **in_guild(TASK_THREAD)))
        self.listener.interact(slash("task", {"brief": "lunch"}))
        started = [self.stores.tasks.get(c[1][0]) for c in self.control.named("start")]
        self.assertEqual(started[0].project_id, self.project.id)
        self.assertTrue(self.stores.projects.get(started[1].project_id).inbox)

    def test_an_empty_brief_opens_a_modal_and_its_submit_is_rechecked(self):
        self.listener.interact(slash("task", {"project": self.project.id}))
        callback = self.last_callback()
        self.assertEqual(callback["type"], 9)
        custom = callback["data"]["custom_id"]
        self.assertEqual(custom, f"jv:task:{self.project.id}::")
        field = callback["data"]["components"][0]["components"][0]
        self.assertEqual((field["style"], field["max_length"]), (2, 4000))
        self.assertEqual(self.control.calls, [])

        self.listener.interact(modal_submit(custom, "a long\nmulti-line brief"))
        opened = self.stores.tasks.get(self.control.named("start")[0][1][0])
        self.assertEqual((opened.project_id, opened.brief),
                         (self.project.id, "a long\nmulti-line brief"))

        self.listener.interact(modal_submit("jv:task:nothex!::", "x"))
        self.refused("archived or gone")
        self.listener.interact(modal_submit("jv:task:::bogus", "x"))
        self.refused("claude or codex")
        self.listener.interact(modal_submit("something-else", "x"))
        self.refused("isn't one I know")
        self.archive(self.project)
        self.listener.interact(modal_submit(custom, "x"))
        self.refused("archived or gone")
        self.assertEqual(len(self.control.named("start")), 1)

    def test_status_cancel_resume_and_project_list(self):
        self.listener.interact(slash("status"))
        self.assertIn(self.task.id, self.edits()[-1]["content"])
        self.listener.interact(slash("status", {"task": self.task.id}))
        self.assertIn(self.task.id, self.edits()[-1]["embeds"][0]["title"])
        old = self.stores.projects.create("Old", self.tmp.name)
        self.archive(old)
        self.listener.interact(slash("project", sub="list"))
        self.assertEqual(self.transport.callbacks()[-1], {"type": 5, "data": {"flags": 64}})
        listing = self.edits()[-1]["content"]
        self.assertIn("Jarvis", listing)
        self.assertNotIn("Old", listing)
        self.listener.interact(slash("resume", **in_guild(TASK_THREAD)))
        self.refused("only a blocked task resumes")
        self.listener.interact(slash("cancel", **in_guild(TASK_THREAD)))
        self.assertEqual(self.control.named("cancel")[0][1], (self.task.id,))


# -- secrets -----------------------------------------------------------------------


class SecretChecks(Harness):
    def test_a_failed_callback_never_echoes_its_token(self):
        def echo(path):                     # a Discord that echoes the request back
            return {"message": f"bad request at {path} with {TOKEN}"}
        self.transport.fail[("POST", r"^/interactions/")] = (400, echo)
        token = ITOKEN + "42"
        with self.assertRaises(DiscordError) as caught:
            self.rest.callback("42", token, 4, {"content": "x"})
        self.assertNotIn(token, str(caught.exception))
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertIn("[REDACTED]", str(caught.exception))

    def test_no_token_in_logs_exceptions_or_bodies(self):
        def echo(path):                     # a Discord that echoes the request back
            return {"message": f"bad request at {path} with {TOKEN}"}
        self.transport.fail[("PATCH", r"@original$")] = (400, echo)
        self.transport.fail[("POST", r"^/webhooks/")] = (400, echo)
        with self.assertLogs("jarvis.v2", level="DEBUG") as logs:
            self.listener.interact(slash("steer", {"text": "x"}, **in_guild(TASK_THREAD)))
            self.listener.interact({"type": 2, "application_id": APP, "id": "1",
                                    "token": ITOKEN + "malformed", "context": 1,
                                    "user": {"id": OWNER}, "channel_id": DM_CHANNEL,
                                    "data": None})
            logging.getLogger("jarvis.v2").debug("marker")
        text = "\n".join(logs.output)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(ITOKEN, text)
        self.assertNotIn("/webhooks/", text)
        self.assertNotIn("/interactions/", text)
        try:
            self.rest.edit_original(APP, ITOKEN + "1", content="x")
        except DiscordError as exc:
            self.assertNotIn(ITOKEN, str(exc))
            self.assertNotIn(TOKEN, str(exc))
        else:
            self.fail("a 400 must raise")
        with self.assertRaises(DiscordError):
            self.rest.callback("1", "bad/../token", 4)            # never reaches a path
        for call in self.transport.calls:
            body = json.dumps(_body(call)) if ("json" in call or "data" in call) else ""
            self.assertNotIn(TOKEN, body)
            self.assertNotIn(ITOKEN, body)
            self.assertNotIn(ITOKEN, repr(call.get("files", "")))

    def test_an_interaction_reply_never_shows_its_token(self):
        payload = slash("status")
        reply = InteractionReply(self.rest, APP, payload, fallback=self.surface._post)
        self.assertNotIn(payload["token"], repr(reply))
        self.assertNotIn(payload["token"], json.dumps(self.surface.status()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
