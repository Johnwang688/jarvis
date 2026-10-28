"""The morning review (work package MR, docs/plans/2026-10-28-morning-review.md;
`jarvis/v2/review.py`, `quiet_defer.settle_many`/`approve_many`,
`quiet.last_window`, `push.Push.review_opens`).

What it pins (the plan's unit list):

  - the night: 22:59 is before, 23:00 overnight, 07:00 the end; quiet hours
    off; a daylight-saving night; an open decision asked in it is in; PRs,
    checks and rank 8 are only counted (rank 8) or left out; a live rank-1
    approval is "Live now", with no tag and never an item;
  - tags: another id, digest, card or a restart is another tag; no digest
    and no unredacted argument in any payload;
  - the subject: order-free, holds the tags, at most 64 characters; one id
    or tag different is another subject; set A's assertion is refused for
    set B; a `defer:<id>` assertion is refused at the group route and a
    group's at the row route;
  - all or nothing: one of three left at the desk between the options and
    the answer is 409 `changed`, zero grants, the file unchanged, no write,
    nothing delivered; likewise a task resumed, a deferral past 24 h, a tag
    that no longer matches (a restart), a closed chat, a withheld card;
  - success: one write; each grant only for its exact call, once, 30
    minutes; two deferrals in one chat are one message, two in one blocked
    task one resume;
  - limits: 11 items, a duplicate, a malformed tag (400);
  - the phone's authority: no assertion 401; each of the five §4.1
    failures refuses a group but not a per-row retry; Leave needs no
    assertion;
  - owner-only: 403 from the API listener, 401 without a session, 403 for a
    wrong Origin, 400 for a wrong Host; neither listener serves the other's
    paths; no tool, MCP entry or session tool names the module or its
    paths; shell probes DENY at v2 layer 1 and in v1;
  - push: the payload byte for byte; `opens_here` after the morning push,
    not after the next push nor after a restart.

Free and hermetic: temporary HOME and data; daemons on ephemeral loopback
ports (never 8402, 8403, 8405 or 8408); every clock a fake; real WebAuthn
assertions from a software authenticator.

Run: PYTHONPATH=. .venv/bin/python tests/v2/review_check.py
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import secrets as pysecrets
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qd = _load("quiet_defer_check")
ra = qd.ra
pc = _load("push_check")

from jarvis import config, rules  # noqa: E402
from jarvis.v2 import inbox, push as push_mod, quiet_defer, remote_answer, review  # noqa: E402
from jarvis.v2.model import ProviderName as PN, Role, TaskState  # noqa: E402
from jarvis.v2.provider import Decision  # noqa: E402
from jarvis.v2.quiet import QuietHours, window_bounds  # noqa: E402

at, Clock, request = qd.at, qd.Clock, qd.request
# A GitHub-shaped token in an argument: the card's public projection redacts
# it, so it must never reach a payload (assembled at run time).
SECRET = "ghp_" + "Kx9" * 12


def item_ids(report) -> list[str]:
    return [i["row"]["id"] for i in report["overnight"] + report["before"]]


def picked(report, *row_ids) -> list[dict]:
    """`{id, tag}` of these rows, as the page sends them."""
    found = {i["row"]["id"]: i for i in report["overnight"] + report["before"]}
    return [{"id": r, "tag": found[r]["tag"]} for r in row_ids]


class Writes:
    """Counts the deferral store's writes."""

    def __init__(self, store):
        self.count = 0
        real = store._write

        def counted(*a, **k):
            self.count += 1
            return real(*a, **k)
        store._write = counted


# -- the night (MR-1) ------------------------------------------------------------------------


