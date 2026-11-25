"""Orchestrator-owned acceptance tests for J2 (implementers: do not edit; if one is wrong, stop and say why).

Contract: the job loop runs what is merged. jobs.default_jobs() gains three jobs beside "feed" and "skill-curator":
  "transcripts"   every 300 s: jarvis.v3.ingest.transcripts.ingest(index, budget_s=<= 20) over the default session folders
  "gmail"         every 300 s: one ingest.Runner tick with a GmailIngester. Only when the saved Google grant exists and lists
                  gmail.readonly; otherwise the job does nothing and makes no request (no client is even constructed).
  "consolidate"   due = consolidate.due(now, last_run=...); runs consolidate.run(index=..., budget=Budget(usage=claude_usage.read_windows)).
                  needs_idle_s = 0 (it runs at 03:30 whether or not the owner is up).
  One Index per process for these jobs, opened lazily on first use and never at import or in Daemon.start().
  A job module that cannot be imported, a missing token file, a malformed token file: the job is a quiet no-op or an "error"
  outcome for that job only; the daemon starts and the other jobs run. No job here calls a model except consolidate, and
  consolidate only through consolidate.run (which asks the budget first).
  default_jobs(*, feed_root=None, index_path=None, google_token=None)   the two new parameters are for tests; defaults are
  config.V3_INDEX_DB (env JARVIS_INDEX_DB) and config.GOOGLE_TOKEN_PATH, read when the job runs.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.v3 import consolidate, jobs
from jarvis.v3.ingest import gmail as gmail_module
from jarvis.v3.ingest import transcripts

REPO = Path(__file__).resolve().parents[2]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.token = self.tmp / "google_token.json"
        self.index = self.tmp / "v3" / "index.sqlite"
        self.jobs = {job.name: job for job in jobs.default_jobs(feed_root=self.tmp / "feed", index_path=self.index, google_token=self.token)}

    def grant(self, scopes):
        self.token.write_text(json.dumps({"client_id": "fake-client", "client_secret": "fake-secret", "refresh_token": "fake-token", "scopes": scopes}))
        self.token.chmod(0o600)


class Table(Base):
    def test_names_and_schedules(self):
        self.assertLessEqual({"consolidate", "feed", "gmail", "skill-curator", "transcripts"}, set(self.jobs))   # later PRs add jobs
        self.assertEqual((self.jobs["transcripts"].every_s, self.jobs["gmail"].every_s), (300, 300))
        self.assertEqual(self.jobs["consolidate"].needs_idle_s, 0)
        self.assertIsNone(self.jobs["consolidate"].every_s)
        self.assertFalse(self.index.exists())                            # nothing opened by building the list

    def test_consolidate_follows_its_own_clock(self):
        seen = []
        with patch.object(consolidate, "due", lambda now, *, last_run: (seen.append((now, last_run)), True)[1]):
            self.assertTrue(self.jobs["consolidate"].due(1000.0, 5.0))
        self.assertEqual(seen, [(1000.0, 5.0)])


class Runs(Base):
    def test_transcripts(self):
        calls = []
        with patch.object(transcripts, "ingest", lambda index, **kw: calls.append((type(index).__name__, kw))):
            self.jobs["transcripts"].run()
            self.jobs["transcripts"].run()
        self.assertEqual([name for name, _ in calls], ["Index", "Index"])
        self.assertTrue(all(0 < kw["budget_s"] <= 20 for _, kw in calls))
        self.assertTrue(self.index.exists())

    def test_gmail_does_nothing_without_a_grant(self):
        with patch.object(gmail_module, "HttpGmail", side_effect=AssertionError("no client without a grant")), \
             patch.object(gmail_module, "GmailIngester", side_effect=AssertionError("no ingester without a grant")):
            self.jobs["gmail"].run()                                     # no token file
            self.grant(["https://www.googleapis.com/auth/calendar.events"])
            self.jobs["gmail"].run()                                     # a grant without gmail.readonly
            for junk in ("{", "[]", json.dumps({"scopes": "gmail.readonly"}), ""):
                self.token.write_text(junk)
                self.jobs["gmail"].run()

    def test_gmail_ticks_a_runner_with_the_grant(self):
        self.grant(["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send"])
        made, polls = [], []

        class FakeClient:
            def __init__(self, token, **kw):
                made.append(token)

        class FakeIngester:
            name = "gmail"

            def __init__(self, api, **kw):
                self.api = api

            def poll(self, cursor):
                polls.append(cursor)
                return [], {"at": len(polls)}
        with patch.object(gmail_module, "HttpGmail", FakeClient), patch.object(gmail_module, "GmailIngester", FakeIngester):
            self.jobs["gmail"].run()
            self.jobs["gmail"].run()                                     # the Runner's own interval decides whether it polls again
        self.assertEqual(len(made), 1)                                   # one client, reused
        self.assertTrue(callable(made[0]))                               # the token is fetched when needed, never held as a string
        self.assertEqual(polls, [None])

    def test_consolidate_asks_the_budget_through_its_own_entry(self):
        calls = []
        with patch.object(consolidate, "run", lambda **kw: calls.append(kw)):
            self.jobs["consolidate"].run()
        (kw,) = calls
        self.assertEqual(type(kw["index"]).__name__, "Index")
        self.assertEqual(type(kw["budget"]).__name__, "Budget")
        self.assertNotIn("run_job", kw)                                  # the real runner, chosen inside consolidate

    def test_one_broken_job_is_its_own_error(self):
        with patch.object(transcripts, "ingest", side_effect=RuntimeError("private text")):
            scheduler = jobs.Scheduler([self.jobs["transcripts"], self.jobs["feed"]], state=self.tmp / "state.json", idle=lambda: 10 ** 6)
            rows = scheduler.tick()
        self.assertEqual([(row["job"], row["outcome"]) for row in rows], [("transcripts", "error"), ("feed", "ok")])
        self.assertNotIn("private", json.dumps(rows))

    def test_agentd_still_starts_light(self):
        code = ("import sys\nfrom jarvis.v3 import daemon, jobs\njobs.default_jobs()\n"
                "heavy = sorted(m for m in sys.modules if m.split('.')[0] in ('httpx', 'openai', 'anthropic', 'sqlite3') or m in ('jarvis.llm', 'jarvis.agent', 'jarvis.tools', 'jarvis.v3.ingest.gmail', 'jarvis.v3.consolidate', 'jarvis.v3.index'))\nprint(heavy)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                             env={"PYTHONPATH": str(REPO), "HOME": str(self.tmp), "PATH": os.environ.get("PATH", "")})
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "[]"), out.stderr[-400:])


if __name__ == "__main__":
    unittest.main()
