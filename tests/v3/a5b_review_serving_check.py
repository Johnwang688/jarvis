"""A5b routing and build turnover regressions without opening a socket."""

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v2 import hud_install
from jarvis.v3.daemon import _status_handler


class ServingReview(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(TemporaryDirectory()))
        self.root = self.temp / "installed"
        self.source = self.temp / "dist"
        self.source.mkdir()

    def install_fixture(self, index, sequence):
        (self.source / "index.html").write_bytes(index)
        return hud_install.install(self.source, self.root, now=1_700_000_000 + sequence)

    def request(self, target, method="GET"):
        with patch.object(config, "HUD3_DIR", self.root, create=True):
            handler_type = _status_handler(self.temp / "data")
        handler = handler_type.__new__(handler_type)
        handler.server = SimpleNamespace(server_port=49152)
        handler.rfile = BytesIO(f"{method} {target} HTTP/1.1\r\nHost: localhost:49152\r\n\r\n".encode())
        handler.wfile = BytesIO()
        handler.handle_one_request()
        head, _, body = handler.wfile.getvalue().partition(b"\r\n\r\n")
        return int(head.split()[1]), body

    def test_status_still_requires_its_exact_request_target(self):
        self.assertEqual(self.request("/v3/status")[0], 200)
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                self.assertEqual(self.request("/v3/status?view=compact", method)[0], 404)

    @unittest.skipUnless(hasattr(config, "HUD3_DIR"), "v3 HUD serving is absent")
    def test_removed_unversioned_asset_does_not_outlive_its_build(self):
        asset = self.source / "assets" / "preferences.json"
        asset.parent.mkdir()
        asset.write_bytes(b"{}")
        self.install_fixture(b"<!doctype html><title>First</title>", 0)
        self.assertEqual(self.request("/assets/preferences.json"), (200, b"{}"))
        asset.unlink()
        self.install_fixture(b"<!doctype html><title>Second</title>", 1)
        self.assertEqual(self.request("/assets/preferences.json")[0], 404)

    @unittest.skipUnless(hasattr(config, "HUD3_DIR"), "v3 HUD serving is absent")
    def test_index_stays_available_if_a_concurrent_install_prunes_the_selected_build(self):
        original = b"<!doctype html><title>First</title>"
        first = self.install_fixture(original, 0)
        newest = hud_install.newest
        latest = b"<!doctype html><title>Latest</title>"

        def select(root):
            selected = newest(root)
            if selected is not None and selected.id == first.id:
                for sequence in range(1, hud_install.KEEP + 1):
                    self.install_fixture(latest, sequence)
                self.assertFalse(first.path.exists())
            return selected

        with patch.object(hud_install, "newest", side_effect=select):
            status, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(body, (original, latest))

    def test_v3_does_not_serve_the_v2_install_root(self):
        old_index = b"<!doctype html><title>V2 window</title>"
        self.install_fixture(old_index, 0)
        with patch.object(config, "HUD_DIR", self.root):
            status, body = self.request("/")
        self.assertIn(status, (404, 503))
        self.assertNotEqual(body, old_index)


if __name__ == "__main__":
    unittest.main()
