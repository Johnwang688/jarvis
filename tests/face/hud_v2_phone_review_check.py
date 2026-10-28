"""The phone's Review tab (MR-2 to MR-5, MR-11, MR-12; docs/plans/2026-10-28-
morning-review.md), in a real headless Chromium at an iPhone viewport.

A mock mobile server: the built `mobile.html` and its assets from `hud/dist`,
served as `https://<host>/m/` by Playwright routes, with `/m/api/*` played by
this file (the report, the options and the answers; nothing real is
approved). WebAuthn is a fake: `navigator.credentials.get` answers a
credential-shaped object and counts its calls, standing in for Face ID.

What it pins:
  - a launch (a notification's tap opens `/m/#inbox`) with `opens_here`
    opens the Review tab; without it, the Inbox;
  - no box is ticked at first; there is no select-all;
  - tick 2 of 3: the confirmation lists exactly those 2; the passkey is
    asked once; the POSTs carry exactly those ids and tags;
  - a 409 `changed` unticks that item and says why;
  - a decision answered in place folds to a receipt and the scroll holds
    within one row;
  - each jump chip lands on its chat;
  - Approve is off with a withheld item ticked, and the footer names it;
  - every tap target at least 44 px; Approve and Leave far apart; the
    footer stays on screen; hostile text stays text; no page errors.

Skipped when `hud/dist` has no phone shell (run `npm run build` in hud/).
`JARVIS_REVIEW_SHOTS=<dir>` also saves the tab and its confirmation as PNGs.

Run: PYTHONPATH=. .venv/bin/python tests/face/hud_v2_phone_review_check.py
"""
from __future__ import annotations

import json
import mimetypes
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

HOST = "jarvis-box.tail-test.ts.net"
ORIGIN = "https://" + HOST
HOSTILE = '<img src=x onerror="window.__pwned=(window.__pwned||0)+1"><script>window.__pwned=9</script>'
MARKUP = "img, script, svg, iframe, object, embed, [onerror], [onload]"
FAKE_WEBAUTHN = """
window.__gets = [];
(() => {
  const bytes = (s) => new TextEncoder().encode(s).buffer;
  const fake = {
    get: async (options) => {
      window.__gets.push(JSON.stringify(Array.from(new Uint8Array(options.publicKey.challenge))));
      return { id: "Y3JlZA", rawId: bytes("cred"), type: "public-key", authenticatorAttachment: "platform",
               getClientExtensionResults: () => ({}),
               response: { clientDataJSON: bytes("{}"), authenticatorData: bytes("ad"), signature: bytes("sig"),
                           userHandle: null } };
    },
    create: async () => null,
  };
  try { Object.defineProperty(navigator, "credentials", { configurable: true, get: () => fake }); } catch (e) {}
})();
"""


def _iso(seconds=0.0):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def row(id_, kind, rank, thread, **over):
    out = {"id": id_, "kind": kind, "rank": rank, "title": f"{kind} {id_}", "blocks": "b", "project_id": None,
           "thread_id": thread, "task_id": None, "asked_at": _iso(-3600), "expires_at": None, "origin": "",
           "detail": {}}
    out.update(over)
    return out


def deferred(did, thread, command):
    return row(f"defer:{did}", "deferred", 4, thread, title="Bash is waiting for your morning approval",
               detail={"deferral_id": did, "tool": "Bash", "grantable": True, "why_not": "", "expired": False,
                       "hud_only": False, "count": 1, "more": 0,
                       "card": {"tool": "Bash", "command": command, "args": {"command": command}}})


def item(r, place, *, approve=False, leave=False, why="", tag=None):
    return {"row": r, "section": "overnight", "place": place, "approve": approve, "leave": leave, "why": why,
            "tag": tag}


