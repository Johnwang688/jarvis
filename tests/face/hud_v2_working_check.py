"""WS-2 and WS-3 (docs/plans/2026-11-14-inbox-working-set-plan.md, D-5, D-9, D-10,
§11): the sidebar's Inbox section under the owner Inbox row (which is its header), against WS-1's routes
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
  collapse  (WS-3) a click on the rainbow Inbox row folds the list and does not open
            the page, `aria-expanded` follows, Enter and Space work, the choice
            lasts across a reload, and there is no separate header line
            [the row opens the page; no persistence; header kept]
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
  inbox     the Needs you line opens the page, titled Needs you, and the list stays
  line      (WS-3) the Needs you line shows at 0 as a quiet "Needs you · 0" and
            still opens the page; with rows it carries the badge's count
            [hidden at 0]
  event     an `inbox_changed` alone (no `working_changed`) re-reads the list: a
            removed PR leaves Needs you; Done's reason is read again as its menu
            opens [no refresh on inbox_changed; no read on open]
  keys      a thread row is a real button: Tab reaches it, Enter and Space open
            the thread; the menu focuses its first enabled item and Escape gives
            focus back to its opener; a disabled Done is described by its reason
            [row without a tab stop; focus left behind]
  hint      a thread with an attached decision says "1 decision" and the hint
            opens the Needs you page [no hint]
  page      (WS-3) a thread's hint ("2 items") opens the Needs you page at that
            thread's group (focused, scrolled to); the page groups the chat's
            rows under its name, still lists every row, and the group's link
            opens the chat; nothing is listed twice in the sidebar [no grouping;
            hint opens the page only]
  tall      31 threads and a short window: the section stops at 40% of the
            sidebar and scrolls inside, the project tree keeps rows, nothing
            reaches the orb; collapsed it costs nothing, empty its lines
            [no max-height / list scroll]
  short     six threads in a short, zoomed window (1920x700 and 560 high, both
            at 160%): at least one whole working row is visible, at least one
            tree row too, and Schedules is not under the orb [list shrinks to
            nothing; fixed tree minimum]
  settings  (WS-4) Settings > Inbox shows the daemon's idle limit and bounds; a
            client-side bad value (0, 31, 2.5) posts nothing; 3 -> 5 posts
            exactly {idle_days: 5} and says saved; a server 400 shows its
            sentence inline; Enter in the box saves; a 404 and a 403 show a
            quiet "Not available" with no field; a working_changed read does not
            overwrite a typed draft [hard-coded bounds; 400 swallowed; unavailable
            shown as 3; refresh clobbers typing]
  menukeys  a read that disables the focused Done keeps focus inside the menu;
            ArrowDown and ArrowUp move between the enabled items and wrap
            [focus to body; no arrow keys]

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
from tests.face.hud_v2_check import open_settings  # noqa: E402
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


def _shot_page(page, name: str) -> None:
    d = _shot_dir()
    if d:
        page.screenshot(path=str(d / f"{name}.png"))


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


def _fold(page):
    return page.locator('[data-testid="owner-inbox"]')


def _collapse_checks(page, mock, check, until):
    _open(page, mock, until)
    row = _fold(page)
    check("the section starts expanded; the rainbow Inbox row says so and names the list it folds",
          row.get_attribute("aria-expanded") == "true" and len(_rows(page)) == 6
          and row.get_attribute("aria-controls") == page.locator('[data-testid="working-section"]').get_attribute("id"))
    check("there is no separate In progress header line any more [mutation: header kept]",
          page.locator('[data-testid="working-toggle"]').count() == 0 and page.locator(".workinghead").count() == 0)
    row.click()
    until(lambda: len(_rows(page)) == 0, timeout=2)
    check("a click on the rainbow row folds the list and does NOT open the page [mutation: the row opens the page]",
          _fold(page).get_attribute("aria-expanded") == "false"
          and page.locator('[data-testid="working-needs-line"]').count() == 0
          and page.locator(_pane(1)).get_attribute("data-view") == "chat"
          and page.locator('[data-testid="inbox-view"]').count() == 0)
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
          _fold(page).get_attribute("aria-expanded") == "false" and len(_rows(page)) == 0)
    _fold(page).click()
    until(lambda: len(_rows(page)) == 6, timeout=2)
    check("unfolding brings the list back", _rows(page) == ORDER
          and _fold(page).get_attribute("aria-expanded") == "true"
          and page.evaluate("(k) => localStorage.getItem(k)", KEY) == "1")
    # Keyboard: the row is a button, Enter and Space fold, and its focus ring stays.
    _fold(page).focus()
    page.keyboard.press("Enter")
    until(lambda: len(_rows(page)) == 0, timeout=2)
    page.keyboard.press("Space")
    until(lambda: len(_rows(page)) == 6, timeout=2)
    check("Enter and Space on the focused row fold and unfold it, and it keeps focus",
          page.evaluate("() => document.activeElement && document.activeElement.dataset.testid") == "owner-inbox")
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
    check("with nothing in the set the section says so in one muted line, under the Needs you line",
          page.locator('[data-testid="working-empty"]').inner_text() == "Nothing in progress"
          and page.locator('[data-testid="working-row"]').count() == 0
          and page.locator('[data-testid="working-list"]').count() == 0
          and page.locator('[data-testid="working-needs-line"]').count() == 1
          and page.locator('[data-testid="working-section"]').evaluate("e => e.getBoundingClientRect().height") <= 72)


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
    page.locator('[data-testid="working-needs-line"]').click()
    until(lambda: page.locator('[data-testid="inbox-view"]').count() == 1, timeout=3)
    check("the Needs you line opens the page, titled Needs you, and the list stays under the row",
          page.locator(_pane(1)).get_attribute("data-view") == "inbox"
          and page.locator('[data-testid="inbox-page-title"]').text_content() == "Needs you"
          and "sel" in (page.locator('[data-testid="owner-inbox"]').get_attribute("class") or "")
          and page.locator('[data-testid="working-section"]').count() == 1
          and len(_rows(page)) == 6)
    check("the pane's own label reads Needs you, not Inbox",
          "needs you" in (page.locator(_pane(1)).get_attribute("aria-label") or "").lower()
          and "inbox" not in (page.locator(_pane(1)).get_attribute("aria-label") or "").lower())


def _line_checks(page, mock, check, until):
    # With nothing waiting the line is quiet and still opens the page.
    _open(page, mock, until)
    line = page.locator('[data-testid="working-needs-line"]')
    until(lambda: line.count() == 1 and line.inner_text() == "Needs you · 0", timeout=4)
    check("the Needs you line is shown with nothing waiting, as a quiet 'Needs you · 0' [mutation: hidden at 0]",
          line.count() == 1 and line.inner_text() == "Needs you · 0"
          and "quiet0" in (line.get_attribute("class") or "")
          and page.locator('[data-testid="owner-inbox-count"]').count() == 0)
    _shot(page, "7-needs-line-0")
    line.click()
    until(lambda: page.locator('[data-testid="inbox-view"]').count() == 1, timeout=3)
    check("and at 0 it still opens the Needs you page",
          page.locator('[data-testid="inbox-page-title"]').text_content() == "Needs you")
    # With rows waiting it carries the count the rainbow badge shows.
    rows = [{"id": f"pr:r{i}", "kind": "pr", "rank": 5, "title": f"Review PR {i}", "blocks": "", "project_id": "p1",
             "thread_id": None, "task_id": None, "asked_at": "2026-11-14T09:00:00+00:00", "expires_at": None,
             "origin": "", "detail": {}} for i in range(3)]
    _reset(mock)
    mock_inbox.state(mock)["rows"] = rows
    page.evaluate("(k) => localStorage.removeItem(k)", KEY)
    _fresh(page, mock, until, SINGLE)
    until(lambda: page.locator('[data-testid="working-needs-line"]').count() == 1, timeout=4)
    until(lambda: page.locator('[data-testid="owner-inbox-count"]').count() == 1, timeout=4)
    badge = page.locator('[data-testid="owner-inbox-count"]').inner_text()
    txt = page.locator('[data-testid="working-needs-line"]').inner_text()
    check("with rows waiting the line carries the same count as the rainbow badge", txt == f"Needs you · {badge}" and badge == "3",
          f"{txt} / {badge}")
    _shot(page, "8-needs-line-n")
    mock_inbox.state(mock)["rows"] = []


def _page_checks(page, mock, check, until):
    _reset(mock)
    t6q = {"id": "q:t6:r1", "kind": "question", "rank": 2, "title": "chat t6 is stopped until you answer",
           "blocks": "chat t6 waits on this", "project_id": "p1", "thread_id": "t6", "task_id": None,
           "asked_at": "2026-11-14T09:00:00+00:00", "expires_at": None, "origin": "",
           "detail": {"thread_id": "t6", "req_id": "r1", "text": "Pick one", "options": ["left", "right"]}}
    t6d = {**t6q, "id": "dec:t6", "kind": "decision", "rank": 3, "title": "which database",
           "detail": {"decision_id": "d" + "0" * 12, "question": "which?", "options": [{"label": "a"}], "suggested": 0}}
    pr = {"id": "pr:r1", "kind": "pr", "rank": 5, "title": "Review PR 12", "blocks": "", "project_id": "p1",
          "thread_id": None, "task_id": None, "asked_at": "2026-11-14T09:00:00+00:00", "expires_at": None,
          "origin": "", "detail": {}}
    mock_inbox.state(mock)["rows"] = [t6q, t6d, pr]
    for e in _st(mock)["entries"]:
        if e["thread_id"] == "t6":
            e["rows"] = [t6q, t6d]
    _fresh(page, mock, until, SINGLE)
    until(lambda: page.locator('[data-testid="working-hint-t6"]').count() == 1, timeout=4)
    hint = page.locator('[data-testid="working-hint-t6"]')
    check("a thread with a question and a decision on it says '2 items' on its row, and nowhere else in the sidebar",
          hint.inner_text() == "2 items" and page.locator('[data-testid="working-need"]').count() == 0,
          hint.inner_text())
    _shot(page, "10-hint")
    hint.click()
    until(lambda: page.locator('[data-testid="inbox-view"]').count() == 1, timeout=3)
    until(lambda: page.evaluate("() => document.activeElement && document.activeElement.dataset.threadId") == "t6", timeout=3)
    check("a hint click opens the Needs you page at that thread: its group is focused and scrolled to [mutation: hint opens the page only]",
          page.locator('[data-testid="inbox-page-title"]').text_content() == "Needs you"
          and page.evaluate("() => document.activeElement.dataset.testid") == "inbox-thread")
    groups = page.locator('[data-testid="inbox-thread"][data-thread-id="t6"]')
    check("the page groups the chat's rows under its name, one group per rank group",
          groups.count() == 2 and groups.first.locator('[data-testid="inbox-thread-name"]').inner_text() == THREADS["t6"][1]
          and groups.first.locator('[data-testid="inbox-row"]').count() == 1
          and page.locator('[data-testid="inbox-row"][data-id="pr:r1"]').evaluate(
              "e => !e.closest('[data-testid=\"inbox-thread\"]')"))
    check("and lists every row: the chat's two and the PR",
          page.locator('[data-testid="inbox-row"]').count() == 3)
    _shot_page(page, "9-page-grouped")
    page.locator('[data-testid="inbox-thread-open"]').first.click()
    until(lambda: page.locator(_pane(1)).get_attribute("data-view") == "chat", timeout=3)
    check("the group's link opens the thread (the pane shows its chat)",
          page.locator(_pane(1)).get_attribute("data-view") == "chat"
          and page.locator(f'[data-testid="working-row"][data-id="t6"].sel').count() == 1)
    mock_inbox.state(mock)["rows"] = []


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
    check("the hint opens the Needs you page, not the thread",
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
    _fold(page).click()
    until(lambda: page.locator('[data-testid="working-list"]').count() == 0, timeout=2)
    h = page.locator('[data-testid="working-section"]').evaluate("e => e.getBoundingClientRect().height")
    check("folded the list costs no room: the rainbow row is its header", h <= 2, str(h))
    page.evaluate("(k) => localStorage.removeItem(k)", KEY)


def _zfresh(page, mock, until, size, zoom):
    """Reload into the single-pane workspace at a window size and a HUD zoom (percent)."""
    page.set_viewport_size({"width": size[0], "height": size[1]})
    page.evaluate("""([ws, zoom]) => {
      localStorage.setItem('jarvis.hud.zoom', zoom);
      localStorage.setItem('jarvis.hud.layout', '{}');
      localStorage.setItem('jarvis.hud.workspace', ws);
      localStorage.setItem('jarvis.dictation', 'review');
    }""", [SINGLE, str(zoom)])
    before = mock.sse_connections()
    page.reload()
    page.wait_for_selector('[data-testid="sidebar"]', state="attached")
    until(lambda: page.locator('[data-testid="project-p1"]').count() > 0)
    mock.await_reconnect(before)
    time.sleep(0.4)


def _q6(thread="t6"):
    return {"id": f"q:{thread}:r1", "kind": "question", "rank": 2, "title": f"chat {thread} is stopped until you answer",
            "blocks": f"chat {thread} waits on this", "project_id": "p1", "thread_id": thread, "task_id": None,
            "asked_at": "2026-11-14T09:00:00+00:00", "expires_at": None, "origin": "",
            "detail": {"thread_id": thread, "req_id": "r1", "text": "Pick one", "options": ["left", "right"]}}


def _focused(page):
    return page.evaluate("() => document.activeElement && document.activeElement.dataset && document.activeElement.dataset.threadId || null")


def _nav_checks(page, mock, check, until):
    """Review of PR #368 (fix round 1): hint targets and the header's fallbacks."""
    q = _q6()
    # 1. A hint newer than the cached inbox rows: the page reads first, then focuses.
    _reset(mock)
    mock_inbox.state(mock)["rows"] = []
    _fresh(page, mock, until, SINGLE)
    until(lambda: page.locator('[data-testid="working-needs-line"]').inner_text() == "Needs you · 0", timeout=4)
    page.locator('[data-testid="working-needs-line"]').click()
    until(lambda: page.locator('[data-testid="inbox-view"]').get_attribute("data-status") == "ready", timeout=4)
    page.locator(_pane(1, '[data-testid="tab-chat"]')).click()
    until(lambda: page.locator('[data-testid="inbox-view"]').count() == 0, timeout=3)
    mock_inbox.state(mock)["rows"] = [q]                       # the inbox moved on; this window has not heard (no inbox_changed)
    for e in _st(mock)["entries"]:
        if e["thread_id"] == "t6":
            e["rows"] = [q]
    mock_working.changed(mock)
    until(lambda: page.locator('[data-testid="working-hint-t6"]').count() == 1, timeout=4)
    page.locator('[data-testid="working-hint-t6"]').click()
    until(lambda: _focused(page) == "t6", timeout=4)
    check("a hint newer than the cached inbox rows still lands: the page reads first, then focuses the thread "
          "[mutation: the target is judged before the read]", _focused(page) == "t6", str(_focused(page)))
    # 1b. A second hint for the same thread while the page stays mounted is a new request with its own read.
    mock_inbox.state(mock)["rows"] = []
    mock_inbox.changed(mock)                                  # the page re-reads: the group (and the focus) is gone
    until(lambda: page.locator('[data-testid="inbox-thread"]').count() == 0, timeout=4)
    mock_inbox.state(mock)["rows"] = [q]                      # back again, but this window has not heard
    page.locator('[data-testid="working-hint-t6"]').click()
    until(lambda: _focused(page) == "t6", timeout=4)
    check("a second hint for the same thread, page still mounted, reads for itself and lands "
          "[mutation: the earlier read's completion is reused]", _focused(page) == "t6", str(_focused(page)))
    # 2. A cancelled navigation leaves no target behind.
    _reset(mock)
    mock_inbox.state(mock)["rows"] = [q]
    mock_inbox.state(mock)["fail"] = (500, "boom")
    for e in _st(mock)["entries"]:
        if e["thread_id"] == "t6":
            e["rows"] = [q]
    _fresh(page, mock, until, SINGLE)
    until(lambda: page.locator('[data-testid="working-hint-t6"]').count() == 1, timeout=4)
    page.locator('[data-testid="working-hint-t6"]').click()
    until(lambda: page.locator('[data-testid="inbox-error"]').count() == 1, timeout=4)
    page.locator(_pane(1, '[data-testid="tab-chat"]')).click()
    until(lambda: page.locator('[data-testid="inbox-view"]').count() == 0, timeout=3)
    mock_inbox.state(mock)["fail"] = None
    mock_inbox.changed(mock)
    until(lambda: page.locator('[data-testid="owner-inbox-count"]').count() == 1, timeout=4)
    page.locator('[data-testid="working-needs-line"]').click()
    until(lambda: page.locator('[data-testid="inbox-view"]').get_attribute("data-status") == "ready", timeout=4)
    until(lambda: page.locator('[data-testid="inbox-thread"]').count() == 1, timeout=4)
    time.sleep(0.4)
    check("a hint opened while the inbox could not be read, then abandoned, leaves no target: a later ordinary open "
          "focuses nothing [mutation: the request survives the page]", _focused(page) is None, str(_focused(page)))
    # 3. A first /working error, whatever it is, keeps the row an opener.
    _reset(mock)
    _st(mock)["fail"] = (500, "boom")
    _fresh(page, mock, until, SINGLE)
    time.sleep(0.8)
    row = page.locator('[data-testid="owner-inbox"]')
    check("a first /working error (500) leaves the rainbow row a plain button with no list to fold [mutation: folds an empty list]",
          row.count() == 1 and row.get_attribute("aria-expanded") is None
          and page.locator('[data-testid="working-section"]').count() == 0
          and page.locator('[data-testid="working-stale"]').count() == 1)
    row.click()
    until(lambda: page.locator('[data-testid="inbox-view"]').count() == 1, timeout=3)
    check("and a click on it opens the Needs you page", page.locator('[data-testid="inbox-page-title"]').text_content() == "Needs you")
    _st(mock)["fail"] = None
    # 5. An unread inbox is a mark, not a zero.
    _reset(mock)
    mock_inbox.state(mock)["fail"] = (500, "boom")
    _fresh(page, mock, until, SINGLE)
    until(lambda: page.locator('[data-testid="working-needs-line"]').count() == 1, timeout=4)
    time.sleep(0.5)
    line = page.locator('[data-testid="working-needs-line"]')
    check("with the inbox unread the line says so ('Needs you · –'), is still clickable and is not the quiet zero "
          "[mutation: unknown shown as 0]",
          line.inner_text() == "Needs you · –" and "quiet0" not in (line.get_attribute("class") or "")
          and "nothing" not in (line.get_attribute("aria-label") or ""), line.inner_text())
    mock_inbox.state(mock)["fail"] = None
    # 9. With a hint on a row, the project chip gives way first and the title keeps its letters.
    _reset(mock)
    mock_inbox.state(mock)["rows"] = [q, {**q, "id": "dec:t6", "kind": "decision", "rank": 3,
                                          "detail": {"decision_id": "d" + "0" * 12, "question": "which?",
                                                     "options": [{"label": "a"}], "suggested": 0}}]
    for e in _st(mock)["entries"]:
        if e["thread_id"] == "t6":
            e["rows"] = mock_inbox.state(mock)["rows"]
    for t in mock.world["threads"]:
        if t["id"] == "t6":
            t["title"] = "your move on the long thread with a long title"
    _fresh(page, mock, until, SINGLE)
    until(lambda: page.locator('[data-testid="working-hint-t6"]').count() == 1, timeout=4)
    page.locator('[data-testid="working-row"][data-id="t6"]').hover()      # the row's menu button shows on hover and takes room
    time.sleep(0.2)
    m = page.evaluate("""() => {
      const row = document.querySelector('[data-testid="working-row"][data-id="t6"]');
      const btn = row.querySelector('button.ttl-btn'), chip = row.querySelector('.wchip'), hint = row.querySelector('.whint');
      return {title: btn.clientWidth, chip: chip ? chip.getBoundingClientRect().width : 0,
              hint: hint.getBoundingClientRect().width, text: row.querySelector('[data-testid="working-name-t6"]').textContent};
    }""")
    check("a hinted row whose title needs the room: the project chip gives way first, to almost nothing, and the hint stays "
          "whole [mutation: chip does not yield]", m["chip"] < 12 and m["hint"] > 40, str(m))
    _shot(page, "r1-hint-row")
    mock_inbox.state(mock)["rows"] = []


