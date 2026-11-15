"""RD-0/RD-1: isolated phone header states, targets, pill menu and reduced motion.

Built assets under the real remote CSP; fake API/EventSource, no daemon or live ports.
Set JARVIS_RD01_SHOTS to save the five requested states at both phone sizes.
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
    shots = os.environ.get("JARVIS_RD01_SHOTS")
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
    for width, height in [(390, 844), (375, 812)]:
        state = {"mode": "live", "working": False}
        context = browser.new_context(viewport={"width": width, "height": height}, has_touch=True,
                                      device_scale_factor=2, ignore_https_errors=True)
        context.add_init_script("""window.__csp = [];
        document.addEventListener('securitypolicyviolation', e => window.__csp.push(e.violatedDirective));
        window.EventSource = class {
          readyState = 0;
          constructor() { window.__stream = this; queueMicrotask(() => this.open()); }
          open() { this.readyState = 1; this.onopen?.(new Event('open')); }
          close() { this.readyState = 2; }
          drop() { this.readyState = 0; this.onerror?.(new Event('error')); }
        };""")
        problems = []

        def serve(route):
            path = route.request.url.split(HOST, 1)[1].split("?", 1)[0]
            if path.startswith("/m/api/"):
                if state["mode"] == "offline":
                    return route.abort("internetdisconnected")
                if state["mode"] == "unreachable":
                    # The proxy's own page (`tailscale serve` with nothing behind it), not the daemon's
                    # `{"error": ...}`: a daemon 503 with its words is a refusal, never "unreachable".
                    return route.fulfill(status=503, content_type="text/plain", body="Service Unavailable")
                if state["mode"] == "signed-out":
                    return route.fulfill(status=401, json={"error": "signed out"})
                if path == "/m/api/me":
                    return route.fulfill(json={"signed_in": True, "secure": {"ok": state["mode"] != "limited"}, "unlock_s": 1800})
                if path == "/m/api/inbox":
                    return route.fulfill(json={"rows": []})
                if path == "/m/api/tasks":
                    return route.fulfill(json={"tasks": []})
                if path == "/m/api/review":
                    return route.fulfill(json={"open": 0})
                if path == "/m/api/working":
                    entries = [{"thread_id": "aaaaaaaa", "status": "working", "title": "Work"}] if state["working"] else []
                    return route.fulfill(json={"entries": entries, "total": len(entries), "more": False})
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
        page.on("pageerror", lambda error: problems.append(str(error)))
        page.goto(f"https://{HOST}/m/")
        conn = page.get_by_test_id("m-conn")
        conn.wait_for()
        page.wait_for_function("document.querySelector('[data-testid=m-conn]')?.textContent === 'Live'")

        for mode, expected, tone in [
            ("live", "Live", "ok"), ("reconnecting", "Reconnecting…", "wait"),
            ("offline", "Offline", "wait"), ("unreachable", "Jarvis unreachable", "err"),
            ("limited", "Limited connection", "err"), ("signed-out", "Signed out", "err"),
        ]:
            if mode != "live":
                state["mode"] = mode
                if mode == "signed-out":
                    page.reload()
                else:
                    page.evaluate("window.__stream.drop()")
            page.wait_for_function("word => document.querySelector('[data-testid=m-conn]')?.textContent.startsWith(word)", arg=expected)
            brand = page.get_by_test_id("m-brand")
            check(f"RD-1 {mode} has its state word and tone at {width}",
                  conn.is_visible() and conn.get_attribute("data-tone") == tone
                  and expected in brand.get_attribute("aria-label"))
            box = brand.bounding_box()
            check(f"RD-1 brand is a 48px target at {width} ({mode})", box["height"] >= 48 and box["width"] >= 48)
            check(f"RD-1 decorative SVGs are hidden at {width} ({mode})",
                  page.locator('svg:not([aria-hidden="true"])').count() == 0)
            check(f"RD-1 header fits at {width} ({mode})", page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            if shots and mode != "limited":
                page.screenshot(path=str(Path(shots) / f"header-{mode}-{width}x{height}.png"), animations="disabled")
            if mode == "offline":
                check(f"RD-1 offline core is hollow at {width}",
                      page.locator(".m-o-core").evaluate("e => getComputedStyle(e).fill") == "none")
            if mode == "live":
                pill = page.get_by_test_id("m-lock-pill")
                check(f"RD-1 pill at the right, unlocked with icon, 48px at {width}",
                      pill.bounding_box()["x"] > box["x"] + box["width"]
                      and pill.bounding_box()["height"] >= 48 and pill.get_attribute("data-state") == "unlocked"
                      and pill.locator("svg").count() == 1 and "Unlocked until" in pill.get_attribute("aria-label"))
                check(f"RD-1 no header more button at {width}", page.locator(".top .m-more").count() == 0)
                pill.click()
                panel = page.get_by_test_id("m-menu-panel")
                check(f"RD-1 all four former menu rows in one tap at {width}",
                      all(panel.get_by_test_id(testid).is_visible() for testid in
                          ["m-lock-now", "m-notify", "m-menu-lastread", "m-signout"]))
                check(f"RD-1 background inert behind popover at {width}",
                      page.get_by_test_id("m-main").get_attribute("inert") is not None
                      and brand.get_attribute("inert") is not None)
                page.keyboard.press("Escape")
                check(f"RD-1 Escape closes popover and restores pill focus at {width}",
                      panel.count() == 0 and pill.evaluate("e => e === document.activeElement"))
                pill.evaluate("e => e.blur()")
            if mode == "reconnecting":
                check(f"RD-1 reconnecting pulses at {width}",
                      page.locator(".m-orb").evaluate("e => getComputedStyle(e, '::before').animationName") == "m-o-pulse")
                page.emulate_media(reduced_motion="reduce")
                check(f"RD-1 reduced motion stops halo and all rings at {width}", page.evaluate("""() => {
                  const orb = document.querySelector('.m-orb');
                  return getComputedStyle(orb, '::before').animationName === 'none'
                    && [...orb.querySelectorAll('*')].every(e => getComputedStyle(e).animationName === 'none');
                }"""))
                page.emulate_media(reduced_motion="no-preference")
        check(f"RD-1 no page errors or CSP violations at {width}", not problems and page.evaluate("window.__csp.length === 0"), str(problems))
        context.close()


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    if not (DIST / "mobile.html").is_file():
        print("Build hud/ first")
        return 1
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            run(browser, v2.check)
        finally:
            browser.close()
    print(f"{v2.CHECKS} phone orb checks, {len(v2.FAILURES)} failures")
    return bool(v2.FAILURES)


if __name__ == "__main__":
    sys.exit(main())
