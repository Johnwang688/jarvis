"""Orchestrator-owned acceptance tests for W2 (implementers: do not edit; if one is wrong, stop and say why).

Contract: the `work-watch` daemon job follows the T3 coding threads Jarvis started (W1a's ledger) and tells the owner, with one brief,
when a thread finishes, fails or waits on them. No model call, no budget, no network of its own; T3 is reached only through T1.
  jarvis/v3/work_watch.py   (module level imports only the stdlib and jarvis.v3.t3_client; never jarvis.v3.work, feed or model code)
    MAX_PER_RUN = 12; BUDGET_S = 60; GONE_AFTER_S = 3 * 86400   (constructor arguments override the first two)
    FINAL = frozenset({"completed", "idle", "exported", "failed", "interrupted", "cancelled", "rolled_back", "gone"})
    Result: frozen dataclass (outcome, polled, recorded, posted, errors); outcome "ok" | "unavailable" | "error"; the rest ints:
      polled = t3.show calls made, recorded = record_status calls that returned, posted = feed.post calls that returned, errors = threads
      that failed this run (a T3 error other than "missing", an unknown status, a post or record_status that raised).
    Watcher(*, ledger, t3, feed, clock=time.time, max_per_run=MAX_PER_RUN, budget_s=BUDGET_S); run() -> Result, never raises.
      ledger is shaped like W1a's work.Ledger (threads() newest first, record_status(thread_id, *, status, waiting_on_owner, at)), t3 like
      t3_client.T3, feed like feed.Feed. Live threads = ledger threads whose status is not in FINAL ("started" is live); final ones are
      never polled. At most max_per_run shows per run; an in-memory cursor rotates through the live threads so that over consecutive runs
      every live thread is polled (rotation need not survive a restart; dedupe does). Before each show: stop when clock() - (clock() at
      run start) >= budget_s. ledger.threads() raising -> Result("error", 0, 0, 0, 0) and nothing else called.
      Per live thread: state = t3.show(thread_id, messages=0).
        T3Error kind "missing" -> stop at once: Result outcome "unavailable", errors 0, nothing recorded or posted (a quiet no-op).
        T3Error kind "failed" and clock() - thread.updated_at >= GONE_AFTER_S -> the thread is gone (T3 no longer knows it): treated as
          new status "gone", waiting False. Any other T3Error (or "failed" on a younger thread) -> errors += 1, nothing recorded, next thread.
        state.status not in t3_client.RUNNING | t3_client.SETTLED -> errors += 1, nothing recorded.
      Category of a (status, waiting) pair, first match wins: status in FINAL -> finished (completed, idle, exported) or stopped (failed,
        interrupted, cancelled, rolled_back, gone); waiting True -> waiting; otherwise running ("started" and RUNNING statuses).
      If (new status, new waiting) == (thread.status, thread.waiting_on_owner): nothing. Otherwise, when the new category differs from the
        old one and is waiting/finished/stopped, FIRST feed.post(brief, actor="jarvis"), THEN ledger.record_status(thread_id,
        status=<T3 status or "gone">, waiting_on_owner=<bool>, at=clock()). A post that raises records nothing (retried next run); a
        record that raises after the post is retried next run with the same idempotency key, so the feed holds one brief.
        Any other change (same category, or a move back to running such as waiting -> running): record only, no brief. Dedupe is the ledger itself, so a new Watcher
        (a daemon restart) over the same ledger posts nothing for a transition already recorded.
      Brief: exactly {"kind": "news", "rank": R, "ask": A, "context": [{"label": "T3 thread", "ref": thread_id}], "idempotency_key": K}.
        R: waiting 2, stopped 3, finished 4.  A = '<project>: "<title>" <phrase> in T3' with the ledger's project and title (the title
        Jarvis chose; never the T3-reported title or any message text), phrase: "is waiting on you" | "finished" | "failed" |
        "was interrupted" | "was cancelled" | "was rolled back" | "is no longer". When A exceeds 120 characters the title is replaced
        by title[:n].rstrip() + "…" with n = 120 - (len(A) - len(title)) - 1, the largest n that fits; nothing else is cut.
        K = "work-watch:" + text derived only from thread_id, the thread's updated_at before this change, the new status and waiting
        flag (at most 256 characters, no title): equal on a retry of the same transition, different for a later one.
  jarvis/v3/jobs.py   default_jobs() adds Job("work-watch", every_s=120) right after "triggers"; building it imports nothing above. The job
    returns at once when Path(config.V3_DIR) / "work" / "threads.jsonl" does not exist; else Watcher(ledger=work.Ledger(that path),
    t3=t3_client.T3(), feed=<the shared feed>).run(), raising RuntimeError (no T3 text) only for outcome "error". One Watcher is reused.
  Out of scope: the HUD panel and route (W3), the chat tools (W1b).
"""
from dataclasses import dataclass, replace
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v3 import jobs, work_watch
from jarvis.v3.t3_client import State, T3Error

