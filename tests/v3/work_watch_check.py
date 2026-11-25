"""Focused checks for work-watch with a fake T3 and ledger."""

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v3 import feed, jobs, t3_client, work_watch
from w2_acceptance_check import Clock, FakeFeed, FakeLedger, FakeT3, thread


class Transitions(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.t3 = FakeT3()
        self.feed = feed.Feed(self.root / "feed")
        self.clock = Clock()

    def watch(self, ledger, **kwargs):
        return work_watch.Watcher(ledger=ledger, t3=self.t3, feed=self.feed,
                                  clock=self.clock, **kwargs)

    def test_asks_with_quotes_and_emoji_at_the_length_limit(self):
        prefix, suffix = f'{"p" * 32}: "', '" is waiting on you in T3'
        room = 120 - len(prefix) - len(suffix)
        for extra in (0, 1):
            with self.subTest(extra=extra):
                start = 'Fix "launch" 🚀 '
                title = start + "x" * (room - len(start) + extra)
                self.assertEqual(len(prefix + title + suffix), 120 + extra)
                row = thread(f"th-{extra}", project="p" * 32, title=title)
                ledger = FakeLedger([row])
                self.t3.set(row.thread_id, "waiting", True)
                with patch.object(self.feed, "post", wraps=self.feed.post) as post:
                    result = self.watch(ledger).run()
                expected_title = title[:room - 1] + "…" if extra else title
                self.assertEqual(post.call_args.args[0]["ask"], prefix + expected_title + suffix)
                self.assertEqual(result, work_watch.Result("ok", 1, 1, 1, 0))

    def test_t3_waiting_status_with_zero_pending_requests(self):
        row = thread("th-1", "running")
        ledger = FakeLedger([row])
        self.t3.states[row.thread_id] = t3_client._state(
            {"threadId": row.thread_id, "status": "waiting", "pendingRequestCount": 0,
             "title": "ignored title", "providerInstanceId": "fake", "model": "fake"},
            [],
        )
        with patch.object(self.feed, "post", wraps=self.feed.post) as post:
            self.assertEqual(self.watch(ledger).run(), work_watch.Result("ok", 1, 1, 1, 0))
        self.assertEqual(post.call_args.args[0]["rank"], 2)
        self.assertEqual(ledger.calls, [(row.thread_id, "waiting", True, self.clock.now)])

    def test_rolled_back_and_exported_take_precedence_over_waiting(self):
        rows = [thread(name, "running", project="example-app") for name in ("th-1", "th-2")]
        ledger = FakeLedger(rows)
        self.t3.set("th-1", "rolled_back", True)
        self.t3.set("th-2", "exported", True)
        watcher = self.watch(ledger)
        with patch.object(self.feed, "post", wraps=self.feed.post) as post:
            self.assertEqual(watcher.run(), work_watch.Result("ok", 2, 2, 2, 0))
            self.assertEqual(watcher.run(), work_watch.Result("ok", 0, 0, 0, 0))
        fields = [call.args[0] for call in post.call_args_list]
        self.assertEqual([(item["rank"], item["ask"]) for item in fields],
                         [(3, 'example-app: "UI audit" was rolled back in T3'),
                          (4, 'example-app: "UI audit" finished in T3')])

    def test_a_title_the_feed_refuses_still_tells_the_owner_without_it(self):
        row = thread("th-1", "running", title="Invalid\x00title")
        ledger = FakeLedger([row])
        self.t3.set(row.thread_id, "completed")
        watcher = self.watch(ledger)
        with self.assertRaises(feed.FeedError):
            self.feed.post({"kind": "news", "rank": 4, "ask": row.title})
        with patch.object(self.feed, "post", wraps=self.feed.post) as post:
            self.assertEqual(watcher.run(), work_watch.Result("ok", 1, 1, 1, 0))
            self.assertEqual(post.call_count, 2)  # the titled ask is refused, the plain one is posted
        (brief,) = self.feed.briefs()
        self.assertEqual(brief["ask"], f"{row.project}: a thread finished in T3")
        self.assertNotIn("Invalid", json.dumps(brief))
        self.assertEqual(len(ledger.calls), 1)

    def test_a_feed_that_refuses_everything_counts_once_per_run_and_records_nothing(self):
        row = thread("th-1", "running")
        ledger = FakeLedger([row])
        self.t3.set(row.thread_id, "completed")
        watcher = self.watch(ledger)
        with patch.object(self.feed, "post", side_effect=feed.FeedError("refused")) as post:
            for attempts in (1, 2):
                self.assertEqual(watcher.run(), work_watch.Result("ok", 1, 0, 0, 1))
                self.assertEqual(post.call_count, 2 * attempts)
                self.assertEqual(ledger.calls, [])

    def test_missing_t3_mid_run_discards_observed_changes_and_errors(self):
        ledger = FakeLedger([thread(f"th-{i}", "running") for i in range(4)])
        self.t3.set("th-0", "completed")
        self.t3.states["th-1"] = t3_client.T3Error("timeout")
        self.t3.states["th-2"] = t3_client.T3Error("missing")
        with patch.object(self.feed, "post", wraps=self.feed.post) as post:
            self.assertEqual(self.watch(ledger).run(), work_watch.Result("unavailable", 3, 0, 0, 0))
        post.assert_not_called()
        self.assertEqual(ledger.calls, [])
        self.assertEqual(self.t3.calls, [("th-0", 0), ("th-1", 0), ("th-2", 0)])


class Rotation(unittest.TestCase):
    def make_watch(self, count, **kwargs):
        names = [f"th-{i:02}" for i in range(count)]
        ledger = FakeLedger([thread(name, "running") for name in names])
        t3 = FakeT3()
        for name in names:
            t3.set(name, "running")
        watcher = work_watch.Watcher(ledger=ledger, t3=t3, feed=FakeFeed(), **kwargs)
        return watcher, ledger, t3, names

    def test_zero_one_and_forty_live_threads(self):
        for count, expected_polls in ((0, 0), (1, 1), (40, 12)):
            with self.subTest(count=count):
                watcher, _, t3, names = self.make_watch(count)
                for _ in range(4):
                    self.assertEqual(watcher.run(), work_watch.Result("ok", expected_polls, 0, 0, 0))
                self.assertEqual({name for name, _ in t3.calls}, set(names))
                self.assertEqual(t3.calls[:count], [(name, 0) for name in names])

    def test_cursor_keeps_its_place_when_polled_threads_become_final(self):
        watcher, ledger, t3, names = self.make_watch(7, max_per_run=3)
        watcher.run()
        for name in names[:3]:
            ledger.rows[name] = replace(ledger.get(name), status="completed")
        t3.calls.clear()
        self.assertEqual(watcher.run().polled, 3)
        self.assertEqual(t3.calls, [(name, 0) for name in names[3:6]])


class Wiring(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "work" / "threads.jsonl"
        self.path.parent.mkdir()
        self.ledger = FakeLedger([thread("th-1", "running")])
        self.t3 = FakeT3()
        self.t3.set("th-1", "running")
        work = ModuleType("jarvis.v3.work")
        self.Ledger = self.enterContext(patch.object(work, "Ledger", create=True, return_value=self.ledger))
        self.enterContext(patch.dict(sys.modules, {"jarvis.v3.work": work}))
        self.enterContext(patch.object(config, "V3_DIR", self.root))
        self.T3 = self.enterContext(patch.object(t3_client, "T3", return_value=self.t3))
        self.Watcher = self.enterContext(patch.object(work_watch, "Watcher", wraps=work_watch.Watcher))
        self.Feed = self.enterContext(patch.object(feed, "Feed", return_value=FakeFeed()))
        self.jobs = {job.name: job for job in jobs.default_jobs(feed_root=self.root / "feed")}
        self.run_job = self.jobs["work-watch"].run

    def test_lazy_construction_shared_feed_and_watcher_reuse(self):
        self.run_job()
        for constructor in (self.Ledger, self.T3, self.Feed, self.Watcher):
            constructor.assert_not_called()
        self.path.touch()
        with patch.object(self.Feed.return_value, "tick", create=True) as tick:
            self.jobs["feed"].run()
        tick.assert_called_once_with()
        with patch.object(self.ledger, "threads", wraps=self.ledger.threads) as read:
            self.run_job()
            self.run_job()
        self.assertEqual(read.call_count, 2)
        self.Ledger.assert_called_once_with(self.path)
        self.T3.assert_called_once_with(timeout=15)
        self.Feed.assert_called_once_with(self.root / "feed")
        self.Watcher.assert_called_once_with(ledger=self.ledger, t3=self.t3, feed=self.Feed.return_value)
        self.assertEqual(self.t3.calls, [("th-1", 0), ("th-1", 0)])

    def test_unreadable_ledger_raises_only_fixed_text(self):
        self.path.touch()
        with patch.object(self.ledger, "threads", side_effect=OSError("fixture private ledger text")):
            with self.assertRaisesRegex(RuntimeError, r"^Work watch failed\.$") as error:
                self.run_job()
        self.assertIsNone(error.exception.__context__)
        self.assertEqual(self.t3.calls, [])

    def test_unavailable_t3_returns_normally(self):
        self.path.touch()
        self.t3.states["th-1"] = t3_client.T3Error("missing")
        self.assertIsNone(self.run_job())
        self.assertEqual(self.ledger.calls, [])


if __name__ == "__main__":
    unittest.main()
