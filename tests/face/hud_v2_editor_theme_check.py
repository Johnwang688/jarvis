"""Headless checks for the Graphite editor scheme (ED-3; editor plan
docs/plans/2026-10-09-hud-editor-plan.md §2.5, decisions E-4: no syntax colour
may use amber or red).

Free and hermetic: a `MockDaemon(0)` of its own, whose world gains a C++ and a
Python file, and a browser context of its own. Nothing here reaches
8402/8403/8405 (`hud_v2_check.guard_live`).

Every colour is read back **as the window draws it** — the computed style of a
rendered token, Monaco's published `--vscode-*` variables, its `.mtkN` colour
map — and compared with the HUD's own tokens read off `:root` in the page, so
the check holds the editor to theme.css, never to a copy of its values. The
checks worth keeping, each written to bite:

  - a C++ and a Python sample show each class of token in its row's colour:
    a keyword bold in `--text`, a function call and a definition in
    `--syn-fn`, a string in `--syn-string`, a number and a constant in
    `--syn-const`, a type, a directive and a decorator in `--syn-type`, a
    comment italic in `--text-muted`, a builtin italic `--syn-fn`, `self`
    italic `--text`, punctuation `--text-2`;
  - no rendered token, and no colour in Monaco's token colour map (every
    colour any token can take), is in the amber or red band
    (340°–65° at saturation ≥ 0.35: the plan's two bands and the orange
    between them);
  - of every colour Monaco publishes for its whole colour registry, the only
    warm ones are the HUD's `--red`, where Monaco's own name says error or
    removed;
  - the chrome: the pane, the current line, line numbers, bracket pairs in
    the three syntax hues;
  - the find-match highlight is the accent, never Monaco's orange (a guard:
    ED-1 already pinned it, so it passes on ED-1's build too);
  - a control character (ESC, BEL, a form feed) is not drawn on Monaco's own
    white-on-dark-red box, which no colour registry holds (review of PR #45);
  - the approval card's computed colours are the same before Monaco ever
    loaded and with the editor open (a guard against the editor's global
    styles reaching the card).

Run alone:  PYTHONPATH=. .venv/bin/python tests/face/hud_v2_editor_theme_check.py
"""
from __future__ import annotations

import colorsys
import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.face.hud_v2_layout_check import (  # noqa: E402
    _approval, _boot, _open_file, _pane, _set_storage, _set_ws,
)

CPP = """#include "vex.h"
// Drive forward a set distance
void driveFor(double inches, int speed) {
  if (inches > 0.5) {
    Drivetrain.driveFor(forward, inches, vex::inches);
  }
  bool ok = true;
  return;
}
"""

PY = '''import math

# move the arm
@property
def lift(self, height=90.0):
    """Raise the arm."""
    if height > 1:
        print(f"up {height}")
    return math.sqrt(height) is None
'''

# Control characters as an ANSI build log or a GNU C page break leaves them:
# ESC, BEL and a form feed (review of PR #45, LOW-1).
CTL = "int x = 1; /* esc:\x1b[0m bell:\x07 */\n\x0c\n"

FILES = {"auton.cpp": CPP, "arm.py": PY, "ctl.c": CTL}
TOKENS = ("--text", "--text-2", "--text-muted", "--syn-fn", "--syn-string", "--syn-const", "--syn-type",
          "--bg-1", "--bg-2", "--bg-3", "--accent", "--accent-hi", "--red")


