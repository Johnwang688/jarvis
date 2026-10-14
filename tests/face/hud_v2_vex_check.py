"""Headless checks for the HUD's VEX view (VX-2; editor plan §2.9, §3's VX-2
package; decisions V-1..V-5).

Free and hermetic: no VEX tool, no device, no make. The section runs against
a `MockDaemon(0)` of its own (`tests/face/hud_v2_mock_vex.py` plays VX-1's
routes; a build, a download and the brain terminal are terminal rows there)
and a browser context of its own, the program terminals' sockets played in
the browser by `route_web_socket` (`FakePty`, from the terminal suite), and
every request to 8402/8403/8405 refused and counted (`hud_v2_check.guard`).
The daemon's side — argv, refusals, locks, owner-only — is
`tests/v2/vex_check.py`.

What it pins:
  - **the view appears only for a VEX project**: the strip offers "vex" for
    a project whose settings parse (and one whose file is malformed or PROS,
    to say why), never for an ordinary one; a pane switched to it is pinned
    to its project;
  - with no `vex.json` the view names the one command, `jarvis vex setup`,
    and builds and downloads nothing;
  - **Build shows ✓ or the errors list**, the build's output is a program
    terminal in the panel, and **an error click opens the file at its
    line** in the editor (a File pane beside the view), while an error
    outside the project is text only;
  - **Download needs a click on the labelled button**: Enter or Space on it,
    a script's `.click()`, an untrusted click event and a battery of keys
    send nothing; the label names the slot it replaces; a bad name is
    refused before anything is sent;
  - a download the daemon says needs a build builds first, then downloads —
    and **a card that comes up while it builds drops the download**;
  - **under a card every VEX control is disabled and inert**;
  - the brain terminal opens in the panel, **reattaches after a reload**,
    and is paused for a download and opened again after it;
  - device info (`vexcom --json`) and the programs list (`vexcom --programs`)
    each ask their own read and are drawn as text: markup in them is
    characters;
  - a device action this window did not start is said;
  - the review round (one check per finding): **one download chain per
    window** — refused for another project while one runs, shown in every
    VEX view with a Cancel that stops its build and sends nothing, the
    pickers off meanwhile (M1); a build-first that still ends stale says
    Rebuild, never loops (M2); a card that comes up while the daemon answers
    builds nothing (L1), and one that comes and goes while it builds drops
    the download (L2); only a click's first click counts (L3); a project
    that becomes a VEX project is offered the view on focus (L4); a lone
    VEX pane survives a reload (L5); a dropped chain never reopens the brain
    terminal (L6); a VEX pane whose project is gone says "Pick a project"
    (L7).

Run (after `cd hud && npm ci && npm run build`):
    PYTHONPATH=. .venv/bin/python tests/face/hud_v2_vex_check.py
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.face import hud_v2_mock_vex as mock_vex  # noqa: E402
from tests.face.hud_v2_layout_check import _approval, _boot, _pane, _set_storage, _set_ws, _ws  # noqa: E402
from tests.face.hud_v2_terminal_check import FakePty, pumping  # noqa: E402

VIEW = (1800, 900)
V = 2  # the VEX view's pane, beside a chat (a lone VEX pane: _lone_pane_checks)
P3_ROOT = "/mnt/c/Users/johnw/OneDrive/Desktop/override"
MAIN_CPP = "#include \"vex.h\"\n\nint main() {\n  int x = 1\n  return 0;\n}\n"
ERRORS = [
    {"file": "src/main.cpp", "line": 4, "col": 12, "severity": "error", "message": "expected ';' after declaration",
     "in_project": True},
    {"file": "C:/vex/sdk/vex_motor.h", "line": 40, "col": 3, "severity": "warning", "message": "<b>deprecated</b>",
     "in_project": False},
]


def _project(pid: str, name: str, root: str) -> dict:
    return {"id": pid, "name": name, "root": root, "created": "2026-10-11T00:00:00+00:00", "profile": "auto",
            "routing": {"chains": {}, "models": {}, "no_new_work": None}, "discord_channel_id": None,
            "extra_dirs": [], "always_ask": [], "inbox": False}


def _world(mock):
    """A VEX project (p3), one whose settings file is malformed (p4), a second
    VEX project (p5), an ordinary one that becomes VEX (p6), and src/main.cpp to open."""
    w = mock.world
    if not any(p["id"] == "p3" for p in w["projects"]):
        w["projects"].append(_project("p3", "override", P3_ROOT))
        w["projects"].append(_project("p4", "broken", "/mnt/c/Users/johnw/OneDrive/Desktop/broken"))
        w["projects"].append(_project("p5", "flywheel", "/mnt/c/Users/johnw/OneDrive/Desktop/flywheel"))
        w["projects"].append(_project("p6", "intake", "/mnt/c/Users/johnw/OneDrive/Desktop/intake"))
    w["files"]["src/main.cpp"] = {"content": MAIN_CPP, "mtime": 1757900300}
    src = w["tree"].setdefault("src", [])
    if not any(e["name"] == "main.cpp" for e in src):
        src.append({"name": "main.cpp", "kind": "file", "size": len(MAIN_CPP), "mtime": 1757900300})
    v = mock_vex.state(mock)
    v["projects"]["p3"] = mock_vex.vex_project("p3")
    v["projects"]["p5"] = mock_vex.vex_project("p5", "flywheel")
    v["projects"]["p4"] = {"project_id": "p4", "vex": False, "malformed": True,
                           "reason": "the VEX settings file is not the extension's version 2 format"}


def _reset(page, mock, until, ws: dict | None = None, configured: bool = True):
    """A clean start: no terminals, a fresh VEX state, the workspace given, a reload."""
    mock.world["terminals"] = []
    mock.world.pop("vex", None)
    _world(mock)
    v = mock_vex.state(mock)
    if not configured:
        v["status"] = {"configured": False, "hint": "VEX is not set up: run `jarvis vex setup` in a terminal"}
    ws = ws or {"preset": "cols2", "focused": V, "panes": [{"view": "chat"}, {"view": "vex", "projectId": "p3"}],
                "panel": {"open": False, "height": 260}}
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, json.dumps(ws))
    page.evaluate("() => { try { localStorage.removeItem('jarvis.hud.vex.builds');"
                  " sessionStorage.removeItem('jarvis.hud.terminals.mine') } catch (e) {} }")
    _boot(page, mock, None, until, reload=True)


def _view(n: int = 1, sel: str = "") -> str:
    return _pane(n, f'[data-testid="vex-view-{n}"]' + (f" {sel}" if sel else ""))


def _tid(sel: str) -> str:
    return f'[data-testid="{sel}"]'


def _posted(mock, path: str) -> list:
    return [b for (m, p, b) in mock.calls if m == "POST" and p == path]


def _sent_downloads(mock) -> list:
    return [d for d in mock_vex.state(mock)["downloads"] if d.get("sent")]


def _latest_build(mock) -> str | None:
    builds = mock_vex.state(mock)["builds"]
    return list(builds)[-1] if builds else None


def _finish(mock, fake, build_id: str, state_: str = "succeeded", errors=None):
    """The build's program ends: its record says so, and its terminal prints and exits, as make would."""
    mock_vex.finish_build(mock, build_id, state_, errors)
    rec = mock_vex.state(mock)["builds"][build_id]
    tid = rec["terminal_id"]
    lines = "".join(f"{e['file']}:{e['line']}:{e['col']}: {e['severity']}: {e['message']}\r\n" for e in errors or [])
    fake.output(tid, (lines + ("make: done\r\n" if state_ == "succeeded" else "make: *** [build] Error 2\r\n")).encode())
    fake.exit(tid, rec["exit_code"])


