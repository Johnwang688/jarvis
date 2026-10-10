"""Headless checks for the HUD's zoom, folding panes and resizable edges
(design §18, 2026-10-08).

Called from `hud_v2_check.main()` **first**, in a browser context of its own
and against **a `MockDaemon` of its own** on an ephemeral port: the mock hands
SSE frames only to the newest `/events` connection, so a second page beside
the main one would steal its frames; a context of its own keeps this
section's localStorage (zoom, widths, folded panes, the workspace) away from
the rest of the suite; and a mock of its own keeps its saves, `/seen` posts,
approval decisions and preview hits out of the world the main suite asserts
on.

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

The workspace (2026-10-09, WP-A of docs/plans/2026-10-09-hud-workspace-plan.md):
  - the title bar is the window's top-right in every fold state: Model, Voice,
    Avatar, Settings, zoom and ⊞ ◧ ⬓ ◨, each toggle pressed exactly when its
    area is drawn open, a window-folded one marked as such, and folding the
    status pane no longer takes the zoom control with it;
  - with a card up every title-bar, fold and rail button is disabled and an
    open layout menu closes — Enter on a toggle that kept focus does nothing;
  - presets draw, resize, swap the single chat, hide a pane without
    unmounting it (a loaded preview is the same frame afterwards), keep a
    pane's preview URL across a reload, drop the unfocused pane first on a
    small window, and send sidebar clicks where the plan says;
  - **a File pane is pinned to the project it read from**: with the chat moved
    to another project, its save still goes to the first one;
  - in a 2×2 grid with Monaco and a preview frame on screen, at 160% and 70%,
    the card is wholly on screen, AUTHORIZE is below the fold for a long
    command, and every button is topmost at its own point.
"""
from __future__ import annotations

import json
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
                        _monaco_zoom_checks, _menu_zoom_checks, _model_chip_checks,
                        _titlebar_checks, _workspace_checks, _grid_card_checks, _sticky_checks,
                        _card_focus_checks, _seen_checks):
            try:
                section(page, mock, check, until)
            except Exception as e:  # a section that cannot run is a failure, and the rest still run
                check(f"{section.__name__.strip('_')} ran to the end", False, str(e).splitlines()[0][:200])
                try:
                    page.set_viewport_size({"width": 1280, "height": 800})
                    _set_storage(page, zoom="100", layout="{}")
                    _set_ws(page, None)
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


def _set_ws(page, raw: str | None):
    """The workspace store (lib/workspace.ts); None removes it."""
    page.evaluate("v => v === null ? localStorage.removeItem('jarvis.hud.workspace')"
                  " : localStorage.setItem('jarvis.hud.workspace', v)", raw)


def _ws(page) -> dict:
    return page.evaluate("window.__hud.workspace()")


def _height(page, sel: str) -> int:
    return page.evaluate(f"document.querySelector({sel!r}).offsetHeight")


def _attr(page, sel: str, name: str):
    return page.locator(sel).first.get_attribute(name)


def _active_testid(page):
    return page.evaluate("document.activeElement && document.activeElement.getAttribute('data-testid')")


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
    # Folded, the mic's mode is one button on the mini orb (it cycles).
    for sel in [('[data-testid="dictation-cycle"]' if t == '[data-testid="dictation"]' else t) for t in TOOLS]:
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


CHIP_CONTROLS = ['[data-testid="provider-chip-select"]', '[data-testid="model-chip-btn"]']
POP_CONTROLS = ['[data-testid="model-opt"][data-value=""]', '[data-testid="effort-slider"]',
                '[data-testid="provider-defaults-open"]']


def _model_chip_checks(page, mock, check, until):
    """The chip's own controls, not just the chip: the chip's centre was
    reachable while its right half was clipped away. The model and effort are
    one button now, so its popover is checked too: it opens upward inside the
    window at every zoom and size, and its first rows, effort and the provider
    default are on screen and clickable. On Claude, so `Set … default` is drawn
    too: the fullest the popover gets."""
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
        check(f"at {w}x{h} and {zoom}% the provider and the model button are on screen and clickable",
              not bad, "; ".join(bad))
        page.locator(CHIP_CONTROLS[1]).click()
        until(lambda: page.locator('[data-testid="model-pop"]').count() > 0, timeout=3)
        until(lambda: all(page.locator(sel).count() > 0 for sel in POP_CONTROLS), timeout=3)
        box = page.evaluate("""() => {
          const r = document.querySelector('[data-testid="model-pop"]').getBoundingClientRect();
          return {l: r.left, t: r.top, r: r.right, b: r.bottom, w: innerWidth, h: innerHeight};
        }""")
        check(f"at {w}x{h} and {zoom}% the popover stays inside the window",
              box["l"] >= 0 and box["t"] >= 0 and box["r"] <= box["w"] + 0.5 and box["b"] <= box["h"] + 0.5,
              str(box))
        bad = [f"{sel}: {why}" for sel in POP_CONTROLS for ok, why in [_reachable(page, sel)] if not ok]
        check(f"at {w}x{h} and {zoom}% the model, effort and default controls in it are clickable",
              not bad, "; ".join(bad))
        page.keyboard.press("Escape")
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


# ---------------------------------------------------------------------------
# the title bar and the workspace (2026-10-09, WP-A)

def _reset_all(page, mock, until, size=(1280, 800)):
    page.set_viewport_size({"width": size[0], "height": size[1]})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, None)
    _boot(page, mock, None, until, reload=True)


def _drag_y(page, sel: str, dy: float, steps: int = 6):
    box = page.locator(sel).bounding_box()
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x, y + dy, steps=steps)
    page.mouse.up()
    time.sleep(0.1)


def _open_layout_menu(page, until):
    if page.locator('[data-testid="layout-menu"]').count() == 0:
        page.locator('[data-testid="layout-customize"]').click()
    until(lambda: page.locator('[data-testid="layout-menu"]').count() > 0, timeout=2)


def _choose(page, until, preset: str):
    """A preset from ⊞'s menu, then the menu closed again (Escape)."""
    _open_layout_menu(page, until)
    page.locator(f'[data-testid="layout-preset-{preset}"]').click()
    until(lambda: page.locator('[data-testid="workspace"]').get_attribute("data-preset") == preset, timeout=2)
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="layout-menu"]').count() == 0, timeout=2)
    time.sleep(0.1)


def _pane(n: int, sel: str = "") -> str:
    return f'[data-testid="pane-{n}"]' + (f" {sel}" if sel else "")


def _ptt_on_space(page) -> object:
    """Hold Space where focus is now; what push-to-talk made of it."""
    page.keyboard.down("Space")
    time.sleep(0.15)
    ptt = page.evaluate("window.__hud.capture.ptt")
    page.keyboard.up("Space")
    return ptt


