"""VA-1 round-1 acceptance tests (orchestrator-owned; the implementer may not edit this file).

The verifier showed that name-only protection (`secrets.EXACT_NAMES`) has gaps
for a copy outside a closed folder (case variants, symlink aliases, recursive
reads). Those gaps are pre-existing for every EXACT_NAMES entry and are tracked
separately; VA-1 must simply never put the Windows key copy anywhere but a
closed Windows profile entry (a dot folder or AppData, `config.windows_closed_name`).

Run: PYTHONPATH=. .venv/bin/python tests/v2/voice_link_va1_r1_check.py
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jarvis import config, voice_link_setup  # noqa: E402


class ClosedFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.users = Path(self.tmp.name) / "Users"
        (self.users / "johnw").mkdir(parents=True)
        patcher = patch.dict(os.environ, {"JARVIS_WINDOWS_USERS": str(self.users)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_closed_profile_entries(self):
        closed = voice_link_setup.windows_folder_closed
        self.assertTrue(closed(self.users / "johnw" / ".jarvis-bridge"))
        self.assertTrue(closed(self.users / "johnw" / ".jarvis-bridge" / "deeper"))
        self.assertTrue(closed(self.users / "johnw" / "AppData" / "Local" / "Jarvis"))

    def test_open_places(self):
        closed = voice_link_setup.windows_folder_closed
        self.assertFalse(closed(self.users / "johnw" / "jarvis-bridge"))
        self.assertFalse(closed(self.users / "johnw" / "Documents" / ".jarvis"))   # closed entry must be the profile's own child
        self.assertFalse(closed(self.users / "johnw"))
        self.assertFalse(closed(self.users / "Public" / ".jarvis-bridge"))        # not a profile
        self.assertFalse(closed(self.users / "johnw" / ".." / "johnw2" / "x"))
        self.assertFalse(closed(Path(self.tmp.name) / "elsewhere" / ".jarvis-bridge"))

    def test_setup_refuses_an_open_bridge_folder_before_doing_anything(self):
        bridge = self.users / "johnw" / "jarvis-bridge"
        (bridge / "venv" / "Scripts").mkdir(parents=True)
        (bridge / "venv" / "Scripts" / "python.exe").write_text("")
        key = Path(self.tmp.name) / "data" / "voice-link" / "voice-link.key"
        calls = []

        def fake_run(argv, *a, **k):
            calls.append(list(argv))
            if argv[0] == "wslpath":
                return subprocess.CompletedProcess(argv, 0, stdout=str(bridge) + "\n", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with patch.object(voice_link_setup.desktop_setup, "_find_python", lambda: "python.exe"), \
             patch.object(voice_link_setup.subprocess, "run", fake_run), \
             patch.object(config, "voice_link_key", lambda: key):
            self.assertEqual(voice_link_setup.setup(), 1)
        self.assertFalse(key.exists())
        self.assertFalse((bridge / "voice-link.key").exists())
        self.assertFalse((bridge / "run-voice.cmd").exists())
        self.assertFalse(any("pip" in argv for argv in calls), "nothing is installed for a refused folder")


if __name__ == "__main__":
    unittest.main(verbosity=2)
