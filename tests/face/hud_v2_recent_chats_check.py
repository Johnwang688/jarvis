"""Headless checks for the sidebar's "five most recent chats" rule (2026-10-15).

Called from `hud_v2_check.main()` in a browser context of its own and against
**a `MockDaemon` of its own** on an ephemeral port (it opens threads, reloads
and keeps localStorage). It also runs on its own:
`python tests/face/hud_v2_recent_chats_check.py`. It is given `PLAIN_RECOGNIZER`, not
`FAKE_RECOGNIZER`: the latter starts every context with each project's full list asked for
(`SHOW_ALL_CHATS`), which is what the other suites need and this one must not have. `HUD_SHOTS=<dir>` saves a
screenshot of the list there.

A project shows the five chats modified most recently (`Thread.updated`),
newest first, then "Show N more". The checks written to bite:

  - 8 chats show 5, newest first, whatever order the daemon sent them in;
  - the button counts the hidden ones, is a focusable <button> with
    aria-expanded, works from the keyboard, and "Show less" folds it back;
  - the choice is per project and survives a reload;
  - an old chat that is open in a pane stays visible (and the count drops by
    one); once another chat replaces it, it goes behind the button again;
  - an old chat that wants the owner (needs input), is unread, failed or has a
    turn running is never hidden, and goes back when it is idle;
  - a chat modified now moves to the top and pushes the fifth out;
  - a project of five or fewer has no button; a folded project's dot still
    counts the hidden chats; and a task's rows are untouched.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

PROJECT = "p2"          # the mock's second project, which starts with no chats
KEY = "jarvis.hud.sidebar.showmore"


def recent_chats_checks(browser, mock, base, check, until, guard, init_script):
    print("\nthe sidebar's five most recent chats")
    _seed(mock)
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    guard(ctx)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    try:
        before = mock.sse_connections()
        page.goto(base + "/")
        page.wait_for_selector('[data-testid="sidebar"]', state="attached")
        until(lambda: page.locator('[data-testid="project-p1"]').count() > 0)
        mock.await_reconnect(before)
        for section in (_limit_checks, _toggle_checks, _kept_checks, _attention_checks, _recency_checks,
                        _other_checks):
            try:
                section(page, mock, base, check, until)
            except Exception as e:  # a section that cannot run is a failure, and the rest still run
                check(f"recent chats {section.__name__.strip('_')} ran to the end", False,
                      str(e).splitlines()[0][:200])
        check("no page errors in the recent chats section", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()


# ---------------------------------------------------------------------------

def _seed(mock):
    """Eight chats in p2, handed over in a scrambled order: s1 the oldest, s8 the newest."""
    base = next(t for t in mock.world["threads"] if t["id"] == "t1")
    for n in (4, 1, 8, 2, 7, 3, 6, 5):
        mock.world["threads"].append({
            **base, "id": f"s{n}", "project_id": PROJECT, "title": f"chat {n}", "turns": 1,
            "created": "2026-09-20T00:00:00+00:00", "updated": f"2026-10-0{n}T00:00:00+00:00",
        })
        mock.world["transcripts"][f"s{n}"] = []


def _rows(page) -> list[str]:
    """The ids of p2's chat rows, in the order they are drawn."""
    return page.evaluate("""() => [...document.querySelectorAll('[data-testid^="thread-s"]')]
        .map((e) => e.getAttribute('data-testid')).filter((x) => /^thread-s\\d$/.test(x)).map((x) => x.slice(7))""")


def _button(page):
    return page.locator(f'[data-testid="more-chats-{PROJECT}"]')


def _label(page) -> str:
    b = _button(page)
    return b.inner_text().strip(" ▴▾\n") if b.count() else ""


def _reload(page, mock, until):
    before = mock.sse_connections()
    page.reload()
    page.wait_for_selector('[data-testid="sidebar"]', state="attached")
    until(lambda: page.locator('[data-testid="project-p1"]').count() > 0)
    mock.await_reconnect(before)
    _open_project(page, until)


def _open_project(page, until):
    for _ in range(4):
        if _rows(page):
            return
        page.locator(f'[data-testid="project-{PROJECT}"]').click()
        until(lambda: bool(_rows(page)), timeout=1.0)


