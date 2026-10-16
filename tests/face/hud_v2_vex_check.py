"""Headless checks for the HUD's VEX view (VX-2; editor plan §2.9, §3's VX-2
package; decisions V-1..V-5; redesigned 2026-10-15, VX-UI-1..3 of
docs/plans/2026-10-15-vex-ui-redesign.md and the owner's decisions in its §0.1).

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
    in place of the action bar, and builds and downloads nothing;
  - **Build shows ✓ or the errors list**, the build's output is a program
    terminal in the panel and in the Build tab, and **an error click opens
    the file at its line** in the editor (a File pane beside the view), while
    an error outside the project is text only; a saved source makes Build's
    ✓ "edited since" (advisory);
  - **there is one Download button, and it needs the owner's click**: Enter
    or Space on it, a script's `.click()`, an untrusted click event, a
    double or triple click's later clicks and a battery of keys send nothing;
    its text is "Download to slot N" and "replaces whatever is in slot N" is
    its description, hover help and the slot popover's warning; a bad name is
    refused (the chip turns red) before anything is sent;
  - **one click is clean → build → download** (owner decision 3): a failed
    clean or build sends nothing, and **a card that comes up while it builds
    drops the download**;
  - **Stop** (owner decision 6) stops a running clean, build or **download**
    on a trusted pointer or key press, never on a script's click; a stopped
    download says the slot may be incomplete, never success; it never asks
    for anything but closing the action's own terminal (no `vexcom --stop`);
  - **under a card every VEX control is disabled and inert**, the slot
    popover and ⋯ menu close, the status line says Held in amber, and no
    hover help shows;
  - the output area: an action selects its tab, background events do not;
    tab badges follow state; fold, fill and a keyboard tablist; every control
    keeps its hover help (owner decision 7) and `?` lists them all;
  - **the Brain tab** (VX-UI-4a) draws the brain terminal in the VEX pane
    (`vex-<pane>`), the same session with its "Jarvis can read" switch,
    input and Ctrl-C; **it connects only on a trusted Connect** (pointer or
    Enter/Space; owner decision 5) — never when the tab is shown, on a reload
    or after a download, and never on a script's click or behind a card; it
    says not connected, refused (the daemon's words), connected, paused for a
    download (Connect off, with the reason), ended (exit code, Connect
    again) and shown elsewhere (another VEX pane, a terminal pane; the panel
    says it is in the VEX view); Disconnect closes it; a reload reattaches a
    link already open; a download closes it only once its build has
    succeeded, and **never reopens it**;
  - device info (`vexcom --json`) and the programs list (`vexcom --programs`)
    each ask their own read (⋯) and are drawn as text in the Device tab:
    markup in them is characters;
  - a device action this window did not start is said;
  - the review round (one check per finding): **one download chain per
    window** — refused for another project while one runs, shown in every
    VEX view with Stop, which stops its build and sends nothing, the pickers
    off meanwhile (M1); a build-first that still ends stale says Rebuild,
    never loops (M2); a card that comes up while the daemon answers builds
    nothing more (L1), and one that comes and goes while it builds drops the
    download (L2); only a click's first click counts (L3); a project that
    becomes a VEX project is offered the view on focus (L4); a lone VEX pane
    survives a reload (L5); a dropped or stopped chain never reopens the
    brain terminal (L6, in `_stop_checks`; since VX-UI-4a no chain does,
    `_brain_checks`); a VEX pane whose project is gone
    says "Pick a project" (L7).

Run (after `cd hud && npm ci --ignore-scripts && npm run build`):
    PYTHONPATH=. .venv/bin/python tests/face/hud_v2_vex_check.py [section,...]
`VEX_SCREENSHOTS=<dir>` saves the key states as PNGs there.
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


def _auto(mock):
    """Builds end by themselves (on their second status read): a Download's clean and build go on."""
    mock_vex.state(mock)["build_auto"] = {"state": "succeeded"}


def _slot_open(page, n: int = V):
    """The slot chip's popover, opened if it is not."""
    if page.locator(_view(n, _tid("vex-slot-pop"))).count() == 0:
        page.locator(_view(n, _tid("vex-slot-chip"))).click()
    page.locator(_view(n, _tid("vex-slot-pop"))).wait_for(state="visible", timeout=3000)


def _slot_close(page, n: int = V):
    pop = page.locator(_view(n, _tid("vex-slot-pop")))
    if pop.count():
        page.locator(_view(n, _tid("vex-slot-pick"))).press("Escape")
        pop.wait_for(state="detached", timeout=3000)


def _more(page, item: str, n: int = V):
    """⋯ then one of its items."""
    page.locator(_view(n, _tid("vex-more"))).click()
    page.locator(_view(n, _tid(item))).click()


def _tab(page, name: str, n: int = V):
    page.locator(_view(n, _tid(f"vex-out-tab-{name}"))).click()


def _described(page, sel: str) -> str:
    """The text an element's `aria-describedby` names (its hover help)."""
    return page.locator(sel).evaluate(
        "e => (e.getAttribute('aria-describedby') || '').split(' ').map(i => document.getElementById(i))"
        ".filter(Boolean).map(x => x.textContent).join(' ')")


def _radii(page, sel: str) -> list:
    return page.locator(sel).evaluate_all(
        "els => els.map(e => parseFloat(getComputedStyle(e).borderTopLeftRadius))")


def _css(page, name: str) -> str:
    return page.evaluate(f"() => {{ const e = document.createElement('span'); e.style.color = 'var({name})';"
                         " document.body.append(e); const c = getComputedStyle(e).color; e.remove(); return c; }")


def _shot(page, name: str, n: int = V, hover: bool = False):
    """A key state for the owner (`VEX_SCREENSHOTS=<dir>`): the VEX pane only."""
    import os
    out = os.environ.get("VEX_SCREENSHOTS")
    if not out:
        return
    Path(out).mkdir(parents=True, exist_ok=True)
    if not hover:
        page.mouse.move(1, 1)
    time.sleep(0.6)
    page.locator(_pane(n)).screenshot(path=str(Path(out) / f"{name}.png"))


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
    lang = page.locator(_view(2, _tid("vex-language")))
    check("the VEX view's header shows the project (name, language, slot; the SDK in the language's title)",
          "override" in card and "C++" in card and "Slot 1" in card
          and "V5_20240802" in (lang.get_attribute("title") or ""), card)
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
    # Earlier sections legitimately POST /vex/brain (their Connect), and
    # mock.calls is the whole run's: judge only what this section asked for.
    base = len(mock.calls)
    _reset(page, mock, until, configured=False)
    check("setup: the view draws", _ready(page, until))
    note = page.locator(_view(V, _tid("vex-setup")))
    until(lambda: note.count() > 0, timeout=4)
    cmd = page.locator(_view(V, _tid("vex-setup-command")))
    check("with no vex.json the view names the one command to run",
          cmd.count() == 1 and cmd.inner_text().strip() == "jarvis vex setup", note.inner_text() if note.count() else "")
    check("in place of the action bar: no Build or Download button at all",
          page.locator(_view(V, _tid("vex-actionbar"))).count() == 0
          and all(page.locator(_view(V, _tid(t))).count() == 0
                  for t in ("vex-build-btn", "vex-rebuild-btn", "vex-clean-btn", "vex-download-btn")))
    check("and nothing was asked of the daemon but its status",
          not _posted_since(mock, base, "/projects/p3/vex/build")
          and not _posted_since(mock, base, "/projects/p3/vex/download")
          and not _posted_since(mock, base, "/vex/brain"))
    mock_vex.state(mock)["status"] = mock_vex.state(mock).get("status_ok") or {
        "configured": True, "hint": None, "sdks": {"cpp": "V5_20240802_15_00_00", "python": None}}
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    check("once setup has run, the window's focus brings the action bar back",
          bool(until(lambda: note.count() == 0 and page.locator(_view(V, _tid("vex-build-btn"))).count() == 1
                     and page.locator(_view(V, _tid("vex-build-btn"))).is_enabled(), timeout=4)))


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
    check("the status line says it is building", "Building" in state.inner_text()
          and page.locator(_view(V, _tid("vex-status"))).get_attribute("data-state") == "busy")
    mark = page.locator(_view(V, _tid("vex-mark-build")))
    check("Build's mark is running (cyan), Clean's is idle",
          mark.get_attribute("data-state") == "busy" and mark.evaluate("e => getComputedStyle(e).color") == _css(page, "--accent")
          and page.locator(_view(V, _tid("vex-mark-clean"))).get_attribute("data-state") == "idle")
    check("the Build tab is selected and its badge is running",
          page.locator(_view(V, _tid("vex-out-panel"))).get_attribute("data-tab") == "build"
          and page.locator(_view(V, _tid("vex-out-tab-build"))).get_attribute("data-state") == "busy")
    check("a silent build shows elapsed time and explains that it is waiting for output",
          page.locator(_view(V, _tid("vex-build-elapsed"))).count() == 1
          and "Waiting for tool output" in page.locator(_view(V, _tid("vex-build-waiting"))).inner_text())
    _shot(page, "02-building")
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
    check("Show terminal (the Build tab's toolbar) reopens the full build output",
          bool(until(lambda: tab.count() > 0, timeout=4)))
    _finish(mock, fake, bid, "succeeded")
    check("a build that succeeds shows ✓",
          bool(until(lambda: state.inner_text().startswith("✓ Built"), timeout=4)), state.inner_text())
    check("and its mark is ✓ in green", mark.get_attribute("data-state") == "ok" and mark.inner_text() == "✓"
          and mark.evaluate("e => getComputedStyle(e).color") == _css(page, "--green")
          and mark.get_attribute("aria-label") == "Build: succeeded")
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
    check("its mark is ✗ in red, said in words, and the Build tab's badge too",
          mark.get_attribute("data-state") == "failed" and mark.inner_text() == "✗"
          and mark.evaluate("e => getComputedStyle(e).color") == _css(page, "--red")
          and mark.get_attribute("aria-label") == "Build: failed (1 error)"
          and page.locator(_view(V, _tid("vex-out-tab-build"))).get_attribute("aria-label") == "Build, failed")
    check("amber is never a result: no mark is amber",
          _css(page, "--amber") not in page.locator(_view(V, "[data-testid^=vex-mark-]")).evaluate_all(
              "els => els.map(e => getComputedStyle(e).color)"))
    _shot(page, "03-build-failed")
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

    # A build that succeeds, then a source saved in the editor: ✓ dimmed, "edited since" (advisory).
    page.locator(_view(V, _tid("vex-build-btn"))).click()
    until(lambda: len(mock_vex.state(mock)["builds"]) == 3, timeout=4)
    _finish(mock, fake, _latest_build(mock), "succeeded")
    until(lambda: mark.get_attribute("data-state") == "ok", timeout=4)
    page.locator(_pane(3, ".view-lines")).click()
    page.keyboard.press("Control+End")
    page.keyboard.type("// edited\n")
    page.keyboard.press("Control+s")
    check("a saved source makes Build's ✓ stale: dimmed, and said in words",
          bool(until(lambda: mark.get_attribute("data-state") == "stale", timeout=5))
          and mark.get_attribute("aria-label") == "Build: succeeded, edited since"
          and mark.evaluate("e => getComputedStyle(e).opacity") == "0.6",
          mark.get_attribute("aria-label") or "")
    check("stale disables nothing: Download and Build stay on",
          page.locator(_view(V, _tid("vex-download-btn"))).is_enabled()
          and page.locator(_view(V, _tid("vex-build-btn"))).is_enabled())


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
    check("Clean's mark is ✓", page.locator(_view(V, _tid("vex-mark-clean"))).get_attribute("data-state") == "ok")
    page.locator(_view(V, _tid("vex-rebuild-btn"))).click()
    check("Rebuild is its own visible button: exactly {action: rebuild}",
          bool(until(lambda: _posted(mock, "/projects/p3/vex/build")[-1] == {"action": "rebuild"}, timeout=4)))
    _finish(mock, fake, _latest_build(mock))
    check("and its own mark", bool(until(lambda: page.locator(_view(V, _tid("vex-mark-rebuild"))).get_attribute(
        "data-state") == "ok", timeout=4)))


