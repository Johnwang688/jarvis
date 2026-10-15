"""Headless checks for the Diff pane's uncommitted changes (2026-10-12).

The owner: "the Diff doesn't work right now it just says to select a Task" —
and yes to showing the project's uncommitted changes, like VS Code's Source
Control, when no task is selected.

Free and hermetic: a `MockDaemon(0)` of its own and a browser context of its
own, under `hud_v2_check.guard` (no request to 8402/8403/8405, no CSP
violation). The world gains a repository for the run (`world["git"]`: HEAD's
text, a status, `states` for a binary file); the real routes' refusals are
the daemon's (`tests/v2/editor_git_check.py`).

What it pins:
  - **the mode**: with no task, a Diff pane says "Uncommitted changes in
    <project>" and lists them; a task picked shows the task's diff as before;
    the pane's project picker pins a project (over the task) and Follow
    gives the task back; a thread picked gives the chat's project back;
  - **the rows**: modified, untracked, deleted, renamed, binary, an env file
    and an untracked folder, each with its letter; ignored entries never;
  - **the sides**: HEAD's text on the left; on the right the file as read,
    or — once it is open in a File pane — **the buffer's own model**, so an
    unsaved edit shows and typing updates it; a buffer the File pane lets go
    of falls back to a read with no page error; a deletion's right side is
    empty; binary says so;
  - **env files are listed and never diffed**: no `git/base` and no file
    read is ever made for one (the guard-removal check bites here);
  - **Open** opens the file in the drawn File pane, the Diff pane stays;
  - **under a card** the picker, Open and ↻ are disabled and a row click
    does nothing;
  - **no repository** and **no changes** are said in a sentence.

The review of PR #67 added:
  - **L1**: a row picked and the file opened and edited at once, with no
    wait — the diff swaps from the read copy to the buffer while Monaco may
    still be computing — raises no page error (`no diff result available`);
  - **L2**: HEAD is asked before the file is read: `link.txt`, a name no env
    rule catches whose HEAD text the daemon refuses as an env file (a link to
    `.env`), is never read and no model holds it;
  - **L3**: a Diff pane never shows another task than a drawn Task pane: a
    thread picked leaves both on the task, the caption says why and the
    picker is off; the Task pane switched away gives the chat's project back.

The Diff follow-ups (2026-10-12) added:
  - **the list at scale**: at git/status's 20,000-row cap only a window of
    rows is in the DOM, and typing in the chat box beside it costs under
    15 ms a character (~100 ms with every row drawn); the last row is reached
    by scrolling, at 100% and 160%; the keys move the selection, which holds
    and moves while its row is scrolled out of the window; at 160% Page Down
    and End clip nothing of their row (review of PR #73, NIT);
  - **a held key** (review of PR #73, MED): ↓ held over 60 rows against a
    `git/base` that takes 0.5 s sends a handful of requests, not 60, and the
    last row's diff shows promptly;
  - **clear task**: in the 2×2 grid with a Task pane, the Task pane's header
    unpicks the task, and the Diff pane shows its project's changes again
    (disabled under a card); a task picked again holds it again;
  - **ED-8's settings** draw the uncommitted-changes diff, live;
  - **closing a diff mid-compute**: the task diff and Compare each closed
    while their diff waits on a busy editor worker raise no page error
    (`no diff result available`) and leave the buffer's edit, dirty state
    and undo as they were.

Run: PYTHONPATH=. .venv/bin/python tests/face/hud_v2_scm_check.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.face.hud_v2_editor_check import WIDE, _fresh, _lines, _open, _tab, _text, _type  # noqa: E402
from tests.face.hud_v2_layout_check import _boot, _pane, _show, expand  # noqa: E402

WS = json.dumps({"preset": "cols3", "focused": 2,
                 "panes": [{"view": "chat"}, {"view": "file"}, {"view": "diff"}]})
# L3: a Task pane beside the Diff pane.
WS_TASK = json.dumps({"preset": "cols3", "focused": 2,
                      "panes": [{"view": "chat"}, {"view": "task"}, {"view": "diff"}]})
CALC_HEAD = "def add(a, b):\n    return a - b\n"
D = 3  # the Diff pane
# The Diff follow-ups (2026-10-12): chat and Diff side by side (the list at
# scale, measured the PR #67 reviewer's way), and the 2×2 grid with a Task pane.
WS_SCALE = json.dumps({"preset": "cols2", "focused": 1, "panes": [{"view": "chat"}, {"view": "diff"}]})
WS_GRID4 = json.dumps({"preset": "grid4", "focused": 1,
                       "panes": [{"view": "chat"}, {"view": "diff"}, {"view": "file"}, {"view": "task"}]})
SCALE_N = 20000  # git/status's cap
# A task-diff file whose diff keeps Monaco's one editor worker busy for
# seconds (every line differs: the line diff runs to its 5 s limit), so a diff
# opened after it is still waiting on the worker when it closes.
HUGE_LINES = 40000
# The run's page errors, for the checks that look at their own window of them.
ERRORS: list[str] = []


def _tid(t: str) -> str:
    return f'[data-testid="{t}"]'


def _in(t: str) -> str:
    return _pane(D, _tid(t))


def _side(page, which: str) -> str:
    loc = page.locator(_pane(D, f'[data-testid="scm-editor"] .editor.{which} .view-lines'))
    return loc.first.inner_text().replace(" ", " ") if loc.count() else ""


def _pick(page, until, path: str) -> None:
    row = page.locator(_in(f"scm-{path}"))
    until(lambda: row.count() > 0, timeout=6)
    row.click()
    until(lambda: page.locator(_in("scm-path")).inner_text() == path, timeout=3)


def _diffed(page, until, before: str, after: str, timeout: float = 6) -> bool:
    return bool(until(lambda: before in _side(page, "original") and after in _side(page, "modified"),
                      timeout=timeout))


def scm_checks(browser, mock, base, check, until, guard, init_script, only: str = ""):
    print("\nthe Diff pane: a project's uncommitted changes")
    ctx = browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]}, permissions=["microphone"])
    guard(ctx)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    ERRORS.clear()
    page.on("pageerror", lambda e: ERRORS.append(str(e)))
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url))
    page.set_default_timeout(5000)
    w = mock.world
    keep_files = {k: dict(v) for k, v in w["files"].items()}
    keep_kinds = dict(w["kinds"])
    try:
        w["files"]["renamed.py"] = {"content": "def new():\n    pass\n", "mtime": 1757900050}
        w["files"]["blob.bin"] = {"content": "ab\x00cd", "mtime": 1757900051}
        # L2: a name no env rule catches whose target the daemon judges an env
        # file (a link to `.env`): its HEAD text is refused, and it is never read.
        w["files"]["link.txt"] = {"content": "MOCK_LINKED=fake-not-a-secret\n", "mtime": 1757900052}
        w["kinds"]["link.txt"] = "env"
        w["git"] = {
            "head": "c0ffee1", "branch": "main",
            "base": {"calc.py": CALC_HEAD, ".env": "MOCK_KEY=old\n", "gone.txt": "an old line\n",
                     "renamed.py": "def old():\n    pass\n", "blob.bin": "", "link.txt": "MOCK_LINKED=old\n"},
            "files": {"calc.py": "modified", "notes.txt": "untracked", "gone.txt": "deleted",
                      "renamed.py": "renamed", "blob.bin": "modified", ".env": "modified",
                      "link.txt": "modified", "debug.log": "ignored"},
            "dirs": {"newdir": "untracked", "build": "ignored"},
            "states": {"blob.bin": "binary"},
        }
        keep = {k: dict(v) for k, v in w["files"].items()}
        _boot(page, mock, base, until)
        for section, ws in ((_list_checks, WS), (_sides_checks, WS), (_env_checks, WS), (_link_checks, WS),
                            (_live_checks, WS), (_swap_checks, WS), (_open_checks, WS), (_mode_checks, WS),
                            (_task_pane_checks, WS_TASK), (_card_checks, WS), (_empty_checks, WS),
                            (_scale_checks, WS_SCALE), (_hold_checks, WS), (_clear_task_checks, WS_GRID4),
                            (_settings_checks, WS), (_midcompute_checks, WS)):
            if only and only not in section.__name__:
                continue
            _fresh(page, mock, until, ws=ws, keep_files=keep)
            try:
                section(page, mock, check, until, requests)
            except Exception as e:  # a section that cannot run is a failure; the rest still run
                check(f"{section.__name__.strip('_')} ran to the end", False, str(e).splitlines()[0][:200])
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        check("no page errors in the Diff pane section", not ERRORS, "; ".join(ERRORS[:3]))
    finally:
        ctx.close()
        w.pop("git", None)
        w["files"].clear()
        w["files"].update(keep_files)
        w["kinds"].clear()
        w["kinds"].update(keep_kinds)


# ---------------------------------------------------------------------------

def _what(page) -> str:
    """The caption's first half: what the Diff pane shows."""
    loc = page.locator(_in("diff-mode-what"))
    return loc.inner_text().strip() if loc.count() else ""


