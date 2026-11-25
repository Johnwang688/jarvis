import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.v3.jobs import Job, Scheduler


class IndexImportCheck(unittest.TestCase):
    def test_missing_sqlite_does_not_disable_a_job_without_index_dependencies(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        finished = threading.Event()
        outcomes = []

        def run():
            outcomes.append("ran")
            finished.set()

        def worker_failed(args):
            outcomes.append(args.exc_type.__name__)
            finished.set()

        scheduler = Scheduler([Job("independent", run, every_s=60)],
                              state=root / "jobs/state.json", tick_s=0.01)
        with (patch.dict(sys.modules, {"_sqlite3": None}),
              patch.object(threading, "excepthook", side_effect=worker_failed)):
            sys.modules.pop("sqlite3", None)
            sys.modules.pop("jarvis.v3.index", None)
            scheduler.start()
            try:
                self.assertTrue(finished.wait(5))
            finally:
                scheduler.stop()
        self.assertEqual(outcomes, ["ran"])


if __name__ == "__main__":
    unittest.main()
