"""Drive the built v3 window against a real, isolated v3 daemon.

Build with `cd hud3 && npm ci --ignore-scripts && npm run build`.
When running with a temporary HOME, set PLAYWRIGHT_BROWSERS_PATH to the
installed Playwright browser cache if it lives outside that HOME.
"""

import json
from datetime import datetime, timezone
import os
from pathlib import Path
import re
from secrets import token_urlsafe
import tempfile
from threading import Event
import unittest
from unittest.mock import patch


DIST = Path(__file__).resolve().parents[2] / "hud3" / "dist"
WAIT_MS = 8_000
APPROVALS = "/v3/owner/approvals"
APPROVAL_ANSWER = APPROVALS + "/answer"
CSP_DIRECTIVES = {
    "frame-ancestors": ["'none'"], "default-src": ["'none'"], "script-src": ["'self'"],
    "worker-src": ["'self'"], "connect-src": ["'self'"], "style-src": ["'self'", "'unsafe-inline'"],
    "img-src": ["'self'", "data:"], "font-src": ["'self'", "data:"], "manifest-src": ["'self'"],
    "base-uri": ["'none'"], "form-action": ["'none'"], "object-src": ["'none'"],
}
VAULT_ENTRY = {
    "folder": "facts",
    "name": "fixture-notice",
    "description": "A note for the browser test",
    "body": (
        "The first line of the fixture note is deliberately long enough that the card's "
        "compact preview cannot show the entire proposed write.\n"
        "The second line must be stored exactly as shown in Details."
    ),
    "source": "browser test",
}
HOSTILE_REF = "javascript:void(0)"
HOSTILE_TEXT = (
    """<img src=x onerror="console.error('fixture markup executed')"> """
    f'<a href="{HOSTILE_REF}">fixture link</a>'
)


