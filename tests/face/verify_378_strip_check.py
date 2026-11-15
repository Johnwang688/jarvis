"""PR 378 / spec section 1: Inbox filters must remain in the header on scroll.

Fake routes only; no listener or live port. Requires a built hud/dist.
"""
import mimetypes
import re

from playwright.sync_api import sync_playwright

from jarvis.v2.remote_listener import SECURITY_HEADERS
from tests.face.hud_v2_phone_threads_check import DIST, HOST, row


def main():
    assert (DIST / "mobile.html").is_file(), "Build hud first"
    failures = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for width in (375, 390):
            for font in (125, 150):
                ctx = browser.new_context(viewport={"width": width, "height": 844})

                def serve(route):
                    path = route.request.url.split(HOST, 1)[1].split("?", 1)[0]
                    values = {
                        "/m/api/me": {"signed_in": True, "secure": {"ok": True}},
                        "/m/api/inbox": {"rows": [row(f"q:{i:03d}", "question", 3, f"Question {i}",
                                                       {"text": "Which?", "options": []}) for i in range(125)], "more": False},
                        "/m/api/working": {"entries": [], "more": False, "total": 0},
                        "/m/api/tasks": {"tasks": [], "more": False},
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
                    page.wait_for_function("() => document.querySelectorAll('[data-testid=m-inbox-row]').length === 125")
                    page.evaluate("font => document.documentElement.style.fontSize = font + '%'", font)
                    page.evaluate("document.querySelector('[data-testid=m-main]').scrollTop = 400")
                    page.wait_for_function("() => document.querySelector('[data-testid=m-main]').scrollTop >= 399")
                    geometry = page.evaluate("""() => {
                        const strip = document.querySelector('.m-strip').getBoundingClientRect();
                        const main = document.querySelector('[data-testid=m-main]').getBoundingClientRect();
                        return {stripTop: strip.top, stripBottom: strip.bottom, mainTop: main.top,
                                scroll: document.querySelector('[data-testid=m-main]').scrollTop};
                    }""")
                    # A track in the fixed header (section 1) or a translucent
                    # sticky track both keep the controls reachable here.
                    ok = geometry["stripTop"] >= 0 and geometry["stripBottom"] >= geometry["mainTop"]
                    failures += int(not ok)
                    print(f"{'PASS' if ok else 'FAIL'} header strip @{width}/{font}%: {geometry}", flush=True)
                finally:
                    ctx.close()
        browser.close()
    print(f"4 persistent-strip cases, {failures} failures")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
