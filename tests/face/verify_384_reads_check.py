"""Independent PR 384 browser controls and a failing provider-read counterexample.

Fake HTTPS responses and EventSource only; no listeners or live configuration.
"""
from __future__ import annotations

from contextlib import contextmanager
import mimetypes
from pathlib import Path
import re
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

REPO = Path(__file__).resolve().parents[2]
HOST = "verify384.tail-test.ts.net"
A, B, PROJECT, TASK = "aaaaaaaa", "bbbbbbbb", "cccccccc", "11111111"


class Verify384Face(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    @contextmanager
    def phone(self, width, scale):
        from jarvis.v2.remote_listener import SECURITY_HEADERS
        dist = REPO / "hud/dist"
        self.assertTrue((dist / "mobile.html").is_file())
        row = {"id": "q:retained", "kind": "question", "rank": 3, "title": "Retained review question",
               "detail": {"text": "Choose", "options": []}}
        report = {"v": 1, "overnight": [], "since": [{"row": row}], "before": [], "live": [], "open": 1}
        replies = {
            "/m/api/me": {"signed_in": True, "secure": {"ok": True}},
            "/m/api/inbox": {"rows": []}, "/m/api/working": {"entries": [], "total": 0},
            "/m/api/review": report,
            "/m/api/tasks": {"tasks": [{"id": TASK, "project_id": PROJECT, "brief": "Retained task", "state": "running"}]},
            f"/m/api/tasks/{TASK}": {"task": {"id": TASK, "project_id": PROJECT, "brief": "Retained task", "state": "running"}},
            "/m/api/projects": {"projects": [{"id": PROJECT, "name": "Project"}]},
            "/m/api/threads": {"threads": [{"id": tid, "project_id": PROJECT, "title": title}
                                            for tid, title in ((A, "Chat A"), (B, "Chat B"))]},
            f"/m/api/threads/{A}/log": {"rows": [{"seq": 9, "role": "Claude", "text": "Fresh A answer"}], "next": 10},
            f"/m/api/threads/{B}/log": {"rows": [], "next": 0},
            "/m/api/thread-models": {"providers": {"fast": {"label": "Available provider", "available": True,
                                                          "models": [{"id": "example-model"}]}}},
        }
        held, hold = [], {"next_a": False}
        ctx = self.browser.new_context(viewport={"width": width, "height": 844}, ignore_https_errors=True)
        ctx.add_init_script("""window.EventSource = class {
          readyState = 1; constructor() { window.verifyStream = this; queueMicrotask(() => this.onopen?.({})); }
          close() {} };""")

        def serve(route):
            path = urlsplit(route.request.url).path
            if path == f"/m/api/threads/{A}/log" and hold["next_a"]:
                hold["next_a"] = False
                held.append(route)
                return
            if path in replies:
                return route.fulfill(json=replies[path])
            if path.startswith("/m/api/"):
                return route.fulfill(status=404, json={"error": "not found"})
            target = (dist / ("mobile.html" if path == "/m/" else path.removeprefix("/m/"))).resolve()
            if dist.resolve() not in target.parents or not target.is_file():
                return route.fulfill(status=404)
            headers = dict(SECURITY_HEADERS)
            headers["Content-Type"] = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return route.fulfill(headers=headers, body=target.read_bytes())

        ctx.route(re.compile(".*"), lambda route: route.abort())
        ctx.route(re.compile(r"^https://" + re.escape(HOST) + "/"), serve)
        page = ctx.new_page()
        page.set_default_timeout(5000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(f"https://{HOST}/m/")
            page.get_by_test_id("m-strip").wait_for()
            base = page.locator("html").evaluate("e => parseFloat(getComputedStyle(e).fontSize)")
            page.locator("html").evaluate("(e, size) => e.style.fontSize = size + 'px'", base * scale)
            yield page, replies, held, hold
            self.assertFalse(errors, errors)
            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        finally:
            ctx.close()

    def test_requested_fixes_at_owner_widths_and_fonts(self):
        for width in (375, 390):
            for scale in (1.25, 1.5):
                with self.subTest(width=width, scale=scale), self.phone(width, scale) as (page, replies, held, hold):
                    page.get_by_test_id("m-review-open").click()
                    expect(page.get_by_test_id("m-review")).to_contain_text("Retained review question")
                    valid = replies["/m/api/review"]
                    for section in ("since", "live"):
                        replies["/m/api/review"] = {**valid, section: {}}
                        page.evaluate("window.verifyStream.onmessage({data: JSON.stringify({kind:'resync'})})")
                        warning = page.locator('.m-resource-warning[data-resource="review"]')
                        expect(warning).to_be_visible()
                        expect(page.get_by_test_id("m-review")).to_contain_text("Retained review question")
                        replies["/m/api/review"] = valid
                        page.evaluate("window.verifyStream.onmessage({data: JSON.stringify({kind:'resync'})})")
                        expect(warning).to_have_count(0)

                    page.get_by_test_id("m-tab-inbox").click()
                    page.get_by_test_id("m-chip-tasks").click()
                    page.get_by_test_id("m-task").click()
                    expect(page.get_by_test_id("m-task-brief")).to_have_text("Retained task")
                    replies[f"/m/api/tasks/{TASK}"] = {}
                    page.evaluate("window.verifyStream.onmessage({data: JSON.stringify({kind:'resync'})})")
                    expect(page.locator(f'.m-resource-warning[data-resource="tasks/{TASK}"]')).to_be_visible()
                    expect(page.get_by_test_id("m-task-brief")).to_have_text("Retained task")

                    page.get_by_test_id("m-tab-chats").click()
                    project = page.get_by_test_id("m-project-head")
                    if project.get_attribute("aria-expanded") == "false":
                        project.click()
                    hold["next_a"] = True
                    page.get_by_test_id("m-chat").filter(has_text="Chat A").click()
                    page.wait_for_timeout(50)
                    self.assertEqual(len(held), 1)
                    page.get_by_test_id("m-back").click()
                    page.get_by_test_id("m-chat").filter(has_text="Chat B").click()
                    page.get_by_test_id("m-transcript").wait_for()
                    page.get_by_test_id("m-back").click()
                    page.get_by_test_id("m-chat").filter(has_text="Chat A").click()
                    expect(page.get_by_test_id("m-transcript")).to_contain_text("Fresh A answer")
                    held.pop().fulfill(json={"rows": [{"seq": 1, "role": "Claude", "text": "Obsolete A answer"}], "next": 2})
                    page.wait_for_timeout(100)
                    expect(page.get_by_test_id("m-transcript")).to_contain_text("Fresh A answer")
                    expect(page.get_by_test_id("m-transcript")).not_to_contain_text("Obsolete A answer")
                    print(f"PASS {width}px {scale:.0%}: optional Review shape, task retention, A-B-A late reply")

    def test_thread_models_malformed_200_keeps_last_read(self):
        for width in (375, 390):
            for scale in (1.25, 1.5):
                with self.subTest(width=width, scale=scale), self.phone(width, scale) as (page, replies, _, __):
                    page.get_by_test_id("m-tab-chats").click()
                    page.get_by_test_id("m-new-chat-open").click()
                    expect(page.get_by_test_id("m-new-provider")).to_contain_text("Available provider")
                    page.get_by_test_id("m-sheet-close").click()
                    replies["/m/api/thread-models"] = {"providers": []}
                    with page.expect_response(lambda response: urlsplit(response.url).path == "/m/api/thread-models"):
                        page.get_by_test_id("m-new-chat-open").click()
                    page.wait_for_timeout(100)
                    expect(page.get_by_test_id("m-new-provider")).to_contain_text("Available provider", timeout=1000)


if __name__ == "__main__":
    unittest.main()
