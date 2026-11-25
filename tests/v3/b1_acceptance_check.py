"""Orchestrator-owned acceptance tests for B1 (implementers: do not edit; if one is wrong, stop and say why).

Contract (store, routing and the agents' tool; the daemon's owner routes come with A6a/B2)
  jarvis/v3/feed.py
    FeedError(ValueError)
    Feed(root=None, *, clock=None, quiet=None, push=None, execute=None)
      root default config.V3_DIR / "feed": briefs.jsonl (append-only events) + index.json; 0700/0600; no link is followed; one lock per operation
      clock() -> aware datetime (default: local now); quiet: .is_quiet(now) and .ends_at(now) (jarvis.v2.quiet.QuietHours' shape) or None
      push(count, rank): two ints and nothing else; execute(proposal_id): I4's entry, called at most once per brief
    post(fields, *, actor="jarvis") -> brief      contract 1.7; id, created, state, route are the store's, never the caller's
      validation (refused, never cut): kind decision|news|digest; rank 1..8; ask 1..120 chars, one line; why_now <= 200; <= 3 options;
      a decision needs default {"action", "at"}; due and default.at are ISO datetimes with a zone, or None; unknown keys are refused
      route "push" when rank <= 2 and (due is today on the clock's calendar, or time_critical is True), else "batch"
      a push brief posted in quiet hours is pushed by tick() once they end; otherwise at once. push failures never fail the post.
    get(id); briefs(state="open") (rank, then oldest first); batch() -> open briefs routed "batch" (B4 reads it)
    answer(id, action, *, text=None)   action approve|edit|other|not_now -> state approved|edited|other|dismissed; edit and other need text;
                                       only an open brief can be answered; approve with a proposal_id calls execute once
    feedback(id, tag)                  tag in FEEDBACK_TAGS; kept once each
    tick(now=None) -> {"pushed": n, "expired": [ids], "defaulted": [ids]}
      a decision past default.at: default.action == "none" -> state "done", resolved_by "default" (exactly once); anything else -> "expired"
  jarvis/v3/tools/feed.py
    FeedTools(feed, *, session)       brief_post(...) -> text, never raises, errors start with "Error:". It is the only public method:
      agents cannot read, list or answer briefs. Agent briefs: actor "agent", rank 3..8 only (an agent never reaches the phone),
      no proposal_id, at most 5 open per session and 20 a day; text is redacted for display.
"""
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jarvis.v3 import feed as feed_module
from jarvis.v3.feed import Feed, FeedError
from jarvis.v3.tools import feed as feed_tools
from jarvis.v3.tools.feed import FeedTools

T0 = datetime(2030, 1, 15, 14, 0, tzinfo=timezone.utc)
PLANTED = "ghp" + "_" + "B1k4" * 9


def iso(delta_h=0.0):
    return (T0 + timedelta(hours=delta_h)).isoformat()


class Quiet:
    def __init__(self):
        self.until = None

    def is_quiet(self, now=None):
        return self.until is not None and now < self.until

    def ends_at(self, now=None):
        return self.until if self.is_quiet(now) else None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.root = self.tmp / "v3" / "feed"
        self.now, self.pushes, self.ran, self.quiet = T0, [], [], Quiet()
        self.feed = self.open()

    def open(self):
        return Feed(self.root, clock=lambda: self.now, quiet=self.quiet, push=lambda *a, **k: self.pushes.append((a, k)),
                    execute=self.ran.append)

    def decision(self, **kw):
        return {"kind": "decision", "rank": 3, "ask": "Move the dentist to Friday?", "why_now": "It clashes with the lab.",
                "options": [{"label": "Move it"}, {"label": "Keep it"}], "default": {"action": "none", "at": iso(24)}, **kw}

    def news(self, **kw):
        return {"kind": "news", "rank": 6, "ask": "The build is green again.", **kw}


