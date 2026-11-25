"""Regression coverage for the learning digest's source and owner boundaries."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import http.client
import json
import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from jarvis.v3 import daemon, learning_digest, skill_store
from jarvis.v3.feed import Feed
from jarvis.v3.tools.skills import SkillTools
from jarvis.v3.vault import Vault

CHICAGO = ZoneInfo("America/Chicago")
DESCRIPTION = "Fixture description for a notes workflow."
BODY = "# Notes\n\nThe working folder contains the notes.\n"


def at(day, hour=20, minute=45, *, month=1):
    return datetime(2030, month, day, hour, minute, tzinfo=CHICAGO).timestamp()


class DigestCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}))
        self.store = skill_store.SkillStore(self.root / "skills", ledger=self.root / "skill-ledger")
        self.agent = SkillTools(self.store, session="codex:fixture-session")
        self.vault = Vault(self.root / "vault")
        self.state = self.root / "digest"
        self.reviewer = self.root / "reviewer"
        self.feed = Feed(self.root / "feed")
        self.ledger = self.root / "skill-ledger" / "ledger.jsonl"

    def run_digest(self, day, *, feed=None):
        return learning_digest.run(feed=self.feed if feed is None else feed, now=at(day),
                                   state_dir=self.state, store=self.store,
                                   vault=self.vault, reviewer_dir=self.reviewer)

    def make_skill(self, name):
        result = self.agent.skill_manage("create", name, description=DESCRIPTION, body=BODY)
        self.assertIn("Skill change saved.", result)
        return self.store.records()[-1]["id"]

    def add_memory(self, name):
        self.vault.add("facts", name, "Fixture description.", "The example is synthetic.", "owner")

    def labels(self, day):
        result = self.run_digest(day)
        self.assertEqual(result.outcome, "posted")
        return [line["label"] for line in self.feed.get(result.brief_id)["context"]]

    @staticmethod
    def append(path, row, *, complete=True):
        path.parent.mkdir(mode=0o700, exist_ok=True)
        with open(path, "ab") as stream:
            stream.write(json.dumps(row).encode() + (b"\n" if complete else b""))

    def test_torn_source_lines_wait_for_the_newline(self):
        self.make_skill("sort-notes")
        self.add_memory("sample-fact")
        self.assertEqual(self.run_digest(1).outcome, "baseline")

        self.make_skill("tidy-notes")
        with self.ledger.open("r+b") as stream:
            stream.seek(-1, os.SEEK_END)
            stream.truncate()
        logs = {
            self.ledger: None,
            self.ledger.parent / "curator.jsonl": {"skill": "sort-notes", "change": "stale", "idle_days": 17},
            self.vault.root / ".flags.jsonl": {"name": "facts/sample-fact", "rule": "imperative"},
            self.reviewer / "log.jsonl": {"outcome": "ok", "input_tokens": 321, "output_tokens": 27},
        }
        for path, row in logs.items():
            if row is not None:
                self.append(path, row, complete=False)
        self.add_memory("force-checkpoint")
        self.assertEqual(self.labels(2), ["Memory added: facts/force-checkpoint"])

        for path in logs:
            with path.open("ab") as stream:
                stream.write(b"\n")
        self.assertEqual(self.labels(3), [
            "Skill: tidy-notes created", "Memory flagged: facts/sample-fact (imperative)",
            "Curator: sort-notes stale (17 idle days)", "Reviewer: 1 runs, 321 in / 27 out tokens, 0 skipped",
        ])
        self.assertEqual(self.run_digest(4).outcome, "empty")

    def test_shorter_log_restarts_and_missing_ledger_id_rebaselines(self):
        self.make_skill("sort-notes")
        reviewer = self.reviewer / "log.jsonl"
        self.append(reviewer, {"outcome": "ok", "input_tokens": 10000, "padding": "x" * 400})
        self.assertEqual(self.run_digest(1).outcome, "baseline")

        self.make_skill("tidy-notes")
        self.ledger.write_bytes(self.ledger.read_bytes().splitlines(keepends=True)[-1])
        reviewer.write_text(json.dumps({"outcome": "ok", "input_tokens": 1}) + "\n")
        self.add_memory("force-checkpoint")
        self.assertEqual(self.labels(2), [
            "Memory added: facts/force-checkpoint", "Reviewer: 1 runs, 1 in / 0 out tokens, 0 skipped",
        ])

        self.make_skill("label-notes")
        self.append(reviewer, {"outcome": "error", "input_tokens": 2, "output_tokens": 3})
        self.assertEqual(self.labels(3), [
            "Skill: label-notes created", "Reviewer: 1 runs, 2 in / 3 out tokens, 0 skipped",
        ])

    def test_linked_and_wrong_owner_logs_give_no_rows(self):
        self.make_skill("sort-notes")
        self.assertEqual(self.run_digest(1).outcome, "baseline")
        self.add_memory("sample-fact")
        outside = self.root / "elsewhere" / "events.jsonl"
        self.append(outside, {"name": "facts/sample-fact", "rule": "imperative"})
        (self.vault.root / ".flags.jsonl").symlink_to(outside)
        reviewer = self.reviewer / "log.jsonl"
        self.append(reviewer, {"outcome": "ok", "input_tokens": 10})
        different_owner = reviewer.stat()
        original_fstat = os.fstat

        def wrong_owner(fd):
            info = original_fstat(fd)
            if os.path.samestat(info, different_owner):
                values = list(info)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return info

        with patch.object(learning_digest.os, "fstat", side_effect=wrong_owner):
            self.assertEqual(self.labels(2), ["Memory added: facts/sample-fact"])

    def test_contending_run_returns_while_the_post_is_still_open(self):
        self.assertEqual(self.run_digest(1).outcome, "baseline")
        self.make_skill("sort-notes")
        entered, release = Event(), Event()
        real_post = self.feed.post

        def held_post(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("The test feed was not released.")
            return real_post(*args, **kwargs)

        with patch.object(self.feed, "post", side_effect=held_post), ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.run_digest, 2)
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.run_digest, 2)
                self.assertEqual(second.result(2).outcome, "already")
            finally:
                release.set()
            self.assertEqual(first.result(5).outcome, "posted")
        self.assertEqual(len(self.feed.briefs("open")), 1)

    def test_spring_and_fall_clock_changes_preserve_local_schedule(self):
        for month, day in ((3, 10), (11, 3)):
            with self.subTest(month=month):
                previous_day = at(day - 1, month=month)
                self.assertFalse(learning_digest.due(at(day, 20, 29, month=month), last_run=previous_day))
                current = at(day, 20, 30, month=month)
                self.assertTrue(learning_digest.due(current, last_run=previous_day))
                self.assertFalse(learning_digest.due(at(day, 22, 0, month=month), last_run=current))

    def test_a_later_day_stays_claimed_after_clock_moves_back(self):
        self.assertEqual(self.run_digest(3).outcome, "baseline")
        prior = (self.state / "state.json").read_bytes()
        self.make_skill("sort-notes")
        self.assertFalse(learning_digest.due(at(2), last_run=at(3)))
        self.assertEqual(self.run_digest(2).outcome, "already")
        self.assertEqual((self.state / "state.json").read_bytes(), prior)
        self.assertEqual(self.labels(4), ["Skill: sort-notes created"])

    def test_unapplied_is_hidden_and_a_scan_flag_alone_is_not_taint(self):
        self.assertEqual(self.run_digest(1).outcome, "baseline")
        with patch.object(skill_store._Operation, "_publish", side_effect=OSError("interrupted")):
            self.assertIn("Error:", self.agent.skill_manage("create", "tidy-notes", description=DESCRIPTION, body=BODY))
        self.assertEqual(self.store.records(with_status=True)[0]["status"], "unapplied")

        original = skill_store.content_scan.scan

        def flagged(text, *, purpose):
            verdict = original(text, purpose=purpose)
            hit = skill_store.content_scan.Hit("fixture-flag", (0, 3), "flag")
            return skill_store.content_scan.Verdict((*verdict.hits, hit))

        with patch.object(skill_store.content_scan, "scan", flagged):
            self.make_skill("sort-notes")
        self.assertFalse(self.store.records()[-1]["tainted"])
        self.assertEqual(self.labels(2), ["Skill: sort-notes created"])

    def test_same_day_archive_and_readd_preserves_the_active_ref(self):
        self.add_memory("sample-fact")
        self.assertEqual(self.run_digest(1).outcome, "baseline")
        self.vault.archive("facts/sample-fact")
        self.add_memory("sample-fact")
        self.assertEqual(self.run_digest(2).outcome, "empty")
        self.assertEqual(json.loads((self.state / "state.json").read_bytes())["memory"], ["facts/sample-fact"])

    def test_old_checkpoint_scrubs_a_sensitive_ref_and_keeps_its_identity(self):
        name = "fake-legacy-vault-value-123x"
        ref = "facts/" + name
        self.add_memory(name)
        self.assertEqual(self.run_digest(1).outcome, "baseline")
        checkpoint = self.state / "state.json"
        self.assertEqual(json.loads(checkpoint.read_bytes())["memory"], [ref])
        with patch.dict(os.environ, {"L3_FIXTURE_API_KEY": name}):
            self.assertEqual(self.run_digest(1).outcome, "already")
            self.assertNotIn(name.encode(), checkpoint.read_bytes())
        self.assertEqual(self.run_digest(2).outcome, "empty")
        self.vault.archive(ref)
        self.assertEqual(self.labels(3), ["Memory: (name withheld)"])

    def test_long_memory_flag_and_hostile_curator_name_keep_the_post(self):
        self.make_skill("sort-notes")
        self.assertEqual(self.run_digest(1).outcome, "baseline")
        slug = "x" * 100
        self.vault.root.mkdir(mode=0o700)
        self.append(self.vault.root / ".flags.jsonl", {"name": "facts/" + slug, "rule": "imperative"})
        self.append(self.ledger.parent / "curator.jsonl",
                    {"skill": "\ud800\nfixture", "change": "stale", "idle_days": 17})
        result = self.run_digest(2)
        self.assertEqual(result.outcome, "posted")
        flag, curator = self.feed.get(result.brief_id)["context"]
        self.assertEqual(flag["ref"], "memory:facts/" + slug)
        self.assertLessEqual(len(flag["label"]), 120)
        self.assertTrue(flag["label"].startswith("Memory flagged: facts/"))
        self.assertTrue(flag["label"].endswith(" (imperative)"))
        self.assertEqual(curator, {"label": "Curator: (name withheld)", "ref": "jarvis:learning"})

    def test_digest_uses_vault_status_without_parsing_entries(self):
        self.add_memory("sample-fact")
        self.assertEqual(self.run_digest(1).outcome, "baseline")
        with patch.object(Vault, "_finish_archive", side_effect=OSError("rename interrupted")):
            self.vault.archive("facts/sample-fact")
        self.assertTrue((self.vault.root / "facts" / "sample-fact.md").is_file())
        self.add_memory("different-fact")
        with patch("jarvis.v3.vault._parse", side_effect=AssertionError("Full memory parse")):
            self.assertEqual(self.labels(2), ["Memory added: facts/different-fact",
                                              "Memory archived: facts/sample-fact"])

    def test_unreadable_memory_stays_present_and_empty_day_keeps_last_brief(self):
        self.assertEqual(self.run_digest(1).outcome, "baseline")
        self.add_memory("sample-fact")
        result = self.run_digest(2)
        self.assertEqual(result.outcome, "posted")
        (self.vault.root / "facts" / "sample-fact.md").write_bytes(b"\xff")
        self.assertEqual(self.run_digest(3).outcome, "empty")
        state = json.loads((self.state / "state.json").read_bytes())
        self.assertEqual(state["memory"], ["facts/sample-fact"])
        self.assertEqual(state["last_brief"]["id"], result.brief_id)
        self.assertIsNone(learning_digest.summary_line(now=at(3), state_dir=self.state))

    def test_owner_unpin_unlock_extra_key_and_store_contention(self):
        rid = self.make_skill("sort-notes")
        self.store.pin("sort-notes", True)
        self.store.lock("sort-notes", True)
        server = daemon.Daemon(port=0, data_dir=self.root, jobs=[])
        server.start()
        self.addCleanup(server.stop)
        host = f"127.0.0.1:{server.port}"

        def call(action, **fields):
            conn = http.client.HTTPConnection(host, timeout=5)
            try:
                conn.request("POST", f"/v3/owner/learned/{action}", json.dumps({"id": rid, **fields}),
                             {"Origin": f"http://{host}", "Content-Type": "application/json"})
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()

        self.assertEqual(call("pin", on=False, extra=True)[0], 400)
        with self.store._operation(initialize=False):
            self.assertEqual(call("lock", on=False), (503, {"error": "Service Unavailable"}))
        for action in ("pin", "lock"):
            self.assertEqual(call(action, on=False), (200, {"changed": True}))
            self.assertEqual(call(action, on=False), (200, {"changed": False}))


if __name__ == "__main__":
    unittest.main()