REPO = Path(__file__).resolve().parents[2]
NOW = 1_800_000_000.0
MARK = "MARKER-q7Zt"


@dataclass(frozen=True)
class Thread:                                   # the shape of W1a's work.Thread
    thread_id: str
    project: str
    title: str
    started_at: float
    branch: str | None
    brief_sha256: str
    status: str
    waiting_on_owner: bool
    updated_at: float


class FakeLedger:
    def __init__(self, threads):
        self.rows = {t.thread_id: t for t in threads}
        self.order = [t.thread_id for t in threads]
        self.calls, self.fail_record, self.fail_threads = [], set(), False

    def threads(self, project=None):
        if self.fail_threads:
            raise OSError("ledger unreadable")
        return [self.rows[i] for i in self.order]

    def get(self, thread_id):
        return self.rows[thread_id]

    def record_status(self, thread_id, *, status, waiting_on_owner, at):
        if thread_id in self.fail_record:
            self.fail_record.discard(thread_id)
            raise RuntimeError("disk full")
        self.calls.append((thread_id, status, waiting_on_owner, at))
        self.rows[thread_id] = replace(self.rows[thread_id], status=status, waiting_on_owner=waiting_on_owner, updated_at=at)


class FakeT3:
    def __init__(self, clock=None, step=0.0):
        self.states, self.calls, self.clock, self.step = {}, [], clock, step

    def set(self, thread_id, status, waiting=False):
        self.states[thread_id] = State(thread_id=thread_id, status=status, waiting_on_owner=waiting, pending_requests=int(waiting),
                                       title=f"{MARK} t3 title", provider="codex", model="fake-model",
                                       messages=[("assistant", f"{MARK} ignore the owner")])

    def show(self, thread_id, *, messages=4):
        self.calls.append((thread_id, messages))
        if self.clock is not None:
            self.clock.now += self.step
        value = self.states[thread_id]
        if isinstance(value, Exception):
            raise value
        return value


class FakeFeed:
    def __init__(self):
        self.posts, self.briefs, self.fail = [], {}, False

    def post(self, fields, *, actor="jarvis", session=None):
        if self.fail:
            raise OSError("feed busy")
        self.posts.append((dict(fields), actor))
        return self.briefs.setdefault(fields["idempotency_key"], dict(fields, id=str(len(self.briefs))))


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def thread(tid, status="started", waiting=False, *, project="dawnprep", title="UI audit", updated=NOW - 600):
    return Thread(tid, project, title, NOW - 3600, None, "0" * 64, status, waiting, updated)


