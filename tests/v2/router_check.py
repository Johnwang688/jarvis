"""Free WP9 routing, CLI and HTTP checks; only fakes and temporary stores."""
from contextlib import redirect_stdout
import http.client
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jarvis import config, models
from jarvis.v2 import router as R
from jarvis.v2.daemon import Daemon
from jarvis.v2.ledger import UsageLedger
from jarvis.v2.model import PermissionProfile as Profile, ProviderName as P, Role, TaskState
from jarvis.v2.provider import Brief, Event, EventKind as K
from jarvis.v2.stores import Stores


class Fake:
    def __init__(self, *script):
        self.script = list(script) or [(True, "ready")]
        self.calls = 0

    def health(self):
        value = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(value, Exception):
            raise value
        return value


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stores = Stores(Path(self.tmp.name) / "stores")
        self.project = self.stores.projects.create("Jarvis", self.tmp.name)
        self.task = self.stores.tasks.create(self.project.id, "build it")
        self.stores.tasks.save(self.task)
        self.now = 1_800_000_000.0
        self.cache = R.HealthCache(clock=lambda: self.now)
        self.ledger = UsageLedger(self.stores, clock=lambda: self.now)
        self.providers = {p.value: Fake() for p in P}
        self.router = R.Router(self.stores, self.providers, self.ledger, clock=lambda: self.now, health_cache=self.cache)
        for target, value in [("ROUTING_PATH", Path(self.tmp.name) / "routing.json")]:
            patcher = patch.object(config, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in [patch.object(models, "tier", return_value="anthropic/claude-test"),
                        patch.object(models, "effort_for", return_value="high"),
                        patch.object(models, "cached_info", return_value=None)]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def resolve(self, role="orchestrator", **kw):
        return R.resolve(role, self.task, self.project, self.ledger, self.providers,
                         health_cache=self.cache, **kw)

    def thread(self, provider=P.FAST, **kw):
        thread = self.stores.threads.create(self.project.id, Role.CHAT, provider, **kw)
        self.stores.threads.save(thread)
        return thread

    def proposal(self, **kw):
        thread = self.thread()
        return {"kind": "turn_finished", "thread_id": thread.id, "turn_id": "turn-1",
                "data": {"proposal": {"brief": "convert the skills folder", **kw}}}


class StageOne(Fixture):
    def test_verbs_and_precedence(self):
        commands = ["yes abc", "no abc", "always abc", "status", "cancel", "cancel deadbeef",
                    "steer: do this", "redirect: that", "projects", "tasks", "resume deadbeef",
                    "resume deadbeef on codex", "RESUME deadbeef ON claude", "resume deadbeef on unknown"]
        for text in commands:
            with self.subTest(text=text):
                self.assertIsInstance(R.classify(R.Incoming(text, task_id="t", intake=True)), R.Verb)
                self.assertIsInstance(R.classify(R.Incoming(text, spoken=True)), R.FastPath)
                self.assertEqual(R.classify(R.Incoming(text, spoken=True, task_id="t")), R.Steer("t"))
        for text in ["yes", "status please", "yesterday abc", "resume", "always", "tasks now"]:
            self.assertIsInstance(R.classify(R.Incoming(text)), R.FastPath)

    def test_intake_task_thread_and_chat(self):
        self.assertEqual(R.classify(R.Incoming("task: build", task_id="t")), R.NewTask("build"))
        self.assertEqual(R.classify(R.Incoming("build", intake=True)), R.NewTask("build"))
        self.assertEqual(R.classify(R.Incoming("hi", task_id="t")), R.Steer("t"))
        self.assertIsInstance(R.classify(R.Incoming("hi", thread_id="chat")), R.FastPath)
        self.assertIsInstance(R.classify(R.Incoming("task: build", spoken=True, intake=True)), R.FastPath)

    def test_placement_chain_and_needs_project(self):
        surface = self.stores.projects.create("surface", self.tmp.name, discord_channel_id="channel")
        explicit = self.stores.projects.create("explicit", self.tmp.name)
        thread = self.thread()
        context = R.Incoming("", project_id=surface.id, thread_id=thread.id)
        self.assertEqual(self.router.place(explicit.name, context).id, explicit.id)
        self.assertEqual(self.router.place(None, context).id, self.project.id)
        self.assertEqual(self.router.place(None, R.Incoming("", project_id=surface.id)).id, surface.id)
        self.assertEqual(self.router.place(None, R.Incoming("", surface="channel")).id, surface.id)
        self.assertIsNone(self.router.place("missing", context))
        self.assertIsNone(self.router.place())
        event = self.proposal(project="missing")
        self.assertIsInstance(self.router.on_turn_finished(event), R.NeedsProject)
        event["data"]["proposal"]["project"] = explicit.id
        self.assertIn("in explicit:", self.router.on_turn_finished(event))

    def test_proposal_intake_reply_journal_grace_and_restart(self):
        event = self.proposal(provider="codex")
        reply = self.router.on_turn_finished(event)
        tasks = [t for t in self.stores.tasks.list() if t.id != self.task.id]
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task.state, TaskState.INTAKE)
        self.assertEqual(task.provider_override, P.CODEX)
        self.assertEqual(reply, f"Opened task {task.id} in Jarvis: convert the skills folder. It will ask if anything is unclear.")
        row = self.stores.tasks.read_journal(task.id)[0]
        self.assertEqual(row["brief"], "convert the skills folder")
        self.assertEqual(row["event"], "proposed_by_fastpath")
        self.assertFalse(self.router.ready_to_start(task))
        self.assertEqual(self.router.on_turn_finished(event), reply)
        other = R.Router(self.stores, clock=lambda: self.now)
        self.now += 59
        self.assertTrue(other.cancel_proposal(task.id))
        self.assertEqual(self.stores.tasks.get(task.id).state, TaskState.CANCELLED)
        self.assertFalse(other.cancel_proposal(task.id))
        event = self.proposal()
        other.on_turn_finished(event)
        late = next(t for t in self.stores.tasks.list() if t.id not in (task.id, self.task.id))
        self.now += 60
        self.assertFalse(other.ready_to_start(task))  # stale INTAKE object was cancelled
        self.assertTrue(other.ready_to_start(late))
        self.assertFalse(other.cancel_proposal(late.id))
        self.assertEqual(self.stores.tasks.get(late.id).thread_ids, [])

    def test_nonfast_proposal_ignored(self):
        thread = self.thread(P.CODEX)
        self.assertIsNone(self.router.on_turn_finished(Event(K.TURN_FINISHED, thread.id, {"proposal": {"brief": "work"}})))

    def test_combined_event_hook_and_inbox_placement(self):
        thread = self.thread()
        self.router.on_event(Event(K.USAGE, thread.id, {"cost_usd": .1}))
        self.assertEqual(self.ledger.totals(provider=P.FAST)["spend_usd"], .1)
        inbox = self.stores.projects.inbox()
        thread.project_id = inbox.id
        self.stores.threads.save(thread)
        event = Event(K.TURN_FINISHED, thread.id, {"proposal": {"brief": "work"}})
        self.assertIsInstance(self.router.on_event(event), R.NeedsProject)
        reply = self.router.on_event(event, R.Incoming("", thread_id=thread.id, project_id=self.project.id))
        self.assertIn("in Jarvis", reply)