def editor_theme_checks(browser, mock, base, check, until, guard, init_script):
    print("\neditor: the Graphite scheme (ED-3)")
    for i, (name, text) in enumerate(FILES.items()):
        mock.world["tree"][""].append({"name": name, "kind": "file", "size": len(text), "mtime": 1757900020 + i})
        mock.world["files"][name] = {"content": text, "mtime": 1757900020 + i}
    ctx = browser.new_context(viewport={"width": 1600, "height": 900}, permissions=["microphone"])
    guard(ctx)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    try:
        _boot(page, mock, base, until)
        # The card first, in a window where Monaco has never loaded.
        card_before = None
        try:
            card_before = _card_colours_without_editor(page, mock, check, until)
        except Exception as e:
            check("card colours before the editor ran to the end", False, str(e).splitlines()[0][:200])
        # The chrome right after C++: its bracket pairs nest three deep.
        for section in (_cpp_checks, _chrome_checks, _python_checks, _map_checks, _find_checks, _control_checks):
            try:
                section(page, mock, check, until)
            except Exception as e:  # a section that cannot run is a failure; the rest still run
                check(f"{section.__name__.strip('_')} ran to the end", False, str(e).splitlines()[0][:200])
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        try:
            _card_colours_with_editor(page, mock, check, until, card_before)
        except Exception as e:
            check("card colours with the editor ran to the end", False, str(e).splitlines()[0][:200])
        check("no page errors in the editor-scheme section", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# colours

def rgba(value: str) -> tuple[int, int, int, float] | None:
    """A CSS colour as the browser or Monaco writes it: #rgb[a], #rrggbb[aa], rgb(), rgba()."""
    v = value.strip().lower()
    m = re.fullmatch(r"#([0-9a-f]{3,8})", v)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        if len(h) not in (6, 8):
            return None
        a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a
    m = re.fullmatch(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)", v)
    if m:
        return int(float(m.group(1))), int(float(m.group(2))), int(float(m.group(3))), \
            float(m.group(4)) if m.group(4) is not None else 1.0
    return None


def warm(value) -> bool:
    """editorTheme.ts's WARM: hue 340°–65° (amber 25°–65°, red 340°–20°, and
    the orange between) at HSL saturation ≥ 0.35, and not fully transparent."""
    c = rgba(value) if isinstance(value, str) else value
    if not c or c[3] == 0:
        return False
    h, _l, s = colorsys.rgb_to_hls(c[0] / 255, c[1] / 255, c[2] / 255)
    h *= 360
    return s >= 0.35 and (h >= 340 or h <= 65)


def _tokens(page) -> dict:
    """The HUD's tokens as the page has them on :root (theme.css)."""
    return page.evaluate("""(names) => {
      const s = getComputedStyle(document.documentElement);
      return Object.fromEntries(names.map(n => [n, s.getPropertyValue(n).trim()]));
    }""", list(TOKENS))


def _same(drawn: str, token_value: str, alpha: float | None = None) -> bool:
    a, b = rgba(drawn), rgba(token_value or "")
    if not a or not b:
        return False
    if a[:3] != b[:3]:
        return False
    return alpha is None or abs(a[3] - alpha) < 0.02


SPANS = """(pane) => [...document.querySelectorAll(
    `[data-testid="pane-${pane}"] [data-testid="editor"] .view-line`)]
  .sort((a, b) => parseFloat(a.style.top) - parseFloat(b.style.top))
  .map(line => [...line.querySelectorAll('span[class*="mtk"]')].map(s => {
    const cs = getComputedStyle(s);
    return {text: s.textContent.replace(/\\u00a0/g, ' '), color: cs.color, weight: parseInt(cs.fontWeight, 10),
            style: cs.fontStyle, cls: s.className};
  }))"""


def _open(page, mock, until, name: str) -> list:
    """Open `name` in pane 2 of a chat | file layout, from a fresh load; its
    tokens once Monarch has coloured them, one list per line."""
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, json.dumps({"preset": "cols2", "focused": 2,
                              "panes": [{"view": "chat"}, {"view": "file"}, {"view": "browser"}, {"view": "task"}]}))
    _boot(page, mock, None, until, reload=True)
    if not _open_file(page, until, 2, name):
        return []
    # Coloured: more than two token classes on the page (the tokenizer is a
    # lazy chunk, so the first paint can be uncoloured).
    until(lambda: page.evaluate("""() => new Set([...document.querySelectorAll(
        '[data-testid="pane-2"] [data-testid="editor"] .view-line span[class*="mtk"]')]
        .map(s => (s.className.match(/mtk\\d+/) || [''])[0])).size""") > 3, timeout=6)
    # Then tree-sitter's colour (ED-5), for a language it covers: it arrives
    # after Monarch's, and can split or join spans (a C string's quotes), so
    # the spans are read only once the syntax worker has answered for this
    # file (or sent it back to Monarch) and the drawing has stopped changing.
    until(lambda: page.evaluate("""(name) => {
        const h = window.__hudSyntax;
        if (!h) return false;
        const s = h.states().filter(s => s.uri.split('?')[0].endsWith('/' + name)).pop();
        return !s || s.served > 0 || s.state === 'basic';
      }""", name), timeout=10)
    last = None
    for _ in range(20):
        time.sleep(0.2)
        now = page.evaluate(SPANS, 2)
        if now == last:
            break
        last = now
    return last