def _why(page) -> str:
    loc = page.locator(_in("diff-mode-why"))
    return loc.inner_text().strip(" ·") if loc.count() else ""


def _list_checks(page, mock, check, until, requests):
    mode = page.locator(_in("diff-mode"))
    until(lambda: mode.count() > 0 and mode.get_attribute("data-mode") == "project", timeout=6)
    check("with no task, the Diff pane shows its project's uncommitted changes",
          mode.get_attribute("data-mode") == "project"
          and _what(page) == "Uncommitted changes in jarvis", mode.inner_text())
    check("…and the caption says why: the chat's project", _why(page) == "the chat's project", _why(page))
    check("…not the old 'pick a task'", "Pick a task" not in page.locator(_pane(D)).inner_text())
    summary = page.locator(_in("scm-summary"))
    until(lambda: "uncommitted" in summary.inner_text(), timeout=6)
    check("the line above the list says how many, and on which branch",
          summary.inner_text().strip() == "8 uncommitted changes on main", summary.inner_text())
    letters = {"calc.py": "M", "notes.txt": "U", "gone.txt": "D", "renamed.py": "R", "blob.bin": "M",
               ".env": "M", "link.txt": "M", "newdir": "U"}
    got = {p: (page.locator(_in(f"scm-letter-{p}")).inner_text().strip()
               if page.locator(_in(f"scm-letter-{p}")).count() else None) for p in letters}
    check("each changed file wears its letter: modified, untracked, deleted, renamed, binary, env, a folder",
          got == letters, str(got))
    check("ignored files and folders are not listed",
          page.locator(_in("scm-debug.log")).count() == 0 and page.locator(_in("scm-build")).count() == 0)
    check("a row carries its git state for the explorer's colours, and the editor's file icon",
          page.locator(_in("scm-calc.py")).get_attribute("data-git") == "modified"
          and page.locator(_in("scm-calc.py") + " .ficon").count() == 1)
    check("the env file is listed, marked as one", page.locator(_in("scm-.env")).get_attribute("data-env") == "true")
    check("an untracked folder reads as a folder", page.locator(_in("scm-newdir")).inner_text().strip().startswith("newdir/"))
    _pick(page, until, "newdir")
    check("a folder says why it has no diff", page.locator(_in("scm-folder")).count() == 1)


def _sides_checks(page, mock, check, until, requests):
    _pick(page, until, "calc.py")
    check("a modified file: HEAD's text on the left, the file on the right",
          _diffed(page, until, "return a - b", "return a + b"),
          f"{_side(page, 'original')!r} | {_side(page, 'modified')!r}")
    host = page.locator(_in("scm-editor"))
    check("…read from disk while no File pane has it open", host.get_attribute("data-live") == "false")
    models = page.evaluate("window.__hud.monacoModels()")
    check("…both sides jarvis-diff: models of this view",
          any(u.startswith("jarvis-diff:") and "/calc.py?original" in u for u in models)
          and any(u.startswith("jarvis-diff:") and "/calc.py?modified" in u for u in models), str(models))
    check("…with Open and ↻ offered", page.locator(_in("scm-open")).is_enabled()
          and page.locator(_in("scm-refresh")).is_enabled())
    _pick(page, until, "notes.txt")
    check("an untracked file: an empty left side, the file on the right",
          _diffed(page, until, "", "a") and _side(page, "original").strip() == "",
          f"{_side(page, 'original')!r} | {_side(page, 'modified')!r}")
    _pick(page, until, "gone.txt")
    check("a deleted file: HEAD's text on the left, an empty right side",
          _diffed(page, until, "an old line", "") and _side(page, "modified").strip() == "",
          f"{_side(page, 'original')!r} | {_side(page, 'modified')!r}")
    check("…and no Open for a file that is not there", page.locator(_in("scm-open")).count() == 0)
    _pick(page, until, "renamed.py")
    check("a renamed file: its source's HEAD text on the left",
          _diffed(page, until, "def old", "def new"))
    _pick(page, until, "blob.bin")
    until(lambda: page.locator(_in("scm-note")).count() > 0, timeout=4)
    check("a binary file says so instead of a diff",
          "binary" in page.locator(_in("scm-note")).inner_text().lower()
          and page.locator(_in("scm-editor")).count() == 0)
    shown = page.evaluate("window.__hud.monacoModels()")
    check("a file's models go with its view (none left for calc.py or notes.txt)",
          not any(("/calc.py?" in u or "/notes.txt?" in u) and u.startswith("jarvis-diff:") for u in shown), str(shown))


