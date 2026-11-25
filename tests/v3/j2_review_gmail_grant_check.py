"""J2 must authorize the same private bundle that supplies credentials."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from jarvis import config, google_auth
from jarvis.v3 import jobs
from jarvis.v3.index import Index


READONLY = "https://www.googleapis.com/auth/gmail.readonly"


class GmailGrantCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.token = self.root / "google_token.json"
        self.enterContext(patch.object(config, "GOOGLE_TOKEN_PATH", self.token))
        self.enterContext(patch.object(google_auth, "_cached_token", None))
        self.post = self.enterContext(patch.object(httpx, "post", side_effect=AssertionError))
        self.get = self.enterContext(patch.object(httpx.Client, "get", side_effect=AssertionError))

    def bundle(self, path, **fields):
        bundle = dict(client_id="fake-client", client_secret="fake-secret",
                      refresh_token="fake-refresh", scopes=[READONLY])
        path.write_text(json.dumps(bundle | fields))
        path.chmod(0o600)

    def tick(self, *, after_open=None, **options):
        original_init = Index.__init__

        def open_index(index, *args, **kwargs):
            original_init(index, *args, **kwargs)
            self.addCleanup(index.close)
            if after_open is not None:
                after_open()

        gmail = next(job for job in jobs.default_jobs(
            index_path=self.root / "index" / "index.sqlite", **options)
            if job.name == "gmail")
        with patch.object(Index, "__init__", open_index):
            jobs.Scheduler([gmail], state=self.root / "jobs" / "state.json").tick()

    def test_credential_file_must_match_checked_grant(self):
        grant = self.root / "checked.json"
        self.bundle(grant)
        self.bundle(self.token, scopes=["https://www.googleapis.com/auth/gmail.send"])
        self.tick(google_token=grant)
        self.assertEqual(self.post.call_count, 0, "OAuth used a bundle without Gmail read consent")
        self.assertEqual(self.get.call_count, 0)

    def test_malformed_bundle_does_not_construct_client_or_refresh(self):
        self.bundle(self.token, refresh_token=["fake-refresh"])
        with patch.object(httpx, "Client", side_effect=httpx.Client) as client:
            self.tick()
        self.assertEqual((client.call_count, self.post.call_count, self.get.call_count), (0, 0, 0),
                         "a non-string refresh token must invalidate the grant")

if __name__ == "__main__":
    unittest.main()