def _explicit_click_checks(page, mock, check, until, fake):
    """The one Download button (the build row's and the pickers' merged,
    plan §10): every refusal both used to have."""
    before = len(mock.calls)
    _reset(page, mock, until)
    _auto(mock)
    check("setup: the view draws", _ready(page, until))
    btn = page.locator(_view(V, _tid("vex-download-btn")))
    sel = _view(V, _tid("vex-download-btn"))
    check("there is exactly one Download control",
          page.locator(_view(V, "button")).filter(has_text=re.compile(r"^Download to slot")).count() == 1
          and page.locator(_view(V, _tid("vex-download-quick"))).count() == 0)
    check("its text says where it goes (owner decision 2)", btn.inner_text().strip() == "Download to slot 1",
          btn.inner_text())
    check("and its description says what it replaces",
          "Download to slot 1: replaces whatever is in slot 1 on the brain" in _described(page, sel)
          and "Mouse click only" in _described(page, sel), _described(page, sel))
    _slot_open(page)
    page.locator(_view(V, _tid("vex-slot-pick"))).select_option("3")
    check("and follows the slot picked in the chip's popover",
          btn.inner_text().strip() == "Download to slot 3"
          and "replaces whatever is in slot 3 on the brain" in _described(page, sel)
          and "Slot 3" in page.locator(_view(V, _tid("vex-slot-chip"))).inner_text())
    check("the popover says it too",
          "replaces whatever is in slot 3 on the brain" in page.locator(_view(V, _tid("vex-slot-warning"))).inner_text())
    check("the popover's focus went to the slot select", page.evaluate(
        "document.activeElement && document.activeElement.getAttribute('data-testid')") == "vex-slot-pick")
    check("it is out of the Tab order", btn.get_attribute("tabindex") == "-1")
    # Every way but the owner's own click.
    page.locator(_view(V, _tid("vex-name-input"))).focus()
    for key in ("Enter", "Control+Enter", "Control+Shift+D", "Control+s", "Control+b", "F2",
                "Control+Shift+P", "Control+k"):
        page.keyboard.press(key)
    _slot_close(page)
    check("Escape closes the popover and gives focus back to the chip",
          page.locator(_view(V, _tid("vex-slot-pop"))).count() == 0 and page.evaluate(
              "document.activeElement && document.activeElement.getAttribute('data-testid')") == "vex-slot-chip")
    btn.focus()
    page.keyboard.press("Enter")
    page.keyboard.press("Space")
    page.keyboard.press("Control+Enter")
    btn.evaluate("e => e.click()")
    btn.dispatch_event("click")
    page.locator("body").focus()
    for key in ("Enter", "Control+Enter", "Control+Shift+P"):
        page.keyboard.press(key)
    # The palette Ctrl+Shift+P opened would take the clicks below: closed first.
    page.keyboard.press("Escape")
    assert until(lambda: page.locator(_tid("palette")).count() == 0, timeout=3), "the palette stayed open"
    box = btn.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    for count in (2, 3):
        page.mouse.down(click_count=count)
        page.mouse.up(click_count=count)
    time.sleep(0.5)
    check("no key, script click, untrusted event or later click of a run downloads (or cleans or builds)",
          not mock_vex.state(mock)["downloads"] and not _posted_since(mock, before, "/projects/p3/vex/download")
          and not _posted_since(mock, before, "/projects/p3/vex/build"),
          str(mock_vex.state(mock)["downloads"]))
    # A name the daemon would refuse is refused here first.
    _slot_open(page)
    name = page.locator(_view(V, _tid("vex-name-input")))
    name.fill("-x")
    check("a name starting with - is refused before anything is sent",
          page.locator(_view(V, _tid("vex-name-refusal"))).count() == 1 and btn.is_disabled())
    name.fill("a;b")
    check("and one holding ; too", btn.is_disabled())
    _slot_close(page)
    check("the chip says so in red while the popover is closed",
          "bad" in (page.locator(_view(V, _tid("vex-slot-chip"))).get_attribute("class") or "") and btn.is_disabled())
    _slot_open(page)
    name.fill("auton")
    _slot_close(page)
    # The owner's click.
    btn.click()
    check("the owner's click cleans, then builds, then downloads: exactly {slot, name}",
          bool(until(lambda: _sent_downloads(mock), timeout=8))
          and _posted_since(mock, before, "/projects/p3/vex/build") == [{"action": "clean"}, {"action": "build"}]
          and _posted(mock, "/projects/p3/vex/download")[-1] == {"slot": 3, "name": "auton"},
          f"{_posted_since(mock, before, '/projects/p3/vex/build')} {_posted(mock, '/projects/p3/vex/download')}")
    tid = _sent_downloads(mock)[-1]["terminal_id"] if _sent_downloads(mock) else ""
    check("its progress is a program terminal in the panel",
          bool(until(lambda: page.locator(_tid(f"panel-tab-{tid}")).count() > 0, timeout=4))
          and page.locator(_tid(f"panel-tab-{tid}")).get_attribute("data-program") == "vex-download")
    done = page.locator(_view(V, _tid("vex-downloaded")))
    check("and the view says it is in the slot", bool(until(lambda: done.count() > 0 and "slot 3" in done.inner_text(),
                                                          timeout=6)))
    note = page.locator(_view(V, _tid("vex-note")))
    check("the status line says so in words", note.count() == 1 and "Downloaded to slot 3" in note.inner_text())
    check("Clean, Build and Download each show ✓",
          all(page.locator(_view(V, _tid(f"vex-mark-{k}"))).get_attribute("data-state") == "ok"
              for k in ("clean", "build", "download")))
    check("one click, one download", len(mock_vex.state(mock)["downloads"]) == 1, str(mock_vex.state(mock)["downloads"]))
    check("the click left no focus on the button", page.evaluate(
        "document.activeElement && document.activeElement.getAttribute('data-testid')") != "vex-download-btn")
    _shot(page, "05-downloaded")


def _chain_checks(page, mock, check, until, fake):
    _reset(page, mock, until)
    base = len(mock.calls)
    check("setup: the view draws", _ready(page, until))
    v = mock_vex.state(mock)
    _auto(mock)
    page.locator(_view(V, _tid("vex-download-btn"))).click()
    check("Download cleans, then builds, then downloads, on the one click (owner decision 3)",
          bool(until(lambda: _sent_downloads(mock), timeout=8))
          and _posted_since(mock, base, "/projects/p3/vex/build") == [{"action": "clean"}, {"action": "build"}],
          str(_posted_since(mock, base, "/projects/p3/vex/build")))
    check("one try, sent: no build-first round trip after its own build", len(v["downloads"]) == 1, str(v["downloads"]))

    # The chain shown step by step; a card while it builds drops the download.
    _reset(page, mock, until)
    _ready(page, until)
    base = len(mock.calls)
    v = mock_vex.state(mock)
    page.locator(_view(V, _tid("vex-download-btn"))).click()
    until(lambda: _posted_since(mock, base, "/projects/p3/vex/build"), timeout=4)
    steps = page.locator(_view(V, _tid("vex-chain-steps")) + " li")
    check("the status line lists the steps: Clean running, Build and Download to come",
          bool(until(lambda: steps.evaluate_all("els => els.map(e => e.dataset.state)") == ["busy", "todo", "todo"],
                     timeout=4)), str(steps.evaluate_all("els => els.map(e => e.dataset.state)")))
    check("the step chips are 4px rectangles too, not pills", _radii(page, _view(V, _tid("vex-chain-steps")) + " li") == [4.0] * 3,
          str(_radii(page, _view(V, _tid("vex-chain-steps")) + " li")))
    _finish(mock, fake, _latest_build(mock), "succeeded")
    until(lambda: len(_posted_since(mock, base, "/projects/p3/vex/build")) == 2, timeout=6)
    check("then Clean ✓ and Build running",
          bool(until(lambda: steps.evaluate_all("els => els.map(e => e.dataset.state)") == ["done", "busy", "todo"],
                     timeout=4)))
    check("Clean's mark is ✓ and Build's runs, in the action bar too",
          page.locator(_view(V, _tid("vex-mark-clean"))).get_attribute("data-state") == "ok"
          and page.locator(_view(V, _tid("vex-mark-build"))).get_attribute("data-state") == "busy")
    bid = _latest_build(mock)
    _approval(mock, "vexcard1")
    until(lambda: page.locator(_tid("approval-card")).count() > 0, timeout=4)
    time.sleep(0.3)
    _finish(mock, fake, bid, "succeeded")
    note = page.locator(_view(V, _tid("vex-note")))
    until(lambda: note.count() > 0 and "approval card" in note.inner_text(), timeout=6)
    check("a card that came up while it built drops the download: nothing is sent behind it",
          not _sent_downloads(mock) and not v["downloads"], str(v["downloads"]))
    check("and the view says so", note.count() > 0 and "nothing was downloaded" in note.inner_text(),
          note.inner_text() if note.count() else "")
    page.keyboard.press("Escape")
    until(lambda: page.locator(_tid("approval-card")).count() == 0, timeout=4)
    time.sleep(0.5)
    check("and answering the card sends nothing either", not _sent_downloads(mock), str(v["downloads"]))

    # A failed clean stops the chain before anything reaches the robot.
    _reset(page, mock, until)
    _ready(page, until)
    base = len(mock.calls)
    v = mock_vex.state(mock)
    page.locator(_view(V, _tid("vex-download-btn"))).click()
    until(lambda: _posted_since(mock, base, "/projects/p3/vex/build"), timeout=4)
    _finish(mock, fake, _latest_build(mock), "failed")
    note = page.locator(_view(V, _tid("vex-note")))
    check("a failed clean stops it: no build, nothing sent, and the line says why",
          bool(until(lambda: note.count() > 0 and "clean did not succeed" in note.inner_text(), timeout=6))
          and _posted_since(mock, base, "/projects/p3/vex/build") == [{"action": "clean"}] and not v["downloads"],
          note.inner_text() if note.count() else "")
    check("and the status line is red", page.locator(_view(V, _tid("vex-status"))).get_attribute("data-state") == "failed")


