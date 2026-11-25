"""B1 store recovery, concurrency and clock checks with fake callbacks."""

from datetime import UTC, datetime, timedelta
import json
import multiprocessing
import os
from pathlib import Path
import stat
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from jarvis.v3 import feed as feed_module
from jarvis.v3.feed import Feed, FeedError
from jarvis.v3.tools.feed import FeedTools


T0 = datetime(2031, 1, 10, 14, tzinfo=UTC)
NEWS = {"kind": "news", "rank": 3, "ask": "The example build finished."}


def decision(**fields):
    return {
        **NEWS, "kind": "decision", "default": {"action": "none", "at": None}, **fields,
    }


def post_in_process(root, barrier, results):
    tool = FeedTools(Feed(root, clock=lambda: T0), session="shared-session")
    barrier.wait(timeout=10)
    for _ in range(4):
        results.put(tool.brief_post("news", NEWS["ask"]))


def crash_after_append(root, bid):
    def crash(*args):
        os._exit(19)

    def executed(proposal):
        os._exit(20)

    feed_module._replace_index = crash
    Feed(root, clock=lambda: T0, execute=executed).answer(bid, "approve")


class Quiet:
    def __init__(self, until):
        self.until = until

    def is_quiet(self, now):
        return now < self.until

    def ends_at(self, now):
        return self.until if self.is_quiet(now) else None


class FeedCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "feed"
        self.log = self.root / "briefs.jsonl"
        self.index = self.root / "index.json"
        self.now = T0
        self.feed = self.open()

    def open(self, **kwargs):
        return Feed(self.root, clock=lambda: self.now, **kwargs)

    def assert_modes(self):
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual({file.name for file in self.root.iterdir()}, {self.log.name, self.index.name})
        for file in self.root.iterdir():
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)

    def test_two_processes_share_the_session_cap_and_preserve_all_committed_posts(self):
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        results = ctx.Queue()
        processes = [ctx.Process(target=post_in_process, args=(self.root, barrier, results))
                     for _ in range(2)]
        self.assertEqual(self.feed.briefs(), [])  # Warm a third reader before either process writes.
        try:
            for process in processes:
                process.start()
            replies = [results.get(timeout=15) for _ in range(8)]
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(replies.count("Brief posted."), 5)
            rows = self.feed.briefs()
            self.assertEqual(len(rows), 5)
            self.assertEqual(len({row["id"] for row in rows}), 5)
            self.assertEqual(rows, self.open().briefs())
        finally:
            for process in processes:
                if process.pid is not None and process.is_alive():
                    process.terminate()
                    process.join(5)
            results.close()
        self.assert_modes()

    def test_crash_between_log_and_index_keeps_approval_and_default(self):
        ran = []
        self.feed = self.open(execute=ran.append)
        approved = self.feed.post(decision(proposal_id="example-proposal"))["id"]
        defaulted = self.feed.post(decision(default={"action": "none", "at": T0.isoformat()}))["id"]
        before = self.index.read_bytes()
        self.now += timedelta(seconds=1)
        # Once initialized, the index is unchanged until after an append.
        ctx = multiprocessing.get_context("spawn")
        process = ctx.Process(target=crash_after_append, args=(self.root, approved))
        process.start()
        try:
            process.join(10)
            self.assertEqual(process.exitcode, 19)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(5)
        self.assertEqual(self.index.read_bytes(), before)
        self.assertEqual(ran, [])
        fresh = self.open(execute=ran.append)
        self.assertEqual(fresh.get(approved)["state"], "approved")
        self.assertNotEqual(self.index.read_bytes(), before)
        with self.assertRaises(FeedError):
            fresh.answer(approved, "approve")
        with patch.object(feed_module, "_replace_index", side_effect=SystemExit(19)):
            with self.assertRaises(SystemExit):
                fresh.tick()
        rebuilt = self.open()
        self.assertEqual(rebuilt.get(defaulted)["resolved_by"], "default")
        self.assertEqual(rebuilt.tick()["defaulted"], [])
        self.assertEqual(ran, [])

    def test_garbage_wrong_shapes_and_complete_but_unterminated_event(self):
        bid = self.feed.post(decision())["id"]
        answer = {"event": "answer", "id": bid, "answer": {"action": "approve", "text": None}}
        with self.log.open("ab") as log:
            log.write(b"garbage\n\xff\nnull\n[]\n")
            log.write(b'{"event":"unknown",' + json.dumps(answer).encode()[1:] + b"\n")
            for bad in (
                {"event": "answer", "id": bid, "answer": ["approve"]},
                {**answer, "unknown": False},
                {"event": "expire", "id": bid, "at": "bad"},
                {"event": "post", "id": "0" * 32, "pending": False, "brief": {
                    **NEWS, "created": None, "route": "batch", "session": None, "actor": "jarvis"}},
                {"event": "feedback", "id": bid, "tag": {"too_long": True}},
            ):
                log.write(json.dumps(bad).encode() + b"\n")
            log.write(json.dumps(answer).encode())
        self.index.write_text('{"version":1,"counts":{"approved":999}}')
        self.assertEqual(self.feed.get(bid)["state"], "open")
        newer = self.feed.post(NEWS)["id"]
        self.index.unlink()
        reopened = self.open()
        self.assertEqual({row["id"] for row in reopened.briefs()}, {bid, newer})
        self.assertEqual(json.loads(self.index.read_text())["counts"]["open"], 2)
        self.assertEqual(reopened.briefs("approved"), [])
        self.assert_modes()

    def test_due_today_uses_the_clock_zone_including_dst_offset_changes(self):
        zone = ZoneInfo("America/New_York")
        cases = [
            # At the spring transition, this UTC timestamp is still Sunday.
            (datetime(2031, 3, 9, 0, tzinfo=zone), "2031-03-10T03:30:00+00:00", "push"),
            # At the fall transition, due time's offset is an hour behind creation's.
            (datetime(2031, 11, 2, 0, tzinfo=zone), "2031-11-03T04:30:00+00:00", "push"),
            (datetime(2031, 11, 2, 0, tzinfo=zone), "2031-11-03T05:00:00+00:00", "batch"),
            (datetime(2031, 11, 2, 1, 45, tzinfo=zone, fold=0), "2031-11-02T01:15:00-05:00", "push"),
        ]
        for self.now, due, route in cases:
            with self.subTest(due=due):
                row = self.feed.post(decision(rank=2, due=due))
                self.assertEqual(row["route"], route)
                self.assertEqual(self.open().get(row["id"])["route"], route)

    def test_expiry_uses_the_instant_across_a_repeated_wall_hour(self):
        zone = ZoneInfo("America/New_York")
        self.now = datetime(2031, 11, 2, 1, 45, tzinfo=zone, fold=0)
        deadline = datetime(2031, 11, 2, 1, 15, tzinfo=zone, fold=1)
        bid = self.feed.post(decision(default={"action": "none", "at": deadline.isoformat()}))["id"]
        self.assertEqual(self.feed.tick()["defaulted"], [])
        self.assertEqual(self.feed.tick(deadline)["defaulted"], [])
        self.assertEqual(self.feed.tick(deadline.astimezone(UTC) + timedelta(seconds=1))["defaulted"], [bid])
        self.assertEqual(self.feed.tick()["defaulted"], [])
        self.assertEqual(self.feed.get(bid)["state"], "done")

    def test_clock_rollback_does_not_free_daily_budget_or_reopen_a_default(self):
        for i in range(20):
            tool = FeedTools(self.feed, session=f"example-{i}")
            self.assertEqual(tool.brief_post("news", NEWS["ask"]), "Brief posted.")
        deadline = T0 + timedelta(minutes=5)
        bid = self.feed.post(decision(default={"action": "none", "at": deadline.isoformat()}))["id"]
        self.assertEqual(self.feed.tick(deadline + timedelta(seconds=1))["defaulted"], [bid])
        self.now = T0 - timedelta(hours=2)
        tool = FeedTools(self.open(), session="new-session")
        self.assertTrue(tool.brief_post("news", NEWS["ask"]).startswith("Error:"))
        self.assertEqual(self.feed.tick()["defaulted"], [])
        self.assertEqual(self.feed.get(bid)["state"], "done")
        self.now = T0 + timedelta(days=1)
        self.assertEqual(tool.brief_post("news", NEWS["ask"]), "Brief posted.")

    def test_quiet_hours_rechecked_and_only_open_deferred_briefs_push(self):
        pushes = []
        quiet = Quiet(T0 + timedelta(hours=8))
        self.feed = self.open(quiet=quiet, push=lambda *args: pushes.append(args))
        one = self.feed.post(decision(rank=2, time_critical=True))
        self.feed.post(decision(rank=1, time_critical=True))
        self.feed.post(decision(rank=2, time_critical=True))
        self.feed.answer(one["id"], "not_now")
        self.assertEqual(pushes, [])
        self.now += timedelta(hours=2)
        # The current quiet-hours setting is authoritative, even after restart.
        quiet.until = self.now
        self.assertEqual(self.open(quiet=quiet, push=lambda *args: pushes.append(args)).tick()["pushed"], 1)
        self.assertEqual(pushes, [(2, 1)])
        self.now -= timedelta(hours=3)
        self.assertEqual(self.feed.tick()["pushed"], 0)
        self.now += timedelta(hours=25)
        self.assertEqual(self.feed.tick()["pushed"], 0)

    def test_ten_thousand_briefs_replay_and_rebuild_the_index(self):
        self.feed.post(NEWS)
        sample = json.loads(self.log.read_bytes())
        with self.log.open("ab") as log:
            for i in range(1, 10_000):
                sample["id"] = f"{i:032x}"
                log.write(json.dumps(sample).encode() + b"\n")
        self.index.unlink()
        fresh = self.open()
        rows = fresh.briefs()
        self.assertEqual(len(rows), 10_000)
        self.assertEqual(rows[-1]["id"], f"{9999:032x}")
        fresh.answer(rows[-1]["id"], "not_now")
        self.assertEqual(len(fresh.batch()), 9999)
        self.assertEqual(self.feed.get(rows[-1]["id"])["state"], "dismissed")
        self.assert_modes()

    def test_modes_across_operations_and_private_data_file_links(self):
        self.feed.briefs()
        self.assert_modes()
        row = self.feed.post(decision())
        self.assert_modes()
        self.feed.get(row["id"])
        self.assert_modes()
        self.feed.batch()
        self.assert_modes()
        self.feed.feedback(row["id"], "too_long")
        self.assert_modes()
        self.feed.answer(row["id"], "edit", text="Try another example.")
        self.assert_modes()
        self.feed.tick()
        self.assert_modes()
        target = self.root.parent / "target"
        target.write_text("untouched")
        for file in (self.index, self.log):
            file.unlink()
            file.symlink_to(target)
            with self.assertRaises((OSError, FeedError)):
                self.feed.post(NEWS)
            self.assertEqual(target.read_text(), "untouched")
            file.unlink()

    def test_execute_reads_the_durable_approval_and_failure_stays_private(self):
        observed = []
        bid = self.feed.post(decision(proposal_id="example-proposal"))["id"]

        def broken(proposal):
            # Check the log before starting a second caller, so an early
            # execution fails this test even if it happens outside the lock.
            recorded = any(event["event"] == "answer" and event["id"] == bid
                           for event in map(json.loads, self.log.read_bytes().splitlines()))
            reader = Thread(target=lambda: observed.append((proposal, recorded, self.open().get(bid)["state"])),
                            daemon=True)
            reader.start()
            reader.join(3)
            raise RuntimeError("private example callback detail")

        fresh = self.open(execute=broken)
        with self.assertRaises(FeedError) as error:
            fresh.answer(bid, "approve")
        self.assertNotIn("private example", str(error.exception))
        self.assertIsNone(error.exception.__context__)
        self.assertEqual(observed, [("example-proposal", True, "approved")])
        with self.assertRaises(FeedError):
            fresh.answer(bid, "approve")
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.feed.get(bid)["state"], "approved")

    def test_push_can_open_a_second_feed_and_never_receives_brief_text(self):
        received = []

        def push(*args):
            reader = Thread(target=lambda: received.append((args, len(self.open().briefs()))), daemon=True)
            reader.start()
            reader.join(3)

        self.open(push=push).post(decision(rank=1, time_critical=True))
        self.assertEqual(received, [((1, 1), 1)])

    def test_expiry_never_executes_any_non_no_op_default(self):
        ran = []
        fresh = self.open(execute=ran.append)
        bid = fresh.post(decision(proposal_id="example-proposal", default={
            "action": "send the example reply", "at": T0.isoformat(),
        }))["id"]
        self.now += timedelta(seconds=1)
        self.assertEqual(fresh.tick()["expired"], [bid])
        self.assertEqual(fresh.get(bid)["state"], "expired")
        self.assertEqual(ran, [])

    def test_nested_invisible_text_and_invalid_shapes_are_refused(self):
        for fields in (
            decision(options=[{"label": "A\u034fB"}]),
            decision(context=[{"label": "Example", "ref": "source:\u202eabc"}]),
            decision(attachment={"type": [], "ref": "example"}),
            decision(recommend={"label": "Example", "because": "hidden\u200bvalue"}),
            decision(default={"action": "none", "at": "2031-01-10\x0112:00:00+00:00"}),
        ):
            with self.assertRaises(FeedError):
                self.feed.post(fields)
        self.assertEqual(self.feed.briefs(), [])

    def test_nested_fields_and_owner_answer_are_redacted_before_append(self):
        token = "ghp" + "_" + "Example9" * 5
        row = self.feed.post(decision(
            attachment={"type": "draft", "ref": "draft:" + token},
            context=[{"label": token, "ref": "source:example"}],
            default={"action": "Set aside " + token, "at": None},
        ))
        self.feed.answer(row["id"], "edit", text="Remove " + token)
        self.assertNotIn(token.encode(), self.log.read_bytes())
        self.assertNotIn(token, json.dumps(self.open().get(row["id"])))


if __name__ == "__main__":
    unittest.main()