def _ready(page, until) -> bool:
    return bool(until(lambda: page.locator(_view(V, _tid("vex-card"))).count() > 0, timeout=5))


# ---------------------------------------------------------------------------

def _offer_checks(page, mock, check, until, fake):
    _reset(page, mock, until, {"preset": "cols2", "focused": 1,
                               "panes": [{"view": "file", "projectId": "p1"}, {"view": "file", "projectId": "p3"}],
                               "panel": {"open": False, "height": 260}})
    until(lambda: page.locator(_pane(2, _tid("tab-vex"))).count() > 0, timeout=4)
    check("an ordinary project's pane offers no VEX view", page.locator(_pane(1, _tid("tab-vex"))).count() == 0)
    check("a VEX project's pane offers it", page.locator(_pane(2, _tid("tab-vex"))).count() == 1)
    page.locator(_pane(2, _tid("tab-vex"))).click()
    until(lambda: page.locator(_view(2, _tid("vex-card"))).count() > 0, timeout=4)
    card = page.locator(_view(2, _tid("vex-card"))).inner_text()
    check("the VEX view shows the project's card (name, language, slot, SDK)",
          "override" in card and "C++" in card and "slot 1" in card and "V5_20240802" in card, card)
    ws = page.evaluate("window.__hud.workspace().ws")
    check("the pane stays on its project", ws["panes"][1]["projectId"] == "p3", str(ws["panes"][1]))
    # A chat pane in an ordinary project, a malformed settings file.
    _reset(page, mock, until, {"preset": "cols2", "focused": 1,
                               "panes": [{"view": "chat"}, {"view": "file", "projectId": "p4"}],
                               "panel": {"open": False, "height": 260}})
    until(lambda: page.locator(_pane(2, _tid("tab-vex"))).count() > 0, timeout=4)
    check("a chat in an ordinary project offers no VEX view", page.locator(_pane(1, _tid("tab-vex"))).count() == 0)
    page.locator(_pane(2, _tid("tab-vex"))).click()
    bad = page.locator(_view(2, _tid("vex-malformed")))
    until(lambda: bad.count() > 0, timeout=4)
    check("a malformed settings file is reported, never guessed around",
          bad.count() == 1 and "version 2" in bad.inner_text(), bad.inner_text() if bad.count() else "")
    check("and it offers no Build or Download", page.locator(_view(2, _tid("vex-build-btn"))).count() == 0
          and page.locator(_view(2, _tid("vex-download-btn"))).count() == 0)


def _setup_checks(page, mock, check, until, fake):
    # Earlier sections (feedback) legitimately POST /vex/brain on a resume, and
    # mock.calls is the whole run's: judge only what this section asked for.
    base = len(mock.calls)
    _reset(page, mock, until, configured=False)
    check("setup: the view draws", _ready(page, until))
    note = page.locator(_view(V, _tid("vex-setup")))
    until(lambda: note.count() > 0, timeout=4)
    cmd = page.locator(_view(V, _tid("vex-setup-command")))
    check("with no vex.json the view names the one command to run",
          cmd.count() == 1 and cmd.inner_text().strip() == "jarvis vex setup", note.inner_text() if note.count() else "")
    check("and every Build and Download button is off",
          all(page.locator(_view(V, _tid(t))).is_disabled()
              for t in ("vex-build-btn", "vex-rebuild-btn", "vex-clean-btn", "vex-download-btn")))
    check("and nothing was asked of the daemon but its status",
          not _posted_since(mock, base, "/projects/p3/vex/build")
          and not _posted_since(mock, base, "/projects/p3/vex/download")
          and not _posted_since(mock, base, "/vex/brain"))
    mock_vex.state(mock)["status"] = mock_vex.state(mock).get("status_ok") or {
        "configured": True, "hint": None, "sdks": {"cpp": "V5_20240802_15_00_00", "python": None}}
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    check("once setup has run, the window's focus brings the view back",
          bool(until(lambda: note.count() == 0 and page.locator(_view(V, _tid("vex-build-btn"))).is_enabled(),
                     timeout=4)))