def _card_checks(page, mock, check, until, fake):
    _reset(page, mock, until)
    check("setup: the view draws", _ready(page, until))
    pills = {t: _radii(page, _view(V, _tid(t))) for t in ("vex-slot-chip", "vex-help-toggle", "vex-build-btn", "vex-more")}
    check("the slot chip, help toggle, Build and ⋯ are 4px rectangles (owner 2026-10-15: no pills)",
          all(r == [4.0] for r in pills.values()), str(pills))
    page.locator(_view(V, _tid("vex-build-btn"))).click()
    until(lambda: _latest_build(mock), timeout=4)
    _finish(mock, fake, _latest_build(mock), "failed", ERRORS)
    until(lambda: page.locator(_view(V, _tid("vex-error-0"))).count() > 0, timeout=4)
    _slot_open(page)
    _approval(mock, "vexcard2")
    until(lambda: page.locator(_tid("approval-card")).count() > 0, timeout=4)
    time.sleep(0.3)
    # Counted from once the card is up: a USB poll already on its way when it came is not behind it.
    before = len(mock.calls)
    check("a card closes the slot popover", page.locator(_view(V, _tid("vex-slot-pop"))).count() == 0)
    controls = ["vex-build-btn", "vex-rebuild-btn", "vex-clean-btn", "vex-download-btn", "vex-slot-chip", "vex-more",
                "vex-out-tab-build", "vex-out-tab-download", "vex-out-tab-brain", "vex-out-tab-device",
                "vex-out-fold", "vex-out-max", "vex-help-toggle", "vex-usb-open", "vex-error-0"]
    off = {t: page.locator(_view(V, _tid(t))).is_disabled() for t in controls}
    check("under a card every VEX control is disabled", all(off.values()), str(off))
    status = page.locator(_view(V, _tid("vex-status")))
    held = page.locator(_view(V, _tid("vex-held")))
    check("the status line says Held, in amber (amber's one meaning)",
          status.get_attribute("data-state") == "held" and held.count() == 1
          and held.evaluate("e => getComputedStyle(e).color") == _css(page, "--amber"))
    picker = page.locator(_tid("pane-2-vex-project"))
    check("and the pane's project picker too", picker.is_disabled())
    inert = page.evaluate("() => !!document.querySelector('[data-testid=\"vex-view-2\"]').closest('[inert]')")
    check("and inert behind the veil", inert)
    page.locator(_view(V, _tid("vex-build-btn"))).hover(force=True)
    time.sleep(0.2)
    check("no hover help under a card", page.locator(_view(V, _tid("vex-tip-build"))).is_hidden())
    for t in ("vex-build-btn", "vex-download-btn", "vex-slot-chip", "vex-more", "vex-error-0"):
        try:
            page.locator(_view(V, _tid(t))).click(force=True, timeout=500)
        except Exception:
            pass
    page.keyboard.press("Enter")
    time.sleep(0.4)
    vexy = [c for c in mock.calls[before:] if c[0] == "POST" and ("/vex" in c[1])]
    check("nothing builds, downloads or opens behind the card", not vexy, str(vexy))
    check("nor does a click open a file, the popover or the menu", (page.locator(_pane(3)).count() == 0
          or page.locator(_pane(3)).get_attribute("data-view") != "file")
          and page.locator(_view(V, _tid("vex-slot-pop"))).count() == 0
          and page.locator(_view(V, _tid("vex-more-menu"))).count() == 0)
    _shot(page, "08-held-by-card")
    page.keyboard.press("Escape")
    until(lambda: page.locator(_tid("approval-card")).count() == 0, timeout=4)
    check("answered, the controls come back",
          bool(until(lambda: page.locator(_view(V, _tid("vex-build-btn"))).is_enabled(), timeout=3)))
    # The ⋯ menu closes under a card too.
    page.locator(_view(V, _tid("vex-more"))).click()
    until(lambda: page.locator(_view(V, _tid("vex-more-menu"))).count() == 1, timeout=3)
    _approval(mock, "vexcard3")
    until(lambda: page.locator(_tid("approval-card")).count() > 0, timeout=4)
    check("a card closes the ⋯ menu", bool(until(lambda: page.locator(_view(V, _tid("vex-more-menu"))).count() == 0,
                                               timeout=3)))
    page.keyboard.press("Escape")
    until(lambda: page.locator(_tid("approval-card")).count() == 0, timeout=4)


def _brain(page, n: int = V, sel: str = "") -> str:
    """Inside pane `n`'s Brain tab."""
    return _view(n, _tid("vex-brain-tab") + (f" {sel}" if sel else ""))


def _brain_state(page, n: int = V) -> str | None:
    tab = page.locator(_brain(page, n))
    return tab.get_attribute("data-state") if tab.count() else None


def _socks(fake, tid: str) -> int:
    return len([s for s in fake.sockets if s["tid"] == tid])


def _connect(page, mock, until, n: int = V) -> str:
    """The owner's Connect in pane `n`'s Brain tab: the brain terminal's id, attached there."""
    _tab(page, "brain", n)
    page.locator(_brain(page, n, _tid("vex-brain-connect"))).click()
    assert until(lambda: mock_vex.state(mock)["brain"], timeout=4), "Connect opened nothing"
    brain = mock_vex.state(mock)["brain"]
    until(lambda: page.evaluate(f"window.__hudTerminals.state('{brain}')") == "attached", timeout=6)
    return brain