class Schema(Base):
    def test_a_valid_brief_is_stored(self):
        b = self.feed.post(self.decision(recommend={"label": "Move it", "because": "the lab is fixed"},
                                         context=[{"label": "Calendar", "ref": "calendar:abc"}]))
        self.assertEqual((b["state"], b["kind"], b["route"]), ("open", "decision", "batch"))
        self.assertTrue(b["id"] and b["created"])
        self.assertEqual(self.feed.get(b["id"])["ask"], "Move the dentist to Friday?")
        self.assertEqual(self.open().get(b["id"])["default"]["action"], "none")       # survives a restart
        for p in (self.root, *self.root.iterdir()):
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o700 if p.is_dir() else 0o600, p)

    def test_invalid_briefs_are_refused_whole(self):
        d = self.decision
        bad = [d(kind="alert"), d(rank=0), d(rank=9), d(rank="1"), d(rank=True), d(ask=""), d(ask="x" * 121), d(ask="two\nlines"),
               d(why_now="x" * 201), d(options=[{"label": str(i)} for i in range(4)]), d(options="Move it"), d(options=[{"label": ""}]),
               d(default=None), d(default={"action": "none"}), d(default={"action": "", "at": None}), d(default={"action": "none", "at": "tomorrow"}),
               d(default={"action": "none", "at": "2030-01-16T10:00:00"}), d(due="2030-01-16"), d(id="mine"), d(state="approved"),
               d(route="push"), d(created=iso()), d(extra=1), d(feedback=["too_long"]), d(ask=None), "not a dict", None]
        for fields in bad:
            with self.assertRaises(FeedError, msg=repr(fields)):
                self.feed.post(fields)
        no_default = self.decision()
        del no_default["default"]
        with self.assertRaises(FeedError):
            self.feed.post(no_default)
        self.assertEqual(self.feed.briefs(), [])
        self.feed.post(self.news())                                                   # news needs no default
        self.feed.post(self.decision(ask="x" * 120, why_now="y" * 200, options=[{"label": str(i)} for i in range(3)]))

    def test_order_is_rank_then_oldest(self):
        ids = []
        for rank in (6, 3, 6, 4):
            ids.append(self.feed.post(self.news(rank=rank))["id"])
            self.now += timedelta(seconds=1)
        self.assertEqual([b["id"] for b in self.feed.briefs()], [ids[1], ids[3], ids[0], ids[2]])

    def test_a_torn_log_line_and_a_linked_folder(self):
        b = self.feed.post(self.news())
        with open(self.root / "briefs.jsonl", "a") as f:
            f.write('{"id": "torn')
        fresh = self.open()
        self.assertEqual([x["id"] for x in fresh.briefs()], [b["id"]])
        fresh.post(self.news())
        self.assertEqual(len(self.open().briefs()), 2)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.tmp / "link").symlink_to(elsewhere)
        with self.assertRaises(Exception):
            Feed(self.tmp / "link", clock=lambda: self.now).post(self.news())
        self.assertEqual(list(elsewhere.iterdir()), [])


class Routing(Base):
    def test_table(self):
        late_today, tomorrow = (T0.replace(hour=23, minute=30)).isoformat(), iso(24)
        cases = [(1, late_today, None, "push"), (2, late_today, None, "push"), (3, late_today, None, "batch"), (1, tomorrow, None, "batch"),
                 (1, None, None, "batch"), (2, None, True, "push"), (2, tomorrow, True, "push"), (5, None, True, "batch"), (2, None, False, "batch")]
        for rank, due, critical, route in cases:
            fields = self.decision(rank=rank, due=due)
            if critical is not None:
                fields["time_critical"] = critical
            self.assertEqual(self.feed.post(fields)["route"], route, (rank, due, critical))
        self.assertEqual(len(self.pushes), 4)
        self.assertEqual(len(self.feed.batch()), 5)
        with self.assertRaises(FeedError):
            self.feed.post(self.decision(time_critical="yes"))

    def test_the_push_carries_a_count_and_a_rank_only(self):
        self.feed.post(self.decision(rank=2, time_critical=True, ask="Secret words " + PLANTED[:8]))
        self.feed.post(self.decision(rank=1, time_critical=True))
        self.assertEqual(self.pushes, [((1, 2), {}), ((2, 1), {})])
        for args, kwargs in self.pushes:
            self.assertTrue(all(type(a) is int for a in args))

    def test_quiet_hours_defer_the_push(self):
        self.quiet.until = T0 + timedelta(hours=9)
        a = self.feed.post(self.decision(rank=2, time_critical=True))
        self.feed.post(self.decision(rank=1, time_critical=True))
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.feed.tick()["pushed"], 0)
        self.feed.answer(a["id"], "not_now")                     # handled meanwhile: not pushed for
        self.now = T0 + timedelta(hours=9, minutes=1)
        self.assertEqual(self.open().tick()["pushed"], 1)        # one push for the night, after a restart too
        self.assertEqual(self.pushes, [((1, 1), {})])
        self.assertEqual(self.feed.tick()["pushed"], 0)
        self.assertEqual(len(self.pushes), 1)

    def test_a_failing_push_never_fails_the_post(self):
        def broken(count, rank):
            raise RuntimeError("no subscription")
        f = Feed(self.root, clock=lambda: self.now, push=broken)
        b = f.post(self.decision(rank=1, time_critical=True))
        self.assertEqual(f.get(b["id"])["state"], "open")
        Feed(self.tmp / "nopush", clock=lambda: self.now).post(self.decision(rank=1, time_critical=True))


