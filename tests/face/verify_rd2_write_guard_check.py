"""RD-2 counterexample: genuine tab icons fail the phone-write text-safety guard.

No listener: all requests are intercepted. Run with PYTHONPATH set to the clone.
Executes the original clean() helper unchanged, on a freshly rendered Inbox.
"""
from __future__ import annotations

import ast
from pathlib import Path

from playwright.sync_api import sync_playwright
from tests.face.hud_v2_phone_review_check import Mock, ORIGIN, report
from tests.face.hud_v2_phone_answer_check import MARKUP

REPO = Path(__file__).resolve().parents[2]


def main():
    tree = ast.parse((REPO / "tests/face/hud_v2_phone_write_check.py").read_text())
    helper = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "clean")
    code = compile(ast.Module(body=[helper], type_ignores=[]), "original_phone_write_clean", "exec")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": 375, "height": 812}, has_touch=True)
            context.add_init_script("""window.EventSource = class {
              readyState = 1;
              constructor() { queueMicrotask(() => this.onopen?.(new Event('open'))); }
              close() { this.readyState = 2; }
            };""")
            mock = Mock(REPO / "hud/dist")
            mock.review = report(False)
            context.route("**/*", mock)
            page = context.new_page()
            page.goto(ORIGIN + "/m/")
            page.get_by_test_id("m-review-open").wait_for()
            assert page.locator("nav.tabs svg[aria-hidden=true]").count() == 3
            assert page.locator(".app img,.app [onerror],.app [onload]").count() == 0
            scope = {"page": page, "MARKUP": MARKUP}
            exec(code, scope)
            assert scope["clean"](), (
                "Phone-write clean() rejects the three genuine RD-2 tab SVGs: "
                "its icon_slots allowlist still contains only brand, pill and project icons"
            )
            # Updating the code-built slots must keep the guard hostile to forged decorative SVGs.
            page.evaluate("""() => {
              const text = document.createElement('div'); text.className = 'm-notice';
              const forged = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
              forged.classList.add('m-icon'); forged.setAttribute('aria-hidden', 'true');
              text.append(forged); document.querySelector('.app').append(text);
            }""")
            assert not scope["clean"](), "A forged decorative SVG in server-text space must still fail"
            print("1 RD-2 phone-write guard check passed")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
