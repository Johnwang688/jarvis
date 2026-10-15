"""Headless checks for the File pane's unsaved-changes dots and file icons
(2026-10-11, the owner's ask: "like VS Code", for the robotics lab).

Free and hermetic: a `MockDaemon(0)` of its own and a browser context of its
own, under `hud_v2_check.guard` (no request to 8402/8403/8405, no CSP
violation). The world gains a nested `src/robot/` folder with a C++ source
and header, and a `config.json`, for the run only.

What it pins:
  - **icons**: a file row wears a 16px icon after the twistie's room and
    before its name, vertically centred, `aria-hidden`, drawn in one of the
    theme's quiet tokens and never a warm hue — `.cpp`, `.h`, `.json`, `.py`,
    `.md`, `.env` each its own; a folder row is its chevron and no icon;
  - **tab icons**: each editor tab starts with the very icon (glyph and
    colour) its tree row wears — one component, one rule — 16px, before the
    name, centred, `aria-hidden`; a long name still ellipsizes, and at a
    narrow window and at 160% / 200% zoom the icon keeps its size;
  - **the dot**: typing marks the file's tree row and its tab (the dot stands
    where × is; hovering or focusing the tab brings × back), with the name
    "unsaved changes" and a tooltip, in `--text-2`; while a slow save is
    out (ED-2) the tab's slot holds a hollow ring, named "saving", and the
    tree's dot stays; the answer clears both;
  - **folders**: a collapsed folder holding a dirty file at any depth wears
    the dot, an open one does not — its dirty child does;
  - **every pane**: dirty is the buffer's, so another pane's tree on the same
    project marks it without opening it, and its tab too once it does — and
    so does a project whose root is a symlink onto the same folder (found
    under `root_real`, where its root resolves);
  - **under a card**: the marks add nothing focusable — none of them takes
    focus, all sit behind the inert veil, Tab stays on the card, and Escape
    denies.

Run: PYTHONPATH=. .venv/bin/python tests/face/hud_v2_editor_marks_check.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.face.hud_v2_editor_check import TWO, WIDE, _fresh, _lines, _open, _tab, _type  # noqa: E402
from tests.face.hud_v2_layout_check import _approval, _boot, _pane  # noqa: E402

CPP = "src/robot/drive.cpp"
HDR = "src/robot/drive.h"
LONG = "a-rather-long-file-name-for-the-tab-ellipsis-check.cpp"
FILES = {
    CPP: {"content": "#include \"drive.h\"\n\nvoid drive(int speed) {\n  motor(speed);\n}\n", "mtime": 1757900030},
    HDR: {"content": "#pragma once\nvoid drive(int speed);\n", "mtime": 1757900031},
    "config.json": {"content": "{\n  \"port\": 3\n}\n", "mtime": 1757900032},
    LONG: {"content": "int x;\n", "mtime": 1757900033},
}
TOKENS = ("--syn-fn", "--syn-string", "--syn-const", "--syn-type", "--text-2", "--text-muted")


def _row(n: int, kind: str, path: str) -> str:
    return _pane(n, f'[data-testid="{kind}-{path}"]')


def _dot(n: int, path: str) -> str:
    return _pane(n, f'[data-testid="dirty-{path}"]')


def _count(page, sel: str) -> int:
    return page.locator(sel).count()


def _toggle(page, until, n: int, path: str, want_open: bool):
    """Open or close folder `path` in pane N's tree (its chevron says which)."""
    row = page.locator(_row(n, "dir", path))
    until(lambda: row.count() > 0, timeout=4)
    kids = _pane(n, f'[data-testid^="file-{path}/"]') + ", " + _pane(n, f'[data-testid^="dir-{path}/"]')
    child_shown = lambda: page.locator(kids).count() > 0
    if child_shown() != want_open:
        row.click()
        until(lambda: child_shown() == want_open, timeout=4)


def _expand(page, until, n: int, *paths: str):
    for p in paths:
        _toggle(page, until, n, p, True)


