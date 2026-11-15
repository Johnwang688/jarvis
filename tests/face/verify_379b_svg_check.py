"""Counterexample to PR 379's face-check SVG provenance assertion.

Run the exact clean() guard from the write face check against server-text DOM
regions. This tests the guard itself, not a claim that HEAD renders text as HTML.
"""
from __future__ import annotations

import ast
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "tests/face/hud_v2_phone_write_check.py"
MARKUP = "img, script, b, svg, iframe, object, embed, [onerror], [onload], a[href^='javascript']"


def main():
    tree = ast.parse(SOURCE.read_text())
    guard = next(node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef) and node.name == "clean")
    code = compile(ast.Module(body=[guard], type_ignores=[]), str(SOURCE), "exec")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        scope = {"page": page, "MARKUP": MARKUP}
        exec(code, scope)
        clean = scope["clean"]
        try:
            page.set_content('<main class="app"><p class="m-notice" data-testid="m-compose-note">Refused.</p></main>')
            assert clean(), "sanity: ordinary text should pass"
            page.locator(".m-notice").evaluate("e => e.innerHTML = '<img src=x>'")
            assert not clean(), "sanity: ordinary injected markup must fail"
            page.locator(".m-notice").evaluate("""e => e.innerHTML =
                '<svg class="m-icon" aria-hidden="true"><path d="M0 0h24v24H0z"/></svg>'""")
            assert not clean(), (
                "write face guard accepted a forged m-icon SVG in a server refusal notice; "
                "CSS classes and aria-hidden are not code provenance")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
