"""WS-0: the catch-all project is named "Unsorted" (owner answer O-4, plan D-12).

Run: PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/unsorted_check.py

Free and hermetic: a temp data directory, real loopback listeners on ephemeral
ports, fake brains. Nothing reads or writes the owner's state.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from archive_check import Base  # noqa: E402
from jarvis.v2 import projects as P  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.provider import Decision  # noqa: E402
from jarvis.v2.stores import StoreError, Stores  # noqa: E402


def rewrite(stores: Stores, project_id: str, **values) -> None:
    """Write a stored project behind the store's back, as a record saved before this
    rule (an owner's project named "Unsorted", a catch-all still named "Inbox")."""
    path = stores.projects.path(project_id)
    data = json.loads(path.read_text())
    data.update(values)
    path.write_text(json.dumps(data))


class Store(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.stores = Stores(self.root / "data")
        self.projects = self.stores.projects

    def legacy(self):
        found = self.projects.inbox()
        rewrite(self.stores, found.id, name="Inbox")
        return found

    def test_fresh_install_is_born_unsorted(self):
        self.assertEqual(self.projects.migrate_unsorted(), "none")
        found = self.projects.inbox()
        self.assertEqual((found.name, found.inbox, found.root), ("Unsorted", True, str(Path.home())))
        self.assertEqual(self.projects.migrate_unsorted(), "ok")
        self.assertEqual(self.projects.inbox().id, found.id)

    def test_existing_inbox_is_renamed_keeping_id_and_root(self):
        before = self.legacy()
        self.assertEqual(self.projects.get(before.id).name, "Inbox")
        self.assertEqual(self.projects.migrate_unsorted(), "renamed")
        after = self.projects.get(before.id)
        self.assertEqual((after.name, after.id, after.root, after.inbox),
                         ("Unsorted", before.id, before.root, True))
        # Idempotent: a second start changes nothing.
        self.assertEqual(self.projects.migrate_unsorted(), "ok")
        self.assertEqual(self.projects.get(before.id), after)

    def test_collision_keeps_inbox_and_never_renames_the_owners_project(self):
        before = self.legacy()
        mine = self.projects.create("Mine", str(self.root))
        rewrite(self.stores, mine.id, name="  UNSORTED ")
        with self.assertLogs("jarvis.v2.stores", "WARNING") as logged:
            self.assertEqual(self.projects.migrate_unsorted(), "collision")
        self.assertIn("another project is already named Unsorted", logged.output[0])
        self.assertEqual(self.projects.get(before.id).name, "Inbox")
        self.assertEqual(self.projects.get(mine.id).name, "  UNSORTED ")
        # Every start, the same answer; and the catch-all stays saveable (a Discord link).
        with self.assertLogs("jarvis.v2.stores", "WARNING"):
            self.assertEqual(self.projects.migrate_unsorted(), "collision")
        self.projects.save(replace(self.projects.get(before.id), discord_channel_id="123"))
        self.assertEqual(self.projects.get(before.id).name, "Inbox")

    def test_collision_counts_archived_projects(self):
        before = self.legacy()
        mine = self.projects.create("Mine", str(self.root))
        rewrite(self.stores, mine.id, name="unsorted", archived="2026-11-01T00:00:00+00:00")
        with self.assertLogs("jarvis.v2.stores", "WARNING"):
            self.assertEqual(self.projects.migrate_unsorted(), "collision")
        self.assertEqual(self.projects.get(before.id).name, "Inbox")

    def test_collision_resolves_on_a_later_start(self):
        before = self.legacy()
        mine = self.projects.create("Mine", str(self.root))
        rewrite(self.stores, mine.id, name="Unsorted")
        with self.assertLogs("jarvis.v2.stores", "WARNING"):
            self.projects.migrate_unsorted()
        rewrite(self.stores, mine.id, name="Elsewhere")      # the owner renamed theirs
        self.assertEqual(self.projects.migrate_unsorted(), "renamed")
        self.assertEqual(self.projects.get(before.id).name, "Unsorted")

    def test_catch_all_created_while_another_project_holds_the_name(self):
        mine = self.projects.create("Mine", str(self.root))
        rewrite(self.stores, mine.id, name="Unsorted")
        self.assertEqual(self.projects.inbox().name, "Inbox")

    def test_a_normal_project_cannot_take_the_reserved_name(self):
        for name in ("Unsorted", "unsorted", "  UNSORTED  "):
            with self.assertRaises(StoreError) as caught:
                self.projects.create(name, str(self.root))
            self.assertIn("reserved", str(caught.exception))
        mine = self.projects.create("Mine", str(self.root))
        with self.assertRaises(StoreError):
            self.projects.save(replace(mine, name="Unsorted"))
        self.assertEqual(self.projects.get(mine.id).name, "Mine")
        self.assertEqual(self.projects.list(), [mine])

    def test_an_older_project_with_the_name_stays_saveable(self):
        mine = self.projects.create("Mine", str(self.root))
        rewrite(self.stores, mine.id, name="Unsorted")
        self.projects.save(replace(self.projects.get(mine.id), always_ask=["x"]))

    def test_the_catch_all_cannot_be_given_other_names(self):
        found = self.projects.inbox()
        for name in ("Elsewhere", "Inbox"):               # Inbox only while another project holds Unsorted
            with self.assertRaises(StoreError):
                self.projects.save(replace(found, name=name))
        with self.assertRaises(StoreError):
            self.projects.create("Inbox", str(Path.home()), inbox=True)
        self.assertEqual(self.projects.get(found.id).name, "Unsorted")

    def test_names_taken_reserves_unsorted_not_inbox(self):
        taken = P.project_names_taken(self.stores)
        self.assertIn("Unsorted", taken)
        self.assertNotIn("Inbox", taken)
        self.assertEqual(P.unique_name("unsorted", taken), "unsorted (1)")

    # -- "Inbox" is the fallback name: never on two projects (review of #366, MED) --

    def test_inbox_is_reserved_when_the_fallback_would_be_needed(self):
        mine = self.projects.create("Mine", str(self.root))
        rewrite(self.stores, mine.id, name="Unsorted")        # an older project holds the name
        taken = P.project_names_taken(self.stores)
        self.assertIn("Inbox", taken)                         # the catch-all will be called Inbox
        with self.assertRaises(StoreError):
            self.projects.create("inbox", str(self.root))
        self.assertEqual(self.projects.inbox().name, "Inbox")
        self.assertEqual([p.name for p in self.projects.list() if p.name == "Inbox"], ["Inbox"])

    def test_ordinary_inbox_first_then_the_fallback_is_refused(self):
        mine = self.projects.create("Mine", str(self.root))
        rewrite(self.stores, mine.id, name="Unsorted")
        other = self.projects.create("Other", str(self.root))
        rewrite(self.stores, other.id, name="Inbox")          # written behind the API's back
        with self.assertRaises(StoreError):
            self.projects.inbox()                             # never a second "Inbox"
        with self.assertRaises(StoreError):
            self.projects.create("Inbox", str(Path.home()), inbox=True)
        self.assertEqual(len(self.projects.list(inbox=True)), 0)

    def test_fallback_first_then_an_ordinary_inbox_is_refused(self):
        self.legacy()
        mine = self.projects.create("Mine", str(self.root))
        rewrite(self.stores, mine.id, name="Unsorted")
        self.assertIn("Inbox", P.project_names_taken(self.stores))
        for name in ("Inbox", " INBOX "):
            with self.assertRaises(StoreError):
                self.projects.create(name, str(self.root))
        third = self.projects.create("Third", str(self.root))
        with self.assertRaises(StoreError):
            self.projects.save(replace(third, name="inbox"))

    def test_a_corrupt_neighbour_never_blocks_an_ordinary_name(self):
        # review of ff9095e5: the strict cross-project scan ran on every save.
        mine = self.projects.create("Mine", str(self.root))
        bad = self.projects.create("Broken", str(self.root))
        self.projects.path(bad.id).write_text("{")
        created = self.projects.create("Fresh", str(self.root))
        self.projects.save(replace(mine, name="Renamed"))
        self.projects.save(replace(self.projects.get(created.id), always_ask=["x"]))
        self.assertEqual(self.projects.get(mine.id).name, "Renamed")
        self.assertEqual(P.project_names_taken(self.stores).count("Unsorted"), 1)
        # A reservation-relevant save fails closed, deliberately: it cannot see the neighbour.
        with self.assertRaises(StoreError):
            self.projects.create("Inbox", str(self.root))
        with self.assertRaises(StoreError):
            self.projects.inbox()

    def test_inbox_is_free_for_ordinary_projects_otherwise(self):
        self.projects.inbox()                                 # Unsorted
        self.assertNotIn("Inbox", P.project_names_taken(self.stores))
        self.assertEqual(self.projects.create("Inbox", str(self.root)).name, "Inbox")


class Wire(Base):
    """The daemon: start migrates, and the PATCH guard stays keyed on the flag."""

    def restart(self):
        self.daemon.stop()
        self.daemon = Daemon(self.stores, self.providers, lambda *_: lambda *_: Decision.DENY, 0)
        self.daemon.start()
        self.addCleanup(self.daemon.stop)

    def test_daemon_start_migrates_once(self):
        self.assertEqual(self.stores.projects.list(inbox=True), [])       # fresh: nothing made at start
        found = self.stores.projects.inbox()
        rewrite(self.stores, found.id, name="Inbox")
        self.restart()
        self.assertEqual(self.stores.projects.get(found.id).name, "Unsorted")
        self.restart()
        self.assertEqual(self.stores.projects.get(found.id).name, "Unsorted")
        self.assertEqual(self.request("GET", "/projects/%s" % found.id)["name"], "Unsorted")

    def test_daemon_start_leaves_a_collision_alone(self):
        found = self.stores.projects.inbox()
        rewrite(self.stores, found.id, name="Inbox")
        rewrite(self.stores, self.other.id, name="unsorted")
        with self.assertLogs("jarvis.v2.stores", "WARNING"):
            self.restart()
        self.assertEqual(self.stores.projects.get(found.id).name, "Inbox")
        self.assertEqual(self.stores.projects.get(self.other.id).name, "unsorted")

    def test_a_corrupt_neighbour_does_not_block_http_edits(self):
        bad = self.stores.projects.create("Broken", str(self.repo_b))
        self.stores.projects.path(bad.id).write_text("{")
        made = self.request("POST", "/projects", {"name": "Fresh", "root": str(self.repo_b)}, 201)
        self.assertEqual(made["name"], "Fresh")
        self.request("PATCH", f"/projects/{self.other.id}", {"name": "Renamed"}, 200, owner=True)
        self.request("PATCH", f"/projects/{self.other.id}", {"profile": "ask"}, 200, owner=True)

    def test_post_projects_never_makes_a_second_inbox(self):
        rewrite(self.stores, self.other.id, name="Unsorted")
        made = self.request("POST", "/projects", {"name": "Inbox", "root": str(self.repo_b)}, 201)
        self.assertEqual(made["name"], "Inbox (1)")           # numbered, never a second Inbox
        self.assertEqual(self.stores.projects.inbox().name, "Inbox")

    def test_a_normal_project_cannot_be_created_or_renamed_to_unsorted_over_http(self):
        made = self.request("POST", "/projects", {"name": "Unsorted", "root": str(self.repo_b)}, 201)
        self.assertEqual(made["name"], "Unsorted (1)")
        renamed = self.request("PATCH", f"/projects/{self.other.id}", {"name": "unsorted"}, 200, owner=True)
        self.assertEqual(renamed["name"], "unsorted (2)")

    def test_patch_guard_still_refuses_name_and_root_on_the_catch_all(self):
        found = self.stores.projects.inbox()
        url = f"/projects/{found.id}"
        for body in ({"name": "Mine"}, {"name": "Unsorted"}, {"root": str(self.repo_b)},
                     {"name": "Mine", "root": str(self.repo_b)}):
            got = self.request("PATCH", url, body, 409, owner=True)
            self.assertIn("inbox name and root cannot change", json.dumps(got))
        after = self.stores.projects.get(found.id)
        self.assertEqual((after.name, after.root), ("Unsorted", str(Path.home())))
        # Other fields still edit.
        self.request("PATCH", url, {"profile": "ask"}, 200, owner=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