def _rgb(text: str) -> tuple[int, int, int] | None:
    m = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def _warm(rgb) -> bool:
    """editorTheme.ts's `isWarm`: hue 340°–65° at HSL saturation ≥ 0.35."""
    r, g, b = (c / 255 for c in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    d, lum = mx - mn, (mx + mn) / 2
    if d == 0:
        return False
    s = d / (1 - abs(2 * lum - 1))
    if mx == r:
        h = ((g - b) / d) % 6
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    h = h * 60 % 360
    return s >= 0.35 and (h >= 340 or h <= 65)


def _token_rgb(page, token: str):
    return _rgb(page.evaluate("""t => { const d = document.createElement('span'); d.style.color = `var(${t})`;
      document.body.appendChild(d); const c = getComputedStyle(d).color; d.remove(); return c; }""", token))


def _box(page, sel: str):
    return page.locator(sel).first.bounding_box()


def marks_checks(browser, mock, base, check, until, guard, init_script):
    print("\neditor marks: the unsaved-changes dot and file icons")
    ctx = browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]}, permissions=["microphone"])
    guard(ctx)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    w = mock.world
    keep_files = {k: dict(v) for k, v in w["files"].items()}
    keep_tree = {k: [dict(e) for e in v] for k, v in w["tree"].items()}
    keep_projects = [dict(p) for p in w["projects"]]
    try:
        # A project whose root is a symlink onto p1's folder (`root_real`):
        # the mock serves one tree, so its pane shows p1's files.
        p1 = next(p for p in w["projects"] if p["id"] == "p1")
        w["projects"].append({**p1, "id": "p3", "name": "lab-link", "root": "/home/johnw/lab-link",
                              "root_real": p1["root"], "inbox": False})
        w["files"].update({k: dict(v) for k, v in FILES.items()})
        w["tree"][""].append({"name": "config.json", "kind": "file", "size": 18, "mtime": 1757900032})
        w["tree"][""].append({"name": LONG, "kind": "file", "size": 8, "mtime": 1757900033})
        w["tree"]["src"].insert(0, {"name": "robot", "kind": "dir", "size": 0, "mtime": 1757900029})
        w["tree"]["src/robot"] = [
            {"name": "drive.cpp", "kind": "file", "size": 60, "mtime": 1757900030},
            {"name": "drive.h", "kind": "file", "size": 36, "mtime": 1757900031},
        ]
        keep = {k: dict(v) for k, v in w["files"].items()}
        _boot(page, mock, base, until)
        for section, ws in ((_icon_checks, TWO), (_tab_icon_checks, TWO), (_dirty_checks, TWO), (_two_pane_checks, TWO),
                            (_linked_root_checks, LINKED), (_card_checks, TWO)):
            _fresh(page, mock, until, ws=ws, keep_files=keep)
            try:
                section(page, mock, check, until)
            except Exception as e:  # a section that cannot run is a failure; the rest still run
                check(f"{section.__name__.strip('_')} ran to the end", False, str(e).splitlines()[0][:200])
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        check("no page errors in the editor marks section", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()
        w["files"].clear()
        w["files"].update(keep_files)
        w["tree"].clear()
        w["tree"].update(keep_tree)
        w["projects"][:] = keep_projects


# ---------------------------------------------------------------------------

def _icon_checks(page, mock, check, until):
    _expand(page, until, 2, "src", "src/robot")
    want = {CPP: "cpp", HDR: "h", "config.json": "json", "calc.py": "python", "src/main.py": "python",
            "README.md": "markdown", ".env": "env", "notes.txt": "text"}
    got = {p: page.locator(_row(2, "file", p) + " .ficon").first.get_attribute("data-icon") for p in want}
    check("each file wears its own icon: .cpp, .h, .json, .py, .md, .env, .txt", got == want, str(got))
    check("a folder row is its chevron, with no icon",
          _count(page, _row(2, "dir", "src") + " .chev") == 1 and _count(page, _row(2, "dir", "src") + " .ficon") == 0
          and _count(page, _row(2, "dir", "src/robot") + " .ficon") == 0)

    bad = []
    tokens = {t: _token_rgb(page, t) for t in TOKENS}
    for p in (CPP, HDR, "config.json", "calc.py", ".env"):
        row = _row(2, "file", p)
        icon, name, tw = _box(page, row + " .ficon"), _box(page, row + " .nm"), _box(page, row + " .tw")
        rbox = _box(page, row)
        if abs(icon["width"] - 16) > 0.5 or abs(icon["height"] - 16) > 0.5:
            bad.append(f"{p}: {icon['width']}x{icon['height']}")
        if not (tw["x"] + tw["width"] <= icon["x"] + 0.5 and icon["x"] + icon["width"] <= name["x"] + 0.5):
            bad.append(f"{p}: not between the twistie and the name")
        if abs((icon["y"] + icon["height"] / 2) - (rbox["y"] + rbox["height"] / 2)) > 1.5:
            bad.append(f"{p}: not centred in its row")
        info = page.locator(row + " .ficon").first.evaluate(
            "e => ({ color: getComputedStyle(e).color, hidden: e.getAttribute('aria-hidden'),"
            " tab: e.querySelector('[tabindex]') !== null, svg: !!e.querySelector('svg') })")
        rgb = _rgb(info["color"])
        if rgb not in tokens.values() or _warm(rgb):
            bad.append(f"{p}: colour {info['color']} is not a quiet theme token")
        if info["hidden"] != "true" or info["tab"] or not info["svg"]:
            bad.append(f"{p}: {info}")
    check("icons are 16px between the twistie and the name, centred, aria-hidden, in a quiet token",
          not bad, "; ".join(bad))
    check("no theme token an icon may wear is warm", all(c and not _warm(c) for c in tokens.values()), str(tokens))
    # A folder's chevron sits where a file's twistie room is, so the icon follows it.
    chev, ficon = _box(page, _row(2, "dir", "src/robot") + " .tw"), _box(page, _row(2, "file", CPP) + " .ficon")
    check("a nested file's icon starts past its folder's chevron", ficon["x"] > chev["x"] + chev["width"] - 0.5,
          f"{chev} {ficon}")
    shots = os.environ.get("HUD_MARKS_SHOTS")
    if shots:
        page.locator(_pane(2, '[data-testid="filetree"]')).screenshot(path=str(Path(shots) / "tree-icons.png"))


def _tab_icon_checks(page, mock, check, until):
    """The editor's tabs wear the explorer's icon (one component, one rule)."""
    _expand(page, until, 2, "src", "src/robot")
    paths = (CPP, HDR, "config.json", "calc.py", "README.md", ".env", LONG)
    for p in paths:
        check(f"setup: {p} opens in pane 2", _open(page, until, 2, p))
    svg = "e => e.querySelector('svg').outerHTML"
    bad = []
    for p in paths:
        tab, row = _tab(2, p) + " .ficon", _row(2, "file", p) + " .ficon"
        if _count(page, tab) != 1:
            bad.append(f"{p}: {_count(page, tab)} tab icons")
            continue
        t, r = page.locator(tab).first, page.locator(row).first
        if t.get_attribute("data-icon") != r.get_attribute("data-icon") or t.evaluate(svg) != r.evaluate(svg):
            bad.append(f"{p}: tab {t.get_attribute('data-icon')} vs tree {r.get_attribute('data-icon')}")
        if _rgb(t.evaluate("e => getComputedStyle(e).color")) != _rgb(r.evaluate("e => getComputedStyle(e).color")):
            bad.append(f"{p}: colour differs")
    check("each tab wears the very icon (glyph and colour) its tree row does", not bad, "; ".join(bad))
    check("…cpp, h, json, python, markdown, env and the long name's cpp",
          [page.locator(_tab(2, p) + " .ficon").first.get_attribute("data-icon") for p in paths]
          == ["cpp", "h", "json", "python", "markdown", "env", "cpp"])

    def geometry():
        out = []
        for p in paths:
            tb = _tab(2, p)
            icon, name = _box(page, tb + " .ficon"), _box(page, tb + " .edtab-name")
            tab = _box(page, tb)
            info = page.locator(tb + " .ficon").first.evaluate(
                "e => ({ hidden: e.getAttribute('aria-hidden'), tab: e.querySelector('[tabindex]') !== null,"
                " first: e.parentElement.firstElementChild === e })")
            cut = page.locator(tb + " .edtab-name").first.evaluate(
                "e => ({ over: e.scrollWidth > e.clientWidth, ell: getComputedStyle(e).textOverflow })")
            # computed sizes are in zoomed px and rects on screen: compare in screen px
            z = page.evaluate("parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--ui-zoom')) || 1")
            if abs(icon["width"] - 16 * z) > 0.6 or abs(icon["height"] - 16 * z) > 0.6:
                out.append(f"{p}: icon {icon['width']:.1f}x{icon['height']:.1f} at zoom {z}")
            if not (icon["x"] + icon["width"] <= name["x"] + 0.6 and icon["x"] >= tab["x"] - 0.6):
                out.append(f"{p}: icon not before the name inside the tab")
            if abs((icon["y"] + icon["height"] / 2) - (name["y"] + name["height"] / 2)) > 2 * z:
                out.append(f"{p}: icon not centred on the name")
            if info["hidden"] != "true" or info["tab"] or not info["first"]:
                out.append(f"{p}: {info}")
            if tab["width"] > 220 * z + 0.6:
                out.append(f"{p}: tab {tab['width']:.0f}px wider than 220")
            if p == LONG and not (cut["over"] and cut["ell"] == "ellipsis"):
                out.append(f"{p}: name not ellipsized {cut}")
        return out
    shots = os.environ.get("HUD_MARKS_SHOTS")
    check("every tab: the icon is 16px, before the name, aria-hidden, unfocusable; the long name ellipsizes",
          not geometry(), "; ".join(geometry()))
    if shots:
        page.locator(_pane(2, '[data-testid="editor-tabs"]')).screenshot(path=str(Path(shots) / "tabs-100.png"))
    for z, vp in ((1.0, (900, 700)), (1.6, (1500, 800)), (2.0, (1500, 800))):
        page.set_viewport_size({"width": vp[0], "height": vp[1]})
        page.evaluate("z => document.documentElement.style.setProperty('--ui-zoom', String(z))", z)
        time.sleep(0.4)
        g = geometry()
        check(f"narrow/zoomed (zoom {z}, {vp[0]}px wide): icons keep 16px, order and centring", not g, "; ".join(g))
        if shots and z == 1.6:
            page.locator(_pane(2, '[data-testid="editor-tabs"]')).screenshot(path=str(Path(shots) / "tabs-160.png"))
    page.evaluate("document.documentElement.style.setProperty('--ui-zoom', '1')")
    page.set_viewport_size({"width": WIDE[0], "height": WIDE[1]})
    # The tab's other parts are where they were.
    tab = _tab(2, "README.md")
    check("a tab still has its dot slot and its close button after the icon",
          page.locator(tab).evaluate("e => [...e.children].map(c => c.className.split(' ')[0]).join()")
          == "ficon,edtab-name,edtab-dot,edtab-x")
    env_tab = page.locator(_tab(2, ".env"))
    check("the env tab keeps its words for assistive technology (the lock is its icon)",
          env_tab.locator(".edtab-mark").count() == 1 and "Jarvis can't read this file" in env_tab.inner_text()
          and env_tab.locator(".ficon").get_attribute("data-icon") == "env")


def _dirty_checks(page, mock, check, until):
    _expand(page, until, 2, "src", "src/robot")
    check("setup: drive.cpp opens in pane 2", _open(page, until, 2, CPP))
    check("a clean file wears no dot", _count(page, _pane(2, ".dirtydot")) == 0
          and page.locator(_tab(2, CPP) + " .edtab-dot.on").count() == 0)
    check("setup: an edit", _type(page, until, 2, "\n// tuned at the lab"))
    dot = page.locator(_dot(2, CPP))
    until(lambda: dot.count() == 1, timeout=3)
    check("typing marks the file's tree row", dot.count() == 1 and dot.is_visible())
    check("the dot has a name and a tooltip",
          dot.get_attribute("aria-label") == "unsaved changes" and dot.get_attribute("role") == "img"
          and (dot.get_attribute("title") or "").lower().startswith("unsaved changes"))
    text2 = _token_rgb(page, "--text-2")
    check("the tree's dot is --text-2, never amber or red",
          _rgb(dot.evaluate("e => getComputedStyle(e).backgroundColor")) == text2)
    check("an open folder above it wears no dot (its dirty child does)",
          _count(page, _dot(2, "src")) == 0 and _count(page, _dot(2, "src/robot")) == 0)
    check("no other row is marked", _count(page, _pane(2, ".dirtydot")) == 1)

    tab = _tab(2, CPP)
    tdot = page.locator(tab + " .edtab-dot.on")
    check("the tab carries the dot, named", tdot.count() == 1 and tdot.get_attribute("aria-label") == "unsaved changes"
          and (tdot.get_attribute("title") or "").lower().startswith("unsaved changes"))
    style = ("e => { const d = e.querySelector('.edtab-dot'), x = e.querySelector('.edtab-x');"
             " return { dot: getComputedStyle(d, '::before').display, bg: getComputedStyle(d, '::before').backgroundColor,"
             " x: getComputedStyle(x).opacity, xv: getComputedStyle(x).visibility,"
             " dx: d.getBoundingClientRect().left, xr: x.getBoundingClientRect() }; }")
    page.locator(_lines(2)).hover()
    s = page.locator(tab).evaluate(style)
    check("on a dirty tab the dot stands where × is, in --text-2", s["dot"] != "none" and s["x"] == "0"
          and _rgb(s["bg"]) == text2 and s["xr"]["left"] <= s["dx"] + 5 + 4 <= s["xr"]["right"], str(s))
    shots = os.environ.get("HUD_MARKS_SHOTS")
    if shots:
        page.locator(_pane(2)).screenshot(path=str(Path(shots) / "dirty.png"))
    page.locator(tab).hover()
    s = page.locator(tab).evaluate(style)
    check("hovering the tab brings × back", s["dot"] == "none" and s["x"] == "1" and s["xv"] == "visible", str(s))
    page.locator(_lines(2)).hover()
    page.keyboard.press("Shift")
    focused = page.locator(tab).evaluate("e => { e.focus(); return e.matches(':focus-visible'); }")
    s = page.locator(tab).evaluate(style)
    check("focusing the tab from the keyboard brings × back", focused and s["dot"] == "none" and s["x"] == "1",
          f"focus-visible={focused} {s}")

    # Collapse the folders: the dot moves up to the outermost collapsed one.
    _toggle(page, until, 2, "src/robot", False)
    check("collapsing src/robot moves the dot to its row",
          _count(page, _dot(2, "src/robot")) == 1 and _count(page, _row(2, "file", CPP)) == 0
          and _count(page, _dot(2, "src")) == 0)
    _toggle(page, until, 2, "src", False)
    check("collapsing src too moves it to src's row",
          _count(page, _dot(2, "src")) == 1 and _count(page, _dot(2, "src/robot")) == 0
          and page.locator(_dot(2, "src")).get_attribute("aria-label") == "unsaved changes")
    _expand(page, until, 2, "src", "src/robot")
    check("expanding them gives it back to the file",
          _count(page, _dot(2, CPP)) == 1 and _count(page, _pane(2, ".dirtydot")) == 1)

    # A slow write (ED-2; the owner's VEX projects live on /mnt/c): until it
    # answers, the tab's slot holds a hollow ring in the dot's place.
    page.locator(_lines(2)).click()
    mock.world["put_delays"] = [1.5]
    try:
        page.keyboard.press("Control+s")
        ring = page.locator(tab + " .edtab-dot.saving")
        until(lambda: ring.count() == 1, timeout=1.5)
        page.locator(_lines(2)).hover()
        r = page.locator(tab).evaluate(
            "e => { const d = e.querySelector('.edtab-dot'), b = getComputedStyle(d, '::before'),"
            " x = e.querySelector('.edtab-x'); return { dot: b.display, bg: b.backgroundColor,"
            " border: b.borderTopColor, bw: parseFloat(b.borderTopWidth), x: getComputedStyle(x).opacity }; }")
        check("a save on its way: the slot holds a hollow --text-2 ring where × is, named \"saving\"",
              ring.count() == 1 and ring.get_attribute("aria-label") == "saving"
              and page.locator(tab).get_attribute("data-saving") == "true"
              and r["dot"] != "none" and _rgb(r["border"]) == text2 and r["bw"] > 0
              and r["bg"] in ("transparent", "rgba(0, 0, 0, 0)") and r["x"] == "0", str(r))
        check("…while the tree's dot stays: the file is still unsaved until the write answers",
              _count(page, _dot(2, CPP)) == 1)
        until(lambda: page.locator(tab).get_attribute("data-dirty") == "false"
              and page.locator(tab).get_attribute("data-saving") == "false", timeout=5)
    finally:
        mock.world.pop("put_delays", None)
    until(lambda: _count(page, _pane(2, ".dirtydot")) == 0, timeout=3)
    check("saving clears the tree's dot and the tab's (no ring left either)",
          "tuned at the lab" in mock.world["files"][CPP]["content"]
          and _count(page, _pane(2, ".dirtydot")) == 0 and page.locator(tab + " .edtab-dot.on").count() == 0
          and page.locator(tab + " .edtab-dot.saving").count() == 0)


def _two_pane_checks(page, mock, check, until):
    _expand(page, until, 2, "src", "src/robot")
    check("setup: drive.cpp opens in pane 2", _open(page, until, 2, CPP))
    check("setup: an edit", _type(page, until, 2, "\n// two panes"))
    until(lambda: _count(page, _dot(3, "src")) == 1, timeout=3)
    check("another pane's tree on the project marks it without opening it (collapsed: on src)",
          _count(page, _dot(3, "src")) == 1)
    _expand(page, until, 3, "src", "src/robot")
    check("and on the file once expanded", _count(page, _dot(3, CPP)) == 1 and _count(page, _dot(3, "src")) == 0)
    check("setup: drive.cpp opens in pane 3 too", _open(page, until, 3, CPP))
    check("both panes' tabs carry the dot",
          page.locator(_tab(2, CPP) + " .edtab-dot.on").count() == 1
          and page.locator(_tab(3, CPP) + " .edtab-dot.on").count() == 1)
    page.locator(_lines(3)).click()
    page.keyboard.press("Control+s")
    until(lambda: _count(page, '.dirtydot') == 0, timeout=5)
    check("one save from pane 3 clears both trees and both tabs",
          _count(page, ".dirtydot") == 0 and _count(page, ".edtab-dot.on") == 0)


# Pane 3 pinned to a project whose root is a symlink onto p1's folder: the
# same files, another root as written (#50's review, LOW).
LINKED = json.dumps({"preset": "cols3", "focused": 2,
                     "panes": [{"view": "chat"}, {"view": "file"}, {"view": "file", "projectId": "p3"},
                               {"view": "task"}]})


def _linked_root_checks(page, mock, check, until):
    _expand(page, until, 2, "src", "src/robot")
    check("setup: drive.cpp opens in pane 2 (through p1)", _open(page, until, 2, CPP))
    check("setup: pane 3 is on the linked project",
          page.locator(_pane(3, '[data-testid="file-config.json"]')).count() == 1)
    check("setup: an edit", _type(page, until, 2, "\n// through a link"))
    until(lambda: _count(page, _dot(3, "src")) == 1, timeout=3)
    check("a project whose root is a symlink marks the file too, by where its root resolves (root_real)",
          _count(page, _dot(3, "src")) == 1)
    _expand(page, until, 3, "src", "src/robot")
    check("…on the file once expanded", _count(page, _dot(3, CPP)) == 1 and _count(page, _dot(3, "src")) == 0)


FOCUSABLE = ("a[href], button, input, select, textarea, [tabindex]:not([tabindex='-1']), [contenteditable='true']")


def _card_checks(page, mock, check, until):
    _expand(page, until, 2, "src", "src/robot")
    check("setup: drive.cpp opens in pane 2", _open(page, until, 2, CPP))
    check("setup: an edit", _type(page, until, 2, "\n// under a card"))
    _toggle(page, until, 2, "src/robot", False)
    until(lambda: _count(page, _dot(2, "src/robot")) == 1, timeout=3)
    _approval(mock, "mkcard1")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    time.sleep(0.25)
    marks = page.evaluate("""sel => [...document.querySelectorAll('.dirtydot, .ficon, .ficon *, .edtab-dot, .chev')]
      .map(e => ({ focusable: e.matches(sel) || e.tabIndex >= 0, inert: !!e.closest('[inert]') }))""", FOCUSABLE)
    check("every dot, icon and chevron is unfocusable and behind the inert veil",
          bool(marks) and all(not m["focusable"] and m["inert"] for m in marks),
          f"{sum(m['focusable'] for m in marks)} focusable, {sum(not m['inert'] for m in marks)} not inert of {len(marks)}")
    stays = True
    for _ in range(6):
        page.keyboard.press("Tab")
        if not page.evaluate("() => !!document.activeElement?.closest('[data-testid=\"approval-card\"]')"):
            stays = False
    check("Tab stays on the card", stays)
    page.keyboard.press("Escape")
    body = until(lambda: mock.sent("POST", "/approvals/mkcard1") or None, timeout=4)
    check("Escape denies the card", bool(body) and body[-1].get("decision") == "deny")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    check("and the marks are as they were after it", _count(page, _dot(2, "src/robot")) == 1
          and page.locator(_tab(2, CPP)).get_attribute("data-dirty") == "true")


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
            marks_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until,
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
    print(f"all {v2.CHECKS} editor marks checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