def _edit(page, until, pane: int, typed: str):
    """Type at the end of the file open in pane N, in Monaco or its fallback."""
    editor = page.locator(_pane(pane, '[data-testid="editor"] .view-lines'))
    until(lambda: editor.count() > 0
          or page.locator(_pane(pane, '[data-testid="editor-fallback"]')).count() > 0, timeout=10)
    if editor.count():
        editor.click()
        page.keyboard.press("Control+End")
        page.keyboard.type(typed)
    else:
        page.evaluate("([n, t]) => { const h = document.querySelector("
                      "`[data-testid=\"pane-${n}\"] [data-testid=\"editor-fallback\"]`);"
                      " h.value += t; h.dispatchEvent(new Event('input', {bubbles: true})); }", [pane, typed])


def _approval(mock, req: str, command: str = "ls"):
    mock.emit("approval_requested", {
        "req_id": req, "code": req[-4:].upper(), "tool": "run_command", "args": {"command": command},
        "command": command, "reason": "reviewer declined", "layer": "human", "origin": "task: workspace",
        "allowlistable": True, "timeout_s": 120,
    })


TOGGLES = ("left", "panel", "right")


def _fills(page) -> list:
    """Each toggle's area: filled (`currentColor`) when open, an outline (`none`) when closed."""
    return page.evaluate("""() => ['left', 'panel', 'right'].map(a => {
      const r = document.querySelector(`[data-testid="toggle-${a}"] svg rect:nth-of-type(2)`);
      return r && r.getAttribute('fill');
    })""")


