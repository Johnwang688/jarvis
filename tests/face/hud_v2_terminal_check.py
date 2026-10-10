"""Headless checks for the HUD's terminals (WP-D; plan docs/plans/2026-10-09-
hud-workspace-plan.md §2.3–§2.4, decisions W-1, W-2, W-5; contract in
docs/hud-api.md).

Free and hermetic. **No shell ever runs and HOME is never touched**: the
PTY is played by `FakePty`, a Playwright `route_web_socket` on the mock's
own ephemeral port, so the HUD's socket never leaves the browser; the
`/terminals` HTTP routes are `hud_v2_mock_terminals`. The section runs
against a `MockDaemon(0)` of its own and a browser context of its own, and
every request or socket to 8402/8403/8405 — the owner's live daemon — is
refused and counted as a failure (`hud_v2_check.guard_live`, which this
suite also proves bites on sockets).

The checks worth keeping, each written to bite:
  - typing round-trips; Space never fires push-to-talk; Ctrl+B and Ctrl+_
    reach the shell while the zoom keys stay the HUD's;
  - **under a card nothing reaches the shell** — not with the card holding
    focus, and not with the terminal's own textarea focused behind it — and
    Escape still denies;
  - **a paste stops the moment the daemon drops a frame**, typing waits for
    the owner's "Resume", nothing of the paste follows the resume, and **a
    paste never continues onto a new socket**;
  - every attach fetches a fresh ticket, builds its URL from `location`,
    and sends `resize` only after `replayed`; a reload reattaches and the
    ring is replayed;
  - another window's takeover asks here (Let it / Keep it), "taken" offers
    to take it back, and a refused newcomer says so;
  - exit offers Restart and Close; Jarvis's restart offers "New terminal
    here"; a busy terminal is asked about before it closes;
  - an attach this window did not make is said, quietly; its own never are;
  - an OSC title never reaches `document.title` and renders as text; a
    `javascript:` link is inert, an http link opens only on Ctrl+click;
  - at 160% and 70% the terminal fits its pane, its text scales with the
    HUD, and a drag selects exactly the cells under the pointer;
  - one terminal is drawn in one place: in a pane, its panel tab says so.

Run (after `cd hud && npm ci && npm run build`):
    PYTHONPATH=. .venv/bin/python tests/face/hud_v2_terminal_check.py
"""
from __future__ import annotations

import json
import re
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.face import hud_v2_mock_terminals as mock_terminals  # noqa: E402

# A window.open that records instead of opening, and no real clipboard.
OPEN_STUB = """
window.__opened = [];
window.open = function (url) { window.__opened.push(String(url)); return null; };
"""


def _echo_line(line: str) -> bytes:
    """What the fake shell prints for a command line."""
    word, _, rest = line.partition(" ")
    if word == "echo":
        return rest.encode() + b"\r\n"
    if not word:
        return b""
    return f"fake: {word}: command not found\r\n".encode()


class FakePty:
    """The daemon's half of `/terminals/{id}/attach` (docs/hud-api.md), for
    one browser context, played by `route_web_socket` — the socket never
    leaves the browser, and no shell runs. It redeems the mock's tickets
    (single use, one terminal), keeps a ring per terminal and replays it,
    echoes like a tty, latches on a scripted drop, answers `input_resume`,
    asks takeovers, and publishes `terminal_attached` as the daemon does."""

    def __init__(self, ctx, mock):
        self.mock = mock
        self.sockets: list[dict] = []
        self.holders: dict[str, dict] = {}
        self.ring: dict[str, bytearray] = {}
        self.line: dict[str, str] = {}
        self.controls: list[tuple[str, dict, float, int]] = []
        self.drop_after: dict[str, int] = {}
        self.drop_at: dict[str, int] = {}
        self.resume_refuse: set[str] = set()
        self.hold: set[str] = set()
        self.waiting: dict[str, dict] = {}
        self.takeover_answers: list[dict] = []
        self.refused_tickets = 0
        self.closing: list[tuple[dict, int, str]] = []
        ctx.route_web_socket(re.compile(rf"^ws://127\.0\.0\.1:{mock.port}/terminals/"), self._attach)

    def _close(self, sock, code: int, reason: str):
        """Close a socket — later, from the test's own flow (`flush`): a close
        from inside a route or message handler deadlocks the sync API."""
        if sock["open"]:
            sock["open"] = False
            self.closing.append((sock, code, reason))

    def flush(self):
        pending, self.closing = self.closing, []
        for sock, code, reason in pending:
            sock["ws"].close(code=code, reason=reason)

    # -- the socket -------------------------------------------------------------

    def _row(self, tid):
        return next((r for r in self.mock.world.get("terminals", []) if r["id"] == tid), None)

    def _attach(self, ws):
        url = urlparse(ws.url)
        parts = [p for p in url.path.split("/") if p]
        tid = parts[1] if len(parts) == 3 and parts[2] == "attach" else ""
        ticket = parse_qs(url.query).get("ticket", [""])[0]
        tickets = self.mock.__dict__.setdefault("terminal_tickets", {})
        sock = {"tid": tid, "ws": ws, "url": ws.url, "frames": [], "latched": False, "open": True,
                "n": len(self.sockets), "replayed_at": None, "at": time.monotonic()}
        self.sockets.append(sock)
        if not ticket or tickets.pop(ticket, None) != tid or self._row(tid) is None:
            self.refused_tickets += 1
            self._close(sock, 1008, "a fresh ticket for this terminal is required")
            return
        ws.on_message(lambda m, s=sock: self._message(s, m))
        # No on_close: Playwright's own handler for it raises KeyError('code')
        # on a page-side close without a code (every reload), and the fake
        # never needs to know — a send to a closed page socket is dropped.
        if tid in self.hold:
            self.hold.discard(tid)
            self.waiting[tid] = sock
            self._send(sock, {"type": "waiting", "timeout_s": 20})
            return
        self._install(sock)

    def _install(self, sock):
        tid, ws = sock["tid"], sock["ws"]
        row = self._row(tid)
        old = self.holders.get(tid)
        if old is not None and old is not sock:
            old["open"] = False
        self.holders[tid] = sock
        ring = bytes(self.ring.setdefault(tid, bytearray()))
        self._send(sock, {"type": "attached", "terminal": dict(row, shown=True), "replay": len(ring)})
        for i in range(0, len(ring), 32768):
            ws.send(ring[i:i + 32768])
        self._send(sock, {"type": "replayed"})
        sock["replayed_at"] = time.monotonic()
        if row.get("exited"):
            self._send(sock, {"type": "exit", "code": row.get("exit_code")})
        # As the daemon does on every attach: the id and the time, nothing else.
        self.mock.emit("terminal_attached", {"terminal_id": tid, "at": "2026-10-09T12:00:00+00:00"})

    def _send(self, sock, payload: dict):
        if sock["open"]:
            sock["ws"].send(json.dumps(payload))

    def _message(self, sock, m):
        tid = sock["tid"]
        if isinstance(m, (bytes, bytearray)):
            data = bytes(m)
            sock["frames"].append(data)
            if sock["latched"] and data != b"\x03":
                self._send(sock, {"type": "input_dropped", "bytes": len(data), "latched": True,
                                  "reason": "input is paused after a dropped paste"})
                return
            limit = self.drop_after.get(tid)
            if limit is not None and len([f for f in sock["frames"] if f != b"\x03"]) > limit and data != b"\x03":
                sock["latched"] = True
                self._send(sock, {"type": "input_dropped", "bytes": len(data), "latched": True,
                                  "reason": "the program in this terminal is not reading its input"})
                return
            if self.holders.get(tid) is sock and len(data) < 1024:
                self._tty(tid, data)
            return
        try:
            msg = json.loads(m)
        except ValueError:
            return
        self.controls.append((tid, msg, time.monotonic(), sock["n"]))
        kind = msg.get("type")
        if kind == "input_resume":
            if tid in self.resume_refuse:
                self._send(sock, {"type": "input_resume_refused", "reason": "earlier input is still being written"})
            else:
                sock["latched"] = False
                self._send(sock, {"type": "input_resumed"})
        elif kind == "takeover":
            self.takeover_answers.append(msg)
            if msg.get("allow") is True and self.holders.get(tid) is sock:
                self._send(sock, {"type": "taken"})
                self._close(sock, 1000, "taken by another window")
                self.holders.pop(tid, None)

    def _tty(self, tid, data: bytes):
        """Echo as a tty would, and run `echo` lines."""
        out = bytearray()
        line = self.line.get(tid, "")
        for ch in data.decode("utf-8", "replace"):
            if ch == "\r":
                out += b"\r\n" + _echo_line(line) + b"$ "
                line = ""
            elif ch == "\x7f":
                line = line[:-1]
                out += b"\b \b"
            elif ch >= " ":
                line += ch
                out += ch.encode()
        self.line[tid] = line
        if out:
            self.output(tid, bytes(out))

    # -- the test's levers -----------------------------------------------------

    def output(self, tid: str, data: bytes):
        self.ring.setdefault(tid, bytearray()).extend(data)
        sock = self.holders.get(tid)
        if sock and sock["open"]:
            sock["ws"].send(data)

    def control(self, tid: str, payload: dict):
        sock = self.holders.get(tid)
        if sock:
            self._send(sock, payload)

    def exit(self, tid: str, code: int):
        row = self._row(tid)
        row["exited"], row["exit_code"] = True, code
        self.control(tid, {"type": "exit", "code": code})

    def end(self, tid: str):
        """Jarvis stopped: exit "ended", close 1001, and the terminal is gone."""
        rows = self.mock.world.get("terminals", [])
        row = self._row(tid)
        if row in rows:
            rows.remove(row)
        sock = self.holders.pop(tid, None)
        if sock and sock["open"]:
            self._send(sock, {"type": "exit", "code": None, "reason": "ended"})
            self._close(sock, 1001, "ended")
            self.flush()

    def drop(self, tid: str):
        """The daemon drops the socket (too far behind): the window reattaches."""
        sock = self.holders.pop(tid, None)
        if sock and sock["open"]:
            self._close(sock, 1011, "dropped")
            self.flush()

    def drop_after_frames(self, tid: str, n: int):
        """Arm a drop once `n` more frames have arrived (`drop_if_due`). Not
        from inside the message handler: a sync-API close there deadlocks."""
        sock = self.holders[tid]
        self.drop_at[tid] = len(sock["frames"]) + n

    def drop_if_due(self, tid: str) -> bool:
        sock = self.holders.get(tid)
        at = self.drop_at.get(tid)
        if sock is None or at is None or len(sock["frames"]) < at:
            return False
        del self.drop_at[tid]
        self.drop(tid)
        return True

    def ask_takeover(self, tid: str, req: str = "tk01"):
        self.control(tid, {"type": "takeover_request", "id": req, "timeout_s": 20})

    def finish_waiting(self, tid: str, allow: bool):
        sock = self.waiting.pop(tid)
        if allow:
            self._install(sock)
        else:
            self._send(sock, {"type": "refused", "reason": "the window showing this terminal kept it"})
            self._close(sock, 1008, "refused")
            self.flush()

    # -- reading --------------------------------------------------------------

    def of(self, tid: str) -> list[dict]:
        return [s for s in self.sockets if s["tid"] == tid]

    def frames(self, tid: str, since_socket: int = 0) -> list[bytes]:
        return [f for s in self.of(tid) if s["n"] >= since_socket for f in s["frames"]]

    def sent(self, tid: str) -> bytes:
        return b"".join(self.frames(tid))

    def resizes(self, tid: str) -> list[tuple[dict, float, int]]:
        return [(m, t, n) for (i, m, t, n) in self.controls if i == tid and m.get("type") == "resize"]