def _short_metrics(page):
    return page.evaluate("""() => {
      const r = (q) => { const e = document.querySelector(q); return e && e.getBoundingClientRect(); };
      const wl = r('[data-testid="working-list"]'), tree = r('[data-testid="sidebar"]');
      const orb = r('#orbdock'), foot = r('[data-testid="open-schedules"]');
      const whole = (list, sel) => {
        let n = 0;
        for (const e of document.querySelectorAll(sel)) {
          const b = e.getBoundingClientRect();
          if (b.height < 4 || !list || b.top < list.top - 0.5 || b.bottom > list.bottom + 0.5) continue;
          const hit = document.elementFromPoint(b.left + Math.min(40, b.width / 2), b.top + b.height / 2);
          if (hit && e.contains(hit)) n++;
        }
        return n;
      };
      const sch = document.querySelector('[data-testid="open-schedules"]');
      const sb = sch && sch.getBoundingClientRect();
      const top = sb && document.elementFromPoint(sb.left + 30, sb.top + sb.height / 2);
      return {wrows: whole(wl, '[data-testid="working-row"]'), trows: whole(tree, '[data-testid="sidebar"] .tree-row'),
              wl: wl && wl.height, tree: tree.height, footBottom: foot && foot.bottom, orbTop: orb && orb.top,
              schedulesFree: !!(top && sch.contains(top)), innerH: innerHeight};
    }""")