def _env_checks(page, mock, check, until, requests):
    start = len(requests)
    _pick(page, until, ".env")
    until(lambda: page.locator(_in("scm-env")).count() > 0, timeout=3)
    check("an env file is not diffed: it says so", page.locator(_in("scm-env")).count() == 1
          and "aren't diffed" in page.locator(_in("scm-env")).inner_text()
          and page.locator(_in("scm-editor")).count() == 0)
    check("…and offers Open in the editor", page.locator(_in("scm-open")).is_enabled())
    check("…and ↻ is not offered for it", page.locator(_in("scm-refresh")).count() == 0)
    time.sleep(0.6)
    asked = [u for u in requests[start:] if ("path=.env" in u) and ("/git/base" in u or "/file?" in u)]
    check("no git/base and no file read is ever made for an env file", not asked, str(asked))
    models = page.evaluate("window.__hud.monacoModels()")
    check("…and no model holds it", not any("/.env" in u for u in models), str(models))


def _link_checks(page, mock, check, until, requests):
    """L2 (review of PR #67): HEAD is asked before the file is read. `link.txt`
    passes every name rule; the daemon judges where it leads (an env file)
    and refuses its HEAD text — so the file is never read, nor put in a model.
    With the read no longer waiting on HEAD's answer (`baseOk` dropped), the
    read goes out first and this fails."""
    start = len(requests)
    _pick(page, until, "link.txt")
    until(lambda: page.locator(_in("scm-env")).count() > 0, timeout=4)
    check("L2: a file the daemon refuses as an env file says so, and is not diffed",
          page.locator(_in("scm-env")).count() == 1 and page.locator(_in("scm-editor")).count() == 0)
    time.sleep(0.8)
    based = [u for u in requests[start:] if "/git/base" in u and "path=link.txt" in u]
    read = [u for u in requests[start:] if "/file?" in u and "path=link.txt" in u]
    check("L2: HEAD was asked, and the file was never read", bool(based) and not read, str(read or based))
    models = page.evaluate("window.__hud.monacoModels()")
    check("L2: …and no model holds it", not any("link.txt" in u for u in models), str(models))


def _live_checks(page, mock, check, until, requests):
    _pick(page, until, "calc.py")
    host = page.locator(_in("scm-editor"))
    check("setup: calc.py diffed from disk", _diffed(page, until, "return a - b", "return a + b")
          and host.get_attribute("data-live") == "false")
    check("setup: calc.py opens in the File pane", _open(page, until, 2, "calc.py"))
    until(lambda: page.locator(_in("scm-editor")).get_attribute("data-live") == "true", timeout=4)
    check("once the file is open, the right side is the buffer itself",
          page.locator(_in("scm-editor")).get_attribute("data-live") == "true"
          and _diffed(page, until, "return a - b", "return a + b"))
    models = page.evaluate("window.__hud.monacoModels()")
    check("…its model is the buffer's (file:), never a jarvis-diff: copy",
          any(u.startswith("jarvis-diff:") and "/calc.py?original" in u for u in models)
          and not any(u.startswith("jarvis-diff:") and "/calc.py?modified" in u for u in models), str(models))
    # The File pane lets go of the buffer (its tab closed — a tree click now
    # opens a new tab instead of replacing a preview, so the tab is closed
    # explicitly with its ×): the registry disposes the model, and the diff
    # reads the file again.
    page.locator(_pane(2, _tid("editor-tab-close-calc.py"))).click()
    until(lambda: page.locator(_pane(2, _tid("editor-tab-calc.py"))).count() == 0, timeout=4)
    until(lambda: page.locator(_in("scm-editor")).get_attribute("data-live") == "false", timeout=4)
    check("a buffer let go of falls back to a read, still diffed",
          page.locator(_in("scm-editor")).get_attribute("data-live") == "false"
          and _diffed(page, until, "return a - b", "return a + b"))
    check("setup: calc.py opens again", _open(page, until, 2, "calc.py"))
    check("setup: an unsaved edit in the File pane", _type(page, until, 2, "\n# tuned at the lab"))
    until(lambda: page.locator(_in("scm-editor")).get_attribute("data-live") == "true", timeout=4)
    check("an open dirty buffer shows its unsaved text on the right",
          _diffed(page, until, "return a - b", "tuned at the lab"),
          _side(page, "modified"))
    page.keyboard.type(" and again")
    check("typing in the File pane updates the diff", _diffed(page, until, "return a - b", "and again"),
          _side(page, "modified"))
    check("the edit is still unsaved (the diff never saves)",
          page.locator(_tab(2, "calc.py")).get_attribute("data-dirty") == "true")
    # Read-only: what is typed into the diff's right side goes nowhere.
    page.locator(_pane(D, '[data-testid="scm-editor"] .editor.modified .view-lines')).first.click()
    page.keyboard.press("Control+End")
    page.keyboard.type("QQQ")
    time.sleep(0.4)
    check("the diff's right side is read-only: typing there changes neither it nor the buffer",
          "QQQ" not in _side(page, "modified")
          and "QQQ" not in page.locator(_pane(2, '[data-testid="editor"] .view-lines')).first.inner_text(),
          _side(page, "modified"))


