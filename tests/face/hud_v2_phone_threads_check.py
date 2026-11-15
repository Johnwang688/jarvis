"""The phone's thread list (plan C, PX-2) in a real headless Chromium.

The built `mobile.html` and its assets are served from `hud/dist` through
Playwright routes under the listener's own security headers (the CSP is the
real `REMOTE_POLICY`); the read API is a fake answering per scenario, so no
daemon, passkey or live port is involved. What it pins, at 375 and 390 px:

  - the three bands in order (Needs you, Running, Recent), with an approval
    and a question INSIDE their thread's card, a row on a thread the working
    list does not hold (an archived thread) drawn as its own card, a row with
    no thread drawn as a row card, and a running task seeded from the tasks
    read (no entry, no inbox row);
  - every received inbox row drawn exactly once;
  - the summary strip (`Needs you · N`, `Running · M`, `Tasks`), and a chip
    filtering to its band and back;
  - a working 503 beside inbox 200s: "In progress unavailable" in Running, the
    app never offline (no banner, the connection dot live), every row still
    drawn;
  - the "the rest are at the desk" notice when the working list was cut;
  - 48 px targets for this package's controls, no sideways scroll at 320, 375
    and 390, no inline style and no CSP violation;
  - the approval card still opens from its row with focus on its heading (its
    DENY/AUTHORIZE gap is pinned by hud_v2_phone_answer_check.py).

Screenshots, when JARVIS_PX2_SHOTS names a directory, are written there.
Skipped when `hud/dist` has no phone shell (run `npm run build` in hud/).

Run: PYTHONPATH=. .venv/bin/python tests/face/hud_v2_phone_threads_check.py
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

HOST = "jarvis-box.tail-test.ts.net"
DIST = REPO / "hud" / "dist"
HOSTILE = '<img src=x onerror="window.__pwned=7"><b>bold</b>'
T_NEEDS, T_WORK, T_IDLE, T_FAILED, T_ARCHIVED = "a0000001", "a0000002", "a0000003", "a0000004", "a0000009"
K_RUN, K_BLOCKED = "b0000001", "b0000002"
PROJECT = "c0000001"
CMD = "npm test"
DETAIL = {
    "card": {"req_id": "r1", "tool": "Bash", "args": {"command": CMD}, "command": CMD, "reason": "runs the tests", "layer": "reviewer",
             "origin": "chat x", "headline": "Run the test suite", "asked_at": ago(3) if False else "2026-11-15T10:00:00+00:00",
             "timeout_s": 600, "allowlistable": True, "summary": "Runs the project's tests.", "summary_state": "ready"},
    "private": False, "withheld": False, "secure": {"ok": True, "failed": None, "label": "", "reason": ""},
    "can": {"deny": True, "allow": True, "always": True}, "why": "",
}


def ago(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def row(rid, kind, rank, title, detail=None, **over):
    base = {"id": rid, "kind": kind, "rank": rank, "title": title, "blocks": "", "project_id": None, "thread_id": None,
            "task_id": None, "asked_at": ago(3), "expires_at": None, "origin": "", "detail": detail or {}}
    base.update(over)
    return base


def entry(tid, status, title, minutes):
    return {"thread_id": tid, "project_id": PROJECT, "project": "Calc", "title": title, "status": status, "last": ago(minutes)}


def task(tid, state, brief, minutes=10):
    return {"id": tid, "project_id": PROJECT, "brief": brief, "state": state, "updated": ago(minutes),
            "status": {"step": 2, "steps": 5}, "plan": [], "spec": {}}


def world():
    rows = [
        row("appr:r1", "approval", 1, "Claude asks to use Bash",
            {"tool": "Bash", "command": "npm test", "headline": "Run the test suite"}, thread_id=T_NEEDS),
        row("q:" + T_NEEDS + ":1", "question", 2, "Claude asks you", {"text": "Which branch should I use?", "options": ["main", "dev"]},
            thread_id=T_NEEDS),
        row("q:" + T_ARCHIVED + ":1", "question", 3, "An old chat asks you", {"text": "Still there?", "options": []},
            thread_id=T_ARCHIVED, origin="Old docs chat"),
        row("pr:Johnwang688/Calc#12", "pr", 5, "PR #12 is ready to merge", {}),
        row("tq:" + K_BLOCKED + ":0", "task_question", 2, "A task asks you", {"text": "Rebase?", "options": []}, task_id=K_BLOCKED),
        row("missed:x1", "missed", 8, "Denied while you slept", {"text": "n", "n": 1}),
    ]
    return {
        "rows": rows,
        "inbox_more": False,
        "working": {"entries": [entry(T_NEEDS, "needs_input", "Fix the build " + HOSTILE, 4), entry(T_WORK, "working", "Add multiply", 1),
                                entry(T_IDLE, "idle", "Notes", 90), entry(T_FAILED, "failed", "Refactor", 200)],
                    "more": False, "total": 4},
        "working_status": 200,
        "tasks": [task(K_RUN, "running", "Implement divide"), task(K_BLOCKED, "blocked", "Needs a rebase")],
        "answers": [],
    }


def run(browser, check, until, shots=None):
    print("\nThe phone's thread list (PX-2): bands, cards, the strip, the working route's own state")
    if not (DIST / "mobile.html").is_file():
        print("  skip  hud/dist has no phone shell (run npm run build in hud/)")
        return
    from jarvis.v2.remote_listener import SECURITY_HEADERS
    state = world()
    seen: list[str] = []
    calls: list[tuple[str, str]] = []

    def api(route):
        request = route.request
        path = request.url.split(HOST, 1)[1].split("?", 1)[0]
        seen.append(path)
        calls.append((request.method, path))
        if request.method == "POST":
            state["answers"].append((path, request.post_data))
            return route.fulfill(status=200, json={"note": "Sent."})
        if path == "/m/api/me":
            return route.fulfill(json={"signed_in": True, "secure": {"ok": True}})
        if path == "/m/api/inbox":
            return route.fulfill(json={"rows": state["rows"], "more": state["inbox_more"]})
        if path == "/m/api/working":
            if state["working_status"] != 200:
                return route.fulfill(status=state["working_status"], json={"error": "the In progress list is not running"})
            return route.fulfill(json=state["working"])
        if path == "/m/api/tasks":
            return route.fulfill(json={"tasks": state["tasks"], "more": state.get("tasks_more", False)})
        if path == "/m/api/review":
            return route.fulfill(json=state.get("review", {"open": 0}))
        if path == "/m/api/projects":
            return route.fulfill(json={"projects": [{"id": PROJECT, "name": "Calc"}]})
        if path == "/m/api/threads":
            return route.fulfill(json={"threads": [{"id": T_IDLE, "project_id": PROJECT, "title": "Notes", "provider": "claude",
                                                    "updated": ago(90), "running": False, "turns": 2}]})
        if path == "/m/api/inbox/appr%3Ar1/detail":
            return route.fulfill(json=DETAIL)
        if (m := re.fullmatch(r"/m/api/threads/([a-z0-9]+)/log", path)):
            return route.fulfill(json={"rows": [{"seq": 1, "role": "Claude", "text": "Hello from the log", "at": None, "marks": []}],
                                       "next": 2, "older": False, "thread": {"id": m.group(1), "title": "A chat", "project_id": PROJECT}})
        if (m := re.fullmatch(r"/m/api/tasks/([a-z0-9]+)", path)):
            found = [t for t in state["tasks"] if t["id"] == m.group(1)]
            return route.fulfill(json={"task": found[0]}) if found else route.fulfill(status=404, json={"error": "not found"})
        if path == "/m/api/events":
            return route.fulfill(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"},
                                 body="retry: 600000\n: hello\n\n")
        return route.fulfill(status=404, json={"error": "not found"})

    def serve(route):
        request = route.request
        path = request.url.split(HOST, 1)[1].split("?", 1)[0]
        if path.startswith("/m/api/"):
            return api(route)
        rel = "mobile.html" if path in ("/m/", "/m") else path[len("/m/"):] if path.startswith("/m/") else ""
        target = (DIST / rel).resolve()
        if not rel or DIST.resolve() not in target.parents and target != DIST.resolve() or not target.is_file():
            return route.fulfill(status=404, body="no")
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        headers = dict(SECURITY_HEADERS)
        headers["Content-Type"] = kind
        return route.fulfill(status=200, headers=headers, body=target.read_bytes())

    def open_page(width, height=2200):
        ctx = browser.new_context(viewport={"width": width, "height": height}, ignore_https_errors=True, has_touch=False)
        ctx.route(re.compile(r".*"), lambda r: r.abort())
        ctx.route(re.compile(r"^https://" + re.escape(HOST) + r"/"), serve)
        page = ctx.new_page()
        page.set_default_timeout(10000)
        problems: list[str] = []
        page.on("pageerror", lambda e: problems.append(str(e)))
        page.on("console", lambda m: problems.append(m.text) if "Content Security Policy" in m.text else None)
        page.goto(f"https://{HOST}/m/")
        return ctx, page, problems

    def shot(page, name, element=None):
        if not shots:
            return
        target = page.locator(element) if element else page
        target.screenshot(path=str(Path(shots) / f"{name}.png"))

    for width in (375, 390):
        state.update(world())
        ctx, page, problems = open_page(width)
        tid = lambda i: page.locator(f'[data-testid="{i}"]')  # noqa: E731
        try:
            until(lambda: tid("m-band-needs").count() == 1 and tid("m-inbox-row").count() >= 1, timeout=10)
            until(lambda: tid("m-band-running").count() == 1)
            tag = f"@{width}"
            order = page.evaluate("Array.from(document.querySelectorAll('section.m-band')).map(s => s.dataset.band)")
            check(f"PX-2 bands: Needs you, Running, Recent in order {tag}", order == ["needs", "running", "recent"], str(order))
            check(f"PX-2 summary strip: Needs you · N, Running · M, Tasks {tag}",
                  tid("m-chip-needs").text_content() == "Needs you · 5" and tid("m-chip-running").text_content() == "Running · 2"
                  and tid("m-chip-tasks").text_content() == "Tasks",
                  " / ".join(tid(i).text_content() for i in ("m-chip-needs", "m-chip-running", "m-chip-tasks")))
            ids = page.evaluate("Array.from(document.querySelectorAll('[data-testid=m-inbox-row]')).map(r => r.dataset.id)")
            want = sorted(r["id"] for r in state["rows"])
            check(f"PX-2 IN-8: every received row drawn exactly once {tag}", sorted(ids) == want, f"{sorted(ids)} vs {want}")
            card = page.locator(f'[data-testid="m-thread"][data-key="thread:{T_NEEDS}"]')
            inside = card.locator('[data-testid="m-inbox-row"]').evaluate_all("els => els.map(e => e.dataset.id)")
            check(f"PX-2 an approval and a question sit inside their thread's card {tag}",
                  inside == ["appr:r1", "q:" + T_NEEDS + ":1"], str(inside))
            check(f"PX-2 an archived thread's row is its own orphan card, titled from its origin {tag}",
                  page.locator(f'[data-key="orphan:{T_ARCHIVED}"] .m-thread-title').inner_text() == "Old docs chat")
            check(f"PX-2 a row with no thread is a row card {tag}", page.locator('[data-key="row:pr:Johnwang688/Calc#12"]').count() == 1)
            check(f"PX-2 a running task with no row and no entry is under Running {tag}",
                  page.locator(f'[data-testid="m-band-running"] [data-key="task:{K_RUN}"]').count() == 1)
            check(f"PX-2 a blocked task's row lands in its task card, under Needs you {tag}",
                  page.locator(f'[data-testid="m-band-needs"] [data-key="task:{K_BLOCKED}"] [data-id="tq:{K_BLOCKED}:0"]').count() == 1)
            check(f"PX-2 hostile thread text is text, never markup {tag}",
                  page.locator("[data-testid=m-main] img, [data-testid=m-main] b").count() == 0
                  and "<img" in card.inner_text() and not problems, str(problems))
            check(f"PX-2 the working route answering 200: no unavailable line {tag}", tid("m-working-unavailable").count() == 0)
            small = page.evaluate("""() => Array.from(document.querySelectorAll('.tap.chip, .m-thread-open, .m-recent-all'))
                .map(e => [e.dataset.testid, Math.round(e.getBoundingClientRect().height)]).filter(([, h]) => h < 48)""")
            check(f"PX-2 the strip's chips and thread heads are at least 48 px tall {tag}", not small, str(small))
            wide = page.evaluate("[document.documentElement.scrollWidth, innerWidth, document.querySelector('main').scrollWidth, document.querySelector('main').clientWidth]")
            check(f"PX-2 no sideways scroll {tag}", wide[0] <= wide[1] and wide[2] <= wide[3], str(wide))
            check(f"PX-2 no inline style attribute, no CSP violation {tag}", page.locator("[data-testid=m-main] [style]").count() == 0 and not problems, str(problems))
            shot(page, f"bands-mixed-{width}")
            shot(page, f"thread-card-approval-question-{width}", f'[data-testid="m-thread"][data-key="thread:{T_NEEDS}"]')
            shot(page, f"summary-strip-{width}", '[data-testid="m-strip"]')
            # A chip filters to its band, and again shows everything.
            tid("m-chip-running").click()
            until(lambda: tid("m-band-needs").count() == 0)
            check(f"PX-2 the Running chip filters to Running, pressed {tag}",
                  tid("m-band-running").count() == 1 and tid("m-chip-running").get_attribute("aria-pressed") == "true"
                  and tid("m-chip-needs").text_content() == "Needs you · 5")
            tid("m-chip-running").click()
            until(lambda: tid("m-band-needs").count() == 1)
            check(f"PX-2 the chip again shows everything {tag}", tid("m-band-recent").count() == 1)
            # The Tasks chip is a filter, not a tab.
            check(f"PX-2 Tasks is a chip, not a tab {tag}", tid("m-tab-tasks").count() == 0)
            tid("m-chip-tasks").click()
            until(lambda: tid("m-new-task-open").count() == 1)
            check(f"PX-2 the Tasks chip shows the tasks, headed by New task {tag}", tid("m-task").count() == 2)
            tid("m-chip-tasks").click()
            until(lambda: tid("m-band-needs").count() == 1)
            # The approval's own card still opens from its row (the sheet is PX-4's).
            page.locator('[data-id="appr:r1"] [data-testid="m-item-review"]').click()
            until(lambda: tid("m-card-heading").count() == 1)
            check(f"PX-2 the approval's card still opens, focus on its heading {tag}",
                  page.evaluate("document.activeElement?.dataset?.testid") == "m-card-heading")
        finally:
            ctx.close()

    # -- the working route's own failure: never the offline state ------------------------------
    for width in (375, 390):
        state.update(world())
        state["working_status"] = 503
        ctx, page, problems = open_page(width)
        tid = lambda i: page.locator(f'[data-testid="{i}"]')  # noqa: E731
        try:
            until(lambda: tid("m-working-unavailable").count() == 1, timeout=10)
            tag = f"@{width}"
            check(f"PX-2 a working 503: 'In progress unavailable' in the Running band {tag}",
                  tid("m-band-running").locator('[data-testid="m-working-unavailable"]').inner_text() == "In progress unavailable")
            check(f"PX-2 a working 503 never shows the offline banner or flips the connection {tag}",
                  not tid("m-offline").is_visible() and tid("m-conn").inner_text() != "offline"
                  and page.evaluate("document.querySelector('[data-screen]').dataset.stale") == "false")
            ids = page.evaluate("Array.from(document.querySelectorAll('[data-testid=m-inbox-row]')).map(r => r.dataset.id)")
            check(f"PX-2 a working 503 hides no row {tag}", sorted(ids) == sorted(r["id"] for r in state["rows"]), str(ids))
            check(f"PX-2 a working 503: a running task still shows, from the tasks read {tag}",
                  page.locator(f'[data-key="task:{K_RUN}"]').count() == 1)
            check(f"PX-2 with working down, the thread's rows fall to orphan cards, still under Needs you {tag}",
                  page.locator(f'[data-testid="m-band-needs"] [data-key="orphan:{T_NEEDS}"]').count() == 1)
            shot(page, f"in-progress-unavailable-{width}", '[data-testid="m-band-running"]')
        finally:
            ctx.close()

    # -- a big count stays whole in its chip -------------------------------------------------------
    for width in (375, 390):
        state.update(world())
        state["rows"] = [row(f"q:{i:03d}", "question", 3, f"Question {i}", {"text": "Which?", "options": []}) for i in range(125)]
        ctx, page, problems = open_page(width, 900)
        try:
            page.wait_for_selector('[data-testid="m-chip-needs"] .m-chip-count', timeout=10000)
            geometry = page.evaluate("""() => {
                const chip = document.querySelector('[data-testid="m-chip-needs"]');
                const count = chip.querySelector('.m-chip-count').getBoundingClientRect();
                const box = chip.getBoundingClientRect();
                const label = chip.querySelector('.m-chip-label');
                return {text: chip.textContent, labelClipped: label.scrollWidth > label.clientWidth, fits: count.right <= box.right + 0.5 && count.left >= box.left - 0.5,
                        height: box.height};
            }""")
            check(f"PX-2 the full 'Needs you · 125' is visible in its chip @{width}",
                  geometry["text"] == "Needs you · 125" and not geometry["labelClipped"] and geometry["fits"] and geometry["height"] >= 48, str(geometry))
            shot(page, f"strip-125-{width}", '[data-testid="m-strip"]')
        finally:
            ctx.close()

    # -- the rest are at the desk ---------------------------------------------------------------
    state.update(world())
    state["working"] = {"entries": [entry(f"d{i:07x}", "idle", f"Chat {i}", 30 + i) for i in range(100)], "more": True, "total": 137}
    state["inbox_more"] = True
    ctx, page, problems = open_page(390)
    tid = lambda i: page.locator(f'[data-testid="{i}"]')  # noqa: E731
    try:
        until(lambda: tid("m-working-more").count() == 1, timeout=10)
        check("PX-2 a cut working list says 'Showing 100 of 137: the rest are at the desk'",
              tid("m-working-more").inner_text() == "Showing 100 of 137: the rest are at the desk", tid("m-working-more").inner_text())
        check("PX-2 a cut inbox says so too", tid("m-inbox-more").count() == 1)
        check("PX-2 Recent shows 5, then Show all (N); the rest stay in the page, folded",
              tid("m-recent-all").inner_text().startswith("Show all (")
              and page.locator('[data-testid="m-recent-rest"]').evaluate("e => e.hidden") is True)
        shot(page, "truncation-notice-390", '[data-testid="m-working-more"]')
        for width in (320,):
            page.set_viewport_size({"width": width, "height": 800})
            wide = page.evaluate("[document.documentElement.scrollWidth, innerWidth, document.querySelector('main').scrollWidth, document.querySelector('main').clientWidth]")
            check(f"PX-2 no sideways scroll at {width}", wide[0] <= wide[1] and wide[2] <= wide[3], str(wide))
    finally:
        ctx.close()

    # -- PX-3b: compact items, "+N for later" and Later ----------------------------------------------
    px3b = os.environ.get("JARVIS_PX3B_SHOTS")
    if px3b:
        Path(px3b).mkdir(parents=True, exist_ok=True)

    def shot3b(page, name, element):
        if px3b:
            page.locator(element).screenshot(path=str(Path(px3b) / f"{name}.png"))

    for width in (375, 390):
        state.update(world())
        state["rows"] = [
            row("appr:r1", "approval", 1, "Claude asks to use Bash",
                {"tool": "Bash", "command": "npm test", "headline": "Run the test suite", "summary": "Runs the project's tests",
                 "summary_state": "ready", "where": {"cwd": "/home/me/calc"}}, thread_id=T_NEEDS, expires_at=ago(-19)),
            row("q:" + T_NEEDS + ":1", "question", 2, "Claude asks you", {"text": "Which branch should I use?", "options": ["main", "dev"]},
                thread_id=T_NEEDS),
            row("q:" + T_NEEDS + ":4", "question", 4, "Claude asks again", {"text": "Rebase first?", "options": []}, thread_id=T_NEEDS),
            row("chk:5", "check", 5, "Check the multiply button", {"text": "Try it on the phone"}, thread_id=T_NEEDS),
            row("missed:6", "missed", 6, "Denied while you slept", {"text": "n", "n": 1}, thread_id=T_NEEDS),
            row("missed:8", "missed", 8, "Another denied call", {"text": "n", "n": 1}, thread_id=T_NEEDS),
        ]
        ctx, page, problems = open_page(width)
        tid = lambda i: page.locator(f'[data-testid="{i}"]')  # noqa: E731
        item = lambda i: page.locator(f'[data-testid="m-inbox-row"][data-id="{i}"]')  # noqa: E731
        tag = f"@{width}"
        try:
            until(lambda: tid("m-item-fold").count() == 1, timeout=10)
            card = f'[data-testid="m-thread"][data-key="thread:{T_NEEDS}"]'
            check(f"PX-3b the approval item is compact: headline, summary, one Review button, no Deny {tag}",
                  item("appr:r1").locator("button").evaluate_all("els => els.map(e => e.dataset.testid)") == ["m-item-review"]
                  and item("appr:r1").locator(".headline").inner_text() == "Run the test suite"
                  and item("appr:r1").locator('[data-testid="m-command"]').count() == 0)
            check(f"PX-3b '+2 for later' folds only ranks above 5; ranks 1 to 5 show {tag}",
                  tid("m-item-fold").inner_text() == "+2 for later"
                  and all(item(i).is_visible() for i in ("appr:r1", "q:" + T_NEEDS + ":1", "q:" + T_NEEDS + ":4", "chk:5"))
                  and not item("missed:6").is_visible() and not item("missed:8").is_visible()
                  and page.locator('[data-testid="m-inbox-row"]').count() == 6)
            shot3b(page, f"mixed-card-{width}", card)
            tid("m-item-fold").click()
            check(f"PX-3b expanding shows the folded rows and says Hide {tag}",
                  item("missed:6").is_visible() and item("missed:8").is_visible()
                  and tid("m-item-fold").get_attribute("aria-expanded") == "true")
            shot3b(page, f"fold-open-{width}", card)
            tid("m-item-fold").click()
            check(f"PX-3b collapsing folds them again {tag}", not item("missed:6").is_visible())
            check(f"PX-3b Later is offered to rank 5 and up, never to ranks 1 to 4 {tag}",
                  all(item(i).locator('[data-testid="m-item-later"]').count() == 0
                      for i in ("appr:r1", "q:" + T_NEEDS + ":1", "q:" + T_NEEDS + ":4"))
                  and item("chk:5").locator('[data-testid="m-item-later"]').count() == 1)
            before = len(state["answers"])
            n_before = len(seen)
            item("chk:5").locator('[data-testid="m-item-later"]').click()
            stub = item("chk:5")
            check(f"PX-3b Later collapses the row to one line with Show; ranks 1 to 4 stay {tag}",
                  stub.get_attribute("data-later") == "true" and stub.locator('[data-testid="m-item-unlater"]').count() == 1
                  and all(item(i).is_visible() for i in ("appr:r1", "q:" + T_NEEDS + ":1", "q:" + T_NEEDS + ":4")))
            check(f"PX-3b Later sends nothing to the server {tag}",
                  len(state["answers"]) == before and len(seen) == n_before, str(seen[n_before:]))
            shot3b(page, f"later-in-use-{width}", card)
            small = page.evaluate("""() => Array.from(document.querySelectorAll('.m-item-review, .m-later, .m-unlater, .m-fold'))
                .filter(e => e.offsetParent !== null).map(e => [e.dataset.testid, Math.round(e.getBoundingClientRect().height)])
                .filter(([, h]) => h < 48)""")
            check(f"PX-3b Review, Later, Show and the fold are at least 48 px tall {tag}", not small, str(small))
            wide = page.evaluate("[document.documentElement.scrollWidth, innerWidth, document.querySelector('main').scrollWidth, document.querySelector('main').clientWidth]")
            check(f"PX-3b no sideways scroll with items {tag}", wide[0] <= wide[1] and wide[2] <= wide[3], str(wide))
            stub.locator('[data-testid="m-item-unlater"]').click()
            check(f"PX-3b Show restores the row {tag}", item("chk:5").get_attribute("data-later") is None
                  and item("chk:5").locator('[data-testid="m-item-later"]').count() == 1)
            item("chk:5").locator('[data-testid="m-item-later"]').click()
            page.reload()
            until(lambda: tid("m-item-fold").count() == 1, timeout=10)
            check(f"PX-3b Later is this page's memory: a reload restores the row {tag}",
                  item("chk:5").get_attribute("data-later") is None and item("chk:5").is_visible())
            tid("m-item-fold").click()
            tid("m-item-review").first.click()
            until(lambda: tid("m-card-heading").count() == 1)
            check(f"PX-3b Review opens the approval's card, focus on its heading {tag}",
                  page.evaluate("document.activeElement?.dataset?.testid") == "m-card-heading")
            check(f"PX-3b no CSP violation or script error {tag}", not problems, str(problems))
        finally:
            ctx.close()

    # -- PX-4: scroll memory, back, the sheet (no browser-history entries), the sheet and the ⋯ menus, at 375 and 390 -------------------
    shots4 = os.environ.get("JARVIS_PX4_SHOTS")

    def shot4(page, name):
        if shots4:
            page.screenshot(path=str(Path(shots4) / f"{name}.png"))

    review = {"v": 1, "quiet": {"enabled": True, "now": False, "start": None, "end": None},
              "summary": {"approvals": 0, "questions": 1, "decisions": 0, "blocked": 0, "proposals": 0, "live": 0}, "live": [],
              "overnight": [{"row": row("q:" + T_NEEDS + ":9", "question", 2, "Which branch?", {"text": "Which branch?", "options": []},
                                        thread_id=T_NEEDS),
                             "section": "overnight", "place": "chat:" + T_NEEDS, "approve": False, "leave": False, "why": "", "tag": None}],
              "before": [], "fyi": {"count": 0}, "more": 0, "open": 5, "limits": {"approve": 10, "leave": 25},
              "projects": [{"id": PROJECT, "name": "Calc"}],
              "places": [{"key": "chat:" + T_NEEDS, "kind": "chat", "id": T_NEEDS, "project_id": PROJECT, "title": "Fix the build"}],
              "group": {"ok": True, "why": ""}, "opens_here": False}
    for width in (375, 390):
        state.update(world())
        state["rows"] = state["rows"] + [row(f"q:f{i:02d}", "question", 4, f"Filler question {i}", {"text": "Which?", "options": []},
                                              thread_id=f"e{i:07x}") for i in range(14)]
        state["review"] = review
        ctx, page, problems = open_page(width, 844)
        tid = lambda i: page.locator(f'[data-testid="{i}"]')  # noqa: E731
        tag = f"@{width}"
        scroll = lambda: page.evaluate("document.querySelector('[data-testid=m-main]').scrollTop")  # noqa: E731
        try:
            until(lambda: tid("m-review-open").count() == 1 and tid("m-inbox-row").count() >= 3, timeout=10)
            line = tid("m-review-open")
            check(f"PX-4 the morning review is a line: 'Morning review · 5 left overnight', 48 px, no Review tab {tag}",
                  line.inner_text().replace("\n", "") == "Morning review · 5 left overnightOpen"
                  and line.bounding_box()["height"] >= 47.5 and not tid("m-tab-review").is_visible(), line.inner_text())
            shot4(page, f"review-line-{width}")
            tid("m-menu").click()
            until(lambda: tid("m-menu-panel").count() == 1)
            box = tid("m-menu-panel").bounding_box()
            rows = page.evaluate("Array.from(document.querySelectorAll('[data-testid=m-menu-panel] > *')).map(e => [e.getBoundingClientRect().height, e.dataset.testid || e.dataset.menuItem])")
            check(f"PX-4 the ⋯ menu is inside the screen, with 48 px rows, Sign out last {tag}",
                  box["x"] >= 0 and box["x"] + box["width"] <= width + 0.5 and all(h >= 47.5 for h, _ in rows) and rows[-1][1] == "m-signout", f"{box} {rows}")
            shot4(page, f"menu-{width}")
            page.keyboard.press("Escape")
            until(lambda: tid("m-menu-panel").count() == 0)

            # Scroll the list, open a chat from a card low in it: no id in the address, no history entry.
            target = '[data-key="orphan:e0000009"] [data-testid="m-thread-open"]'
            page.evaluate("document.querySelector('[data-key=\"orphan:e0000009\"]').scrollIntoView({block: 'center'})")
            before = scroll()
            hist0 = page.evaluate("history.length")
            check(f"PX-4 the list is long enough to scroll {tag}", before > 100, str(before))
            page.locator(target).click()
            until(lambda: tid("m-transcript").count() == 1)
            check(f"PX-4 opening a chat makes no history entry and no id reaches the address {tag}",
                  page.url == f"https://{HOST}/m/" and page.evaluate("history.length") == hist0 and "e0000009" not in page.url,
                  f"{page.url} {page.evaluate('history.length')} vs {hist0}")
            check(f"PX-4 the back button says where it goes {tag}", tid("m-back").inner_text() == "‹ Inbox")
            # The in-app back works with no swipe: the list as it was, the row focused.
            tid("m-back").click()
            until(lambda: tid("m-inbox").count() == 1)
            check(f"PX-4 back: the Inbox is as it was left, scroll kept {tag}", abs(scroll() - before) <= 1, f"{before} -> {scroll()}")
            check(f"PX-4 back: the row it was opened from has the focus {tag}",
                  page.evaluate("document.activeElement.closest('[data-key]')?.dataset.key") == "orphan:e0000009")
            check(f"PX-4 the in-app back left the history and the address alone {tag}",
                  page.evaluate("history.length") == hist0 and page.url == f"https://{HOST}/m/")
            # Again, to the same row: the scroll and the row are remembered every time.
            page.locator(target).click()
            until(lambda: tid("m-transcript").count() == 1)
            tid("m-back").click()
            until(lambda: tid("m-inbox").count() == 1)
            check(f"PX-4 back again: the Inbox, scroll kept, the address unchanged {tag}",
                  abs(scroll() - before) <= 1 and page.url == f"https://{HOST}/m/", f"{before} -> {scroll()} {page.url}")

            # Inbox -> chat -> tap Chats -> open a chat from that list: back goes to the Chats list, not the Inbox.
            page.locator(target).click()
            until(lambda: tid("m-transcript").count() == 1)
            tid("m-tab-chats").click()
            until(lambda: tid("m-chat").count() == 1)
            tid("m-chat").click()
            until(lambda: tid("m-transcript").count() == 1)
            check(f"PX-4 a chat opened from the Chats list says and goes back to Chats, not to the earlier Inbox origin {tag}",
                  tid("m-back").inner_text() == "‹ Chats")
            tid("m-back").click()
            until(lambda: tid("m-chat").count() == 1 and tid("m-transcript").count() == 0)
            check(f"PX-4 back landed on the Chats list {tag}", tid("m-inbox").count() == 0 and tid("m-chat").count() == 1)
            tid("m-tab-inbox").click()
            until(lambda: tid("m-inbox").count() == 1)

            # The approval sheet over the list: the list keeps its scroll; the safety gap holds on the sheet.
            page.evaluate("document.querySelector('[data-testid=m-main]').scrollTop = 0")
            page.locator('[data-id="appr:r1"] [data-testid="m-item-review"]').click()
            until(lambda: tid("m-sheet").count() == 1 and tid("approval-allow").count() == 1)
            # Read once the card has its body (the sheet grows from the bottom edge as it loads), both boxes in one read.
            until(lambda: page.locator(".m-card-body").inner_text() not in ("", "Loading the card…"), timeout=8)
            gap = lambda: page.evaluate("""() => { const d = document.querySelector('[data-testid=approval-deny]'),
                a = document.querySelector('[data-testid=approval-allow]');
                return d && a ? a.getBoundingClientRect().top - d.getBoundingClientRect().bottom : null; }""")  # noqa: E731
            until(lambda: (gap() or 0) > 0, timeout=4)
            check(f"PX-4 sheet over the list: AUTHORIZE is more than 100 px below DENY {tag}", (gap() or 0) > 100, str(gap()))
            check(f"PX-4 sheet over the list: no history entry, the list still in the page {tag}",
                  page.evaluate("history.length") == hist0 and tid("m-inbox").count() == 1)
            shot4(page, f"sheet-{width}")
            page.mouse.click(width / 2, 30)
            until(lambda: tid("m-sheet").count() == 0)
            check(f"PX-4 a tap on the dim area closes the sheet; the focus is back on the row's button {tag}",
                  page.evaluate("document.activeElement?.dataset?.testid") == "m-item-review")
            page.locator('[data-id="appr:r1"] [data-testid="m-item-review"]').click()
            until(lambda: tid("m-sheet").count() == 1)
            tid("m-back").click()
            until(lambda: tid("m-sheet").count() == 0)
            check(f"PX-4 the sheet's back closes it at once, the list kept {tag}",
                  tid("m-inbox").count() == 1 and page.evaluate("history.length") == hist0)

            # A task page, its ⋯ open: Cancel is the only row.
            tid("m-chip-tasks").click()
            until(lambda: tid("m-task").count() == 2)
            page.locator(f'[data-testid="m-task"][data-id="{K_RUN}"]').click()
            until(lambda: tid("m-task-view").count() == 1 and tid("m-task-more").count() == 1)
            tid("m-task-more").click()
            until(lambda: tid("m-task-cancel").count() == 1)
            box = tid("m-task-more-panel").bounding_box()
            check(f"PX-4 the task page's ⋯ opens inside the screen over its header, Cancel the only row {tag}",
                  box["x"] >= 0 and box["x"] + box["width"] <= width + 0.5 and page.locator('[data-testid="m-task-more-panel"] button').count() == 1,
                  str(box))
            shot4(page, f"task-menu-{width}")
            check(f"PX-4 no CSP violation, no page error {tag}", not problems, str(problems))
        finally:
            ctx.close()
    seen_paths = sorted(set(seen))
    # Exact routes, with their methods: reads are GETs of these patterns, and the only POST is an answer.
    get_ok = [r"/m/api/(me|inbox|working|tasks|review|events|projects|threads)", r"/m/api/threads/[a-z0-9]+/log",
              r"/m/api/tasks/[a-z0-9]+", r"/m/api/inbox/[^/]+/detail",
              # WS-5c: the op-token pool, an op's status and Always's rule (reads, session only).
              r"/m/api/ops/tokens", r"/m/api/ops/[A-Za-z0-9_-]{22}", r"/m/api/inbox/[^/]+/answer/rule"]
    post_ok = [r"/m/api/inbox/[^/]+/answer", r"/m/api/(unlock/options|unlock|lock)"]
    stray = [(m, p) for m, p in sorted(set(calls)) if p.startswith("/m/api/") and not (
        (m == "GET" and any(re.fullmatch(g, p) for g in get_ok)) or (m == "POST" and any(re.fullmatch(g, p) for g in post_ok)))]
    check("PX-2 the app calls only today's routes plus WS-5c's, by method (GET reads; a POST only to an answer or the unlock)", not stray,
          str(stray) + " " + json.dumps(seen_paths))
    check("PX-4 no request went to any thread or task action route (only GET log and GET task)",
          not [c for c in calls if re.search(r"/m/api/(threads|tasks)/[^/]+/", c[1]) and not (c[0] == "GET" and c[1].endswith("/log"))])


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    shots = os.environ.get("JARVIS_PX2_SHOTS")
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            run(browser, v2.check, v2.until, shots)
        except Exception as exc:  # noqa: BLE001
            v2.check("Phone thread list scenario completes", False, f"{type(exc).__name__}: {exc}")
        finally:
            browser.close()
    print(f"{v2.CHECKS} phone thread checks, {len(v2.FAILURES)} failures")
    return bool(v2.FAILURES)


if __name__ == "__main__":
    sys.exit(main())
