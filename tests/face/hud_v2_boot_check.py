"""Boot animation checks ("Constellation + Orb", D-12): built dist, ephemeral MockDaemon, live-port/CSP guard.

The only opt-in is an init-script boolean; it bypasses automation alone.
Run with JARVIS_HUD_DEV=1, as the other built HUD suites do.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

OVERLAY = '[data-testid="boot-overlay"]'
CARD = {"req_id": "boot-card", "code": "BOOT", "tool": "run_command",
        "args": {"command": "echo boot"}, "command": "echo boot", "reason": "test",
        "layer": "human", "provider": "codex", "timeout_s": 120, "allowlistable": False}

# Remember even a transient mount, rather than merely sampling the final DOM.
OBSERVER = """
window.__bootMounts = 0;
window.__bootBeforeHud = null;
new MutationObserver(records => {
  for (const record of records) for (const node of record.addedNodes) {
    if (node.nodeType === 1 && (node.matches('#hud-boot') || node.querySelector('#hud-boot')))
      window.__bootMounts++;
  }
  // When the chat box first appears, was the overlay already over it?
  if (window.__bootBeforeHud === null && document.querySelector('[data-testid="input"]'))
    window.__bootBeforeHud = !!document.querySelector('#hud-boot');
}).observe(document, {childList: true, subtree: true});
"""


# Remember every WebGL context the overlay makes, to see that it is released.
TRACK_GL = """
window.__gls = [];
const ownGet = HTMLCanvasElement.prototype.getContext;
HTMLCanvasElement.prototype.getContext = function (type, ...rest) {
  const c = ownGet.call(this, type, ...rest);
  if (c && /webgl/.test(String(type)) && this.closest('#hud-boot')) window.__gls.push(c);
  return c;
};"""

GONE = "document.querySelectorAll('#hud-boot, #hud-boot canvas').length === 0"
RELEASED = "window.__gls.length >= 1 && window.__gls.every(g => g.isContextLost())"


def boot_checks(browser, mock, base, check, until, guard, init_script):
    print("\nboot animation: automation, skips, approvals, session and zoom")
    errors = []
    keep_approvals = mock.world["approvals"]

    def fresh(forced=True, reduced=False, zoom=100, size=(1280, 800), storage=True, extra=""):
        ctx = browser.new_context(viewport={"width": size[0], "height": size[1]},
                                  permissions=["microphone"], device_scale_factor=2 if zoom == 160 else 1,
                                  reduced_motion="reduce" if reduced else "no-preference")
        guard(ctx)
        ctx.add_init_script(init_script)
        ctx.add_init_script(OBSERVER)
        ctx.add_init_script(f"""
          window.__hudBootForce = {str(forced).lower()};
          localStorage.setItem('jarvis.hud.zoom', '{zoom}');
          localStorage.setItem('jarvis.dictation', 'review');
        """)
        if extra:
            ctx.add_init_script(extra)
        if not storage:
            ctx.add_init_script("Object.defineProperty(window, 'sessionStorage', {get() { throw new Error('unavailable'); }});")
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.set_default_timeout(5000)
        before = mock.sse_connections()
        page.goto(base + "/")
        page.wait_for_selector('[data-testid="project-p1"]', state="attached")
        mock.await_reconnect(before)
        page.wait_for_function("!!window.__hud")
        # Initial approvals have been fetched.
        until(lambda: mock.saw("GET", "/approvals"), timeout=2)
        return ctx, page

    def run(name, fn):
        mock.world["approvals"] = []
        try:
            fn()
        except Exception as e:
            check(f"boot {name} ran to the end", False, str(e).splitlines()[0][:200])
        finally:
            mock.world["approvals"] = []

    def no_play(forced=False, reduced=False):
        ctx, page = fresh(forced=forced, reduced=reduced)
        try:
            page.wait_for_timeout(800)
            check("reduced motion never mounts" if reduced else "plain automation never mounts",
                  page.locator(OVERLAY).count() == 0 and page.evaluate("window.__bootMounts") == 0)
        finally:
            ctx.close()

    def finishes():
        ctx, page = fresh(extra=TRACK_GL)
        try:
            start = time.monotonic()
            page.wait_for_selector(OVERLAY, state="attached")
            check("forced boot plays", page.locator(OVERLAY).count() == 1)
            check("the HUD is never drawn before the overlay", page.evaluate("window.__bootBeforeHud") is True)
            check("HUD connects and renders underneath", page.locator('[data-testid="input"]').count() == 1
                  and mock.sse_connections() > 0)
            check("overlay never holds focus", not page.evaluate("document.querySelector('#hud-boot').contains(document.activeElement)"))
            check("title unchanged during boot", page.title() == "J.A.R.V.I.S.")
            page.wait_for_selector(OVERLAY, state="detached", timeout=7000)
            took = time.monotonic() - start
            check("finishes and removes itself within 7 seconds", took < 7, f"{took:.2f}s")
            check("plays its whole story (about 4 seconds), not a flash", took > 3.0, f"{took:.2f}s")
            check("no overlay or canvas remains", page.evaluate(GONE))
            check("the GL context is released", page.evaluate(RELEASED))
            check("the real HUD is intact and focus never went to the overlay",
                  page.locator('[data-testid="input"]').count() == 1
                  and not page.evaluate("document.activeElement?.closest('#hud-boot')"))
            check("title unchanged after boot", page.title() == "J.A.R.V.I.S.")
        finally:
            ctx.close()

    def click_skip():
        ctx, page = fresh(extra=TRACK_GL)
        try:
            page.wait_for_selector(OVERLAY, state="attached")
            page.evaluate("""() => {
              window.__underClicks = 0; window.__underPresses = 0;
              const button = document.createElement('button'); button.id = 'under-boot';
              button.textContent = 'underlay'; button.style.cssText =
                'position:fixed;left:0;top:0;width:100px;height:100px;z-index:98';
              button.onclick = () => window.__underClicks++;
              button.onpointerdown = () => window.__underPresses++;
              document.querySelector('#root').appendChild(button);
            }""")
            page.mouse.move(40, 40)
            page.mouse.down()
            page.wait_for_timeout(350)  # keep shield through a hold past the skip fade
            page.mouse.up()
            check("skip press and click never reach the element underneath",
                  page.evaluate("[window.__underClicks, window.__underPresses]") == [0, 0])
            page.wait_for_selector(OVERLAY, state="detached", timeout=1000)
            check("click skips and eats the press and click underneath",
                  page.evaluate("[window.__underClicks, window.__underPresses]") == [0, 0])
            check("after a skip no overlay or canvas remains", page.evaluate(GONE))
            check("after a skip the GL context is released", page.evaluate(RELEASED))
            page.mouse.click(40, 40)
            check("underlay probe can actually receive clicks after skip", page.evaluate("window.__underClicks") == 1)
            before = mock.sse_connections()
            page.reload()
            mock.await_reconnect(before)
            page.wait_for_timeout(800)
            check("reload never replays, even when forced", page.locator(OVERLAY).count() == 0
                  and page.evaluate("window.__bootMounts") == 0)
        finally:
            ctx.close()

    def key_skip(key):
        ctx, page = fresh()
        try:
            page.wait_for_selector(OVERLAY, state="attached")
            page.locator('[data-testid="input"]').focus()
            page.keyboard.press(key)
            page.wait_for_selector(OVERLAY, state="detached", timeout=1000)
            check(f"{key} skips boot", page.locator(OVERLAY).count() == 0)
            if key == "x":
                check("typing still reaches focused input", page.locator('[data-testid="input"]').input_value() == "x")
            if key == "Control+`":
                check("Ctrl+` still toggles the bottom panel", page.locator('[data-testid="panel"]').get_attribute('data-open') == 'true'
                      and page.locator('[data-testid="panel"]').is_visible())
        finally:
            ctx.close()

    def ptt():
        ctx, page = fresh()
        try:
            page.wait_for_selector(OVERLAY, state="attached")
            page.locator('[data-testid="input"]').focus()
            page.keyboard.down("Control")
            # Ctrl itself skips, but D must still reach the HUD's capture handler.
            page.keyboard.down("d")
            held = until(lambda: page.evaluate("window.__hud.capture.ptt"), timeout=1)
            check("Ctrl+D reaches push-to-talk while boot is up", held is not None)
            page.keyboard.up("d")
            page.keyboard.up("Control")
            page.wait_for_selector(OVERLAY, state="detached", timeout=1000)
            check("Ctrl+D also skips boot", page.locator(OVERLAY).count() == 0)
        finally:
            ctx.close()

    def approval(before):
        if before:
            mock.world["approvals"] = [dict(CARD)]
        ctx, page = fresh(extra=TRACK_GL)
        try:
            if not before:
                page.wait_for_selector(OVERLAY, state="attached")
                page.wait_for_timeout(600)  # well into the animation
                page.locator('[data-testid="input"]').focus()
                mock.emit("approval_requested", dict(CARD))
            page.wait_for_selector('[data-testid="approval-card"]', state="attached")
            # A card waiting at open: the overlay mounts on the first frame (the
            # HUD does not know of the card yet) and is gone in the same render
            # the card is drawn in — never above it, since it sits below the veil.
            check("card waiting at open removes boot as the card is drawn" if before
                  else "arriving card removes boot immediately",
                  page.locator(OVERLAY).count() == 0)
            check("approval card takes focus", page.evaluate("document.activeElement?.getAttribute('data-testid')") == "approval-card")
            if not before:
                check("a card arriving mid-boot leaves no canvas and releases the GL context",
                      page.evaluate(GONE) and page.evaluate(RELEASED))
            page.keyboard.press("Escape")
            page.wait_for_selector('[data-testid="approval-card"]', state="detached")
            page.wait_for_timeout(250)
            check("answering card never starts or resumes boot", page.locator(OVERLAY).count() == 0)
            if before:
                page.reload()
                page.wait_for_timeout(800)
                check("suppressed initial boot also stays suppressed on reload", page.evaluate("window.__bootMounts") == 0)
        finally:
            ctx.close()

    UNDERLAY = """() => {
      window.__under = {click: 0, dblclick: 0, down: 0, up: 0};
      const button = document.createElement('button'); button.id = 'under-boot';
      button.textContent = 'underlay'; button.style.cssText =
        'position:fixed;left:0;top:0;width:100px;height:100px;z-index:98';
      for (const [kind, key] of [['click', 'click'], ['dblclick', 'dblclick'],
                                 ['pointerdown', 'down'], ['pointerup', 'up']])
        button.addEventListener(kind, () => window.__under[key]++);
      document.querySelector('#root').appendChild(button);
    }"""

    def canvas_fails(kind, label):
        # Review of PR #82, F1: a throw in the overlay must never take the HUD
        # (and its approval card) with it.
        broken = f"""
          const own = HTMLCanvasElement.prototype.getContext;
          HTMLCanvasElement.prototype.getContext = function (type, ...args) {{
            return this.closest('#hud-boot') && {kind} ? null : own.call(this, type, ...args);
          }};"""
        ctx, page = fresh(extra=broken)
        try:
            page.wait_for_timeout(600)
            check(f"{label} ends boot, never the HUD",
                  page.locator(OVERLAY).count() == 0 and page.locator('[data-testid="input"]').count() == 1
                  and page.evaluate("window.__bootMounts") >= 1)
            mock.emit("approval_requested", dict(CARD))
            page.wait_for_selector('[data-testid="approval-card"]', state="attached")
            check("…and an approval card still draws and takes focus",
                  page.evaluate("document.activeElement?.getAttribute('data-testid')") == "approval-card")
            page.keyboard.press("Escape")
            page.wait_for_selector('[data-testid="approval-card"]', state="detached")
        finally:
            ctx.close()

    def shader_fails():
        # A shader that will not compile is the same silent end.
        broken = """
          const own = WebGLRenderingContext.prototype.getShaderParameter;
          WebGLRenderingContext.prototype.getShaderParameter = function (s, p) {
            return p === this.COMPILE_STATUS ? false : own.call(this, s, p);
          };"""
        ctx, page = fresh(extra=broken)
        try:
            page.wait_for_timeout(600)
            check("a shader that does not compile ends boot, never the HUD",
                  page.locator(OVERLAY).count() == 0 and page.locator('[data-testid="input"]').count() == 1
                  and page.evaluate("window.__bootMounts") >= 1)
        finally:
            ctx.close()

    def dock(zoom, size):
        # The boot orb must end exactly on the real orb: centre and radius.
        ctx, page = fresh(zoom=zoom, size=size)
        try:
            handle = page.wait_for_function("""() => {
              const el = document.querySelector('#hud-boot');
              if (!el || el.dataset.docked !== '1') return null;
              const [x, y, r] = el.dataset.orb.split(',').map(Number);
              const b = document.getElementById('orb').getBoundingClientRect();
              return {x, y, r, bx: b.x + b.width / 2, by: b.y + b.height / 2, br: b.width * 63 / 132, bw: b.width};
            }""", polling="raf", timeout=7000)
            d = handle.json_value()
            check(f"the docked boot orb lands on the real orb at {zoom}% (centre within 3px)",
                  abs(d["x"] - d["bx"]) <= 3 and abs(d["y"] - d["by"]) <= 3, str(d))
            check(f"…and its radius matches the real orb's at {zoom}% (within 3px)", abs(d["r"] - d["br"]) <= 3, str(d))
            check("…and the real orb has a real size", d["bw"] > 20, str(d))
        finally:
            ctx.close()

    def plays_and_looks():
        # The overlay really draws: the aurora (WebGL) and sparks (2D) are not blank.
        ctx, page = fresh()
        try:
            page.wait_for_selector(OVERLAY, state="attached")
            page.wait_for_timeout(1700)
            ink = page.evaluate("""() => {
              const fx = document.querySelector('#hud-boot canvas[data-layer=fx]');
              const d = fx.getContext('2d').getImageData(0, 0, fx.width, fx.height).data;
              let lit = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 20) lit++;
              return lit;
            }""")
            check("sparks and rings are drawn on the 2D layer by 1.7 s", ink > 500, str(ink))
        finally:
            ctx.close()

    def shield():
        # F3: a quick double-click is eaten whole.
        ctx, page = fresh()
        try:
            page.wait_for_selector(OVERLAY, state="attached")
            page.evaluate(UNDERLAY)
            page.mouse.dblclick(40, 40)
            page.wait_for_selector(OVERLAY, state="detached", timeout=2000)
            check("a double-click on boot reaches nothing underneath",
                  page.evaluate("window.__under") == {"click": 0, "dblclick": 0, "down": 0, "up": 0},
                  str(page.evaluate("window.__under")))
        finally:
            ctx.close()
        # F2: a cancelled press lets the shield go at once.
        ctx, page = fresh()
        try:
            page.wait_for_selector(OVERLAY, state="attached")
            page.evaluate(UNDERLAY)
            page.mouse.move(40, 40)
            page.mouse.down()
            page.evaluate("""() => document.querySelector('#hud-boot').dispatchEvent(
              new PointerEvent('pointercancel', {bubbles: true, pointerId: 1}))""")
            start = time.monotonic()
            page.wait_for_selector(OVERLAY, state="detached", timeout=3000)
            check("a cancelled press removes boot within a second", time.monotonic() - start < 1.0)
            page.mouse.up()
        finally:
            ctx.close()
        # F4: a press held past the hard cap still keeps its release from the HUD.
        ctx, page = fresh()
        try:
            page.wait_for_selector(OVERLAY, state="attached")
            page.evaluate(UNDERLAY)
            page.mouse.move(40, 40)
            page.mouse.down()
            page.wait_for_timeout(7800)
            check("a press held past the hard cap keeps boot up", page.locator(OVERLAY).count() == 1)
            page.mouse.up()
            page.wait_for_selector(OVERLAY, state="detached", timeout=2000)
            check("…and its release reaches nothing underneath",
                  page.evaluate("window.__under") == {"click": 0, "dblclick": 0, "down": 0, "up": 0},
                  str(page.evaluate("window.__under")))
        finally:
            ctx.close()

    def covers(zoom, size):
        ctx, page = fresh(zoom=zoom, size=size)
        try:
            page.wait_for_selector(OVERLAY, state="attached")
            got = page.evaluate("""() => {
              const el = document.querySelector('#hud-boot'), r = el.getBoundingClientRect();
              return {x:r.x, y:r.y, w:r.width, h:r.height, vw:innerWidth, vh:innerHeight,
                top:document.elementFromPoint(innerWidth-1, innerHeight-1) === el,
                pixels: [...el.querySelectorAll('canvas[data-layer=fx]')].every(c =>
                  c.width === Math.round(innerWidth * Math.min(devicePixelRatio, 2)) &&
                  c.height === Math.round(innerHeight * Math.min(devicePixelRatio, 2))),
                gl: [...el.querySelectorAll('canvas[data-layer=gl]')].every(c =>
                  c.width <= Math.round(innerWidth * Math.min(devicePixelRatio, 1)) &&
                  c.height <= Math.round(innerHeight * Math.min(devicePixelRatio, 1)))};
            }""")
            check(f"covers {size} at {zoom}% with a device-pixel 2D layer and a GL layer at most 1x",
                  abs(got['x']) < 1 and abs(got['y']) < 1 and abs(got['w']-got['vw']) < 1
                  and abs(got['h']-got['vh']) < 1 and got['top'] and got['pixels'] and got['gl'], str(got))
            page.set_viewport_size({"width": size[0]-50, "height": size[1]-50})
            page.wait_for_timeout(100)
            check(f"resize keeps boot filling viewport at {zoom}%", page.evaluate("""() => {
              const r = document.querySelector('#hud-boot').getBoundingClientRect();
              return Math.abs(r.width-innerWidth)<1 && Math.abs(r.height-innerHeight)<1;
            }"""))
        finally:
            ctx.close()

    def storage_unavailable():
        ctx, page = fresh(storage=False)
        try:
            page.wait_for_selector(OVERLAY, state="attached")
            page.keyboard.press("Escape")
            page.wait_for_selector(OVERLAY, state="detached")
            page.wait_for_timeout(250)
            check("unavailable storage plays once per page without errors", page.evaluate("window.__bootMounts") == 1)
        finally:
            ctx.close()

    try:
        run("automation", lambda: no_play())
        run("reduced motion", lambda: no_play(forced=True, reduced=True))
        run("finish", finishes)
        run("click and reload", click_skip)
        run("looks", plays_and_looks)
        run("2D canvas failure", lambda: canvas_fails("String(type) === '2d'", "a canvas with no 2D context"))
        run("WebGL failure", lambda: canvas_fails("String(type) === 'webgl'", "a forced WebGL failure (getContext('webgl') is null)"))
        run("shader failure", shader_fails)
        for zoom, size in ((100, (1280, 800)), (160, (1600, 1000))):
            run("dock", lambda zoom=zoom, size=size: dock(zoom, size))
        run("click shield", shield)
        for key in ("x", "Escape", "Control+`"):
            run("key " + key, lambda key=key: key_skip(key))
        run("push-to-talk", ptt)
        run("initial card", lambda: approval(True))
        run("arriving card", lambda: approval(False))
        for zoom, size in ((70, (900, 600)), (160, (1600, 1000))):
            run("zoom", lambda zoom=zoom, size=size: covers(zoom, size))
        run("storage", storage_unavailable)
        check("no page errors in boot suite", not errors, "; ".join(errors[:3]))
    finally:
        mock.world["approvals"] = keep_approvals


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
            browser = pw.chromium.launch(headless=True, args=["--use-fake-ui-for-media-stream",
                                                             "--use-fake-device-for-media-stream"])
            boot_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until,
                        v2.guard, v2.FAKE_RECOGNIZER)
            browser.close()
    finally:
        mock.stop()
    if v2.FAILURES:
        print(f"{len(v2.FAILURES)} of {v2.CHECKS} checks FAILED:")
        for failure in v2.FAILURES:
            print("  - " + failure)
        return 1
    print(f"all {v2.CHECKS} boot checks passed (including CSP watch)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
