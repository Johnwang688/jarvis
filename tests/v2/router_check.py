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
        for target, value in [("ROUTING_PATH", Path(self.tmp.name) / "routing.json"),
                              ("CODEX_CATALOG_PATH", Path(self.tmp.name) / "codex-models.json")]:
            patcher = patch.object(config, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        codex_table = R.CLI_MODELS["codex"]
        self.addCleanup(lambda: R.CLI_MODELS.__setitem__("codex", codex_table))
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
        with patch.dict(R.CLI_MODELS["codex"], {"gpt-text": {"name": "Text", "efforts": ("high",),
                                                             "vision": False}}):
            self.project.routing.models["implementer"] = {"codex": "gpt-text/high"}
            self.assertEqual(self.resolve("implementer", images=[{}]).provider, P.CLAUDE)
        # A project entry naming a model Jarvis does not know degrades to the
        # global table (vision-capable here), it does not fail the role.
        self.project.routing.models["implementer"] = {"codex": "unknown/high"}
        self.assertEqual(self.resolve("implementer", images=[{}]).provider, P.CODEX)

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
                     {"action": "models", "role": "reviewer", "provider": "codex", "model": "gpt-6-luna/ultra"},
                     {"action": "models", "role": "reviewer", "provider": "claude", "model": "roster/ultra"},
                     {"action": "models", "role": "reviewer", "provider": "claude", "model": "claude-opus-5-5/ultra"},
                     {"action": "models", "role": "reviewer", "provider": "claude", "model": "claude-haiku-4-5/high"}]:
            with self.assertRaises(ValueError):
                self.router.configure(body)
        self.assertEqual(json.loads(config.ROUTING_PATH.read_text())["models"]["reviewer"]["claude"],
                         "claude-sonnet-5-5/xhigh", "a refused model writes nothing")
        for good in ("roster/default", "claude-haiku-4-5/default", "claude-opus-5-5/max"):
            self.router.configure({"action": "models", "role": "reviewer", "provider": "claude", "model": good})
        # A hand-edited routing.json is held to the same list: the entry it
        # cannot use falls back to that role's default, with a note, rather
        # than making the whole table unreadable (PR #20 review).
        config.ROUTING_PATH.write_text(json.dumps({"models": {"reviewer": {"codex": "gpt-9-imaginary/high"}}}))
        notes = []
        loaded = R.load_routing(notes=notes)
        self.assertEqual(loaded["models"]["reviewer"]["codex"], R.defaults()["models"]["reviewer"]["codex"])
        self.assertIn("gpt-9-imaginary is not a codex model", " ".join(notes))
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



def live_rows(*ids, ladder=("low", "medium", "high", "xhigh", "max", "ultra"), **extra):
    """`model/list` rows as the account's app-server sends them."""
    return [{"model": m, "displayName": m.upper(), "hidden": False, "inputModalities": ["text", "image"],
             "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ladder],
             "defaultReasoningEffort": "medium", **extra} for m in ids]


LIVE = live_rows("gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol",
                 "gpt-5.6-terra", "gpt-5.6-luna")   # gpt-5.5 is hidden from the live listing


