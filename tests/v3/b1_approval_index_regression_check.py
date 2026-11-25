from datetime import UTC, datetime
import errno
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jarvis.v3 import feed as feed_module
from jarvis.v3.feed import Feed, FeedError


class ApprovalIndexCheck(unittest.TestCase):
    def test_index_enospc_after_durable_approval_does_not_skip_execution(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        calls = []
        clock = lambda: datetime(2031, 1, 10, 14, tzinfo=UTC)
        feed = Feed(root, clock=clock, execute=calls.append)
        row = feed.post({"kind": "decision", "rank": 3, "ask": "Move the build?",
                         "default": {"action": "none", "at": None},
                         "proposal_id": "example-proposal"})

        # The append fits, but writing the replacement index needs more space.
        with patch.object(feed_module, "_replace_index", side_effect=OSError(errno.ENOSPC, "Disk full")):
            try:
                feed.answer(row["id"], "approve")
            except (FeedError, OSError):
                pass

        recovered = Feed(root, clock=clock, execute=calls.append)
        self.assertEqual(recovered.get(row["id"])["state"], "approved")
        with self.assertRaises(FeedError):
            recovered.answer(row["id"], "approve")
        self.assertEqual(calls, ["example-proposal"])


if __name__ == "__main__":
    unittest.main()
