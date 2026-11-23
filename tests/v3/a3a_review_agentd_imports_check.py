"""(A3a verifier r1, orchestrator-owned; the fixer must not edit.) The agentd CLI entry point must boot without importing the model stack."""

import runpy
import signal
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v3.daemon import Daemon


class AgentdImportsCheck(unittest.TestCase):
    def test_agentd_entry_point_does_not_load_models_or_providers(self):
        before = set(sys.modules)
        with TemporaryDirectory() as data, patch.object(
            config, "V3_DIR", data
        ), patch.object(config, "V3_API_PORT", 0), patch.object(
            sys, "argv", ["jarvis", "agentd"]
        ), patch.object(
            Daemon, "start", side_effect=lambda: signal.raise_signal(signal.SIGTERM)
        ) as start:
            with self.assertRaises(SystemExit) as result:
                runpy.run_module("jarvis", run_name="__main__")

        self.assertEqual(result.exception.code, 0)
        start.assert_called_once_with()
        forbidden = {"jarvis.agent", "jarvis.llm", "jarvis.models", "jarvis.v2.providers"}
        self.assertEqual((set(sys.modules) - before) & forbidden, set())


if __name__ == "__main__":
    unittest.main()
