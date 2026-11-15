"""PR 378 counterexample: reduced motion must stop the new status dots.

Uses the built phone shell, fake API routes, and no listener or live port.
Run from the repo root after building hud (runner config loader if needed):
  PYTHONPATH=$PWD PLAYWRIGHT_BROWSERS_PATH=/home/johnw/.cache/ms-playwright \
    /home/johnw/projects/Jarvis/.venv/bin/python tests/face/verify_378_reduced_motion_check.py
"""
import mimetypes
import re

from playwright.sync_api import sync_playwright

from jarvis.v2.remote_listener import SECURITY_HEADERS
from tests.face.hud_v2_phone_threads_check import DIST, HOST, entry, task


def main():
    assert (DIST / "mobile.html").is_file(), "Build hud first"
    failures = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for width in (375, 390):
            for font in (125, 150):
                ctx = browser.new_context(viewport={"width": width, "height": 844}, reduced_motion="reduce")

                def serve(route):
                    path = route.request.url.split(HOST, 1)[1].split("?", 1)[0]
                    values = {
                        "/m/api/me": {"signed_in": True, "secure": {"ok": True}},
                        "/m/api/inbox": {"rows": [], "more": False},
                        "/m/api/working": {"entries": [entry("a0000001", "working", "A running chat", 1)], "more": False, "total": 1},
                        "/m/api/tasks": {"tasks": [task("b0000001", "running", "A running task"),
                                                     task("b0000002", "verifying", "A verifying task")], "more": False},
                        "/m/api/review": {"open": 0},
                    }
                    if path in values:
                        return route.fulfill(json=values[path])
                    if path == "/m/api/events":
                        return route.fulfill(headers={"Content-Type": "text/event-stream"}, body="retry: 600000\n: hello\n\n")
                    if path.startswith("/m/api/"):
                        return route.fulfill(status=404, json={"error": "not found"})
                    target = (DIST / ("mobile.html" if path == "/m/" else path.removeprefix("/m/"))).resolve()
                    if DIST.resolve() not in target.parents or not target.is_file():
                        return route.fulfill(status=404)
                    headers = dict(SECURITY_HEADERS)
                    headers["Content-Type"] = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                    return route.fulfill(headers=headers, body=target.read_bytes())

                ctx.route("**/*", lambda route: route.abort())
                ctx.route(re.compile(r"^https://" + re.escape(HOST) + "/"), serve)
                page = ctx.new_page()
                try:
                    page.goto(f"https://{HOST}/m/")
                    page.wait_for_function("() => document.querySelectorAll('.m-thread-running .m-thread-dot').length === 3")
                    page.evaluate("font => document.documentElement.style.fontSize = font + '%'", font)
                    assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")
                    dots = page.locator(".m-thread-running .m-thread-dot").evaluate_all("""nodes => nodes.map(e => ({
                        state: e.closest('[data-state]').dataset.state,
                        animation: getComputedStyle(e).animationName,
                        active: e.getAnimations().filter(a => a.playState === 'running').length,
                    }))""")
                    ok = all(d["animation"] == "none" and d["active"] == 0 for d in dots)
                    failures += int(not ok)
                    print(f"{'PASS' if ok else 'FAIL'} reduced motion @{width}/{font}%: {dots}", flush=True)
                finally:
                    ctx.close()
        browser.close()
    print(f"4 reduced-motion cases, {failures} failures")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