# ---------------------------------------------------------------------------
# helpers


def _boot(page, mock, base, until, reload=False):
    before = mock.sse_connections()
    if reload:
        page.reload()
    else:
        page.goto(base + "/")
    page.wait_for_selector('[data-testid="sidebar"]', state="attached")
    until(lambda: page.locator('[data-testid="project-p1"]').count() > 0)
    mock.await_reconnect(before)
    page.wait_for_timeout(150)


_KEYS = {"zoom": "jarvis.hud.zoom", "ws": "jarvis.hud.workspace", "prefs": "jarvis.hud.terminals",
         "layout": "jarvis.hud.layout"}


def _store(page, **values):
    """Set the named stores; None removes one, and an unnamed one is left alone."""
    page.evaluate("""(pairs) => { for (const [k, v] of pairs) {
      if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v);
    } }""", [[_KEYS[k], v] for k, v in values.items()])


def _sel(tid: str, inner: str = "") -> str:
    return f'[data-testid="terminal-{tid}"]' + (f" {inner}" if inner else "")


def _state(page, tid):
    loc = page.locator(_sel(tid))
    return loc.first.get_attribute("data-state") if loc.count() else None


def _text(page, tid) -> str:
    return page.evaluate("id => window.__hudTerminals.text(id)", tid) or ""


def _visible(page, sel: str) -> bool:
    loc = page.locator(sel)
    return loc.count() > 0 and loc.first.is_visible()


def _focus_term(page, tid):
    page.locator(_sel(tid, ".xterm-screen")).click(position={"x": 30, "y": 10})
    page.wait_for_timeout(60)


def _in_term(page) -> bool:
    return page.evaluate("!!(document.activeElement && document.activeElement.closest('.termslot'))")


def _approval(mock, req: str, command: str = "ls"):
    mock.emit("approval_requested", {
        "req_id": req, "code": req[-4:].upper(), "tool": "run_command", "args": {"command": command},
        "command": command, "reason": "reviewer declined", "layer": "human", "origin": "task: terminal",
        "allowlistable": True, "timeout_s": 120,
    })


def _paste(page, tid, text):
    """A real paste event on xterm's textarea, as Ctrl+V delivers one."""
    page.evaluate("""([id, text]) => {
      const ta = document.querySelector(`[data-testid="terminal-${id}"] .xterm-helper-textarea`);
      ta.focus();
      const dt = new DataTransfer();
      dt.setData('text/plain', text);
      ta.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
    }""", [tid, text])


def _cell(page, tid, col, row, frac=0.5):
    box = page.locator(_sel(tid, ".xterm-screen")).bounding_box()
    size = page.evaluate("id => window.__hudTerminals.size(id)", tid)
    cw, ch = box["width"] / size["cols"], box["height"] / size["rows"]
    return box["x"] + (col + frac) * cw, box["y"] + (row + 0.5) * ch, cw, ch


def _attached(page, until, tid, timeout=6):
    return until(lambda: _state(page, tid) == "attached", timeout=timeout)


def _created(mock) -> list:
    return mock.__dict__.get("terminal_created", [])


def _reset(page, mock, base, until, fake, size=(1280, 800)):
    """A clean window: no terminals in the world, default layout, panel shut."""
    mock.world["terminals"] = []
    page.set_viewport_size({"width": size[0], "height": size[1]})
    _store(page, zoom="100", ws=None, prefs=None, layout="{}")
    _boot(page, mock, base, until, reload=True)