def _build_checks(page, mock, check, until, fake):
    _reset(page, mock, until)
    check("setup: the view draws", _ready(page, until))
    page.locator(_view(V, _tid("vex-build-btn"))).click()
    check("Build asks the daemon for exactly {action: build}",
          bool(until(lambda: _posted(mock, "/projects/p3/vex/build"), timeout=4))
          and _posted(mock, "/projects/p3/vex/build")[-1] == {"action": "build"},
          str(_posted(mock, "/projects/p3/vex/build")))
    bid = _latest_build(mock)
    tid = mock_vex.state(mock)["builds"][bid]["terminal_id"]
    tab = page.locator(_tid(f"panel-tab-{tid}"))
    check("the build's output is a program terminal in the panel, opened for it",
          bool(until(lambda: tab.count() > 0, timeout=4)) and tab.get_attribute("data-program") == "vex-build")
    check("and it attaches", bool(until(lambda: page.evaluate(f"window.__hudTerminals.state('{tid}')") == "attached",
                                        timeout=4)))
    state = page.locator(_view(V, _tid("vex-build-state")))
    check("the view says it is building", "Building" in state.inner_text())
    check("a silent build shows elapsed time and explains that it is waiting for output",
          page.locator(_view(V, _tid("vex-build-elapsed"))).count() == 1
          and "Waiting for tool output" in page.locator(_view(V, _tid("vex-build-waiting"))).inner_text())
    rec = mock_vex.state(mock)["builds"][bid]
    rec["output"] = "Compiling src/main.cpp\nerror: <b>link failed</b>\n"
    rec["errors"] = ERRORS
    preview = page.locator(_view(V, _tid("vex-build-output")))
    check("output and errors are visible while the build is still running, rendered as text",
          bool(until(lambda: preview.count() and "link failed" in preview.inner_text(), timeout=4))
          and preview.locator("b").count() == 0
          and page.locator(_view(V, _tid("vex-errors")) + " li").count() == 2)
    page.locator(_tid("panel-hide")).click()
    page.locator(_view(V, _tid("vex-build-output-btn"))).click()
    check("Show terminal reopens the full build output", bool(until(lambda: tab.count() > 0, timeout=4)))
    _finish(mock, fake, bid, "succeeded")
    check("a build that succeeds shows ✓",
          bool(until(lambda: state.inner_text().startswith("✓ Built"), timeout=4)), state.inner_text())
    check("no errors list after a clean build", page.locator(_view(V, _tid("vex-errors"))).count() == 0)
    check("its terminal ended: no Restart (that would open a shell)",
          bool(until(lambda: page.locator(_tid("term-exited")).count() > 0, timeout=4))
          and page.locator(_tid("term-restart")).count() == 0)

    # A failed build: the errors list, and a click that opens the file at its line.
    page.locator(_view(V, _tid("vex-build-btn"))).click()
    until(lambda: len(mock_vex.state(mock)["builds"]) == 2, timeout=4)
    bid2 = _latest_build(mock)
    _finish(mock, fake, bid2, "failed", ERRORS)
    errs = page.locator(_view(V, _tid("vex-errors")) + " li")
    until(lambda: errs.count() == 2, timeout=4)
    check("a failed build shows the errors list", errs.count() == 2 and "✗ Build failed (exit 2): 1 error, 1 warning"
          in state.inner_text(), state.inner_text())
    outside = page.locator(_view(V, _tid("vex-error-1")))
    check("an error outside the project is text, never a button to open",
          outside.evaluate("e => e.tagName") == "DIV")
    check("an error's message is text: markup in it is characters",
          "<b>deprecated</b>" in outside.inner_text() and outside.locator("b").count() == 0)
    first = page.locator(_view(V, _tid("vex-error-0")))
    check("an error inside the project is a button naming its file and line",
          first.evaluate("e => e.tagName") == "BUTTON" and "src/main.cpp:4:12" in first.inner_text())
    first.click()
    check("the click opens the file in a File pane beside the view",
          bool(until(lambda: page.locator(_pane(3)).get_attribute("data-view") == "file", timeout=5))
          and page.locator(_pane(2)).get_attribute("data-view") == "vex")
    path = page.locator(_pane(3, _tid("file-path")))
    check("at that file", bool(until(lambda: path.count() and path.inner_text() == "src/main.cpp", timeout=5)),
          path.inner_text() if path.count() else "")

    def cursor():
        eds = page.evaluate("window.__hud.monacoEditors()")
        return next(((e["line"], e["column"]) for e in eds if (e["uri"] or "").endswith("src/main.cpp")), None)
    check("with the cursor at the error's line and column", bool(until(lambda: cursor() == (4, 12), timeout=6)),
          str(cursor()))
    # Back to the view; the same error again moves the cursor in the same pane.
    page.locator(_pane(3, ".view-lines")).click()
    page.keyboard.press("Control+Home")
    until(lambda: cursor() == (1, 1), timeout=2)
    page.locator(_view(V, _tid("vex-error-0"))).click()
    check("a second click finds the File pane already showing the project",
          bool(until(lambda: cursor() == (4, 12), timeout=4))
          and page.evaluate("window.__hud.workspace().ws.order.length") == 3, str(cursor()))


def _clean_output_checks(page, mock, check, until, fake):
    _reset(page, mock, until)
    page.locator(_view(V, _tid("vex-clean-btn"))).click()
    until(lambda: _latest_build(mock), timeout=4)
    bid = _latest_build(mock)
    rec = mock_vex.state(mock)["builds"][bid]
    tid = rec["terminal_id"]
    check("Clean sends exactly its action and opens its program output",
          _posted(mock, "/projects/p3/vex/build")[-1] == {"action": "clean"}
          and bool(until(lambda: page.locator(_tid(f"panel-tab-{tid}")).count() > 0, timeout=4)))
    rec["output"] = "clean project\n"
    fake.output(tid, b"clean project\r\n")
    preview = page.locator(_view(V, _tid("vex-build-output")))
    check("Clean output is visible before completion",
          bool(until(lambda: preview.count() and "clean project" in preview.inner_text(), timeout=4)))
    _finish(mock, fake, bid)
    state = page.locator(_view(V, _tid("vex-build-state")))
    check("Clean exits successfully and Build becomes usable again",
          bool(until(lambda: state.inner_text().startswith("✓ Cleaned")
                     and page.locator(_view(V, _tid("vex-build-btn"))).is_enabled(), timeout=4)),
          state.inner_text())


def _explicit_click_checks(page, mock, check, until, fake):
    before = len(mock.calls)
    _reset(page, mock, until)
    check("setup: the view draws", _ready(page, until))
    btn = page.locator(_view(V, _tid("vex-download-btn")))
    check("the button says what it does",
          btn.inner_text().strip() == "Download to slot 1: replaces whatever is in slot 1 on the brain", btn.inner_text())
    page.locator(_view(V, _tid("vex-slot-pick"))).select_option("3")
    check("and follows the slot picked",
          btn.inner_text().strip() == "Download to slot 3: replaces whatever is in slot 3 on the brain")
    check("it is out of the Tab order", btn.get_attribute("tabindex") == "-1")
    # Every way but the owner's own click.
    btn.focus()
    page.keyboard.press("Enter")
    page.keyboard.press("Space")
    page.keyboard.press("Control+Enter")
    btn.evaluate("e => e.click()")
    btn.dispatch_event("click")
    page.locator(_view(V, _tid("vex-name-input"))).focus()
    for key in ("Enter", "Control+Enter", "Control+Shift+D", "Control+s", "Control+b", "F2",
                "Control+Shift+P", "Control+k"):
        page.keyboard.press(key)
    page.locator("body").focus()
    for key in ("Enter", "Control+Enter", "Control+Shift+P"):
        page.keyboard.press(key)
    time.sleep(0.4)
    check("no key, no script click and no untrusted event downloads",
          not mock_vex.state(mock)["downloads"] and not _posted_since(mock, before, "/projects/p3/vex/download"),
          str(mock_vex.state(mock)["downloads"]))
    # A name the daemon would refuse is refused here first.
    name = page.locator(_view(V, _tid("vex-name-input")))
    name.fill("-x")
    check("a name starting with - is refused before anything is sent",
          page.locator(_view(V, _tid("vex-name-refusal"))).count() == 1 and btn.is_disabled())
    name.fill("a;b")
    check("and one holding ; too", btn.is_disabled())
    name.fill("auton")
    # The owner's click.
    btn.click()
    check("the owner's click downloads: exactly {slot, name}",
          bool(until(lambda: _sent_downloads(mock), timeout=4))
          and _posted(mock, "/projects/p3/vex/download")[-1] == {"slot": 3, "name": "auton"},
          str(_posted(mock, "/projects/p3/vex/download")))
    tid = _sent_downloads(mock)[-1]["terminal_id"] if _sent_downloads(mock) else ""
    check("its progress is a program terminal in the panel",
          bool(until(lambda: page.locator(_tid(f"panel-tab-{tid}")).count() > 0, timeout=4))
          and page.locator(_tid(f"panel-tab-{tid}")).get_attribute("data-program") == "vex-download")
    done = page.locator(_view(V, _tid("vex-downloaded")))
    check("and the view says it is in the slot", bool(until(lambda: done.count() > 0 and "slot 3" in done.inner_text(),
                                                          timeout=6)))
    check("the click left no focus on the button", page.evaluate(
        "document.activeElement && document.activeElement.getAttribute('data-testid')") != "vex-download-btn")


