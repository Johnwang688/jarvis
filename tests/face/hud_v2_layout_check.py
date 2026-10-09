"""Headless checks for the HUD's zoom, folding panes and resizable edges
(design §18, 2026-10-08).

Called from `hud_v2_check.main()` **first**, in a browser context of its own:
the mock hands SSE frames only to the newest `/events` connection, so a second
page beside the main one would steal its frames, and a context of its own
keeps this section's localStorage (zoom, widths, folded panes) away from the
rest of the suite.

The checks worth keeping, each written to bite:
  - at 160% and at 70%, on 1280x800 and 1024x700, an approval card is entirely
    on screen and, scrolled to its end, its buttons are the topmost thing under
    the pointer — and for a long command AUTHORIZE is *below the fold until the
    owner scrolls past the whole command* (a sticky button row would let them
    approve a `curl … | sh` tail they never saw);
  - every layout key is inert while a card is up, so nothing re-lays the card
    out under a pointer that has not moved;
  - the zoom keys are the HUD's in the input bar and in Monaco too (else they
    are Chrome's page zoom, which the control cannot see), while Ctrl+B stands
    aside in Monaco and ignores auto-repeat, composition and Ctrl+Shift+B;
  - folding a pane keeps the sidebar's expanded projects, the selected thread
    and a compose draft, and New thread stays reachable from the rail;
  - at 1024x700 and 160% the window folds both panes for the render, without
    storing it, and every tab and tool and the input stay usable;
  - a drag at 150% moves the edge by the pointer's travel in *zoomed* pixels;
  - every control in the provider · model · effort chip stays on screen and
    clickable at any width and zoom — the chip wraps, it never clips (a capped
    width with overflow hidden hid the effort select behind dictation);
  - every one of those settings survives a reload, garbage in storage opens at
    the defaults, and storage that throws on every call still opens.
"""
from __future__ import annotations

import time

LONG_COMMAND = "rm -rf /home/johnw/projects/scratch && " + " && ".join(
    f"echo step-{i} >> /tmp/zoom-check.log" for i in range(60))


def layout_checks(browser, mock, base, check, until, guard, init_script):
    print("\nzoom, folding and resizing")
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, permissions=["microphone"])
    ctx.route(guard[0], guard[1])
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    # A missing control is a failed check, not a 30-second hang per locator.
    page.set_default_timeout(5000)
    try:
        _boot(page, mock, base, until)
        for section in (_zoom_checks, _fold_checks, _resize_checks, _approval_zoom_checks,
                        _blocked_checks, _small_window_checks, _picker_zoom_checks,
                        _monaco_zoom_checks, _menu_zoom_checks, _model_chip_checks):
            try:
                section(page, mock, check, until)
            except Exception as e:  # a section that cannot run is a failure, and the rest still run
                check(f"{section.__name__.strip('_')} ran to the end", False, str(e).splitlines()[0][:200])
                try:
                    page.set_viewport_size({"width": 1280, "height": 800})
                    _set_storage(page, zoom="100", layout="{}")
                    page.keyboard.press("Escape")
                    _boot(page, mock, None, until, reload=True)
                except Exception:
                    pass
        check("no page errors in the layout section", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()
    _throwing_storage_checks(browser, mock, base, check, until, guard, init_script)


# Storage that refuses every call, as a profile with site data blocked does.
THROWING_STORAGE = """
(() => {
  const no = function () { throw new DOMException('storage is blocked', 'SecurityError'); };
  Storage.prototype.getItem = no;
  Storage.prototype.setItem = no;
  Storage.prototype.removeItem = no;
})();
"""


def _throwing_storage_checks(browser, mock, base, check, until, guard, init_script):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, permissions=["microphone"])
    ctx.route(guard[0], guard[1])
    ctx.add_init_script(init_script)
    ctx.add_init_script(THROWING_STORAGE)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    try:
        _boot(page, mock, base, until)
        threw = page.evaluate("(() => { try { localStorage.getItem('x'); return false; } catch { return true; } })()")
        check("setup: this window's storage throws", threw)
        check("with storage throwing, the HUD opens at 100% with both panes at their defaults",
              abs(_zoom(page) - 1.0) < 1e-6 and _width(page, "#sidebar") == 236 and _width(page, "#right") == 316,
              f"{_zoom(page)} {_width(page, '#sidebar')} {_width(page, '#right')}")
        page.locator('[data-testid="zoom-in"]').click()
        until(lambda: abs(_zoom(page) - 1.1) < 1e-6, timeout=2)
        _press_on_body(page, "Control+b")
        until(lambda: not _visible(page, "#sidebar"), timeout=2)
        check("and zoom and folding still work, unremembered",
              abs(_zoom(page) - 1.1) < 1e-6 and not _visible(page, "#sidebar"))
        check("without a page error", not errors, "; ".join(errors[:3]))
    except Exception as e:
        check("the throwing-storage section ran to the end", False, str(e).splitlines()[0][:200])
    finally:
        ctx.close()


# ---------------------------------------------------------------------------

def _boot(page, mock, base, until, reload=False):
    before = mock.sse_connections()
    if reload:
        page.reload()
    else:
        page.goto(base + "/")
    page.wait_for_selector('[data-testid="sidebar"]', state="attached")
    until(lambda: page.locator('[data-testid="project-p1"]').count() > 0)
    mock.await_reconnect(before)
    time.sleep(0.2)
    _probe(page)


