"""I4a checks with private stores, the real owner route, and a fake T3."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
import hashlib
from http.client import HTTPConnection
import itertools
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v3.daemon import Daemon
from jarvis.v3.executor import Executor
from jarvis.v3.feed import Feed, FeedError
from jarvis.v3.jobs import default_jobs
from jarvis.v3.policy import Policy, set_rule
from jarvis.v3.proposals import Draft, ProposalStore, action_sha256
from jarvis.v3.t3_client import Run
from jarvis.v3.work import Ledger, Registry, WorkService


MARKER = "fake private thread brief marker"


class FakeT3:
    def __init__(self):
        self.starts = []
        self.sends = []
        self.before_start = lambda *_: None

    def start(self, path, message, **options):
        self.starts.append((path, message, options))
        self.before_start(path, message)
        number = len(self.starts)
        return Run(f"fake-thread-{number}", f"fake-run-{number}", "started")

    def send(self, thread_id, message):
        self.sends.append((thread_id, message))
        return Run(thread_id, "fake-follow-up", "running")


class ExecutorChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.home / "v3"
        self.policy_path = self.data / "policy.json"
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home), "JARVIS_V3_DIR": str(self.data)}))
        self.enterContext(patch.multiple(config, V3_DIR=self.data, V3_POLICY_FILE=self.policy_path))
        self.enterContext(patch("jarvis.v3.t3_client.T3._invoke", side_effect=AssertionError("Real T3 is forbidden.")))
        self.instant = datetime(2026, 1, 15, 18, tzinfo=UTC)
        self.store = ProposalStore(self.data / "proposals", clock=self.clock)
        self.feed = Feed(self.data / "feed", clock=lambda: self.instant)
        self.registry = Registry(self.data / "work" / "projects.json")
        self.ledger = Ledger(self.data / "work" / "threads.jsonl")
        self.folder = self.tmp / "fake-project"
        self.folder.mkdir()
        self.register()
        self.t3 = FakeT3()
        tokens = itertools.count(1)
        self.work = WorkService(registry=self.registry, ledger=self.ledger, t3=self.t3,
                                clock=self.clock, token=lambda _: f"{next(tokens):04x}")
        self.executor = self.new_executor()
        self.actions = self.store.root / "actions.jsonl"

    def clock(self):
        return self.instant.timestamp()

    def register(self, *, path=None, provider="fake-provider", model="fake-model"):
        return self.registry.add("demo", self.folder if path is None else path,
                                 provider=provider, model=model, base="main")

    def new_executor(self, store=None):
        return Executor(self.store if store is None else store, self.feed, self.work,
                        Policy(self.policy_path), clock=self.clock)

    def add(self, key="one", *, kind="thread.start", thread_id="fake-missing", text=MARKER):
        if kind == "thread.start":
            args = dict(project="demo", title=f"Fake thread {key}", brief=text, worktree=True)
            action = dict(tool="work_start", args=args, preview=self.work.plan_start(**args))
        elif kind == "thread.send":
            action = dict(tool="work_send", args=dict(thread_id=thread_id, message=text),
                          preview=dict(thread_id=thread_id, title="Fake follow-up", project="demo"))
        elif kind in ("calendar.block", "vault.update"):
            tool = "calendar_create" if kind == "calendar.block" else "vault_add"
            action = dict(tool=tool, args={}, preview={})
        else:
            action = None
        draft = Draft(kind=kind, level_proposed="L2", basis=[{"type": "signal", "id": f"fake-{key}"}],
                      brief={"kind": "decision", "rank": 3, "ask": "Carry out the fake proposal?"},
                      action=action, dedupe_key=f"{kind}:demo:{key}", project="demo", tainted=False)
        proposal = self.store.add(draft)
        self.assertIsNotNone(proposal)
        self.store.brief(proposal["id"], self.feed)
        return self.store.get(proposal["id"])

    def answer(self, proposal, action="approve", *, text=None):
        self.feed.answer(proposal["brief_id"], action, text=text)

    def state(self, proposal):
        return self.store.get(proposal["id"])["state"]

    def news(self, proposal):
        key = f"jarvis-feed-post:proposal-failed:{proposal['id']}"
        return self.feed.get(hashlib.sha256(key.encode()).hexdigest()[:32])

    def rewrite(self, path, change):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            change(row)
        replacement = path.with_suffix(".tmp")
        replacement.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))
        replacement.chmod(0o600)
        replacement.replace(path)

    def wait_state(self, proposal, expected):
        deadline = time.monotonic() + 4
        poll = threading.Event()
        while self.state(proposal) != expected and time.monotonic() < deadline:
            poll.wait(0.01)
        self.assertEqual(self.state(proposal), expected)

    def test_01_owner_route_records_before_wake_and_does_not_wait_for_t3(self):
        self.instant = datetime.now(UTC)
        proposal = self.add()
        observations = []
        entered, release = threading.Event(), threading.Event()

        def blocked_start(*_):
            observations.append((self.feed.get(proposal["brief_id"])["answer"], self.state(proposal)))
            entered.set()
            if not release.wait(6):
                raise RuntimeError("Fake start was not released.")

        self.t3.before_start = blocked_start
        with patch("jarvis.v3.daemon.WorkService", return_value=self.work):
            daemon = Daemon(port=0, data_dir=self.data, jobs=[], phone_host=None)
            initial_scan = threading.Event()
            run_sweep = daemon._executor_worker._run_sweep

            def observe_initial_scan():
                run_sweep()
                initial_scan.set()

            self.enterContext(patch.object(daemon._executor_worker, "_run_sweep", side_effect=observe_initial_scan))
            daemon.start()
            worker = daemon._executor_thread
            try:
                self.assertTrue(initial_scan.wait(3), "The worker did not finish its initial sweep.")
                with closing(HTTPConnection(*daemon.address, timeout=2)) as connection:
                    try:
                        connection.request("POST", "/v3/owner/feed/answer",
                                           json.dumps({"id": proposal["brief_id"], "action": "approve"}),
                                           {"Origin": f"http://127.0.0.1:{daemon.port}",
                                            "Content-Type": "application/json"})
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        body = json.load(response)
                    except TimeoutError:
                        self.fail("The owner route waited for fake T3.")
                self.assertEqual(body["brief"]["state"], "approved")
                self.assertIs(body.get("executed"), False)
                self.assertIs(body.get("queued"), True)
                self.assertTrue(entered.wait(3), "The approval did not wake the executor.")
                self.assertEqual(observations, [({"action": "approve", "text": None}, "running")])
                self.assertEqual(self.state(proposal), "running")
                release.set()
                self.wait_state(proposal, "done")
            finally:
                release.set()
                daemon.stop()
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.t3.starts), 1)

    def test_02_double_approval_starts_once(self):
        proposal = self.add()
        self.answer(proposal)
        self.assertEqual([row.outcome for row in self.executor.sweep()], ["done"])
        with self.assertRaises(FeedError):
            self.answer(proposal)
        self.assertEqual(self.new_executor().sweep(), [])
        self.assertEqual(self.state(proposal), "done")
        self.assertEqual(len(self.t3.starts), 1)

    def test_03_changed_args_are_refused_even_if_stored_hash_is_updated(self):
        for updated_hash in (False, True):
            with self.subTest(updated_hash=updated_hash):
                proposal = self.add(str(updated_hash))
                self.answer(proposal)

                def corrupt(row):
                    if "action" in row and row["id"] == proposal["id"]:
                        row["action"]["args"]["worktree"] = False
                        if updated_hash:
                            row["action_sha256"] = action_sha256(row)

                self.rewrite(self.store.path, corrupt)
                self.assertEqual(self.executor.sweep()[0].outcome, "failed")
                self.assertEqual(self.news(proposal)["rank"], 3)
        self.assertEqual(self.t3.starts, [])

    def test_04_changed_provider_or_model_starts_nothing(self):
        for key, changed in (("provider", "fake-other-provider"), ("model", "fake-other-model")):
            with self.subTest(field=key):
                proposal = self.add(key)
                self.answer(proposal)
                self.registry.remove("demo")
                self.register(**{key: changed})
                self.executor.sweep()
                self.assertEqual(self.state(proposal), "failed")
                self.assertEqual(self.t3.starts, [])
                self.registry.remove("demo")
                self.register()

    def test_05_removed_project_starts_nothing(self):
        proposal = self.add()
        self.answer(proposal)
        self.registry.remove("demo")
        self.executor.sweep()
        self.assertEqual(self.state(proposal), "failed")
        self.assertEqual(self.t3.starts, [])

    def test_06_l3_policy_starts_nothing(self):
        proposal = self.add()
        self.answer(proposal)
        set_rule(self.policy_path, "thread.start", "L3")
        self.executor.sweep()
        self.assertEqual(self.state(proposal), "failed")
        self.assertIn("policy", self.news(proposal)["ask"])
        self.assertEqual(self.t3.starts, [])

    def test_07_dismissal_other_and_expiry_never_start_work(self):
        for action in ("not_now", "other"):
            proposal = self.add(action)
            self.answer(proposal, action, text="Fake response" if action == "other" else None)
            self.executor.sweep()
            self.assertEqual(self.state(proposal), "dismissed")
        expiry = self.add("expiry")
        unswept = self.add("late-approval")
        self.instant += timedelta(hours=73)
        self.answer(unswept)
        self.feed.tick()
        self.assertEqual(self.feed.get(expiry["brief_id"])["resolved_by"], "default")
        self.executor.sweep()
        self.assertEqual(self.state(expiry), "expired")
        self.assertEqual(self.state(unswept), "expired")
        self.assertEqual(self.t3.starts, [])

    def test_08_edit_briefs_a_fresh_plan_and_requires_its_own_approval(self):
        proposal = self.add()
        self.answer(proposal, "edit", text="Use the fake alternate approach.")
        self.executor.sweep()
        self.assertEqual(self.state(proposal), "edited")
        [revision] = self.store.proposals("briefed")
        self.assertEqual(revision["revision_of"], proposal["id"])
        self.assertIsNone(revision["run"])
        self.assertNotEqual(revision["brief_id"], proposal["brief_id"])
        self.assertNotEqual(revision["action"]["preview"]["branch"], proposal["action"]["preview"]["branch"])
        expected = MARKER + "\n\nOwner's changes (take precedence)\n\nUse the fake alternate approach."
        self.assertEqual(revision["action"]["args"]["brief"], expected)
        self.assertEqual(revision["action"]["preview"]["brief"], expected)
        self.assertEqual(self.feed.get(revision["brief_id"])["state"], "open")
        self.assertEqual(self.executor.sweep(), [])
        self.assertEqual(self.t3.starts, [])
        self.answer(revision)
        self.executor.sweep()
        self.assertEqual([call[1] for call in self.t3.starts], [expected])

    def test_09_running_claim_recovers_only_a_matching_later_ledger_start(self):
        for mismatch in (None, "timestamp", "project", "title", "brief_sha256", "ambiguous"):
            with self.subTest(mismatch=mismatch):
                proposal = self.add(str(mismatch))
                self.answer(proposal)
                self.store.mark(proposal["id"], "approved")
                self.assertTrue(self.store.claim(proposal["id"]))
                plan = proposal["action"]["preview"]
                event = dict(thread_id=f"fake-ledger-{mismatch}", run_id="fake-run",
                             project=plan["project"], title=plan["title"], branch=plan["branch"],
                             brief_sha256=hashlib.sha256(plan["brief"].encode()).hexdigest(), at=self.clock() + 1)
                if mismatch in ("project", "title", "brief_sha256"):
                    event[mismatch] = {"project": "fake-other", "title": "Fake other title",
                                       "brief_sha256": "0" * 64}[mismatch]
                if mismatch == "timestamp":
                    event["at"] = self.clock() - 1
                self.ledger.record_start(**event)
                if mismatch == "ambiguous":
                    self.ledger.record_start(**{**event, "thread_id": "fake-second-match"})
                [outcome] = self.new_executor().sweep()
                self.assertEqual(outcome.outcome, "done" if mismatch is None else "unknown")
                if mismatch is None:
                    self.assertEqual(outcome.result, event["thread_id"])
                    self.assertEqual(self.store.get(proposal["id"])["result"], event["thread_id"])
                else:
                    self.assertIn("may or may not have run", self.news(proposal)["ask"])
                self.assertEqual(self.new_executor().sweep(), [])
        self.assertEqual(self.t3.starts, [])

    def test_10_approval_age_uses_persisted_first_observation(self):
        for hours, expected in ((25, "failed"), (23, "done")):
            with self.subTest(hours=hours):
                proposal = self.add(str(hours))
                self.answer(proposal)
                with patch.object(self.store, "claim", return_value=False):
                    self.assertEqual(self.executor.sweep(), [])
                self.assertEqual(self.state(proposal), "approved")
                self.assertEqual(self.store.transition_at(proposal["id"]), self.instant)
                self.instant += timedelta(hours=hours)
                reopened = ProposalStore(self.store.root, clock=self.clock)
                self.new_executor(reopened).sweep()
                self.assertEqual(self.state(proposal), expected)
                if expected == "failed":
                    self.assertIn("24 hours", self.news(proposal)["ask"])
                    self.assertEqual(self.t3.starts, [])
        self.assertEqual(len(self.t3.starts), 1)

    def test_11_send_reaches_only_ledger_threads(self):
        plan = self.work.plan_start("demo", "Fake existing thread", MARKER)
        thread = self.work.start(plan)
        known = self.add("known", kind="thread.send", thread_id=thread.thread_id)
        unknown = self.add("unknown", kind="thread.send")
        for proposal in (known, unknown):
            self.answer(proposal)
        self.executor.sweep()
        self.assertEqual(self.state(known), "done")
        self.assertEqual(self.state(unknown), "failed")
        self.assertEqual(self.t3.sends, [(thread.thread_id, MARKER)])
        self.assertEqual(self.ledger.get(thread.thread_id).status, "running")

    def test_12_every_outcome_has_one_private_append_only_action_row(self):
        expected = {}
        for kind, action, final in (
            ("thread.start", "approve", "done"), ("ask", "approve", "done"),
            ("thread.send", "approve", "failed"), ("thread.start", "not_now", "dismissed"),
            ("thread.start", "edit", "edited"), ("thread.start", None, "expired"),
            ("thread.start", "crash", "unknown"),
        ):
            proposal = self.add(f"fake-{len(expected)}", kind=kind)
            expected[proposal["id"]] = final
            if action == "crash":
                self.answer(proposal)
                self.store.mark(proposal["id"], "approved")
                self.assertTrue(self.store.claim(proposal["id"]))
            elif action is None:
                self.instant += timedelta(hours=73)
            else:
                self.answer(proposal, action, text="Fake edit" if action == "edit" else None)
            self.executor.sweep()
        self.assertTrue(self.actions.is_file(), "No action log was created.")
        contents = self.actions.read_text()
        rows = [json.loads(line) for line in contents.splitlines()]
        self.assertEqual(stat.S_IMODE(self.actions.stat().st_mode), 0o600)
        self.assertEqual(len(rows), len(expected) + 1)  # The unanswered edit revision also expires.
        self.assertEqual({row["proposal_id"]: row["outcome"] for row in rows if row["proposal_id"] in expected},
                         expected)
        for row in rows:
            self.assertEqual(row.keys(), {"at", "proposal_id", "kind", "outcome", "result"})
        self.assertNotIn(MARKER, contents)
        self.assertEqual(self.executor.sweep(), [])
        self.assertEqual(self.actions.read_text(), contents)
        started_at = rows[0]["at"]
        self.assertEqual(self.executor.activity_counts(started_at, started_at + 1), {"thread.start": 1})
        self.assertEqual(self.executor.activity_counts(started_at - 1, started_at), {})

    def test_13_executor_job_is_a_sixty_second_backstop(self):
        proposal = self.add()
        self.answer(proposal)
        entries = [job for job in default_jobs(executor=self.executor.sweep) if job.name == "executor"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].every_s, 60)
        entries[0].run()
        self.assertEqual(self.state(proposal), "done")
        self.assertEqual(len(self.t3.starts), 1)

    def start_test_daemon(self):
        self.enterContext(patch("jarvis.v3.daemon.WorkService", return_value=self.work))
        daemon = Daemon(port=0, data_dir=self.data, jobs=[], phone_host=None)
        daemon.start()
        self.addCleanup(daemon.stop)
        return daemon

    def hold_t3_start(self):
        entered, release = threading.Event(), threading.Event()

        def blocked_start(*_):
            entered.set()
            if not release.wait(6):
                raise RuntimeError("Fake start was not released.")

        self.t3.before_start = blocked_start
        return entered, release

    def test_stop_between_approvals_leaves_second_runnable_after_restart(self):
        self.instant = datetime.now(UTC)
        first, second = self.add("first"), self.add("second")
        self.answer(first)
        self.answer(second)
        entered, release = self.hold_t3_start()
        checked = []
        execute = Executor._execute

        def track(executor, proposal, *args):
            checked.append(proposal["id"])
            return execute(executor, proposal, *args)

        self.enterContext(patch.object(Executor, "_execute", track))
        daemon = self.start_test_daemon()
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.state(second), "approved")
                stopped = pool.submit(daemon.stop)
                self.assertTrue(daemon._executor_stop.wait(3))
            finally:
                release.set()
            stopped.result(timeout=4)

        self.assertEqual(self.state(first), "done")
        self.assertEqual(self.state(second), "approved")
        self.assertEqual(checked, [first["id"]])
        self.assertEqual(len(self.t3.starts), 1)
        restarted = self.start_test_daemon()
        self.wait_state(second, "done")
        restarted.stop()
        self.assertEqual([options["title"] for _, _, options in self.t3.starts],
                         ["Fake thread first", "Fake thread second"])

    def test_stop_waits_for_in_flight_start_and_records_it(self):
        self.instant = datetime.now(UTC)
        proposal = self.add()
        self.answer(proposal)
        entered, release = self.hold_t3_start()
        daemon = self.start_test_daemon()
        worker = daemon._executor_thread
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                self.assertTrue(entered.wait(3))
                stopped = pool.submit(daemon.stop)
                self.assertTrue(daemon._executor_stop.wait(3))
                with self.assertRaises(TimeoutError):
                    stopped.result(timeout=0.8)
                self.assertEqual(self.state(proposal), "running")
            finally:
                release.set()
            stopped.result(timeout=4)

        self.assertFalse(worker.is_alive())
        self.assertEqual(self.state(proposal), "done")
        thread_id = self.ledger.get("fake-thread-1").thread_id
        self.assertEqual(self.store.get(proposal["id"])["result"], thread_id)
        self.assertEqual(json.loads(self.actions.read_text())["result"], thread_id)

    def test_executor_job_starts_nothing_after_daemon_stop(self):
        self.instant = datetime.now(UTC)
        proposal = self.add()
        daemon = self.start_test_daemon()
        daemon.stop()
        self.answer(proposal)
        self.store.mark(proposal["id"], "approved")
        [job] = [job for job in default_jobs(executor=daemon._sweep_executor)
                 if job.name == "executor"]
        with patch.object(daemon, "_initiative", wraps=daemon._initiative) as initiative:
            job.run()
            initiative.assert_not_called()
        self.assertEqual(self.state(proposal), "approved")
        self.assertEqual(self.t3.starts, [])

    def test_stop_during_start_preflight_releases_claim_at_original_approval_time(self):
        proposal = self.add()
        self.answer(proposal)
        self.store.mark(proposal["id"], "approved")
        approved_at = self.instant
        self.instant += timedelta(hours=1)
        get_project = self.registry.get
        observed = []

        def stop_during_registry_check(name):
            observed.append(self.state(proposal))
            self.executor._stop.set()
            return get_project(name)

        with patch.object(self.registry, "get", side_effect=stop_during_registry_check):
            self.assertEqual(self.executor.sweep(), [])
        self.assertEqual(observed, ["running"])
        release = json.loads(self.store.path.read_text().splitlines()[-1])
        self.assertEqual(release["at"], approved_at.isoformat())
        self.assertEqual(self.state(proposal), "approved")
        self.assertEqual(self.store.transition_at(proposal["id"]), approved_at)
        self.assertEqual(self.t3.starts, [])
        self.assertEqual([row.outcome for row in self.new_executor().sweep()], ["done"])
        self.assertEqual(len(self.t3.starts), 1)

    def test_stop_while_claiming_skips_action_preflight(self):
        proposal = self.add()
        self.answer(proposal)
        claim = self.store.claim

        def stop_after_claim(pid):
            claimed = claim(pid)
            self.executor._stop.set()
            return claimed

        with patch.object(self.store, "claim", side_effect=stop_after_claim), \
                patch.object(self.registry, "get", wraps=self.registry.get) as lookup:
            self.assertEqual(self.executor.sweep(), [])
            lookup.assert_not_called()
        self.assertEqual(self.state(proposal), "approved")
        self.assertEqual(self.t3.starts, [])

    def test_stop_during_authorization_leaves_no_running_claim(self):
        proposal = self.add()
        self.answer(proposal)
        explain = self.executor._policy.explain

        def stop_during_policy_check(facts):
            self.executor._stop.set()
            return explain(facts)

        with patch.object(self.executor._policy, "explain", side_effect=stop_during_policy_check), \
                patch.object(self.store, "claim", wraps=self.store.claim) as claim:
            self.assertEqual(self.executor.sweep(), [])
            claim.assert_not_called()
        self.assertEqual(self.state(proposal), "approved")

    def test_slow_in_flight_start_records_outcome_after_stop_grace(self):
        self.instant = datetime.now(UTC)
        proposal = self.add()
        self.answer(proposal)
        entered, release = self.hold_t3_start()
        daemon = self.start_test_daemon()
        worker = daemon._executor_thread
        with ThreadPoolExecutor(max_workers=1) as pool, \
                patch("jarvis.v3.daemon._EXECUTOR_STOP_GRACE_S", 0.05):
            try:
                self.assertTrue(entered.wait(3))
                active = daemon._executor_sweep
                stopped = pool.submit(daemon.stop)
                try:
                    stopped.result(timeout=2)
                except TimeoutError:
                    self.fail("Daemon.stop exceeded its grace while fake T3 was blocked.")
                self.assertFalse(worker.is_alive())
                self.assertEqual(self.state(proposal), "running")
                with self.assertRaisesRegex(RuntimeError, "previous proposal sweep"):
                    daemon.start()
            finally:
                release.set()
        active.join(3)
        self.assertFalse(active.is_alive())
        self.assertEqual(self.state(proposal), "done")
        thread_id = self.ledger.get("fake-thread-1").thread_id
        self.assertEqual(json.loads(self.actions.read_text())["result"], thread_id)

    def test_two_executors_do_not_recover_or_repeat_a_live_claim(self):
        proposal = self.add()
        self.answer(proposal)
        entered, release = threading.Event(), threading.Event()

        def blocked_start(*_):
            entered.set()
            if not release.wait(6):
                raise RuntimeError("Fake start was not released.")

        self.t3.before_start = blocked_start
        competitor = self.new_executor(ProposalStore(self.store.root, clock=self.clock))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.executor.sweep)
            try:
                self.assertTrue(entered.wait(3))
                second = pool.submit(competitor.sweep)
                self.assertEqual(second.result(timeout=3), [])
                self.assertEqual(self.state(proposal), "running")
            finally:
                release.set()
            self.assertEqual([row.outcome for row in first.result(timeout=3)], ["done"])
        self.assertEqual(len(self.t3.starts), 1)
        self.assertEqual(self.state(proposal), "done")

    def test_feed_hash_change_is_refused(self):
        proposal = self.add()
        self.answer(proposal)
        feed_log = next(self.feed.root.glob("*.jsonl"))
        original = f"{proposal['id']}:{proposal['action_sha256']}".encode()
        corrupt = (f"{proposal['id']}:" + "0" * 64).encode()
        raw = feed_log.read_bytes()
        self.assertEqual(raw.count(original), 1)
        feed_log.write_bytes(raw.replace(original, corrupt) + b"\n")
        self.assertEqual(Feed(self.feed.root).get(proposal["brief_id"])["proposal_id"], corrupt.decode())
        self.executor.sweep()
        self.assertEqual(self.state(proposal), "failed")
        self.assertIn("does not match", self.news(proposal)["ask"])
        self.assertEqual(self.t3.starts, [])

    def test_approval_must_belong_to_the_stored_brief_id(self):
        proposal = self.add()
        self.answer(proposal)

        def corrupt(row):
            if row["id"] == proposal["id"] and row["state"] == "briefed":
                row["brief_id"] = "0" * 32

        self.rewrite(self.store.path, corrupt)
        self.executor.sweep()
        self.assertEqual(self.state(proposal), "failed")
        self.assertIn("does not match", self.news(proposal)["ask"])
        self.assertEqual(self.t3.starts, [])

    def test_start_guard_rechecks_policy_after_registry_validation(self):
        proposal = self.add()
        self.answer(proposal)
        original_get = self.registry.get

        def change_policy(name):
            project = original_get(name)
            set_rule(self.policy_path, "thread.start", "L3")
            return project

        with patch.object(self.registry, "get", side_effect=change_policy):
            self.executor.sweep()
        self.assertEqual(self.state(proposal), "failed")
        self.assertIn("policy", self.news(proposal)["ask"])
        self.assertEqual(self.t3.starts, [])

    def test_reregistered_folder_starts_nothing(self):
        proposal = self.add()
        self.answer(proposal)
        alternate = self.tmp / "fake-alternate"
        alternate.mkdir()
        self.registry.remove("demo")
        self.register(path=alternate)
        self.executor.sweep()
        self.assertEqual(self.state(proposal), "failed")
        self.assertEqual(self.t3.starts, [])

    def test_worker_survives_sweep_and_action_exceptions_without_leaking_text(self):
        bad = self.add("bad")
        good = self.add("good", text="Fake independent work.")

        def fail_on_marker(_path, message):
            if message == MARKER:
                raise RuntimeError(message)

        self.t3.before_start = fail_on_marker
        self.answer(bad)
        self.answer(good)
        real_answers = self.executor._answers
        failures = iter([RuntimeError(MARKER)])

        def flake():
            error = next(failures, None)
            if error is not None:
                raise error
            return real_answers()

        worker = threading.Thread(target=self.executor.run, kwargs={"interval": 0.02})
        with patch.object(self.executor, "_answers", side_effect=flake), self.assertLogs("jarvis.v3.executor") as logs:
            worker.start()
            try:
                self.wait_state(good, "done")
                self.assertTrue(worker.is_alive())
            finally:
                self.executor._stop.set()
                self.executor.wake()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.state(bad), "failed")
        self.assertNotIn(MARKER, "\n".join(logs.output))
        self.assertNotIn(MARKER, json.dumps(self.news(bad)))
        self.assertNotIn(MARKER, self.actions.read_text())

    def test_higher_level_rebriefs_and_requires_a_new_approval(self):
        set_rule(self.policy_path, "thread.start", "L0")
        proposal = self.add()
        self.answer(proposal)
        set_rule(self.policy_path, "thread.start", "L2")
        self.executor.sweep()
        self.assertEqual(self.state(proposal), "failed")
        self.assertEqual(self.t3.starts, [])
        [revision] = self.store.proposals("briefed")
        self.assertEqual(revision["revision_of"], proposal["id"])
        self.assertEqual(revision["level"], "L2")
        self.assertNotEqual(revision["brief_id"], proposal["brief_id"])
        self.answer(revision)
        self.executor.sweep()
        self.assertEqual(len(self.t3.starts), 1)

    def test_calendar_and_vault_are_unavailable_and_ask_only_runs_nothing(self):
        for kind in ("calendar.block", "vault.update"):
            proposal = self.add(kind, kind=kind)
            self.answer(proposal)
            self.executor.sweep()
            self.assertEqual(self.state(proposal), "failed")
            self.assertIn("not available yet", self.news(proposal)["ask"])
        ask = self.add("ask", kind="ask")
        self.assertEqual(self.feed.get(ask["brief_id"])["kind"], "news")
        self.answer(ask)
        self.assertEqual([row.outcome for row in self.executor.sweep()], ["done"])
        self.assertEqual(self.t3.starts, [])
        self.assertEqual(self.t3.sends, [])

    def test_terminal_outcome_is_logged_after_a_publication_failure(self):
        proposal = self.add()
        self.answer(proposal)
        with patch.object(self.executor, "_publish", side_effect=OSError(MARKER)):
            with self.assertLogs("jarvis.v3.executor") as logs:
                self.assertEqual(self.executor.sweep(), [])
        self.assertEqual(self.state(proposal), "done")
        self.assertNotIn(MARKER, "\n".join(logs.output))
        self.assertEqual([row.outcome for row in self.new_executor().sweep()], ["done"])
        self.assertEqual(len(self.actions.read_text().splitlines()), 1)
        self.assertEqual(len(self.t3.starts), 1)


if __name__ == "__main__":
    unittest.main()
