"""Inbox B4 (owner inbox design `docs/plans/2026-10-24-owner-inbox-design.md`,
IN-7, IN-8, §3.2, §4): the owner's snoozes and dismissals.

* **Only rows of rank 5 to 8, judged live.** `POST /inbox/<id>/mark`
  refuses every rank 1 to 4 kind (an approval, a chat question, a task
  question, a proposal, a blocked task) on the row's current rank in a
  fresh projection; the client sends no rank. A mark applies only while the
  row's current rank is 5 or more, so a forged mark on a live row hides
  nothing, and a row that rises to rank 4 shows again.
* **"PR list unavailable" (#320's `prx:list`, kind `pr_unavailable`) is
  never marked**, by id prefix and by kind, tested on synthetic rows (B3 is
  not in this branch).
* **A mark is for the row as it was.** A PR row with a new head, a task
  with a new state, a decision with a new delivery shows again; a check
  with a new step result does not need to.
* **Applying.** `GET /inbox` leaves hidden rows out, `?all=1` keeps them
  flagged; the phone shows what `GET /inbox` shows; snoozes end on a fake
  clock set relative to now.
* **The file** fails closed (malformed, oversized, symlinked, a FIFO: every
  row shows), is 0600 in a 0700 folder, bounded, protected state (v2 layer
  1 refuses a shell write, v1 asks or denies), and no tool, MCP entry,
  session tool or CLI path writes it.

Free and hermetic: temp config and data, ephemeral ports, no model.

    systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 -- env \\
        PYTHONPATH=$PWD TMPDIR=/var/tmp OPENROUTER_API_KEY=sk-dummy-not-a-key \\
        JARVIS_DISCORD_TOKEN=/nonexistent/discord_token.json \\
        .venv/bin/python tests/v2/inbox_marks_check.py

TEST -> MUTATION
================
  the route trusts no rank / skips the check    Route.test_every_rank_1_to_4_kind_is_refused
  apply ignores the current rank                Apply.test_a_forged_mark_on_a_live_row_hides_nothing
  no material key                               Apply.test_a_material_change_shows_the_row_again
  prx markable                                  Route.test_pr_list_unavailable_is_never_marked
  snooze never ends                             Route.test_a_snooze_ends_on_its_time
  a malformed file applied in part              File.test_a_file_that_is_not_ours_hides_nothing
  O_NOFOLLOW dropped on the file                File.test_a_symlinked_file_or_folder_is_refused
  the route not owner-only                      Route.test_only_the_owner_marks
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import inspect
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import inbox_check as IC  # noqa: E402  (its harness and helpers only)
import inbox_prs_check as PRS  # noqa: E402  (its fake gh answers only)
from jarvis import config  # noqa: E402
from jarvis.v2 import inbox, inbox_marks  # noqa: E402
from jarvis.v2.approvals import ApprovalRequest  # noqa: E402
from jarvis.v2.model import TaskState  # noqa: E402
from jarvis.v2.provider import Decision  # noqa: E402

SHA = {c: c * 40 for c in "abc"}


def now_utc():
    return datetime.now(timezone.utc)


class Shift:
    """A clock that is the real now plus an offset the test moves (relative,
    never a fixed date or the machine's uptime)."""

    def __init__(self):
        self.base, self.delta = now_utc(), timedelta()

    def __call__(self):
        return self.base + self.delta


class FakeTimer:
    """`threading.Timer`'s shape, never started for real: the test fires it."""
    made: list = []

    def __init__(self, interval, function, args=()):
        self.interval, self.function, self.args = interval, function, args
        self.started = self.cancelled = False
        self.daemon = False
        FakeTimer.made.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        assert self.started and not self.cancelled
        self.function(*self.args)


def pr_row(n=301, head=SHA["a"], rank=5):
    """#320's row shape (`inbox_prs.pr_row`), synthetic."""
    return inbox._row("pr", rank, f"pr:Johnwang688/jarvis#{n}", f"PR #{n} is ready to merge", "waits on you",
                      detail={"repo": "Johnwang688/jarvis", "number": n, "title": "x", "head": head,
                              "checks": "success"})


def prx_row(kind="pr_unavailable", row_id="prx:list"):
    """#320's "PR list unavailable" row, synthetic."""
    return inbox._row(kind, 5, row_id, "PR list unavailable", "Jarvis could not read the open PRs",
                      detail={"repos": []})


# --- the contract ------------------------------------------------------------------------


class Contract(unittest.TestCase):
    def test_the_bounds_are_pinned(self):
        self.assertEqual((inbox_marks.MARKS_MAX, inbox_marks.SNOOZE_MAX, inbox_marks.MARKABLE_MIN_RANK,
                          inbox_marks.MARKABLE_MAX_RANK, inbox_marks.UNMARKABLE_KINDS,
                          inbox_marks.UNMARKABLE_PREFIXES, inbox_marks.NAME),
                         (500, timedelta(days=30), 5, 8, frozenset({"pr_unavailable"}), frozenset({"prx"}),
                          "marks.json"))

    def test_the_module_and_file_are_protected(self):
        from jarvis.tools import files
        from jarvis.v2 import permissions as v2_permissions
        self.assertIn("jarvis/v2/inbox_marks.py", files.SELF_PROTECTED)
        self.assertTrue(files._self_protected(config.REPO_ROOT / "jarvis/v2/inbox_marks.py"))
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "V2_DATA_DIR", Path(tmp)):
            target = Path(tmp) / "inbox" / "marks.json"
            self.assertIn(target, files._protected_state_paths())
            self.assertTrue(files._self_protected(target), "v1 write tools refuse it")
            self.assertTrue(v2_permissions.denied_file(str(target)), "v2 layer 1 refuses a file tool")

    def test_shell_writes_are_refused_at_v2_layer_1_and_asked_or_denied_on_v1(self):
        from jarvis import permissions as v1_permissions, rules
        from jarvis.v2 import permissions as v2_permissions
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "V2_DATA_DIR", Path(tmp)), \
                patch.object(config, "ALLOWLIST_PATH", Path(tmp) / "cfg" / "allowlist.json"):
            d = Path(tmp)
            (d / "inbox").mkdir()
            target = d / "inbox" / "marks.json"
            for command in (f"echo '{{}}' > {target}", f"cp /tmp/forged.json {target}", f"rm {target}",
                            f": > {target}", f"tee {target} < /tmp/x", f"cp x {d}/*/marks.json",
                            f"cp x {d}/inbox/mark[s].json", f"mv /tmp/x {d}/inbox/marks.json"):
                with self.subTest(command=command):
                    self.assertIsNotNone(v2_permissions._command_writes_protected_state(command), command)
                    self.assertIsNotNone(v2_permissions.denied_command(command), "layer 1 refuses it")
                    self.assertIn(v1_permissions.static_verdict(command).decision, (rules.ASK, rules.DENY))
            self.assertIsNone(v2_permissions._command_writes_protected_state(f"cat {target}"),
                              "reading it is not writing it")


