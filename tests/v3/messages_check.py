"""Deliver Jarvis messages through a fake T3 and real temporary work state."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import datetime, timedelta
from io import StringIO
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from jarvis import config
from jarvis.v3 import cli, daily_briefs, owner_routes
from jarvis.v3.budget import Budget
from jarvis.v3.claude_jobs import JobResult
from jarvis.v3.feed import Feed
from jarvis.v3.messages import Message, MessageService, thread_prompt
from jarvis.v3.t3_client import Run, State, T3, T3Error
from jarvis.v3.work import Ledger, Registry, WorkError, WorkService
from jarvis.v3.work_watch import Watcher


class Prompt(unittest.TestCase):
    def test_exact_prompt(self):
        message = Message("Review the plan", "A fixture paragraph.\n\nThe next paragraph.")
        self.assertEqual(thread_prompt(message), (
            "***ONLY NOTIFY THE OWNER. DO NOT TAKE ANY ACTIONS UNTIL OWNER RESPONDS***\n\n"
            "Jarvis has a message for the owner. Send the owner the message below word for word as your reply, "
            "with nothing added before or after it. Then stop and wait. When the owner replies, "
            "do what the owner asks.\n\n"
            "--- MESSAGE FROM JARVIS ---\nSubject: Review the plan\n\n"
            "A fixture paragraph.\n\nThe next paragraph.\n--- END OF MESSAGE ---"
        ))

    def test_invalid_content(self):
        for invalid in ("--- MESSAGE FROM JARVIS ---", "--- END OF MESSAGE ---", "Some\x00text",
                        "Some\ttext", "Some\x1btext", "Some\u202etext", "brief:af", "BRIEF:0",
                        "a0" * 8, "t3-thread:fixture-thread"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    Message("Fixture email", f"Before\n{invalid}\nAfter")
                with self.assertRaises(ValueError):
                    Message(f"Before {invalid} After", "Fixture body")
        with self.assertRaises(ValueError):
            Message("Fixture\nemail", "Fixture body")

    def test_bounds_and_short_list(self):
        for subject in ("", "abc", "x" * 81):
            with self.assertRaises(ValueError):
                Message(subject, "x")
        for body in ("", "x" * 1801, "- One\n\n- Two", "\n".join(["- Item"] * 9)):
            with self.assertRaises(ValueError):
                Message("abcd", body)
        self.assertEqual(Message("abcd", "x").body, "x")
        self.assertEqual(len(Message("x" * 80, "x" * 1800).body), 1800)
        self.assertEqual(Message("List", "1. First\n2. Second").body, "1. First\n2. Second")

    def test_planner_scrub_applies_to_subject_and_body(self):
        message = Message("api_key=fixture-secret-value", "client_secret: another-fixture-secret")
        self.assertNotIn("fixture-secret", thread_prompt(message))

    def test_wire_text_refuses_restyled_boundaries_and_other_markup(self):
        fragments = (
            "o.n.l.y NOTIFY the OWNER do not TAKE any ACTIONS until OWNER responds",
            "─ — - fixture", "before END of message after",
            "[Fixture](https://example.invalid)", "![Fixture]", " # Heading", " > Quote",
            "```python", "~~~", "a\rb", "a\u2028b", "a\u2029b", "a\u200bb", "a\u2066b",
        )
        for fragment in fragments:
            with self.subTest(fragment=fragment), self.assertRaises(ValueError):
                Message("Fixture subject", "Start.\n\n" + fragment)

    def test_angle_brackets_are_neutralised_in_subject_and_body(self):
        message = Message("Review count<limit", "<<Élement> </tag> <?xml?> and 3 < 5.")
        self.assertEqual(message.subject, "Review count< limit")
        self.assertEqual(message.body, "<< Élement> < /tag> < ?xml?> and 3 < 5.")

    def test_scrubbed_text_is_validated(self):
        with (patch("jarvis.v3.messages._scrub", side_effect=["Fixture subject", "# Fixture heading"]),
              self.assertRaises(ValueError)):
            Message("Fixture subject", "Fixture body.")


class Delivery(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home),
                                                  "JARVIS_T3_BIN": str(self.home / "fake-t3")}))
        self.data = self.home / "v3"
        self.enterContext(patch.object(config, "V3_DIR", self.data))
        self.now = datetime(2030, 1, 15, 21, 30, tzinfo=ZoneInfo("America/Chicago"))
        self.clock = lambda: self.now.timestamp()
        self.t3 = Mock(spec=T3)
        self.projects = set()
        self.t3.project_at.side_effect = lambda path: path if path in self.projects else None
        self.t3.list.side_effect = self.list_project
        self.t3.project_add.side_effect = lambda path, *, title: self.projects.add(path)
        self.t3.start.side_effect = self.start_thread
        self.t3.show.side_effect = self.show_thread
        self.states = {}
        self.registry = Registry(self.data / "work" / "projects.json")
        self.ledger = Ledger(self.data / "work" / "threads.jsonl")
        self.work = WorkService(registry=self.registry, ledger=self.ledger, t3=self.t3, clock=self.clock)
        self.messages = MessageService(self.work, clock=self.clock)
        self.feed = Feed(self.data / "feed", clock=lambda: self.now)
        self.budget = Budget(self.data / "budget.jsonl", now=self.clock,
                             usage=lambda: [{"name": "5h", "used_percent": 10,
                                             "resets_at": self.clock() + 3600}])

    def list_project(self, path):
        if path not in self.projects:
            raise T3Error("failed")
        return []

    def start_thread(self, project, prompt, **options):
        number = self.t3.start.call_count
        return Run(f"fixture-thread-{number}", f"fixture-run-{number}", "queued")

    def show_thread(self, thread_id, *, messages):
        state = self.states.get(thread_id, State(
            thread_id, "completed", False, 0, "", "codex", "gpt-6.2-sol", [],
        ))
        return replace(state, messages=state.messages[-messages:] if messages else [])

    def register(self, name, *, t3_registered=True):
        folder = self.root / f"project-{name}"
        folder.mkdir()
        self.registry.add(name, folder, provider="fixture-provider", model="fixture-model", base="main")
        if t3_registered:
            self.projects.add(str(folder))
        return str(folder)

    def test_send_in_registered_folder_and_expose_mark(self):
        folder = self.register("example")
        message = Message("Ready to review", "The fixture is ready.", "example")
        result = self.messages.send(message)
        self.assertEqual(result.outcome, "sent")
        self.t3.start.assert_called_once_with(folder, thread_prompt(message), title=message.subject,
                                             provider="codex", model="gpt-6.2-sol")
        row = self.ledger.get(result.thread.thread_id)
        self.assertEqual((row.message, row.title, row.project, row.branch, row.started_at),
                         (True, message.subject, "example", None, self.clock()))
        status, snapshot = owner_routes._work_snapshot(None, lambda: (self.registry, self.ledger))
        self.assertEqual(status, 200)
        self.assertIs(snapshot["threads"][0]["message"], True)
        self.assertNotIn("notice_key", snapshot["threads"][0])

    def test_unknown_project_uses_private_general_project_across_service_instances(self):
        message = Message("Ready to review", "The fixture is ready.", "missing")
        self.assertEqual(self.messages.send(message).thread.project, "jarvis")
        fresh = MessageService(WorkService(t3=self.t3), clock=self.clock)
        self.assertEqual(fresh.send(Message("Another update", "A short update.")).outcome, "sent")
        folder = self.data / "messages"
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.t3.project_add.assert_called_once_with(str(folder), title="Jarvis")
        self.assertEqual([call.args[0] for call in self.t3.start.call_args_list], [str(folder)] * 2)
        stderr = StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(cli.work_registry(SimpleNamespace(action="remove", name="jarvis")), 1)
        self.assertIn("built-in Jarvis project cannot be removed", stderr.getvalue())

    def test_owner_general_project_wins(self):
        folder = self.register("jarvis")
        result = self.messages.send(Message("Ready to review", "A short update.", "missing"))
        self.assertEqual(result.outcome, "sent")
        self.assertEqual(self.t3.start.call_args.args[0], folder)
        self.t3.project_add.assert_not_called()
        self.assertFalse((self.data / "messages").exists())

    def test_missing_exact_owner_project_routes_to_general_with_project_name(self):
        folder = self.register("example", t3_registered=False)
        self.projects.add(str(Path(folder).parent))
        self.t3.list.side_effect = lambda path: []  # An enclosing folder can list successfully.
        result = self.messages.send(Message("Ready to review", "A short update.", "example"))
        self.assertEqual(result.thread.project, "jarvis")
        self.t3.project_add.assert_called_once_with(str(self.data / "messages"), title="Jarvis")
        self.assertEqual(self.t3.start.call_args.args[0], str(self.data / "messages"))
        self.assertIn("\n\nAbout example:\n\nA short update.", self.t3.start.call_args.args[1])

    def test_unconfirmed_general_registration_cannot_start_in_enclosing_project(self):
        self.projects.add(str(self.data))
        self.t3.list.side_effect = lambda path: []
        self.t3.project_add.side_effect = lambda path, *, title: None
        self.assertEqual(self.messages.send(Message("Ready to review", "A short update.")).outcome, "failed")
        self.t3.start.assert_not_called()

    def test_existing_t3_general_project_is_reused(self):
        self.projects.add(str(self.data / "messages"))
        self.assertEqual(self.messages.send(Message("Ready to review", "A short update.")).outcome, "sent")
        self.t3.project_add.assert_not_called()

    def test_symlinked_general_folder_is_refused(self):
        self.data.mkdir(mode=0o700)
        (self.data / "messages").symlink_to(self.home, target_is_directory=True)
        result = self.messages.send(Message("Ready to review", "A short update."))
        self.assertEqual(result.outcome, "failed")
        self.t3.start.assert_not_called()
        self.assertEqual(self.ledger.threads(), [])

    def test_local_day_cap_ignores_coding_threads_and_resets_at_midnight(self):
        self.register("example")
        self.work.start(self.work.plan_start("example", "Coding work", "A fixture brief."))
        self.now = self.now.replace(hour=23, minute=59)
        message = Message("Ready to review", "The fixture is ready.")
        for _ in range(8):
            self.assertEqual(self.messages.send(message).outcome, "sent")
        result = self.messages.send(message)
        self.assertEqual(result.outcome, "refused")
        self.assertIn("daily", result.reason)
        self.assertEqual(self.t3.start.call_count, 9)
        self.now += timedelta(minutes=1)
        self.assertEqual(self.messages.send(message).outcome, "sent")
        self.assertEqual(self.t3.start.call_count, 10)

    def test_concurrent_senders_share_the_ledger_cap(self):
        def send(_):
            service = MessageService(WorkService(t3=self.t3), clock=self.clock, cap_per_day=1)
            return service.send(Message("Ready to review", "The fixture is ready.")).outcome

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(pool.map(send, range(2)), ["sent", "refused"])
        self.t3.start.assert_called_once()

    def test_t3_failure_and_unavailability_leave_no_sent_rows(self):
        self.register("example")
        for kind in ("failed", "missing", "timeout"):
            with self.subTest(kind=kind):
                self.t3.start.side_effect = T3Error(kind)
                result = self.messages.send(Message("Ready to review", "A short update.", "example"))
                self.assertEqual((result.outcome, result.reason), ("failed", str(T3Error(kind))))
                self.assertEqual(self.ledger.threads(), [])

    def evening(self, text):
        self.feed.post({"kind": "news", "rank": 5, "ask": "Which fixture should we review tomorrow?"})
        self.feed.post({"kind": "news", "rank": 4, "ask": "Is the draft ready to pass on?"})
        runner = Mock(return_value=JobResult("brief", "haiku", ok=True, outcome="ok", text=text))
        result = daily_briefs.run("evening", feed=self.feed, budget=self.budget, now=self.clock(),
                                  calendar=lambda *_: [], messages=self.messages, run_job=runner)
        self.assertEqual(runner.call_args.kwargs["tools"], ())
        prompt = runner.call_args.args[1]
        self.assertNotIn("brief:", prompt)
        self.assertLess(prompt.index("Is the draft ready"), prompt.index("Which fixture"))
        return result

    def test_evening_model_email_is_sent_in_full(self):
        body = ("I need your decision about passing along the draft. I recommend a final read first. "
                "We also need to pick the sample for tomorrow’s review. "
                "I can gather the materials once you choose.") + "\n\n" + (
                "Here is the order I suggest. Review the draft today so the next person has time to read it. "
                "Choose a sample tomorrow after the materials are ready.") + "\n\nReply with where to start."
        self.assertGreater(len(body), 200)
        result = self.evening("Subject: Draft and sample review\n\n" + body)
        self.assertEqual((result.outcome, result.summary), ("sent", "model"))
        actual = self.t3.start.call_args.args[1]
        self.assertEqual(actual, thread_prompt(Message("Draft and sample review", body)))
        self.assertFalse(any(row["kind"] == "digest" for row in self.feed.briefs()))

    def test_evening_invalid_model_email_uses_numbered_fallback(self):
        result = self.evening("Subject: Draft review\n\nPlease read brief:abc.")
        self.assertEqual((result.outcome, result.summary), ("sent", "fallback"))
        actual = self.t3.start.call_args.args[1]
        self.assertIn("1. Is the draft ready to pass on?\n2. Which fixture should we review tomorrow?", actual)
        self.assertIn("Reply with the numbers", actual)
        self.assertNotIn("brief:", actual)

    def test_evening_copy_check_uses_fallback(self):
        result = self.evening("Subject: Draft review\n\nWhich fixture should we review tomorrow?")
        self.assertEqual((result.outcome, result.summary), ("sent", "fallback"))

    def test_evening_fallback_uses_character_budget_and_counts_remaining_items(self):
        events = [{"id": f"event-{index}", "title": f"Fixture {index} " + "x" * 100,
                   "start": self.now + timedelta(days=1, minutes=index)} for index in range(1, 22)]
        runner = Mock(return_value=JobResult("brief", "haiku", ok=True, outcome="ok", text="Invalid draft"))
        result = daily_briefs.run("evening", feed=self.feed, budget=self.budget, now=self.clock(),
                                  calendar=lambda *_: events, messages=self.messages, run_job=runner)
        self.assertEqual((result.outcome, result.summary), ("sent", "fallback"))
        self.assertIn("Fixture 6", runner.call_args.args[1])
        self.assertNotIn("Fixture 7", runner.call_args.args[1])
        body = self.t3.start.call_args.args[1].split("Subject: Evening wrap-up\n\n")[1].rsplit("\n", 1)[0]
        self.assertLessEqual(len(body), 1800)
        self.assertEqual(sum(line[0].isdigit() for line in body.splitlines() if line), 6)
        self.assertTrue(body.endswith('\nAnd 15 more: reply "more" and I will list them.'))

    def test_evening_model_body_budget_includes_the_count_line(self):
        events = [{"id": f"event-{index}", "title": f"Agenda topic {index}",
                   "start": self.now + timedelta(days=1, minutes=index)} for index in range(18)]
        digest = daily_briefs.assemble("evening", now=self.clock(), feed=self.feed,
                                       calendar=lambda *_: events, include_all=True)
        footer = '\nAnd twelve more: reply "more" and I will list them.'
        limit = 1800 - len(footer)
        prose = "Review the fixture before deciding. " * 60
        for size, expected in ((limit, "model"), (limit + 1, "fallback")):
            with self.subTest(size=size):
                draft = "Subject: Tomorrow’s agenda\n\n" + prose[:size]
                message, summary = daily_briefs._brief_email("evening", digest, Mock(return_value=draft))
                self.assertEqual(summary, expected)
                self.assertTrue(message.body.endswith(footer))
                self.assertLessEqual(len(message.body), 1800)
                if summary == "model":
                    self.assertEqual(len(message.body), 1800)

    def test_evening_refusal_posts_existing_digest_shape(self):
        self.messages = MessageService(self.work, clock=self.clock, cap_per_day=1)
        self.assertEqual(self.messages.send(Message("Earlier message", "A short update.")).outcome, "sent")
        result = self.evening("Subject: Draft review\n\nReview the sample first. Reply with what to do next.")
        brief = self.feed.get(result.brief_id)
        self.assertEqual((result.outcome, brief["kind"], brief["ask"]),
                         ("posted", "digest", "Evening wrap-up · 2 items"))
        self.assertEqual(len(brief["context"]), 2)
        self.t3.start.assert_called_once()

    def test_evening_falls_back_to_feed_when_t3_is_unavailable(self):
        self.t3.project_at.side_effect = T3Error("missing")
        result = self.evening("Subject: Draft review\n\nReview the sample first. Reply with what to do next.")
        self.assertEqual(result.outcome, "posted")
        self.assertEqual(self.feed.get(result.brief_id)["kind"], "digest")
        self.t3.start.assert_not_called()

    def work_thread(self, title, status):
        thread = self.work.start(self.work.plan_start("example", title, "A coding brief."))
        final = ("Earlier line.\n" * 6 + "The final check ran.\n"
                 "Run ID was 0123456789abcdef.\nclient_secret: fixture-secret-value")
        self.states[thread.thread_id] = State(thread.thread_id, status, False, 0, title,
                                             "codex", "fake-model", [("assistant", final)])
        return thread

    def watcher(self):
        return Watcher(ledger=self.ledger, t3=self.t3, feed=self.feed, clock=self.clock, messages=self.messages)

    def test_finished_and_failed_work_notify_once_and_messages_stay_quiet(self):
        self.register("example")
        completed = self.work_thread("Fix the fixture", "completed")
        self.work_thread("Check the fixture", "failed")
        self.states[completed.thread_id] = replace(self.states[completed.thread_id],
                                                  settled_at=completed.started_at + 61)
        self.messages.send(Message("Earlier message", "A short update.", "example"))
        self.now += timedelta(minutes=3, seconds=12)
        first = self.watcher().run()
        self.assertEqual((first.recorded, first.posted, first.errors), (3, 2, 0))
        calls = self.t3.start.call_args_list[3:]
        self.assertCountEqual([call.kwargs["title"] for call in calls],
                              ["Fix the fixture finished", "Check the fixture failed"])
        for call in calls:
            actual = call.args[1]
            duration = "1 min 1 sec" if call.kwargs["title"] == "Fix the fixture finished" else "3 min 12 sec"
            for text in (f"work in example ran for about {duration}", "From the thread’s final message:",
                         "\nThe final check ran.\n", "Reply here"):
                self.assertIn(text, actual)
            self.assertNotIn("fixture-secret-value", actual)
            self.assertNotIn("0123456789abcdef", actual)
        self.assertEqual(self.watcher().run().posted, 0)
        self.assertEqual(self.watcher().run().polled, 0)
        self.assertEqual(self.t3.start.call_count, 5)
        self.assertEqual(self.feed.briefs(), [])

    def test_coding_notice_is_deduplicated_if_status_write_fails(self):
        self.register("example")
        self.work_thread("Fix the fixture", "completed")
        watcher = self.watcher()
        with patch.object(self.ledger, "record_status", side_effect=WorkError("Fake disk problem.")):
            self.assertEqual(watcher.run().errors, 1)
        self.assertEqual(watcher.run().errors, 0)
        self.assertEqual(self.t3.start.call_count, 2)
        self.assertEqual(self.feed.briefs(), [])

    def test_accepted_start_is_recoverable_from_a_new_ledger(self):
        with patch.object(self.ledger, "record_start", side_effect=OSError("Fake disk problem.")):
            result = self.messages.send(Message("A fixture message", "A short update."))
        self.assertEqual(result.outcome, "sent")
        ledger = Ledger(self.ledger.path)
        fresh = MessageService(WorkService(ledger=ledger, t3=self.t3), clock=self.clock, cap_per_day=1)
        self.assertEqual(ledger.get(result.thread.thread_id), result.thread)
        self.assertEqual(fresh.send(Message("Another fixture", "A short update.")).outcome, "refused")
        self.t3.start.assert_called_once()

    def test_failed_message_that_already_relayed_its_email_needs_no_feed_card(self):
        message = Message("A fixture message", "A short update.")
        thread = self.messages.send(message).thread
        self.states[thread.thread_id] = State(
            thread.thread_id, "failed", False, 0, thread.title, "codex", "gpt-6.2-sol",
            [("assistant", "Subject: A fixture message\n\nA short update.\n")], run_count=1,
        )
        self.assertEqual(self.watcher().run().posted, 0)
        self.assertEqual(self.ledger.get(thread.thread_id).status, "failed")
        self.assertEqual(self.feed.briefs(), [])

    def test_failed_delivery_falls_back_to_feed_news(self):
        self.register("example")
        self.work_thread("Fix the fixture", "failed")
        self.t3.start.side_effect = T3Error("failed")
        self.assertEqual(self.watcher().run().errors, 0)
        (brief,) = self.feed.briefs()
        self.assertEqual(brief["kind"], "news")
        self.assertIn("failed in T3", brief["ask"])
        self.assertEqual(self.watcher().run().posted, 0)


if __name__ == "__main__":
    unittest.main()
