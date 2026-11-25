"""Orchestrator-owned acceptance tests for C3 (implementers: do not edit; if one is wrong, stop and say why).

Contract: three more read-only ingesters on C2's framework (ingest.Doc / ingest.Runner), each also writing a snapshot for I2.
  jarvis/config.py      V3_SIGNALS_DIR = V3_DIR / "signals"            (map 1.1 SIGNALS_DIR)
  jarvis/v3/signals.py  (imports only the stdlib, jarvis.untrusted, jarvis.v2.redact and jarvis.v3._paths)
    write(source, items, *, directory=None, **extra) -> Path     directory default config.V3_SIGNALS_DIR (read at call time)
      ValueError unless source matches [a-z][a-z0-9-]{0,31}, every item is a dict with a unique str "id", and no extra key is
      version/source/items. <directory>/<source>.json = {"version": 1, "source": source, **extra, "items": items sorted by id},
      every string (keys too) through untrusted.strip_invisible then redact.for_model, serialised as json.dumps(payload,
      sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n" in UTF-8. No clock value is added, so
      identical input gives identical bytes. Directory made 0700 without following links (a linked component: OSError, nothing
      written); file 0600, written to a temp file in that directory, fsynced, renamed over the target (a link there is replaced,
      never followed); no temp file is left behind.
    read(source, *, directory=None) -> dict | None   None when missing, a link, not a regular 0600 file of this user, > 8 MiB,
      not JSON, or not version 1 of that source.
  jarvis/v3/ingest/proc.py
    run_bounded(argv, *, timeout_s, max_bytes) -> (returncode, stdout_text)   no shell; stdin and stderr are /dev/null (stderr is
      never read); stdout decoded UTF-8 with replacement. ProcError(Exception) when the program is missing, outlives timeout_s or
      writes more than max_bytes: the process group is killed at once; the message is a short fixed class, never output.
  All three ingesters: every doc is untrusted (meta "untrusted": 1); source text is strip_invisible'd and redacted before a doc
  or snapshot holds it; an error is an exception whose text is a short class (never source text or a credential). The snapshot
  is written after the poll has computed its docs and before it returns; a snapshot that cannot be written fails the poll.
  jarvis/v3/ingest/calendar.py   CalendarIngester(api, *, signals_dir=None, calendar="primary", backfill_days=30, clock=time.time)
    name "calendar". api.list_events(calendar, *, sync_token=None, page_token=None, time_min=None) -> {"items", "nextPageToken"?,
    "nextSyncToken"?} (events.list without singleEvents: recurring masters plus their exceptions); raises SyncTokenExpired (defined
    here; HTTP 410). Full sync: time_min = UTC "%Y-%m-%dT%H:%M:%SZ" of clock() - backfill_days days, the same on every page; pages
    may span polls; ends with nextSyncToken. Incremental: sync_token (never time_min), then page tokens. SyncTokenExpired, or a
    missing or unreadable calendar snapshot, starts a full sync. While one runs, the snapshot keeps every event known before it
    began (updated by the pages); when it completes, each of those it did not return becomes Doc(deleted=True) and leaves the
    snapshot. status "cancelled" -> Doc(deleted=True). An event whose times cannot be read is skipped.
    Doc: ref = event id, title = summary, one message holding the times, location and description. meta = {"untrusted": 1,
    "calendar", "status", "start", "end", "all_day": 0|1, "attendees": attendees that are neither self nor resource (at least 1 when
    attendeesOmitted), "owner_event": organizer.self as 0|1, "jarvis": extendedProperties.private.jarvis == "1" as 0|1,
    "recurring": recurrence or recurringEventId present as 0|1}. Timed start/end: UTC "%Y-%m-%dT%H:%M:%SZ"; all-day: the dates as
    given (end exclusive). Snapshot "calendar": items {"id", "title", "start", "end", "all_day", "attendees", "owner_event",
    "jarvis", "status", "recurring"} for each live event known whose end is not before clock() - backfill_days days.
    HttpCalendar(token) is the real api: httpx GETs only, imported lazily, 410 -> SyncTokenExpired, 429 or a rate 403 -> RateLimited.
  jarvis/v3/ingest/schoolwork.py  SchoolworkIngester(run, *, post, signals_dir=None, clock=time.time)    name "schoolwork"
    SW_ARGV = (("sw", "prep", "--json"), ("sw", "review", "--json")), the only commands: read-only by construction. No other
    subcommand (sync, login, add, triage, ...) appears in the module, and nothing can submit work (standing decision).
    run(argv) -> (returncode, stdout). Success is a JSON object on stdout with status 0 (review may also exit 1: the CLI does when
    a health check is an error). Items: prep days[].due[] and overdue[], review undated[] and recentChanges[], merged by id, prep's
    copy winning. Doc: ref = str(id), title = title, one message holding the title, course, due, state, points, type, lock note and
    description; meta = {"untrusted": 1, "course", "due": dueAt as UTC "%Y-%m-%dT%H:%M:%SZ" or
    None, "state" (grades are the state, e.g. "graded": the CLI exposes no score), "done": 0|1, "locked": 0|1, "points", "type",
    "system": the item's source}; a missing field is None (0 for done/locked). A doc is emitted only when its item is new or
    changed; nothing is deleted (items leave the CLI's window, not the school). Snapshot "schoolwork": extra needs_sign_in (bool:
    a review health finding with code "needs_login"); items {"id", "title", "course", "due", "state", "done", "locked"}.
    When needs_sign_in turns true: post({"kind": "news", "rank": 3, "ask": fixed text naming `sw login-msa`, ...}) once, holding no
    CLI text; no further post while it stays true (also across processes); after it clears, a later expiry posts again.
  jarvis/v3/ingest/github.py   GitHubIngester(run, *, signals_dir=None)    name "github"
    GH_ARGV: tuple of argv tuples ("gh", "api", "graphql", "-f", "query=<query>"), the only commands; no mutation. Aliases:
    authored: search(query: "is:pr is:open archived:false author:@me", type: ISSUE, first: <= 100) and review: the same with
    review-requested:@me. Output {"data": {"authored": {"nodes": [...]}, "review": {"nodes": [...]}}}; a PR node has number,
    title, url, isDraft, state, updatedAt, repository{nameWithOwner}, author{login}, reviewDecision,
    commits(last: 1){nodes{commit{oid statusCheckRollup{state}}}}; a node without them is skipped. "errors" on stdout: one of type
    RATE_LIMITED -> RateLimited, otherwise an error, as is a non-zero status or non-JSON. Doc: ref = "<owner>/<repo>#<number>",
    title = PR title; meta = {"untrusted": 1, "repo", "number", "state": "open", "draft": 0|1, "checks": success|failure|pending|
    error|expected|none, "authored": 0|1, "review_requested": 0|1, "review_decision": lower-cased or None, "url", "updated",
    "author"}. Emitted only when new or changed; a PR no longer listed -> Doc(deleted=True). Snapshot "github": items {"id",
    "repo", "number", "title", "draft", "checks", "authored", "review_requested", "review_decision"}.
    rules.decide never says DENY to a GH_ARGV line. (The map asked for ALLOW: the shared gate asks for every gh command and this
    PR does not touch rules.py. The daemon runs these itself, never through an agent's shell.)
  jarvis/v3/jobs.py   default_jobs(*, feed_root=None, index_path=None, google_token=None, signals_dir=None, find_cli=None)
    adds "calendar" (every 300 s; only with a saved Google grant valid as J2 checks it and listing calendar.events or
    calendar.readonly; HttpCalendar(google_auth.access_token)), "schoolwork" (every 900 s; only when find_cli("sw") returns a
    path) and "github" (every 600 s; find_cli("gh")). find_cli is called when the job runs; its default looks only for an
    executable regular file Path.home() / ".local/bin" / name (no PATH search: a test's temporary HOME never reaches the real
    CLIs, and gh never runs on an inherited GH_TOKEN). Without the grant or CLI nothing is built. Each ingester is built once with signals_dir=signals_dir, shares the jobs' one Index and runs
    through an ingest.Runner; schoolwork's post is Feed(feed_root).post. A subprocess run calls proc.run_bounded (looked up on the
    module at call time) with the found path as argv[0], 0 < timeout_s <= 120, 0 < max_bytes <= 16 MiB. Building the list imports
    none of these modules.
"""
import ast
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from itertools import takewhile
from pathlib import Path
from unittest.mock import patch

