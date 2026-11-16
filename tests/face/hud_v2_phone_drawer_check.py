"""RD-6 drawer at phone sizes, real CSP, synthetic touch and enlarged text.

No daemon or live ports: all requests are intercepted. JARVIS_RD6_SHOTS exports screenshots.
"""
from __future__ import annotations

import mimetypes
import os
from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parents[2]
DIST = REPO / "hud" / "dist"
HOST = "jarvis-box.tail-test.ts.net"


def run(browser, check):
    from jarvis.v2.remote_listener import SECURITY_HEADERS
    shots = os.environ.get("JARVIS_RD6_SHOTS")
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
    for width, height in [(390, 844), (375, 812)]:
        context = browser.new_context(viewport={"width": width, "height": height}, has_touch=True, device_scale_factor=2)
        context.add_init_script("""window.__csp = [];
          document.addEventListener('securitypolicyviolation', e => window.__csp.push(e.violatedDirective));
          window.EventSource = class {
            readyState = 0;
            constructor() { window.__stream = this; queueMicrotask(() => {this.readyState = 1; this.onopen?.(new Event('open'));}); }
            close() {this.readyState = 2;}
          };""")
        state = {"unlock": 1800, "locks": 0, "questions": 2, "hostile": False, "gets": [], "refuse": False}
        hostile = '<svg class="m-icon" aria-hidden="true" onload="window.__pwned=1"><path/></svg><img src=x>'
        projects = [{"id": "11111111", "name": "Jarvis"}, {"id": "22222222", "name": "VEX-override"}, {"id": "33333333", "name": "Unsorted"}]
        threads = [{"id": "aaaaaaaa", "project_id": "11111111", "title": "Phone redesign mockups", "running": True},
                   {"id": "bbbbbbbb", "project_id": "11111111", "title": "Phone unlock window review"},
                   {"id": "cccccccc", "project_id": "11111111", "title": "Inbox working set, WS-4 follow-ups"},
                   {"id": "dddddddd", "project_id": "22222222", "title": "Motor Allocation Active Toggle"}]
        for t in threads:
            t.update(updated="2026-11-15T10:00:00Z", provider="claude", role="chat", task_id=None)

        def serve(route):
            path = route.request.url.split(HOST, 1)[1].split("?", 1)[0]
            if path == "/m/rd6-type.css":
                return route.fulfill(content_type="text/css", body="html {font-size: " + route.request.url.split("size=")[-1] + "%}")
            if path.startswith("/m/api/"):
                if route.request.method == "GET":
                    state["gets"].append(path)
                if path == "/m/api/me":
                    return route.fulfill(json={"signed_in": True, "secure": {"ok": True}, "unlock_s": state["unlock"]})
                if path == "/m/api/inbox":
                    return route.fulfill(json={"rows": [{"id": f"q:aaaaaaaa:{i}", "kind": "question", "rank": 2,
                        "title": "Which approach?", "blocks": "b", "thread_id": "aaaaaaaa", "project_id": "11111111",
                        "task_id": None, "asked_at": None, "expires_at": None, "origin": "", "detail": {"text": "Which approach?", "options": []}}
                        for i in range(state["questions"])]})
                if path == "/m/api/review":
                    return route.fulfill(json={"v": 1, "open": 3, "overnight": [], "before": [], "summary": {"approvals": 1},
                                               "quiet": {"enabled": True, "now": True}})
                if path == "/m/api/projects":
                    return route.fulfill(json={"projects": [{**p, "name": hostile if state["hostile"] else p["name"]} for p in projects]})
                if path == "/m/api/threads":
                    return route.fulfill(json={"threads": [{**t, "title": hostile if state["hostile"] else t["title"]} for t in threads]})
                if path.endswith("/log"):
                    return route.fulfill(json={"rows": [], "next": 0, "thread": threads[0]})
                if path == "/m/api/working":
                    return route.fulfill(json={"entries": [], "total": 0, "more": False})
                if path == "/m/api/tasks":
                    return route.fulfill(json={"tasks": []})
                if path == "/m/api/lock":
                    state["locks"] += 1
                    if state["refuse"]:
                        return route.fulfill(status=403, json={"error": "refused"})
                    state["unlock"] = 0
                    return route.fulfill(json={"ok": True})
                return route.fulfill(status=404, json={"error": "not found"})
            relative = "mobile.html" if path == "/m/" else path.removeprefix("/m/")
            target = (DIST / relative).resolve()
            if DIST.resolve() not in target.parents or not target.is_file():
                return route.fulfill(status=404, body="no")
            headers = dict(SECURITY_HEADERS)
            headers["Content-Type"] = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return route.fulfill(headers=headers, body=target.read_bytes())

        context.route(re.compile(r".*"), lambda route: route.abort())
        context.route(re.compile(r"^https://" + re.escape(HOST) + "/"), serve)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        context.new_cdp_session(page).send("Emulation.setSafeAreaInsetsOverride", {"insets": {"top": 47 if width == 390 else 44, "bottom": 34}})
        page.goto(f"https://{HOST}/m/")
        tid = page.get_by_test_id
        page.wait_for_function("() => document.querySelector('[data-testid=m-conn]')?.textContent === 'Live'")
        tid("m-tab-chats").click()
        tid("m-project-head").first.wait_for()
        tid("m-tab-inbox").click()
        tid("m-review-open").wait_for()
        before = len(state["gets"])
        tid("m-brand").click()
        drawer = tid("m-drawer")
        drawer.wait_for()
        check(f"RD-6 named modal and first row focused {width}", drawer.get_attribute("role") == "dialog"
              and drawer.get_attribute("aria-modal") == "true" and drawer.get_attribute("aria-label") == "Navigation"
              and tid("m-drawer-inbox").evaluate("e => e === document.activeElement"))
        check(f"RD-6 inert behind drawer {width}", page.locator(".app > :not(.m-drawer-host)").evaluate_all("es => es.every(e => e.inert)"))
        check(f"RD-6 open adds no reads {width}", len(state["gets"]) == before)
        # Capture the requested expanded project lists; folds still use the same owner controls.
        if shots:
            for head in tid("m-drawer-project").all():
                if head.get_attribute("aria-expanded") == "false":
                    head.click()
            tid("m-drawer-inbox").focus()
            drawer.locator(".m-drawer-body").evaluate("e => e.scrollTop = 0")
            drawer.evaluate("e => Promise.all(e.getAnimations().map(a => a.finished))")
            page.screenshot(path=str(Path(shots) / f"drawer-{width}x{height}.png"))
        tid("m-drawer-close").focus()
        page.keyboard.press("Shift+Tab")
        check(f"RD-6 reverse trap {width}", tid("m-drawer-lock-now").evaluate("e => e === document.activeElement"))
        page.keyboard.press("Tab")
        check(f"RD-6 forward trap {width}", tid("m-drawer-close").evaluate("e => e === document.activeElement"))
        for method in ["Escape", "m-drawer-dim", "m-drawer-close"]:
            if method == "Escape":
                page.keyboard.press("Escape")
            elif method == "m-drawer-dim":
                tid(method).click(position={"x": width - 5, "y": 200})
            else:
                tid(method).click()
            check(f"RD-6 {method} focus return {width}", not drawer.count() and tid("m-brand").evaluate("e => e === document.activeElement")
                  and not tid("m-main").evaluate("e => e.inert"))
            tid("m-brand").click()
        project = tid("m-drawer-project").first
        project.click()
        page.keyboard.press("Escape")
        tid("m-tab-chats").click()
        check(f"RD-6 drawer fold shared with Chats {width}", tid("m-project-head").first.get_attribute("aria-expanded") == "false")
        tid("m-project-head").first.click()
        tid("m-brand").click()
        check(f"RD-6 Chats fold shared with drawer {width}", project.get_attribute("aria-expanded") == "true")
        tid("m-drawer-tasks").click()
        check(f"RD-6 Tasks navigates and closes {width}", not drawer.count() and tid("m-chip-tasks").get_attribute("aria-pressed") == "true")
        tid("m-brand").click()
        tid("m-drawer-settings").click()
        check(f"RD-6 Settings navigates and closes {width}", not drawer.count() and tid("m-settings").count() == 1)
        tid("m-brand").click()
        tid("m-drawer-inbox").click()
        check(f"RD-6 Inbox navigates and closes {width}", not drawer.count() and tid("m-chip-tasks").get_attribute("aria-pressed") == "false")
        tid("m-brand").click()
        tid("m-drawer-review").click()
        check(f"RD-6 Review navigates and closes {width}", not drawer.count() and tid("m-review").count() == 1)
        # Return through the established tabs; review is a level above Inbox, not a top-level drawer opener.
        tid("m-tab-chats").click()
        tid("m-brand").click()
        tid("m-drawer-project-chat-aaaaaaaa").click()
        check(f"RD-6 project chat navigates and closes {width}", not drawer.count() and tid("m-transcript").count() == 1)
        tid("m-tab-chats").click()
        tid("m-brand").click()
        tid("m-drawer-recent-aaaaaaaa").click()
        check(f"RD-6 recent chat navigates and closes {width}", not drawer.count() and tid("m-transcript").count() == 1)
        tid("m-tab-inbox").click()
        tid("m-brand").click()
        check(f"RD-6 unlocked foot {width}", "Unlocked until" in tid("m-drawer-lock").inner_text())
        state["refuse"] = True
        tid("m-drawer-lock-now").click()
        page.wait_for_function("() => document.querySelector('[data-testid=m-flash]')?.textContent.includes('refused')")
        tid("m-brand").click()
        check(f"RD-6 refused Lock now stays truthful {width}", "Unlocked until" in tid("m-drawer-lock").inner_text())
        state["refuse"] = False
        tid("m-drawer-lock-now").click()
        page.wait_for_function("() => document.querySelector('[data-testid=m-lock-pill]')?.dataset.state === 'locked'")
        tid("m-brand").click()
        check(f"RD-6 Lock now uses server and closes {width}", state["locks"] == 2 and tid("m-drawer-lock").inner_text() == "Locked")
        state["questions"] = 3
        page.evaluate("window.__stream.onmessage({data: JSON.stringify({kind:'inbox_changed', ids: []})})")
        page.wait_for_function("() => document.querySelector('[data-testid=m-drawer-inbox]')?.textContent === 'Inbox3'")
        check(f"RD-6 live count retains inert and focus {width}", tid("m-main").evaluate("e => e.inert")
              and drawer.evaluate("e => e.contains(document.activeElement)"))
        # A same-origin stylesheet tests text scaling under CSP, without style attributes.
        for size in [125, 150]:
            page.evaluate("size => {document.getElementById('rd6-type')?.remove(); const l = document.createElement('link'); l.id='rd6-type'; l.rel='stylesheet'; l.href='/m/rd6-type.css?size='+size; document.head.append(l)}", size)
            page.wait_for_function("size => parseFloat(getComputedStyle(document.documentElement).fontSize) >= 16*size/100", arg=size)
            check(f"RD-6 no overflow at {width}/{size}%", drawer.evaluate("e => e.scrollWidth <= e.clientWidth && e.getBoundingClientRect().right <= innerWidth"))
            check(f"RD-6 all visible targets 48px at {width}/{size}%", drawer.locator("button").evaluate_all("es => es.filter(e => e.getClientRects().length).every(e => {const r=e.getBoundingClientRect();return r.width>=48 && r.height>=48})"))
        page.emulate_media(reduced_motion="reduce")
        check(f"RD-6 reduced motion {width}", drawer.evaluate("e => getComputedStyle(e).animationName === 'none' && e.getAnimations().length === 0"))
        page.keyboard.press("Escape")
        page.evaluate("document.getElementById('rd6-type').remove()")

        def swipe(x, y, dx, dy, target=".app"):
            page.locator(target).evaluate("""(e, p) => {
              const send = (type, x, y) => {const t = new Touch({identifier:1, target:e, clientX:x, clientY:y});
                e.dispatchEvent(new TouchEvent(type, {bubbles:true, cancelable:true, touches:type==='touchend'?[]:[t], changedTouches:[t]}));};
              send('touchstart', p.x,p.y); send('touchmove',p.x+p.dx,p.y+p.dy); send('touchend',p.x+p.dx,p.y+p.dy);
            }""", {"x": x, "y": y, "dx": dx, "dy": dy})
        for x, dy in [(120, 0), (3, 0), (16, 100)]:
            swipe(x, 250, 60, dy)
            check(f"RD-6 edge excludes x={x}, dy={dy} at {width}", not drawer.count())
        swipe(16, 250, 60, 1)
        check(f"RD-6 edge opens at {width}", drawer.count() == 1)
        swipe(200, 250, -60, 1, ".m-drawer")
        check(f"RD-6 swipe closes and returns focus {width}", not drawer.count() and tid("m-brand").evaluate("e => e === document.activeElement"))
        state["hostile"] = True
        tid("m-brand").click()
        tid("m-drawer-chats").click()
        page.wait_for_function("() => document.querySelector('.m-project-name')?.textContent.includes('<svg')")
        tid("m-brand").click()
        check(f"RD-6 hostile names stay text {width}", hostile in drawer.inner_text() and not page.evaluate("window.__pwned || false")
              and drawer.locator("img, script, iframe").count() == 0 and drawer.locator(".m-drawer-label svg").count() == 0)
        check(f"RD-6 no page errors or CSP violations {width}", not errors and page.evaluate("window.__csp.length === 0"), str(errors))
        context.close()


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            run(browser, v2.check)
        finally:
            browser.close()
    print(f"{v2.CHECKS} phone drawer checks, {len(v2.FAILURES)} failures")
    for failure in v2.FAILURES:
        print("FAILED: " + failure)
    return bool(v2.FAILURES)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        import traceback
        for frame in traceback.extract_tb(error.__traceback__):
            if frame.filename == __file__:
                print(f"drawer check line {frame.lineno}: {frame.line}")
        print(f"{type(error).__name__}: {error}")
        sys.exit(1)