# Registered after the app's own capture-phase listener, so it sees what that
# listener decided: whether a Ctrl key was taken from the browser.
PROBE = """
window.__keys = [];
window.addEventListener('keydown', (e) => {
  if (e.ctrlKey || e.metaKey) window.__keys.push({ key: e.key, prevented: e.defaultPrevented });
}, true);
true;
"""


def _probe(page):
    page.evaluate(PROBE)


def _last_key(page) -> dict:
    keys = page.evaluate("window.__keys || []")
    return keys[-1] if keys else {}


def _zoom(page) -> float:
    return float(page.evaluate("getComputedStyle(document.getElementById('root')).zoom") or 0)


def _width(page, sel: str) -> int:
    """A pane's width in the HUD's own pixels (offsetWidth is unzoomed)."""
    return page.evaluate(f"document.querySelector({sel!r}).offsetWidth")


def _visible(page, sel: str) -> bool:
    loc = page.locator(sel)
    return loc.count() > 0 and loc.first.is_visible()


def _set_storage(page, zoom: str | None = None, layout: str | None = None):
    page.evaluate(
        "([z, l]) => { if (z !== null) localStorage.setItem('jarvis.hud.zoom', z);"
        " if (l !== null) localStorage.setItem('jarvis.hud.layout', l); }",
        [zoom, layout])


def _press_on_body(page, keys: str):
    page.evaluate("document.activeElement && document.activeElement.blur()")
    page.keyboard.press(keys)


# ---------------------------------------------------------------------------

def _zoom_checks(page, mock, check, until):
    check("the HUD opens at 100%", abs(_zoom(page) - 1.0) < 1e-6, str(_zoom(page)))
    check("the zoom control shows 100%",
          page.locator('[data-testid="zoom-reset"]').inner_text().strip() == "100%")

    page.locator('[data-testid="zoom-in"]').click()
    until(lambda: abs(_zoom(page) - 1.1) < 1e-6, timeout=2)
    check("+ zooms in a step", abs(_zoom(page) - 1.1) < 1e-6, str(_zoom(page)))
    page.locator('[data-testid="zoom-out"]').click()
    page.locator('[data-testid="zoom-out"]').click()
    until(lambda: abs(_zoom(page) - 0.9) < 1e-6, timeout=2)
    check("− zooms out a step at a time", abs(_zoom(page) - 0.9) < 1e-6, str(_zoom(page)))

    _press_on_body(page, "Control+Equal")
    _press_on_body(page, "Control+Equal")
    until(lambda: abs(_zoom(page) - 1.1) < 1e-6, timeout=2)
    check("Ctrl+= zooms in", abs(_zoom(page) - 1.1) < 1e-6, str(_zoom(page)))
    _press_on_body(page, "Control+Minus")
    until(lambda: abs(_zoom(page) - 1.0) < 1e-6, timeout=2)
    check("Ctrl+- zooms out", abs(_zoom(page) - 1.0) < 1e-6, str(_zoom(page)))
    for _ in range(12):
        _press_on_body(page, "Control+Equal")
    until(lambda: abs(_zoom(page) - 1.6) < 1e-6, timeout=2)
    check("zoom stops at 160%", abs(_zoom(page) - 1.6) < 1e-6, str(_zoom(page)))
    check("and + is disabled there", page.locator('[data-testid="zoom-in"]').is_disabled())
    _press_on_body(page, "Control+0")
    until(lambda: abs(_zoom(page) - 1.0) < 1e-6, timeout=2)
    check("Ctrl+0 resets to 100%", abs(_zoom(page) - 1.0) < 1e-6, str(_zoom(page)))
    for _ in range(6):
        _press_on_body(page, "Control+Minus")
    until(lambda: abs(_zoom(page) - 0.7) < 1e-6, timeout=2)
    check("zoom stops at 70%", abs(_zoom(page) - 0.7) < 1e-6, str(_zoom(page)))

    # The keys are the HUD's in the input bar too: letting one through would
    # be Chrome's page zoom, invisible to the control and remembered per site.
    page.locator('[data-testid="zoom-reset"]').click()
    until(lambda: abs(_zoom(page) - 1.0) < 1e-6, timeout=2)
    box = page.locator('[data-testid="input"]')
    box.click()
    box.fill("typing here")
    page.keyboard.press("Control+Equal")
    until(lambda: abs(_zoom(page) - 1.1) < 1e-6, timeout=2)
    check("Ctrl+= in the input bar zooms the HUD", abs(_zoom(page) - 1.1) < 1e-6, str(_zoom(page)))
    check("and is taken from the browser's own zoom", _last_key(page).get("prevented") is True,
          str(_last_key(page)))
    page.keyboard.press("Control+0")
    until(lambda: abs(_zoom(page) - 1.0) < 1e-6, timeout=2)
    check("Ctrl+0 in the input bar resets it", abs(_zoom(page) - 1.0) < 1e-6, str(_zoom(page)))
    check("and the box keeps what was typed", box.input_value() == "typing here", box.input_value())
    box.fill("")

    # It persists across a reload, and the canvas follows it.
    page.locator('[data-testid="zoom-in"]').click()
    page.locator('[data-testid="zoom-in"]').click()
    page.locator('[data-testid="zoom-in"]').click()
    until(lambda: abs(_zoom(page) - 1.3) < 1e-6, timeout=2)
    _boot(page, mock, None, until, reload=True)
    check("the zoom persists across a reload", abs(_zoom(page) - 1.3) < 1e-6, str(_zoom(page)))
    check("and the control says so",
          page.locator('[data-testid="zoom-reset"]').inner_text().strip() == "130%",
          page.locator('[data-testid="zoom-reset"]').inner_text())
    orb = page.evaluate("(() => { const r = document.getElementById('orb').getBoundingClientRect();"
                        " return [r.left, r.bottom, r.width]; })()")
    check("the orb scales with it and stays in its corner",
          abs(orb[2] - 132 * 1.3) < 2 and orb[0] >= 0 and orb[1] <= 800, str(orb))
    px = page.evaluate("[document.getElementById('orb').width, window.devicePixelRatio]")
    check("the orb's canvas is drawn at the zoom, not stretched",
          px[0] == int(132 * min(px[1], 2) * 1.3), str(px))

    # Garbage in storage opens at 100%.
    for bad in ["banana", "1000", "105", "", "{}", "-30"]:
        _set_storage(page, zoom=bad)
        _boot(page, mock, None, until, reload=True)
        if abs(_zoom(page) - 1.0) > 1e-6:
            break
    check("a garbage stored zoom opens at 100%", abs(_zoom(page) - 1.0) < 1e-6,
          f"{bad!r} -> {_zoom(page)}")