def _find(lines: list, text: str, nth: int = 0) -> dict | None:
    # A string's quotes may be spans of their own (Monarch) or part of the
    # string (tree-sitter's `string`, ED-5): `vex.h` matches `"vex.h"` too.
    hits = [s for line in lines for s in line if s["text"].strip() in (text, f'"{text}"')]
    return hits[nth] if len(hits) > nth else None


def _expect(check, tokens: dict, lines: list, label: str, text: str, token: str, bold=False, italic=False, nth=0):
    s = _find(lines, text, nth)
    ok = bool(s) and _same(s["color"], tokens[token]) and (s["weight"] >= 600) == bold \
        and (s["style"] == "italic") == italic
    style = ("bold " if bold else "") + ("italic " if italic else "")
    check(f"{label}: `{text}` is {style}{token}", ok,
          f"drawn {s and (s['color'], s['weight'], s['style'])}, {token} is {tokens[token]!r}")


def _expect_run(check, tokens: dict, lines: list, label: str, text: str, token: str):
    """`text` drawn in `token` whether it is one span (Monarch's `@property`)
    or a run of adjacent ones (tree-sitter's `@` and `property`, ED-5)."""
    run = None
    for line in lines:
        for i in range(len(line)):
            joined = ""
            for j in range(i, len(line)):
                joined += line[j]["text"]
                if joined.strip() == text:
                    run = line[i:j + 1]
                    break
                if len(joined) >= len(text) + 2:
                    break
            if run:
                break
        if run:
            break
    ok = bool(run) and all(_same(s["color"], tokens[token]) for s in run if s["text"].strip())
    check(f"{label}: `{text}` is {token}", ok,
          f"drawn {run and [(s['text'], s['color']) for s in run]}, {token} is {tokens[token]!r}")


def _no_warm_rendered(check, lines: list, label: str):
    hot = [(s["text"], s["color"]) for line in lines for s in line if warm(s["color"])]
    check(f"{label}: no rendered token is amber, orange or red", bool(lines) and not hot, str(hot[:5]))


# ---------------------------------------------------------------------------
# sections

def _cpp_checks(page, mock, check, until):
    lines = _open(page, mock, until, "auton.cpp")
    check("C++ setup: auton.cpp opens in Monaco, coloured", bool(lines))
    t = _tokens(page)
    _expect(check, t, lines, "C++ keyword", "if", "--text", bold=True)
    _expect(check, t, lines, "C++ keyword", "return", "--text", bold=True)
    _expect(check, t, lines, "C++ function call", "driveFor", "--syn-fn", nth=1)
    _expect(check, t, lines, "C++ function definition", "driveFor", "--syn-fn", nth=0)
    _expect(check, t, lines, "C++ string", "vex.h", "--syn-string")
    _expect(check, t, lines, "C++ number", "0.5", "--syn-const")
    _expect(check, t, lines, "C++ constant", "true", "--syn-const")
    _expect(check, t, lines, "C++ comment", "// Drive forward a set distance", "--text-muted", italic=True)
    _expect(check, t, lines, "C++ builtin type", "double", "--syn-type")
    _expect(check, t, lines, "C++ builtin type", "void", "--syn-type")
    _expect(check, t, lines, "C++ directive", "#include", "--syn-type")
    _expect(check, t, lines, "C++ namespace", "vex", "--syn-type")
    _expect(check, t, lines, "C++ punctuation", ";", "--text-2")
    _no_warm_rendered(check, lines, "C++")


def _python_checks(page, mock, check, until):
    lines = _open(page, mock, until, "arm.py")
    check("Python setup: arm.py opens in Monaco, coloured", bool(lines))
    t = _tokens(page)
    _expect(check, t, lines, "Python keyword", "def", "--text", bold=True)
    _expect(check, t, lines, "Python keyword", "return", "--text", bold=True)
    _expect(check, t, lines, "Python function call", "sqrt", "--syn-fn")
    _expect(check, t, lines, "Python function definition", "lift", "--syn-fn")
    _expect(check, t, lines, "Python builtin", "print", "--syn-fn", italic=True)
    _expect(check, t, lines, "Python string", '"""Raise the arm."""', "--syn-string")
    _expect(check, t, lines, "Python number", "90.0", "--syn-const")
    _expect(check, t, lines, "Python constant", "None", "--syn-const")
    _expect(check, t, lines, "Python comment", "# move the arm", "--text-muted", italic=True)
    _expect_run(check, t, lines, "Python decorator", "@property", "--syn-type")
    _expect(check, t, lines, "Python self", "self", "--text", italic=True)
    _no_warm_rendered(check, lines, "Python")


