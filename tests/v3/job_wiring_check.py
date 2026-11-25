"""Failure, concurrency, and offline coverage for the daemon's J2 jobs."""

from datetime import datetime
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx

from jarvis import config, google_auth
from jarvis.v3 import consolidate, daemon, jobs
from jarvis.v3.index import Index
from jarvis.v3.ingest import gmail, transcripts


GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class JobWiringCheck(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}))
        self.data = self.home / "v3"
        self.index = self.data / "index.sqlite"
        self.token = self.home / "google_token.json"
        self.enterContext(patch.object(config, "GOOGLE_TOKEN_PATH", self.token))
        self.state = self.data / "jobs" / "state.json"
        self.now = 1_800_000_000

    def entries(self):
        return {job.name: job for job in jobs.default_jobs(
            feed_root=self.data / "feed", index_path=self.index, google_token=self.token)}

    def scheduler(self, *entries):
        return jobs.Scheduler(entries, state=self.state, clock=lambda: self.now)

    def write_grant(self, path):
        path.write_text(json.dumps({"client_id": "fake-client", "client_secret": "fake-secret",
                                    "refresh_token": "fake-token", "scopes": [GMAIL_SCOPE]}))
        path.chmod(0o600)

    def test_index_open_failure_is_retried_and_index_is_shared(self):
        self.index.mkdir(parents=True)
        entries = self.entries()
        scheduler = self.scheduler(entries["transcripts"], entries["feed"])
        with (patch.object(transcripts, "ingest") as ingest,
              patch.object(consolidate, "run") as consolidate_run,
              patch.object(gmail, "HttpGmail"),
              patch.object(gmail.GmailIngester, "poll_indexed", return_value=([], {})) as poll):
            self.assertEqual({row["job"]: row["outcome"] for row in scheduler.tick()},
                             {"transcripts": "error", "feed": "ok"})
            ingest.assert_not_called()
            self.index.rmdir()
            self.now += 59
            self.assertEqual(scheduler.tick(), [])
            self.now += 1
            self.assertEqual({row["job"]: row["outcome"] for row in scheduler.tick()},
                             {"transcripts": "ok", "feed": "ok"})
            shared = ingest.call_args.args[0]
            self.addCleanup(shared.close)
            self.assertIsInstance(shared, Index)
            self.write_grant(self.token)
            entries["gmail"].run()
            entries["consolidate"].run()
            self.assertIs(poll.call_args.args[1], shared)
            self.assertIs(consolidate_run.call_args.kwargs["index"], shared)
            self.assertEqual(scheduler.status()["transcripts"]["failures"], 0)

    def test_unsafe_grants_do_not_construct_the_client(self):
        target = self.home / "fake-grant.json"
        self.write_grant(target)
        gmail_job = self.entries()["gmail"]
        with patch.object(gmail, "HttpGmail") as client:
            self.token.symlink_to(target)
            gmail_job.run()
            self.token.unlink()
            self.token.hardlink_to(target)
            gmail_job.run()
            self.token.unlink()
            self.write_grant(self.token)
            self.token.chmod(0o640)
            gmail_job.run()
            client.assert_not_called()
        self.assertFalse(self.index.exists())

    def test_runner_error_surfaces_without_mail_text_and_grant_is_rechecked(self):
        self.write_grant(self.token)
        private_text = "example.invalid fake mail body"
        entries = self.entries()
        scheduler = self.scheduler(entries["gmail"], entries["feed"])
        with (patch.object(google_auth, "access_token", return_value="fake-token") as token,
              patch.object(gmail, "HttpGmail") as client,
              patch.object(gmail.GmailIngester, "poll_indexed",
                           side_effect=RuntimeError(private_text)) as poll):
            rows = scheduler.tick()
            self.assertEqual({row["job"]: row["outcome"] for row in rows},
                             {"gmail": "error", "feed": "ok"})
            client.assert_called_once_with(token)
            token.assert_not_called()
            poll.assert_called_once()
            self.addCleanup(poll.call_args.args[1].close)
            self.assertEqual(scheduler.status()["gmail"]["failures"], 1)
            self.assertNotIn(private_text, json.dumps(rows))
            self.assertNotIn(private_text, json.dumps(scheduler.status()))
            self.assertNotIn(private_text, self.state.read_text())
            self.token.unlink()
            self.now += 600
            with patch("jarvis.v3.ingest.Runner.tick") as tick:
                scheduler.tick()
                tick.assert_not_called()

    def test_overlapping_ticks_do_not_duplicate_ingestion(self):
        active, release = threading.Event(), threading.Event()

        def ingest(index, *, budget_s):
            active.set()
            if not release.wait(5):
                raise TimeoutError

        entries = self.entries()
        scheduler = self.scheduler(entries["transcripts"], entries["feed"])
        with patch.object(transcripts, "ingest", side_effect=ingest) as ingest_mock:
            rows = []
            worker = threading.Thread(target=lambda: rows.extend(scheduler.tick()))
            worker.start()
            try:
                self.assertTrue(active.wait(5))
                self.assertEqual(scheduler.tick(), [])
                ingest_mock.assert_called_once()
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.addCleanup(ingest_mock.call_args.args[0].close)
        self.assertEqual({row["job"]: row["outcome"] for row in rows},
                         {"transcripts": "ok", "feed": "ok"})

    def test_daemon_full_defaults_start_tick_and_stop_offline(self):
        names = {"feed", "skill-curator", "transcripts", "reviewer", "gmail", "calendar",
                 "schoolwork", "github", "triggers", "work-watch", "consolidate", "morning-brief",
                 "learning-digest", "evening-wrap"}
        due_names = {"feed", "transcripts", "reviewer", "gmail", "calendar",
                     "schoolwork", "github", "triggers", "work-watch", "consolidate"}
        no_network = AssertionError("unexpected outbound request")
        with (patch.multiple(config, V3_DIR=self.data, V3_INDEX_DB=self.index,
                             GOOGLE_TOKEN_PATH=self.token, SESSIONS_DIR=self.home / "sessions"),
              patch.object(socket, "create_connection", side_effect=no_network) as connect,
              patch.object(httpx, "Client", side_effect=no_network) as http_client,
              patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected subprocess")) as popen,
              patch.object(google_auth, "access_token", side_effect=no_network) as token):
            server = daemon.Daemon(port=0)
            scheduler = server._scheduler
            self.assertEqual(set(scheduler.status()), names)
            self.assertFalse(self.index.exists())
            scheduler._clock = lambda: datetime(2026, 11, 24, 3, 30,
                                                 tzinfo=ZoneInfo("America/Chicago")).timestamp()
            scheduler._tick_s = 0.01
            finished = threading.Event()
            real_tick = scheduler._tick

            def tick(stop):
                real_tick(stop)
                finished.set()

            with patch.object(scheduler, "_tick", side_effect=tick):
                try:
                    server.start()
                    self.assertNotEqual(server.port, 0)
                    self.assertTrue(finished.wait(5))
                    status = scheduler.status()
                    for name in due_names:
                        self.assertEqual(status[name]["last_outcome"], "ok", name)
                finally:
                    server.stop()
            self.assertFalse(scheduler._thread.is_alive())
            connect.assert_not_called()
            http_client.assert_not_called()
            token.assert_not_called()
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
