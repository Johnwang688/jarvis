"""Offline edge cases for Calendar, schoolwork, GitHub, signals and CLI polling."""

from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import time
import unittest

from c3_acceptance_check import Base, FakeCalendar, FakeGh, FakeSw, cal_event, gh_pr, sw_item
from jarvis.v3 import signals
from jarvis.v3.ingest.calendar import CalendarIngester
from jarvis.v3.ingest.github import GH_ARGV, GitHubIngester
from jarvis.v3.ingest.proc import ProcError, run_bounded
from jarvis.v3.ingest.schoolwork import SchoolworkIngester


class CalendarEdges(Base):
    def test_all_day_bounds_across_dst(self):
        before_end = datetime.fromisoformat("2027-03-15T04:59:59Z").timestamp()
        self.now = before_end + 30 * 86400
        api = FakeCalendar([cal_event("weekend", "Example weekend", start={"date": "2027-03-13"},
                                      end={"date": "2027-03-15"})])
        ingester = CalendarIngester(api, signals_dir=self.sig, clock=lambda: self.now)
        docs, cursor = ingester.poll(None)
        self.assertEqual(docs[0].ts, datetime.fromisoformat("2027-03-13T06:00:00Z").timestamp())
        self.sub(self.items("calendar")["weekend"], {"start": "2027-03-13", "end": "2027-03-15", "all_day": 1})
        self.now += 1
        docs, cursor = ingester.poll(cursor)
        self.assertIn("weekend", self.items("calendar"))
        self.now += 1
        self.assertEqual(ingester.poll(cursor)[0], [])
        self.assertEqual(self.items("calendar"), {})

    def test_missing_end_and_recurring_exception_with_hidden_attendees(self):
        api = FakeCalendar([cal_event("no-end", "Incomplete example", end=None),
                            cal_event("series_20270120T150000Z", "Moved example", recurringEventId="series",
                                      attendees=[{"self": True}, {"resource": True}], attendeesOmitted=True)])
        ingester = CalendarIngester(api, signals_dir=self.sig, clock=lambda: self.now)
        self.drain(self.runner(ingester), 1)
        metas = self.metas()
        self.assertEqual(set(metas), {"series_20270120T150000Z"})
        self.sub(metas["series_20270120T150000Z"], {"attendees": 1, "recurring": 1})
        api.cancel("series_20270120T150000Z")
        self.drain(self.runner(ingester), 1)
        self.assertEqual(self.metas(), {})

    def test_410_during_incremental_paging_restarts_full(self):
        api = FakeCalendar([cal_event("a", "First"), cal_event("b", "Second"), cal_event("z", "Vanishing")], page=1)
        ingest = CalendarIngester(api, signals_dir=self.sig, clock=lambda: self.now)
        runner = self.runner(ingest)
        self.drain(runner, 3)
        api.put(cal_event("a", "First changed"))
        api.put(cal_event("b", "Second changed"))
        self.drain(runner, 1)
        self.assertEqual(self.items("calendar")["a"]["title"], "First changed")
        api.vanish("z")
        api.expire()
        mark = len(api.calls)
        self.drain(runner, 1)
        rejected, restart = api.calls[mark:]
        self.assertIsNotNone(rejected["page_token"])
        self.assertIsNotNone(rejected["sync_token"])
        self.assertIsNone(restart["sync_token"])
        self.assertIsNone(restart["page_token"])
        self.assertIn("z", self.items("calendar"))
        self.assertIn("z", self.metas())
        self.drain(runner, 1)
        self.assertEqual(set(self.metas()), {"a", "b"})
        self.assertEqual(self.items("calendar")["b"]["title"], "Second changed")
        self.assertNotIn("z", self.items("calendar"))
        self.assertEqual(restart["time_min"], api.calls[-1]["time_min"])

    def test_page_token_cycle_does_not_advance_or_publish(self):
        replies = iter([{"items": [], "nextPageToken": "a"}, {"items": [], "nextPageToken": "b"},
                        {"items": [cal_event("bad", "Never published")], "nextPageToken": "a"}])

        class Api:
            def list_events(self, *args, **kwargs):
                return next(replies)

        runner = self.runner(CalendarIngester(Api(), signals_dir=self.sig, clock=lambda: self.now))
        self.drain(runner, 2)
        checkpoint = self.index.get_ingest_cursor("calendar")
        before = (self.sig / "calendar.json").read_bytes()
        self.assertEqual(runner.tick()["calendar"]["error"], "ingest_error")
        self.assertEqual(self.index.get_ingest_cursor("calendar"), checkpoint)
        self.assertEqual((self.sig / "calendar.json").read_bytes(), before)
        self.assertEqual(self.metas(), {})