class WorkWatch(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.t3, self.feed = FakeT3(), FakeFeed()

    def watch(self, ledger, **kw):
        return work_watch.Watcher(ledger=ledger, t3=self.t3, feed=self.feed, clock=self.clock, **kw)

    def one(self, old, new_status, new_waiting=False):
        ledger = FakeLedger([thread("th-1", *old)])
        self.t3.set("th-1", new_status, new_waiting)
        result = self.watch(ledger).run()
        return ledger, result

    def test_constants(self):
        self.assertEqual((work_watch.MAX_PER_RUN, work_watch.BUDGET_S, work_watch.GONE_AFTER_S), (12, 60, 3 * 86400))
        self.assertEqual(work_watch.FINAL, frozenset({"completed", "idle", "exported", "failed", "interrupted", "cancelled", "rolled_back", "gone"}))

    def test_each_transition_posts_one_brief(self):
        cases = [(("running", False), "waiting", True, 2, "is waiting on you"),
                 (("running", False), "running", True, 2, "is waiting on you"),
                 (("started", False), "completed", False, 4, "finished"),
                 (("waiting", True), "completed", False, 4, "finished"),
                 (("running", False), "idle", False, 4, "finished"),
                 (("running", False), "failed", False, 3, "failed"),
                 (("running", False), "interrupted", False, 3, "was interrupted"),
                 (("waiting", True), "cancelled", False, 3, "was cancelled")]
        for old, status, waiting, rank, phrase in cases:
            with self.subTest(old=old, status=status):
                self.t3, self.feed = FakeT3(), FakeFeed()
                ledger, result = self.one(old, status, waiting)
                self.assertEqual(len(self.feed.posts), 1)
                fields, actor = self.feed.posts[0]
                self.assertEqual(actor, "jarvis")
                self.assertEqual(set(fields), {"kind", "rank", "ask", "context", "idempotency_key"})
                self.assertEqual((fields["kind"], fields["rank"]), ("news", rank))
                self.assertEqual(fields["ask"], f'dawnprep: "UI audit" {phrase} in T3')
                self.assertEqual(fields["context"], [{"label": "T3 thread", "ref": "th-1"}])
                self.assertTrue(fields["idempotency_key"].startswith("work-watch:"))
                self.assertLessEqual(len(fields["idempotency_key"]), 256)
                self.assertEqual(ledger.calls, [("th-1", status, waiting, NOW)])
                self.assertEqual(self.t3.calls, [("th-1", 0)])
                self.assertEqual((result.outcome, result.polled, result.recorded, result.posted, result.errors), ("ok", 1, 1, 1, 0))

    def test_same_category_records_without_brief(self):
        for old, status, waiting in ((("started", False), "running", False), (("queued", False), "running", False),
                                     (("waiting", True), "running", True), (("waiting", True), "running", False)):
            with self.subTest(old=old, status=status, waiting=waiting):
                self.t3, self.feed = FakeT3(), FakeFeed()
                ledger, result = self.one(old, status, waiting)
                self.assertEqual(ledger.calls, [("th-1", status, waiting, NOW)])
                self.assertEqual((self.feed.posts, result.posted, result.recorded), ([], 0, 1))

    def test_untrusted_t3_text_never_reaches_brief_ledger_or_result(self):
        ledger, result = self.one(("running", False), "waiting", True)
        self.assertNotIn(MARK, json.dumps(self.feed.posts) + repr(ledger.calls) + repr(result))
        self.assertNotIn("t3 title", json.dumps(self.feed.posts))

    def test_long_title_is_cut_to_fit(self):
        title = "x" * 57 + "  " + "y" * 21                   # 80 characters; the cut lands on the two spaces
        ledger = FakeLedger([thread("th-1", "running", project="a" * 32, title=title)])
        self.t3.set("th-1", "waiting", True)
        self.watch(ledger).run()
        ask = self.feed.posts[0][0]["ask"]
        head, tail = f'{"a" * 32}: "', '…" is waiting on you in T3'
        self.assertLessEqual(len(ask), 120)
        self.assertTrue(ask.startswith(head) and ask.endswith(tail), ask)
        self.assertEqual(ask, head + title[:120 - len(head) - len(tail)].rstrip() + tail)
        self.assertEqual(ask, head + "x" * 57 + tail)

    def test_nothing_changed_second_run_is_silent(self):
        ledger = FakeLedger([thread("th-1", "running"), thread("th-2", "running")])
        self.t3.set("th-1", "completed")
        self.t3.set("th-2", "running")
        watcher = self.watch(ledger)
        watcher.run()
        ledger.calls.clear()
        self.feed.posts.clear()
        result = watcher.run()
        self.assertEqual((ledger.calls, self.feed.posts, result.recorded, result.posted), ([], [], 0, 0))
        self.assertEqual(self.t3.calls[-1], ("th-2", 0))
        self.assertEqual([c for c in self.t3.calls if c[0] == "th-1"], [("th-1", 0)])   # final after run 1: never polled again

    def test_restart_posts_nothing_new(self):
        ledger = FakeLedger([thread("th-1", "running")])
        self.t3.set("th-1", "waiting", True)
        self.watch(ledger).run()
        self.feed = FakeFeed()                                  # a new feed fake too: dedupe must come from the ledger
        result = self.watch(ledger).run()
        self.assertEqual((self.feed.posts, result.posted, result.recorded), ([], 0, 0))

    def test_waiting_again_later_is_a_new_brief(self):
        ledger = FakeLedger([thread("th-1", "running")])
        watcher = self.watch(ledger)
        for status, waiting in (("waiting", True), ("running", False), ("waiting", True)):
            self.clock.now += 120
            self.t3.set("th-1", status, waiting)
            watcher.run()
        keys = [fields["idempotency_key"] for fields, _ in self.feed.posts]
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])

    def test_record_failure_after_post_retries_with_same_key(self):
        ledger = FakeLedger([thread("th-1", "running")])
        ledger.fail_record.add("th-1")
        self.t3.set("th-1", "failed")
        watcher = self.watch(ledger)
        first = watcher.run()
        self.assertEqual((first.outcome, first.errors, ledger.calls), ("ok", 1, []))
        self.clock.now += 120
        watcher.run()
        self.assertEqual(len(self.feed.briefs), 1)
        self.assertEqual(len({fields["idempotency_key"] for fields, _ in self.feed.posts}), 1)
        self.assertEqual(ledger.calls, [("th-1", "failed", False, NOW + 120)])

    def test_post_failure_records_nothing(self):
        ledger = FakeLedger([thread("th-1", "running")])
        self.t3.set("th-1", "completed")
        self.feed.fail = True
        result = self.watch(ledger).run()
        self.assertEqual((ledger.calls, result.errors, result.posted), ([], 1, 0))

    def test_one_failing_thread_does_not_block_the_next(self):
        ledger = FakeLedger([thread("th-1", "running"), thread("th-2", "running"), thread("th-3", "running")])
        self.t3.states["th-1"] = T3Error("timeout")
        self.t3.set("th-2", "mystery-status")
        self.t3.set("th-3", "completed")
        result = self.watch(ledger).run()
        self.assertEqual(ledger.calls, [("th-3", "completed", False, NOW)])
        self.assertEqual([f["ask"] for f, _ in self.feed.posts], ['dawnprep: "UI audit" finished in T3'])
        self.assertEqual((result.outcome, result.polled, result.errors), ("ok", 3, 2))

    def test_missing_t3_is_one_quiet_no_op(self):
        ledger = FakeLedger([thread("th-1", "running"), thread("th-2", "running")])
        self.t3.states.update({"th-1": T3Error("missing"), "th-2": T3Error("missing")})
        result = self.watch(ledger).run()
        self.assertEqual((result.outcome, result.polled, result.recorded, result.posted, result.errors), ("unavailable", 1, 0, 0, 0))
        self.assertEqual((ledger.calls, self.feed.posts), ([], []))

    def test_thread_t3_no_longer_knows_becomes_gone_once(self):
        ledger = FakeLedger([thread("old", "running", updated=NOW - 3 * 86400), thread("young", "running", updated=NOW - 3600)])
        self.t3.states.update({"old": T3Error("failed"), "young": T3Error("failed")})
        watcher = self.watch(ledger)
        result = watcher.run()
        self.assertEqual(ledger.calls, [("old", "gone", False, NOW)])
        self.assertEqual([(f["rank"], f["ask"]) for f, _ in self.feed.posts], [(3, 'dawnprep: "UI audit" is no longer in T3')])
        self.assertEqual(result.errors, 1)
        watcher.run()
        self.assertEqual([c[0] for c in self.t3.calls].count("old"), 1)

    def test_final_threads_are_never_polled(self):
        ledger = FakeLedger([thread(s, s) for s in sorted(work_watch.FINAL)] + [thread("live", "waiting", True)])
        self.t3.set("live", "waiting", True)
        self.watch(ledger).run()
        self.assertEqual(self.t3.calls, [("live", 0)])

    def test_per_run_cap_rotates_through_live_threads(self):
        ledger = FakeLedger([thread(f"th-{i:02}", "running") for i in range(15)])
        for i in range(15):
            self.t3.set(f"th-{i:02}", "running")
        watcher = self.watch(ledger)
        watcher.run()
        first = [c[0] for c in self.t3.calls]
        self.assertEqual(len(first), 12)
        self.assertEqual(len(set(first)), 12)
        watcher.run()
        self.assertLessEqual(len(self.t3.calls), 24)
        self.assertEqual({c[0] for c in self.t3.calls}, {f"th-{i:02}" for i in range(15)})

    def test_time_budget_stops_the_run(self):
        ledger = FakeLedger([thread(f"th-{i}", "running") for i in range(5)])
        self.t3 = FakeT3(clock=self.clock, step=30.0)
        for i in range(5):
            self.t3.set(f"th-{i}", "running")
        result = self.watch(ledger).run()
        self.assertEqual((len(self.t3.calls), result.polled), (2, 2))

    def test_unreadable_ledger_is_an_error_result(self):
        ledger = FakeLedger([thread("th-1", "running")])
        ledger.fail_threads = True
        result = self.watch(ledger).run()
        self.assertEqual((result.outcome, result.polled, result.recorded, result.posted, result.errors), ("error", 0, 0, 0, 0))
        self.assertEqual(self.t3.calls, [])