# --- the rule, on synthetic rows -----------------------------------------------------------


class StoreBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.clock = Shift()
        self.store = inbox_marks.MarksStore(self.root, clock=self.clock)

    def forge(self, marks):
        (self.root / "inbox").mkdir(mode=0o700, exist_ok=True)
        self.store.path.write_text(json.dumps(marks))

    def entry(self, row, **over):
        at = self.clock().isoformat(timespec="seconds")
        base = {"snoozed_until": None, "dismissed_at": at, "key": inbox_marks.material_key(row), "at": at}
        base.update(over)
        return base


class Apply(StoreBase):
    def test_only_ranks_5_to_8_are_markable(self):
        for rank in range(1, 9):
            row = inbox._row("task", rank, f"task:t{rank}", "t", "b")
            self.assertEqual(inbox_marks.markable(row), rank >= 5, rank)
        for bad in (0, 9, "8", True, None, 5.0):
            self.assertFalse(inbox_marks.markable({**inbox._row("task", 8, "task:x", "t", "b"), "rank": bad}), bad)

    def test_the_store_itself_refuses_ranks_1_to_4(self):
        """A second guard behind the route's: the store never writes one."""
        for rank in (1, 2, 3, 4):
            row = inbox._row("task", rank, f"task:r{rank}", "t", "b")
            with self.subTest(rank=rank), self.assertRaises(ValueError):
                self.store.mark(row, [row])
        self.assertFalse(self.store.path.exists())

    def test_the_30_day_bound_holds_in_the_store_too(self):
        pr = pr_row()
        for until in (self.clock() + inbox_marks.SNOOZE_MAX + timedelta(seconds=1), self.clock(),
                      self.clock() - timedelta(minutes=1)):
            with self.subTest(until=until), self.assertRaises(ValueError):
                self.store.mark(pr, [pr], snooze_until=until)
        self.store.mark(pr, [pr], snooze_until=self.clock() + inbox_marks.SNOOZE_MAX)
        self.assertEqual(self.store.apply([pr], flag=False), [], "exactly 30 days is allowed")
        # On read: a snooze longer than 30 days after its `at` is not one of ours, and the row shows.
        at = self.clock()
        self.forge({pr["id"]: self.entry(pr, dismissed_at=None, at=at.isoformat(timespec="seconds"),
                                         snoozed_until=(at + timedelta(days=31)).isoformat(timespec="seconds"))})
        self.assertEqual(self.store.read(), {})
        self.assertEqual(self.store.apply([pr], flag=False), [pr])

    def test_a_forged_mark_on_a_live_row_hides_nothing(self):
        """A mark applies only while the row's current rank is 5 or more,
        whatever the file says: here the key matches exactly."""
        live = [inbox._row(kind, rank, f"{p}:x", "t", "b")
                for kind, rank, p in (("approval", 1, "appr"), ("question", 2, "q"), ("task_question", 2, "tq"),
                                      ("proposal", 3, "prop"), ("task", 4, "task"))]
        self.forge({r["id"]: self.entry(r) for r in live})
        self.assertEqual(len(self.store.read()), 5, "the file itself is well formed")
        self.assertEqual(self.store.apply(live, flag=False), live)
        self.assertNotIn("hidden", json.dumps(self.store.apply(live, flag=True)))
        self.assertEqual([r["markable"] for r in self.store.apply(live, flag=True)], [False] * 5)

    def test_a_row_that_rises_to_rank_4_shows_again(self):
        """A failed task (8) dismissed, then blocked (4) under the same id."""
        failed = inbox._row("task", 8, "task:k1", "task k1 failed", "for your information",
                            asked_at="2026-10-28T10:00:00+00:00", detail={"state": "failed"})
        self.store.mark(failed, [failed])
        self.assertEqual(self.store.apply([failed], flag=False), [])
        blocked = inbox._row("task", 4, "task:k1", "task k1 is blocked", "waits",
                             asked_at="2026-10-28T10:00:00+00:00", detail={"state": "blocked"})
        self.assertEqual(self.store.apply([blocked], flag=False), [blocked])
        # Even with a mark forged to match it exactly.
        self.forge({"task:k1": self.entry(blocked)})
        self.assertEqual(self.store.apply([blocked], flag=False), [blocked])

    def test_a_material_change_shows_the_row_again(self):
        pr = pr_row(head=SHA["a"])
        self.store.mark(pr, [pr])
        self.assertEqual(self.store.apply([pr], flag=False), [], "dismissed")
        self.assertEqual(self.store.apply([pr_row(head=SHA["a"])], flag=False), [], "same head, still hidden")
        new = pr_row(head=SHA["b"])
        self.assertEqual(self.store.apply([new], flag=False), [new], "new commits bring it back")
        # A check that gets a new step result keeps its mark; one that is done does not.
        check = inbox._row("check", 7, "chk:c1", "Live check c1: 0 of 2 steps marked", "b",
                           detail={"done": False, "results": []})
        self.store.mark(check, [check, pr])
        stepped = inbox._row("check", 7, "chk:c1", "Live check c1: 1 of 2 steps marked", "b",
                             detail={"done": False, "results": [{"step": 0}]})
        self.assertEqual(self.store.apply([stepped], flag=False), [])
        done = inbox._row("check", 8, "chk:c1", "Live check c1 done", "b", detail={"done": True})
        self.assertEqual(self.store.apply([done], flag=False), [done])
        # A decision recorded for a closed chat (6 -> 8, a new delivery) comes back.
        dec = inbox._row("decision", 6, "dec:d1", "t", "b", detail={"delivery": None})
        self.store.mark(dec, [dec])
        later = inbox._row("decision", 8, "dec:d1", "t", "b", detail={"delivery": "closed"})
        self.assertEqual(self.store.apply([later], flag=False), [later])
        # A task that failed again (a new last update) comes back.
        t1 = inbox._row("task", 8, "task:k2", "t", "b", asked_at="2026-10-28T10:00:00+00:00",
                        detail={"state": "failed"})
        self.store.mark(t1, [t1])
        t2 = {**t1, "asked_at": "2026-10-28T11:00:00+00:00"}
        self.assertEqual(self.store.apply([t2], flag=False), [t2])

    def test_pr_list_unavailable_is_never_marked_by_kind_or_prefix(self):
        for row in (prx_row(), prx_row(row_id="zz:list"), prx_row(kind="other", row_id="prx:list")):
            with self.subTest(row=(row["kind"], row["id"])):
                self.assertFalse(inbox_marks.markable(row))
                with self.assertRaises(ValueError):
                    self.store.mark(row, [row])
                self.forge({row["id"]: self.entry(row)})
                self.assertEqual(self.store.apply([row], flag=False), [row], "a forged mark hides nothing")
        self.assertTrue(inbox_marks.markable(pr_row()), "an ordinary PR row is markable")

    def test_flag_mode_says_markable_and_hidden(self):
        pr, other = pr_row(), pr_row(302)
        until = self.clock() + timedelta(hours=1)
        entry = self.store.mark(pr, [pr, other], snooze_until=until)
        flagged = self.store.apply([pr, other], flag=True)
        self.assertEqual(flagged[0]["hidden"], {"snoozed_until": entry["snoozed_until"], "dismissed_at": None})
        self.assertNotIn("hidden", flagged[1])
        self.assertEqual([r["markable"] for r in flagged], [True, True])
        self.assertNotIn("hidden", pr, "rows are copied, never changed")

    def test_the_store_is_bounded_and_prunes_on_write(self):
        rows = [pr_row(n) for n in range(1, inbox_marks.MARKS_MAX + 6)]
        for i, row in enumerate(rows):
            self.clock.delta = timedelta(seconds=i)
            self.store.mark(row, rows)
        marks = self.store.read()
        self.assertEqual(len(marks), inbox_marks.MARKS_MAX)
        self.assertNotIn(rows[0]["id"], marks, "the oldest go first")
        self.assertIn(rows[-1]["id"], marks)
        # A row gone, a row changed, a snooze over: all dropped at the next write.
        live = rows[10:]
        live[0] = pr_row(11, head=SHA["c"])
        self.store.mark(rows[-1], live, snooze_until=self.clock() + timedelta(minutes=5))
        self.clock.delta += timedelta(minutes=6)
        self.store.mark(rows[-2], live)
        marks = self.store.read()
        self.assertNotIn(rows[5]["id"], marks, "its row is gone")
        self.assertNotIn(rows[10]["id"], marks, "its row changed")
        self.assertNotIn(rows[-1]["id"], marks, "its snooze ended")
        self.assertIn(rows[-2]["id"], marks)