def _titlebar_checks(page, mock, check, until):
    _reset_all(page, mock, until)
    geo = page.evaluate("""() => {
      const t = document.querySelector('[data-testid="titlebar"]'), s = document.getElementById('shell');
      return [t.offsetHeight, t.getBoundingClientRect().top, s.getBoundingClientRect().top, document.title];
    }""")
    check("the title bar is 28px across the top, the shell under it",
          geo[0] == 28 and geo[1] == 0 and abs(geo[2] - 28) < 1, str(geo))
    check("and the window's <title> is still J.A.R.V.I.S. (the desktop bridge's backstop)",
          geo[3] == "J.A.R.V.I.S.", geo[3])
    missing = [sid for sid in ("open-model", "open-voice", "open-avatar", "open-settings", "zoom-control",
                               "layout-customize", "toggle-left", "toggle-panel", "toggle-right")
               if page.locator(f'[data-testid="titlebar"] [data-testid="{sid}"]').count() != 1]
    check("Model, Voice, Avatar, Settings, zoom and the four toggles live in the title bar", not missing,
          str(missing))
    check("the zoom control left the status pane's header",
          page.locator('#right [data-testid="zoom-control"]').count() == 0)
    check("and the global buttons left the centre's header",
          page.locator('#main [data-testid="open-model"]').count() == 0)
    xs = [page.locator(f'[data-testid="{sid}"]').bounding_box()["x"]
          for sid in ("open-settings", "zoom-control", "layout-customize", "toggle-left", "toggle-panel",
                      "toggle-right")]
    check("in VS Code's order: tools │ zoom │ ⊞ ◧ ⬓ ◨", xs == sorted(xs), str(xs))
    end = page.locator('[data-testid="toggle-right"]').bounding_box()
    check("at the window's top-right", end["x"] + end["width"] >= 1280 - 16 and end["y"] < 28, str(end))
    pressed = {a: _attr(page, f'[data-testid="toggle-{a}"]', "aria-pressed") for a in TOGGLES}
    check("each toggle is pressed exactly when its area is open",
          pressed == {"left": "true", "panel": "false", "right": "true"}, str(pressed))
    check("and draws that area filled, or as an outline when closed",
          _fills(page) == ["currentColor", "none", "currentColor"], str(_fills(page)))
    titles = {a: _attr(page, f'[data-testid="toggle-{a}"]', "title") or "" for a in TOGGLES}
    check("each tooltip names its shortcut",
          "Ctrl+B" in titles["left"] and "Ctrl+Alt+B" in titles["right"] and "Ctrl+`" in titles["panel"],
          str(titles))

    # ◧ and ◨ fold and open the side panes, and say so.
    page.locator('[data-testid="toggle-left"]').click()
    until(lambda: not _visible(page, "#sidebar"), timeout=2)
    check("◧ folds the sidebar and reads closed",
          not _visible(page, "#sidebar") and _attr(page, '[data-testid="toggle-left"]', "aria-pressed") == "false"
          and _fills(page)[0] == "none")
    page.locator('[data-testid="toggle-left"]').click()
    until(lambda: _visible(page, "#sidebar"), timeout=2)
    check("and opens it again", _visible(page, "#sidebar")
          and _attr(page, '[data-testid="toggle-left"]', "aria-pressed") == "true")
    page.locator('[data-testid="toggle-right"]').click()
    until(lambda: not _visible(page, "#right"), timeout=2)
    check("◨ folds the status pane", not _visible(page, "#right")
          and _attr(page, '[data-testid="toggle-right"]', "aria-pressed") == "false")
    ok, why = _reachable(page, '[data-testid="zoom-in"]')
    check("with the status pane folded the zoom control is still on screen", ok, why)
    page.locator('[data-testid="zoom-in"]').click()
    until(lambda: abs(_zoom(page) - 1.1) < 1e-6, timeout=2)
    check("and still zooms", abs(_zoom(page) - 1.1) < 1e-6, str(_zoom(page)))
    page.locator('[data-testid="zoom-reset"]').click()
    until(lambda: abs(_zoom(page) - 1.0) < 1e-6, timeout=2)
    page.locator('[data-testid="toggle-right"]').click()
    until(lambda: _visible(page, "#right"), timeout=2)

    # ⬓: the bottom panel, an empty dock until WP-D's terminals arrive.
    panel = '[data-testid="panel"]'
    check("the panel starts hidden and takes no room",
          not _visible(page, panel) and _attr(page, panel, "data-open") == "false"
          and page.locator('[data-testid="panel-split"]').count() == 0)
    ws_h = _height(page, "#workspace")
    page.locator('[data-testid="toggle-panel"]').click()
    until(lambda: _visible(page, panel), timeout=2)
    check("⬓ shows the bottom panel and reads open",
          _visible(page, panel) and _attr(page, panel, "data-open") == "true"
          and _attr(page, '[data-testid="toggle-panel"]', "aria-pressed") == "true" and _fills(page)[1] == "currentColor")
    check("at its default 260px, taken from the panes above it",
          _height(page, panel) == 260 and abs(_height(page, "#workspace") - (ws_h - 260)) <= 2,
          f"{_height(page, panel)} / {ws_h} -> {_height(page, '#workspace')}")
    panel_box = page.locator(panel).bounding_box()
    main_box = page.locator("#main").bounding_box()
    check("between the side panes, under the panes",
          abs(panel_box["x"] - main_box["x"]) < 1 and abs(panel_box["width"] - main_box["width"]) < 1
          and abs(panel_box["y"] + panel_box["height"] - (main_box["y"] + main_box["height"])) < 1,
          f"{panel_box} in {main_box}")
    check("it says where the terminal will be", _visible(page, '[data-testid="panel-empty"]'))
    psep = page.locator('[data-testid="panel-split"]')
    check("its top edge is a horizontal separator",
          psep.get_attribute("role") == "separator" and psep.get_attribute("aria-orientation") == "horizontal")
    psep.focus()
    page.keyboard.press("ArrowUp")
    check("ArrowUp on the edge makes the panel taller by a step", _height(page, panel) == 276,
          str(_height(page, panel)))
    page.keyboard.press("Shift+ArrowDown")
    check("Shift+ArrowDown shorter by a bigger step", _height(page, panel) == 212, str(_height(page, panel)))
    psep.focus()
    check("Space on the edge is not push-to-talk", _ptt_on_space(page) is None)
    psep.dblclick()
    until(lambda: _height(page, panel) == 260, timeout=2)
    _drag_y(page, '[data-testid="panel-split"]', -100)
    check("dragging the edge up 100px makes the panel 100px taller", abs(_height(page, panel) - 360) <= 2,
          str(_height(page, panel)))
    _boot(page, mock, None, until, reload=True)
    check("the panel and its height survive a reload",
          _visible(page, panel) and abs(_height(page, panel) - 360) <= 2, str(_height(page, panel)))
    page.locator('[data-testid="panel-split"]').dblclick()
    until(lambda: _height(page, panel) == 260, timeout=2)
    _press_on_body(page, "Control+Backquote")
    until(lambda: not _visible(page, panel), timeout=2)
    check("Ctrl+` hides it", not _visible(page, panel))
    check("and is taken from the browser", _last_key(page).get("prevented") is True, str(_last_key(page)))
    page.locator('[data-testid="input"]').click()
    page.keyboard.press("Control+Backquote")
    until(lambda: _visible(page, panel), timeout=2)
    check("Ctrl+` shows it, from the input bar too", _visible(page, panel))
    page.locator('[data-testid="panel-hide"]').click()
    until(lambda: not _visible(page, panel), timeout=2)
    check("× in its header hides it", not _visible(page, panel)
          and _attr(page, '[data-testid="toggle-panel"]', "aria-pressed") == "false")

    # A pane the window folded reads closed, marked, and opening it folds the other.
    _set_storage(page, zoom="150", layout="{}")
    _boot(page, mock, None, until, reload=True)
    t = '[data-testid="toggle-right"]'
    title = _attr(page, t, "title") or ""
    check("a status pane the window folded reads closed, marked as the window's doing",
          _attr(page, t, "aria-pressed") == "false" and _attr(page, t, "data-auto") == "true"
          and "too narrow" in title and "Ctrl+Alt+B" in title, title)
    page.locator(t).click()
    until(lambda: _visible(page, "#right"), timeout=2)
    check("clicking it opens the status pane and folds the sidebar instead, as the rail does",
          _visible(page, "#right") and not _visible(page, "#sidebar")
          and _attr(page, '[data-testid="toggle-left"]', "data-auto") == "true")
    check("and nothing about it was stored",
          '"leftCollapsed":false' in (page.evaluate("localStorage.getItem('jarvis.hud.layout')") or ""))

    # ⊞'s menu: under its button at any zoom, keyboard-driven, Space inert.
    for zoom in ("100", "140"):
        _set_storage(page, zoom=zoom, layout="{}")
        _boot(page, mock, None, until, reload=True)
        btn = page.locator('[data-testid="layout-customize"]').bounding_box()
        _open_layout_menu(page, until)
        menu = page.locator('[data-testid="layout-menu"]').bounding_box()
        check(f"at {zoom}% ⊞ opens its menu right under it, right edges aligned",
              abs((menu["x"] + menu["width"]) - (btn["x"] + btn["width"])) <= 3
              and 0 <= menu["y"] - (btn["y"] + btn["height"]) <= 6, f"menu {menu} vs button {btn}")
        check(f"at {zoom}% the menu is on screen",
              menu["x"] >= 0 and menu["y"] + menu["height"] <= 800 + 0.5, str(menu))
        check(f"at {zoom}% ⊞ reads expanded",
              _attr(page, '[data-testid="layout-customize"]', "aria-expanded") == "true")
        page.keyboard.press("Escape")
        until(lambda: page.locator('[data-testid="layout-menu"]').count() == 0, timeout=2)
        check(f"at {zoom}% Escape closes it and gives focus back to ⊞",
              page.locator('[data-testid="layout-menu"]').count() == 0 and _active_testid(page) == "layout-customize",
              str(_active_testid(page)))
    _set_storage(page, zoom="100")
    _boot(page, mock, None, until, reload=True)
    _open_layout_menu(page, until)
    checked = [p for p in ("single", "cols2", "rows2", "cols3", "main2", "grid4")
               if _attr(page, f'[data-testid="layout-preset-{p}"]', "aria-checked") == "true"]
    check("six preset tiles, the current one checked", checked == ["single"]
          and page.locator('[data-testid^="layout-preset-"]').count() == 6, str(checked))
    check("the menu opens with focus on the current preset", _active_testid(page) == "layout-preset-single",
          str(_active_testid(page)))
    page.keyboard.press("ArrowRight")
    check("ArrowRight moves to the next tile", _active_testid(page) == "layout-preset-cols2", str(_active_testid(page)))
    page.keyboard.press("ArrowDown")
    check("ArrowDown to the tile below", _active_testid(page) == "layout-preset-main2", str(_active_testid(page)))
    check("and the arrows chose nothing", _attr(page, '[data-testid="workspace"]', "data-preset") == "single")
    check("Space in the menu is not push-to-talk", _ptt_on_space(page) is None)
    until(lambda: _attr(page, '[data-testid="workspace"]', "data-preset") == "main2", timeout=2)
    check("it activates the tile under focus instead",
          _attr(page, '[data-testid="workspace"]', "data-preset") == "main2")
    rows = [r for r in TOGGLES if page.locator(f'[data-testid="layout-toggle-{r}"]').count() == 1]
    keys = page.locator('[data-testid="layout-menu"] .lm-key').all_inner_texts()
    check("the menu writes the three toggles out with their shortcuts",
          len(rows) == 3 and keys == ["Ctrl+B", "Ctrl+`", "Ctrl+Alt+B"], f"{rows} {keys}")
    page.locator('[data-testid="layout-reset"]').click()
    until(lambda: _attr(page, '[data-testid="workspace"]', "data-preset") == "single", timeout=2)
    check("Reset layout goes back to one pane and closes the menu",
          page.locator('[data-testid="layout-menu"]').count() == 0)

    # With a card up nothing in the title bar, the fold buttons or the rails moves anything.
    page.locator('[data-testid="collapse-left"]').click()
    until(lambda: _visible(page, '[data-testid="rail-left"]'), timeout=2)
    _open_layout_menu(page, until)
    page.locator('[data-testid="toggle-right"]').focus()
    _approval(mock, "wsblk")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    time.sleep(0.2)
    check("an authorization card closes the layout menu", page.locator('[data-testid="layout-menu"]').count() == 0)
    live = page.evaluate("""() => Array.from(document.querySelectorAll('[data-testid="titlebar"] button'))
                            .filter(b => !b.disabled).map(b => b.getAttribute('data-testid'))""")
    check("and disables every title-bar button", not live, str(live))
    live = [sid for sid in ("expand-left", "rail-new-thread", "collapse-right")
            if not page.locator(f'[data-testid="{sid}"]').is_disabled()]
    check("and the fold and rail buttons (Enter still reached them behind the veil)", not live, str(live))
    page.keyboard.press("Enter")
    page.keyboard.press("Control+Backquote")
    time.sleep(0.2)
    check("Enter on a toggle that had focus, and Ctrl+`, do nothing behind the card",
          _visible(page, "#right") and _visible(page, '[data-testid="rail-left"]')
          and not _visible(page, '[data-testid="panel"]') and _last_key(page).get("prevented") is True)
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    check("once it is answered they work again",
          not page.locator('[data-testid="toggle-left"]').is_disabled()
          and not page.locator('[data-testid="expand-left"]').is_disabled())
    page.locator('[data-testid="expand-left"]').click()
    until(lambda: _visible(page, "#sidebar"), timeout=2)

    # The narrowest windows: the text buttons fold into ⋯, nothing else hides.
    page.set_viewport_size({"width": 700, "height": 700})
    _set_storage(page, zoom="160", layout="{}")
    _boot(page, mock, None, until, reload=True)
    check("under 560 zoomed px the four text buttons fold into ⋯",
          _visible(page, '[data-testid="titlebar-more"]') and page.locator('[data-testid="open-model"]').count() == 0)
    bad = [f"{sid}: {why}" for sid in ("titlebar-more", "zoom-out", "zoom-in", "layout-customize", "toggle-left",
                                        "toggle-panel", "toggle-right")
           for ok, why in [_reachable(page, f'[data-testid="{sid}"]')] if not ok]
    check("while ⋯, zoom and the four toggles stay on screen and clickable", not bad, "; ".join(bad))
    over = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    check("and nothing overflows sideways", over <= 0, f"{over}px")
    page.locator('[data-testid="titlebar-more"]').click()
    until(lambda: page.locator('[data-testid="titlebar-more-menu"]').count() > 0, timeout=2)
    page.locator('[data-testid="titlebar-more-menu"] [data-testid="open-settings"]').click()
    until(lambda: page.locator('[data-testid="picker"]').count() > 0, timeout=3)
    check("⋯ reaches Settings", page.locator('[data-testid="picker"]').count() > 0)
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="picker"]').count() == 0, timeout=2)
    _reset_all(page, mock, until)