def _theme_css(page) -> str:
    """The stylesheet Monaco's theme service writes: its colour variables and token colour map."""
    return page.evaluate("""() => [...document.querySelectorAll('style')].map(s => s.textContent)
        .filter(t => t.includes('--vscode-editor-background')).join('\\n')""")


def _map_checks(page, mock, check, until):
    """Every colour any token can be drawn in, not only the ones these samples drew."""
    css = _theme_css(page)
    colours = re.findall(r"\.mtk\d+\s*\{\s*color:\s*([^;]+);", css)
    t = _tokens(page)
    check("Monaco's token colour map was found", len(colours) >= 2, str(len(colours)))
    hot = [c for c in colours if warm(c)]
    check("no colour in Monaco's token colour map is amber, orange or red", not hot, str(hot))
    allowed = {rgba(t[k])[:3] for k in ("--text", "--text-2", "--text-muted", "--syn-fn", "--syn-string",
                                         "--syn-const", "--syn-type", "--bg-1") if rgba(t[k])}
    stray = [c for c in colours if not rgba(c) or rgba(c)[:3] not in allowed]
    check("and every colour in it is one of the HUD's text greys, syntax hues or the pane", not stray, str(stray))


def _chrome_checks(page, mock, check, until):
    css = _theme_css(page)
    t = _tokens(page)
    variables = {k: v.strip() for k, v in re.findall(r"(--vscode-[\w-]+):\s*([^;]+);", css) if rgba(v)}
    check("Monaco publishes its colour registry", len(variables) > 200, str(len(variables)))
    red = rgba(t["--red"])
    bad = [(k, v) for k, v in variables.items()
           if warm(v) and not (re.search(r"[Ee]rror|[Rr]emoved|-red$", k) and red and rgba(v)[:3] == red[:3])]
    check("of every colour Monaco publishes, the only warm ones are --red where it means error or removed",
          not bad, f"{len(bad)}: {bad[:6]}")
    v = lambda k: variables.get(f"--vscode-{k}", "")
    check("the pane is --bg-1 and the current line --bg-2",
          _same(v("editor-background"), t["--bg-1"]) and _same(v("editor-lineHighlightBackground"), t["--bg-2"]),
          f"{v('editor-background')} {v('editor-lineHighlightBackground')}")
    check("line numbers are --text-muted, the active one --text-2",
          _same(v("editorLineNumber-foreground"), t["--text-muted"])
          and _same(v("editorLineNumber-activeForeground"), t["--text-2"]),
          f"{v('editorLineNumber-foreground')} {v('editorLineNumber-activeForeground')}")
    check("the cursor is --accent-hi and an error squiggle --red, a warning --text-2",
          _same(v("editorCursor-foreground"), t["--accent-hi"]) and _same(v("editorError-foreground"), t["--red"])
          and _same(v("editorWarning-foreground"), t["--text-2"]),
          f"{v('editorCursor-foreground')} {v('editorError-foreground')} {v('editorWarning-foreground')}")
    # Bracket pairs as drawn: auton.cpp's, by nesting level (the function's
    # braces, the `if`, the call inside it).
    drawn = page.evaluate("""() => Object.fromEntries([0, 1, 2].map(n => {
      const s = document.querySelector(`[data-testid="pane-2"] .view-line .bracket-highlighting-${n}`);
      return [n, s ? getComputedStyle(s).color : null];
    }))""")
    check("bracket pairs are drawn in --syn-fn, --syn-const, --syn-type by depth (never Monaco's gold)",
          _same(drawn.get("0") or "", t["--syn-fn"]) and _same(drawn.get("1") or "", t["--syn-const"])
          and _same(drawn.get("2") or "", t["--syn-type"]), str(drawn))


