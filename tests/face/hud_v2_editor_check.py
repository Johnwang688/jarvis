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
        page.set_viewport_size({"width": 1600, "height": 900})
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
    # A narrow pane folds its tree a moment after a file opens: let any such
    # moment pass, then unfold it with "Files" if it is folded.
    time.sleep(0.45)
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
    ctx = browser.new_context(viewport={"width": 1600, "height": 900}, permissions=["microphone"])
    guard(ctx)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    keep = {k: dict(v) for k, v in mock.world["files"].items()}
    try:
        _boot(page, mock, base, until)
        for section in (_tab_checks, _shared_checks, _reload_in_place_checks, _conflict_checks,
                        _gone_checks, _persist_checks, _env_checks, _kinds_checks, _add_to_chat_checks,
                        _card_checks):
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
    _show(page, until, 3, "preview")
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
    mock.emit("tool_started", {"call_id": "w1", "name": "Write", "args": {"file_path": "calc.py"}}, thread_id="t1")
    mock.disk_write("calc.py", "def add(a, b):\n    return a + b\n\n# an agent added this\n")
    until(lambda: "an agent added this" in _text(page, 2), timeout=8)
    check("a clean file an agent rewrote reloads in place", "an agent added this" in _text(page, 2))
    note = page.locator(_pane(2, '[data-testid="file-note"]'))
    until(lambda: note.count() > 0 and "changed on disk" in note.inner_text(), timeout=2)
    check("with a toast that names the writer the activity feed saw",
          note.count() > 0 and "changed on disk by Jarvis · desk chat" in note.inner_text(),
          note.inner_text() if note.count() else "no note")
    check("and it is still clean, at the disk's etag",
          page.locator(_tab(2, "calc.py")).get_attribute("data-dirty") == "false"
          and (_buf(page, "calc.py") or {}).get("etag") == etag_of(mock.world["files"]["calc.py"]["content"]))
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
    # A word from the env file is never suggested while typing in calc.py.
    _open(page, until, 3, "calc.py")
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
    before = _text(page, 2)
    page.locator(_lines(2)).click()
    page.keyboard.type("x")
    time.sleep(0.2)
    check("typing changes nothing and Save stays off",
          _text(page, 2) == before and page.locator(_pane(2, '[data-testid="file-save"]')).is_disabled(),
          repr(_text(page, 2)))
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
                          lambda ctx: v2.guard_live(ctx, v2.FAILURES.append), v2.FAKE_RECOGNIZER)
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


if __name__ == "__main__":
    sys.exit(main())