class SchoolworkEdges(Base):
    def test_string_and_integer_ids_merge_and_null_course_is_kept(self):
        cli = FakeSw()
        cli.prep["days"][0]["due"] = [sw_item("11", "Prep item", course=None)]
        cli.review["recentChanges"] = [sw_item(11, "Review copy")]
        runner = self.runner(SchoolworkIngester(cli, post=self.fail, signals_dir=self.sig))
        self.drain(runner, 1)
        self.assertEqual(self.items("schoolwork")["11"]["title"], "Prep item")
        self.assertIsNone(self.metas()["11"]["course"])
        cli.prep["days"][0]["due"][0]["id"] = 11
        self.assertEqual(runner.tick()["schoolwork"]["docs"], 0)

    def test_failed_post_retries_and_does_not_commit_docs(self):
        cli = FakeSw()
        cli.review["health"] = [{"code": "needs_login", "severity": "error"}]
        posted = []

        def post(fields):
            posted.append(fields)
            if len(posted) == 1:
                raise OSError("fake external error")

        ingest = SchoolworkIngester(cli, post=post, signals_dir=self.sig)
        runner = self.runner(ingest)
        self.assertEqual(runner.tick()["schoolwork"]["error"], "ingest_error")
        self.assertEqual(self.index.get_ingest_cursor("schoolwork"), (0, None))
        self.assertEqual(self.metas(), {})
        self.now += 301
        self.assertIsNone(runner.tick()["schoolwork"]["error"])
        self.assertTrue(self.metas())
        self.drain(runner, 2)
        self.assertEqual(len(posted), 2)
        self.assertEqual(posted[0], posted[1])

    def test_partial_output_cannot_start_or_clear_sign_in_episode(self):
        cli, posted = FakeSw(), []
        runner = self.runner(SchoolworkIngester(cli, post=posted.append, signals_dir=self.sig))
        snapshot = self.sig / "schoolwork.json"
        self.drain(runner, 1)

        def unchanged_on_failure(posts):
            before = snapshot.read_bytes()
            checkpoint = self.index.get_ingest_cursor("schoolwork")
            result = runner.tick()["schoolwork"]
            self.assertEqual((result["error"], result["docs"], result["deleted"]), ("ingest_error", 0, 0))
            self.assertEqual(snapshot.read_bytes(), before)
            self.assertEqual(self.index.get_ingest_cursor("schoolwork"), checkpoint)
            self.assertEqual(len(posted), posts)
            self.now = result["next"]

        expired = [{"code": "needs_login", "severity": "error"}]
        cli.review["health"] = expired
        overdue = cli.prep.pop("overdue")
        unchanged_on_failure(0)
        cli.prep["overdue"] = [*overdue, {"id": None}]
        unchanged_on_failure(0)
        cli.prep["overdue"] = overdue
        self.drain(runner, 1)
        self.assertEqual(len(posted), 1)

        cli.review["health"] = [{"code": "last_sync_failed", "severity": "error"}]
        unchanged_on_failure(1)
        cli.review["health"] = expired
        self.drain(runner, 1)
        self.assertEqual(len(posted), 1)
        cli.review["health"] = []
        self.drain(runner, 1)
        cli.review["health"] = expired
        self.drain(runner, 1)
        self.assertEqual(len(posted), 2)
        self.assertNotEqual(posted[0]["idempotency_key"], posted[1]["idempotency_key"])