class CodexCatalog(Controls):
    """PR #20 review: the Codex table moved under persisted config. The table
    is the account's last good catalog, else the built-in fallback — replaced
    whole, never a union. What was saved naming a model it lacks degrades to
    the default with a note, an effort a model no longer offers is clamped,
    and nothing is ever rewritten, so the choice returns with the model."""

    def restart(self):
        """What a daemon restart does to the table before `main()` loads the
        saved catalog: the built-in fallback."""
        R.CLI_MODELS["codex"] = {m: dict(e) for m, e in R.CODEX_FALLBACK.items()}

    def test_the_offline_ladders_match_the_live_catalog(self):
        self.restart()
        for value in ("gpt-6-astra/max", "gpt-6-astra/ultra", "gpt-5.6-sol/ultra", "gpt-5.6-terra/ultra",
                      "gpt-6.1-sol/ultra", "gpt-6-sol/ultra", "gpt-6-luna/max", "gpt-5.6-luna/max"):
            self.assertEqual(R._cli_model("codex", value)[1], value.rsplit("/", 1)[1], value)
        for value in ("gpt-6-luna/ultra", "gpt-5.6-luna/ultra", "gpt-5.5/max"):
            with self.assertRaises(ValueError, msg=value):
                R._cli_model("codex", value)

    def test_scenario_a_a_live_only_entry_survives_a_restart(self):
        R.set_codex_models(live_rows("gpt-7-nova") + LIVE)
        self.router.configure({"action": "models", "role": "implementer", "provider": "codex",
                               "model": "gpt-7-nova/ultra"})
        self.restart()
        self.assertIsNotNone(R.load_codex_catalog())
        notes = []
        self.assertEqual(R.load_routing(notes=notes)["models"]["implementer"]["codex"], "gpt-7-nova/ultra")
        self.assertEqual(notes, [])
        self.assertEqual(R.model_settings("implementer", "codex"), ("gpt-7-nova", "ultra"))
        # Without the saved catalog the entry degrades, said, never raised,
        # and routing.json still holds it.
        config.CODEX_CATALOG_PATH.unlink()
        self.restart()
        self.assertIsNone(R.load_codex_catalog())
        notes = []
        loaded = R.load_routing(notes=notes)
        self.assertEqual(loaded["models"]["implementer"]["codex"], R.defaults()["models"]["implementer"]["codex"])
        self.assertIn("gpt-7-nova is not a codex model", " ".join(notes))
        self.assertEqual(json.loads(config.ROUTING_PATH.read_text())["models"]["implementer"]["codex"],
                         "gpt-7-nova/ultra", "never rewritten")
        self.resolve("implementer")                      # a task still routes
        self.start()
        for path in ("/route", "/usage"):
            self.assertEqual(self.request("GET", path)[0], 200, path)
        status, view = self.request("GET", "/route")
        self.assertIn("gpt-7-nova is not a codex model", " ".join(view["notes"]))

    def test_scenario_b_a_dropped_model_degrades_and_comes_back(self):
        self.router.configure({"action": "models", "role": "implementer", "provider": "codex",
                               "model": "gpt-5.5/high"})
        stored = config.ROUTING_PATH.read_text()
        R.set_codex_models(LIVE + [{"model": "gpt-5.5", "hidden": True}])
        self.assertNotIn("gpt-5.5", R.CLI_MODELS["codex"], "replaced whole: no union")
        notes = []
        self.assertEqual(R.load_routing(notes=notes)["models"]["implementer"]["codex"],
                         R.defaults()["models"]["implementer"]["codex"])
        self.assertIn("gpt-5.5 is not a codex model", " ".join(notes))
        self.task.provider_override = P.CODEX
        self.resolve("implementer")
        journal = [r for r in self.stores.tasks.read_journal(self.task.id) if r["event"] == "routing_decision"]
        self.assertEqual((journal[-1]["model"], journal[-1]["effort"]), ("gpt-5.6-sol", "high"))
        self.assertEqual(config.ROUTING_PATH.read_text(), stored, "never rewritten")
        # The account offers it again: the owner's choice is back.
        R.set_codex_models(LIVE + live_rows("gpt-5.5", ladder=("low", "medium", "high")))
        self.assertEqual(R.model_settings("implementer", "codex"), ("gpt-5.5", "high"))

    def test_a_saved_effort_the_model_no_longer_offers_is_clamped(self):
        """Option 1: clamp, don't refuse. gpt-6-astra/ultra saved while the
        account offered it; a catalog whose astra stops at xhigh runs xhigh,
        says so, and keeps ultra in the file for when it returns."""
        self.router.configure({"action": "models", "role": "orchestrator", "provider": "codex",
                               "model": "gpt-6-astra/ultra"})
        R.set_codex_models(live_rows("gpt-6-astra", ladder=("low", "medium", "high", "xhigh")) + LIVE[2:])
        notes = []
        self.assertEqual(R.model_settings("orchestrator", "codex", notes=notes), ("gpt-6-astra", "xhigh"))
        self.assertIn("gpt-6-astra does not offer effort 'ultra' now; running at xhigh", " ".join(notes))
        self.assertEqual(json.loads(config.ROUTING_PATH.read_text())["models"]["orchestrator"]["codex"],
                         "gpt-6-astra/ultra")
        # A project's own entry clamps the same way.
        self.project.routing.models["reviewer"] = {"codex": "gpt-6-astra/max"}
        self.assertEqual(R.model_settings("reviewer", "codex", self.project), ("gpt-6-astra", "xhigh"))

    def test_the_table_is_the_last_catalog_never_a_union(self):
        R.set_codex_models(live_rows("gpt-7-nova", "gpt-6-astra"))
        R.set_codex_models(live_rows("gpt-6-astra"))
        self.assertEqual(set(R.CLI_MODELS["codex"]), {"gpt-6-astra"})
        with self.assertRaises(ValueError):
            R.check_project_models({"implementer": {"codex": "gpt-7-nova/ultra"}})
        self.restart()
        R.load_codex_catalog()
        self.assertEqual(set(R.CLI_MODELS["codex"]), {"gpt-6-astra"}, "the saved catalog is the last one")

    def test_a_bad_routing_file_degrades_and_post_route_repairs_it(self):
        bad = {"chains": {"reviewer": ["fast"], "janitor": ["codex"]},
               "models": {"implementer": {"codex": "gpt-9/high", "fast": "x/high"},
                          "reviewer": {"codex": "gpt-6-astra/turbo", "claude": "roster/ultra"},
                          "researcher": {"codex": "gpt-6-astra/low"}},
               "no_new_work": 7, "allowances": {"codex": {"work_tokens": -1}, "mystery": {}},
               "surprise": True}
        config.ROUTING_PATH.write_text(json.dumps(bad))
        notes = []
        loaded = R.load_routing(notes=notes)
        defaults = R.defaults()
        self.assertEqual(loaded["chains"]["reviewer"], defaults["chains"]["reviewer"])
        self.assertEqual(loaded["models"]["implementer"], defaults["models"]["implementer"])
        self.assertEqual(loaded["models"]["reviewer"], defaults["models"]["reviewer"])
        self.assertEqual(loaded["models"]["researcher"]["codex"], "gpt-6-astra/low", "the good entry is kept")
        self.assertEqual((loaded["no_new_work"], loaded["allowances"]),
                         (defaults["no_new_work"], defaults["allowances"]))
        joined = " | ".join(notes)
        for words in ("surprise", "chains.reviewer", "chains.janitor", "gpt-9 is not a codex model",
                      "models.implementer.fast", "models.reviewer.codex", "claude takes no effort 'ultra'",
                      "no_new_work", "allowances.codex", "allowances.mystery"):
            self.assertIn(words, joined)
        self.start()
        for path in ("/route", "/usage"):
            self.assertEqual(self.request("GET", path)[0], 200, path)
        # POST /route reads the table before writing it; it must still work,
        # and it keeps a well-formed entry it was not asked about.
        bad["models"]["reviewer"]["codex"] = "gpt-7-later/high"
        config.ROUTING_PATH.write_text(json.dumps(bad))
        status, _ = self.request("POST", "/route", {"action": "models", "role": "implementer",
                                                    "provider": "codex", "model": "gpt-6-astra/high"})
        self.assertEqual(status, 200)
        written = json.loads(config.ROUTING_PATH.read_text())
        self.assertEqual(written["models"]["implementer"]["codex"], "gpt-6-astra/high")
        self.assertEqual(written["models"]["reviewer"]["codex"], "gpt-7-later/high", "never erased")
        self.assertNotIn("surprise", written)
        self.assertEqual(written["chains"]["reviewer"], defaults["chains"]["reviewer"])
        # A file that is not JSON at all is replaced by the next write.
        config.ROUTING_PATH.write_text("{ not json")
        self.assertEqual(R.load_routing(), defaults)
        self.assertEqual(self.request("GET", "/route")[0], 200)
        status, view = self.request("POST", "/route", {"action": "set", "role": "reviewer", "chain": "codex"})
        self.assertEqual((status, view["table"]["chains"]["reviewer"]), (200, ["codex"]))
        self.assertEqual(R.load_routing(notes=[])["chains"]["reviewer"], ["codex"])

    def test_roster_takes_only_its_own_clis_efforts(self):
        self.assertEqual(R._cli_model("claude", "roster/max"), ("roster", "max"))
        self.assertEqual(R._cli_model("codex", "roster/ultra"), ("roster", "ultra"))
        for value in ("roster/ultra", "roster/minimal", "roster/none"):
            with self.assertRaises(ValueError, msg=value):
                R._cli_model("claude", value)
        with self.assertRaises(ValueError):
            R.check_project_models({"implementer": {"claude": "roster/ultra"}})
        # A table handed in, not loaded, is held to the same rule.
        table = R.defaults()
        table["models"]["implementer"]["claude"] = "roster/ultra"
        notes = []
        self.assertEqual(R.model_settings("implementer", "claude", None, table, notes=notes), (None, None))
        self.assertIn("claude takes no effort 'ultra'", " ".join(notes))

    def test_hostile_catalog_rows_are_skipped_or_cleaned(self):
        from jarvis.v2 import codex_catalog as C
        before = dict(R.CLI_MODELS["codex"])
        for rows in (None, "rows", [], [None, 5, "x", {"id": 5}, {"model": "bad id"}, {"model": "x" * 200},
                                        {"model": "gpt-‮evil"}, {"model": "-leading"}]):
            with self.assertRaises(ValueError, msg=rows):
                R.set_codex_models(rows)
            self.assertEqual(R.CLI_MODELS["codex"], before, "a refused catalog changes nothing")
        self.assertFalse(config.CODEX_CATALOG_PATH.exists(), "and saves nothing")
        rows = [
            {"model": "a\x1b[31mb", "displayName": "control"},
            {"model": "gpt-ok", "displayName": "‮evil​ name\n" + "D" * 2_000_000,
             "supportedReasoningEfforts": [{"reasoningEffort": "turbo"}, "high", {"reasoningEffort": "high"},
                                           {"reasoningEffort": "default"}, None],
             "inputModalities": None},
            {"model": "gpt-ok", "displayName": "second copy"},
            {"model": "gpt-6-astra"},
            {"model": "gpt-text", "inputModalities": ["text"]},
            {"model": "gpt-hidden", "hidden": True},
            {"model": "gpt-hidden-ish", "hidden": "yes"},
            {"id": "gpt-by-id", "displayName": "   "},
        ]
        parsed = R.set_codex_models(rows)
        self.assertEqual(list(parsed), ["gpt-ok", "gpt-6-astra", "gpt-text", "gpt-by-id"])
        ok = parsed["gpt-ok"]
        self.assertLessEqual(len(ok["name"]), C.MAX_NAME)
        self.assertTrue(ok["name"].startswith("evil name"), ok["name"])
        self.assertFalse(any(ord(c) < 32 or c in "‮​" for c in ok["name"]))
        self.assertEqual(ok["efforts"], ("high",))
        # No modalities is not evidence of sight, whoever the model is.
        self.assertFalse(ok["vision"])
        self.assertFalse(parsed["gpt-6-astra"]["vision"])
        self.assertFalse(parsed["gpt-text"]["vision"])
        self.assertEqual(parsed["gpt-by-id"]["name"], "gpt-by-id")
        self.assertNotIn("gpt-hidden", R.CLI_MODELS["codex"])
        self.assertNotIn("gpt-hidden-ish", R.CLI_MODELS["codex"], "anything but an explicit false hides")
        many = R.set_codex_models(live_rows(*(f"m{n}" for n in range(C.MAX_MODELS + 50))))
        self.assertEqual(len(many), C.MAX_MODELS)

    def test_the_saved_catalog_is_atomic_private_and_read_back_validated(self):
        R.set_codex_models(live_rows("gpt-7-nova", "gpt-6-astra"))
        path = config.CODEX_CATALOG_PATH
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in path.parent.iterdir() if p.name.startswith(f".{path.name}.")], [])
        saved = json.loads(path.read_text())
        self.assertEqual([row["model"] for row in saved["models"]], ["gpt-7-nova", "gpt-6-astra"])
        # The file goes through the same parser as the live response: a
        # tampered row is skipped, a strange one cleaned.
        saved["models"].append({"model": "evil id", "displayName": "x",
                                "supportedReasoningEfforts": [{"reasoningEffort": "high"}]})
        saved["models"].append({"model": "gpt-odd", "displayName": "x",
                                "supportedReasoningEfforts": ["warp", "low"], "inputModalities": "image"})
        path.write_text(json.dumps(saved))
        self.restart()
        table = R.load_codex_catalog()
        self.assertNotIn("evil id", table)
        self.assertEqual((table["gpt-odd"]["efforts"], table["gpt-odd"]["vision"]), (("low",), False))
        # A corrupt file is the built-in fallback.
        for raw in (b"{ not json", b"[]", b'{"models": 5}', b'{"version": 1, "models": []}',
                    b'{"version": 9, "models": [{"model": "gpt-x"}]}', b"\xff\xfe"):
            R.set_codex_models(live_rows("gpt-7-nova"))     # then a restart reads garbage
            path.write_bytes(raw)
            self.restart()
            self.assertIsNone(R.load_codex_catalog(), raw)
            self.assertEqual(set(R.CLI_MODELS["codex"]), set(R.CODEX_FALLBACK), raw)
        path.write_bytes(b" " * (2 * 1024 * 1024))
        self.assertIsNone(R.load_codex_catalog(), "an oversized file is not read")
        # A save that fails keeps the table it installed, and says so.
        with patch("jarvis.v2.codex_catalog._write_bytes", side_effect=R.StoreError("disk full")), \
                self.assertLogs("jarvis.v2.codex_catalog", "WARNING") as logged:
            R.set_codex_models(live_rows("gpt-7-nova"))
        self.assertIn("gpt-7-nova", R.CLI_MODELS["codex"])
        self.assertIn("not written (StoreError)", " ".join(logged.output))
        self.assertNotIn("disk full", " ".join(logged.output), "error class only")

    def load_in_child(self, path):
        """`codex_catalog.load(path)` in a child process with a 1 GB address
        space and a time limit, so a loader that reads `/dev/zero` or blocks
        on a FIFO fails this test instead of eating the machine or the run."""
        import os
        import resource
        import subprocess
        import sys

        def limit():
            resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))

        code = ("import sys, json\nfrom jarvis.v2 import codex_catalog as C\n"
                "print(json.dumps(C.load(sys.argv[1]) is None))")
        try:
            run = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True,
                                 timeout=20, preexec_fn=limit, env=dict(os.environ))
        except subprocess.TimeoutExpired:
            self.fail(f"loading {path} hung")
        self.assertEqual((run.returncode, run.stdout.strip()), (0, "true"), run.stderr[-500:])
        return run

    def test_the_cache_loader_refuses_special_files(self):
        """PR #20 re-review: the loader runs at daemon start. A FIFO planted at
        the path hung boot; a symlink to /dev/zero (size 0) read until
        MemoryError, which `main()` did not catch — a crash loop under
        systemd. Only a regular file, never followed through a symlink and
        never read past the cap, is loaded; anything else is the fallback."""
        import os
        import time
        from jarvis.v2 import codex_catalog as C
        from jarvis.v2 import daemon as D
        root = Path(self.tmp.name)
        fifo = root / "fifo.json"
        os.mkfifo(fifo)
        self.load_in_child(fifo)        # bounded first: a broken loader fails, not hangs
        started = time.monotonic()
        self.assertIsNone(C.load(fifo))
        self.assertLess(time.monotonic() - started, 2, "a FIFO is refused, not waited on")
        zero = root / "zero.json"
        zero.symlink_to("/dev/zero")
        self.load_in_child(zero)
        self.assertIsNone(C.load(zero))
        self.assertIsNone(C.load("/dev/zero"), "a character device is not read")
        # A symlink is not followed even to a good catalog.
        R.set_codex_models(live_rows("gpt-7-nova"))
        good = config.CODEX_CATALOG_PATH
        link = root / "link.json"
        link.symlink_to(good)
        self.assertIsNone(C.load(link))
        self.assertIn("gpt-7-nova", C.load(good))
        # Oversized: refused by size before reading, and by count when the
        # size lies (a file grown past the cap reads as too large).
        big = root / "big.json"
        big.write_bytes(b" " * (C.MAX_CACHE_BYTES + 1))
        self.assertIsNone(C.load(big))
        with patch.object(C.os, "fstat", side_effect=lambda fd, real=os.fstat: os.stat_result(
                real(fd)[:6] + (0,) + real(fd)[7:])):
            self.assertIsNone(C.load(big), "a size of 0 that is not true is still capped")
        # Whatever the loader raises, the daemon starts on the fallback.
        self.restart()
        for exc in (MemoryError(), RuntimeError("odd"), OSError("io")):
            with patch.object(R, "load_codex_catalog", side_effect=exc), \
                    self.assertLogs("jarvis.v2.daemon", "WARNING") as logged:
                self.assertFalse(D.load_codex_catalog_at_start())
            self.assertIn(f"not loaded ({type(exc).__name__})", " ".join(logged.output))
            self.assertEqual(set(R.CLI_MODELS["codex"]), set(R.CODEX_FALLBACK))
        config.CODEX_CATALOG_PATH.unlink()
        os.mkfifo(config.CODEX_CATALOG_PATH)
        self.assertFalse(D.load_codex_catalog_at_start())
        self.assertEqual(set(R.CLI_MODELS["codex"]), set(R.CODEX_FALLBACK))

    def test_a_missing_built_in_default_is_said_not_changed(self):
        """PR #20 re-review: if the account drops a routing default (astra,
        5.6-sol), it is still what runs — there is nothing to fall back to —
        but `GET /route` says so instead of a turn failing unexplained."""
        R.set_codex_models(live_rows("gpt-6.1-sol", "gpt-5.6-sol"))
        notes = []
        self.assertEqual(R.model_settings("orchestrator", "codex", notes=notes), ("gpt-6-astra", "xhigh"))
        self.assertIn("the built-in default gpt-6-astra is not a codex model the account offers now",
                      " ".join(notes))
        self.start()
        status, view = self.request("GET", "/route")
        self.assertEqual(status, 200)
        text = " ".join(view["notes"])
        self.assertIn("orchestrator.codex: the built-in default gpt-6-astra", text)
        self.assertIn("reviewer.codex: the built-in default gpt-6-astra", text)
        self.assertNotIn("implementer.codex", text, "gpt-5.6-sol is offered")
        self.assertNotIn("claude", text, "roster/default names no model")
        self.assertEqual(view["table"]["resolved_models"]["orchestrator"]["codex"],
                         {"model": "gpt-6-astra", "effort": "xhigh"}, "what runs does not change")
        # An owner's entry for the role silences it.
        self.router.configure({"action": "models", "role": "orchestrator", "provider": "codex",
                               "model": "gpt-6.1-sol/high"})
        status, view = self.request("GET", "/route")
        self.assertNotIn("orchestrator.codex", " ".join(view["notes"]))

    def test_ultra_never_reaches_claude(self):
        """`ultra` is Codex's alone: no Claude routing entry carries it, saved
        or new, roster or not, so no Claude brief is ever built with it."""
        for value in ("roster/ultra", "claude-opus-5-5/ultra"):
            with self.assertRaises(ValueError, msg=value):
                R._cli_model("claude", value)
        config.ROUTING_PATH.write_text(json.dumps({"models": {
            "implementer": {"claude": "roster/ultra"}, "reviewer": {"claude": "claude-opus-5-5/ultra"}}}))
        for role in R.ROLES:
            self.assertNotEqual(R.model_settings(role, "claude")[1], "ultra", role)
        self.assertEqual(R.model_settings("reviewer", "claude"), ("claude-opus-5-5", None),
                         "clamps to nothing on Claude's ladder: the model's own default")
        self.project.routing.models["orchestrator"] = {"claude": "claude-sonnet-5-5/ultra"}
        self.assertEqual(R.model_settings("orchestrator", "claude", self.project), ("claude-sonnet-5-5", None))



if __name__ == "__main__":
    unittest.main()