def _open_terminal(page, mock, until, fake) -> str:
    """Ctrl+` from the body with no terminal open: the panel opens with a new one, attached."""
    n = len(_created(mock))
    page.evaluate("document.activeElement && document.activeElement.blur()")
    page.keyboard.press("Control+Backquote")
    until(lambda: len(_created(mock)) > n, timeout=4)
    if len(_created(mock)) == n:
        raise AssertionError("Ctrl+` opened no terminal")
    tid = _created(mock)[-1][0]
    _attached(page, until, tid)
    until(lambda: fake.resizes(tid), timeout=4)
    return tid


def _new_terminal(page, mock, until, fake) -> str:
    """The panel open (⬓ opens nothing by itself), then `+`: a new terminal, attached."""
    if not _visible(page, '[data-testid="panel"]'):
        page.locator('[data-testid="toggle-panel"]').click()
        until(lambda: _visible(page, '[data-testid="panel-new"]'), timeout=3)
    n = len(_created(mock))
    page.locator('[data-testid="panel-new"]').click()
    until(lambda: len(_created(mock)) > n, timeout=4)
    if len(_created(mock)) == n:
        raise AssertionError("+ opened no terminal")
    tid = _created(mock)[-1][0]
    _attached(page, until, tid)
    until(lambda: fake.resizes(tid), timeout=4)
    return tid


def _fresh(page, mock, base, until, fake, size=(1280, 800)) -> str:
    """Every section starts here: a clean window and one new terminal in the panel."""
    _reset(page, mock, base, until, fake, size)
    return _new_terminal(page, mock, until, fake)


# ---------------------------------------------------------------------------
# the suite


def pumping(page, until, fake=None):
    """`until`, but every poll lets Playwright dispatch: the fake PTY's
    handlers run only while a Playwright call is in progress, so a predicate
    that reads only the fake's state would otherwise wait on events nobody
    delivers. Each poll also carries out the closes the fake deferred."""
    def wait(fn, timeout=6.0, step=0.0):
        def poll():
            page.wait_for_timeout(20)
            if fake is not None:
                fake.flush()
            return fn()
        return until(poll, timeout=timeout, step=step)
    return wait


def terminal_checks(browser, mock, base, check, until, guard, init_script):
    print("\nterminals")
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, permissions=["microphone"])
    guard(ctx)
    ctx.add_init_script(init_script)
    ctx.add_init_script(OPEN_STUB)
    fake = FakePty(ctx, mock)
    page = ctx.new_page()
    until = pumping(page, until, fake)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(f"{e} @ {(getattr(e, 'stack', '') or '').splitlines()[1:3]}"))
    page.set_default_timeout(5000)
    only = set(filter(None, (sys.argv[1] if len(sys.argv) > 1 and __name__ == "__main__" else "").split(",")))
    try:
        _boot(page, mock, base, until)
        for section in (_open_checks, _typing_checks, _card_checks, _paste_checks, _reload_checks,
                        _takeover_checks, _exit_checks, _busy_checks, _readable_checks, _integration_checks,
                        _osc_link_checks, _pane_checks, _zoom_checks, _ended_checks):
            if only and section.__name__.strip("_") not in only:
                continue
            try:
                section(page, mock, base, check, until, fake)
            except Exception as e:  # a section that cannot run is a failure; the rest still run
                lines = [ln.strip() for ln in str(e).splitlines() if ln.strip()]
                check(f"{section.__name__.strip('_')} ran to the end", False, " | ".join(lines[:3])[:300])
                try:
                    page.keyboard.press("Escape")
                    _reset(page, mock, base, until, fake)
                except Exception:
                    pass
        check("no page errors in the terminal section", not errors, "; ".join(errors[:3]))
        check("every socket the HUD opened presented a fresh ticket", fake.refused_tickets == 0,
              f"{fake.refused_tickets} refused")
    finally:
        ctx.close()
    _guard_selftest(browser, mock, base, check, until, init_script)


def _open_checks(page, mock, base, check, until, fake):
    _reset(page, mock, base, until, fake)
    check("a fresh window opens no terminal and attaches nothing",
          not fake.sockets and not _created(mock) and not _visible(page, '[data-testid="panel"]'))
    page.locator('[data-testid="toggle-panel"]').click()
    until(lambda: _visible(page, '[data-testid="panel-empty"]'), timeout=3)
    check("⬓ shows the panel and says there is no terminal, creating none",
          _visible(page, '[data-testid="panel-empty"]') and not _created(mock))
    page.locator('[data-testid="toggle-panel"]').click()
    until(lambda: not _visible(page, '[data-testid="panel"]'), timeout=2)
    tid = _open_terminal(page, mock, until, fake)
    check("Ctrl+` opens the panel with a terminal when there is none (VS Code's key)",
          _visible(page, '[data-testid="panel"]') and _visible(page, _sel(tid))
          and _visible(page, f'[data-testid="panel-tab-{tid}"]'))
    check("in the folder of what the focused pane shows: the chat's project, as an id",
          _created(mock)[-1][1] == {"project": "p1"}, str(_created(mock)[-1]))
    sock = fake.of(tid)[0]
    check("the socket's URL is this window's own host and carries a ticket the mock minted",
          sock["url"].startswith(f"ws://127.0.0.1:{mock.port}/terminals/{tid}/attach?ticket=")
          and sock["open"], sock["url"])
    tickets = len(mock.sent("POST", f"/terminals/{tid}/ticket"))
    check("a ticket was fetched for the attach", tickets == 1, str(tickets))
    rz = fake.resizes(tid)
    check("a resize follows the replay, at the fitted size",
          rz and rz[0][1] >= sock["replayed_at"] and 2 <= rz[0][0]["cols"] <= 1000 and 1 <= rz[0][0]["rows"] <= 500
          and rz[0][0]["cols"] == page.evaluate("id => window.__hudTerminals.size(id).cols", tid),
          str(rz[:1]))
    check("the window's <title> is still J.A.R.V.I.S.", page.title() == "J.A.R.V.I.S.", page.title())
    page.evaluate("document.activeElement && document.activeElement.blur()")
    page.keyboard.press("Control+Backquote")
    until(lambda: not _visible(page, '[data-testid="panel"]'), timeout=2)
    page.keyboard.press("Control+Backquote")
    until(lambda: _visible(page, _sel(tid)), timeout=2)
    page.wait_for_timeout(200)
    check("hiding and showing the panel keeps the terminal and its socket (no second terminal, no reattach)",
          len(_created(mock)) == 1 and len(fake.of(tid)) == 1 and _state(page, tid) == "attached",
          f"{len(_created(mock))} {len(fake.of(tid))}")
    check("its tab is selected and names it", page.locator(f'[data-testid="panel-tab-{tid}"]')
          .get_attribute("aria-selected") == "true")