class Actions(Base):
    def test_each_action(self):
        want = {"approve": "approved", "edit": "edited", "other": "other", "not_now": "dismissed"}
        for action, state in want.items():
            b = self.feed.post(self.decision())
            self.feed.answer(b["id"], action, text="Move it to Monday" if action in ("edit", "other") else None)
            got = self.open().get(b["id"])
            self.assertEqual(got["state"], state)
            if action in ("edit", "other"):
                self.assertEqual(got["answer"]["text"], "Move it to Monday")
        self.assertEqual(self.feed.briefs(), [])
        self.assertEqual(len(self.feed.briefs(state="approved")), 1)

    def test_bad_answers(self):
        b = self.feed.post(self.decision())
        for action, text in (("delete", None), ("edit", None), ("other", ""), ("edit", "x" * 5000), (None, None), ("approve", "extra words")):
            with self.assertRaises(FeedError, msg=action):
                self.feed.answer(b["id"], action, text=text)
        with self.assertRaises(FeedError):
            self.feed.answer("no-such-id", "approve")
        self.feed.answer(b["id"], "approve")
        for action in ("approve", "not_now"):
            with self.assertRaises(FeedError):
                self.feed.answer(b["id"], action)                # answered once
        self.assertEqual(self.feed.get(b["id"])["state"], "approved")

    def test_approve_runs_the_proposal_once(self):
        b = self.feed.post(self.decision(proposal_id="prop-1"))
        plain = self.feed.post(self.decision())
        self.feed.answer(plain["id"], "approve")
        self.assertEqual(self.ran, [])
        self.feed.answer(b["id"], "approve")
        with self.assertRaises(FeedError):
            self.feed.answer(b["id"], "approve")
        self.assertEqual(self.ran, ["prop-1"])
        c = self.feed.post(self.decision(proposal_id="prop-2"))
        self.feed.answer(c["id"], "not_now")
        self.assertEqual(self.ran, ["prop-1"])

    def test_feedback(self):
        b = self.feed.post(self.news())
        self.assertIn("too_long", feed_module.FEEDBACK_TAGS)
        self.assertIn("missing_context", feed_module.FEEDBACK_TAGS)
        for tag in ("too_long", "too_long", "missing_context"):
            self.feed.feedback(b["id"], tag)
        self.assertEqual(sorted(self.open().get(b["id"])["feedback"]), ["missing_context", "too_long"])
        with self.assertRaises(FeedError):
            self.feed.feedback(b["id"], "run rm -rf")
        self.assertEqual(self.feed.get(b["id"])["state"], "open")


class Expiry(Base):
    def test_a_no_op_default_runs_exactly_once(self):
        noop = self.feed.post(self.decision(default={"action": "none", "at": iso(2)}, proposal_id="prop-9"))
        real = self.feed.post(self.decision(default={"action": "send the reply", "at": iso(2)}))
        never = self.feed.post(self.decision(default={"action": "none", "at": None}))
        self.assertEqual(self.feed.tick(), {"pushed": 0, "expired": [], "defaulted": []})
        self.now = T0 + timedelta(hours=2, seconds=1)
        out = self.feed.tick()
        self.assertEqual((out["defaulted"], out["expired"]), ([noop["id"]], [real["id"]]))
        again = self.open().tick()
        self.assertEqual((again["defaulted"], again["expired"]), ([], []))
        got = self.feed.get(noop["id"])
        self.assertEqual((got["state"], got["resolved_by"]), ("done", "default"))
        self.assertEqual(self.feed.get(real["id"])["state"], "expired")
        self.assertEqual(self.feed.get(never["id"])["state"], "open")
        self.assertEqual(self.ran, [])                           # a default never runs a proposal
        for bid in (noop["id"], real["id"]):
            with self.assertRaises(FeedError):
                self.feed.answer(bid, "approve")

    def test_an_answered_decision_does_not_expire(self):
        b = self.feed.post(self.decision(default={"action": "none", "at": iso(1)}))
        self.feed.answer(b["id"], "approve")
        self.now = T0 + timedelta(hours=3)
        self.assertEqual(self.feed.tick()["defaulted"], [])
        self.assertEqual(self.feed.get(b["id"])["state"], "approved")