class Window(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (DIST / "index.html").is_file():
            raise unittest.SkipTest("hud3/dist is missing. Build it with npm run build in hud3.")
        try:
            from playwright.sync_api import Error, sync_playwright
        except ImportError:
            raise unittest.SkipTest("Playwright is not installed.") from None

        # Preserve access to the browser binary while isolating the browser profile
        # and Jarvis config from the invoking user's HOME.
        browser_cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        home = Path(cls.enterClassContext(tempfile.TemporaryDirectory())).resolve()
        cls.enterClassContext(patch.dict(os.environ, {
            "HOME": str(home),
            "XDG_CACHE_HOME": str(home / "cache"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_RUNTIME_DIR": str(home),
            "PLAYWRIGHT_BROWSERS_PATH": os.environ.get(
                "PLAYWRIGHT_BROWSERS_PATH", str(browser_cache / "ms-playwright")
            ),
        }))
        playwright = cls.enterClassContext(sync_playwright())
        try:
            cls.browser = playwright.chromium.launch(
                headless=True, timeout=15_000,
                args=["--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"],
            )
        except Error as exc:
            if any(reason in str(exc) for reason in (
                "Executable doesn't exist", "Host system is missing dependencies",
                "error while loading shared libraries",
            )):
                raise unittest.SkipTest(
                    "Playwright Chromium is unavailable. Run playwright install chromium "
                    "and install its system dependencies."
                ) from None
            raise
        cls.addClassCleanup(cls.browser.close)

        from jarvis.v2 import hud_install

        # hud_dir takes an installed build store. Publish the built bytes to a
        # throwaway store so the daemon uses its normal static-file verifier.
        cls.hud_dir = home / "hud3"
        hud_install.install(DIST, cls.hud_dir)

    def setUp(self):
        from playwright.sync_api import expect

        from a6b1_acceptance_check import reply
        from jarvis import config, llm, sessions
        from jarvis.v3.feed import Feed
        from jarvis.v3.vault import Vault

        self.expect = expect
        self.reply = reply
        self.data = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.dict(os.environ, {
            "JARVIS_VAULT": str(self.data / "vault"),
            "JARVIS_INDEX_DB": str(self.data / "index.sqlite"),
        }))
        self.enterContext(patch.multiple(
            config, V3_DIR=self.data, V3_POLICY_FILE=self.data / "policy.json",
            GOOGLE_TOKEN_PATH=self.data / "no-google-token.json",
        ))
        self.enterContext(patch.object(sessions, "AUTO_TITLE", False))
        model = self.enterContext(patch.object(llm, "chat", side_effect=AssertionError("live model call")))
        self.addCleanup(model.assert_not_called)
        self.feed = Feed(self.data / "feed")
        self.vault = Vault(self.data / "vault")
        self.approval_log = self.data / "approvals.jsonl"

    def start_daemon(self, *steps, approval_timeout=120, approval_clock=None):
        from a6b1_acceptance_check import Model
        from a6c1_acceptance_check import Cal, NOW
        from jarvis.v3.approvals import ApprovalBroker
        from jarvis.v3.chat import ChatService
        from jarvis.v3.daemon import Daemon
        from jarvis.v3.tools.calendar import CalendarTools

        self.model = Model(*steps)
        calendar = Cal()
        self.addCleanup(self.assertEqual, calendar.calls, [])
        self.approval_started = Event()

        def approval_id():
            self.approval_started.set()
            return token_urlsafe()

        clock = {} if approval_clock is None else {"clock": approval_clock}
        self.broker = ApprovalBroker(timeout_s=approval_timeout, log=self.approval_log,
                                     new_id=approval_id, **clock)
        self.addCleanup(self.broker.shutdown)
        self.chat = ChatService(
            self.data / "chats", completion=self.model, approvals=self.broker,
            calendar=lambda approve=None: CalendarTools(request=calendar, now=lambda: NOW, approve=approve),
        )
        daemon = Daemon(port=0, data_dir=self.data, hud_dir=self.hud_dir, jobs=[], chat=self.chat)
        daemon.start()
        self.addCleanup(daemon.stop)
        self.origin = f"http://127.0.0.1:{daemon.port}"

    def open_window(self, *, pause_clock=False):
        errors = []
        self.addCleanup(self.assertEqual, errors, [], "Browser errors or CSP violations")
        context = self.browser.new_context(
            viewport={"width": 1440, "height": 1200}, service_workers="block",
        )
        self.addCleanup(context.close)
        context.set_default_timeout(WAIT_MS)

        def locked_to_daemon(route):
            if route.request.url.startswith(self.origin + "/"):
                route.continue_()
            else:
                errors.append(f"Unexpected browser request: {route.request.url}")
                route.abort()

        def block_websocket(socket):
            errors.append(f"Unexpected WebSocket: {socket.url}")
            socket.close()

        context.route("**/*", locked_to_daemon)
        context.route_web_socket("**/*", block_websocket)
        context.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        context.on("weberror", lambda error: errors.append(str(error.error)))
        context.add_init_script("""(() => {
            document.addEventListener("securitypolicyviolation", (event) => {
                console.error("CSP", event.effectiveDirective, event.blockedURI);
            });
            window.e2eFocus = [];
            document.addEventListener("focusin", (event) => {
                window.e2eFocus.push(event.target.tagName);
            });
            window.e2eApproveClicks = [];
            document.addEventListener("click", (event) => {
                if (event.target.matches?.(".approval-approve")) {
                    window.e2eApproveClicks.push(event.isTrusted);
                }
            }, true);
        })()""")
        self.page = context.new_page()
        if pause_clock:
            self.page.clock.install(time=datetime(2030, 1, 1, tzinfo=timezone.utc))
        self.approval_answers = []
        self.page.on("request", self.observe_request)
        response = self.page.goto(self.origin + "/", wait_until="load")
        self.assertEqual(response.status, 200)
        directives = [part.split() for part in response.headers["content-security-policy"].split(";")]
        self.assertEqual(len(directives), len(CSP_DIRECTIVES))
        self.assertEqual({name: values for name, *values in directives}, CSP_DIRECTIVES)
        self.chat_panel = self.page.get_by_role("region", name="Chat", exact=True)
        self.transcript = self.chat_panel.get_by_role("region", name="Chat transcript")
        self.composer = self.chat_panel.get_by_role("textbox", name="Message")
        self.feed_panel = self.page.get_by_role("region", name="Feed", exact=True)
        self.expect(self.composer).to_be_visible(timeout=WAIT_MS)
        self.expect(self.chat_panel.get_by_role("button", name="New chat")).to_be_enabled(timeout=WAIT_MS)
        if pause_clock:
            self.page.clock.pause_at(datetime(2030, 1, 2, tzinfo=timezone.utc))

    def observe_request(self, request):
        if request.method == "POST" and request.url == self.origin + APPROVAL_ANSWER:
            self.approval_answers.append(request.post_data_json["decision"])

    def send(self, text):
        self.composer.click(force=True)
        self.expect(self.composer).to_be_focused()
        self.page.keyboard.insert_text(text)
        self.page.keyboard.press("Enter")
        self.expect(self.composer).to_have_value("", timeout=WAIT_MS)

    def expect_idle(self):
        self.expect(self.chat_panel.get_by_role("button", name="New chat")).to_be_enabled(timeout=WAIT_MS)
        self.expect(self.chat_panel.get_by_role("button", name="Stop")).to_have_count(0)
        self.assertEqual(self.chat.snapshot()["state"], "idle")

    def request_vault_add(self, response, *, pause_clock=False, **kwargs):
        self.start_daemon(
            self.reply(calls=[("vault_add", VAULT_ENTRY)]), self.reply(response), **kwargs,
        )
        self.open_window(pause_clock=pause_clock)
        self.send("Save the fixture note.")
        if pause_clock:
            # The broker calls new_id with its lock held. pending() takes
            # that lock after the request is entered and the tool is waiting.
            self.assertTrue(self.approval_started.wait(5))
            self.assertEqual(len(self.broker.pending()), 1)
            with self.page.expect_response(self.origin + APPROVALS) as listed:
                self.page.clock.run_for(700)
            self.assertTrue(listed.value.ok)
            self.assertEqual(len(listed.value.json()["approvals"]), 1)
        card = self.chat_panel.get_by_role("region", name="Tool approval")
        self.expect(card.get_by_role("heading", name="Add a vault entry")).to_be_visible(timeout=WAIT_MS)
        return card

    def expect_pending_without_write(self):
        with self.page.expect_request(self.origin + APPROVALS) as next_poll:
            pass
        response = next_poll.value.response()
        self.assertTrue(response.ok)
        (listed,) = response.json()["approvals"]
        self.assertEqual([row["id"] for row in self.broker.pending()], [listed["id"]])
        self.assertEqual(self.approval_answers, [])
        self.assertEqual(list((self.data / "vault").rglob("*.md")), [])

    def approval_states(self):
        return [json.loads(line)["state"] for line in self.approval_log.read_text().splitlines()]

    def test_shell_shows_every_slot_without_taking_focus(self):
        self.start_daemon()
        self.open_window()
        self.expect(self.page.get_by_role("heading", level=1, name="J.A.R.V.I.S. Agent")).to_be_visible()
        for title in ("T3", "Chat", "Feed", "Vault", "Skills", "Cron", "Status"):
            panel = self.page.get_by_role("region", name=title, exact=True)
            self.expect(panel.get_by_role("heading", level=2, name=title)).to_be_visible()
        self.expect(self.page.locator("canvas[data-orb]")).to_be_visible()
        self.expect(self.feed_panel.get_by_text("No open briefs.", exact=True)).to_be_visible()
        status = self.page.get_by_role("region", name="Status", exact=True)
        self.expect(status.get_by_role("status")).to_have_text("Running")
        self.expect(status.get_by_text(str(os.getpid()), exact=True)).to_be_visible()
        # Observe the startup focus log through the next idle chat poll.
        with self.page.expect_request(self.origin + "/v3/owner/chat") as next_poll:
            pass
        response = next_poll.value.response()
        self.assertTrue(response.ok)
        self.assertEqual(response.json()["state"], "idle")
        self.assertTrue(self.page.evaluate("document.activeElement === document.body"))
        self.assertEqual(self.page.evaluate("window.e2eFocus"), [])

    def test_chat_tool_reply_steering_and_stop(self):
        entered, release = Event(), Event()

        def held_reply(_):
            entered.set()
            if not release.wait(25):
                raise TimeoutError("The browser did not release the held completion")
            return self.reply("This reply should be stopped.")

        self.start_daemon(
            self.reply(calls=[("vault_list", {})]), self.reply("The fixture vault is empty."),
            held_reply,
        )
        self.addCleanup(release.set)
        self.open_window()
        self.send("List the fixture vault.")
        rows = self.transcript.locator(".chat-text, .chat-tools li")
        first_turn = ["List the fixture vault.", re.compile(r"^vault_list · OK"), "The fixture vault is empty."]
        self.expect(rows).to_have_text(first_turn, timeout=WAIT_MS)
        self.expect_idle()

        self.send("Hold this turn open.")
        self.assertTrue(entered.wait(5), "The fake completion did not start")
        self.expect(self.composer).to_have_attribute("placeholder", "Add to this turn")
        self.send("Add this fixture detail.")
        steered = self.transcript.get_by_role("article").filter(has_text="Add this fixture detail.")
        self.expect(steered.get_by_text("· Added to this turn")).to_be_visible(timeout=WAIT_MS)
        self.expect(rows).to_have_text([*first_turn, "Hold this turn open.", "Add this fixture detail."])

        with self.page.expect_response(self.origin + "/v3/owner/chat/stop") as stopped:
            self.chat_panel.get_by_role("button", name="Stop").click()
        self.assertEqual(stopped.value.status, 200)
        self.assertEqual(stopped.value.json(), {"state": "stopping"})
        release.set()
        self.expect(self.chat_panel.get_by_text("Turn stopped.", exact=True)).to_be_visible(timeout=WAIT_MS)
        self.expect_idle()
        self.expect(self.transcript.get_by_text("This reply should be stopped.")).to_have_count(0)
        self.expect(steered.get_by_text("· Discarded before delivery")).to_be_visible()
        self.expect(steered.get_by_text("· Added to this turn")).to_have_count(0)

    def test_approve_writes_the_full_body_shown_in_chat(self):
        card = self.request_vault_add("Fixture note saved.")
        target = f"{VAULT_ENTRY['folder']}/{VAULT_ENTRY['name']}"
        self.expect(card.locator(".approval-target dd").first).to_have_text(target)
        self.expect(self.composer).to_be_focused()

        # A fresh window must show the specific active prompt and running call.
        self.page.reload(wait_until="load")
        self.expect(card.locator(".approval-target dd").first).to_have_text(target, timeout=WAIT_MS)
        self.expect(self.transcript.locator(".chat-text, .chat-tools li")).to_have_text(
            ["Save the fixture note.", "vault_add · Running"], timeout=WAIT_MS,
        )

        card.get_by_text("Details", exact=True).click()
        body = card.locator("details .approval-field").filter(
            has=self.page.get_by_text("Body", exact=True)
        ).locator("dd")
        self.expect(body).to_be_visible()
        shown_body = body.text_content()
        self.assertEqual(shown_body, VAULT_ENTRY["body"])
        self.expect_pending_without_write()

        approve = card.get_by_role("button", name="Approve")
        self.expect(approve).to_be_enabled(timeout=WAIT_MS)
        approve.click()
        self.expect(self.transcript.get_by_text("Fixture note saved.", exact=True)).to_be_visible(timeout=WAIT_MS)
        self.expect_idle()
        self.expect(card).to_have_count(0)
        self.assertEqual(self.vault.read(target).body, shown_body)
        self.assertEqual(self.approval_answers, ["approve"])
        self.assertEqual(self.approval_states(), ["pending", "approved"])

    def test_approval_guards_and_denial(self):
        card = self.request_vault_add("Fixture note declined.", pause_clock=True)
        approve = card.get_by_role("button", name="Approve")
        # The card is verified and only the arming timer is still pending.
        self.expect(card.get_by_role("button", name="Deny")).to_be_enabled()
        self.page.clock.run_for(599)
        self.expect(approve).to_be_disabled()
        box = approve.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        self.page.mouse.move(x, y)
        self.assertTrue(self.page.evaluate(
            "([x, y]) => document.elementFromPoint(x, y)?.matches('button.approval-approve:disabled')",
            [x, y],
        ))
        self.page.mouse.down()
        self.page.clock.run_for(1)
        self.expect(approve).to_be_enabled(timeout=WAIT_MS)
        self.assertTrue(self.page.evaluate(
            "([x, y]) => document.elementFromPoint(x, y)?.matches('button.approval-approve:enabled')",
            [x, y],
        ))
        self.page.mouse.up()
        self.assertEqual(self.page.evaluate("window.e2eApproveClicks"), [True])
        self.page.clock.resume()
        self.expect_pending_without_write()

        self.send("Keep this message in the composer flow.")
        steered = self.transcript.get_by_role("article").filter(has_text="Keep this message in the composer flow.")
        self.expect(steered.get_by_text("· Added to this turn")).to_be_visible(timeout=WAIT_MS)
        self.expect_pending_without_write()
        self.page.get_by_role("heading", level=1).click()
        self.assertTrue(self.page.evaluate("document.activeElement === document.body"))
        self.page.keyboard.press("Enter")
        self.expect_pending_without_write()

        card.get_by_role("button", name="Deny").click()
        self.expect(self.transcript.get_by_text("Fixture note declined.", exact=True)).to_be_visible(timeout=WAIT_MS)
        self.expect_idle()
        self.expect(card).to_have_count(0)
        results = [row["content"] for row in self.model.seen[-1]["messages"] if row["role"] == "tool"]
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].startswith("The user declined"), results)
        self.assertEqual(self.approval_answers, ["deny"])
        self.assertEqual(self.approval_states(), ["pending", "denied"])
        self.assertEqual(list((self.data / "vault").rglob("*.md")), [])

    def test_expired_approval_loses_its_buttons(self):
        now = 100
        card = self.request_vault_add("Fixture approval expired.", pause_clock=True,
                                      approval_clock=lambda: now)
        self.expect(card.get_by_role("button", name="Deny")).to_be_visible()
        self.page.clock.run_for(600)
        self.expect(card.get_by_role("button", name="Approve")).to_be_enabled(timeout=WAIT_MS)
        self.assertEqual(len(self.broker.pending()), 1)
        now += 120
        self.page.clock.fast_forward(120_000)
        self.page.clock.resume()
        self.expect(card.get_by_text("Approval expired.", exact=True)).to_be_visible(timeout=WAIT_MS)
        self.expect(card.get_by_role("button")).to_have_count(0)
        self.expect(self.transcript.get_by_text("Fixture approval expired.", exact=True)).to_be_visible(timeout=WAIT_MS)
        self.expect_idle()
        self.assertEqual(self.approval_answers, [])
        self.assertEqual(self.approval_states(), ["pending", "expired"])
        self.assertEqual(list((self.data / "vault").rglob("*.md")), [])

    def test_feed_is_ranked_and_approval_persists(self):
        from jarvis.v3.feed import Feed

        self.start_daemon()
        news = "The fixture build finished."
        decision = "Schedule the fixture review?"
        self.feed.post({"kind": "news", "rank": 6, "ask": news, "why_now": "<em>Build passed</em>"})
        brief = self.feed.post({
            "kind": "decision", "rank": 3, "ask": decision, "why_now": "The fixture is ready.",
            "default": {"action": "none", "at": None},
        })
        self.open_window()
        open_list = self.feed_panel.locator(":scope > .feed-list")
        self.expect(open_list.get_by_role("heading")).to_have_text([decision, news], timeout=WAIT_MS)
        news_card = open_list.get_by_role("article", name=news)
        self.expect(news_card.get_by_text("<em>Build passed</em>", exact=True)).to_be_visible()
        self.expect(news_card.locator("em")).to_have_count(0)
        decision_card = open_list.get_by_role("article", name=decision)
        self.expect(decision_card.get_by_text("The fixture is ready.", exact=True)).to_be_visible()

        # A brief is a closed row until it is opened: no answer is offered before then.
        self.expect(decision_card.get_by_role("button", name="Approve")).to_have_count(0)
        decision_card.get_by_role("button", name=decision).click()
        decision_card.get_by_role("button", name="Approve").click()
        self.expect(open_list.get_by_role("heading")).to_have_text([news], timeout=WAIT_MS)
        recent = self.feed_panel.get_by_role("region", name="Recent")
        self.expect(recent.get_by_role("article", name=decision).get_by_text("Approved", exact=True)).to_be_visible()
        self.assertEqual(Feed(self.feed.root).get(brief["id"])["state"], "approved")

    def test_hostile_reply_and_brief_are_literal_text(self):
        self.start_daemon(self.reply(HOSTILE_TEXT))
        self.feed.post({
            "kind": "news", "rank": 5, "ask": "Fixture markup sample", "why_now": HOSTILE_TEXT,
            "context": [{"label": "Fixture reference", "ref": HOSTILE_REF}],
        })
        self.open_window()
        self.send("Show the fixture markup.")
        self.expect(self.transcript.get_by_text(HOSTILE_TEXT, exact=True)).to_be_visible(timeout=WAIT_MS)
        self.expect(self.transcript.locator("img, a")).to_have_count(0)
        self.expect_idle()

        brief = self.feed_panel.get_by_role("article", name="Fixture markup sample")
        self.expect(brief.get_by_text(HOSTILE_TEXT, exact=True)).to_be_visible(timeout=WAIT_MS)
        brief.get_by_role("button", name="Fixture markup sample").click()
        brief.get_by_text("Context", exact=True).click()
        self.expect(brief.get_by_text(HOSTILE_REF, exact=True)).to_be_visible()
        self.expect(brief.locator("img, a")).to_have_count(0)


if __name__ == "__main__":
    unittest.main()
