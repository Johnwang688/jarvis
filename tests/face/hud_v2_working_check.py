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
  event     an `inbox_changed` alone (no `working_changed`) re-reads the list: a
            removed PR leaves Needs you; Done's reason is read again as its menu
            opens [no refresh on inbox_changed; no read on open]
  keys      a thread row is a real button: Tab reaches it, Enter and Space open
            the thread; the menu focuses its first enabled item and Escape gives
            focus back to its opener; a disabled Done is described by its reason
            [row without a tab stop; focus left behind]
  hint      a thread with an attached decision says "1 decision" and the hint
            opens the Inbox page [no hint]
  tall      31 threads and a short window: the section stops at 40% of the
            sidebar and scrolls inside, the project tree keeps rows, nothing
            reaches the orb; collapsed or empty costs its header line
            [no max-height / list scroll]

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
    _menu(page, "t6")                    # opening the menu reads the list
    time.sleep(0.4)
    for e in _st(mock)["entries"]:       # then the daemon moves on; no event reaches this window
        if e["thread_id"] == "t6":
            e["seq"] = 5
            e["why_not"] = None
    reads = _reads(mock)
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
          and page.locator('[data-testid="working-row"]').count() == 0
          and page.locator('[data-testid="working-list"]').count() == 0
          and page.locator('[data-testid="working-section"]').evaluate("e => e.getBoundingClientRect().height") <= 36)


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

def _event_checks(page, mock, check, until):
    _open(page, mock, until, [_need_row("pr:r1", "pr", 5, "Review PR 12")])
    until(lambda: page.locator('[data-testid="working-need"]').count() == 1, timeout=4)
    _st(mock)["needs_you"] = []
    reads = _reads(mock)
    mock_inbox.changed(mock, ["pr:r1"], "pr_gone")          # the daemon says nothing of the working set
    until(lambda: page.locator('[data-testid="working-need"]').count() == 0, timeout=3)
    check("an inbox_changed alone re-reads the list: a removed PR leaves Needs you [mutation: no refresh on inbox_changed]",
          page.locator('[data-testid="working-need"]').count() == 0 and _reads(mock) > reads,
          f"{reads} -> {_reads(mock)}")
    reads = _reads(mock)
    for _ in range(5):
        mock_inbox.changed(mock, [], "burst")
    time.sleep(0.8)
    check("a burst of five events is a few reads, not five more than needed",
          _reads(mock) - reads <= 6, f"{reads} -> {_reads(mock)}")
    for e in _st(mock)["entries"]:       # t2's turn ended: no event reached the window
        if e["thread_id"] == "t2":
            e["why_not"] = None
    _menu(page, "t2")
    until(lambda: page.locator('[data-testid="tmenu-done"]').is_enabled(), timeout=3)
    check("opening an Inbox row's menu reads the list again, so Done is enabled once the turn ended [mutation: no read on open]",
          page.locator('[data-testid="tmenu-done"]').is_enabled() and page.locator('[data-testid="tmenu-done-why"]').count() == 0)
    _close_menu(page)


def _active_id(page):
    return page.evaluate("() => document.activeElement && document.activeElement.getAttribute('data-testid')")


def _keys_checks(page, mock, check, until):
    _open(page, mock, until)
    until(lambda: len(_rows(page)) == 6, timeout=4)
    btn = page.locator('[data-testid="working-open-t4"]')
    check("a thread row holds a real button (a tab stop) named by its title, state and project",
          btn.evaluate("e => e.tagName") == "BUTTON" and "finished one" in (btn.get_attribute("aria-label") or "")
          and "jarvis" in (btn.get_attribute("aria-label") or ""), str(btn.get_attribute("aria-label")))
    btn.focus()
    page.keyboard.press("Enter")
    until(lambda: _chat(page, 1)["threadId"] == "t4", timeout=3)
    check("Enter on it opens the thread [mutation: row without a tab stop]", _chat(page, 1)["threadId"] == "t4")
    page.locator('[data-testid="working-open-t1"]').focus()
    page.keyboard.press("Space")
    until(lambda: _chat(page, 1)["threadId"] == "t1", timeout=3)
    check("Space on it opens the thread too", _chat(page, 1)["threadId"] == "t1")
    opener = page.locator('[data-testid="working-menu-t2"]')
    opener.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector('[data-testid="thread-menu"]')
    until(lambda: _active_id(page) == "tmenu-beside", timeout=2)
    check("the menu opened from the keyboard focuses its first enabled item (Done is disabled here)",
          _active_id(page) == "tmenu-beside", str(_active_id(page)))
    done = page.locator('[data-testid="tmenu-done"]')
    why = page.locator('[data-testid="tmenu-done-why"]')
    check("the disabled Done is described by its reason",
          done.is_disabled() and done.get_attribute("aria-describedby") == why.get_attribute("id")
          and why.inner_text() == WHY["t2"])
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="thread-menu"]').count() == 0, timeout=2)
    until(lambda: _active_id(page) == "working-menu-t2", timeout=2)
    check("Escape closes it and gives focus back to its opener [mutation: focus left behind]",
          _active_id(page) == "working-menu-t2", str(_active_id(page)))
    page.locator('[data-testid="working-menu-t6"]').focus()
    page.keyboard.press("Enter")
    page.wait_for_selector('[data-testid="thread-menu"]')
    until(lambda: _active_id(page) == "tmenu-done", timeout=2)
    check("an eligible Done is the first item focused", _active_id(page) == "tmenu-done", str(_active_id(page)))
    page.keyboard.press("Escape")