def _workspace_checks(page, mock, check, until):
    _reset_all(page, mock, until, (1600, 900))
    ws = '[data-testid="workspace"]'
    check("a fresh window is one pane, today's centre",
          _attr(page, ws, "data-preset") == "single" and _attr(page, ws, "data-drawn-preset") == "single"
          and page.locator(_pane(1)).count() == 1
          and all(page.locator(_pane(n)).count() == 0 for n in (2, 3, 4))
          and page.locator('[data-testid="tab-chat"]').count() == 1
          and _attr(page, _pane(1), "data-view") == "chat")
    check("with no split chrome: no focus line, no context",
          "focused" not in (_attr(page, _pane(1), "class") or "") and page.locator(".panectx").count() == 0)

    _choose(page, until, "cols2")
    check("two side by side: a second pane, showing Preview",
          _attr(page, ws, "data-drawn-preset") == "cols2" and _visible(page, _pane(2))
          and _attr(page, _pane(2), "data-view") == "preview")
    b1 = page.locator(_pane(1)).bounding_box()
    b2 = page.locator(_pane(2)).bounding_box()
    check("beside the first, as tall, the centre halved",
          b1["x"] < b2["x"] and abs(b1["y"] - b2["y"]) < 1 and abs(b1["height"] - b2["height"]) < 1
          and abs(b1["width"] - b2["width"]) <= 3, f"{b1} {b2}")
    check("the chat is still one conversation with one box, in pane 1",
          page.locator('[data-testid="input"]').count() == 1 and page.locator(_pane(1, '[data-testid="input"]')).count() == 1)
    page.locator(_pane(2, '[data-testid="tab-preview"]')).click()
    until(lambda: _attr(page, _pane(2), "data-focused") == "true", timeout=2)
    check("a click in a pane focuses it, and only it, with a line under its header",
          _attr(page, _pane(2), "data-focused") == "true" and _attr(page, _pane(1), "data-focused") == "false"
          and "focused" in (_attr(page, _pane(2), "class") or ""))

    sep = page.locator('[data-testid="split-col-1"]')
    check("the edge between them is a vertical separator",
          sep.get_attribute("role") == "separator" and sep.get_attribute("aria-orientation") == "vertical")
    w1 = _width(page, _pane(1))
    _drag(page, '[data-testid="split-col-1"]', 100)
    check("dragging it moves it by the pointer's travel", abs(_width(page, _pane(1)) - (w1 + 100)) <= 3,
          f"{w1} -> {_width(page, _pane(1))}")
    w1 = _width(page, _pane(1))
    sep.focus()
    page.keyboard.press("ArrowLeft")
    check("ArrowLeft on it moves it a step", abs(_width(page, _pane(1)) - (w1 - 16)) <= 2,
          f"{w1} -> {_width(page, _pane(1))}")
    sep.focus()
    check("Space on it is not push-to-talk", _ptt_on_space(page) is None)
    w1 = _width(page, _pane(1))
    _boot(page, mock, None, until, reload=True)
    check("the preset and the edge survive a reload",
          _attr(page, ws, "data-preset") == "cols2" and abs(_width(page, _pane(1)) - w1) <= 2,
          f"{w1} -> {_width(page, _pane(1))}")
    _drag(page, '[data-testid="split-col-1"]', -2000)
    check("and stops short of the pane minimum", abs(_width(page, _pane(1)) - 360) <= 2, str(_width(page, _pane(1))))
    page.locator('[data-testid="split-col-1"]').dblclick()
    until(lambda: abs(_width(page, _pane(1)) - _width(page, _pane(2))) <= 2, timeout=2)
    check("a double-click makes the panes equal", abs(_width(page, _pane(1)) - _width(page, _pane(2))) <= 2)

    # One chat: choosing it elsewhere swaps.
    page.locator(_pane(2, '[data-testid="tab-chat"]')).click()
    until(lambda: _attr(page, _pane(2), "data-view") == "chat", timeout=2)
    check("choosing chat in pane 2 swaps the two panes' views",
          _attr(page, _pane(2), "data-view") == "chat" and _attr(page, _pane(1), "data-view") == "preview"
          and page.locator('[data-testid="input"]').count() == 1
          and page.locator(_pane(2, '[data-testid="input"]')).count() == 1)
    page.locator(_pane(1, '[data-testid="tab-chat"]')).click()
    until(lambda: _attr(page, _pane(1), "data-view") == "chat", timeout=2)

    # Hidden, never unmounted: the same frame after one pane and back.
    page.locator(_pane(2, '[data-testid="preview-project"]')).click()
    until(lambda: page.locator(_pane(2, "iframe")).count() > 0, timeout=4)
    page.evaluate("document.querySelector('[data-testid=\"pane-2\"] iframe').__kept = 42")
    src = page.locator(_pane(2, "iframe")).get_attribute("src")
    _choose(page, until, "single")
    check("one pane again: pane 2 is hidden, not unmounted",
          page.locator(_pane(2)).count() == 1 and not _visible(page, _pane(2))
          and page.evaluate("document.querySelector('[data-testid=\"pane-2\"] iframe').__kept") == 42)
    _choose(page, until, "cols2")
    check("and back: the very same frame, not a reload",
          _visible(page, _pane(2, "iframe"))
          and page.evaluate("document.querySelector('[data-testid=\"pane-2\"] iframe').__kept") == 42)
    _boot(page, mock, None, until, reload=True)
    until(lambda: page.locator(_pane(2, "iframe")).count() > 0, timeout=4)
    check("a pane's preview URL survives a reload, judged again and loaded",
          page.locator(_pane(2, "iframe")).get_attribute("src") == src, str(src))

    # Where a sidebar click goes.
    page.locator(_pane(2, '[data-testid="tab-preview"]')).click()
    until(lambda: _attr(page, _pane(2), "data-focused") == "true", timeout=2)
    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: page.evaluate("window.__hud.state().threadId") == "t1")
    check("a thread click goes to the pane already showing the chat",
          _attr(page, _pane(1), "data-view") == "chat" and _attr(page, _pane(2), "data-view") == "preview"
          and _attr(page, _pane(1), "data-focused") == "true")
    page.locator(_pane(2, '[data-testid="tab-preview"]')).click()
    page.locator('[data-testid="task-k1"]').click()
    until(lambda: _attr(page, _pane(2), "data-view") == "task", timeout=2)
    check("a task click, with no task or diff pane on screen, switches the focused pane",
          _attr(page, _pane(2), "data-view") == "task" and _attr(page, _pane(1), "data-view") == "chat")

    # The FileTab fix: a file pane is its project's, whatever the chat does.
    calls_before = len(mock.calls)
    page.locator(_pane(2, '[data-testid="tab-file"]')).click()
    page.locator('[data-testid="new-thread"]').click()
    until(lambda: page.evaluate("window.__hud.state().compose") is not None)
    page.locator('[data-testid="project-chip-select"]').select_option("p1")
    until(lambda: page.locator(_pane(2, '[data-testid="file-calc.py"]')).count() > 0, timeout=4)
    page.locator(_pane(2, '[data-testid="file-calc.py"]')).click()
    until(lambda: page.locator(_pane(2, '[data-testid="file-path"]')).inner_text() == "calc.py", timeout=4)
    check("opening a file pins its pane to that project", _ws(page)["ws"]["panes"][1]["projectId"] == "p1",
          str(_ws(page)["ws"]["panes"][1]))
    check("and says nothing while the chat is in the same project",
          page.locator('[data-testid="pane-2-project"]').count() == 0)
    page.locator('[data-testid="project-chip-select"]').select_option("p2")
    until(lambda: page.locator('[data-testid="pane-2-project"]').count() > 0, timeout=3)
    check("with the chat moved to another project the file stays open, and the pane says where it is",
          page.locator(_pane(2, '[data-testid="file-path"]')).inner_text() == "calc.py"
          and "jarvis" in page.locator('[data-testid="pane-2-project"]').inner_text())
    _edit(page, until, 2, "\n# pinned")
    save = page.locator(_pane(2, '[data-testid="file-save"]'))
    until(lambda: not save.is_disabled(), timeout=3)
    follow = page.locator('[data-testid="pane-2-follow"]')
    until(lambda: follow.is_disabled(), timeout=3)
    check("with an unsaved edit, follow chat waits and says why",
          follow.is_disabled() and "Save" in (follow.get_attribute("title") or ""),
          str(follow.get_attribute("title")))
    save.click()
    until(lambda: any(m == "PUT" for m, _p, _b in mock.calls[calls_before:]), timeout=4)
    puts = [p for m, p, _b in mock.calls[calls_before:] if m == "PUT"]
    check("and its save goes to the project it was read from, never the one the chat moved to",
          puts == ["/projects/p1/file"], str(puts))
    until(lambda: not follow.is_disabled(), timeout=3)
    check("once saved, follow chat is offered again", not follow.is_disabled())
    follow.click()
    until(lambda: page.locator(_pane(2, '[data-testid="file-path"]')).inner_text() == "no file open", timeout=3)
    check("follow chat unpins it: a fresh tree in the chat's project, nothing open",
          _ws(page)["ws"]["panes"][1]["projectId"] is None
          and page.locator('[data-testid="pane-2-project"]').count() == 0)

    # A project that goes away under an unsaved edit: the edit is kept, and
    # the pane asks before it lets go of it.
    until(lambda: page.locator(_pane(2, '[data-testid="file-calc.py"]')).count() > 0, timeout=4)
    page.locator(_pane(2, '[data-testid="file-calc.py"]')).click()
    until(lambda: page.locator(_pane(2, '[data-testid="file-path"]')).inner_text() == "calc.py", timeout=4)
    _edit(page, until, 2, "\n# kept")
    until(lambda: not page.locator(_pane(2, '[data-testid="file-save"]')).is_disabled(), timeout=3)
    mock.emit("project_archived", {"project_id": "p2"}, project_id="p2")
    until(lambda: page.locator('[data-testid="pane-2-gone"]').count() > 0, timeout=4)
    check("its project archived under an unsaved edit, the file stays open and the pane asks",
          page.locator(_pane(2, '[data-testid="file-path"]')).inner_text() == "calc.py"
          and page.locator('[data-testid="pane-2-gone"]').count() == 1
          and _ws(page)["ws"]["panes"][1]["projectId"] == "p2")
    dialogs: list[str] = []

    def on_dialog(d):
        dialogs.append(d.type)
        d.dismiss()

    page.on("dialog", on_dialog)
    try:
        page.evaluate("location.reload()")
    except Exception:
        pass  # a reload that went through destroys the context: the check below says so
    time.sleep(0.6)
    page.remove_listener("dialog", on_dialog)
    check("closing the window with an unsaved edit asks first (beforeunload)", dialogs == ["beforeunload"],
          str(dialogs))
    check("and dismissing that keeps the edit",
          page.locator(_pane(2, '[data-testid="file-path"]')).inner_text() == "calc.py")
    page.locator('[data-testid="pane-2-discard"]').click()
    until(lambda: page.locator(_pane(2, '[data-testid="file-path"]')).inner_text() == "no file open", timeout=3)
    check("discard edit lets it go: the pane follows the chat again",
          _ws(page)["ws"]["panes"][1]["projectId"] is None and page.locator('[data-testid="pane-2-gone"]').count() == 0)
    _boot(page, mock, None, until, reload=True)

    # Ctrl+Alt+N focuses a drawn pane, from the input bar too.
    page.locator('[data-testid="input"]').click()
    page.keyboard.press("Control+Alt+2")
    until(lambda: _attr(page, _pane(2), "data-focused") == "true", timeout=2)
    check("Ctrl+Alt+2 focuses pane 2, even from the input bar",
          _attr(page, _pane(2), "data-focused") == "true" and _last_key(page).get("prevented") is True)
    page.keyboard.press("Control+Alt+1")
    until(lambda: _attr(page, _pane(1), "data-focused") == "true", timeout=2)
    time.sleep(0.1)
    check("Ctrl+Alt+1 focuses pane 1 and puts the cursor in its message box", _active_testid(page) == "input",
          str(_active_testid(page)))
    page.keyboard.press("Control+Alt+3")
    time.sleep(0.15)
    check("Ctrl+Alt+3 does nothing in two columns and is left to the browser",
          _attr(page, _pane(1), "data-focused") == "true" and _last_key(page).get("prevented") is False,
          str(_last_key(page)))

    # Three columns, then a window too small for them.
    # 1600 < 236 + 316 + 1080, but the minimums fit (180 + 240 + 1080): the
    # side panes give back their slack and stay open, as for one pane.
    _choose(page, until, "cols3")
    sides = _ws(page)["fit"]["sides"]
    check("three side by side at 1600px, the side panes shrunk to make room",
          _attr(page, ws, "data-drawn-preset") == "cols3" and all(_visible(page, _pane(n)) for n in (1, 2, 3))
          and not sides["leftFolded"] and not sides["rightFolded"]
          and sides["left"] + sides["right"] + 1080 <= 1600 and sides["left"] < 236,
          f"{_attr(page, ws, 'data-drawn-preset')} {sides}")
    page.keyboard.press("Control+Alt+3")
    until(lambda: _attr(page, _pane(3), "data-focused") == "true", timeout=2)
    page.set_viewport_size({"width": 1280, "height": 800})
    _set_storage(page, zoom="150")
    _boot(page, mock, None, until, reload=True)
    check("at 150% on 1280px three columns draw as two, with a badge on ⊞",
          _attr(page, ws, "data-drawn-preset") == "cols2"
          and page.locator('[data-testid="layout-badge"]').inner_text().strip() == "1")
    check("and the focused pane is never the one dropped",
          _visible(page, _pane(3)) and _visible(page, _pane(2)) and not _visible(page, _pane(1)))
    _open_layout_menu(page, until)
    note = page.locator('[data-testid="layout-dropped"]')
    check("the menu says so", note.count() == 1 and "Showing 2 of 3 panes" in note.inner_text()
          and "too narrow" in note.inner_text(), note.inner_text() if note.count() else "")
    page.keyboard.press("Escape")
    check("a narrow split pane shows `view ▾` instead of the five words",
          page.locator(_pane(2, '[data-testid="pane-2-view-select"]')).count() == 1
          and page.locator(_pane(2, '[data-testid="tab-chat"]')).count() == 0)
    page.locator('[data-testid="pane-3-view-select"]').select_option("task")
    until(lambda: _attr(page, _pane(3), "data-view") == "task", timeout=2)
    check("and choosing from it switches the pane", _attr(page, _pane(3), "data-view") == "task")
    stored = json.loads(page.evaluate("localStorage.getItem('jarvis.hud.workspace')") or "{}")
    check("without storing the drop", stored.get("preset") == "cols3", str(stored.get("preset")))

    page.set_viewport_size({"width": 1600, "height": 900})
    _set_storage(page, zoom="100")
    _boot(page, mock, None, until, reload=True)
    _choose(page, until, "rows2")
    r1 = page.locator(_pane(1)).bounding_box()
    r2 = page.locator(_pane(2)).bounding_box()
    check("two stacked", abs(r1["x"] - r2["x"]) < 1 and r1["y"] < r2["y"] and abs(r1["width"] - r2["width"]) < 1,
          f"{r1} {r2}")
    row = page.locator('[data-testid="split-row"]')
    check("with a horizontal edge between them", row.get_attribute("aria-orientation") == "horizontal")
    h1 = _height(page, _pane(1))
    _drag_y(page, '[data-testid="split-row"]', 60)
    check("dragging it down grows the top pane by the pointer's travel", abs(_height(page, _pane(1)) - (h1 + 60)) <= 3,
          f"{h1} -> {_height(page, _pane(1))}")
    page.locator('[data-testid="split-row"]').dblclick()

    _choose(page, until, "grid4")
    boxes = [page.locator(_pane(n)).bounding_box() for n in (1, 2, 3, 4)]
    check("2×2: four panes, reading left to right, top to bottom",
          all(boxes) and boxes[0]["x"] < boxes[1]["x"] and boxes[0]["y"] < boxes[2]["y"]
          and abs(boxes[2]["x"] - boxes[0]["x"]) < 1 and abs(boxes[3]["y"] - boxes[2]["y"]) < 1, str(boxes))
    check("an edge down and one across",
          page.locator('[data-testid="split-col-1"]').count() == 1 and page.locator('[data-testid="split-row"]').count() == 1)
    _choose(page, until, "main2")
    m = [page.locator(_pane(n)).bounding_box() for n in (1, 2, 3)]
    check("one large and two stacked",
          abs(m[0]["height"] - (m[1]["height"] + m[2]["height"])) <= 2 and abs(m[1]["x"] - m[2]["x"]) < 1
          and m[1]["x"] > m[0]["x"], str(m))

    views_before = [p["view"] for p in _ws(page)["ws"]["panes"]]
    _open_layout_menu(page, until)
    page.locator('[data-testid="layout-reset"]').click()
    until(lambda: _attr(page, ws, "data-preset") == "single", timeout=2)
    check("Reset layout: one pane, default widths, the panel closed",
          _width(page, "#sidebar") == 236 and _width(page, "#right") == 316 and not _visible(page, '[data-testid="panel"]'))
    check("and keeps what each pane showed", [p["view"] for p in _ws(page)["ws"]["panes"]] == views_before,
          str(views_before))

    # The workspace and the side-pane layout are stored apart: damage to one never resets the other.
    _set_storage(page, layout="{")
    _set_ws(page, json.dumps({"preset": "cols2"}))
    _boot(page, mock, None, until, reload=True)
    check("a garbage side-pane layout leaves the workspace alone",
          _attr(page, ws, "data-preset") == "cols2" and _width(page, "#sidebar") == 236)
    _set_storage(page, layout='{"left":300}')
    _set_ws(page, "{not json")
    _boot(page, mock, None, until, reload=True)
    check("and a garbage workspace opens one pane, leaving the widths alone",
          _attr(page, ws, "data-preset") == "single" and _width(page, "#sidebar") == 300)
    _reset_all(page, mock, until)