def _short_checks(page, mock, check, until):
    for height in (700, 560):
        _reset(mock)
        page.evaluate("(k) => localStorage.removeItem(k)", KEY)
        _zfresh(page, mock, until, (1920, height), 160)
        until(lambda: len(_rows(page)) == 6, timeout=5)
        page.evaluate("document.querySelector('[data-testid=\"sidebar\"]').scrollTop = 0")
        m = _short_metrics(page)
        where = f"1920x{height} at 160%, six threads"
        check(f"{where}: at least one whole working row is visible [mutation: list shrinks to nothing]", m["wrows"] >= 1, str(m))
        check(f"{where}: and at least one tree row", m["trows"] >= 1, str(m))
        check(f"{where}: Schedules is above the orb and not covered [mutation: fixed tree minimum]",
              m["schedulesFree"] and m["footBottom"] <= m["orbTop"] + 1, str(m))
        _shot(page, f"6-short-{height}-160")
    _zfresh(page, mock, until, (1600, 900), 100)


IDLE = '[data-testid="inbox-idle-days"]'
RESULT = '[data-testid="inbox-idle-result"]'
SAVE = '[data-testid="inbox-idle-save"]'


def _settings_open(page, mock, until, zoom=100):
    _reset(mock)
    _zfresh(page, mock, until, (1600, 900), zoom)
    open_settings(page, "Inbox")
    until(lambda: page.locator('[data-testid="inbox-settings"]').count() == 1, timeout=4)