# --- the file -----------------------------------------------------------------------------


class File(StoreBase):
    def test_a_file_that_is_not_ours_hides_nothing(self):
        pr = pr_row()
        good = self.entry(pr)
        bad_files = {
            "not json": "{",
            "a list": "[]",
            "an extra field": json.dumps({pr["id"]: {**good, "rank": 8}}),
            "both set": json.dumps({pr["id"]: {**good, "snoozed_until": good["at"]}}),
            "neither set": json.dumps({pr["id"]: {**good, "dismissed_at": None}}),
            "a naive time": json.dumps({pr["id"]: {**good, "dismissed_at": "2026-10-28T10:00:00"}}),
            "a bad key": json.dumps({pr["id"]: {**good, "key": "x"}}),
            "one bad entry beside a good one": json.dumps({pr["id"]: good, "pr:x#2": {"key": 1}}),
            "too many": json.dumps({f"pr:x#{i}": good for i in range(inbox_marks.MARKS_MAX + 1)} | {pr["id"]: good}),
            "too large": json.dumps({pr["id"]: good, "pad:x": good}) + " " * (inbox_marks._FILE_MAX + 1),
            "not utf-8": b"\xff\xfe".decode("latin-1"),
        }
        for label, text in bad_files.items():
            with self.subTest(label=label):
                self.forge({})
                self.store.path.write_text(text, encoding="latin-1" if label == "not utf-8" else "utf-8")
                self.assertEqual(self.store.read(), {})
                self.assertEqual(self.store.apply([pr], flag=False), [pr], "every row shows")
        # The good file, for contrast, hides it.
        self.forge({pr["id"]: good})
        self.assertEqual(self.store.apply([pr], flag=False), [])
        # A mark written over a file that is not ours starts from empty.
        self.store.path.write_text("{")
        self.store.mark(pr, [pr])
        self.assertEqual(list(self.store.read()), [pr["id"]])

    def test_a_symlinked_file_or_folder_is_refused(self):
        pr = pr_row()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "marks.json").write_text(json.dumps({pr["id"]: self.entry(pr)}))
        (self.root / "inbox").mkdir(mode=0o700)
        self.store.path.symlink_to(elsewhere / "marks.json")
        self.assertEqual(self.store.read(), {}, "a symlinked file is never read")
        self.assertEqual(self.store.apply([pr], flag=False), [pr])
        before = (elsewhere / "marks.json").read_text()
        self.store.mark(pr, [pr])     # replaces the link itself, never writes through it
        self.assertEqual((elsewhere / "marks.json").read_text(), before)
        self.assertFalse(self.store.path.is_symlink())
        # A symlinked `inbox/` folder: nothing read, nothing written through it.
        self.store.path.unlink()
        (self.root / "inbox").rmdir()
        (self.root / "inbox").symlink_to(elsewhere, target_is_directory=True)
        self.assertEqual(self.store.read(), {})
        with self.assertRaises(OSError):
            self.store.mark(pr, [pr])
        self.assertEqual((elsewhere / "marks.json").read_text(), before)

    def test_a_fifo_never_blocks_and_hides_nothing(self):
        (self.root / "inbox").mkdir(mode=0o700)
        os.mkfifo(self.store.path)
        pr = pr_row()
        self.assertEqual(self.store.apply([pr], flag=False), [pr])

    def test_modes_whatever_the_umask_or_what_was_there(self):
        old = os.umask(0)
        try:
            (self.root / "inbox").mkdir(mode=0o777)
            self.store.path.write_text("{}")
            os.chmod(self.store.path, 0o666)
            pr = pr_row()
            self.store.mark(pr, [pr])
        finally:
            os.umask(old)
        self.assertEqual(IC.mode(self.root / "inbox"), 0o700)
        self.assertEqual(IC.mode(self.store.path), 0o600)
        self.assertEqual([p.name for p in (self.root / "inbox").iterdir()], ["marks.json"], "no temp file left")