def _chain_checks(page, mock, check, until, fake):
    _reset(page, mock, until)
    check("setup: the view draws", _ready(page, until))
    v = mock_vex.state(mock)
    v["bin_fresh"] = False
    v["build_auto"] = {"state": "succeeded"}
    page.locator(_view(V, _tid("vex-download-btn"))).click()
    check("a download the daemon says needs a build builds first",
          bool(until(lambda: _posted(mock, "/projects/p3/vex/build"), timeout=4)))
    check("then downloads, on the one click", bool(until(lambda: _sent_downloads(mock), timeout=8)),
          str(v["downloads"]))
    check("the refused try and the sent one, nothing else", len(v["downloads"]) == 2, str(v["downloads"]))

    # A card while it builds: the download is dropped.
    _reset(page, mock, until)
    _ready(page, until)
    v = mock_vex.state(mock)
    v["bin_fresh"] = False
    page.locator(_view(V, _tid("vex-download-btn"))).click()
    until(lambda: _latest_build(mock), timeout=4)  # this reset's build, not an earlier one
    bid = _latest_build(mock)
    _approval(mock, "vexcard1")
    until(lambda: page.locator(_tid("approval-card")).count() > 0, timeout=4)
    time.sleep(0.3)
    _finish(mock, fake, bid, "succeeded")
    note = page.locator(_view(V, _tid("vex-note")))
    until(lambda: note.count() > 0 and "approval card" in note.inner_text(), timeout=6)
    check("a card that came up while it built drops the download: nothing is sent behind it",
          not _sent_downloads(mock) and len(v["downloads"]) == 1, str(v["downloads"]))
    check("and the view says so", note.count() > 0 and "nothing was downloaded" in note.inner_text(),
          note.inner_text() if note.count() else "")
    page.keyboard.press("Escape")
    until(lambda: page.locator(_tid("approval-card")).count() == 0, timeout=4)
    time.sleep(0.5)
    check("and answering the card sends nothing either", not _sent_downloads(mock), str(v["downloads"]))


def _card_checks(page, mock, check, until, fake):
    _reset(page, mock, until)
    check("setup: the view draws", _ready(page, until))
    page.locator(_view(V, _tid("vex-build-btn"))).click()
    until(lambda: _latest_build(mock), timeout=4)
    _finish(mock, fake, _latest_build(mock), "failed", ERRORS)
    until(lambda: page.locator(_view(V, _tid("vex-error-0"))).count() > 0, timeout=4)
    before = len(mock.calls)
    _approval(mock, "vexcard2")
    until(lambda: page.locator(_tid("approval-card")).count() > 0, timeout=4)
    time.sleep(0.3)
    controls = ["vex-build-btn", "vex-rebuild-btn", "vex-clean-btn", "vex-download-btn", "vex-slot-pick",
                "vex-name-input", "vex-brain-btn", "vex-info-btn", "vex-programs-btn", "vex-error-0"]
    off = {t: page.locator(_view(V, _tid(t))).is_disabled() for t in controls}
    check("under a card every VEX control is disabled", all(off.values()), str(off))
    picker = page.locator(_tid("pane-2-vex-project"))
    check("and the pane's project picker too", picker.is_disabled())
    inert = page.evaluate("() => !!document.querySelector('[data-testid=\"vex-view-2\"]').closest('[inert]')")
    check("and inert behind the veil", inert)
    for t in ("vex-build-btn", "vex-download-btn", "vex-brain-btn", "vex-error-0"):
        try:
            page.locator(_view(V, _tid(t))).click(force=True, timeout=500)
        except Exception:
            pass
    page.keyboard.press("Enter")
    time.sleep(0.4)
    vexy = [c for c in mock.calls[before:] if c[0] == "POST" and ("/vex" in c[1])]
    check("nothing builds, downloads or opens behind the card", not vexy, str(vexy))
    check("nor does a click open a file", page.locator(_pane(3)).count() == 0
          or page.locator(_pane(3)).get_attribute("data-view") != "file")
    page.keyboard.press("Escape")
    until(lambda: page.locator(_tid("approval-card")).count() == 0, timeout=4)
    check("answered, the controls come back",
          bool(until(lambda: page.locator(_view(V, _tid("vex-build-btn"))).is_enabled(), timeout=3)))


def _brain_checks(page, mock, check, until, fake):
    _reset(page, mock, until)
    check("setup: the view draws", _ready(page, until))
    page.locator(_view(V, _tid("vex-brain-btn"))).click()
    check("the brain terminal is asked for with {}", bool(until(lambda: _posted(mock, "/vex/brain"), timeout=4))
          and _posted(mock, "/vex/brain")[-1] == {})
    brain = mock_vex.state(mock)["brain"]
    tab = page.locator(_tid(f"panel-tab-{brain}"))
    check("it opens in the panel", bool(until(lambda: tab.count() > 0, timeout=4))
          and tab.get_attribute("data-program") == "vex-brain")
    fake.ring[brain] = bytearray(b"battery 87%\r\n")
    until(lambda: page.evaluate(f"window.__hudTerminals.state('{brain}')") == "attached", timeout=4)
    socks = len([s for s in fake.sockets if s["tid"] == brain])
    page.reload()
    page.wait_for_selector('[data-testid="sidebar"]', state="attached")
    tab = page.locator(_tid(f"panel-tab-{brain}"))
    check("after a reload its panel tab is back", bool(until(lambda: tab.count() > 0, timeout=6)))
    check("and it reattaches, its ring replayed",
          bool(until(lambda: page.evaluate(f"window.__hudTerminals.state('{brain}')") == "attached", timeout=6))
          and len([s for s in fake.sockets if s["tid"] == brain]) > socks
          and "battery 87%" in page.evaluate(f"window.__hudTerminals.text('{brain}')"))
    # A download pauses it and opens it again after.
    _ready(page, until)
    page.locator(_view(V, _tid("vex-download-btn"))).click()
    check("a download while it is open pauses it (closed for the download)",
          bool(until(lambda: _sent_downloads(mock), timeout=6))
          and any(c[0] == "DELETE" and c[1] == f"/terminals/{brain}" for c in mock.calls))
    check("and opens it again after", bool(until(lambda: mock_vex.state(mock)["brain"] not in (None, brain), timeout=6)),
          str(mock_vex.state(mock)["brain"]))