def _fold_checks(page, mock, check, until):
    # Something to keep: a selected thread, and a non-active project expanded.
    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: page.evaluate("window.__hud.state().threadId") == "t1")
    page.locator('[data-testid="project-p2"]').click()
    until(lambda: page.locator('[data-testid="project-p2"] .tw').inner_text() == "▾", timeout=2)
    check("setup: p2 expanded and t1 selected",
          page.locator('[data-testid="project-p2"] .tw').inner_text() == "▾"
          and page.evaluate("window.__hud.state().threadId") == "t1")

    check("each pane has a fold button",
          _visible(page, '[data-testid="collapse-left"]') and _visible(page, '[data-testid="collapse-right"]'))
    check("and no rails while open",
          page.locator('[data-testid="rail-left"]').count() == 0
          and page.locator('[data-testid="rail-right"]').count() == 0)

    page.locator('[data-testid="collapse-left"]').click()
    until(lambda: _visible(page, '[data-testid="rail-left"]'), timeout=2)
    check("folding the left pane hides the sidebar", not _visible(page, "#sidebar"))
    check("and leaves a rail with an expand button",
          _visible(page, '[data-testid="rail-left"]') and _visible(page, '[data-testid="expand-left"]'))
    check("New thread is reachable from the rail", _visible(page, '[data-testid="rail-new-thread"]'))
    check("the rail is thin", 0 < _width(page, '[data-testid="rail-left"]') <= 40,
          str(_width(page, '[data-testid="rail-left"]')))
    check("the left edge cannot be dragged while folded",
          page.locator('[data-testid="split-left"]').count() == 0)
    orb = page.evaluate("(() => { const r = document.getElementById('orb').getBoundingClientRect();"
                        " return [r.left, r.right, r.bottom]; })()")
    check("the orb shrinks into the rail's foot, off the input bar",
          orb[1] <= 40 and orb[2] <= 800, str(orb))
    check("and is still the push-to-talk control",
          page.evaluate("(() => { const r = document.getElementById('orb').getBoundingClientRect();"
                        " return document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)"
                        "?.id; })()") == "orb")

    page.locator('[data-testid="collapse-right"]').click()
    until(lambda: _visible(page, '[data-testid="rail-right"]'), timeout=2)
    check("folding the right pane hides it", not _visible(page, "#right"))
    check("and leaves a rail with an expand button", _visible(page, '[data-testid="expand-right"]'))
    main_w = _width(page, "#main")
    check("the centre takes the room", main_w >= 1280 - 2 * 40, str(main_w))

    # A folded status pane still says an authorization is waiting.
    mock.emit("approval_requested", {
        "req_id": "zq1", "code": "ZQ01", "tool": "run_command", "args": {"command": "ls"},
        "command": "ls", "origin": "task: fold", "allowlistable": True, "timeout_s": 120,
    })
    until(lambda: _visible(page, '[data-testid="rail-approvals"]'), timeout=4)
    check("a folded status pane shows pending authorizations on its rail",
          _visible(page, '[data-testid="rail-approvals"]')
          and page.locator('[data-testid="rail-approvals"]').inner_text().strip() == "1")
    mock.emit("approval_resolved", {"req_id": "zq1", "decision": "deny"})
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)

    _boot(page, mock, None, until, reload=True)
    check("folded panes stay folded across a reload",
          _visible(page, '[data-testid="rail-left"]') and _visible(page, '[data-testid="rail-right"]')
          and not _visible(page, "#sidebar") and not _visible(page, "#right"))
    # The reload opened a new compose row; pick t1 again to test state across a fold.
    page.locator('[data-testid="expand-left"]').click()
    until(lambda: _visible(page, "#sidebar"), timeout=2)
    page.locator('[data-testid="project-p2"]').click()
    until(lambda: page.locator('[data-testid="project-p2"] .tw').inner_text() == "▾", timeout=2)
    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: page.evaluate("window.__hud.state().threadId") == "t1")

    # Ctrl+B folds and unfolds the left pane — even from the input bar, whose
    # keys do not bubble — and nothing in it is lost on the way.
    _press_on_body(page, "Control+b")
    until(lambda: not _visible(page, "#sidebar"), timeout=2)
    check("Ctrl+B folds the left pane", not _visible(page, "#sidebar"))
    page.locator('[data-testid="input"]').click()
    page.keyboard.press("Control+b")
    until(lambda: _visible(page, "#sidebar"), timeout=2)
    check("Ctrl+B unfolds it, even from the input bar", _visible(page, "#sidebar"))
    check("folding kept the expanded projects",
          page.locator('[data-testid="project-p2"] .tw').inner_text() == "▾")
    check("folding kept the selected thread",
          page.evaluate("window.__hud.state().threadId") == "t1"
          and "sel" in (page.locator('[data-testid="thread-t1"]').get_attribute("class") or ""))

    _press_on_body(page, "Control+Alt+b")
    until(lambda: _visible(page, "#right"), timeout=2)
    check("Ctrl+Alt+B unfolds the right pane", _visible(page, "#right"))
    _press_on_body(page, "Control+Alt+b")
    until(lambda: not _visible(page, "#right"), timeout=2)
    check("and folds it again", not _visible(page, "#right"))
    page.locator('[data-testid="expand-right"]').click()
    until(lambda: _visible(page, "#right"), timeout=2)

    # Ctrl+Shift+B is the browser's bookmarks bar, not ours.
    _press_on_body(page, "Control+Shift+B")
    time.sleep(0.2)
    check("Ctrl+Shift+B is left to the browser",
          _visible(page, "#sidebar") and _last_key(page).get("prevented") is False, str(_last_key(page)))
    # An auto-repeated Ctrl+B (a held key) and one mid-composition do nothing;
    # the same event without either flag does fold, so the dispatch is real.
    fire = ("init => document.body.dispatchEvent(new KeyboardEvent('keydown', Object.assign("
            "{key: 'b', code: 'KeyB', ctrlKey: true, bubbles: true, cancelable: true}, init)))")
    page.evaluate(fire, {"repeat": True})
    page.evaluate(fire, {"isComposing": True})
    time.sleep(0.2)
    check("an auto-repeated or mid-composition Ctrl+B does not fold", _visible(page, "#sidebar"))
    page.evaluate(fire, {})
    until(lambda: not _visible(page, "#sidebar"), timeout=2)
    check("while a plain one does", not _visible(page, "#sidebar"))
    page.evaluate(fire, {})
    until(lambda: _visible(page, "#sidebar"), timeout=2)

    # A compose draft survives a fold, and the rail's + starts a new thread.
    page.locator('[data-testid="new-thread"]').click()
    until(lambda: page.evaluate("window.__hud.state().compose") is not None)
    page.locator('[data-testid="project-chip-select"]').select_option("p2")
    box = page.locator('[data-testid="input"]')
    box.fill("a draft that must survive")
    page.locator('[data-testid="collapse-left"]').click()
    page.locator('[data-testid="collapse-right"]').click()
    until(lambda: _visible(page, '[data-testid="rail-right"]'), timeout=2)
    page.locator('[data-testid="expand-left"]').click()
    page.locator('[data-testid="expand-right"]').click()
    until(lambda: _visible(page, "#sidebar") and _visible(page, "#right"), timeout=2)
    compose = page.evaluate("window.__hud.state().compose")
    check("folding kept the compose row and its project",
          bool(compose) and compose.get("projectId") == "p2", str(compose))
    check("and the words typed into it", box.input_value() == "a draft that must survive", box.input_value())
    box.fill("")

    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: page.evaluate("window.__hud.state().threadId") == "t1")
    page.locator('[data-testid="collapse-left"]').click()
    until(lambda: _visible(page, '[data-testid="rail-new-thread"]'), timeout=2)
    page.locator('[data-testid="rail-new-thread"]').click()
    until(lambda: page.evaluate("window.__hud.state().compose") is not None, timeout=2)
    check("the rail's + opens a new thread",
          page.evaluate("window.__hud.state().threadId") is None
          and page.evaluate("window.__hud.state().compose") is not None)
    page.locator('[data-testid="expand-left"]').click()
    until(lambda: _visible(page, "#sidebar"), timeout=2)