def _grid_card_checks(page, mock, check, until):
    """A 2×2 grid with Monaco and a preview frame under the card (the
    terminal joins it with WP-D): the card stays global and on top."""
    grid = json.dumps({"preset": "grid4", "panes": [{"view": "chat"}, {"view": "file"}, {"view": "preview"},
                                                     {"view": "task"}]})
    for zoom in ("160", "70"):
        page.set_viewport_size({"width": 1280, "height": 800})
        _set_storage(page, zoom=zoom, layout="{}")
        _set_ws(page, grid)
        _boot(page, mock, None, until, reload=True)
        check(f"at {zoom}% on 1280x800 a 2×2 grid is drawn",
              _attr(page, '[data-testid="workspace"]', "data-drawn-preset") == "grid4")
        page.locator(_pane(2, '[data-testid="file-calc.py"]')).click()
        until(lambda: page.locator(_pane(2, '[data-testid="editor"] .view-line')).count() > 0
              or page.locator(_pane(2, '[data-testid="editor-fallback"]')).count() > 0, timeout=10)
        page.locator(_pane(3, '[data-testid="preview-project"]')).click()
        until(lambda: page.locator(_pane(3, "iframe")).count() > 0, timeout=4)
        check(f"at {zoom}% Monaco and a preview frame are on screen",
              _visible(page, _pane(2, '[data-testid="editor"]')) and _visible(page, _pane(3, "iframe")))
        req = f"grid{zoom}"
        _approval(mock, req, LONG_COMMAND)
        until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
        time.sleep(0.2)
        if zoom == "160":
            ok, why = _authorize_below_fold(page)
            check(f"at {zoom}% over the grid, AUTHORIZE is below the fold until the command is scrolled past",
                  ok, why)
        ok, why = _card_on_screen(page)
        check(f"at {zoom}% over the grid, the card is wholly on screen and every button topmost at its point",
              ok, why)
        page.locator('[data-testid="approval-card"] button.deny').click()
        until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    _reset_all(page, mock, until)


