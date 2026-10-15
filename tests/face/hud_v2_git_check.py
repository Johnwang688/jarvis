"""Headless checks for the git gutter and the explorer's git colours (ED-9).

Free and hermetic: a `MockDaemon(0)` of its own and a browser context of its
own, under `hud_v2_check.guard` (no request to 8402/8403/8405, no CSP
violation). The world gains a repository for the run: HEAD's text for some
files (`world["git"]["base"]`) and a status (`files`, `dirs`); the real
routes' refusals are the daemon's (`tests/v2/editor_git_check.py`).

What it pins:
  - **the gutter follows the buffer**: a line typed (unsaved) shows an
    "added" bar in `--syn-string`; undoing it back to HEAD's text clears the
    gutter; a changed line is "modified" in `--syn-fn`; a removed one a
    wedge in `--red` at 60%; a clean file has none;
  - **HEAD moving** (a new commit id on the status poll) reads HEAD's text
    again, and the gutter follows it;
  - **never for an env file**: no HEAD text is asked for, no gutter drawn;
  - **the explorer's colours**: modified `--syn-fn`, untracked and added
    `--syn-string`, an ignored folder dimmed, a folder holding a change
    coloured, a clean file plain — and a status change on the poll recolours
    the row;
  - **no redraw per keystroke**: the tree's DOM does not change while typing
    in a file that is already dirty.

Run: PYTHONPATH=. .venv/bin/python tests/face/hud_v2_git_check.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.face.hud_v2_editor_check import TWO, WIDE, _fresh, _lines, _open, _tab, _type  # noqa: E402
from tests.face.hud_v2_editor_marks_check import _expand, _rgb, _token_rgb  # noqa: E402
from tests.face.hud_v2_layout_check import _boot, _pane  # noqa: E402

CALC = "def add(a, b):\n    return a + b\n"
README_HEAD = "# Title\n\nSome docs here.\n"


def _git(n: int, kind: str = "") -> str:
    return _pane(n, f'[data-testid="editor"] .margin-view-overlays .jgit{"-" + kind if kind else ""}')


def _count(page, sel: str) -> int:
    return page.locator(sel).count()


def _before(page, sel: str, prop: str) -> str:
    return page.locator(sel).first.evaluate(f"e => getComputedStyle(e, '::before').{prop}")


def _row(n: int, kind: str, path: str) -> str:
    return _pane(n, f'[data-testid="{kind}-{path}"]')


def git_checks(browser, mock, base, check, until, guard, init_script):
    print("\neditor git: the gutter and the explorer's colours")
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
    try:
        w["tree"][""].insert(0, {"name": "build", "kind": "dir", "size": 0, "mtime": 1757900040})
        w["tree"]["build"] = [{"name": "out.o", "kind": "file", "size": 3, "mtime": 1757900041}]
        w["files"]["build/out.o"] = {"content": "obj\n", "mtime": 1757900041}
        w["git"] = {
            "head": "c0ffee1", "branch": "main",
            "base": {"calc.py": CALC, "README.md": README_HEAD, ".env": "MOCK_KEY=old\n",
                     "src/main.py": "print('hi')\n"},
            "files": {"README.md": "modified", "notes.txt": "untracked", "src/main.py": "added"},
            "dirs": {"build": "ignored"},
        }
        keep = {k: dict(v) for k, v in w["files"].items()}
        _boot(page, mock, base, until)
        # Markdown is word-wrapped by default since ED-8, and a wrapped line
        # draws its gutter mark on each of its rows; these checks count marks
        # per line, so README.md is drawn unwrapped here.
        page.evaluate("localStorage.setItem('jarvis.hud.editor', JSON.stringify({ wordWrapMarkdown: 'off' }))")
        for section in (_explorer_checks, _gutter_checks, _head_checks, _env_checks):
            _fresh(page, mock, until, ws=TWO, keep_files=keep)
            try:
                section(page, mock, check, until)
            except Exception as e:  # a section that cannot run is a failure; the rest still run
                check(f"{section.__name__.strip('_')} ran to the end", False, str(e).splitlines()[0][:200])
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        check("no page errors in the editor git section", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()
        w.pop("git", None)
        w["files"].clear()
        w["files"].update(keep_files)
        w["tree"].clear()
        w["tree"].update(keep_tree)


# ---------------------------------------------------------------------------

def _explorer_checks(page, mock, check, until):
    readme = page.locator(_row(2, "file", "README.md"))
    until(lambda: readme.get_attribute("data-git") == "modified", timeout=5)
    tokens = {t: _token_rgb(page, t) for t in ("--syn-fn", "--syn-string", "--text")}
    color = lambda sel: _rgb(page.locator(sel + " .nm").first.evaluate("e => getComputedStyle(e).color"))
    check("a modified file's name is --syn-fn", readme.get_attribute("data-git") == "modified"
          and color(_row(2, "file", "README.md")) == tokens["--syn-fn"])
    check("…with the state in its tooltip", "modified" in (readme.get_attribute("title") or ""))
    check("an untracked file's name is --syn-string",
          page.locator(_row(2, "file", "notes.txt")).get_attribute("data-git") == "untracked"
          and color(_row(2, "file", "notes.txt")) == tokens["--syn-string"])
    check("a folder holding an added file is coloured as added",
          page.locator(_row(2, "dir", "src")).get_attribute("data-git") == "added"
          and color(_row(2, "dir", "src")) == tokens["--syn-string"])
    build = page.locator(_row(2, "dir", "build"))
    check("an ignored folder is dimmed", build.get_attribute("data-git") == "ignored"
          and float(build.locator(".nm").evaluate("e => getComputedStyle(e).opacity")) < 0.8)
    _expand(page, until, 2, "build")
    check("…and a file inside it too", page.locator(_row(2, "file", "build/out.o")).get_attribute("data-git") == "ignored")
    calc = page.locator(_row(2, "file", "calc.py"))
    check("a clean file is plain", calc.get_attribute("data-git") is None and color(_row(2, "file", "calc.py")) != tokens["--syn-fn"])
    check("the env file and the withheld one wear no git colour",
          page.locator(_row(2, "file", ".env")).get_attribute("data-git") is None
          and page.locator(_row(2, "file", "google_token.json")).get_attribute("data-git") is None)
    # The status moves (an agent wrote calc.py): the poll recolours its row.
    mock.world["git"]["files"]["calc.py"] = "modified"
    try:
        until(lambda: calc.get_attribute("data-git") == "modified", timeout=6)
        check("a status change on the poll recolours the row", calc.get_attribute("data-git") == "modified"
              and color(_row(2, "file", "calc.py")) == tokens["--syn-fn"])
    finally:
        mock.world["git"]["files"].pop("calc.py", None)


def _gutter_checks(page, mock, check, until):
    check("setup: calc.py opens in pane 2", _open(page, until, 2, "calc.py"))
    time.sleep(0.6)
    check("a file equal to HEAD has no gutter", _count(page, _git(2)) == 0)
    tree = _pane(2, '[data-testid="filetree"]')
    check("setup: an unsaved edit adds a line", _type(page, until, 2, "\n# tuned at the lab"))
    until(lambda: _count(page, _git(2, "add")) > 0, timeout=3)
    add = _git(2, "add")
    check("the gutter follows the unsaved edit: an added bar", _count(page, add) == 1
          and page.locator(_tab(2, "calc.py")).get_attribute("data-dirty") == "true")
    check("…in --syn-string", _rgb(_before(page, add, "backgroundColor")) == _token_rgb(page, "--syn-string"))
    if os.environ.get("HUD_GIT_SHOTS"):
        page.locator(_pane(2)).screenshot(path=str(Path(os.environ["HUD_GIT_SHOTS"]) / "added.png"))
    # No redraw of the tree per keystroke: the file is already dirty.
    page.evaluate("""sel => { const t = document.querySelector(sel); window.__treeMut = 0;
      window.__treeObs = new MutationObserver((m) => { window.__treeMut += m.length; });
      window.__treeObs.observe(t, { subtree: true, childList: true, attributes: true, characterData: true }); }""", tree)
    page.keyboard.type(" and again")
    time.sleep(0.4)
    muts = page.evaluate("() => { window.__treeObs.disconnect(); return window.__treeMut; }")
    check("typing in a dirty file changes nothing in the tree", muts == 0, f"{muts} mutations")
    for _ in range(40):
        if page.locator(_tab(2, "calc.py")).get_attribute("data-dirty") == "false":
            break
        page.keyboard.press("Control+z")
        time.sleep(0.05)
    until(lambda: _count(page, _git(2)) == 0, timeout=3)
    check("undo back to HEAD's text: the gutter is clean again",
          page.locator(_tab(2, "calc.py")).get_attribute("data-dirty") == "false" and _count(page, _git(2)) == 0)

    check("setup: README.md opens in pane 2", _open(page, until, 2, "README.md"))
    until(lambda: _count(page, _git(2, "mod")) > 0, timeout=3)
    mod = _git(2, "mod")
    check("a line changed since HEAD is modified, in --syn-fn, on its own line",
          _count(page, mod) == 1 and _rgb(_before(page, mod, "backgroundColor")) == _token_rgb(page, "--syn-fn"))
    # Remove the first line: a deletion marker, --red at 60%.
    page.locator(_lines(2)).click()
    page.keyboard.press("Control+Home")
    page.keyboard.press("Shift+ArrowDown")
    page.keyboard.press("Delete")
    until(lambda: _count(page, _git(2, "del")) > 0, timeout=3)
    shots = os.environ.get("HUD_GIT_SHOTS")
    if shots:
        page.locator(_pane(2)).screenshot(path=str(Path(shots) / "gutter.png"))
    dele = _git(2, "del")
    check("a removed line is a wedge in --red at 60%",
          _count(page, dele) == 1 and _rgb(_before(page, dele, "borderLeftColor")) == _token_rgb(page, "--red")
          and abs(float(_before(page, dele, "opacity")) - 0.6) < 0.01)
    check("nothing in the gutter is amber", all(
        not _warm_amber(_rgb(page.locator(s).first.evaluate("e => getComputedStyle(e, '::before').backgroundColor")))
        for s in (mod,) if _count(page, s)))


def _warm_amber(rgb) -> bool:
    """Amber: hue 25°–65° at HSL saturation ≥ 0.35 (editorTheme.ts's band)."""
    if not rgb:
        return False
    r, g, b = (c / 255 for c in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    d, lum = mx - mn, (mx + mn) / 2
    if d == 0:
        return False
    s = d / (1 - abs(2 * lum - 1))
    h = (((g - b) / d) % 6 if mx == r else (b - r) / d + 2 if mx == g else (r - g) / d + 4) * 60 % 360
    return s >= 0.35 and 25 <= h <= 65


def _head_checks(page, mock, check, until):
    check("setup: calc.py opens in pane 2", _open(page, until, 2, "calc.py"))
    time.sleep(0.6)
    check("setup: no gutter at HEAD c0ffee1", _count(page, _git(2)) == 0)
    g = mock.world["git"]
    old = (g["head"], g["base"]["calc.py"])
    try:
        # A commit elsewhere: HEAD moves, and HEAD's calc.py lacks the return.
        g["base"]["calc.py"] = "def add(a, b):\n    pass\n"
        g["head"] = "c0ffee2"
        until(lambda: _count(page, _git(2, "mod")) > 0, timeout=7)
        check("HEAD moving on the status poll reads HEAD's text again; the gutter follows",
              _count(page, _git(2, "mod")) == 1)
    finally:
        g["head"], g["base"]["calc.py"] = old


def _env_checks(page, mock, check, until):
    before = getattr(mock, "git_base_calls", 0)
    check("setup: .env opens in pane 2", _open(page, until, 2, ".env"))
    check("setup: an edit to the env file", _type(page, until, 2, "\nMORE=1"))
    time.sleep(0.8)
    check("an env file: HEAD's text is never asked for, and no gutter is drawn",
          getattr(mock, "git_base_calls", 0) == before and _count(page, _git(2)) == 0,
          f"{getattr(mock, 'git_base_calls', 0) - before} base reads")


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
            git_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until, v2.guard,
                       v2.FAKE_RECOGNIZER)
            browser.close()
    finally:
        mock.stop()
    print()
    if v2.FAILURES:
        print(f"{len(v2.FAILURES)} of {v2.CHECKS} checks FAILED:")
        for f in v2.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {v2.CHECKS} editor git checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