def report(opens_here):
    items = [
        item(deferred("a", "aaaaaaaa", "make a " + HOSTILE), "chat:aaaaaaaa", approve=True, leave=True, tag="a" * 64),
        item(deferred("b", "aaaaaaaa", "make b"), "chat:aaaaaaaa", approve=True, leave=True, tag="b" * 64),
        item(deferred("c", "aaaaaaaa", "make c [withheld]"), "chat:aaaaaaaa", leave=True, why="withheld"),
        # A third approvable item: two ticked must be exactly the two listed (#329 LOW 5).
        item(deferred("d", "aaaaaaaa", "make d"), "chat:aaaaaaaa", approve=True, leave=True, tag="d" * 64),
    ]
    for i in range(6):
        items.append(item(row(f"q:cccccccc:{i}", "question", 2, "cccccccc",
                              detail={"thread_id": "cccccccc", "req_id": str(i), "text": f"Question {i}? " + "x" * 120,
                                      "options": []}), "chat:cccccccc"))
    items.append(item(row("dec:d0123456789ab", "decision", 6, "bbbbbbbb", detail={
        "decision_id": "d0123456789ab", "question": "Which store? " + HOSTILE, "options": ["SQLite", "Files"],
        "default": 1, "why": "It decides the schema.", "answer": None}), "chat:bbbbbbbb"))
    for i in range(4):
        items.append(item(row(f"q:dddddddd:{i}", "question", 2, "dddddddd",
                              detail={"thread_id": "dddddddd", "req_id": str(i), "text": f"Later {i}? " + "y" * 120,
                                      "options": []}), "chat:dddddddd"))
    return {"v": 1, "quiet": {"enabled": True, "now": False, "start": None, "end": None},
            "summary": {"approvals": 3, "questions": 10, "decisions": 1, "blocked": 0, "proposals": 0, "live": 0},
            "live": [], "overnight": items, "before": [], "fyi": {"count": 0}, "more": 0, "open": len(items),
            "limits": {"approve": 10, "leave": 25, "shown": 60}, "projects": [{"id": "p1", "name": "Calc"}],
            "places": [{"key": "chat:aaaaaaaa", "kind": "chat", "id": "aaaaaaaa", "project_id": "p1",
                        "title": "Deploy " + HOSTILE},
                       {"key": "chat:bbbbbbbb", "kind": "chat", "id": "bbbbbbbb", "project_id": "p1", "title": "Schema"},
                       {"key": "chat:cccccccc", "kind": "chat", "id": "cccccccc", "project_id": "p1", "title": "Docs"},
                       {"key": "chat:dddddddd", "kind": "chat", "id": "dddddddd", "project_id": "p1", "title": "Tests"}],
            "group": {"ok": True, "why": ""}, "opens_here": opens_here}


class Mock:
    def __init__(self, dist: Path):
        self.dist = dist
        self.review = report(True)
        self.posts: list[tuple[str, dict]] = []
        self.replies: dict[str, tuple[int, dict]] = {}

    def __call__(self, route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path
        if url.netloc != HOST:
            return route.abort()
        if path in ("/m/", "/m/pair"):
            return route.fulfill(status=200, content_type="text/html", body=(self.dist / "mobile.html").read_bytes())
        if path.startswith("/m/assets/"):
            file = self.dist / "assets" / path[len("/m/assets/"):]
            if not file.is_file():
                return route.fulfill(status=404, body=b"")
            kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            return route.fulfill(status=200, content_type=kind, body=file.read_bytes())
        if not path.startswith("/m/api/"):
            return route.fulfill(status=404, body=b"")
        if path == "/m/api/events":
            return route.fulfill(status=200, content_type="text/event-stream", body=b"retry: 60000\n: connected\n\n")
        if request.method == "GET":
            payload = {"/m/api/me": {"signed_in": True, "secure": {"ok": True, "failed": None, "label": ""}},
                       "/m/api/inbox": {"rows": [], "more": False},
                       "/m/api/review": self.review,
                       "/m/api/push": {"key": "", "enabled": False, "subscribed": False}}.get(path)
            if payload is None:
                return route.fulfill(status=404, content_type="application/json", body=json.dumps({"error": "not found"}))
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
        body = json.loads(request.post_data or "{}")
        self.posts.append((path, body))
        status, payload = self.replies.get(path, (200, {"ok": True}))
        if path == "/m/api/review/approve/options" and path not in self.replies:
            payload = {"count": len(body.get("items", [])), "publicKey": {
                "challenge": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8", "rpId": HOST, "timeout": 60000,
                "userVerification": "required", "allowCredentials": [{"type": "public-key", "id": "Y3JlZA"}]}}
        return route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))