def _sticky_checks(page, mock, check, until):
    """A dropped set stays put while focus moves inside it (review of PR #26):
    the pair three-columns-drawn-as-two shows used to be re-chosen from the
    focused pane on every click, so clicking the other pane redrew a
    different pair and the pane under the pointer jumped."""
    ws = '[data-testid="workspace"]'
    three = json.dumps({"preset": "cols3", "focused": 3,
                        "panes": [{"view": "chat"}, {"view": "preview"}, {"view": "file"}, {"view": "task"}]})
    page.set_viewport_size({"width": 1280, "height": 800})
    _set_storage(page, zoom="150", layout="{}")
    _set_ws(page, three)
    _boot(page, mock, None, until, reload=True)
    check("setup: three columns at 150% draw as two, the focused pane 3 with pane 2",
          _attr(page, ws, "data-drawn-preset") == "cols2" and _visible(page, _pane(2)) and _visible(page, _pane(3))
          and not _visible(page, _pane(1)))
    before = [page.locator(_pane(n)).bounding_box() for n in (2, 3)]
    url = page.locator(_pane(2, '[data-testid="preview-url"]'))
    url.click()
    page.keyboard.type("http://localhost:5173/")
    time.sleep(0.2)
    after = [page.locator(_pane(n)).bounding_box() for n in (2, 3)]
    check("clicking into the other drawn pane focuses it and leaves both panes where they are",
          _attr(page, _pane(2), "data-focused") == "true" and before == after
          and _visible(page, _pane(3)) and not _visible(page, _pane(1)), f"{before} -> {after}")
    check("and what was typed there stayed there", url.input_value() == "http://localhost:5173/", url.input_value())
    page.keyboard.press("Control+Alt+1")
    until(lambda: _visible(page, _pane(1)), timeout=2)
    check("focusing a pane the window was not drawing (Ctrl+Alt+1) brings it in",
          _visible(page, _pane(1)) and _attr(page, _pane(1), "data-focused") == "true"
          and _attr(page, ws, "data-drawn-preset") == "cols2")

    # One large plus two stacked, too short for the stack: two columns.
    main = json.dumps({"preset": "main2", "focused": 3,
                       "panes": [{"view": "chat"}, {"view": "preview"}, {"view": "file"}, {"view": "task"}]})
    page.set_viewport_size({"width": 1600, "height": 420})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, main)
    _boot(page, mock, None, until, reload=True)
    check("setup: one large plus two stacked on a short window draws the large pane and the focused one",
          _attr(page, ws, "data-drawn-preset") == "cols2" and _visible(page, _pane(1)) and _visible(page, _pane(3))
          and not _visible(page, _pane(2)))
    before = [page.locator(_pane(n)).bounding_box() for n in (1, 3)]
    page.locator('[data-testid="input"]').click()
    time.sleep(0.2)
    after = [page.locator(_pane(n)).bounding_box() for n in (1, 3)]
    check("clicking into the large pane leaves the pair as it was",
          _attr(page, _pane(1), "data-focused") == "true" and before == after
          and _visible(page, _pane(3)) and not _visible(page, _pane(2)), f"{before} -> {after}")
    _reset_all(page, mock, until)


