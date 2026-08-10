"""Checks the avatar in the real `jarvis.html`, headless and free.

hud_state_check's puppet pattern: the real page is served with `/config`,
`/avatars`, `/avatar` and `/avatar.svg` scripted, so every transition is
driven on cue with no API calls and no timing luck.

What it pins down:
  1. **No avatar in /config leaves the window exactly as it was** — banner
     "J A R V I S", the two built-in wake phrases, the triangle emblem. Every
     other HUD suite depends on that, and so does a fresh clone with no
     `avatars/` directory.
  2. **An avatar renames the window**: banner, the COMMS log byline, the
     SYSTEMS row, and the armed hint (which has to name the *new* phrases —
     "SAY JARVIS" after a rename is a lie).
  3. **The wake word moves with it.** The new phrase fires and the old one
     does not, which is the half that decides whether the owner can summon
     him at all.
  4. **The face replaces the emblem** rather than sitting on top of it, and a
     face that fails to load falls back to the triangle instead of a hole.
  5. **The picker behaves like the session picker**: lists, marks the current
     one, PTT and the wake word are inert while it is open, Escape closes
     without switching, and a click round-trips through POST /avatar.
  6. **An SSE `avatar` broadcast** (another window, `jarvis avatar`, or the
     set_avatar tool) relabels this one live.
  7. **A hostile avatar cannot script the window that gates approvals** —
     the art is an <img>, so its onload never runs.

Run:  .venv/bin/python tests/face/hud_avatar_check.py
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from playwright.sync_api import sync_playwright  # noqa: E402

from jarvis import avatars  # noqa: E402
from jarvis.face.server import STATIC_DIR  # noqa: E402

PORT = 8481
BASE = f"http://127.0.0.1:{PORT}"
SHUTDOWN = threading.Event()
FAILURES: list[str] = []

# One queue per live /events connection, like the real server. A single shared
# queue looks fine until the page reloads: the abandoned handler is still
# blocked on get(), so it eats the broadcast meant for the live window.
_subs: list[queue.Queue] = []
_subs_lock = threading.Lock()


def push(kind: str, data: dict) -> None:
    with _subs_lock:
        for q in list(_subs):
            q.put({"kind": kind, "data": data})

VEX = {
    "slug": "vex", "name": "Vex", "banner": "V E X", "label": "V.E.X.",
    "wake": ["VEX", "HEY VEX"],
    "wake_regex": [r"\bvex\b", r"\bhey\s*vex\b"],
    "accent": "245,158,11", "description": "A fox.", "has_svg": True,
}
JARVIS = avatars.DEFAULT.describe()

# The art the window is asked to draw. If an <img> could run this, the window
# that gates approvals would be scriptable by a file `write_file` can reach.
HOSTILE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" '
    'onload="window.__pwned = true">'
    '<circle cx="50" cy="50" r="40" fill="none" stroke="#f59e0b" stroke-width="4"/>'
    "</svg>"
)

# What the page last POSTed to /avatar, so the round trip is observable.
SWITCHED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("ok  " if ok else "FAIL") + "  " + label + (f"  — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


class Handler(SimpleHTTPRequestHandler):
    """The real page; every route it touches at boot is scripted."""

    config_payload = {"llm": "t/m", "stt": "t/s", "tts": "t/t", "voice": "v", "speed": 1.0}

    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            mine: queue.Queue = queue.Queue()
            with _subs_lock:
                _subs.append(mine)
            try:
                while not SHUTDOWN.is_set():
                    try:
                        item = mine.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    self.wfile.write(f"data: {json.dumps(item)}\n\n".encode())
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                with _subs_lock:
                    if mine in _subs:
                        _subs.remove(mine)
            return
        if path == "/config":
            return self._json(dict(type(self).config_payload))
        if path == "/avatars":
            return self._json({"current": "jarvis", "avatars": [JARVIS, VEX]})
        if path == "/avatar.svg":
            body = HOSTILE_SVG.encode()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    def do_POST(self):
        if self.path == "/avatar":
            length = int(self.headers.get("Content-Length", "0"))
            slug = json.loads(self.rfile.read(length) or b"{}").get("slug", "")
            SWITCHED.append(slug)
            return self._json(VEX if slug == "vex" else JARVIS)
        return self._json({"ok": True})


def boot(page, avatar=None):
    Handler.config_payload = {"llm": "t/m", "stt": "t/s", "tts": "t/t", "voice": "v", "speed": 1.0}
    if avatar is not None:
        Handler.config_payload["avatar"] = avatar
    page.goto(f"{BASE}/jarvis.html", wait_until="load")
    page.wait_for_function("() => window.__hud && window.__hud.avatar")


def run_checks(page) -> None:
    hud = lambda expr, *a: page.evaluate(expr, *a)  # noqa: E731

    # ---- 1. no avatar configured: nothing changes -------------------------
    print("\n--- default (no avatar in /config) ---")
    boot(page)
    check("banner is untouched", page.text_content("#banner-title") == "J A R V I S")
    check("the built-in wake phrase still fires",
          hud("() => window.__hud.matchesWake('hey jarvis')"))
    check("ordinary speech still does not",
          hud("() => !window.__hud.matchesWake('we rented a big yacht')"))
    # "big yahoo" moved to the bibi avatar; the default must not still answer
    # to a phrase that belongs to someone else.
    check("a phrase that moved to an avatar does not fire on the default",
          hud("() => !window.__hud.matchesWake('big ya hoo')"))
    check("the triangle emblem is still what draws", hud("() => !window.__hud.avatarShown()"))
    check("the COMMS byline is unchanged",
          hud("() => { window.__hud.addMsg('jarvis', 'hi'); "
              "return document.querySelector('.msg.jarvis .who').textContent; }") == "J.A.R.V.I.S.")

    # ---- 2 & 3. an avatar renames the window and moves the wake word ------
    print("\n--- an avatar is active ---")
    boot(page, VEX)
    check("banner takes the avatar's name", page.text_content("#banner-title") == "V E X")
    check("the SYSTEMS row names it", page.text_content("#avatar-name") == "VEX")
    check("the COMMS byline takes the avatar's label",
          hud("() => { window.__hud.addMsg('jarvis', 'hi'); "
              "return document.querySelector('.msg.jarvis .who').textContent; }") == "V.E.X.")
    page.click("#wake-row")
    hint = page.text_content("#hint")
    check("the armed hint names the new phrases, not the old",
          "VEX" in hint and "JARVIS" not in hint, hint)
    check("the new wake phrase fires",
          hud("() => window.__hud.matchesWake('hey vex, what time is it')"))
    check("the old one no longer does", hud("() => !window.__hud.matchesWake('jarvis')"))
    check("the new phrase is still anchored",
          hud("() => !window.__hud.matchesWake('a vexing problem')"))
    page.click("#wake-row")  # disarm

    # ---- 4. the face replaces the emblem ---------------------------------
    page.wait_for_function("() => window.__hud.avatarShown()")
    check("the face is drawn", hud("() => window.__hud.avatarShown()"))
    check("it is an <img>, so nothing in the file can run",
          hud("() => document.getElementById('avatar').tagName") == "IMG")
    check("the art is displayed",
          hud("() => getComputedStyle(document.getElementById('avatar')).display") == "block")
    check("it cannot intercept a click meant for the orb",
          hud("() => getComputedStyle(document.getElementById('avatar')).pointerEvents") == "none")

    # ---- 7. the hostile SVG's onload never ran ---------------------------
    check("the avatar SVG cannot script the approval window",
          hud("() => window.__pwned === undefined"))

    # a face that 404s falls back to the triangle rather than a blank hole
    hud("() => { const i = document.getElementById('avatar'); "
        "i.src = '/nope-there-is-no-such-file.svg'; }")
    page.wait_for_function("() => !window.__hud.avatarShown()", timeout=4000)
    check("a face that fails to load falls back to the emblem",
          hud("() => !window.__hud.avatarShown()"))

    # ---- 5. the picker ---------------------------------------------------
    print("\n--- the picker ---")
    boot(page, JARVIS)
    page.click("#avatar-row")
    page.wait_for_selector("#avatars.on")
    rows = page.locator("#avatarlist .sess")
    check("it lists every avatar", rows.count() == 2, str(rows.count()))
    check("it marks the current one",
          page.locator("#avatarlist .sess.here").text_content().startswith("Jarvis"))
    check("it shows each avatar's wake phrases",
          "hey vex" in page.locator("#avatarlist .sess").nth(1).text_content())
    check("PTT is inert while it is open",
          hud("() => { const before = window.__hud.state(); "
              "document.getElementById('orb').dispatchEvent("
              "new PointerEvent('pointerdown', {bubbles: true})); "
              "return window.__hud.state() === before; }"))
    page.keyboard.press("Escape")
    check("Escape closes it", not page.locator("#avatars").evaluate("e => e.classList.contains('on')"))
    check("Escape switched nothing", SWITCHED == [], str(SWITCHED))

    page.click("#avatar-row")
    page.wait_for_selector("#avatars.on")
    page.locator("#avatarlist .sess").nth(1).click()
    page.wait_for_function("() => window.__hud.avatar().slug === 'vex'")
    check("clicking one round-trips through POST /avatar", SWITCHED == ["vex"], str(SWITCHED))
    check("and the window renames itself", page.text_content("#banner-title") == "V E X")
    check("the picker closes on the switch",
          not page.locator("#avatars").evaluate("e => e.classList.contains('on')"))

    # ---- 6. an SSE broadcast relabels this window ------------------------
    print("\n--- SSE broadcast ---")
    push("avatar", JARVIS)
    page.wait_for_function("() => window.__hud.avatar().slug === 'jarvis'", timeout=5000)
    check("an avatar broadcast relabels the window",
          page.text_content("#banner-title") == "J A R V I S")
    check("and hands the wake word back",
          hud("() => window.__hud.matchesWake('jarvis') && !window.__hud.matchesWake('vex')"))

    # ---- the orb's outer ring follows the avatar -------------------------
    print("\n--- rings ---")
    default_rings = hud("() => window.__hud.rings()")
    check("the default outer ring is the tick ring",
          default_rings[0] == "ticks144", str(default_rings))

    hud("() => window.__hud.applyAvatar({slug: 't', name: 'T', wake: ['T'], "
        "wake_regex: ['\\\\bt\\\\b'], rings: 'triangles'})")
    rings = hud("() => window.__hud.rings()")
    check("an avatar can ask for the two rotating triangles",
          rings[0] == "poly3x2", str(rings))
    check("and only the outer ring changes",
          rings[1:] == default_rings[1:], str(rings))

    # The orb is the push-to-talk button. An avatar that could blank it would
    # take the surface's main control with it.
    hud("() => window.__hud.applyAvatar({slug: 'u', name: 'U', wake: ['U'], "
        "wake_regex: ['\\\\bu\\\\b'], rings: 'no-such-ring-set'})")
    check("an unknown ring set falls back to the default rather than nothing",
          hud("() => window.__hud.rings()") == default_rings)

    # A broken payload must not be able to leave the window mute.
    hud("() => window.__hud.applyAvatar({slug: 'x', name: 'X', wake_regex: ['(('], wake: ['X']})")
    check("an avatar whose regexes will not compile falls back to the built-ins",
          hud("() => window.__hud.matchesWake('jarvis')"))


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), partial(Handler, directory=str(STATIC_DIR)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"],
            )
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            run_checks(page)
            check("no page errors", not errors, str(errors))
            browser.close()
    finally:
        SHUTDOWN.set()
        server.shutdown()
        server.server_close()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print("all HUD avatar checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