from jarvis import config, rules
from jarvis.v3 import jobs, signals
from jarvis.v3.feed import Feed
from jarvis.v3.index import Index
from jarvis.v3.ingest import RateLimited, Runner, proc
from jarvis.v3.ingest import calendar as calendar_ingest
from jarvis.v3.ingest import github, schoolwork
from jarvis.v3.ingest.calendar import CalendarIngester, SyncTokenExpired
from jarvis.v3.ingest.github import GitHubIngester
from jarvis.v3.ingest.schoolwork import SchoolworkIngester

REPO = Path(__file__).resolve().parents[2]
NOW = 1_800_000_000.0
PLANTED = "ghp" + "_" + "C3x9" * 9
ME = "owner@example.com"


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cal_event(eid, title, day="2027-01-20", **kw):
    event = {"id": eid, "status": "confirmed", "summary": title, "updated": "2027-01-10T00:00:00.000Z", "organizer": {"email": ME, "self": True},
             "start": {"dateTime": f"{day}T09:00:00-06:00", "timeZone": "America/Chicago"}, "end": {"dateTime": f"{day}T10:00:00-06:00", "timeZone": "America/Chicago"}}
    event.update(kw)
    return event


class FakeCalendar:
    """Page tokens say which listing they continue, so a client may or may not resend sync_token with them."""

    def __init__(self, events=(), page=2):
        self.box, self.changed, self.version, self.page, self.expired_before, self.calls, self.fail = {}, {}, 0, page, 0, [], None
        for event in events:
            self.put(event)

    def put(self, event):
        self.version += 1
        self.box[event["id"]], self.changed[event["id"]] = event, self.version

    def cancel(self, eid):
        self.put({"id": eid, "status": "cancelled"})

    def vanish(self, eid):                       # gone with no tombstone: only a full sync can tell
        del self.box[eid], self.changed[eid]

    def expire(self):
        self.expired_before = self.version + 1
        self.version += 1

    def list_events(self, calendar, *, sync_token=None, page_token=None, time_min=None):
        self.calls.append({"calendar": calendar, "sync_token": sync_token, "page_token": page_token, "time_min": time_min})
        if self.fail:
            raise self.fail
        mode, _, start = (page_token or (f"s{sync_token}:0" if sync_token is not None else "f:0")).partition(":")
        if mode == "f":
            items = [e for _, e in sorted(self.box.items()) if e.get("status") != "cancelled"]
        else:
            if int(mode[1:]) < self.expired_before:
                raise SyncTokenExpired()
            items = [self.box[i] for i in sorted(self.changed) if self.changed[i] > int(mode[1:])]
        start = int(start)
        out = {"items": items[start:start + self.page]}
        if start + self.page < len(items):
            out["nextPageToken"] = f"{mode}:{start + self.page}"
        else:
            out["nextSyncToken"] = str(self.version)
        return out


