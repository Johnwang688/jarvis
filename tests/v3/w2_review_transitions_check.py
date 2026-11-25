"""Exercise notification delivery through the real work ledger and feed."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

from jarvis import config
from jarvis.v3.feed import Feed
from jarvis.v3.t3_client import State
from jarvis.v3.work import Ledger
from jarvis.v3.work_watch import Watcher


class Delivery(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(root)}))
        self.enterContext(patch.object(config, "V3_DIR", root / "v3"))
        self.path = config.V3_DIR / "work" / "threads.jsonl"
        self.feed_root = config.V3_DIR / "feed"
        self.ledger = Ledger(self.path)

    def start(self, title="Example review", project="example"):
        self.ledger.record_start(thread_id="fake-thread", run_id="fake-run", project=project,
                                 title=title, branch=None, brief_sha256="0" * 64, at=1)

    def state(self, status, waiting=False):
        return State("fake-thread", status, waiting, int(waiting),
                     "fake title", "fake-provider", "fake-model", [])

    def watcher(self, status, *, waiting=False, at=2, ledger=None, show=None, clock=None):
        t3 = Mock(spec=["show"])
        t3.show.return_value = self.state(status, waiting)
        t3.show.side_effect = show
        return Watcher(ledger=ledger if ledger is not None else Ledger(self.path),
                       t3=t3, feed=Feed(self.feed_root), clock=clock or (lambda: at))

    def test_accepted_emoji_title_still_delivers_completion(self):
        prefix = '"مرحبا" ❤️ 👩\u200d💻 '
        title = prefix + "x" * (80 - len(prefix))
        self.start(title, project="p" * 32)
        for at in (2, 122):
            self.watcher("completed", at=at).run()
        briefs = Feed(self.feed_root).briefs()
        self.assertEqual(len(briefs), 1)
        self.assertEqual(self.ledger.get("fake-thread").status, "completed")
        self.assertEqual((briefs[0]["kind"], briefs[0]["rank"]), ("news", 4))

    def test_terminal_reply_for_another_thread_cannot_stop_watching(self):
        self.start()
        wrong_thread = replace(self.state("failed"), thread_id="fake-other-thread")
        self.watcher("failed", show=lambda *args, **kwargs: wrong_thread).run()
        self.watcher("completed", at=122).run()

        briefs = Feed(self.feed_root).briefs()
        self.assertEqual([brief["rank"] for brief in briefs], [4])
        self.assertEqual(self.ledger.get("fake-thread").status, "completed")



if __name__ == "__main__":
    unittest.main()