def run(browser, check, until):
    print("\nThe phone's Review tab (MR): opening, ticks, one passkey for a set, 409, in place, chips")
    dist = REPO / "hud" / "dist"
    if not (dist / "mobile.html").is_file():
        print("  skip  hud/dist has no phone shell (run npm run build in hud/)")
        return
    mock = Mock(dist)
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True,
                              has_touch=True, ignore_https_errors=True, service_workers="block")
    ctx.route("**/*", mock)
    ctx.add_init_script(FAKE_WEBAUTHN)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(6000)
    # The routes are served only while Playwright runs its loop: wait through
    # the page, never with a bare sleep.
    outer = until

    def until(fn, timeout=6.0):  # noqa: F811 — the page-pumping wait
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if fn():
                return True
            page.wait_for_timeout(50)
        return outer(fn, timeout=0.05)

    def pause(seconds):
        page.wait_for_timeout(int(seconds * 1000))

    def selected_tab():
        return page.evaluate("() => { const t = document.querySelector('[role=tab][aria-selected=true]');"
                             " return t && t.dataset.testid; }")

    def ticks():
        return page.evaluate("() => [...document.querySelectorAll('[data-testid=\"m-review-tick\"]')]"
                             ".map(b => [b.dataset.id, b.checked])")

    def tap(selector):
        page.locator(selector).first.tap()

    try:
        # A launch without opens_here: the Inbox.
        mock.review = report(False)
        page.goto(ORIGIN + "/m/#inbox")
        until(lambda: page.locator('[data-testid="m-tab-review"]').count() == 1, timeout=6)
        until(lambda: page.locator('[data-testid="m-inbox"]').count() == 1, timeout=6)
        pause(0.4)
        check("a launch without opens_here opens the Inbox", selected_tab() == "m-tab-inbox", str(selected_tab()))
        check("the Review tab sits beside the Inbox while the review has open items",
              page.locator('[data-testid="m-tab-review"]').is_visible())
        # The notification's tap with opens_here: the Review tab.
        mock.review = report(True)
        page.goto("about:blank")
        page.goto(ORIGIN + "/m/#inbox")
        until(lambda: page.locator('[data-testid="m-review"]').count() == 1, timeout=6)
        check("a tap with opens_here opens the Review tab", selected_tab() == "m-tab-review", str(selected_tab()))
        until(lambda: len(ticks()) == 4, timeout=4)
        check("no box is ticked at first", ticks() == [["defer:a", False], ["defer:b", False], ["defer:c", False],
                                                       ["defer:d", False]],
              str(ticks()))
        view = page.locator('[data-testid="m-review"]')
        check("there is no select-all", "select all" not in view.inner_text().lower())
        scoped = ", ".join(f'[data-testid="m-main"] {sel.strip()}' for sel in MARKUP.split(","))
        shots = __import__("os").environ.get("JARVIS_REVIEW_SHOTS")
        if shots:
            page.screenshot(path=str(Path(shots) / "phone-review.png"))
        check("hostile text stays text", page.evaluate("(q) => document.querySelectorAll(q).length", scoped) == 0
              and page.evaluate("window.__pwned === undefined") and HOSTILE in view.inner_text())
        # Touch targets.
        small = page.evaluate("""() => [...document.querySelectorAll(
            '[data-testid="m-review"] button, [data-testid="m-review"] .m-tick, [data-testid="m-review"] .m-choice')]
            .filter(e => e.offsetParent !== null).map(e => [e.dataset.testid || e.className, e.getBoundingClientRect().height])
            .filter(([, h]) => h < 44)""")
        check("every tap target in the Review tab is at least 44 px tall", small == [], str(small[:5]))
        # A withheld item ticked: Approve off, the footer names it.
        tap('[data-testid="m-review-tick"][data-id="defer:c"]')
        until(lambda: ticks()[2][1] is True, timeout=3)
        approve = page.locator('[data-testid="m-review-approve"]')
        check("Approve is off with a withheld item ticked, and the footer names it",
              approve.is_disabled() and "answer it at the desk" in page.locator('[data-testid="m-review-blockers"]').inner_text())
        tap('[data-testid="m-review-tick"][data-id="defer:c"]')
        # Tick 2 of 3.
        tap('[data-testid="m-review-tick"][data-id="defer:a"]')
        tap('[data-testid="m-review-tick"][data-id="defer:b"]')
        until(lambda: [t[1] for t in ticks()] == [True, True, False, False], timeout=3)
        check("a tap ticks only its own box", [t[1] for t in ticks()] == [True, True, False, False], str(ticks()))
        footer = page.locator('[data-testid="m-review-footer"]').bounding_box()
        check("the footer stays on screen", footer is not None and footer["y"] + footer["height"] <= 844 - 56 + 2,
              str(footer))
        leave_box = page.locator('[data-testid="m-review-leave"]').bounding_box()
        approve_box = approve.bounding_box()
        gap = approve_box["x"] - (leave_box["x"] + leave_box["width"])
        check("Leave selected and Approve selected are far apart", gap >= 48, f"{gap:.0f}px")
        tap('[data-testid="m-review-approve"]')
        until(lambda: page.locator('[data-testid="m-review-confirm"]').count() == 1, timeout=3)
        if shots:
            page.screenshot(path=str(Path(shots) / "phone-review-confirm.png"))
        listed = page.evaluate("() => [...document.querySelectorAll('[data-testid=\"m-review-confirm-item\"]')]"
                               ".map(li => li.dataset.id)")
        check("the confirmation lists exactly those 2", listed == ["defer:a", "defer:b"], str(listed))
        check("nothing is posted and no passkey asked before the confirmation",
              mock.posts == [] and page.evaluate("window.__gets.length") == 0, str(mock.posts))
        mock.replies["/m/api/review/approve"] = (409, {"error": "changed", "changed": [{"id": "defer:b",
                                                                                      "reason": "answered"}]})
        tap('[data-testid="m-review-confirm-go"]')
        until(lambda: len(mock.posts) == 2, timeout=4)
        items = [{"id": "defer:a", "tag": "a" * 64}, {"id": "defer:b", "tag": "b" * 64}]
        check("the POSTs carry exactly those ids and tags, with one assertion",
              mock.posts[0] == ("/m/api/review/approve/options", {"items": items})
              and mock.posts[1][0] == "/m/api/review/approve" and mock.posts[1][1]["items"] == items
              and isinstance(mock.posts[1][1].get("assertion"), dict)
              and page.evaluate("window.__gets.length") == 1, str(mock.posts)[:300])
        until(lambda: page.locator('[data-testid="m-review-flash"]').count() == 1, timeout=3)
        check("a 409 changed unticks that item and explains",
              [t[1] for t in ticks()] == [True, False, False, False]
              and "Nothing was approved" in page.locator('[data-testid="m-review-flash"]').inner_text()
              and "Unticked" in page.locator('[data-id="defer:b"] [data-testid="m-review-note"]').inner_text(),
              str(ticks()))
        # A chip lands on its chat.
        main = page.locator('[data-testid="m-main"]')
        tap('[data-testid="m-review-chip"][data-anchor="review-overnight-chat_dddddddd"]')
        pause(0.4)
        offset = page.evaluate("""() => {
            const m = document.querySelector('[data-testid="m-main"]').getBoundingClientRect().top;
            return document.getElementById('review-overnight-chat_dddddddd').getBoundingClientRect().top - m; }""")
        check("a jump chip lands on its chat", 0 <= offset <= 40, f"{offset:.0f}px")
        # A decision answered in place: the scroll holds within one row.
        decision = page.locator('[data-testid="m-review-item"][data-id="dec:d0123456789ab"]')
        decision.scroll_into_view_if_needed()
        page.evaluate("""() => { const m = document.querySelector('[data-testid="m-main"]');
            const d = document.querySelector('[data-id="dec:d0123456789ab"]');
            m.scrollTop += d.getBoundingClientRect().top - m.getBoundingClientRect().top - 200; }""")
        pause(0.2)
        row_height = decision.bounding_box()["height"]
        top_of = ("(sel) => document.querySelector(sel).getBoundingClientRect().top"
                  " - document.querySelector('[data-testid=\"m-main\"]').getBoundingClientRect().top")
        before = page.evaluate(top_of, '[data-id="dec:d0123456789ab"]')
        mock.replies["/m/api/inbox/decisions/d0123456789ab"] = (200, {"ok": True, "note": "Sent to the asking chat."})
        page.locator('[data-testid="m-review-option"][value="0"]').check()
        tap('[data-testid="m-review-decision-send"]')
        until(lambda: page.locator('[data-testid="m-review-receipt"]').count() == 1, timeout=4)
        pause(0.3)
        after = page.evaluate(top_of, '[data-testid="m-review-receipt"]')
        check("a decision answered in place folds to a receipt where it stood, the scroll within one row",
              abs(after - before) <= row_height and page.locator('[data-testid="m-review-receipt"]').inner_text()
              == "Sent to the asking chat.", f"its place moved {after - before:.0f}px, row {row_height:.0f}px")
        check("the decision's post was its own, once", mock.posts[-1] == ("/m/api/inbox/decisions/d0123456789ab",
                                                                         {"choice": 0}), str(mock.posts[-1]))
        check("no page errors", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()


def main():
    from playwright.sync_api import sync_playwright

    from tests.face import hud_v2_check as H
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            run(browser, H.check, H.until)
        except Exception as e:  # a section that cannot run is a failure
            H.check("the phone review section ran to the end", False, str(e).splitlines()[0][:300])
        browser.close()
    print()
    if H.FAILURES:
        print(f"{len(H.FAILURES)} of {H.CHECKS} checks FAILED:")
        for f in H.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {H.CHECKS} phone review checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
