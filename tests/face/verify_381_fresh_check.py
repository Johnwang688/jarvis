"""Fresh #381 controls: SVG slots and stale-warning geometry at owner text sizes.

Fake network only; no listeners, daemon, credentials or live configuration.
"""
from __future__ import annotations

import ast
import mimetypes
from pathlib import Path
import re
import unittest

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[2]
HOST = "verify381.tail-test.ts.net"


class Fresh381Face(unittest.TestCase):
    def test_all_three_real_slots_and_forged_project_icons(self):
        source = REPO / "tests/face/hud_v2_phone_write_check.py"
        guard = next(n for n in ast.walk(ast.parse(source.read_text()))
                     if isinstance(n, ast.FunctionDef) and n.name == "clean")
        answer = ast.parse(source.with_name("hud_v2_phone_answer_check.py").read_text())
        markup = next(n.value.value for n in answer.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "MARKUP" for t in n.targets))
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                scope = {"page": page, "MARKUP": markup}
                exec(compile(ast.Module(body=[guard], type_ignores=[]), str(source), "exec"), scope)
                icon = '<svg class="m-icon" aria-hidden="true"><path/></svg>'
                project = ('<section class="project"><h2 class="m-project-heading">'
                           '<button data-testid="m-project-head">' + icon
                           + '<span class="m-project-name">Project</span></button></h2></section>')
                shell = ('<main id="app" class="app-root"><div class="app">'
                         '<header class="top"><button data-testid="m-brand"><span class="m-orb">'
                         + icon + '</span></button><div class="m-menu"><button data-testid="m-lock-pill">'
                         + icon + '</button></div></header><main class="main">'
                         '<section class="view" data-testid="m-chats">' + project + '</section></main></div></main>')
                page.set_content(shell)
                self.assertTrue(scope["clean"](), "all three real slots must pass")
                for region in ("m-project-name", "m-thread-project", "m-thread-provider", "m-limited", "tool", "m-error"):
                    with self.subTest(region=region):
                        page.set_content(shell)
                        page.locator(".project").evaluate("(e, cls) => e.classList.add(cls)", region)
                        self.assertFalse(scope["clean"](), "server-text ancestry overrides the new project slot")
                        page.set_content(shell)
                        page.locator(".m-project-name").evaluate("(e, html) => e.innerHTML = html", project)
                        self.assertFalse(scope["clean"](), "a complete forged project slot inside text must fail")
                page.set_content(shell)
                page.locator('[data-testid="m-project-head"] > svg').evaluate("e => e.setAttribute('aria-hidden', 'false')")
                self.assertFalse(scope["clean"]())
            finally:
                browser.close()

    def test_owner_widths_and_larger_root_fonts(self):
        from jarvis.v2.remote_listener import SECURITY_HEADERS
        dist = REPO / "hud/dist"
        self.assertTrue((dist / "mobile.html").is_file(), "build hud first")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                for width in (375, 390):
                    for scale in (1.25, 1.5):
                        with self.subTest(width=width, root_scale=scale):
                            ctx = browser.new_context(viewport={"width": width, "height": 844}, ignore_https_errors=True)
                            ctx.add_init_script("""window.EventSource = class {
                              readyState = 1; constructor() { queueMicrotask(() => this.onopen?.({})); }
                              close() {} };""")
                            data = {"body": {"rows": []}}

                            def serve(route):
                                path = route.request.url.split(HOST, 1)[1].split("?", 1)[0]
                                replies = {
                                    "/m/api/me": {"signed_in": True, "secure": {"ok": True}},
                                    "/m/api/inbox": data["body"],
                                    "/m/api/working": {"entries": [], "total": 0},
                                    "/m/api/tasks": {"tasks": []},
                                    "/m/api/review": {"v": 1, "overnight": [], "before": [], "open": 0},
                                    "/m/api/projects": {"projects": [{"id": "cccccccc", "name": "Project"}]},
                                    "/m/api/threads": {"threads": []},
                                }
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
                            errors = []
                            page.on("pageerror", lambda error: errors.append(str(error)))
                            page.goto(f"https://{HOST}/m/")
                            page.get_by_test_id("m-strip").wait_for()
                            base = page.locator("html").evaluate("e => parseFloat(getComputedStyle(e).fontSize)")
                            page.locator("html").evaluate("(e, size) => e.style.fontSize = size + 'px'", base * scale)
                            # Partial data arrives through the actual fetch/decode/apply/render path.
                            data["body"] = {"rows": [{"id": "q:new", "kind": "question", "rank": 2,
                                "title": "A newly received question", "detail": {"text": "Choose an option", "options": []}}, None]}
                            page.get_by_test_id("m-tab-chats").click()
                            page.get_by_test_id("m-tab-inbox").click()
                            warning = page.locator('.m-resource-warning[data-resource="inbox"]')
                            warning.wait_for()
                            self.assertIn("incomplete", warning.text_content())
                            self.assertEqual(warning.get_attribute("data-read"), "partial")
                            self.assertTrue(page.locator('[data-testid="m-inbox-row"][data-id="q:new"]').is_visible())
                            geometry = warning.evaluate("""e => {
                              const r = e.getBoundingClientRect(); return {left:r.left, right:r.right,
                              width:e.clientWidth, scroll:e.scrollWidth, height:r.height}; }""")
                            self.assertGreaterEqual(geometry["left"], 0)
                            self.assertLessEqual(geometry["right"], width)
                            self.assertLessEqual(geometry["scroll"], geometry["width"] + 1)
                            self.assertGreater(geometry["height"], 0)
                            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                            self.assertFalse(errors, errors)
                            print(f"PASS {width}px, root {base * scale}px ({scale:.0%}): partial warning wraps and row stays visible")
                            ctx.close()
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