def _info_checks(page, mock, check, until, fake):
    _reset(page, mock, until)
    check("setup: the view draws", _ready(page, until))
    # What `vexcom --json` answers, pretty-printed by the daemon, with markup in a string.
    mock_vex.state(mock)["info"] = {
        "text": '{\n  "name": "<b>bold</b> <img src=x onerror=\\"window.__pwned=1\\">",\n'
                '  "vexos": "1.1.5",\n  "battery": 87\n}',
        "exit_code": 0}
    page.locator(_view(V, _tid("vex-info-btn"))).click()
    pre = page.locator(_view(V, _tid("vex-info-text")))
    until(lambda: pre.count() > 0, timeout=4)
    check("device info asks for the system read", mock_vex.state(mock)["info_reads"] == ["system"],
          str(mock_vex.state(mock)["info_reads"]))
    check("device info is drawn as text, its lines kept",
          '"vexos": "1.1.5",\n  "battery": 87' in pre.inner_text() and "<b>bold</b>" in pre.inner_text(),
          pre.inner_text())
    check("markup in it is characters: no element, nothing run",
          pre.locator("b, img").count() == 0 and not page.evaluate("window.__pwned || false"))
    what = page.locator(_view(V, _tid("vex-info-what")))
    check("it says what it is", what.get_attribute("data-read") == "system" and "Device info" in what.inner_text(),
          what.inner_text())
    # What `vexcom --programs` answers (a brain with nothing on it exits non-zero).
    mock_vex.state(mock)["info"] = {"text": "slot 1: <script>window.__pwned=2</script>override\nslot 3: skills",
                                    "exit_code": 2}
    page.locator(_view(V, _tid("vex-programs-btn"))).click()
    until(lambda: what.get_attribute("data-read") == "programs", timeout=4)
    check("List programs asks for the programs read",
          mock_vex.state(mock)["info_reads"] == ["system", "programs"], str(mock_vex.state(mock)["info_reads"]))
    check("the programs list is drawn as text",
          "<script>window.__pwned=2</script>override" in pre.inner_text()
          and pre.locator("script").count() == 0 and not page.evaluate("window.__pwned || false"),
          pre.inner_text())
    check("a non-zero exit is said beside it", "Programs on the brain" in what.inner_text()
          and "exited 2" in what.inner_text(), what.inner_text())
    mock_vex.state(mock)["info_refuse"] = "A download is running. One device action at a time."
    page.locator(_view(V, _tid("vex-info-btn"))).click()
    note = page.locator(_view(V, _tid("vex-info-note")))
    check("a refusal is said in the daemon's words", bool(until(lambda: note.count() > 0, timeout=4))
          and "One device action at a time" in note.inner_text())


def _notice_checks(page, mock, check, until, fake):
    _reset(page, mock, until)
    check("setup: the view draws", _ready(page, until))
    mock.emit("vex_device", {"action": "download", "at": "2026-10-11T12:30:00+00:00"})
    n = page.locator(_view(V, _tid("vex-device-notice")))
    check("a download this window did not start is said", bool(until(lambda: n.count() > 0, timeout=4))
          and "did not start" in n.inner_text())


# -- the review round (one section per finding) -------------------------------------------------

def _chain_view(n: int):
    return _view(n, _tid("vex-chain"))


def _posted_since(mock, base: int, path: str) -> list:
    """POSTs to `path` since `base` (mock.calls is the whole run's)."""
    return [b for (m, p, b) in mock.calls[base:] if m == "POST" and p == path]


def _switch_view(page, n: int, view: str):
    """The pane's view strip: its buttons, or its select in a compact pane."""
    pick = page.locator(_tid(f"pane-{n}-view-select"))
    if pick.count():
        pick.select_option(view)
    else:
        page.locator(_pane(n, _tid(f"tab-{view}"))).click()


def _one_chain_checks(page, mock, check, until, fake):
    """M1: one chain per window; every VEX view shows it; Cancel stops the build and sends nothing."""
    _reset(page, mock, until, {"preset": "cols3", "focused": 2,
                               "panes": [{"view": "chat"}, {"view": "vex", "projectId": "p3"},
                                         {"view": "vex", "projectId": "p5"}],
                               "panel": {"open": False, "height": 260}})
    base = len(mock.calls)
    check("M1 setup: both views draw", _ready(page, until)
          and bool(until(lambda: page.locator(_view(3, _tid("vex-card"))).count() > 0, timeout=5)))
    v = mock_vex.state(mock)
    v["bin_fresh"] = False
    page.locator(_view(2, _tid("vex-download-btn"))).click()
    until(lambda: _posted_since(mock, base, "/projects/p3/vex/build"), timeout=4)
    bar2, bar3 = page.locator(_chain_view(2)), page.locator(_chain_view(3))
    want = "Building override, then downloading to slot 1"
    check("M1: the running chain is said in its own view",
          bool(until(lambda: bar2.count() > 0 and want in bar2.inner_text(), timeout=4)),
          bar2.inner_text() if bar2.count() else "")
    check("M1: and in every other VEX view, whatever project it shows",
          bar3.count() > 0 and want in bar3.inner_text(), bar3.inner_text() if bar3.count() else "")
    other = page.locator(_view(3, _tid("vex-download-btn")))
    check("M1: another project's Download is off while it runs", other.is_disabled())
    check("M1: so are the brain terminal and device info",
          page.locator(_view(3, _tid("vex-brain-btn"))).is_disabled()
          and page.locator(_view(3, _tid("vex-info-btn"))).is_disabled()
          and page.locator(_view(3, _tid("vex-programs-btn"))).is_disabled())
    check("M1: and every VEX pane's project picker",
          page.locator(_tid("pane-2-vex-project")).is_disabled()
          and page.locator(_tid("pane-3-vex-project")).is_disabled())
    try:
        other.click(force=True, timeout=500)
    except Exception:
        pass
    time.sleep(0.3)
    check("M1: a click on it sends nothing", not _posted_since(mock, base, "/projects/p5/vex/download")
          and not [d for d in v["downloads"] if d["project_id"] == "p5"], str(v["downloads"]))
    # The reviewer's path: the chain's own pane moved to another project mid-build
    # (to File, its project picked there, back to VEX).
    _switch_view(page, 2, "file")
    page.locator(_tid("pane-2-folder")).select_option("p5")
    _switch_view(page, 2, "vex")
    until(lambda: page.locator(_view(2)).get_attribute("data-project") == "p5", timeout=4)
    check("M1: a pane moved to another project mid-build still says what is running",
          page.locator(_view(2)).get_attribute("data-project") == "p5" and bar2.count() > 0
          and want in bar2.inner_text())
    check("M1: and its Download is off", page.locator(_view(2, _tid("vex-download-btn"))).is_disabled())
    # Cancel: the build's program is stopped and nothing is sent.
    bid = _latest_build(mock)
    btid = v["builds"][bid]["terminal_id"]
    page.locator(_view(3, _tid("vex-chain-cancel"))).click()
    check("M1: Cancel stops the build's program (its terminal closed)",
          bool(until(lambda: any(c[0] == "DELETE" and c[1] == f"/terminals/{btid}" for c in mock.calls[base:]), timeout=4)))
    check("M1: the chain ends", bool(until(lambda: bar2.count() == 0 and bar3.count() == 0, timeout=6)))
    time.sleep(0.5)
    check("M1: and nothing was downloaded", not _sent_downloads(mock) and len(v["downloads"]) == 1,
          str(v["downloads"]))
    check("M1: the pickers come back", page.locator(_tid("pane-2-vex-project")).is_enabled()
          and page.locator(_tid("pane-3-vex-project")).is_enabled())
    # p3's view says it was cancelled.
    page.locator(_tid("pane-2-vex-project")).select_option("p3")
    note = page.locator(_view(2, _tid("vex-note")))
    check("M1: the chain's project says it was cancelled",
          bool(until(lambda: note.count() > 0 and "Cancelled" in note.inner_text(), timeout=4)),
          note.inner_text() if note.count() else "")


