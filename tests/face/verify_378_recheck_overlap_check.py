"""Fresh RD-3 verifier: the sticky strip must not obscure a question jumped to.

Fake routes only, no listener. Build hud first, then run from the repo root:
  HOME=$(mktemp -d) PYTHONPATH=$PWD \
    PLAYWRIGHT_BROWSERS_PATH=/home/johnw/.cache/ms-playwright \
    /home/johnw/projects/Jarvis/.venv/bin/python \
    tests/face/verify_378_recheck_overlap_check.py 2>&1 | tail -20

VERIFY378_DIST optionally selects an independently built baseline for comparison.
"""
import mimetypes
import os
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

from jarvis.v2.remote_listener import SECURITY_HEADERS
from tests.face.hud_v2_phone_threads_check import DIST, HOST, entry, row, task

DIST = Path(os.environ.get("VERIFY378_DIST", str(DIST)))


def main():
    assert (DIST / "mobile.html").is_file(), "Build hud first"
    failures = []
    checks = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for width in (375, 390):
            for font in (125, 150):
                ctx = browser.new_context(viewport={"width": width, "height": 812 if width == 375 else 844})
                entries = [entry(f"e{i:07x}", "needs_input", f"Chat {i}", 1) for i in range(125)]
                for e in entries:
                    e["last"] = "2026-11-15T10:00:00Z"
                state = {"rows": [], "entries": entries}
                blocked = task("b0000001", "blocked", "Deploy a test")
                blocked["spec"] = {"questions": [{"text": "Choose deployment target", "options": ["dev", "prod"],
                                                   "blocking": True, "answer": None}]}

                def serve(route):
                    path = route.request.url.split(HOST, 1)[1].split("?", 1)[0]
                    values = {
                        "/m/api/me": {"signed_in": True, "secure": {"ok": True}},
                        "/m/api/inbox": {"rows": state["rows"], "more": False},
                        "/m/api/working": {"entries": state["entries"], "more": False, "total": len(state["entries"])},
                        "/m/api/tasks": {"tasks": [blocked], "more": False},
                        "/m/api/tasks/b0000001": {"task": blocked},
                        "/m/api/review": {"open": 0},
                    }
                    if path in values:
                        return route.fulfill(json=values[path])
                    if path.endswith("/log"):
                        return route.fulfill(json={"rows": [], "next": 1, "older": False,
                                                   "thread": {"id": path.split("/")[-2], "title": "Chat", "project_id": "c0000001"}})
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
                page.set_default_timeout(10000)

                def check(name, ok, detail):
                    nonlocal checks
                    checks += 1
                    print(f"{'PASS' if ok else 'FAIL'} {name} @{width}/{font}%: {detail}", flush=True)
                    if not ok:
                        failures.append(f"{name} @{width}/{font}%")

                try:
                    page.goto(f"https://{HOST}/m/")
                    page.wait_for_function("() => document.querySelectorAll('.m-thread-needs').length === 125")
                    page.evaluate("font => document.documentElement.style.fontSize = font + '%'", font)
                    target = page.locator('[data-key="thread:e000000a"] .m-thread-open')
                    target.evaluate("e => e.scrollIntoView({block:'center'})")
                    before = page.locator('[data-testid=m-main]').evaluate("e => e.scrollTop")
                    target.click()
                    page.locator('[data-testid=m-transcript]').wait_for()
                    page.locator('[data-testid=m-back]').click()
                    page.locator('[data-testid=m-inbox]').wait_for()
                    after = page.locator('[data-testid=m-main]').evaluate("e => e.scrollTop")
                    hit = target.evaluate("""e => {const b=e.getBoundingClientRect();
                        return e.contains(document.elementFromPoint(b.x+b.width/2,b.y+b.height/2));} """)
                    check("ordinary chat Back restores scroll and a tappable head", abs(before - after) <= 1 and hit,
                          {"before": before, "after": after, "hit": hit})

                    state["entries"] = []
                    state["rows"] = [row(f"q:{i:03d}", "question", 3, f"Question {i}",
                                         {"text": "Which branch?", "options": []}, thread_id=f"e{i:07x}",
                                         origin=f"Chat {i}", asked_at="2026-11-15T10:00:00Z") for i in range(124)]
                    state["rows"].insert(60, row("tq:b0000001:0", "task_question", 3, "Which deployment target?",
                                                 {"text": "Choose deployment target", "options": ["dev", "prod"]},
                                                 task_id="b0000001", asked_at="2026-11-15T10:00:00Z"))
                    page.reload()
                    page.wait_for_function("() => document.querySelectorAll('[data-testid=m-inbox-row]').length === 125")
                    page.evaluate("font => document.documentElement.style.fontSize = font + '%'", font)
                    badge = page.locator('[data-testid=m-chip-needs]').evaluate("""e => {
                        const b=e.getBoundingClientRect(), c=e.querySelector('.m-chip-count'), r=c.getBoundingClientRect();
                        const main=document.querySelector('[data-testid=m-main]');
                        return {text:e.textContent, fits:r.left>=b.left && r.right<=b.right,
                                uncut:c.scrollWidth<=c.clientWidth, overflow:main.scrollWidth>main.clientWidth};} """)
                    check("125-count chip fits", badge["fits"] and badge["uncut"] and not badge["overflow"]
                          and badge["text"] == "Needs you · 125", badge)
                    page.locator('[data-key="task:b0000001"] .m-thread-open').click()
                    page.locator('[data-testid=m-answer-in-inbox]').click()
                    page.locator('[data-testid=m-inbox]').wait_for()
                    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
                    geometry = page.locator('[data-id="tq:b0000001:0"]').evaluate("""e => {
                        const s=document.querySelector('.m-strip').getBoundingClientRect();
                        const q=e.querySelector('.question').getBoundingClientRect();
                        return {stripTop:s.top, stripBottom:s.bottom, questionTop:q.top, questionBottom:q.bottom};} """)
                    # This is the real task -> Answer it in the Inbox flow, not a synthetic DOM scroll.
                    # At 125%, the first line of the question is painted beneath the two-row strip.
                    check("Answer in Inbox leaves the question text below the strip",
                          geometry["questionTop"] >= geometry["stripBottom"] - 1, geometry)
                finally:
                    ctx.close()
        browser.close()
    print(f"{checks} fresh recheck assertions, {len(failures)} failures: {failures}", flush=True)
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
