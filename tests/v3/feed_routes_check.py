"""Wire and storage regressions for the v3 daemon's owner feed."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v3.daemon import Daemon
from jarvis.v3.feed import Feed


FEED = "/v3/owner/feed"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FeedRoutes(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.data = self.root / "v3"
        self.feed = Feed(self.data / "feed", clock=lambda: NOW)
        self.daemon = self.start(self.data)

    def start(self, data):
        daemon = Daemon(port=0, data_dir=data, hud_dir=self.root / "hud", jobs=[])
        daemon.start()
        self.addCleanup(daemon.stop)
        return daemon

    def call(self, method="GET", path=FEED, *, daemon=None, body=None, owner=True):
        daemon = self.daemon if daemon is None else daemon
        headers = {}
        if owner:
            headers = ({"Origin": f"http://127.0.0.1:{daemon.port}"} if method == "POST"
                       else {"Sec-Fetch-Site": "same-origin"})
        if body is not None:
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        with closing(HTTPConnection(*daemon.address, timeout=5)) as conn:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.headers, response.read()

    def post(self, *, feed=None, ask="Check the fixture", rank=4, deadline=None):
        feed = self.feed if feed is None else feed
        fields = {"kind": "news", "rank": rank, "ask": ask}
        if deadline is not None:
            fields.update(kind="decision", default={"action": "none", "at": deadline.isoformat()})
        return feed.post(fields)

    def test_daemons_use_their_own_data_dirs(self):
        another = self.start(self.root / "another")
        first = self.post()
        second = self.post(feed=Feed(self.root / "another" / "feed"))
        for daemon, brief in ((self.daemon, first), (another, second)):
            status, _, raw = self.call(daemon=daemon)
            self.assertEqual((status, json.loads(raw)["briefs"]), (200, [brief]))
        status, _, _ = self.call("POST", FEED + "/answer", daemon=another,
                                 body={"id": first["id"], "action": "approve"})
        self.assertEqual(status, 404)
        self.assertEqual(self.feed.get(first["id"])["state"], "open")

    def test_missing_unreadable_and_linked_feed_folder(self):
        status, _, raw = self.call()
        self.assertEqual((status, json.loads(raw)), (200, {"briefs": [], "recent": []}))
        self.assertFalse(self.feed.root.exists())
        self.post()
        self.feed.root.chmod(0)
        try:
            # A privileged runner can still open mode-000 directories.
            if os.geteuid() == 0:
                with patch("jarvis.v3.feed.open_directory", side_effect=PermissionError("fake private path")):
                    unreadable = self.call()
            else:
                unreadable = self.call()
            self.assertEqual((unreadable[0], json.loads(unreadable[2])),
                             (500, {"error": "Internal Server Error"}))
        finally:
            self.feed.root.chmod(0o700)
        target = self.root / "linked-target"
        self.feed.root.rename(target)
        self.feed.root.symlink_to(target, target_is_directory=True)
        before = (target / "briefs.jsonl").read_bytes()
        status, _, raw = self.call()
        self.assertEqual((status, json.loads(raw)), (500, {"error": "Internal Server Error"}))
        self.assertEqual((target / "briefs.jsonl").read_bytes(), before)

    def test_answer_and_expiry_serialize_across_feed_instances(self):
        deadline = NOW + timedelta(hours=1)
        brief = self.post(deadline=deadline)
        due = self.post(deadline=deadline)
        ticking_feed = Feed(self.feed.root)
        answering, ticking, proceed = Event(), Event(), Event()
        record = Feed._record
        locked = Feed._locked

        def pause_answer(feed, directory, log, events):
            if events and events[0]["event"] == "answer":
                answering.set()
                if not proceed.wait(5):
                    raise TimeoutError("test did not release the answer")
            return record(feed, directory, log, events)

        def observe_tick(feed, **kwargs):
            if feed is ticking_feed:
                ticking.set()
            return locked(feed, **kwargs)

        body = {"id": brief["id"], "action": "approve"}
        with (patch.object(Feed, "_record", pause_answer), patch.object(Feed, "_locked", observe_tick),
              ThreadPoolExecutor(max_workers=2) as pool):
            reply = pool.submit(self.call, "POST", FEED + "/answer", body=body)
            try:
                self.assertTrue(answering.wait(5))
                tick = pool.submit(ticking_feed.tick, deadline + timedelta(seconds=1))
                self.assertTrue(ticking.wait(5))
            finally:
                proceed.set()
            status, _, raw = reply.result(timeout=5)
            self.assertEqual((status, json.loads(raw)["brief"]["state"]), (200, "approved"))
            self.assertEqual(tick.result(timeout=5)["defaulted"], [due["id"]])
        self.assertEqual(self.feed.get(brief["id"])["state"], "approved")
        self.assertEqual(self.feed.get(due["id"])["state"], "done")

    def test_large_history_returns_only_twenty_resolutions(self):
        older = self.post(ask="First post, last resolution")
        fixture = self.post()
        self.feed.answer(fixture["id"], "not_now")
        low, high = self.post(rank=6), self.post(rank=3)
        log = self.feed.root / "briefs.jsonl"
        _, post, answer, *_ = map(json.loads, log.read_bytes().splitlines())
        ids = [f"{i:032x}" for i in range(2048)]
        with log.open("ab") as stream:
            for bid in ids:
                for event in (post, answer):
                    stream.write((json.dumps({**event, "id": bid}) + "\n").encode())
        self.feed.answer(older["id"], "not_now")
        status, _, raw = self.call()
        self.assertEqual(status, 200)
        self.assertLess(len(raw), 16 * 1024)
        view = json.loads(raw)
        self.assertEqual([row["id"] for row in view["briefs"]], [high["id"], low["id"]])
        self.assertEqual([row["id"] for row in view["recent"]], [older["id"], *ids[:-20:-1]])

    def test_head_checks_owner_and_omits_body(self):
        self.post()
        status, _, raw = self.call()
        self.assertEqual(status, 200)
        status, headers, body = self.call("HEAD")
        self.assertEqual((status, headers["Content-Length"], body), (200, str(len(raw)), b""))
        self.assertEqual(self.call("HEAD", owner=False)[0::2], (403, b""))

    def test_edit_round_trips_unicode_and_refuses_null_text(self):
        brief = self.post()
        status, _, _ = self.call("POST", FEED + "/answer",
                                 body={"id": brief["id"], "action": "approve", "text": None})
        self.assertEqual(status, 400)
        self.assertEqual(self.feed.get(brief["id"])["state"], "open")
        text = "Café 東京 📚"
        status, _, raw = self.call("POST", FEED + "/answer",
                                   body={"id": brief["id"], "action": "edit", "text": text})
        stored = self.feed.get(brief["id"])
        self.assertEqual((status, json.loads(raw)["brief"]), (200, stored))
        self.assertEqual(stored["answer"], {"action": "edit", "text": text})

    def test_v3_listener_is_a_control_port(self):
        self.assertIn(config.V3_API_PORT, config.control_ports())


if __name__ == "__main__":
    unittest.main()
