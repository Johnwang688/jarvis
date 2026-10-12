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

Run: PYTHONPATH=. .venv/bin/python tests/face/hud_v2_scm_check.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.face.hud_v2_editor_check import WIDE, _fresh, _open, _tab, _type  # noqa: E402
from tests.face.hud_v2_layout_check import _boot, _pane, _show, expand  # noqa: E402

WS = json.dumps({"preset": "cols3", "focused": 2,
                 "panes": [{"view": "chat"}, {"view": "file"}, {"view": "diff"}]})
# L3: a Task pane beside the Diff pane.
WS_TASK = json.dumps({"preset": "cols3", "focused": 2,
                      "panes": [{"view": "chat"}, {"view": "task"}, {"view": "diff"}]})
CALC_HEAD = "def add(a, b):\n    return a - b\n"
D = 3  # the Diff pane
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


def scm_checks(browser, mock, base, check, until, guard, init_script):
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
                            (_task_pane_checks, WS_TASK), (_card_checks, WS), (_empty_checks, WS)):
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
    # The File pane lets go of the buffer (its preview tab replaced): the
    # registry disposes the model, and the diff reads the file again.
    page.locator(_pane(2, _tid("file-notes.txt"))).click()
    until(lambda: page.locator(_pane(2, _tid("file-path"))).inner_text() == "notes.txt", timeout=4)
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
            scm_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until, v2.guard,
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
    print(f"all {v2.CHECKS} Diff pane checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
