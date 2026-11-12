"""Headless checks for the appearance colours (AG-3, hud/src/lib/appearance.ts and AuroraSetting.tsx).

Runs on its own (`python tests/face/hud_v2_appearance_check.py`), in a context and against a `MockDaemon` of
its own. The terminal is played by the terminal suite's `FakePty`, so no shell runs.

What is pinned, each written to bite:
  - picking an accent changes the focus ring, the selection colour, the badge border and the stored record;
  - an amber (or red, or near-red) pick is refused: the reason shows, and nothing changes (the tokens, the
    picker's value and the stored record are all as they were);
  - a bright wash is clamped: the swatch and `--wash-2` hold the darkened colour, and a note says so;
  - Reset restores the defaults (no inline tokens left) and clears the record;
  - Monaco and xterm re-theme live, with no reload, and the orb's hue follows;
  - a saved record applies on reload, before the first paint's React tree (the tokens are set by then);
  - a hostile stored record (url(), var(), amber) is never applied.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

APPEARANCE = "jarvis.hud.appearance"
DEFAULT_ACCENT = "#6fc3df"
LAVENDER = "#c0aef5"
AMBER = "#fbbf24"


def _token(page, name):
    return page.evaluate("(n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim()", name)


def _inline(page):
    """The inline style on :root, without the window's own --ui-zoom."""
    style = page.evaluate("document.documentElement.getAttribute('style') || ''")
    return re.sub(r"--ui-zoom:[^;]*;?", "", style).strip()


def _stored(page):
    return json.loads(page.evaluate(f"localStorage.getItem('{APPEARANCE}')") or "{}")


def _rgb(hexcolour):
    h = hexcolour.lstrip("#")
    return "rgb(%d, %d, %d)" % tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _pick(page, testid, value):
    page.locator(f'[data-testid="{testid}"]').fill(value)