class Night(unittest.TestCase):
    def test_window_bounds(self):
        zone = qd.ZONE
        s, e = dtime(23), dtime(7)
        day0 = datetime(2026, 3, 10, tzinfo=zone)
        night = (day0 + timedelta(hours=23), day0 + timedelta(days=1, hours=7))
        for when in (day0 + timedelta(days=1, hours=7, minutes=30), day0 + timedelta(days=1, hours=6, minutes=59),
                     day0 + timedelta(days=1, hours=7), day0 + timedelta(days=1, hours=22, minutes=59),
                     day0 + timedelta(hours=23), day0 + timedelta(days=1, hours=1)):
            self.assertEqual(window_bounds(s, e, when), night, when)
        self.assertEqual(window_bounds(s, e, day0 + timedelta(days=1, hours=23)),
                         (night[0] + timedelta(days=1), night[1] + timedelta(days=1)), "23:00 starts tonight's")
        # A window inside one day.
        noon = (day0 + timedelta(hours=13), day0 + timedelta(hours=14))
        self.assertEqual(window_bounds(dtime(13), dtime(14), day0 + timedelta(hours=13, minutes=30)), noon)
        self.assertEqual(window_bounds(dtime(13), dtime(14), day0 + timedelta(hours=15)), noon)
        self.assertEqual(window_bounds(dtime(13), dtime(14), day0 + timedelta(hours=12)),
                         (noon[0] - timedelta(days=1), noon[1] - timedelta(days=1)))
        print("ok  the night: 23:00 to 07:00 (current, or the last that ended); a same-day window too")

    def test_a_daylight_saving_night(self):
        from zoneinfo import ZoneInfo
        york = ZoneInfo("America/New_York")
        start, end = window_bounds(dtime(23), dtime(7), datetime(2026, 3, 8, 7, 30, tzinfo=york))
        self.assertEqual((start.isoformat(), end.isoformat()),
                         ("2026-03-07T23:00:00-05:00", "2026-03-08T07:00:00-04:00"))
        utc = timezone.utc
        self.assertEqual(end.astimezone(utc) - start.astimezone(utc), timedelta(hours=7),
                         "the skipped hour: a shorter night")
        start, end = window_bounds(dtime(23), dtime(7), datetime(2026, 11, 1, 9, tzinfo=york))
        self.assertEqual(end.astimezone(utc) - start.astimezone(utc), timedelta(hours=9),
                         "the repeated hour: a longer night")
        print("ok  a daylight-saving night: 23:00 at the evening's offset, 07:00 at the morning's")

    def test_quiet_off_has_no_night(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            q = QuietHours(Path(tmp), clock=Clock(at(24 + 7.5)))
            self.assertEqual(q.last_window(), (at(23), at(24 + 7)))
            q.update({"enabled": False})
            self.assertIsNone(q.last_window())
        print("ok  quiet hours off: no night (QuietHours.last_window is None)")


def _row(row_id, kind, rank, asked, **kw):
    return inbox._row(kind, rank, row_id, kw.pop("title", f"{kind} {row_id}"), "blocks",
                      asked_at=asked.astimezone(timezone.utc).isoformat(timespec="seconds"), **kw)


class Report(unittest.TestCase):
    """`review.report` over a projection the test writes (`inbox.rows` patched)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.clock = Clock(at(24 + 7.5))
        self.quiet = QuietHours(Path(tmp.name), clock=self.clock)
        self.daemon = SimpleNamespace(quiet=self.quiet, inbox=SimpleNamespace(deferred=None),
                                      stores=SimpleNamespace(), push=None)

    def run_report(self, rows, *, phone=False):
        with patch.object(inbox, "rows", lambda daemon, **kw: list(rows)):
            return review.report(self.daemon, phone=phone)

    def test_what_the_night_holds(self):
        rows = [_row("q:t1:a", "question", 2, at(22 + 59 / 60), thread_id="t1"),
                _row("q:t1:b", "question", 2, at(23), thread_id="t1"),
                _row("task:k1", "task", 4, at(24 + 7), task_id="k1"),
                _row("dec:d1", "decision", 6, at(24 + 1), thread_id="t2"),
                _row("dec:d0", "decision", 6, at(22), thread_id="t2"),
                _row("pr:o/r#1", "pr", 5, at(24 + 1)),
                _row("chk:c1", "check", 7, at(24 + 1)),
                _row("chk:c2", "check", 8, at(24 + 1)),
                _row("missed:m1", "missed", 8, at(24 + 1), thread_id="t1"),
                _row("missed:m0", "missed", 8, at(20), thread_id="t1"),
                _row("appr:live1", "approval", 1, at(24 + 1), thread_id="t3")]
        out = self.run_report(rows)
        self.assertEqual([i["row"]["id"] for i in out["overnight"]], ["q:t1:b", "dec:d1"])
        self.assertEqual([i["row"]["id"] for i in out["before"]], ["q:t1:a", "task:k1"],
                         "22:59 is before; 07:00 is the end, not the night")
        self.assertEqual(out["fyi"], {"count": 1}, "only the night's rank 8, never a done check")
        self.assertNotIn("pr:o/r#1", json.dumps(out), "no PRs")
        self.assertNotIn("chk:c1", json.dumps(out), "no checks")
        self.assertEqual([x["row"]["id"] for x in out["live"]], ["appr:live1"])
        self.assertEqual(set(out["live"][0]), {"row"}, "Live now: no tag, nothing to tick")
        self.assertNotIn("appr:live1", item_ids(out))
        self.assertEqual(out["summary"], {"approvals": 0, "questions": 2, "decisions": 1, "blocked": 1,
                                          "proposals": 0, "live": 1})
        self.assertEqual((out["quiet"]["enabled"], out["open"], out["more"]), (True, 4, 0))
        self.assertTrue(all(i["approve"] is False and i["tag"] is None for i in out["overnight"] + out["before"]),
                        "only a deferred approval is ever ticked")
        self.quiet.update({"enabled": False})
        out = self.run_report(rows)
        self.assertEqual(out["overnight"], [])
        self.assertEqual(item_ids(out), ["q:t1:a", "q:t1:b", "task:k1"],
                         "quiet hours off: only what still waits, no decision")
        self.assertEqual((out["quiet"]["enabled"], out["fyi"]["count"]), (False, 0))
        print("ok  the night: 22:59 before, 23:00 overnight, 07:00 the end; a decision in; PRs and checks out; "
              "rank 8 counted; Live now untagged; quiet off")

    def test_at_most_60_items(self):
        rows = [_row(f"q:t1:{i}", "question", 2, at(24 + 1), thread_id="t1") for i in range(75)]
        out = self.run_report(rows)
        self.assertEqual((len(item_ids(out)), out["more"], out["open"]), (60, 15, 75))
        print("ok  at most 60 items, then 'and N more in the Inbox'")


# -- tags and the subject (MR-6) --------------------------------------------------------------


def record(**over):
    rec = {"id": "abcDEF123", "digest": "1" * 64,
           "card": {"tool": "Bash", "command": "make deploy", "args": {"command": "make deploy"}}}
    rec.update(over)
    return rec


class Binding(unittest.TestCase):
    def test_tags(self):
        base = review.item_tag(record())
        self.assertRegex(base, r"^[0-9a-f]{64}$")
        self.assertEqual(review.item_tag(record()), base, "the same deferral, the same tag")
        for name, other in {"id": record(id="abcDEF124"), "digest": record(digest="2" * 64),
                            "card": record(card={"tool": "Bash", "command": "make deploy --prod",
                                                 "args": {"command": "make deploy"}})}.items():
            self.assertNotEqual(review.item_tag(other), base, name)
        with patch.object(review, "_KEY", pysecrets.token_bytes(32)):
            self.assertNotEqual(review.item_tag(record()), base, "a restart: every tag stale")
        self.assertNotIn("1" * 16, base)
        print("ok  a tag changes with the id, the digest, the card and a restart")

    def test_subject(self):
        a = [("defer:a1", "a" * 64), ("defer:b2", "b" * 64)]
        s = review.subject(a)
        self.assertEqual(s, review.subject(list(reversed(a))), "order-free")
        self.assertTrue(re.fullmatch(r"defers:[0-9a-f]{56}", s) and len(s) <= 64, s)
        from jarvis.v2 import remote_auth
        self.assertTrue(remote_auth._DEFER_SUBJECT.fullmatch(s))
        self.assertNotEqual(review.subject([("defer:a1", "a" * 64), ("defer:c3", "b" * 64)]), s, "another id")
        self.assertNotEqual(review.subject([("defer:a1", "a" * 64), ("defer:b2", "c" * 64)]), s,
                            "another tag [mutation: a subject from the ids only]")
        self.assertNotEqual(review.subject(a[:1]), remote_answer.defer_binding("a1"),
                            "one item's set is never the row's subject")
        self.assertEqual(remote_answer.defer_binding("a1"), "defer:a1")
        print("ok  the subject: order-free, holds the tags, <= 64 characters, never a row's")

    def test_limits_and_shapes(self):
        tag = "a" * 64
        ok = review.parse_items({"items": [{"id": f"defer:x{i}", "tag": tag} for i in range(10)]}, approve=True)
        self.assertEqual(len(ok), 10)
        for name, body in {
                "11 items": {"items": [{"id": f"defer:x{i}", "tag": tag} for i in range(11)]},
                "a duplicate": {"items": [{"id": "defer:x", "tag": tag}, {"id": "defer:x", "tag": tag}]},
                "a malformed tag": {"items": [{"id": "defer:x", "tag": "A" * 64}]},
                "a short tag": {"items": [{"id": "defer:x", "tag": "a" * 63}]},
                "no tag": {"items": [{"id": "defer:x"}]},
                "an extra key": {"items": [{"id": "defer:x", "tag": tag, "x": 1}]},
                "an and-N-more row": {"items": [{"id": "defer:more.t1", "tag": tag}]},
                "an approval": {"items": [{"id": "appr:x", "tag": tag}]},
                "none": {"items": []}, "not a list": {"items": "defer:x"},
                "another key": {"items": [{"id": "defer:x", "tag": tag}], "all": True}}.items():
            with self.assertRaises(review.Bad, msg=name):
                review.parse_items(body, approve=True)
        self.assertEqual(len(review.parse_items({"items": [{"id": f"defer:x{i}"} for i in range(25)]},
                                                approve=False)), 25)
        review.parse_items({"items": [{"id": "defer:more.t1"}]}, approve=False)
        for body in ({"items": [{"id": f"defer:x{i}"} for i in range(26)]},
                     {"items": [{"id": "defer:x", "tag": tag}]}):
            with self.assertRaises(review.Bad):
                review.parse_items(body, approve=False)
        self.assertEqual((review.APPROVE_MAX, review.LEAVE_MAX, review.SHOWN), (10, 25, 60))
        print("ok  limits: at most 10 to approve (11, a duplicate or a malformed tag is 400), 25 to leave")


# -- the desk (MR-10) -----------------------------------------------------------------------------


class Desk(unittest.TestCase):
    """quiet_defer_check's `Live` daemon (its helpers, not its tests)."""
    call, chat, deferred = qd.Live.call, qd.Live.chat, qd.Live.deferred

    def setUp(self):
        qd.Live.setUp(self)
        self.store = self.daemon.inbox.deferred
        self.told: list = []
        real = self.daemon.deliver_queued

        def spy(thread_id, message, **kw):
            self.told.append((thread_id, message))
            return real(thread_id, message, **kw)
        self.daemon.deliver_queued = spy

    def report(self):
        return self.call("GET", "/inbox/review")

    def test_the_report_carries_tags_and_never_a_digest(self):
        thread = self.chat()
        one = self.deferred(thread_id=thread.id, args={"command": "make deploy", "token": SECRET},
                            command="make deploy")
        two = self.deferred(thread_id=thread.id, command="make test", args={"command": "make test"})
        out = self.report()
        self.assertEqual(sorted(item_ids(out)), sorted([f"defer:{one.req_id}", f"defer:{two.req_id}"]))
        for item in out["overnight"]:
            self.assertTrue(item["approve"] and item["leave"], item["why"])
            self.assertRegex(item["tag"], r"^[0-9a-f]{64}$")
            self.assertEqual(item["place"], f"chat:{thread.id}")
        self.assertEqual(out["places"][0]["project_id"], self.project.id)
        self.assertEqual(out["projects"], [{"id": self.project.id, "name": "Calc"}])
        text = json.dumps(out)
        for rec in self.store.all():
            self.assertNotIn(rec["digest"], text)
            self.assertNotIn(rec["digest"][:16], text)
        self.assertNotIn(SECRET, text, "the card's public projection only")
        items = picked(out, f"defer:{one.req_id}")
        result = self.call("POST", "/inbox/review/approve", {"items": items})
        self.assertNotIn(self.store.get(one.req_id)["digest"], json.dumps(result))
        print("ok  desk: each deferred item carries its tag; no digest and no unredacted argument in any payload")

    def test_success_one_write_one_message_per_chat_and_the_grants(self):
        a, b = self.chat(), self.chat()
        a1 = self.deferred(thread_id=a.id, command="make a1", args={"command": "make a1"})
        a2 = self.deferred(thread_id=a.id, command="make a2", args={"command": "make a2"})
        b1 = self.deferred(thread_id=b.id, command="make b1", args={"command": "make b1"})
        items = picked(self.report(), *(f"defer:{r.req_id}" for r in (a1, a2, b1)))
        writes = Writes(self.store)
        out = self.call("POST", "/inbox/review/approve", {"items": items})
        self.assertEqual(writes.count, 1, "one store transaction for the whole set")
        self.assertEqual((out["ok"], out["approved"]), (True, 3))
        self.assertEqual([r["id"] for r in out["results"]], [i["id"] for i in items])
        self.assertEqual(sorted(t for t, _ in self.told), sorted([a.id, b.id]),
                         "one message per chat [mutation: a message per record]")
        (text_a,) = [m.text for t, m in self.told if t == a.id]
        self.assertIn(a1.req_id, text_a)
        self.assertIn(a2.req_id, text_a)
        self.assertIn("Only the identical calls", text_a)
        for _t, message in self.told:
            self.assertEqual((message.origin, message.via, message.desk), ("owner", "inbox", False))
        until = quiet_defer._parse(self.store.get(a1.req_id)["grant_until"])
        self.assertEqual(until, self.clock.utc() + timedelta(minutes=30))
        self.assertTrue(self.daemon.owner_present(), "a desk answer stamps presence")
        # Each grant only for its exact call, once.
        self.mono.now += quiet_defer.PRESENT_S
        other = request("make a3", thread_id=a.id, args={"command": "make a3"})
        self.assertIs(self.daemon.approvals.ask(other), Decision.DENY)
        self.assertEqual(other.outcome[0], "deferred", "another call: not granted")
        for req in (a1, a2, b1):
            same = request(req.command, thread_id=req.thread_id, args={"command": req.command})
            self.assertIs(self.daemon.approvals.ask(same), Decision.ALLOW, req.command)
            again = request(req.command, thread_id=req.thread_id, args={"command": req.command})
            self.assertIs(self.daemon.approvals.ask(again), Decision.DENY)
            self.assertEqual(again.outcome[0], "deferred", "once")
        print("ok  desk Approve selected: one write, one message per chat (via inbox, no desk), "
              "each grant its exact call once for 30 minutes")

    def test_two_in_one_blocked_task_one_resume(self):
        task = self.stores.tasks.create(self.project.id, "migrate")
        self.stores.tasks.save(task)
        self.stores.tasks.transition(task.id, TaskState.CLARIFYING)
        self.stores.tasks.transition(task.id, TaskState.BLOCKED, reason="deferred overnight")
        one = self.deferred(thread_id="thtask01", task_id=task.id, command="m1", args={"command": "m1"})
        two = self.deferred(thread_id="thtask01", task_id=task.id, command="m2", args={"command": "m2"})
        out = self.report()
        self.assertEqual({i["place"] for i in out["overnight"]}, {f"task:{task.id}"})
        result = self.call("POST", "/inbox/review/approve",
                           {"items": picked(out, f"defer:{one.req_id}", f"defer:{two.req_id}")})
        self.assertEqual([r["delivery"] for r in result["results"]], ["resumed", "resumed"])
        self.assertEqual(self.runner.calls.count(("resume", task.id)), 1, "one resume per task")
        self.assertEqual(self.told, [], "a task is resumed, not told")
        print("ok  two deferrals of one blocked task: one resume")

    def nothing_happened(self, before: bytes, writes: Writes, ids):
        self.assertEqual(self.store.path.read_bytes(), before, "the file is unchanged")
        self.assertEqual(writes.count, 0, "no write")
        self.assertEqual(self.told, [], "nothing delivered")
        self.assertNotIn("resume", [c[0] for c in self.runner.calls])
        for rid in ids:
            self.assertEqual(self.store.get(rid)["state"], "waiting", rid)

    def test_all_or_nothing_at_the_desk(self):
        thread = self.chat()
        reqs = [self.deferred(thread_id=thread.id, command=f"c{i}", args={"command": f"c{i}"}) for i in range(3)]
        items = picked(self.report(), *(f"defer:{r.req_id}" for r in reqs))
        # One is left meanwhile (at the desk, in another window).
        self.assertTrue(quiet_defer.leave(self.daemon, reqs[1].req_id)["ok"])
        before, writes = self.store.path.read_bytes(), Writes(self.store)
        out = self.call("POST", "/inbox/review/approve", {"items": items}, 409)
        self.assertEqual(out["changed"], [{"id": f"defer:{reqs[1].req_id}", "reason": "answered"}])
        self.assertEqual(out["error"], quiet_defer.CHANGED)
        self.nothing_happened(before, writes, [reqs[0].req_id, reqs[2].req_id])
        # A restart between the read and the answer: every tag is stale.
        items = [i for i in items if i["id"] != f"defer:{reqs[1].req_id}"]
        with patch.object(review, "_KEY", pysecrets.token_bytes(32)):
            out = self.call("POST", "/inbox/review/approve", {"items": items}, 409)
        self.assertEqual({c["reason"] for c in out["changed"]}, {"changed"})
        self.nothing_happened(before, writes, [reqs[0].req_id, reqs[2].req_id])
        # A tag for another card (another deferral's tag) is a mismatch too.
        swapped = [{"id": items[0]["id"], "tag": items[1]["tag"]}, items[1]]
        out = self.call("POST", "/inbox/review/approve", {"items": swapped}, 409)
        self.assertEqual(out["changed"], [{"id": items[0]["id"], "reason": "changed"}])
        # Past 24 h.
        self.clock.now += timedelta(hours=24)
        out = self.call("POST", "/inbox/review/approve", {"items": items}, 409)
        self.assertEqual({c["reason"] for c in out["changed"]}, {"expired"})
        self.nothing_happened(before, writes, [reqs[0].req_id, reqs[2].req_id])
        print("ok  desk all or nothing: one left, a restart's stale tags, another's tag, past 24 h: 409, "
              "zero grants, no write, nothing delivered")

    def test_a_closed_chat_or_a_running_task_is_not_approvable(self):
        from jarvis.v2.projects import archive_thread
        thread = self.chat()
        mine = self.deferred(thread_id=thread.id)
        task = self.stores.tasks.create(self.project.id, "migrate")
        self.stores.tasks.save(task)
        self.stores.tasks.transition(task.id, TaskState.CLARIFYING)
        self.stores.tasks.transition(task.id, TaskState.BLOCKED, reason="deferred overnight")
        tasked = self.deferred(thread_id="thtask01", task_id=task.id, command="m1", args={"command": "m1"})
        items = picked(self.report(), f"defer:{mine.req_id}", f"defer:{tasked.req_id}")
        archive_thread(self.daemon, thread.id)
        self.stores.tasks.transition(task.id, TaskState.CLARIFYING, reason="resumed meanwhile")
        out = self.report()
        found = {i["row"]["id"]: i for i in out["overnight"] + out["before"]}
        self.assertEqual((found[f"defer:{mine.req_id}"]["approve"], found[f"defer:{mine.req_id}"]["why"]),
                         (False, "chat-closed"), "[mutation: an approvable item whose chat is closed]")
        self.assertIsNone(found[f"defer:{mine.req_id}"]["tag"])
        self.assertEqual(found[f"defer:{tasked.req_id}"]["why"], "task-running")
        self.assertTrue(found[f"defer:{mine.req_id}"]["leave"], "it can still be left")
        before, writes = self.store.path.read_bytes(), Writes(self.store)
        out = self.call("POST", "/inbox/review/approve", {"items": items}, 409)
        self.assertEqual(sorted(c["reason"] for c in out["changed"]), ["chat-closed", "task-running"])
        self.nothing_happened(before, writes, [mine.req_id, tasked.req_id])
        print("ok  a closed chat's or a no-longer-blocked task's deferral is never approved (report and route)")

    def test_limits_and_leave_at_the_desk(self):
        tag = "a" * 64
        self.call("POST", "/inbox/review/approve",
                  {"items": [{"id": f"defer:x{i}", "tag": tag} for i in range(11)]}, 400)
        self.call("POST", "/inbox/review/approve",
                  {"items": [{"id": "defer:x", "tag": tag}, {"id": "defer:x", "tag": tag}]}, 400)
        self.call("POST", "/inbox/review/approve", {"items": [{"id": "defer:x", "tag": "nope"}]}, 400)
        self.call("POST", "/inbox/review/leave", {"items": [{"id": f"defer:x{i}"} for i in range(26)]}, 400)
        thread = self.chat()
        one = self.deferred(thread_id=thread.id)
        out = self.call("POST", "/inbox/review/leave", {"items": [{"id": f"defer:{one.req_id}"}, {"id": "defer:gone"}]})
        self.assertEqual([(r["ok"], r["id"]) for r in out["results"]],
                         [(True, f"defer:{one.req_id}"), (False, "defer:gone")])
        self.assertEqual(self.told, [], "Leave sends nothing")
        print("ok  desk limits (11, a duplicate, a bad tag, 26 to leave: 400); Leave selected: per item, nothing sent")

    def test_owner_only_and_the_route_table(self):
        for method, path, body in (("GET", "/inbox/review", None),
                                   ("POST", "/inbox/review/approve", {"items": []}),
                                   ("POST", "/inbox/review/leave", {"items": []})):
            self.call(method, path, body, 403, owner=False)
        # The HUD listener serves none of the phone's paths.
        self.call("GET", "/m/api/review", None, 404)
        self.call("POST", "/m/api/review/approve", {"items": []}, 404)
        print("ok  /inbox/review*: 403 from the API listener; the HUD serves no /m/api path")


# -- the phone (MR-11) -----------------------------------------------------------------------


class Phone(ra.Phone):
    """quiet_defer_check's `PhoneRetry` daemon (its set-up, not its tests)."""

    def setUp(self):
        ra.Phone.setUp(self)
        # As quiet_defer_check's PhoneRetry: its night, its presence clock, its desk.
        self.clock = Clock(at(23.5))
        self.d.quiet.clock = self.clock
        self.d.inbox.deferred.clock = self.clock.utc
        self.mono2 = qd.Mono()
        self.d._presence_clock = self.mono2
        self.d.attach_quiet_desk()
        self.d.runner = qd.ResumingRunner(self.d)
        self.store = self.d.inbox.deferred
        self.thread = self.d.open_thread(self.project.id, Role.CHAT, PN.FAST, {})
        self.told: list = []
        real = self.d.deliver_queued
        self.d.deliver_queued = lambda tid, msg, **kw: self.told.append((tid, msg)) or real(tid, msg, **kw)

    def defer(self, command, thread_id=None, **kw):
        self.mono2.now += quiet_defer.PRESENT_S
        req = request(command, thread_id=thread_id or self.thread.id, args={"command": command}, **kw)
        self.assertIs(self.d.approvals.ask(req), Decision.DENY)
        self.assertEqual(req.outcome[0], "deferred")
        return req

    def review_report(self, cookie=None):
        status, _, body = self.phone("GET", "/m/api/review", cookie=cookie or self.cookie)
        self.assertEqual(status, 200, body)
        return body

    def group_options(self, items, cookie=None):
        return self.phone("POST", "/m/api/review/approve/options", {"items": items}, cookie=cookie or self.cookie)

    def group_assertion(self, items):
        status, _, body = self.group_options(items)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["count"], len(items))
        return self.a.get(body["publicKey"])

    def approve(self, items, credential, cookie=None):
        return self.phone("POST", "/m/api/review/approve", {"items": items, "assertion": credential},
                          cookie=cookie or self.cookie)

    def session(self):
        return self.auth.session_for([self.cookie])

    def test_group_approve_with_one_assertion(self):
        self.secure_ok()
        r1, r2 = self.defer("make one"), self.defer("make two")
        out = self.review_report()
        self.assertTrue(out["group"]["ok"], out["group"])
        self.assertFalse(out["opens_here"], "no morning push in this process")
        items = picked(out, f"defer:{r1.req_id}", f"defer:{r2.req_id}")
        status, _, body = self.approve(items, None)
        self.assertEqual(status, 401, "no assertion")
        credential = self.group_assertion(items)
        self.mono2.now += quiet_defer.PRESENT_S
        status, _, body = self.approve(list(reversed(items)), credential)
        self.assertEqual(status, 200, body)
        self.assertEqual({self.store.get(r.req_id)["state"] for r in (r1, r2)}, {"granted"})
        (tid, message), = self.told
        self.assertEqual((tid, message.via, message.desk), (self.thread.id, "phone", False))
        self.assertTrue(self.d.owner_present(), "a verified group approval stamps presence")
        print("ok  phone Approve selected: one assertion for the set (any order), one message, presence")

    def test_set_a_assertion_is_refused_for_set_b(self):
        r1, r2, r3 = self.defer("a"), self.defer("b"), self.defer("c")
        out = self.review_report()
        set_a = picked(out, f"defer:{r1.req_id}", f"defer:{r2.req_id}")
        set_b = picked(out, f"defer:{r1.req_id}", f"defer:{r3.req_id}")
        credential = self.group_assertion(set_a)
        status, _, body = self.approve(set_b, credential)
        self.assertEqual(status, 401, body)
        # One tag different is another subject too.
        credential = self.group_assertion(set_a)
        forged = [set_a[0], {"id": set_a[1]["id"], "tag": "f" * 64}]
        status, _, body = self.approve(forged, credential)
        self.assertEqual(status, 401, body)
        self.assertEqual({self.store.get(r.req_id)["state"] for r in (r1, r2, r3)}, {"waiting"})
        print("ok  set A's assertion is refused for set B, and for A with one tag changed")

    def test_a_row_assertion_and_a_group_assertion_never_cross(self):
        r1 = self.defer("only")
        out = self.review_report()
        items = picked(out, f"defer:{r1.req_id}")
        # A per-row Approve and retry's assertion (`defer:<id>`), sent to the group route.
        row_credential, _ = self.assertion(f"defer:{r1.req_id}", "allow")
        status, _, body = self.approve(items, row_credential)
        self.assertEqual(status, 401, body)
        # A group's assertion for that one item, sent to the row route.
        credential = self.group_assertion(items)
        status, _, body = self.post_answer(f"defer:{r1.req_id}", {"action": "retry", "assertion": credential})
        self.assertEqual(status, 401, body)
        self.assertEqual(self.store.get(r1.req_id)["state"], "waiting")
        print("ok  a defer:<id> assertion is refused at the group route, and a group's at the row route")

    def nothing_happened(self, before, writes, ids):
        self.assertEqual(self.store.path.read_bytes(), before, "the file is unchanged")
        self.assertEqual(writes.count, 0, "no write")
        self.assertEqual(self.told, [], "nothing delivered")
        self.assertNotIn("resume", [c[0] for c in self.d.runner.calls])
        for rid in ids:
            self.assertEqual(self.store.get(rid)["state"], "waiting", rid)

    def test_one_of_three_left_between_options_and_answer(self):
        reqs = [self.defer(f"c{i}") for i in range(3)]
        items = picked(self.review_report(), *(f"defer:{r.req_id}" for r in reqs))
        credential = self.group_assertion(items)
        self.assertTrue(quiet_defer.leave(self.d, reqs[1].req_id)["ok"], "left at the HUD meanwhile")
        before, writes = self.store.path.read_bytes(), Writes(self.store)
        status, _, body = self.approve(items, credential)
        self.assertEqual(status, 409, body)
        self.assertEqual(body["changed"], [{"id": f"defer:{reqs[1].req_id}", "reason": "answered"}])
        self.nothing_happened(before, writes, [reqs[0].req_id, reqs[2].req_id])
        print("ok  phone all or nothing: one of three left between options and answer: 409 changed, zero grants, "
              "the file unchanged, no write, nothing delivered [mutation: per-id settles]")

    def test_the_second_check_after_the_assertion(self):
        task = self.d.stores.tasks.create(self.project.id, "migrate")
        self.d.stores.tasks.save(task)
        self.d.stores.tasks.transition(task.id, TaskState.CLARIFYING)
        self.d.stores.tasks.transition(task.id, TaskState.BLOCKED, reason="deferred overnight")
        tasked = self.defer("t1", thread_id="thtask01", task_id=task.id)
        other = self.defer("plain")
        ids = [tasked.req_id, other.req_id]
        cases = {
            "task-running": lambda: self.d.stores.tasks.transition(task.id, TaskState.CLARIFYING, reason="resumed"),
            "changed": lambda: setattr(review, "_KEY", pysecrets.token_bytes(32)),
            "expired": lambda: setattr(self.clock, "now", self.clock.now + timedelta(hours=24)),
        }
        key = review._KEY
        for reason, change in cases.items():
            with self.subTest(reason=reason):
                review._KEY = key
                if reason != "task-running" and self.d.stores.tasks.get(task.id).state != TaskState.BLOCKED:
                    self.d.stores.tasks.transition(task.id, TaskState.BLOCKED, reason="blocked again")
                items = picked(self.review_report(), *(f"defer:{i}" for i in ids))
                credential = self.group_assertion(items)
                change()
                before, writes = self.store.path.read_bytes(), Writes(self.store)
                status, _, body = self.approve(items, credential)
                self.assertEqual(status, 409, body)
                self.assertIn(reason, {c["reason"] for c in body["changed"]}, body)
                self.nothing_happened(before, writes, ids)
                if reason == "expired":
                    self.clock.now -= timedelta(hours=24)
        review._KEY = key
        print("ok  after the assertion: a task resumed, a restart, past 24 h: 409, nothing written "
              "[mutation: no second check]")

    def test_a_closed_chat_after_the_options(self):
        from jarvis.v2.projects import archive_thread
        r1 = self.defer("one")
        items = picked(self.review_report(), f"defer:{r1.req_id}")
        credential = self.group_assertion(items)
        archive_thread(self.d, self.thread.id)
        before, writes = self.store.path.read_bytes(), Writes(self.store)
        status, _, body = self.approve(items, credential)
        self.assertEqual((status, body["changed"]), (409, [{"id": f"defer:{r1.req_id}", "reason": "chat-closed"}]))
        self.nothing_happened(before, writes, [r1.req_id])
        print("ok  a chat closed after the options: 409 chat-closed, nothing written")

    def test_a_withheld_card_is_never_in_a_group(self):
        r1 = self.defer("ls", reason="password: " + ra.PASSWORD)
        r2 = self.defer("make two")
        out = self.review_report()
        found = {i["row"]["id"]: i for i in out["overnight"]}
        self.assertEqual((found[f"defer:{r1.req_id}"]["approve"], found[f"defer:{r1.req_id}"]["why"]),
                         (False, "withheld"))
        self.assertIsNone(found[f"defer:{r1.req_id}"]["tag"])
        self.assertNotIn(ra.PASSWORD, json.dumps(out))
        tag = review.item_tag(self.store.get(r1.req_id))
        items = [{"id": f"defer:{r1.req_id}", "tag": tag}] + picked(out, f"defer:{r2.req_id}")
        status, _, body = self.group_options(items)
        self.assertEqual((status, body.get("changed")), (409, [{"id": f"defer:{r1.req_id}", "reason": "withheld"}]))
        # A challenge for that set made past the first check: the answer refuses it too.
        subject = review.subject([(i["id"], i["tag"]) for i in items])
        credential = self.a.get(self.auth.answer_options(self.session(), subject, "allow"))
        before, writes = self.store.path.read_bytes(), Writes(self.store)
        status, _, body = self.approve(items, credential)
        self.assertEqual(status, 409, body)
        self.assertEqual(body["changed"], [{"id": f"defer:{r1.req_id}", "reason": "withheld"}])
        self.nothing_happened(before, writes, [r1.req_id, r2.req_id])
        # At the desk it shows whole, so it may be approved there.
        desk = review.report(self.d, phone=False)
        self.assertTrue({i["row"]["id"]: i for i in desk["overnight"]}[f"defer:{r1.req_id}"]["approve"])
        print("ok  a card the phone's scrub would change: never ticked on the phone (report, options, answer)")

    def test_limits_on_the_phone(self):
        tag = "a" * 64
        status, _, body = self.group_options([{"id": f"defer:x{i}", "tag": tag} for i in range(11)])
        self.assertEqual(status, 400, body)
        status, _, body = self.group_options([{"id": "defer:x", "tag": tag}] * 2)
        self.assertEqual(status, 400, body)
        status, _, body = self.group_options([{"id": "defer:x", "tag": "z"}])
        self.assertEqual(status, 400, body)
        status, _, body = self.approve([{"id": f"defer:x{i}", "tag": tag} for i in range(11)], {"x": 1})
        self.assertEqual(status, 400, body)
        print("ok  phone limits: 11 items, a duplicate, a malformed tag: 400 [mutation: the 10-item cap removed]")

    def limited(self, name):
        r1, r2 = self.defer("one"), self.defer("two")
        items = picked(self.review_report(), f"defer:{r1.req_id}", f"defer:{r2.req_id}")
        early = self.group_assertion(items)
        ra.FLIPS[name](self)
        result = self.d.secure.check()
        self.assertEqual((result["ok"], result["failed"]), (False, name), result)
        status, _, body = self.group_options(items)
        self.assertEqual(status, 409, body)
        self.assertTrue(body["error"].startswith("limited: "), body)
        status, _, body = self.approve(items, early)
        self.assertEqual(status, 409, body)
        self.assertTrue(body["error"].startswith("limited: "), body)
        self.assertEqual({self.store.get(r.req_id)["state"] for r in (r1, r2)}, {"waiting"})
        self.assertEqual(self.told, [])
        self.assertFalse(self.review_report()["group"]["ok"], "the report says so")
        # The per-row Approve and retry still works on a limited phone.
        credential, _ = self.assertion(f"defer:{r1.req_id}", "allow")
        status, _, body = self.post_answer(f"defer:{r1.req_id}", {"action": "retry", "assertion": credential})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.store.get(r1.req_id)["state"], "granted")

    def test_limited_loopback(self):
        self.limited("loopback")
        print("ok  §4.1 (1) off loopback: no group approval; a per-row retry still works "
              "[mutation: a limited phone allowed a group]")

    def test_limited_serve(self):
        self.limited("serve")
        print("ok  §4.1 (2) Funnel on: no group approval; a per-row retry still works")

    def test_limited_certificate(self):
        self.limited("certificate")
        print("ok  §4.1 (3) the certificate: no group approval; a per-row retry still works")

    def test_limited_passkey(self):
        self.limited("passkey")
        print("ok  §4.1 (4) the passkey's verification: no group approval; a per-row retry still works")

    def test_limited_listeners(self):
        self.limited("listeners")
        print("ok  §4.1 (5) another listener on the port: no group approval; a per-row retry still works")

    def test_leave_needs_the_session_only(self):
        r1 = self.defer("one")
        status, _, body = self.phone("POST", "/m/api/review/leave", {"items": [{"id": f"defer:{r1.req_id}"}]},
                                     cookie="__Host-jarvis_m=nope")
        self.assertEqual(status, 401, body)
        status, _, body = self.phone("POST", "/m/api/review/leave", {"items": [{"id": f"defer:{r1.req_id}"}]},
                                     cookie=self.cookie)
        self.assertEqual((status, body["results"][0]["ok"]), (200, True), body)
        self.assertEqual(self.store.get(r1.req_id)["state"], "left")
        self.assertFalse(self.d.owner_present(), "a Leave from the phone is not presence")
        print("ok  phone Leave selected: the session only, no assertion; not presence")

    def test_owner_only_on_the_remote_listener(self):
        self.defer("one")
        self.assertEqual(self.phone("GET", "/m/api/review")[0], 401, "no session")
        self.assertEqual(self.phone("POST", "/m/api/review/leave", {"items": []}, cookie=self.cookie,
                                    origin="https://evil.ts.net")[0], 403)
        self.assertEqual(self.phone("GET", "/m/api/review", cookie=self.cookie, host="other.ts.net")[0], 400)
        self.assertEqual(self.phone("POST", "/m/api/review", {}, cookie=self.cookie)[0], 405)
        self.assertEqual(self.phone("GET", "/m/api/review/approve", cookie=self.cookie)[0], 405)
        # The remote listener serves none of the HUD's review paths.
        for method, path in (("GET", "/inbox/review"), ("POST", "/inbox/review/approve")):
            status = self.phone(method, path, {} if method == "POST" else None, cookie=self.cookie)[0]
            self.assertIn(status, (403, 404), path)
        # The API listener serves neither.
        api = ("127.0.0.1", self.d.port)
        for method, path in (("GET", "/m/api/review"), ("GET", "/inbox/review"),
                             ("POST", "/inbox/review/approve")):
            status = ra.raw(api[1], method, path, [("Host", f"127.0.0.1:{api[1]}")],
                            b"{}" if method == "POST" else None)[0]
            self.assertIn(status, (403, 404), path)
        print("ok  /m/api/review*: 401 without a session, 403 a wrong Origin, 400 a wrong Host; neither "
              "listener serves the other's paths, the API listener none")