def _swap_checks(page, mock, check, until, requests):
    """L1 (review of PR #67): pick a row, then at once open the file and edit
    it — the diff swaps from the read copy to the buffer while Monaco may
    still be computing the first one. Monaco never released the view model it
    made from `{original, modified}`, so that computation was never cancelled
    and threw `no diff result available`, uncaught (3/3 in review). Twice,
    nothing waited for, page errors watched."""
    before = len(ERRORS)
    for path in ("calc.py", "notes.txt"):
        row = page.locator(_in(f"scm-{path}"))
        until(lambda: row.count() > 0, timeout=6)
        row.click()
        page.locator(_pane(2, _tid(f"file-{path}"))).click()
        until(lambda: page.locator(_pane(2, '[data-testid="editor"] .view-lines')).count() > 0, timeout=4)
        page.locator(_pane(2, '[data-testid="editor"] .view-lines')).first.click()
        page.keyboard.press("Control+End")
        page.keyboard.type("\n# swapped")
        until(lambda: page.locator(_in("scm-editor")).get_attribute("data-live") == "true", timeout=4)
    time.sleep(1.5)
    new = ERRORS[before:]
    check("L1: a diff swapped to the buffer mid-computation throws nothing", not new, "; ".join(new[:3]))
    check("L1: …and ends on the live buffer, the edit shown", "swapped" in _side(page, "modified"),
          _side(page, "modified"))


def _open_checks(page, mock, check, until, requests):
    _pick(page, until, "notes.txt")
    page.locator(_in("scm-open")).click()
    until(lambda: page.locator(_pane(2, _tid("file-path"))).inner_text() == "notes.txt", timeout=4)
    check("Open shows the file in the drawn File pane",
          page.locator(_pane(2, _tid("file-path"))).inner_text() == "notes.txt")
    check("…and the Diff pane stays a Diff pane",
          page.locator(_pane(D)).get_attribute("data-view") == "diff" and page.locator(_in("scm")).count() == 1)
    _boot(page, mock, None, until, reload=True)
    until(lambda: page.locator(_in("scm-path")).count() > 0
          and page.locator(_in("scm-path")).inner_text() == "notes.txt", timeout=6)
    check("the file picked is the pane's: it is picked again after a reload",
          page.locator(_in("scm-path")).inner_text() == "notes.txt"
          and _diffed(page, until, "", "a"))


def _mode_checks(page, mock, check, until, requests):
    mode = page.locator(_in("diff-mode"))
    until(lambda: mode.get_attribute("data-mode") == "project", timeout=6)
    expand(page, until, "p1", "task-k1")
    page.locator(_tid("task-k1")).click()
    until(lambda: mode.get_attribute("data-mode") == "task", timeout=4)
    until(lambda: page.locator(_in("diff-calc.py")).count() > 0, timeout=4)
    check("a task picked still shows the task's diff, as before",
          mode.get_attribute("data-mode") == "task" and page.locator(_in("difflist")).count() == 1
          and page.locator(_in("diff-test_calc.py")).count() == 1 and page.locator(_in("scm")).count() == 0)
    check("…and the heading names the task", _what(page).startswith("Task k1"), mode.inner_text())
    check("…and why: the task picked last (no Task pane is drawn)",
          _why(page) == "the task picked last" and mode.get_attribute("data-why") == "focus", _why(page))
    picker = page.locator(_tid("pane-3-diff-project"))
    check("the pane's picker offers Follow (the task) and every project",
          "task" in picker.locator("option").first.inner_text() and picker.locator("option").count() >= 3)
    picker.select_option("p2")
    until(lambda: mode.get_attribute("data-mode") == "project", timeout=4)
    check("a project picked in the pane shows its uncommitted changes (the window leaves the task)",
          _what(page) == "Uncommitted changes in schoolwork"
          and page.locator(_in("scm")).get_attribute("data-project") == "p2"
          and picker.input_value() == "p2" and _why(page) == "this pane's project")
    page.locator(_tid("task-k1")).click()
    until(lambda: mode.get_attribute("data-mode") == "task", timeout=4)
    check("a task picked again beats the pin: its diff, Follow shown",
          mode.get_attribute("data-mode") == "task" and picker.input_value() == "")
    expand(page, until, "p1", "thread-t1")
    page.locator(_tid("thread-t1")).click()
    until(lambda: mode.get_attribute("data-mode") == "project", timeout=4)
    check("a thread picked: the pinned project's uncommitted changes again",
          _what(page) == "Uncommitted changes in schoolwork" and picker.input_value() == "p2")
    picker.select_option("")
    until(lambda: _what(page) == "Uncommitted changes in jarvis", timeout=4)
    check("Follow: the chat's project's uncommitted changes",
          _what(page) == "Uncommitted changes in jarvis" and picker.input_value() == ""
          and _why(page) == "the chat's project")


def _task_pane_checks(page, mock, check, until, requests):
    """L3 (review of PR #67): a Diff pane never shows another task than a
    drawn Task pane. Chat, Task and Diff side by side: a task picked shows in
    both; a thread picked since leaves the Task pane on the task, and the Diff
    with it — the caption says it is the Task pane's task and the picker is
    off; the Task pane switched away, the Diff follows the chat again."""
    mode = page.locator(_in("diff-mode"))
    until(lambda: mode.count() > 0, timeout=6)
    expand(page, until, "p1", "task-k1")
    page.locator(_tid("task-k1")).click()
    until(lambda: mode.get_attribute("data-mode") == "task", timeout=4)
    until(lambda: page.locator(_pane(2, _tid("task-view"))).count() > 0, timeout=4)
    until(lambda: page.locator(_in("diff-calc.py")).count() > 0, timeout=4)
    check("L3: a task picked shows in the Task pane and its diff in the Diff pane",
          mode.get_attribute("data-mode") == "task" and page.locator(_in("diff-calc.py")).count() > 0)
    expand(page, until, "p1", "thread-t1")
    page.locator(_tid("thread-t1")).click()
    time.sleep(0.6)
    check("L3: a thread picked since: the Task pane still shows the task — and so does the Diff",
          page.locator(_pane(2, _tid("task-view"))).count() > 0 and mode.get_attribute("data-mode") == "task"
          and page.locator(_in("diff-calc.py")).count() > 0 and page.locator(_in("scm")).count() == 0,
          mode.inner_text())
    check("L3: …and the caption says it is the Task pane's task",
          mode.get_attribute("data-why") == "pane" and _why(page) == "the Task pane's task", _why(page))
    picker = page.locator(_tid("pane-3-diff-project"))
    check("L3: …and the picker is off while the Task pane holds the task, saying why",
          picker.is_disabled() and "Task pane" in (picker.get_attribute("title") or ""))
    _show(page, until, 2, "file")
    until(lambda: mode.get_attribute("data-mode") == "project", timeout=4)
    check("L3: the Task pane switched away, the Diff follows the chat's project again",
          _what(page) == "Uncommitted changes in jarvis" and _why(page) == "the chat's project"
          and picker.is_enabled(), mode.inner_text())