def appearance_checks(browser, mock, base, check, until, guard, init_script):
    from tests.face.hud_v2_editor_check import TWO, WIDE, _fresh, _open, _pane
    from tests.face.hud_v2_terminal_check import FakePty, OPEN_STUB, _attached, _created, _open_terminal, pumping

    print("\nappearance colours (AG-3)")
    errors: list[str] = []
    ctx = browser.new_context(viewport={"width": WIDE[0], "height": WIDE[1]}, permissions=["microphone"])
    guard(ctx)
    ctx.add_init_script(init_script)
    ctx.add_init_script(OPEN_STUB)
    # Observe the tokens at React's first insertion, rather than inferring early apply from a later read.
    ctx.add_init_script(r"""(() => {
      const stroke = Object.getOwnPropertyDescriptor(CanvasRenderingContext2D.prototype, 'strokeStyle');
      Object.defineProperty(CanvasRenderingContext2D.prototype, 'strokeStyle', {...stroke,
        set(value) {
          if (this.canvas.dataset.testid === 'orb') {
            const pigment = /^rgba\((\d+,\d+,\d+),/.exec(value);
            if (pigment) (window.__orbPigments ||= new Set()).add(pigment[1]);
          }
          stroke.set.call(this, value);
        }
      });
      const append = Node.prototype.appendChild;
      Node.prototype.appendChild = function(child) {
        if (this.id === 'root' && !window.__appearanceAtMount) {
          const s = getComputedStyle(document.documentElement);
          window.__appearanceAtMount = {accent: s.getPropertyValue('--accent').trim(), wash2: s.getPropertyValue('--wash-2').trim()};
        }
        return append.call(this, child);
      };
    })();""")
    fake = FakePty(ctx, mock)
    page = ctx.new_page()
    until = pumping(page, until, fake)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    try:
        page.goto(base + "/")
        page.wait_for_selector('[data-testid="sidebar"]', state="attached")
        _fresh(page, mock, until)
        page.evaluate(f"localStorage.removeItem('{APPEARANCE}')")

        # ---- the defaults ------------------------------------------------------------------------------
        check("by default nothing is set inline and the accent is today's cyan",
              _inline(page) == "" and _token(page, "--accent") == DEFAULT_ACCENT, _inline(page))

        # An editor, a terminal and the Settings page, all open together, so the re-theme is seen live.
        _open(page, until, 2, "calc.py")
        page.locator('[data-testid="editor"] .view-lines').first.click()
        tid = _open_terminal(page, mock, until, fake)
        fake.output(tid, b"\x1b[36mcyantext\x1b[0m\r\n")
        until(lambda: page.locator(".xterm-fg-6").count() > 0, timeout=4)
        cursor = "document.querySelector('.monaco-editor .cursors-layer .cursor')"
        until(lambda: page.evaluate(f"!!{cursor}"), timeout=3)
        cyan0 = page.evaluate("getComputedStyle(document.querySelector('.xterm-fg-6')).color")
        cur0 = page.evaluate(f"getComputedStyle({cursor}).backgroundColor")
        check("xterm's cyan is the default accent before a pick", cyan0 == _rgb(DEFAULT_ACCENT), cyan0)

        # ---- live re-theme (the workspace is on screen, so the terminal and Monaco are mounted) -----------------
        page.evaluate("""([k, v]) => { localStorage.setItem(k, JSON.stringify({motion: 'fluid', stir: true, activity: true, accent: v}));
          window.__orbPigments = new Set();
          window.dispatchEvent(new Event('hud-appearance-change')); }""", [APPEARANCE, LAVENDER])
        until(lambda: _token(page, "--accent") == LAVENDER, timeout=3)
        cyan1 = page.evaluate("getComputedStyle(document.querySelector('.xterm-fg-6')).color")
        check("the open terminal re-themed live (cyan is the new accent), with no reload", cyan1 == _rgb(LAVENDER), cyan1)
        until(lambda: page.evaluate(f"getComputedStyle({cursor}).backgroundColor") != cur0, timeout=3)
        cur1 = page.evaluate(f"getComputedStyle({cursor}).backgroundColor")
        check("and the open Monaco editor re-themed live (its cursor colour changed)", cur1 != cur0, f"{cur0} -> {cur1}")
        lavender_pigments = ("192,174,245", "211,200,253", "165,194,252")
        until(lambda: any(p in page.evaluate("[...window.__orbPigments]") for p in lavender_pigments), timeout=3)
        pigments = page.evaluate("[...window.__orbPigments]")
        check("the running orb re-themes live without a React update", any(p in pigments for p in lavender_pigments), str(pigments))
        page.evaluate("(k) => { localStorage.removeItem(k); window.dispatchEvent(new Event('hud-appearance-change')); }", APPEARANCE)
        until(lambda: _token(page, "--accent") == DEFAULT_ACCENT, timeout=3)
        cyan2 = page.evaluate("getComputedStyle(document.querySelector('.xterm-fg-6')).color")
        check("and back to the default again live", cyan2 == _rgb(DEFAULT_ACCENT), cyan2)

        # Another window's storage event takes the same live path, including Monaco and xterm.
        page.evaluate("""([k, v]) => { localStorage.setItem(k, JSON.stringify({accent: v}));
          window.dispatchEvent(new StorageEvent('storage', {key: k})); }""", [APPEARANCE, LAVENDER])
        until(lambda: page.evaluate("getComputedStyle(document.querySelector('.xterm-fg-6')).color") == _rgb(LAVENDER), timeout=3)
        until(lambda: page.evaluate(f"getComputedStyle({cursor}).backgroundColor") == cur1, timeout=3)
        check("a storage event also re-themes xterm and Monaco live", _token(page, "--accent") == LAVENDER)
        page.evaluate("(k) => { localStorage.removeItem(k); window.dispatchEvent(new Event('hud-appearance-change')); }", APPEARANCE)

        page.locator('[data-testid="titlebar-settings"]').click()
        page.wait_for_selector('[data-testid="appearance-accent"]')
        check("Settings has the colour pickers, the hex readouts and Reset",
              all(page.locator(f'[data-testid="appearance-{s}"]').count() == 1
                  for s in ("accent", "wash-1", "wash-2", "wash-3", "base", "reset", "preview"))
              and page.locator('[data-testid="appearance-accent-hex"]').inner_text() == DEFAULT_ACCENT)

        page.evaluate("""(k) => { localStorage.setItem(k, JSON.stringify({washes: ['#838283', '#3468c4', '#6450c4']}));
          window.dispatchEvent(new StorageEvent('storage', {key: k})); }""", APPEARANCE)
        until(lambda: page.locator('[data-testid="appearance-wash-1"]').input_value() == "#838283", timeout=3)
        _pick(page, "appearance-accent", "#ababab")
        until(lambda: page.locator('[data-testid="appearance-accent-note"]').count() > 0, timeout=3)
        check("open Settings checks the current washes after a storage event",
              page.locator('[data-testid="appearance-wash-1"]').input_value() == "#838283"
              and "Too dark" in page.locator('[data-testid="appearance-accent-note"]').inner_text()
              and _token(page, "--accent") == DEFAULT_ACCENT)
        page.evaluate("(k) => { localStorage.removeItem(k); window.dispatchEvent(new Event('hud-appearance-change')); }", APPEARANCE)
        until(lambda: page.locator('[data-testid="appearance-wash-1"]').input_value() == "#2a8fa0", timeout=3)

        # ---- an accent ---------------------------------------------------------------------------------
        _pick(page, "appearance-accent", LAVENDER)
        until(lambda: _token(page, "--accent") == LAVENDER, timeout=3)
        sel = page.evaluate("""() => { const s = document.createElement('span'); s.textContent = 'x'; document.body.appendChild(s);
          const r = document.createRange(); r.selectNodeContents(s); const g = getSelection(); g.removeAllRanges(); g.addRange(r);
          const c = getComputedStyle(s, '::selection').backgroundColor; g.removeAllRanges(); s.remove(); return c; }""")
        check("an accent pick changes the accent, the focus ring and the badge border",
              _token(page, "--focus-ring") == f"0 0 0 2px {LAVENDER}" and _token(page, "--badge-fast") == LAVENDER
              and _token(page, "--accent-line") == "rgba(192, 174, 245, 0.45)", _inline(page)[:200])
        check("and the selection colour follows it", "192, 174, 245" in sel, sel)
        check("the lighter and quieter relatives are derived, not the default's",
              _token(page, "--accent-hi") not in ("#9ad6ea", "") and _token(page, "--syn-fn") not in ("#8ec5d6", ""),
              f"{_token(page, '--accent-hi')} {_token(page, '--syn-fn')}")
        check("the pick is stored and shown in the readout",
              _stored(page).get("accent") == LAVENDER and page.locator('[data-testid="appearance-accent-hex"]').inner_text() == LAVENDER)
        # ---- refused picks -----------------------------------------------------------------------------
        before = (_inline(page), _stored(page))
        for bad in (AMBER, "#f87171", "#ff8800", "#b0ae7e", "#b1ae76"):
            _pick(page, "appearance-accent", bad)
            until(lambda: page.locator('[data-testid="appearance-accent-note"]').count() > 0, timeout=3)
            note = page.locator('[data-testid="appearance-accent-note"]').inner_text()
            check(f"an accent of {bad} is refused with a reason",
                  "amber or red" in note and note.startswith("Not applied"), note)
            check(f"and nothing changes: the tokens, the stored record and the picker keep {LAVENDER}",
                  (_inline(page), _stored(page)) == before and _token(page, "--accent") == LAVENDER
                  and page.locator('[data-testid="appearance-accent"]').input_value() == LAVENDER)
        _pick(page, "appearance-accent", "#1a3a4a")
        until(lambda: "Too dark" in (page.locator('[data-testid="appearance-accent-note"]').inner_text() or ""), timeout=3)
        check("a too-dark accent is refused with its contrast ratio and the last good value stays",
              _token(page, "--accent") == LAVENDER)
        _pick(page, "appearance-accent", "#a0c4ff")
        until(lambda: _token(page, "--accent") == "#a0c4ff", timeout=3)
        check("a good pick after a refusal clears the reason", page.locator('[data-testid="appearance-accent-note"]').count() == 0)

        # ---- washes and the base -----------------------------------------------------------------------
        _pick(page, "appearance-accent", "#ababab")
        until(lambda: _token(page, "--accent") == "#ababab", timeout=3)
        _pick(page, "appearance-wash-2", "#ffffff")
        until(lambda: page.locator('[data-testid="appearance-wash-2-note"]').count() > 0, timeout=3)
        check("a wash change rechecks the accent and refuses a contrast regression",
              "hard to read" in page.locator('[data-testid="appearance-wash-2-note"]').inner_text()
              and _token(page, "--wash-2") == "#3468c4" and _stored(page).get("washes") is None)
        _pick(page, "appearance-accent", "#a0c4ff")
        _pick(page, "appearance-wash-2", "#ffffff")
        until(lambda: _token(page, "--wash-2") not in ("#3468c4", ""), timeout=3)
        w2 = _token(page, "--wash-2")
        lum = page.evaluate("""(h) => { const l = (v) => { v /= 255; return v < 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
          const n = parseInt(h.slice(1), 16); return 0.2126 * l(n >> 16) + 0.7152 * l((n >> 8) & 255) + 0.0722 * l(n & 255); }""", w2)
        note = page.locator('[data-testid="appearance-wash-2-note"]').inner_text()
        check("a white wash is clamped (darkened, not refused): luminance at most the default brightest wash's",
              w2 != "#ffffff" and lum <= 0.2275 and note.startswith("Darkened to " + w2), f"{w2} {lum:.4f} {note}")
        check("the swatch and the readout show the clamped value",
              page.locator('[data-testid="appearance-wash-2-hex"]').inner_text() == w2
              and page.locator('[data-testid="appearance-wash-2"]').input_value() == w2
              and _stored(page)["washes"][1] == w2)
        _pick(page, "appearance-accent", "#ababab")
        until(lambda: page.locator('[data-testid="appearance-accent-note"]').count() > 0, timeout=3)
        check("an accent pick checks the current clamped washes", "Too dark" in page.locator('[data-testid="appearance-accent-note"]').inner_text()
              and _token(page, "--accent") == "#a0c4ff")
        _pick(page, "appearance-wash-1", "#ff8800")
        until(lambda: "amber or red" in (page.locator('[data-testid="appearance-wash-1-note"]').inner_text() or ""), timeout=3)
        check("an amber wash is refused and the wash stays", _token(page, "--wash-1") != "#ff8800" and _stored(page)["washes"][0] == "#2a8fa0")
        _pick(page, "appearance-wash-1", "#dbb1b9")
        until(lambda: "amber or red" in page.locator('[data-testid="appearance-wash-1-note"]').inner_text(), timeout=3)
        check("a near-grey wash whose clamped output enters the band is refused", _token(page, "--wash-1") == "#2a8fa0")
        _pick(page, "appearance-base", "#808080")
        until(lambda: _token(page, "--base") not in ("#0a1116", ""), timeout=3)
        check("a bright base is clamped dark", _token(page, "--base") != "#808080"
              and page.locator('[data-testid="appearance-base-note"]').count() == 1)
        check("the aurora layer still has its canvas, undisturbed", page.locator("#aurora canvas").count() == 1)

        # ---- reload: a saved record applies --------------------------------------------------------------
        saved = _stored(page)
        before = mock.sse_connections()
        page.reload()
        page.wait_for_selector('[data-testid="sidebar"]', state="attached")
        check("a saved record applies on reload", _token(page, "--accent") == "#a0c4ff" and _token(page, "--wash-2") == w2
              and _stored(page) == saved)
        check("the saved colours are already applied at React's first mount",
              page.evaluate("window.__appearanceAtMount") == {"accent": "#a0c4ff", "wash2": w2})
        mock.await_reconnect(before)

        before_inline = _inline(page)
        page.evaluate("""() => window.dispatchEvent(new CustomEvent('hud-appearance-change', {
          detail: {accent: 'red;background:url(x)', base: 'url(//evil/x)', washes: ['var(--x)', 'red', '#fff']}
        }))""")
        check("hostile event detail is ignored", _inline(page) == before_inline)
        page.evaluate("""(k) => { localStorage.setItem(k, JSON.stringify({accent: 'red;background:url(x)',
          base: 'url(//evil/x)', washes: ['var(--x)', 'red', '#fff']}));
          window.dispatchEvent(new Event('hud-appearance-change')); }""", APPEARANCE)
        check("a hostile stored record is safe on the live event path too", _inline(page) == "" and _token(page, "--accent") == DEFAULT_ACCENT)

        # ---- a hostile stored record ---------------------------------------------------------------------
        page.evaluate("""(k) => localStorage.setItem(k, JSON.stringify({accent: 'url(//evil/x)', base: 'var(--accent)',
          washes: ['red;background:url(x)', '#fff', '#fbbf24']}))""", APPEARANCE)
        before = mock.sse_connections()
        page.reload()
        page.wait_for_selector('[data-testid="sidebar"]', state="attached")
        check("a hostile stored record is never applied (no inline token holds url(), var() or amber)",
              "url(" not in _inline(page) and "var(" not in _inline(page) and AMBER not in _inline(page)
              and _token(page, "--accent") == DEFAULT_ACCENT, _inline(page)[:200])
        mock.await_reconnect(before)

        # ---- reset -----------------------------------------------------------------------------------------
        page.evaluate("(k) => localStorage.setItem(k, %s)" % json.dumps(json.dumps(saved)), APPEARANCE)
        before = mock.sse_connections()
        page.reload()
        page.wait_for_selector('[data-testid="sidebar"]', state="attached")
        mock.await_reconnect(before)
        page.locator('[data-testid="titlebar-settings"]').click()
        page.wait_for_selector('[data-testid="appearance-reset"]')
        page.locator('[data-testid="appearance-reset"]').click()
        until(lambda: _inline(page) == "", timeout=3)
        st = _stored(page)
        check("Reset restores the defaults (no inline token is left) and clears the stored colours",
              _inline(page) == "" and _token(page, "--accent") == DEFAULT_ACCENT and _token(page, "--wash-2") == "#3468c4"
              and not any(k in st for k in ("accent", "washes", "base")), f"{_inline(page)} {st}")
        check("the pickers show the defaults again", page.locator('[data-testid="appearance-accent"]').input_value() == DEFAULT_ACCENT)
        check("no page errors in the appearance section", not errors, "; ".join(errors[:3]))
    finally:
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
            appearance_checks(browser, mock, f"http://127.0.0.1:{mock.port}", H.check, H.until, H.guard, H.FAKE_RECOGNIZER)
            browser.close()
    finally:
        mock.stop()
    print()
    if H.FAILURES:
        print(f"{len(H.FAILURES)} of {H.CHECKS} checks FAILED:")
        for f in H.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {H.CHECKS} appearance checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