class GitHubEdges(Base):
    def test_invalid_row_preserves_absent_pr_until_a_complete_search(self):
        repo = "example-org/demo"
        retained = gh_pr(repo, 1, "Existing PR")
        changed = gh_pr(repo, 2, "Another PR")
        cli = FakeGh([retained, changed])
        include_totals = True

        def listing(argv):
            status, text = cli(argv)
            if not include_totals:
                return status, text
            payload = json.loads(text)
            for search in payload["data"].values():
                search["issueCount"] = len(search["nodes"])
                search["pageInfo"] = {"hasNextPage": False, "endCursor": None}
            return status, json.dumps(payload)

        runner = self.runner(GitHubIngester(listing, signals_dir=self.sig, require_pagination=True))
        self.drain(runner, 1)
        changed["title"] = "Updated PR"
        cli.authored[:] = [changed, {}]
        result = runner.tick()["github"]
        self.assertEqual((result["docs"], result["deleted"]), (1, 0))
        self.assertEqual(set(self.metas()), {f"{repo}#1", f"{repo}#2"})
        self.assertEqual(set(self.items("github")), {f"{repo}#1", f"{repo}#2"})

        self.now += 301
        cli.authored.pop()
        include_totals = False
        self.assertEqual(runner.tick()["github"]["deleted"], 0)
        self.assertIn(f"{repo}#1", self.items("github"))

        self.now += 301
        include_totals = True
        result = runner.tick()["github"]
        self.assertEqual((result["docs"], result["deleted"]), (0, 1))
        self.assertEqual(set(self.metas()), {f"{repo}#2"})
        self.assertEqual(set(self.items("github")), {f"{repo}#2"})

    def test_readable_identity_with_missing_details_allows_deleting_an_absent_pr(self):
        repo = "example-org/demo.with.dots"
        retained = gh_pr(repo, 1, "Still open")
        absent = gh_pr(repo, 2, "No checks yet", checks=None)
        fake = FakeGh([retained, absent, {}])
        runner = self.runner(GitHubIngester(fake, signals_dir=self.sig))
        self.drain(runner, 1)
        self.assertEqual(self.items("github")[f"{repo}#2"]["checks"], "none")

        fake.authored[:] = [{"repository": retained["repository"], "number": retained["number"]}]
        result = runner.tick()["github"]
        self.assertEqual((result["docs"], result["deleted"]), (1, 1))
        self.assertEqual(set(self.metas()), {f"{repo}#1"})
        item, = self.items("github").values()
        self.assertEqual(item["id"], f"{repo}#1")
        self.assertTrue(all(item[key] is None for key in ("title", "draft", "checks", "review_decision")))

    def test_first_caps_query_and_each_alias(self):
        limits = list(map(int, re.findall(r"\bfirst:\s*(\d+)", GH_ARGV[0][-1])))
        self.assertEqual(len(limits), 2)
        self.assertTrue(all(0 < limit <= 100 for limit in limits))
        fake = FakeGh([gh_pr("example-org/demo", i, "Authored") for i in range(1, limits[0] + 2)],
                      [gh_pr("example-org/review", i, "Review") for i in range(1, limits[1] + 2)])
        self.drain(self.runner(GitHubIngester(fake, signals_dir=self.sig)), 1)
        items = self.items("github")
        self.assertEqual(len(items), sum(limits))
        self.assertNotIn(f"example-org/demo#{limits[0] + 1}", items)
        self.assertNotIn(f"example-org/review#{limits[1] + 1}", items)

    def test_searches_page_independently_and_reject_overlapping_pages(self):
        repo = "example-org/demo"
        first, second, third = [gh_pr(repo, n, f"Pull request {n}") for n in range(1, 4)]
        overlap = False
        calls = []

        def page(nodes, count, after=None):
            return {"nodes": nodes, "issueCount": count,
                    "pageInfo": {"hasNextPage": after is not None, "endCursor": after}}

        def cli(argv):
            calls.append(argv)
            if "authoredAfter=authored-next" in argv:
                self.assertIn("reviewAfter=review-next", argv)
                data = {"authored": page([first if overlap else second], 2),
                        "review": page([second], 3, "review-last")}
            elif "reviewAfter=review-last" in argv:
                self.assertIn("fetchAuthored=false", argv)
                data = {"review": page([third], 3)}
            else:
                data = {"authored": page([first], 2, "authored-next"),
                        "review": page([first], 3, "review-next")}
            return 0, json.dumps({"data": data})

        runner = self.runner(GitHubIngester(cli, signals_dir=self.sig))
        self.assertIsNone(runner.tick()["github"]["error"])
        self.assertEqual(len(calls), 3)
        items = self.items("github")
        self.assertEqual({key: (row["authored"], row["review_requested"]) for key, row in items.items()},
                         {f"{repo}#1": (1, 1), f"{repo}#2": (1, 1), f"{repo}#3": (0, 1)})
        checkpoint = self.index.get_ingest_cursor("github")
        self.now += 301
        overlap = True
        self.assertEqual(runner.tick()["github"]["error"], "ingest_error")
        self.assertEqual(self.index.get_ingest_cursor("github"), checkpoint)
        self.assertEqual(self.items("github"), items)


class SignalEdges(Base):
    def test_unwritable_directory_and_non_file_target(self):
        self.sig.mkdir(mode=0o700)
        self.sig.chmod(0o500)
        try:
            with self.assertRaises(PermissionError):
                signals.write("demo", [], directory=self.sig)
            self.assertEqual(list(self.sig.iterdir()), [])
        finally:
            self.sig.chmod(0o700)
        target = signals.write("demo", [], directory=self.sig)
        target.unlink()
        target.mkdir()
        with self.assertRaises(OSError):
            signals.write("demo", [], directory=self.sig)
        self.assertEqual(list(self.sig.iterdir()), [target])
        self.assertIsNone(signals.read("demo", directory=self.sig))

    def test_nine_mib_file_is_rejected(self):
        path = signals.write("demo", [], directory=self.sig)
        path.write_bytes(b'{"version":1,"source":"demo","items":[]}\n' + b" " * (9 * 1024 * 1024))
        self.assertIsNone(signals.read("demo", directory=self.sig))


class ProcEdges(Base):
    def test_timeout_applies_after_stdout_closes(self):
        code = "import os, time; os.close(1); time.sleep(30)"
        with self.assertRaisesRegex(ProcError, r"^Process timeout\.$"):
            run_bounded([sys.executable, "-c", code], timeout_s=1, max_bytes=4096)

    def test_grandchild_holding_stdout_is_killed_with_process_group(self):
        pidfile = self.tmp / "child.pid"
        code = ("import subprocess, sys; from pathlib import Path; "
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                "Path(sys.argv[1]).write_text(str(child.pid))")
        with self.assertRaisesRegex(ProcError, r"^Process timeout\.$"):
            run_bounded([sys.executable, "-c", code, os.fspath(pidfile)], timeout_s=2, max_bytes=4096)
        stat = Path(f"/proc/{int(pidfile.read_text())}/stat")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if stat.read_text().rsplit(")", 1)[-1].split()[0] == "Z":
                    break
            except FileNotFoundError:
                break
            time.sleep(0.01)
        else:
            self.fail("grandchild is still alive")


if __name__ == "__main__":
    unittest.main()