def _settings_checks(page, mock, check, until):
    print("\nSettings > Inbox: the idle limit (WS-4)")
    s = _st(mock)
    for zoom in (100, 160):
        s.update({"settings": {"idle_days": 3, "min": 1, "max": 30, "default": 3}, "settings_fail": None,
                  "settings_reply": None, "settings_posts": [], "settings_reads": 0})
        _settings_open(page, mock, until, zoom)
        until(lambda: page.locator(IDLE).count() == 1, timeout=4)
        z = f" at {zoom}%"
        box = page.locator('[data-testid="inbox-settings"]')
        check(f"the box shows the daemon's 3 and its bounds{z} [hard-coded value]",
              page.locator(IDLE).input_value() == "3" and "1–30, default 3" in box.inner_text())
        help_text = page.locator('[data-testid="inbox-idle-help"]').inner_text()
        check(f"the label is linked to the box and the help line says Done and waiting{z}",
              page.get_by_label("Leave In progress after").count() == 1
              and "Done" in help_text and "never leaves on its own" in help_text)
        check(f"Save is disabled until the value changes{z}", page.locator(SAVE).is_disabled())
        _shot_page(page, f"settings-normal-{zoom}")
        for bad in ("0", "31", "2.5"):
            page.locator(IDLE).fill(bad)
            page.locator(SAVE).click()
            check(f"{bad} is refused in the HUD, nothing posted{z} [no client check]",
                  "whole number from 1 to 30" in page.locator(RESULT).inner_text() and not s["settings_posts"],
                  page.locator(RESULT).inner_text())
        page.locator(IDLE).fill("5")
        page.locator(SAVE).click()
        until(lambda: "Saved" in page.locator(RESULT).inner_text(), timeout=4)
        check(f"3 -> 5 posts exactly {{idle_days: 5}}{z}", s["settings_posts"] == [{"idle_days": 5}], str(s["settings_posts"]))
        check(f"the box shows 5 and says saved{z}", page.locator(IDLE).input_value() == "5"
              and page.locator(RESULT).inner_text() == "Saved: 5 days.")
        _shot_page(page, f"settings-saved-{zoom}")
        # A server 400 shows its own sentence inline.
        s["settings_reply"] = (400, "idle_days must be a whole number from 1 to 30 (server)")
        page.locator(IDLE).fill("6")
        page.locator(SAVE).click()
        until(lambda: "(server)" in page.locator(RESULT).inner_text(), timeout=4)
        check(f"a server 400 shows its sentence and keeps the typed 6{z} [400 swallowed]",
              page.locator(RESULT).inner_text() == "idle_days must be a whole number from 1 to 30 (server)"
              and page.locator(IDLE).input_value() == "6" and page.locator(IDLE).is_enabled()
              and page.locator(IDLE).get_attribute("aria-invalid") == "true")
        _shot_page(page, f"settings-400-{zoom}")
        s["settings_reply"] = None
        # A working_changed read does not clobber a typed draft.
        s["settings"]["idle_days"] = 9
        page.locator(IDLE).fill("12")
        reads = s["settings_reads"]
        mock_working.changed(mock)
        check(f"the event made the HUD read the setting again{z}", bool(until(lambda: s["settings_reads"] > reads, timeout=4)))
        time.sleep(0.3)
        check(f"a working_changed read leaves the typed 12 alone{z} [refresh clobbers typing]",
              page.locator(IDLE).input_value() == "12", page.locator(IDLE).input_value())
        # Enter saves, from the keyboard alone.
        page.locator(IDLE).fill("4")
        page.locator(IDLE).press("Enter")
        until(lambda: s["settings_posts"][-1:] == [{"idle_days": 4}], timeout=4)
        check(f"Enter in the box saves{z}", s["settings_posts"][-1] == {"idle_days": 4})
        # Server bounds, not constants: a daemon that says 2..9 refuses 10 here.
        s["settings"] = {"idle_days": 3, "min": 2, "max": 9, "default": 3}
        page.locator(IDLE).fill("")
        mock_working.changed(mock)
        check(f"the new bounds 2-9 arrive with the event{z}", bool(until(lambda: "2–9" in box.inner_text(), timeout=4)))
        posts = len(s["settings_posts"])
        page.locator(IDLE).fill("10")
        page.locator(SAVE).click()
        check(f"the daemon's bounds are the HUD's{z} [hard-coded max 30]",
              "from 2 to 9" in page.locator(RESULT).inner_text() and len(s["settings_posts"]) == posts,
              page.locator(RESULT).inner_text())
    for status in (404, 403):
        s["settings_fail"] = (status, "nope")
        _settings_open(page, mock, until)
        until(lambda: page.locator('[data-testid="inbox-settings-unavailable"]').count() == 1, timeout=4)
        text = page.locator('[data-testid="inbox-settings"]').inner_text()
        check(f"a {status} is a quiet Not available with no field and no value [shown as 3]",
              page.locator(IDLE).count() == 0 and "Not available" in text and "3" not in text, text)
        if status == 404:
            _shot_page(page, "settings-unavailable-100")
    s["settings_fail"] = None
    _zfresh(page, mock, until, (1600, 900), 100)