def _card_focus_checks(page, mock, check, until):
    """Keys in a focused preview frame went to the framed page, card or no
    card, and Monaco kept its focus under one (review of PR #26). A card now
    takes focus off both, onto itself — never onto a button."""
    two = json.dumps({"preset": "cols2", "focused": 2,
                      "panes": [{"view": "chat"}, {"view": "preview"}, {"view": "file"}, {"view": "task"}]})
    page.set_viewport_size({"width": 1600, "height": 900})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, two)
    _boot(page, mock, None, until, reload=True)
    page.locator(_pane(2, '[data-testid="preview-project"]')).click()
    until(lambda: page.locator(_pane(2, "iframe")).count() > 0, timeout=4)
    time.sleep(0.3)
    page.locator(_pane(2, "iframe")).click()
    until(lambda: page.evaluate("document.activeElement && document.activeElement.tagName") == "IFRAME", timeout=2)
    check("setup: keys are going into the preview frame",
          page.evaluate("document.activeElement.tagName") == "IFRAME")
    _approval(mock, "frm1")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    time.sleep(0.2)
    focus = page.evaluate("[document.activeElement.tagName, document.activeElement.getAttribute('data-testid')]")
    check("a card takes focus out of the frame, onto the card and not a button", focus == ["DIV", "approval-card"],
          str(focus))
    page.keyboard.press("Escape")
    body = until(lambda: mock.sent("POST", "/approvals/frm1") or None, timeout=4)
    check("so Escape reaches it and denies", bool(body) and body[-1].get("decision") == "deny",
          str(body[-1] if body else None))
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    time.sleep(0.1)
    check("and once it is answered, focus goes back to the frame",
          page.evaluate("document.activeElement.tagName") == "IFRAME")

    page.locator(_pane(2, '[data-testid="tab-file"]')).click()
    until(lambda: page.locator(_pane(2, '[data-testid="file-calc.py"]')).count() > 0, timeout=4)
    page.locator(_pane(2, '[data-testid="file-calc.py"]')).click()
    editor = page.locator(_pane(2, '[data-testid="editor"] .view-lines'))
    until(lambda: editor.count() > 0, timeout=10)
    if not editor.count():
        check("Monaco loads in a split pane (fell back to a textarea)", False)
        _reset_all(page, mock, until)
        return
    editor.click()
    page.keyboard.press("Control+End")
    in_monaco = page.evaluate("!!document.activeElement.closest('.monaco-editor')")
    _approval(mock, "mon1")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    time.sleep(0.2)
    page.keyboard.type("zzqq")
    time.sleep(0.2)
    text = editor.inner_text()
    check("with Monaco focused, a card takes the keys: nothing typed reaches the buffer behind it",
          in_monaco and "zzqq" not in text
          and page.locator(_pane(2, '[data-testid="file-save"]')).is_disabled(), text[-40:])
    page.keyboard.press("Escape")
    body = until(lambda: mock.sent("POST", "/approvals/mon1") or None, timeout=4)
    check("and Escape denies", bool(body) and body[-1].get("decision") == "deny")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    time.sleep(0.1)
    check("and focus goes back to the editor",
          page.evaluate("!!document.activeElement.closest('.monaco-editor')"))
    _reset_all(page, mock, until)