def _limit_checks(page, mock, base, check, until):
    _open_project(page, until)
    until(lambda: len(_rows(page)) == 5)
    check("eight chats show the five most recently modified, newest first, whatever order they came in",
          _rows(page) == ["s8", "s7", "s6", "s5", "s4"], str(_rows(page)))
    check("the button says how many are behind it", _label(page) == "Show 3 more", _label(page))
    check("the button is a real, focusable <button>, collapsed",
          _button(page).evaluate("e => e.tagName") == "BUTTON"
          and _button(page).get_attribute("aria-expanded") == "false")
    check("the button is the last of the chats, above new task",
          page.evaluate("""() => { const b = document.querySelector('[data-testid="more-chats-p2"]');
            return b.nextElementSibling?.getAttribute('data-testid') === 'new-task-p2'; }"""))
    check("a project with a single chat has no button", page.locator('[data-testid="more-chats-p1"]').count() == 0)
    if os.environ.get("HUD_SHOTS"):
        Path(os.environ["HUD_SHOTS"]).mkdir(parents=True, exist_ok=True)
        page.locator('[data-testid="sidebar"]').screenshot(path=str(Path(os.environ["HUD_SHOTS"]) / "recent-chats-collapsed.png"))


def _toggle_checks(page, mock, base, check, until):
    _button(page).click()
    until(lambda: len(_rows(page)) == 8)
    check("Show more reveals the rest, still newest first",
          _rows(page) == ["s8", "s7", "s6", "s5", "s4", "s3", "s2", "s1"], str(_rows(page)))
    check("and the button now folds back", _label(page) == "Show less"
          and _button(page).get_attribute("aria-expanded") == "true", _label(page))
    if os.environ.get("HUD_SHOTS"):
        page.locator('[data-testid="sidebar"]').screenshot(path=str(Path(os.environ["HUD_SHOTS"]) / "recent-chats-expanded.png"))
    check("the choice is remembered for this project",
          page.evaluate(f"localStorage.getItem('{KEY}')") == f'{{"{PROJECT}":true}}')
    _reload(page, mock, until)
    until(lambda: len(_rows(page)) == 8)
    check("and survives a reload", len(_rows(page)) == 8 and _label(page) == "Show less", str(_rows(page)))
    _button(page).click()
    until(lambda: len(_rows(page)) == 5)
    check("Show less folds back to five", _rows(page) == ["s8", "s7", "s6", "s5", "s4"]
          and _label(page) == "Show 3 more", str(_rows(page)))
    _button(page).focus()
    page.keyboard.press("Enter")
    until(lambda: len(_rows(page)) == 8)
    check("Enter on the focused button shows the rest", len(_rows(page)) == 8)
    page.keyboard.press("Space")
    until(lambda: len(_rows(page)) == 5)
    check("Space folds them back", len(_rows(page)) == 5)
    check("the button kept focus through both", page.evaluate(
        "document.activeElement?.getAttribute('data-testid')") == f"more-chats-{PROJECT}")
    page.evaluate(f"localStorage.setItem('{KEY}', '{{')")
    _reload(page, mock, until)
    until(lambda: len(_rows(page)) == 5)
    check("a corrupt remembered value reads as collapsed, not as a broken sidebar", len(_rows(page)) == 5)


def _kept_checks(page, mock, base, check, until):
    page.locator('[data-testid="thread-s7"]').click()
    until(lambda: page.evaluate("window.__hud.state().threadId") == "s7")
    # Open the oldest chat, which is behind the button: expand, click, fold.
    _button(page).click()
    until(lambda: len(_rows(page)) == 8)
    page.locator('[data-testid="thread-s1"]').click()
    until(lambda: page.evaluate("window.__hud.state().threadId") == "s1")
    _button(page).click()
    until(lambda: _label(page).startswith("Show"))
    until(lambda: len(_rows(page)) == 6)
    check("the open chat stays visible though it is older than the fifth, in its sorted place",
          _rows(page) == ["s8", "s7", "s6", "s5", "s4", "s1"], str(_rows(page)))
    check("and the count only counts what is out of sight", _label(page) == "Show 2 more", _label(page))
    check("it is the selected row", "sel" in (page.locator('[data-testid="thread-s1"]').get_attribute("class") or ""))
    if os.environ.get("HUD_SHOTS"):
        page.locator('[data-testid="sidebar"]').screenshot(path=str(Path(os.environ["HUD_SHOTS"]) / "recent-chats-active-old.png"))
    page.locator('[data-testid="thread-s8"]').click()
    until(lambda: page.evaluate("window.__hud.state().threadId") == "s8")
    until(lambda: len(_rows(page)) == 5)
    check("once another chat replaces it, the old one goes behind the button again",
          _rows(page) == ["s8", "s7", "s6", "s5", "s4"] and _label(page) == "Show 3 more", str(_rows(page)))


