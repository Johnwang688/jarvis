"""Orchestrator-owned acceptance tests for L3 (implementers: do not edit; if one is wrong, stop and say why).

Contract: once a day, plain code (no model, no budget) tells the owner what Jarvis learned on its own, in one "digest" brief (contract 1.7).
  jarvis/v3/learning_digest.py      imports neither claude_jobs, budget, reviewer nor daily_briefs; default paths resolve from config.V3_DIR at call time
    DIGEST_AT = "20:30" (before B4's evening wrap-up); MAX_LINES = 12; RANK = 3 (>= 3: batched, never pushed); zone America/Chicago
    due(now, *, last_run, at=DIGEST_AT) -> bool    local time in [at, 23:00) and (last_run is None or on an earlier local day).
                                      at must be "HH:MM" with 07:00 <= at < 23:00, else ValueError.
    run(*, feed, now=None, state_dir=None, store=None, vault=None, reviewer_dir=None) -> Result(outcome, brief_id=None, items=0, lines=0)
      Defaults: state_dir config.V3_DIR/"learning-digest" (0700, files 0600), store SkillStore(), vault Vault(), reviewer_dir config.V3_DIR/"reviewer".
      Sources, each read from a saved position so nothing is reported twice or skipped across restarts (all positions live in state_dir/state.json):
        skill ledger    store's ledger rows after the last row id seen (SkillStore.records order); status "applied" or "blocked" are
                        reported, "unapplied" are skipped; rows with actor "curator" are skipped here (the curator line reports them).
                        A saved id missing from the ledger: nothing from the ledger this run, the position moves to its end.
        curator         curator.jsonl in the store's ledger folder, byte offset of complete lines ({"ts","skill","change","idle_days"}).
        memory          the set of active vault refs (Vault.entries(); an unreadable entry still counts as present): added = new refs,
                        archived = refs gone since; and vault/.flags.jsonl ({"name","rule"}) by byte offset of complete lines.
        reviewer spend  reviewer_dir/log.jsonl (L2 rows) by byte offset of complete lines: runs = outcome ok|error, tokens summed,
                        skipped = skipped-budget|skipped-usage. A file shorter than its saved offset restarts at its end, reporting nothing from it.
      Reading never creates a store, vault or reviewer folder; a missing one has no rows. Only owner-owned regular files, never through a link.
      Item lines, in this order (refs exact; labels <= 120, one line, built ONLY from names, ops, actors, counts, rule names and ids:
      never skill text, memory text, sources, sessions or transcript text; names go through redact.for_display and are cut to fit):
        1 tainted applied skill rows      "Tainted: <name> ..."                    ref "skill-ledger:<id>"
        2 quarantined (status blocked)    "Quarantined: <name> ... (<rules>)"      ref "skill-ledger:<id>"   (the .pending items; rule names from the row's scan)
        3 other applied skill rows        "Skill: <name> ..."; actor owner: "Owner: ..." (op rollback says "rolled back")   ref "skill-ledger:<id>"
        4 memory: adds sorted by ref "Memory added: <ref>", then archives sorted "Memory archived: <ref>", then flags in file order
          "Memory flagged: <ref> (<rule>)"                                         ref "memory:<folder>/<name>"
        5 curator lines                   "Curator: <name> <change> ..."           ref "skill:<name>"
      then at most one spend line "Reviewer: <runs> runs, <in> in / <out> out tokens, <skipped> skipped" (plain decimal digits), ref "jarvis:reviewer".
      Cap: when items + spend exceed MAX_LINES, show the first MAX_LINES - 1 - spend items, then {"label": "+N more", "ref": "jarvis:learning"}, then spend.
      Brief: feed.post({"kind": "digest", "rank": RANK, "ask": "Learning digest · <items> item(s)", "why_now": <counts only, <= 200>,
      "context": <lines>}, actor="jarvis"): no options, no default, no proposal. items counts item lines before the cap (spend excluded).
      outcome: "quiet" (outside 07:00-23:00: nothing read or written) | "already" (this local day was claimed) | "baseline" (no state, or a
      corrupt state.json, which is renamed aside as state.corrupt-<hex>.json: positions are set to each source's end, the day is claimed,
      nothing posted) | "empty" (no item: the day is claimed, positions do NOT move, so spend accumulates into the next digest, no brief)
      | "posted" | "error". The claim and the new positions are written in ONE durable state write BEFORE feed.post (the brief id may be
      saved after a successful post, for summary_line): a crash or a failed post loses that digest, never doubles it. A nonblocking lock on
      state_dir: a second run at once returns "already". Each run appends one row to state_dir/log.jsonl, exactly {"ts","day","outcome","items","lines"}.
    summary_line(*, now=None, state_dir=None) -> {"label", "ref"} | None    for B4: when today's digest was posted, {"label": "Learning
      digest: <counts>" (<= 120, no names), "ref": "brief:<id>"}; else None.
  jarvis/v3/daily_briefs.py   assemble(..., learning=None) and run(..., learning=None): learning() -> line | None, evening only, placed right
    after the activity line; raising -> note {"label": "Learning unavailable", "ref": "jarvis:learning"}. Morning never calls it.
  jarvis/v3/owner_routes.py   owner-only (A6a rules), on the store under the daemon's data dir (<data_dir>/skills, <data_dir>/skill-ledger,
    consumer links and owner skills as SkillStore() has them), each calling the store as the owner:
      POST /v3/owner/learned/rollback {"id"}        SkillStore.rollback(id). A repeat after this id was already rolled back (the newest applied
                                                    row for that skill and path is an owner rollback whose before/after are the id's after/before)
                                                    -> 200 {"changed": false}.
      POST /v3/owner/learned/pin  {"id", "on": bool}   pin / lock the live skill named by that ledger row; already so -> {"changed": false}
      POST /v3/owner/learned/lock {"id", "on": bool}
      200 {"changed": true|false}; wrong body 400; an id that is malformed or not in the ledger 404; a store refusal (live skill differs,
      not live, quarantined) 409. No route reads the digest or the ledger: the digest lives only in the owner's feed (agents post, never read).
  jarvis/v3/jobs.py default_jobs(): "learning-digest" before "evening-wrap": due = learning_digest.due(now, last_run=..., at=<earlier of DIGEST_AT
    and B4's configured evening>); run = learning_digest.run(feed=<the feed job's Feed>); "error" raises. "evening-wrap" passes
    learning=<a callable over summary_line>. Imports stay lazy (agentd starts light). The hud3 buttons are a later PR.
"""
import http.client
import inspect
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from jarvis import config
from jarvis.v3 import daemon, daily_briefs, jobs, learning_digest, owner_routes
from jarvis.v3.feed import Feed
from jarvis.v3.skill_curator import Curator
from jarvis.v3.skill_store import SkillStore
from jarvis.v3.tools.skills import SkillTools
from jarvis.v3.vault import Vault

