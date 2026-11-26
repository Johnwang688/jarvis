"""Check that the new idless checkpoint describes a sent digest."""

from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

from jarvis.v3 import learning_digest


class LearningStateCheck(unittest.TestCase):
    def test_idless_zero_item_digest_is_rejected(self):
        state = Path(self.enterContext(TemporaryDirectory()))
        path = state / "state.json"
        day = "2030-01-02"
        now = datetime(2030, 1, 2, 21, tzinfo=ZoneInfo("America/Chicago")).timestamp()
        row = {"day": day, "ledger": None, "memory": [],
               "offsets": dict.fromkeys(("curator", "flags", "reviewer")),
               "items": 1, "lines": 1, "last_brief": {"day": day, "items": 1}}
        path.write_text(json.dumps(row))
        path.chmod(0o600)
        self.assertEqual(learning_digest.summary_line(now=now, state_dir=state),
                         {"label": "Learning digest: 1 item", "ref": "jarvis:learning"})
        row["last_brief"]["items"] = row["items"] = row["lines"] = 0
        path.write_text(json.dumps(row))
        self.assertIsNone(learning_digest.summary_line(now=now, state_dir=state))


if __name__ == "__main__":
    unittest.main()