def _typing_checks(page, mock, base, check, until, fake):
    tid = _fresh(page, mock, base, until, fake)
    _focus_term(page, tid)
    check("setup: keys are going into the terminal", _in_term(page))
    page.keyboard.type("echo hello")
    page.keyboard.press("Enter")
    until(lambda: "\nhello" in _text(page, tid), timeout=3)
    check("typing round-trips: the keys reach the shell and its output is drawn",
          b"echo hello\r" in fake.sent(tid) and "\nhello" in _text(page, tid), _text(page, tid)[-80:])
    before = len(fake.sent(tid))
    page.keyboard.down("Space")
    page.wait_for_timeout(150)
    ptt = page.evaluate("window.__hud.capture.ptt")
    page.keyboard.up("Space")
    page.wait_for_timeout(100)
    check("Space in the terminal is the shell's, never push-to-talk",
          not ptt and fake.sent(tid)[before:] == b" ", f"ptt={ptt} sent={fake.sent(tid)[before:]!r}")
    before = len(fake.sent(tid))
    page.keyboard.press("Control+b")
    page.wait_for_timeout(150)
    check("Ctrl+B reaches the shell (readline back-char) and folds nothing",
          fake.sent(tid)[before:] == b"\x02" and _visible(page, "#sidebar"), repr(fake.sent(tid)[before:]))
    before = len(fake.sent(tid))
    zoom = page.evaluate("getComputedStyle(document.getElementById('root')).zoom")
    page.keyboard.press("Control+Shift+Minus")
    page.wait_for_timeout(150)
    check("Ctrl+_ reaches the shell (readline undo) and zooms nothing",
          fake.sent(tid)[before:] == b"\x1f"
          and page.evaluate("getComputedStyle(document.getElementById('root')).zoom") == zoom,
          repr(fake.sent(tid)[before:]))
    before = len(fake.sent(tid))
    page.keyboard.press("Control+Equal")
    page.wait_for_timeout(150)
    zoomed = page.evaluate("getComputedStyle(document.getElementById('root')).zoom")
    check("Ctrl+= stays the HUD's zoom there, and sends the shell nothing",
          abs(float(zoomed) - 1.1) < 1e-6 and fake.sent(tid)[before:] == b"", f"{zoomed} {fake.sent(tid)[before:]!r}")
    page.keyboard.press("Control+0")
    until(lambda: abs(float(page.evaluate("getComputedStyle(document.getElementById('root')).zoom")) - 1) < 1e-6,
          timeout=2)
    before = len(fake.sent(tid))
    page.keyboard.press("Escape")
    page.wait_for_timeout(100)
    check("Escape is the shell's in a terminal", fake.sent(tid)[before:] == b"\x1b", repr(fake.sent(tid)[before:]))


def _card_checks(page, mock, base, check, until, fake):
    tid = _fresh(page, mock, base, until, fake)
    _focus_term(page, tid)
    check("setup: typing into the terminal", _in_term(page))
    before = len(fake.sent(tid))
    _approval(mock, "trm1")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    page.wait_for_timeout(200)
    focus = page.evaluate("document.activeElement && document.activeElement.getAttribute('data-testid')")
    check("a card takes focus off the terminal, onto the card", focus == "approval-card", str(focus))
    page.keyboard.type("y")
    page.keyboard.press("Enter")
    page.wait_for_timeout(150)
    check("a y⏎ typed under the card goes nowhere", fake.sent(tid)[before:] == b"", repr(fake.sent(tid)[before:]))
    # Even with the terminal's own textarea focused behind the card.
    page.evaluate(f"document.querySelector('{_sel(tid)} .xterm-helper-textarea').focus()")
    page.keyboard.type("y")
    page.keyboard.press("Enter")
    _paste(page, tid, "rm -rf ~/scratch\r")
    page.wait_for_timeout(250)
    check("and with the terminal focused behind it, keys and a paste still reach nothing",
          fake.sent(tid)[before:] == b"", repr(fake.sent(tid)[before:]))
    fake.output(tid, b"\r\noutput-under-card\r\n$ ")
    until(lambda: "output-under-card" in _text(page, tid), timeout=3)
    check("output keeps drawing under the card", "output-under-card" in _text(page, tid))
    check("its switch and × are disabled behind the card",
          page.locator(_sel(tid, '[data-testid="term-readable"]')).is_disabled()
          and page.locator(_sel(tid, '[data-testid="term-close"]')).is_disabled())
    page.keyboard.press("Escape")
    body = until(lambda: mock.sent("POST", "/approvals/trm1") or None, timeout=4)
    check("Escape still reaches the card and denies", bool(body) and body[-1].get("decision") == "deny",
          str(body[-1] if body else None))
    check("and the Escape went to the card, not the shell", fake.sent(tid)[before:] == b"",
          repr(fake.sent(tid)[before:]))
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    page.wait_for_timeout(150)
    check("once it is answered, focus is back in the terminal", _in_term(page))
    page.keyboard.type("echo back")
    page.keyboard.press("Enter")
    until(lambda: "\nback" in _text(page, tid), timeout=3)
    check("and typing works again", b"echo back\r" in fake.sent(tid)[before:])


def _resumes(fake, tid):
    return [m for (i, m, _, _) in fake.controls if i == tid and m.get("type") == "input_resume"]


