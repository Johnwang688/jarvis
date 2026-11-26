"""Boundary and recovery checks for the deterministic trigger job."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from jarvis import config
from jarvis.v3 import jobs, signals, triggers
from jarvis.v3.commitments import Commitment
from jarvis.v3.feed import Feed, FeedError
from jarvis.v3.vault import VaultError


CHICAGO = ZoneInfo("America/Chicago")
NOW = datetime(2027, 1, 15, 8, tzinfo=timezone.utc).timestamp()


def stamp(instant):
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


def snapshot(*items):
    return {"items": list(items)}


def event(**changes):
    item = {"id": "event-1", "title": "Example", "start": "2027-01-15", "end": "2027-01-16",
            "all_day": 1, "attendees": 0, "owner_event": 1, "jarvis": 0, "status": "confirmed",
            "recurring": 0}
    return dict(item, **changes)


def assignment(item_id, due):
    return {"id": item_id, "title": "Example", "course": None, "due": due,
            "state": None, "done": 0, "locked": 0}


def pull_request(authored=1):
    return {"id": "example/demo#1", "repo": "example/demo", "number": 1, "title": None, "draft": None,
            "checks": "failure", "authored": authored, "review_requested": 0, "review_decision": None}


def header(mid, *, owner=0, thread="thread-1", labels=None, sender=None):
    meta = {"from_owner": owner, "thread": thread, "labels": ["INBOX"] if labels is None else labels}
    if sender is not None:
        meta["from"] = sender
    return {"source": "gmail", "ref": mid, "title": "Example", "ts": NOW - 3600, "meta_json": json.dumps(meta)}


class Rules(unittest.TestCase):
    def test_display_text_normalizes_and_withholds_whole_labelled_values(self):
        self.assertEqual(triggers._line("Meeting\u00a0notes\u2003with\x00 links"),
                         "Meeting notes with links")
        for title in ("Reminder Password\u00a0:\u2003fake-password",
                      "Reminder token  =  fake-token"):
            with self.subTest(title=title):
                self.assertEqual(triggers._line(title), "[withheld: labelled secret]")
        wrapped = "sk-or-" + "A1b2" * 7 + "\u00a0" + "C3d4" * 5
        self.assertEqual(triggers._line(wrapped), "[withheld: OpenRouter key]")
        for suffix in ("", "@", "@example.invalid"):
            with self.subTest(suffix=suffix):
                title = f"Workshop https://example-user:fake-password{suffix}"
                output = triggers._line(title)
                self.assertNotIn("example-user", output)
                self.assertNotIn("fake-password", output)
                self.assertEqual(triggers._line(output), output)

    def test_dst_window_and_due_boundaries(self):
        for month, day, utc_hour in ((3, 14, 14), (11, 7, 15)):
            with self.subTest(month=month):
                # The prior evening is already tomorrow in UTC.
                local_now = datetime(2027, month, day - 1, 23, tzinfo=CHICAGO)
                now = local_now.timestamp()
                busy = Mock(return_value=[])
                [record] = triggers.evaluate("calendar", None, snapshot(), now, busy=busy)
                start, end = busy.call_args.args
                self.assertEqual((start.day, start.hour, end.day, end.hour), (day, 9, day, 21))
                self.assertEqual(start.astimezone(timezone.utc).hour, utc_hour)
                self.assertEqual(end.timestamp() - start.timestamp(), 12 * 3600)
                self.assertEqual(record["summary_fields"]["blocks"], [{"start": "09:00", "end": "21:00"}])

                utc_now = local_now.astimezone(timezone.utc)
                rows = [assignment(name, stamp(utc_now + timedelta(seconds=delta)))
                        for name, delta in (("now", 0), ("next", 1), ("limit", 72 * 3600), ("after", 72 * 3600 + 1))]
                records = triggers.evaluate("schoolwork", None, snapshot(*rows), now)
                self.assertEqual([r["summary_fields"]["assignment"] for r in records], ["next", "limit"])

                today = local_now.date()
                rows = [dict(id=f"commitment-{i}", due=(today + timedelta(days=i)).isoformat(),
                             direction="owner-owes", state="open", what="Example")
                        for i in (-1, 0, 2, 3)]
                records = triggers.evaluate("commitments", None, snapshot(*rows), now)
                self.assertEqual([r["summary_fields"]["commitment"] for r in records],
                                 ["commitment--1", "commitment-0", "commitment-2"])
                self.assertEqual([r["summary_fields"]["overdue"] for r in records], [True, False, False])

    def test_events_ending_today_and_events_turning_jarvis(self):
        later = event(start="2027-01-15", end="2027-01-16")
        self.assertEqual(triggers.evaluate("calendar", snapshot(later), snapshot(dict(later, jarvis=1)), NOW), [])
        ending_today = event(start="2027-01-14", end="2027-01-15")
        self.assertEqual(triggers.evaluate("calendar", snapshot(), snapshot(ending_today), NOW), [])
        before_midnight = datetime(2027, 1, 15, 5, 59, 59, tzinfo=timezone.utc).timestamp()
        self.assertEqual(len(triggers.evaluate("calendar", snapshot(), snapshot(ending_today), before_midnight)), 1)


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.signals = self.tmp / "signals"
        self.root = self.tmp / "triggers"
        self.state = self.root / "state.json"
        self.log = self.root / "signals.jsonl"
        self.post = Mock()

    def put(self, source, *items, mtime=NOW, **extra):
        path = signals.write(source, items, directory=self.signals, **extra)
        os.utime(path, (mtime, mtime))

    def trigger(self, *sources, **kwargs):
        return triggers.Triggers(root=self.root, signals_dir=self.signals, sources=sources,
                                 post=self.post, clock=lambda: NOW, **kwargs)

    def test_pr_losing_authored_releases_the_level_key(self):
        trigger = self.trigger("github")
        for authored, expected in ((1, 1), (0, 0), (1, 1)):
            self.put("github", pull_request(authored))
            self.assertEqual(trigger.tick()["emitted"], expected)

    def test_mail_reply_uses_id_for_same_timestamp_and_empty_thread_falls_back(self):
        index = Mock()
        sender = "Fixture Sender <no-reply@example.invalid>"
        index.documents_between.return_value = [
            header("b", owner=1), header("a"), header("c", thread="", sender=sender),
        ]
        self.assertEqual(self.trigger("mail", index=index).tick(), {"emitted": 1, "blocked_config": {}})
        [row] = triggers.read_signals(root=self.root)
        self.assertEqual(row["source_ref"], "gmail:c")
        self.assertEqual(row["summary_fields"]["thread"], "c")
        self.assertEqual(row["summary_fields"]["untrusted_from"], sender)
        self.assertIs(row["summary_fields"]["automated"], True)
        self.assertNotIn(sender, (self.root / "previous/mail.json").read_text())
        index.documents_between.assert_called_once_with(since=NOW - 7 * 86400, until=NOW)

    def test_bad_mail_metadata_blocks_without_releasing_the_key(self):
        index = Mock()
        valid = header("a")
        tick = self.trigger("mail", index=index).tick
        index.documents_between.return_value = [valid]
        self.assertEqual(tick()["emitted"], 1)
        previous = (self.root / "previous/mail.json").read_bytes()
        for invalid in (header("a", labels="INBOX"), header("a", sender=7), dict(valid, meta_json="{bad json")):
            with self.subTest(invalid=invalid):
                index.documents_between.return_value = [invalid]
                self.assertEqual(tick(), {"emitted": 0, "blocked_config": {"mail": "unreadable"}})
                self.assertEqual((self.root / "previous/mail.json").read_bytes(), previous)
        index.documents_between.return_value = [valid]
        self.assertEqual(tick()["emitted"], 0)
        self.post.assert_called_once()

    def test_free_text_is_scrubbed_in_snapshots_records_and_pending_checkpoint(self):
        planted = ("fake-mail-key", "fake-thread-key", "fake-label-key", "fake-title-key",
                   "fake-person-key", "fake-action-key", "fake-course-key", "fake-pr-key")
        index = Mock()
        index.documents_between.return_value = [
            dict(header(f"Token: {planted[0]}", thread=f"Auth token: {planted[1]}",
                        labels=["INBOX", f"API key={planted[2]}"]),
                 title="x" * 100 + f"\nPassword: \"{planted[3]}\""),
        ]
        row = Commitment("c-00000001", f"Password: {planted[4]}", f"Token: {planted[5]}",
                         date(2027, 1, 15), "owner-owes", "owner", "open")
        assignment_row = dict(assignment("a", "2027-01-16T08:00:00Z"),
                              course=f"Password: {planted[6]}")
        self.put("schoolwork", assignment_row, needs_sign_in=False)
        self.put("github", dict(pull_request(), title=f"Review\nPassword: {planted[7]}"))
        trigger = self.trigger("mail", "commitments", "schoolwork", "github", index=index,
                               commitments=lambda: [row])
        original_atomic = triggers._atomic

        def interrupt_at_state(directory, name, data):
            if name == "state.json":
                raise OSError("interrupted checkpoint")
            return original_atomic(directory, name, data)

        def assert_clean(paths):
            for path in paths:
                content = path.read_text()
                for secret in planted:
                    self.assertNotIn(secret, content, path.name)

        with patch.object(triggers, "_atomic", side_effect=interrupt_at_state):
            with self.assertRaisesRegex(OSError, "interrupted checkpoint"):
                trigger.tick()
        own_snapshots = [self.signals / f"{source}.json" for source in ("mail", "commitments")]
        assert_clean([self.log, self.root / "pending.json", *own_snapshots])
        self.assertEqual(trigger.tick()["emitted"], 0)
        assert_clean([self.state, *self.root.glob("previous/*.json")])
        self.assertFalse((self.root / "pending.json").exists())
        records = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len(records), 4)
        for record in records:
            self.assertLessEqual(len(record["summary_fields"]["untrusted_title"]), 120)
        self.post.assert_not_called()

    def test_checkpoint_free_text_is_digest_only_and_calendar_changes_use_it(self):
        first = "Workshop preparations"
        second = "Updated workshop preparations"
        location = "https://example-user:fake-password@"
        self.put("calendar", event(title=first))
        trigger = self.trigger("calendar")
        self.assertEqual(trigger.tick()["emitted"], 0)
        self.put("calendar", event(title=f"{second} {location}"))
        with patch.object(triggers, "_append", side_effect=OSError("fake checkpoint interruption")):
            with self.assertRaises(OSError):
                trigger.tick()
        for path in (self.state, self.root / "pending.json", self.root / "previous/calendar.json"):
            data = path.read_text()
            for text in (first, second, "example-user", "fake-password"):
                self.assertNotIn(text, data)
        self.assertEqual(trigger.tick()["emitted"], 1)
        self.assertEqual(trigger.tick()["emitted"], 0)
        [row] = triggers.read_signals(root=self.root)
        self.assertEqual(row["summary_fields"]["change"], "updated")
        self.assertEqual(row["summary_fields"]["untrusted_title"], "")
        self.put("calendar", event(title=second))
        self.assertEqual(trigger.tick()["emitted"], 1)
        [row] = triggers.read_signals(after=1, root=self.root)
        self.assertEqual(row["summary_fields"]["untrusted_title"], second)

    def test_at_capacity_reports_and_retries_level_and_edge_conditions(self):
        index = Mock()
        messages = [header(mid, thread=mid) for mid in ("a", "b", "c")]
        index.documents_between.return_value = messages[:2]
        self.put("schoolwork", needs_sign_in=False)
        trigger = self.trigger("mail", "schoolwork", index=index)
        with patch.object(triggers, "_STATE_LIMIT", 610):
            self.assertEqual(trigger.tick()["emitted"], 2)
            self.put("schoolwork", assignment("a", None), needs_sign_in=False)
            index.documents_between.return_value = messages
            for _ in range(2):
                self.assertEqual(trigger.tick(), {"emitted": 0, "held_back": 2, "blocked_config": {}})
                self.assertLessEqual(self.state.stat().st_size, 610)
            index.documents_between.return_value = messages[2:]
            self.assertEqual(trigger.tick(), {"emitted": 1, "held_back": 1, "blocked_config": {}})
            index.documents_between.return_value = []
            self.assertEqual(trigger.tick(), {"emitted": 1, "blocked_config": {}})
            self.assertEqual(trigger.tick()["emitted"], 0)
        self.assertEqual([r["source_ref"] for r in triggers.read_signals(root=self.root)],
                         ["gmail:a", "gmail:b", "gmail:c", "schoolwork:a"])

    def test_legacy_pending_recovery_migrates_unevaluated_baselines(self):
        title = "Workshop preparations"
        previous = signals.write("calendar", [event(title=title)], directory=self.root / "previous")
        pending = self.root / "pending.json"
        pending.touch(mode=0o600)
        pending.write_text(json.dumps({
            "records": [], "snapshots": {},
            "state": {"next_seq": 1, "held": {}, "episodes": {}},
        }))
        self.assertEqual(self.trigger("calendar").tick()["blocked_config"], {"calendar": "missing"})
        self.assertNotIn(title, previous.read_text())
        self.assertFalse(pending.exists())
        self.put("calendar", event(title=title))
        self.assertEqual(self.trigger("calendar").tick()["emitted"], 0)

    def test_state_link_corruption_and_size_recover_from_log(self):
        self.put("github", pull_request())
        self.assertEqual(self.trigger("github").tick()["emitted"], 1)
        outside = self.tmp / "outside"
        outside.write_bytes(b"unchanged")
        for damage in ("link", "syntax", "schema", "size"):
            with self.subTest(damage=damage):
                self.state.unlink()
                if damage == "link":
                    self.state.symlink_to(outside)
                else:
                    raw = {"syntax": b"{", "schema": b"[]", "size": b" " * (1024 * 1024 + 1)}
                    self.state.touch(mode=0o600)
                    self.state.write_bytes(raw[damage])
                self.assertEqual(self.trigger("github").tick()["emitted"], 0)
                self.assertFalse(self.state.is_symlink())
                self.assertEqual(json.loads(self.state.read_bytes())["next_seq"], 2)
                self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
                self.assertEqual(outside.read_bytes(), b"unchanged")

    def test_log_skips_valid_json_without_sequence_and_repairs_torn_tail(self):
        self.put("github", pull_request())
        self.trigger("github").tick()
        with self.log.open("ab") as stream:
            stream.write(b'{"kind":"gh.ci_failed"}\n{"seq":2')
        self.assertEqual([row["seq"] for row in triggers.read_signals(root=self.root)], [1])
        self.put("github")
        self.trigger("github").tick()
        self.put("github", pull_request())
        self.assertEqual(self.trigger("github").tick()["emitted"], 1)
        self.assertEqual([row["seq"] for row in triggers.read_signals(root=self.root)], [1, 2])

    def test_append_failure_preserves_previous_copy(self):
        before = assignment("a", None)
        self.put("schoolwork", before, needs_sign_in=False)
        self.trigger("schoolwork").tick()
        prev = self.root / "previous/schoolwork.json"
        baseline = prev.read_bytes()
        self.put("schoolwork", before, assignment("b", None), needs_sign_in=False)
        with patch.object(triggers, "_append", side_effect=OSError("fake append failure")):
            with self.assertRaises(OSError):
                self.trigger("schoolwork").tick()
        self.assertEqual(prev.read_bytes(), baseline)
        self.assertEqual(self.trigger("schoolwork").tick()["emitted"], 1)

    def test_partial_append_replays_only_the_missing_records(self):
        self.put("github", pull_request(), dict(pull_request(), id="example/demo#2", number=2))
        trigger = self.trigger("github")
        original_append = triggers._append

        def interrupt_after_first(log, records):
            original_append(log, records[:1])
            log.write(b'{"seq":2')
            log.flush()
            raise OSError("interrupted append")

        with patch.object(triggers, "_append", side_effect=interrupt_after_first):
            with self.assertRaisesRegex(OSError, "interrupted append"):
                trigger.tick()
        self.put("github")
        self.assertEqual(trigger.tick()["emitted"], 1)
        records = triggers.read_signals(root=self.root)
        self.assertEqual([(r["seq"], r["summary_fields"]["number"]) for r in records], [(1, 1), (2, 2)])

    def test_restart_recovers_a_baseline_update_without_signals(self):
        trigger = self.trigger("schoolwork")
        row = assignment("a", None)
        self.put("schoolwork", row, needs_sign_in=False)
        self.assertEqual(trigger.tick()["emitted"], 0)
        self.put("schoolwork", needs_sign_in=False)
        original_atomic = triggers._atomic

        def interrupt_after_state(directory, name, data):
            if name == "schoolwork.json":
                raise OSError("interrupted baseline")
            return original_atomic(directory, name, data)

        with patch.object(triggers, "_atomic", side_effect=interrupt_after_state):
            with self.assertRaisesRegex(OSError, "interrupted baseline"):
                trigger.tick()
        self.put("schoolwork", row, needs_sign_in=False)
        self.assertEqual(trigger.tick()["emitted"], 1)
        [record] = triggers.read_signals(root=self.root)
        self.assertEqual(record["kind"], "school.assignment_new")

    def test_racing_ticks_share_a_lock(self):
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
        row = Commitment("c-00000001", "Example", "Example", date(2027, 1, 15),
                         "owner-owes", "owner", "open")

        def slow_load():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release loader")
            return [row]

        original_flock = triggers.fcntl.flock

        def observe_flock(fd, operation):
            try:
                return original_flock(fd, operation | triggers.fcntl.LOCK_NB)
            except BlockingIOError:
                waiting.set()
                return original_flock(fd, operation)

        with patch.object(triggers.fcntl, "flock", side_effect=observe_flock), ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.trigger("commitments", commitments=slow_load).tick)
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.trigger("commitments", commitments=lambda: [row]).tick)
                self.assertTrue(waiting.wait(5))
            finally:
                release.set()
            self.assertEqual([first.result()["emitted"], second.result()["emitted"]], [1, 0])

    def test_missing_signals_directory_and_feed_error_keep_episode_id(self):
        self.post.side_effect = FeedError("fake feed error")
        trigger = self.trigger("calendar", "schoolwork", "github")
        self.assertFalse(self.signals.exists())
        expected = dict.fromkeys(("calendar", "schoolwork", "github"), "missing")
        # Isolate the retry on the first source while the whole directory is absent.
        self.assertEqual(self.trigger("calendar").tick()["blocked_config"], {"calendar": "missing"})
        self.post.side_effect = None
        self.assertEqual(trigger.tick()["blocked_config"], expected)
        self.assertEqual(trigger.tick()["blocked_config"], expected)
        calls = [call.args[0] for call in self.post.call_args_list]
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(len(calls), 4)
        self.assertFalse(self.signals.exists())

    def test_crash_after_feed_post_reuses_the_durable_episode(self):
        feed = Feed(self.tmp / "feed")
        self.post.side_effect = feed.post
        write = triggers._atomic

        def fail_receipt(directory, name, data):
            if name == self.state.name and b'"posted":true' in data:
                raise OSError("fake receipt write failure")
            write(directory, name, data)

        with patch.object(triggers, "_atomic", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                self.trigger("calendar").tick()
        self.assertEqual(len(feed.batch()), 1)
        self.trigger("calendar").tick()
        self.assertEqual(len(feed.batch()), 1)
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self.post.call_args_list[0], self.post.call_args_list[1])

    def test_preflight_exact_types_dates_and_future_mtime(self):
        invalid = [("calendar", event(attendees=True)), ("calendar", event(end="invalid")),
                   ("calendar", event(start="invalid")), ("schoolwork", assignment("a", "invalid")),
                   ("github", pull_request(authored=True))]
        for source, item in invalid:
            self.put(source, item, mtime=NOW - 90000)
            self.assertEqual(self.trigger(source).tick()["blocked_config"], {source: "unreadable"})
        self.put("github", pull_request(), mtime=NOW + 60)
        self.assertEqual(self.trigger("github").tick(), {"emitted": 1, "blocked_config": {}})

    def test_default_commitments_only_ignores_the_vault_missing_error(self):
        with patch("jarvis.v3.commitments.load", side_effect=VaultError("Memory entry was not found.")):
            self.assertEqual(self.trigger("commitments").tick(), {"emitted": 0, "blocked_config": {}})
        with patch("jarvis.v3.commitments.load", side_effect=VaultError("Table separator not found.")):
            self.assertEqual(self.trigger("commitments").tick()["blocked_config"], {"commitments": "unreadable"})
        self.assertEqual(self.trigger("mail").tick()["blocked_config"], {"mail": "unreadable"})


class Wiring(unittest.TestCase):
    def test_job_rechecks_grants_and_executables_at_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            grant, signals_dir = root / "grant.json", root / "signals"
            available = set()

            def lookup(name):
                return root / name if name in available else None

            with (patch("jarvis.v3.triggers.Triggers") as factory, patch("jarvis.v3.index.Index") as index,
                  patch("jarvis.v3.feed.Feed") as feed):
                entries = jobs.default_jobs(google_token=grant, signals_dir=signals_dir, find_cli=lookup)
                job = next(entry for entry in entries if entry.name == "triggers")
                index.assert_not_called()
                feed.assert_not_called()
                factory.assert_not_called()
                job.run()
                self.assertEqual(factory.call_args.kwargs, {"sources": ["commitments"], "index": None,
                                 "signals_dir": signals_dir, "post": feed.return_value.post, "busy": None})
                index.assert_not_called()
                for scopes, names, expected in (
                        (["calendar.events"], {"sw"}, ["calendar", "schoolwork", "commitments"]),
                        (["gmail.readonly"], {"gh"}, ["github", "commitments", "mail"]),
                        (["calendar.readonly", "gmail.readonly"], {"sw", "gh"}, list(triggers.SOURCES))):
                    with self.subTest(scopes=scopes, names=names):
                        available = names
                        grant.touch(mode=0o600)
                        grant.write_text(json.dumps({"client_id": "fake-id", "client_secret": "fake-secret",
                                                     "refresh_token": "fake-token",
                                                     "scopes": [f"https://www.googleapis.com/auth/{s}" for s in scopes]}))
                        job.run()
                        args = factory.call_args.kwargs
                        self.assertEqual(args["sources"], expected)
                        self.assertIs(args["index"], index.return_value if "mail" in expected else None)
                        self.assertIs(args["post"], feed.return_value.post)
                index.assert_called_once()
                feed.assert_called_once()

    def test_failed_tick_is_a_job_error_with_no_exception_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (patch.object(config, "V3_TRIGGERS_DIR", root / "triggers"),
                  patch("jarvis.v3.triggers.Triggers.tick", side_effect=OSError("private example text"))):
                entries = jobs.default_jobs(google_token=root / "no-grant", feed_root=root / "feed",
                                            find_cli=lambda name: None)
                job = next(entry for entry in entries if entry.name == "triggers")
                state = root / "jobs/state.json"
                scheduler = jobs.Scheduler([job], state=state, clock=lambda: NOW)
                [row] = scheduler.tick()
                self.assertEqual((row["job"], row["outcome"]), ("triggers", "error"))
                self.assertNotIn("private example text", state.read_text())


if __name__ == "__main__":
    unittest.main()
