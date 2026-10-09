"""Headless checks for the HUD's zoom, folding panes and resizable edges
(design §18, 2026-10-08).

Called from `hud_v2_check.main()` **first**, in a browser context of its own:
the mock hands SSE frames only to the newest `/events` connection, so a second
page beside the main one would steal its frames, and a context of its own
keeps this section's localStorage (zoom, widths, folded panes) away from the
rest of the suite.

The checks worth keeping, each written to bite:
  - at 160% and at 70% an approval card is entirely on screen and its buttons
    are the topmost thing under the pointer — it is the safety surface, and an
    undivided `86vh` cap put its buttons off the bottom at 160%;
  - the zoom keys stand aside in the input bar and in Monaco;
  - folding a pane keeps the sidebar's expanded projects, the selected thread
    and a compose draft, and New thread stays reachable from the rail;
  - a drag at 150% moves the edge by the pointer's travel in *zoomed* pixels;
  - every one of those settings survives a reload, and garbage in storage
    opens at the defaults.
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
                        _monaco_zoom_checks, _menu_zoom_checks):
            try:
                section(page, mock, check, until)
            except Exception as e:  # a section that cannot run is a failure, and the rest still run
                check(f"{section.__name__.strip('_')} ran to the end", False, str(e).splitlines()[0][:200])
                try:
                    _set_storage(page, zoom="100", layout="{}")
                    page.keyboard.press("Escape")
                    _boot(page, mock, None, until, reload=True)
                except Exception:
                    pass
        check("no page errors in the layout section", not errors, "; ".join(errors[:3]))
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

    # The keys stand aside while the owner is typing.
    page.locator('[data-testid="zoom-reset"]').click()
    until(lambda: abs(_zoom(page) - 1.0) < 1e-6, timeout=2)
    box = page.locator('[data-testid="input"]')
    box.click()
    box.fill("typing here")
    page.keyboard.press("Control+Equal")
    page.keyboard.press("Control+Minus")
    time.sleep(0.3)
    check("Ctrl+= does nothing while focused in the input bar",
          abs(_zoom(page) - 1.0) < 1e-6, str(_zoom(page)))
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

    # At 150% a 150px drag on screen is 100px in the HUD's own pixels. The
    # window is 853 zoomed px wide there, so with both panes open they are
    # already giving way to the centre; fold the right one to leave room.
    _set_storage(page, zoom="150")
    _boot(page, mock, None, until, reload=True)
    avail = 1280 / 1.5
    check("at 150% both panes give way to the centre, down to their minimums",
          _width(page, "#sidebar") == 180 and _width(page, "#right") == 240
          and abs(_width(page, "#main") - (avail - 420)) <= 2,
          f"{_width(page, '#sidebar')} / {_width(page, '#main')} / {_width(page, '#right')}")
    check("without forgetting the chosen widths",
          '"left":236' in (page.evaluate("localStorage.getItem('jarvis.hud.layout')") or ""))
    nothing_over = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    check("nothing overflows horizontally at 150%", nothing_over <= 0, f"{nothing_over}px")
    page.locator('[data-testid="collapse-right"]').click()
    until(lambda: _width(page, "#sidebar") == 236, timeout=2)
    check("folding the right pane gives the sidebar its width back", _width(page, "#sidebar") == 236,
          str(_width(page, "#sidebar")))
    before = _width(page, "#sidebar")
    _drag(page, '[data-testid="split-left"]', 150)
    after = _width(page, "#sidebar")
    check("a drag at 150% follows the pointer in zoomed pixels", abs((after - before) - 100) <= 2,
          f"{before} -> {after}")
    check("and the centre keeps its minimum", _width(page, "#main") >= 480 - 2, str(_width(page, "#main")))
    page.locator('[data-testid="split-left"]').dblclick()
    page.locator('[data-testid="expand-right"]').click()
    _set_storage(page, zoom="100", layout="{")
    _boot(page, mock, None, until, reload=True)
    check("a garbage stored layout opens at the defaults",
          _width(page, "#sidebar") == 236 and _width(page, "#right") == 316
          and _visible(page, "#sidebar") and _visible(page, "#right"))


def _card_on_screen(page) -> tuple[bool, str]:
    return page.evaluate("""() => {
      const vw = innerWidth, vh = innerHeight, bad = [];
      const card = document.querySelector('[data-testid="approval-card"]');
      if (!card) return [false, 'no card'];
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
    }""")


def _approval_zoom_checks(page, mock, check, until):
    n = 0
    for zoom, command in (("160", "rm -rf /home/johnw/projects/scratch && git push --force"),
                          ("70", "rm -rf /home/johnw/projects/scratch && git push --force"),
                          ("160", LONG_COMMAND)):
        n += 1
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
        long = " (a long command)" if command == LONG_COMMAND else ""
        ok, why = _card_on_screen(page)
        check(f"at {zoom}%{long} the approval card is fully on screen and its buttons are clickable", ok, why)
        page.locator('[data-testid="approval-card"] button.deny').click()
        body = until(lambda: mock.sent("POST", f"/approvals/{req}") or None, timeout=4)
        check(f"at {zoom}%{long} DENY answers with one click",
              bool(body) and body[-1].get("decision") == "deny", str(body[-1] if body else None))
        until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    _set_storage(page, zoom="100")
    _boot(page, mock, None, until, reload=True)


def _monaco_zoom_checks(page, mock, check, until):
    _set_storage(page, zoom="160")
    _boot(page, mock, None, until, reload=True)
    page.locator('[data-testid="tab-file"]').click()
    until(lambda: page.locator('[data-testid="file-calc.py"]').count() > 0)
    page.locator('[data-testid="file-calc.py"]').click()
    until(lambda: page.locator('[data-testid="editor"] .view-line').count() >= 2
          or page.locator('[data-testid="editor-fallback"]').count() > 0, timeout=10)
    if page.locator('[data-testid="editor"]').count() == 0:
        check("Monaco loads at 160% (fell back to a textarea)", False)
        return
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
    page.keyboard.press("Control+Equal")
    time.sleep(0.3)
    check("Ctrl+= does nothing inside Monaco", abs(_zoom(page) - 1.6) < 1e-6, str(_zoom(page)))
    # Folding the right pane relayouts the editor to the new width.
    w0 = page.evaluate("document.querySelector('[data-testid=\"editor\"] .monaco-editor').offsetWidth")
    page.locator('[data-testid="collapse-right"]').click()
    until(lambda: page.evaluate(
        "document.querySelector('[data-testid=\"editor\"] .monaco-editor').offsetWidth") > w0 + 50, timeout=3)
    w1 = page.evaluate("document.querySelector('[data-testid=\"editor\"] .monaco-editor').offsetWidth")
    check("Monaco relayouts when a pane folds", w1 > w0 + 50, f"{w0} -> {w1}")
    page.locator('[data-testid="expand-right"]').click()
    _set_storage(page, zoom="100")
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
