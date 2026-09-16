"""Free WP9 ledger checks: temporary roots and controlled Chicago clock."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from jarvis import config
from jarvis.v2.daemon import Daemon
from jarvis.v2.ledger import UsageLedger
from jarvis.v2.model import ProviderName as P, Role
from jarvis.v2.provider import Event, EventKind as K
from jarvis.v2.stores import Stores


class LedgerChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stores = Stores(Path(self.tmp.name))
        patcher = patch.object(config, "ROUTING_PATH", Path(self.tmp.name) / "routing.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.project = self.stores.projects.create("test", self.tmp.name)
        self.task = self.stores.tasks.create(self.project.id, "work")
        self.stores.tasks.save(self.task)
        self.now = datetime(2026, 9, 15, 12, tzinfo=timezone.utc).timestamp()
        self.ledger = UsageLedger(self.stores, clock=lambda: self.now)
        self.threads = {}
        for p in P:
            t = self.stores.threads.create(self.project.id, Role.IMPLEMENTER, p, task_id=self.task.id)
            self.stores.threads.save(t)
            self.threads[p] = t

    def usage(self, provider, **data):
        self.ledger.on_event(Event(K.USAGE, self.threads[provider].id, data))

    def error(self, provider, **data):
        self.ledger.on_event(Event(K.ERROR, self.threads[provider].id, data))

    def test_cumulative_codex_highwater_and_increment_providers(self):
        for inp, out, cache in [(100, 30, 20), (150, 60, 40), (140, 50, 30), (150, 60, 40)]:
            self.usage(P.CODEX, input=inp, output=out, cached=cache, cost_usd=99)
        self.assertEqual(self.ledger.totals(provider=P.CODEX)["work_tokens"], 170)
        self.assertEqual(self.ledger.totals(provider=P.CODEX)["spend_usd"], 0)
        self.usage(P.CODEX, input=10, usage_mode="delta")
        self.assertEqual(self.ledger.totals(provider=P.CODEX)["work_tokens"], 180)
        for _ in range(2):
            self.usage(P.CLAUDE, input=100, output=30, cached=20, cost_usd=.5)
            self.usage(P.FAST, cost_usd=.25)
        self.assertEqual(self.ledger.totals(provider=P.CLAUDE),
                         {"work_tokens": 220, "equivalent_usd": 1, "spend_usd": 0})
        self.assertEqual(self.ledger.totals(provider=P.FAST),
                         {"work_tokens": 0, "equivalent_usd": 0, "spend_usd": .5})
        rows = [json.loads(line) for p in self.ledger.root.glob("*") for line in p.read_text().splitlines()]
        self.assertFalse(any("spend_usd" in r for r in rows if r["provider"] == "claude"))

    def test_thresholds_and_visible_window(self):
        for p, amount in [(P.CODEX, 12_750_000), (P.CLAUDE, 3_400_000), (P.FAST, 4.25)]:
            self.assertEqual(self.ledger.state(p)["state"], "available")
            self.usage(p, **({"cost_usd": amount} if p == P.FAST else {"input": amount}))
            self.assertEqual(self.ledger.state(p)["state"], "over_threshold")
            self.assertEqual(self.ledger.state(p, no_new_work=.9)["state"], "available")
        self.assertEqual(self.ledger.state(P.CODEX, visible_window={"used": 1, "limit": 100})["state"], "available")
        self.assertEqual(self.ledger.state(P.CODEX, visible_window={"used": 86, "limit": 100})["state"], "over_threshold")

    def test_routing_allowances_are_used_by_ledger_queries(self):
        config.ROUTING_PATH.write_text(json.dumps({"allowances": {"fast": {"spend_usd": 2}}, "no_new_work": .5}))
        self.usage(P.FAST, cost_usd=1)
        self.assertEqual(self.ledger.state(P.FAST)["state"], "over_threshold")

    def test_actual_wp7_published_counters_are_cumulative(self):
        daemon = Daemon(self.stores, {}, lambda *_: None, 0)
        subscription = daemon.bus.subscribe()
        thread = self.threads[P.CODEX]
        session = SimpleNamespace(retired=False, thread=thread, token_baseline=0,
                                  cost_baseline=0, turn_id="turn")
        for value in (100, 170):
            daemon._record(session, Event(K.USAGE, thread.id, {"input": value}))
            record = subscription.get_nowait()
            self.assertEqual(record["data"]["input"], value)
            self.ledger.on_event(record)
        self.assertEqual(self.stores.threads.get(thread.id).tokens, 170)
        self.assertEqual(self.ledger.totals(provider=P.CODEX)["work_tokens"], 170)

    def test_cooling_health_and_recovery(self):
        self.error(P.CLAUDE, message="ordinary failure")
        self.assertEqual(self.ledger.state(P.CLAUDE)["state"], "available")
        self.error(P.CLAUDE, message="rate_limit_error")
        self.assertEqual(self.ledger.state(P.CLAUDE)["state"], "cooling")
        self.ledger.set_health(P.CLAUDE, False, "login expired")
        self.assertEqual(self.ledger.state(P.CLAUDE)["state"], "unavailable")
        self.assertIn("login expired", self.ledger.state(P.CLAUDE)["reason"])
        self.ledger.set_health(P.CLAUDE, True, "ready")
        self.assertEqual(self.ledger.state(P.CLAUDE)["state"], "cooling")
        self.now += 900
        self.assertEqual(self.ledger.state(P.CLAUDE)["state"], "available")
        self.error(P.CODEX, message="Codex model cooling", retry_at=self.now + 1800, attempts=2)
        self.now += 901
        self.assertEqual(self.ledger.state(P.CODEX)["state"], "cooling")
        self.now += 900
        self.assertEqual(self.ledger.state(P.CODEX)["state"], "available")
        self.error(P.FAST, provider_reported={"code": "capacity"})
        self.assertEqual(self.ledger.state(P.FAST)["state"], "cooling")

    def test_restart_offsets_replay_and_midnight(self):
        self.now = datetime(2026, 9, 16, 4, 59, 50, tzinfo=timezone.utc).timestamp()
        self.usage(P.CODEX, input=100)
        event = Event(K.USAGE, self.threads[P.FAST].id, {"cost_usd": 1})
        self.ledger.on_event(event, event_id="once")
        self.error(P.CODEX, retry_at=self.now + 1800)
        self.ledger = UsageLedger(self.stores, clock=lambda: self.now)
        self.ledger.on_event(event, event_id="once")
        self.assertEqual(self.ledger.totals(provider=P.FAST)["spend_usd"], 1)
        self.assertEqual(self.ledger.state(P.CODEX)["totals"]["work_tokens"], 100)
        self.now += 20
        self.usage(P.CODEX, input=130)
        self.assertEqual(self.ledger.state(P.CODEX)["totals"]["work_tokens"], 30)
        self.assertEqual(self.ledger.totals(task_id=self.task.id)["work_tokens"], 130)
        self.assertEqual(self.ledger.state(P.CODEX)["state"], "cooling")
        self.assertEqual({p.name for p in self.ledger.root.glob("*")}, {"2026-09-15.jsonl", "2026-09-16.jsonl"})
        self.assertEqual(self.ledger.state(P.FAST)["totals"]["spend_usd"], 0)

    def test_ceiling_kinds_and_status_fallback(self):
        self.assertIsNone(self.ledger.ceiling_hit(self.task))
        self.usage(P.CLAUDE, input=20, cost_usd=.8)
        self.usage(P.FAST, cost_usd=.2)
        for kind, limit in [("usd", 1), ("tokens", 20), ("hours", 1)]:
            self.task.ceilings = {kind: limit}
            self.task.status.elapsed_s = 3600
            self.assertIn(kind, self.ledger.ceiling_hit(self.task))
            self.task.ceilings[kind] = limit + 1
            self.assertIsNone(self.ledger.ceiling_hit(self.task))
        self.task.ceilings = {"hours": .5}
        self.task.status.elapsed_s = 0
        self.task.status.started = datetime.fromtimestamp(self.now - 1801, timezone.utc).isoformat()
        self.assertIn("hours", self.ledger.ceiling_hit(self.task))
        self.task.ceilings = {"tokens": 100}
        self.task.status.tokens = 100
        self.assertIn("tokens", self.ledger.ceiling_hit(self.task))

    def test_invalid_usage_does_not_write(self):
        for value in (-1, float("nan"), True, "1"):
            with self.assertRaises(ValueError):
                self.usage(P.FAST, cost_usd=value)
        self.assertFalse(self.ledger.root.exists())


if __name__ == "__main__":
    unittest.main()