def _card_checks(page, mock, check, until, requests):
    _pick(page, until, "calc.py")
    check("setup: calc.py diffed", _diffed(page, until, "return a - b", "return a + b"))
    mock.emit("approval_requested", {
        "req_id": "scm1", "code": "SC01", "tool": "run_command", "args": {"command": "ls"},
        "command": "ls", "origin": "task: scm", "allowlistable": True, "timeout_s": 120,
    })
    until(lambda: page.locator(_tid("approval-card")).count() > 0, timeout=4)
    time.sleep(0.3)
    off = {t: page.locator(sel).is_disabled() for t, sel in (
        ("picker", _tid("pane-3-diff-project")), ("open", _in("scm-open")), ("refresh", _in("scm-refresh")))}
    check("under a card the Diff pane's picker, Open and ↻ are disabled", all(off.values()), str(off))
    inert = page.evaluate("() => !!document.querySelector('[data-testid=\"scm\"]').closest('[inert]')")
    check("…and inert behind the veil", inert)
    try:
        page.locator(_in("scm-notes.txt")).click(force=True, timeout=500)
    except Exception:
        pass
    time.sleep(0.3)
    check("a row clicked under the card changes nothing", page.locator(_in("scm-path")).inner_text() == "calc.py")
    page.keyboard.press("Escape")
    until(lambda: page.locator(_tid("approval-card")).count() == 0, timeout=4)
    check("answered, the controls come back",
          bool(until(lambda: page.locator(_in("scm-open")).is_enabled(), timeout=3)))


def _empty_checks(page, mock, check, until, requests):
    g = mock.world["git"]
    files, dirs = g["files"], g["dirs"]
    try:
        g["files"], g["dirs"] = {"debug.log": "ignored"}, {"build": "ignored"}
        _boot(page, mock, None, until, reload=True)
        summary = page.locator(_in("scm-summary"))
        until(lambda: summary.count() > 0 and "No uncommitted" in summary.inner_text(), timeout=8)
        check("no changes: said in a sentence", summary.inner_text().strip() == "No uncommitted changes on main.",
              summary.inner_text())
        mock.world["git"] = None
        until(lambda: "Not a git repository" in summary.inner_text(), timeout=8)
        check("outside a repository: said in a sentence (on the poll)",
              "Not a git repository" in summary.inner_text(), summary.inner_text())
    finally:
        mock.world["git"] = g
        g["files"], g["dirs"] = files, dirs


# ---------------------------------------------------------------------------
# The Diff follow-ups (2026-10-12)

def _in_view(page, list_sel: str, row_sel: str, tol: float = 1.0) -> bool:
    """The row is drawn and wholly inside the list's scrolled view (to `tol` drawn px)."""
    lb = page.locator(list_sel).bounding_box()
    loc = page.locator(row_sel)
    rb = loc.bounding_box() if loc.count() == 1 else None
    if not lb or not rb:
        return False
    return rb["y"] >= lb["y"] - tol and rb["y"] + rb["height"] <= lb["y"] + lb["height"] + tol


def _clip(page, list_sel: str, row_sel: str) -> float:
    """How many drawn px of the row the list's edges cut off (0: none)."""
    lb = page.locator(list_sel).bounding_box()
    rb = page.locator(row_sel).bounding_box() if page.locator(row_sel).count() == 1 else None
    if not lb or not rb:
        return float("inf")
    return max(0.0, lb["y"] - rb["y"]) + max(0.0, rb["y"] + rb["height"] - lb["y"] - lb["height"])


