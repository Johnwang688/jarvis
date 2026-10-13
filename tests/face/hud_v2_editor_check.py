"""Headless checks for the editor's buffers, tabs and guarded save (ED-2,
editor plan §2.3–§2.4), and the owner's `.env*` files in the editor
(`docs/plans/2026-10-11-env-files.md`).

Free and hermetic. The section runs against a `MockDaemon(0)` of its own and a
browser context of its own; the mock writes files *behind* the editor
(`mock.disk_write`, `mock.disk_delete`) as an agent would, and every request
to 8402/8403/8405 is refused and counted (`hud_v2_check.guard_live`). The real
daemon's side — owner-only routes, the API listener's 403, the 409's etag —
is `tests/v2/editor_files_check.py`.

What it pins, each written to fail on b04d6e8 (ED-1's main):
  - **tabs**: a single click opens an italic preview tab the next single
    click replaces; a double click or an edit pins it; middle-click and ×
    close; closing the last tab on an unsaved edit asks Save / Don't save /
    Cancel, and Save saves first;
  - **one buffer per file**: the same file in two panes is one model — an
    edit in one shows in the other, both tabs dirty, one undo stack, one
    save cleans both; a pane switched away and back gets its tabs back;
  - **the poll**: a clean file an agent rewrote reloads in place with a
    toast naming the writer the activity feed saw, and Ctrl+Z steps back;
    an edited one gets the amber conflict bar — Compare (disk read-only on
    the left, the buffer on the right), Keep mine (saved against the *disk's*
    etag, so another write brings the bar back), Take theirs (asks once,
    undoable); a file gone gets Save to recreate (`expected_etag: null`);
  - **reload**: tabs come back after a page reload and are read from disk
    again; no file's text is ever in localStorage; `beforeunload` is armed
    only while something is dirty;
  - **env files**: `.env.production` opens with the lock ("Jarvis can't read
    this file") and Add to chat disabled, is saved by the owner, and its text
    goes in no request but its own save, never into localStorage or
    `__hud.buffers()`, and is never offered as a word suggestion elsewhere;
  - **read-only kinds**: the gate's state opens read-only with its reason;
    mixed line endings are said in the status line;
  - **Add to chat** puts `path:Lx` and the fenced line in the selected chat's
    box, unsent;
  - **under a card**: the editor is read-only and unfocused, the tabs and the
    conflict bar are inert behind the veil, a typed `y` lands nowhere, and
    Escape denies.

Run: PYTHONPATH=. .venv/bin/python tests/face/hud_v2_editor_check.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.face.hud_v2_layout_check import (  # noqa: E402
    _approval, _boot, _pane, _set_storage, _set_ws, _show, _ws,
)
from tests.face.hud_v2_mock import etag_of  # noqa: E402

ENV_SECRET = "sk-mock-ed2-fake-0000"
# Wide enough that panes 2 and 3 of three columns are not narrow (a narrow
# pane folds its tree and opens every file pinned).
WIDE = (2400, 1000)
TWO = json.dumps({"preset": "cols3", "focused": 2,
                  "panes": [{"view": "chat"}, {"view": "file"}, {"view": "file"}, {"view": "task"}]})


def _tab(n: int, path: str) -> str:
    return _pane(n, f'[data-testid="editor-tab-{path}"]')


def _lines(n: int) -> str:
    return _pane(n, '[data-testid="editor"] .view-lines')


def _text(page, n: int) -> str:
    loc = page.locator(_lines(n))
    return loc.first.inner_text().replace(" ", " ") if loc.count() else ""


def _buffers(page) -> list:
    return page.evaluate("window.__hud.buffers ? window.__hud.buffers() : []")


def _buf(page, path: str):
    return next((b for b in _buffers(page) if b["path"] == path), None)


def _fresh(page, mock, until, ws: str | None = TWO, keep_files=None):
    """A clean start: the files as they were, the workspace given, a reload
    that no leftover edit can hold up (beforeunload is accepted here)."""
    if keep_files is not None:
        mock.world["files"].clear()
        mock.world["files"].update({k: dict(v) for k, v in keep_files.items()})

    def accept(d):
        try:
            d.accept()
        except Exception:
            pass
    page.on("dialog", accept)
    try:
        page.set_viewport_size({"width": WIDE[0], "height": WIDE[1]})
        _set_storage(page, zoom="100", layout="{}")
        _set_ws(page, ws)
        _boot(page, mock, None, until, reload=True)
    finally:
        page.remove_listener("dialog", accept)


def _open(page, until, n: int, name: str, double: bool = False) -> bool:
    """Click `name` in pane N's tree; True once its tab is active and, for a
    text file, Monaco shows it."""
    row = page.locator(_pane(n, f'[data-testid="file-{name}"]'))
    until(lambda: row.count() > 0, timeout=4)
    toggle = page.locator(_pane(n, '[data-testid="tree-toggle"]'))
    # A narrow pane folds its tree once a file opens: unfold it with "Files".
    for _ in range(3):
        if not row.first.is_visible() and toggle.count():
            toggle.click()
            until(lambda: row.first.is_visible(), timeout=2)
        try:
            if double:
                row.dblclick(timeout=2000)
            else:
                row.click(timeout=2000)
            break
        except Exception:
            continue
    path = page.locator(_pane(n, '[data-testid="file-path"]'))
    until(lambda: path.inner_text() == name, timeout=4)
    if name.endswith(".md"):  # Markdown opens rendered: its split view has the editor
        toggle = page.locator(_pane(n, '[data-testid="toggle-rendered"]'))
        until(lambda: toggle.count() > 0, timeout=3)
        if page.locator(_pane(n, '[data-testid="md-split"]')).count() == 0:
            toggle.click()
    host = page.locator(_pane(n, '[data-testid="editor"]'))
    return bool(until(lambda: host.count() == 1 and host.get_attribute("data-language") is not None
                      and host.get_attribute("data-path") == name and _text(page, n).strip() != "", timeout=10))


def _type(page, until, n: int, text: str) -> bool:
    """Type at the end of pane N's file; True once its tab says dirty."""
    for _ in range(3):
        try:
            page.locator(_lines(n)).click(timeout=3000)
        except Exception:
            time.sleep(0.2)
            continue
        page.keyboard.press("Escape")
        page.keyboard.press("Control+End")
        page.keyboard.type(text)
        active = page.locator(_pane(n, '[data-testid="file-path"]')).inner_text()
        if until(lambda: page.locator(_tab(n, active)).get_attribute("data-dirty") == "true", timeout=2):
            return True
    return False


