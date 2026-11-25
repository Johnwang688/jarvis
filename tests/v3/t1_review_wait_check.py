"""T1 must recognize a terminal status before treating a thread as done."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jarvis.v3.t3_client import T3, T3Error


class WaitStatus(unittest.TestCase):
    def test_wait_refuses_an_unknown_status(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(home)}, clear=True))
        state = {"threadId": "t:1", "status": "resuming", "pendingRequestCount": 0,
                 "title": "Example", "providerInstanceId": "codex", "model": "example"}
        (home / "show.json").write_text(json.dumps({"thread": state, "messages": []}), encoding="utf-8")
        binary = home / "fake-cli"
        binary.write_text('#!/bin/sh\nexec /bin/cat "$HOME/show.json"\n', encoding="utf-8")
        binary.chmod(0o700)
        with self.assertRaises(T3Error) as caught:
            T3(binary).wait("t:1", timeout=1)
        self.assertIn(caught.exception.kind, {"shape", "timeout"})


if __name__ == "__main__":
    unittest.main()