def _scale_checks(page, mock, check, until, requests):
    """Item 1: git/status's 20,000-row cap. Drawn whole, the list made every
    keystroke in the window cost ~100 ms of style work (the PR #67 reviewer's
    measure: chat and Diff side by side, 9.8 ms with the list hidden). Only a
    window of rows is drawn now; the keys move a selection that is a path, so
    it holds — and moves on — while its row is scrolled out of the window."""
    from tests.face.hud_v2_layout_check import _set_storage
    L = 2  # the Diff pane here
    w = mock.world
    g = w["git"]
    name = lambda i: f"src/dir{i // 100:03d}/file{i:05d}.py"  # noqa: E731
    keep = {k: dict(v) for k, v in w["files"].items()}
    rows_sel = _pane(L, _tid("scm-rows"))
    path = lambda: page.locator(_pane(L, _tid("scm-path"))).inner_text()  # noqa: E731
    row = lambda i: _pane(L, _tid(f"scm-{name(i)}"))  # noqa: E731
    selected = lambda i: "sel" in (page.locator(row(i)).get_attribute("class") or "").split()  # noqa: E731
    try:
        w["git"] = {"head": "c0ffee1", "branch": "main", "base": {}, "dirs": {},
                    "files": {name(i): "modified" for i in range(SCALE_N)}}
        _fresh(page, mock, until, ws=WS_SCALE, keep_files=keep)
        summary = page.locator(_pane(L, _tid("scm-summary")))
        until(lambda: summary.count() > 0 and "uncommitted" in summary.inner_text(), timeout=30)
        rows = page.locator(rows_sel)
        drawn = page.locator(_pane(L, ".scmrow")).count()
        check(f"{SCALE_N:,} changed files: the list says so, and draws only a window of rows",
              summary.inner_text().startswith(f"{SCALE_N} uncommitted changes")
              and rows.count() == 1 and rows.get_attribute("data-count") == str(SCALE_N) and 0 < drawn <= 100,
              f"{drawn} rows drawn; {summary.inner_text()!r}")

        box = page.locator(_pane(1, _tid("input")))
        text = "the quick brown fox jumps over the lazy dog " * 2
        costs = []
        for _ in range(2):
            box.click()
            box.fill("")
            t0 = time.time()
            page.keyboard.type(text)
            until(lambda: text.strip() in box.input_value(), timeout=60)
            costs.append((time.time() - t0) / len(text) * 1000)
        box.fill("")
        print(f"    typing in the chat box beside {SCALE_N:,} changed files: "
              + ", ".join(f"{c:.1f}" for c in costs) + " ms a character")
        check("typing in the chat box beside them costs under 15 ms a character (~100 ms with every row drawn)",
              min(costs) < 15, f"{min(costs):.1f} ms")

        rows.evaluate("e => { e.scrollTop = e.scrollHeight; }")
        until(lambda: page.locator(row(SCALE_N - 1)).count() > 0, timeout=4)
        check("scrolled to the bottom, the last row is drawn and in view",
              _in_view(page, rows_sel, row(SCALE_N - 1)))
        page.locator(row(SCALE_N - 1)).click()
        until(lambda: path() == name(SCALE_N - 1), timeout=4)
        page.keyboard.press("ArrowUp")
        until(lambda: path() == name(SCALE_N - 2), timeout=4)
        check("a row clicked gives the list the keys: ↑ selects the row above",
              path() == name(SCALE_N - 2) and selected(SCALE_N - 2), path())
        page.keyboard.press("Home")
        until(lambda: path() == name(0), timeout=4)
        check("Home: the first row, selected and scrolled into view",
              path() == name(0) and _in_view(page, rows_sel, row(0)) and selected(0)
              and rows.evaluate("e => e.scrollTop") == 0)
        for _ in range(5):
            page.keyboard.press("ArrowDown")
        until(lambda: path() == name(5), timeout=4)
        rows.evaluate("e => { e.scrollTop = 10000 * 24; }")
        until(lambda: page.locator(row(5)).count() == 0, timeout=4)
        check("scrolled away, the selected row is no longer drawn — and is still the selection",
              page.locator(row(5)).count() == 0 and path() == name(5) and page.locator(row(10000)).count() == 1)
        page.keyboard.press("ArrowDown")
        until(lambda: path() == name(6), timeout=4)
        check("↓ moves on from it, and brings the new row back into view",
              path() == name(6) and _in_view(page, rows_sel, row(6)) and selected(6))
        page.keyboard.press("End")
        until(lambda: path() == name(SCALE_N - 1), timeout=4)
        check("End: the last row, in view", _in_view(page, rows_sel, row(SCALE_N - 1)) and selected(SCALE_N - 1))
        page.keyboard.press("PageUp")
        until(lambda: path() != name(SCALE_N - 1), timeout=4)
        moved = SCALE_N - 1 - int(path()[-8:-3])
        check("Page Up: a page of rows up, in view", 10 < moved < 80
              and _in_view(page, rows_sel, row(SCALE_N - 1 - moved)), f"moved {moved}")
        # The pane's file is kept (a reload): brought into view when the rows arrive.
        rows.evaluate("e => { e.scrollTop = 12345 * 24; }")
        until(lambda: page.locator(row(12345)).count() > 0, timeout=4)
        page.locator(row(12345)).click()
        until(lambda: path() == name(12345), timeout=4)
        _set_storage(page, zoom="160")
        _boot(page, mock, None, until, reload=True)
        until(lambda: path() == name(12345), timeout=30)
        # At 160% the pane is narrow: a picked file folds the list away. Shown
        # again ("Files"), it is laid out only now — and brings the file into view then.
        toggle = page.locator(_pane(L, _tid("tree-toggle")))
        narrow = toggle.count() > 0 and not page.locator(rows_sel).is_visible()
        if narrow:
            toggle.click()
        until(lambda: page.locator(row(12345)).count() > 0 and _in_view(page, rows_sel, row(12345)), timeout=6)
        check("a reload at 160%: the pane's file is picked again and, the list shown, scrolled into view"
              + (" (narrow: shown with Files)" if narrow else ""),
              path() == name(12345) and _in_view(page, rows_sel, row(12345)) and selected(12345), path())
        page.locator(rows_sel).evaluate("e => { e.scrollTop = e.scrollHeight; }")
        until(lambda: page.locator(row(SCALE_N - 1)).count() > 0, timeout=4)
        check("…and at 160% the last row is reached by scrolling, in view",
              _in_view(page, rows_sel, row(SCALE_N - 1)))
        # Review of PR #73, NIT: at 160% End and Page Down clipped ~0.8 drawn px
        # of the row they scrolled to (`clientHeight` is rounded). Keys keep a
        # narrow pane's list open (a click folds it to show the diff).
        # The list as tall as a window can make it: a part-pixel height, which
        # `clientHeight` rounds up (400.6 → 401 CSS px; 0.4 × 1.6 = 0.64 drawn px).
        page.locator(rows_sel).evaluate("e => { e.style.flex = '0 0 400.6px'; }")
        page.locator(rows_sel).focus()
        page.keyboard.press("Home")
        until(lambda: path() == name(0), timeout=4)
        page.keyboard.press("PageDown")
        until(lambda: path() != name(0), timeout=4)
        at = int(path()[-8:-3])
        down = _clip(page, rows_sel, row(at))
        page.keyboard.press("End")
        until(lambda: path() == name(SCALE_N - 1), timeout=4)
        end = _clip(page, rows_sel, row(SCALE_N - 1))
        check("at 160% Page Down and End scroll their row wholly into view (no part-pixel clipped)",
              down < 0.05 and end < 0.05 and page.locator(rows_sel).is_visible(),
              f"Page Down clipped {down:.3f} px, End {end:.3f} px")
    finally:
        _set_storage(page, zoom="100")
        w["git"] = g