def _drag(page, sel: str, dx: float, steps: int = 6):
    box = page.locator(sel).bounding_box()
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + dx, y, steps=steps)
    page.mouse.up()
    time.sleep(0.1)


def _resize_checks(page, mock, check, until):
    sep = page.locator('[data-testid="split-left"]')
    check("the left edge is a focusable separator",
          sep.get_attribute("role") == "separator"
          and sep.get_attribute("aria-orientation") == "vertical"
          and sep.get_attribute("tabindex") == "0")
    check("it reports the width", sep.get_attribute("aria-valuenow") == "236", sep.get_attribute("aria-valuenow"))
    cursor = page.evaluate("getComputedStyle(document.querySelector('[data-testid=\"split-left\"]')).cursor")
    check("with a resize cursor", cursor == "col-resize", cursor)
    check("and it takes no room", _width(page, "#sidebar") == 236, str(_width(page, "#sidebar")))

    _drag(page, '[data-testid="split-left"]', 100)
    w = _width(page, "#sidebar")
    check("dragging the left edge widens the sidebar", abs(w - 336) <= 2, str(w))
    check("and the separator follows", abs(int(sep.get_attribute("aria-valuenow")) - w) <= 1)
    _drag(page, '[data-testid="split-left"]', -600)
    check("the drag stops at the minimum", _width(page, "#sidebar") == 180, str(_width(page, "#sidebar")))
    _drag(page, '[data-testid="split-left"]', 900)
    check("and at the maximum", _width(page, "#sidebar") == 480, str(_width(page, "#sidebar")))
    _drag(page, '[data-testid="split-left"]', -100)
    w = _width(page, "#sidebar")
    _boot(page, mock, None, until, reload=True)
    check("the width persists across a reload", abs(_width(page, "#sidebar") - w) <= 1,
          f"{_width(page, '#sidebar')} vs {w}")

    page.locator('[data-testid="split-left"]').dblclick()
    until(lambda: _width(page, "#sidebar") == 236, timeout=2)
    check("double-clicking the edge resets the width", _width(page, "#sidebar") == 236,
          str(_width(page, "#sidebar")))
    _boot(page, mock, None, until, reload=True)
    check("and the reset persists", _width(page, "#sidebar") == 236, str(_width(page, "#sidebar")))

    # The keyboard: arrows move the edge, Home and End go to the ends.
    sep = page.locator('[data-testid="split-left"]')
    sep.focus()
    page.keyboard.press("ArrowRight")
    check("ArrowRight on the left separator widens by a step", _width(page, "#sidebar") == 252,
          str(_width(page, "#sidebar")))
    page.keyboard.press("Shift+ArrowLeft")
    check("Shift+ArrowLeft narrows by a bigger step", _width(page, "#sidebar") == 188,
          str(_width(page, "#sidebar")))
    page.keyboard.press("Home")
    check("Home goes to the minimum", _width(page, "#sidebar") == 180)
    page.keyboard.press("End")
    check("End goes to the maximum", _width(page, "#sidebar") == 480, str(_width(page, "#sidebar")))
    check("aria-valuenow tracks the keys", sep.get_attribute("aria-valuenow") == "480")
    check("the arrows did not reach push-to-talk", page.evaluate("window.__hud.capture.ptt") is None)
    sep.focus()
    page.keyboard.down("Space")
    time.sleep(0.15)
    ptt = page.evaluate("window.__hud.capture.ptt")
    page.keyboard.up("Space")
    check("Space on a focused separator is not push-to-talk", ptt is None, str(ptt))
    sep.dblclick()

    rsep = page.locator('[data-testid="split-right"]')
    rsep.focus()
    page.keyboard.press("ArrowLeft")
    check("ArrowLeft on the right separator widens the right pane", _width(page, "#right") == 332,
          str(_width(page, "#right")))
    _drag(page, '[data-testid="split-right"]', 100)
    # 332 - 100 = 232, under the right pane's minimum.
    check("dragging the right edge rightwards narrows it, to its minimum", _width(page, "#right") == 240,
          str(_width(page, "#right")))
    rsep.dblclick()
    until(lambda: _width(page, "#right") == 316, timeout=2)
    check("double-click resets the right pane too", _width(page, "#right") == 316, str(_width(page, "#right")))

    # At 150% the window is 853 zoomed px wide: 180 + 240 + 480 does not fit,
    # so the window folds the right pane for the render — and stores nothing.
    _set_storage(page, zoom="150")
    _boot(page, mock, None, until, reload=True)
    rail = page.locator('[data-testid="rail-right"]')
    check("at 150% on 1280px the window folds the right pane to give the centre room",
          _visible(page, '[data-testid="rail-right"]') and rail.get_attribute("data-auto") == "true"
          and not _visible(page, "#right"))
    stored = page.evaluate("localStorage.getItem('jarvis.hud.layout')") or ""
    check("without storing a fold, or forgetting the chosen widths",
          '"rightCollapsed":false' in stored and '"right":316' in stored, stored)
    check("and the centre keeps its minimum", _width(page, "#main") >= 480 - 2, str(_width(page, "#main")))
    nothing_over = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    check("nothing overflows horizontally at 150%", nothing_over <= 0, f"{nothing_over}px")
    # A drag of 60 screen px is 40 of the HUD's own pixels at 150% — well
    # inside the clamp (337), so an unconverted 60 would show.
    before = _width(page, "#sidebar")
    _drag(page, '[data-testid="split-left"]', 60)
    after = _width(page, "#sidebar")
    check("a drag at 150% follows the pointer in zoomed pixels", abs((after - before) - 40) <= 1,
          f"{before} -> {after}")
    page.locator('[data-testid="split-left"]').dblclick()
    # Opening the pane the window folded folds the other one instead.
    page.locator('[data-testid="expand-right"]').click()
    until(lambda: _visible(page, "#right"), timeout=2)
    check("opening the window-folded pane opens it, and the other folds instead",
          _visible(page, "#right") and _visible(page, '[data-testid="rail-left"]')
          and page.locator('[data-testid="rail-left"]').get_attribute("data-auto") == "true")
    page.locator('[data-testid="collapse-right"]').click()
    until(lambda: _visible(page, "#sidebar"), timeout=2)
    check("folding it by hand gives the left pane back",
          _visible(page, "#sidebar") and page.locator('[data-testid="rail-right"]').get_attribute("data-auto") is None)
    _set_storage(page, zoom="100", layout="{")
    _boot(page, mock, None, until, reload=True)
    check("a garbage stored layout opens at the defaults",
          _width(page, "#sidebar") == 236 and _width(page, "#right") == 316
          and _visible(page, "#sidebar") and _visible(page, "#right"))


