"""The morning review at the desk (MR-10, docs/plans/2026-10-28-morning-review.md):
the HUD's "Morning review" view, against `GET /inbox/review`, `POST
/inbox/review/approve` and `/leave` played by `hud_v2_mock_inbox.py`.

Free and hermetic: its own `MockDaemon(0)` (an ephemeral port, never
8402/8403/8405; `guard` refuses the live ports and watches the CSP).

What it pins (each names the mutation it is meant to catch):
  open      the sidebar's "Morning review" row under the Inbox opens the
            view; its count is the review's open items; the title stays
            `J.A.R.V.I.S.` [no row; a title change]
  ticks     no box is ticked when the view opens; a click ticks only its own
            box; nothing is selected by anything else [pre-ticked; select all]
  dialog    Approve selected opens a confirmation listing exactly the ticked
            items (chat, tool, the command's first line) with Cancel focused;
            Enter and Escape cancel and post nothing; only its Approve posts
            exactly those ids and tags [Approve focused; Enter approves;
            a post without the dialog]
  changed   a 409 `changed` unticks what changed and says why
  blockers  a ticked item that cannot be approved turns Approve off and the
            footer names it; Approve and Leave sit far apart
  inert     under a live approval card the view is inert, its controls off,
            and a trusted click posts nothing [not inert]
  hostile   a hostile chat title and command stay text [innerHTML]

`JARVIS_REVIEW_SHOTS=<dir>` also saves the view and its dialog as PNGs.

Run alone (after `cd hud && npm run build`):
  systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 -- env PYTHONPATH=$PWD \\
    /home/johnw/projects/Jarvis/.venv/bin/python tests/face/hud_v2_review_check.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.face import hud_v2_mock_inbox as mock_inbox  # noqa: E402
from tests.face.hud_v2_inbox_check import SINGLE, TITLE, card, _iso  # noqa: E402
from tests.face.hud_v2_multichat_check import _fresh  # noqa: E402

HOSTILE = '<img src=x onerror="window.__pwned=(window.__pwned||0)+1">'


def defer_row(did, thread="t1", command="make deploy"):
    return {"id": f"defer:{did}", "kind": "deferred", "rank": 4, "title": "Bash is waiting for your morning approval",
            "blocks": f"chat {thread} was told to carry on without it", "project_id": None, "thread_id": thread,
            "task_id": None, "asked_at": _iso(-3600), "expires_at": None, "origin": "",
            "detail": {"deferral_id": did, "tool": "Bash", "grantable": True, "why_not": "", "expired": False,
                       "hud_only": False, "count": 1, "more": 0,
                       "card": {"tool": "Bash", "command": command + "\nsecond line", "args": {"command": command}}}}


def item(row, *, approve=True, why="", place="chat:t1"):
    return {"row": row, "section": "overnight", "place": place, "approve": approve, "leave": True, "why": why,
            "tag": (row["id"][-1] * 64 if row["id"][-1] in "0123456789abcdef" else "e" * 64) if approve else None}


def report():
    rows = [defer_row("a", command="make a " + HOSTILE), defer_row("b", command="make b"),
            defer_row("c", thread="t2", command="make c")]
    items = [item(rows[0]), item(rows[1]), item(rows[2], approve=False, why="chat-closed", place="chat:t2")]
    return {"v": 1, "quiet": {"enabled": True, "now": False, "start": None, "end": None},
            "summary": {"approvals": 3, "questions": 0, "decisions": 0, "blocked": 0, "proposals": 0, "live": 0},
            "live": [], "overnight": items, "before": [], "fyi": {"count": 1}, "more": 0, "open": 3,
            "limits": {"approve": 10, "leave": 25, "shown": 60}, "projects": [{"id": "p1", "name": "Calc"}],
            "places": [{"key": "chat:t1", "kind": "chat", "id": "t1", "project_id": "p1", "title": "Fix " + HOSTILE},
                       {"key": "chat:t2", "kind": "chat", "id": "t2", "project_id": "p1", "title": "Other"}]}


def _view(page):
    return page.locator('[data-testid="review-view"]')


def _posts(mock):
    return list(mock_inbox.state(mock).get("review_posts", []))


def _active(page):
    return page.evaluate("() => document.activeElement && (document.activeElement.getAttribute('data-testid') "
                         "|| document.activeElement.tagName)")


def review_checks(browser, mock, base, check, until, guard, init_script):
    print("\nthe morning review at the desk (MR-10)")
    ctx = browser.new_context(viewport={"width": 1600, "height": 900})
    guard(ctx)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    try:
        s = mock_inbox.state(mock)
        s.update({"rows": [], "fail": None, "answer_status": {}, "review": report(), "review_posts": [],
                  "review_reply": {}})
        mock.world["approvals"] = []
        before = mock.sse_connections()
        page.goto(base + "/")
        page.wait_for_selector('[data-testid="sidebar"]', state="attached")
        mock.await_reconnect(before)
        _fresh(page, mock, until, SINGLE)
        row = page.locator('[data-testid="owner-review"]')
        until(lambda: row.count() == 1, timeout=4)
        order = page.evaluate("() => [...document.querySelectorAll('#sidebar button.tree-row')].slice(0, 2)"
                              ".map(b => b.dataset.testid)")
        check("the sidebar's Morning review row sits under the Inbox [mutation: no row]",
              order == ["owner-inbox", "owner-review"], str(order))
        until(lambda: page.locator('[data-testid="owner-review-count"]').count() == 1, timeout=4)
        check("its count is the review's open items",
              page.locator('[data-testid="owner-review-count"]').inner_text().strip() == "3")
        row.click()
        until(lambda: _view(page).count() == 1 and _view(page).get_attribute("data-status") == "ready", timeout=4)
        check("the title stays J.A.R.V.I.S. [mutation: a title change]", page.title() == TITLE, page.title())
        if __import__("os").environ.get("JARVIS_REVIEW_SHOTS"):
            page.screenshot(path=str(Path(__import__("os").environ["JARVIS_REVIEW_SHOTS"]) / "hud-review.png"))
        imgs = page.evaluate("() => document.querySelectorAll('[data-testid=\"review-view\"] img').length")
        check("a hostile title and command stay text [mutation: innerHTML]",
              imgs == 0 and page.evaluate("window.__pwned === undefined")
              and HOSTILE in _view(page).inner_text(), str(imgs))
        ticks = page.locator('[data-testid="review-tick"]')
        checked = page.evaluate("() => [...document.querySelectorAll('[data-testid=\"review-tick\"]')].map(b => b.checked)")
        check("no box is ticked when the view opens [mutation: pre-ticked]",
              len(checked) == 3 and not any(checked), str(checked))
        check("there is no select-all [mutation: select all]",
              "select all" not in _view(page).inner_text().lower()
              and page.locator('[data-testid="review-view"] input[type="checkbox"]').count() == 3)
        page.locator('[data-testid="review-tick"][data-id="defer:a"]').click()
        checked = page.evaluate("() => [...document.querySelectorAll('[data-testid=\"review-tick\"]')].map(b => b.checked)")
        check("a click ticks only its own box", checked == [True, False, False], str(checked))
        # A ticked item that cannot be approved: Approve off, the footer names it.
        page.locator('[data-testid="review-tick"][data-id="defer:c"]').click()
        approve = page.locator('[data-testid="review-approve"]')
        check("a ticked item that cannot be approved turns Approve off and the footer names it",
              approve.is_disabled() and "closed or archived" in page.locator('[data-testid="review-blockers"]').inner_text())
        page.locator('[data-testid="review-tick"][data-id="defer:c"]').click()
        page.locator('[data-testid="review-tick"][data-id="defer:b"]').click()
        leave_box = page.locator('[data-testid="review-leave"]').bounding_box()
        approve_box = approve.bounding_box()
        gap = approve_box["x"] - (leave_box["x"] + leave_box["width"]) if leave_box and approve_box else 0
        check("Leave selected sits left and Approve selected right, far apart", gap >= 200, f"{gap:.0f}px")
        # The confirmation: exactly the ticked items, Cancel focused.
        approve.click()
        dialog = page.locator('[data-testid="review-confirm"]')
        until(lambda: dialog.count() == 1, timeout=3)
        shots = __import__("os").environ.get("JARVIS_REVIEW_SHOTS")
        if shots:
            page.screenshot(path=str(Path(shots) / "hud-review-confirm.png"))
        listed = page.evaluate("() => [...document.querySelectorAll('[data-testid=\"review-confirm-item\"]')]"
                               ".map(li => li.dataset.id)")
        check("the dialog lists exactly the selection", listed == ["defer:a", "defer:b"], str(listed))
        first = page.locator('[data-testid="review-confirm-item"]').first.inner_text()
        check("each line names the chat, the tool and the command's first line only",
              "Tool: Bash" in first and "make a" in first and "second line" not in first, first[:200])
        check("Cancel is focused [mutation: Approve focused]", _active(page) == "review-confirm-cancel",
              str(_active(page)))
        page.keyboard.press("Enter")
        until(lambda: dialog.count() == 0, timeout=3)
        time.sleep(0.3)
        check("Enter cancels and posts nothing [mutation: Enter approves]", _posts(mock) == [], str(_posts(mock)))
        approve.click()
        until(lambda: dialog.count() == 1, timeout=3)
        page.locator('[data-testid="review-confirm-approve"]').focus()
        page.keyboard.press("Enter")
        until(lambda: dialog.count() == 0, timeout=3)
        time.sleep(0.3)
        check("Enter on its Approve still cancels", _posts(mock) == [], str(_posts(mock)))
        approve.click()
        until(lambda: dialog.count() == 1, timeout=3)
        page.keyboard.press("Escape")
        until(lambda: dialog.count() == 0, timeout=3)
        check("Escape cancels and posts nothing", _posts(mock) == [], str(_posts(mock)))
        # A 409 changed: unticked, said why.
        s["review_reply"] = {"approve": (409, {"error": "changed", "changed": [{"id": "defer:b", "reason": "answered"}]})}
        approve.click()
        until(lambda: dialog.count() == 1, timeout=3)
        page.locator('[data-testid="review-confirm-approve"]').click()
        until(lambda: len(_posts(mock)) == 1, timeout=3)
        posted = _posts(mock)[0]
        check("only the dialog's Approve posts, exactly those ids and tags",
              posted == ("/inbox/review/approve", {"items": [{"id": "defer:a", "tag": "a" * 64},
                                                           {"id": "defer:b", "tag": "b" * 64}]}), str(posted))
        until(lambda: page.locator('[data-testid="review-note"]').count() == 1, timeout=3)
        checked = page.evaluate("() => [...document.querySelectorAll('[data-testid=\"review-tick\"]')].map(b => b.checked)")
        check("a 409 unticks what changed and says why", checked == [True, False, False]
              and "Unticked" in page.locator('[data-testid="review-note"]').inner_text(), str(checked))
        s["review_reply"] = {}
        # Under a live card the view is inert.
        c = card("revcard0001")
        mock.world["approvals"].append(c)
        mock.emit("approval_requested", c)
        cardloc = page.locator('[data-testid="approval-card"]')
        until(lambda: cardloc.count() == 1, timeout=4)
        until(lambda: _view(page).evaluate("e => !!e.closest('[inert]')"), timeout=3)
        check("under a card the view is inert and Approve off [mutation: not inert]",
              _view(page).evaluate("e => !!e.closest('[inert]')") and approve.is_disabled())
        posts = len(_posts(mock))
        box = page.locator('[data-testid="review-tick"][data-id="defer:b"]').bounding_box()
        if box:
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        time.sleep(0.3)
        checked = page.evaluate("() => [...document.querySelectorAll('[data-testid=\"review-tick\"]')].map(b => b.checked)")
        check("a trusted click under the card ticks and posts nothing",
              checked == [True, False, False] and len(_posts(mock)) == posts and cardloc.count() == 1, str(checked))
        page.keyboard.press("Escape")
        until(lambda: cardloc.count() == 0, timeout=4)
        check("the title is still J.A.R.V.I.S.", page.title() == TITLE, page.title())
        check("no page errors in the review section", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()


def main():
    from playwright.sync_api import sync_playwright

    from tests.face import hud_v2_check as H
    from tests.face.hud_v2_mock import DIST, MockDaemon

    if not (DIST / "index.html").exists():
        print("hud/dist is not built. Run: cd hud && npm ci --ignore-scripts && npm run build")
        return 2
    mock = MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--use-fake-ui-for-media-stream",
                                                              "--use-fake-device-for-media-stream"])
            try:
                review_checks(browser, mock, f"http://127.0.0.1:{mock.port}", H.check, H.until, H.guard,
                              H.FAKE_RECOGNIZER)
            except Exception as e:  # a section that cannot run is a failure
                H.check("the review section ran to the end", False, str(e).splitlines()[0][:300])
            browser.close()
    finally:
        mock.stop()
    print()
    if H.FAILURES:
        print(f"{len(H.FAILURES)} of {H.CHECKS} checks FAILED:")
        for f in H.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {H.CHECKS} review checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