# --- the route, on a real daemon -----------------------------------------------------------


class Route(unittest.TestCase):
    setUp = IC.Live.setUp
    call = IC.Live.call
    ids = IC.Live.ids
    ask = IC.Live.ask
    spy = IC.Live.spy
    clarifying = IC.Live.clarifying
    proposal = IC.Live.proposal
    question = IC.Live.question

    def mark(self, row_id, body, status=200, **kw):
        from urllib.parse import quote
        return self.call("POST", f"/inbox/{quote(row_id, safe='')}/mark", body, status, **kw)

    def all_rows(self):
        return {r["id"]: r for r in self.call("GET", "/inbox?all=1")}

    def failed_task(self, brief="y"):
        t = self.stores.tasks.create(self.project.id, brief)
        self.stores.tasks.save(t)
        for step in (TaskState.CLARIFYING, TaskState.PLANNED, TaskState.RUNNING, TaskState.FAILED):
            self.stores.tasks.transition(t.id, step)
        return f"task:{t.id}"

    def blocked_task(self):
        t = self.stores.tasks.create(self.project.id, "x")
        self.stores.tasks.save(t)
        self.stores.tasks.transition(t.id, TaskState.CLARIFYING)
        self.stores.tasks.transition(t.id, TaskState.BLOCKED)
        return f"task:{t.id}"

    def decision(self):
        rec = self.daemon.inbox.decisions.create(thread_id="th000009", project_id=None, task_id=None,
                                                 origin="Claude · chat (th000009)", question="q?",
                                                 options=["a", "b"], default=None, why="w")
        return f"dec:{rec['id']}"

    def check(self):
        rec = self.daemon.inbox.checks.create(thread_id=None, project_id=None, task_id=None,
                                              created_by="Imported by you", title="t", prs=[],
                                              steps=[{"do": "d", "expect": "e"}], imported_from="/x/docs/plans/a.md")
        return f"chk:{rec['id']}"

    def missed(self):
        req = ApprovalRequest(tool="Bash", args={}, command="x", provider="codex", thread_id="th000008")
        req.req_id, req.asked_at, req.timeout_s = "MISSa1", now_utc().isoformat(), 120.0
        self.assertTrue(self.daemon.inbox.missed.record(req, "timeout"))
        return "missed:MISSa1"

    # -- refused for ranks 1 to 4 ------------------------------------------------------

    def test_every_rank_1_to_4_kind_is_refused(self):
        req, _out, worker = self.ask(thread_id="th-r1")
        appr = f"appr:{req.req_id}"
        tq = f"tq:{self.clarifying().id}:0"
        prop = f"prop:{self.proposal().id}"
        blocked = self.blocked_task()
        _thread, qrow = self.question()
        live = {appr: 1, qrow["id"]: 2, tq: 2, prop: 3, blocked: 4}
        rows = self.all_rows()
        self.assertEqual({i: rows[i]["rank"] for i in live}, live)
        self.assertEqual({i: rows[i]["markable"] for i in live}, dict.fromkeys(live, False))
        for row_id in live:
            for body in ({"dismiss": True}, {"snooze_until": (now_utc() + timedelta(hours=1)).isoformat()}):
                with self.subTest(row=row_id, body=body):
                    got = self.call("POST", f"/inbox/{row_id.replace(':', '%3A')}/mark", body, 409)
                    self.assertIn("cannot be snoozed or dismissed", got["error"])
        self.assertEqual(self.daemon.inbox.marks.read(), {}, "nothing was written")
        self.assertTrue(set(live) <= set(self.ids()), "every one still shows")
        # The client cannot claim a rank: an extra field is a 400.
        self.call("POST", f"/inbox/{blocked}/mark", {"dismiss": True, "rank": 8}, 400)
        self.brain.awaiting.clear()
        self.daemon.approvals.resolve(req.req_id, Decision.DENY)
        worker.join(5)

    def test_the_rank_is_judged_live_at_mark_time(self):
        """The row's rank is read from a fresh projection when the owner
        marks: a row that rose to rank 4 since the HUD read it is refused."""
        row_id = self.failed_task()
        self.assertEqual(self.all_rows()[row_id]["rank"], 8)
        real = inbox._task_rows

        def risen(daemon, hidden):
            return [{**r, "rank": inbox.RANK_BLOCKED} if r["id"] == row_id else r for r in real(daemon, hidden)]
        with patch.object(inbox, "_task_rows", risen):
            self.mark(row_id, {"dismiss": True}, 409)
        self.assertEqual(self.daemon.inbox.marks.read(), {})
        self.mark(row_id, {"dismiss": True})
        with patch.object(inbox, "_task_rows", risen):
            self.assertIn(row_id, self.ids(), "a marked row that rises to rank 4 shows again")

    def test_ranks_5_to_8_are_accepted_and_undone(self):
        rows = {self.failed_task(): 8, self.decision(): 6, self.check(): 7, self.missed(): 8}
        got = self.all_rows()
        self.assertEqual({i: got[i]["rank"] for i in rows}, rows)
        for row_id in rows:
            with self.subTest(row=row_id):
                reply = self.mark(row_id, {"dismiss": True})
                self.assertEqual(reply["id"], row_id)
                self.assertIsNotNone(reply["hidden"]["dismissed_at"])
                self.assertNotIn(row_id, self.ids())
                flagged = self.all_rows()[row_id]
                self.assertEqual(flagged["hidden"], reply["hidden"])
                self.assertTrue(flagged["markable"])
        self.assertEqual(self.ids(), [], "everything is hidden")
        for row_id in rows:
            self.assertEqual(self.mark(row_id, {"clear": True}), {"ok": True, "id": row_id, "hidden": None,
                                                                   "cleared": True})
        self.assertEqual(set(self.ids()), set(rows))
        self.assertFalse(self.mark("task:nothing", {"clear": True})["cleared"], "an undo of nothing is harmless")

    def test_unknown_vanished_and_malformed(self):
        row_id = self.failed_task()
        soon = (now_utc() + timedelta(hours=1)).isoformat()
        self.mark("task:nothing", {"dismiss": True}, 409)
        self.mark("dec:d000000000000", {"snooze_until": soon}, 409)
        for bad in ("nocolon", "zz:x", "", "%ZZ"):
            self.call("POST", f"/inbox/{bad}/mark", {"dismiss": True}, 400)
        for body in ({}, {"dismiss": False}, {"dismiss": 1}, {"clear": False}, {"dismiss": True, "clear": True},
                     {"snooze_until": "tomorrow"}, {"snooze_until": "2030-01-01T10:00:00"},
                     {"snooze_until": (now_utc() - timedelta(minutes=1)).isoformat()},
                     {"snooze_until": (now_utc() + timedelta(days=31)).isoformat()},
                     {"snooze_until": 1}, {"snooze_until": None}, [], "x", {"hide": True}):
            with self.subTest(body=body):
                self.mark(row_id, body, 400)
        self.call("POST", f"/inbox/{row_id}/mark?x=1", {"dismiss": True}, 400)
        self.assertEqual(self.daemon.inbox.marks.read(), {})
        self.mark(row_id, {"snooze_until": (now_utc() + timedelta(days=29)).isoformat()})

    def test_only_the_owner_marks(self):
        row_id = self.failed_task()
        api, hud = self.daemon.port, self.daemon.face_port
        for kw in ({"owner": False}, {"owner": False, "origin": f"http://127.0.0.1:{api}"},
                   {"owner": False, "port": hud}, {"owner": False, "port": hud, "origin": "null"}):
            with self.subTest(kw=kw):
                self.mark(row_id, {"dismiss": True}, 403, **kw)
                self.mark(row_id, {"clear": True}, 403, **kw)
                self.call("GET", "/inbox?all=1", None, 403, **kw)
        self.assertEqual(self.daemon.inbox.marks.read(), {})

    def test_inbox_changed_carries_the_id_only(self):
        row_id = self.failed_task()
        before = len(self.changes)
        self.mark(row_id, {"dismiss": True})
        self.mark(row_id, {"clear": True})
        news = [c for c in self.changes[before:] if c["data"]["reason"] == "inbox_marked"]
        self.assertEqual([c["data"] for c in news], [{"ids": [row_id], "reason": "inbox_marked"}] * 2)
        self.assertEqual({k for c in news for k in c}, {"kind", "data"})

    def test_a_snooze_ends_on_its_time(self):
        clock = Shift()
        self.daemon.inbox.marks.clock = clock
        row_id = self.failed_task()
        self.mark(row_id, {"snooze_until": (clock() + timedelta(hours=1)).isoformat()})
        self.assertNotIn(row_id, self.ids())
        clock.delta = timedelta(minutes=59)
        self.assertNotIn(row_id, self.ids())
        clock.delta = timedelta(minutes=61)
        self.assertIn(row_id, self.ids(), "the snooze ended")
        self.assertNotIn("hidden", self.all_rows()[row_id])
        # The next write drops it.
        other = self.decision()
        self.mark(other, {"dismiss": True})
        self.assertEqual(list(self.daemon.inbox.marks.read()), [other])

    def test_a_snooze_that_ends_is_announced_by_one_timer(self):
        """Review of #322, MED: the row comes back when its snooze ends, not at
        the next unrelated event. One timer for the earliest end, by the
        store's clock (a fake one, relative to now), re-armed on each mark
        write; it publishes `inbox_changed` with the ids only."""
        clock = Shift()
        self.daemon.inbox.marks.clock = clock
        FakeTimer.made = []
        self.daemon.inbox._timer_factory = FakeTimer
        first, second, gone = self.failed_task("a"), self.failed_task("b"), self.failed_task("c")
        self.mark(gone, {"dismiss": True})
        self.assertEqual(FakeTimer.made, [], "a dismissal arms nothing")
        self.mark(second, {"snooze_until": (clock() + timedelta(hours=2)).isoformat()})
        self.mark(first, {"snooze_until": (clock() + timedelta(hours=1)).isoformat()})
        live = [t for t in FakeTimer.made if t.started and not t.cancelled]
        self.assertEqual(len(live), 1, "one timer, the earlier one replaced")
        self.assertAlmostEqual(live[0].interval, 3600 + inbox.SNOOZE_SLACK_S, delta=1)
        self.assertTrue(live[0].daemon)
        before = len(self.changes)
        clock.delta = timedelta(minutes=30)
        live[0].fire()                       # early (a clock that drifted): nothing ended, re-armed
        self.assertEqual([c for c in self.changes[before:] if c["data"]["reason"] == "snooze_ended"], [])
        live = [t for t in FakeTimer.made if t.started and not t.cancelled and t is not live[0]]
        self.assertAlmostEqual(live[-1].interval, 1800 + inbox.SNOOZE_SLACK_S, delta=1)
        clock.delta = timedelta(minutes=61)
        live[-1].fire()
        ended = [c for c in self.changes[before:] if c["data"]["reason"] == "snooze_ended"]
        self.assertEqual([c["data"] for c in ended], [{"ids": [first], "reason": "snooze_ended"}])
        self.assertEqual({k for c in ended for k in c}, {"kind", "data"})
        self.assertIn(first, self.ids(), "back in the list")
        self.assertNotIn(second, self.ids())
        nxt = FakeTimer.made[-1]
        self.assertAlmostEqual(nxt.interval, 59 * 60 + inbox.SNOOZE_SLACK_S, delta=1, msg="the next one armed")
        # Undo re-arms too: with no snooze left, nothing is armed.
        made = len(FakeTimer.made)
        self.mark(second, {"clear": True})
        self.assertTrue(nxt.cancelled)
        self.assertEqual(len(FakeTimer.made), made, "nothing left to wait for")
        # A snooze days ahead waits at most an hour at a time.
        self.mark(second, {"snooze_until": (clock() + timedelta(days=3)).isoformat()})
        self.assertEqual(FakeTimer.made[-1].interval, inbox.SNOOZE_RECHECK_S)
        # Stopped: cancelled, and a late fire neither publishes nor re-arms.
        armed = FakeTimer.made[-1]
        self.daemon.inbox.stop_snoozes()
        self.assertTrue(armed.cancelled)
        count = len(FakeTimer.made)
        self.daemon.inbox._snoozes_ended(clock())
        self.assertEqual(len(FakeTimer.made), count)

    def test_daemon_start_arms_and_stop_cancels(self):
        """At start the timer is armed from the file (a snooze from a previous run)."""
        clock = Shift()
        self.daemon.inbox.marks.clock = clock
        row_id = self.failed_task()
        self.mark(row_id, {"snooze_until": (clock() + timedelta(hours=1)).isoformat()})
        self.daemon.inbox.stop_snoozes()
        FakeTimer.made = []
        self.daemon.inbox._timer_factory = FakeTimer
        self.daemon.inbox.start_snoozes()
        self.assertEqual(len(FakeTimer.made), 1)
        self.assertTrue(FakeTimer.made[0].started)
        self.daemon.stop()
        self.assertTrue(FakeTimer.made[0].cancelled, "Daemon.stop() cancels it")

    def test_a_real_timer_brings_the_row_back(self):
        row_id = self.failed_task()
        self.mark(row_id, {"snooze_until": (now_utc() + timedelta(seconds=1.5)).isoformat()})
        self.assertNotIn(row_id, self.ids())
        IC.eventually(lambda: any(c["data"]["reason"] == "snooze_ended" and c["data"]["ids"] == [row_id]
                                  for c in self.changes), timeout=8)
        self.assertIn(row_id, self.ids())

    def test_pr_list_unavailable_is_never_marked(self):
        """B3 (#320) is not in this branch: its two rows are synthetic, and its
        prefixes are made known as #320 makes them."""
        rows = [prx_row(), pr_row(), prx_row(row_id="missed:oddone")]
        with patch.object(inbox, "_missed_rows", lambda daemon: [dict(r) for r in rows]), \
                patch.object(inbox, "_KNOWN", inbox._KNOWN | {"pr", "prx"}):
            got = self.call("POST", "/inbox/prx%3Alist/mark", {"dismiss": True}, 409)
            self.assertIn("cannot read", got["error"])
            self.mark("missed:oddone", {"dismiss": True}, 409)      # refused by its kind
            self.assertEqual(self.daemon.inbox.marks.read(), {})
            self.mark("pr:Johnwang688/jarvis#301", {"dismiss": True})
            ids = self.ids()
            self.assertIn("prx:list", ids)
            self.assertNotIn("pr:Johnwang688/jarvis#301", ids)
            flagged = self.all_rows()
            self.assertFalse(flagged["prx:list"]["markable"])
            self.assertTrue(flagged["pr:Johnwang688/jarvis#301"]["markable"])
            rows[1] = pr_row(head=SHA["b"])
            self.assertIn("pr:Johnwang688/jarvis#301", self.ids(), "a new head brings it back")

    # -- B3's real PR rows (#320 merged) ---------------------------------------------

    def poll(self, answer):
        """One PR poll over a fake `gh`: `answer` is its bytes, or a GhFailed code."""
        from jarvis.v2 import inbox_prs
        src = self.daemon.inbox.prs
        src.repos = lambda daemon: {PRS.REPO: [self.project.id]}

        def list_repo(repo):
            if isinstance(answer, str):
                raise inbox_prs.GhFailed(answer)
            return answer
        src.list_repo = list_repo
        src.poll()

    def test_a_real_pr_row_is_markable_and_new_commits_bring_it_back(self):
        pr_id = f"pr:{PRS.REPO}#5"
        self.poll(PRS.answer(PRS.pr(5)))
        row = self.all_rows()[pr_id]
        self.assertEqual((row["kind"], row["rank"], row["markable"]), ("pr", 5, True))
        self.mark(pr_id, {"dismiss": True})
        self.assertNotIn(pr_id, self.ids())
        self.poll(PRS.answer(PRS.pr(5, title="Change 5, retitled")))
        self.assertNotIn(pr_id, self.ids(), "the same head stays dismissed")
        self.poll(PRS.answer(PRS.pr(5, headRefOid="f" * 40)))
        self.assertIn(pr_id, self.ids(), "a new head (new commits) brings it back")

    def test_a_pr_that_leaves_and_returns_shows_again_but_a_failed_poll_is_no_leave(self):
        pr_id = f"pr:{PRS.REPO}#5"
        self.poll(PRS.answer(PRS.pr(5), PRS.pr(6)))
        self.mark(pr_id, {"dismiss": True})
        self.mark(f"pr:{PRS.REPO}#6", {"dismiss": True})
        # A failed read: both PRs are gone from the list and "PR list
        # unavailable" shows, but neither PR left, so the marks stay.
        self.poll("auth")
        self.assertIn("prx:list", self.ids())
        self.assertIn(pr_id, self.daemon.inbox.marks.read())
        self.poll(PRS.answer(PRS.pr(5), PRS.pr(6)))
        self.assertNotIn(pr_id, self.ids(), "back after a failed read: still dismissed")
        # PR 5 leaves a read that went well (merged or closed), then comes back (reopened).
        self.poll(PRS.answer(PRS.pr(6)))
        self.assertNotIn(pr_id, self.daemon.inbox.marks.read(), "its mark is forgotten")
        self.assertIn(f"pr:{PRS.REPO}#6", self.daemon.inbox.marks.read(), "the other keeps its own")
        self.poll(PRS.answer(PRS.pr(5), PRS.pr(6)))
        self.assertIn(pr_id, self.ids(), "it left and returned: it shows")
        self.assertNotIn(f"pr:{PRS.REPO}#6", self.ids())

    def test_the_real_pr_list_unavailable_row_is_never_marked(self):
        self.poll("auth")
        rows = self.all_rows()
        row = rows["prx:list"]
        self.assertEqual((row["kind"], row["rank"], row["markable"]), ("pr_unavailable", 5, False))
        for body in ({"dismiss": True}, {"snooze_until": (now_utc() + timedelta(hours=1)).isoformat()}):
            self.call("POST", "/inbox/prx%3Alist/mark", body, 409)
        self.assertEqual(self.daemon.inbox.marks.read(), {})
        # A forged mark with its exact key hides nothing.
        store = self.daemon.inbox.marks
        at = store.clock().isoformat(timespec="seconds")
        store.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        store.path.write_text(json.dumps({"prx:list": {"snoozed_until": None, "dismissed_at": at,
                                                       "key": inbox_marks.material_key(row | {"hidden": None}), "at": at}}))
        self.assertEqual(len(store.read()), 1)
        self.assertIn("prx:list", self.ids())
        self.assertNotIn("hidden", self.all_rows()["prx:list"])

    def test_the_phone_shows_what_get_inbox_shows_and_cannot_mark(self):
        from jarvis.v2 import remote_answer, remote_api
        a, b = self.failed_task("a"), self.failed_task("b")
        self.mark(a, {"dismiss": True})
        phone = [r["id"] for r in remote_api.inbox(self.daemon)["rows"]]
        self.assertEqual(phone, self.ids())
        self.assertEqual(phone, [b])
        # Read-only on the phone (this package): no read or answer route is a mark.
        self.assertIsNone(remote_answer._match(["inbox", a, "mark"]), "the phone has no mark route")
        self.assertIsNone(remote_api._match(["inbox", a, "mark"]))

    def test_a_dismissed_decision_comes_back_when_its_row_changes(self):
        dec = self.decision()
        self.mark(dec, {"dismiss": True})
        self.assertNotIn(dec, self.ids())
        # Answered for a chat that is gone: recorded (rank 8), a new row.
        from jarvis.v2 import decisions
        decision_id = dec.split(":", 1)[1]
        self.daemon.inbox.decisions.settle(decision_id, choice=0, text=None)
        self.daemon.inbox.decisions.delivered(decision_id, "closed")
        row = self.all_rows()[dec]
        self.assertEqual(row["rank"], inbox.RANK_RECORDED)
        self.assertIn(dec, self.ids())
        self.assertTrue(decisions.RECORDED_WINDOW)