REPO = Path(__file__).resolve().parents[2]
CHICAGO = ZoneInfo("America/Chicago")
DAY = 86400
BODY = "# Notes\n\nPRIVATE BODY MARKER: run the steps in order.\n"
DESC = "SECRET DESCRIPTION WORDS for the skill."
PRIVATE = ("SECRET DESCRIPTION", "PRIVATE BODY", "PRIVATE MEMORY", "sess-marker", "Sneaky")
P = "/v3/owner/learned/"


def at(day, hour, minute=0):
    return datetime(2030, 1, day, hour, minute, tzinfo=CHICAGO).timestamp()


class SpyFeed(Feed):
    def __init__(self, *a, crash=False, **kw):
        super().__init__(*a, **kw)
        self.crash, self.answers = crash, []

    def post(self, fields, **kw):
        if self.crash:
            raise RuntimeError("the process died here")
        return super().post(fields, **kw)

    def answer(self, *a, **kw):
        self.answers.append(a)
        return super().answer(*a, **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.v3 = self.tmp / "v3"
        self.v3.mkdir(mode=0o700)
        (self.tmp / "home").mkdir()
        self.enterContext(patch.object(config, "V3_DIR", self.v3))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.tmp / "home")}))
        for key in ("JARVIS_LEARNED_SKILLS", "JARVIS_VAULT", "JARVIS_BRIEF_MORNING", "JARVIS_BRIEF_EVENING"):
            os.environ.pop(key, None)
        links = [self.tmp / "links" / "claude"]
        links[0].mkdir(parents=True)
        self.bind(SkillStore(self.v3 / "skills", ledger=self.v3 / "skill-ledger", links=links, owner_roots=[]))
        self.vault, self.reviewer_dir, self.state = Vault(self.v3 / "vault"), self.v3 / "reviewer", self.v3 / "learning-digest"
        self.now = at(15, 20, 45)
        self.feed = self.make_feed()

    def bind(self, store):
        self.store = store
        self.agent = SkillTools(store, session="codex:sess-marker-2")
        self.tainted = SkillTools(store, session="claude:sess-marker-1", tainted=True)

    def make_feed(self, **kw):
        return SpyFeed(self.v3 / "feed", clock=lambda: datetime.fromtimestamp(self.now, CHICAGO), **kw)

    def digest(self, feed=None):
        return learning_digest.run(feed=feed or self.feed, now=self.now, state_dir=self.state, store=self.store,
                                   vault=self.vault, reviewer_dir=self.reviewer_dir)

    def create(self, tools, name):
        text = tools.skill_manage("create", name, description=DESC, body=BODY)
        self.assertFalse(text.startswith("Error:"), text)

    def ledger_id(self, name, op="create"):
        return [row["id"] for row in self.store.records(with_status=True) if row["skill"] == name and row["op"] == op][-1]

    def append(self, path, row):
        path.parent.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as stream:
            stream.write(json.dumps(row) + "\n")

    def reviewer_row(self, outcome, tokens_in, tokens_out):
        self.append(self.reviewer_dir / "log.jsonl", {"ts": self.now, "kind": "skill", "provider": "claude", "session": "claude:sess-marker-1",
                                                       "outcome": outcome, "tainted": True, "input_tokens": tokens_in, "output_tokens": tokens_out})

    def digests(self):
        return [b for b in self.feed.briefs("open") if b["kind"] == "digest"]

    def lines(self, out):
        self.assertEqual(out.outcome, "posted")
        return self.feed.get(out.brief_id)["context"]