class Agents(Base):
    def setUp(self):
        super().setUp()
        self.t = FeedTools(self.feed, session="s1")

    def post(self, t=None, **kw):
        args = {"kind": "decision", "ask": "Rename the branch before the PR?", "why_now": "The PR opens today.",
                "options": ["Rename", "Keep"], "default_action": "none", "default_at": iso(24), **kw}
        return (t or self.t).brief_post(**args)

    def test_post_only(self):
        text = self.post()
        self.assertFalse(text.startswith("Error:"), text)
        b = self.feed.briefs()[0]
        self.assertEqual((b["actor"], b["session"], b["route"]), ("agent", "s1", "batch"))
        self.assertEqual([o["label"] for o in b["options"]], ["Rename", "Keep"])
        public = {n for n in dir(self.t) if not n.startswith("_")}
        self.assertEqual(public, {"brief_post"})
        source = Path(feed_tools.__file__).read_text()
        for word in (".answer(", ".briefs(", ".batch(", ".get(", ".feedback(", ".tick("):
            self.assertNotIn(word, source)
        self.assertNotIn(b["id"], text)                          # the id is the owner's handle, not the agent's

    def test_an_agent_never_reaches_the_phone_or_a_proposal(self):
        for kw in (dict(rank=1), dict(rank=2), dict(rank=0), dict(rank=9)):
            self.assertTrue(self.post(**kw).startswith("Error:"), kw)
        for kw in (dict(time_critical=True), dict(proposal_id="prop-1"), dict(state="approved"), dict(actor="jarvis"), dict(route="push")):
            try:
                text = self.post(**kw)
            except TypeError:
                continue                                         # not a parameter at all
            self.assertTrue(text.startswith("Error:"), kw)
        self.assertFalse(self.post(rank=3, due=T0.replace(hour=23).isoformat()).startswith("Error:"))
        self.assertEqual(self.pushes, [])
        self.assertEqual([b["route"] for b in self.feed.briefs()], ["batch"])

    def test_errors_are_text_and_caps_hold(self):
        for kw in (dict(ask="x" * 121), dict(kind="check"), dict(options=["a", "b", "c", "d"]), dict(default_action=""), dict(ask=None), dict(options="Rename")):
            self.assertTrue(self.post(**kw).startswith("Error:"), kw)
        for _ in range(5):
            self.assertFalse(self.post().startswith("Error:"))
        self.assertTrue(self.post().startswith("Error:"))        # 5 open per session
        other = FeedTools(self.feed, session="s2")
        self.assertFalse(self.post(other).startswith("Error:"))
        for b in self.feed.briefs():
            self.feed.answer(b["id"], "not_now")
        sessions = [FeedTools(self.feed, session=f"x{i}") for i in range(6)]
        results = [self.post(s) for s in sessions for _ in range(4)]
        self.assertEqual(sum(not r.startswith("Error:") for r in results), 14)   # 20 a day in all, 6 were used

    def test_text_is_redacted_and_one_line(self):
        self.assertFalse(self.post(ask="Use the token " + PLANTED + " here?").startswith("Error:"))
        raw = b"".join(p.read_bytes() for p in self.root.iterdir() if p.is_file())
        self.assertNotIn(PLANTED.encode(), raw)
        for ask in ("line one\nline two", "bell\x07", "hidden‮flip"):
            self.assertTrue(self.post(ask=ask).startswith("Error:"), repr(ask))


if __name__ == "__main__":
    unittest.main()