def _header_rebuild_checks(page, mock, check, until, fake):
    """Delta review MED-1: make found nothing to do after a header it does not list
    changed, so the daemon still says Build first — one rebuild, then the download."""
    _reset(page, mock, until)
    base = len(mock.calls)
    check("MED-1 setup: the view draws", _ready(page, until))
    v = mock_vex.state(mock)
    v["bin_fresh"] = False
    v["stale_until_rebuild"] = True
    v["build_auto"] = {"state": "succeeded"}
    page.locator(_view(V, _tid("vex-download-btn"))).click()
    check("MED-1: a build that leaves it stale is followed by one rebuild, on the one click",
          bool(until(lambda: _posted_since(mock, base, "/projects/p3/vex/build") == [{"action": "build"},
                                                                                      {"action": "rebuild"}],
                     timeout=8)),
          str(_posted_since(mock, base, "/projects/p3/vex/build")))
    check("MED-1: then the download goes", bool(until(lambda: _sent_downloads(mock), timeout=8))
          and len(v["downloads"]) == 3, str(v["downloads"]))
    done = page.locator(_view(V, _tid("vex-downloaded")))
    check("MED-1: and the view says it is in the slot",
          bool(until(lambda: done.count() > 0 and "slot 1" in done.inner_text(), timeout=6)))


def _still_stale_checks(page, mock, check, until, fake):
    """M2 and MED-1: still stale after a build and a rebuild — stops, never loops."""
    _reset(page, mock, until)
    base = len(mock.calls)
    check("M2 setup: the view draws", _ready(page, until))
    v = mock_vex.state(mock)
    v["bin_fresh"] = False
    v["stays_stale"] = True
    v["build_auto"] = {"state": "succeeded"}
    page.locator(_view(V, _tid("vex-download-btn"))).click()
    note = page.locator(_view(V, _tid("vex-note")))
    check("M2: still stale after a build and a rebuild, it says so and sends nothing",
          bool(until(lambda: note.count() > 0 and "after a build and a rebuild" in note.inner_text(), timeout=10)),
          note.inner_text() if note.count() else "")
    time.sleep(1.0)
    check("M2: one build, one rebuild and three tries, never a loop",
          _posted_since(mock, base, "/projects/p3/vex/build") == [{"action": "build"}, {"action": "rebuild"}]
          and len(v["downloads"]) == 3 and not _sent_downloads(mock),
          f"{_posted_since(mock, base, '/projects/p3/vex/build')}, {v['downloads']}")


def _answer_card_checks(page, mock, check, until, fake):
    """L1: a card that comes up while the daemon answers "Build first" builds nothing."""
    _reset(page, mock, until)
    base = len(mock.calls)
    check("L1 setup: the view draws", _ready(page, until))
    v = mock_vex.state(mock)
    v["bin_fresh"] = False
    v["download_delay"] = 1.2
    page.locator(_view(V, _tid("vex-download-btn"))).click()
    until(lambda: v["downloads"], timeout=4)
    _approval(mock, "vexcardL1")
    until(lambda: page.locator(_tid("approval-card")).count() > 0, timeout=4)
    note = page.locator(_view(V, _tid("vex-note")))
    until(lambda: note.count() > 0 and "approval card" in note.inner_text(), timeout=6)
    time.sleep(0.4)
    check("L1: a card up when the daemon answered Build first: nothing builds behind it",
          not _posted_since(mock, base, "/projects/p3/vex/build"), str(_posted_since(mock, base, "/projects/p3/vex/build")))
    check("L1: and the view says so", note.count() > 0 and "nothing was downloaded" in note.inner_text())
    page.keyboard.press("Escape")
    until(lambda: page.locator(_tid("approval-card")).count() == 0, timeout=4)
    time.sleep(0.4)
    check("L1: answering the card builds and sends nothing either",
          not _posted_since(mock, base, "/projects/p3/vex/build") and not _sent_downloads(mock))


def _passing_card_checks(page, mock, check, until, fake):
    """L2 and L6: a card that comes and goes while it builds drops the download; the brain stays closed."""
    _reset(page, mock, until)
    base = len(mock.calls)
    check("L2 setup: the view draws", _ready(page, until))
    page.locator(_view(V, _tid("vex-brain-btn"))).click()
    until(lambda: mock_vex.state(mock)["brain"], timeout=4)
    v = mock_vex.state(mock)
    v["bin_fresh"] = False
    page.locator(_view(V, _tid("vex-download-btn"))).click()
    until(lambda: _posted_since(mock, base, "/projects/p3/vex/build"), timeout=6)
    bid = _latest_build(mock)
    _approval(mock, "vexcardL2")
    until(lambda: page.locator(_tid("approval-card")).count() > 0, timeout=4)
    page.keyboard.press("Escape")
    until(lambda: page.locator(_tid("approval-card")).count() == 0, timeout=4)
    time.sleep(0.3)
    _finish(mock, fake, bid, "succeeded")
    note = page.locator(_view(V, _tid("vex-note")))
    until(lambda: note.count() > 0 and "approval card" in note.inner_text(), timeout=6)
    time.sleep(0.5)
    check("L2: a card that came and went while it built drops the download",
          not _sent_downloads(mock) and len(v["downloads"]) == 1, str(v["downloads"]))
    check("L2: and the view says so", note.count() > 0 and "nothing was downloaded" in note.inner_text(),
          note.inner_text() if note.count() else "")
    brains = _posted_since(mock, base, "/vex/brain")
    check("L6: the dropped chain never opens the brain terminal again",
          len(brains) == 1 and mock_vex.state(mock)["brain"] is None, f"{len(brains)} brain posts")
    bnote = page.locator(_view(V, _tid("vex-brain-note")))
    check("L6: and says it stays closed, and how to open it",
          bnote.count() > 0 and "closed for the download" in bnote.inner_text()
          and "Open the brain terminal" in bnote.inner_text() and "paused" not in bnote.inner_text(),
          bnote.inner_text() if bnote.count() else "")


