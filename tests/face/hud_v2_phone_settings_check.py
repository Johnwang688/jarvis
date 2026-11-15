"""RD-2: icon tabs, live Settings, unlock and two-tap sign out under the real CSP.

Isolated browser routes, no daemon or live ports. JARVIS_RD2_SHOTS saves both phone sizes.
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
    shots = os.environ.get("JARVIS_RD2_SHOTS")
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
    for width, height in [(390, 844), (375, 812)]:
        context = browser.new_context(viewport={"width": width, "height": height}, has_touch=True,
                                      device_scale_factor=2)
        context.add_init_script("""window.__csp = [];
        const sub = {endpoint: 'https://example.org/phone', toJSON: () => ({endpoint: 'https://example.org/phone'}), unsubscribe: async () => true};
        Object.defineProperty(navigator, 'serviceWorker', {value: {addEventListener() {}, register: async () => ({pushManager: {
          getSubscription: async () => sub, subscribe: async () => sub
        }})}});
        window.PushManager = class {};
        Object.defineProperty(Notification, 'permission', {get: () => 'granted'});
        document.addEventListener('securitypolicyviolation', e => window.__csp.push(e.violatedDirective));
        window.EventSource = class {
          readyState = 0;
          constructor() { window.__stream = this; queueMicrotask(() => {this.readyState = 1; this.onopen?.(new Event('open'));}); }
          close() { this.readyState = 2; }
          reopen() { this.onopen?.(new Event('open')); }
        };""")
        state = {"unlock": 1800, "limited": False, "logout": 0, "locks": 0}
        hostile = '<img src=x onerror="window.__pwned=1">'
        row = {"id": "q:aaaaaaaa:1", "kind": "question", "rank": 2, "title": "Which approach?", "blocks": "b",
               "project_id": None, "thread_id": "aaaaaaaa", "task_id": None, "asked_at": None,
               "expires_at": None, "origin": "", "detail": {"text": "Which approach?", "options": ["First", "Second"]}}

        def serve(route):
            path = route.request.url.split(HOST, 1)[1].split("?", 1)[0]
            if path == "/m/rd2-type.css":
                return route.fulfill(content_type="text/css", body="html {font-size: 200%}")
            if path == "/m/api/push":
                import base64
                key = base64.urlsafe_b64encode(bytes([4] + [0] * 64)).decode().rstrip("=")
                return route.fulfill(json={"enabled": True, "subscribed": True, "key": key})
            if path.startswith("/m/api/"):
                if path == "/m/api/me":
                    return route.fulfill(json={"signed_in": True, "secure": {"ok": not state["limited"], "label": hostile}, "unlock_s": state["unlock"]})
                if path == "/m/api/inbox":
                    return route.fulfill(json={"rows": [row]})
                if path == "/m/api/tasks":
                    return route.fulfill(json={"tasks": []})
                if path == "/m/api/review":
                    return route.fulfill(json={"v": 1, "open": 61, "more": 1, "overnight": [], "before": [],
                                               "summary": {"approvals": 1, "questions": 60},
                                               "quiet": {"enabled": True, "now": True, "start": "23:00", "end": "07:00"}})
                if path == "/m/api/working":
                    return route.fulfill(json={"entries": [], "total": 0, "more": False})
                if path == "/m/api/lock":
                    state["unlock"] = 0
                    state["locks"] += 1
                    return route.fulfill(json={"ok": True})
                if path == "/m/api/logout":
                    state["logout"] += 1
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
        # Real CSS env() values, including the home indicator, without injecting inline CSS under CSP.
        context.new_cdp_session(page).send("Emulation.setSafeAreaInsetsOverride", {"insets": {"top": 47 if width == 390 else 44, "bottom": 34}})
        page.goto(f"https://{HOST}/m/")
        tid = page.get_by_test_id
        page.wait_for_function("() => document.querySelector('[data-testid=m-conn]')?.textContent === 'Live'")

        def shot(name):
            if shots:
                page.screenshot(path=str(Path(shots) / f"{name}-{width}x{height}.png"), animations="disabled")

        check(f"RD-2 three icon tabs and exact badge at {width}",
              page.locator("nav.tabs button").count() == 3 and page.locator("nav.tabs svg[aria-hidden=true]").count() == 3
              and tid("m-tab-inbox").get_attribute("aria-label") == "Inbox, 1 need you"
              and page.locator(".m-tab-badge").inner_text() == "1")
        check(f"RD-2 tabs 48px above 34px safe area at {width}", page.evaluate("""() => {
          const nav = document.querySelector('nav.tabs');
          return parseFloat(getComputedStyle(nav).paddingBottom) === 34 && [...nav.children].every(e => {
            const b = e.getBoundingClientRect(); return b.height >= 48 && b.width >= 48 && b.bottom <= innerHeight - 34;
          }); }"""))
        check(f"RD-2 quiet capped deferrals reachable at {width}", tid("m-review-open").is_visible() and tid("m-tab-review").count() == 0)
        shot("inbox")
        tid("m-review-open").click()
        check(f"RD-2 review opens under Inbox at {width}", tid("m-review").is_visible() and tid("m-tab-inbox").get_attribute("aria-current") == "page")
        tid("m-review-back").click()
        tid("m-lock-pill").click()
        check(f"RD-2 pill has only Lock now and Settings at {width}",
              tid("m-menu-panel").locator("button").count() == 2 and tid("m-lock-now").is_visible() and tid("m-menu-settings").is_visible()
              and tid("m-notify").count() == 0)
        shot("pill-popover")
        page.keyboard.press("Escape")
        check(f"RD-2 pill Escape returns focus at {width}", tid("m-lock-pill").evaluate("e => e === document.activeElement"))
        tid("m-tab-settings").click()
        check(f"RD-2 Settings sections and quiet hours at {width}",
              tid("m-settings").locator("h2").all_text_contents() == ["Connection", "Unlock", "Notifications", "Session"]
              and "23:00 to 07:00" in tid("m-settings-quiet").inner_text()
              and tid("m-menu-lastread").inner_text() != "Not read yet" and tid("m-notify").count() == 1)
        shot("settings")
        page.evaluate("""() => { const link = document.createElement('link'); link.rel = 'stylesheet';
          link.href = '/m/rd2-type.css'; link.id = 'rd2-type'; document.head.append(link); }""")
        page.wait_for_function("() => getComputedStyle(document.documentElement).fontSize === '32px'")
        check(f"RD-2 Settings wraps at 200% text at {width}", tid("m-settings").evaluate("e => e.scrollWidth <= e.clientWidth"))
        page.evaluate("document.getElementById('rd2-type').remove()")
        tid("m-lock-now").click()
        page.wait_for_function("() => document.querySelector('[data-testid=m-settings-unlock]')?.textContent === 'Locked'")
        check(f"RD-2 Lock now uses server and pill follows at {width}", state["locks"] == 1 and tid("m-lock-pill").get_attribute("data-state") == "locked")
        state["limited"] = True
        page.evaluate("window.__stream.reopen()")
        page.wait_for_function("() => document.querySelector('[data-testid=m-settings-security]')?.textContent.startsWith('Limited:')")
        check(f"RD-2 hostile security text and live orb at {width}", hostile in tid("m-settings-security").inner_text()
              and tid("m-settings").locator("img,[onerror]").count() == 0
              and tid("m-settings-connection").inner_text() == tid("m-conn").inner_text())
        state["limited"] = False
        state["unlock"] = 1800
        page.evaluate("window.__stream.reopen()")
        page.wait_for_function("() => document.querySelector('[data-testid=m-settings-security]')?.textContent.startsWith('Secure:')")
        tid("m-signout").click()
        check(f"RD-2 first sign-out tap opens dialog and makes background inert at {width}", state["logout"] == 0
              and tid("m-signout-sheet").get_attribute("role") == "alertdialog"
              and page.evaluate("['.top','.main','.tabs'].every(s => document.querySelector(s).inert)"))
        shot("signout-confirm")
        check(f"RD-2 destructive choice is not initially focused at {width}", tid("m-signout-sheet").evaluate("e => e === document.activeElement"))
        page.keyboard.press("Shift+Tab")
        check(f"RD-2 reverse Tab starts on Cancel at {width}", tid("m-signout-cancel").evaluate("e => e === document.activeElement"))
        page.keyboard.press("Tab")
        check(f"RD-2 forward Tab wraps to confirm at {width}", tid("m-signout-confirm").evaluate("e => e === document.activeElement"))
        for close in ["Escape", "m-signout-cancel", "m-signout-dim"]:
            if close == "Escape":
                page.keyboard.press("Escape")
            elif close == "m-signout-dim":
                tid(close).click(position={"x": 5, "y": 5})
            else:
                tid(close).click()
            check(f"RD-2 {close} returns focus and cancels at {width}", tid("m-signout-sheet").count() == 0
                  and tid("m-signout").evaluate("e => e === document.activeElement") and state["logout"] == 0
                  and not tid("m-main").evaluate("e => e.inert"))
            tid("m-signout").click()
        tid("m-signout-confirm").click()
        page.wait_for_function("() => document.querySelector('[data-screen=signin]') || !document.querySelector('[data-testid=m-signout-sheet]')")
        check(f"RD-2 exactly two taps signs out at {width}", state["logout"] == 1)
        check(f"RD-2 no overflow, page errors or CSP violations at {width}", not errors and page.evaluate("document.documentElement.scrollWidth <= innerWidth && window.__csp.length === 0"), str(errors))
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
    print(f"{v2.CHECKS} phone Settings checks, {len(v2.FAILURES)} failures")
    return bool(v2.FAILURES)


if __name__ == "__main__":
    sys.exit(main())