def _find_checks(page, mock, check, until):
    """Find's matches in the accent: Monaco's own are orange (#ea5c00 at 24°)."""
    lines = _open(page, mock, until, "auton.cpp")
    if not lines:
        check("find setup: auton.cpp opens in Monaco", False)
        return
    t = _tokens(page)
    page.locator(_pane(2, '[data-testid="editor"] .view-lines')).click()
    page.keyboard.press("Control+Home")
    page.keyboard.press("Control+f")
    find = page.locator(_pane(2, ".find-widget.visible textarea, .find-widget.visible input")).first
    until(lambda: find.count() > 0, timeout=3)
    find.fill("inches")
    current = page.locator(_pane(2, ".currentFindMatch"))
    others = page.locator(_pane(2, ".findMatch"))
    until(lambda: current.count() > 0 and others.count() > 0, timeout=4)
    got = page.evaluate("""() => {
      const q = (sel) => document.querySelector(`[data-testid="pane-2"] ${sel}`);
      const c = q('.currentFindMatch'), o = q('.findMatch:not(.currentFindMatch)') || q('.findMatch');
      return {
        current: c && getComputedStyle(c).backgroundColor, border: c && getComputedStyle(c).borderTopColor,
        other: o && getComputedStyle(o).backgroundColor,
      };
    }""")
    check("find: the current match is the accent at 35% with an accent border, the others the accent at 15%",
          _same(got["current"] or "", t["--accent"], 0.35) and _same(got["border"] or "", t["--accent"])
          and _same(got["other"] or "", t["--accent"], 0.15), str(got))
    check("find: no match highlight is Monaco's orange, or any warm colour",
          bool(got["current"]) and not any(warm(v or "") for v in got.values()), str(got))
    page.keyboard.press("Escape")


def _control_checks(page, mock, check, until):
    """Monaco draws a control character on its own box, white on dark red
    (`.mtkcontrol`, `!important`, viewLines.css) — not a registry colour, so
    no theme or registry check sees it. theme.css pins it to HUD tokens."""
    lines = _open(page, mock, until, "ctl.c")
    check("control-character setup: ctl.c opens in Monaco, coloured", bool(lines))
    t = _tokens(page)
    got = page.evaluate("""() => [...document.querySelectorAll(
        '[data-testid="pane-2"] [data-testid="editor"] .view-line .mtkcontrol')].map(s => {
          const cs = getComputedStyle(s);
          return [s.textContent.codePointAt(0).toString(16), cs.color, cs.backgroundColor];
        })""")
    check("control-character setup: ESC, BEL and the form feed are drawn as control characters",
          len(got) >= 3, str(got))
    hot = [g for g in got if warm(g[1]) or warm(g[2])]
    check("control characters are not drawn on Monaco's red box: no warm colour or fill",
          bool(got) and not hot, str(hot or got))
    check("they are --text-2 on --bg-3",
          bool(got) and all(_same(c, t["--text-2"]) and _same(bg, t["--bg-3"]) for _, c, bg in got), str(got))


CARD = """() => {
  const card = document.querySelector('[data-testid="approval-card"]');
  if (!card) return null;
  // The card and everything in it, by position in the card: colour, fill,
  // border, weight and slant. The text (the request's code, the countdown)
  // is not part of it.
  return [card, ...card.querySelectorAll('*')].map((el, i) => {
    const s = getComputedStyle(el);
    return [i, el.tagName, el.getAttribute('data-testid') || '', s.color, s.backgroundColor,
            s.borderTopColor, s.fontWeight, s.fontStyle, s.outlineColor];
  });
}"""


def _card_snapshot(page, mock, until, req: str):
    _approval(mock, req, "make -C robot build")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    page.mouse.move(2, 2)
    time.sleep(0.6)  # past any entrance transition
    snap = page.evaluate(CARD)
    page.keyboard.press("Escape")
    until(lambda: mock.sent("POST", f"/approvals/{req}") or None, timeout=4)
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    return snap


def _card_colours_without_editor(page, mock, check, until):
    loaded = page.evaluate("() => !!document.querySelector('.monaco-editor') || [...document.querySelectorAll("
                           "'style')].some(s => s.textContent.includes('--vscode-editor-background'))")
    check("card setup: Monaco has not loaded in this window yet", not loaded)
    snap = _card_snapshot(page, mock, until, "ed3card1")
    check("card setup: the card's colours were read", bool(snap), str(snap)[:120])
    return snap


def _card_colours_with_editor(page, mock, check, until, before):
    lines = _open(page, mock, until, "auton.cpp")
    check("card setup: the editor is open, its theme applied",
          bool(lines) and "--vscode-editor-background" in _theme_css(page))
    after = _card_snapshot(page, mock, until, "ed3card2")
    diff = [(a, b) for a, b in zip(before or [], after or []) if a != b]
    check("the approval card's computed colours are unchanged with the editor open",
          bool(before) and bool(after) and len(before) == len(after) and not diff,
          f"{len(before or [])} vs {len(after or [])} elements; first differences {diff[:2]}")


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
            editor_theme_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until,
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
    print(f"all {v2.CHECKS} editor-scheme checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
