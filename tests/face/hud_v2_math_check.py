"""Shared Markdown math in actual chat, under the HUD CSP, using MockDaemon(0).
No live ports, providers, owner state or external resources.
"""
from __future__ import annotations
import os
import re
from pathlib import Path
import sys
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

MVT = "For $a<b$, the Mean Value Theorem gives a point $c$ such that:\n\n$$f'(c)=\\frac{f(b)-f(a)}{b-a}.$$"

def math_checks(browser, mock, base, check, until, guard, fake):
    ctx = browser.new_context(viewport={"width": 1200, "height": 850})
    guard(ctx)
    off_origin = []
    def refuse(route):
        off_origin.append(route.request.url)
        route.abort()
    ctx.route(re.compile(r"^https?://(?!"+re.escape(base.removeprefix("http://"))+r"/)"), refuse)
    ctx.add_init_script(fake)
    page = ctx.new_page()
    errors, font_urls = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda req: font_urls.append(req.url) if "KaTeX_" in req.url else None)
    try:
        mock.world["transcripts"]["t1"] = [
            {"role": "user", "text": "Explain the Mean Value Theorem.", "at": "2026-10-13T00:00:00+00:00"},
            {"role": "assistant", "text": MVT, "at": "2026-10-13T00:00:01+00:00"}]
        page.goto(base + "/")
        page.locator('[data-testid="thread-t1"]').click()
        body = page.locator('[data-testid="msg-assistant"] .body').last
        until(lambda: body.locator(".katex").count() == 3)
        check("MVT comparison, point and display equation render in actual chat", body.locator(".katex").count() == 3)
        check("display fraction retains accessible MathML", body.locator(".katex-display math mfrac").count() == 1)
        page.evaluate("document.fonts.ready")
        fonts = page.evaluate("""() => [...document.fonts].filter(f => f.family.startsWith('KaTeX') && f.status === 'loaded').map(f => f.family)""")
        check("KaTeX Main and Math fonts actually load", "KaTeX_Main" in fonts and "KaTeX_Math" in fonts, str(fonts))
        check("all math font requests use the mock's local assets", bool(font_urls) and all(u.startswith(base + "/assets/") for u in font_urls), str(font_urls))
        geometry = body.locator(".katex-display .mfrac").evaluate("""el => {
            const line = el.querySelector('.frac-line');
            return {height:el.getBoundingClientRect().height,width:line.getBoundingClientRect().width,
                border:parseFloat(getComputedStyle(line).borderBottomWidth),
                rows:[...el.querySelectorAll(".vlist > span")].filter(n=>n.textContent.trim()).map(n=>n.getBoundingClientRect().top)};
        }""")
        check("fraction is drawn with a visible horizontal rule", geometry["width"] > 30 and geometry["border"] > 0 and len(geometry["rows"]) >= 2 and max(geometry["rows"]) - min(geometry["rows"]) > 8, str(geometry))
        screenshot = os.environ.get("JARVIS_MATH_SCREENSHOT")
        if screenshot:
            page.screenshot(path=screenshot, full_page=True)
        # The shared renderer re-renders only the completed draft, as before.
        mock.emit("turn_started", {}, thread_id="t1")
        mock.emit("text_delta", {"text": "$$\\frac{1}"}, thread_id="t1")
        until(lambda: page.locator('[data-testid="draft"]').count() == 1)
        draft = page.locator('[data-testid="draft"]')
        check("incomplete streaming draft stays readable plain text", draft.locator(".katex").count() == 0 and "$$\\frac{1}" in draft.inner_text())
        mock.emit("text", {"text": "$$\\frac{1}{2}$$"}, thread_id="t1")
        until(lambda: page.locator('[data-testid="draft"]').count() == 0)
        body = page.locator('[data-testid="msg-assistant"] .body').last
        check("completed stream draws its math fraction", bool(until(lambda: body.locator(".mfrac").count() == 1)))
        mock.emit("turn_finished", {"stop": "end"}, thread_id="t1")
        hostile = "$\\href{javascript:alert(1)}{click}$ $\\includegraphics{https://evil.test/x}$ $\\htmlStyle{position:fixed;inset:0}{x}$"
        mock.emit("text", {"text": hostile}, thread_id="t1")
        until(lambda: "htmlStyle" in page.locator('[data-testid="msg-assistant"] .body').last.inner_text())
        body = page.locator('[data-testid="msg-assistant"] .body').last
        check("math trust commands cannot draw links, images, controls or fixed overlays", body.locator("a,img,button,input,[id],[style*='position:fixed']").count() == 0)
        extreme = "$\\kern{-1000000em}x$ $\\raisebox{-1000000em}{x}$ $\\llap{\\raisebox{1000000em}{x}}y$"
        mock.emit("text", {"text": extreme}, thread_id="t1")
        until(lambda: "1000000em" in page.locator('[data-testid="msg-assistant"] .body').last.inner_text())
        body = page.locator('[data-testid="msg-assistant"] .body').last
        check("extreme spacing stays literal without generated geometry or page overflow",
              body.locator(".katex,[style]").count() == 0 and body.inner_text().strip() == extreme
              and page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        mock.emit("text", {"text": "$$" + "x+".join(["x"] * 100) + "$$"}, thread_id="t1")
        until(lambda: page.locator('[data-testid="msg-assistant"] .body').last.locator(".math-display").count() == 1)
        body = page.locator('[data-testid="msg-assistant"] .body').last
        page.set_viewport_size({"width": 375, "height": 800})
        layout = body.locator(".math-display").evaluate("""el => ({box:el.clientWidth,content:el.scrollWidth,
            body:el.parentElement.clientWidth,scroll:getComputedStyle(el).overflowX})""")
        check("long display equation scrolls within its Markdown body at 375px", layout["box"] <= layout["body"] and layout["content"] > layout["box"] and layout["scroll"] == "auto", str(layout))
        check("math makes no external resource requests", not off_origin, str(off_origin))
        check("math raises no page errors", not errors, str(errors))
    finally:
        ctx.close()

def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    from tests.face.hud_v2_mock import DIST, MockDaemon
    if not (DIST / "index.html").exists():
        raise RuntimeError("build the isolated worktree HUD first")
    mock = MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            math_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until, v2.guard, v2.FAKE_RECOGNIZER)
            browser.close()
    finally:
        mock.stop()
    for failure in v2.FAILURES:
        print("FAIL:", failure)
    print(f"{v2.CHECKS-len(v2.FAILURES)}/{v2.CHECKS} math browser checks passed")
    return int(bool(v2.FAILURES))
if __name__ == "__main__":
    raise SystemExit(main())