def _hint_checks(page, mock, check, until):
    _reset(mock)
    dec = {**_need_row("dec:d9", "decision", 6, "Which way?"), "thread_id": "t6"}
    for e in _st(mock)["entries"]:
        if e["thread_id"] == "t6":
            e["rows"] = [dec]
    _fresh(page, mock, until, SINGLE)
    until(lambda: page.locator('[data-testid="working-hint-t6"]').count() == 1, timeout=4)
    hint = page.locator('[data-testid="working-hint-t6"]')
    check("a thread with an attached decision says so on its row [mutation: no hint]",
          hint.inner_text() == "1 decision" and page.locator('[data-testid="working-hint-t1"]').count() == 0,
          hint.inner_text())
    hint.click()
    until(lambda: page.locator('[data-testid="inbox-view"]').count() == 1, timeout=3)
    check("the hint opens today's Inbox page, not the thread",
          page.locator(_pane(1)).get_attribute("data-view") == "inbox")


def _tall_checks(page, mock, check, until):
    _reset(mock, [_need_row(f"pr:r{i}", "pr", 5, f"Review PR {i}") for i in range(4)])
    w = mock.world
    base = next(t for t in w["threads"] if t["id"] == "t1")
    for n in range(7, 32):
        w["threads"].append({**base, "id": f"t{n}", "project_id": "p2", "title": f"extra chat {n}", "turns": 1,
                             "updated": f"2026-11-14T{(n % 12) + 8:02d}:00:00+00:00"})
        w["transcripts"][f"t{n}"] = []
        _st(mock)["entries"].append(mock_working.entry(f"t{n}", project_id="p2", last=f"2026-11-14T{(n % 12) + 8:02d}:00:00+00:00"))
    page.evaluate("(k) => localStorage.removeItem(k)", KEY)
    _fresh(page, mock, until, SINGLE, size=(1280, 720))
    until(lambda: len(_rows(page)) == 31, timeout=5)
    m = page.evaluate("""() => {
      const r = (q) => { const e = document.querySelector(q); return e && e.getBoundingClientRect(); };
      const side = r('#sidebar'), sec = r('[data-testid="working-section"]'), list = r('[data-testid="sidebar"]');
      const orb = r('#orbdock'), wl = document.querySelector('[data-testid="working-list"]');
      let rows = 0;
      for (const e of document.querySelectorAll('[data-testid="sidebar"] .tree-row')) {
        const b = e.getBoundingClientRect();
        if (b.height < 4 || b.top < list.top - 0.5 || b.bottom > list.bottom + 0.5) continue;
        const hit = document.elementFromPoint(b.left + Math.min(40, b.width / 2), b.top + b.height / 2);
        if (hit && e.contains(hit)) rows++;
      }
      return {side: side.height, sec: sec.height, list: list.height, rows, secBottom: sec.bottom, listBottom: list.bottom,
              orbTop: orb ? orb.top : null, scrolls: wl.scrollHeight > wl.clientHeight + 1};
    }""")
    check("31 threads: the section stops at 40% of the sidebar [mutation: no max-height]",
          m["sec"] <= m["side"] * 0.4 + 1, str(m))
    check("and its list scrolls inside that", m["scrolls"], str(m))
    check("the project tree keeps its room: at least 88px and a few rows you can click",
          m["list"] >= 88 and m["rows"] >= 3, str(m))
    check("nothing reaches the orb: the section and the project list end above it",
          m["orbTop"] is not None and m["secBottom"] <= m["orbTop"] + 1 and m["listBottom"] <= m["orbTop"] + 1, str(m))
    _shot(page, "5-31-threads")
    page.locator('[data-testid="working-toggle"]').click()
    until(lambda: page.locator('[data-testid="working-list"]').count() == 0, timeout=2)
    h = page.locator('[data-testid="working-section"]').evaluate("e => e.getBoundingClientRect().height")
    check("collapsed it costs its header line", h <= 36, str(h))
    page.evaluate("(k) => localStorage.removeItem(k)", KEY)


SECTIONS = (_section_checks, _collapse_checks, _needs_checks, _open_checks, _done_checks, _conflict_checks,
            _live_checks, _empty_checks, _stale_checks, _inbox_checks, _event_checks, _keys_checks,
            _hint_checks, _tall_checks)


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
