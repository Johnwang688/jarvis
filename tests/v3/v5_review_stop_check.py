"""Check that Stop can still cancel a launch during interop setup."""

import os
from pathlib import Path
import subprocess
from threading import Event
import unittest
from unittest.mock import patch

from jarvis.v3 import pc


class StopCheck(unittest.TestCase):
    def test_stop_during_interop_setup_prevents_launch(self):
        stopped = Event()

        def inspect_interop():
            stopped.set()
            return True

        def guard():
            return "Error: The chat turn is stopping." if stopped.is_set() else None

        with (patch.dict(os.environ, {}, clear=True),
              patch.object(pc, "WSL_INTEROP_SOCKET", Path("/unused/interop")),
              patch.object(Path, "is_socket", side_effect=inspect_interop),
              patch.object(subprocess, "run") as run):
            result = pc.open_app(pc.app_preview("chrome"), guard)

        run.assert_not_called()
        self.assertEqual(result, "Error: The chat turn is stopping.")


if __name__ == "__main__": unittest.main()