def _brain_checks(page, mock, check, until, fake):
    """VX-UI-4a: the brain terminal in the VEX pane's Brain tab. It connects only
    on the owner's trusted Connect (owner decision 5) — never when the tab is
    shown, on a reload or after a download — and is the same session (its
    "Jarvis can read" switch, input and Ctrl-C) wherever it is drawn."""
    _reset(page, mock, until)
    base = len(mock.calls)
    check("setup: the view draws", _ready(page, until))
    _tab(page, "brain")
    tab = page.locator(_brain(page))
    connect = page.locator(_brain(page, V, _tid("vex-brain-connect")))
    check("the Brain tab says it is not connected and never connects by itself",
          tab.count() == 1 and _brain_state(page) == "off"
          and "never connects by itself" in page.locator(_brain(page, V, _tid("vex-brain-text"))).inner_text())
    check("its Connect is offered, enabled, in the Tab order, with hover help on the port and the download",
          connect.count() == 1 and connect.is_enabled() and connect.get_attribute("tabindex") != "-1"
          and "holds the robot's port" in _described(page, _brain(page, V, _tid("vex-brain-connect")))
          and "never opens by itself" in _described(page, _brain(page, V, _tid("vex-brain-connect"))))
    time.sleep(0.6)
    check("showing the tab connects nothing (no POST /vex/brain)", not _posted_since(mock, base, "/vex/brain"))
    _tab(page, "build")
    page.locator(_view(V, _tid("vex-out-tab-build"))).focus()
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    time.sleep(0.4)
    check("nor does showing it again, or reaching it with the keyboard",
          not _posted_since(mock, base, "/vex/brain") and _brain_state(page) == "off")
    _shot(page, "13-brain-not-connected")
    connect.evaluate("e => e.click()")
    connect.dispatch_event("click")
    connect.evaluate("e => e.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}))")
    time.sleep(0.5)
    check("a script's click, an untrusted click event or a made-up Enter connects nothing",
          not _posted_since(mock, base, "/vex/brain") and _brain_state(page) == "off")

    # Under a card: Connect is off and a click does nothing.
    _approval(mock, "vexbrain1")
    until(lambda: page.locator(_tid("approval-card")).count() > 0, timeout=4)
    time.sleep(0.2)
    check("under a card Connect is disabled", connect.is_disabled())
    try:
        connect.click(force=True, timeout=500)
    except Exception:
        pass
    time.sleep(0.3)
    check("and a click behind the card connects nothing", not _posted_since(mock, base, "/vex/brain"))
    page.keyboard.press("Escape")
    until(lambda: page.locator(_tid("approval-card")).count() == 0, timeout=4)
    until(lambda: connect.is_enabled(), timeout=3)

    # The daemon refuses: said in its words, and Connect again.
    mock_vex.state(mock)["brain_refuse"] = "A device-info read is running. One device action at a time."
    connect.click()
    text = page.locator(_brain(page, V, _tid("vex-brain-text")))
    check("a refusal is said in the daemon's words, with Connect again (error state)",
          bool(until(lambda: _brain_state(page) == "error", timeout=4))
          and "One device action at a time" in text.inner_text() and connect.is_enabled())
    _shot(page, "14-brain-refused")

    # A trusted key press connects (the same rule as Stop).
    connect.focus()
    page.keyboard.press("Enter")
    check("a trusted Enter on Connect connects: POST /vex/brain with {}",
          bool(until(lambda: len(_posted_since(mock, base, "/vex/brain")) == 2, timeout=4))
          and _posted_since(mock, base, "/vex/brain")[-1] == {})
    brain = mock_vex.state(mock)["brain"]
    fake.ring[brain] = bytearray(b"battery 87%\r\n")
    term = page.locator(_brain(page, V, f'[data-testid="terminal-{brain}"]'))
    check("the brain terminal is drawn in the VEX pane (data-where vex-2), not the panel",
          bool(until(lambda: term.count() == 1 and term.get_attribute("data-where") == f"vex-{V}", timeout=6))
          and page.evaluate(f"window.__hudTerminals.shownIn('{brain}')") == f"vex-{V}"
          and not page.locator(_tid("panel-tabs")).is_visible())
    check("connected: it attaches and shows what the program prints",
          bool(until(lambda: _brain_state(page) == "connected", timeout=6))
          and bool(until(lambda: "battery 87%" in page.evaluate(f"window.__hudTerminals.text('{brain}')"), timeout=4)))
    check("the tab's badge says live", page.locator(_view(V, _tid("vex-out-tab-brain"))).get_attribute("data-state") == "ok"
          and "live" in (page.locator(_view(V, _tid("vex-out-tab-brain"))).get_attribute("aria-label") or ""))
    check("the same session's \u201cJarvis can read\u201d switch is in its bar",
          term.locator(_tid("term-readable")).count() == 1)
    socks = _socks(fake, brain)
    term.locator(".termslot").click()
    page.keyboard.type("go")
    page.keyboard.press("Control+c")
    sock = next(s for s in reversed(fake.sockets) if s["tid"] == brain)
    check("typing goes to the program, and Ctrl-C too",
          bool(until(lambda: b"g" in b"".join(sock["frames"]) and b"\x03" in sock["frames"], timeout=4)),
          str(sock["frames"][-4:]))
    _shot(page, "15-brain-connected")
    page.evaluate("document.activeElement && document.activeElement.blur()")

    # Another tab keeps it attached; the panel says where it is.
    _tab(page, "build")
    time.sleep(0.4)
    check("switching to another tab keeps the session attached (no new socket)",
          page.evaluate(f"window.__hudTerminals.state('{brain}')") == "attached" and _socks(fake, brain) == socks
          and page.evaluate(f"window.__hudTerminals.shownIn('{brain}')") is None)
    _tab(page, "brain")
    until(lambda: term.count() == 1, timeout=4)
    page.locator(_tid("toggle-panel")).click()
    ptab = page.locator(_tid(f"panel-tab-{brain}"))
    check("the panel's tab for it says it is shown in the VEX view",
          bool(until(lambda: ptab.count() == 1 and ptab.get_attribute("data-place") == f"vex-{V}", timeout=4))
          and "VEX view" in ptab.inner_text())
    pel = page.locator(_tid("panel-elsewhere"))
    check("and the panel draws it nowhere: \u201cshown in the VEX view\u201d with Go there",
          bool(until(lambda: pel.count() == 1, timeout=4)) and "VEX view" in pel.inner_text()
          and page.locator(f'[data-testid="terminal-{brain}"]').count() == 1
          and _socks(fake, brain) == socks)
    page.locator(_tid("toggle-panel")).click()

    # A reload re-shows the open link — it never asks for one.
    asked = len(_posted_since(mock, base, "/vex/brain"))
    page.reload()
    page.wait_for_selector('[data-testid="sidebar"]', state="attached")
    _ready(page, until)
    time.sleep(0.6)
    check("a reload connects nothing", len(_posted_since(mock, base, "/vex/brain")) == asked)
    _tab(page, "brain")
    check("its Brain tab shows the link the daemon holds, reattached with its ring replayed",
          bool(until(lambda: _brain_state(page) == "connected", timeout=6))
          and _socks(fake, brain) > socks
          and "battery 87%" in page.evaluate(f"window.__hudTerminals.text('{brain}')")
          and len(_posted_since(mock, base, "/vex/brain")) == asked)

    # Disconnect: a script's click does nothing; the owner's closes it and frees the port.
    disc = page.locator(_brain(page, V, _tid("vex-brain-disconnect")))
    check("Disconnect is offered, with its hover help", disc.count() == 1
          and "frees the robot's port" in _described(page, _brain(page, V, _tid("vex-brain-disconnect"))))
    disc.evaluate("e => e.click()")
    time.sleep(0.4)
    deletes = lambda: [c for c in mock.calls[base:] if c[0] == "DELETE" and c[1] == f"/terminals/{brain}"]  # noqa: E731
    check("a script's click on Disconnect closes nothing", not deletes())
    disc.click()
    check("Disconnect closes the brain terminal (DELETE /terminals/{id})", bool(until(lambda: deletes(), timeout=4)))
    check("and the tab is not connected again, with Connect",
          bool(until(lambda: _brain_state(page) == "off", timeout=4)) and connect.is_enabled()
          and page.locator(f'[data-testid="terminal-{brain}"]').count() == 0)
    check("Disconnect opened nothing new", len(_posted_since(mock, base, "/vex/brain")) == asked)

    # The program ends by itself: said, with its last output, and Connect again.
    brain = _connect(page, mock, until)
    fake.output(brain, b"vexcom: no V5 device found\r\n")
    fake.exit(brain, 3)
    check("a link whose program ended says so, with its exit code and its last output",
          bool(until(lambda: _brain_state(page) == "ended", timeout=4))
          and "exited with code 3" in page.locator(_brain(page, V, _tid("vex-brain-text"))).inner_text()
          and "no V5 device found" in page.evaluate(f"window.__hudTerminals.text('{brain}')"))
    check("and offers Connect again and Close", connect.is_enabled()
          and page.locator(_brain(page, V, _tid("vex-brain-disconnect"))).inner_text().strip() == "Close")
    _shot(page, "16-brain-ended")
    asked = len(_posted_since(mock, base, "/vex/brain"))
    connect.click()
    check("Connect again opens a new link", bool(until(lambda: len(_posted_since(mock, base, "/vex/brain")) == asked + 1,
                                                       timeout=4))
          and bool(until(lambda: mock_vex.state(mock)["brain"] not in (None, brain), timeout=4)))
    old = brain
    brain = mock_vex.state(mock)["brain"]
    check("LOW-2: the ended link is cleared away (closed, its panel tab gone)",
          bool(until(lambda: any(c[0] == "DELETE" and c[1] == f"/terminals/{old}" for c in mock.calls[base:])
                     and old not in page.evaluate("window.__hudTerminals.ids()")
                     and not any(r["id"] == old for r in mock.world["terminals"]), timeout=4)))
    until(lambda: _brain_state(page) == "connected", timeout=6)

    # A download pauses it — only once its build has succeeded — and never reopens it.
    base = len(mock.calls)
    v = mock_vex.state(mock)
    v["download_auto"] = False
    _auto(mock)
    page.locator(_view(V, _tid("vex-download-btn"))).click()
    check("a download while it is open closes it for the download",
          bool(until(lambda: _sent_downloads(mock), timeout=8))
          and any(c[0] == "DELETE" and c[1] == f"/terminals/{brain}" for c in mock.calls[base:]))
    calls = mock.calls[base:]
    closed_at = next(i for i, c in enumerate(calls) if c[0] == "DELETE" and c[1] == f"/terminals/{brain}")
    built_at = max(i for i, c in enumerate(calls) if c[0] == "POST" and c[1] == "/projects/p3/vex/build")
    check("only after the clean and the build: its output runs on while the project builds",
          closed_at > built_at, f"close at {closed_at}, last build POST at {built_at}")
    _tab(page, "brain")
    check("while the download runs the Brain tab says paused, and Connect is off with the reason",
          bool(until(lambda: _brain_state(page) == "paused", timeout=4))
          and "Paused for the download" in page.locator(_brain(page, V, _tid("vex-brain-text"))).inner_text()
          and connect.is_disabled() and "download holds the robot's port" in _described(page, _brain(page, V, _tid("vex-brain-connect")))
          and page.locator(_view(V, _tid("vex-out-tab-brain"))).get_attribute("data-state") == "unknown")
    _shot(page, "17-brain-paused")
    rec = next(iter(v["download_records"].values()))
    rec.update(state="succeeded", exit_code=0, output="Finished", ended="2026-10-13T12:00:02+00:00")
    until(lambda: page.evaluate("window.__hudVex.chain()") is None, timeout=6)
    time.sleep(2.0)
    check("after the download it is never opened again by itself (no POST /vex/brain)",
          not _posted_since(mock, base, "/vex/brain") and mock_vex.state(mock)["brain"] is None)
    closed = page.locator(_view(V, _tid("vex-brain-closed")))
    check("the tab offers Connect again and says why it is closed (also under the status line)",
          _brain_state(page) == "off" and connect.is_enabled()
          and "closed for the download" in page.locator(_brain(page, V, _tid("vex-brain-text"))).inner_text()
          and closed.count() == 1 and "Click Connect" in closed.inner_text())
    _shot(page, "18-brain-after-download")
    v["download_auto"] = True

    # Shown elsewhere: a second VEX pane, then a terminal pane.
    _reset(page, mock, until, {"preset": "cols3", "focused": 2,
                               "panes": [{"view": "chat"}, {"view": "vex", "projectId": "p3"},
                                         {"view": "vex", "projectId": "p5"}],
                               "panel": {"open": False, "height": 260}})
    ebase = len(mock.calls)
    check("elsewhere setup: both views draw", _ready(page, until)
          and bool(until(lambda: page.locator(_view(3, _tid("vex-card"))).count() > 0, timeout=5)))
    # MED-1, the H4 case: all the ring holds is interop's cursor query, vexcom hung behind it.
    nxt = f"{0xb0000000 + mock.__dict__.get('terminal_seq', 0) + 1:08x}"
    fake.ring[nxt] = bytearray(b"\x1b[6n")
    # LOW-1: both Brain tabs open; Connect is pressed in pane 3, the later pane — it shows it, not pane 2.
    _tab(page, "brain", 2)
    brain = _connect(page, mock, until, 3)
    check("LOW-1: the pane whose Connect was pressed shows it, though another Brain tab was open first",
          brain == nxt and page.evaluate(f"window.__hudTerminals.shownIn('{brain}')") == "vex-3"
          and _brain_state(page, 2) == "elsewhere")
    check("focus is not moved into the terminal by Connect", page.evaluate(
        "() => !(document.activeElement && document.activeElement.closest('.termhost, .xterm'))"))
    _tab(page, "build", 3)
    check("once pane 3 shows another tab, pane 2's open Brain tab draws the same session",
          bool(until(lambda: page.evaluate(f"window.__hudTerminals.shownIn('{brain}')") == "vex-2", timeout=4))
          and _socks(fake, brain) == 1)
    silent = page.locator(_brain(page, 2, _tid("vex-brain-silent")))
    check("MED-1: a ring of only ESC[6n is not output: it says nothing printed yet, naming H4",
          bool(until(lambda: _brain_state(page, 2) == "connected" and silent.count() == 1, timeout=4))
          and "H4" in silent.inner_text() and "Nothing printed yet" in silent.inner_text()
          and "may not have started" in silent.inner_text() and "Disconnect" not in silent.inner_text())
    time.sleep(0.5)
    check("and it stays said (no frame of control bytes counts)", silent.count() == 1)
    fake.output(brain, b"\x1b[?25l\x1b[2J")
    time.sleep(0.5)
    check("nor does a live frame of control sequences alone", silent.count() == 1)
    fake.output(brain, b"$BEGIN,odom,x\r\n")
    check("and stops saying it once the program prints",
          bool(until(lambda: silent.count() == 0, timeout=4)))
    # MED-2: another window takes it — said as taken, with the strip's Take it back, never a spinner.
    held = fake.holders.pop(brain)                       # as the daemon does when another window takes it
    fake._send(held, {"type": "taken"})
    fake._close(held, 1000, "taken by another window")
    fake.flush()
    check("MED-2: taken by another window is said so, with Take it back, never 'connecting'",
          bool(until(lambda: _brain_state(page, 2) == "taken", timeout=4))
          and "Taken by another window" in page.locator(_brain(page, 2, _tid("vex-brain-text"))).inner_text()
          and page.locator(_brain(page, 2, _tid("term-take-back"))).count() == 1
          and page.locator(_view(2, _tid("vex-out-tab-brain"))).get_attribute("data-state") != "busy")
    page.locator(_brain(page, 2, _tid("term-take-back"))).click()
    check("and Take it back attaches it here again, asking the daemon for no new link",
          bool(until(lambda: _brain_state(page, 2) == "connected", timeout=6))
          and len(_posted_since(mock, ebase, "/vex/brain")) == 1,
          f"{_brain_state(page, 2)} {page.evaluate(f'window.__hudTerminals.state(\'{brain}\')')}")
    _tab(page, "brain", 3)
    check("another VEX pane's Brain tab says the terminal is shown in pane 2, and draws it nowhere",
          bool(until(lambda: _brain_state(page, 3) == "elsewhere", timeout=4))
          and "VEX view in pane 2" in page.locator(_brain(page, 3, _tid("vex-brain-text"))).inner_text()
          and page.locator(_brain(page, 3, f'[data-testid="terminal-{brain}"]')).count() == 0
          and page.locator(_brain(page, 2, f'[data-testid="terminal-{brain}"]')).count() == 1
          and page.locator(_brain(page, 3, _tid("vex-brain-goto"))).count() == 1
          and page.locator(_brain(page, 3, _tid("vex-brain-connect"))).count() == 0)
    _shot(page, "19-brain-elsewhere", n=3)
    _tab(page, "build", 2)
    check("once pane 2 shows another tab, pane 3's Brain tab draws it",
          bool(until(lambda: page.locator(_brain(page, 3, f'[data-testid="terminal-{brain}"]')).count() == 1, timeout=4))
          and page.evaluate(f"window.__hudTerminals.shownIn('{brain}')") == "vex-3")
    _switch_view(page, 2, "terminal")
    page.locator(_tid(f"pane-2-pick-{brain}")).click()
    check("a terminal pane showing it wins: the Brain tab says \u201cshown in pane 2\u201d",
          bool(until(lambda: _brain_state(page, 3) == "elsewhere", timeout=4))
          and "shown in pane 2" in page.locator(_brain(page, 3, _tid("vex-brain-text"))).inner_text()
          and page.evaluate(f"window.__hudTerminals.shownIn('{brain}')") == "pane-2"
          and page.locator(f'[data-testid="terminal-{brain}"]').count() == 1)
    check("and nothing but the one Connect asked for a link",
          len(_posted_since(mock, ebase, "/vex/brain")) == 1 and mock_vex.state(mock)["brain"] == brain)


