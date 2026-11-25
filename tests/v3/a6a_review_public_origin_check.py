"""The v3 listener should refuse the cross-site reads v2's origin guard refuses."""

from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jarvis.v3 import daemon, hud


class PublicOriginCheck(unittest.TestCase):
    def test_cross_site_reads_are_refused_on_status_and_the_installed_hud(self):
        root = Path(self.enterContext(TemporaryDirectory())).resolve()
        source = root / "dist"
        (source / "assets").mkdir(parents=True)
        (source / "index.html").write_text("<!doctype html><title>Test HUD</title>")
        (source / "assets" / "test.css").write_text("body { color: black }")
        installed = root / "hud"
        build = hud.hud_install.install(source, installed)
        self.addCleanup(hud.hud_install._remove, build.path)

        agent = daemon.Daemon(port=0, data_dir=root / "data", hud_dir=installed)
        agent.start()
        self.addCleanup(agent.stop)

        def get(path, headers):
            with closing(HTTPConnection(*agent.address, timeout=2)) as connection:
                connection.request("GET", path, headers=headers)
                response = connection.getresponse()
                response.read()
                return response.status

        cross_site = {"Sec-Fetch-Site": "cross-site"}
        alternate_origin = {**cross_site, "Origin": f"http://localhost:{agent.port}"}
        for path in ("/v3/status", "/", "/assets/test.css"):
            with self.subTest(path=path):
                self.assertEqual(get(path, {}), 200)
            for headers in (cross_site, alternate_origin):
                with self.subTest(path=path, origin_present="Origin" in headers):
                    self.assertEqual(get(path, headers), 403)


if __name__ == "__main__":
    unittest.main()