class Digest(Base):
    def test_end_to_end(self):
        self.create(self.agent, "old-habit")
        self.vault.add("people", "pat-example", "PRIVATE MEMORY DESCRIPTION", "PRIVATE MEMORY BODY about Pat.", "owner")
        self.assertEqual((self.digest().outcome, self.digests()), ("baseline", []))
        Curator(self.store).run(now=time.time() + 31 * DAY)          # archives old-habit (a curator ledger row and a curator.jsonl line)
        self.create(self.tainted, "tidy-notes")
        self.create(self.agent, "sort-imports")
        self.assertIn("quarantined", self.agent.skill_manage("create", "plan-trips", description="Plan trips.", body="# Trips\n\nSneaky​step.\n"))
        self.vault.add("facts", "likes-tea", "PRIVATE MEMORY DESCRIPTION", "PRIVATE MEMORY BODY about tea.", "reviewer:claude:sess-marker-1")
        self.vault.archive("people/pat-example")
        self.append(self.vault.root / ".flags.jsonl", {"name": "facts/likes-tea", "rule": "imperative"})
        self.reviewer_row("ok", 1000, 200)
        self.reviewer_row("skipped-usage", 0, 0)
        self.now = at(16, 20, 45)
        out = self.digest()
        brief = self.feed.get(out.brief_id)
        tidy, plan, sort = (self.ledger_id(n) for n in ("tidy-notes", "plan-trips", "sort-imports"))
        curator_row = self.ledger_id("old-habit", op="archive")
        refs = [line["ref"] for line in brief["context"]]
        self.assertEqual(refs, [f"skill-ledger:{tidy}", f"skill-ledger:{plan}", f"skill-ledger:{sort}", "memory:facts/likes-tea",
                                "memory:people/pat-example", "memory:facts/likes-tea", "skill:old-habit", "jarvis:reviewer"])
        self.assertNotIn(f"skill-ledger:{curator_row}", refs)
        labels = [line["label"] for line in brief["context"]]
        for label, prefix, words in zip(labels, ("Tainted: ", "Quarantined: ", "Skill: ", "Memory added: ", "Memory archived: ", "Memory flagged: ", "Curator: ", "Reviewer: "),
                                        (["tidy-notes"], ["plan-trips", "invisible-unicode"], ["sort-imports"], ["facts/likes-tea"], ["people/pat-example"],
                                         ["facts/likes-tea", "imperative"], ["old-habit", "archived"], ["1000", "200"])):
            self.assertTrue(label.startswith(prefix) and len(label) <= 120 and "\n" not in label, label)
            for word in words:
                self.assertIn(word, label)
        self.assertEqual((out.items, brief["kind"], brief["actor"], brief["route"], brief["options"], brief["default"], brief["proposal_id"]),
                         (7, "digest", "jarvis", "batch", [], None, None))
        self.assertGreaterEqual(brief["rank"], 3)
        self.assertTrue(brief["ask"].startswith("Learning digest") and "7 items" in brief["ask"] and len(brief["ask"]) <= 120)
        self.assertTrue(0 < len(brief["why_now"]) <= 200)
        self.assertEqual(self.digest().outcome, "already")
        self.assertEqual((len(self.digests()), self.feed.answers), (1, []))
        rows = [json.loads(line) for line in (self.state / "log.jsonl").read_text().splitlines()]
        self.assertEqual([set(r) for r in rows], [{"ts", "day", "outcome", "items", "lines"}] * 3)
        self.assertEqual([r["outcome"] for r in rows], ["baseline", "posted", "already"])
        text = json.dumps(brief) + "".join(p.read_text() for p in self.state.iterdir() if p.is_file())
        for private in PRIVATE:
            self.assertNotIn(private, text)
        for p in (self.state, *self.state.iterdir()):
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o700 if p.is_dir() else 0o600, p)

    def test_cap_keeps_tainted_first_and_counts_the_rest(self):
        self.assertEqual(self.digest().outcome, "baseline")
        words = ("apple", "berry", "cedar", "delta", "ember", "fable", "grove", "harbor", "island", "jasper", "koala", "lemon", "maple", "nectar")
        for word in words:
            self.create(self.agent, f"topic-{word}")
        self.create(self.tainted, "topic-zebra")
        self.reviewer_row("ok", 10, 5)
        self.now = at(16, 20, 45)
        out = self.digest()
        lines = self.lines(out)
        self.assertEqual(len(lines), learning_digest.MAX_LINES)
        self.assertEqual(lines[0]["ref"], f"skill-ledger:{self.ledger_id('topic-zebra')}")
        self.assertTrue(lines[0]["label"].startswith("Tainted: "))
        self.assertEqual([line["ref"] for line in lines[1:10]], [f"skill-ledger:{self.ledger_id('topic-' + w)}" for w in words[:9]])
        self.assertEqual(lines[10], {"label": "+5 more", "ref": "jarvis:learning"})
        self.assertEqual(lines[11]["ref"], "jarvis:reviewer")
        self.assertEqual(out.items, 15)
        self.assertIn("15", self.feed.get(out.brief_id)["ask"])

    def test_empty_day_posts_nothing_and_spend_waits(self):
        self.assertEqual(self.digest().outcome, "baseline")
        self.reviewer_row("ok", 300, 40)
        self.now = at(16, 20, 45)
        self.assertEqual(self.digest().outcome, "empty")                 # spend alone is not news
        self.assertEqual((self.digest().outcome, self.digests()), ("already", []))
        self.create(self.agent, "sort-imports")
        self.now = at(17, 20, 45)
        lines = self.lines(self.digest())
        self.assertEqual([line["ref"] for line in lines], [f"skill-ledger:{self.ledger_id('sort-imports')}", "jarvis:reviewer"])
        self.assertIn("300", lines[1]["label"])

    def test_crash_quiet_hours_and_corrupt_state(self):
        self.assertEqual(self.digest().outcome, "baseline")
        self.assertFalse((self.v3 / "skills").exists() or (self.v3 / "vault").exists() or self.reviewer_dir.exists())
        self.create(self.agent, "sort-imports")
        for day, hour, minute in ((16, 23, 30), (17, 6, 59)):
            self.now = at(day, hour, minute)
            self.assertEqual(self.digest().outcome, "quiet")
        self.now = at(17, 20, 45)
        self.assertEqual(self.digest(feed=self.make_feed(crash=True)).outcome, "error")
        self.now = at(17, 21, 30)
        self.assertEqual(self.digest(feed=self.make_feed()).outcome, "already")
        self.now = at(18, 20, 45)
        self.assertEqual(self.digest().outcome, "empty")                 # the lost digest is never sent twice
        self.assertEqual(self.digests(), [])
        (self.state / "state.json").write_text("{not json")
        self.create(self.agent, "tidy-notes")
        self.now = at(19, 20, 45)
        self.assertEqual(self.digest().outcome, "baseline")
        self.assertTrue(any(p.name.startswith("state.corrupt-") for p in self.state.iterdir()))
        self.now = at(20, 20, 45)
        self.assertEqual((self.digest().outcome, self.digests()), ("empty", []))