def _card_on_screen(page, scroll: bool = True) -> tuple[bool, str]:
    """The card inside the viewport, and — scrolled to its end, as the owner
    must scroll to reach them — every button inside it and topmost."""
    return page.evaluate("""(scroll) => {
      const vw = innerWidth, vh = innerHeight, bad = [];
      const card = document.querySelector('[data-testid="approval-card"]');
      if (!card) return [false, 'no card'];
      if (scroll) card.scrollTop = card.scrollHeight;
      const inside = (r) => r.left >= 0 && r.top >= 0 && r.right <= vw + 0.5 && r.bottom <= vh + 0.5;
      const c = card.getBoundingClientRect();
      if (!inside(c)) bad.push('card ' + [c.left, c.top, c.right, c.bottom].map(Math.round));
      for (const b of card.querySelectorAll('.btns button')) {
        const r = b.getBoundingClientRect();
        if (!inside(r) || r.top < c.top || r.bottom > c.bottom + 0.5) bad.push(b.textContent + ' off screen');
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        if (hit !== b && !b.contains(hit)) bad.push(b.textContent + ' covered by ' + (hit && hit.className));
      }
      return [bad.length === 0, bad.join('; ')];
    }""", scroll)


def _authorize_below_fold(page) -> tuple[bool, str]:
    """Before any scrolling: the card overflows and AUTHORIZE is past its end."""
    return page.evaluate("""() => {
      const card = document.querySelector('[data-testid="approval-card"]');
      const b = card && card.querySelector('[data-testid="approval-allow"]');
      if (!b) return [false, 'no card'];
      card.scrollTop = 0;
      const c = card.getBoundingClientRect(), r = b.getBoundingClientRect();
      return [card.scrollHeight > card.clientHeight && r.top >= c.bottom - 0.5,
              'scroll ' + card.scrollHeight + '/' + card.clientHeight + ', button top ' + Math.round(r.top)
              + ' vs card bottom ' + Math.round(c.bottom)];
    }""")