def _first_click_checks(page, mock, check, until, fake):
    """L3: only a click's first click counts: a double or triple click is one download or none."""
    _reset(page, mock, until)
    base = len(mock.calls)
    check("L3 setup: the view draws", _ready(page, until))
    v = mock_vex.state(mock)
    btn = page.locator(_view(V, _tid("vex-download-btn")))
    box = btn.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    def idle():
        return btn.is_enabled() and page.evaluate("window.__hudVex.chain()") is None

    page.mouse.move(x, y)
    for count in (2, 3):
        page.mouse.down(click_count=count)
        page.mouse.up(click_count=count)
    time.sleep(0.6)
    check("L3: a click whose detail is 2 or 3 (a double or triple click's later clicks) sends nothing",
          not v["downloads"], str(v["downloads"]))
    btn.dblclick()
    until(lambda: _sent_downloads(mock), timeout=4)
    until(idle, timeout=6)
    time.sleep(0.4)
    check("L3: a double click is one download", len(v["downloads"]) == 1, str(v["downloads"]))
    btn.click(click_count=3)
    until(lambda: len(v["downloads"]) >= 2, timeout=4)
    until(idle, timeout=6)
    time.sleep(0.4)
    check("L3: a triple click is one download", len(v["downloads"]) == 2, str(v["downloads"]))


def _detect_again_checks(page, mock, check, until, fake):
    """L4: a project that becomes a VEX project is offered the view without a reload."""
    _reset(page, mock, until, {"preset": "cols2", "focused": 2,
                               "panes": [{"view": "chat"}, {"view": "file", "projectId": "p6"}],
                               "panel": {"open": False, "height": 260}})
    base = len(mock.calls)
    until(lambda: any(c[1] == "/projects/p6/vex" for c in mock.calls[base:]), timeout=4)
    time.sleep(0.3)
    check("L4: an ordinary project offers no VEX view", page.locator(_pane(2, _tid("tab-vex"))).count() == 0)
    mock_vex.state(mock)["projects"]["p6"] = mock_vex.vex_project("p6", "intake")
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    check("L4: once it is one, the window's focus offers it",
          bool(until(lambda: page.locator(_pane(2, _tid("tab-vex"))).count() > 0, timeout=5)))


def _lone_pane_checks(page, mock, check, until, fake):
    """L5: a lone VEX pane comes back as itself after a reload."""
    _reset(page, mock, until, {"preset": "single", "focused": 1, "panes": [{"view": "vex", "projectId": "p3"}],
                               "panel": {"open": False, "height": 260}})
    base = len(mock.calls)
    check("L5: a lone VEX pane boots as the VEX view",
          bool(until(lambda: page.locator(_view(1, _tid("vex-card"))).count() > 0, timeout=5))
          and page.locator(_pane(1)).get_attribute("data-view") == "vex")
    page.reload()
    page.wait_for_selector('[data-testid="sidebar"]', state="attached")
    time.sleep(0.6)
    check("L5: and after a reload it is still the VEX view, not chat",
          bool(until(lambda: page.locator(_view(1, _tid("vex-card"))).count() > 0, timeout=5))
          and page.locator(_pane(1)).get_attribute("data-view") == "vex"
          and page.evaluate("window.__hud.workspace().ws.panes[0].view") == "vex")


def _gone_project_checks(page, mock, check, until, fake):
    """L7: a VEX pane whose pinned project is gone says Pick a project; it never follows the chat."""
    _reset(page, mock, until, {"preset": "cols2", "focused": 2,
                               "panes": [{"view": "chat"}, {"view": "vex", "projectId": "pgone"}],
                               "panel": {"open": False, "height": 260}})
    base = len(mock.calls)
    none = page.locator(_view(2, _tid("vex-none")))
    check("L7: a VEX pane whose project is gone says Pick a project",
          bool(until(lambda: none.count() > 0, timeout=5)), page.locator(_pane(2)).inner_text()[:200])
    check("L7: and shows no project's controls",
          page.locator(_view(2, _tid("vex-card"))).count() == 0
          and page.locator(_view(2, _tid("vex-download-btn"))).count() == 0
          and page.locator(_view(2, _tid("vex-not"))).count() == 0)
    check("L7: its picker says so too",
          page.locator(_tid("pane-2-vex-project")).input_value() == "")
    page.locator(_tid("pane-2-vex-project")).select_option("p3")
    check("L7: picking a project shows it",
          bool(until(lambda: page.locator(_view(2, _tid("vex-card"))).count() > 0, timeout=5)))


def _ed7_beside_checks(page, mock, check, until, fake):
    """ED-7 with a VEX view focused (the lead merge of #51 and #53): quick
    open opens the file beside the view and never in its place, and neither
    quick open nor the palette offers a download."""
    _reset(page, mock, until)
    check("setup: the view draws", _ready(page, until))
    page.locator(_view(V, _tid("vex-name-input"))).focus()
    page.keyboard.press("Control+p")
    qo = page.locator(_tid("quickopen"))
    check("ED-7: Ctrl+P with the VEX view focused opens quick open", bool(until(lambda: qo.count() == 1, timeout=3)))
    page.keyboard.type("main.cpp")
    rows = lambda: page.locator(_tid("quickopen-row")).evaluate_all("els => els.map(e => e.dataset.key)")  # noqa: E731
    until(lambda: (rows() or [""])[0] == "src/main.cpp", timeout=3)
    check("ED-7: quick open offers files, never a download",
          not [r for r in rows() if re.search(r"download|vexcom|brain|device", r, re.I)], str(rows()))
    page.keyboard.press("Enter")
    check("ED-7: the file opens in a File pane beside the VEX view, which stays",
          bool(until(lambda: page.locator(_pane(3)).get_attribute("data-view") == "file", timeout=5))
          and page.locator(_pane(V)).get_attribute("data-view") == "vex"
          and page.locator(_view(V, _tid("vex-card"))).count() == 1)
    path = page.locator(_pane(3, _tid("file-path")))
    check("ED-7: at that file", bool(until(lambda: path.count() and path.inner_text() == "src/main.cpp", timeout=5)))
    page.locator(_view(V, _tid("vex-name-input"))).focus()
    page.keyboard.press("Control+Shift+P")
    pal = page.locator(_tid("palette"))
    until(lambda: pal.count() == 1, timeout=3)
    labels = page.locator(_tid("palette-row")).evaluate_all("els => els.map(e => e.innerText)")
    check("ED-7: the palette offers no download or VEX device command",
          bool(labels) and not [x for x in labels if re.search(r"download|vexcom|brain|device|robot", x, re.I)],
          str([x for x in labels if re.search(r"download|vex", x, re.I)]))
    page.locator(_view(V, _tid("vex-name-input"))).focus()
    check("ED-7: the palette closes when focus goes back to the view", bool(until(lambda: pal.count() == 0, timeout=2)))
    check("ED-7: and the download button is reachable", page.locator(_view(V, _tid("vex-download-btn"))).is_visible())


