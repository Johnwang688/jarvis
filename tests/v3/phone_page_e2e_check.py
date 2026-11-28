"""Run the built phone dashboard in Chromium with a fixture session on a test daemon."""

from http.client import HTTPConnection
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from bt4_chat_tools_check import TYPED
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
        self.origin = f"https://{HOST}"

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
            # Serve the fixture origin through an ephemeral upstream, with no Chromium network access.
            url = urlsplit(request.url)
            target = url.path + (f"?{url.query}" if url.query else "")
            sent = request.all_headers()
            headers = {name: sent[name] for name in ("origin", "content-type", "user-agent") if name in sent}
            headers.update(Host=HOST, Cookie=COOKIE)
            connection = HTTPConnection("127.0.0.1", daemon.phone_port, timeout=WAIT_MS / 1000)
            try:
                connection.request(request.method, target, request.post_data_buffer, headers)
                response = connection.getresponse()
                status, response_headers, body = response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()
            route.fulfill(status=status, headers=response_headers, body=body)

        def block_websocket(socket):
            errors.append(f"Unexpected WebSocket: {socket.url}")
            socket.close()

        context.route_web_socket("**/*", block_websocket)
        context.on("console", lambda event: errors.append(event.text) if event.type == "error" else None)
        context.on("weberror", lambda event: errors.append(str(event.error)))
        context.add_init_script("""document.addEventListener("securitypolicyviolation", (event) => {
            console.error("CSP", event.effectiveDirective, event.blockedURI);
        });""")
        self.page = context.new_page()
        self.page.route("**/*", forward)
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

    def test_browser_cards_wrap_and_show_every_row_at_phone_width(self):
        site = {"label": "Site", "value": "https://shop.example.invalid"}
        link_title = "Follow a link on the browser page"
        link_rows = [
            {"label": "Link number", "value": "1"},
            {"label": "Address", "value": site["value"] + "/?q=" + "fixture" * 240 + "#end", "source": "page"},
            {"label": "From", "value": site["value"], "source": "page"},
        ]
        cases = (
            ("browser_fill", "Fill a browser field", [
                site, {"label": "Field", "value": "Notes", "source": "page"},
                {"label": "Text", "value": TYPED},
                {"label": "Then", "value": "the page receives this text; the form is not submitted"},
            ]),
            ("browser_press", "Press a browser control", [
                site, {"label": "Button", "value": "Send", "source": "page"}, {"label": "Kind", "value": "button"},
            ]),
            ("browser_sign_in", "Sign in to a saved site", [
                {"label": "Saved login", "value": "fixture"}, site, {"label": "Browser", "value": "chrome"},
            ]),
            ("browser_open", "Open a browser address", [
                {"label": "URL", "value": site["value"] + "/" + "long-path-" * 25},
                {"label": "Reason", "value": "This chat has read outside text."},
            ]),
            ("browser_follow", link_title, link_rows),
            ("browser_press", link_title, link_rows),
        )
        chat = {"state": "running", "seq": 0, "session_id": "fixture-session", "approvals": 1,
                "draft": "", "history": [], "tools": []}
        self.page.route(self.origin + "/m/api/chat", lambda route: route.fulfill(json=chat))
        self.page.route(self.origin + "/m/api/chat/events",
                        lambda route: route.fulfill(json={**chat, "gap": False, "events": []}))
        self.page.set_viewport_size({"width": 320, "height": VIEW["height"]})
        for tool, title, rows in cases:
            with self.subTest(tool=tool):
                def approval(route):
                    route.fulfill(json={"session_id": chat["session_id"], "approvals": [{
                        "id": "fixture", "digest": "a" * 64, "tool": tool, "level": "L2", "why": "fixture",
                        "asked_at": 123, "expires_in": 120, "card": {"title": title, "rows": rows},
                    }]})

                approvals = self.origin + "/m/api/approvals"
                self.page.route(approvals, approval)
                try:
                    self.page.goto(self.origin + "/m/")
                    self.page.get_by_role("tab", name="Chat").tap()
                    card = self.page.get_by_role("region", name="Tool approval")
                    self.expect(card.get_by_role("heading", name=title)).to_be_visible()
                    self.expect(card.get_by_role("button", name="Unlock to approve")).to_be_enabled()
                    self.assertEqual(self.page.evaluate("innerWidth"), 320)
                    for row in rows:
                        label = row["label"] + (" (text from the page)" if row.get("source") == "page" else "")
                        field = card.locator(".approval-target .approval-field, .approval-summary .approval-field").filter(
                            has=self.page.get_by_text(label, exact=True)
                        )
                        # Match raw DOM text so whitespace and newlines must survive unchanged.
                        shown = field.locator("dd")
                        self.assertEqual(shown.text_content(), row["value"])
                        shown.scroll_into_view_if_needed()
                        self.assertTrue(shown.evaluate("(el) => el.scrollWidth <= el.clientWidth"))
                        if row["label"] == "Address":
                            self.assertGreater(shown.locator("bdi").evaluate("(el) => el.getClientRects().length"), 1)
                    self.assertTrue(card.evaluate("(el) => el.scrollWidth <= el.clientWidth"))
                finally:
                    self.page.unroute(approvals, approval)


if __name__ == "__main__":
    unittest.main(verbosity=2)
