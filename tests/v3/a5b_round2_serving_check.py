"""A5b build turnover and launch feedback using temporary HUD stores."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v2 import hud_install

if hasattr(config, "HUD3_DIR"):
    from jarvis.v3 import hud


@unittest.skipUnless(hasattr(config, "HUD3_DIR"), "v3 HUD serving is absent")
class ServingTurnover(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(TemporaryDirectory()))
        self.source = self.temp / "dist"
        self.source.mkdir()
        self.root = self.temp / "installed"
        self.enterContext(patch.multiple(config, HUD3_DIR=self.root, HUD_DIR=self.temp / "installed-v2"))

    def install(self, title, sequence):
        (self.source / "index.html").write_bytes(title)
        return hud_install.install(self.source, self.root, now=1_700_000_000 + sequence)

    def test_index_remains_available_during_partial_prune(self):
        old = self.install(b"<title>Old</title>", 0)
        newest = hud_install.newest
        latest = b"<title>Latest</title>"
        picked_old = False

        def begin_prune(path):
            path.chmod(0o755)
            (path / "index.html").unlink()
            self.assertTrue(path.is_dir())

        def select(root):
            nonlocal picked_old
            build = newest(root)
            if not picked_old and build == old:
                picked_old = True
                with patch.object(hud_install, "_remove", side_effect=begin_prune):
                    for sequence in range(1, hud_install.KEEP + 1):
                        self.install(latest, sequence)
            return build

        with patch.object(hud_install, "newest", side_effect=select):
            page = hud.static_file(self.root, "/")

        self.assertEqual(page, ("text/html; charset=utf-8", latest))

    def test_an_unversioned_hyphenated_asset_does_not_outlive_its_build(self):
        asset = self.source / "assets" / "panel-settings.json"
        asset.parent.mkdir()
        asset.write_bytes(b'{"panel":"old"}')
        self.install(b"<title>Old</title>", 0)
        self.assertIsNotNone(hud.static_file(self.root, "/assets/panel-settings.json"))
        asset.unlink()
        self.install(b"<title>New</title>", 1)

        self.assertIsNone(hud.static_file(self.root, "/assets/panel-settings.json"))

    def test_impossible_manifest_name_skips_the_fallback_matcher(self):
        self.install(b"<title>Current</title>", 0)
        name = "a" * 199 + ".css"  # the install manifest admits at most 200 characters
        with patch.object(hud, "_HASHED", create=True) as matcher:
            matcher.fullmatch.return_value = None
            self.assertIsNone(hud.static_file(self.root, f"/assets/{name}"))
        matcher.fullmatch.assert_not_called()

    def test_window_launch_warns_when_the_configured_store_is_the_v2_store(self):
        from jarvis.face import server

        self.install(b"<title>V2 window</title>", 0)
        messages = []
        with patch.object(config, "HUD_DIR", self.root), patch.object(server, "launch_window"):
            self.assertIsNone(hud.static_file(self.root, "/"))
            hud.open_window(messages.append)

        self.assertIn("jarvis agent install", " ".join(messages))


if __name__ == "__main__":
    unittest.main()