def _approval_zoom_checks(page, mock, check, until):
    short = "rm -rf /home/johnw/projects/scratch && git push --force"
    cases = [
        ((1280, 800), "160", short), ((1280, 800), "70", short), ((1280, 800), "160", LONG_COMMAND),
        ((1024, 700), "160", short), ((1024, 700), "160", LONG_COMMAND), ((1024, 700), "70", LONG_COMMAND),
    ]
    for n, ((w, h), zoom, command) in enumerate(cases, 1):
        page.set_viewport_size({"width": w, "height": h})
        _set_storage(page, zoom=zoom)
        _boot(page, mock, None, until, reload=True)
        req = f"zoom-{n}"
        mock.emit("approval_requested", {
            "req_id": req, "code": f"Z{n:03d}", "tool": "run_command",
            "args": {"command": command}, "command": command, "reason": "reviewer declined",
            "layer": "human", "origin": "task: zoom", "allowlistable": True, "timeout_s": 120,
        })
        until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
        time.sleep(0.2)
        where = f"at {zoom}% on {w}x{h}" + (" (a long command)" if command == LONG_COMMAND else "")
        if command == LONG_COMMAND and zoom == "160":
            ok, why = _authorize_below_fold(page)
            check(f"{where} AUTHORIZE is below the fold until the command is scrolled past", ok, why)
        ok, why = _card_on_screen(page)
        check(f"{where} the card is fully on screen and, scrolled to its end, its buttons are clickable",
              ok, why)
        page.locator('[data-testid="approval-card"] button.deny').click()
        body = until(lambda: mock.sent("POST", f"/approvals/{req}") or None, timeout=4)
        check(f"{where} DENY answers with one click",
              bool(body) and body[-1].get("decision") == "deny", str(body[-1] if body else None))
        until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    page.set_viewport_size({"width": 1280, "height": 800})
    _set_storage(page, zoom="100")
    _boot(page, mock, None, until, reload=True)


def _blocked_checks(page, mock, check, until):
    """With a card up, no layout key moves anything — and none reaches Chrome."""
    mock.emit("approval_requested", {
        "req_id": "zb1", "code": "ZB01", "tool": "run_command", "args": {"command": "ls"},
        "command": "ls", "origin": "task: block", "allowlistable": True, "timeout_s": 120,
    })
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    time.sleep(0.2)
    before = page.locator('[data-testid="approval-card"]').bounding_box()
    page.keyboard.press("Control+Equal")
    time.sleep(0.2)
    after = page.locator('[data-testid="approval-card"]').bounding_box()
    check("while a card is up Ctrl+= does nothing", abs(_zoom(page) - 1.0) < 1e-6, f"zoom {_zoom(page)}")
    check("so the card has not moved under the pointer", before == after, f"{before} -> {after}")
    taken = [_last_key(page).get("prevented")]
    for keys in ("Control+b", "Control+Alt+b"):
        page.keyboard.press(keys)
        time.sleep(0.15)
        taken.append(_last_key(page).get("prevented"))
    check("nor does Ctrl+B or Ctrl+Alt+B", _visible(page, "#sidebar") and _visible(page, "#right"))
    for keys in ("Control+Minus", "Control+0"):
        page.keyboard.press(keys)
        time.sleep(0.1)
        taken.append(_last_key(page).get("prevented"))
    check("and none of them falls through to the browser's zoom",
          abs(_zoom(page) - 1.0) < 1e-6 and all(t is True for t in taken), str(taken))
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    _press_on_body(page, "Control+Equal")
    until(lambda: abs(_zoom(page) - 1.1) < 1e-6, timeout=2)
    check("and once it is answered they work again", abs(_zoom(page) - 1.1) < 1e-6, str(_zoom(page)))
    _press_on_body(page, "Control+0")
    until(lambda: abs(_zoom(page) - 1.0) < 1e-6, timeout=2)


