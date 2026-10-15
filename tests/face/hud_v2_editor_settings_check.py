"""Headless checks for the editor settings, `.editorconfig` and vim (ED-8,
editor plan §2.8; decisions E-9, E-11).

Free and hermetic: a `MockDaemon(0)` of its own and a browser context of its
own, under `hud_v2_check.guard` (no request to 8402/8403/8405, no CSP
violation). The pure rules — every field's fallback, the font check, the
EditorConfig parser and globs — are vitest's (`editorSettings.test.ts`,
`editorconfig.test.ts`).

What it pins:
  - **settings apply to an open editor at once and survive a reload**: the
    File pane's ⚙ opens the dialog, a size, a cursor and the minimap change
    the editor showing calc.py in the same breath, a safe font list is taken
    and an unsafe one is refused with a sentence; Markdown wraps; the palette
    opens the same dialog; after a reload the editor carries them again;
  - **garbage in storage falls back field by field**: an out-of-range size,
    a string minimap and a font with `;}` each take their default while a good
    cursor style is kept, and a store that is not JSON is all defaults;
  - **`.editorconfig`**: read through the editor's own routes (one stat of
    each folder up to the project's root, never above), it makes a `*.c`
    file tab-indented although its content is spaces, gives a file with no
    line break CRLF for its new lines, survives a settings change, and **a
    save after it changes nothing else** — no indentation converted, no line
    ending rewritten, trailing whitespace and a missing final newline kept
    although the file asks for both;
  - **under a card** the dialog closes, the ⚙ is disabled behind the inert
    veil, and Escape denies the card;
  - **vim** (E-11): on, its status line shows the mode; `i`, typing, Escape
    and `:w` save **that pane's buffer** through the guarded save (with its
    etag) and no other pane's; Ctrl+D stays push-to-talk's (the owner's
    decision, 2026-10-12): vim never sees it; off again, keys type.

Run: PYTHONPATH=. .venv/bin/python tests/face/hud_v2_editor_settings_check.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.face.hud_v2_editor_check import (  # noqa: E402
    WIDE, _buf, _fresh, _lines, _open, _tab, _text,
)
from tests.face.hud_v2_layout_check import _approval, _boot, _pane  # noqa: E402

DIALOG = '[data-testid="editor-settings-dialog"]'
KEY = "jarvis.hud.editor"

EDITORCONFIG = """\
# the mock project's .editorconfig (ED-8)
root = true

[*]
indent_style = space
indent_size = 2

[*.c]
indent_style = tab
indent_size = 4
end_of_line = crlf
trim_trailing_whitespace = true
insert_final_newline = true