def _feedback_checks(page, mock, check, until, fake):
    _reset(page, mock, until)
    v = mock_vex.state(mock)
    v["download_auto"] = False
    page.get_by_test_id("vex-download-btn").click()
    assert until(lambda: page.get_by_test_id("vex-download-output").count() == 1)
    check("Download shows actual live plain-text output", "<b>fixture</b>" in page.get_by_test_id("vex-download-output").inner_text()
          and page.get_by_test_id("vex-download-output").locator("b").count() == 0)
    check("exited terminal listing cannot invent Download completion", page.evaluate("() => window.__hudVex.run('p3').busy") == "download"
          and page.get_by_test_id("vex-downloaded").count() == 0)
    rec = next(iter(v["download_records"].values()))
    bar = page.get_by_test_id("vex-download-progress")
    check("no progress bar before vexcom prints its progress line", bar.count() == 0)
    rec.update(output="Device: COM3\nvexcom utility V1.0.2b2\ndownload :\n0....10....20....30..")
    check("the progress bar follows vexcom's progress line",
          bool(until(lambda: bar.count() == 1 and bar.get_attribute("aria-valuenow") == "34", timeout=4))
          and "34%" in bar.inner_text())
    rec.update(output="Device: COM3\nvexcom utility V1.0.2b2\ndownload :\n0....10....20....30....40....50....60....70.")
    check("and moves as the download goes", bool(until(lambda: bar.get_attribute("aria-valuenow") == "72", timeout=4)))
    rec.update(state="succeeded", exit_code=0, output="Finished <script>fixture</script>", ended="2026-10-13T12:00:02+00:00")
    assert until(lambda: page.get_by_test_id("vex-downloaded").count() == 1)
    check("a finished download shows a full bar", bar.get_attribute("aria-valuenow") == "100")
    check("Download settles visibly and retains output/terminal control", "In slot" in page.get_by_test_id("vex-downloaded").inner_text()
          and "Finished <script>fixture</script>" in page.get_by_test_id("vex-download-output").inner_text()
          and page.get_by_test_id("vex-download-output-btn").is_visible())
    from datetime import datetime, timezone
    at = datetime.now(timezone.utc).isoformat()
    v["connection"] = {"brain": True, "controller": False, "checked_at": at, "state": "ready", "message": "USB check complete.",
        "events": [{"device": "brain", "connected": True, "at": at}, {"device": "controller", "connected": False, "at": at}]}
    assert until(lambda: page.get_by_test_id("vex-usb-brain").inner_text().startswith("Brain: Connected"))
    check("Brain/Controller and tooling have distinct indicators", page.get_by_test_id("vex-usb-brain").inner_text() == "Brain: Connected"
          and page.get_by_test_id("vex-usb-controller").inner_text() == "Controller: Disconnected"
          and "Tooling: Configured" in page.get_by_test_id("vex-tooling").inner_text())
    color = lambda tid: page.get_by_test_id(tid).evaluate("e => getComputedStyle(e).color")
    css = lambda name: page.evaluate(f"() => {{ const e = document.createElement('span'); e.style.color = 'var({name})'; document.body.append(e); const c = getComputedStyle(e).color; e.remove(); return c; }}")
    check("connected is green and disconnected red", color("vex-usb-brain") == css("--green")
          and color("vex-usb-controller") == css("--red"), f"{color('vex-usb-brain')} {color('vex-usb-controller')}")
    page.get_by_test_id("vex-connection").locator("summary").click()
    check("connection log contains timestamps and confirmed transitions", page.get_by_test_id("vex-connection-log").locator("time").count() == 2)
    v["connection"].update(state="error", message="Unsupported USB response; retained.")
    assert until(lambda: "last confirmed" in page.get_by_test_id("vex-usb-brain").inner_text())
    check("USB errors preserve last confirmed hardware state", "Connected (last confirmed)" in page.get_by_test_id("vex-usb-brain").inner_text()
          and "Unsupported" in page.get_by_test_id("vex-connection-note").inner_text())
    v["connection"].update(state="ready", brain=False, checked_at=at,
        events=[*v["connection"]["events"], {"device": "brain", "connected": False, "at": at}])
    assert until(lambda: page.get_by_test_id("vex-usb-brain").inner_text() == "Brain: Disconnected")
    check("confirmed disconnect has a timestamped log entry", page.get_by_test_id("vex-connection-log").locator("time").count() == 3)
    page.set_viewport_size({"width": 375, "height": 812})
    try:
        connection = page.get_by_test_id("vex-connection")
        output = page.get_by_test_id("vex-download-output")
        def geometry():
            return page.evaluate("""() => {
              const shape=id=>{const e=document.querySelector('[data-testid="'+id+'"]');
                return e?{rect:e.getBoundingClientRect().toJSON(),scrollWidth:e.scrollWidth,clientWidth:e.clientWidth}:null};
              return {width:innerWidth,connection:shape('vex-connection'),output:shape('vex-download-output')};
            }""")
        assert until(lambda: _ws(page)["fit"]["panes"] == [V] and connection.is_visible() and output.is_visible()
                     and connection.evaluate("e=>e.getBoundingClientRect().width<=innerWidth")), "narrow VEX pane did not settle: " + json.dumps(geometry())
        fits = all(el.evaluate("e=>e.scrollWidth<=e.clientWidth+1 && e.getBoundingClientRect().left>=0 && e.getBoundingClientRect().right<=innerWidth+1")
                   for el in (connection, output))
        check("USB status and Download output fit a 375px VEX pane", fits, "" if fits else json.dumps(geometry()))
        import os
        capture = os.environ.get("VEX_FEEDBACK_SCREENSHOT")
        if capture:
            page.screenshot(path=capture, full_page=True)
    finally:
        page.set_viewport_size({"width": VIEW[0], "height": VIEW[1]})


SECTIONS = (
    _feedback_checks, _offer_checks, _setup_checks, _build_checks, _clean_output_checks, _explicit_click_checks, _chain_checks, _card_checks,
            _brain_checks, _info_checks, _notice_checks, _one_chain_checks, _header_rebuild_checks,
            _still_stale_checks,
            _answer_card_checks, _passing_card_checks, _first_click_checks, _detect_again_checks,
            _lone_pane_checks, _gone_project_checks, _ed7_beside_checks)


def vex_checks(browser, mock, base, check, until, guard, init_script):
    print("\nVEX view (VX-2)")
    ctx = browser.new_context(viewport={"width": VIEW[0], "height": VIEW[1]}, permissions=["microphone"])
    guard(ctx)
    ctx.add_init_script(init_script)
    fake = FakePty(ctx, mock)
    page = ctx.new_page()
    until = pumping(page, until, fake)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    only = set(filter(None, (sys.argv[1] if len(sys.argv) > 1 and __name__ == "__main__" else "").split(",")))
    keep_files = {k: dict(v) for k, v in mock.world["files"].items()}
    try:
        _world(mock)
        _boot(page, mock, base, until)
        for section in SECTIONS:
            if only and section.__name__.strip("_") not in only:
                continue
            try:
                section(page, mock, check, until, fake)
            except Exception as e:  # a section that cannot run is a failure; the rest still run
                import traceback
                traceback.print_exc()
                lines = [ln.strip() for ln in str(e).splitlines() if ln.strip()]
                check(f"{section.__name__.strip('_')} ran to the end", False, " | ".join(lines[:3])[:300])
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        check("no page errors in the VEX section", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()
        mock.world["files"].clear()
        mock.world["files"].update(keep_files)


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
            vex_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until, v2.guard,
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
    print(f"all {v2.CHECKS} VEX checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