class Schedule(unittest.TestCase):
    def test_due(self):
        due = lambda now, last=None, **kw: learning_digest.due(now, last_run=last, **kw)
        self.assertFalse(due(at(15, 20, 29)))
        self.assertTrue(due(at(15, 20, 30)) and due(at(15, 22, 59), at(14, 20, 31)))
        self.assertFalse(due(at(15, 22, 0), at(15, 20, 31)) or due(at(15, 23, 0)) or due(at(16, 1, 0), at(14, 20, 31)))
        self.assertTrue(due(at(15, 20, 0), at="20:00"))
        for bad in ("06:30", "23:00", "8:00", "noon"):
            with self.assertRaises(ValueError):
                due(at(15, 12), at=bad)
        self.assertEqual((learning_digest.DIGEST_AT, learning_digest.MAX_LINES), ("20:30", 12))
        self.assertGreaterEqual(learning_digest.RANK, 3)


class Evening(Base):
    def test_summary_line_reaches_the_wrap_up(self):
        self.assertEqual(self.digest().outcome, "baseline")
        self.create(self.tainted, "tidy-notes")
        self.assertIsNone(learning_digest.summary_line(now=at(16, 21, 5), state_dir=self.state))
        self.now = at(16, 20, 45)
        out = self.digest()
        line = learning_digest.summary_line(now=at(16, 21, 5), state_dir=self.state)
        self.assertEqual((set(line), line["ref"]), ({"label", "ref"}, f"brief:{out.brief_id}"))
        self.assertTrue(line["label"].startswith("Learning digest") and len(line["label"]) <= 120 and "tidy-notes" not in line["label"])
        self.assertIsNone(learning_digest.summary_line(now=at(17, 21, 5), state_dir=self.state))
        calls = []
        evening = daily_briefs.assemble("evening", now=at(16, 21, 5), feed=self.feed, activity=lambda s, e: {"docs consolidated": 3},
                                        learning=lambda: (calls.append("evening"), line)[1])
        self.assertEqual([x["ref"] for x in evening.lines[:2]], ["jarvis:activity", line["ref"]])
        self.assertEqual(evening.lines[1]["label"], line["label"])
        daily_briefs.assemble("morning", now=at(16, 8), feed=self.feed, learning=lambda: calls.append("morning"))
        self.assertEqual(calls, ["evening"])
        def broken():
            raise OSError("state is unreadable")
        self.assertIn({"label": "Learning unavailable", "ref": "jarvis:learning"},
                      daily_briefs.assemble("evening", now=at(16, 21, 5), feed=self.feed, learning=broken).lines)
        self.assertIsNone(inspect.signature(daily_briefs.run).parameters["learning"].default)