def sw_item(i, title, *, due="2027-01-18T15:00:00Z", **kw):
    item = {"id": i, "title": title, "course": "Example Chemistry", "source": "canvas", "dueAt": due, "dueLocal": None, "allDay": False, "points": 10.0, "type": "Quiz",
            "state": "open", "done": False, "url": f"https://example.invalid/a/{i}", "locked": False, "lockedIn": None, "lockNote": None, "agentUrgency": None}
    item.update(kw)
    return item


class FakeSw:
    def __init__(self):
        self.calls, self.fail = [], None
        self.prep = {"generatedAt": "2027-01-15T08:00:00Z", "today": "2027-01-15", "timezone": "America/Chicago", "courses": [], "materialKinds": [],
                     "days": [{"date": "2027-01-18", "label": "A", "due": [sw_item(1, "Quiz walrus"), sw_item(2, "Essay one", state="graded", done=True)], "classes": []},
                              {"date": "2027-01-19", "label": "B", "due": [sw_item(6, "Unit test", due="2027-01-19T15:00:00Z", locked=True, lockedIn="canvas", lockNote="not published")], "classes": []}],
                     "overdue": [sw_item(3, "Late worksheet", due="2027-01-10T15:00:00Z")]}
        self.review = {"generatedAt": "2027-01-15T08:00:00Z", "health": [], "totals": {}, "courses": [],
                       "undated": [{"id": 4, "source": "msa", "course": "Example History", "title": "Reading log", "type": "Homework", "points": None, "description": "Read chapter two heron " + PLANTED}],
                       "recentChanges": [{"id": 5, "source": "canvas", "title": "Lab report", "course": "Example Chemistry", "dueAt": "2027-02-01T15:00:00Z", "state": "open"},
                                         {"id": 1, "source": "canvas", "title": "Quiz walrus OLD", "course": "Example Chemistry", "dueAt": "2027-01-17T15:00:00Z", "state": "open"}]}

    def __call__(self, argv):
        argv = tuple(argv)
        self.calls.append(argv)
        if self.fail:
            return self.fail
        if argv == ("sw", "prep", "--json"):
            return 0, json.dumps(self.prep)
        if argv == ("sw", "review", "--json"):
            return int(any(h.get("severity") == "error" for h in self.review["health"])), json.dumps(self.review)
        raise AssertionError("unexpected command")


def gh_pr(repo, number, title, *, checks="SUCCESS", draft=False, decision=None):
    return {"number": number, "title": title, "url": f"https://example.invalid/{repo}/pull/{number}", "isDraft": draft, "state": "OPEN", "updatedAt": "2027-01-14T10:00:00Z",
            "repository": {"nameWithOwner": repo}, "author": {"login": "octo-dev"}, "reviewDecision": decision,
            "commits": {"nodes": [{"commit": {"oid": "a" * 40, "statusCheckRollup": None if checks is None else {"state": checks}}}]}}


class FakeGh:
    def __init__(self, authored=(), review=()):
        self.authored, self.review, self.calls, self.reply = list(authored), list(review), [], None

    def __call__(self, argv):
        self.calls.append(tuple(argv))
        if self.reply is not None:
            return self.reply
        return 0, json.dumps({"data": {"authored": {"nodes": self.authored}, "review": {"nodes": self.review}}})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.index, self.sig = self.store("v3")
        self.now = NOW

    def store(self, name):
        return self.enterContext(Index(self.tmp / name / "index.sqlite")), self.tmp / name / "signals"

    def runner(self, ingester, index=None):
        return Runner(index or self.index, [ingester], clock=lambda: self.now)

    def drain(self, runner, rounds=6):
        for _ in range(rounds):
            runner.tick()
            self.now += 301

    def metas(self, index=None):
        return {d["ref"]: json.loads(d["meta_json"]) for d in (index or self.index).recent(20)}

    def items(self, source, sig=None):
        snap = signals.read(source, directory=sig or self.sig)
        self.assertEqual((snap["version"], snap["source"]), (1, source))
        return {item["id"]: item for item in snap["items"]}

    def sub(self, mapping, want):
        self.assertEqual({key: mapping.get(key, "<missing>") for key in want}, want)

    def raw(self):
        return b"".join(p.read_bytes() for p in self.tmp.rglob("*") if p.is_file())


