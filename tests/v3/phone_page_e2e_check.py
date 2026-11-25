"""Run the built phone dashboard in Chromium with a fixture session on a test daemon."""

import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from e2e_window_check import DIST, WAIT_MS


HOST = "phone.example.invalid"
COOKIE = "__Host-jarvis_m=fake-session-token"
VIEW = {"width": 390, "height": 844}


class PhonePage(unittest.TestCase):
    def setUp(self):
        browser_cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache/ms-playwright"))
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {
            "HOME": str(home),
            "XDG_CACHE_HOME": str(home / "cache"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_RUNTIME_DIR": str(home),
            "PLAYWRIGHT_BROWSERS_PATH": browser_cache,
        }))
        try:
            import jarvis.v3.phone  # noqa: F401
        except ImportError:
            self.skipTest("jarvis.v3.phone is not importable. The phone daemon package is required.")
        if not (DIST / "phone.html").is_file():
            self.skipTest("hud3/dist/phone.html is missing. Build it with npm run build in hud3.")
        try:
            from playwright.sync_api import Error, expect, sync_playwright
        except ImportError:
            self.skipTest("Playwright is not installed.")

        from jarvis import config, llm, sessions
        from jarvis.v2 import hud_install, remote_auth
        from jarvis.v3.daemon import Daemon
        from jarvis.v3.feed import Feed

        data = home / "daemon"
        self.enterContext(patch.multiple(config, V3_DIR=data, V3_POLICY_FILE=data / "policy.json"))
        self.enterContext(patch.object(config, "remote_dir", return_value=home / "remote"))
        self.enterContext(patch.object(sessions, "AUTO_TITLE", False))
        model = self.enterContext(patch.object(llm, "chat", side_effect=AssertionError("live model call")))
        self.addCleanup(model.assert_not_called)
        now = time.time()
        session = {"id": "s-fixture", "created": now, "last_seen": now, "passkey": "fixture",
                   "token_sha256": "0" * 64}
        self.enterContext(patch.object(
            remote_auth.RemoteAuth, "session_for",
            lambda _auth, cookies: dict(session) if cookies == [COOKIE] else None,
        ))
        hud_dir = home / "hud3"
        hud_install.install(DIST, hud_dir)
        feed = Feed(data / "feed")
        for n in range(14):
            feed.post({"kind": "news", "rank": 5, "ask": f"Fixture brief {n}", "why_now": "Fixture text."})
        daemon = Daemon(port=0, phone_port=0, phone_host=HOST, data_dir=data, hud_dir=hud_dir, jobs=[])
        daemon.start()
        self.addCleanup(daemon.stop)
        self.assertIsNotNone(daemon.phone_port, "the ephemeral phone listener did not bind")
        self.origin = f"http://127.0.0.1:{daemon.phone_port}"

        playwright = self.enterContext(sync_playwright())
        try:
            browser = playwright.chromium.launch(headless=True, timeout=15_000, args=[
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
            ])
        except Error as exc:
            if any(reason in str(exc) for reason in (
                "Executable doesn't exist", "Host system is missing dependencies",
                "error while loading shared libraries",
            )):
                self.skipTest("Playwright Chromium or its system dependencies are unavailable.")
            raise
        self.addCleanup(browser.close)
        context = browser.new_context(viewport=VIEW, is_mobile=True, has_touch=True, service_workers="block")
        self.addCleanup(context.close)
        context.set_default_timeout(WAIT_MS)
        errors = []
        self.addCleanup(self.assertEqual, errors, [], "Browser errors, CSP violations or unexpected requests")

        def forward(route):
            request = route.request
            if not request.url.startswith(self.origin + "/"):
                errors.append(f"Unexpected browser request: {request.url}")
                route.abort()
                return
            # Model the tunnel's upstream Host and Origin on the ephemeral
            # loopback connection. Chromium still applies the returned CSP.
            headers = {**request.headers, "host": HOST, "cookie": COOKIE}
            if request.method not in ("GET", "HEAD"):
                self.assertEqual(headers.get("origin"), self.origin)
                headers["origin"] = f"https://{HOST}"
            response = route.fetch(headers=headers, max_redirects=0, timeout=WAIT_MS)
            try:
                route.fulfill(response=response)
            finally:
                response.dispose()

        def block_websocket(socket):
            errors.append(f"Unexpected WebSocket: {socket.url}")
            socket.close()

        context.route("**/*", forward)
        context.route_web_socket("**/*", block_websocket)
        context.on("console", lambda event: errors.append(event.text) if event.type == "error" else None)
        context.on("weberror", lambda event: errors.append(str(event.error)))
        context.add_init_script("""document.addEventListener("securitypolicyviolation", (event) => {
            console.error("CSP", event.effectiveDirective, event.blockedURI);
        });""")
        self.page = context.new_page()
        self.expect = expect

    def test_dashboard_fits_in_the_viewport_under_the_phone_csp(self):
        response = self.page.goto(self.origin + "/m/", wait_until="load")
        self.assertEqual(response.status, 200)
        policies = {name: values for name, *values in
                    (part.split() for part in response.headers["content-security-policy"].split(";") if part.strip())}
        self.assertEqual(policies["script-src"], ["'self'"])
        self.expect(self.page.get_by_text("Locked", exact=True)).to_be_visible()
        self.expect(self.page.get_by_text("Running", exact=True)).to_be_visible()
        self.expect(self.page.get_by_role("article")).to_have_count(14)
        for name in ("Feed", "Chat", "Work"):
            with self.subTest(tab=name):
                self.page.get_by_role("tab", name=name, exact=True).tap()
                panel = self.page.get_by_role("tabpanel", name=name, exact=True)
                self.expect(panel).to_be_visible()
                if name == "Chat":
                    self.expect(panel.get_by_role("textbox", name="Message")).to_be_visible()
                if name == "Work":
                    self.expect(panel.get_by_role("heading", name="Scheduled jobs")).to_be_visible()
                self.assertEqual(self.page.evaluate("innerWidth"), VIEW["width"])
                self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), VIEW["width"])
                self.assertLessEqual(self.page.evaluate("document.documentElement.scrollHeight"), VIEW["height"])
                short = self.page.locator("button:visible, summary:visible, textarea:visible").evaluate_all(
                    "(nodes) => nodes.filter(node => node.getBoundingClientRect().height < 44).map(node => node.textContent)"
                )
                self.assertEqual(short, [], "a visible control is shorter than 44 px")
                if name == "Feed":
                    self.assertTrue(panel.locator(".agent-feed").evaluate("(el) => el.scrollHeight > el.clientHeight"))
                shots = os.environ.get("E2E_SHOTS")
                if shots:
                    self.page.screenshot(path=str(Path(shots) / f"phone-{name.lower()}-390x844.png"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
