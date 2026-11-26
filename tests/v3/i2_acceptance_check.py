"""Orchestrator-owned acceptance tests for I2 (implementers: do not edit; if one is wrong, stop and say why).

Contract: cheap triggers (plan §15): compare I2's previous copy of each source's snapshot with the current one and append signal records
that I3 will turn into proposals. No model, no network of its own, no proposals, no decision briefs, no levels (I1 assigns those later).
  jarvis/config.py   V3_TRIGGERS_DIR = V3_DIR / "triggers"   (map 1.1 TRIGGERS_DIR; I2's own state, apart from V3_SIGNALS_DIR)
  jarvis/v3/triggers.py   (imports only the stdlib, jarvis.config, jarvis.untrusted and jarvis.v3.signals/_paths at module level)
    SOURCES = ("calendar", "schoolwork", "github", "commitments", "mail")
    KINDS = ("mail.needs_reply", "calendar.changed", "calendar.idle_block_tomorrow", "school.assignment_new", "school.due_within_72h",
             "gh.review_requested", "gh.ci_failed", "commitment.due_soon");  DEFERRED = ("project.stale",) (no projects snapshot exists)
    STALE_S = {"calendar": 7200, "schoolwork": 21600, "github": 7200}; DEDUPE_TTL_S = 30 * 86400; MAX_PER_KIND = 20; LOG_KEEP = 5000
    (STALE_S, MAX_PER_KIND and LOG_KEEP are read from the module at call time.)  Time zone for days: America/Chicago.
    evaluate(source, previous, current, now, *, busy=None) -> list of candidates {"kind", "source_ref", "summary_fields", "dedupe_key",
      "tainted"}: pure (no I/O apart from calling busy). previous/current are snapshot envelopes ({"version": 1, "source", ..., "items"});
      previous None = first run: edge kinds (calendar.changed, school.assignment_new) emit nothing, level kinds (all others) do.
      Every candidate is tainted True (other people's text, or rows a model extracted). summary_fields hold ids, counts, times and
      flags, plus "untrusted_title": untrusted.one_line(title, cap=120) where the kind has a title and a bounded untrusted mail
      sender for CTX1. No bodies, recipients or notes.
      calendar.changed  ids in previous or current whose items differ; skipped when the item (current, else previous) has jarvis == 1 or
        its end is not after now (timed "...Z" instants; an all-day date is midnight in Chicago). change "added" | "removed" | "moved"
        (start, end or all_day differ) | "updated"; fields event, change, start, end, all_day, attendees, owner_event, recurring,
        untrusted_title, and previous_start/previous_end only for "moved". key "calendar.changed:<id>:<16 hex digest of the change>".
      calendar.idle_block_tomorrow  only with busy: busy(start, end) is called once with tomorrow 09:00 and 21:00 Chicago (aware) and
        returns (start, end) aware busy intervals; gaps of at least 120 minutes in that window -> one candidate per date: source_ref
        "calendar:<date>", key "calendar.idle_block_tomorrow:<date>", fields date, blocks [{"start": "HH:MM", "end": "HH:MM"}] (earliest
        five, Chicago), longest_min. busy raising -> no candidate of this kind, nothing raised.
      school.assignment_new  id in current, not in previous, done == 0. key "school.assignment_new:<id>".
      school.due_within_72h  done == 0 and now < due <= now + 72 h. key "school.due_within_72h:<id>:<due>".
      gh.review_requested  review_requested == 1 and draft != 1. key "gh.review_requested:<id>".
      gh.ci_failed  authored == 1 and checks in ("failure", "error"). key "gh.ci_failed:<id>".
      commitment.due_soon  state "open", due set and due <= Chicago date of now + 2 days (commitments.due_within(days=2): overdue too);
        fields commitment, due, direction, overdue (due before the Chicago date of now), untrusted_title (the "what"; never "who"). key "commitment.due_soon:<id>:<due>".
      mail.needs_reply  (a heuristic candidate; the planner confirms) per thread, its newest item (ts, then id) when from_owner == 0,
        "INBOX" in labels, no label in SPAM/TRASH/CATEGORY_PROMOTIONS/CATEGORY_SOCIAL/CATEGORY_UPDATES/CATEGORY_FORUMS, and
        now - ts >= 3600. fields message, thread, received (UTC "%Y-%m-%dT%H:%M:%SZ"), untrusted_title, untrusted_from,
        automated. key "mail.needs_reply:<id>".
      source_ref "<calendar|schoolwork|github|commitments|gmail>:<item id>".
    Snapshots I2 builds itself each tick, written with signals.write into signals_dir: "commitments" (items {"id", "who", "what", "due":
      "YYYY-MM-DD"|None, "direction", "state"} from commitments()) and "mail" (items {"id", "thread", "from_owner", "ts", "labels",
      "title", "from"} from index.documents_between(since=now - 7 days, until=now) rows of source "gmail"; meta_json gives from_owner,
      thread (the ref when empty), labels and sender. No body or recipient.
    Triggers(*, root=None, signals_dir=None, sources=SOURCES, post, index=None, commitments=None, busy=None, clock=time.time)
      root default config.V3_TRIGGERS_DIR and signals_dir default config.V3_SIGNALS_DIR, both read at call time.
      tick() -> {"emitted": int, "blocked_config": {source: reason}}. Per listed source: pre-flight, evaluate, drop candidates whose
      dedupe key is held, keep at most MAX_PER_KIND per kind (the rest are not held, so a level kind emits them later), append records.
      Record {"seq", "id", "kind", "source", "source_ref", "summary_fields", "dedupe_key", "tainted", "created"}: seq counts from 1 by 1 and
      is never reused, id 32 lowercase hex, created = clock() as UTC "%Y-%m-%dT%H:%M:%SZ". root/signals.jsonl is the append-only log
      (dir 0700, file 0600, no link followed: a link there makes tick raise OSError and writes nothing through it; a log it cannot
      append to, such as one made 0400, makes tick raise OSError having advanced no state or previous copy); after a tick it holds
      at most LOG_KEEP records, the newest; a torn last line is skipped by readers and never swallows a later record. Everything
      else I2 keeps (held keys, episodes, previous copies) lives under root and is written after the log is durable; after a crash at any
      point the next tick re-emits nothing: keys of log records newer than that state are held again.
      Dedupe: a key emitted is held. An edge kind's key is held for DEDUPE_TTL_S. A level kind's key is released by the first tick that
      evaluated its source (not blocked) without that condition, or after DEDUPE_TTL_S; it may then emit again.
      Pre-flight -> blocked_config reason, by precedence: "missing" (no file), "unreadable" (signals.read is None, an item lacks a C3 field
      or has the wrong type, an index or commitments() error), "stale" (now - file mtime > STALE_S[source]), "needs_sign_in" (schoolwork).
      A blocked source emits nothing, keeps its previous copy and releases no keys; the other sources go on. Once per episode (blocked
      ticks in a row) at its first tick with a reason other than needs_sign_in (the schoolwork ingester posts that one):
      post({"kind": "news", "rank": 4, "ask": BLOCKED_ASK[source], "idempotency_key": "triggers-blocked:<source>:<episode>"}); a post that
      raises is retried next tick with the same key; a readable tick ends the episode.
    read_signals(after=0, *, root=None, limit=500) -> records with seq > after, ascending (I3 keeps its own position); [] with no log.
  jarvis/v3/jobs.py   default_jobs() adds Job("triggers", every_s=300) after "github"; building it imports none of the above.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from jarvis import config
from jarvis.v3 import jobs, signals, triggers
from jarvis.v3.commitments import Commitment
from jarvis.v3.feed import Feed

REPO = Path(__file__).resolve().parents[2]
NOW = 1_800_000_000.0                      # 2027-01-15T08:00:00Z, 02:00 in Chicago; tomorrow is 2027-01-16
CHI = ZoneInfo("America/Chicago")
PLANTED = "ghp" + "_" + "I2x7" * 9
SENDER = "Example Sender <alerts@example.invalid>"
RECORD = {"seq", "id", "kind", "source", "source_ref", "summary_fields", "dedupe_key", "tainted", "created"}
CANDIDATE = {"kind", "source_ref", "summary_fields", "dedupe_key", "tainted"}


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tmr(h, m=0, day=16):
    return datetime(2027, 1, day, h, m, tzinfo=CHI)


def cal(eid, title="Standup", start="2027-01-20T15:00:00Z", end="2027-01-20T16:00:00Z", **kw):
    return {"id": eid, "title": title, "start": start, "end": end, "all_day": 0, "attendees": 0, "owner_event": 1, "jarvis": 0,
            "status": "confirmed", "recurring": 0, **kw}


def sw(aid, due=None, done=0, title="Essay"):
    return {"id": aid, "title": title, "course": "English", "due": due, "state": "open", "done": done, "locked": 0}


def pr(number, authored=0, review_requested=0, checks="success", draft=0, title="Fix parser"):
    return {"id": f"example/app#{number}", "repo": "example/app", "number": number, "title": title, "draft": draft, "checks": checks,
            "authored": authored, "review_requested": review_requested, "review_decision": None}


def mail(mid, thread, ts, from_owner=0, labels=("INBOX",), title="Lunch Friday?"):
    return {"id": mid, "thread": thread, "from_owner": from_owner, "ts": ts, "labels": list(labels), "title": title}


def env(source, items, **extra):
    return {"version": 1, "source": source, **extra, "items": sorted(items, key=lambda item: item["id"])}


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


class Posts:
    def __init__(self):
        self.attempts, self.ok, self.fail = [], [], False

    def __call__(self, fields):
        self.attempts.append(dict(fields))
        if self.fail:
            raise RuntimeError("feed down")
        self.ok.append(dict(fields))


class FakeIndex:
    def __init__(self):
        self.rows, self.fail = [], False

    def add(self, mid, thread, ts, from_owner=0, labels=("INBOX",), title="Lunch Friday?"):
        meta = {"untrusted": 1, "from_owner": from_owner, "thread": thread, "from": SENDER, "to": "user@example.com",
                "labels": list(labels)}
        self.rows.append({"id": mid * 2, "source": "gmail", "ref": mid, "title": title, "ts": ts, "meta_json": json.dumps(meta)})

    def documents_between(self, *, since, until):
        if self.fail:
            raise OSError("index locked")
        return sorted((row for row in self.rows if since < row["ts"] <= until), key=lambda row: -row["ts"])


class Busy:
    def __init__(self, *spans, fail=False):
        self.spans, self.fail, self.calls = list(spans), fail, []

    def __call__(self, start, end):
        self.calls.append((start, end))
        if self.fail:
            raise RuntimeError("calendar down")
        return list(self.spans)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.sig, self.root = self.tmp / "signals", self.tmp / "triggers"
        self.clock, self.posts, self.index, self.rows = Clock(), Posts(), FakeIndex(), []

    def put(self, source, items, age=60, **extra):
        path = signals.write(source, items, directory=self.sig, **extra)
        os.utime(path, (self.clock.t - age, self.clock.t - age))

    def load(self):
        if isinstance(self.rows, Exception):
            raise self.rows
        return list(self.rows)

    def tick(self, sources, at=None, fresh=True):
        self.clock.t = self.clock.t if at is None else at
        if fresh and self.sig.is_dir():
            for path in self.sig.glob("*.json"):
                os.utime(path, (self.clock.t - 60, self.clock.t - 60))
        made = triggers.Triggers(root=self.root, signals_dir=self.sig, sources=sources, post=self.posts, index=self.index,
                                 commitments=self.load, clock=self.clock)
        return made.tick()

    def log(self, after=0):
        return triggers.read_signals(after, root=self.root)

    def check(self, row, keys=CANDIDATE):
        self.assertEqual(set(row), keys)
        self.assertIn(row["kind"], triggers.KINDS)
        self.assertIs(row["tainted"], True)
        self.assertTrue(row["dedupe_key"].startswith(row["kind"] + ":"), row)
        self.assertRegex(row["source_ref"], r"^(calendar|schoolwork|github|commitments|gmail):.+")
        fields = dict(row["summary_fields"])
        text = json.dumps(fields)
        self.assertLessEqual(len(text), 2048)
        for bad in (PLANTED, "user@example.com", "\\n"):
            self.assertNotIn(bad, text)
        if row["kind"] == "mail.needs_reply":
            self.assertIn(fields.pop("untrusted_from"), ("", SENDER))
            self.assertIs(fields["automated"], False)
        self.assertNotIn("alerts@example.invalid", json.dumps(fields))
        stack = list(row["summary_fields"].values())
        while stack:
            value = stack.pop()
            self.assertIsInstance(value, (str, int, bool, type(None), list, dict))
            stack.extend(value.values() if isinstance(value, dict) else value if isinstance(value, list) else ())
            if isinstance(value, str):
                self.assertLessEqual(len(value), 120)

    def ev(self, source, prev, cur, now=NOW, **kw):
        out = triggers.evaluate(source, None if prev is None else env(source, prev), env(source, cur), now, **kw)
        for row in out:
            self.check(row)
        return out


class Rules(Base):
    def test_public_names(self):
        self.assertEqual(set(triggers.KINDS), {"mail.needs_reply", "calendar.changed", "calendar.idle_block_tomorrow", "school.assignment_new",
                                               "school.due_within_72h", "gh.review_requested", "gh.ci_failed", "commitment.due_soon"})
        self.assertEqual((triggers.DEFERRED, triggers.SOURCES), (("project.stale",), ("calendar", "schoolwork", "github", "commitments", "mail")))
        self.assertEqual((triggers.STALE_S, triggers.DEDUPE_TTL_S, triggers.MAX_PER_KIND, triggers.LOG_KEEP),
                         ({"calendar": 7200, "schoolwork": 21600, "github": 7200}, 30 * 86400, 20, 5000))
        with patch.object(config, "V3_TRIGGERS_DIR", self.tmp / "elsewhere", create=True):
            self.assertEqual(triggers.read_signals(0), [])

    def test_calendar_changed(self):
        prev = [cal("A"), cal("B", title="Lab"), cal("C", start="2027-01-01T15:00:00Z", end="2027-01-01T16:00:00Z"), cal("E"), cal("J", jarvis=1)]
        cur = [cal("A", start="2027-01-21T15:00:00Z", end="2027-01-21T16:00:00Z"), cal("B", title="Lab moved\nrooms " + PLANTED + " x" * 200),
               cal("D", title="Dentist", start="2027-01-22", end="2027-01-23", all_day=1), cal("J", jarvis=1, title="Study block 2")]
        out = {row["summary_fields"]["event"]: row for row in self.ev("calendar", prev, signals.scrub(cur))}
        self.assertEqual({k: row["summary_fields"]["change"] for k, row in out.items()}, {"A": "moved", "B": "updated", "D": "added", "E": "removed"})
        a = out["A"]["summary_fields"]
        self.assertEqual((a["start"], a["previous_start"], a["previous_end"], out["A"]["source_ref"]),
                         ("2027-01-21T15:00:00Z", "2027-01-20T15:00:00Z", "2027-01-20T16:00:00Z", "calendar:A"))
        self.assertNotIn("previous_start", out["B"]["summary_fields"])
        self.assertTrue(out["B"]["summary_fields"]["untrusted_title"].startswith("Lab moved rooms"))
        for eid, row in out.items():
            self.assertRegex(row["dedupe_key"], rf"^calendar\.changed:{eid}:[0-9a-f]{{16}}$")
        again = self.ev("calendar", prev, [cal("A", start="2027-01-21T17:00:00Z", end="2027-01-21T18:00:00Z")])
        self.assertNotIn(out["A"]["dedupe_key"], {row["dedupe_key"] for row in again})
        self.assertEqual((self.ev("calendar", None, cur), self.ev("calendar", cur, cur)), ([], []))

    def test_idle_block_tomorrow(self):
        items = [cal("A")]
        busy = Busy((tmr(9), tmr(11)))
        [row] = self.ev("calendar", items, items, busy=busy)
        self.assertEqual((row["kind"], row["source_ref"], row["dedupe_key"]),
                         ("calendar.idle_block_tomorrow", "calendar:2027-01-16", "calendar.idle_block_tomorrow:2027-01-16"))
        self.assertEqual(row["summary_fields"], {"date": "2027-01-16", "blocks": [{"start": "11:00", "end": "21:00"}], "longest_min": 600})
        self.assertEqual(busy.calls, [(tmr(9), tmr(21))])
        [row] = self.ev("calendar", items, items, busy=Busy((tmr(9, 30), tmr(10)), (tmr(11), tmr(13)), (tmr(15, 30), tmr(21))))
        self.assertEqual(row["summary_fields"]["blocks"], [{"start": "13:00", "end": "15:30"}])
        [row] = self.ev("calendar", items, items, busy=Busy((tmr(8), tmr(12)), (tmr(19), tmr(2, day=17))))
        self.assertEqual(row["summary_fields"]["longest_min"], 420)
        for busy in (Busy((tmr(8), tmr(22))), Busy(fail=True), None):
            self.assertEqual(self.ev("calendar", items, items, busy=busy), [])

    def test_school_kinds(self):
        cur = [sw("A"), sw("X", "2027-01-17T08:00:00Z"), sw("Y", "2027-01-18T08:00:00Z"), sw("Z", "2027-01-18T08:00:01Z"),
               sw("P", "2027-01-14T00:00:00Z"), sw("D", "2027-01-16T00:00:00Z", done=1)]
        out = self.ev("schoolwork", [sw("A")], cur)
        self.assertEqual({r["dedupe_key"] for r in out if r["kind"] == "school.assignment_new"}, {f"school.assignment_new:{x}" for x in "XYZP"})
        self.assertEqual({r["dedupe_key"] for r in out if r["kind"] == "school.due_within_72h"},
                         {"school.due_within_72h:X:2027-01-17T08:00:00Z", "school.due_within_72h:Y:2027-01-18T08:00:00Z"})
        self.assertEqual({r["source_ref"] for r in out}, {f"schoolwork:{x}" for x in "XYZP"})
        self.assertEqual({r["kind"] for r in self.ev("schoolwork", None, cur)}, {"school.due_within_72h"})

    def test_github_kinds(self):
        cur = [pr(1, review_requested=1), pr(2, review_requested=1, draft=1), pr(3, authored=1, checks="failure"), pr(4, authored=1, checks="error"),
               pr(5, checks="failure"), pr(6, authored=1, checks="pending")]
        out = self.ev("github", None, cur)
        self.assertEqual({r["dedupe_key"] for r in out}, {"gh.review_requested:example/app#1", "gh.ci_failed:example/app#3", "gh.ci_failed:example/app#4"})
        self.assertIn("github:example/app#1", {r["source_ref"] for r in out})
        key = lambda rows: sorted(r["dedupe_key"] for r in rows)
        self.assertEqual(key(self.ev("github", cur, cur)), key(out))                        # level kinds: dedupe, not the diff, stops repeats

    def test_mail_needs_reply(self):
        items = [mail("m1", "t1", NOW - 7200), mail("m2", "t2", NOW - 600), mail("m3", "t3", NOW - 9000), mail("m4", "t3", NOW - 8000, from_owner=1),
                 mail("m5", "t4", NOW - 7200, labels=("INBOX", "CATEGORY_PROMOTIONS")), mail("m6", "t5", NOW - 7200, labels=("IMPORTANT",)),
                 mail("m7", "t6", NOW - 9000, from_owner=1), mail("m8", "t6", NOW - 5000)]
        out = {r["dedupe_key"]: r for r in self.ev("mail", None, items)}
        self.assertEqual(set(out), {"mail.needs_reply:m1", "mail.needs_reply:m8"})
        m8 = out["mail.needs_reply:m8"]
        self.assertEqual((m8["source_ref"], m8["summary_fields"]["thread"], m8["summary_fields"]["received"]), ("gmail:m8", "t6", stamp(NOW - 5000)))


class Store(Base):
    ALL = ("calendar", "schoolwork", "github", "commitments", "mail")

    def test_quiet_day_records_log_and_restart(self):
        self.put("calendar", [cal("A")])
        self.put("schoolwork", [sw("X", "2027-01-17T08:00:00Z")], needs_sign_in=False)
        self.put("github", [pr(1, review_requested=1)])
        self.rows = [Commitment("c-0000000a", "Sam", "send the slides", date(2027, 1, 16), "owner-owes", "owner", "open")]
        self.index.add("m1", "t1", NOW - 7200)
        first = self.tick(self.ALL)
        records = self.log()
        self.assertEqual((first["emitted"], first["blocked_config"], len(records)), (4, {}, 4))
        self.assertEqual([r["seq"] for r in records], [1, 2, 3, 4])
        self.assertEqual(len({r["id"] for r in records}), 4)
        for r in records:
            self.check(r, RECORD)
            self.assertRegex(r["id"], r"^[0-9a-f]{32}$")
            self.assertEqual((r["created"], r["source"] in triggers.SOURCES), (stamp(NOW), True))
        self.assertEqual(self.log(3), records[3:])
        self.assertEqual((self.root.stat().st_mode & 0o777, (self.root / "signals.jsonl").stat().st_mode & 0o777), (0o700, 0o600))
        for at in (NOW + 300, NOW + 600):                                    # identical snapshots, a new instance each tick
            self.assertEqual(self.tick(self.ALL, at=at), {"emitted": 0, "blocked_config": {}})
        self.assertEqual((self.log(), self.posts.attempts), (records, []))

    def test_level_key_released_by_absence_not_by_blocked_ticks(self):
        self.put("github", [pr(1, review_requested=1)])
        self.assertEqual(self.tick(("github",))["emitted"], 1)
        self.put("github", [pr(1)])
        self.assertEqual(self.tick(("github",), at=NOW + 60)["emitted"], 0)
        self.put("github", [pr(1, review_requested=1)])
        self.assertEqual(self.tick(("github",), at=NOW + 120)["emitted"], 1)          # came back: emits again
        (self.sig / "github.json").unlink()
        self.assertEqual(self.tick(("github",), at=NOW + 180)["blocked_config"], {"github": "missing"})
        self.put("github", [pr(1, review_requested=1)])
        self.assertEqual(self.tick(("github",), at=NOW + 240)["emitted"], 0)          # unseen while blocked: still held
        self.assertEqual(self.tick(("github",), at=NOW + 120 + triggers.DEDUPE_TTL_S + 1)["emitted"], 1)

    def test_edge_key_held_for_ttl(self):
        self.put("schoolwork", [sw("A")], needs_sign_in=False)
        self.assertEqual(self.tick(("schoolwork",))["emitted"], 0)                     # first run is the baseline
        self.put("schoolwork", [sw("A"), sw("X")], needs_sign_in=False)
        self.assertEqual(self.tick(("schoolwork",), at=NOW + 60)["emitted"], 1)
        self.put("schoolwork", [sw("A")], needs_sign_in=False)
        self.assertEqual(self.tick(("schoolwork",), at=NOW + 120)["emitted"], 0)
        self.put("schoolwork", [sw("A"), sw("X")], needs_sign_in=False)                # left the CLI's window and came back
        self.assertEqual(self.tick(("schoolwork",), at=NOW + 180)["emitted"], 0)
        self.assertEqual([r["kind"] for r in self.log()], ["school.assignment_new"])

    def test_crash_after_the_log_reemits_nothing(self):
        self.put("schoolwork", [sw("A")], needs_sign_in=False)
        self.put("github", [pr(1)])
        self.tick(("schoolwork", "github"))
        saved = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file() and p.name != "signals.jsonl"}
        self.put("schoolwork", [sw("A"), sw("X")], needs_sign_in=False)
        self.put("github", [pr(1, review_requested=1)])
        self.assertEqual(self.tick(("schoolwork", "github"), at=NOW + 60)["emitted"], 2)
        for p in [p for p in self.root.rglob("*") if p.is_file() and p.name != "signals.jsonl"]:
            p.unlink()                                                          # the state and previous copies never reached disk
        for rel, data in saved.items():
            (self.root / rel).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            (self.root / rel).write_bytes(data)
            (self.root / rel).chmod(0o600)
        self.assertEqual(self.tick(("schoolwork", "github"), at=NOW + 120)["emitted"], 0)
        self.assertEqual(sorted(r["kind"] for r in self.log()), ["gh.review_requested", "school.assignment_new"])
        log = self.root / "signals.jsonl"
        log.chmod(0o400)                                                        # readable, but the append fails
        self.put("schoolwork", [sw("A"), sw("X"), sw("Y")], needs_sign_in=False)
        with self.assertRaises(OSError):
            self.tick(("schoolwork", "github"), at=NOW + 180)
        log.chmod(0o600)
        self.assertEqual(self.tick(("schoolwork", "github"), at=NOW + 240)["emitted"], 1)        # the failed tick advanced no previous copy

    def test_cap_compaction_torn_tail_and_links(self):
        self.put("github", [pr(n, review_requested=1) for n in range(1, 26)])
        self.assertEqual(self.tick(("github",))["emitted"], 20)
        self.assertEqual(self.tick(("github",), at=NOW + 60)["emitted"], 5)
        with patch.object(triggers, "LOG_KEEP", 3):
            self.put("github", [pr(n, review_requested=1) for n in range(1, 27)])
            self.tick(("github",), at=NOW + 120)
            self.assertEqual([r["seq"] for r in self.log()], [24, 25, 26])
            with open(self.root / "signals.jsonl", "ab") as stream:
                stream.write(b'{"seq": 27, "id": "tor')
            self.put("github", [pr(n, review_requested=1) for n in range(1, 28)])
            self.tick(("github",), at=NOW + 180)
            [new] = self.log(26)
            self.assertEqual((new["seq"], new["dedupe_key"]), (27, "gh.review_requested:example/app#27"))
        (self.root / "signals.jsonl").unlink()
        outside = self.tmp / "outside.txt"
        outside.write_text("keep")
        (self.root / "signals.jsonl").symlink_to(outside)
        self.put("github", [pr(n, review_requested=1) for n in range(1, 29)])
        with self.assertRaises(OSError):
            self.tick(("github",), at=NOW + 240)
        self.assertEqual(outside.read_text(), "keep")

    def test_commitments_and_mail_snapshots(self):
        self.rows = [Commitment("c-0000000a", "Sam", "send the slides", date(2027, 1, 17), "owner-owes", "owner", "open"),
                     Commitment("c-0000000b", "Lee", "return the book", date(2027, 1, 10), "owed-to-owner", "gmail:m9", "open"),
                     Commitment("c-0000000c", "Sam", "call back", date(2027, 1, 18), "owner-owes", "owner", "open"),
                     Commitment("c-0000000d", "Sam", "pay dues", date(2027, 1, 16), "owner-owes", "owner", "done")]
        self.index.add("m1", "t1", NOW - 7200, title="Re: lunch " + PLANTED)
        self.index.add("m0", "t0", NOW - 8 * 86400)                              # outside the 7-day window
        self.tick(("commitments", "mail"))
        due = {r["summary_fields"]["commitment"]: r for r in self.log() if r["kind"] == "commitment.due_soon"}
        self.assertEqual({k: r["summary_fields"]["overdue"] for k, r in due.items()}, {"c-0000000a": False, "c-0000000b": True})
        self.assertEqual(due["c-0000000a"]["dedupe_key"], "commitment.due_soon:c-0000000a:2027-01-17")
        self.assertEqual(signals.read("commitments", directory=self.sig)["items"][0],
                         {"id": "c-0000000a", "who": "Sam", "what": "send the slides", "due": "2027-01-17", "direction": "owner-owes", "state": "open"})
        [item] = signals.read("mail", directory=self.sig)["items"]
        self.assertEqual(set(item), {"id", "thread", "from_owner", "ts", "labels", "title", "from"})
        self.assertEqual((item["id"], item["thread"], item["from_owner"], item["from"]), ("m1", "t1", 0, SENDER))
        [record] = [r for r in self.log() if r["kind"] == "mail.needs_reply"]
        self.assertEqual((record["source_ref"], record["summary_fields"]["untrusted_from"]), ("gmail:m1", SENDER))
        self.rows, self.index.fail = ValueError("Commitments line 3: bad"), True
        self.assertEqual(self.tick(("commitments", "mail"), at=NOW + 60)["blocked_config"], {"commitments": "unreadable", "mail": "unreadable"})
        self.assertEqual(sorted(p["ask"] for p in self.posts.ok), sorted([triggers.BLOCKED_ASK["commitments"], triggers.BLOCKED_ASK["mail"]]))


class Preflight(Base):
    def test_blocked_once_per_episode(self):
        self.assertEqual(self.tick(("calendar",))["blocked_config"], {"calendar": "missing"})
        self.assertEqual(self.posts.ok, [{"kind": "news", "rank": 4, "ask": triggers.BLOCKED_ASK["calendar"], "idempotency_key": self.posts.ok[0]["idempotency_key"]}])
        self.assertTrue(self.posts.ok[0]["idempotency_key"].startswith("triggers-blocked:calendar:"))
        self.assertEqual(self.tick(("calendar",), at=NOW + 60)["blocked_config"], {"calendar": "missing"})
        self.sig.mkdir(mode=0o700, exist_ok=True)
        (self.sig / "calendar.json").write_bytes(b"{not json")
        (self.sig / "calendar.json").chmod(0o600)
        self.assertEqual(self.tick(("calendar",), at=NOW + 120)["blocked_config"], {"calendar": "unreadable"})
        self.assertEqual(len(self.posts.ok), 1)                                   # same episode, across instances
        self.put("calendar", [cal("A")])
        self.assertEqual(self.tick(("calendar",), at=NOW + 180)["blocked_config"], {})
        self.put("calendar", [cal("A")], age=triggers.STALE_S["calendar"] + 60)
        self.assertEqual(self.tick(("calendar",), fresh=False)["blocked_config"], {"calendar": "stale"})
        self.put("calendar", [{"id": "A", "title": "no times"}])
        self.assertEqual(self.tick(("calendar",), at=NOW + 240)["blocked_config"], {"calendar": "unreadable"})
        self.assertEqual(len(self.posts.ok), 2)
        self.assertNotEqual(self.posts.ok[0]["idempotency_key"], self.posts.ok[1]["idempotency_key"])

    def test_one_bad_source_and_a_failed_post(self):
        self.put("github", [pr(1, review_requested=1)])
        self.posts.fail = True
        result = self.tick(("calendar", "github"))
        self.assertEqual((result["emitted"], result["blocked_config"], len(self.posts.attempts)), (1, {"calendar": "missing"}, 1))
        self.posts.fail = False
        self.tick(("calendar", "github"), at=NOW + 60)
        self.tick(("calendar", "github"), at=NOW + 120)
        self.assertEqual((len(self.posts.attempts), len(self.posts.ok)), (2, 1))
        self.assertEqual(self.posts.attempts[0]["idempotency_key"], self.posts.attempts[1]["idempotency_key"])

    def test_sign_in_blocks_without_a_post_until_another_reason(self):
        self.put("schoolwork", [sw("X", "2027-01-17T08:00:00Z")], needs_sign_in=True)
        self.assertEqual(self.tick(("schoolwork",)), {"emitted": 0, "blocked_config": {"schoolwork": "needs_sign_in"}})
        self.assertEqual(self.posts.attempts, [])
        self.put("schoolwork", [sw("X", "2027-01-17T08:00:00Z")], age=triggers.STALE_S["schoolwork"] + 60, needs_sign_in=True)
        self.assertEqual(self.tick(("schoolwork",), fresh=False)["blocked_config"], {"schoolwork": "stale"})
        self.assertEqual([p["ask"] for p in self.posts.ok], [triggers.BLOCKED_ASK["schoolwork"]])

    def test_blocked_texts_are_valid_fixed_news(self):
        feed = Feed(self.tmp / "feed")
        self.assertEqual(set(triggers.BLOCKED_ASK), set(triggers.SOURCES))
        for source, ask in triggers.BLOCKED_ASK.items():
            self.assertEqual(feed.post({"kind": "news", "rank": 4, "ask": ask, "idempotency_key": f"k-{source}"})["ask"], ask)


class Wiring(unittest.TestCase):
    def test_job_after_the_ingesters_and_agentd_starts_light(self):
        made = jobs.default_jobs()
        names = [job.name for job in made]
        self.assertGreater(names.index("triggers"), names.index("github"))
        self.assertEqual(made[names.index("triggers")].every_s, 300)
        code = ("import sys\nfrom jarvis.v3 import jobs\njobs.default_jobs()\n"
                "print(sorted(m for m in sys.modules if m in {'jarvis.v3.triggers', 'jarvis.v3.signals', 'jarvis.v3.commitments', 'markdown_it'}))")
        with tempfile.TemporaryDirectory() as home:
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                                 env={"PYTHONPATH": str(REPO), "HOME": home, "PATH": os.environ.get("PATH", "")})
        self.assertEqual((out.returncode, out.stdout.split()), (0, ["[]"]), out.stderr[-400:])


if __name__ == "__main__":
    unittest.main()