class Signals(Base):
    def test_bytes_modes_and_order(self):
        items = [{"id": "b", "n": 2}, {"id": "a", "n": 1, "note": "x​y"}]
        path = signals.write("demo", items, directory=self.sig, needs_sign_in=False)
        expected = {"version": 1, "source": "demo", "needs_sign_in": False, "items": [{"id": "a", "n": 1, "note": "xy"}, {"id": "b", "n": 2}]}
        self.assertEqual(path.read_bytes(), (json.dumps(expected, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
        self.assertEqual((path.stat().st_mode & 0o777, self.sig.stat().st_mode & 0o777), (0o600, 0o700))
        signals.write("demo", list(reversed(items)), directory=self.sig, needs_sign_in=False)
        self.assertEqual(path.read_bytes(), (json.dumps(expected, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
        self.assertEqual(signals.read("demo", directory=self.sig), expected)
        self.assertEqual(os.listdir(self.sig), ["demo.json"])               # no temp file left behind
        signals.write("demo", [{"id": "k", "text": "key " + PLANTED}], directory=self.sig)
        self.assertNotIn(PLANTED.encode(), path.read_bytes())
        for bad in (("../x", []), ("Demo", []), ("demo", [{"id": "a"}, {"id": "a"}]), ("demo", [{"n": 1}])):
            with self.assertRaises(ValueError):
                signals.write(*bad, directory=self.sig)
        with self.assertRaises(ValueError):
            signals.write("demo", [], directory=self.sig, version=2)
        self.assertIsNone(signals.read("absent", directory=self.sig))
        path.chmod(0o644)
        self.assertIsNone(signals.read("demo", directory=self.sig))

    def test_links_are_never_followed(self):
        outside = self.tmp / "outside.json"
        outside.write_text("keep")
        self.sig.mkdir(mode=0o700)
        (self.sig / "demo.json").symlink_to(outside)
        self.assertIsNone(signals.read("demo", directory=self.sig))
        signals.write("demo", [{"id": "a"}], directory=self.sig)
        self.assertEqual(outside.read_text(), "keep")
        self.assertFalse((self.sig / "demo.json").is_symlink())
        real = self.tmp / "real"
        real.mkdir(mode=0o700)
        (self.tmp / "linked").symlink_to(real)
        with self.assertRaises(OSError):
            signals.write("demo", [{"id": "a"}], directory=self.tmp / "linked")
        self.assertEqual(os.listdir(real), [])
        self.assertEqual(config.V3_SIGNALS_DIR, config.V3_DIR / "signals")


class Proc(unittest.TestCase):
    def py(self, code):
        return [sys.executable, "-c", code]

    def test_output_status_stdin_and_stderr(self):
        code = "import json, sys; sys.stderr.write('hidden-stderr'); print(json.dumps({'stdin': sys.stdin.read()})); sys.exit(3)"
        self.assertEqual(proc.run_bounded(self.py(code), timeout_s=30, max_bytes=1000), (3, '{"stdin": ""}\n'))

    def test_limits_kill_at_once(self):
        cases = [("import time; time.sleep(60)", 0.5, 1000), ("import sys, time; sys.stdout.write('x' * 5000000); sys.stdout.flush(); time.sleep(60)", 30, 1000)]
        for code, timeout_s, max_bytes in cases:
            started = time.monotonic()
            with self.assertRaises(proc.ProcError) as caught:
                proc.run_bounded(self.py(code), timeout_s=timeout_s, max_bytes=max_bytes)
            self.assertLess(time.monotonic() - started, 15)
            self.assertNotIn("xxxx", str(caught.exception))
        with self.assertRaises(proc.ProcError):
            proc.run_bounded([os.path.join(tempfile.gettempdir(), "c3-missing-cli-xyz")], timeout_s=5, max_bytes=100)


class Calendar(Base):
    def setUp(self):
        super().setUp()
        people = [{"email": ME, "self": True}, {"email": "ada@example.org"}, {"email": "bo@example.org"}, {"email": "room@example.org", "resource": True}]
        self.api = FakeCalendar([cal_event("e1", "Robotics meet", description="Bring the agenda walrus", location="Gym", attendees=people, organizer={"email": "ada@example.org"}),
                                 cal_event("e2", "Study block", start={"date": "2027-01-21"}, end={"date": "2027-01-22"}, extendedProperties={"private": {"jarvis": "1"}}),
                                 cal_event("e3", "Weekly lab", recurrence=["RRULE:FREQ=WEEKLY"]), cal_event("e9", "Broken", start={"dateTime": "not a time"})])
        self.ingester = CalendarIngester(self.api, signals_dir=self.sig, clock=lambda: self.now)

    def test_full_sync_then_incremental(self):
        r = self.runner(self.ingester)
        self.drain(r, 4)
        metas = self.metas()
        self.assertEqual(set(metas), {"e1", "e2", "e3"})                   # the unreadable event is skipped
        self.sub(metas["e1"], {"untrusted": 1, "calendar": "primary", "status": "confirmed", "start": "2027-01-20T15:00:00Z", "end": "2027-01-20T16:00:00Z",
                               "all_day": 0, "attendees": 2, "owner_event": 0, "jarvis": 0, "recurring": 0})
        self.sub(metas["e2"], {"start": "2027-01-21", "end": "2027-01-22", "all_day": 1, "owner_event": 1, "jarvis": 1})
        self.assertEqual(metas["e3"]["recurring"], 1)
        self.assertEqual(len(self.index.discover("walrus")), 1)
        first = self.api.calls[0]
        self.assertEqual((first["calendar"], first["sync_token"], first["page_token"], first["time_min"]), ("primary", None, None, stamp(NOW - 30 * 86400)))
        self.assertEqual({c["time_min"] for c in takewhile(lambda c: c["sync_token"] is None, self.api.calls)}, {stamp(NOW - 30 * 86400)})
        mark = len(self.api.calls)
        self.api.put(cal_event("e4", "Dentist"))
        self.api.cancel("e2")
        self.drain(r, 2)
        self.assertEqual(set(self.metas()), {"e1", "e3", "e4"})
        self.assertTrue(all(c["time_min"] is None and (c["sync_token"] or c["page_token"]) for c in self.api.calls[mark:]))
        items = self.items("calendar")
        self.assertEqual(list(items), ["e1", "e3", "e4"])
        self.sub(items["e1"], {"title": "Robotics meet", "start": "2027-01-20T15:00:00Z", "end": "2027-01-20T16:00:00Z", "all_day": 0, "attendees": 2,
                               "owner_event": 0, "jarvis": 0, "status": "confirmed", "recurring": 0})

    def test_expired_token_resyncs_and_drops_what_vanished(self):
        r = self.runner(self.ingester)
        self.drain(r, 4)
        self.api.vanish("e2")
        self.api.expire()
        mark = len(self.api.calls)
        self.drain(r, 4)
        self.assertTrue([c for c in self.api.calls[mark:] if c["sync_token"] is None and c["page_token"] is None])
        self.assertEqual(set(self.metas()), {"e1", "e3"})
        self.assertEqual(list(self.items("calendar")), ["e1", "e3"])
        self.assertEqual(len(self.index.read(self.index.discover("walrus")[0]["id"])["messages"]), 1)   # replaced, not appended

    def test_missing_snapshot_forces_a_full_sync(self):
        r = self.runner(self.ingester)
        self.drain(r, 4)
        (self.sig / "calendar.json").unlink()
        mark = len(self.api.calls)
        self.drain(r, 3)
        self.assertEqual((self.api.calls[mark]["sync_token"], self.api.calls[mark]["page_token"]), (None, None))
        self.assertEqual(list(self.items("calendar")), ["e1", "e2", "e3"])

    def test_untrusted_text_and_identical_snapshots(self):
        self.api.put(cal_event("e5", "Re‮view " + PLANTED, description="token " + PLANTED))
        self.drain(self.runner(self.ingester), 4)
        index2, sig2 = self.store("again")
        api2 = FakeCalendar(reversed([self.api.box[k] for k in sorted(self.api.box)]))
        self.drain(self.runner(CalendarIngester(api2, signals_dir=sig2, clock=lambda: NOW), index2), 4)
        self.assertEqual((self.sig / "calendar.json").read_bytes(), (sig2 / "calendar.json").read_bytes())
        self.assertNotIn(PLANTED.encode(), self.raw())
        self.assertNotIn("‮", self.items("calendar")["e5"]["title"])
        self.assertTrue(hasattr(calendar_ingest, "HttpCalendar"))


class Schoolwork(Base):
    def setUp(self):
        super().setUp()
        self.sw, self.posts = FakeSw(), []
        self.ingester = self.make()

    def make(self):
        return SchoolworkIngester(self.sw, post=lambda fields: self.posts.append(fields) or {"id": "b"}, signals_dir=self.sig, clock=lambda: self.now)

    def test_items_become_docs(self):
        self.drain(self.runner(self.ingester), 2)
        metas = self.metas()
        self.assertEqual(set(metas), {"1", "2", "3", "4", "5", "6"})
        self.sub(metas["1"], {"untrusted": 1, "course": "Example Chemistry", "due": "2027-01-18T15:00:00Z", "state": "open", "done": 0, "locked": 0, "points": 10.0, "type": "Quiz", "system": "canvas"})
        self.sub(metas["2"], {"state": "graded", "done": 1})
        self.sub(metas["4"], {"due": None, "course": "Example History", "system": "msa", "done": 0, "locked": 0})
        self.sub(metas["6"], {"locked": 1})
        self.assertEqual(self.index.discover("walrus")[0]["title"], "Quiz walrus")       # prep's copy wins
        self.assertEqual(len(self.index.discover("heron")), 1)
        self.assertNotIn(PLANTED.encode(), self.raw())
        self.assertTrue(set(self.sw.calls) <= set(schoolwork.SW_ARGV))
        items = self.items("schoolwork")
        self.assertEqual(list(items), ["1", "2", "3", "4", "5", "6"])
        self.sub(items["2"], {"title": "Essay one", "course": "Example Chemistry", "due": "2027-01-18T15:00:00Z", "state": "graded", "done": 1, "locked": 0})
        self.assertIs(signals.read("schoolwork", directory=self.sig)["needs_sign_in"], False)
        self.assertEqual(self.posts, [])

    def test_a_quiet_repoll_changes_nothing(self):
        r = self.runner(self.ingester)
        self.drain(r, 2)
        before = (self.sig / "schoolwork.json").read_bytes()
        self.sw.prep["generatedAt"] = self.sw.review["generatedAt"] = "2027-01-15T09:00:00Z"
        self.assertEqual(r.tick()["schoolwork"]["docs"], 0)
        self.assertEqual((self.sig / "schoolwork.json").read_bytes(), before)
        self.now += 301
        self.sw.prep["overdue"][0]["state"] = "submitted"
        self.assertEqual(r.tick()["schoolwork"]["docs"], 1)
        index2, sig2 = self.store("again")
        self.drain(self.runner(SchoolworkIngester(self.sw, post=self.posts.append, signals_dir=sig2, clock=lambda: NOW), index2), 2)
        self.assertEqual((self.sig / "schoolwork.json").read_bytes(), (sig2 / "schoolwork.json").read_bytes())

    def test_expired_login_is_one_news_brief(self):
        self.sw.review["health"] = [{"code": "needs_login", "severity": "error", "message": "msa needs a new sign-in. Run: sw login-msa " + PLANTED, "data": {"source": "msa"}}]
        r = self.runner(self.ingester)
        self.assertIsNone(r.tick()["schoolwork"]["error"])                   # review exits 1 here; that is not a failure
        self.assertEqual(len(self.posts), 1)
        self.sub(self.posts[0], {"kind": "news", "rank": 3})
        self.assertIn("sw login-msa", self.posts[0]["ask"])
        self.assertNotIn(PLANTED, json.dumps(self.posts))
        self.assertIs(signals.read("schoolwork", directory=self.sig)["needs_sign_in"], True)
        self.assertTrue(self.metas())                                        # the board is still indexed
        self.now += 301
        self.drain(r, 3)
        self.drain(self.runner(self.make()), 2)                              # a new process remembers
        self.assertEqual(len(self.posts), 1)
        self.sw.review["health"] = []
        self.drain(r, 2)
        self.assertIs(signals.read("schoolwork", directory=self.sig)["needs_sign_in"], False)
        self.sw.review["health"] = [{"code": "needs_login", "severity": "error", "message": "again"}]
        self.drain(r, 2)
        self.assertEqual(len(self.posts), 2)

    def test_failures_carry_no_output(self):
        r = self.runner(self.ingester)
        for reply in ((2, "fatal " + PLANTED), (0, "not json " + PLANTED), (0, json.dumps(["a list", PLANTED])), (1, json.dumps(self.sw.prep))):
            self.sw.fail = reply
            with self.assertRaises(Exception) as caught:
                self.ingester.poll(None)
            self.assertNotIn(PLANTED, str(caught.exception))
            out = r.tick()
            self.assertTrue(out["schoolwork"]["error"])
            self.assertNotIn(PLANTED, json.dumps(out))
            self.now += 7200
        self.assertEqual((self.metas(), self.posts), ({}, []))

    def test_read_only_by_construction(self):
        self.assertEqual(schoolwork.SW_ARGV, (("sw", "prep", "--json"), ("sw", "review", "--json")))
        tree = ast.parse(Path(schoolwork.__file__).read_text())
        words = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertFalse(words & {"sync", "login-msa", "msa-probe", "add", "triage-apply", "triage-clear", "agent-review", "reparse", "serve", "init", "submit"})


class GitHub(Base):
    def setUp(self):
        super().setUp()
        self.gh = FakeGh([gh_pr("example-org/demo", 7, "Fix walrus", checks="FAILURE"), gh_pr("example-org/tools", 3, "Draft idea", draft=True, checks=None), {}, {"number": "x"}],
                         [gh_pr("example-org/demo", 7, "Fix walrus", checks="FAILURE"), gh_pr("other-org/site", 12, "Docs heron", checks="PENDING", decision="REVIEW_REQUIRED")])
        self.ingester = GitHubIngester(self.gh, signals_dir=self.sig)

    def test_prs_become_docs(self):
        self.drain(self.runner(self.ingester), 2)
        metas = self.metas()
        self.assertEqual(set(metas), {"example-org/demo#7", "example-org/tools#3", "other-org/site#12"})
        self.sub(metas["example-org/demo#7"], {"untrusted": 1, "repo": "example-org/demo", "number": 7, "state": "open", "draft": 0, "checks": "failure",
                                               "authored": 1, "review_requested": 1, "review_decision": None, "author": "octo-dev",
                                               "url": "https://example.invalid/example-org/demo/pull/7", "updated": "2027-01-14T10:00:00Z"})
        self.sub(metas["example-org/tools#3"], {"draft": 1, "checks": "none", "authored": 1, "review_requested": 0})
        self.sub(metas["other-org/site#12"], {"checks": "pending", "authored": 0, "review_requested": 1, "review_decision": "review_required"})
        self.assertTrue(set(self.gh.calls) <= set(github.GH_ARGV))
        items = self.items("github")
        self.assertEqual(list(items), ["example-org/demo#7", "example-org/tools#3", "other-org/site#12"])
        self.sub(items["other-org/site#12"], {"repo": "other-org/site", "number": 12, "title": "Docs heron", "draft": 0, "checks": "pending",
                                             "authored": 0, "review_requested": 1, "review_decision": "review_required"})

    def test_changes_closures_and_identical_snapshots(self):
        r = self.runner(self.ingester)
        self.drain(r, 2)
        out = r.tick()["github"]
        self.assertEqual((out["docs"], out["deleted"]), (0, 0))
        index2, sig2 = self.store("again")
        self.drain(self.runner(GitHubIngester(FakeGh(reversed(self.gh.authored), reversed(self.gh.review)), signals_dir=sig2), index2), 2)
        self.assertEqual((self.sig / "github.json").read_bytes(), (sig2 / "github.json").read_bytes())
        self.now += 301
        self.gh.authored[0] = self.gh.review[0] = gh_pr("example-org/demo", 7, "Fix walrus", checks="SUCCESS")
        del self.gh.authored[1:]                                # the PR goes, and so do the two unreadable rows: a listing with a row
        out = r.tick()["github"]                              # whose identity cannot be read is incomplete and deletes nothing
        self.assertEqual((out["docs"], out["deleted"]), (1, 1))
        self.assertEqual(self.metas()["example-org/demo#7"]["checks"], "success")
        self.assertEqual(list(self.items("github")), ["example-org/demo#7", "other-org/site#12"])

    def test_errors_and_rate_limits(self):
        r = self.runner(self.ingester)
        self.gh.reply = (1, json.dumps({"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}))
        self.assertEqual(r.tick()["github"]["error"], "rate_limited")
        for reply in ((1, "gh: bad credentials " + PLANTED), (0, "<html>" + PLANTED), (0, json.dumps({"errors": [{"type": "FORBIDDEN", "message": PLANTED}]}))):
            self.gh.reply = reply
            self.now += 7200
            with self.assertRaises(Exception) as caught:
                self.ingester.poll(None)
            self.assertNotIsInstance(caught.exception, RateLimited)
            self.assertNotIn(PLANTED, str(caught.exception))
            out = r.tick()
            self.assertEqual(out["github"]["error"], "ingest_error")
            self.assertNotIn(PLANTED, json.dumps(out))
        self.assertEqual(self.metas(), {})

    def test_every_command_is_read_only(self):
        self.assertTrue(github.GH_ARGV)
        for argv in github.GH_ARGV:
            self.assertEqual(tuple(argv[:3]), ("gh", "api", "graphql"))
            rest = argv[3:]
            self.assertTrue(rest and len(rest) % 2 == 0 and all(flag == "-f" for flag in rest[::2]), argv)
            fields = dict(value.partition("=")[::2] for value in rest[1::2])
            bare = re.sub(r'"(?:[^"\\]|\\.)*"', '""', fields["query"])
            self.assertIsNone(re.search(r"\b(?:mutation|subscription)\b", bare, re.I))
            self.assertRegex(bare, r"^\s*(?:query\b|\{)")
            self.assertRegex(bare, r"\bauthored\s*:\s*search\s*\(")
            self.assertRegex(bare, r"\breview\s*:\s*search\s*\(")
            self.assertIn("author:@me", fields["query"])
            self.assertIn("review-requested:@me", fields["query"])
            self.assertNotEqual(rules.decide(shlex.join(argv)).decision, rules.DENY)


class Jobs(Base):
    def setUp(self):
        super().setUp()
        self.token, self.found, self.jobindex = self.tmp / "google_token.json", {}, self.tmp / "jobs" / "index.sqlite"
        self.jobs = {job.name: job for job in jobs.default_jobs(feed_root=self.tmp / "feed", index_path=self.jobindex, google_token=self.token,
                                                                signals_dir=self.sig, find_cli=lambda name: self.found.get(name))}

    def grant(self, scopes):
        self.token.write_text(json.dumps({"client_id": "fake-client", "client_secret": "fake-secret", "refresh_token": "fake-token", "scopes": scopes}))
        self.token.chmod(0o600)

    def patched(self, factories):
        targets = (calendar_ingest, "HttpCalendar"), (calendar_ingest, "CalendarIngester"), (schoolwork, "SchoolworkIngester"), (github, "GitHubIngester")
        for (module, name), factory in zip(targets, factories):
            self.enterContext(patch.object(module, name, factory))

    @staticmethod
    def recorder(name, made, polls):
        class Fake:
            def __init__(self, source=None, **kw):
                source = source if source is not None else kw.pop("run", kw.pop("api", None))
                made.setdefault(name, []).append((source, kw))
                self.name = name

            def poll(self, cursor):
                polls.append((name, cursor))
                return [], {"at": 1}
        return Fake

    def test_table(self):
        self.assertLessEqual({"calendar", "schoolwork", "github"}, set(self.jobs))
        self.assertEqual([self.jobs[n].every_s for n in ("calendar", "schoolwork", "github")], [300, 900, 600])
        self.assertFalse(self.jobindex.exists())

    def test_nothing_is_built_without_a_grant_or_cli(self):
        built = []
        self.patched([lambda *a, **k: built.append(a)] * 4)
        for name in ("calendar", "schoolwork", "github"):
            self.jobs[name].run()
        self.grant(["https://www.googleapis.com/auth/gmail.readonly"])
        self.jobs["calendar"].run()
        self.assertEqual(built, [])
        self.assertFalse(self.jobindex.exists())

    def test_built_once_and_wired(self):
        self.grant(["https://www.googleapis.com/auth/calendar.events"])
        self.found.update(sw="/opt/fake/bin/sw", gh="/opt/fake/bin/gh")
        made, polls = {}, []
        self.patched([lambda token, **kw: made.setdefault("client", []).append(token)] + [self.recorder(n, made, polls) for n in ("calendar", "schoolwork", "github")])
        for _ in range(2):
            for name in ("calendar", "schoolwork", "github"):
                self.jobs[name].run()
        self.assertEqual({k: len(v) for k, v in made.items()}, {"client": 1, "calendar": 1, "schoolwork": 1, "github": 1})
        self.assertTrue(callable(made["client"][0]))                       # the token is fetched per call, never held
        self.assertEqual(sorted(polls), [("calendar", None), ("github", None), ("schoolwork", None)])
        self.assertTrue(all(kw.get("signals_dir") == self.sig for n in ("calendar", "schoolwork", "github") for _, kw in made[n]))
        made["schoolwork"][0][1]["post"]({"kind": "news", "rank": 3, "ask": "Test brief"})
        self.assertEqual([b["ask"] for b in Feed(self.tmp / "feed").briefs()], ["Test brief"])
        seen = []
        with patch.object(proc, "run_bounded", lambda argv, **kw: seen.append((list(argv), kw)) or (0, "{}")):
            made["schoolwork"][0][0](("sw", "prep", "--json"))
            made["github"][0][0](github.GH_ARGV[0])
        self.assertEqual([argv[0] for argv, _ in seen], ["/opt/fake/bin/sw", "/opt/fake/bin/gh"])
        self.assertEqual(seen[0][0][1:], ["prep", "--json"])
        self.assertTrue(all(0 < kw["timeout_s"] <= 120 and 0 < kw["max_bytes"] <= 16 * 2 ** 20 for _, kw in seen))

    def test_default_lookup_is_home_local_only(self):
        made, polls, seen = {}, [], []
        self.patched([None, None, self.recorder("schoolwork", made, polls), self.recorder("github", made, polls)])
        plain = {job.name: job for job in jobs.default_jobs(feed_root=self.tmp / "feed", index_path=self.jobindex, google_token=self.token, signals_dir=self.sig)}
        local = self.tmp / ".local" / "bin"
        local.mkdir(parents=True)
        (local / "gh").write_text("not executable")
        (local / "sw").write_text("#!/bin/sh\nexit 0\n")
        (local / "sw").chmod(0o755)
        with patch.dict(os.environ, {"HOME": str(self.tmp)}), patch("shutil.which", lambda name, *a, **k: "/usr/bin/" + name), \
             patch.object(proc, "run_bounded", lambda argv, **kw: seen.append(list(argv)) or (0, "{}")):
            plain["github"].run()                                            # PATH has one, ~/.local/bin has no executable
            plain["schoolwork"].run()
            made["schoolwork"][0][0](("sw", "review", "--json"))
        self.assertEqual(list(made), ["schoolwork"])
        self.assertEqual(seen, [[str(local / "sw"), "review", "--json"]])

    def test_agentd_still_starts_light(self):
        new = ("jarvis.v3.signals", "jarvis.v3.ingest.proc", "jarvis.v3.ingest.calendar", "jarvis.v3.ingest.schoolwork", "jarvis.v3.ingest.github")
        code = (f"import sys\nfrom jarvis.v3 import jobs\njobs.default_jobs()\nprint(sorted(m for m in sys.modules if m.split('.')[0] in ('httpx', 'sqlite3') or m in {new!r}))\n"
                "import jarvis.v3.ingest.calendar, jarvis.v3.ingest.schoolwork, jarvis.v3.ingest.github\nprint('httpx' in sys.modules)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                             env={"PYTHONPATH": str(REPO), "HOME": str(self.tmp), "PATH": os.environ.get("PATH", "")})
        self.assertEqual((out.returncode, out.stdout.split()), (0, ["[]", "False"]), out.stderr[-400:])


if __name__ == "__main__":
    unittest.main()
