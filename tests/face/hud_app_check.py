"""Offline launcher tests: temporary Windows paths, Popen recorded, no browser run."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.face import server


class HudApp(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name, value in dict(WINDOWS_LOCALAPPDATA=self.root, HUD_CHROME_PROFILE="Default",
                                FACE_PORT=8402).items():
            guard = patch.object(config, name, value)
            guard.start(); self.addCleanup(guard.stop)
        self.popen = patch.object(server.subprocess, "Popen").start()
        self.addCleanup(patch.stopall)
        self.url = "http://localhost:8402/"
        self.app_id = server.chrome_app_id(self.url)
        self.resources = (self.root / "Google/Chrome/User Data/Default/Web Applications"
                          / "Manifest Resources" / self.app_id)

    def launch(self, browser="/mnt/c/fake/chrome.exe", windows=True, url=None):
        with patch.object(server, "find_browser", return_value=(browser, windows)):
            server.launch_window(url or self.url)
        return self.popen.call_args.args[0]

    def test_app_id_algorithm(self):
        for url in (self.url, "https://example.test/", "http://localhost:12345/"):
            # GenerateAppIdFromManifestId hashes the URL; GenerateId hashes
            # that raw digest again. Independent byte/nibble implementation.
            raw = hashlib.sha256(hashlib.sha256(url.encode()).digest()).digest()[:16]
            expected = "".join("abcdefghijklmnop"[n] for byte in raw for n in (byte >> 4, byte & 15))
            self.assertEqual(server.chrome_app_id(url), expected)
        self.assertRegex(self.app_id, r"^[a-p]{32}$")
        self.assertEqual(self.app_id, "aglnfhnmikeehnbacbmjcjggmgdhgkkh")
        self.assertNotEqual(self.app_id, server.chrome_app_id(self.url.rstrip("/")))

    def test_uninstalled_chrome_uses_explicit_profile_and_url(self):
        args = self.launch()
        self.assertIn("--profile-directory=Default", args)
        self.assertIn(f"--app={self.url}", args)
        self.assertFalse(any(a.startswith("--app-id=") for a in args))

    def test_installed_chrome_uses_app_id_and_profile(self):
        self.resources.mkdir(parents=True)
        with patch.object(Path, "iterdir", side_effect=AssertionError("profile must not be listed")), \
                patch.object(Path, "open", side_effect=AssertionError("profile must not be opened")):
            args = self.launch()
        self.assertIn("--profile-directory=Default", args)
        self.assertIn(f"--app-id={self.app_id}", args)
        self.assertFalse(any(a.startswith("--app=") for a in args))

    def test_other_pages_and_origins_never_use_installed_app(self):
        self.resources.mkdir(parents=True)
        for url in (self.url + "whiteboard.html", self.url + "?page=x", self.url + "#x",
                    "http://127.0.0.1:8402/", "http://localhost:8405/"):
            with self.subTest(url=url):
                args = self.launch(url=url)
                self.assertIn(f"--app={url}", args)
                self.assertIn("--profile-directory=Default", args)

    def test_profile_override_and_changed_hud_port(self):
        with patch.object(config, "HUD_CHROME_PROFILE", "Profile 2"), patch.object(config, "FACE_PORT", 12345):
            url = "http://localhost:12345/"
            app_id = server.chrome_app_id(url)
            (self.root / "Google/Chrome/User Data/Profile 2/Web Applications/Manifest Resources"
             / app_id).mkdir(parents=True)
            args = self.launch(url=url)
            self.assertIn("--profile-directory=Profile 2", args)
            self.assertIn(f"--app-id={app_id}", args)

    def test_unsafe_profiles_refused_before_popen(self):
        for profile in ("../Default", "x/y", "x\\y", "foo..bar", ""):
            with self.subTest(profile=profile), patch.object(config, "HUD_CHROME_PROFILE", profile):
                with self.assertRaises(ValueError):
                    self.launch()
        self.popen.assert_not_called()

    def test_edge_and_playwright_receive_no_chrome_profile_or_app_id(self):
        self.resources.mkdir(parents=True)
        for browser, windows in (("/mnt/c/fake/msedge.exe", True), ("/tmp/chrome.exe", False),
                                 ("/tmp/chrome", False)):
            with self.subTest(browser=browser):
                args = self.launch(browser, windows)
                self.assertIn(f"--app={self.url}", args)
                self.assertFalse(any(a.startswith(("--profile-directory=", "--app-id=")) for a in args))
                self.assertEqual(any(a.startswith("--user-data-dir=") for a in args), not windows)

    def test_windows_localappdata_conversion_and_override(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config._windows_localappdata(), Path("/mnt/c/Users/johnw/AppData/Local"))
            os.environ["LOCALAPPDATA"] = "D:\\Users\\Test\\AppData\\Local"
            self.assertEqual(config._windows_localappdata(), Path("/mnt/d/Users/Test/AppData/Local"))
            os.environ["JARVIS_WINDOWS_LOCALAPPDATA"] = str(self.root)
            self.assertEqual(config._windows_localappdata(), self.root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
