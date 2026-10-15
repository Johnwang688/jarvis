"""Headless checks for the HUD's New VEX project dialog (VX-3; editor plan
§2.9 "New VEX project", §3's VX-3 package; decisions V-4).

Free and hermetic: no VEX tool, no device, nothing under /mnt. The section
runs against a `MockDaemon(0)` of its own whose VEX world
(`hud_v2_mock_vex.setup_new`) is a temp "Windows drive" holding the
extension's templates folder — real zips, read by the daemon's own template
code — and the Robotics folder new projects go in; a browser context of its
own, under `hud_v2_check.guard` (no request to 8402/8403/8405, no CSP
violation). The daemon's side (the check route, owner-only, refusals word for
word) is `tests/v2/vex_check.py` (`NewProjectCheck`, `NewProject`).

What it pins:
  - the dialog is reachable from New project and from the palette;
  - **the template list comes from the mock's templates folder** — a zip
    added to it is offered on the next open, an example zip never is — split
    by language, labelled from its `index.json`;
  - the name is held to the daemon's rule before anything is asked;
  - **the dialog shows the exact folder** the daemon answers for these
    inputs, and the card names that very path;
  - **Create raises the `project_folder` card, and Deny leaves nothing**: no
    folder, no project, the dialog still open saying so;
  - **Approve creates the project, which appears in the sidebar**, its folder
    holding the template and the settings file;
  - **a Python template yields `src/main.py` and the settings file**;
  - an existing empty folder is used without a card (O5);
  - **under a card every control is disabled and inert**: no click or key
    sends anything, and Escape is the card's (it denies; the dialog stays);
  - with no `vex.json` the dialog names `jarvis vex setup` and creates nothing.

Run (after `cd hud && npm ci --ignore-scripts && npm run build`):
    PYTHONPATH=. .venv/bin/python tests/face/hud_v2_vex_new_check.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.face import hud_v2_mock_vex as mock_vex  # noqa: E402
from tests.face.hud_v2_layout_check import _approval, _boot  # noqa: E402

VIEW = (1400, 900)
CONTROLS = ("vex-new-name", "vex-new-language", "vex-new-template", "vex-new-location", "vex-new-create",
            "vex-new-close")


def _tid(sel: str) -> str:
    return f'[data-testid="{sel}"]'


def _posted(mock, path: str) -> list:
    return [b for (m, p, b) in mock.calls if m == "POST" and p == path]


def _options(page) -> list[tuple[str, str]]:
    return page.locator(f'{_tid("vex-new-template")} option').evaluate_all(
        "els => els.map(e => [e.value, e.textContent])")


def _open_from_new_project(page, until) -> bool:
    page.locator(_tid("new-project")).click()
    until(lambda: page.locator(_tid("project-new-vex")).count() == 1, timeout=3)
    page.locator(_tid("project-new-vex")).click()
    return bool(until(lambda: page.locator(_tid("vex-new")).count() == 1
                      and page.locator(f'{_tid("vex-new-template")} option').count() > 0, timeout=5))


def _path(page) -> str:
    loc = page.locator(_tid("vex-new-path"))
    return loc.inner_text().strip() if loc.count() else ""


def _name(page, until, name: str, expect: str | None) -> bool:
    page.locator(_tid("vex-new-name")).fill(name)
    if expect is None:
        return True
    return bool(until(lambda: _path(page) == expect
                      and page.locator(_tid("vex-new-create")).is_enabled(), timeout=5))


def _card(page, until) -> bool:
    return bool(until(lambda: page.locator(_tid("approval-card")).count() > 0, timeout=5))


def _sidebar_has(page, name: str) -> bool:
    return page.locator('[data-testid^="project-name-"]', has_text=name).count() > 0


def _close(page, until):
    if page.locator(_tid("vex-new")).count():
        page.locator(_tid("vex-new-close")).click()
        until(lambda: page.locator(_tid("vex-new")).count() == 0, timeout=3)


def _list_checks(page, mock, check, until, world):
    from jarvis.v2 import vex
    check("New project offers a New VEX project, which opens the dialog", _open_from_new_project(page, until))
    listed = vex.list_templates(mock_vex._cfg(mock_vex.state(mock)["new"]))
    cpp = [(t["id"], t["label"]) for t in listed if t["language"] == "cpp"]
    check("the C++ templates are the folder's own, labelled from its index.json",
          [tuple(o) for o in _options(page)] == cpp
          and ("cpp_v5_vsc_competition_template", "Competition Template") in cpp, str(_options(page)))
    check("an example zip that is no VS Code template is never offered",
          not [o for o in _options(page) if "Clawbot" in o[0] + o[1]])
    page.locator(_tid("vex-new-language")).select_option("python")
    until(lambda: [o[0] for o in _options(page)] == ["py_v5_vsc_empty"], timeout=3)
    check("Python offers the Python templates only", [o[0] for o in _options(page)] == ["py_v5_vsc_empty"],
          str(_options(page)))
    _close(page, until)
    # A template added to the folder is offered on the next open: the list is the folder's.
    with zipfile.ZipFile(world["templates"] / "cpp_v5_vsc_drivetrain.zip", "w") as archive:
        archive.writestr("src/main.cpp", "int main() { return 0; }\n")
    _open_from_new_project(page, until)
    check("the template list comes from the mock's templates folder (a zip added there is offered)",
          "cpp_v5_vsc_drivetrain" in [o[0] for o in _options(page)], str(_options(page)))
    (world["templates"] / "cpp_v5_vsc_drivetrain.zip").unlink()
    _close(page, until)


def _name_checks(page, mock, check, until, world):
    _open_from_new_project(page, until)
    for bad in ("my bot", "-x", "naïve"):
        page.locator(_tid("vex-new-name")).fill(bad)
        time.sleep(0.4)
        check(f"the name {bad!r} is refused before anything is asked",
              page.locator(_tid("vex-new-name-refusal")).count() == 1
              and page.locator(_tid("vex-new-create")).is_disabled()
              and not [b for b in _posted(mock, "/vex/projects/check") if b.get("name") == bad])
    target = world["robotics"] / "newbot"
    check("the dialog shows the exact folder the daemon answers for the name",
          _name(page, until, "newbot", str(target)), _path(page))
    check("and its Windows spelling",
          page.locator(_tid("vex-new-windows")).inner_text().strip() == "C:\\Users\\johnw\\Documents\\Robotics\\newbot")
    check("and says the folder is approved on a card",
          "card" in page.locator(_tid("vex-new-asks")).inner_text())
    sent = _posted(mock, "/vex/projects/check")[-1]
    check("the check sent exactly the daemon's keys (no location while it is the default)",
          sent == {"name": "newbot", "language": "cpp", "template": "cpp_v5_vsc_competition_template"}, str(sent))
    # A folder the daemon refuses is said in its words, and Create stays off.
    page.locator(_tid("vex-new-location")).fill(str(world["robotics"] / "nowhere"))
    until(lambda: page.locator(_tid("vex-new-refusal")).count() == 1, timeout=4)
    check("a folder the daemon refuses is said in its own words, and Create stays off",
          "does not exist" in page.locator(_tid("vex-new-refusal")).inner_text()
          and page.locator(_tid("vex-new-create")).is_disabled())
    page.locator(_tid("vex-new-location")).fill(str(world["robotics"]))
    _close(page, until)


def _deny_checks(page, mock, check, until, world):
    _open_from_new_project(page, until)
    target = world["robotics"] / "newbot"
    _name(page, until, "newbot", str(target))
    shown = _path(page)
    projects = len(mock.world["projects"])
    page.locator(_tid("vex-new-create")).click()
    check("Create raises the approval card", _card(page, until))
    card = page.locator(_tid("approval-card")).inner_text()
    check("the card is the project_folder card naming the very path the dialog showed",
          page.locator(_tid("approval-tool")).inner_text().strip() == "project_folder" and shown in card, card[:300])
    check("and offers no Always", page.locator(_tid("approval-always")).count() == 0)
    req = page.locator(_tid("approval-card")).get_attribute("data-req")
    time.sleep(0.2)
    off = {t: page.locator(_tid(t)).is_disabled() for t in CONTROLS}
    check("under the card every control of the dialog is disabled", all(off.values()), str(off))
    page.keyboard.press("Escape")
    check("Escape is the card's: it denies", bool(until(lambda: page.locator(_tid("approval-card")).count() == 0,
                                                       timeout=4)))
    decided = _posted(mock, f"/approvals/{req}")
    check("the card was answered deny", bool(decided) and decided[-1].get("decision") == "deny", str(decided))
    state = page.locator(_tid("vex-new-state"))
    check("the dialog stays open and says nothing was made",
          bool(until(lambda: state.count() == 1 and state.get_attribute("data-state") == "declined", timeout=4))
          and page.locator(_tid("vex-new")).count() == 1, state.inner_text() if state.count() else "")
    check("Deny leaves nothing behind: no folder", not target.exists())
    check("and no project", len(mock.world["projects"]) == projects and not _sidebar_has(page, "newbot"))
    check("Create is offered again", page.locator(_tid("vex-new-create")).is_enabled())


def _approve_checks(page, mock, check, until, world):
    if not page.locator(_tid("vex-new")).count():
        _open_from_new_project(page, until)
        _name(page, until, "newbot", str(world["robotics"] / "newbot"))
    target = world["robotics"] / "newbot"
    page.locator(_tid("vex-new-create")).click()
    _card(page, until)
    page.locator(_tid("approval-allow")).click()
    check("Approve: the dialog closes", bool(until(lambda: page.locator(_tid("vex-new")).count() == 0, timeout=6)))
    check("and the project appears in the sidebar", bool(until(lambda: _sidebar_has(page, "newbot"), timeout=6)))
    files = sorted(str(p.relative_to(target)) for p in target.rglob("*") if p.is_file())
    check("its folder holds the template and the settings file",
          files == [".vscode/vex_project_settings.json", "include/vex.h", "makefile", "src/main.cpp",
                    "vex/mkenv.mk", "vex/mkrules.mk"], str(files))
    settings = json.loads((target / ".vscode" / "vex_project_settings.json").read_text())
    check("the settings name the project, C++, V5, slot 1",
          settings["project"]["name"] == "newbot" and settings["project"]["language"] == "cpp"
          and settings["project"]["platform"] == "V5" and settings["project"]["slot"] == 1, str(settings))


def _python_checks(page, mock, check, until, world):
    page.keyboard.press("Control+Shift+P")
    pal = page.locator(_tid("palette"))
    until(lambda: pal.count() == 1, timeout=3)
    page.keyboard.type("new vex")
    rows = page.locator(_tid("palette-row"))
    until(lambda: rows.count() > 0 and "New VEX Project" in rows.first.inner_text(), timeout=3)
    check("the palette offers New VEX Project", "New VEX Project" in rows.first.inner_text(),
          rows.first.inner_text() if rows.count() else "")
    page.keyboard.press("Enter")
    check("and opens the dialog", bool(until(lambda: page.locator(_tid("vex-new")).count() == 1
                                             and page.locator(f'{_tid("vex-new-template")} option').count() > 0,
                                             timeout=5)))
    page.locator(_tid("vex-new-language")).select_option("python")
    until(lambda: [o[0] for o in _options(page)] == ["py_v5_vsc_empty"], timeout=3)
    target = world["robotics"] / "pybot"
    _name(page, until, "pybot", str(target))
    page.locator(_tid("vex-new-create")).click()
    _card(page, until)
    page.locator(_tid("approval-allow")).click()
    until(lambda: _sidebar_has(page, "pybot"), timeout=6)
    check("a Python template yields src/main.py", (target / "src" / "main.py").is_file())
    settings_path = target / ".vscode" / "vex_project_settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.is_file() else {}
    check("and the settings file, in the extension's v2 shape",
          settings.get("project", {}).get("language") == "python"
          and settings.get("project", {}).get("python") == {"main": "src/main.py"}
          and settings.get("extension", {}).get("json") == 2, str(settings))
    files = sorted(str(p.relative_to(target)) for p in target.rglob("*") if p.is_file())
    check("and nothing else", files == [".vscode/vex_project_settings.json", "src/main.py"], str(files))
    check("and it is in the sidebar", _sidebar_has(page, "pybot"))


def _empty_checks(page, mock, check, until, world):
    target = world["robotics"] / "emptybot"
    target.mkdir()
    _open_from_new_project(page, until)
    _name(page, until, "emptybot", str(target))
    check("an existing empty folder is said to be used without asking",
          "without asking" in page.locator(_tid("vex-new-asks")).inner_text())
    page.locator(_tid("vex-new-create")).click()
    check("and Create makes the project with no card",
          bool(until(lambda: _sidebar_has(page, "emptybot"), timeout=6))
          and page.locator(_tid("approval-card")).count() == 0)
    check("its template is in the folder", (target / "src" / "main.cpp").is_file())


def _card_checks(page, mock, check, until, world):
    _open_from_new_project(page, until)
    _name(page, until, "cardbot", str(world["robotics"] / "cardbot"))
    before = len(mock.calls)
    _approval(mock, "vexnewcard1")
    _card(page, until)
    time.sleep(0.3)
    off = {t: page.locator(_tid(t)).is_disabled() for t in CONTROLS}
    check("under any card every control of the dialog is disabled", all(off.values()), str(off))
    inert = page.evaluate("() => !!document.querySelector('[data-testid=\"vex-new\"]').closest('[inert]')")
    check("and inert behind the veil", inert)
    for t in ("vex-new-create", "vex-new-close"):
        try:
            page.locator(_tid(t)).click(force=True, timeout=500)
        except Exception:
            pass
    page.keyboard.press("Enter")
    page.keyboard.type("x")
    time.sleep(0.4)
    sent = [c for c in mock.calls[before:] if c[0] == "POST" and c[1] == "/vex/projects"]
    check("no click or key behind the card sends anything", not sent, str(sent))
    check("and the dialog is still there", page.locator(_tid("vex-new")).count() == 1)
    page.keyboard.press("Escape")
    check("Escape reaches the card (it denies)",
          bool(until(lambda: page.locator(_tid("approval-card")).count() == 0, timeout=4))
          and any(p == "/approvals/vexnewcard1" and (b or {}).get("decision") == "deny"
                  for (m, p, b) in mock.calls[before:] if m == "POST"))
    check("and the dialog neither closed on it nor lost its name",
          page.locator(_tid("vex-new")).count() == 1
          and page.locator(_tid("vex-new-name")).input_value() == "cardbot")
    check("answered, the controls come back",
          bool(until(lambda: page.locator(_tid("vex-new-create")).is_enabled(), timeout=3)))
    check("and nothing was made", not (world["robotics"] / "cardbot").exists())
    page.keyboard.press("Escape")
    check("with no card up, Escape closes the dialog",
          bool(until(lambda: page.locator(_tid("vex-new")).count() == 0, timeout=3)))


def _setup_checks(page, mock, check, until, world):
    v = mock_vex.state(mock)
    keep = dict(v["status"])
    v["status"] = {"configured": False, "hint": "VEX is not set up: run `jarvis vex setup` in a terminal"}
    try:
        page.locator(_tid("new-project")).click()
        page.locator(_tid("project-new-vex")).click()
        note = page.locator(_tid("vex-new-setup"))
        check("with no vex.json the dialog names jarvis vex setup",
              bool(until(lambda: note.count() == 1, timeout=4)) and "jarvis vex setup" in note.inner_text())
        page.locator(_tid("vex-new-name")).fill("setupbot")
        time.sleep(0.4)
        check("and creates nothing", page.locator(_tid("vex-new-create")).is_disabled()
              and not [b for b in _posted(mock, "/vex/projects/check") if b.get("name") == "setupbot"])
        _close(page, until)
    finally:
        v["status"] = keep


def _toolchain_checks(page, mock, check, until, world):
    """Review MED-1: a folder VEX's tools could never build in is refused before
    anyone is asked — the refusal shown, no path, Create off, nothing sent."""
    _open_from_new_project(page, until)
    page.locator(_tid("vex-new-name")).fill("toolbot")
    for shape in ("My (1)", "a&b", "Документы"):
        parent = world["robotics"].parent / shape
        parent.mkdir(exist_ok=True)
        page.locator(_tid("vex-new-location")).fill(str(parent))
        refused = page.locator(_tid("vex-new-refusal"))
        until(lambda: refused.count() == 1 and shape in refused.inner_text(), timeout=4)
        text = refused.inner_text() if refused.count() else ""
        check(f"MED-1: a folder in {shape!r} is refused in the daemon's words before anyone is asked",
              "could never build" in text and "Windows command line" in text, text[:200])
        check(f"MED-1: and with {shape!r} no path is shown and Create stays off",
              page.locator(_tid("vex-new-path")).count() == 0
              and page.locator(_tid("vex-new-create")).is_disabled())
        check(f"MED-1: nothing is made in {shape!r}", list(parent.iterdir()) == [])
    sent = [b for b in _posted(mock, "/vex/projects") if b.get("name") == "toolbot"]
    check("MED-1: Create sent nothing for a folder the toolchain refuses", not sent, str(sent))
    # Review LOW-1: a location the daemon normalises shows the very path the card names.
    location = str(world["robotics"].parent.parent) + "//Documents/Robotics/"
    page.locator(_tid("vex-new-name")).fill("normbot")
    page.locator(_tid("vex-new-location")).fill(location)
    target = str(world["robotics"] / "normbot")
    check("LOW-1: a doubled and a trailing slash show the normalised folder",
          bool(until(lambda: _path(page) == target and page.locator(_tid("vex-new-create")).is_enabled(),
                     timeout=5)), _path(page))
    page.locator(_tid("vex-new-create")).click()
    _card(page, until)
    card = page.locator(_tid("approval-card")).inner_text()
    check("LOW-1: and the card names that very folder", target in card and "//" not in card, card[:300])
    page.keyboard.press("Escape")
    until(lambda: page.locator(_tid("approval-card")).count() == 0, timeout=4)
    page.locator(_tid("vex-new-location")).fill(str(world["robotics"]))
    _close(page, until)


def _blip_checks(page, mock, check, until, world):
    """Review LOW-3: a read of the request that fails while the card is up is a
    blip, not an ending: approve, and the project is made, the dialog closes and
    a new thread opens in it."""
    _open_from_new_project(page, until)
    _name(page, until, "blipbot", str(world["robotics"] / "blipbot"))
    before = len(mock.calls)
    page.locator(_tid("vex-new-create")).click()
    _card(page, until)
    new = mock_vex.state(mock)["new"]
    new["state_fail"] = 2
    note = page.locator(_tid("vex-new-poll-note"))
    check("LOW-3: a failed read says it is still checking",
          bool(until(lambda: note.count() == 1, timeout=6)))
    until(lambda: new.get("state_fail", 0) == 0, timeout=8)
    check("LOW-3: two failed reads end nothing: the dialog still waits on the card",
          page.locator(_tid("vex-new-state")).get_attribute("data-state") == "asking")
    page.locator(_tid("approval-allow")).click()
    check("LOW-3: approved after the blip, the dialog closes",
          bool(until(lambda: page.locator(_tid("vex-new")).count() == 0, timeout=10)))
    check("LOW-3: the project appears in the sidebar", bool(until(lambda: _sidebar_has(page, "blipbot"),
                                                                  timeout=6)))
    pid = next((p["id"] for p in mock.world["projects"] if p["name"] == "blipbot"), None)
    compose = lambda: (page.evaluate("window.__hud.state().compose") or {}).get("projectId")  # noqa: E731
    check("LOW-3: and a new thread opens in it", bool(until(lambda: pid and compose() == pid, timeout=4)),
          str(compose()))
    reads = [p for (m, p, _b) in mock.calls[before:] if m == "GET" and p.startswith("/vex/projects/")]
    check("LOW-3: the dialog read its request, one read at a time, backing off", 3 <= len(reads) < 25,
          str(len(reads)))


SECTIONS = (_list_checks, _name_checks, _deny_checks, _approve_checks, _python_checks, _empty_checks,
            _card_checks, _setup_checks, _toolchain_checks, _blip_checks)


def vex_new_checks(browser, mock, base, check, until, guard, init_script):
    print("\nNew VEX project (VX-3)")
    root = Path(tempfile.mkdtemp(prefix="hud-vex-new-"))
    ctx = browser.new_context(viewport={"width": VIEW[0], "height": VIEW[1]}, permissions=["microphone"])
    guard(ctx)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    only = set(filter(None, (sys.argv[1] if len(sys.argv) > 1 and __name__ == "__main__" else "").split(",")))
    try:
        world = mock_vex.setup_new(mock, root)
        _boot(page, mock, base, until)
        for section in SECTIONS:
            if only and section.__name__.strip("_") not in only:
                continue
            try:
                section(page, mock, check, until, world)
            except Exception as e:  # a section that cannot run is a failure; the rest still run
                lines = [ln.strip() for ln in str(e).splitlines() if ln.strip()]
                check(f"{section.__name__.strip('_')} ran to the end", False, " | ".join(lines[:3])[:300])
                for key in ("Escape", "Escape"):
                    try:
                        page.keyboard.press(key)
                    except Exception:
                        pass
        check("no page errors in the New VEX project section", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()
        shutil.rmtree(root, ignore_errors=True)


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
            vex_new_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until, v2.guard,
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
    print(f"all {v2.CHECKS} New VEX project checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