def _seen_checks(page, mock, check, until):
    """"Read" is a thread or task shown in **any** drawn pane, not only the
    focused one (review of PR #26: a focused-pane-only mutation passed every
    check before these)."""
    seen = lambda path: len(mock.posted(path))  # noqa: E731
    two = json.dumps({"preset": "cols2", "focused": 1,
                      "panes": [{"view": "preview"}, {"view": "chat"}, {"view": "file"}, {"view": "task"}]})
    page.set_viewport_size({"width": 1600, "height": 900})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, two)
    _boot(page, mock, None, until, reload=True)
    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: page.evaluate("window.__hud.state().threadId") == "t1")
    page.locator(_pane(1, '[data-testid="tab-preview"]')).click()
    until(lambda: _attr(page, _pane(1), "data-focused") == "true", timeout=2)
    check("setup: t1's chat in pane 2, pane 1 focused",
          _attr(page, _pane(2), "data-view") == "chat" and _attr(page, _pane(2), "data-focused") == "false")
    before = seen("/threads/t1/seen")
    mock.activity("thread", "t1", "unread")
    until(lambda: seen("/threads/t1/seen") > before, timeout=4)
    check("a thread finishing in a chat pane that is drawn but not focused is read",
          seen("/threads/t1/seen") > before)

    _choose(page, until, "single")
    check("setup: one pane, showing Preview; the chat is hidden",
          _attr(page, _pane(1), "data-view") == "preview" and not _visible(page, _pane(2)))
    before = seen("/threads/t1/seen")
    mock.activity("thread", "t1", "unread")
    time.sleep(0.8)
    check("a thread finishing while its chat is hidden is not read", seen("/threads/t1/seen") == before)
    mock.activity("thread", "t1", "idle")

    # The same for a task.
    _choose(page, until, "cols2")
    page.locator(_pane(1, '[data-testid="tab-preview"]')).click()
    page.locator('[data-testid="task-k1"]').click()
    until(lambda: _attr(page, _pane(1), "data-view") == "task", timeout=2)
    page.locator(_pane(2, '[data-testid="tab-chat"]')).click()
    until(lambda: _attr(page, _pane(2), "data-focused") == "true", timeout=2)
    check("setup: the task in pane 1, pane 2 focused",
          _attr(page, _pane(1), "data-view") == "task" and _attr(page, _pane(1), "data-focused") == "false")
    before = seen("/tasks/k1/seen")
    mock.activity("task", "k1", "unread")
    until(lambda: seen("/tasks/k1/seen") > before, timeout=4)
    check("a task finishing in a task pane that is drawn but not focused is read", seen("/tasks/k1/seen") > before)

    _choose(page, until, "single")
    page.locator(_pane(1, '[data-testid="tab-chat"]')).click()
    until(lambda: _attr(page, _pane(1), "data-view") == "chat", timeout=2)
    check("setup: one pane showing the chat; the task is hidden",
          _attr(page, _pane(2), "data-view") == "task" and not _visible(page, _pane(2)))
    before = seen("/tasks/k1/seen")
    mock.activity("task", "k1", "unread")
    time.sleep(0.8)
    check("a task finishing while no pane shows it is not read", seen("/tasks/k1/seen") == before)
    mock.activity("task", "k1", "idle")
    _reset_all(page, mock, until)