def _hold_checks(page, mock, check, until, requests):
    """Review of PR #73, MED: holding ↓ started a `git/base` request (and a
    read) for every row passed, with no settle and no abort; against a slow
    repository (the owner's VEX projects on /mnt/c) the last row's diff came
    4 s late at 500 ms a request. A key now moves the highlight at once and
    reads once the keys settle; a request no longer waited for is aborted.
    Held here at ~33 repeats a second over 60 rows, `git/base` taking 0.5 s."""
    w = mock.world
    g = w["git"]
    n = 61
    name = lambda i: f"r{i:02d}.txt"  # noqa: E731
    files = {k: dict(v) for k, v in w["files"].items()}
    files.update({name(i): {"content": f"row {i} now\n", "mtime": 1757900300 + i} for i in range(n)})
    path = lambda: page.locator(_in("scm-path")).inner_text()  # noqa: E731
    try:
        w["git"] = {"head": "c0ffee1", "branch": "main", "dirs": {}, "base_delay": 0.5,
                    "base": {name(i): f"row {i} then\n" for i in range(n)},
                    "files": {name(i): "modified" for i in range(n)}}
        _fresh(page, mock, until, ws=WS, keep_files=files)
        _pick(page, until, name(0))
        check("setup: the first row diffed against a slow HEAD", _diffed(page, until, "row 0 then", "row 0 now"))
        start = len(requests)
        t0 = time.time()
        for _ in range(n - 1):
            page.keyboard.press("ArrowDown")
            time.sleep(0.03)
        held = time.time() - t0
        until(lambda: path() == name(n - 1), timeout=3)
        t1 = time.time()
        shown = _diffed(page, until, f"row {n - 1} then", f"row {n - 1} now", timeout=12)
        late = time.time() - t1
        time.sleep(0.3)
        bases = [u for u in requests[start:] if "/git/base" in u]
        reads = [u for u in requests[start:] if "/file?" in u]
        print(f"    held ↓ over {n - 1} rows in {held:.2f} s: {len(bases)} git/base and {len(reads)} reads sent; "
              f"the last row's diff {late:.2f} s after the last key")
        check("holding ↓ over 60 rows asks HEAD about a handful of rows, not 60",
              len(bases) <= 3 and len(reads) <= 3, f"{len(bases)} git/base, {len(reads)} reads")
        check("…and the last row's diff shows promptly (one slow request, not a queue of them)",
              bool(shown) and late < 2.0, f"{late:.2f} s")
        check("…the highlight followed every key at once (the row passed is the one shown)",
              path() == name(n - 1) and "sel" in (page.locator(_in(f"scm-{name(n - 1)}")).get_attribute("class") or ""))
        # Clicks read at once; one clicked away from before HEAD answered is
        # dropped (aborted), not left holding a connection ahead of the next.
        failed: list[str] = []
        listener = lambda r: failed.append(f"{r.url} {r.failure}")  # noqa: E731
        page.on("requestfailed", listener)
        try:
            page.locator(_in(f"scm-{name(n - 3)}")).click()
            time.sleep(0.1)
            page.locator(_in(f"scm-{name(n - 2)}")).click()
            shown = _diffed(page, until, f"row {n - 2} then", f"row {n - 2} now", timeout=6)
            time.sleep(0.3)
        finally:
            page.remove_listener("requestfailed", listener)
        dropped = [f for f in failed if "/git/base" in f and f"path={name(n - 3)}" in f]
        check("a row clicked away from before HEAD answers: its request is aborted, the next row diffed",
              bool(dropped) and bool(shown), "; ".join(failed[:3]) or "nothing aborted")
    finally:
        w["git"] = g


def _clear_task_checks(page, mock, check, until, requests):
    """Item 2: the 2×2 grid draws a Task pane (slot 4 by default), and while it
    shows a task every Diff pane shows that task's diff (L3) — so once a task
    was picked nothing took the window off it. The Task pane's header clears
    the task now; a Diff pane never shows another task than the Task pane."""
    L = 2  # the Diff pane; the Task pane is 4
    mode = page.locator(_pane(L, _tid("diff-mode")))
    what = lambda: page.locator(_pane(L, _tid("diff-mode-what"))).inner_text().strip()  # noqa: E731
    why = lambda: page.locator(_pane(L, _tid("diff-mode-why"))).inner_text().strip(" ·")  # noqa: E731
    picker = page.locator(_tid("pane-2-diff-project"))
    clear = page.locator(_tid("pane-4-task-clear"))
    until(lambda: mode.count() > 0 and mode.get_attribute("data-mode") == "project", timeout=6)
    check("2×2 with a Task pane, no task picked: nothing to clear", clear.count() == 0)
    expand(page, until, "p1", "task-k1")
    page.locator(_tid("task-k1")).click()
    until(lambda: mode.get_attribute("data-mode") == "task", timeout=4)
    expand(page, until, "p1", "thread-t1")
    page.locator(_tid("thread-t1")).click()
    time.sleep(0.5)
    check("a task picked holds the Diff pane on it, a thread picked since too (L3)",
          mode.get_attribute("data-mode") == "task" and mode.get_attribute("data-why") == "pane"
          and picker.is_disabled(), mode.inner_text())
    check("the Task pane's header names the task and offers to clear it",
          clear.count() == 1 and clear.is_enabled() and "k1" in page.locator(_tid("pane-4-task")).inner_text())
    check("…and the Diff pane's picker says how to get out",
          "clear the task" in (picker.get_attribute("title") or ""))
    clear.click()
    until(lambda: mode.get_attribute("data-mode") == "project", timeout=4)
    check("clear: the Task pane shows no task", page.locator(_pane(4, _tid("task-empty"))).count() == 1)
    check("…and the Diff pane its project's uncommitted changes, its picker back",
          what() == "Uncommitted changes in jarvis" and why() == "the chat's project" and picker.is_enabled(),
          f"{what()} · {why()}")
    check("…the sidebar's task row is no longer selected, and nothing is left to clear",
          "sel" not in (page.locator(_tid("task-k1")).get_attribute("class") or "").split() and clear.count() == 0)
    page.locator(_tid("task-k1")).click()
    until(lambda: mode.get_attribute("data-mode") == "task", timeout=4)
    check("a task picked again holds the Diff pane again", mode.get_attribute("data-why") == "pane")
    mock.emit("approval_requested", {
        "req_id": "scm2", "code": "SC02", "tool": "run_command", "args": {"command": "ls"},
        "command": "ls", "origin": "task: scm", "allowlistable": True, "timeout_s": 120,
    })
    until(lambda: page.locator(_tid("approval-card")).count() > 0, timeout=4)
    time.sleep(0.3)
    check("under a card 'clear' is disabled (and the task stays)",
          clear.is_disabled() and mode.get_attribute("data-mode") == "task")
    page.keyboard.press("Escape")
    until(lambda: page.locator(_tid("approval-card")).count() == 0, timeout=4)
    check("answered, it comes back", bool(until(lambda: clear.is_enabled(), timeout=3)))


def _settings_checks(page, mock, check, until, requests):
    """Item 4: ED-8's editor settings draw the uncommitted-changes diff as they
    draw the task diff and Compare — and live, on a change."""
    from tests.face.hud_v2_editor_settings_check import DIALOG, KEY, _open_dialog

    def size(which: str = "modified") -> str:
        loc = page.locator(_pane(D, f'[data-testid="scm-editor"] .editor.{which} .view-lines'))
        return loc.first.evaluate("e => getComputedStyle(e).fontSize") if loc.count() else ""
    try:
        _pick(page, until, "calc.py")
        check("setup: calc.py diffed", _diffed(page, until, "return a - b", "return a + b"))
        check("with nothing stored the diff draws the default size", size() == "12px" and size("original") == "12px",
              f"{size()} {size('original')}")
        check("setup: README.md opens in the File pane", _open(page, until, 2, "README.md"))
        check("setup: the editor settings open", _open_dialog(page, until, 2))
        page.locator(_tid("es-font-size")).select_option("16")
        check("ED-8: a size chosen reaches the uncommitted-changes diff at once, both sides",
              bool(until(lambda: size() == "16px" and size("original") == "16px", timeout=3)),
              f"{size()} {size('original')}")
        page.locator(_tid("es-close")).click()
        until(lambda: page.locator(DIALOG).count() == 0, timeout=2)
        _pick(page, until, "notes.txt")
        check("…and a diff opened after it draws it too",
              bool(until(lambda: size() == "16px", timeout=4)), size())
    finally:
        page.evaluate("k => localStorage.removeItem(k)", KEY)


