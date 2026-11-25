"""Fixture-only regression checks for deterministic skill curation."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timezone
import fcntl
from io import StringIO
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from threading import Barrier, Event
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.__main__ import main
from jarvis.v3 import skill_store
from jarvis.v3.skill_curator import Curator, DAY, due
from jarvis.v3.skill_store import SkillStore
from jarvis.v3.tools.skills import SkillTools


NAME = "release-guide"
EPOCH = 1_800_000_000
BODY = "# Release\n\nRun the checks.\n"


class Interrupted(RuntimeError):
    """Simulate termination at a durable write boundary."""


class CuratorChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.home),
            "JARVIS_LEARNED_SKILLS": str(self.tmp / "state" / "skills"),
        }))
        self.enterContext(patch.object(config, "V3_DIR", self.tmp / "state"))
        self.enterContext(patch.object(config, "REPO_ROOT", self.tmp / "repo"))
        self.root = config.V3_DIR / "skills"
        self.ledger = config.V3_DIR / "skill-ledger"
        self.links = [self.home / f".{consumer}" / "skills" / NAME for consumer in ("claude", "codex")]
        self.usage = self.root / ".usage.json"
        self.digest = self.ledger / "curator.jsonl"
        self.store = SkillStore()
        self.tools = SkillTools(self.store, session="fixture")
        with patch.object(skill_store, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.fromtimestamp(EPOCH, timezone.utc)
            result = self.tools.skill_manage("create", NAME, description="Release steps.", body=BODY)
        self.assertTrue(result.startswith("Skill change saved."), result)

    def run_at(self, days, **kwargs):
        return Curator(self.store).run(EPOCH + days * DAY, **kwargs)

    def view_at(self, days):
        with patch.object(skill_store.time, "time", return_value=EPOCH + days * DAY):
            self.assertIn(BODY, self.tools.skill_view(NAME))

    def assert_live(self):
        self.assertTrue((self.root / NAME / "SKILL.md").is_file())
        self.assertTrue(all(link.is_symlink() for link in self.links))

    def snapshot(self):
        entries = {}
        for path in self.tmp.rglob("*"):
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                data = os.readlink(path)
            else:
                data = path.read_bytes() if path.is_file() else None
            entries[path.relative_to(self.tmp)] = (mode, data)
        return entries

    def test_crash_after_ledger_before_publish(self):
        with patch.object(skill_store._Operation, "_publish", side_effect=Interrupted):
            with self.assertRaises(Interrupted):
                self.run_at(31)
        self.assert_live()
        self.assertFalse(self.digest.exists())
        self.assertEqual(self.store.records(with_status=True)[-1]["status"], "unapplied")

        lines = self.run_at(31)
        self.assertEqual([line["change"] for line in lines], ["archived"])
        self.assertEqual(self.run_at(31), [])
        receipts = [row["status"] for row in self.store.records(with_status=True) if row["op"] == "archive"]
        self.assertEqual(receipts, ["unapplied", "applied"])
        self.assertEqual([json.loads(self.digest.read_text())], lines)

    def test_crash_after_publish_before_consumer_unlink(self):
        with patch.object(skill_store._Operation, "_sync_links", side_effect=Interrupted):
            with self.assertRaises(Interrupted):
                self.run_at(31)
        self.assertFalse((self.root / NAME).exists())
        self.assertTrue(all(link.is_symlink() for link in self.links))
        self.assertFalse(self.digest.exists())

        lines = self.run_at(31)
        self.assertEqual([line["change"] for line in lines], ["archived"])
        self.assertTrue(all(not link.is_symlink() for link in self.links))
        self.assertEqual(self.run_at(31), [])
        self.assertEqual([json.loads(self.digest.read_text())], lines)
        self.assertEqual([row["op"] for row in self.store.records()], ["create", "archive"])

    def test_crash_after_digest_before_final_state(self):
        write = skill_store._Operation.write_sidecar

        def interrupt_final_state(op, name, data, expected, **kwargs):
            if name == ".curator.json" and self.digest.exists():
                raise Interrupted
            return write(op, name, data, expected, **kwargs)

        with patch.object(skill_store._Operation, "write_sidecar", interrupt_final_state):
            with self.assertRaises(Interrupted):
                self.run_at(31)
        original = self.digest.read_bytes()
        self.assertEqual(len(original.splitlines()), 1)

        self.assertEqual(self.run_at(31), [json.loads(original)])
        self.assertEqual(self.digest.read_bytes(), original)
        self.assertEqual(self.run_at(31), [])

    def test_two_curators_record_one_archive(self):
        starting = Barrier(2)

        def references():
            starting.wait(timeout=5)
            return ()

        def curate():
            return Curator(self.store, referenced=references).run(EPOCH + 31 * DAY)

        with ThreadPoolExecutor(max_workers=2) as workers:
            runs = [workers.submit(curate) for _ in range(2)]
            lines = [line for run in runs for line in run.result(timeout=10)]
        self.assertEqual([line["change"] for line in lines], ["archived"])
        self.assertEqual([json.loads(self.digest.read_text())], lines)
        self.assertEqual([row["op"] for row in self.store.records()], ["create", "archive"])

    def test_archive_by_another_actor_during_reference_collection(self):
        entered, archived = Event(), Event()

        def references():
            entered.set()
            if not archived.wait(timeout=5):
                raise TimeoutError("Fixture archive did not finish.")
            return ()

        curator = Curator(self.store, referenced=references)
        with ThreadPoolExecutor(max_workers=1) as workers:
            run = workers.submit(curator.run, EPOCH + 31 * DAY)
            try:
                self.assertTrue(entered.wait(timeout=5))
                result = self.tools.skill_manage("archive", NAME)
                self.assertTrue(result.startswith("Skill change saved."), result)
            finally:
                archived.set()
            self.assertEqual(run.result(timeout=10), [])
        self.assertFalse(self.digest.exists())
        self.assertEqual([(row["op"], row["actor"]) for row in self.store.records()],
                         [("create", "reviewer"), ("archive", "reviewer")])

    def test_clock_regression_keeps_usage_and_curator_progress(self):
        self.assertEqual([line["change"] for line in self.run_at(15)], ["stale"])
        self.view_at(16)
        self.view_at(12)
        self.assertEqual(json.loads(self.usage.read_text())[NAME],
                         {"count": 2, "last_used": EPOCH + 16 * DAY})
        snapshot = self.snapshot()
        self.assertEqual(self.run_at(13), [])
        self.assertEqual(self.snapshot(), snapshot)
        self.assertFalse(due(EPOCH + 13 * DAY, last_run=EPOCH + 15 * DAY, idle_s=10_800))
        self.assertEqual([line["change"] for line in self.run_at(17)], ["fresh"])
        self.assert_live()

    def test_oversized_usage_is_ignored_without_a_write(self):
        with self.usage.open("wb") as file:
            file.truncate(8 * 1024 * 1024)
        before = self.snapshot()
        self.view_at(1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.run_at(31), [])
        self.assertEqual(self.snapshot(), before)
        self.assert_live()

    def test_invalid_usage_names_are_rejected_and_replaced_on_view(self):
        for name in ("caf\u00e9", "two words", "../elsewhere", "folder\\name", "odd\u202ename", "\n", "\ud800"):
            with self.subTest(name=name):
                raw = json.dumps({name: {"count": 1, "last_used": EPOCH}})
                self.usage.write_text(raw)
                self.assertEqual(self.run_at(31), [])
                self.assertEqual(self.usage.read_text(), raw)
                self.view_at(1)
                self.assertEqual(json.loads(self.usage.read_text()),
                                 {NAME: {"count": 1, "last_used": EPOCH + DAY}})
                self.assert_live()

    def test_torn_ledger_leaves_everything_untouched(self):
        with (self.ledger / "ledger.jsonl").open("ab") as file:
            file.write(b'{"id":')
        before = self.snapshot()
        self.assertEqual(self.run_at(31), [])
        self.assertEqual(self.snapshot(), before)

    def test_cli_dry_run_only_prints_the_preview(self):
        before = self.snapshot()
        with patch("jarvis.v3.skill_curator.time.time", return_value=EPOCH + 31 * DAY):
            preview = StringIO()
            with patch.object(sys, "argv", ["jarvis", "learned", "curate", "--dry-run"]), redirect_stdout(preview):
                self.assertEqual(main(), 0)
            line = json.loads(preview.getvalue())
            self.assertEqual((line["skill"], line["change"], line["idle_days"]),
                             (NAME, "archived", 31))
            self.assertEqual(self.snapshot(), before)
            with patch.object(sys, "argv", ["jarvis", "learned", "curate"]), redirect_stdout(StringIO()) as applied:
                self.assertEqual(main(), 0)
        self.assertEqual(json.loads(applied.getvalue()), line)
        self.assertEqual(json.loads(self.digest.read_text()), line)

    def test_agent_tools_cannot_archive_as_curator(self):
        tools = SkillTools(self.store, session="fixture", actor="curator")
        self.assertTrue(tools.skill_manage("archive", NAME).startswith("Error:"))
        self.assert_live()
        self.assertEqual([row["op"] for row in self.store.records()], ["create"])

    def test_usage_replacement_holds_store_lock(self):
        write = skill_store._Operation.write_sidecar
        checked = []

        def check_lock(op, name, data, expected, **kwargs):
            if name == ".usage.json":
                # A separate open file description must conflict even in this process.
                with (self.root / ".store.lock").open("rb") as contender:
                    try:
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        checked.append(True)
                    else:
                        checked.append(False)
            return write(op, name, data, expected, **kwargs)

        with patch.object(skill_store._Operation, "write_sidecar", check_lock):
            self.view_at(1)
        self.assertEqual(checked, [True], "Usage replacement must hold the store lock.")
        self.assertEqual(json.loads(self.usage.read_text())[NAME]["count"], 1)

    def test_usage_write_error_does_not_fail_the_view(self):
        with patch.object(skill_store._Operation, "write_sidecar", side_effect=OSError):
            self.view_at(1)
        self.assertFalse(self.usage.exists())
        self.view_at(2)
        self.assertEqual(json.loads(self.usage.read_text())[NAME]["count"], 1)


if __name__ == "__main__":
    unittest.main()
