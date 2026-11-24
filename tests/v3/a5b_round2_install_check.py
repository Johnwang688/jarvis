"""A5b build-output freshness and preservation, with a fake npm build."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v2 import hud_install

if hasattr(config, "HUD3_DIR"):
    from jarvis.v3 import hud


@unittest.skipUnless(hasattr(config, "HUD3_DIR"), "v3 HUD installer is absent")
class InstallOutput(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(TemporaryDirectory()))
        self.repo = self.temp / "checkout"
        self.dist = self.repo / "hud3" / "dist"
        self.dist.parent.mkdir(parents=True)
        self.root = self.temp / "installed"
        self.enterContext(patch.multiple(
            config, REPO_ROOT=self.repo, HUD3_BUILD=self.dist,
            HUD3_DIR=self.root, HUD_DIR=self.temp / "installed-v2",
        ))
        self.enterContext(patch.object(hud, "frontend_dirty", return_value=False))
        self.enterContext(patch.object(hud_install, "git_head", return_value="a" * 40))
        self.enterContext(patch.object(hud_install, "npm_path", return_value="/fake/npm"))
        self.enterContext(patch.object(hud, "_dependencies_stale", return_value=False))

    def test_a_fresh_index_does_not_publish_leftover_assets(self):
        asset = self.dist / "assets" / "retired.css"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"body { color: gray }")
        (self.dist / "index.html").write_text("<title>Previous build</title>")

        def build(*args, **kwargs):
            self.dist.mkdir(exist_ok=True)
            (self.dist / "index.html").write_text("<title>New build</title>")

        with patch.object(hud, "_npm", side_effect=build):
            installed = hud.install_hud()

        self.assertNotIn("assets/retired.css", installed.files)

    def test_a_refused_install_preserves_the_index_in_a_linked_dist(self):
        output = self.temp / "local-preview"
        output.mkdir()
        index = output / "index.html"
        index.write_text("<title>Local preview</title>")
        self.dist.symlink_to(output, target_is_directory=True)

        with patch.object(hud, "_npm"), self.assertRaises(hud_install.InstallRefused):
            hud.install_hud()

        self.assertTrue(index.is_file(), "installer removed the preview's index")
        self.assertEqual(index.read_text(), "<title>Local preview</title>")


if __name__ == "__main__":
    unittest.main()
