"""A5b install provenance and refusal regressions with a fake frontend build."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from jarvis import config
from jarvis.v2 import hud_install

if hasattr(config, "HUD3_DIR"):
    from jarvis.v3 import hud


@unittest.skipUnless(hasattr(config, "HUD3_DIR"), "v3 HUD installer is absent")
class InstallReview(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(TemporaryDirectory())
        self.root = Path(temp) / "installed"
        self.repo = Path(temp) / "checkout"
        self.dist = self.repo / "hud3" / "dist"
        self.dist.mkdir(parents=True)
        self.index = self.dist / "index.html"
        self.enterContext(patch.multiple(
            config, REPO_ROOT=self.repo, HUD3_BUILD=self.dist,
            HUD3_DIR=self.root, HUD_DIR=Path(temp) / "installed-v2",
        ))
        self.enterContext(patch.object(hud, "frontend_dirty", return_value=False))
        self.head = self.enterContext(patch.object(hud_install, "git_head", return_value="a" * 40))

    def test_successful_build_must_produce_its_own_index(self):
        self.index.write_bytes(b"<!doctype html><title>Previous build</title>")
        with patch.object(hud_install, "npm_path", return_value="/fake/npm"), patch.object(
            hud.subprocess, "run", return_value=SimpleNamespace(returncode=0)
        ) as npm:
            with self.assertRaises(hud_install.InstallRefused):
                hud.install_hud()
        self.assertIn(["/fake/npm", "run", "build"], [call.args[0] for call in npm.call_args_list])
        self.assertFalse(self.root.exists())

    def test_allow_dirty_cannot_attribute_a_moving_checkout_to_its_ending_commit(self):
        def build():
            self.index.write_bytes(b"<!doctype html><title>Starting checkout</title>")
            self.head.return_value = "b" * 40

        try:
            installed = hud.install_hud(build=build, allow_dirty=True)
        except hud_install.InstallRefused:
            self.assertFalse(self.root.exists())
        else:
            manifest = hud_install.load(self.root, installed.id)
            self.assertIsNotNone(manifest)
            self.assertNotEqual(manifest.git_head, "b" * 40)

    def test_symlink_refusal_identifies_the_v3_setting_to_fix(self):
        self.root.symlink_to(self.dist, target_is_directory=True)
        build = Mock()
        with self.assertRaisesRegex(hud_install.InstallRefused, r"\bJARVIS_HUD3_DIR\b"):
            hud.install_hud(build=build)
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