[src/**.py]
indent_size = 3
"""
# Spaces in its content, trailing whitespace, no final newline: what a
# rewriting save would change.
TABBED = "int main(void) {\n    return 0;   \n}"

# Long tasks, as the browser reports them (the Long Tasks API: any task over
# 50 ms), from the first script on: what the owner would feel as a frozen window.
LONGTASK_PROBE = """
if (!window.__longTasks) {
  window.__longTasks = [];
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) window.__longTasks.push({ start: e.startTime, ms: e.duration });
    }).observe({ type: 'longtask', buffered: true });
  } catch (e) { window.__longTasks = null; }
}
"""
DEEP_DIRS = [f"d{i}" for i in range(1, 32)]  # 31 folders below the root: 32 configs on every walk


def _heavy_glob(tag: str) -> str:
    """The heaviest glob the editor's caps allow: 64 alternatives (`{a,b}` six
    times) of at most 64 tokens each — `**/`, six letters, `*x` k times, `y`
    and the tag — so 64 × 64 = the 4,096-token total exactly."""
    k = (64 - 1 - 6 - 1 - len(tag)) // 2
    return f"{'{a,b}' * 6}{'*x' * k}y{tag}"


def _heavy_config(salt: str) -> str:
    """123 sections as heavy as the editor's caps allow (the review's shape)."""
    rows = [f"[{_heavy_glob(f'{salt}_{n}')}]\nindent_size = 2" for n in range(123)]
    return "\n".join(rows) + "\n"


def _probe(page) -> dict:
    return page.evaluate("window.__hud.editorSettings()")


def _editor(page, name: str) -> dict | None:
    for e in _probe(page)["editors"]:
        if (e.get("uri") or "").endswith("/" + name):
            return e
    return None


def _puts(mock, path: str) -> list[dict]:
    return [b for m, p, b in mock.calls if m == "PUT" and p.endswith("/file") and b.get("path") == path]


def _stats(mock) -> list[list[str]]:
    return [b.get("paths") for m, p, b in mock.calls if m == "POST" and p.endswith("/files/stat")]


def _store(page, value):
    page.evaluate("([k, v]) => v === null ? localStorage.removeItem(k) : localStorage.setItem(k, v)", [KEY, value])


def _open_dialog(page, until, n: int = 2) -> bool:
    page.locator(_pane(n, '[data-testid="editor-settings"]')).click()
    return bool(until(lambda: page.locator(DIALOG).count() == 1, timeout=3))


def settings_checks(browser, mock, base, check, until, guard, init_script, only: str = ""):
    print("\neditor settings, .editorconfig and vim (ED-8)")
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
        for section in (_apply_checks, _garbage_checks, _editorconfig_checks, _card_checks, _vim_checks,
                        _vim_review_checks, _long_task_checks):
            if only and only not in section.__name__:
                continue
            _store(page, None)
            _fresh(page, mock, until, keep_files=keep)
            try:
                section(page, mock, check, until)
            except Exception as e:  # a section that cannot run is a failure; the rest still run
                check(f"{section.__name__.strip('_')} ran to the end", False, str(e).splitlines()[0][:200])
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        check("no page errors in the editor settings section", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()
        mock.world["files"].clear()
        mock.world["files"].update(keep)


# ---------------------------------------------------------------------------

def _apply_checks(page, mock, check, until):
    check("setup: calc.py opens in pane 2", _open(page, until, 2, "calc.py"))
    e = _editor(page, "calc.py")
    check("with nothing stored the editor draws the defaults (size 12, minimap off, no wrap)",
          bool(e) and e["fontSize"] == 12 and e["minimap"] is False and e["wordWrap"] == "off"
          and e["cursorStyle"] == "line", str(e))
    check("the File pane's ⚙ opens the editor settings", _open_dialog(page, until))
    focused = page.evaluate("document.activeElement && document.activeElement.dataset.testid")
    check("the dialog takes focus (its first field)", focused == "es-font-family", str(focused))
    page.locator('[data-testid="es-font-size"]').select_option("16")
    check("a size applies to the open editor at once", (_editor(page, "calc.py") or {}).get("fontSize") == 16,
          str(_editor(page, "calc.py")))
    page.locator('[data-testid="es-cursor"]').select_option("block")
    page.locator('[data-testid="es-minimap"]').check()
    e = _editor(page, "calc.py") or {}
    check("the cursor style and the minimap too", e.get("cursorStyle") == "block" and e.get("minimap") is True, str(e))
    font = page.locator('[data-testid="es-font-family"]')
    font.fill('"Courier New", monospace')
    check("a safe font list is taken", (_editor(page, "calc.py") or {}).get("fontFamily") == '"Courier New", monospace',
          str(_editor(page, "calc.py")))
    font.fill("x;}body{background:url(//e.example)}")
    check("an unsafe one is refused with a sentence, and the editor keeps the last good font",
          page.locator('[data-testid="es-font-error"]').count() == 1
          and (_editor(page, "calc.py") or {}).get("fontFamily") == '"Courier New", monospace')
    stored = json.loads(page.evaluate("k => localStorage.getItem(k)", KEY) or "{}")
    check("what is stored is the good values only",
          stored.get("fontSize") == 16 and stored.get("cursorStyle") == "block" and stored.get("minimap") is True
          and stored.get("fontFamily") == '"Courier New", monospace', str(stored))
    page.keyboard.press("Escape")
    check("Escape closes it", bool(until(lambda: page.locator(DIALOG).count() == 0, timeout=2)))
    # Markdown wraps (its own setting, on).
    _open(page, until, 3, "README.md")
    md = _editor(page, "README.md") or {}
    check("Markdown is word-wrapped, code is not",
          md.get("wordWrap") == "on" and (_editor(page, "calc.py") or {}).get("wordWrap") == "off", str(md))
    # The palette opens the same dialog.
    page.locator(_lines(2)).click()
    page.keyboard.press("Control+Shift+P")
    until(lambda: page.locator('[data-testid="palette"]').count() == 1, timeout=3)
    page.keyboard.type("Editor Settings")
    until(lambda: page.locator('[data-testid="palette-row"][data-key="hud:view.editorSettings"]').count() == 1, timeout=2)
    page.keyboard.press("Enter")
    check("the palette's Editor Settings… opens it", bool(until(lambda: page.locator(DIALOG).count() == 1, timeout=3)))
    page.locator('[data-testid="es-close"]').click()
    until(lambda: page.locator(DIALOG).count() == 0, timeout=2)
    # A reload: the tabs come back, and the settings with them.
    _boot(page, mock, None, until, reload=True)
    until(lambda: _editor(page, "calc.py") is not None, timeout=10)
    e = _editor(page, "calc.py") or {}
    check("after a reload the editor carries them again",
          e.get("fontSize") == 16 and e.get("cursorStyle") == "block" and e.get("minimap") is True
          and e.get("fontFamily") == '"Courier New", monospace', str(e))
    _open_dialog(page, until)
    page.locator('[data-testid="es-reset"]').click()
    e = _editor(page, "calc.py") or {}
    check("Reset to defaults puts them back (the font to Monaco's own)",
          e.get("fontSize") == 12 and e.get("minimap") is False and e.get("fontFamily") is None
          and page.locator('[data-testid="es-font-family"]').input_value() == "", str(e))
    page.keyboard.press("Escape")


def _garbage_checks(page, mock, check, until):
    _store(page, json.dumps({"fontSize": "huge", "tabSize": 400, "minimap": "yes", "cursorStyle": "block",
                             "fontFamily": "x;}body{color:red}", "wordWrap": ["on"], "vim": 1}))
    _boot(page, mock, None, until, reload=True)
    check("setup: calc.py opens", _open(page, until, 2, "calc.py"))
    p = _probe(page)
    s = p["settings"]
    check("garbage in a field falls back to that field's default, the good ones kept",
          s["fontSize"] == 12 and s["tabSize"] == 4 and s["minimap"] is False and s["fontFamily"] == ""
          and s["wordWrap"] == "off" and s["vim"] is False and s["cursorStyle"] == "block", str(s))
    e = _editor(page, "calc.py") or {}
    check("and the editor draws that", e.get("fontSize") == 12 and e.get("fontFamily") is None
          and e.get("cursorStyle") == "block", str(e))
    _store(page, "not json{")
    _boot(page, mock, None, until, reload=True)
    _open(page, until, 2, "calc.py")
    s = _probe(page)["settings"]
    check("a store that is not JSON is all defaults", s["fontSize"] == 12 and s["cursorStyle"] == "line", str(s))


def _editorconfig_checks(page, mock, check, until):
    w = mock.world
    w["files"][".editorconfig"] = {"content": EDITORCONFIG, "mtime": 1757900400}
    w["files"]["tabbed.c"] = {"content": TABBED, "mtime": 1757900401}
    w["files"]["new.c"] = {"content": "", "mtime": 1757900402}
    tree = w["tree"][""]
    added = [{"name": n, "kind": "file", "size": 1, "mtime": 1757900400} for n in (".editorconfig", "tabbed.c", "new.c")]
    tree.extend(added)
    try:
        page.reload()
        _boot(page, mock, None, until, reload=True)
        before = len(mock.calls)
        check("setup: tabbed.c opens", _open(page, until, 2, "tabbed.c"))
        e = _editor(page, "tabbed.c") or {}
        model = e.get("model") or {}
        check(".editorconfig makes a *.c file tab-indented though its content is spaces",
              model.get("insertSpaces") is False and model.get("tabSize") == 4, str(model))
        stats = [p for p in _stats(mock)[0:] if p and any(x.endswith(".editorconfig") for x in p)]
        check("it was looked for through the editor's stat route, at the project's root and never above",
              bool(stats) and all(x == ".editorconfig" for p in stats for x in p if x.endswith(".editorconfig")
                                  and "/" not in x) and not any(".." in x for p in stats for x in p), str(stats[:3]))
        reads = [b for m, p, b in mock.calls[before:] if m == "GET" and p.endswith("/file")
                 and b.get("path") == ".editorconfig"]
        check("and read through the editor's own (owner-only) file route", len(reads) >= 1, str(reads))
        check("the buffer opens clean", (_buf(page, "tabbed.c") or {}).get("dirty") is False)
        # A save after it changes nothing else.
        page.locator(_lines(2)).click()
        page.keyboard.press("Control+Home")
        page.keyboard.press("Tab")
        until(lambda: page.locator(_tab(2, "tabbed.c")).get_attribute("data-dirty") == "true", timeout=2)
        page.keyboard.press("Control+s")
        put = until(lambda: _puts(mock, "tabbed.c") or None, timeout=4)
        got = put[-1]["content"] if put else None
        check("a save after it changes nothing else: the Tab typed is a tab, and no line, ending, trailing "
              "space or final newline moved", got == "\t" + TABBED, repr(got))
        # A file with no line break takes CRLF for its new lines; it opens clean.
        # (Opened by hand: `_open` waits for text, and this file has none.)
        page.locator(_pane(3, '[data-testid="file-new.c"]')).dblclick()
        host = page.locator(_pane(3, '[data-testid="editor"]'))
        until(lambda: host.count() == 1 and host.get_attribute("data-path") == "new.c"
              and host.get_attribute("data-language") is not None, timeout=8)
        e = _editor(page, "new.c") or {}
        check("a file with no line break yet gets .editorconfig's CRLF for its new lines, and opens clean",
              (e.get("model") or {}).get("eol") == "CRLF" and (_buf(page, "new.c") or {}).get("dirty") is False,
              str(e.get("model")) + " " + str(_buf(page, "new.c")))
        page.locator(_lines(3)).click()
        page.keyboard.type("a")
        page.keyboard.press("Enter")
        page.keyboard.type("b")
        page.keyboard.press("Control+s")
        put = until(lambda: _puts(mock, "new.c") or None, timeout=4)
        got = put[-1]["content"] if put else None
        check("and its new line is written CRLF", got == "a\r\nb", repr(got))
        # A settings change keeps the file's .editorconfig indentation.
        _open_dialog(page, until)
        page.locator('[data-testid="es-tab-size"]').select_option("8")
        page.locator('[data-testid="es-brackets"]').uncheck()
        model = (_editor(page, "tabbed.c") or {}).get("model") or {}
        check("a settings change (tab size, bracket colours) keeps .editorconfig's indentation",
              model.get("insertSpaces") is False and model.get("tabSize") == 4, str(model))
        page.keyboard.press("Escape")
    finally:
        for n in (".editorconfig", "tabbed.c", "new.c"):
            w["files"].pop(n, None)
        for a in added:
            if a in tree:
                tree.remove(a)


def _card_checks(page, mock, check, until):
    check("setup: calc.py opens in pane 2", _open(page, until, 2, "calc.py"))
    check("setup: the dialog is open", _open_dialog(page, until))
    _approval(mock, "es1")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    check("a card closes the editor settings", bool(until(lambda: page.locator(DIALOG).count() == 0, timeout=3)))
    gear = page.locator(_pane(2, '[data-testid="editor-settings"]'))
    inert = page.evaluate("""() => {
      const el = document.querySelector('[data-testid="pane-2"] [data-testid="editor-settings"]');
      return !!el && !!el.closest('[inert]');
    }""")
    check("the ⚙ is disabled and behind the inert veil", gear.is_disabled() and inert)
    page.keyboard.press("Control+Shift+P")
    time.sleep(0.2)
    check("no key opens the palette (and so the dialog) under it", page.locator('[data-testid="palette"]').count() == 0)
    page.keyboard.press("Escape")
    body = until(lambda: mock.sent("POST", "/approvals/es1") or None, timeout=4)
    check("Escape denies the card", bool(body) and body[-1].get("decision") == "deny")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    check("and the ⚙ works again after it", not gear.is_disabled() and _open_dialog(page, until))
    page.keyboard.press("Escape")


def _vim_checks(page, mock, check, until):
    page.evaluate("localStorage.setItem('jarvis.dictation', 'off')")
    _boot(page, mock, None, until, reload=True)
    check("setup: calc.py opens in pane 2", _open(page, until, 2, "calc.py", double=True))
    check("setup: notes.txt opens in pane 3", _open(page, until, 3, "notes.txt", double=True))
    _open_dialog(page, until)
    page.locator('[data-testid="es-vim"]').check()
    page.keyboard.press("Escape")
    status = page.locator(_pane(2, '[data-testid="editor-vim-status"]'))
    check("vim on: each File pane gets its status line, in normal mode",
          bool(until(lambda: status.count() == 1 and "NORMAL" in status.inner_text(), timeout=8)),
          status.inner_text() if status.count() else "")
    check("the probe says vim drives both editors",
          bool(until(lambda: all(e["vim"] for e in _probe(page)["editors"]), timeout=4)), str(_probe(page)["editors"]))
    # Ctrl+D is push-to-talk's: vim's half-page-down never runs (the cursor stays on line 1).
    page.locator(_lines(2)).click()
    page.keyboard.press("g")
    page.keyboard.press("g")
    pos = page.locator(_pane(2, '[data-testid="status-pos"]'))
    until(lambda: pos.inner_text().startswith("Ln 1,"), timeout=2)
    page.keyboard.press("Control+d")
    time.sleep(0.2)
    on_one = pos.inner_text().startswith("Ln 1,")
    page.keyboard.press("j")
    moved = until(lambda: pos.inner_text().startswith("Ln 2,"), timeout=2)
    check("Ctrl+D stays push-to-talk's: vim never sees it, while vim's j moves", on_one and bool(moved),
          pos.inner_text())
    # i, typing, Escape, :w — this buffer's guarded save.
    before = mock.world["files"]["calc.py"]["content"]
    page.keyboard.press("g")
    page.keyboard.press("g")
    page.keyboard.press("i")
    until(lambda: "INSERT" in status.inner_text(), timeout=2)
    page.keyboard.type("#v")
    page.keyboard.press("Escape")
    until(lambda: "NORMAL" in status.inner_text(), timeout=2)
    # Monaco draws the lines a frame after the edit: wait for the drawing, not just the buffer.
    check("i and typing insert, Escape is normal mode again",
          bool(until(lambda: (_buf(page, "calc.py") or {}).get("dirty") is True
                     and _text(page, 2).startswith("#vdef add"), timeout=3)), repr(_text(page, 2)[:40]))
    # An edit in pane 3 too: `:w` in pane 2 must not save it.
    page.locator(_lines(3)).click()
    page.keyboard.press("i")
    page.keyboard.type("Z")
    page.keyboard.press("Escape")
    until(lambda: (_buf(page, "notes.txt") or {}).get("dirty") is True, timeout=2)
    page.locator(_lines(2)).click()
    page.keyboard.type(":w")
    page.keyboard.press("Enter")
    put = until(lambda: _puts(mock, "calc.py") or None, timeout=4)
    check(":w saves this pane's buffer, through the guarded save (with its etag)",
          bool(put) and put[-1]["content"] == "#v" + before and isinstance(put[-1].get("expected_etag"), str),
          repr(put[-1] if put else None))
    time.sleep(0.3)
    check("and no other pane's buffer", not _puts(mock, "notes.txt")
          and (_buf(page, "notes.txt") or {}).get("dirty") is True)
    check("the buffer reads saved after it", bool(until(lambda: (_buf(page, "calc.py") or {}).get("dirty") is False,
                                                          timeout=3)))
    # Off again: keys type.
    _open_dialog(page, until)
    page.locator('[data-testid="es-vim"]').uncheck()
    page.keyboard.press("Escape")
    check("vim off: the status line goes", bool(until(lambda: status.count() == 0, timeout=3)))
    page.locator(_lines(2)).click()
    page.keyboard.press("Control+Home")
    page.keyboard.type("q")
    check("and a key types again", bool(until(lambda: _text(page, 2).startswith("q#v"), timeout=2)), _text(page, 2)[:20])
    check("the probe says no editor is vim's", not any(e["vim"] for e in _probe(page)["editors"]))
    page.evaluate("localStorage.removeItem('jarvis.dictation')")


def _theme_of(page, n: int) -> dict:
    return page.evaluate("""n => {
      const ed = document.querySelector(`[data-testid="pane-${n}"] .monaco-editor`);
      const bg = document.querySelector(`[data-testid="pane-${n}"] .monaco-editor-background`);
      return { cls: ed ? ed.className : '', bg: bg ? getComputedStyle(bg).backgroundColor : '' };
    }""", n)


def _vim_review_checks(page, mock, check, until):
    """The review of PR #59: `:colorscheme` never leaves Graphite, a settings
    change keeps vim's cursor, and vim's undo does nothing on a read-only file."""
    page.evaluate("localStorage.setItem('jarvis.dictation', 'off')")
    _boot(page, mock, None, until, reload=True)
    check("setup: calc.py opens in pane 2", _open(page, until, 2, "calc.py", double=True))
    check("setup: allowlist.json (read-only) opens in pane 3", _open(page, until, 3, "allowlist.json", double=True))
    _open_dialog(page, until)
    page.locator('[data-testid="es-vim"]').check()
    page.keyboard.press("Escape")
    status = page.locator(_pane(2, '[data-testid="editor-vim-status"]'))
    until(lambda: status.count() == 1 and "NORMAL" in status.inner_text(), timeout=8)
    before = _theme_of(page, 2)
    check("setup: the editor wears the Graphite scheme (a dark base)", "vs-dark" in before["cls"].split(), str(before))
    page.locator(_lines(2)).click()
    page.keyboard.type(":colorscheme vs")
    page.keyboard.press("Enter")
    time.sleep(0.3)
    after = _theme_of(page, 2)
    other = _theme_of(page, 3)
    check(":colorscheme vs changes nothing, in this editor or any other",
          after["bg"] == before["bg"] and "vs-dark" in after["cls"].split() and "vs" not in after["cls"].split()
          and "vs-dark" in other["cls"].split() and other["bg"] == before["bg"],
          f"{before} → {after}; pane 3 {other}")
    # A settings change while vim drives the editor keeps vim's block cursor.
    page.locator(_lines(2)).click()
    page.keyboard.press("Escape")
    until(lambda: (_editor(page, "calc.py") or {}).get("cursorStyle") == "block", timeout=3)
    _open_dialog(page, until)
    page.locator('[data-testid="es-font-size"]').select_option("14")
    e = _editor(page, "calc.py") or {}
    check("a settings change keeps vim's normal-mode block cursor (and still applies)",
          e.get("cursorStyle") == "block" and e.get("fontSize") == 14, str(e))
    page.keyboard.press("Escape")
    # Vim's u on a read-only file: an agent's write reloads it in place (one
    # undo step), and u must not take the file back to the old text.
    mock.disk_write("allowlist.json", '["agent"]\n')
    check("setup: the read-only file reloads in place with the new text",
          bool(until(lambda: '"agent"' in _text(page, 3), timeout=10)), _text(page, 3))
    page.locator(_lines(3)).click()
    page.keyboard.press("Escape")
    page.keyboard.press("u")
    page.keyboard.type(":undo")
    page.keyboard.press("Enter")
    time.sleep(0.3)
    check("vim's u and :undo do nothing on a read-only editor",
          '"agent"' in _text(page, 3) and (_buf(page, "allowlist.json") or {}).get("dirty") is False,
          _text(page, 3) + " " + str(_buf(page, "allowlist.json")))
    page.locator(_lines(2)).click()
    page.keyboard.press("Escape")
    page.keyboard.press("x")
    until(lambda: (_buf(page, "calc.py") or {}).get("dirty") is True, timeout=2)
    page.keyboard.press("u")
    check("while u still undoes in an editable one",
          bool(until(lambda: (_buf(page, "calc.py") or {}).get("dirty") is False, timeout=2)))
    _open_dialog(page, until)
    page.locator('[data-testid="es-vim"]').uncheck()
    page.keyboard.press("Escape")
    page.evaluate("localStorage.removeItem('jarvis.dictation')")


def _open_tabs_at_once(page, mock, until, paths) -> tuple[list, float]:
    """Monaco already loaded (calc.py in pane 2), switch pane 3 — pinned to p1
    with these tabs stored — to Files: every tab's buffer opens at once, as a
    reload restores them. Returns the long tasks from the switch on, and how
    long until every tab's buffer was made (its `.editorconfig` judged)."""
    ws = json.dumps({"preset": "cols3", "focused": 2, "panes": [
        {"view": "chat"}, {"view": "file"},
        {"view": "task", "projectId": "p1", "editors": {"paths": paths, "active": paths[0]}},
        {"view": "task"}]})
    _fresh(page, mock, until, ws=ws)
    if not _open(page, until, 2, "calc.py"):
        raise RuntimeError("calc.py did not open in pane 2")
    t0 = page.evaluate("performance.now()")
    page.locator(_pane(3, '[data-testid="tab-file"]')).click()
    want = set(paths)
    if not until(lambda: want <= {b["path"] for b in page.evaluate("window.__hud.buffers()")}, timeout=30):
        raise RuntimeError("the tabs' buffers were never made")
    done = page.evaluate("performance.now()") - t0
    time.sleep(0.5)  # anything still queued settles
    tasks = page.evaluate("t0 => (window.__longTasks || []).filter(e => e.start >= t0).map(e => Math.round(e.ms))", t0)
    return tasks, done


def _long_task_checks(page, mock, check, until):
    """The review of PR #59, round 3, in Chromium: 32 `.editorconfig`s on every
    walk, each as heavy as the caps allow, and 20 tabs opened at once — no
    long task of 100 ms or more (it was one of 1,517 ms)."""
    page.add_init_script(LONGTASK_PROBE)
    w = mock.world
    paths = [f"{'/'.join(DEEP_DIRS)}/robot_drive_subsystem_controller_for_the_left_side_{i}.py" for i in range(20)]
    configs = ["/".join(DEEP_DIRS[:i] + [".editorconfig"]) for i in range(len(DEEP_DIRS) + 1)]
    for p in paths:
        w["files"][p] = {"content": "x = 1\n", "mtime": 1757900500}
    try:
        base, base_ms = _open_tabs_at_once(page, mock, until, paths)
        check("setup: the browser reports long tasks", page.evaluate("Array.isArray(window.__longTasks)"))
        for i, c in enumerate(configs):
            w["files"][c] = {"content": _heavy_config(str(i)), "mtime": 1757900600}
        said: list[str] = []
        listen = lambda m: said.append(m.text) if ".editorconfig not applied" in m.text else None  # noqa: E731
        page.on("console", listen)
        try:
            tasks, ms = _open_tabs_at_once(page, mock, until, paths)
        finally:
            page.remove_listener("console", listen)
        check("setup: the configs were judged — and stopped (\"took too long\" said in the console)",
              any("took too long" in s for s in said), str(said[:2]))
        print(f"    long tasks without configs {base}, with 32 heavy configs {tasks}; "
              f"tabs ready in {base_ms:.0f} ms without, {ms:.0f} ms with")
        check("32 heavy configs deep and 20 tabs at once: no task of 100 ms or more",
              max(tasks, default=0) < 100, f"long tasks {tasks}; without configs {base}")
        check("and the tabs are not visibly late (under a second more than without configs)",
              ms < base_ms + 1000, f"{ms:.0f} ms vs {base_ms:.0f} ms")
        bufs = [b for b in page.evaluate("window.__hud.buffers()") if b["path"] in set(paths)]
        check("every tab opened, clean", len(bufs) == 20 and not any(b["dirty"] for b in bufs))
    finally:
        for p in paths + configs:
            w["files"].pop(p, None)


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    from tests.face.hud_v2_mock import DIST, MockDaemon
    if not (DIST / "index.html").exists():
        print("hud/dist is not built. Run: cd hud && npm ci --ignore-scripts && npm run build")
        return 2
    mock = MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
            # `ONLY=vim` runs one section (a mutation round's quick way in).
            import os
            settings_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until,
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
    print(f"all {v2.CHECKS} editor settings checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
