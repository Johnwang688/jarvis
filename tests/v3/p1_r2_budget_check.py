"""Check shared admission and crash accounting with local fixture ledgers."""

from importlib import util
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jarvis.v3 import budget, claude_jobs, consolidate
from jarvis.v3.budget import Budget
from jarvis.v3.claude_jobs import JobResult
from jarvis.v3.index import Index


NOW = 1_895_000_000



def _need(name):
    """Comparisons with main's files run only when the caller supplies them."""
    value = os.environ.get(name)
    if not value:
        raise unittest.SkipTest(f"set {name} to run this comparison with main's files")
    return value

class Crash(BaseException):
    pass


class Ledger(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.path = self.root / "budget.jsonl"

    def ledger(self, cls=Budget, *, path=None):
        return cls(self.path if path is None else path, daily_cap=2,
                   caps={"consolidate": 1}, now=lambda: NOW,
                   usage=lambda: [{"used_percent": 10, "resets_at": NOW + 3600}])

    def test_legacy_and_ticketed_spend_share_caps_after_reopen(self):
        spec = util.spec_from_file_location("jarvis.v3.p1_main_budget",
                                            Path(_need("P1_MAIN_FILES")) / "budget.py")
        main = util.module_from_spec(spec)
        with patch.dict(sys.modules, {spec.name: main}):
            spec.loader.exec_module(main)
        legacy = self.ledger(main.Budget)
        legacy.record(legacy.admit("reviewer"), JobResult("reviewer", "haiku"))
        current = self.ledger()
        ticket = current.admit("consolidate", start_model="sonnet")
        self.assertTrue(ticket.ok, ticket)
        self.assertFalse(self.ledger().admit("consolidate", start_model="sonnet").ok)
        self.assertFalse(self.ledger().admit("brief").ok)
        current.record(ticket, JobResult("consolidate", "sonnet", ok=True, outcome="ok"))
        self.assertEqual(self.ledger().used()["jobs"], {"reviewer": 1, "consolidate": 1})

    def test_reopen_before_start_during_start_and_during_result_write(self):
        write = os.write

        def torn(fd, line):
            write(fd, line[:len(line) // 2])
            raise Crash

        with patch.object(budget.os, "write", side_effect=Crash), self.assertRaises(Crash):
            self.ledger().admit("consolidate", start_model="sonnet")
        self.assertEqual(self.ledger().used()["total"], 0)
        with patch.object(budget.os, "write", torn), self.assertRaises(Crash):
            self.ledger().admit("consolidate", start_model="sonnet")
        self.assertEqual(self.ledger().used()["total"], 0)
        current = self.ledger()
        ticket = current.admit("consolidate", start_model="sonnet")
        self.assertTrue(ticket.ok, ticket)
        self.assertEqual(self.ledger().used()["total"], 1)
        with patch.object(budget.os, "write", torn), self.assertRaises(Crash):
            current.record(ticket, JobResult("consolidate", "sonnet"))
        restarted = self.ledger()
        self.assertFalse(restarted.admit("consolidate", start_model="sonnet").ok)
        restarted.record(restarted.admit("brief"), JobResult("brief", "haiku"))
        self.assertEqual(self.ledger().used()["jobs"], {"consolidate": 1, "brief": 1})

    def test_missing_cli_leaves_the_unstarted_job_slot_available(self):
        with Index(self.root / "index.sqlite") as index:
            index.add("gmail", "fixture", title="Fixture note", ts=NOW - 10,
                      messages=[("owner", "Fixture body.", NOW - 10)])
            with (patch.object(claude_jobs, "resolve_cli", return_value=None) as resolve,
                  patch.object(claude_jobs, "_capture", side_effect=AssertionError("unexpected subprocess"))):
                result = consolidate.run(index=index, budget=self.ledger(), now=NOW,
                                         state_dir=self.root / "state")
            resolve.assert_called_once()
        self.assertEqual(result.outcome, "error")
        self.assertEqual(self.ledger().used()["total"], 0)


if __name__ == "__main__":
    unittest.main()