def _reachable(page, sel: str) -> tuple[bool, str]:
    return page.evaluate("""(sel) => {
      const el = document.querySelector(sel);
      if (!el) return [false, 'missing'];
      const r = el.getBoundingClientRect();
      if (r.width < 4 || r.height < 4) return [false, 'collapsed ' + Math.round(r.width) + 'x' + Math.round(r.height)];
      if (r.left < 0 || r.top < 0 || r.right > innerWidth + 0.5 || r.bottom > innerHeight + 0.5)
        return [false, 'off screen ' + [r.left, r.top, r.right, r.bottom].map(Math.round)];
      // Clipped by an ancestor that hides overflow?
      for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
        const cs = getComputedStyle(p);
        if (cs.overflowX !== 'visible' || cs.overflowY !== 'visible') {
          const q = p.getBoundingClientRect();
          if (r.left < q.left - 0.5 || r.right > q.right + 0.5 || r.top < q.top - 0.5 || r.bottom > q.bottom + 0.5)
            return [false, 'clipped by ' + (p.id || p.className)];
        }
      }
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return [hit === el || el.contains(hit), 'hit ' + (hit && (hit.id || hit.className || hit.tagName))];
    }""", sel)


TOOLS = ['[data-testid="tab-chat"]', '[data-testid="tab-task"]', '[data-testid="tab-file"]',
         '[data-testid="tab-diff"]', '[data-testid="tab-preview"]', '[data-testid="profile"]',
         '[data-testid="open-model"]', '[data-testid="open-voice"]', '[data-testid="open-avatar"]',
         '[data-testid="open-settings"]', '[data-testid="input"]', '[data-testid="model-chip"]',
         '[data-testid="project-chip"]', '[data-testid="dictation"]']


def _small_window_checks(page, mock, check, until):
    page.set_viewport_size({"width": 1024, "height": 700})
    _set_storage(page, zoom="160", layout="{}")
    _boot(page, mock, None, until, reload=True)
    check("at 1024x700 and 160% the window folds both panes for the render",
          _visible(page, '[data-testid="rail-left"]') and _visible(page, '[data-testid="rail-right"]')
          and page.locator('[data-testid="rail-left"]').get_attribute("data-auto") == "true"
          and page.locator('[data-testid="rail-right"]').get_attribute("data-auto") == "true")
    stored = page.evaluate("localStorage.getItem('jarvis.hud.layout')") or ""
    check("and stores neither fold", '"leftCollapsed":false' in stored and '"rightCollapsed":false' in stored,
          stored)
    check("the centre keeps its minimum there", _width(page, "#main") >= 480, str(_width(page, "#main")))
    bad = []
    for sel in TOOLS:
        ok, why = _reachable(page, sel)
        if not ok:
            bad.append(f"{sel}: {why}")
    check("every tab, tool and input-bar control is on screen and clickable", not bad, "; ".join(bad))
    box = page.locator('[data-testid="input"]')
    width = box.bounding_box()["width"]
    box.fill("still usable")
    check("the message box is usable, not crushed", width >= 300 and box.input_value() == "still usable",
          f"{width}px")
    box.fill("")
    check("New thread is still one click away", _visible(page, '[data-testid="rail-new-thread"]'))

    page.set_viewport_size({"width": 1280, "height": 800})
    _set_storage(page, zoom="130")
    _boot(page, mock, None, until, reload=True)
    bad = [f"{sel}: {why}" for sel in TOOLS for ok, why in [_reachable(page, sel)] if not ok]
    check("at 130% on 1280x800 every tab and tool is reachable, Settings included", not bad, "; ".join(bad))
    _set_storage(page, zoom="100", layout="{}")
    _boot(page, mock, None, until, reload=True)


CHIP_CONTROLS = ['[data-testid="provider-chip-select"]', '[data-testid="model-chip-select"]',
                 '[data-testid="effort-chip-select"]', '[data-testid="provider-defaults-open"]']


def _model_chip_checks(page, mock, check, until):
    """The chip's own controls, not just the chip: the chip's centre was
    reachable while its right half was clipped away behind dictation. On
    Claude, so `default ▾` is drawn too: the widest the chip gets."""
    for (w, h), zoom, layout in (((1280, 800), "100", '{"left":480,"right":560}'),
                                 ((1280, 800), "160", "{}"),
                                 ((1024, 700), "160", "{}"),
                                 ((760, 700), "100", "{}")):
        page.set_viewport_size({"width": w, "height": h})
        _set_storage(page, zoom=zoom, layout=layout)
        _boot(page, mock, None, until, reload=True)
        until(lambda: page.locator(CHIP_CONTROLS[0]).count() > 0, timeout=3)
        page.locator(CHIP_CONTROLS[0]).select_option("claude")
        until(lambda: all(page.locator(sel).count() > 0 for sel in CHIP_CONTROLS), timeout=3)
        bad = [f"{sel}: {why}" for sel in CHIP_CONTROLS for ok, why in [_reachable(page, sel)] if not ok]
        check(f"at {w}x{h} and {zoom}% provider, model, effort and default ▾ are all on screen and clickable",
              not bad, "; ".join(bad))
    page.set_viewport_size({"width": 1280, "height": 800})
    _set_storage(page, zoom="100", layout="{}")
    _boot(page, mock, None, until, reload=True)


def _picker_zoom_checks(page, mock, check, until):
    """The pickers' 78vh cap divides by the zoom as the card's does."""
    # Settings is ~197px tall; at 160% on a 300px window an undivided 78vh
    # cap (374 zoomed px) would let it run off both edges.
    page.set_viewport_size({"width": 1280, "height": 300})
    _set_storage(page, zoom="160")
    _boot(page, mock, None, until, reload=True)
    page.locator('[data-testid="open-settings"]').click()
    until(lambda: page.locator('[data-testid="picker"] .picker').count() > 0, timeout=3)
    time.sleep(0.2)
    r = page.evaluate("""() => {
      const p = document.querySelector('[data-testid="picker"] .picker');
      const b = p.getBoundingClientRect();
      return {top: b.top, bottom: b.bottom, h: b.height, vh: innerHeight};
    }""")
    check("at 160% on a short window a dialog stays inside it",
          r["top"] >= 0 and r["bottom"] <= r["vh"] + 0.5 and r["h"] <= 0.78 * r["vh"] + 1, str(r))
    page.keyboard.press("Escape")
    page.set_viewport_size({"width": 1280, "height": 800})
    _set_storage(page, zoom="100")
    _boot(page, mock, None, until, reload=True)