# -- the morning push (MR-12) -----------------------------------------------------------------


class MorningPush(pc.SenderBase):
    def test_payload_unchanged_and_opens_here(self):
        # Something still waits (a blocked task): the review has an open item.
        project = self.d.stores.projects.create("Calc", str(self.root))
        task = self.d.stores.tasks.create(project.id, "migrate")
        self.d.stores.tasks.save(task)
        self.d.stores.tasks.transition(task.id, TaskState.CLARIFYING)
        self.d.stores.tasks.transition(task.id, TaskState.BLOCKED, reason="waiting")
        self.local.now = pc.local(23.5)
        self.wait("task:a", rank=4)
        self.assertEqual(self.net.sent, [])
        self.assertFalse(self.push.review_opens())
        self.local.now = pc.local(24 + 7)
        self.mono2.now += 100
        self.wait("task:a", rank=4)
        self.assertEqual(len(self.net.sent), 1, "the morning push")
        self.assertEqual(self.receiver.decrypt(self.net.sent[0]["body"]), push_mod.payload(1, 4),
                         "the payload, byte for byte: {count, rank} only")
        self.assertTrue(self.push.review_opens())
        status, _, body = self.phone("GET", "/m/api/review", cookie=self.cookie)
        self.assertEqual((status, body["opens_here"], body["open"]), (200, True, 1), body)
        # The next push (a new row, past the coalescing window) clears it.
        self.mono2.now += 120
        self.wait("task:a", "q:t:1", rank=2)
        self.assertEqual(len(self.net.sent), 2)
        self.assertFalse(self.push.review_opens())
        self.assertFalse(self.phone("GET", "/m/api/review", cookie=self.cookie)[2]["opens_here"])
        # A morning push whose review has nothing open does not open on it.
        self.push._morning = True
        self.d.stores.tasks.transition(task.id, TaskState.CLARIFYING, reason="resumed")
        self.assertFalse(self.phone("GET", "/m/api/review", cookie=self.cookie)[2]["opens_here"])
        # A restart: a new sender remembers nothing.
        fresh = push_mod.Push(self.d, Path(config.V2_DATA_DIR), self.d.remote_host)
        self.assertFalse(fresh.review_opens())
        print("ok  push: the payload byte for byte; opens_here after the morning push, not after the next push, "
              "not with nothing open, not after a restart")