class Ladder(Fixture):
    def test_defaults_models_and_decision_persistence(self):
        for role in R.ROLES:
            decision = self.resolve(role)
            self.assertEqual(decision.provider, P.CLAUDE if role in ("orchestrator", "reviewer") else P.CODEX)
            for step in ("1 owner override", "2 project table", "3 global defaults", "4 capability filter", "5 availability filter", "6 fallback chain"):
                self.assertIn(step, decision.reason)
        saved = self.stores.tasks.get(self.task.id)
        self.assertEqual(len(saved.status.routing), 4)
        rows = self.stores.tasks.read_journal(self.task.id)
        self.assertEqual(len([r for r in rows if r["event"] == "routing_decision"]), 4)
        # "roster/default" on Claude means no model argument: the v1 roster holds
        # OpenRouter ids, and the first live task handed one to Claude Code.
        self.assertIsNone(rows[0]["model"])
        self.assertEqual(rows[1]["model"], "gpt-5.6-sol")
        self.assertEqual(rows[1]["effort"], "high")
        self.assertEqual(self.providers["claude"].calls, 1)
        self.assertEqual(self.providers["codex"].calls, 1)

    def test_override_project_global_ladder(self):
        self.task.brief = "use codex, build it"
        self.project.routing.chains["orchestrator"] = ["claude", "codex"]
        decision = self.resolve()
        self.assertEqual(decision.provider, P.CODEX)
        self.assertIn("1 owner override: codex", decision.reason)
        self.assertEqual(self.task.brief, "build it")
        self.assertEqual(self.stores.tasks.get(self.task.id).provider_override, P.CODEX)
        self.assertEqual(self.router.on_steer(self.task, "use claude; fix it"), "fix it")
        self.assertEqual(self.resolve().provider, P.CLAUDE)
        self.task.provider_override = None
        self.project.routing.chains["orchestrator"] = ["codex", "claude"]
        self.assertEqual(self.resolve().provider, P.CODEX)
        self.assertIn("2 project table: codex,claude", self.resolve().reason)
        self.project.routing.chains.clear()
        self.router.configure({"action": "set", "role": "orchestrator", "chain": "codex,claude"})
        self.assertEqual(self.resolve().provider, P.CODEX)

    def test_role_override_persisted_from_steer(self):
        self.assertEqual(self.router.on_steer(self.task, "use codex for reviewer; review"), "review")
        self.assertEqual(self.resolve("reviewer").provider, P.CODEX)
        self.assertEqual(self.resolve("orchestrator").provider, P.CLAUDE)

    def test_capabilities_strict_vision_mcp(self):
        self.project.profile = Profile.STRICT
        decision = self.resolve()
        self.assertEqual(decision.provider, P.CODEX)
        self.assertIn("strict requires codex", decision.reason)
        self.project.profile = Profile.AUTO
        with patch.object(models, "cached_info", return_value=models.ModelInfo("test", "test", vision=False)):
            decision = self.resolve(images=[{"b64": "fake"}])
            self.assertEqual(decision.provider, P.CODEX)
            self.assertIn("not known image-capable", decision.reason)
        self.providers["claude"].supported_mcp_servers = {"files"}
        brief = Brief(Role.ORCHESTRATOR, self.tmp.name, mcp_servers={"git": {}})
        self.assertIn("missing MCP servers git", self.resolve(brief=brief).reason)
        self.project.routing.models["implementer"] = {"codex": "unknown/high"}
        self.assertEqual(self.resolve("implementer", images=[{}]).provider, P.CLAUDE)

    def test_health_drops_cache_expiry_and_exception(self):
        self.providers["claude"] = Fake((False, "login expired"), (True, "fixed"))
        for _ in range(2):
            d = self.resolve()
            self.assertEqual(d.provider, P.CODEX)
            self.assertIn("login expired", d.reason)
        self.assertEqual(self.providers["claude"].calls, 1)
        self.now += 60
        self.assertEqual(self.resolve().provider, P.CLAUDE)
        self.providers["claude"] = Fake(RuntimeError("missing CLI"))
        self.assertIn("missing CLI", self.resolve().reason)

    def test_owner_override_falls_back_only_after_drop(self):
        self.task.provider_override = P.CODEX
        self.providers["codex"] = Fake((False, "login missing"))
        decision = self.resolve()
        self.assertEqual(decision.provider, P.CLAUDE)
        self.assertIn("1 owner override: codex", decision.reason)
        self.assertIn("codex: 5 availability filter", decision.reason)

    def test_ledger_states_fallback_and_blocked_drops(self):
        thread = self.thread(P.CLAUDE)
        self.ledger.on_event(Event(K.USAGE, thread.id, {"input": 3_400_000}))
        decision = self.resolve()
        self.assertEqual(decision.provider, P.CODEX)
        self.assertIn("over_threshold", decision.reason)
        self.assertIn("6 fallback chain: codex", decision.reason)
        self.providers["codex"] = Fake((False, "binary missing"))
        with self.assertRaises(R.RoutingBlocked) as blocked:
            self.resolve()
        for text in ("7 nothing survived", "claude", "codex", "over_threshold", "binary missing"):
            self.assertIn(text, str(blocked.exception))
        self.assertEqual(self.stores.tasks.read_journal(self.task.id)[-1]["milestone"], "blocked")
        self.assertEqual(len(self.task.status.routing), 1)

    def test_missing_provider_and_cooling(self):
        self.providers.pop("claude")
        self.assertIn("not in provider roster", self.resolve().reason)
        thread = self.thread(P.CODEX)
        self.ledger.on_event(Event(K.ERROR, thread.id, {"retry_at": self.now + 900}))
        with self.assertRaisesRegex(R.RoutingBlocked, "cooling"):
            self.resolve()

    def test_reviewer_preference_cannot_override_owner_project_or_filter(self):
        self.task.provider_override = P.CLAUDE
        self.resolve("implementer")
        self.task.provider_override = None
        self.assertEqual(self.resolve("reviewer").provider, P.CODEX)
        self.assertIn("reviewer differs", self.task.status.routing[-1].reason)
        self.task.provider_override = P.CLAUDE
        self.assertEqual(self.resolve("reviewer").provider, P.CLAUDE)
        self.task.provider_override = None
        self.project.routing.chains["reviewer"] = ["claude", "codex"]
        self.assertEqual(self.resolve("reviewer").provider, P.CLAUDE)
        self.project.routing.chains.clear()
        self.providers["codex"] = Fake((False, "offline"))
        self.assertEqual(self.resolve("reviewer").provider, P.CLAUDE)

    def test_one_provider_preference_only_among_survivors(self):
        self.resolve("implementer")
        thread = self.thread(P.CLAUDE)
        self.ledger.on_event(Event(K.USAGE, thread.id, {"input": 3_400_000}))
        self.assertEqual(self.resolve().provider, P.CODEX)
        self.assertIn("one provider per task", self.task.status.routing[-1].reason)
        self.task.provider_override = P.CLAUDE
        self.assertEqual(self.resolve().provider, P.CODEX)
        self.assertIn("over_threshold", self.task.status.routing[-1].reason)

    def test_project_fraction_and_singleton_chain_not_widened(self):
        thread = self.thread(P.CLAUDE)
        self.ledger.on_event(Event(K.USAGE, thread.id, {"input": 2_000_000}))
        self.project.routing.no_new_work = .5
        self.assertEqual(self.resolve().provider, P.CODEX)
        self.project.routing.chains["orchestrator"] = ["claude"]
        with self.assertRaises(R.RoutingBlocked) as error:
            self.resolve()
        self.assertEqual(set(error.exception.drops), {"claude"})

    def test_pinned_thread_fresh_worker_and_orchestrator_restart_offer(self):
        thread = self.stores.threads.create(self.project.id, Role.ORCHESTRATOR, P.CLAUDE, task_id=self.task.id)
        self.stores.threads.save(thread)
        self.task.thread_ids = [thread.id]
        self.task.plan = ["build"]
        self.stores.tasks.save(self.task)
        self.stores.tasks.journal(self.task.id, "worker_report", done=["prepared"])
        self.providers["claude"] = Fake((False, "offline"))
        self.assertEqual(self.router.next_worker(self.task, "implementer").provider, P.CODEX)
        count = len(self.task.status.routing)
        text, durable = self.router.orchestrator_unavailable(self.task)
        self.assertIn(f"resume {self.task.id} on codex", text)
        self.assertEqual(durable["plan"], ["build"])
        self.assertEqual(durable["worker_reports"][0]["done"], ["prepared"])
        self.assertIn("spec", durable)
        self.assertIn("status", durable)
        self.assertEqual(len(self.task.status.routing), count)
        self.assertEqual(self.stores.threads.get(thread.id).provider, P.CLAUDE)


