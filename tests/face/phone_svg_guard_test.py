"""Position controls for the write face check's code-built SVG exceptions."""
from __future__ import annotations

import ast
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright


class PhoneSvgSlots(unittest.TestCase):
    def test_exact_tab_slots_and_forged_tabs(self):
        source = Path(__file__).with_name("hud_v2_phone_write_check.py")
        tree = ast.parse(source.read_text())
        guard = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "clean")
        answer = ast.parse(source.with_name("hud_v2_phone_answer_check.py").read_text())
        markup = next(n.value.value for n in answer.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "MARKUP" for t in n.targets))
        shell = '<main id="app" class="app-root"><div class="app">{}</div></main>'
        def tab(name):
            return (f'<button data-testid="m-tab-{name}"><span class="m-tab-icon">'
                    '<svg class="m-icon" aria-hidden="true"><path/></svg></span></button>')
        nav = '<nav class="tabs">' + ''.join(tab(name) for name in ("inbox", "chats", "settings")) + '</nav>'
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                scope = {"page": page, "MARKUP": markup}
                exec(compile(ast.Module(body=[guard], type_ignores=[]), str(source), "exec"), scope)
                clean = scope["clean"]
                page.set_content(shell.format(nav))
                self.assertTrue(clean(), "all three real root-to-tab icon slots must pass")
                for forged in (
                    '<section>' + nav + '</section>',
                    nav.replace('class="tabs"', 'class="other"'),
                    nav.replace('m-tab-inbox', 'm-tab-forged'),
                    nav.replace('class="m-tab-icon"', 'class="other"'),
                    nav.replace('<path/>', '<svg class="m-icon" aria-hidden="true"><path/></svg>'),
                    nav.replace('aria-hidden="true"', 'aria-hidden="false"'),
                ):
                    with self.subTest(forged=forged):
                        page.set_content(shell.format(forged))
                        self.assertFalse(clean(), "only the exact root-anchored decorative tab slots may pass")
                for region in ("m-notice", "m-thread-project", "m-thread-provider", "m-limited", "tool"):
                    with self.subTest(region=region):
                        page.set_content(shell.format(f'<section class="{region}">{nav}</section>'))
                        self.assertFalse(clean(), "a complete forged tab bar inside server text must fail")
                        page.set_content(shell.format(nav))
                        page.locator('.m-tab-icon').first.evaluate('(e, cls) => e.classList.add(cls)', region)
                        self.assertFalse(clean(), "server-text ancestry overrides a valid tab icon slot")
            finally:
                browser.close()

    def test_real_header_slots_and_wrapped_forgeries(self):
        source = Path(__file__).with_name("hud_v2_phone_write_check.py")
        tree = ast.parse(source.read_text())
        guard = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "clean")
        answer = ast.parse(source.with_name("hud_v2_phone_answer_check.py").read_text())
        markup = next(n.value.value for n in answer.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "MARKUP" for t in n.targets))
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                scope = {"page": page, "MARKUP": markup}
                exec(compile(ast.Module(body=[guard], type_ignores=[]), str(source), "exec"), scope)
                clean = scope["clean"]
                icons = ('<button data-testid="m-brand"><span class="m-orb">'
                         '<svg aria-hidden="true"><path/></svg></span></button>'
                         '<div class="m-menu"><button data-testid="m-lock-pill">'
                         '<svg class="m-icon" aria-hidden="true">'
                         '<path/></svg><span>Locked</span></button></div>')
                header = f'<header class="top">{icons}</header>'
                shell = '<main id="app" class="app-root"><div class="app">{}</div></main>'
                page.set_content(shell.format(header))
                self.assertTrue(clean(), "real root-to-header icon slots must pass")
                for region in ("m-thread-project", "m-thread-provider", "m-limited", "tool"):
                    with self.subTest(region=region):
                        page.set_content(shell.format(header + f'<section class="{region}"><div>{header}</div></section>'))
                        self.assertFalse(clean(), "a complete forged header inside server text must fail")
                        # Even a server-text region at a real slot forbids SVGs below it.
                        page.set_content(shell.format(header))
                        page.locator(".m-orb").evaluate("(e, cls) => e.classList.add(cls)", region)
                        self.assertFalse(clean(), "server-text ancestry overrides a valid icon slot")
                page.set_content(shell.format(f'<section><div>{header}</div></section>'))
                self.assertFalse(clean(), "matching controls outside the real header must fail")
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
