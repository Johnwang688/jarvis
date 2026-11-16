"""Round-2 VA-1 counterexamples; temporary keys and fake subprocesses only.

Run with PYTHONPATH=. and the shared venv under the prescribed systemd scope.
These tests concern setup certifying an unclosed destination, rather than the
separately tracked EXACT_NAMES limitations for arbitrary existing key copies.
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
from jarvis.tools import files  # noqa: E402
from jarvis.v2 import permissions  # noqa: E402


class SymlinkedUsersRootTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="va1-verifier2-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.real_users = self.root / "actual-users"
        self.bridge = self.real_users / "owner" / ".jarvis-bridge"
        exe = self.bridge / "venv" / "Scripts" / "python.exe"
        exe.parent.mkdir(parents=True)
        exe.touch()
        self.users = self.root / "Users"
        self.users.symlink_to(self.real_users, target_is_directory=True)
        self.wsl_key = self.root / "data" / "voice-link" / "voice-link.key"
        guard = patch.dict(os.environ, {
            "HOME": str(self.root / "home"),
            "JARVIS_WINDOWS_USERS": str(self.users),
        })
        guard.start()
        self.addCleanup(guard.stop)
        for name, value in {
            "V2_DATA_DIR": self.root / "data",
            "BROWSER_DIR": self.root / "browser",
            "VOICE_LINK_KEY": config._VOICE_LINK_KEY_DEFAULT,
            "DESKTOP_BRIDGE_DIR": r"C:\Users\owner\.jarvis-bridge",
        }.items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)

    def test_closed_certificate_must_match_canonical_folder_guard(self):
        # A provider can name the canonical path directly, bypassing the
        # lexical Users alias. The helper resolves its root; the reader's
        # Windows closure compares against the lexical root instead.
        certified = voice_link_setup.windows_folder_closed(self.bridge)
        guarded = files.credential_folder_judge()(self.bridge / "voice-link.key")
        self.assertTrue(
            not certified or guarded,
            "windows_folder_closed=True but canonical Windows key folder is agent-readable",
        )

    def test_setup_must_not_pair_into_canonical_folder_readable_by_grep(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            lexical_bridge = self.users / "owner" / ".jarvis-bridge"
            return subprocess.CompletedProcess(argv, 0, str(lexical_bridge) + "\n", "")

        with patch.object(voice_link_setup.desktop_setup, "_find_python", return_value="python.exe"), \
             patch.object(voice_link_setup.desktop_setup, "_win_path", return_value=r"\\wsl.localhost\Ubuntu\repo\windows\voice_agent.py"), \
             patch.object(voice_link_setup.desktop_setup.console, "print"), \
             patch.object(voice_link_setup.subprocess, "run", fake_run), \
             patch.object(config, "voice_link_key", return_value=self.wsl_key):
            result = voice_link_setup.setup()
            refusal = permissions.denied_read(
                "Grep", {"path": str(self.bridge), "pattern": "."}, str(self.root)
            )
            installed = any("pip" in argv for argv in calls)
            written = [path.name for path in (
                self.wsl_key, self.bridge / "voice-link.key", self.bridge / "run-voice.cmd"
            ) if path.exists()]
            # Refusal before installation/writes is safe; accepting a folder
            # actually closed by the existing gate is also safe. Current HEAD
            # accepts it, installs, and writes both keys plus the launcher,
            # while a provider's recursive Grep is permitted without a scrub.
            safe = (refusal is not None if result == 0 else not installed and not written)
            self.assertTrue(
                safe,
                f"setup={result}; pip={installed}; written={written}; recursive Grep allowed={refusal is None}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