def _midcompute_checks(page, mock, check, until, requests):
    """Item 3: the task diff (`DiffEditor`) and Compare (`CompareEditor`) own
    their view model, as ScmDiffEditor does since L1. Handed `{original,
    modified}`, Monaco never releases the view model it makes, so a diff still
    waiting on the editor worker when the view closed was never cancelled and
    threw `no diff result available` against the disposed models. A huge
    task-diff file keeps the one worker busy; each view is closed while its
    own diff waits behind it. Compare's right side is the buffer: its edit,
    dirty state and undo stay."""
    w = mock.world
    keep_diff = json.loads(json.dumps(w["diff"]))
    keep_sides = dict(w["diff_files"])
    models = lambda: page.evaluate("window.__hud.monacoModels()")  # noqa: E731

    def has(path: str, side: str = "original") -> bool:
        return any(u.startswith("jarvis-diff:") and f"/{path}?{side}" in u for u in models())

    def b_drawn() -> bool:
        """b.txt's diff is drawn: the worker has answered everything queued before it."""
        mod = _pane(D, '[data-testid="diff-editor"] .editor.modified')
        lines = page.locator(mod + " .view-lines")
        return (lines.count() > 0 and "two" in lines.first.inner_text()
                and page.locator(mod + ' [class*="insert"]').count() > 0)
    try:
        w["diff"]["files"] = keep_diff["files"] + [
            {"path": "huge.txt", "status": "M", "additions": HUGE_LINES, "deletions": HUGE_LINES},
            {"path": "a.txt", "status": "M", "additions": 1, "deletions": 0},
            {"path": "b.txt", "status": "M", "additions": 1, "deletions": 0},
        ]
        w["diff_files"].update({
            "huge.txt": {"before": "".join(f"alpha line {i} of the old text\n" for i in range(HUGE_LINES)),
                         "after": "".join(f"omega row {i} in the new file\n" for i in range(HUGE_LINES))},
            "a.txt": {"before": "first\n", "after": "first\nsecond\n"},
            "b.txt": {"before": "one\n", "after": "one\ntwo\n"},
        })
        check("setup: calc.py opens in the File pane", _open(page, until, 2, "calc.py"))
        check("setup: an unsaved edit", _type(page, until, 2, "\n#mine"))
        mock.disk_write("calc.py", "def add(a, b):\n    return a + b  # theirs\n")
        bar = page.locator(_pane(2, _tid("editor-conflict")))
        check("setup: the disk changed under it (the conflict bar)", bool(until(lambda: bar.count() > 0, timeout=8)))
        expand(page, until, "p1", "task-k1")
        page.locator(_tid("task-k1")).click()
        check("setup: the Diff pane shows the task's diff",
              bool(until(lambda: page.locator(_in("diff-huge.txt")).count() > 0, timeout=6)))

        # The task diff: huge.txt's diff takes the worker, a.txt's waits behind
        # it, and a.txt is closed (b.txt picked) before it comes back.
        start = len(ERRORS)
        page.locator(_in("diff-huge.txt")).click()
        until(lambda: has("huge.txt"), timeout=15)
        page.locator(_in("diff-a.txt")).click()
        until(lambda: has("a.txt"), timeout=6)
        page.locator(_in("diff-b.txt")).click()
        until(lambda: has("b.txt") and not has("a.txt"), timeout=6)
        check("setup: the worker answered the diff after them", bool(until(b_drawn, timeout=30)))
        time.sleep(0.8)
        new = ERRORS[start:]
        check("a task diff closed while its diff waits on the worker throws nothing", not new, "; ".join(new[:3]))
        check("…and its models went with it",
              not has("a.txt") and not has("a.txt", "modified") and not has("huge.txt"), str(models())[:300])

        # Compare: open while huge.txt's diff holds the worker, closed before its own comes back.
        start = len(ERRORS)
        page.locator(_in("diff-huge.txt")).click()
        until(lambda: has("huge.txt"), timeout=15)
        page.locator(_pane(2, _tid("conflict-compare"))).click()
        cmp_ = page.locator(_pane(2, _tid("editor-compare")))
        until(lambda: cmp_.count() > 0 and any(u.startswith("jarvis-disk:") for u in models()), timeout=6)
        check("setup: Compare opened", cmp_.count() == 1)
        page.locator(_pane(2, _tid("conflict-compare"))).click()
        until(lambda: cmp_.count() == 0 and not any(u.startswith("jarvis-disk:") for u in models()), timeout=6)
        page.locator(_in("diff-b.txt")).click()
        check("setup: the worker answered the diff after it", bool(until(b_drawn, timeout=30)))
        time.sleep(0.8)
        new = ERRORS[start:]
        check("Compare closed while its diff waits on the worker throws nothing", not new, "; ".join(new[:3]))
        check("…the buffer keeps its edit, still unsaved, its model alive",
              "#mine" in _text(page, 2) and page.locator(_tab(2, "calc.py")).get_attribute("data-dirty") == "true"
              and any(u.startswith("file:") and u.endswith("/calc.py") for u in models()), _text(page, 2)[-60:])
        page.locator(_lines(2)).click()
        page.keyboard.press("Control+End")
        undone = False
        for _ in range(3):
            page.keyboard.press("Control+z")
            if until(lambda: "#mine" not in _text(page, 2), timeout=0.6):
                undone = True
                break
        check("…and its undo still takes the edit back", undone, _text(page, 2)[-60:])
    finally:
        w["diff"].clear()
        w["diff"].update(keep_diff)
        w["diff_files"].clear()
        w["diff_files"].update(keep_sides)


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
            # `ONLY=scale` runs one section (a mutation round's quick way in).
            scm_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until, v2.guard,
                       v2.FAKE_RECOGNIZER, os.environ.get("ONLY", ""))
            browser.close()
    finally:
        mock.stop()
    print()
    if v2.FAILURES:
        print(f"{len(v2.FAILURES)} of {v2.CHECKS} checks FAILED:")
        for f in v2.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {v2.CHECKS} Diff pane checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
