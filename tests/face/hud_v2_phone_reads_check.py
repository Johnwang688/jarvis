"""Phone resource refreshes through the built app; fake network, no listener or owner state."""
from __future__ import annotations

import mimetypes
from pathlib import Path
import re
import unittest

from playwright.sync_api import expect, sync_playwright

REPO = Path(__file__).resolve().parents[2]
HOST = "phone-reads.tail-test.ts.net"
TASK, PROJECT, THREAD = "11111111", "22222222", "33333333"


class PhoneReadsFace(unittest.TestCase):
    def test_detail_and_list_warnings_at_owner_widths_and_large_text(self):
        from jarvis.v2.remote_listener import SECURITY_HEADERS
        dist = REPO / "hud/dist"
        self.assertTrue((dist / "mobile.html").is_file(), "build hud first")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                for width in (375, 390):
                    for scale in (1, 1.5):
                        with self.subTest(width=width, scale=scale):
                            task = {"task": {"id": TASK, "project_id": PROJECT, "brief": "Retained task brief", "state": "running"}}
                            replies = {
                                "/m/api/me": {"signed_in": True, "secure": {"ok": True}},
                                "/m/api/inbox": {"rows": []},
                                "/m/api/working": {"entries": [], "total": 0},
                                "/m/api/tasks": {"tasks": [task["task"]]},
                                f"/m/api/tasks/{TASK}": task,
                                f"/m/api/tasks/{TASK}/diff": {"files": [{"path": "sample.txt", "patch": "retained patch"}]},
                                "/m/api/review": {"v": 1, "overnight": [], "before": [], "open": 0},
                                "/m/api/projects": {"projects": [{"id": PROJECT, "name": "Retained project"}]},
                                "/m/api/threads": {"threads": [{"id": THREAD, "project_id": PROJECT, "title": "Retained chat"}]},
                            }
                            ctx = browser.new_context(viewport={"width": width, "height": 844}, ignore_https_errors=True)
                            ctx.add_init_script("""window.EventSource = class {
                              readyState = 1;
                              constructor() { window.readStream = this; queueMicrotask(() => this.onopen?.({})); }
                              close() {} };""")

                            def serve(route):
                                path = route.request.url.split(HOST, 1)[1].split("?", 1)[0]
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
                                page.get_by_test_id("m-chip-tasks").click()
                                page.get_by_test_id("m-task").click()
                                expect(page.get_by_test_id("m-task-brief")).to_have_text("Retained task brief")
                                page.get_by_test_id("m-show-diff").click()
                                page.get_by_test_id("m-diff-file").wait_for()
                                base = page.locator("html").evaluate("e => parseFloat(getComputedStyle(e).fontSize)")
                                page.locator("html").evaluate("(e, size) => e.style.fontSize = size + 'px'", base * scale)

                                def refresh():
                                    page.evaluate("window.readStream.onmessage({data: JSON.stringify({kind: 'resync'})})")

                                def warning(resource):
                                    return page.locator(f'.m-resource-warning[data-resource="{resource}"]')

                                def fits(note):
                                    box = note.bounding_box()
                                    self.assertIsNotNone(box)
                                    self.assertGreaterEqual(box["x"], 0)
                                    self.assertLessEqual(box["x"] + box["width"], width + 0.5)
                                    self.assertTrue(note.evaluate("e => e.scrollWidth <= e.clientWidth + 1"))

                                replies[f"/m/api/tasks/{TASK}"] = {}
                                refresh()
                                expect(warning(f"tasks/{TASK}")).to_contain_text("showing the last read")
                                expect(page.get_by_test_id("m-task-brief")).to_have_text("Retained task brief")
                                fits(warning(f"tasks/{TASK}"))

                                replies[f"/m/api/tasks/{TASK}"] = task
                                replies[f"/m/api/tasks/{TASK}/diff"] = {}
                                refresh()
                                expect(warning(f"tasks/{TASK}/diff")).to_contain_text("showing the last read")
                                expect(warning(f"tasks/{TASK}")).to_have_count(0)
                                self.assertIn("retained patch", page.get_by_test_id("m-diff").text_content())
                                fits(warning(f"tasks/{TASK}/diff"))

                                page.get_by_test_id("m-tab-chats").click()
                                page.get_by_test_id("m-project-head").wait_for()
                                replies["/m/api/projects"] = {}
                                replies["/m/api/threads"] = {}
                                refresh()
                                expect(warning("projects")).to_contain_text("showing the last read")
                                expect(warning("threads")).to_contain_text("showing the last read")
                                self.assertIn("Retained project", page.get_by_test_id("m-chats").text_content())
                                self.assertIn("Retained chat", page.get_by_test_id("m-chats").text_content())
                                fits(warning("projects"))
                                fits(warning("threads"))
                                replies["/m/api/projects"] = {"projects": []}
                                replies["/m/api/threads"] = {"threads": []}
                                refresh()
                                expect(warning("projects")).to_have_count(0)
                                expect(warning("threads")).to_have_count(0)
                                expect(page.get_by_test_id("m-project-head")).to_have_count(0)
                                self.assertFalse(errors, errors)
                                self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                                print(f"PASS {width}px, {scale:.0%}: task/diff/list retention, warnings, recovery, geometry")
                            finally:
                                ctx.close()
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