class Routes(Base):
    def setUp(self):
        super().setUp()
        self.d = daemon.Daemon(port=0, data_dir=self.v3, hud_dir=self.tmp / "hud3", jobs=[])
        self.d.start()
        self.addCleanup(self.d.stop)
        self.host = f"127.0.0.1:{self.d.port}"
        self.bind(SkillStore(self.v3 / "skills", ledger=self.v3 / "skill-ledger"))     # the daemon's store; links under the temporary HOME

    def call(self, path, body, owner=True):
        data = json.dumps(body).encode()
        headers = {"Host": self.host, "Content-Type": "application/json", "Content-Length": str(len(data))}
        if owner:
            headers["Origin"] = f"http://{self.host}"
        conn = http.client.HTTPConnection("127.0.0.1", self.d.port, timeout=10)
        conn.request("POST", P + path, body=data, headers=headers)
        resp = conn.getresponse()
        text = resp.read()
        conn.close()
        return resp.status, (json.loads(text) if text else None)

    def test_owner_rolls_back_pins_and_locks_and_the_next_digest_says_so(self):
        self.create(self.agent, "tidy-notes")
        self.agent.skill_manage("create", "plan-trips", description="Plan trips.", body="# Trips\n\nSneaky​step.\n")
        create_id, blocked_id = self.ledger_id("tidy-notes"), self.ledger_id("plan-trips")
        self.assertEqual(self.digest().outcome, "baseline")
        n = len(self.store.records())
        self.assertEqual(self.call("rollback", {"id": create_id}, owner=False)[0], 403)
        for path, body in (("rollback", {}), ("rollback", {"id": 5}), ("rollback", {"id": create_id, "x": 1}), ("pin", {"id": create_id}),
                           ("pin", {"id": create_id, "on": "yes"}), ("lock", {"id": create_id, "on": 1})):
            self.assertEqual(self.call(path, body)[0], 400, (path, body))
        for path, body in (("rollback", {"id": "0" * 32}), ("rollback", {"id": "nope"}), ("pin", {"id": "f" * 32, "on": True})):
            self.assertEqual(self.call(path, body)[0], 404, (path, body))
        self.assertEqual(len(self.store.records()), n)
        self.assertEqual(self.call("pin", {"id": create_id, "on": True}), (200, {"changed": True}))
        self.assertEqual(self.call("pin", {"id": create_id, "on": True}), (200, {"changed": False}))
        self.assertEqual(self.call("lock", {"id": create_id, "on": True}), (200, {"changed": True}))
        lock_id = self.store.records()[-1]["id"]
        self.assertEqual(self.call("rollback", {"id": lock_id}), (200, {"changed": True}))
        self.assertEqual(self.call("rollback", {"id": lock_id}), (200, {"changed": False}))      # a repeated tap
        text = (self.v3 / "skills" / "tidy-notes" / "SKILL.md").read_text()
        self.assertTrue("\npinned: true\n" in text and "\nlocked: false\n" in text)
        for path, body in (("rollback", {"id": create_id}), ("pin", {"id": blocked_id, "on": True}), ("rollback", {"id": blocked_id})):
            self.assertEqual(self.call(path, body)[0], 409, (path, body))
        rows = self.store.records()[n:]
        self.assertEqual([(r["op"], r["actor"]) for r in rows], [("patch", "owner"), ("patch", "owner"), ("rollback", "owner")])
        routes = {key for key in owner_routes.ROUTES if "/learned" in key[1]}
        self.assertEqual(routes, {("POST", P + name) for name in ("rollback", "pin", "lock")})
        self.assertTrue(all(owner_routes.ROUTES[key].owner for key in routes))
        self.now = at(16, 20, 45)
        lines = self.lines(self.digest())
        self.assertEqual([line["ref"] for line in lines], [f"skill-ledger:{r['id']}" for r in rows])
        self.assertTrue(all(line["label"].startswith("Owner: ") and "tidy-notes" in line["label"] for line in lines), lines)
        self.assertIn("rolled back", lines[2]["label"])


