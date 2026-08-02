"""Free synthetic checks for desktop control. No Windows, no bridge, no API.

Three layers get covered here:

  1. `windows/uiatree.py` — snapshot rendering and ref resolution, driven by a
     dict-tree fake backend. This is why that module has no Windows imports.
  2. `jarvis/desktop.py` — the allowlist (the safety boundary) and the wire
     protocol, against a fake bridge on a real loopback socket.
  3. The tool surface — that no desktop tool accepts anything but a registered
     app name, and that none of them leak into background workflows.

Run after touching desktop.py, tools/desktop.py, windows/bridge.py, or
windows/uiatree.py:

    .venv/bin/python tests/desktop_check.py
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "windows"))

import uiatree  # noqa: E402
from jarvis import config, desktop  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    mark = "\033[32m✓\033[0m" if condition else "\033[31m✗\033[0m"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail and not condition else ""))


# ---------------------------------------------------------------------------
# A fake backend: nested dicts standing in for an accessibility tree
# ---------------------------------------------------------------------------

def node(role, name="", *, value="", interactive=None, offscreen=False,
         enabled=True, checked=None, selected=None, kids=()):
    if interactive is None:
        interactive = role in {"button", "editable", "listitem", "checkbox",
                               "combobox", "link", "menuitem", "tab"}
    return {"role": role, "name": name, "value": value, "checked": checked,
            "selected": selected, "offscreen": offscreen, "enabled": enabled,
            "interactive": interactive, "rect": [0, 0, 10, 10], "kids": list(kids)}


class FakeBackend:
    def __init__(self, root):
        self.root = root

    def children(self, n):
        return n["kids"]

    def describe(self, n):
        return {k: v for k, v in n.items() if k != "kids"}


# ---------------------------------------------------------------------------
print("\nuiatree — text hygiene")
# ---------------------------------------------------------------------------

check("private-use icon glyphs are stripped",
      uiatree.clean("\ue721 Search") == "Search",
      repr(uiatree.clean("\ue721 Search")))
check("whitespace collapses", uiatree.clean("  a\n\n b  ") == "a b")
check("None survives", uiatree.clean(None) == "")
check("ordinary text is untouched", uiatree.clean("Windows Update") == "Windows Update")

# ---------------------------------------------------------------------------
print("\nuiatree — the HUD is refused by title")
# ---------------------------------------------------------------------------

check("the HUD title is forbidden", uiatree.forbidden_title("J.A.R.V.I.S."))
check("case and padding do not evade it",
      uiatree.forbidden_title("  j.a.r.v.i.s.  "))
check("the design board is forbidden",
      uiatree.forbidden_title("JARVIS — Design Board"))
check("an ordinary window is allowed", not uiatree.forbidden_title("Settings"))

# ---------------------------------------------------------------------------
print("\nuiatree — snapshot rendering")
# ---------------------------------------------------------------------------

tree = node("window", "Settings", kids=[
    node("pane", kids=[                                    # structural: hidden
        node("listitem", "Personalization", kids=[
            node("text", "Personalization"),               # redundant: hidden
            node("image", "icon", interactive=False),
        ]),
        node("editable", "Search box", value="night light"),
        node("button", "Hidden", offscreen=True),          # offscreen: hidden
        node("checkbox", "Dark mode", checked=True),
        node("button", "Apply", enabled=False),
    ]),
])
text, refs = uiatree.snapshot(FakeBackend(tree))

check("interactive elements get refs", len(refs) == 4, f"{len(refs)}: {list(refs)}")
check("unnamed structural containers are skipped", "pane" not in text)
check("a label duplicating its parent is collapsed",
      text.count("Personalization") == 1, text)
check("values are shown", 'value="night light"' in text)
check("checked state is shown", "checked=true" in text)
check("disabled state is shown", "disabled" in text)
check("offscreen elements are omitted", "Hidden" not in text)
check("refs are sequential from e1", list(refs) == ["e1", "e2", "e3", "e4"], list(refs))
check("a ref records role and name",
      refs["e1"]["role"] == "listitem" and refs["e1"]["name"] == "Personalization")

capped, _ = uiatree.snapshot(FakeBackend(tree), max_elements=2)
check("max_elements truncates and says so", "truncated at 2" in capped)

# A Windows 11 settings switch is a *button* carrying TogglePattern, not a
# checkbox. Reading state only from checkboxes left every switch in Settings
# stateless, and the agent answered "Bluetooth: off" about a radio that was on.
switches = node("window", "S", kids=[
    node("button", "Bluetooth", checked=True),
    node("button", "Wi-Fi", checked=False),
    node("listitem", "Bluetooth & devices", selected=True),
    node("listitem", "Home", selected=False),
])
switch_text, _ = uiatree.snapshot(FakeBackend(switches))
check("a toggle button reads as a switch that is ON",
      'switch "Bluetooth" ON' in switch_text, switch_text)
check("a toggle button reads as a switch that is OFF",
      'switch "Wi-Fi" OFF' in switch_text, switch_text)
check("a real checkbox still reads as checked=",
      'checked=true' in uiatree.snapshot(FakeBackend(node("window", "S", kids=[
          node("checkbox", "Dark mode", checked=True)])))[0])
check("the selected nav item is marked",
      'listitem "Bluetooth & devices" selected' in switch_text, switch_text)
check("unselected items stay quiet (no selected=false noise)",
      "selected=false" not in switch_text and switch_text.count("selected") == 1,
      switch_text)

# ---------------------------------------------------------------------------
print("\nuiatree — ref resolution and staleness")
# ---------------------------------------------------------------------------

backend = FakeBackend(tree)
check("a fresh ref resolves to its element",
      backend.describe(uiatree.resolve(backend, refs["e2"]))["name"] == "Search box")

# The tree shifts: something is inserted ahead of the recorded path.
shifted = node("window", "Settings", kids=[
    node("pane", kids=[
        node("button", "NEW BANNER"),                      # pushes everything down
        node("listitem", "Personalization", kids=[]),
        node("editable", "Search box", value="night light"),
    ]),
])
shifted_backend = FakeBackend(shifted)
resolved = uiatree.resolve(shifted_backend, refs["e2"])
check("a shifted ref re-finds its element by role+name, not by position",
      shifted_backend.describe(resolved)["name"] == "Search box")

# The element is gone entirely.
gone = FakeBackend(node("window", "Settings", kids=[node("pane", kids=[])]))
try:
    uiatree.resolve(gone, refs["e2"])
    check("a vanished ref raises StaleRef", False, "no exception")
except uiatree.StaleRef:
    check("a vanished ref raises StaleRef", True)

# Ambiguity must fail rather than guess.
twins = FakeBackend(node("window", "Settings", kids=[
    node("pane", kids=[node("button", "OK"), node("button", "OK")])]))
_, twin_refs = uiatree.snapshot(twins)
moved = dict(twin_refs["e1"], path=[9, 9])
try:
    uiatree.resolve(twins, moved)
    check("an ambiguous fallback refuses to guess", False, "picked one anyway")
except uiatree.StaleRef:
    check("an ambiguous fallback refuses to guess", True)

# ---------------------------------------------------------------------------
print("\ndesktop.py — the allowlist is the safety boundary")
# ---------------------------------------------------------------------------

session = desktop.DesktopSession(port=0)

for name in ("settings", "claude"):
    check(f"{name!r} is registered", session.app_spec(name)["name"] == name)

check("app names are case-insensitive", session.app_spec("SETTINGS")["name"] == "settings")
check("surrounding space is tolerated", session.app_spec("  claude ")["name"] == "claude")

for bad in ("notepad", "cmd", "powershell", "explorer", "chrome", "msedge",
            "terminal", "", "settings; notepad", "../settings",
            r"C:\Windows\System32\cmd.exe"):
    try:
        session.app_spec(bad)
        check(f"{bad!r} is refused", False, "ALLOWED")
    except desktop.DesktopError:
        check(f"{bad!r} is refused", True)

def refusal_message() -> str:
    try:
        session.app_spec("notepad")
    except desktop.DesktopError as exc:
        return str(exc)
    return ""


check("the refusal lists what IS available",
      "settings" in refusal_message() and "claude" in refusal_message())

# No shell, browser, or file manager may ever be registered — those would each
# route around a gate that exists elsewhere (run_command's approval prompt for
# a terminal; the HUD's authorization card for a browser).
banned = {"cmd", "powershell", "terminal", "wt", "explorer", "chrome",
          "msedge", "firefox", "browser", "regedit"}
check("no shell/browser/file-manager is in the registry",
      not (banned & set(config.DESKTOP_APPS)),
      str(banned & set(config.DESKTOP_APPS)))

# ---------------------------------------------------------------------------
print("\ndesktop.py — the wire protocol, against a fake bridge")
# ---------------------------------------------------------------------------


class FakeBridge:
    """A stand-in for windows/bridge.py: dials in, answers one op at a time."""

    def __init__(self, port, responder):
        self.port, self.responder = port, responder
        self.seen: list[dict] = []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            conn = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        except OSError:
            return
        stream = conn.makefile("rwb")
        stream.write((json.dumps({"hello": "fake", "version": "1"}) + "\n").encode())
        stream.flush()
        while True:
            line = stream.readline()
            if not line:
                return
            req = json.loads(line)
            self.seen.append(req)
            reply = self.responder(req)
            if reply is None:      # simulate the bridge dying mid-request
                conn.close()
                return
            stream.write((json.dumps({"id": req["id"], **reply}) + "\n").encode())
            stream.flush()


def with_bridge(responder):
    s = desktop.DesktopSession(port=0)
    s.start()
    s.port = s._server.getsockname()[1]
    bridge = FakeBridge(s.port, responder)
    connected = s.wait_for_bridge(5)
    return s, bridge, connected


def happy(req: dict) -> dict:
    """Answer each op with the shape the real bridge returns for it."""
    results = {
        "open": {"title": "Settings", "backend": "uia",
                 "snapshot": '[e1] button "Go"'},
        "snapshot": {"snapshot": '[e1] button "Go"'},
        "click": {"clicked": "Go", "via": "Invoke"},
        "type": {"typed_into": "Search box"},
        "key": {"sent": req.get("keys", "")},
        "screenshot": {"image_b64": "AAAA", "bytes": 4, "marks": 1,
                       "size": [800, 600]},
        "ping": {"version": "1", "python": "3.12.0"},
    }
    return {"ok": True, "result": results.get(req["op"], {})}


sess, bridge, connected = with_bridge(happy)
check("the bridge handshake registers a connection", connected)

if connected:
    out = sess.open("settings")
    check("open() sends the resolved spec, not a raw name",
          bridge.seen[-1]["spec"]["title"] == "Settings"
          and bridge.seen[-1]["spec"]["backend"] == "uia")
    check("open() returns the snapshot", '[e1] button "Go"' in out)
    check("open() reports the window and backend",
          "Settings" in out and "uia" in out)

    sess.click("settings", "e1")
    check("click() forwards app and ref",
          bridge.seen[-1]["op"] == "click"
          and bridge.seen[-1]["ref"] == "e1"
          and bridge.seen[-1]["app"] == "settings")

    sess.type_text("settings", "e1", "hello", submit=True)
    check("type() forwards text and submit",
          bridge.seen[-1]["text"] == "hello" and bridge.seen[-1]["submit"] is True)

    sess.send_keys("settings", "{Esc}")
    check("key() forwards the keys", bridge.seen[-1]["keys"] == "{Esc}")

    # An unregistered app must never reach the wire at all.
    before = len(bridge.seen)
    try:
        sess.click("notepad", "e1")
    except desktop.DesktopError:
        pass
    check("an unregistered app never reaches the bridge", len(bridge.seen) == before)
    sess.stop()

# a bridge-side error must surface as DesktopError, not a crash
sess2, _, ok2 = with_bridge(lambda req: {"ok": False, "error": "RuntimeError: stale ref"})
if ok2:
    try:
        sess2.snapshot("settings")
        check("a bridge error becomes DesktopError", False, "no exception")
    except desktop.DesktopError as exc:
        check("a bridge error becomes DesktopError", "stale ref" in str(exc))
    sess2.stop()

# a bridge that dies mid-request must not hang the agent
sess3, _, ok3 = with_bridge(lambda req: None)
if ok3:
    try:
        sess3.snapshot("settings")
        check("a dead bridge raises instead of hanging", False, "no exception")
    except desktop.DesktopError:
        check("a dead bridge raises instead of hanging", True)
    sess3.stop()

# with no bridge at all, the error tells the owner how to start one
lonely = desktop.DesktopSession(port=0)
lonely.start()
try:
    lonely.snapshot("settings")
    check("no bridge gives an actionable error", False, "no exception")
except desktop.DesktopError as exc:
    check("no bridge gives an actionable error", "run-bridge" in str(exc), str(exc))
lonely.stop()

# ---------------------------------------------------------------------------
print("\ntool surface")
# ---------------------------------------------------------------------------

from jarvis import tools  # noqa: E402
from jarvis import workflows  # noqa: E402

desktop_tools = {n: t for n, t in tools.REGISTRY.items() if n.startswith("desktop_")}
check("the desktop tools are registered", len(desktop_tools) == 7, str(sorted(desktop_tools)))

ALLOWED_PARAMS = {"app", "ref", "text", "submit", "keys", "marked"}
offenders = {
    name: set(t.schema["properties"]) - ALLOWED_PARAMS
    for name, t in desktop_tools.items()
    if set(t.schema["properties"]) - ALLOWED_PARAMS
}
check("no desktop tool accepts a title, handle, or path", not offenders, str(offenders))

check("every acting tool takes an app name",
      all("app" in t.schema["properties"]
          for n, t in desktop_tools.items() if n != "desktop_status"))

leaked = [t for t in workflows.SAFE_TOOLS if t.startswith("desktop_")]
check("no desktop tool is available to background workflows", not leaked, str(leaked))

# ---------------------------------------------------------------------------
print()
if FAIL:
    print(f"\033[31m{len(FAIL)} failed\033[0m, {len(PASS)} passed")
    for name in FAIL:
        print(f"  - {name}")
    sys.exit(1)
print(f"\033[32mall {len(PASS)} checks passed\033[0m")