def _paste_checks(page, mock, base, check, until, fake):
    tid = _fresh(page, mock, base, until, fake)
    big = "".join(f"{i:07d}\n" for i in range(256 * 1024))           # 2 MiB: 128 chunks
    chunks = -(-len(big) // 16384)
    sock_n = fake.holders[tid]["n"]
    fake.drop_after[tid] = 2
    _focus_term(page, tid)
    _paste(page, tid, big)
    until(lambda: _visible(page, _sel(tid, '[data-testid="term-paste-notice"]')), timeout=4)
    page.wait_for_timeout(1500)
    sent = [f for f in fake.frames(tid, sock_n) if f != b"\x03"]
    check("a paste goes out in chunks of at most 16 KiB",
          sent and max(len(f) for f in sent) <= 16384, str([len(f) for f in sent[:4]]))
    check("and stops when the daemon drops a frame: the rest of it is never sent",
          len(sent) < chunks, f"{len(sent)} of {chunks} chunks reached the daemon")
    count = len(fake.frames(tid, sock_n))
    page.wait_for_timeout(600)
    check("nothing more of it is sent afterwards", len(fake.frames(tid, sock_n)) == count,
          f"{count} -> {len(fake.frames(tid, sock_n))}")
    fake.drop_after.pop(tid, None)          # the socket stays latched until it resumes
    note = page.locator(_sel(tid, '[data-testid="term-paste-notice"]'))
    check("the owner is told, in words, and typing is paused",
          note.get_attribute("data-kind") == "dropped" and "not reading" in note.inner_text()
          and page.evaluate("id => window.__hudTerminals.latched(id)", tid) is True, note.inner_text()[:120])
    check("and the notice cannot be dismissed while it holds the only way to resume",
          page.locator(_sel(tid, '[data-testid="term-paste-dismiss"]')).count() == 0
          and _visible(page, _sel(tid, '[data-testid="term-resume"]')))
    before = len(fake.frames(tid, sock_n))
    page.keyboard.type("abc")
    page.wait_for_timeout(200)
    check("typing while paused sends nothing, and asks nothing of the daemon on its own",
          len(fake.frames(tid, sock_n)) == before and not _resumes(fake, tid))
    page.keyboard.press("Control+c")
    page.wait_for_timeout(150)
    check("a lone Ctrl-C still goes through", fake.frames(tid, sock_n)[before:] == [b"\x03"],
          repr(fake.frames(tid, sock_n)[before:]))
    # The daemon refuses to resume until its queue drains: the window asks again.
    fake.resume_refuse.add(tid)
    page.locator(_sel(tid, '[data-testid="term-resume"]')).click()
    until(lambda: len(_resumes(fake, tid)) >= 2, timeout=5)
    check("Resume asks the daemon, and asks again while it is refused", len(_resumes(fake, tid)) >= 2,
          str(len(_resumes(fake, tid))))
    check("and says the way out is reattaching or closing",
          "reattach or close" in note.inner_text() and _visible(page, _sel(tid, '[data-testid="term-reattach"]')),
          note.inner_text()[:160])
    fake.resume_refuse.discard(tid)
    until(lambda: not _visible(page, _sel(tid, '[data-testid="term-paste-notice"]')), timeout=4)
    check("once the daemon resumes, the notice goes",
          not _visible(page, _sel(tid, '[data-testid="term-paste-notice"]')))
    before = len(fake.frames(tid, sock_n))
    _focus_term(page, tid)
    page.keyboard.type("z")
    page.wait_for_timeout(400)
    check("typing works again, and nothing of the paste follows it",
          fake.frames(tid, sock_n)[before:] == [b"z"], repr([f[:12] for f in fake.frames(tid, sock_n)[before:]]))

    # The daemon drops the socket while a paste is going out: the window
    # reattaches, and the rest of the paste dies with the old socket.
    sockets = len(fake.of(tid))
    fake.drop_after_frames(tid, 3)
    old = fake.holders[tid]
    seen = len([f for f in old["frames"] if len(f) > 1])
    _paste(page, tid, big)
    dropped = until(lambda: fake.drop_if_due(tid), timeout=5)
    until(lambda: len(fake.of(tid)) > sockets and _state(page, tid) == "attached", timeout=8)
    new = fake.of(tid)[-1]
    page.wait_for_timeout(1500)
    pasted = [f for f in old["frames"] if len(f) > 1][seen:]
    if not dropped or new is old:
        raise AssertionError(f"setup: the daemon never dropped the socket mid-paste ({len(pasted)} frames)")
    check("setup: the socket closed with the paste part-way out",
          0 < len(pasted) < chunks and not old["open"], f"{len(pasted)} of {chunks} on the old socket")
    check("the window reattaches with a fresh ticket", new["open"] and _state(page, tid) == "attached")
    check("and never continues the paste on the new socket", new["frames"] == [],
          f"{len(new['frames'])} frames, first {new['frames'][:1]!r}"[:200])
    lost = page.locator(_sel(tid, '[data-testid="term-paste-notice"]'))
    check("and says the rest of the paste was not sent, and will not be",
          lost.count() > 0 and lost.get_attribute("data-kind") == "lost" and "will not be" in lost.inner_text())
    # Not even behind the owner's next keystroke: what goes out on the new
    # socket is exactly what was typed there (a queue that outlived its
    # socket would send the rest of the paste ahead of these keys).
    _focus_term(page, tid)
    page.keyboard.type("echo fresh")
    page.keyboard.press("Enter")
    until(lambda: "\nfresh" in _text(page, tid), timeout=3)
    page.wait_for_timeout(300)
    check("typing on the new socket sends exactly the keys typed, nothing of the old paste",
          b"".join(new["frames"]) == b"echo fresh\r", repr(b"".join(new["frames"])[:60]))
    if lost.count():
        page.locator(_sel(tid, '[data-testid="term-paste-dismiss"]')).click()


def _reload_checks(page, mock, base, check, until, fake):
    tid = _fresh(page, mock, base, until, fake)
    _focus_term(page, tid)
    page.keyboard.type("echo before-reload")
    page.keyboard.press("Enter")
    until(lambda: "\nbefore-reload" in _text(page, tid), timeout=3)
    sockets = len(fake.of(tid))
    tickets = len(mock.sent("POST", f"/terminals/{tid}/ticket"))
    _boot(page, mock, base, until, reload=True)
    until(lambda: _state(page, tid) == "attached" and "before-reload" in _text(page, tid), timeout=6)
    check("after a reload the terminal is back, its output replayed",
          _state(page, tid) == "attached" and "\nbefore-reload" in _text(page, tid), _text(page, tid)[-120:])
    new = fake.of(tid)[-1]
    check("on a new socket with a new ticket",
          len(fake.of(tid)) == sockets + 1 and len(mock.sent("POST", f"/terminals/{tid}/ticket")) == tickets + 1)
    until(lambda: [r for r in fake.resizes(tid) if r[2] == new["n"]], timeout=3)
    rz = [r for r in fake.resizes(tid) if r[2] == new["n"]]
    check("and a resize after the replay, so a full-screen program redraws",
          rz and rz[0][1] >= new["replayed_at"], str(rz[:1]))
    page.wait_for_timeout(300)
    check("its own attaches are never announced",
          page.locator('[data-testid="term-attached-notice"]').count() == 0)
    mock.emit("terminal_attached", {"terminal_id": tid, "at": "2026-10-09T15:42:00+00:00"})
    until(lambda: page.locator('[data-testid="term-attached-notice"]').count() > 0, timeout=4)
    notice = page.locator('[data-testid="term-attached-notice"]')
    check("an attach this window did not make is said, quietly, naming the terminal",
          notice.count() == 1 and "other than this window" in notice.inner_text()
          and "bash · jarvis" in notice.inner_text() and notice.get_attribute("role") == "status",
          notice.inner_text() if notice.count() else "")
    page.locator('[data-testid="term-attached-dismiss"]').click()
    until(lambda: page.locator('[data-testid="term-attached-notice"]').count() == 0, timeout=2)
    check("and is dismissed with ×", page.locator('[data-testid="term-attached-notice"]').count() == 0)
    mock.emit("terminal_attached", {"terminal_id": "../etc", "at": "x"})
    mock.emit("terminal_attached", {"terminal_id": tid, "output": "x", "at": "<b>x</b>"})
    until(lambda: page.locator('[data-testid="term-attached-notice"]').count() > 0, timeout=3)
    page.wait_for_timeout(200)
    check("a malformed record names nothing, and a strange time is not drawn as markup",
          page.locator('[data-testid="term-attached-notice"]').count() == 1
          and page.locator('[data-testid="term-attached-notice"] b').count() == 1)
    page.locator('[data-testid="term-attached-dismiss"]').click()


def _takeover_checks(page, mock, base, check, until, fake):
    tid = _fresh(page, mock, base, until, fake)
    fake.output(tid, b"marker-before-takeover\r\n$ ")
    until(lambda: "marker-before-takeover" in _text(page, tid), timeout=3)
    fake.ask_takeover(tid, "tk01")
    until(lambda: _visible(page, '[data-testid="term-takeover"]'), timeout=3)
    ask = page.locator('[data-testid="term-takeover"]')
    check("another window asking for this terminal is asked here: Let it / Keep it",
          "Another window wants" in ask.inner_text() and _visible(page, '[data-testid="term-let"]')
          and _visible(page, '[data-testid="term-keep"]'), ask.inner_text())
    page.locator('[data-testid="term-keep"]').click()
    until(lambda: fake.takeover_answers, timeout=3)
    check("Keep it answers no, and the terminal stays",
          fake.takeover_answers[-1] == {"type": "takeover", "id": "tk01", "allow": False}
          and _state(page, tid) == "attached" and not _visible(page, '[data-testid="term-takeover"]'),
          str(fake.takeover_answers[-1:]))
    _approval(mock, "trm2")
    fake.ask_takeover(tid, "tk02")
    until(lambda: _visible(page, '[data-testid="term-takeover"]') and _visible(page, '[data-testid="approval-card"]'),
          timeout=3)
    check("under a card the question cannot be answered (unanswered is kept)",
          page.locator('[data-testid="term-let"]').is_disabled() and page.locator('[data-testid="term-keep"]').is_disabled())
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    page.locator('[data-testid="term-let"]').click()
    until(lambda: _state(page, tid) == "taken", timeout=3)
    check("Let it answers yes, and the window says another window took it, offering it back",
          fake.takeover_answers[-1] == {"type": "takeover", "id": "tk02", "allow": True}
          and _visible(page, _sel(tid, '[data-testid="term-take-back"]')))
    sockets = len(fake.of(tid))
    fake.hold.add(tid)
    page.locator(_sel(tid, '[data-testid="term-take-back"]')).click()
    until(lambda: _state(page, tid) == "waiting", timeout=3)
    check("taking it back asks the other window, and says it is waiting",
          len(fake.of(tid)) == sockets + 1 and "Asking the window" in page.locator(
              _sel(tid, '[data-testid="term-waiting"]')).inner_text())
    fake.finish_waiting(tid, allow=False)
    until(lambda: _state(page, tid) == "refused", timeout=3)
    check("a refused take says why, and offers to ask again",
          "kept it" in page.locator(_sel(tid, '[data-testid="term-refused"]')).inner_text()
          and _visible(page, _sel(tid, '[data-testid="term-retry"]')))
    page.locator(_sel(tid, '[data-testid="term-retry"]')).click()
    until(lambda: _state(page, tid) == "attached", timeout=3)
    check("asking again attaches, with the ring replayed", _state(page, tid) == "attached"
          and "marker-before-takeover" in _text(page, tid))


def _exit_checks(page, mock, base, check, until, fake):
    tid = _fresh(page, mock, base, until, fake)
    spec = next(s for (i, s) in _created(mock) if i == tid)
    fake.exit(tid, 3)
    until(lambda: _state(page, tid) == "exited", timeout=3)
    strip = page.locator(_sel(tid, '[data-testid="term-exited"]'))
    check("a shell that exits reads 'exited (code N)' with Restart and Close",
          "exited (code 3)" in strip.inner_text() and _visible(page, _sel(tid, '[data-testid="term-restart"]'))
          and _visible(page, _sel(tid, '[data-testid="term-exit-close"]')), strip.inner_text())
    n = len(_created(mock))
    page.locator(_sel(tid, '[data-testid="term-restart"]')).click()
    until(lambda: len(_created(mock)) > n, timeout=3)
    new = _created(mock)[-1]
    _attached(page, until, new[0])
    check("Restart opens a new terminal in the same folder and the same place, the old one closed",
          new[1] == spec and mock.saw("DELETE", f"/terminals/{tid}") and _visible(page, _sel(new[0]))
          and page.locator(f'[data-testid="panel-tab-{tid}"]').count() == 0, str(new))
    tid = new[0]
    fake.exit(tid, 0)
    until(lambda: _state(page, tid) == "exited", timeout=3)
    page.locator(_sel(tid, '[data-testid="term-exit-close"]')).click()
    until(lambda: page.locator(f'[data-testid="panel-tab-{tid}"]').count() == 0, timeout=3)
    check("Close closes it", mock.saw("DELETE", f"/terminals/{tid}"))
    # The dot on ⬓: a terminal in the hidden panel has exited.
    tid = _new_terminal(page, mock, until, fake)
    page.locator('[data-testid="toggle-panel"]').click()
    until(lambda: not _visible(page, '[data-testid="panel"]'), timeout=2)
    check("no dot while nothing has exited", page.locator('[data-testid="toggle-panel-dot"]').count() == 0)
    fake.exit(tid, 1)
    until(lambda: page.locator('[data-testid="toggle-panel-dot"]').count() > 0, timeout=3)
    check("a terminal in the hidden panel exits: a dot on ⬓",
          page.locator('[data-testid="toggle-panel-dot"]').count() == 1)
    page.locator('[data-testid="toggle-panel"]').click()
    until(lambda: _visible(page, '[data-testid="panel"]'), timeout=2)
    check("opening the panel shows it, and the dot goes",
          page.locator('[data-testid="toggle-panel-dot"]').count() == 0 and _state(page, tid) == "exited")


def _busy_checks(page, mock, base, check, until, fake):
    tid = _fresh(page, mock, base, until, fake)
    fake._row(tid)["busy"] = True

    def deletes(t):
        return [p for (m, p, _) in mock.calls if m == "DELETE" and p == f"/terminals/{t}"]
    page.locator(f'[data-testid="panel-close-{tid}"]').click()
    until(lambda: _visible(page, '[data-testid="term-close-confirm"]'), timeout=3)
    check("closing a busy terminal asks first, and closes nothing yet",
          "Something is running" in page.locator('[data-testid="term-close-confirm"]').inner_text()
          and not deletes(tid))
    page.locator('[data-testid="term-close-no"]').click()
    until(lambda: not _visible(page, '[data-testid="term-close-confirm"]'), timeout=2)
    page.wait_for_timeout(150)
    check("Keep it keeps it", not deletes(tid) and _state(page, tid) == "attached")
    page.locator(_sel(tid, '[data-testid="term-close"]')).click()
    until(lambda: _visible(page, '[data-testid="term-close-confirm"]'), timeout=3)
    page.locator('[data-testid="term-close-yes"]').click()
    until(lambda: deletes(tid), timeout=3)
    until(lambda: page.locator(f'[data-testid="panel-tab-{tid}"]').count() == 0, timeout=3)
    check("Close it closes it (from the terminal's own × too)", len(deletes(tid)) == 1
          and page.locator(f'[data-testid="panel-tab-{tid}"]').count() == 0)
    tid = _new_terminal(page, mock, until, fake)
    page.locator(f'[data-testid="panel-close-{tid}"]').click()
    until(lambda: deletes(tid), timeout=3)
    page.wait_for_timeout(150)
    check("an idle terminal closes at once, unasked",
          not _visible(page, '[data-testid="term-close-confirm"]') and len(deletes(tid)) == 1)


def _readable_checks(page, mock, base, check, until, fake):
    tid = _fresh(page, mock, base, until, fake)
    sw = page.locator(_sel(tid, '[data-testid="term-readable"]'))
    check("each terminal has a 'Jarvis can read' switch, on by default",
          sw.get_attribute("aria-pressed") == "true" and "Jarvis can read" in sw.inner_text())
    sw.click()
    until(lambda: sw.get_attribute("aria-pressed") == "false", timeout=3)
    patches = mock.sent("PATCH", f"/terminals/{tid}")
    check("off is the owner's PATCH, exactly {readable: false}, and reads off",
          patches == [{"readable": False}] and fake._row(tid)["readable"] is False
          and "can't read" in sw.inner_text(), str(patches))
    sw.click()
    until(lambda: sw.get_attribute("aria-pressed") == "true", timeout=3)
    check("and back on", mock.sent("PATCH", f"/terminals/{tid}")[-1] == {"readable": True})


def _integration_checks(page, mock, base, check, until, fake):
    tid = _fresh(page, mock, base, until, fake)
    page.wait_for_timeout(300)
    check("a terminal whose startup file ran says nothing about it",
          page.locator(_sel(tid, '[data-testid="term-integration"]')).count() == 0)
    try:
        # A shell that reads no startup file (zsh, fish): said at once.
        mock.terminal_defaults = {"integration": "none", "marked": False, "integrated": False}
        t1 = _new_terminal(page, mock, until, fake)
        until(lambda: page.locator(_sel(t1, '[data-testid="term-integration"]')).count() > 0, timeout=3)
        badge = page.locator(_sel(t1, '[data-testid="term-integration"]'))
        check("a shell with no integration says so (no sudo -k, no marks)",
              badge.get_attribute("data-kind") == "none" and "sudo" in (badge.get_attribute("title") or ""))
        # Configured, but the startup file never ran (a profile exec'd another shell).
        mock.terminal_defaults = {"integration": "bash", "marked": False, "integrated": False}
        t2 = _new_terminal(page, mock, until, fake)
    finally:
        mock.terminal_defaults = {}
    page.wait_for_timeout(300)
    check("configured: nothing is said while the shell may still be starting",
          page.locator(_sel(t2, '[data-testid="term-integration"]')).count() == 0)
    until(lambda: page.locator(_sel(t2, '[data-testid="term-integration"]')).count() > 0, timeout=7)
    check("and once it has had time, a startup file that never ran is said",
          page.locator(_sel(t2, '[data-testid="term-integration"]')).get_attribute("data-kind") == "inactive")
    fake.control(t2, {"type": "marked"})
    until(lambda: page.locator(_sel(t2, '[data-testid="term-integration"]')).count() == 0, timeout=3)
    check("the daemon's `marked` clears it", page.locator(_sel(t2, '[data-testid="term-integration"]')).count() == 0)


def _osc_link_checks(page, mock, base, check, until, fake):
    tid = _fresh(page, mock, base, until, fake)
    fake.output(tid, b"\x1b]0;PWNED <img src=x onerror=window.__x=1>\x07")
    until(lambda: "PWNED" in page.locator(_sel(tid, '[data-testid="term-name"]')).inner_text(), timeout=3)
    fake.output(tid, b"\x1b]2;J.A.R.V.I.S. is gone\x1b\\")
    page.wait_for_timeout(200)
    check("an OSC title never changes the window's <title>", page.title() == "J.A.R.V.I.S.", page.title())
    name = page.locator(_sel(tid, '[data-testid="term-name"]'))
    check("it is drawn as text in the terminal's header, never as markup",
          "is gone" in name.inner_text() and page.locator(_sel(tid, ".termbar img")).count() == 0
          and page.evaluate("window.__x") is None, name.inner_text())
    fake.output(tid, b"\x1b[2J\x1b[H"
                     b"\x1b]8;;javascript:window.__pwned=1\x07OSCJS\x1b]8;;\x07 "
                     b"\x1b]8;;data:text/html,<script>window.__pwned=2</script>\x07OSCDATA\x1b]8;;\x07 "
                     b"javascript:window.__pwned=3\r\n"
                     b"http://example.com/ok-link \x1b]8;;https://example.org/osc8\x07OSCHTTP\x1b]8;;\x07\r\n")
    until(lambda: "OSCHTTP" in _text(page, tid), timeout=3)
    page.wait_for_timeout(200)

    def click(col, row, ctrl):
        x, y, _, _ = _cell(page, tid, col, row)
        page.mouse.move(x, y)
        page.wait_for_timeout(120)
        if ctrl:
            page.keyboard.down("Control")
        page.mouse.click(x, y)
        if ctrl:
            page.keyboard.up("Control")
        page.wait_for_timeout(150)
    click(2, 0, True)                                   # OSCJS
    click(9, 0, True)                                   # OSCDATA
    click(20, 0, True)                                  # plain javascript: text
    check("a javascript: or data: link, OSC 8 or plain, is inert",
          page.evaluate("window.__opened") == [] and page.evaluate("window.__pwned") is None,
          str(page.evaluate("window.__opened")))
    click(5, 1, False)
    check("an http link does not open on a plain click (clicks select)", page.evaluate("window.__opened") == [])
    click(5, 1, True)
    click(28, 1, True)
    opened = page.evaluate("window.__opened")
    check("Ctrl+click opens an http link, and an OSC 8 https one, in a new window",
          opened == ["http://example.com/ok-link", "https://example.org/osc8"], str(opened))


def _layout(page, until, preset):
    page.locator('[data-testid="layout-customize"]').click()
    page.locator(f'[data-testid="layout-preset-{preset}"]').click()
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="layout-menu"]').count() == 0, timeout=2)


