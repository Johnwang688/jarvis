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
  - device info is drawn as text: markup in it is characters;
  - a device action this window did not start is said.

Run (after `cd hud && npm ci && npm run build`):
    PYTHONPATH=. .venv/bin/python tests/face/hud_v2_vex_check.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.face import hud_v2_mock_vex as mock_vex  # noqa: E402
from tests.face.hud_v2_layout_check import _approval, _boot, _pane, _set_storage, _set_ws  # noqa: E402
from tests.face.hud_v2_terminal_check import FakePty, pumping  # noqa: E402

VIEW = (1800, 900)
V = 2  # the VEX view's pane: beside a chat (a window with no chat drawn opens one)
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
    """A VEX project (p3), one whose settings file is malformed (p4), and src/main.cpp to open."""
    w = mock.world
    if not any(p["id"] == "p3" for p in w["projects"]):
        w["projects"].append(_project("p3", "override", P3_ROOT))
        w["projects"].append(_project("p4", "broken", "/mnt/c/Users/johnw/OneDrive/Desktop/broken"))
    w["files"]["src/main.cpp"] = {"content": MAIN_CPP, "mtime": 1757900300}
    src = w["tree"].setdefault("src", [])
    if not any(e["name"] == "main.cpp" for e in src):
        src.append({"name": "main.cpp", "kind": "file", "size": len(MAIN_CPP), "mtime": 1757900300})
    v = mock_vex.state(mock)
    v["projects"]["p3"] = mock_vex.vex_project("p3")
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
    check("and nothing was asked of the daemon but its status", not _posted(mock, "/projects/p3/vex/build")
          and not _posted(mock, "/projects/p3/vex/download") and not _posted(mock, "/vex/brain"))
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
    check("a failed build shows the errors list", errs.count() == 2 and "✗ Build failed: 1 error, 1 warning"
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


def _explicit_click_checks(page, mock, check, until, fake):
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
          not mock_vex.state(mock)["downloads"] and not _posted(mock, "/projects/p3/vex/download"),
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
    until(lambda: _posted(mock, "/projects/p3/vex/build"), timeout=4)
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
                "vex-name-input", "vex-brain-btn", "vex-info-btn", "vex-error-0"]
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
    mock_vex.state(mock)["info"] = {
        "text": "brain: <b>bold</b> <img src=x onerror=\"window.__pwned=1\">\nVEXos 1.1.5\nbattery 87%",
        "exit_code": 0}
    page.locator(_view(V, _tid("vex-info-btn"))).click()
    pre = page.locator(_view(V, _tid("vex-info-text")))
    until(lambda: pre.count() > 0, timeout=4)
    check("device info is drawn as text", "VEXos 1.1.5" in pre.inner_text() and "<b>bold</b>" in pre.inner_text(),
          pre.inner_text())
    check("markup in it is characters: no element, nothing run",
          pre.locator("b, img").count() == 0 and not page.evaluate("window.__pwned || false"))
    mock_vex.state(mock)["info_refuse"] = ("Device info waits on one step of the setup spike: run `vexcom --help` "
                                           "once and record its device-info option (design §18)")
    page.locator(_view(V, _tid("vex-info-btn"))).click()
    note = page.locator(_view(V, _tid("vex-info-note")))
    check("a refusal is said in the daemon's words", bool(until(lambda: note.count() > 0, timeout=4))
          and "setup spike" in note.inner_text())


def _notice_checks(page, mock, check, until, fake):
    _reset(page, mock, until)
    check("setup: the view draws", _ready(page, until))
    mock.emit("vex_device", {"action": "download", "at": "2026-10-11T12:30:00+00:00"})
    n = page.locator(_view(V, _tid("vex-device-notice")))
    check("a download this window did not start is said", bool(until(lambda: n.count() > 0, timeout=4))
          and "did not start" in n.inner_text())


SECTIONS = (_offer_checks, _setup_checks, _build_checks, _explicit_click_checks, _chain_checks, _card_checks,
            _brain_checks, _info_checks, _notice_checks)


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