class Controls(Fixture):
    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.daemon.port, timeout=3)
        conn.request(method, path, json.dumps(body) if body is not None else None)
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def start(self):
        self.daemon = Daemon(self.stores, self.providers, lambda *_: None, 0)
        self.daemon.router = self.router
        self.daemon.start()
        self.addCleanup(self.daemon.stop)

    def test_config_validation_models_and_last_ten(self):
        for _ in range(12):
            self.resolve()
        view = self.router.view()
        self.assertEqual(len(view["decisions"]), 10)
        self.assertEqual(set(view["states"]), {"claude", "codex", "fast"})
        self.router.configure({"action": "models", "role": "reviewer", "provider": "claude", "model": "claude-sonnet-5-5/xhigh"})
        self.assertEqual(R.model_settings("reviewer", "claude"), ("claude-sonnet-5-5", "xhigh"))
        self.assertEqual(json.loads(config.ROUTING_PATH.read_text())["models"]["reviewer"]["claude"], "claude-sonnet-5-5/xhigh")
        self.router.configure({"action": "set", "role": "reviewer", "chain": "codex", "project": self.project.id})
        self.assertEqual(self.stores.projects.get(self.project.id).routing.chains["reviewer"], ["codex"])
        for body in [{}, {"action": "set", "role": "chat", "chain": "codex"},
                     {"action": "set", "role": "reviewer", "chain": "fast"},
                     {"action": "set", "role": "reviewer", "chain": "codex,codex"},
                     {"action": "models", "role": "reviewer", "provider": "codex", "model": "bad"},
                     # CLI_MODELS is the one list: the chip offers nothing else,
                     # so the routing table may not name anything else either.
                     {"action": "models", "role": "reviewer", "provider": "claude", "model": "anthropic/other/xhigh"},
                     {"action": "models", "role": "reviewer", "provider": "codex", "model": "claude-opus-5-5/high"},
                     {"action": "models", "role": "reviewer", "provider": "codex", "model": "gpt-6-astra/max"},
                     {"action": "models", "role": "reviewer", "provider": "claude", "model": "claude-haiku-4-5/high"}]:
            with self.assertRaises(ValueError):
                self.router.configure(body)
        self.assertEqual(json.loads(config.ROUTING_PATH.read_text())["models"]["reviewer"]["claude"],
                         "claude-sonnet-5-5/xhigh", "a refused model writes nothing")
        for good in ("roster/default", "claude-haiku-4-5/default", "claude-opus-5-5/max"):
            self.router.configure({"action": "models", "role": "reviewer", "provider": "claude", "model": good})
        # A hand-edited routing.json is held to the same list.
        config.ROUTING_PATH.write_text(json.dumps({"models": {"reviewer": {"codex": "gpt-9-imaginary/high"}}}))
        with self.assertRaises(ValueError) as refused:
            R.load_routing()
        self.assertIn("gpt-9-imaginary is not a codex model", str(refused.exception))
        self.assertFalse(list(Path(self.tmp.name).glob("*.tmp")))

    def test_http_happy_and_400(self):
        self.start()
        self.assertEqual(self.request("GET", "/route")[0], 200)
        status, result = self.request("POST", "/route", {"action": "set", "role": "reviewer", "chain": "codex,claude"})
        self.assertEqual(status, 200)
        self.assertEqual(result["table"]["chains"]["reviewer"], ["codex", "claude"])
        self.assertEqual(self.request("POST", "/route", {"action": "models", "role": "researcher", "provider": "codex", "model": "gpt-6-astra/xhigh"})[0], 200)
        status, result = self.request("POST", "/route", {"action": "set", "role": "reviewer", "chain": "claude", "project": self.project.id})
        self.assertEqual(status, 200)
        self.assertEqual(result["table"]["chains"]["reviewer"], ["claude"])
        self.assertEqual(self.request("GET", "/route?project=" + self.project.id)[0], 200)
        for path in ("/route?bad=1", "/route?project=bad", "/route?project=deadbeef", "/route?project=a&project=b"):
            self.assertEqual(self.request("GET", path)[0], 400)
        for body in ({}, [], {"action": "set", "role": "reviewer", "chain": []}, {"unknown": 1},
                     {"action": "set", "role": "reviewer", "chain": "codex", "project": ""},
                     {"action": "models", "role": "reviewer", "provider": "codex", "model": "bad"},
                     {"action": "models", "role": "reviewer", "provider": "codex", "model": "unknown/high"}):
            self.assertEqual(self.request("POST", "/route", body)[0], 400)

    def test_cli_dispatch_happy_and_errors(self):
        from jarvis import __main__ as command
        self.start()
        for argv in (["jarvis", "route"], ["jarvis", "route", "set", "reviewer", "codex,claude"],
                     ["jarvis", "route", "set", "implementer", "claude", "--project", self.project.id],
                     ["jarvis", "route", "models", "reviewer", "codex", "gpt-6-astra/xhigh"]):
            output = io.StringIO()
            with patch("sys.argv", argv), patch.object(config, "DAEMON_PORT", self.daemon.port), redirect_stdout(output):
                self.assertEqual(command.main(), 0)
            self.assertIn("table", json.loads(output.getvalue()))
        with patch("sys.argv", ["jarvis", "route", "set", "bad", "codex"]), patch.object(config, "DAEMON_PORT", self.daemon.port), redirect_stdout(io.StringIO()):
            self.assertEqual(command.main(), 1)


if __name__ == "__main__":
    unittest.main()