def _pane_checks(page, mock, base, check, until, fake):
    tid = _fresh(page, mock, base, until, fake, size=(1600, 900))
    fake.output(tid, b"marker-in-panel\r\n$ ")
    until(lambda: "marker-in-panel" in _text(page, tid), timeout=3)
    sockets = len(fake.of(tid))
    _layout(page, until, "cols2")
    page.locator('[data-testid="pane-2"] [data-testid="tab-terminal"]').click()
    until(lambda: _visible(page, '[data-testid="pane-2-terminal-pick"]'), timeout=3)
    check("a pane can show the terminal view, which offers the open terminals and a new one",
          _visible(page, f'[data-testid="pane-2-pick-{tid}"]') and _visible(page, '[data-testid="pane-2-terminal-new"]'))
    page.locator(f'[data-testid="pane-2-pick-{tid}"]').click()
    until(lambda: page.locator(_sel(tid)).count() and page.locator(_sel(tid)).get_attribute("data-where") == "pane-2",
          timeout=3)
    check("choosing one draws it in the pane", _visible(page, f'[data-testid="pane-2"] {_sel(tid)}'))
    check("and in one place only: one xterm, its panel tab saying 'in pane 2'",
          page.locator(f'{_sel(tid)} .xterm').count() == 1
          and page.locator(f'[data-testid="panel-tab-{tid}"]').get_attribute("data-place") == "pane-2"
          and "in pane 2" in page.locator(f'[data-testid="panel-tab-{tid}"]').inner_text()
          and _visible(page, '[data-testid="panel-elsewhere"]'))
    check("moving it kept its socket and its output (no reattach)",
          len(fake.of(tid)) == sockets and "marker-in-panel" in _text(page, tid),
          f"{len(fake.of(tid))} sockets, was {sockets}")
    page.locator('[data-testid="pane-1"] [data-testid="input"]').click()
    page.locator(f'[data-testid="panel-tab-{tid}"]').click()
    until(lambda: page.locator('[data-testid="pane-2"]').get_attribute("data-focused") == "true", timeout=2)
    check("its tab jumps to the pane", page.locator('[data-testid="pane-2"]').get_attribute("data-focused") == "true")
    fake.output(tid, b"\x1b]2;vim notes.md\x07")
    until(lambda: "vim notes.md" in page.locator('[data-testid="pane-2"] .panectx').inner_text(), timeout=3)
    check("the pane's header carries the title the program set, as text",
          "vim notes.md" in page.locator('[data-testid="pane-2"] .panectx').inner_text())
    _focus_term(page, tid)
    page.keyboard.type("echo in-a-pane")
    page.keyboard.press("Enter")
    until(lambda: "\nin-a-pane" in _text(page, tid), timeout=3)
    check("and typing there reaches the same shell", b"echo in-a-pane\r" in fake.sent(tid))
    n = len(_created(mock))
    page.locator('[data-testid="pane-2"] [data-testid="tab-chat"]').click()
    page.locator('[data-testid="pane-2"] [data-testid="tab-terminal"]').click()
    until(lambda: _visible(page, f'[data-testid="pane-2"] {_sel(tid)}'), timeout=3)
    check("the pane keeps its terminal across a view switch", len(_created(mock)) == n)
    page.locator(_sel(tid, '[data-testid="term-close"]')).click()
    until(lambda: _visible(page, '[data-testid="pane-2-terminal-pick"]'), timeout=3)
    page.locator('[data-testid="pane-2-terminal-new"]').click()
    until(lambda: len(_created(mock)) > n, timeout=3)
    new = _created(mock)[-1][0]
    _attached(page, until, new)
    check("closing it lets the pane choose again; 'New terminal here' opens one in the pane",
          page.locator(_sel(new)).get_attribute("data-where") == "pane-2")
    _layout(page, until, "single")
    page.wait_for_timeout(200)
    check("a pane the layout stops drawing gives its terminal back to the panel",
          page.locator(f'[data-testid="panel-tab-{new}"]').get_attribute("data-place") == "panel")


