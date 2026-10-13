"""Verify chrome_app_id against real Chromium PWA.install, entirely offline.

Use the full installed Chromium (channel='chromium'), not its headless shell,
which does not expose PWA.install. Only a temp user-data-dir and port-0 server
are used. Run with temp HOME/config paths like the other HUD checks.
"""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from playwright.sync_api import sync_playwright
from jarvis.face.server import chrome_app_id
from jarvis.v2 import hud_api

PUBLIC = Path(__file__).resolve().parents[2] / "hud/public"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC), **kwargs)

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path != "/":
            return super().do_GET()
        body = (b'<!doctype html><title>J.A.R.V.I.S.</title>'
                b'<link rel="manifest" href="/manifest.webmanifest"><h1>J.A.R.V.I.S.</h1>')
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def guess_type(self, path):
        return "application/manifest+json" if path.endswith(".webmanifest") else super().guess_type(path)

    def end_headers(self):
        for name, value in hud_api.frame_headers(self):
            self.send_header(name, value)
        super().end_headers()


class ChromiumAppId(unittest.TestCase):
    def test_installed_resources_use_the_launcher_app_id(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://localhost:{server.server_port}/"
        with tempfile.TemporaryDirectory(prefix="hud-chromium-profile-") as profile, sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(profile, headless=True, channel="chromium")
            try:
                ctx.route("**/*", lambda route: route.continue_() if route.request.url.startswith(url) else route.abort())
                page = ctx.new_page()
                page.goto(url)
                cdp = ctx.browser.new_browser_cdp_session()
                version = cdp.send("Browser.getVersion")["product"]
                cdp.send("PWA.install", {"manifestId": url, "installUrlOrBundleUrl": url})
                # This is our temporary profile only, never a real one.
                resources = Path(profile) / "Default/Web Applications/Manifest Resources"
                actual = [p.name for p in resources.iterdir()] if resources.exists() else []
                self.assertIn(chrome_app_id(url), actual, f"{version}: {actual}")
                print(f"\n{version}: PWA.install verified {chrome_app_id(url)}", flush=True)
            finally:
                ctx.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
