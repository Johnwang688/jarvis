"""VA-1 re-check 3: closed-folder certification and its setup/read boundary.

All paths, keys and executables are temporary. Every setup subprocess is fake;
no listeners, Windows executables, package installations or network are used.
Run with PYTHONPATH=. and the shared venv in the prescribed systemd scope.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from jarvis import config, voice_link_setup
from jarvis.tools import files
from jarvis.v2 import permissions


class CertificationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="va1-verifier3-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.users = self.root / "Users"
        self.users.mkdir()
        self.wsl_key = self.root / "data" / "voice-link" / "voice-link.key"
        self.guard(patch.dict(os.environ, {
            "HOME": str(self.home), "JARVIS_WINDOWS_USERS": str(self.users),
        }))
        for name, value in {
            "V2_DATA_DIR": self.root / "data",
            "BROWSER_DIR": self.root / "browser",
            "VOICE_LINK_KEY": config._VOICE_LINK_KEY_DEFAULT,
            "DESKTOP_BRIDGE_DIR": r"C:\Users\owner\.jarvis-bridge",
        }.items():
            self.guard(patch.object(config, name, value))

    def guard(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def bridge(self, path):
        exe = path / "venv" / "Scripts" / "python.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.touch()
        (path / "ordinary.txt").write_text("synthetic-private-sentinel")
        return path

    def snapshot(self):
        # rglob does not traverse directory symlinks. All targets in this suite
        # are also reachable through their real path inside this temp tree.
        return {
            str(p.relative_to(self.root)):
                ("link", os.readlink(p)) if p.is_symlink() else
                ("dir",) if p.is_dir() else ("file", p.read_bytes())
            for p in self.root.rglob("*")
        }

    @contextmanager
    def fake_setup(self, bridge):
        calls = []

        def run(argv, **kwargs):
            calls.append(list(argv))
            if argv == ["wslpath", "-u", config.DESKTOP_BRIDGE_DIR]:
                return subprocess.CompletedProcess(argv, 0, str(bridge) + "\n", "")
            if "pip" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            raise AssertionError(f"unexpected subprocess: {argv!r}")

        with patch.object(voice_link_setup.desktop_setup, "_find_python", return_value="python.exe"), \
             patch.object(voice_link_setup.desktop_setup, "_win_path", return_value=r"C:\fake\voice_agent.py"), \
             patch.object(voice_link_setup.desktop_setup.console, "print"), \
             patch.object(voice_link_setup.subprocess, "run", side_effect=run), \
             patch.object(voice_link_setup.secrets, "token_hex", return_value="ab" * 32):
            yield calls

    def assert_refused_before_install_or_write(self, bridge):
        self.assertFalse(voice_link_setup.windows_folder_closed(bridge))
        before = self.snapshot()
        with self.fake_setup(bridge) as calls, \
             patch.object(voice_link_setup, "write_keys", wraps=voice_link_setup.write_keys) as write:
            self.assertEqual(voice_link_setup.setup(), 1)
            write.assert_not_called()
        self.assertEqual(calls, [["wslpath", "-u", config.DESKTOP_BRIDGE_DIR]])
        self.assertEqual(self.snapshot(), before)

    def assert_guarded(self, bridge):
        judge = files.credential_folder_judge()
        for folder in (bridge, Path(os.path.realpath(bridge))):
            with self.subTest(folder=str(folder)):
                self.assertTrue(judge(folder))
                self.assertTrue(judge(folder / "voice-link.key"))
                # Use a non-credential name to exercise folder closure rather
                # than the independent EXACT_NAMES rule for voice-link.key.
                self.assertTrue(files.read_file(str(folder / "ordinary.txt")).startswith("Error:"))
                self.assertIsNotNone(permissions.denied_read(
                    "Read", {"file_path": str(folder / "ordinary.txt")}, str(self.root)))
                self.assertIsNotNone(permissions.denied_read(
                    "Grep", {"path": str(folder), "pattern": "."}, str(self.root)))

    def test_plain_closed_profile_setup_still_succeeds(self):
        bridge = self.bridge(self.users / "owner" / ".jarvis-bridge")
        self.assertTrue(voice_link_setup.windows_folder_closed(bridge))
        self.assert_guarded(bridge)
        with self.fake_setup(bridge) as calls:
            self.assertEqual(voice_link_setup.setup(), 0)
        self.assertTrue(any("pip" in argv for argv in calls))
        self.assertEqual(self.wsl_key.read_bytes(), (bridge / "voice-link.key").read_bytes())
        self.assertTrue((bridge / "run-voice.cmd").is_file())
        self.assert_guarded(bridge)

    def test_appdata_and_nested_dot_folder_stay_closed(self):
        for entry in (Path("AppData/Local/Jarvis"), Path(".jarvis-bridge/nested")):
            bridge = self.bridge(self.users / "owner" / entry)
            self.assertTrue(voice_link_setup.windows_folder_closed(bridge))
            self.assert_guarded(bridge)

    def test_symlinked_users_root_refuses_lexical_and_real_destinations(self):
        real = self.root / "actual-users"
        bridge = self.bridge(real / "owner" / ".jarvis-bridge")
        self.users.rmdir()
        self.users.symlink_to(real, target_is_directory=True)
        self.assertFalse(files.credential_folder_judge()(bridge))
        for path in (self.users / "owner" / ".jarvis-bridge", bridge):
            with self.subTest(path=str(path)):
                self.assert_refused_before_install_or_write(path)

    def test_symlinked_root_can_be_certified_when_real_root_is_also_protected(self):
        # The judge may refuse the canonical spelling for a different reason;
        # it still satisfies the contract when the profile shape also holds.
        real = self.home / ".ssh" / "actual-users"
        bridge = self.bridge(real / "owner" / ".jarvis-bridge")
        self.users.rmdir()
        self.users.symlink_to(real, target_is_directory=True)
        for path in (self.users / "owner" / ".jarvis-bridge", bridge):
            with self.subTest(path=str(path)):
                self.assertTrue(voice_link_setup.windows_folder_closed(path))
                self.assert_guarded(path)
                with self.fake_setup(path):
                    self.assertEqual(voice_link_setup.setup(), 0)

    def test_profile_symlink_outside_users_is_refused(self):
        external = self.root / "external-profile"
        bridge = self.bridge(external / ".jarvis-bridge")
        (self.users / "owner").symlink_to(external, target_is_directory=True)
        self.assertFalse(files.credential_folder_judge()(bridge))
        self.assert_refused_before_install_or_write(self.users / "owner" / ".jarvis-bridge")

    def test_profile_symlink_to_another_real_profile_stays_guarded(self):
        self.bridge(self.users / "real-owner" / ".jarvis-bridge")
        (self.users / "owner").symlink_to(self.users / "real-owner", target_is_directory=True)
        bridge = self.users / "owner" / ".jarvis-bridge"
        self.assertTrue(voice_link_setup.windows_folder_closed(bridge))
        self.assert_guarded(bridge)
        with self.fake_setup(bridge):
            self.assertEqual(voice_link_setup.setup(), 0)

    def test_closed_bridge_symlink_to_open_target_is_refused(self):
        external = self.bridge(self.root / "open-target")
        bridge = self.users / "owner" / ".jarvis-bridge"
        bridge.parent.mkdir()
        bridge.symlink_to(external, target_is_directory=True)
        self.assertTrue(files.credential_folder_judge()(bridge))
        self.assertFalse(files.credential_folder_judge()(external))
        self.assert_refused_before_install_or_write(bridge)

    def test_open_bridge_symlink_into_closed_target_has_safe_certificate(self):
        target = self.bridge(self.users / "owner" / ".jarvis-bridge")
        bridge = self.root / "open-alias"
        bridge.symlink_to(target, target_is_directory=True)
        self.assertTrue(voice_link_setup.windows_folder_closed(bridge))
        self.assert_guarded(bridge)
        # write_keys' existing, independent ban on a symlinked immediate
        # parent is outside the changed certification predicate.
        with self.fake_setup(bridge), \
             patch.object(voice_link_setup, "write_keys", wraps=voice_link_setup.write_keys) as write:
            self.assertEqual(voice_link_setup.setup(), 1)
            write.assert_called_once()
        self.assertFalse(self.wsl_key.exists())
        self.assertFalse((target / "voice-link.key").exists())
        self.assertFalse((target / "run-voice.cmd").exists())

    def test_non_profile_credential_folders_are_still_refused(self):
        # Read refusal is necessary, not sufficient: keep the profile rule.
        for path in (self.home / ".ssh", self.users / "Public" / ".jarvis-bridge",
                     self.users / "owner" / "Documents" / ".jarvis-bridge"):
            bridge = self.bridge(path)
            # Explicitly protect the Documents case for a non-Windows reason.
            with patch.object(config, "V2_CREDENTIAL_DIRS", (*config.V2_CREDENTIAL_DIRS, str(path))):
                self.assertTrue(files.credential_folder_judge()(bridge))
                self.assert_refused_before_install_or_write(bridge)

    def test_closed_bridge_symlink_to_non_profile_credential_folder_is_refused(self):
        target = self.bridge(self.home / ".ssh" / "bridge")
        bridge = self.users / "owner" / ".jarvis-bridge"
        bridge.parent.mkdir()
        bridge.symlink_to(target, target_is_directory=True)
        self.assert_guarded(bridge)
        self.assert_refused_before_install_or_write(bridge)

    def test_existing_key_symlink_to_readable_target_prevents_setup(self):
        bridge = self.bridge(self.users / "owner" / ".jarvis-bridge")
        public = self.root / "ordinary.txt"
        public.write_text("synthetic-old-value")
        (bridge / "voice-link.key").symlink_to(public)
        judge = files.credential_folder_judge()
        self.assertTrue(judge(bridge))
        self.assertTrue(judge(bridge / "voice-link.key"))
        self.assertFalse(judge(public))
        self.assert_refused_before_install_or_write(bridge)

    def test_judge_factory_exceptions_fail_closed_before_setup_effects(self):
        bridge = self.bridge(self.users / "owner" / ".jarvis-bridge")
        for error in (OSError, RuntimeError, TypeError, ValueError):
            with self.subTest(error=error.__name__), \
                 patch.object(files, "credential_folder_judge", side_effect=error("synthetic failure")):
                self.assert_refused_before_install_or_write(bridge)

    def test_judge_call_exceptions_fail_closed_before_setup_effects(self):
        bridge = self.bridge(self.users / "owner" / ".jarvis-bridge")
        real_judge = files.credential_folder_judge()
        for error in (OSError, RuntimeError, TypeError, ValueError):
            for fail_on in ("folder", "key"):
                def broken_judge(path):
                    if fail_on == "folder" or Path(path).name == "voice-link.key":
                        raise error("synthetic failure")
                    return real_judge(path)

                with self.subTest(error=error.__name__, fail_on=fail_on), \
                     patch.object(files, "credential_folder_judge", return_value=broken_judge):
                    self.assert_refused_before_install_or_write(bridge)


if __name__ == "__main__":
    unittest.main(verbosity=2)
