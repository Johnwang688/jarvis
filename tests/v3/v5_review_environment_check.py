"""Check that Windows launch receives interop settings without daemon secrets."""

import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from jarvis.v3 import pc


class LauncherEnvironmentCheck(unittest.TestCase):
    def test_windows_launcher_does_not_inherit_daemon_credentials(self):
        interop = "/unused/interop"
        daemon_env = {"WSL_INTEROP": interop, "OPENROUTER_API_KEY": "fake-token"}
        with (patch.dict(os.environ, daemon_env, clear=True),
              patch.object(Path, "is_socket", return_value=True),
              patch.object(subprocess, "run") as run):
            pc.open_app(pc.app_preview("chrome"), lambda: None)

        run.assert_called_once()
        child_env = run.call_args.kwargs["env"]
        self.assertEqual(child_env["WSL_INTEROP"], interop)
        self.assertNotIn("OPENROUTER_API_KEY", child_env)


if __name__ == "__main__": unittest.main()
