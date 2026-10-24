"""BR7 private Fontconfig; no browser, network, global cache or credentials."""
from __future__ import annotations
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import xml.etree.ElementTree as ET
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis import config
from jarvis.v2.browser import host as H

class Fonts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis-fonts-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home & <fixture>"
        H.secure_dir(self.home)
        self.windows = self.root / "Windows & <Fonts>"
        self.windows.mkdir()
        guard = patch.object(H, "_WINDOWS_FONTS", self.windows)
        guard.start(); self.addCleanup(guard.stop)

    def test_missing_fonts_preserves_existing_environment_and_writes_nothing(self):
        self.windows.rmdir(); self.home.rmdir()
        source = {"LANG": "C.UTF-8", "FONTCONFIG_FILE": "/bad", "XDG_CONFIG_HOME": "/bad"}
        self.assertEqual(H.font_environment(self.home, source), H.chrome_environment(self.home, source))
        self.assertFalse(self.home.exists())

    def test_config_keeps_distro_rules_private_cache_first_and_escaped_paths(self):
        env = H.font_environment(self.home, {})
        self.assertIn("FONTCONFIG_FILE", env)
        path = Path(env["FONTCONFIG_FILE"])
        document = ET.parse(path).getroot()
        self.assertEqual([(e.tag, e.text) for e in document], [
            ("cachedir", str(self.home / "fontconfig" / "cache")),
            ("include", "/etc/fonts/fonts.conf"), ("dir", str(self.windows))])
        for folder in (self.home, path.parent, path.parent / "cache"):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        before = path.read_bytes()
        self.assertEqual(H.font_environment(self.home, {}), env)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["cache", "fonts.conf"])

    def test_hostile_environment_not_inherited(self):
        source = {key: "must-not-reach-chrome" for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "FONTCONFIG_FILE", "FONTCONFIG_PATH", "PULSE_SERVER", "DBUS_SESSION_BUS_ADDRESS", "OPENROUTER_API_KEY")}
        env = H.font_environment(self.home, source)
        self.assertNotIn("must-not-reach-chrome", repr(env))
        self.assertEqual(env["HOME"], str(self.home))
        self.assertTrue(Path(env["FONTCONFIG_FILE"]).is_relative_to(self.home))

    def test_symlinked_config_never_changes_target(self):
        H.font_environment(self.home, {})
        path = self.home / "fontconfig" / "fonts.conf"
        target = self.root / "external"; target.write_text("preserve me")
        path.unlink(); path.symlink_to(target)
        with self.assertRaises(H.BrowserRefused): H.font_environment(self.home, {})
        self.assertEqual(target.read_text(), "preserve me")
        self.assertTrue(path.is_symlink())

    def test_open_or_nonregular_config_is_refused(self):
        H.font_environment(self.home, {})
        path = self.home / "fontconfig" / "fonts.conf"
        path.chmod(0o644)
        with self.assertRaises(H.BrowserRefused): H.font_environment(self.home, {})
        path.unlink(); path.mkdir()
        with self.assertRaises(H.BrowserRefused): H.font_environment(self.home, {})
        path.rmdir(); os.mkfifo(path, 0o600)
        with self.assertRaises(H.BrowserRefused): H.font_environment(self.home, {})

    def test_symlinked_home_config_or_cache_directory_is_refused(self):
        for part in ("home", "fontconfig", "cache"):
            with self.subTest(part=part), tempfile.TemporaryDirectory(dir=self.root) as td:
                base = Path(td); home = base / "home"; target = base / "outside"; target.mkdir(mode=0o700)
                H.secure_dir(home)
                folder = H.secure_dir(home / "fontconfig")
                cache = H.secure_dir(folder / "cache")
                chosen = {"home": home, "fontconfig": folder, "cache": cache}[part]
                if chosen == home: cache.rmdir(); folder.rmdir()
                elif chosen == folder: cache.rmdir()
                chosen.rmdir(); chosen.symlink_to(target, target_is_directory=True)
                with self.assertRaises(H.BrowserRefused): H.font_environment(home, {})
                self.assertEqual(list(target.iterdir()), [])

    def test_parent_swap_before_file_creation_cannot_redirect_write(self):
        H.font_environment(self.home, {})
        folder = self.home / "fontconfig"
        outside = H.secure_dir(self.root / "unrelated")
        victim = outside / "fonts.conf"; victim.write_text("KEEP OWNER CONFIG"); victim.chmod(0o600)
        real = H.os.open
        def swap(path, flags, mode=0o777, *, dir_fd=None):
            if isinstance(path, str) and path.startswith(".fonts-"):
                folder.rename(self.home / "original-fontconfig")
                folder.symlink_to(outside, target_is_directory=True)
            return real(path, flags, mode, dir_fd=dir_fd)
        with patch.object(H.os, "open", side_effect=swap):
            with self.assertRaises(H.BrowserRefused): H.font_environment(self.home, {})
        self.assertEqual(victim.read_text(), "KEEP OWNER CONFIG")
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["fonts.conf"])
        self.assertFalse(list((self.home / "original-fontconfig").glob(".fonts-*")))

    def test_failed_replace_keeps_previous_file_and_removes_temporary(self):
        env = H.font_environment(self.home, {})
        path = Path(env["FONTCONFIG_FILE"]); before = path.read_bytes()
        with patch.object(H.os, "replace", side_effect=OSError("private-marker")):
            with self.assertRaises(H.BrowserRefused) as raised: H.font_environment(self.home, {})
        self.assertNotIn("private-marker", str(raised.exception))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["cache", "fonts.conf"])

class Launch(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_and_private_launches_use_correct_home(self):
        with tempfile.TemporaryDirectory(prefix="jarvis-font-launch-") as td:
            root = Path(td) / "browser"; windows = Path(td) / "fonts"; windows.mkdir()
            context = Mock(browser=Mock(version="fixture"))
            launch = AsyncMock(return_value=context)
            host = H.BrowserHost(); host._gate = SimpleNamespace(port=9)
            host._pw = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
            # The gate check (B-LOGIN-W) needs a real Chrome and gate; this one is fake.
            with patch.object(H.BrowserHost, "_check_gate", AsyncMock(return_value=True)), \
                    patch.object(config, "BROWSER_DIR", root), patch.object(H, "_WINDOWS_FONTS", windows), patch.object(H, "stop_pids", return_value=0):
                # _launch reads the profile's password-store row (password_store.py).
                host._profiles._write([{"id": "p-12345678", "label": "Main", "created": "2026-10-15T00:00:00+00:00"}])
                for profile in ("p-12345678", H.PRIVATE):
                    instance = await host._launch(profile)
                    kwargs = launch.call_args.kwargs
                    expected = (instance.folder if profile == H.PRIVATE else root) / "home"
                    self.assertEqual(kwargs["env"]["HOME"], str(expected))
                    self.assertEqual(kwargs["env"]["FONTCONFIG_FILE"], str(expected / "fontconfig" / "fonts.conf"))
                    self.assertTrue(kwargs["chromium_sandbox"])
                    self.assertIn("--mute-audio", kwargs["args"])
                    self.assertNotIn("PULSE_SERVER", kwargs["env"])

if __name__ == "__main__": unittest.main()