def _menukeys_checks(page, mock, check, until):
    _open(page, mock, until)
    until(lambda: len(_rows(page)) == 6, timeout=4)
    page.locator('[data-testid="working-menu-t6"]').focus()
    page.keyboard.press("Enter")
    page.wait_for_selector('[data-testid="thread-menu"]')
    until(lambda: _active_id(page) == "tmenu-done", timeout=2)
    # The next read says Done may not happen now: the focused item is disabled under the keyboard.
    for e in _st(mock)["entries"]:
        if e["thread_id"] == "t6":
            e["why_not"] = "a turn is running"
    mock_working.changed(mock)
    until(lambda: page.locator('[data-testid="tmenu-done"]').is_disabled(), timeout=3)
    time.sleep(0.2)
    inside = page.evaluate("() => !!document.activeElement && !!document.activeElement.closest('[data-testid=\"thread-menu\"]')")
    check("a read that disables the focused Done keeps focus inside the menu [mutation: focus to body]",
          inside and _active_id(page) == "tmenu-beside", str(_active_id(page)))
    _shot(page, "7-menu-after-refresh")
    items = page.evaluate("() => [...document.querySelectorAll('[data-testid=\"thread-menu\"] button.mrow:not(:disabled)')]"
                          ".map(e => e.getAttribute('data-testid'))")
    page.keyboard.press("ArrowDown")
    second = _active_id(page)
    page.keyboard.press("ArrowUp")
    back = _active_id(page)
    page.keyboard.press("ArrowUp")
    wrapped = _active_id(page)
    page.keyboard.press("ArrowDown")
    check("ArrowDown and ArrowUp move between the enabled items and wrap [mutation: no arrow keys]",
          len(items) >= 3 and second == items[1] and back == items[0] and wrapped == items[-1]
          and _active_id(page) == items[0], f"{items} {second} {back} {wrapped}")
    page.keyboard.press("Escape")
    until(lambda: _active_id(page) == "working-menu-t6", timeout=2)
    check("Escape still gives focus back to the opener", _active_id(page) == "working-menu-t6", str(_active_id(page)))
    # The project menus share the handler.
    page.locator('[data-testid="project-menu-p2"]').focus()
    page.keyboard.press("Enter")
    page.wait_for_selector('[data-testid="project-menu"]')
    first = _active_id(page)
    page.keyboard.press("ArrowDown")
    second = _active_id(page)
    page.keyboard.press("ArrowUp")
    check("the project menu takes the arrow keys too, from its first item",
          first == "pmenu-edit" and second not in (None, first) and _active_id(page) == first, f"{first} {second} {_active_id(page)}")
    page.keyboard.press("Escape")


SECTIONS = (_section_checks, _collapse_checks, _needs_checks, _open_checks, _done_checks, _conflict_checks,
            _live_checks, _empty_checks, _stale_checks, _inbox_checks, _line_checks, _event_checks, _keys_checks,
            _hint_checks, _page_checks, _settings_checks, _nav_checks, _tall_checks, _short_checks, _menukeys_checks)


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