def _monaco_zoom_checks(page, mock, check, until):
    _set_storage(page, zoom="150", layout="{}")
    _boot(page, mock, None, until, reload=True)
    page.locator('[data-testid="tab-file"]').click()
    until(lambda: page.locator('[data-testid="file-calc.py"]').count() > 0)
    page.locator('[data-testid="file-calc.py"]').click()
    until(lambda: page.locator('[data-testid="editor"] .view-line').count() >= 2
          or page.locator('[data-testid="editor-fallback"]').count() > 0, timeout=10)
    if page.locator('[data-testid="editor"]').count() == 0:
        check("Monaco loads at 150% (fell back to a textarea)", False)
        return
    # Focus inside Monaco, then the zoom keys: they are the HUD's here too.
    page.locator('[data-testid="editor"] .view-line').nth(0).click()
    page.keyboard.press("Control+Equal")
    until(lambda: abs(_zoom(page) - 1.6) < 1e-6, timeout=2)
    check("Ctrl+= inside Monaco zooms the HUD", abs(_zoom(page) - 1.6) < 1e-6, str(_zoom(page)))
    check("and is taken from the browser's own zoom", _last_key(page).get("prevented") is True,
          str(_last_key(page)))
    in_monaco = page.evaluate("!!document.activeElement.closest('.monaco-editor')")
    sidebar = _visible(page, "#sidebar")
    page.keyboard.press("Control+b")
    time.sleep(0.3)
    check("Ctrl+B inside Monaco is Monaco's (its Ctrl+K Ctrl+B chord), not a fold",
          in_monaco and _visible(page, "#sidebar") == sidebar, f"in monaco {in_monaco}")
    time.sleep(0.3)
    fit = page.evaluate("""() => {
      const host = document.querySelector('[data-testid="editor"]').getBoundingClientRect();
      const ed = document.querySelector('[data-testid="editor"] .monaco-editor').getBoundingClientRect();
      return [Math.abs(host.width - ed.width), Math.abs(host.height - ed.height)];
    }""")
    check("at 160% Monaco fills its host", fit[0] < 2 and fit[1] < 2, str(fit))
    # Click the second line: Monaco inverts the scale it measures, so the
    # cursor must land on the line under the pointer, not one zoom-factor off.
    line2 = page.locator('[data-testid="editor"] .view-line').nth(1).bounding_box()
    page.mouse.click(line2["x"] + 40, line2["y"] + line2["height"] / 2)
    time.sleep(0.2)
    cur = page.evaluate("""() => {
      const c = document.querySelector('[data-testid="editor"] .cursors-layer .cursor');
      return c ? c.getBoundingClientRect().top + c.getBoundingClientRect().height / 2 : null;
    }""")
    check("at 160% a click in Monaco lands on the line under the pointer",
          cur is not None and line2["y"] <= cur <= line2["y"] + line2["height"], f"{cur} vs {line2}")
    # Narrowing the sidebar relayouts the editor to the new width. (Folding
    # it would not do: at 160% that frees room and the window-folded right
    # pane comes back.)
    width = "document.querySelector('[data-testid=\"editor\"] .monaco-editor').offsetWidth"
    w0 = page.evaluate(width)
    _drag(page, '[data-testid="split-left"]', -100)
    until(lambda: page.evaluate(width) > w0 + 30, timeout=3)
    w1 = page.evaluate(width)
    check("Monaco relayouts when a pane is resized", w1 > w0 + 30, f"{w0} -> {w1}")
    page.locator('[data-testid="split-left"]').dblclick()
    _set_storage(page, zoom="100", layout="{}")
    _boot(page, mock, None, until, reload=True)


def _menu_zoom_checks(page, mock, check, until):
    """A menu is placed from a screen rect, and its `left` is scaled again by
    the zoom; unconverted, at 140% it opened 40% further out than its ⋯."""
    _set_storage(page, zoom="140")
    _boot(page, mock, None, until, reload=True)
    opener = page.locator('[data-testid="thread-menu-t1"]')
    opener.click()
    until(lambda: page.locator('[data-testid="thread-menu"]').count() > 0, timeout=2)
    at = opener.bounding_box()
    menu = page.locator('[data-testid="thread-menu"]').bounding_box()
    check("at 140% the thread menu opens under its ⋯",
          bool(menu) and abs(menu["x"] - at["x"]) <= 3 and abs(menu["y"] - (at["y"] + at["height"])) <= 3,
          f"menu {menu} vs opener {at}")
    page.keyboard.press("Escape")
    row = page.locator('[data-testid="project-p2"]').bounding_box()
    px, py = row["x"] + 30, row["y"] + row["height"] / 2
    page.mouse.click(px, py, button="right")
    until(lambda: page.locator('[data-testid="project-menu"]').count() > 0, timeout=2)
    menu = page.locator('[data-testid="project-menu"]').bounding_box()
    check("and a right-click menu opens at the pointer",
          bool(menu) and abs(menu["x"] - px) <= 3 and abs(menu["y"] - py) <= 3, f"menu {menu} vs {px},{py}")
    page.keyboard.press("Escape")
    _set_storage(page, zoom="100")
    _boot(page, mock, None, until, reload=True)
