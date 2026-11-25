"""HTTP regressions for B1b storage failures and slow reads."""

from contextlib import closing
import errno
import fcntl
from http.client import HTTPConnection
import os
import unittest
from unittest.mock import patch

from b1b_acceptance_check import Base
from jarvis.v3.feed import Feed, FeedError


FEED = "/v3/owner/feed"


class FeedReview(Base):
    def test_a_refused_answer_leaves_the_brief_open(self):
        brief = self.post()
        directory = self.feed.root.stat()
        fsync = os.fsync

        def fail_feed_directory_sync(fd):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == (directory.st_dev, directory.st_ino):
                raise OSError(errno.EIO, "fixture directory sync failure")
            return fsync(fd)

        with patch("jarvis.v3.feed.os.fsync", side_effect=fail_feed_directory_sync):
            status, reply = self.call("POST", FEED + "/answer", {
                "id": brief["id"], "action": "approve",
            })

        stored = Feed(self.feed.root).get(brief["id"])
        self.assertEqual(status, 200)
        self.assertEqual(stored["state"], "approved")
        self.assertEqual(reply["brief"], stored)

    def test_snapshot_has_a_deadline_when_a_writer_holds_the_lock(self):
        self.post()
        directory = os.open(self.feed.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(directory, fcntl.LOCK_EX)
            with closing(HTTPConnection("127.0.0.1", self.d.port, timeout=6)) as conn:
                conn.request("GET", FEED, headers={"Sec-Fetch-Site": "same-origin"})
                self.assertEqual(self.call("GET", "/v3/status")[0], 200)
                try:
                    response = conn.getresponse()
                    response.read()
                except TimeoutError:
                    self.fail("Feed read outlasted the daemon's five-second request timeout")
        finally:
            os.close(directory)

    def test_storage_exception_details_stay_out_of_error_responses(self):
        brief = self.post()
        detail = f"{brief['ask']}\nCould not read {self.feed.root / 'briefs.jsonl'}"
        with patch.object(Feed, "snapshot", side_effect=FeedError(detail)):
            status, reply = self.call("GET", FEED)
        self.assertGreaterEqual(status, 400)
        self.assertEqual(reply.keys(), {"error"})
        self.assertNotIn(brief["ask"], reply["error"])
        self.assertNotIn(str(self.feed.root), reply["error"])
        self.assertNotIn("\n", reply["error"])
        self.assertEqual(self.call("GET", "/v3/status")[0], 200)


if __name__ == "__main__":
    unittest.main()
