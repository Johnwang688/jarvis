"""Headless checks for the editor's navigation (ED-7, editor plan §2.8 and
§2.10): quick open, the command palette, find in files, the explorer's
new file, new folder, rename and delete, the status bar, and Add selection to
chat.

Free and hermetic: a `MockDaemon(0)` of its own and a browser context of its
own, under `hud_v2_check.guard` (no request to 8402/8403/8405, no CSP
violation). The real routes' refusals, owner-only and the search parity with
`grep_files` are `tests/v2/editor_nav_check.py`.

What it pins:
  - **keys**: Ctrl+P opens quick open and its default (Chrome's print) is
    prevented; Ctrl+Shift+P and F1 open the palette; Ctrl+Shift+F find in
    files; a Ctrl+K chord's second key stays Monaco's;
  - **quick open** lists dotfiles and env files (locked), never a withheld
    file or the gate's state; Space typed in it is a space; push-to-talk
    refuses while it is open; Enter opens the file; `file:line` puts the
    cursor there; Escape closes it;
  - **the palette** offers Monaco's actions when an editor had focus, has no
    download or VEX device entry, and runs a command;
  - **find in files** finds, never shows an env or withheld file, opens a hit
    at its line, and takes the selection;
  - **the explorer** makes a file (and opens it), renames a file with an
    unsaved edit (its tab and its edit follow, and Save writes the new name),
    deletes to the trash after asking, warns about an unsaved edit, and shows
    the daemon's refusal of an env file renamed to a readable name;
  - **the status bar** says Ln/Col, the language, the line endings and
    whether the buffer is saved;
  - **Add selection to chat** puts each selection in the selected chat's box,
    sends nothing, and does nothing for an env file;
  - **under a card** quick open, the palette and the explorer's menu close,
    no key opens one, the search is inert, and Escape denies the card.

Run: PYTHONPATH=. .venv/bin/python tests/face/hud_v2_editor_nav_check.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.face.hud_v2_editor_check import (  # noqa: E402
    WIDE, _buf, _fresh, _open, _tab, _text, _type,
)
from tests.face.hud_v2_layout_check import _approval, _boot, _pane  # noqa: E402

QO = '[data-testid="quickopen"]'
PAL = '[data-testid="palette"]'


def _sends(mock) -> int:
    return sum(1 for m, p, _ in mock.calls if m == "POST" and p.endswith("/send"))


def _rows(page, which: str) -> list[str]:
    return page.locator(f'[data-testid="{which}-row"]').evaluate_all("els => els.map(e => e.dataset.key)")


def _status(page, n: int, testid: str) -> str:
    loc = page.locator(_pane(n, f'[data-testid="{testid}"]'))
    return loc.first.inner_text() if loc.count() else ""


def _ctrl_p_probe(page):
    """Records whether the window prevented Ctrl+P's default (Chrome's print dialog)."""
    page.evaluate("""() => {
      window.__printPrevented = null;
      if (window.__printProbe) return;
      window.__printProbe = true;
      window.addEventListener('keydown', (e) => {
        if (e.ctrlKey && !e.shiftKey && (e.key === 'p' || e.key === 'P')) window.__printPrevented = e.defaultPrevented;
      }, true);
    }""")


def nav_checks(browser, mock, base, check, until, guard, init_script, only: str = ""):
    print("\neditor navigation: quick open, palette, find in files, explorer, status bar (ED-7)")
    ctx = browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]}, permissions=["microphone"])
    guard(ctx)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    keep = {k: dict(v) for k, v in mock.world["files"].items()}
    keep_tree = {k: [dict(e) for e in v] for k, v in mock.world["tree"].items()}
    keep_kinds = dict(mock.world["kinds"])
    try:
        _boot(page, mock, base, until)
        for section in (_quick_open_checks, _palette_checks, _search_checks, _explorer_checks,
                        _status_checks, _add_to_chat_checks, _card_checks):
            if only and only not in section.__name__:
                continue
            mock.world["tree"].clear()
            mock.world["tree"].update({k: [dict(e) for e in v] for k, v in keep_tree.items()})
            mock.world["kinds"].clear()
            mock.world["kinds"].update(keep_kinds)
            _fresh(page, mock, until, keep_files=keep)
            try:
                section(page, mock, check, until)
            except Exception as e:  # a section that cannot run is a failure; the rest still run
                check(f"{section.__name__.strip('_')} ran to the end", False, str(e).splitlines()[0][:200])
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        check("no page errors in the navigation section", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()
        mock.world["files"].clear()
        mock.world["files"].update(keep)
        mock.world["tree"].clear()
        mock.world["tree"].update(keep_tree)
        mock.world["kinds"].clear()
        mock.world["kinds"].update(keep_kinds)


def _quick_open(page, until) -> bool:
    page.keyboard.press("Control+p")
    return bool(until(lambda: page.locator(QO).count() == 1, timeout=3))


def _quick_open_checks(page, mock, check, until):
    check("setup: calc.py opens in pane 2", _open(page, until, 2, "calc.py"))
    _ctrl_p_probe(page)
    page.locator(_pane(2, '[data-testid="editor"] .view-lines')).click()
    check("Ctrl+P in the editor opens quick open", _quick_open(page, until))
    check("and its default — Chrome's print dialog — is prevented", page.evaluate("window.__printPrevented") is True)
    until(lambda: len(_rows(page, "quickopen")) > 0, timeout=3)
    rows = _rows(page, "quickopen")
    check("it lists dotfiles and env files", ".env.production" in rows and ".env" in rows, str(rows))
    check("never a withheld file or the gate's state",
          "google_token.json" not in rows and "allowlist.json" not in rows, str(rows))
    lock = page.locator(f'{QO} [data-key=".env.production"] .qlock')
    check("an env file carries the lock", lock.count() == 1)
    box = page.locator('[data-testid="quickopen-input"]')
    check("its box has focus", box.evaluate("e => e === document.activeElement"))
    page.keyboard.type("src ma")
    check("Space typed in it is a space", box.input_value() == "src ma", repr(box.input_value()))
    page.keyboard.down("Control")
    page.keyboard.down("d")
    time.sleep(0.15)
    held = page.evaluate("window.__hud.capture.ptt")
    page.keyboard.up("d")
    page.keyboard.up("Control")
    check("push-to-talk refuses while it is open", held in (False, None), str(held))
    until(lambda: (_rows(page, "quickopen") or [""])[0] == "src/main.py", timeout=2)
    check("fuzzy: 'src ma' ranks src/main.py first", _rows(page, "quickopen")[:1] == ["src/main.py"],
          str(_rows(page, "quickopen")[:3]))
    page.keyboard.press("Enter")
    path = page.locator(_pane(2, '[data-testid="file-path"]'))
    check("Enter opens it in the File pane", bool(until(lambda: path.inner_text() == "src/main.py", timeout=4))
          and page.locator(QO).count() == 0)
    check("as a pinned tab", page.locator(_tab(2, "src/main.py")).get_attribute("data-preview") != "true")
    # file:line
    _quick_open(page, until)
    page.keyboard.type("calc.py:2")
    until(lambda: "calc.py" in _rows(page, "quickopen"), timeout=2)
    page.keyboard.press("Enter")
    until(lambda: path.inner_text() == "calc.py", timeout=4)
    check("`calc.py:2` puts the cursor on line 2",
          bool(until(lambda: _status(page, 2, "status-pos").startswith("Ln 2,"), timeout=4)),
          _status(page, 2, "status-pos"))
    # :line alone, in the file open
    _quick_open(page, until)
    page.keyboard.type(":1")
    until(lambda: ":line" in _rows(page, "quickopen"), timeout=2)
    page.keyboard.press("Enter")
    check("`:1` alone goes to line 1 of the file open",
          bool(until(lambda: _status(page, 2, "status-pos").startswith("Ln 1,"), timeout=4)))
    _quick_open(page, until)
    page.keyboard.press("Escape")
    check("Escape closes it", bool(until(lambda: page.locator(QO).count() == 0, timeout=2)))
    # A pending Ctrl+K chord's second key is Monaco's: Ctrl+K Ctrl+P opens nothing.
    page.locator(_pane(2, '[data-testid="editor"] .view-lines')).click()
    page.keyboard.press("Control+k")
    page.keyboard.press("Control+p")
    time.sleep(0.25)
    check("Ctrl+K Ctrl+P in the editor is Monaco's chord, not quick open", page.locator(QO).count() == 0)
    page.keyboard.press("Escape")


def _palette_checks(page, mock, check, until):
    check("setup: calc.py opens in pane 2", _open(page, until, 2, "calc.py"))
    page.locator(_pane(2, '[data-testid="editor"] .view-lines')).click()
    page.keyboard.press("Control+Home")
    page.keyboard.press("Control+Shift+P")
    check("Ctrl+Shift+P opens the palette", bool(until(lambda: page.locator(PAL).count() == 1, timeout=3)))
    rows = _rows(page, "palette")
    check("with Monaco's actions when an editor had focus",
          any(r.startswith("monaco:editor.action.commentLine") for r in rows), str(rows[:6]))
    labels = page.locator('[data-testid="palette-row"]').evaluate_all("els => els.map(e => e.innerText)")
    check("no download or VEX device entry",
          not [x for x in labels if any(w in x.lower() for w in ("download", "device", "brain", "vexcom", "robot"))],
          str([x for x in labels if "download" in x.lower()]))
    check("Monaco's own palette is not offered inside ours",
          not any(r == "monaco:editor.action.quickCommand" for r in rows))
    page.keyboard.type("toggle line comment")
    box = page.locator('[data-testid="palette-input"]')
    check("Space typed in it is a space", box.input_value() == "toggle line comment")
    until(lambda: (_rows(page, "palette") or [""])[0] == "monaco:editor.action.commentLine", timeout=2)
    page.keyboard.press("Enter")
    check("a Monaco action runs in the editor it was opened from",
          bool(until(lambda: _text(page, 2).lstrip().startswith("#"), timeout=3)), _text(page, 2)[:40])
    check("closed after", page.locator(PAL).count() == 0)
    page.keyboard.press("F1")
    check("F1 opens the palette too", bool(until(lambda: page.locator(PAL).count() == 1, timeout=3)))
    page.keyboard.type("toggle sidebar")
    until(lambda: (_rows(page, "palette") or [""])[0] == "hud:view.toggleSidebar", timeout=2)
    was = page.locator('[data-testid="rail-left"]').count()
    page.keyboard.press("Enter")
    check("a HUD command runs (Toggle Sidebar)",
          bool(until(lambda: page.locator('[data-testid="rail-left"]').count() != was, timeout=2)))
    page.keyboard.press("Control+b")


def _search_checks(page, mock, check, until):
    check("setup: calc.py opens in pane 2", _open(page, until, 2, "calc.py"))
    page.locator(_pane(2, '[data-testid="editor"] .view-lines')).click()
    page.keyboard.press("Control+Home")
    page.keyboard.press("Control+Shift+ArrowRight")  # selects "def"
    page.keyboard.press("Control+Shift+F")
    box = page.locator(_pane(2, '[data-testid="search-input"]'))
    check("Ctrl+Shift+F shows find in files", bool(until(lambda: box.count() == 1 and box.is_visible(), timeout=3)))
    check("its box has focus and takes the selection",
          bool(until(lambda: box.evaluate("e => e === document.activeElement") and box.input_value() == "def",
                     timeout=2)), repr(box.input_value()))
    box.fill("return")
    hit = page.locator(_pane(2, '[data-testid="search-hit-calc.py:2"]'))
    check("it finds", bool(until(lambda: hit.count() == 1, timeout=4)))
    check("and highlights the match", hit.locator("mark").first.inner_text() == "return")
    sent = mock.sent("POST", "/projects/p1/search")
    check("through the project's search route", bool(sent) and sent[-1].get("query") == "return"
          and sent[-1].get("ignore") is True, str(sent[-1:]))
    _open(page, until, 2, "notes.txt", double=True)
    page.locator(_pane(2, '[data-testid="side-search"]')).click()
    hit.click()
    path = page.locator(_pane(2, '[data-testid="file-path"]'))
    check("a hit opens its file at its line",
          bool(until(lambda: path.inner_text() == "calc.py" and _status(page, 2, "status-pos").startswith("Ln 2,"),
                     timeout=4)), _status(page, 2, "status-pos"))
    page.locator(_pane(2, '[data-testid="side-search"]')).click()
    box.fill("KEY")
    until(lambda: "match" in page.locator(_pane(2, '[data-testid="search-note"]')).inner_text(), timeout=4)
    shown = page.locator(_pane(2, '[data-testid="search-results"]')).inner_text()
    check("an env file or a withheld one is never shown", ".env" not in shown and "google_token" not in shown
          and "fake" not in shown, shown[:200])
    page.locator(_pane(2, '[data-testid="search-ignore"]')).click()
    until(lambda: len(mock.sent("POST", "/projects/p1/search")) and
          mock.sent("POST", "/projects/p1/search")[-1].get("ignore") is False, timeout=3)
    check("the .gitignore toggle is sent", mock.sent("POST", "/projects/p1/search")[-1].get("ignore") is False)


def _explorer_checks(page, mock, check, until):
    check("setup: calc.py opens in pane 2", _open(page, until, 2, "calc.py", double=True))
    # New file, from the folder's menu.
    page.locator(_pane(2, '[data-testid="dir-src"]')).click(button="right")
    menu = page.locator('[data-testid="explorer-menu"]')
    check("right-click on a folder opens the explorer's menu", bool(until(lambda: menu.count() == 1, timeout=2)))
    page.locator('[data-testid="menu-new-file"]').click()
    inp = page.locator(_pane(2, '[data-testid="fs-input"]'))
    until(lambda: inp.count() == 1, timeout=2)
    inp.type("new thing.cpp")
    check("Space in the name box is a space", inp.input_value() == "new thing.cpp")
    page.keyboard.press("Enter")
    check("New File makes it through the fs route",
          bool(until(lambda: "src/new thing.cpp" in mock.world["files"], timeout=3)),
          str(mock.sent("POST", "/projects/p1/fs")[-1:]))
    path = page.locator(_pane(2, '[data-testid="file-path"]'))
    check("and opens it", bool(until(lambda: path.inner_text() == "src/new thing.cpp", timeout=4)))
    # Rename a file with an unsaved edit: the tab and the edit follow.
    page.locator(_tab(2, "calc.py")).click()
    until(lambda: path.inner_text() == "calc.py", timeout=3)
    check("setup: an unsaved edit in calc.py", _type(page, until, 2, "\n#follows_the_rename"))
    page.locator(_pane(2, '[data-testid="file-calc.py"]')).click(button="right")
    until(lambda: menu.count() == 1, timeout=2)
    page.locator('[data-testid="menu-rename"]').click()
    until(lambda: inp.count() == 1, timeout=2)
    check("Rename offers the path, the name's stem selected",
          inp.input_value() == "calc.py" and inp.evaluate("e => [e.selectionStart, e.selectionEnd]") == [0, 4])
    page.keyboard.type("calc2")
    page.keyboard.press("Enter")
    tab = page.locator(_tab(2, "calc2.py"))
    check("the tab follows the rename", bool(until(lambda: tab.count() == 1, timeout=4))
          and page.locator(_tab(2, "calc.py")).count() == 0)
    buf = _buf(page, "calc2.py") or {}
    check("and keeps its unsaved edit", buf.get("dirty") is True and "#follows_the_rename" in _text(page, 2), str(buf))
    check("the file moved on disk", "calc2.py" in mock.world["files"] and "calc.py" not in mock.world["files"])
    page.locator(_pane(2, '[data-testid="file-save"]')).click()
    check("Save writes the new name", bool(until(
        lambda: "#follows_the_rename" in mock.world["files"].get("calc2.py", {}).get("content", ""), timeout=4))
        and "calc.py" not in mock.world["files"])
    # Delete: asks, warns about an unsaved edit, moves to the trash.
    _open(page, until, 2, "notes.txt", double=True)
    check("setup: an unsaved edit in notes.txt", _type(page, until, 2, "x"))
    page.locator(_pane(2, '[data-testid="file-notes.txt"]')).click(button="right")
    until(lambda: menu.count() == 1, timeout=2)
    page.locator('[data-testid="menu-delete"]').click()
    prompt = page.locator(_pane(2, '[data-testid="fs-prompt"]'))
    check("Delete asks first", bool(until(lambda: prompt.count() == 1, timeout=2))
          and "notes.txt" in mock.world["files"])
    check("and says the unsaved edit stays", page.locator(_pane(2, '[data-testid="fs-dirty"]')).count() == 1)
    page.locator(_pane(2, '[data-testid="fs-ok"]')).click()
    check("a delete goes to the trash", bool(until(lambda: "notes.txt" in mock.trashed, timeout=3))
          and "notes.txt" not in mock.world["files"])
    check("its row leaves the tree", bool(until(
        lambda: page.locator(_pane(2, '[data-testid="file-notes.txt"]')).count() == 0, timeout=3)))
    check("the edit is still in its buffer", (_buf(page, "notes.txt") or {}).get("dirty") is True)
    # Review of PR #53, MED 2: notes.txt is gone from disk with its edit kept;
    # renaming another file onto its name is refused before the disk changes.
    _open(page, until, 2, "README.md", double=True)
    page.locator(_pane(2, '[data-testid="file-README.md"]')).click(button="right")
    until(lambda: menu.count() == 1, timeout=2)
    page.locator('[data-testid="menu-rename"]').click()
    until(lambda: inp.count() == 1, timeout=2)
    inp.fill("notes.txt")
    renames = len(mock.sent("POST", "/projects/p1/fs"))
    page.keyboard.press("Enter")
    err = page.locator(_pane(2, '[data-testid="fs-error"]'))
    check("a rename onto an open unsaved edit is refused, and said",
          bool(until(lambda: err.count() == 1 and "unsaved edit" in err.inner_text(), timeout=3))
          and len(mock.sent("POST", "/projects/p1/fs")) == renames and "README.md" in mock.world["files"])
    check("and the edit is still there", (_buf(page, "notes.txt") or {}).get("dirty") is True)
    page.keyboard.press("Escape")
    # The daemon's refusal is said.
    page.locator(_pane(2, '[data-testid="file-.env.production"]')).click(button="right")
    until(lambda: menu.count() == 1, timeout=2)
    page.locator('[data-testid="menu-rename"]').click()
    until(lambda: inp.count() == 1, timeout=2)
    inp.fill("plain.txt")
    page.keyboard.press("Enter")
    err = page.locator(_pane(2, '[data-testid="fs-error"]'))
    check("an env file renamed to a readable name is refused, and said",
          bool(until(lambda: err.count() == 1, timeout=3)) and ".env.production" in mock.world["files"])
    page.keyboard.press("Escape")
    check("Escape closes the prompt", bool(until(lambda: prompt.count() == 0, timeout=2)))
    # The menu closes on Escape.
    page.locator(_pane(2, '[data-testid="file-README.md"]')).click(button="right")
    until(lambda: menu.count() == 1, timeout=2)
    page.keyboard.press("Escape")
    check("the menu closes on Escape", bool(until(lambda: menu.count() == 0, timeout=2)))


def _status_checks(page, mock, check, until):
    check("setup: notes.txt opens in pane 2", _open(page, until, 2, "notes.txt"))
    check("the status bar says the line endings (mixed)", "mixed" in _status(page, 2, "editor-eol"))
    check("and the language", _status(page, 2, "status-lang") == "Plain Text", _status(page, 2, "status-lang"))
    check("and that it is saved", _status(page, 2, "status-state") == "saved")
    page.locator(_pane(2, '[data-testid="editor"] .view-lines')).click()
    page.keyboard.press("Control+Home")
    check("Ln 1, Col 1 at the start", bool(until(lambda: _status(page, 2, "status-pos").startswith("Ln 1, Col 1"),
                                                  timeout=3)), _status(page, 2, "status-pos"))
    page.keyboard.press("ArrowDown")
    page.keyboard.press("End")
    check("the cursor moves it", bool(until(lambda: _status(page, 2, "status-pos").startswith("Ln 2, Col 2"),
                                            timeout=3)), _status(page, 2, "status-pos"))
    page.keyboard.press("Shift+Home")
    check("a selection is counted", bool(until(lambda: "(1 selected)" in _status(page, 2, "status-pos"), timeout=3)))
    page.keyboard.press("End")
    page.keyboard.type("z")
    check("an edit reads unsaved", bool(until(lambda: _status(page, 2, "status-state") == "● unsaved", timeout=3)))
    check("the indentation is said", _status(page, 2, "status-indent").startswith(("Spaces", "Tab")))
    # The registry's save state: a slow write out, then a second Ctrl+S queued behind it.
    mock.world["put_delays"] = [2.0]
    try:
        page.keyboard.press("Control+s")
        saving = page.locator(_pane(2, '[data-testid="editor-saving"]'))
        check("a slow save says saving…", bool(until(lambda: saving.count() == 1, timeout=1.5)))
        page.keyboard.type("q")
        page.keyboard.press("Control+s")
        check("a second Ctrl+S meanwhile says it is queued",
              bool(until(lambda: saving.count() == 1 and saving.get_attribute("data-queued") == "true", timeout=1.5)))
        check("and both end saved", bool(until(lambda: saving.count() == 0
                                               and _status(page, 2, "status-state") == "saved", timeout=8)))
    finally:
        mock.world.pop("put_delays", None)


def _add_to_chat_checks(page, mock, check, until):
    check("setup: calc.py open in pane 2", _open(page, until, 2, "calc.py"))
    page.locator(_pane(1, '[data-testid="input"]')).click()  # pane 1's chat is the selected one
    page.locator(_pane(2, '[data-testid="editor"] .view-lines')).click()
    page.keyboard.press("Control+Home")
    page.keyboard.press("Shift+ArrowDown")
    page.keyboard.press("Shift+End")
    sends = _sends(mock)
    page.keyboard.press("Control+Shift+P")
    until(lambda: page.locator(PAL).count() == 1, timeout=3)
    page.keyboard.type("add selection to chat")
    until(lambda: (_rows(page, "palette") or [""])[0] == "hud:chat.addSelection", timeout=2)
    page.keyboard.press("Enter")
    box = page.locator(_pane(1, '[data-testid="input"]'))
    until(lambda: "calc.py:L1-2" in box.input_value(), timeout=3)
    value = box.input_value()
    check("Add selection to chat puts the lines, with their place, in the selected chat's box",
          value.startswith("calc.py:L1-2\n```\ndef add(a, b):\n    return a + b\n```"), repr(value))
    check("and sends nothing", _sends(mock) == sends)
    box.fill("")
    # Two cursors: two excerpts.
    page.locator(_pane(2, '[data-testid="editor"] .view-lines')).click()
    page.keyboard.press("Control+Home")
    # A second cursor on line 2: Alt+click (Monaco's multi-cursor modifier).
    page.keyboard.down("Alt")
    page.locator(_pane(2, '[data-testid="editor"] .view-line')).nth(1).click(position={"x": 4, "y": 4})
    page.keyboard.up("Alt")
    page.locator(_pane(2, '[data-testid="add-to-chat"]')).click()
    until(lambda: "calc.py:L2" in box.input_value(), timeout=3)
    value = box.input_value()
    check("several cursors give several excerpts", value.count("```") == 4 and "calc.py:L1\n" in value
          and "calc.py:L2\n" in value, repr(value))
    box.fill("")
    # An env file: nothing.
    _open(page, until, 2, ".env.production")
    page.locator(_pane(2, '[data-testid="editor"] .view-lines')).click()
    page.keyboard.press("Control+a")
    page.keyboard.press("Control+Shift+P")
    until(lambda: page.locator(PAL).count() == 1, timeout=3)
    page.keyboard.type("add selection to chat")
    until(lambda: (_rows(page, "palette") or [""])[0] == "hud:chat.addSelection", timeout=2)
    page.keyboard.press("Enter")
    time.sleep(0.3)
    check("an env file's text never goes to the box", box.input_value() == "" and _sends(mock) == sends,
          repr(box.input_value()))


def _card_checks(page, mock, check, until):
    check("setup: calc.py opens in pane 2", _open(page, until, 2, "calc.py"))
    _quick_open(page, until)
    _approval(mock, "nav1")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    check("a card closes quick open", bool(until(lambda: page.locator(QO).count() == 0, timeout=2)))
    for key in ("Control+p", "Control+Shift+P", "F1", "Control+Shift+F"):
        page.keyboard.press(key)
    time.sleep(0.25)
    check("under a card no key opens quick open, the palette or the search",
          page.locator(QO).count() == 0 and page.locator(PAL).count() == 0
          and not page.locator(_pane(2, '[data-testid="searchview"]')).is_visible())
    inert = page.evaluate("""() => {
      const el = document.querySelector('[data-testid="pane-2"] [data-testid="side-search"]');
      return !!el && !!el.closest('[inert]');
    }""")
    check("the explorer and the search are behind the inert veil", inert)
    page.keyboard.press("Escape")
    body = until(lambda: mock.sent("POST", "/approvals/nav1") or None, timeout=4)
    check("Escape denies the card", bool(body) and body[-1].get("decision") == "deny")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    # The palette and the explorer's menu close too.
    page.locator(_pane(2, '[data-testid="editor"] .view-lines')).click()
    page.keyboard.press("Control+Shift+P")
    until(lambda: page.locator(PAL).count() == 1, timeout=3)
    _approval(mock, "nav2")
    check("a card closes the palette", bool(until(lambda: page.locator(PAL).count() == 0, timeout=3)))
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    page.locator(_pane(2, '[data-testid="file-notes.txt"]')).click(button="right")
    until(lambda: page.locator('[data-testid="explorer-menu"]').count() == 1, timeout=2)
    _approval(mock, "nav3")
    check("a card closes the explorer's menu",
          bool(until(lambda: page.locator('[data-testid="explorer-menu"]').count() == 0, timeout=3)))
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)


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
            # `ONLY=card` runs one section (a mutation round's quick way in).
            import os
            nav_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until,
                       v2.guard, v2.FAKE_RECOGNIZER, os.environ.get("ONLY", ""))
            browser.close()
    finally:
        mock.stop()
    print()
    if v2.FAILURES:
        print(f"{len(v2.FAILURES)} of {v2.CHECKS} checks FAILED:")
        for f in v2.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {v2.CHECKS} editor navigation checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
