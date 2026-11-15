"""RD-5 Chats: built phone under its real CSP, fake data and SSE, no live listener.

JARVIS_RD5_SHOTS saves mixed fold states at the two phone sizes.
"""
from __future__ import annotations

import mimetypes
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timedelta, timezone

REPO = Path(__file__).resolve().parents[2]
DIST = REPO / "hud" / "dist"
HOST = "jarvis-box.tail-test.ts.net"
HOSTILE = '<svg class="m-icon" onload="window.__pwned=1"></svg><b>x</b>'
PROJECTS = [dict(id=pid, name=name, inbox=False) for pid, name in [
    ("cccccccc", "Jarvis"), ("dddddddd", "VEX-override"), ("eeeeeeee", "Unsorted"), ("ffffffff", "duskrobotics")]]


def world():
    now = datetime.now(timezone.utc)

    def chat(tid, pid, title, minutes, running=False):
        return dict(id=tid, project_id=pid, task_id=None, role="chat", title=title, provider="claude",
                    updated=(now - timedelta(minutes=minutes)).isoformat(), running=running)

    titles = ["Phone redesign mockups", "Phone unlock window review", "Inbox working set, WS-4 follow-ups",
              "Thread list review", "Phone install notes", "Older chat", "First sketches"]
    threads = [chat(f"a000000{i}", "cccccccc", title, i * 120 + 4, i == 0) for i, title in enumerate(titles)]
    threads += [chat("b0000001", "dddddddd", "Motor Allocation Active Toggle with Optical Sensing", 2),
                chat("b0000002", "dddddddd", "B3 Skills Route Development", 14),
                chat("c0000001", "eeeeeeee", "Opening Instagram in Three HUD Panels", 1440),
                chat("c0000002", "eeeeeeee", "Spotify queue for the drive", 4320)]
    # Two inbox rows on one chat still count as one chat needing the owner.
    rows = [dict(id=f"q:{i}", kind="question", rank=2, thread_id=tid, project_id="dddddddd",
                 title="Which?", detail={"text": "Which?", "options": []})
            for i, tid in enumerate(["b0000001", "b0000001", "b0000002"])]
    return dict(threads=threads, rows=rows, projects=[dict(p) for p in PROJECTS], thread_reads=0)


