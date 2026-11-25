"""Orchestrator-owned acceptance tests for B1b (implementers: do not edit; if one is wrong, stop and say why).

Contract: the owner's feed routes on the v3 daemon (A6a's rules apply unchanged: owner-only, JSON, bounded).
  The daemon serves the feed stored under its data dir (`<data_dir>/feed`, the same folder `Feed()` uses by default).
  GET  /v3/owner/feed            -> 200 {"briefs": [open briefs: rank, then oldest first], "recent": [up to 20 resolved, newest first]}
  POST /v3/owner/feed/answer     {"id", "action": approve|edit|other|not_now, "text"?} -> 200 {"brief": {...}}
  POST /v3/owner/feed/feedback   {"id", "tag"} -> 200 {"brief": {...}}
  Errors: an unknown id is 404; a refused answer (bad action, missing or extra text, already answered, unknown key, wrong type) is 400
  {"error": "<one line, never the brief's text>"}; nothing changes on an error. There is no route that posts, edits or deletes a brief.
  Approving a brief that carries a proposal does NOT execute anything yet (I4 does not exist): the answer is recorded and the response
  says {"executed": false}.
"""
import http.client
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jarvis.v3 import daemon, owner_routes
from jarvis.v3.feed import Feed


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.d = daemon.Daemon(port=0, data_dir=self.tmp / "v3", hud_dir=self.tmp / "hud3", jobs=[])
        self.d.start()
        self.addCleanup(self.d.stop)
        self.host = f"127.0.0.1:{self.d.port}"
        self.feed = Feed(self.tmp / "v3" / "feed")

    def call(self, method, path, body=None, owner=True):
        headers = {"Host": self.host}
        if owner:
            headers.update({"Origin": f"http://{self.host}"} if method == "POST" else {"Sec-Fetch-Site": "same-origin"})
        data = None
        if method == "POST":
            data = json.dumps(body).encode()
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(data))})
        conn = http.client.HTTPConnection("127.0.0.1", self.d.port, timeout=10)
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        text = resp.read()
        conn.close()
        return resp.status, (json.loads(text) if text else None)

    def post(self, ask="Move the dentist to Friday?", rank=4, **kw):
        due = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        return self.feed.post({"kind": "decision", "rank": rank, "ask": ask, "why_now": "It clashes with the lab.",
                               "options": [{"label": "Move it"}, {"label": "Keep it"}], "default": {"action": "none", "at": due}, **kw})


class Read(Base):
    def test_open_briefs_in_order_and_recent(self):
        status, body = self.call("GET", "/v3/owner/feed")
        self.assertEqual((status, body), (200, {"briefs": [], "recent": []}))
        low, high = self.post("Later one?", rank=6), self.post("Urgent one?", rank=3)
        done = self.post("Answered one?")
        self.feed.answer(done["id"], "not_now")
        status, body = self.call("GET", "/v3/owner/feed")
        self.assertEqual([b["id"] for b in body["briefs"]], [high["id"], low["id"]])
        self.assertEqual([(b["id"], b["state"]) for b in body["recent"]], [(done["id"], "dismissed")])
        self.assertEqual(body["briefs"][0]["ask"], "Urgent one?")

    def test_owner_only(self):
        self.post()
        self.assertEqual(self.call("GET", "/v3/owner/feed", owner=False)[0], 403)
        brief = self.feed.briefs()[0]
        self.assertEqual(self.call("POST", "/v3/owner/feed/answer", {"id": brief["id"], "action": "approve"}, owner=False)[0], 403)
        self.assertEqual(self.feed.get(brief["id"])["state"], "open")
        routes = {key for key in owner_routes.ROUTES if "/feed" in key[1]}
        self.assertEqual(routes, {("GET", "/v3/owner/feed"), ("POST", "/v3/owner/feed/answer"), ("POST", "/v3/owner/feed/feedback")})
        self.assertTrue(all(owner_routes.ROUTES[key].owner for key in routes))


class Write(Base):
    def test_each_answer(self):
        for action, state, text in (("approve", "approved", None), ("edit", "edited", "Move it to Monday"), ("other", "other", "Ask me tomorrow"), ("not_now", "dismissed", None)):
            brief = self.post()
            body = {"id": brief["id"], "action": action, **({"text": text} if text else {})}
            status, out = self.call("POST", "/v3/owner/feed/answer", body)
            self.assertEqual((status, out["brief"]["state"]), (200, state), action)
            self.assertEqual(Feed(self.tmp / "v3" / "feed").get(brief["id"])["state"], state)
        self.assertEqual(self.call("GET", "/v3/owner/feed")[1]["briefs"], [])

    def test_refusals_change_nothing(self):
        brief = self.post("A private question?")
        bad = [{"id": brief["id"], "action": "delete"}, {"id": brief["id"], "action": "edit"}, {"id": brief["id"], "action": "approve", "text": "x"},
               {"id": brief["id"], "action": "approve", "extra": 1}, {"id": brief["id"]}, {"action": "approve"}, {"id": 5, "action": "approve"},
               {"id": brief["id"], "action": ["approve"]}, {"id": brief["id"], "action": "other", "text": "x" * 20000}]
        for body in bad:
            status, out = self.call("POST", "/v3/owner/feed/answer", body)
            self.assertEqual(status, 400, body)
            self.assertNotIn("private question", json.dumps(out))
            self.assertNotIn("\n", out["error"])
        self.assertEqual(self.call("POST", "/v3/owner/feed/answer", {"id": "no-such-brief", "action": "approve"})[0], 404)
        self.assertEqual(self.feed.get(brief["id"])["state"], "open")
        self.assertEqual(self.call("POST", "/v3/owner/feed/answer", {"id": brief["id"], "action": "approve"})[0], 200)
        self.assertEqual(self.call("POST", "/v3/owner/feed/answer", {"id": brief["id"], "action": "not_now"})[0], 400)   # answered once
        self.assertEqual(self.feed.get(brief["id"])["state"], "approved")

    def test_feedback(self):
        brief = self.post()
        for tag in ("too_long", "too_long", "missing_context"):
            self.assertEqual(self.call("POST", "/v3/owner/feed/feedback", {"id": brief["id"], "tag": tag})[0], 200)
        self.assertEqual(sorted(self.feed.get(brief["id"])["feedback"]), ["missing_context", "too_long"])
        self.assertEqual(self.call("POST", "/v3/owner/feed/feedback", {"id": brief["id"], "tag": "run rm -rf"})[0], 400)
        self.assertEqual(self.call("POST", "/v3/owner/feed/feedback", {"id": "nope", "tag": "too_long"})[0], 404)
        self.assertEqual(self.feed.get(brief["id"])["state"], "open")

    def test_approving_a_proposal_executes_nothing_yet(self):
        brief = self.post(proposal_id="prop-1")
        status, out = self.call("POST", "/v3/owner/feed/answer", {"id": brief["id"], "action": "approve"})
        self.assertEqual((status, out["brief"]["state"], out.get("executed")), (200, "approved", False))

    def test_no_route_creates_or_deletes(self):
        for method, path in (("POST", "/v3/owner/feed"), ("POST", "/v3/owner/feed/post"), ("POST", "/v3/owner/feed/delete"), ("GET", "/v3/owner/feed/answer")):
            status, _ = self.call(method, path, {"kind": "news", "rank": 5, "ask": "x"} if method == "POST" else None)
            self.assertIn(status, (404, 405), (method, path))
        self.assertEqual(self.feed.briefs(), [])


if __name__ == "__main__":
    unittest.main()