# --- no tool, MCP entry, session tool or CLI writes a mark ----------------------------------


NEEDLES = (r"mark_row", r"MarksStore", r"inbox_marked", r"""/mark["'?]""", r"\binbox_marks\.(?!py\b)",
           r"import inbox_marks", r"from \.+inbox_marks", r"marks\.json")


class NoToolPath(unittest.TestCase):
    def test_only_the_inbox_names_the_marks(self):
        """Grep: across `jarvis/` only the inbox, the store and the files
        rule (which names the module and file to protect them) say it."""
        found = {}
        for path in sorted((ROOT / "jarvis").rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in ("jarvis/v2/inbox.py", "jarvis/v2/inbox_marks.py"):
                continue
            text = path.read_text(errors="replace")
            if rel == "jarvis/tools/files.py":
                from jarvis.tools import files
                exempt = inspect.getsource(files._protected_state_paths)
                self.assertIn(exempt, text)
                text = text.replace(exempt, "")
            for needle in NEEDLES:
                if re.search(needle, text):
                    found.setdefault(rel, []).append(needle)
        self.assertEqual(found, {}, found)


import live_checks_check as LC  # noqa: E402  (owner_post_check's harness, through it)


class Sweep(LC.Base):
    def test_a_sweep_of_every_session_tool_never_marks(self):
        """Every tool a Claude or Codex owner chat holds, called with a row id
        and a mark body in every argument, never reaches the marks."""
        from jarvis import tools
        from jarvis.v2 import session_tools
        _thread, cid = self.desk_check()
        row = f"chk:{cid}"
        chat = self.chat(LC.CLAUDE)
        self.turn(chat)
        names = sorted({t.name for t in session_tools.for_provider(LC.CLAUDE)}
                       | {t.name for t in session_tools.for_provider(LC.CODEX)})
        self.assertIn("owner_post", names)
        stop = AssertionError("a tool reached the marks")
        with patch.object(inbox_marks.MarksStore, "mark", side_effect=stop) as mark, \
                patch.object(inbox_marks.MarksStore, "clear", side_effect=stop) as clear, \
                patch.object(inbox, "mark_row", side_effect=stop) as route:
            for name in names:
                schema = tools.REGISTRY[name].schema
                arguments = {}
                for key, prop in schema["properties"].items():
                    kind = prop.get("type")
                    arguments[key] = (f"/inbox/{row}/mark" if kind == "string" else
                                      [row, {"dismiss": True}] if kind == "array" else
                                      {"dismiss": True, "id": row} if kind == "object" else 0)
                with self.subTest(tool=name):
                    self.mcp(chat, name, arguments)
            self.assertEqual((mark.call_count, clear.call_count, route.call_count), (0, 0, 0))
        self.finish(chat)
        self.assertEqual(self.d.inbox.marks.read(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