def run(browser, check, until):
    from jarvis.v2.remote_listener import SECURITY_HEADERS
    shots = os.environ.get("JARVIS_RD5_SHOTS")
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
    for width, height in [(390, 844), (375, 812)]:
        data = world()
        context = browser.new_context(viewport={"width": width, "height": height}, has_touch=True,
                                      device_scale_factor=2, ignore_https_errors=True)
        context.add_init_script("""window.__csp = [];
          document.addEventListener('securitypolicyviolation', e => window.__csp.push(e.violatedDirective));
          window.EventSource = class {
            readyState = 0;
            constructor() { window.__stream = this; queueMicrotask(() => {
              this.readyState = 1; this.onopen?.(new Event('open'));
            }); }
            emit(value) { this.onmessage?.(new MessageEvent('message', {data: JSON.stringify(value)})); }
            close() { this.readyState = 2; }
          };""")
        problems = []

        def serve(route):
            path = route.request.url.split(HOST, 1)[1].split("?", 1)[0]
            if path.startswith("/m/api/"):
                if path == "/m/api/me":
                    return route.fulfill(json={"signed_in": True, "secure": {"ok": True}, "unlock_s": 0})
                if path == "/m/api/inbox":
                    return route.fulfill(json={"rows": data["rows"]})
                if path == "/m/api/projects":
                    return route.fulfill(json={"projects": data["projects"]})
                if path == "/m/api/threads":
                    data["thread_reads"] += 1
                    return route.fulfill(json={"threads": data["threads"]})
                if path == "/m/api/working":
                    return route.fulfill(json={"entries": [], "more": False, "total": 0})
                if path == "/m/api/tasks":
                    return route.fulfill(json={"tasks": []})
                if path == "/m/api/review":
                    return route.fulfill(json={"open": 0})
                if path.endswith("/log"):
                    return route.fulfill(json={"rows": [], "next": 0, "more": False, "older": False})
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
        page.get_by_test_id("m-tab-chats").click()
        until(lambda: page.get_by_test_id("m-project").count() == 4)
        group = lambda pid: page.locator(f'[data-testid="m-project"][data-id="{pid}"]')
        head = lambda pid: group(pid).get_by_test_id("m-project-head")
        body = lambda pid: group(pid).get_by_test_id("m-project-body")
        tag = f"RD-5 {width}"
        check(f"{tag}: groups, chat and activity counts", [head(p["id"]).text_content() for p in PROJECTS]
              == ["Jarvis1 running7 chats", "VEX-override2 need you2 chats", "Unsorted2 chats", "duskrobotics0 chats"])
        check(f"{tag}: active open, idle and empty folded", [head(p["id"]).get_attribute("aria-expanded") for p in PROJECTS]
              == ["true", "true", "false", "false"])
        check(f"{tag}: each head controls its body and is at least 48px", all(
            head(p["id"]).get_attribute("aria-controls") == body(p["id"]).get_attribute("id")
            and head(p["id"]).bounding_box()["height"] >= 48 for p in PROJECTS))
        check(f"{tag}: collapsed chat excluded from accessibility tree", "Opening Instagram" not in page.get_by_test_id("m-main").aria_snapshot()
              and body("eeeeeeee").get_attribute("hidden") is not None)
        head("dddddddd").focus()
        page.keyboard.press("Enter")
        until(lambda: head("dddddddd").get_attribute("aria-expanded") == "false")
        check(f"{tag}: Enter folds and keeps head focus", body("dddddddd").is_hidden()
              and head("dddddddd").evaluate("e => e === document.activeElement"))
        page.keyboard.press("Space")
        until(lambda: head("dddddddd").get_attribute("aria-expanded") == "true")
        check(f"{tag}: Space unfolds and exposes chats", group("dddddddd").get_by_test_id("m-chat").first.is_visible()
              and "Motor Allocation" in page.get_by_test_id("m-main").aria_snapshot())
        head("dddddddd").click()
        page.get_by_test_id("m-tab-inbox").click()
        page.get_by_test_id("m-tab-chats").click()
        until(lambda: head("dddddddd").count() == 1)
        check(f"{tag}: fold survives leaving and returning", head("dddddddd").get_attribute("aria-expanded") == "false")
        page.reload()
        page.get_by_test_id("m-tab-chats").click()
        until(lambda: head("dddddddd").count() == 1)
        check(f"{tag}: saved active fold survives reload", head("dddddddd").get_attribute("aria-expanded") == "false")
        before = data["thread_reads"]
        data["threads"][1]["title"] = "Refreshed chat"
        page.evaluate("window.__stream.emit({kind: 'thread_changed', data: {thread_id: 'a0000001', project_id: 'cccccccc'}})")
        until(lambda: data["thread_reads"] > before and "Refreshed chat" in group("cccccccc").inner_text())
        check(f"{tag}: SSE refresh keeps saved fold", head("dddddddd").get_attribute("aria-expanded") == "false")
        data["threads"][1]["title"] = "Phone unlock window review"
        page.get_by_test_id("m-tab-inbox").click()
        page.get_by_test_id("m-tab-chats").click()
        until(lambda: "Phone unlock window review" in group("cccccccc").inner_text())
        page.locator('[data-testid="m-main"]').evaluate("e => e.scrollTop = 0")
        if shots:
            page.screenshot(path=str(Path(shots) / f"chats-{width}x{height}.png"), animations="disabled")
        group("cccccccc").get_by_test_id("m-more").click()
        until(lambda: group("cccccccc").get_by_test_id("m-chat").count() == 7)
        for project in PROJECTS:
            pid = project["id"]
            if head(pid).get_attribute("aria-expanded") == "false":
                head(pid).click()
        rows = page.get_by_test_id("m-chat")
        check(f"{tag}: every chat reachable exactly once after unfold and Show more", rows.count() == len(data["threads"])
              and len(set(rows.evaluate_all("es => es.map(e => e.dataset.id)"))) == len(data["threads"])
              and all(rows.nth(i).is_visible() for i in range(rows.count())))
        rows.last.click()
        page.get_by_test_id("m-back").wait_for()
        check(f"{tag}: an unfolded idle chat opens", page.get_by_test_id("m-transcript").count() == 1)
        page.get_by_test_id("m-back").click()
        # Hostile strings cannot impersonate the code-owned SVG slot.
        data["projects"][0]["name"] = HOSTILE
        data["threads"][0]["title"] = HOSTILE
        page.get_by_test_id("m-tab-inbox").click()
        page.get_by_test_id("m-tab-chats").click()
        until(lambda: HOSTILE in head("cccccccc").inner_text())
        check(f"{tag}: hostile project and chat names stay text", HOSTILE in group("cccccccc").locator(".chat-title").first.inner_text()
              and page.get_by_test_id("m-main").locator('img, script, b, [onload], [onerror]').count() == 0
              and page.get_by_test_id("m-main").locator('svg').evaluate_all("es => es.every(e => e.matches('[data-testid=m-project-head] > svg:first-child') && e.getAttribute('aria-hidden') === 'true')")
              and page.evaluate("window.__pwned === undefined"))
        page.locator("html").evaluate("e => e.style.fontSize = '34px'")
        check(f"{tag}: 200% text avoids horizontal overflow", page.evaluate("document.documentElement.scrollWidth <= innerWidth")
              and all(head(p["id"]).bounding_box()["height"] >= 48 for p in PROJECTS))
        check(f"{tag}: no page errors or CSP violations", not problems and page.evaluate("window.__csp.length === 0"), str(problems))
        context.close()

    # A new viewer with completely blocked storage can still open and fold groups.
    context = browser.new_context(viewport={"width": 375, "height": 812}, ignore_https_errors=True)
    context.add_init_script("Object.defineProperty(window, 'localStorage', {get() {throw new Error('blocked');}})")
    # Reuse the last routing fixture; no request reaches the network.
    context.route(re.compile(r".*"), lambda route: route.abort())
    context.route(re.compile(r"^https://" + re.escape(HOST) + "/"), serve)
    page = context.new_page()
    page.goto(f"https://{HOST}/m/")
    page.get_by_test_id("m-tab-chats").click()
    until(lambda: page.get_by_test_id("m-project-head").count() == 4)
    head = page.get_by_test_id("m-project-head").nth(2)
    head.click()
    check("RD-5 blocked storage: idle group opens", head.get_attribute("aria-expanded") == "true")
    page.get_by_test_id("m-tab-inbox").click()
    page.get_by_test_id("m-tab-chats").click()
    until(lambda: head.count() == 1)
    check("RD-5 blocked storage: choice survives rerender", head.get_attribute("aria-expanded") == "true")
    context.close()


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            run(browser, v2.check, v2.until)
        except Exception as exc:
            v2.check("RD-5 scenario completes", False, f"{type(exc).__name__}: {exc}")
        finally:
            browser.close()
    print(f"{v2.CHECKS} phone fold checks, {len(v2.FAILURES)} failures")
    return bool(v2.FAILURES)


if __name__ == "__main__":
    sys.exit(main())
