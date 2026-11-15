"""WS-2 (docs/plans/2026-11-14-inbox-working-set-plan.md, D-5, D-9, D-10): the
sidebar's Inbox section under the owner Inbox row, against WS-1's routes
(`GET /working`, `POST /working/<id>/done` and `/undo`, the `working_changed`
bus record), played by `hud_v2_mock_working.py`.

Free and hermetic: its own `MockDaemon(0)` (an ephemeral port, never
8402/8403/8405/8408; `guard` refuses the live ports and watches the CSP).

What it pins, one section each (each names the mutation it is meant to catch):
  section   the section sits between the owner Inbox row and the projects;
            threads are in D-9 order (needs you, failed, done-unread, working,
            your move; newest first within a label), each with the same dot
            the tree shows, its title and its project's name as a chip; the
            aurora row is unchanged [order by time only; no chips]
  collapse  the chevron folds the list, the choice lasts across a reload, and
            expanding brings it back [no persistence]
  needs     the Needs you group shows the rows that have no thread entry (kind
            word plus title, text only; amber only for a pending rank) and goes
            when there are none; a click opens today's Inbox page [stays
            drawn when empty; hostile title as HTML]
  open      a click opens the thread in the focused pane, as a tree row does
  done      Done is disabled with the daemon's `why_not` while the entry is not
            eligible and posts nothing; an eligible one posts its drawn `seq`,
            shows an Undo toast, Undo posts the daemon's token and the row
            returns; the toast goes by itself after 5 s [skip the seq; no toast]
  conflict  a 409 (the entry moved on) re-reads silently: no toast, no error
            note, the row redrawn with its new seq [error toast on 409]
  live      a `working_changed` event re-reads the list, once [no refresh]
  stale     an older daemon (404) has no section and leaves the Inbox row alone
  inbox     the aurora row still opens today's Inbox page

`JARVIS_WORKING_SHOTS=<dir>` also saves the sidebar, expanded, collapsed, with a
Needs you group and with a Done toast, as PNGs there.

Run alone (after `cd hud && npm ci --ignore-scripts && npm run build`):
  systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 -- env PYTHONPATH=$PWD \\
    PYTHONDONTWRITEBYTECODE=1 /home/johnw/projects/Jarvis/.venv/bin/python tests/face/hud_v2_working_check.py [section,...]
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.face import hud_v2_mock_inbox as mock_inbox  # noqa: E402
from tests.face import hud_v2_mock_working as mock_working  # noqa: E402
from tests.face.hud_v2_layout_check import _pane  # noqa: E402
from tests.face.hud_v2_multichat_check import _chat, _fresh  # noqa: E402

SINGLE = json.dumps({"preset": "single", "focused": 1,
                     "panes": [{"view": "chat"}, {"view": "file"}, {"view": "browser"}, {"view": "task"}]})
KEY = "jarvis.hud.workingSection"
HOSTILE = '<img src=x onerror="window.__pwned=(window.__pwned||0)+1">'
AMBER = "rgb(251, 191, 36)"

# thread id -> (project, title, last activity, the sidebar's dot)
THREADS = {
    "t1": ("p1", "desk chat", "2026-11-14T09:00:00+00:00", "idle"),
    "t2": ("p2", "build the thing", "2026-11-14T10:00:00+00:00", "working"),
    "t3": ("p2", "which file", "2026-11-14T08:00:00+00:00", "needs_input"),
    "t4": ("p1", "finished one", "2026-11-14T11:00:00+00:00", "unread"),
    "t5": ("p2", "broke down", "2026-11-14T07:00:00+00:00", "failed"),
    "t6": ("p1", "your move", "2026-11-14T12:00:00+00:00", "idle"),
}
# D-9: needs you, failed, done-unread, working, then your move newest first.
ORDER = ["t3", "t5", "t4", "t2", "t6", "t1"]
WHY = {"t2": "a turn is running", "t3": "it waits on an answer"}


def _shot_dir() -> Path | None:
    d = os.environ.get("JARVIS_WORKING_SHOTS")
    if not d:
        return None
    p = Path(d)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _shot(page, name: str) -> None:
    d = _shot_dir()
    if d:
        page.locator("#sidebar").screenshot(path=str(d / f"{name}.png"))


def _st(mock):
    return mock_working.state(mock)


def _need_row(rid="pr:r1", kind="pr", rank=5, title="Review PR 12"):
    return {"id": rid, "kind": kind, "rank": rank, "title": title, "blocks": "waits on you", "project_id": "p1",
            "thread_id": None, "task_id": None, "asked_at": "2026-11-14T09:00:00+00:00", "expires_at": None,
            "origin": "", "detail": {}}


def _reset(mock, needs=None):
    """Six chats in two projects, five of them with a dot, all in the working set."""
    w = mock.world
    base = next(t for t in w["threads"] if t["id"] == "t1")
    w["threads"] = [t for t in w["threads"] if t["id"] == "t1"]
    for tid, (pid, title, last, _) in THREADS.items():
        if tid == "t1":
            base.update(title=title, updated=last)
            continue
        w["threads"].append({**base, "id": tid, "project_id": pid, "title": title, "turns": 1, "updated": last})
        w["transcripts"][tid] = []
    w["activity"] = {"threads": {t: v[3] for t, v in THREADS.items() if v[3] != "idle"}, "tasks": {}}
    s = _st(mock)
    s.update({"entries": [mock_working.entry(t, project_id=v[0], last=v[2], status=v[3], why_not=WHY.get(t))
                          for t, v in THREADS.items()],
              "needs_you": needs or [], "count": 0, "pending": False, "fail": None,
              "done_posts": [], "undo_posts": [], "done_reply": {}, "undone": {}})
    mock_inbox.state(mock).update({"rows": [], "fail": None, "answer_status": {}})
    mock.world["approvals"] = []


def _open(page, mock, until, needs=None, collapsed=False):
    _reset(mock, needs)
    page.evaluate("(k) => localStorage.removeItem(k)", KEY)
    if collapsed:
        page.evaluate("(k) => localStorage.setItem(k, '0')", KEY)
    _fresh(page, mock, until, SINGLE)
    until(lambda: page.locator('[data-testid="working-section"]').count() == 1, timeout=4)


def _rows(page) -> list[str]:
    return page.evaluate("() => [...document.querySelectorAll('[data-testid=\"working-row\"]')].map(r => r.dataset.id)")


def _reads(mock) -> int:
    return _st(mock)["reads"]


def _menu(page, tid):
    page.locator(f'[data-testid="working-row"][data-id="{tid}"]').hover()
    page.locator(f'[data-testid="working-menu-{tid}"]').click()
    page.wait_for_selector('[data-testid="thread-menu"]')


def _close_menu(page):
    page.keyboard.press("Escape")


# -- sections ------------------------------------------------------------------

def _section_checks(page, mock, check, until):
    _open(page, mock, until)
    until(lambda: len(_rows(page)) == len(ORDER), timeout=4)
    ids = _rows(page)
    check("the six chats are in D-9 order: needs you, failed, done-unread, working, your move, newest first "
          "[mutation: order by time only]", ids == ORDER, str(ids))
    place = page.evaluate("""() => {
      const r = document.querySelector('[data-testid="owner-inbox"]');
      const s = document.querySelector('[data-testid="working-section"]');
      const p = document.querySelector('[data-testid="project-p1"]');
      const pos = (a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
      return !!(r && s && p && pos(r, s) && pos(s, p) && r.nextElementSibling === s);
    }""")
    check("the section sits directly under the owner Inbox row and above the projects", place)
    chips = page.evaluate("() => Object.fromEntries([...document.querySelectorAll('[data-testid^=\"working-chip-\"]')]"
                          ".map(c => [c.dataset.testid.slice(13), c.textContent]))")
    check("each row carries its project's name as a chip [mutation: no chips]",
          chips == {"t1": "jarvis", "t2": "schoolwork", "t3": "schoolwork", "t4": "jarvis", "t5": "schoolwork",
                    "t6": "jarvis"}, str(chips))
    dots = page.evaluate("() => Object.fromEntries([...document.querySelectorAll('[data-testid^=\"working-activity-\"]')]"
                         ".map(c => [c.dataset.testid.slice(17), c.dataset.status]))")
    check("each dot is the one the tree shows (the sidebar's own activity store)",
          dots == {t: v[3] for t, v in THREADS.items()}, str(dots))
    tree = page.locator('[data-testid="activity-t4"]')
    check("and the tree row of a thread in the set carries the same dot",
          tree.count() == 1 and tree.get_attribute("data-status") == "unread")
    names = page.evaluate("() => [...document.querySelectorAll('[data-testid^=\"working-name-\"]')].map(e => e.textContent)")
    check("each row shows the thread's title",
          names == [THREADS[t][1] for t in ORDER], str(names))
    row = page.locator('[data-testid="owner-inbox"]')
    check("the aurora row is unchanged: a button, the aurora class, no count with nothing waiting",
          row.evaluate("e => e.tagName") == "BUTTON" and "aurora" in (row.get_attribute("class") or "")
          and page.locator('[data-testid="owner-inbox-count"]').count() == 0)
    _shot(page, "1-expanded")
    drag = page.evaluate("() => document.querySelector('[data-testid=\"working-row\"]').getAttribute('draggable')")
    check("a row can be dragged onto a project like a tree row (D-10)", drag == "true")
    # Drop onto a project row moves the thread, as the tree's own drag does.
    mock.world.setdefault("moves", [])
    page.locator('[data-testid="working-row"][data-id="t6"]').drag_to(page.locator('[data-testid="project-p2"]'))
    until(lambda: any(c[0] == "PATCH" and "t6" in c[1] for c in mock.calls), timeout=3)
    patched = [c for c in mock.calls if c[0] == "PATCH" and "t6" in c[1]]
    check("dropping one on another project asks the daemon to move it (the tree's own move)",
          bool(patched) and patched[-1][2].get("project_id") == "p2", str(patched[-1:]))


def _collapse_checks(page, mock, check, until):
    _open(page, mock, until)
    toggle = page.locator('[data-testid="working-toggle"]')
    check("the section starts expanded", toggle.get_attribute("aria-expanded") == "true" and len(_rows(page)) == 6)
    toggle.click()
    until(lambda: len(_rows(page)) == 0, timeout=2)
    check("the chevron folds the list, the header stays",
          page.locator('[data-testid="working-toggle"]').get_attribute("aria-expanded") == "false"
          and page.locator('[data-testid="working-section"]').count() == 1)
    stored = page.evaluate("(k) => localStorage.getItem(k)", KEY)
    check("the choice is stored", stored == "0", str(stored))
    _shot(page, "2-collapsed")
    before = mock.sse_connections()
    page.reload()
    page.wait_for_selector('[data-testid="sidebar"]', state="attached")
    until(lambda: page.locator('[data-testid="working-section"]').count() == 1, timeout=4)
    mock.await_reconnect(before)
    time.sleep(0.3)
    check("and it lasts across a reload [mutation: no persistence]",
          page.locator('[data-testid="working-toggle"]').get_attribute("aria-expanded") == "false"
          and len(_rows(page)) == 0)
    page.locator('[data-testid="working-toggle"]').click()
    until(lambda: len(_rows(page)) == 6, timeout=2)
    check("expanding brings the list back", _rows(page) == ORDER and page.evaluate("(k) => localStorage.getItem(k)", KEY) == "1")
    # Storage that throws must not break the sidebar.
    page.evaluate("(k) => localStorage.removeItem(k)", KEY)


def _needs_checks(page, mock, check, until):
    needs = [_need_row("appr:a1", "approval", 1, "Codex asks to use run_command"),
             _need_row("pr:r1", "pr", 5, "Review PR 12"),
             _need_row("dec:d1", "decision", 6, HOSTILE)]
    _open(page, mock, until, needs)
    until(lambda: page.locator('[data-testid="working-need"]').count() == 3, timeout=4)
    kinds = page.evaluate("() => [...document.querySelectorAll('[data-testid=\"working-need\"] .wkind')].map(e => e.textContent)")
    check("the Needs you group shows each row as a kind word and a title", kinds == ["Approval", "PR", "Decision"], str(kinds))
    first = page.locator('[data-testid="working-need"] .wkind').first
    second = page.locator('[data-testid="working-need"] .wkind').nth(1)
    check("amber marks a pending rank (1 or 2) only",
          first.evaluate("e => getComputedStyle(e).color") == AMBER and second.evaluate("e => getComputedStyle(e).color") != AMBER)
    check("a hostile title is text: no element, nothing ran [mutation: innerHTML]",
          page.locator('[data-testid="working-needs"] img').count() == 0 and page.evaluate("() => window.__pwned || 0") == 0
          and HOSTILE in page.locator('[data-testid="working-needs"]').inner_text())
    check("the group sits above the threads",
          page.evaluate("""() => { const n = document.querySelector('[data-testid="working-needs"]');
            const r = document.querySelector('[data-testid="working-row"]');
            return !!(n.compareDocumentPosition(r) & Node.DOCUMENT_POSITION_FOLLOWING); }"""))
    _shot(page, "3-needs-you")
    posts = len(mock_inbox.state(mock)["answers"])
    page.locator('[data-testid="working-need"][data-id="pr:r1"]').click()
    until(lambda: page.locator('[data-testid="inbox-view"]').count() == 1, timeout=3)
    check("a click opens today's Inbox page and answers nothing",
          page.locator(_pane(1)).get_attribute("data-view") == "inbox" and len(mock_inbox.state(mock)["answers"]) == posts)
    _st(mock)["needs_you"] = []
    mock_working.changed(mock)
    until(lambda: page.locator('[data-testid="working-needs"]').count() == 0, timeout=3)
    check("with no such rows the group is hidden [mutation: stays drawn when empty]",
          page.locator('[data-testid="working-needs"]').count() == 0 and len(_rows(page)) == 6)


def _open_checks(page, mock, check, until):
    _open(page, mock, until)
    until(lambda: len(_rows(page)) == 6, timeout=4)
    page.locator('[data-testid="working-row"][data-id="t4"]').click()
    until(lambda: _chat(page, 1)["threadId"] == "t4", timeout=3)
    check("a click opens the thread in the focused pane, as a tree row does", _chat(page, 1)["threadId"] == "t4")
    check("the row is marked as the open one",
          "sel" in (page.locator('[data-testid="working-row"][data-id="t4"]').get_attribute("class") or ""))
    check("and so is its tree row", "sel" in (page.locator('[data-testid="thread-t4"]').get_attribute("class") or ""))


def _done_checks(page, mock, check, until):
    _open(page, mock, until)
    until(lambda: len(_rows(page)) == 6, timeout=4)
    _menu(page, "t2")
    done = page.locator('[data-testid="tmenu-done"]')
    check("Done is disabled while the entry is not eligible, with the daemon's reason",
          done.is_disabled() and page.locator('[data-testid="tmenu-done-why"]').inner_text() == WHY["t2"])
    done.click(force=True)
    time.sleep(0.2)
    check("and pressing it posts nothing", _st(mock)["done_posts"] == [])
    _close_menu(page)
    _menu(page, "t1")
    check("an eligible entry's Done is enabled with no reason shown",
          page.locator('[data-testid="tmenu-done"]').is_enabled() and page.locator('[data-testid="tmenu-done-why"]').count() == 0)
    page.locator('[data-testid="tmenu-done"]').click()
    until(lambda: bool(_st(mock)["done_posts"]), timeout=3)
    check("Done posts the entry's drawn seq, once [mutation: skip the seq]",
          _st(mock)["done_posts"] == [("t1", {"seq": 1})], str(_st(mock)["done_posts"]))
    until(lambda: page.locator('[data-testid="working-toast"]').count() == 1, timeout=3)
    check("an Undo toast names the thread", "desk chat" in page.locator('[data-testid="working-toast"]').inner_text())
    until(lambda: "t1" not in _rows(page), timeout=3)
    check("the row leaves the Inbox (it is still in its project)",
          "t1" not in _rows(page) and page.locator('[data-testid="thread-t1"]').count() == 1)
    _shot(page, "4-done-toast")
    page.locator('[data-testid="working-undo"]').click()
    until(lambda: "t1" in _rows(page), timeout=3)
    check("Undo posts the daemon's token and the row returns",
          _st(mock)["undo_posts"] == [("t1", {"token": "tok-t1"})] and "t1" in _rows(page)
          and page.locator('[data-testid="working-toast"]').count() == 0, str(_st(mock)["undo_posts"]))
    # The toast goes by itself.
    _menu(page, "t1")
    page.locator('[data-testid="tmenu-done"]').click()
    until(lambda: page.locator('[data-testid="working-toast"]').count() == 1, timeout=3)
    t0 = time.time()
    gone = until(lambda: page.locator('[data-testid="working-toast"]').count() == 0, timeout=7)
    took = time.time() - t0
    check("the toast goes by itself after about 5 s, with no Undo posted",
          bool(gone) and 3.5 < took < 6.5 and len(_st(mock)["undo_posts"]) == 1, f"{took:.1f}s")
    _st(mock)["entries"].append(mock_working.entry("t1", project_id="p1", last=THREADS["t1"][2]))
    _st(mock)["undone"].clear()
    mock_working.changed(mock)
    until(lambda: "t1" in _rows(page), timeout=3)
    # An error that is not a 409 is a short message.
    _st(mock)["done_reply"]["t6"] = (503, {"error": "could not be saved"})
    _menu(page, "t6")
    page.locator('[data-testid="tmenu-done"]').click()
    until(lambda: page.locator('[data-testid="rename-note"]').count() == 1, timeout=3)
    note = page.locator('[data-testid="rename-note"]')
    check("any other failure is a short message, and the row stays",
          "could not be saved" in note.inner_text() and "t6" in _rows(page) and page.locator('[data-testid="working-toast"]').count() == 0)


def _conflict_checks(page, mock, check, until):
    _open(page, mock, until)
    until(lambda: len(_rows(page)) == 6, timeout=4)
    for e in _st(mock)["entries"]:       # the daemon moved on; no event reached this window
        if e["thread_id"] == "t6":
            e["seq"] = 5
            e["why_not"] = None
    reads = _reads(mock)
    _menu(page, "t6")
    page.locator('[data-testid="tmenu-done"]').click()
    until(lambda: _reads(mock) > reads, timeout=3)
    time.sleep(0.4)
    check("a 409 re-reads", _reads(mock) > reads, f"{reads} -> {_reads(mock)}")
    check("and says nothing: no toast, no error note [mutation: error toast on 409]",
          page.locator('[data-testid="working-toast"]').count() == 0 and page.locator('[data-testid="rename-note"]').count() == 0
          and "t6" in _rows(page))
    _menu(page, "t6")
    page.locator('[data-testid="tmenu-done"]').click()
    until(lambda: len(_st(mock)["done_posts"]) == 2, timeout=3)
    check("the next Done carries the new seq and goes through",
          _st(mock)["done_posts"][-1] == ("t6", {"seq": 5}) and until(lambda: page.locator('[data-testid="working-toast"]').count() == 1, timeout=3),
          str(_st(mock)["done_posts"]))


def _live_checks(page, mock, check, until):
    _open(page, mock, until)
    until(lambda: len(_rows(page)) == 6, timeout=4)
    time.sleep(0.3)
    reads = _reads(mock)
    _st(mock)["entries"] = [e for e in _st(mock)["entries"] if e["thread_id"] != "t5"]
    mock_working.changed(mock)
    until(lambda: "t5" not in _rows(page), timeout=3)
    time.sleep(0.3)
    check("a working_changed event re-reads the list, once [mutation: no refresh]",
          "t5" not in _rows(page) and _reads(mock) == reads + 1, f"{reads} -> {_reads(mock)}")
    # The reconnect alone re-reads it too.
    _st(mock)["entries"].append(mock_working.entry("t5", project_id="p2", last=THREADS["t5"][2], status="failed"))
    before = mock.sse_connections()
    page.reload()
    page.wait_for_selector('[data-testid="sidebar"]', state="attached")
    mock.await_reconnect(before)
    until(lambda: "t5" in _rows(page), timeout=4)
    check("a reload reads the list on its own", "t5" in _rows(page))


def _empty_checks(page, mock, check, until):
    _open(page, mock, until)
    _st(mock)["entries"] = []
    mock_working.changed(mock)
    until(lambda: page.locator('[data-testid="working-empty"]').count() == 1, timeout=3)
    check("with nothing in the set the section says so in one muted line",
          page.locator('[data-testid="working-empty"]').inner_text() == "Nothing in progress"
          and page.locator('[data-testid="working-row"]').count() == 0)


def _stale_checks(page, mock, check, until):
    _reset(mock)
    _st(mock)["fail"] = (404, "route not found")
    _fresh(page, mock, until, SINGLE)
    time.sleep(0.6)
    check("an older daemon (404) has no section, and the Inbox row is still there",
          page.locator('[data-testid="working-section"]').count() == 0 and page.locator('[data-testid="owner-inbox"]').count() == 1)
    _st(mock)["fail"] = None


def _inbox_checks(page, mock, check, until):
    _open(page, mock, until)
    page.locator('[data-testid="owner-inbox"]').click()
    until(lambda: page.locator('[data-testid="inbox-view"]').count() == 1, timeout=3)
    check("the aurora row still opens today's Inbox page",
          page.locator(_pane(1)).get_attribute("data-view") == "inbox"
          and "sel" in (page.locator('[data-testid="owner-inbox"]').get_attribute("class") or ""))
    check("the section stays under it while the page is open", page.locator('[data-testid="working-section"]').count() == 1)


# ---------------------------------------------------------------------------

SECTIONS = (_section_checks, _collapse_checks, _needs_checks, _open_checks, _done_checks, _conflict_checks,
            _live_checks, _empty_checks, _stale_checks, _inbox_checks)


def working_checks(browser, mock, base, check, until, guard, init_script):
    print("\nthe Inbox section (WS-2)")
    only = set(filter(None, (sys.argv[1] if len(sys.argv) > 1 and __name__ == "__main__" else "").split(",")))
    ctx = browser.new_context(viewport={"width": 1600, "height": 900}, permissions=["microphone"])
    guard(ctx)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    try:
        before = mock.sse_connections()
        _reset(mock)
        page.goto(base + "/")
        page.wait_for_selector('[data-testid="sidebar"]', state="attached")
        mock.await_reconnect(before)
        for section in SECTIONS:
            if only and section.__name__.strip("_").removesuffix("_checks") not in only:
                continue
            try:
                section(page, mock, check, until)
            except Exception as e:  # a section that cannot run is a failure, and the rest still run
                check(f"working {section.__name__.strip('_')} ran to the end", False, str(e).splitlines()[0][:200])
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        check("no page errors in the working section", not errors, "; ".join(errors[:3]))
        check("the owner test refused nothing the HUD's own page asked", _st(mock)["refused"] == 0, str(_st(mock)["refused"]))
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
            working_checks(browser, mock, f"http://127.0.0.1:{mock.port}", H.check, H.until, H.guard,
                           H.FAKE_RECOGNIZER)
            browser.close()
    finally:
        mock.stop()
    print()
    if H.FAILURES:
        print(f"{len(H.FAILURES)} of {H.CHECKS} checks FAILED:")
        for f in H.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {H.CHECKS} working checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