def _zoom_checks(page, mock, base, check, until, fake):
    cells = {}
    tid = _fresh(page, mock, base, until, fake)
    for zoom in (160, 70):
        _store(page, zoom=str(zoom), layout="{}")
        _boot(page, mock, base, until, reload=True)
        until(lambda: _state(page, tid) == "attached", timeout=6)
        page.wait_for_timeout(300)
        fake.output(tid, f"\x1b[2J\x1b[3;5HSELECTME-{zoom} tail\x1b[6;1H".encode())
        until(lambda: f"SELECTME-{zoom}" in _text(page, tid), timeout=3)
        page.wait_for_timeout(150)
        size = page.evaluate("id => window.__hudTerminals.size(id)", tid)
        geo = page.evaluate("""(id) => {
          const slot = document.querySelector(`[data-testid="terminal-${id}"] .termslot`);
          const scr = document.querySelector(`[data-testid="terminal-${id}"] .xterm-screen`);
          const s = slot.getBoundingClientRect(), r = scr.getBoundingClientRect();
          return { sw: s.width, sh: s.height, rw: r.width, rh: r.height, rl: r.left - s.left, rt: r.top - s.top };
        }""", tid)
        cw, ch = geo["rw"] / size["cols"], geo["rh"] / size["rows"]
        cells[zoom] = ch
        rz = fake.resizes(tid)
        check(f"at {zoom}% the terminal fills its slot to within a cell, and inside it",
              geo["rl"] >= 0 and geo["rt"] >= 0 and geo["rl"] + geo["rw"] <= geo["sw"] + 1
              and geo["rt"] + geo["rh"] <= geo["sh"] + 1
              and geo["sw"] - geo["rw"] - geo["rl"] < cw + 24 and geo["sh"] - geo["rh"] - geo["rt"] < ch + 2,
              f"{geo} cell {cw:.1f}x{ch:.1f}")
        check(f"at {zoom}% the daemon is told the fitted size",
              rz and rz[-1][0] == {"type": "resize", "cols": size["cols"], "rows": size["rows"]}, str(rz[-1:]))
        check(f"at {zoom}% its text scales with the HUD (a cell is ~13px text in the HUD's own px)",
              12 <= ch / (zoom / 100) <= 21, f"{ch:.1f}px on screen at {zoom}%")
        x0, y0, _, _ = _cell(page, tid, 4, 2, 0.3)
        x1, y1, _, _ = _cell(page, tid, 4 + len(f"SELECTME-{zoom}") - 1, 2, 0.8)
        page.mouse.move(x0, y0)
        page.mouse.down()
        page.mouse.move(x1, y1, steps=6)
        page.mouse.up()
        page.wait_for_timeout(100)
        sel = page.evaluate("id => window.__hudTerminals.selection(id)", tid)
        check(f"at {zoom}% a drag selects exactly the cells under the pointer", sel == f"SELECTME-{zoom}", repr(sel))
        x, y, _, _ = _cell(page, tid, 6, 2)
        page.mouse.dblclick(x, y)
        page.wait_for_timeout(100)
        sel = page.evaluate("id => window.__hudTerminals.selection(id)", tid)
        check(f"at {zoom}% a double-click selects the word under it", sel == f"SELECTME-{zoom}", repr(sel))
    if 160 in cells and 70 in cells:
        check("the terminal's text is bigger at 160% than at 70%, as the HUD's is",
              1.8 <= cells[160] / cells[70] <= 2.8, f"{cells}")
    _store(page, zoom="100")
    _boot(page, mock, base, until, reload=True)


