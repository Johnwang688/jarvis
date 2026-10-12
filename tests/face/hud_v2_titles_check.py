"""Headless checks for chat titles in the HUD (2026-10-12, `lib/threadTitle.ts`).

The owner: "right now each chat just displays its id. we should keep the id
in the three dots but have the main display be the title and have the title
wrap after a certain length."

Free and hermetic: a `MockDaemon(0)` of its own and a browser context of its
own, under `hud_v2_check.guard` (no request to 8402/8403/8405, no CSP
violation). The world gains three chats in p1 for the run: one untitled and
new, one untitled with turns behind it, one with a long title.

What it pins:
  - **never the id**: an untitled row reads "New thread" (no turns yet) or
    "Untitled", muted and italic, and its text holds no id;
  - **wraps, clamped**: a long title wraps (no ellipsis), is clamped at three
    lines, and the whole title is the name's tooltip;
  - **live**: a `thread_updated` carrying a title relabels the row and the
    chat pane's header in a split (which read "New thread" before);
  - **Copy id**: the row's ⋯ menu has "Copy id" showing the id, and it puts
    the id on the clipboard;
  - **text only**: a title holding markup is drawn as characters.

Run: PYTHONPATH=. .venv/bin/python tests/face/hud_v2_titles_check.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.face.hud_v2_layout_check import _boot, _pane, _set_storage, _set_ws  # noqa: E402

WIDE = (1600, 1000)
SPLIT = json.dumps({"preset": "cols2", "focused": 1,
                    "panes": [{"view": "chat"}, {"view": "file"}, {"view": "file"}, {"view": "task"}]})
LONG = ("Planning the robotics lab week around the drivetrain rebuild, the autonomous routine "
        "tuning, the scouting spreadsheet and the parts order that is still waiting on approval")
MARKUP = '<img src=x onerror="window.__pwned=1"> & <b>bold</b>'


def _thread(tid: str, title: str, turns: int) -> dict:
    return {"id": tid, "project_id": "p1", "role": "chat", "provider": "fast",
            "provider_session_id": None, "task_id": None, "title": title, "title_source": "",
            "created": "2026-10-12T00:00:00+00:00", "updated": "2026-10-12T00:00:00+00:00",
            "turns": turns, "cost_usd": 0.0, "tokens": 0, "model": None, "effort": None,
            "cwd": "/home/johnw/projects/Jarvis"}


def _name(tid: str) -> str:
    return f'[data-testid="thread-name-{tid}"]'


def _show(page, until, tid: str) -> bool:
    """Make the row on screen (expand p1 if it is folded)."""
    for _ in range(4):
        if page.locator(_name(tid)).count():
            return True
        page.locator('[data-testid="project-p1"]').click()
        until(lambda: page.locator(_name(tid)).count() > 0, timeout=1.0)
    return page.locator(_name(tid)).count() > 0


def _untitled_checks(page, mock, check, until):
    check("setup: the new chats are listed", _show(page, until, "t20") and _show(page, until, "t21"))
    new = page.locator(_name("t20"))
    old = page.locator(_name("t21"))
    check("an untitled chat with no turns reads New thread", new.inner_text() == "New thread")
    check("an untitled chat with turns reads Untitled", old.inner_text() == "Untitled")
    style = page.evaluate("""sel => { const s = getComputedStyle(document.querySelector(sel));
      return { italic: s.fontStyle, untitled: document.querySelector(sel).dataset.untitled }; }""", _name("t20"))
    check("…muted italics, marked untitled", style == {"italic": "italic", "untitled": "true"}, str(style))
    rows = page.evaluate("""() => ['t20', 't21'].map(id =>
      document.querySelector(`[data-testid="thread-${id}"]`).innerText)""")
    check("no untitled row shows its id", all(tid not in text for tid, text in zip(("t20", "t21"), rows)),
          str(rows))


def _wrap_checks(page, mock, check, until):
    check("setup: the long title is listed", _show(page, until, "t22"))
    name = _name("t22")
    got = page.evaluate("""sel => { const el = document.querySelector(sel); const s = getComputedStyle(el);
      const line = parseFloat(s.lineHeight);
      return { clamp: s.webkitLineClamp, ws: s.whiteSpace, overflow: s.textOverflow,
               height: el.clientHeight, scroll: el.scrollHeight, line, title: el.title,
               text: el.textContent }; }""", name)
    # Wrapped, not cut to one line: the ellipsis Chromium draws is at the end
    # of the third line, where the clamp falls.
    check("a long title wraps (white-space normal, more than one line)",
          got["ws"] == "normal" and got["height"] > 1.5 * got["line"], str(got))
    check("…clamped at three lines", got["clamp"] == "3" and got["height"] <= 3 * got["line"] + 2
          and got["height"] >= 2 * got["line"] and got["scroll"] > got["height"], str(got))
    check("…its whole title is the tooltip and the text", got["title"] == LONG and got["text"] == LONG)
    short = page.evaluate("sel => document.querySelector(sel).clientHeight", _name("t1"))
    check("a short title stays one line", short <= got["line"] + 2, f"{short} vs line {got['line']}")


def _live_checks(page, mock, check, until):
    _show(page, until, "t20")
    page.locator(f'[data-testid="thread-t20"]').click()
    ctx = _pane(1, ".panectx")
    until(lambda: page.locator(ctx).count() > 0 and page.locator(ctx).inner_text() == "New thread", timeout=4)
    check("an untitled chat's pane header reads New thread, not its id",
          page.locator(ctx).count() == 1 and page.locator(ctx).inner_text() == "New thread")
    t = next(t for t in mock.world["threads"] if t["id"] == "t20")
    t["title"], t["title_source"], t["turns"] = "Fixing the drivetrain build", "auto", 1
    mock.emit("thread_updated", {"thread_id": "t20", "title": t["title"], "title_source": "auto",
                                 "changed": ["title"]}, thread_id="t20", project_id="p1")
    until(lambda: page.locator(_name("t20")).inner_text() == "Fixing the drivetrain build", timeout=4)
    check("a title arriving by SSE relabels the row",
          page.locator(_name("t20")).inner_text() == "Fixing the drivetrain build"
          and page.locator(_name("t20")).get_attribute("data-untitled") is None)
    until(lambda: page.locator(ctx).inner_text() == "Fixing the drivetrain build", timeout=4)
    check("…and the chat pane's header", page.locator(ctx).inner_text() == "Fixing the drivetrain build")


def _copy_id_checks(page, mock, check, until):
    _show(page, until, "t21")
    page.locator('[data-testid="thread-menu-t21"]').click()
    until(lambda: page.locator('[data-testid="tmenu-copyid"]').count() > 0, timeout=2)
    item = page.locator('[data-testid="tmenu-copyid"]')
    check("the ⋯ menu has Copy id, showing the id",
          item.count() == 1 and page.locator('[data-testid="tmenu-id"]').inner_text() == "t21"
          and item.inner_text().startswith("Copy id"))
    page.evaluate("navigator.clipboard.writeText('before')")
    item.click()
    until(lambda: page.evaluate("navigator.clipboard.readText()") == "t21", timeout=3)
    check("Copy id puts the id on the clipboard and closes the menu",
          page.evaluate("navigator.clipboard.readText()") == "t21"
          and page.locator('[data-testid="thread-menu"]').count() == 0)


def _markup_checks(page, mock, check, until):
    t = next(t for t in mock.world["threads"] if t["id"] == "t21")
    t["title"] = MARKUP
    mock.emit("thread_updated", {"thread_id": "t21", "title": MARKUP, "changed": ["title"]},
              thread_id="t21", project_id="p1")
    until(lambda: page.locator(_name("t21")).inner_text() == MARKUP, timeout=4)
    time.sleep(0.2)
    got = page.evaluate("""sel => ({ text: document.querySelector(sel).textContent,
      kids: document.querySelector(sel).children.length, pwned: !!window.__pwned })""", _name("t21"))
    check("a title holding markup is drawn as characters", got == {"text": MARKUP, "kids": 0, "pwned": False},
          str(got))


def _archive_checks(page, mock, check, until):
    """The Archive names an archived chat by its title too, the id its
    tooltip (review of PR #62); an untitled one by its label, never the id."""
    w = mock.world
    titled, untitled = _thread("t24", "Old Robot Strategy", 5), _thread("t25", "", 2)
    for t in (titled, untitled):
        t["archived"] = "2026-10-12T00:00:00+00:00"
    w["threads"] += [titled, untitled]
    try:
        page.locator('[data-testid="open-archive"]').click()
        until(lambda: page.locator('[data-testid="archived-thread-name-t25"]').count() > 0, timeout=4)
        a = page.locator('[data-testid="archived-thread-name-t24"]')
        b = page.locator('[data-testid="archived-thread-name-t25"]')
        check("the Archive shows an archived chat's title, its id as the tooltip",
              a.inner_text() == "Old Robot Strategy" and a.get_attribute("title") == "id t24")
        check("…and an untitled one's label, never its id",
              b.inner_text() == "Untitled" and "t25" not in page.locator(
                  '[data-testid="archived-thread-t25"]').inner_text())
        page.locator('[data-testid="archive-close"]').click()
    finally:
        w["threads"][:] = [t for t in w["threads"] if t["id"] not in ("t24", "t25")]


def titles_checks(browser, mock, base, check, until, guard, init_script):
    print("\nchat titles: the sidebar row, its ⋯ menu and the pane header")
    ctx = browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]},
                              permissions=["microphone", "clipboard-read", "clipboard-write"])
    guard(ctx)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    w = mock.world
    keep = [dict(t) for t in w["threads"]]
    try:
        w["threads"] += [_thread("t20", "", 0), _thread("t21", "", 3), _thread("t22", LONG, 2)]
        page.goto(base + "/")
        page.wait_for_selector('[data-testid="sidebar"]', state="attached")
        _set_storage(page, zoom="100", layout="{}")
        _set_ws(page, SPLIT)
        _boot(page, mock, base, until, reload=True)
        for section in (_untitled_checks, _wrap_checks, _live_checks, _copy_id_checks, _markup_checks,
                        _archive_checks):
            try:
                section(page, mock, check, until)
            except Exception as e:  # a section that cannot run is a failure; the rest still run
                check(f"{section.__name__.strip('_')} ran to the end", False, str(e).splitlines()[0][:200])
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        check("no page errors in the chat titles section", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()
        w["threads"][:] = keep


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    from tests.face.hud_v2_mock import DIST, MockDaemon
    if not (DIST / "index.html").exists():
        print("hud/dist is not built. Run: cd hud && npm ci && npm run build")
        return 2
    mock = MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
            titles_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until,
                          v2.guard, v2.FAKE_RECOGNIZER)
            browser.close()
    finally:
        mock.stop()
    print()
    if v2.FAILURES:
        print(f"{len(v2.FAILURES)} of {v2.CHECKS} checks FAILED:")
        for f in v2.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {v2.CHECKS} chat titles checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