def editor_checks(browser, mock, base, check, until, guard, init_script):
    print("\neditor: buffers, tabs, the guarded save, env files (ED-2)")
    ctx = browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]}, permissions=["microphone"])
    guard(ctx)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    keep = {k: dict(v) for k, v in mock.world["files"].items()}
    try:
        _boot(page, mock, base, until)
        for section in (_tab_checks, _narrow_checks, _shared_checks, _reload_in_place_checks, _conflict_checks,
                        _gone_checks, _persist_checks, _env_checks, _kinds_checks, _add_to_chat_checks,
                        _unmount_checks, _card_checks,
                        # The review of PR #49, each failing on f869543.
                        _rv49_env_not_utf8_checks, _rv49_project_gone_checks, _rv49_compare_loop_checks,
                        _rv49_two_projects_checks, _rv49_crash_save_checks, _checkpoint_crash_copy_checks, _rv49_double_save_checks,
                        _rv49_save_again_checks, _rv49_close_both_checks):
            _fresh(page, mock, until, keep_files=keep)
            try:
                section(page, mock, check, until)
            except Exception as e:  # a section that cannot run is a failure; the rest still run
                check(f"{section.__name__.strip('_')} ran to the end", False, str(e).splitlines()[0][:200])
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        check("no page errors in the editor section", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()
        mock.world["files"].clear()
        mock.world["files"].update(keep)


# ---------------------------------------------------------------------------

def _tab_checks(page, mock, check, until):
    check("setup: calc.py opens in pane 2", _open(page, until, 2, "calc.py"))
    calc = page.locator(_tab(2, "calc.py"))
    check("a single click opens a preview tab (italic)", calc.get_attribute("data-preview") == "true"
          and calc.evaluate("e => getComputedStyle(e.querySelector('.edtab-name')).fontStyle") == "italic")
    _open(page, until, 2, "README.md")
    readme = page.locator(_tab(2, "README.md"))
    check("the next single click replaces the preview tab",
          calc.count() == 0 and readme.count() == 1 and readme.get_attribute("data-preview") == "true")
    readme.dblclick()
    until(lambda: readme.get_attribute("data-preview") == "false", timeout=2)
    check("a double click on the tab pins it", readme.get_attribute("data-preview") == "false")
    _open(page, until, 2, "calc.py")
    check("a pinned tab stays when another file previews beside it",
          readme.count() == 1 and calc.count() == 1 and calc.get_attribute("data-preview") == "true")
    check("setup: an edit", _type(page, until, 2, "\n#pinned_by_an_edit"))
    until(lambda: calc.get_attribute("data-preview") == "false", timeout=2)
    check("an edit pins the preview tab", calc.get_attribute("data-preview") == "false")
    # Double-clicking a tree row opens a pinned tab directly.
    _open(page, until, 2, "notes.txt", double=True)
    notes = page.locator(_tab(2, "notes.txt"))
    until(lambda: notes.count() == 1 and notes.get_attribute("data-preview") == "false", timeout=4)
    check("a double click in the tree opens a pinned tab, and the edited tab is kept",
          notes.count() == 1 and notes.get_attribute("data-preview") == "false"
          and calc.count() == 1 and calc.get_attribute("data-dirty") == "true")
    # Middle-click closes a clean tab at once.
    readme.click(button="middle")
    until(lambda: readme.count() == 0, timeout=2)
    check("middle-click closes a tab", readme.count() == 0)
    # × on the last tab of an unsaved edit asks; Cancel keeps; Save saves, then closes.
    calc.click()
    until(lambda: page.locator(_pane(2, '[data-testid="file-path"]')).inner_text() == "calc.py", timeout=2)
    ask = page.locator(_pane(2, '[data-testid="editor-close-ask"]'))
    page.locator(_pane(2, '[data-testid="editor-tab-close-calc.py"]')).click()
    until(lambda: ask.count() > 0, timeout=2)
    check("× on the last tab of an unsaved edit asks Save / Don't save / Cancel",
          ask.count() == 1 and calc.count() == 1)
    page.locator(_pane(2, '[data-testid="close-cancel"]')).click()
    until(lambda: ask.count() == 0, timeout=2)
    check("Cancel keeps the tab and the edit", calc.count() == 1 and calc.get_attribute("data-dirty") == "true")
    calc.click(button="middle")
    until(lambda: ask.count() > 0, timeout=2)
    check("middle-click on it asks too", ask.count() == 1)
    puts = len(mock.sent("PUT", "/projects/p1/file"))
    page.locator(_pane(2, '[data-testid="close-save"]')).click()
    until(lambda: calc.count() == 0, timeout=4)
    sent = mock.sent("PUT", "/projects/p1/file")[puts:]
    check("Save saves it first, then closes the tab",
          calc.count() == 0 and len(sent) == 1 and sent[0]["path"] == "calc.py"
          and mock.world["files"]["calc.py"]["content"].endswith("#pinned_by_an_edit"), str(sent))


def _narrow_checks(page, mock, check, until):
    """A narrow pane folds its tree behind "Files" the moment a file opens, so
    there is nothing to browse by clicking: every open there is a pinned tab."""
    page.set_viewport_size({"width": 1500, "height": 900})
    _boot(page, mock, None, until, reload=True)
    row = page.locator(_pane(2, '[data-testid="file-calc.py"]'))
    until(lambda: row.count() > 0, timeout=4)
    row.click()
    tab = page.locator(_tab(2, "calc.py"))
    until(lambda: tab.count() == 1, timeout=4)
    tree = page.locator(_pane(2, '[data-testid="filetree"]'))
    check("in a narrow pane a click opens a pinned tab and folds the tree at once",
          tab.get_attribute("data-preview") == "false" and not tree.is_visible()
          and page.locator(_pane(2, '[data-testid="tree-toggle"]')).count() == 1)


def _shared_checks(page, mock, check, until):
    ok = _open(page, until, 2, "calc.py") and _open(page, until, 3, "calc.py")
    check("setup: calc.py open in panes 2 and 3", ok)
    bufs = [b for b in _buffers(page) if b["path"] == "calc.py"]
    check("one file in two panes is one buffer, held by both",
          len(bufs) == 1 and sorted(bufs[0]["refs"]) == ["2|calc.py", "3|calc.py"], str(bufs))
    _type(page, until, 2, "\n#typed_in_pane_2")
    until(lambda: "#typed_in_pane_2" in _text(page, 3), timeout=3)
    check("an edit in one pane shows in the other", "#typed_in_pane_2" in _text(page, 3))
    check("and both tabs carry the dirty dot",
          page.locator(_tab(2, "calc.py")).get_attribute("data-dirty") == "true"
          and page.locator(_tab(3, "calc.py")).get_attribute("data-dirty") == "true")
    # Letting go of a view another pane still shows loses nothing (plan G-3):
    # pane 3 may switch away; pane 2, the edit's last view then, may not.
    _show(page, until, 3, "browser")
    check("a pane whose unsaved edit another pane also shows may switch away",
          page.locator(_pane(3)).get_attribute("data-view") == "browser"
          and page.locator(_tab(2, "calc.py")).get_attribute("data-dirty") == "true")
    refused = page.locator('[data-testid="pane-2-refused"]')
    try:
        _show(page, until, 2, "browser")
    except Exception:
        pass
    until(lambda: refused.count() > 0, timeout=2)
    check("but the edit's last view may not, and says why",
          page.locator(_pane(2)).get_attribute("data-view") == "file" and refused.count() == 1)
    _show(page, until, 3, "file")
    until(lambda: "#typed_in_pane_2" in _text(page, 3), timeout=6)
    check("back in pane 3, the edit is there", "#typed_in_pane_2" in _text(page, 3))
    # One undo stack: Ctrl+Z in pane 3 undoes pane 2's typing.
    page.locator(_lines(3)).click()
    for _ in range(25):
        if "#typed_in_pane_2" not in _text(page, 3):
            break
        page.keyboard.press("Control+z")
        time.sleep(0.05)
    until(lambda: page.locator(_tab(2, "calc.py")).get_attribute("data-dirty") == "false", timeout=2)
    check("one undo stack: Ctrl+Z in pane 3 undoes it, and back at the saved text both read clean",
          "#typed_in_pane_2" not in _text(page, 2)
          and page.locator(_tab(2, "calc.py")).get_attribute("data-dirty") == "false"
          and page.locator(_tab(3, "calc.py")).get_attribute("data-dirty") == "false")
    _type(page, until, 3, "\n#saved_from_pane_3")
    puts = len(mock.sent("PUT", "/projects/p1/file"))
    page.locator(_pane(3, '[data-testid="file-save"]')).click()
    until(lambda: page.locator(_tab(2, "calc.py")).get_attribute("data-dirty") == "false", timeout=4)
    check("one save cleans both panes", len(mock.sent("PUT", "/projects/p1/file")) == puts + 1
          and page.locator(_tab(2, "calc.py")).get_attribute("data-dirty") == "false")
    # A pane switched away and back has its tabs again.
    _show(page, until, 3, "browser")
    until(lambda: page.locator(_tab(3, "calc.py")).count() == 0, timeout=3)
    bufs = [b for b in _buffers(page) if b["path"] == "calc.py"]
    check("a pane switched away lets go of its tab; the other pane still holds the buffer",
          len(bufs) == 1 and bufs[0]["refs"] == ["2|calc.py"], str(bufs))
    _show(page, until, 3, "file")
    until(lambda: page.locator(_tab(3, "calc.py")).count() == 1 and "#saved_from_pane_3" in _text(page, 3),
          timeout=6)
    check("and switched back, its tab is there again, on the shared buffer",
          page.locator(_tab(3, "calc.py")).count() == 1 and "#saved_from_pane_3" in _text(page, 3))


def _reload_in_place_checks(page, mock, check, until):
    check("setup: calc.py open in pane 2", _open(page, until, 2, "calc.py"))
    before = _text(page, 2)
    p1 = next(p for p in mock.world["projects"] if p["id"] == "p1")
    abs_calc = mock.abs_of(p1, "calc.py")
    # Only a write tool names a writer, by the file's absolute path (review of
    # PR #49): a read of it, or a relative path matched by its tail, never.
    mock.emit("tool_started", {"call_id": "r0", "name": "Read", "args": {"file_path": abs_calc}}, thread_id="t1")
    mock.emit("tool_started", {"call_id": "w0", "name": "write_file", "args": {"path": "calc.py"}},
              thread_id="t-elsewhere")
    mock.disk_write("calc.py", "def add(a, b):\n    return a + b\n\n# a first rewrite\n")
    until(lambda: "a first rewrite" in _text(page, 2), timeout=8)
    note = page.locator(_pane(2, '[data-testid="file-note"]'))
    until(lambda: note.count() > 0 and "changed on disk" in note.inner_text(), timeout=2)
    check("a read, or a relative path from no known folder, names no writer",
          note.count() > 0 and note.inner_text().strip().endswith("changed on disk — reloaded"),
          note.inner_text() if note.count() else "no note")
    check("and a reload in place never pins a preview tab (it is not an edit)",
          page.locator(_tab(2, "calc.py")).get_attribute("data-preview") == "true")
    before = _text(page, 2)
    mock.emit("tool_started", {"call_id": "w1", "name": "Write", "args": {"file_path": abs_calc}}, thread_id="t1")
    mock.disk_write("calc.py", "def add(a, b):\n    return a + b\n\n# an agent added this\n")
    until(lambda: "an agent added this" in _text(page, 2), timeout=8)
    check("a clean file an agent rewrote reloads in place", "an agent added this" in _text(page, 2))
    until(lambda: note.count() > 0 and "by Jarvis" in note.inner_text(), timeout=2)
    check("with a toast that names the writer the activity feed saw",
          note.count() > 0 and "changed on disk by Jarvis · desk chat" in note.inner_text(),
          note.inner_text() if note.count() else "no note")
    check("and it is still clean, at the disk's etag",
          page.locator(_tab(2, "calc.py")).get_attribute("data-dirty") == "false"
          and (_buf(page, "calc.py") or {}).get("etag") == etag_of(mock.world["files"]["calc.py"]["content"]))
    mock.emit("tool_finished", {"call_id": "r0", "name": "Read", "ok": True, "summary": ""}, thread_id="t1")
    mock.emit("tool_finished", {"call_id": "w1", "name": "Write", "ok": True, "summary": ""}, thread_id="t1")
    page.locator(_lines(2)).click()
    page.keyboard.press("Control+z")
    until(lambda: "an agent added this" not in _text(page, 2), timeout=2)
    check("Ctrl+Z steps back over the reload (it was one undoable edit)",
          "an agent added this" not in _text(page, 2) and _text(page, 2).strip() == before.strip())
    # Put it back so this section leaves nothing dirty.
    page.keyboard.press("Control+Shift+z")
    until(lambda: page.locator(_tab(2, "calc.py")).get_attribute("data-dirty") == "false", timeout=2)


def _conflict_checks(page, mock, check, until):
    check("setup: calc.py open in pane 2", _open(page, until, 2, "calc.py"))
    check("setup: an edit", _type(page, until, 2, "\n#mine"))
    theirs = "def add(a, b):\n    return a + b  # theirs\n"
    mock.disk_write("calc.py", theirs)
    bar = page.locator(_pane(2, '[data-testid="editor-conflict"]'))
    until(lambda: bar.count() > 0, timeout=8)
    check("an edited file an agent rewrote gets the conflict bar, never a reload",
          bar.count() == 1 and bar.get_attribute("data-state") == "changed" and "#mine" in _text(page, 2))
    check("and the bar is amber (it waits on the owner)",
          "251, 191, 36" in bar.evaluate("e => getComputedStyle(e).borderBottomColor + getComputedStyle(e).backgroundColor"))
    # Compare: the disk on the left, read-only; the buffer on the right.
    page.locator(_pane(2, '[data-testid="conflict-compare"]')).click()
    cmp_ = page.locator(_pane(2, '[data-testid="editor-compare"]'))
    until(lambda: cmp_.count() > 0 and cmp_.locator(".editor.original .view-lines").count() > 0, timeout=8)
    left = cmp_.locator(".editor.original .view-lines").first.inner_text().replace(" ", " ") if cmp_.count() else ""
    right = cmp_.locator(".editor.modified .view-lines").first.inner_text().replace(" ", " ") if cmp_.count() else ""
    check("Compare shows the disk's text on the left and the buffer on the right",
          "# theirs" in left and "#mine" in right and "#mine" not in left, f"{left!r} | {right!r}")
    ro = page.evaluate("""() => (window.__hud.monacoEditors() || []).filter(e => e.uri && e.uri.startsWith('jarvis-disk:'))
      .map(e => e.readOnly)""")
    check("the disk's side is read-only", ro == [True], str(ro))
    # Another write while comparing: the bar says "again", and Compare's left
    # side is read again — Keep mine saves against what the owner can see.
    theirs = "def add(a, b):\n    return a + b  # theirs, again\n"
    mock.disk_write("calc.py", theirs)
    orig = cmp_.locator(".editor.original .view-lines").first
    until(lambda: bar.get_attribute("data-again") == "true" and "again" in orig.inner_text(), timeout=8)
    check("a write while comparing says 'again', and Compare shows the newer disk",
          bar.get_attribute("data-again") == "true" and "changed on disk again" in bar.inner_text()
          and "again" in orig.inner_text().replace("\u00a0", " "), bar.inner_text())
    # Merge in the compare's right side, then Keep mine.
    cmp_.locator(".editor.modified .view-lines").first.click()
    page.keyboard.press("Control+End")
    page.keyboard.type("\n#merged")
    shown = etag_of(theirs)
    puts = len(mock.sent("PUT", "/projects/p1/file"))
    page.locator(_pane(2, '[data-testid="conflict-keep"]')).click()
    until(lambda: bar.count() == 0, timeout=4)
    sent = mock.sent("PUT", "/projects/p1/file")[puts:]
    check("Keep mine saves the merged buffer against the disk etag the bar was shown",
          bar.count() == 0 and len(sent) == 1 and sent[0].get("expected_etag") == shown
          and mock.world["files"]["calc.py"]["content"].endswith("#mine\n#merged"), str(sent))
    check("and Compare closes with the conflict", page.locator(_pane(2, '[data-testid="editor-compare"]')).count() == 0)
    # Keep mine against the shown etag: a write that lands meanwhile brings the bar back.
    _type(page, until, 2, "\n#again")
    mock.disk_write("calc.py", "# second agent write\n")
    until(lambda: bar.count() > 0, timeout=8)
    mock.disk_write("calc.py", "# third agent write\n")
    page.locator(_pane(2, '[data-testid="conflict-keep"]')).click()
    time.sleep(0.6)
    check("a write after the bar showed is never clobbered: Keep mine 409s and the bar stays",
          bar.count() == 1 and mock.world["files"]["calc.py"]["content"] == "# third agent write\n")
    # Take theirs asks once, then loads the disk's text, undoably.
    page.locator(_pane(2, '[data-testid="conflict-take"]')).click()
    yes = page.locator(_pane(2, '[data-testid="conflict-take-yes"]'))
    until(lambda: yes.count() > 0, timeout=2)
    check("Take theirs asks once", yes.count() == 1 and "#again" in _text(page, 2))
    yes.click()
    until(lambda: bar.count() == 0 and "third agent write" in _text(page, 2), timeout=4)
    check("and then the buffer is the disk's, clean",
          "third agent write" in _text(page, 2) and "#again" not in _text(page, 2)
          and page.locator(_tab(2, "calc.py")).get_attribute("data-dirty") == "false")
    page.locator(_lines(2)).click()
    page.keyboard.press("Control+z")
    until(lambda: "#again" in _text(page, 2), timeout=2)
    check("Ctrl+Z brings the edit back (Take theirs was one undoable step)", "#again" in _text(page, 2))
    page.keyboard.press("Control+Shift+z")
    until(lambda: page.locator(_tab(2, "calc.py")).get_attribute("data-dirty") == "false", timeout=2)


def _gone_checks(page, mock, check, until):
    check("setup: calc.py open in pane 2", _open(page, until, 2, "calc.py"))
    check("setup: an edit", _type(page, until, 2, "\n#kept_though_gone"))
    mock.disk_delete("calc.py")
    bar = page.locator(_pane(2, '[data-testid="editor-conflict"]'))
    until(lambda: bar.count() > 0, timeout=8)
    check("a file deleted under an edit says it is gone", bar.count() == 1 and bar.get_attribute("data-state") == "gone")
    puts = len(mock.sent("PUT", "/projects/p1/file"))
    page.locator(_pane(2, '[data-testid="conflict-recreate"]')).click()
    until(lambda: bar.count() == 0, timeout=4)
    sent = mock.sent("PUT", "/projects/p1/file")[puts:]
    check("Save to recreate writes it with expected_etag null",
          len(sent) == 1 and "expected_etag" in sent[0] and sent[0]["expected_etag"] is None
          and "calc.py" in mock.world["files"]
          and mock.world["files"]["calc.py"]["content"].endswith("#kept_though_gone"), str(sent))
    mock.disk_delete("calc.py")
    until(lambda: bar.count() > 0, timeout=8)
    page.locator(_pane(2, '[data-testid="conflict-close"]')).click()
    until(lambda: page.locator(_tab(2, "calc.py")).count() == 0, timeout=3)
    check("and a clean gone file's Close closes the tab", page.locator(_tab(2, "calc.py")).count() == 0)


def _persist_checks(page, mock, check, until):
    _open(page, until, 2, "calc.py", double=True)
    _open(page, until, 2, "README.md", double=True)
    tabs = _ws(page)["ws"]["panes"][1].get("editors")
    check("the pane's tabs are stored (PaneSpec.editors)",
          tabs == {"paths": ["calc.py", "README.md"], "active": "README.md", "preview": None}, str(tabs))
    check("setup: an edit", _type(page, until, 2, "\n#unsaved_never_stored"))
    stored = page.evaluate("JSON.stringify(Object.assign({}, localStorage))")
    check("unsaved text is never in localStorage, nor any file's text",
          "unsaved_never_stored" not in stored and "return a + b" not in stored)
    dialogs: list[str] = []

    def dismiss(d):
        dialogs.append(d.type)
        d.dismiss()
    page.on("dialog", dismiss)
    try:
        page.evaluate("location.reload()")
    except Exception:
        pass
    time.sleep(0.6)
    page.remove_listener("dialog", dismiss)
    check("while something is dirty, closing the window asks first (beforeunload)", dialogs == ["beforeunload"],
          str(dialogs))
    # Throw the edit away (Reload asks first), then reload: nothing asks.
    page.locator(_pane(2, '[data-testid="file-reload"]')).click()
    page.locator(_pane(2, '[data-testid="file-discard-yes"]')).click()
    until(lambda: page.locator(_tab(2, "README.md")).get_attribute("data-dirty") == "false", timeout=3)
    gets = len(mock.sent("GET", "/projects/p1/file"))
    dialogs.clear()
    page.on("dialog", dismiss)
    try:
        _boot(page, mock, None, until, reload=True)
    finally:
        page.remove_listener("dialog", dismiss)
    check("clean, a reload asks nothing", dialogs == [], str(dialogs))
    until(lambda: page.locator(_tab(2, "README.md")).count() == 1 and page.locator(_tab(2, "calc.py")).count() == 1,
          timeout=6)
    check("after a reload the tabs come back, the active one still active",
          page.locator(_tab(2, "calc.py")).count() == 1 and page.locator(_tab(2, "README.md")).count() == 1
          and page.locator(_pane(2, '[data-testid="file-path"]')).inner_text() == "README.md")
    until(lambda: len(mock.sent("GET", "/projects/p1/file")) >= gets + 2, timeout=4)
    paths = sorted(b.get("path") for b in mock.sent("GET", "/projects/p1/file")[gets:])
    check("and every tab is read from disk again", paths[:2] == ["README.md", "calc.py"], str(paths))


def _env_checks(page, mock, check, until):
    page.locator(_pane(2, '[data-testid="file-.env.production"]')).click()
    path = page.locator(_pane(2, '[data-testid="file-path"]'))
    until(lambda: path.inner_text() == ".env.production", timeout=4)
    host = page.locator(_pane(2, '[data-testid="editor"]'))
    until(lambda: host.count() == 1 and host.get_attribute("data-language") is not None, timeout=10)
    lock = page.locator(_pane(2, '[data-testid="editor-lock"]'))
    check("an env file opens with the lock badge: Jarvis can't read this file",
          lock.count() == 1 and "Jarvis can't read this file" in lock.inner_text())
    check("its tab and its tree row say so too",
          "Jarvis can't read" in (page.locator(_tab(2, ".env.production")).get_attribute("title") or "")
          and page.locator(_pane(2, '[data-testid="file-.env.production"]')).get_attribute("data-policy") == "env")
    check("Add to chat is disabled for it", page.locator(_pane(2, '[data-testid="add-to-chat"]')).is_disabled())
    check("its model is the env language, so its words are offered nowhere else",
          host.get_attribute("data-language") == "dotenv", str(host.get_attribute("data-language")))
    check("the owner edits it", _type(page, until, 2, "\nOTHER=owner-typed-value"))
    puts = len(mock.sent("PUT", "/projects/p1/file"))
    page.locator(_lines(2)).click()
    page.keyboard.press("Control+s")
    until(lambda: len(mock.sent("PUT", "/projects/p1/file")) > puts, timeout=4)
    sent = mock.sent("PUT", "/projects/p1/file")[puts:]
    check("and saves it, against the etag it was read as",
          len(sent) == 1 and sent[0]["path"] == ".env.production"
          and sent[0]["expected_etag"] == etag_of(f"API_KEY={ENV_SECRET}\n")
          and mock.world["files"][".env.production"]["content"].endswith("OTHER=owner-typed-value"), str(sent))
    # Its text goes in no request but its own save.
    leaked = [(m, p) for m, p, b in mock.calls
              if ENV_SECRET in json.dumps(b) and not (m == "PUT" and p == "/projects/p1/file")]
    check("its text goes in no request but its own save", not leaked, str(leaked[:3]))
    stored = page.evaluate("JSON.stringify(Object.assign({}, localStorage, sessionStorage))")
    check("never into localStorage or sessionStorage",
          ENV_SECRET not in stored and "owner-typed-value" not in stored)
    check("and __hud.buffers() carries no text", ENV_SECRET not in json.dumps(_buffers(page)))
    # A word from the env file is never suggested while typing in another
    # file — a plain-text one, the language an env file would get by its name.
    _open(page, until, 3, "notes.txt")
    page.locator(_lines(3)).click()
    page.keyboard.press("Control+End")
    page.keyboard.type("\nmoc")
    page.keyboard.press("Control+Space")
    time.sleep(0.8)
    rows = page.locator(_pane(3, ".suggest-widget.visible .monaco-list-row"))
    labels = [rows.nth(i).get_attribute("aria-label") or "" for i in range(rows.count())]
    check("a word in the env file is never offered as a suggestion in another file",
          not any("mock" in lbl or ENV_SECRET in lbl for lbl in labels), str(labels))
    page.keyboard.press("Escape")
    page.keyboard.press("Control+z")
    page.keyboard.press("Control+z")


def _kinds_checks(page, mock, check, until):
    page.locator(_pane(2, '[data-testid="file-allowlist.json"]')).click()
    host = page.locator(_pane(2, '[data-testid="editor"]'))
    until(lambda: host.count() == 1 and host.get_attribute("data-path") == "allowlist.json"
          and host.get_attribute("data-language") is not None, timeout=10)
    reason = page.locator(_pane(2, '[data-testid="editor-readonly-reason"]'))
    check("the gate's state opens read-only, with its reason beside the lock",
          reason.count() == 1 and "permission state" in reason.inner_text()
          and host.get_attribute("data-readonly") == "true"
          and "permission state" in (reason.get_attribute("title") or ""))
    # Monaco draws the file a frame or more after it holds it (data-language):
    # under load `before` was read from an empty view, and the check then
    # compared "" with the file's own text. Wait until the file is drawn.
    gate_text = mock.world["files"]["allowlist.json"]["content"].strip()
    until(lambda: _text(page, 2).strip() == gate_text, timeout=10)
    before = _text(page, 2)
    page.locator(_lines(2)).click()
    page.keyboard.type("x")
    time.sleep(0.2)
    # The buffer's own dirty flag too: it is set in the keystroke's change
    # event, so a late frame cannot hide an edit the view has not drawn yet.
    gate_buf = _buf(page, "allowlist.json") or {}
    check("typing changes nothing and Save stays off",
          before.strip() == gate_text and _text(page, 2) == before and gate_buf.get("dirty") is False
          and page.locator(_pane(2, '[data-testid="file-save"]')).is_disabled(),
          repr(_text(page, 2)) + f" dirty={gate_buf.get('dirty')!r}")
    check("Add to chat stays open for it (Jarvis may read the gate's state)",
          not page.locator(_pane(2, '[data-testid="add-to-chat"]')).is_disabled())
    # Mixed line endings are said, with what a save would do.
    _open(page, until, 2, "notes.txt")
    status = page.locator(_pane(2, '[data-testid="editor-status"]'))
    check("mixed line endings are said in the status line, with what a save writes",
          status.count() == 1 and status.get_attribute("data-eol") == "mixed"
          and "saving writes" in status.inner_text(), status.inner_text() if status.count() else "")
    _open(page, until, 2, "calc.py")
    check("an LF file says LF", "LF" in status.inner_text() and status.get_attribute("data-eol") == "LF")


def _add_to_chat_checks(page, mock, check, until):
    check("setup: calc.py open in pane 2", _open(page, until, 2, "calc.py"))
    page.locator(_pane(1, '[data-testid="input"]')).click()  # pane 1's chat is the selected one
    page.locator(_lines(2)).click()
    page.keyboard.press("Control+Home")
    sends = sum(1 for m, p, _ in mock.calls if m == "POST" and p.endswith("/send"))
    page.locator(_pane(2, '[data-testid="add-to-chat"]')).click()
    box = page.locator(_pane(1, '[data-testid="input"]'))
    until(lambda: "calc.py:L1" in box.input_value(), timeout=2)
    value = box.input_value()
    check("Add to chat puts the line, with its place, in the selected chat's box",
          value.startswith("calc.py:L1\n```\ndef add(a, b):\n```"), repr(value))
    check("and sends nothing", sum(1 for m, p, _ in mock.calls if m == "POST" and p.endswith("/send")) == sends)
    box.fill("")


def _unmount_checks(page, mock, check, until):
    """The registry lives outside React: a File pane that unmounts — here the
    workspace's error boundary taking it down — never takes an edit with it."""
    check("setup: calc.py open in pane 2", _open(page, until, 2, "calc.py"))
    check("setup: an edit", _type(page, until, 2, "\n#survives_the_unmount"))
    page.evaluate("window.__hudCrashProbe = 'workspace'")
    page.evaluate("window.__hud.dispatch({type: 'patch', patch: {error: 'crash probe render'}})")
    until(lambda: page.locator('[data-testid="hud-crashed"]').count() > 0, timeout=4)
    page.evaluate("delete window.__hudCrashProbe")
    gone = page.locator('[data-testid="editor"]').count() == 0
    buf = _buf(page, "calc.py") or {}
    check("with the pane unmounted, the edit is still in its buffer, held by no tab",
          gone and page.locator('[data-testid="hud-crashed"]').count() == 1
          and buf.get("dirty") is True and buf.get("refs") == [], str(buf))
    dialogs: list[str] = []

    def dismiss(d):
        dialogs.append(d.type)
        d.dismiss()
    page.on("dialog", dismiss)
    try:
        page.evaluate("location.reload()")
    except Exception:
        pass
    time.sleep(0.6)
    page.remove_listener("dialog", dismiss)
    check("and closing the window still asks first", dialogs == ["beforeunload"], str(dialogs))


def _card_checks(page, mock, check, until):
    check("setup: calc.py open in pane 2", _open(page, until, 2, "calc.py"))
    check("setup: an edit", _type(page, until, 2, "\n#under_a_card"))
    mock.disk_write("calc.py", "# an agent wrote this meanwhile\n")
    bar = page.locator(_pane(2, '[data-testid="editor-conflict"]'))
    until(lambda: bar.count() > 0, timeout=8)
    _open(page, until, 2, "notes.txt", double=True)
    page.locator(_tab(2, "calc.py")).click()
    until(lambda: page.locator(_pane(2, '[data-testid="file-path"]')).inner_text() == "calc.py", timeout=2)
    page.locator(_lines(2)).click()
    before = _text(page, 2)
    _approval(mock, "edcard1")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    time.sleep(0.25)
    host = page.locator(_pane(2, '[data-testid="editor"]'))
    eds = page.evaluate("window.__hud.monacoEditors()")
    check("under a card the editor is read-only and unfocused",
          host.get_attribute("data-readonly") == "true" and all(e["readOnly"] and not e["focused"] for e in eds),
          str(eds))
    inert = page.evaluate("""() => ['editor-conflict', 'editor-tabs', 'file-save'].map(t =>
      !!document.querySelector('[data-testid="pane-2"] [data-testid="' + t + '"]').closest('[inert]'))""")
    check("the tabs, the conflict bar and Save are behind the inert veil", inert == [True, True, True], str(inert))
    page.keyboard.type("y")
    page.keyboard.press("Control+s")
    time.sleep(0.2)
    check("a typed y lands nowhere and Ctrl+S saves nothing",
          _text(page, 2) == before and mock.world["files"]["calc.py"]["content"] == "# an agent wrote this meanwhile\n")
    page.keyboard.press("Escape")
    body = until(lambda: mock.sent("POST", "/approvals/edcard1") or None, timeout=4)
    check("Escape denies the card", bool(body) and body[-1].get("decision") == "deny")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    until(lambda: host.get_attribute("data-readonly") == "false", timeout=2)
    check("and the edit and the bar are still there after it", "#under_a_card" in _text(page, 2) and bar.count() == 1)


# ---------------------------------------------------------------------------
# the review of PR #49 (each failing on f869543)
# ---------------------------------------------------------------------------

def _rv49_env_not_utf8_checks(page, mock, check, until):
    """MED1: an env file the daemon says is not editable (one that is not
    UTF-8) is still an env file: the lock, Add to chat closed, `dotenv` — and
    read-only, because the HUD follows `editable`, never the kind alone."""
    w = mock.world
    w["files"][".env.utf16"] = {"content": "API_KEY=sk-mock-utf16-fake\n", "mtime": 1757900300}
    w["kinds"][".env.utf16"] = "env"
    w["uneditable"] = {".env.utf16"}
    row = {"name": ".env.utf16", "kind": "file", "size": 30, "mtime": 1757900300, "policy": "env"}
    w["tree"][""].append(row)
    try:
        _boot(page, mock, None, until, reload=True)
        page.locator(_pane(2, '[data-testid="file-.env.utf16"]')).click()
        host = page.locator(_pane(2, '[data-testid="editor"]'))
        until(lambda: host.count() == 1 and host.get_attribute("data-path") == ".env.utf16"
              and host.get_attribute("data-language") is not None, timeout=10)
        lock = page.locator(_pane(2, '[data-testid="editor-lock"]'))
        reason = page.locator(_pane(2, '[data-testid="editor-readonly-reason"]'))
        check("MED1: an env file that is not editable keeps the lock, and says it is read-only",
              lock.count() == 1 and reason.count() == 1 and "not text" in reason.inner_text(),
              reason.inner_text() if reason.count() else "no read-only reason")
        check("MED1: Add to chat stays closed and its language stays dotenv",
              page.locator(_pane(2, '[data-testid="add-to-chat"]')).is_disabled()
              and host.get_attribute("data-language") == "dotenv")
        check("MED1: it opens read-only, Save off", host.get_attribute("data-readonly") == "true"
              and page.locator(_pane(2, '[data-testid="file-save"]')).is_disabled())
        before = _text(page, 2)
        page.locator(_lines(2)).click()
        page.keyboard.type("x")
        time.sleep(0.2)
        check("MED1: typing changes nothing", _text(page, 2) == before
              and (_buf(page, ".env.utf16") or {}).get("editable") is False)
    finally:
        w["files"].pop(".env.utf16", None)
        w["kinds"].pop(".env.utf16", None)
        w.pop("uneditable", None)
        w["tree"][""].remove(row)


def _rv49_project_gone_checks(page, mock, check, until):
    """H2 (and H1 behind it): two File panes pinned to one project share an
    edit; the project is archived. Both stay pinned with the gone prompt —
    pane by pane, each saw "another pane holds it" and both let go — and a ×
    on any other pane never discards the edit."""
    tabs = {"paths": ["calc.py"], "active": "calc.py", "preview": None}
    ws = json.dumps({"preset": "cols3", "focused": 2, "panes": [
        {"view": "chat"}, {"view": "file", "projectId": "p2", "editors": tabs},
        {"view": "file", "projectId": "p2", "editors": tabs}, {"view": "task"}]})
    _set_ws(page, ws)
    _boot(page, mock, None, until, reload=True)
    until(lambda: "return a + b" in _text(page, 2) and "return a + b" in _text(page, 3), timeout=10)
    check("setup: calc.py of p2 in panes 2 and 3", "return a + b" in _text(page, 3))
    check("setup: an edit in pane 2", _type(page, until, 2, "\n#shared_edit"))
    until(lambda: "#shared_edit" in _text(page, 3), timeout=3)
    mock.emit("project_archived", {"project_id": "p2"}, project_id="p2")
    gone2, gone3 = page.locator('[data-testid="pane-2-gone"]'), page.locator('[data-testid="pane-3-gone"]')
    until(lambda: gone2.count() > 0 and gone3.count() > 0, timeout=4)
    panes = _ws(page)["ws"]["panes"]
    buf = _buf(page, "calc.py") or {}
    check("H2: the project archived, both panes sharing the edit stay pinned and both ask",
          gone2.count() == 1 and gone3.count() == 1 and panes[1].get("projectId") == "p2"
          and panes[2].get("projectId") == "p2", str(panes[1:3]))
    check("H2: and the edit is still held by both tabs",
          buf.get("dirty") is True and sorted(buf.get("refs") or []) == ["2|calc.py", "3|calc.py"], str(buf))
    # × on the chat pane: a pane that held none of it discards none of it (H1).
    page.locator('[data-testid="pane-1-close"]').click()
    until(lambda: page.locator('[data-testid="pane-1"]').count() == 0, timeout=4)
    buf = _buf(page, "calc.py") or {}
    check("H1: × on another pane leaves the edit alone", buf.get("dirty") is True, str(buf))
    # Discard in pane 2: pane 3 still holds the edit, and still asks.
    page.locator('[data-testid="pane-2-discard"]').click()
    until(lambda: gone2.count() == 0, timeout=4)
    buf = _buf(page, "calc.py") or {}
    check("discarding in one pane leaves the edit the other pane holds, which still asks",
          buf.get("dirty") is True and buf.get("refs") == ["3|calc.py"] and gone3.count() == 1, str(buf))
    page.locator('[data-testid="pane-3-discard"]').click()
    until(lambda: _buf(page, "calc.py") is None or not (_buf(page, "calc.py") or {}).get("dirty"), timeout=4)
    check("and discarding in the last one throws it away",
          not any(b.get("dirty") for b in _buffers(page)), str(_buffers(page)))


def _rv49_compare_loop_checks(page, mock, check, until):
    """M2: Compare reads the disk again once per new conflict etag — never in
    a loop while the disk it reads disagrees with what the poll said."""
    w = mock.world
    try:
        check("setup: calc.py open in pane 2", _open(page, until, 2, "calc.py"))
        check("setup: an edit", _type(page, until, 2, "\n#mine_m2"))
        mock.disk_write("calc.py", "# disk A\n")
        bar = page.locator(_pane(2, '[data-testid="editor-conflict"]'))
        until(lambda: bar.count() > 0, timeout=8)
        page.locator(_pane(2, '[data-testid="conflict-compare"]')).click()
        cmp_ = page.locator(_pane(2, '[data-testid="editor-compare"]'))
        until(lambda: cmp_.count() > 0 and cmp_.locator(".editor.original .view-lines").count() > 0, timeout=8)
        # The poll will say B while every read already sees C: the disk moved
        # again between the poll's stat and Compare's read.
        w["stat_etags"] = {"calc.py": etag_of("# disk B\n")}
        mock.disk_write("calc.py", "# disk C\n")
        until(lambda: bar.get_attribute("data-again") == "true", timeout=8)
        start = len(mock.sent("GET", "/projects/p1/file"))
        time.sleep(1.5)
        reads = len(mock.sent("GET", "/projects/p1/file")) - start
        check("M2: Compare re-reads once for the new etag, never in a loop", reads <= 2, f"{reads} reads in 1.5 s")
    finally:
        w.pop("stat_etags", None)


def _rv49_two_projects_checks(page, mock, check, until):
    """L1: one file reached through two projects, restored in two panes at
    once, is one model — never one disposed under its tab."""
    w = mock.world
    p1 = next(p for p in w["projects"] if p["id"] == "p1")
    p9 = dict(p1, id="p9", name="jarvis-src", root=p1["root"].rstrip("/") + "/src", inbox=False)
    w["projects"].append(p9)
    w["files"]["main.py"] = dict(w["files"]["src/main.py"])
    errors: list[str] = []
    on_error = lambda e: errors.append(str(e))  # noqa: E731
    page.on("pageerror", on_error)
    try:
        ws = json.dumps({"preset": "cols3", "focused": 2, "panes": [
            {"view": "chat"},
            {"view": "file", "projectId": "p1", "editors": {"paths": ["src/main.py"], "active": "src/main.py"}},
            {"view": "file", "projectId": "p9", "editors": {"paths": ["main.py"], "active": "main.py"}},
            {"view": "task"}]})
        _set_ws(page, ws)
        _boot(page, mock, None, until, reload=True)
        until(lambda: "print" in _text(page, 2) and "print" in _text(page, 3), timeout=10)
        bufs = [b for b in _buffers(page) if b["abs"].endswith("/src/main.py")]
        check("L1: one file through two projects, restored at once, is one buffer held by both",
              len(bufs) == 1 and sorted(bufs[0]["refs"]) == ["2|src/main.py", "3|main.py"], str(bufs))
        check("L1: and no tab died on a disposed model", not any("disposed" in e.lower() for e in errors),
              "; ".join(errors[:2]))
        _type(page, until, 2, "\n#through_p1")
        until(lambda: "#through_p1" in _text(page, 3), timeout=3)
        check("L1: an edit through one project shows through the other", "#through_p1" in _text(page, 3))
    finally:
        page.remove_listener("pageerror", on_error)
        w["projects"].remove(p9)
        w["files"].pop("main.py", None)


def _rv49_crash_save_checks(page, mock, check, until):
    """L3: after a crash the kept edit can reach the disk — "Save N unsaved
    files" on the crash prompt — and never under a card."""
    check("setup: calc.py open in pane 2", _open(page, until, 2, "calc.py"))
    check("setup: an edit", _type(page, until, 2, "\n#saved_after_the_crash"))
    page.evaluate("window.__hudCrashProbe = 'workspace'")
    page.evaluate("window.__hud.dispatch({type: 'patch', patch: {error: 'crash probe render'}})")
    until(lambda: page.locator('[data-testid="hud-crashed"]').count() > 0, timeout=4)
    page.evaluate("delete window.__hudCrashProbe")
    save = page.locator('[data-testid="hud-crashed-save"]')
    check("L3: the crash prompt offers to save the kept edit", save.count() == 1
          and "Save 1 unsaved file" in save.inner_text(), save.inner_text() if save.count() else "none")
    _approval(mock, "crashsave1")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    time.sleep(0.25)
    puts = len(mock.sent("PUT", "/projects/p1/file"))
    check("L3: under a card it is disabled and behind the inert veil",
          save.is_disabled() and page.evaluate(
              "!!document.querySelector('[data-testid=\"hud-crashed-save\"]').closest('[inert]')"))
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    check("L3: nothing was saved under the card", len(mock.sent("PUT", "/projects/p1/file")) == puts)
    save.click()
    until(lambda: mock.world["files"]["calc.py"]["content"].endswith("#saved_after_the_crash"), timeout=4)
    saved = page.locator('[data-testid="hud-crashed-saved"]')
    until(lambda: saved.count() > 0, timeout=3)
    check("L3: and it saves, against the etag it was read as",
          mock.world["files"]["calc.py"]["content"].endswith("#saved_after_the_crash")
          and mock.sent("PUT", "/projects/p1/file")[-1].get("expected_etag")
          == etag_of("def add(a, b):\n    return a + b\n")
          and saved.count() == 1 and save.count() == 0)


def _rv49_double_save_checks(page, mock, check, until):
    """One save at a time: a second Ctrl+S while the first is on its way sends
    nothing (the mutation that dropped the guard survived the first round)."""
    check("setup: calc.py open in pane 2", _open(page, until, 2, "calc.py"))
    check("setup: an edit", _type(page, until, 2, "\n#twice"))
    mock.world["put_delays"] = [1.0]
    try:
        puts = len(mock.sent("PUT", "/projects/p1/file"))
        page.locator(_lines(2)).click()
        page.keyboard.press("Control+s")
        time.sleep(0.15)
        page.keyboard.press("Control+s")
        until(lambda: page.locator(_tab(2, "calc.py")).get_attribute("data-dirty") == "false", timeout=5)
        time.sleep(0.5)
        sent = mock.sent("PUT", "/projects/p1/file")[puts:]
        check("a second Ctrl+S during a save sends no second PUT, and raises no conflict bar",
              len(sent) == 1 and page.locator(_pane(2, '[data-testid="editor-conflict"]')).count() == 0,
              f"{len(sent)} PUTs")
    finally:
        mock.world.pop("put_delays", None)


def _rv49_save_again_checks(page, mock, check, until):
    """LOW-2: on a slow write (/mnt/c), type A, Ctrl+S, type B, Ctrl+S — both
    reach the disk. The second waits for the first and then saves once against
    the etag the first answer returned; "saving…" shows meanwhile."""
    check("setup: calc.py open in pane 2", _open(page, until, 2, "calc.py"))
    check("setup: an edit A", _type(page, until, 2, "\n#editA"))
    mock.world["put_delays"] = [3.0]
    try:
        puts = len(mock.sent("PUT", "/projects/p1/file"))
        page.keyboard.press("Control+s")
        status = page.locator(_pane(2, '[data-testid="editor-saving"]'))
        tab = page.locator(_tab(2, "calc.py"))
        check("saving… shows on the tab and the status while the write is out",
              until(lambda: status.count() == 1 and tab.get_attribute("data-saving") == "true", timeout=1.5))
        check("setup: an edit B while the first save is out", _type(page, until, 2, "\n#editB"))
        out = len(mock.sent("PUT", "/projects/p1/file")) - puts == 1 and tab.get_attribute("data-saving") == "true"
        page.keyboard.press("Control+s")
        check("setup: the second Ctrl+S lands while the first save is still out", out)
        until(lambda: len(mock.sent("PUT", "/projects/p1/file")) - puts >= 2
              and tab.get_attribute("data-dirty") == "false" and tab.get_attribute("data-saving") == "false",
              timeout=6)
        time.sleep(0.3)
        sent = mock.sent("PUT", "/projects/p1/file")[puts:]
        disk = mock.world["files"]["calc.py"]["content"]
        check("Ctrl+S during a save is not dropped: one follow-up, against the etag the first answer returned",
              len(sent) == 2 and sent[1].get("expected_etag") == etag_of(sent[0].get("content", ""))
              and "#editA" in sent[0].get("content", "") and "#editB" not in sent[0].get("content", "")
              and sent[1].get("content") == disk and "#editA" in disk and "#editB" in disk,
              f"{len(sent)} PUTs")
        check("…and it ends clean, with no conflict bar and no saving… left",
              tab.get_attribute("data-dirty") == "false" and status.count() == 0
              and page.locator(_pane(2, '[data-testid="editor-conflict"]')).count() == 0)
    finally:
        mock.world.pop("put_delays", None)


def _rv49_close_both_checks(page, mock, check, until):
    """× on a File pane holding an edit only it has, whose chat (a compose
    row: #47 review 2 judges it whatever the pane shows) holds unsent words:
    one question naming both, with Save / Don't save / Cancel."""
    pane = page.locator(_pane(2))
    page.locator(_pane(2, '[data-testid="tab-chat"]')).click()
    until(lambda: pane.get_attribute("data-view") == "chat", timeout=3)
    box = page.locator(_pane(2, '[data-testid="input"]'))
    until(lambda: box.count() == 1, timeout=3)
    box.fill("words-for-a-new-thread")
    page.locator(_pane(2, '[data-testid="tab-file"]')).click()
    until(lambda: pane.get_attribute("data-view") == "file", timeout=3)
    check("setup: calc.py open in pane 2", _open(page, until, 2, "calc.py"))
    check("setup: an edit only pane 2 holds", _type(page, until, 2, "\n#both"))
    time.sleep(0.5)  # past ×'s 400 ms guard after the last click
    page.locator('[data-testid="pane-2-close"]').click()
    ask = page.locator('[data-testid="pane-2-close-ask"]')
    until(lambda: ask.count() == 1, timeout=3)
    text = ask.inner_text() if ask.count() else ""
    check("× asks one question about both the unsaved edit and the unsent words",
          "calc.py has an unsaved edit" in text and "unsent message" in text
          and page.locator('[data-testid="pane-2-close-save"]').count() == 1, text)
    page.locator('[data-testid="pane-2-close-keep"]').click()
    until(lambda: ask.count() == 0, timeout=2)
    words = page.evaluate("window.__hud.state().chats[2]?.input ?? null")
    check("…and Cancel keeps the pane, the edit and the words",
          page.locator(_tab(2, "calc.py")).get_attribute("data-dirty") == "true"
          and page.evaluate("window.__hud.workspace().ws.order").count(2) == 1
          and words == "words-for-a-new-thread", f"box: {words!r}")


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
            editor_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until,
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
    print(f"all {v2.CHECKS} editor checks passed")
    return 0


def _checkpoint_crash_copy_checks(page, mock, check, until):
    """The owner can recover exact text after a crash and a refused etag save."""
    check("recovery: file opens", _open(page, until, 2, "calc.py"))
    check("recovery: retained edit", _type(page, until, 2, "\n#exact_copy_after_409"))
    original = mock.world["files"]["calc.py"]["content"]
    mock.world["files"]["calc.py"]["content"] = "#changed_on_disk\n"
    page.evaluate("""() => {
      window.__copyCalls=[]; window.__copyFail=false;
      Object.defineProperty(navigator,'clipboard',{configurable:true,value:{
        writeText: async text => { if(window.__copyFail) throw new Error('fixture denied'); window.__copyCalls.push(text); }
      }});
      window.__hudCrashProbe='workspace';
      window.__hud.dispatch({type:'patch',patch:{error:'crash copy probe'}});
    }""")
    until(lambda: page.locator('[data-testid="hud-crashed"]').count() == 1, timeout=4)
    page.evaluate("delete window.__hudCrashProbe")
    save = page.locator('[data-testid="hud-crashed-save"]')
    copy = page.locator('[data-testid="hud-crashed-copy"][data-path="calc.py"]')
    save.click()
    until(lambda: "could not be saved" in page.locator('[data-testid="hud-crashed-saved"]').inner_text(), timeout=4)
    check("recovery: 409 preserves the unsaved file and offers its named copy", copy.count() == 1 and "calc.py" in page.locator('[data-testid="hud-crashed"]').inner_text())
    _approval(mock, "crashcopy1")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    until(lambda: copy.is_disabled(), timeout=3)
    check("recovery: approval makes copy disabled and inert", copy.is_disabled() and copy.evaluate("e=>!!e.closest('[inert]')"))
    copy.evaluate("e=>e.click()")
    check("recovery: blocked click writes no clipboard", page.evaluate("__copyCalls.length") == 0)
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0 and not copy.is_disabled(), timeout=4)
    page.evaluate("window.__copyFail=true")
    copy.click()
    until(lambda: "Copy failed" in page.locator('[data-testid="hud-crashed-saved"]').inner_text(), timeout=4)
    check("recovery: denied clipboard does not claim success", page.evaluate("__copyCalls.length") == 0 and "Copied calc.py" not in page.locator('[data-testid="hud-crashed-saved"]').inner_text())
    page.evaluate("window.__copyFail=false")
    copy.click()
    until(lambda: "Copied calc.py" in page.locator('[data-testid="hud-crashed-saved"]').inner_text(), timeout=4)
    # Monaco inner_text is a visual line layout, not the model's exact bytes.
    # The refused PUT carried the same retained edit through a separate save path.
    expected = mock.sent("PUT", "/projects/p1/file")[-1]["content"]
    actual = page.evaluate("__copyCalls")
    check("recovery: clipboard gets exact retained text", actual == [expected] and original.strip() in expected and "#exact_copy_after_409" in expected, repr({"actual": actual, "expected": expected}))
    check("recovery: copy neither overwrites disk nor clears dirty state", mock.world["files"]["calc.py"]["content"] == "#changed_on_disk\n" and any(b["dirty"] for b in _buffers(page)))


if __name__ == "__main__":
    sys.exit(main())