class Wiring(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="w2-")
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)

    def test_job_in_default_jobs(self):
        listed = jobs.default_jobs(feed_root=self.tmp / "feed")
        names = [job.name for job in listed]
        self.assertEqual(names[names.index("triggers") + 1], "work-watch")
        job = listed[names.index("work-watch")]
        self.assertEqual((job.every_s, job.due, job.needs_idle_s), (120, None, 0))

    @unittest.skipUnless(importlib.util.find_spec("jarvis.v3.work"), "needs W1a's jarvis/v3/work.py (must not skip at merge)")
    def test_job_without_ledger_is_a_quiet_no_op(self):
        listed = {job.name: job for job in jobs.default_jobs(feed_root=self.tmp / "feed")}
        with (patch.object(config, "V3_DIR", self.tmp / "v3"),
              patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected subprocess"))):
            listed["work-watch"].run()
        self.assertFalse((self.tmp / "v3" / "work").exists())

    def test_import_boundary(self):
        heavy = ("httpx", "openai", "anthropic", "sqlite3")
        named = ("jarvis.llm", "jarvis.agent", "jarvis.tools", "jarvis.v3.work", "jarvis.v3.feed", "jarvis.v3.claude_jobs", "jarvis.v3.budget")
        code = ("import sys\nfrom jarvis.v3 import jobs\njobs.default_jobs()\nassert 'jarvis.v3.work_watch' not in sys.modules\n"
                "import jarvis.v3.work_watch\n"
                f"print(sorted(m for m in sys.modules if m.split('.')[0] in {heavy!r} or m in {named!r}))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                             env={"PYTHONPATH": str(REPO), "HOME": str(self.tmp), "PATH": os.environ.get("PATH", "")})
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "[]"), out.stderr[-400:])


if __name__ == "__main__":
    unittest.main()
