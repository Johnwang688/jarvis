"""Exercise the write face guard in server-text regions omitted from its selector list.

This tests the guard, not a claim that the production renderer inserts HTML.
"""
from __future__ import annotations

import ast
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "tests/face/hud_v2_phone_write_check.py"
MARKUP = "img, script, b, svg, iframe, object, embed, [onerror], [onload], a[href^='javascript']"


class ServerTextSvgGuard(unittest.TestCase):
    def test_forged_icon_slots_in_server_text(self):
        tree = ast.parse(SOURCE.read_text())
        guard = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "clean")
        code = compile(ast.Module(body=[guard], type_ignores=[]), str(SOURCE), "exec")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                scope = {"page": page, "MARKUP": MARKUP}
                exec(code, scope)
                clean = scope["clean"]
                # threadsView.ts:165/173, threadsView.ts:269, card.ts:284. These fields all
                # contain server words, and have no ancestor covered by clean's server_text.
                for region in ("m-thread-project", "m-thread-provider", "m-limited", "tool"):
                    with self.subTest(region=region):
                        page.set_content(f'<main class="app"><section class="view"><span class="{region}">Words</span></section></main>')
                        self.assertTrue(clean())
                        target = page.locator("." + region)
                        target.evaluate("e => e.innerHTML = '<svg class=\"m-icon\" aria-hidden=\"true\"><path/></svg>'")
                        self.assertFalse(clean(), "unpositioned SVG must fail")
                        # Server markup can forge both attributes and the entire allowed slot.
                        target.evaluate("""e => e.innerHTML = '<span data-testid="m-lock-pill">'
                            + '<svg class="m-icon" aria-hidden="true"><path d="M0 0h24v24H0z"/></svg></span>'""")
                        self.assertFalse(clean(), f"forged icon-slot SVG accepted in server text: {region}")
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