def _attention_checks(page, mock, base, check, until):
    for status in ("needs_input", "unread", "failed", "working"):
        mock.activity("thread", "s2", status)
        until(lambda: "s2" in _rows(page), timeout=3)
        check(f"an old chat that is {status} is not hidden", "s2" in _rows(page)
              and _label(page) == "Show 2 more", f"{_rows(page)} {_label(page)}")
    dot = page.locator('[data-testid="activity-s2"]').get_attribute("data-status")
    check("and its dot is still drawn", dot == "working", str(dot))
    mock.activity("thread", "s2", "idle")
    until(lambda: "s2" not in _rows(page), timeout=3)
    check("idle again, it goes back behind the button", _rows(page) == ["s8", "s7", "s6", "s5", "s4"]
          and _label(page) == "Show 3 more", str(_rows(page)))
    # A running turn (the daemon's live flag on the thread record).
    mock.thread_running("s3", "2026-10-15T12:00:00+00:00")
    mock.emit("thread_updated", {"thread_id": "s3", "title": "chat 3"})
    until(lambda: "s3" in _rows(page), timeout=3)
    check("a chat with a turn running is not hidden", "s3" in _rows(page), str(_rows(page)))
    mock.thread_running("s3", None)
    mock.emit("thread_updated", {"thread_id": "s3", "title": "chat 3"})
    until(lambda: "s3" not in _rows(page), timeout=3)
    check("and goes back when the turn ends", "s3" not in _rows(page), str(_rows(page)))
    # A folded project still carries the most urgent dot, hidden chats included.
    mock.activity("thread", "s1", "failed")
    until(lambda: "s1" in _rows(page), timeout=3)
    mock.activity("thread", "s1", "idle")
    mock.activity("thread", "s1", "unread")
    for _ in range(4):
        page.locator(f'[data-testid="project-{PROJECT}"]').click()
        if until(lambda: not _rows(page), timeout=1.0):
            break
    status = page.locator(f'[data-testid="project-activity-{PROJECT}"]').get_attribute("data-status") \
        if page.locator(f'[data-testid="project-activity-{PROJECT}"]').count() else None
    check("a folded project's dot counts the chats behind the button", status == "unread", str(status))
    mock.activity("thread", "s1", "idle")
    _open_project(page, until)
    until(lambda: len(_rows(page)) == 5)


def _recency_checks(page, mock, base, check, until):
    for t in mock.world["threads"]:
        if t["id"] == "s2":
            t["updated"] = "2026-10-15T12:30:00+00:00"
    mock.emit("thread_updated", {"thread_id": "s2", "title": "chat 2"})
    until(lambda: _rows(page)[:1] == ["s2"], timeout=3)
    check("a chat modified now moves to the top and pushes the fifth behind the button",
          _rows(page) == ["s2", "s8", "s7", "s6", "s5"] and _label(page) == "Show 3 more", str(_rows(page)))


def _other_checks(page, mock, base, check, until):
    check("the project's task rows are not trimmed", page.locator('[data-testid="new-task-p2"]').count() == 1)
    page.locator('[data-testid="project-p1"]').click()
    until(lambda: page.locator('[data-testid="thread-t1"]').count() > 0, timeout=2)
    check("another project keeps its own list and has no button",
          page.locator('[data-testid="thread-t1"]').count() == 1 and page.locator('[data-testid="more-chats-p1"]').count() == 0)
    check("the task of that project is still listed", page.locator('[data-testid="task-k1"]').count() == 1)


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
            browser = pw.chromium.launch(headless=True)
            recent_chats_checks(browser, mock, f"http://127.0.0.1:{mock.port}", H.check, H.until,
                                H.guard, H.PLAIN_RECOGNIZER)
            browser.close()
    finally:
        mock.stop()
    print()
    if H.FAILURES:
        print(f"{len(H.FAILURES)} of {H.CHECKS} checks FAILED:")
        for f in H.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {H.CHECKS} recent chats checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