# -- agents cannot reach it -------------------------------------------------------------------


class Boundary(unittest.TestCase):
    REVIEW_SOURCE = (r"import review\b", r"from \.+review\b", r"from \.+ import [^\n]*\breview\b",
                     r"\breview\.(?:report|approve|leave|route|hud_route|subject|item_tag|check_group|_KEY)\b",
                     r"/inbox/review", r"/m/api/review", r"approve_many", r"settle_many", r"review_opens")

    def test_no_tool_mcp_or_session_path_names_it(self):
        from jarvis import tools
        from jarvis.v2 import bridge, chat_relay, decisions, mcp, mcp_catalog, session_tools
        from jarvis.v2.providers import fastpath
        import jarvis.v2.tools as v2tools
        import jarvis.__main__ as cli
        from jarvis.v2 import mcp_session, owner_ops, checks
        from jarvis.v2.discord import gateway, linker, project_commands
        reachable = (set(fastpath.FAST_TOOLS) | set(mcp.MCP_TOOLS) | set(mcp_catalog.MCP_TOOLS)
                     | set(session_tools.SESSION_TOOLS) | set(tools.REGISTRY))
        sources = [v2tools, mcp, mcp_catalog, session_tools, bridge, chat_relay, fastpath, cli, owner_ops,
                   mcp_session, decisions, checks, gateway, linker, project_commands]
        for name in reachable:
            self.assertNotRegex(name, r"(?i)review_|morning")
            func = getattr(tools.REGISTRY.get(name), "func", None)
            if func is not None:
                sources.append(inspect.getmodule(func))
        for module in {m for m in sources if m is not None}:
            text = inspect.getsource(module)
            for needle in self.REVIEW_SOURCE:
                self.assertIsNone(re.search(needle, text), f"{module.__name__} names {needle}")
        print("ok  no tool, MCP entry, session tool, Discord or CLI module names the review or its paths")

    def test_shell_probes(self):
        from jarvis import permissions as v1, rules as v1_rules
        from jarvis.tools import files
        from jarvis.v2 import permissions as v2
        for path in ("/m/api/review", "/m/api/review/approve", "/m/api/review/approve/options",
                     "/m/api/review/leave"):
            for line in (f"curl -X POST http://127.0.0.1:{config.REMOTE_PORT}{path}",
                         f"curl http://localhost:8408{path} -d '{{}}'",
                         f"curl https://box.tail1234.ts.net{path}"):
                self.assertTrue(v2.denied_command(line), line)
                self.assertEqual(rules.decide(line).decision, rules.DENY, line)
                self.assertNotEqual(v1.static_verdict(line).decision, v1_rules.ALLOW, line)
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp, \
                patch.object(config, "V2_DATA_DIR", Path(tmp)):
            target = Path(tmp) / "inbox" / "deferred.json"
            for command in (f"cp /tmp/x {target}", f"echo '{{}}' > {target}"):
                self.assertTrue(v2.denied_command(command), command)
                self.assertNotEqual(v1.static_verdict(command).decision, v1_rules.ALLOW, command)
        self.assertIn("jarvis/v2/review.py", files.SELF_PROTECTED)
        self.assertTrue(files._self_protected(ROOT / "jarvis" / "v2" / "review.py"))
        print("ok  shell probes of /m/api/review* DENY (v2 layer 1, v1); the grants' store is protected; "
              "review.py is SELF_PROTECTED")

    def test_only_the_verified_phone_approves_as_the_phone(self):
        source = inspect.getsource(review.route)
        verify = source.index("auth.answer_verify(session, assertion, subject(items)")
        secure = source.index("_monitor_check(daemon, session, handler)", verify)
        approve = source.index('approve(daemon, items, via="phone", verified=True)')
        self.assertLess(verify, secure)
        self.assertLess(secure, approve)
        self.assertIn('approve(daemon, items, via="inbox")', inspect.getsource(review.hud_route))
        print("ok  approve(verified=True) only after the set's assertion and §4.1")


if __name__ == "__main__":
    unittest.main(verbosity=1)
