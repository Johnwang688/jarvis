"""Drive the installed phone page through real WebAuthn on an isolated daemon."""

from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from e2e_window_check import DIST, VAULT_ENTRY, WAIT_MS
from phone_page_e2e_check import HOST, VIEW


ORIGIN = f"https://{HOST}"
COOKIE_NAME = "__Host-jarvis_m"
LOGIN_VERIFY = "/m/api/login/verify"
FEED_ANSWER = "/m/api/feed/answer"
APPROVAL_ANSWER = "/m/api/approvals/answer"
DENIED_ENTRY = {**VAULT_ENTRY, "name": "fixture-denied"}


class PhonePasskey(unittest.TestCase):
    def setUp(self):
        if not (DIST / "phone.html").is_file():
            self.skipTest("hud3/dist/phone.html is missing. Build it with npm run build in hud3.")
        try:
            from playwright.sync_api import Error, expect, sync_playwright
        except ImportError:
            self.skipTest("Playwright is not installed.")

        browser_cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache/ms-playwright"))
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data = home / "daemon"
        self.remote = home / "remote"
        self.hud = home / "hud3"
        self.shots = os.environ.get("E2E_SHOTS")
        self.enterContext(patch.dict(os.environ, {
            "HOME": str(home),
            "XDG_CACHE_HOME": str(home / "cache"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_RUNTIME_DIR": str(home),
            "PLAYWRIGHT_BROWSERS_PATH": browser_cache,
            "JARVIS_VAULT": str(self.data / "vault"),
            "JARVIS_INDEX_DB": str(self.data / "index.sqlite"),
        }))

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

        from a6b1_acceptance_check import Model, reply
        from jarvis import config, llm, sessions
        from jarvis.v2 import hud_install
        from jarvis.v3.feed import Feed

        self.enterContext(patch.multiple(
            config, V3_DIR=self.data, V3_POLICY_FILE=self.data / "policy.json",
            GOOGLE_TOKEN_PATH=self.data / "no-google-token.json",
        ))
        self.enterContext(patch.object(config, "remote_dir", return_value=self.remote))
        self.enterContext(patch.object(sessions, "AUTO_TITLE", False))
        live_model = self.enterContext(patch.object(llm, "chat", side_effect=AssertionError("live model call")))
        self.addCleanup(live_model.assert_not_called)
        hud_install.install(DIST, self.hud)
        self.feed = Feed(self.data / "feed")
        self.model = Model(
            reply(calls=[("vault_add", VAULT_ENTRY)]), reply("Fixture note saved."),
            reply(calls=[("vault_add", DENIED_ENTRY)]), reply("Fixture note declined."),
        )
        self.start_daemon(self.model)

        self.errors = []
        self.answers = []
        self.login_cookie_headers = []
        self.addCleanup(self.assertEqual, self.errors, [], "Browser errors, CSP violations or unexpected requests")
        self.context = browser.new_context(viewport=VIEW, is_mobile=True, has_touch=True, service_workers="block")
        self.addCleanup(self.context.close)
        self.context.set_default_timeout(WAIT_MS)
        self.context.route("**/*", self.forward)
        self.context.route_web_socket("**/*", self.block_websocket)
        self.context.on("console", lambda msg: self.errors.append(msg.text) if msg.type == "error" else None)
        self.context.on("weberror", lambda event: self.errors.append(str(event.error)))
        self.context.add_init_script("""document.addEventListener("securitypolicyviolation", (event) => {
            console.error("CSP", event.effectiveDirective, event.blockedURI);
        });""")
        self.page = self.context.new_page()
        self.expect = expect
        self.cdp = self.context.new_cdp_session(self.page)
        self.cdp.send("WebAuthn.enable")
        self.authenticator = self.cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
            "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
            "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True,
        }})["authenticatorId"]

    def start_daemon(self, model):
        from jarvis.v3.approvals import ApprovalBroker
        from jarvis.v3.chat import ChatService
        from jarvis.v3.daemon import Daemon

        broker = ApprovalBroker(log=self.data / "approvals.jsonl")
        self.addCleanup(broker.shutdown)
        chat = ChatService(self.data / "chats", completion=model, approvals=broker)
        self.daemon = Daemon(port=0, phone_port=0, phone_host=HOST, data_dir=self.data,
                             hud_dir=self.hud, jobs=[], chat=chat)
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        self.assertIsNotNone(self.daemon.phone_port, "The ephemeral phone listener did not bind")

    def forward(self, route):
        request = route.request
        if not request.url.startswith(ORIGIN + "/"):
            self.errors.append(f"Unexpected browser request: {request.url}")
            route.abort()
            return
        url = urlsplit(request.url)
        target = url.path + (f"?{url.query}" if url.query else "")
        if request.method == "POST" and url.path in (FEED_ANSWER, APPROVAL_ANSWER):
            body = request.post_data_json
            self.answers.append((url.path, body.get("action") or body.get("decision")))

        # Stand in for TLS termination. Keep the browser's Origin and cookie
        # intact while making the upstream connection to the test listener.
        sent = request.all_headers()
        headers = {name: sent[name] for name in ("origin", "cookie", "content-type", "user-agent") if name in sent}
        headers["Host"] = HOST
        connection = HTTPConnection("127.0.0.1", self.daemon.phone_port, timeout=WAIT_MS / 1000)
        try:
            connection.request(request.method, target, request.post_data_buffer, headers)
            response = connection.getresponse()
            if url.path == LOGIN_VERIFY:
                self.login_cookie_headers.extend(response.headers.get_all("Set-Cookie", []))
            status, headers, body = response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()
        route.fulfill(status=status, headers=headers, body=body)

    def block_websocket(self, socket):
        self.errors.append(f"Unexpected WebSocket: {socket.url}")
        socket.close()

    def shot(self, name):
        if self.shots:
            self.page.screenshot(path=str(Path(self.shots) / f"passkey-{name}-390x844.png"))

    def button(self, name):
        return self.page.get_by_role("button", name=name, exact=True)

    def expect_tabs(self):
        tabs = self.page.get_by_role("tablist", name="Dashboard").get_by_role("tab")
        self.expect(tabs).to_have_text(["Feed", "Chat", "Work"])
        self.assertEqual(self.page.evaluate("[innerWidth, innerHeight]"), [VIEW["width"], VIEW["height"]])

    def expect_lock(self, locked):
        pill = self.page.locator(".phone-lock")
        self.expect(pill).to_have_attribute("data-locked", str(locked).lower())
        self.expect(pill).to_have_text("Locked" if locked else re.compile(r"^[1-9]\d* min left$"))

    def pair(self):
        auth = self.daemon._phone_server.auth
        with self.page.expect_response(ORIGIN + "/m/pair") as loaded:
            self.page.goto(auth.open_window()["url"])
        self.assertEqual(loaded.value.status, 200)
        self.assertTrue(self.page.evaluate("isSecureContext"))
        self.expect(self.page).to_have_url(ORIGIN + "/m/pair")
        with self.page.expect_response(ORIGIN + "/m/api/pair/verify") as verified:
            self.button("Create passkey").tap()
        self.assertEqual(verified.value.status, 200)
        self.expect(self.page.get_by_text("Your passkey is ready.")).to_be_visible()
        self.expect(self.page).to_have_url(ORIGIN + "/m/")
        (saved,) = json.loads((self.remote / "passkeys.json").read_text())["passkeys"]
        (credential,) = self.cdp.send("WebAuthn.getCredentials", {
            "authenticatorId": self.authenticator,
        })["credentials"]
        self.assertTrue(credential["isResidentCredential"])
        self.assertEqual(credential["rpId"], HOST)
        self.assertTrue(saved["public_key"])
        self.assertEqual(self.context.cookies(ORIGIN), [])
        self.shot("signin")

    def sign_in(self, brief):
        with self.page.expect_response(ORIGIN + LOGIN_VERIFY) as verified:
            self.button("Sign in with passkey").tap()
        self.assertEqual(verified.value.status, 200)
        (cookie,) = self.login_cookie_headers
        name, *attributes = cookie.split("; ")
        self.assertTrue(name.startswith(COOKIE_NAME + "="))
        self.assertTrue({"Secure", "HttpOnly", "SameSite=Strict", "Path=/"}.issubset(attributes))
        (stored,) = self.context.cookies(ORIGIN)
        self.assertEqual(stored["name"], COOKIE_NAME)
        self.assertTrue(stored["secure"] and stored["httpOnly"])
        self.assertEqual(stored["sameSite"], "Strict")
        self.expect_tabs()
        self.page.reload()
        self.expect_tabs()
        self.expect(self.page.get_by_role("article", name=brief["ask"], exact=True)).to_be_visible()
        self.shot("dashboard")

    def post_decision(self, question):
        return self.feed.post({
            "kind": "decision", "rank": 3, "ask": question, "why_now": "The fixture is ready.",
            "default": {"action": "none", "at": None},
        })

    def open_decision(self, brief):
        card = self.page.get_by_role("article", name=brief["ask"], exact=True)
        card.get_by_role("button", name=brief["ask"], exact=True).tap()
        return card

    def unlock_from(self, card):
        self.expect_lock(True)
        with self.page.expect_response(ORIGIN + "/m/api/unlock") as unlocked:
            card.get_by_role("button", name="Unlock to approve", exact=True).tap()
        self.assertEqual(unlocked.value.status, 200)
        self.expect_lock(False)

    def send_for_approval(self, text):
        panel = self.page.get_by_role("tabpanel", name="Chat")
        panel.get_by_role("textbox", name="Message").fill(text)
        panel.get_by_role("button", name="Send", exact=True).tap()
        card = panel.get_by_role("region", name="Tool approval")
        self.expect(card.get_by_role("heading", name="Add a vault entry")).to_be_visible()
        badge = self.page.get_by_role("tab", name=re.compile(r"^Chat")).locator(".phone-badge")
        self.expect(badge).to_have_text("1")
        return card

    def answer_tool_cards(self):
        from jarvis.v3.vault import Vault

        self.page.get_by_role("tab", name="Chat", exact=True).tap()
        first = self.send_for_approval("Save the fixture note.")
        approve = first.get_by_role("button", name="Approve", exact=True)
        self.expect(approve).to_be_enabled()  # The card's own arming delay.
        self.shot("approval")
        with self.page.expect_response(ORIGIN + APPROVAL_ANSWER) as approved:
            approve.tap()
        self.assertEqual(approved.value.status, 200)
        self.expect(self.page.get_by_text("Fixture note saved.", exact=True)).to_be_visible()
        vault = Vault(self.data / "vault")
        self.assertEqual(vault.read("facts/fixture-notice").body, VAULT_ENTRY["body"])

        # Lock through the session route, then reload to read the new state.
        self.assertEqual(self.page.evaluate("""async () => {
            const response = await fetch('/m/api/lock', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
            });
            return response.status;
        }"""), 200)
        self.page.reload()
        self.expect_lock(True)
        self.page.get_by_role("tab", name="Chat", exact=True).tap()
        second = self.send_for_approval("Request another fixture note.")
        self.expect(second.get_by_role("button", name="Unlock to approve", exact=True)).to_be_visible()
        with self.page.expect_response(ORIGIN + APPROVAL_ANSWER) as denied:
            second.get_by_role("button", name="Deny", exact=True).tap()
        self.assertEqual(denied.value.status, 200)
        self.expect(self.page.get_by_text("Fixture note declined.", exact=True)).to_be_visible()
        self.expect_lock(True)
        self.assertEqual(list((self.data / "vault").rglob("*.md")), [self.data / "vault/facts/fixture-notice.md"])
        self.assertEqual(self.answers, [(FEED_ANSWER, "approve"), (APPROVAL_ANSWER, "approve"),
                                        (APPROVAL_ANSWER, "deny")])
        self.assertEqual(self.model.steps, [])

    def restart(self):
        brief = self.post_decision("Review the second fixture?")
        self.page.reload()
        self.unlock_from(self.open_decision(brief))
        self.daemon.stop()
        self.start_daemon(self.model)
        self.page.reload()
        self.expect_tabs()
        self.expect_lock(True)

    def sign_out(self):
        (session,) = self.context.cookies(ORIGIN)
        with self.page.expect_response(ORIGIN + "/m/api/logout") as logged_out:
            self.button("Sign out").tap()
        self.assertEqual(logged_out.value.status, 200)
        self.expect(self.button("Sign in with passkey")).to_be_visible()
        self.assertEqual(self.context.cookies(ORIGIN), [])
        # The HTTP probe keeps the intentional 401 out of Chromium's console.
        # Even the former cookie must now be unauthorized by the daemon.
        response = self.context.request.get(f"http://127.0.0.1:{self.daemon.phone_port}/m/api/feed", headers={
            "Host": HOST, "Cookie": f"{COOKIE_NAME}={session['value']}",
        }, max_redirects=0, timeout=WAIT_MS)
        self.assertEqual(response.status, 401)

    def test_pair_sign_in_unlock_approve_restart_and_sign_out(self):
        from jarvis.v3.feed import Feed

        brief = self.post_decision("Schedule the fixture review?")
        self.pair()
        self.sign_in(brief)

        # The first tap unlocks. The feed write requires a separate tap.
        card = self.open_decision(brief)
        self.unlock_from(card)
        self.assertEqual(self.answers, [])
        self.assertEqual(Feed(self.feed.root).get(brief["id"])["state"], "open")
        self.shot("unlocked")
        with self.page.expect_response(ORIGIN + FEED_ANSWER) as approved:
            card.get_by_role("button", name="Approve", exact=True).tap()
        self.assertEqual(approved.value.status, 200)
        recent = self.page.get_by_role("region", name="Recent")
        self.expect(recent.get_by_role("article", name=brief["ask"]).get_by_text("Approved", exact=True)).to_be_visible()
        self.assertEqual(Feed(self.feed.root).get(brief["id"])["state"], "approved")

        self.answer_tool_cards()
        self.restart()
        self.sign_out()


if __name__ == "__main__":
    unittest.main(verbosity=2)