class Wiring(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.enterContext(patch.object(config, "V3_DIR", self.tmp / "v3"))
        self.enterContext(patch.dict(os.environ, {}))
        for key in ("JARVIS_BRIEF_MORNING", "JARVIS_BRIEF_EVENING"):
            os.environ.pop(key, None)
        self.list = jobs.default_jobs(feed_root=self.tmp / "feed", index_path=self.tmp / "index.sqlite", google_token=self.tmp / "google_token.json")
        self.jobs = {job.name: job for job in self.list}

    def test_digest_runs_before_the_wrap_up_and_feeds_it(self):
        names = [job.name for job in self.list]
        self.assertLess(names.index("learning-digest"), names.index("evening-wrap"))
        job = self.jobs["learning-digest"]
        self.assertEqual((job.every_s, job.needs_idle_s), (None, 0))
        seen = []
        with patch.object(learning_digest, "due", lambda now, *, last_run, at: (seen.append((now, last_run, at)), True)[1]):
            self.assertTrue(job.due(1000.0, 5.0))
            with patch.dict(os.environ, {"JARVIS_BRIEF_EVENING": "20:00"}):
                job.due(1000.0, None)
        self.assertEqual(seen, [(1000.0, 5.0, "20:30"), (1000.0, None, "20:00")])
        calls = []
        with patch.object(learning_digest, "run", lambda **kw: (calls.append(kw), learning_digest.Result("posted"))[1]):
            job.run()
        self.assertEqual((type(calls[0]["feed"]).__name__, calls[0]["feed"].root), ("Feed", self.tmp / "feed"))
        with patch.object(learning_digest, "run", lambda **kw: learning_digest.Result("error")), self.assertRaises(Exception):
            job.run()
        wraps = []
        with patch.object(daily_briefs, "run", lambda which, **kw: (wraps.append((which, kw)), daily_briefs.Result("posted"))[1]):
            self.jobs["evening-wrap"].run()
            self.jobs["morning-brief"].run()
        (_, evening), (_, morning) = wraps
        self.assertTrue(callable(evening["learning"]))
        self.assertIsNone(evening["learning"]())                          # no digest yet today
        self.assertIsNone(morning.get("learning"))

    def test_agentd_still_starts_light_and_the_digest_calls_no_model(self):
        code = ("import sys\nfrom jarvis.v3 import daemon, jobs\njobs.default_jobs()\n"
                "print(sorted(m for m in sys.modules if m in ('jarvis.v3.learning_digest', 'jarvis.v3.skill_store', 'jarvis.v3.vault')))\n"
                "import jarvis.v3.learning_digest\n"
                "print(sorted(m for m in sys.modules if m.rsplit('.', 1)[-1] in ('claude_jobs', 'budget', 'reviewer', 'daily_briefs')))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                             env={"PYTHONPATH": str(REPO), "HOME": str(self.tmp), "PATH": os.environ.get("PATH", "")})
        self.assertEqual((out.returncode, out.stdout.split()), (0, ["[]", "[]"]), out.stderr[-400:])


if __name__ == "__main__":
    unittest.main()