def _info_checks(page, mock, check, until, fake):
    _reset(page, mock, until)
    check("setup: the view draws", _ready(page, until))
    # What `vexcom --json` answers, pretty-printed by the daemon, with markup in a string.
    mock_vex.state(mock)["info"] = {
        "text": '{\n  "name": "<b>bold</b> <img src=x onerror=\\"window.__pwned=1\\">",\n'
                '  "vexos": "1.1.5",\n  "battery": 87\n}',
        "exit_code": 0}
    page.locator(_view(V, _tid("vex-more"))).click()
    menu = page.locator(_view(V, _tid("vex-more-menu")))
    check("⋯ is a menu of the device reads, its first item focused",
          menu.get_attribute("role") == "menu" and page.evaluate(
              "document.activeElement && document.activeElement.getAttribute('data-testid')") == "vex-info-btn")
    page.keyboard.press("ArrowDown")
    check("arrow keys move along it", page.evaluate(
        "document.activeElement && document.activeElement.getAttribute('data-testid')") == "vex-programs-btn")
    page.keyboard.press("Escape")
    check("Escape closes it and gives focus back to ⋯", menu.count() == 0 and page.evaluate(
        "document.activeElement && document.activeElement.getAttribute('data-testid')") == "vex-more")
    _more(page, "vex-info-btn")
    pre = page.locator(_view(V, _tid("vex-info-text")))
    until(lambda: pre.count() > 0, timeout=4)
    check("device info asks for the system read, and the Device tab shows it",
          mock_vex.state(mock)["info_reads"] == ["system"]
          and page.locator(_view(V, _tid("vex-out-panel"))).get_attribute("data-tab") == "device",
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
    _more(page, "vex-programs-btn")
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
    _more(page, "vex-info-btn")
    note = page.locator(_view(V, _tid("vex-info-note")))
    check("a refusal is said in the daemon's words, under the status line",
          bool(until(lambda: note.count() > 0, timeout=4))
          and "One device action at a time" in note.inner_text() and note.get_attribute("role") == "alert")


def _notice_checks(page, mock, check, until, fake):
    _reset(page, mock, until)
    check("setup: the view draws", _ready(page, until))
    mock.emit("vex_device", {"action": "download", "at": "2026-10-11T12:30:00+00:00"})
    n = page.locator(_view(V, _tid("vex-device-notice")))
    check("a download this window did not start is said", bool(until(lambda: n.count() > 0, timeout=4))
          and "did not start" in n.inner_text())
    check("above everything else in the view", n.evaluate(
        "e => e.compareDocumentPosition(document.querySelector('[data-testid=\"vex-view-2\"] [data-testid=\"vex-card\"]'))"
        " & Node.DOCUMENT_POSITION_FOLLOWING") != 0)


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
    """M1: one chain per window; every VEX view shows it; Stop stops the clean and sends nothing."""
    _reset(page, mock, until, {"preset": "cols3", "focused": 2,
                               "panes": [{"view": "chat"}, {"view": "vex", "projectId": "p3"},
                                         {"view": "vex", "projectId": "p5"}],
                               "panel": {"open": False, "height": 260}})
    base = len(mock.calls)
    check("M1 setup: both views draw", _ready(page, until)
          and bool(until(lambda: page.locator(_view(3, _tid("vex-card"))).count() > 0, timeout=5)))
    v = mock_vex.state(mock)
    page.locator(_view(2, _tid("vex-download-btn"))).click()
    until(lambda: _posted_since(mock, base, "/projects/p3/vex/build"), timeout=4)
    bar2, bar3 = page.locator(_chain_view(2)), page.locator(_chain_view(3))
    want = "Cleaning override, then building and downloading to slot 1"
    check("M1: the running chain is said in its own view",
          bool(until(lambda: bar2.count() > 0 and want in bar2.inner_text(), timeout=4)),
          bar2.inner_text() if bar2.count() else "")
    check("M1: and in every other VEX view, whatever project it shows",
          bar3.count() > 0 and want in bar3.inner_text(), bar3.inner_text() if bar3.count() else "")
    other = page.locator(_view(3, _tid("vex-download-btn")))
    check("M1: another project's Download is off while it runs", other.is_disabled())
    check("MED-2: and its Clean, Build and Rebuild too",
          all(page.locator(_view(3, _tid(f"vex-{k}-btn"))).is_disabled() for k in ("clean", "build", "rebuild")))
    check("MED-2: another project's view offers no Stop for this chain",
          page.locator(_view(3, _tid("vex-stop"))).count() == 0
          and page.locator(_view(2, _tid("vex-stop"))).count() == 1)
    page.locator(_view(3, _tid("vex-more"))).click()
    check("M1: so is device info",
          page.locator(_view(3, _tid("vex-info-btn"))).is_disabled()
          and page.locator(_view(3, _tid("vex-programs-btn"))).is_disabled())
    page.keyboard.press("Escape")
    check("Escape closes ⋯ even with every item off and focus on ⋯",
          bool(until(lambda: page.locator(_view(3, _tid("vex-more-menu"))).count() == 0, timeout=2)))
    _tab(page, "brain", 3)
    check("M1: and the brain terminal's Connect", page.locator(_view(3, _tid("vex-brain-connect"))).is_disabled())
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
    check("MED-2: and it, showing p5 now, has no Stop for p3's chain", page.locator(_view(2, _tid("vex-stop"))).count() == 0)
    for n in (2, 3):
        try:
            page.locator(_view(n, _tid("vex-build-btn"))).click(force=True, timeout=500)
        except Exception:
            pass
    time.sleep(0.3)
    check("MED-2: a forced click on p5's Build builds nothing",
          not _posted_since(mock, base, "/projects/p5/vex/build")
          and not [c for c in mock.calls[base:] if c[0] == "DELETE"])
    # Back to p3 (through File, as above): Stop from the chain's own project.
    _switch_view(page, 2, "file")
    page.locator(_tid("pane-2-folder")).select_option("p3")
    _switch_view(page, 2, "vex")
    until(lambda: page.locator(_view(2)).get_attribute("data-project") == "p3", timeout=4)
    bid = _latest_build(mock)
    btid = v["builds"][bid]["terminal_id"]
    page.locator(_view(2, _tid("vex-stop"))).click()
    check("M1: Stop stops the clean's program (its terminal closed)",
          bool(until(lambda: any(c[0] == "DELETE" and c[1] == f"/terminals/{btid}" for c in mock.calls[base:]), timeout=4)))
    check("M1: the chain ends", bool(until(lambda: bar2.count() == 0 and bar3.count() == 0, timeout=6)))
    time.sleep(0.5)
    check("M1: and nothing was built after it or downloaded",
          not _sent_downloads(mock) and not v["downloads"]
          and _posted_since(mock, base, "/projects/p3/vex/build") == [{"action": "clean"}], str(v["downloads"]))
    check("M1: the pickers come back", page.locator(_tid("pane-2-vex-project")).is_enabled()
          and page.locator(_tid("pane-3-vex-project")).is_enabled())
    check("LOW-2: the stopped clean is said as stopped, not failed",
          page.locator(_view(2, _tid("vex-mark-clean"))).get_attribute("aria-label") == "Clean: stopped")
    # p3's view says it was stopped.
    note = page.locator(_view(2, _tid("vex-note")))
    check("M1: the chain's project says it was stopped and nothing was sent",
          bool(until(lambda: note.count() > 0 and "Stopped: nothing was downloaded" in note.inner_text(), timeout=4)),
          note.inner_text() if note.count() else "")


def _header_rebuild_checks(page, mock, check, until, fake):
    """Delta review MED-1: make found nothing to do after a header it does not list
    changed, so the daemon still says Build first after the chain's build — one
    rebuild, then the download."""
    _reset(page, mock, until)
    base = len(mock.calls)
    check("MED-1 setup: the view draws", _ready(page, until))
    v = mock_vex.state(mock)
    v["bin_fresh"] = False
    v["stale_until_rebuild"] = True
    _auto(mock)
    page.locator(_view(V, _tid("vex-download-btn"))).click()
    check("MED-1: a build that leaves it stale is followed by one rebuild, on the one click",
          bool(until(lambda: _posted_since(mock, base, "/projects/p3/vex/build") == [
              {"action": "clean"}, {"action": "build"}, {"action": "rebuild"}], timeout=10)),
          str(_posted_since(mock, base, "/projects/p3/vex/build")))
    check("MED-1: then the download goes", bool(until(lambda: _sent_downloads(mock), timeout=8))
          and len(v["downloads"]) == 2, str(v["downloads"]))
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
    _auto(mock)
    page.locator(_view(V, _tid("vex-download-btn"))).click()
    note = page.locator(_view(V, _tid("vex-note")))
    check("M2: still stale after a build and a rebuild, it says so and sends nothing",
          bool(until(lambda: note.count() > 0 and "after a build and a rebuild" in note.inner_text(), timeout=12)),
          note.inner_text() if note.count() else "")
    time.sleep(1.0)
    check("M2: one clean, one build, one rebuild and two tries, never a loop",
          _posted_since(mock, base, "/projects/p3/vex/build") == [{"action": "clean"}, {"action": "build"},
                                                                   {"action": "rebuild"}]
          and len(v["downloads"]) == 2 and not _sent_downloads(mock),
          f"{_posted_since(mock, base, '/projects/p3/vex/build')}, {v['downloads']}")


def _answer_card_checks(page, mock, check, until, fake):
    """L1: a card that comes up while the daemon answers "Build first" builds nothing more."""
    _reset(page, mock, until)
    base = len(mock.calls)
    check("L1 setup: the view draws", _ready(page, until))
    v = mock_vex.state(mock)
    v["bin_fresh"] = False
    v["stale_until_rebuild"] = True
    v["download_delay"] = 1.2
    _auto(mock)
    page.locator(_view(V, _tid("vex-download-btn"))).click()
    until(lambda: v["downloads"], timeout=8)
    _approval(mock, "vexcardL1")
    until(lambda: page.locator(_tid("approval-card")).count() > 0, timeout=4)
    note = page.locator(_view(V, _tid("vex-note")))
    until(lambda: note.count() > 0 and "approval card" in note.inner_text(), timeout=6)
    time.sleep(0.4)
    check("L1: a card up when the daemon answered Build first: nothing more builds behind it",
          _posted_since(mock, base, "/projects/p3/vex/build") == [{"action": "clean"}, {"action": "build"}],
          str(_posted_since(mock, base, "/projects/p3/vex/build")))
    check("L1: and the view says so", note.count() > 0 and "nothing was downloaded" in note.inner_text())
    page.keyboard.press("Escape")
    until(lambda: page.locator(_tid("approval-card")).count() == 0, timeout=4)
    time.sleep(0.4)
    check("L1: answering the card builds and sends nothing either",
          len(_posted_since(mock, base, "/projects/p3/vex/build")) == 2 and not _sent_downloads(mock))


def _passing_card_checks(page, mock, check, until, fake):
    """L2: a card that comes and goes while it cleans drops the download; the brain terminal is untouched."""
    _reset(page, mock, until)
    base = len(mock.calls)
    check("L2 setup: the view draws", _ready(page, until))
    brain = _connect(page, mock, until)
    v = mock_vex.state(mock)
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
    check("L2: a card that came and went while it cleaned drops the download (no build, nothing sent)",
          not _sent_downloads(mock) and not v["downloads"]
          and _posted_since(mock, base, "/projects/p3/vex/build") == [{"action": "clean"}], str(v["downloads"]))
    check("L2: and the view says so", note.count() > 0 and "nothing was downloaded" in note.inner_text(),
          note.inner_text() if note.count() else "")
    check("L2: a chain dropped before its download never touched the brain terminal",
          not any(c[0] == "DELETE" and c[1] == f"/terminals/{brain}" for c in mock.calls[base:])
          and mock_vex.state(mock)["brain"] == brain and len(_posted_since(mock, base, "/vex/brain")) == 1)


def _first_click_checks(page, mock, check, until, fake):
    """L3: only a click's first click counts: a double or triple click is one download or none."""
    _reset(page, mock, until)
    check("L3 setup: the view draws", _ready(page, until))
    v = mock_vex.state(mock)
    _auto(mock)
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
    until(lambda: _sent_downloads(mock), timeout=8)
    until(idle, timeout=8)
    time.sleep(0.4)
    check("L3: a double click is one download", len(v["downloads"]) == 1, str(v["downloads"]))
    btn.click(click_count=3)
    until(lambda: len(v["downloads"]) >= 2, timeout=8)
    until(idle, timeout=8)
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
    _slot_open(page)
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
    _slot_open(page)
    page.locator(_view(V, _tid("vex-name-input"))).focus()
    page.keyboard.press("Control+Shift+P")
    pal = page.locator(_tid("palette"))
    until(lambda: pal.count() == 1, timeout=3)
    labels = page.locator(_tid("palette-row")).evaluate_all("els => els.map(e => e.innerText)")
    check("ED-7: the palette offers no download or VEX device command",
          bool(labels) and not [x for x in labels if re.search(r"download|vexcom|brain|device|robot|stop", x, re.I)],
          str([x for x in labels if re.search(r"download|vex|stop", x, re.I)]))
    _slot_open(page)
    page.locator(_view(V, _tid("vex-name-input"))).focus()
    check("ED-7: the palette closes when focus goes back to the view", bool(until(lambda: pal.count() == 0, timeout=2)))
    _slot_close(page)
    check("ED-7: and the download button is reachable", page.locator(_view(V, _tid("vex-download-btn"))).is_visible())


def _feedback_checks(page, mock, check, until, fake):
    _reset(page, mock, until)
    v = mock_vex.state(mock)
    v["download_auto"] = False
    _auto(mock)
    page.get_by_test_id("vex-download-btn").click()
    assert until(lambda: page.get_by_test_id("vex-download-output").count() == 1, timeout=8)
    check("the Download tab is selected once the chain reaches its download",
          page.get_by_test_id("vex-out-panel").get_attribute("data-tab") == "download"
          and page.get_by_test_id("vex-out-tab-download").get_attribute("aria-selected") == "true")
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
    chain = page.get_by_test_id("vex-chain")
    check("the status line says the step and the percentage",
          chain.get_attribute("data-phase") == "download" and "72%" in chain.inner_text()
          and page.get_by_test_id("vex-chain-steps").locator("li").evaluate_all(
              "els => els.map(e => e.dataset.state)") == ["done", "done", "busy"])
    check("LOW-4: the ticking elapsed time and percent stay out of the live region",
          page.get_by_test_id("vex-status-elapsed").get_attribute("aria-hidden") == "true"
          and chain.locator(".vexel[aria-hidden=true]").count() == 2)
    _shot(page, "04-downloading")
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
    page.get_by_test_id("vex-usb-open").click()
    check("the header's Brain/Controller dots open the Device tab",
          page.get_by_test_id("vex-out-panel").get_attribute("data-tab") == "device")
    assert until(lambda: page.get_by_test_id("vex-usb-brain").inner_text().startswith("Brain: Connected"))
    check("Brain/Controller and tooling have distinct indicators", page.get_by_test_id("vex-usb-brain").inner_text() == "Brain: Connected"
          and page.get_by_test_id("vex-usb-controller").inner_text() == "Controller: Disconnected"
          and "Tooling: Configured" in page.get_by_test_id("vex-tooling").inner_text())
    color = lambda tid: page.get_by_test_id(tid).evaluate("e => getComputedStyle(e).color")  # noqa: E731
    css = lambda name: _css(page, name)  # noqa: E731
    check("connected is green and disconnected red", color("vex-usb-brain") == css("--green")
          and color("vex-usb-controller") == css("--red"), f"{color('vex-usb-brain')} {color('vex-usb-controller')}")
    badges = page.get_by_test_id("vex-usb-badges")
    check("the header shows both, small, without opening a tab",
          page.get_by_test_id("vex-card").locator(_tid("vex-usb-badges")).count() == 1 and badges.is_visible()
          and page.get_by_test_id("vex-badge-brain").inner_text() == "Brain"
          and page.get_by_test_id("vex-badge-controller").inner_text() == "Controller"
          and page.get_by_test_id("vex-badge-brain").get_attribute("aria-label") == "Brain: Connected"
          and page.get_by_test_id("vex-badge-controller").get_attribute("aria-label") == "Controller: Disconnected")
    check("the header's dots match the Device tab's colours", color("vex-badge-brain") == css("--green")
          and color("vex-badge-controller") == css("--red"))
    check("the connection log is visible in the Device tab (no <details> to open), with timestamps",
          page.get_by_test_id("vex-connection-log").is_visible()
          and page.get_by_test_id("vex-connection-log").locator("time").count() == 2
          and page.get_by_test_id("vex-connection").locator("summary").count() == 0)
    v["connection"].update(state="error", message="Unsupported USB response; retained.")
    assert until(lambda: "last confirmed" in page.get_by_test_id("vex-usb-brain").inner_text())
    check("the header says a stale reading is the last confirmed one too",
          page.get_by_test_id("vex-badge-brain").get_attribute("aria-label") == "Brain: Connected (last confirmed)"
          and "stale" in (page.get_by_test_id("vex-badge-brain").get_attribute("class") or ""))
    check("USB errors preserve last confirmed hardware state", "Connected (last confirmed)" in page.get_by_test_id("vex-usb-brain").inner_text()
          and "Unsupported" in page.get_by_test_id("vex-connection-note").inner_text())
    v["connection"].update(state="ready", brain=False, checked_at=at,
        events=[*v["connection"]["events"], {"device": "brain", "connected": False, "at": at}])
    assert until(lambda: page.get_by_test_id("vex-usb-brain").inner_text() == "Brain: Disconnected")
    check("and the header follows a confirmed disconnect", color("vex-badge-brain") == css("--red")
          and page.get_by_test_id("vex-badge-brain").get_attribute("aria-label") == "Brain: Disconnected")
    check("confirmed disconnect has a timestamped log entry", page.get_by_test_id("vex-connection-log").locator("time").count() == 3)
    hint = page.get_by_test_id("vex-usb-hint")
    check("two confirmed disconnects add a muted hint to the status line, and disable nothing",
          bool(until(lambda: hint.count() == 1, timeout=3)) and "No brain or controller on USB" in hint.inner_text()
          and page.get_by_test_id("vex-download-btn").is_enabled())
    _shot(page, "07-device-tab")
    page.set_viewport_size({"width": 375, "height": 812})
    try:
        ids = ("vex-actionbar", "vex-status", "vex-out-panel", "vex-connection")
        def geometry():
            return page.evaluate("""(ids) => {
              const shape=id=>{const e=document.querySelector('[data-testid="'+id+'"]');
                return e?{rect:e.getBoundingClientRect().toJSON(),scrollWidth:e.scrollWidth,clientWidth:e.clientWidth}:null};
              return {width:innerWidth, ...Object.fromEntries(ids.map(i => [i, shape(i)]))};
            }""", list(ids))
        els = [page.get_by_test_id(i) for i in ids]
        assert until(lambda: _ws(page)["fit"]["panes"] == [V] and all(e.is_visible() for e in els)
                     and page.get_by_test_id("vex-actionbar").evaluate("e=>e.getBoundingClientRect().width<=innerWidth")), \
            "narrow VEX pane did not settle: " + json.dumps(geometry())
        fits = all(el.evaluate("e=>e.scrollWidth<=e.clientWidth+1 && e.getBoundingClientRect().left>=0 && e.getBoundingClientRect().right<=innerWidth+1")
                   for el in els)
        check("the action bar, status line, output and USB status fit a 375px VEX pane", fits,
              "" if fits else json.dumps(geometry()))
        view_fits = page.locator(_view(V)).evaluate("e => e.scrollWidth <= e.clientWidth + 1")
        check("and the view has no sideways scroll", view_fits)
        _shot(page, "09-narrow-375")
        import os
        capture = os.environ.get("VEX_FEEDBACK_SCREENSHOT")
        if capture:
            page.screenshot(path=capture, full_page=True)
    finally:
        page.set_viewport_size({"width": VIEW[0], "height": VIEW[1]})


def _stop_checks(page, mock, check, until, fake):
    """Stop (owner decision 6): a running download is stopped on a trusted press
    only, and the view is honest about what the slot holds; a running build
    too. Never `vexcom --stop`: nothing but closing the action's own terminal."""
    _reset(page, mock, until)
    check("Stop setup: the view draws", _ready(page, until))
    check("nothing runs: no Stop", page.locator(_view(V, _tid("vex-stop"))).count() == 0)
    brain = _connect(page, mock, until)
    base = len(mock.calls)
    v = mock_vex.state(mock)
    v["download_auto"] = False
    v["download_live"] = True
    _auto(mock)
    page.locator(_view(V, _tid("vex-download-btn"))).click()
    chain = page.locator(_view(V, _tid("vex-chain")))
    assert until(lambda: _sent_downloads(mock) and chain.count() and chain.get_attribute("data-phase") == "download",
                 timeout=10), "the download did not start"
    dtid = _sent_downloads(mock)[-1]["terminal_id"]
    rec = next(iter(v["download_records"].values()))
    rec.update(output="Device: COM3\nvexcom utility V1.0.2b2\ndownload :\n0....10....20....30....40")
    stop = page.locator(_view(V, _tid("vex-stop")))
    check("while the download is being sent, Stop is offered (owner decision 6), in the Tab order",
          stop.count() == 1 and stop.is_enabled() and stop.get_attribute("tabindex") != "-1")
    check("its help says it may leave the slot incomplete and never stops the program on the brain",
          "incomplete" in _described(page, _view(V, _tid("vex-stop")))
          and "never stops the program on the brain" in _described(page, _view(V, _tid("vex-stop"))))
    stop.evaluate("e => e.click()")
    stop.dispatch_event("click")
    time.sleep(0.5)
    closes = lambda: [c for c in mock.calls[base:] if c[0] == "DELETE" and c[1] == f"/terminals/{dtid}"]  # noqa: E731
    check("a script's click or an untrusted event stops nothing",
          not closes() and page.evaluate("window.__hudVex.chain()")["cancelled"] is False and rec["state"] == "running")
    stop.focus()
    page.keyboard.press("Enter")
    check("a trusted key press on it stops the download: vexcom's terminal is closed",
          bool(until(lambda: closes(), timeout=4)))
    note = page.locator(_view(V, _tid("vex-note")))
    check("the view says the download was stopped and the slot may be incomplete — never success",
          bool(until(lambda: note.count() and "Download stopped: slot 1 may hold an incomplete program" in note.inner_text(),
                     timeout=6))
          and "Download again before running it" in note.inner_text()
          and page.locator(_view(V, _tid("vex-status"))).get_attribute("data-state") == "failed",
          note.inner_text() if note.count() else "")
    done = page.locator(_view(V, _tid("vex-downloaded")))
    mark = page.locator(_view(V, _tid("vex-mark-download")))
    check("the Download tab and mark say stopped, not in the slot",
          done.count() == 1 and "Stopped" in done.inner_text() and "In slot" not in done.inner_text()
          and mark.get_attribute("data-state") == "failed" and "stopped" in (mark.get_attribute("aria-label") or ""))
    _shot(page, "06-download-stopped")
    time.sleep(1.0)
    sent = [c for c in mock.calls[base:] if c[0] in ("POST", "DELETE", "PUT", "PATCH")]
    check("Stop asked for nothing but closing its own terminal: no stop route, no vexcom --stop",
          not [c for c in mock.calls[base:] if "stop" in c[1].lower() or "stop" in json.dumps(c[2]).lower()]
          and [c[1] for c in sent if c[0] == "DELETE"] == [f"/terminals/{brain}", f"/terminals/{dtid}"],
          str([(c[0], c[1]) for c in sent]))
    check("nothing is retried", len(v["downloads"]) == 1, str(v["downloads"]))
    bnote = page.locator(_view(V, _tid("vex-brain-closed")))
    check("L6: a stopped chain never opens the brain terminal again, and says so",
          len(_posted_since(mock, base, "/vex/brain")) == 0
          and bnote.count() == 1 and "not opened again, since the download was stopped" in bnote.inner_text(),
          bnote.inner_text() if bnote.count() else "")

    # A project's own build, stopped with the pointer.
    _reset(page, mock, until)
    _ready(page, until)
    base = len(mock.calls)
    page.locator(_view(V, _tid("vex-build-btn"))).click()
    until(lambda: _posted_since(mock, base, "/projects/p3/vex/build"), timeout=4)
    btid = mock_vex.state(mock)["builds"][_latest_build(mock)]["terminal_id"]
    stop = page.locator(_view(V, _tid("vex-stop")))
    until(lambda: stop.count() == 1, timeout=4)
    stop.click()
    check("Stop on a running build closes its program",
          bool(until(lambda: any(c[0] == "DELETE" and c[1] == f"/terminals/{btid}" for c in mock.calls[base:]),
                     timeout=4)))
    state = page.locator(_view(V, _tid("vex-build-state")))
    check("and says it was stopped, not that it failed",
          bool(until(lambda: state.count() and state.inner_text().startswith("✗ Build stopped"), timeout=6))
          and page.locator(_view(V, _tid("vex-mark-build"))).get_attribute("aria-label") == "Build: stopped",
          state.inner_text() if state.count() else "")
    check("and Stop goes away", bool(until(lambda: stop.count() == 0, timeout=3)))

    # Under a card, Stop is off.
    page.locator(_view(V, _tid("vex-build-btn"))).click()
    until(lambda: stop.count() == 1, timeout=4)
    base = len(mock.calls)
    _approval(mock, "vexcardStop")
    until(lambda: page.locator(_tid("approval-card")).count() > 0, timeout=4)
    check("under a card Stop is disabled", stop.is_disabled())
    try:
        stop.click(force=True, timeout=500)
    except Exception:
        pass
    time.sleep(0.3)
    check("and a forced click stops nothing", not [c for c in mock.calls[base:] if c[0] == "DELETE"])
    page.keyboard.press("Escape")
    until(lambda: page.locator(_tid("approval-card")).count() == 0, timeout=4)
    _finish(mock, fake, _latest_build(mock), "succeeded")


def _tabs_checks(page, mock, check, until, fake):
    """The output area (plan §2.5): an action selects its tab, background events never do."""
    _reset(page, mock, until)
    check("tabs setup: the view draws", _ready(page, until))
    panel = page.locator(_view(V, _tid("vex-out-panel")))
    tablist = page.locator(_view(V, "[role=tablist]"))
    check("a real tablist: Build, Download, Brain, Device; Build first",
          tablist.locator("[role=tab]").evaluate_all("els => els.map(e => e.dataset.testid)")
          == ["vex-out-tab-build", "vex-out-tab-download", "vex-out-tab-brain", "vex-out-tab-device"]
          and panel.get_attribute("data-tab") == "build"
          and panel.get_attribute("role") == "tabpanel"
          and panel.get_attribute("aria-labelledby") == page.locator(_view(V, _tid("vex-out-tab-build"))).get_attribute("id"))
    _tab(page, "device")
    check("a click on a tab shows it", panel.get_attribute("data-tab") == "device"
          and page.locator(_view(V, _tid("vex-out-tab-device"))).get_attribute("aria-selected") == "true")
    page.locator(_view(V, _tid("vex-build-btn"))).click()
    check("an action the owner starts selects its tab (Build → Build)",
          bool(until(lambda: panel.get_attribute("data-tab") == "build", timeout=3)))
    _tab(page, "device")
    until(lambda: _latest_build(mock), timeout=4)
    _finish(mock, fake, _latest_build(mock), "failed", ERRORS)
    mock.emit("vex_device", {"action": "info", "at": "2026-10-11T12:31:00+00:00"})
    time.sleep(0.8)
    check("background events (a build ending, a device notice) never switch tabs",
          panel.get_attribute("data-tab") == "device")
    check("the Build tab's badge follows its state",
          page.locator(_view(V, _tid("vex-out-tab-build"))).get_attribute("data-state") == "failed"
          and page.locator(_view(V, _tid("vex-out-badge-build"))).inner_text() == "✗")
    # Keyboard: arrows, Home, End; roving tabindex.
    page.locator(_view(V, _tid("vex-out-tab-device"))).focus()
    page.keyboard.press("ArrowRight")
    focused = lambda: page.evaluate("document.activeElement && document.activeElement.getAttribute('data-testid')")  # noqa: E731
    check("ArrowRight from the last tab wraps to the first, focused and shown",
          focused() == "vex-out-tab-build" and panel.get_attribute("data-tab") == "build")
    page.keyboard.press("ArrowRight")
    check("ArrowRight moves on", focused() == "vex-out-tab-download" and panel.get_attribute("data-tab") == "download")
    page.keyboard.press("End")
    check("End is the last", focused() == "vex-out-tab-device")
    page.keyboard.press("Home")
    check("Home is the first", focused() == "vex-out-tab-build")
    check("only the selected tab is in the Tab order",
          tablist.locator("[role=tab]").evaluate_all("els => els.map(e => e.tabIndex)") == [0, -1, -1, -1])
    # Fold and fill.
    fold = page.locator(_view(V, _tid("vex-out-fold")))
    fold.click()
    check("▾ folds the output away; the tabs, their badges and the status line stay",
          panel.count() == 0 and fold.get_attribute("aria-expanded") == "false"
          and page.locator(_view(V, _tid("vex-out-tab-build"))).is_visible()
          and page.locator(_view(V, _tid("vex-status"))).is_visible())
    _shot(page, "10-output-folded")
    fold.click()
    check("and back", panel.count() == 1)
    out_h = page.locator(_view(V, _tid("vex-output"))).bounding_box()["height"]
    mx = page.locator(_view(V, _tid("vex-out-max")))
    mx.click()
    check("⤢ gives the output the pane: the header and action bar step aside, the status line stays",
          mx.get_attribute("aria-pressed") == "true"
          and page.locator(_view(V, _tid("vex-actionbar"))).is_hidden()
          and page.locator(_view(V, _tid("vex-status"))).is_visible()
          and page.locator(_view(V, _tid("vex-output"))).bounding_box()["height"] > out_h)
    mx.click()
    check("and ⤢ again restores", page.locator(_view(V, _tid("vex-actionbar"))).is_visible())
    # The pane keeps its tab when it shows another view and comes back.
    _tab(page, "download")
    _switch_view(page, V, "file")
    _switch_view(page, V, "vex")
    until(lambda: page.locator(_view(V, _tid("vex-out-panel"))).count() == 1, timeout=4)
    check("a pane switched away and back keeps its tab",
          page.locator(_view(V, _tid("vex-out-panel"))).get_attribute("data-tab") == "download")


def _help_checks(page, mock, check, until, fake):
    """Hover help is permanent (owner decision 7); `?` lists every control."""
    _reset(page, mock, until)
    check("help setup: the view draws", _ready(page, until))
    want = {"vex-clean-btn": "make clean", "vex-build-btn": "Compiles the project", "vex-rebuild-btn": "make -B",
            "vex-download-btn": "replaces whatever is in slot 1", "vex-slot-chip": "Which slot",
            "vex-more": "vexcom --programs", "vex-usb-open": "USB presence only", "vex-help-toggle": "Lists what each",
            "vex-out-fold": "Folds the output", "vex-out-max": "whole pane",
            "vex-out-tab-brain": "connects only when you click"}
    shown = {}
    for tid, text in want.items():
        el = page.locator(_view(V, _tid(tid)))
        tip_id = el.get_attribute("aria-describedby")
        tip = page.locator(f"[id='{tip_id}']")
        el.hover()
        shown[tid] = bool(until(lambda: tip.is_visible(), timeout=2)) and text in tip.inner_text()
        page.mouse.move(2, 2)
    check("every control shows its hover help, named by aria-describedby", all(shown.values()), str(shown))
    btn = page.locator(_view(V, _tid("vex-build-btn")))
    tip = page.locator(f"[id='{btn.get_attribute('aria-describedby')}']")
    seen = []
    for _ in range(6):
        btn.hover()
        seen.append(bool(until(lambda: tip.is_visible(), timeout=2)))
        page.mouse.move(2, 2)
        until(lambda: tip.is_hidden(), timeout=2)
    check("it never retires: the sixth hover shows it as the first did", all(seen), str(seen))
    btn.hover()
    until(lambda: tip.is_visible(), timeout=2)
    _shot(page, "11-hover-help", hover=True)
    page.mouse.move(2, 2)
    ctip = page.locator(f"[id='{page.locator(_view(V, _tid('vex-clean-btn'))).get_attribute('aria-describedby')}']")
    page.locator(_view(V, _tid("vex-help-toggle"))).focus()
    page.keyboard.press("Tab")
    check("keyboard focus shows it too (Tab from ? reaches Clean)", page.evaluate(
        "document.activeElement && document.activeElement.getAttribute('data-testid')") == "vex-clean-btn"
          and bool(until(lambda: ctip.is_visible(), timeout=2)))
    page.locator("body").focus()
    # A click hides it until the pointer leaves: the status line it changed stays readable.
    page.locator(_view(V, _tid("vex-rebuild-btn"))).hover()
    rtip = page.locator(f"[id='{page.locator(_view(V, _tid('vex-rebuild-btn'))).get_attribute('aria-describedby')}']")
    until(lambda: rtip.is_visible(), timeout=2)
    page.mouse.down()
    check("a press hides it while the pointer stays", bool(until(lambda: rtip.is_hidden(), timeout=2)))
    page.mouse.move(2, 2)
    page.mouse.up()
    # A disabled control keeps its help (the reason it is off).
    page.locator(_view(V, _tid("vex-build-btn"))).click()
    until(lambda: btn.is_disabled(), timeout=3)
    page.locator(_view(V, _tid("vex-clean-btn"))).hover(force=True)
    check("a disabled control still shows its help on hover", bool(until(lambda: ctip.is_visible(), timeout=2)))
    page.mouse.move(2, 2)
    _finish(mock, fake, _latest_build(mock), "succeeded")
    toggle = page.locator(_view(V, _tid("vex-help-toggle")))
    toggle.click()
    lst = page.locator(_view(V, _tid("vex-help-list")))
    check("? lists what each control does, in the output area",
          toggle.get_attribute("aria-pressed") == "true" and lst.count() == 1
          and lst.locator("li").count() >= 12 and "Mouse click only" in lst.inner_text()
          and "never stops the program on the brain" in lst.inner_text())
    _shot(page, "01-help-mode")
    page.keyboard.press("Escape")
    check("Escape leaves help mode", bool(until(lambda: lst.count() == 0, timeout=2))
          and toggle.get_attribute("aria-pressed") == "false")
    _slot_open(page)
    _shot(page, "12-slot-popover")
    _slot_close(page)


SECTIONS = (
    _feedback_checks, _offer_checks, _setup_checks, _build_checks, _clean_output_checks, _explicit_click_checks,
    _chain_checks, _card_checks, _brain_checks, _info_checks, _notice_checks, _one_chain_checks,
    _header_rebuild_checks, _still_stale_checks, _answer_card_checks, _passing_card_checks, _first_click_checks,
    _detect_again_checks, _lone_pane_checks, _gone_project_checks, _ed7_beside_checks, _stop_checks, _tabs_checks,
    _help_checks)


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
        print("hud/dist is not built. Run: cd hud && npm ci --ignore-scripts && npm run build")
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