def _ended_checks(page, mock, base, check, until, fake):
    tid = _fresh(page, mock, base, until, fake)
    spec = next((s for (i, s) in _created(mock) if i == tid), None)
    fake.end(tid)
    until(lambda: _state(page, tid) == "ended", timeout=4)
    check("Jarvis stopping ends the terminal: the window says so and offers a new one there",
          "Jarvis restarted" in page.locator(_sel(tid, '[data-testid="term-ended"]')).inner_text()
          and _visible(page, _sel(tid, '[data-testid="term-new-here"]')))
    n = len(_created(mock))
    page.locator(_sel(tid, '[data-testid="term-new-here"]')).click()
    until(lambda: len(_created(mock)) > n, timeout=3)
    new = _created(mock)[-1]
    _attached(page, until, new[0])
    check("'New terminal here' opens one in the same folder", new[1] == spec, f"{new} vs {spec}")
    # A pane remembering a terminal from before a restart: it ended, nothing hangs.
    stale = json.dumps({"preset": "cols2", "focused": 2,
                        "panes": [{"view": "chat"}, {"view": "terminal", "terminalId": "deadbeef"},
                                  {"view": "file"}, {"view": "task"}]})
    page.set_viewport_size({"width": 1600, "height": 900})
    _store(page, ws=stale)
    _boot(page, mock, base, until, reload=True)
    until(lambda: _state(page, "deadbeef") == "ended", timeout=5)
    check("a pane remembering a terminal from before a restart says it ended, offering a new one or another",
          _visible(page, _sel("deadbeef", '[data-testid="term-new-here"]'))
          and _visible(page, _sel("deadbeef", '[data-testid="term-choose"]')))
    page.locator(_sel("deadbeef", '[data-testid="term-choose"]')).click()
    until(lambda: _visible(page, '[data-testid="pane-2-terminal-pick"]'), timeout=3)
    check("and 'Choose another' lets it choose", _visible(page, '[data-testid="pane-2-terminal-pick"]'))
    page.set_viewport_size({"width": 1280, "height": 800})
    _store(page, ws=None)


def _guard_selftest(browser, mock, base, check, until, init_script):
    """The live-port guard bites on sockets too: a WebSocket to a guarded
    port never leaves the browser. Proved against a port of the test's own
    (a listener that counts connections) — never the owner's live ones."""
    from tests.face.hud_v2_check import guard_live
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(0.2)
    dummy = listener.getsockname()[1]
    hits: list[int] = []
    stop = threading.Event()

    def accept():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            hits.append(1)
            conn.close()
    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    refused: list[str] = []
    ctx = browser.new_context()
    try:
        guard_live(ctx, refused.append, ports=(8402, 8403, 8405, dummy))
        page = ctx.new_page()
        page.goto(base + "/")
        page.wait_for_selector('[data-testid="sidebar"]', state="attached")
        page.evaluate("""(port) => { window.__guarded = new WebSocket(
          `ws://127.0.0.1:${port}/terminals/0a1b2c3d/attach?ticket=x`); }""", dummy)
        for _ in range(40):                               # let the route handler run
            page.wait_for_timeout(50)
            if refused:
                break
        page.wait_for_timeout(500)                        # time for a real connect to land
        check("the live-port guard catches a WebSocket to a guarded port before it leaves the browser",
              len(refused) == 1 and "socket" in refused[0] and not hits, f"refused={refused} hits={len(hits)}")
    finally:
        ctx.close()
        stop.set()
        listener.close()


# ---------------------------------------------------------------------------
# standalone


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
            terminal_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until,
                            lambda ctx: v2.guard_live(ctx, v2.FAILURES.append), v2.FAKE_RECOGNIZER)
            browser.close()
    finally:
        mock.stop()
    print()
    if v2.FAILURES:
        print(f"{len(v2.FAILURES)} of {v2.CHECKS} checks FAILED:")
        for f in v2.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {v2.CHECKS} terminal checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
