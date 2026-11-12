"""Headless checks for the moving aurora (AG-2, hud/src/aurora/).

Runs on its own (`python tests/face/hud_v2_aurora_check.py`) and from `hud_v2_check.main()`,
in contexts and against a `MockDaemon` of its own. Headless Chromium renders WebGL in software
(SwiftShader), where the aurora runs its lighter flow shader at 30 fps; the stirring checks need the
fluid style, so those contexts set `jarvis.hud.auroraStats` to `fluid` (the owner's stats overlay key,
whose `fluid` value also lets the fluid run on a software renderer, for measuring).

What is pinned, each written to bite:
  - the canvas is in `#aurora`, behind `#root`, `pointer-events: none`, `aria-hidden`, never focusable,
    never what a point on the screen hits, and Tab never lands on it;
  - the settings (Motion, stir, ripple) are stored, and Still stops frames;
  - a hidden document stops frames, and a shown one resumes;
  - reduced motion is one still frame;
  - pointer movement stirs (the counter rises) yet never moves focus, and a click still reaches HUD controls;
  - a streaming chat ripples (the counter rises), is rate-limited, and is off when the setting is off;
  - without WebGL2 the canvas stays hidden and the static CSS wash remains.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

APPEARANCE = "jarvis.hud.appearance"
CANVAS = "#aurora canvas"


def _attr(page, name):
    return page.evaluate(f"document.querySelector('{CANVAS}')?.getAttribute('{name}')")


def _int(page, name):
    v = _attr(page, name)
    return int(v) if v is not None else -1


def _boot(page, mock, base, until, appearance=None):
    before = mock.sse_connections()
    page.goto(base + "/")
    page.wait_for_selector('[data-testid="sidebar"]', state="attached")
    if appearance is not None:
        page.evaluate("(a) => localStorage.setItem('%s', a)" % APPEARANCE, json.dumps(appearance))
        before = mock.sse_connections()
        page.reload()
        page.wait_for_selector('[data-testid="sidebar"]', state="attached")
    mock.await_reconnect(before)
    until(lambda: _attr(page, "data-aurora-state") is not None, timeout=5)


def _context(browser, guard, init_script, **kw):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, **kw)
    guard(ctx)
    ctx.add_init_script(init_script)
    return ctx


# Keep the drawing buffer so the test can read the picture back (the app does not need it).
KEEP_BUFFER = """
const origCtx = HTMLCanvasElement.prototype.getContext;
HTMLCanvasElement.prototype.getContext = function (kind, attrs, ...rest) {
  if (kind === 'webgl2') attrs = Object.assign({}, attrs, { preserveDrawingBuffer: true });
  return origCtx.call(this, kind, attrs, ...rest);
};
"""

# The brightest pixel of the canvas against the brightest of --base and the washes (WCAG luminance).
PIXEL_BOUND = """async () => {
  const lin = (v) => { v /= 255; return v < 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  const lum = (r, g, b) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  const cs = getComputedStyle(document.documentElement);
  let bound = 0;
  for (const n of ['--base', '--wash-1', '--wash-2', '--wash-3']) {
    const m = /#([0-9a-f]{6})/i.exec(cs.getPropertyValue(n));
    if (!m) return { error: 'no ' + n };
    const v = parseInt(m[1], 16);
    bound = Math.max(bound, lum(v >> 16, (v >> 8) & 255, v & 255));
  }
  const src = document.querySelector('#aurora canvas');
  const img = new Image();
  img.src = src.toDataURL('image/png');
  await img.decode();
  const c = document.createElement('canvas');
  c.width = img.width; c.height = img.height;
  const x = c.getContext('2d');
  x.drawImage(img, 0, 0);
  const d = x.getImageData(0, 0, c.width, c.height).data;
  let worst = 0, lit = 0;
  for (let i = 0; i < d.length; i += 4) {
    const l = lum(d[i], d[i + 1], d[i + 2]);
    if (l > worst) worst = l;
    if (d[i] + d[i + 1] + d[i + 2] > 0) lit++;
  }
  return { bound, worst, lit, w: c.width, h: c.height };
}"""

FLUID_ON_SOFTWARE = "try { localStorage.setItem('jarvis.hud.auroraStats', 'fluid'); } catch (e) {}"
NO_WEBGL2 = """
const orig = HTMLCanvasElement.prototype.getContext;
HTMLCanvasElement.prototype.getContext = function (kind, ...rest) {
  if (kind === 'webgl2') return null;
  return orig.call(this, kind, ...rest);
};
"""


def _pixel_bound(page, check, what):
    r = page.evaluate(PIXEL_BOUND)
    ok = "error" not in r and r["lit"] > 0 and r["worst"] <= r["bound"] + 0.004
    check(f"{what}: no rendered pixel is brighter than the brightest wash", ok, str(r))


def _new_page(ctx, errors):
    """Every page gets the error collector, so a final "no errors" reads its own page."""
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    return page


def aurora_checks(browser, mock, base, check, until, guard, init_script):
    print("\nthe moving aurora (AG-2)")
    errors: list[str] = []

    # ---- 1. the default: software renderer, so the flow shader at 30 fps ----------------------------
    ctx = _context(browser, guard, init_script + KEEP_BUFFER)
    page = _new_page(ctx, errors)
    _boot(page, mock, base, until)
    info = page.evaluate("""() => {
      const root = document.getElementById('root'), layer = document.getElementById('aurora');
      const c = layer.querySelector('canvas'), cs = getComputedStyle(c), ls = getComputedStyle(layer);
      return { inLayer: !!c, before: !!(layer.compareDocumentPosition(root) & Node.DOCUMENT_POSITION_FOLLOWING),
        pe: cs.pointerEvents, layerPe: ls.pointerEvents, z: ls.zIndex, hidden: c.getAttribute('aria-hidden'),
        tabindex: c.hasAttribute('tabindex'), rect: c.getBoundingClientRect().toJSON(), testid: c.dataset.testid };
    }""")
    check("the canvas is in #aurora, which comes before #root and sits under it (z-index -1)",
          info["inLayer"] and info["before"] and info["z"] == "-1", str(info))
    check("the canvas is pointer-events:none, aria-hidden and has no tabindex",
          info["pe"] == "none" and info["layerPe"] == "none" and info["hidden"] == "true" and not info["tabindex"], str(info))
    check("the canvas fills the window", abs(info["rect"]["width"] - 1280) < 2 and abs(info["rect"]["height"] - 800) < 2, str(info["rect"]))
    state = until(lambda: _attr(page, "data-aurora-state") == "running", timeout=5)
    check("on a software renderer it runs the lighter flow shader",
          bool(state) and _attr(page, "data-aurora-style") == "flow", f"{_attr(page, 'data-aurora-state')} {_attr(page, 'data-aurora-style')}")
    f0 = _int(page, "data-aurora-frames")
    time.sleep(1.2)
    f1 = _int(page, "data-aurora-frames")
    check("frames are drawn while it runs, at 30 fps at most", f1 > f0 and (f1 - f0) <= 45, f"{f0} -> {f1}")

    time.sleep(0.5)
    _pixel_bound(page, check, "flow")

    # focus: tab through the HUD, the canvas never takes it
    kinds = []
    for _ in range(25):
        page.keyboard.press("Tab")
        kinds.append(page.evaluate("document.activeElement && document.activeElement.tagName"))
    check("Tab never lands on the canvas", "CANVAS" not in kinds, str(set(kinds)))

    # nothing on screen hits the canvas
    hits = page.evaluate("""() => { const out = [];
      for (let x = 5; x < 1280; x += 97) for (let y = 5; y < 800; y += 89) {
        const el = document.elementFromPoint(x, y); if (el && el.closest('#aurora')) out.push([x, y]); }
      return out; }""")
    check("no point on the screen hits the aurora layer", not hits, str(hits[:3]))

    # hidden document stops frames; shown resumes
    page.evaluate("""() => { Object.defineProperty(document, 'hidden', { value: true, configurable: true });
      document.dispatchEvent(new Event('visibilitychange')); }""")
    until(lambda: _attr(page, "data-aurora-state") == "paused", timeout=3)
    paused = _attr(page, "data-aurora-state")
    f0 = _int(page, "data-aurora-frames")
    time.sleep(0.8)
    f1 = _int(page, "data-aurora-frames")
    check("a hidden document pauses the aurora and stops its frames", paused == "paused" and f0 == f1, f"{paused} {f0} -> {f1}")
    page.evaluate("""() => { Object.defineProperty(document, 'hidden', { value: false, configurable: true });
      document.dispatchEvent(new Event('visibilitychange')); }""")
    check("and showing it again resumes", bool(until(lambda: _attr(page, "data-aurora-state") == "running", timeout=3)))

    # Settings: the section exists, Still stops frames and is stored
    page.locator('[data-testid="titlebar-settings"]').click()
    page.wait_for_selector('[data-testid="settings-page"]')
    sel = page.locator('[data-testid="appearance-motion"]')
    check("Settings has the Appearance section with Fluid selected by default",
          sel.count() == 1 and sel.input_value() == "fluid"
          and page.locator('[data-testid="appearance-stir"]').is_checked()
          and page.locator('[data-testid="appearance-activity"]').is_checked())
    sel.select_option("still")
    until(lambda: _attr(page, "data-aurora-state") == "still", timeout=3)
    f0 = _int(page, "data-aurora-frames")
    time.sleep(0.8)
    f1 = _int(page, "data-aurora-frames")
    stored = json.loads(page.evaluate(f"localStorage.getItem('{APPEARANCE}')") or "{}")
    check("Still stops frames, and is stored",
          _attr(page, "data-aurora-state") == "still" and f0 == f1 and stored.get("motion") == "still", f"{f0} {f1} {stored}")
    sel.select_option("flow")
    check("Flow runs again, live, with no reload", bool(until(lambda: _attr(page, "data-aurora-state") == "running", timeout=3)))
    check("the stir switches are disabled unless Motion is Fluid", page.locator('[data-testid="appearance-stir"]').is_disabled())
    drift = page.locator('[data-testid="appearance-drift"]')
    check("Natural drift is a 0-200% slider defaulting to 100%, shown next to it",
          drift.count() == 1 and drift.input_value() == "100" and drift.get_attribute("max") == "200"
          and drift.get_attribute("step") == "10" and not drift.is_disabled()
          and page.locator('[data-testid="appearance-drift-value"]').inner_text() == "100%"
          and _attr(page, "data-aurora-drift") == "1")
    drift.fill("30")
    until(lambda: _attr(page, "data-aurora-drift") == "0.3", timeout=3)
    stored = json.loads(page.evaluate(f"localStorage.getItem('{APPEARANCE}')") or "{}")
    check("changing it stores the strength and reaches the running aurora live, with no reload",
          stored.get("drift") == 0.3 and _attr(page, "data-aurora-drift") == "0.3"
          and _attr(page, "data-aurora-state") == "running"
          and page.locator('[data-testid="appearance-drift-value"]').inner_text() == "30%", str(stored))
    sel.select_option("still")
    check("Still disables the slider and says why",
          page.locator('[data-testid="appearance-drift"]').is_disabled()
          and "no drift" in page.locator('[data-testid="appearance-drift-note"]').inner_text())
    sel.select_option("fluid")
    page.locator('[data-testid="appearance-stir"]').uncheck()
    page.locator('[data-testid="appearance-activity"]').uncheck()
    stored = json.loads(page.evaluate(f"localStorage.getItem('{APPEARANCE}')") or "{}")
    check("the two switches are stored", stored.get("stir") is False and stored.get("activity") is False, str(stored))
    page.evaluate(f"localStorage.setItem('{APPEARANCE}', '{{not json')")
    page.reload()
    page.wait_for_selector('[data-testid="sidebar"]', state="attached")
    check("a corrupt stored record falls back to the default (Fluid request, flow on software)",
          bool(until(lambda: _attr(page, "data-aurora-state") == "running", timeout=5)))
    check("the page had no errors", not errors, str(errors[:2]))
    ctx.close()

    # ---- 2. reduced motion: one still frame --------------------------------------------------------
    ctx = _context(browser, guard, init_script, reduced_motion="reduce")
    page = _new_page(ctx, errors)
    _boot(page, mock, base, until)
    until(lambda: _attr(page, "data-aurora-state") == "still", timeout=4)
    f0 = _int(page, "data-aurora-frames")
    time.sleep(0.8)
    f1 = _int(page, "data-aurora-frames")
    check("reduced motion draws one still frame and no more",
          _attr(page, "data-aurora-state") == "still" and f0 == f1 == 1, f"{_attr(page, 'data-aurora-state')} {f0} {f1}")
    ctx.close()

    # ---- 3. no WebGL2: the static wash stays --------------------------------------------------------
    ctx = _context(browser, guard, init_script + NO_WEBGL2)
    page = _new_page(ctx, errors)
    _boot(page, mock, base, until)
    time.sleep(0.5)
    vis = page.evaluate(f"getComputedStyle(document.querySelector('{CANVAS}')).visibility")
    bg = page.evaluate("getComputedStyle(document.getElementById('aurora')).backgroundImage")
    check("without WebGL2 nothing runs, the canvas stays hidden and the CSS wash is still painted",
          _attr(page, "data-aurora-state") == "off" and vis == "hidden" and "radial-gradient" in bg, f"{_attr(page, 'data-aurora-state')} {vis}")
    ctx.close()

    # ---- 4. the fluid: pointer stirs and chat ripples ----------------------------------------------
    ctx = _context(browser, guard, init_script + FLUID_ON_SOFTWARE + KEEP_BUFFER)
    page = _new_page(ctx, errors)
    _boot(page, mock, base, until)
    ok = until(lambda: _attr(page, "data-aurora-style") == "fluid" and _attr(page, "data-aurora-state") == "running", timeout=8)
    check("with the stats override the fluid runs even on a software renderer", bool(ok),
          f"{_attr(page, 'data-aurora-style')} {_attr(page, 'data-aurora-state')}")
    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: page.locator('[data-testid="log"] .msg').count() >= 2)
    box = page.locator('[data-testid="input"]').first
    box.focus()
    focus_before = page.evaluate("document.activeElement && (document.activeElement.dataset.testid || document.activeElement.tagName)")
    s0 = _int(page, "data-aurora-stirs")
    for i in range(80):
        page.mouse.move(100 + i * 12, 300 + (i % 9) * 6)
        time.sleep(0.012)
    time.sleep(0.2)
    s1 = _int(page, "data-aurora-stirs")
    focus_after = page.evaluate("document.activeElement && (document.activeElement.dataset.testid || document.activeElement.tagName)")
    check("moving the pointer anywhere stirs the fluid (the stir counter rises)", s1 > s0 + 5, f"{s0} -> {s1}")
    check("pointer movement never changes document.activeElement", focus_before == focus_after, f"{focus_before} -> {focus_after}")
    # saturate the dye: several more strokes across the whole window, then read the picture back
    for _ in range(3):
        for i in range(80):
            page.mouse.move(40 + i * 15, 120 + ((i * 37) % 600))
            time.sleep(0.012)
    time.sleep(0.3)
    check("the stirred fluid really is lit (the bound below is not met by a black canvas)",
          _int(page, "data-aurora-stirs") > s1 + 20, str(_int(page, "data-aurora-stirs")))
    _pixel_bound(page, check, "fluid, stirred")
    # a click still reaches a HUD control (the aurora is never in the way)
    page.mouse.move(640, 400)
    page.locator('[data-testid="titlebar-settings"]').click()
    check("clicking a HUD control still works while the pointer stirs",
          page.locator('[data-testid="settings-page"]').count() == 1)
    page.keyboard.press("Escape")

    # chat activity: a streaming turn ripples, rate-limited
    r0 = _int(page, "data-aurora-ripples")
    mock.emit("turn_started", {}, thread_id="t1")
    for _ in range(30):
        mock.emit("text_delta", {"text": "word "}, thread_id="t1")
        time.sleep(0.02)
    time.sleep(0.4)
    r1 = _int(page, "data-aurora-ripples")
    check("a streaming chat ripples the aurora (the ripple counter rises)", r1 > r0, f"{r0} -> {r1}")
    check("and the ripples are rate-limited (30 chunks in 0.6 s make at most one per pane window)", (r1 - r0) <= 2, f"{r0} -> {r1}")
    time.sleep(1.1)
    for _ in range(5):
        mock.emit("text_delta", {"text": "more "}, thread_id="t1")
        time.sleep(0.05)
    time.sleep(0.3)
    r2 = _int(page, "data-aurora-ripples")
    check("it ripples again once the pane's window has passed", r2 > r1, f"{r1} -> {r2}")

    # a pane that cannot be seen makes no ripple: another view, inert, or under a covering layer
    def streams_silently(label, setup, teardown):
        setup()
        time.sleep(1.2)
        before = _int(page, "data-aurora-ripples")
        for _ in range(8):
            mock.emit("text_delta", {"text": "hidden "}, thread_id="t1")
            time.sleep(0.05)
        time.sleep(0.3)
        after = _int(page, "data-aurora-ripples")
        teardown()
        check(f"a streaming chat makes no ripple while its pane is {label}", after == before, f"{before} -> {after}")

    pane_js = "document.querySelector('[data-testid=\"input\"]').closest('[data-testid^=\"pane-\"]')"
    page.locator('[data-testid="task-k1"]').click()
    streams_silently("showing another view (Tasks)",
                     lambda: until(lambda: page.locator('[data-testid="task-view"]').count() > 0),
                     lambda: page.locator('[data-testid="thread-t1"]').click())
    until(lambda: page.locator('[data-testid="input"]').count() > 0)
    streams_silently("inert", lambda: page.evaluate(f"() => {pane_js}.setAttribute('inert', '')"),
                     lambda: page.evaluate(f"() => {pane_js}.removeAttribute('inert')"))
    streams_silently("covered by a card or modal", lambda: page.evaluate("""() => {
        const v = document.createElement('div'); v.id = 'test-cover';
        v.style.cssText = 'position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,.4)'; document.body.appendChild(v); }"""),
                     lambda: page.evaluate("() => document.getElementById('test-cover')?.remove()"))
    streams_silently("display:none", lambda: page.evaluate(f"() => {pane_js}.style.display = 'none'"),
                     lambda: page.evaluate(f"() => {pane_js}.style.display = ''"))
    time.sleep(1.2)
    rv0 = _int(page, "data-aurora-ripples")
    for _ in range(8):
        mock.emit("text_delta", {"text": "visible "}, thread_id="t1")
        time.sleep(0.05)
    time.sleep(0.3)
    check("and it ripples again once the pane shows the chat, uncovered", _int(page, "data-aurora-ripples") > rv0,
          f"{rv0} -> {_int(page, 'data-aurora-ripples')}")
    mock.emit("turn_finished", {"stop": "end"}, thread_id="t1")

    # switches off: no more stirs or ripples
    page.evaluate(f"""() => {{ localStorage.setItem('{APPEARANCE}', JSON.stringify({{motion: 'fluid', stir: false, activity: false}}));
      window.dispatchEvent(new Event('hud-appearance-change')); }}""")
    time.sleep(0.3)
    r3, s3 = _int(page, "data-aurora-ripples"), _int(page, "data-aurora-stirs")
    mock.emit("turn_started", {}, thread_id="t1")
    time.sleep(1.1)
    for _ in range(5):
        mock.emit("text_delta", {"text": "again "}, thread_id="t1")
        time.sleep(0.05)
    for i in range(40):
        page.mouse.move(200 + i * 10, 500)
        time.sleep(0.012)
    time.sleep(0.3)
    check("with both switches off there are no ripples and no stirs",
          _int(page, "data-aurora-ripples") == r3 and _int(page, "data-aurora-stirs") == s3,
          f"{r3},{s3} -> {_int(page, 'data-aurora-ripples')},{_int(page, 'data-aurora-stirs')}")
    mock.emit("turn_finished", {"stop": "end"}, thread_id="t1")
    check("the page had no errors", not errors, str(errors[:2]))
    ctx.close()


def main():
    from playwright.sync_api import sync_playwright

    from tests.face import hud_v2_check as H
    from tests.face.hud_v2_mock import DIST, MockDaemon

    if not (DIST / "index.html").exists():
        print("hud/dist is not built. Run: cd hud && npm ci --ignore-scripts && npm run build")
        return 2
    mock = MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--use-fake-ui-for-media-stream",
                                                              "--use-fake-device-for-media-stream"])
            aurora_checks(browser, mock, f"http://127.0.0.1:{mock.port}", H.check, H.until, H.guard, H.FAKE_RECOGNIZER)
            browser.close()
    finally:
        mock.stop()
    print()
    if H.FAILURES:
        print(f"{len(H.FAILURES)} of {H.CHECKS} checks FAILED:")
        for f in H.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {H.CHECKS} aurora checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
