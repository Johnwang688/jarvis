"""Button shape and the binary click sparkle (owner, 2026-10-15): real browser
checks against an isolated mock HTTP/SSE daemon.

Free, like the rest of the face suites. Run on its own with
`PYTHONPATH=. python tests/face/hud_v2_buttons_check.py`, or as part of
`hud_v2_check.py`.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.face.hud_v2_check import expand, open_settings
from tests.face.hud_v2_layout_check import _approval, _boot, _set_storage

PAGE = '[data-testid="settings-page"]'
BITS = ".sparkle-bit"
KEY = "jarvis.hud.sparkle"

# The only circles left (owner 2026-10-15): Send, Stop and Steer (Steer is .send.steer).
CIRCLES = ".iconbtn.send, .iconbtn.stop"

# Every button-like thing (buttons, tabs, Attach, chips that hold a control, VEX step chips) with its top-left computed radius, as pixels.
RADII = """(skip) => [...document.querySelectorAll('button, [role=button], [role=tab], label.iconbtn, .chip:not(.edlock):not(.edro), .vexsteps li, .voice-words-chip')]
  .filter(el => !el.matches(skip) && el.getBoundingClientRect().width > 0)
  .map(el => {
    const cs = getComputedStyle(el);
    const px = v => parseFloat(v);
    return {
      id: el.dataset.testid || el.className || el.tagName,
      radii: [px(cs.borderTopLeftRadius), px(cs.borderTopRightRadius), px(cs.borderBottomRightRadius), px(cs.borderBottomLeftRadius)],
    };
  })"""


def _radius(page, sel: str) -> float:
    return page.locator(sel).first.evaluate("e => parseFloat(getComputedStyle(e).borderTopLeftRadius)")


def _count(page) -> int:
    return page.locator(BITS).count()


def _burst(page, until, timeout=1.0) -> bool:
    return bool(until(lambda: _count(page) > 0, timeout=timeout))


def _gone(page, until) -> bool:
    # Half a second of animation; the fallback timer runs 120 ms later.
    return bool(until(lambda: _count(page) == 0, timeout=2.0))


def _pe(page, sel: str) -> str:
    return page.locator(sel).first.evaluate("e => getComputedStyle(e).pointerEvents")


def shape_checks(page, mock, check, until):
    print("\nButtons: crisp rectangles, circles kept")
    wrong = []
    seen = 0

    def sweep(where):
        nonlocal seen
        for b in page.evaluate(RADII, CIRCLES):
            seen += 1
            # 0 is flush (tab strips, menu rows, the inner ends of a segmented group);
            # a corner is never more than the shared 4px.
            if max(b["radii"]) > 4.01:
                wrong.append(f"{where}:{b['id']}={b['radii']}")

    sweep("main")
    check("the shared token is 4px", page.evaluate(
        "getComputedStyle(document.documentElement).getPropertyValue('--radius-btn').trim()") == "4px")
    check("a plain button (New thread) is a 4px rectangle", _radius(page, '[data-testid="new-thread"]') == 4)
    check("an icon button (titlebar gear) is 4px", _radius(page, '[data-testid="titlebar-settings"]') == 4)
    check("the zoom group's outer corners are 4px and the inner ones flush", page.evaluate("""() => {
      const z = [...document.querySelectorAll('.zoomctl button')].map(b => getComputedStyle(b));
      return z[0].borderTopLeftRadius === '4px' && z[0].borderTopRightRadius === '0px'
        && z[z.length-1].borderTopRightRadius === '4px' && z[z.length-1].borderTopLeftRadius === '0px';
    }"""))
    check("the dictation group's outer corners are 4px", page.evaluate("""() => {
      const z = [...document.querySelectorAll('[data-testid="dictation-auto"], [data-testid="dictation-off"]')];
      return z.length === 2 && getComputedStyle(z[0]).borderTopLeftRadius === '4px'
        && getComputedStyle(z[1]).borderTopRightRadius === '4px';
    }"""))
    check("Send stays a true circle", page.locator('[data-testid="send"]').first.evaluate(
        "e => getComputedStyle(e).borderTopLeftRadius") == "50%")
    check("Attach is a 4px rectangle now", _radius(page, ".iconbtn.attach") == 4)
    check("the input bar's project selector chip and model chip are 4px rectangles, not pills",
          page.evaluate("[...document.querySelectorAll('.chip.projchip')].map(e => parseFloat(getComputedStyle(e).borderTopLeftRadius))")
          not in ([], None) and all(r == 4 for r in page.evaluate(
              "[...document.querySelectorAll('.chip.projchip')].map(e => parseFloat(getComputedStyle(e).borderTopLeftRadius))")))
    check("a status badge that is not a control keeps its shape (.badge 3px)", page.evaluate(
        "[...document.querySelectorAll('.badge')].every(e => parseFloat(getComputedStyle(e).borderTopLeftRadius) === 3)"))
    # A running turn shows Stop and Steer beside Send: all three stay circles.
    expand(page, "p1", "thread-t1")
    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: page.evaluate("window.__hud.state().threadId") == "t1")
    mock.emit("turn_started", {}, thread_id="t1")
    until(lambda: page.locator('[data-testid="stop"]').count() > 0)
    round_ = page.evaluate("""() => ['stop', 'send'].map(t => { const e = document.querySelector(`[data-testid="${t}"]`);
      return [t, e && e.className, e && getComputedStyle(e).borderTopLeftRadius]; })""")
    check("Stop and Steer (the running turn's Send) stay true circles",
          round_[0][2] == "50%" and round_[1][2] == "50%" and "steer" in round_[1][1], str(round_))
    sweep("running")
    mock.emit("turn_finished", {}, thread_id="t1")
    until(lambda: not page.evaluate("window.__hud.state().busy"))

    page.locator('[data-testid="titlebar-settings"]').click()
    page.wait_for_selector(PAGE)
    for section in ["General", "Model", "Voice", "Avatar", "Usage", "Routing", "Archive"]:
        open_settings(page, section)
        time.sleep(0.15)
        sweep("settings-" + section)
    check("a Settings nav button is a 4px rectangle", _radius(page, '[data-testid="settings-general"]') == 4)
    page.keyboard.press("Escape")
    page.wait_for_selector(PAGE, state="detached")

    _approval(mock, "shape-card")
    page.wait_for_selector('[data-testid="approval-card"]')
    sweep("approval")
    for tid in ("approval-deny", "approval-always", "approval-allow"):
        check(f"{tid} is a 4px rectangle", _radius(page, f'[data-testid="{tid}"]') == 4)
    page.keyboard.press("Escape")  # denies
    page.wait_for_selector('[data-testid="approval-card"]', state="detached")
    check(f"no button anywhere has a corner over 4px (outside {CIRCLES}); {seen} buttons seen",
          not wrong and seen > 40, "; ".join(wrong[:8]))


def sparkle_checks(page, mock, check, until):
    print("\nButtons: binary click sparkle")
    check("earlier clicks' glyphs clear by themselves", _gone(page, until))
    before = page.evaluate("""() => ({
      h: document.documentElement.scrollHeight, w: document.documentElement.scrollWidth,
      root: JSON.stringify(document.getElementById('root').getBoundingClientRect()),
      kids: document.getElementById('root').getElementsByTagName('*').length })""")

    # The first click does its work: the gear opens Settings.
    gear = page.locator('[data-testid="titlebar-settings"]')
    gear.click()
    check("the first click's own action happens (Settings opens)", page.locator(PAGE).count() == 1
          or bool(until(lambda: page.locator(PAGE).count() == 1, timeout=2)))
    check("a click bursts 1s and 0s", _burst(page, until))
    glyphs = page.evaluate("[...document.querySelectorAll('.sparkle-bit')].map(b => b.textContent)")
    check("the glyphs are only 1 and 0, a few of them", 3 <= len(glyphs) <= 8 and set(glyphs) <= {"0", "1"}, str(glyphs))
    check("the layer and every glyph ignore the pointer", page.evaluate("""() =>
      getComputedStyle(document.getElementById('sparkle-layer')).pointerEvents === 'none'
      && [...document.querySelectorAll('.sparkle-bit')].every(b => getComputedStyle(b).pointerEvents === 'none')"""))
    check("the layer is fixed, aria-hidden, under <body> and holds nothing focusable", page.evaluate("""() => {
      const l = document.getElementById('sparkle-layer');
      return getComputedStyle(l).position === 'fixed' && l.getAttribute('aria-hidden') === 'true'
        && l.parentElement === document.body && !l.querySelector('[tabindex], button, input, a');
    }"""))
    check("glyphs are monospace and glacier cyan", page.locator(BITS).first.evaluate("""e => {
      const cs = getComputedStyle(e), root = getComputedStyle(document.documentElement);
      const probe = document.createElement('i'); probe.style.color = root.getPropertyValue('--accent');
      document.body.appendChild(probe); const want = getComputedStyle(probe).color; probe.remove();
      return cs.color === want && /mono|Cascadia|Consolas/i.test(cs.fontFamily);
    }"""))
    check("the overlay hit-tests as nothing: the page is what is under the glyphs", page.evaluate("""() => {
      const b = document.querySelector('.sparkle-bit'); if (!b) return true;
      const r = b.getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return !hit || !hit.closest('#sparkle-layer');
    }"""))
    check("the glyphs fade out and are removed", _gone(page, until))
    check("no glyph is left in the DOM", page.locator(BITS).count() == 0)
    page.keyboard.press("Escape")
    page.wait_for_selector(PAGE, state="detached")
    after = page.evaluate("""() => ({
      h: document.documentElement.scrollHeight, w: document.documentElement.scrollWidth,
      root: JSON.stringify(document.getElementById('root').getBoundingClientRect()),
      kids: document.getElementById('root').getElementsByTagName('*').length })""")
    check("layout is unchanged: scroll size, #root box and its element count",
          before == after, f"{before} != {after}")

    # Bursts from the click point.
    btn = page.locator('[data-testid="toggle-left"]')
    box = btn.bounding_box()
    px, py = box["x"] + 3, box["y"] + 4
    page.mouse.move(px, py)
    page.mouse.down()
    page.mouse.up()
    check("a mouse click bursts from the click point (to a few px)", bool(until(lambda: _count(page) > 0, timeout=1)) and page.evaluate(
        "([x, y]) => [...document.querySelectorAll('.sparkle-bit')].every(b => Math.abs(parseFloat(b.style.left) - x) < 2 && Math.abs(parseFloat(b.style.top) - y) < 2)",
        [px, py]))
    btn.click()  # put the pane back
    _gone(page, until)

    # Keyboard activation bursts from the button's centre and still activates.
    gear.focus()
    page.keyboard.press("Enter")
    check("Enter activates the focused button (Settings opens)", bool(until(lambda: page.locator(PAGE).count() == 1, timeout=2)))
    check("keyboard activation bursts", _burst(page, until))
    box = gear.bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    check("... from the button's centre", page.evaluate(
        "([x, y]) => [...document.querySelectorAll('.sparkle-bit')].every(b => Math.abs(parseFloat(b.style.left) - x) <= 1 && Math.abs(parseFloat(b.style.top) - y) <= 1)",
        [cx, cy]), f"centre {cx},{cy}")
    _gone(page, until)
    page.keyboard.press("Escape")
    page.wait_for_selector(PAGE, state="detached")
    gear.focus()
    page.keyboard.press("Space")
    check("Space activates and bursts too", bool(until(lambda: page.locator(PAGE).count() == 1, timeout=2)) and _burst(page, until))
    _gone(page, until)
    page.keyboard.press("Escape")
    page.wait_for_selector(PAGE, state="detached")

    # Rapid clicking: capped, then cleaned up.
    for _ in range(30):
        btn.click()
        btn.click()
    peak = page.evaluate("() => document.querySelectorAll('.sparkle-bit').length")
    check(f"rapid clicking stays under the 24-glyph cap (saw {peak})", peak <= 24)
    check("... and every glyph is cleaned up afterwards", _gone(page, until))
    check("... leaving the layer empty", page.locator("#sparkle-layer").evaluate("e => e.childElementCount") == 0)


def approval_checks(page, mock, check, until):
    print("\nButtons: sparkle on the approval card")
    _approval(mock, "spark-allow", "ls")
    page.wait_for_selector('[data-testid="approval-card"]')
    card_text = page.locator('[data-testid="approval-card"]').inner_text()
    allow = page.locator('[data-testid="approval-allow"]')
    allow.click()
    check("AUTHORIZE takes effect on the first click, once", bool(until(
        lambda: len(mock.sent("POST", "/approvals/spark-allow")) == 1, timeout=3))
        and mock.sent("POST", "/approvals/spark-allow")[-1]["decision"] == "allow")
    check("it bursts while the veil is up", _burst(page, until, timeout=1.0))
    page.wait_for_selector('[data-testid="approval-card"]', state="detached")
    check("the burst never put the card's text or focus anywhere else",
          "AUTHORIZATION REQUIRED" in card_text and page.evaluate("!document.activeElement || !document.activeElement.closest('#sparkle-layer')"))
    _gone(page, until)

    # Burst while the card is up: sits above the veil, and does not disturb its trust rules.
    _approval(mock, "spark-deny", "ls")
    page.wait_for_selector('[data-testid="approval-card"]')
    deny = page.locator('[data-testid="approval-deny"]')
    page.locator('[data-testid="approval-card"]').click(position={"x": 12, "y": 60})
    check("a click on the card's text does not burst (not a button) or answer it",
          _count(page) == 0 and not mock.sent("POST", "/approvals/spark-deny"))
    page.keyboard.press("Tab")
    check("Tab still lands on DENY, never on a sparkle glyph",
          page.evaluate("document.activeElement && document.activeElement.dataset.testid") == "approval-deny")
    page.keyboard.press("Enter")
    check("Enter on DENY denies, once", bool(until(lambda: len(mock.sent("POST", "/approvals/spark-deny")) == 1, timeout=3))
          and mock.sent("POST", "/approvals/spark-deny")[-1]["decision"] == "deny")
    page.wait_for_selector('[data-testid="approval-card"]', state="detached")
    _gone(page, until)

    # z-order: while the card is up a burst is drawn over the veil.
    _approval(mock, "spark-z", "ls")
    page.wait_for_selector('[data-testid="approval-card"]')
    z = page.evaluate("""() => [Number(getComputedStyle(document.getElementById('authveil')).zIndex),
      Number(getComputedStyle(document.getElementById('sparkle-layer')).zIndex)]""")
    check("the layer sits above the approval veil", z[1] > z[0], str(z))
    page.keyboard.press("Escape")
    page.wait_for_selector('[data-testid="approval-card"]', state="detached")
    _gone(page, until)


def toggle_checks(page, mock, base, check, until):
    print("\nButtons: Settings toggle")
    open_settings(page, "General")
    toggle = page.locator('[data-testid="sparkle-toggle"]')
    check("General has the sparkle switch, on by default", toggle.is_checked())
    toggle.click()
    check("turning it off is remembered in this window", page.evaluate(f"localStorage.getItem('{KEY}')") == "0"
          and not toggle.is_checked())
    check("... clicking the checkbox itself never sparkles (it is not a button)", _count(page) == 0)
    page.locator('[data-testid="settings-model"]').click()
    time.sleep(0.4)
    check("with it off, a button click bursts nothing and still works",
          _count(page) == 0 and page.locator('[data-testid="settings-model"]').get_attribute("aria-current") == "page")
    _boot(page, mock, base, until, reload=True)
    page.locator('[data-testid="titlebar-settings"]').click()
    page.wait_for_selector(PAGE)
    time.sleep(0.4)
    check("... and after a reload it is still off", _count(page) == 0)
    open_settings(page, "General")
    check("the switch reads off after the reload", not page.locator('[data-testid="sparkle-toggle"]').is_checked())
    page.locator('[data-testid="sparkle-toggle"]').click()
    check("turning it back on is remembered", page.evaluate(f"localStorage.getItem('{KEY}')") == "1")
    page.locator('[data-testid="settings-model"]').click()
    check("... and the next click bursts again", _burst(page, until))
    _gone(page, until)
    page.keyboard.press("Escape")
    page.wait_for_selector(PAGE, state="detached")


def zoom_checks(page, mock, base, check, until):
    print("\nButtons: sparkle under HUD zoom")
    _set_storage(page, zoom="140")
    _boot(page, mock, base, until, reload=True)
    btn = page.locator('[data-testid="toggle-left"]')
    box = btn.bounding_box()
    px, py = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.click(px, py)
    ok = bool(until(lambda: _count(page) > 0, timeout=1.5))
    # The layer is outside the zoomed #root, so a glyph's left/top are viewport pixels
    # (its box moves while it animates, so the start point is read from the style).
    near = page.evaluate(
        "([x, y]) => [...document.querySelectorAll('.sparkle-bit')].every(b =>"
        " Math.abs(parseFloat(b.style.left) - x) < 3 && Math.abs(parseFloat(b.style.top) - y) < 3)",
        [px, py])
    check("at 140% zoom the burst still starts at the pointer", ok and near, f"{px},{py}")
    _gone(page, until)
    page.mouse.click(px, py)  # restore the pane
    _gone(page, until)
    _set_storage(page, zoom="100")


def reduced_motion_checks(browser, mock, base, check, until, guard, init_script):
    print("\nButtons: prefers-reduced-motion")
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, reduced_motion="reduce")
    guard(ctx)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    page.set_default_timeout(6000)
    try:
        _boot(page, mock, base, until)
        check("the emulation is on", page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches"))
        page.locator('[data-testid="titlebar-settings"]').click()
        page.wait_for_selector(PAGE)
        time.sleep(0.5)
        check("a click works and shows no glyph under reduced motion",
              _count(page) == 0 and page.locator("#sparkle-layer").count() == 0)
        open_settings(page, "General")
        check("the switch itself is still shown (on), the system preference wins",
              page.locator('[data-testid="sparkle-toggle"]').is_checked())
    finally:
        ctx.close()


def buttons_checks(browser, mock, base, check, until, guard, init_script):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    guard(ctx)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    page.set_default_timeout(6000)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        _boot(page, mock, base, until)
        shape_checks(page, mock, check, until)
        sparkle_checks(page, mock, check, until)
        approval_checks(page, mock, check, until)
        toggle_checks(page, mock, base, check, until)
        zoom_checks(page, mock, base, check, until)
        check("no page error during the button checks", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()
    reduced_motion_checks(browser, mock, base, check, until, guard, init_script)


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
            browser = pw.chromium.launch(headless=True)
            buttons_checks(browser, mock, f"http://127.0.0.1:{mock.port}", H.check, H.until,
                           H.guard, H.PLAIN_RECOGNIZER)
            browser.close()
    finally:
        mock.stop()
    print()
    if H.FAILURES:
        print(f"{len(H.FAILURES)} of {H.CHECKS} checks FAILED:")
        for f in H.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {H.CHECKS} button/sparkle checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
