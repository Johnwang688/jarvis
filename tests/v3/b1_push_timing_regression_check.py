from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from jarvis.v3 import feed as feed_module
from jarvis.v3.feed import Feed


class PushTimingCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))

    def test_default_clock_routes_due_today_across_spring_dst(self):
        class BeforeSpringChange(datetime):
            @classmethod
            def now(cls, tz=None):
                instant = cls(2031, 3, 9, 5, tzinfo=UTC)
                return instant.astimezone().replace(tzinfo=None) if tz is None else instant.astimezone(tz)

        try:
            with patch.dict(os.environ, {"TZ": "America/New_York"}):
                time.tzset()
                with patch.object(feed_module, "datetime", BeforeSpringChange):
                    row = Feed(self.root).post({
                        "kind": "news", "rank": 2, "ask": "Build finished.",
                        "due": "2031-03-10T04:30:00Z",
                    })
        finally:
            time.tzset()
        # The deadline is 00:30 Monday after the offset changes to UTC-04.
        self.assertEqual(row["route"], "batch")

    def test_post_crossing_into_quiet_hours_defers_its_push(self):
        start = datetime(2031, 1, 10, 23, tzinfo=UTC)
        end = start + timedelta(hours=8)
        now = start - timedelta(seconds=1)
        calls = []

        class Quiet:
            def is_quiet(self, instant):
                return start <= instant < end

            def ends_at(self, instant):
                return end if self.is_quiet(instant) else None

        def push(count, rank):
            calls.append((now, count, rank))

        feed = Feed(self.root, clock=lambda: now, quiet=Quiet(), push=push)
        feed.briefs()
        fsync = os.fsync

        def finish_after_quiet_starts(fd):
            nonlocal now
            if stat.S_ISREG(os.fstat(fd).st_mode):
                now = start
            fsync(fd)

        with patch.object(os, "fsync", side_effect=finish_after_quiet_starts):
            feed.post({"kind": "news", "rank": 2, "ask": "Build finished.", "time_critical": True})
        now = end
        reopened = Feed(self.root, clock=lambda: now, quiet=Quiet(), push=push)
        reopened.tick()
        feed.tick()
        self.assertEqual(calls, [(end, 1, 2)])


if __name__ == "__main__":
    unittest.main()
