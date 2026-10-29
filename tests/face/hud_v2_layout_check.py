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
    the pointer — and, since AS (the owner's request of 2026-10-28, which
    supersedes the old "AUTHORIZE below the fold for a long command" rule),
    the buttons come first and stay in reach unscrolled, for a long command
    too, sticky at the top of the card as it scrolls;
  - every layout key is inert while a card is up, so nothing re-lays the card
    out under a pointer that has not moved;
  - the zoom keys are the HUD's in the input bar and in Monaco too (else they
    are Chrome's page zoom, which the control cannot see); Ctrl+B folds from
    Monaco too except as the second key of its Ctrl+K chord (ED-1, decisions
    E-10), and ignores auto-repeat, composition and Ctrl+Shift+B;
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
  - presets draw, resize, make a second chat with its own box (WP-B lifted
    WP-A's single-chat swap), hide a pane without
    unmounting it (a loaded preview is the same frame afterwards), keep a
    pane's preview URL across a reload, drop the unfocused pane first on a
    small window, and send sidebar clicks where the plan says;
  - **a File pane is pinned to the project it read from**: with the chat moved
    to another project, its save still goes to the first one;
  - in a 2×2 grid with Monaco and a preview frame on screen, at 160% and 70%,
    the card is wholly on screen, AUTHORIZE is in reach unscrolled for a long
    command (AS), and every button is topmost at its own point;
  - (ED-1) with Monaco's context menu or suggest list open, at 100% and 160%,
    a card that arrives is topmost at every button, the editor behind it is
    unfocused and read-only, a typed `y` and Ctrl+S do nothing, and Escape
    denies;
  - (ED-1, review of PR #43) Ctrl+S saves the editor it is pressed in and
    nothing else — not after that editor went away, not another pane's — an
    editor created under a card is read-only, a save answered after another
    file opened leaves that file alone, a key outside the editor does not end
    Monaco's Ctrl+K chord, and the context menu opens at the pointer flipped
    up or left at 70% and 160% and stays on screen in a short window.

A render error (review of PR #29):
  - three clicks in one task on ⊞, and on ⋯ when the title bar is folded,
    leave the window drawn with no page error and the menu open — the menu's
    position used to be read from the click inside a state updater, which
    React ran at render time with `currentTarget` null, and the throw
    unmounted the whole root, an open approval card included;
  - a workspace child that throws while a card is up (a test-only probe)
    leaves a Reload prompt in the window's place and the card on screen,
    readable, its buttons topmost, Tab kept on it and Escape denying;
  - (review of PR #32) the prompt is inert behind a card whichever came
    first, and says a reload loses what was not sent; after a crash nothing
    heard is sent — a transcript landing after it, speech in the follow-up
    window or after a wake hit — the wake recognizer stops, and Space on
    Reload presses Reload and never starts the mic; a dialog that throws is
    closed and the window carries on.
"""
from __future__ import annotations

import json
import threading
import time

LONG_COMMAND = "rm -rf /home/johnw/projects/scratch && " + " && ".join(
    f"echo step-{i} >> /tmp/zoom-check.log" for i in range(60))


from tests.face.hud_v2_check import open_settings
try:  # speak only once the capture is listening (the dictation flake)
    from tests.face.hud_voice import speak_when_listening
except ImportError:  # run as a script from tests/face
    from hud_voice import speak_when_listening  # type: ignore[no-redef]

def layout_checks(browser, mock, base, check, until, guard, init_script):
    print("\nzoom, folding and resizing")
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, permissions=["microphone"])
    guard(ctx)                      # live ports refused, HTTP and WebSocket alike
    ctx.add_init_script(init_script)
    # ⬓ and Ctrl+` open a terminal when there is none, and the 2×2 grid shows one:
    # the PTY is played in the browser (no shell), on this mock's own port.
    from tests.face.hud_v2_terminal_check import FakePty
    mock.fake_pty = FakePty(ctx, mock)
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
                        _card_focus_checks, _editor_card_checks, _save_scope_checks, _late_save_checks,
                        _chord_checks,
                        _menu_place_checks, _seen_checks, _eight_checks, _split_buttons_checks, _split_room_checks,
                        _split_twelve_checks, _split_close_checks, _split_close_one_checks,
                        _no_remount_checks, _twelve_ids_checks, _split_file_checks, _crash_checks):
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
    guard(ctx)
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


def _leaves(node) -> list:
    """A stored v2 tree's panes in reading order (leaves are bare numbers, §8.1)."""
    if isinstance(node, bool) or node is None:
        return []
    if isinstance(node, int):
        return [node]
    if isinstance(node, dict):
        return [n for c in node.get("children") or [] for n in _leaves(c)]
    return []


def _pane_edges(page) -> list:
    """The test ids of the separators between the centre's panes (not the side panes' or the panel's), sorted."""
    return sorted(page.evaluate("""() => [...document.querySelectorAll('[role="separator"][data-testid^="split-"]')]
      .map(e => e.getAttribute('data-testid')).filter(t => t !== 'split-left' && t !== 'split-right')"""))


def _stored_order(stored: dict) -> list:
    """The stored drawn order: the v2 tree's leaves (FS3), else a v1 value's `order`."""
    return _leaves(stored["tree"]) if stored.get("version") == 2 and "tree" in stored else stored.get("order")


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
    check("Space on a focused separator never starts the mic", ptt is None, str(ptt))
    # Push-to-talk's Ctrl+D (2026-10-10) works from a focused separator, and
    # moves nothing.
    width = _width(page, "#sidebar")
    sep.focus()
    held, after, key = _ptt_on_ctrl_d(page)
    check("Ctrl+D held on a focused separator starts and stops push-to-talk, and moves nothing",
          held is not None and after is None and key.get("prevented") is True and _width(page, "#sidebar") == width,
          f"{held} {after} {key}")
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


def _authorize_in_reach(page) -> tuple[bool, str]:
    """Before any scrolling (AS-12): the buttons come first, so every one is
    inside the card's visible box and topmost at its own point, however long
    the command."""
    return page.evaluate("""() => {
      const card = document.querySelector('[data-testid="approval-card"]');
      if (!card) return [false, 'no card'];
      card.scrollTop = 0;
      const c = card.getBoundingClientRect(), bad = [];
      for (const b of card.querySelectorAll('.btns button')) {
        const r = b.getBoundingClientRect();
        if (r.top < c.top - 0.5 || r.bottom > c.bottom + 0.5) bad.push(b.textContent + ' outside the card');
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        if (hit !== b && !b.contains(hit)) bad.push(b.textContent + ' covered by ' + (hit && hit.className));
      }
      return [bad.length === 0, bad.join('; ') || 'scroll ' + card.scrollHeight + '/' + card.clientHeight];
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
            ok, why = _authorize_in_reach(page)
            check(f"{where} AUTHORIZE is in reach before any scrolling (AS-12)", ok, why)
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
         '[data-testid="tab-diff"]', '[data-testid="tab-browser"]', '[data-testid="profile"]',
         '[data-testid="titlebar-settings"]', '[data-testid="input"]', '[data-testid="model-chip"]',
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
    # A schedule dialog at 160% on a 300px window: an undivided 78vh
    # cap (374 zoomed px) would let it run off both edges.
    page.set_viewport_size({"width": 1280, "height": 300})
    _set_storage(page, zoom="160")
    _boot(page, mock, None, until, reload=True)
    page.evaluate("window.__hud.dispatch({type: 'patch', patch: {picker: 'schedule'}})")
    until(lambda: page.locator('[data-testid="schedule-dialog"]').count() > 0, timeout=3)
    time.sleep(0.2)
    r = page.evaluate("""() => {
      const p = document.querySelector('[data-testid="schedule-dialog"]');
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
    # Ctrl+B folds the sidebar from Monaco too, except as the second key of
    # its Ctrl+K chord (decisions E-10; it used to stand aside entirely).
    page.keyboard.press("Control+k")
    page.keyboard.press("Control+b")
    time.sleep(0.3)
    check("Ctrl+K Ctrl+B inside Monaco is Monaco's chord, not a fold",
          in_monaco and sidebar and _visible(page, "#sidebar"), f"in monaco {in_monaco}")
    page.keyboard.press("Control+b")
    until(lambda: not _visible(page, "#sidebar"), timeout=2)
    check("and a lone Ctrl+B inside Monaco folds the sidebar", not _visible(page, "#sidebar"))
    page.keyboard.press("Control+b")
    until(lambda: _visible(page, "#sidebar"), timeout=2)
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



def _project_root(page, pane: int) -> None:
    """Use the owner options gesture, then uncover the frame for later checks."""
    button = page.locator(_pane(pane, '[data-testid="browser-options-toggle"]'))
    panel = page.locator(_pane(pane, '[data-testid="browser-options"]'))
    if button.get_attribute("aria-expanded") != "true":
        button.click()
    panel.wait_for(state="visible")
    page.locator(_pane(pane, '[data-testid="browser-project"]')).click()
    if button.get_attribute("aria-expanded") == "true":
        button.click()
    panel.wait_for(state="hidden")


def _ptt_on_space(page) -> object:
    """Hold Space where focus is now; what push-to-talk made of it (Space is
    no key of push-to-talk's since 2026-10-10: it must never start the mic)."""
    page.keyboard.down("Space")
    time.sleep(0.15)
    ptt = page.evaluate("window.__hud.capture.ptt")
    page.keyboard.up("Space")
    return ptt


# Push-to-talk's key is Ctrl+D (2026-10-10, hud/src/lib/talkkey.ts). Every
# keydown of D or Space is kept, so a check can read afterwards whether the
# window took it from the browser: `defaultPrevented` is final once the event
# has been dispatched, whichever capture listener ran first (an init script
# or this, run again after a reload).
TALK_PROBE = """
if (!window.__talkKeys) {
  window.__talkKeys = [];
  window.addEventListener('keydown', (e) => {
    if (e.code === 'KeyD' || e.code === 'Space') window.__talkKeys.push(e);
  }, true);
  // The first listener after the window's own capture phase: a Ctrl+D the
  // window took for push-to-talk must never get this far (review of PR #40 —
  // nothing behind the window may act on it).
  window.__talkPast = 0;
  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.code === 'KeyD') window.__talkPast++;
  }, true);
}
true;
"""


def _talk_past(page) -> int:
    """How many Ctrl+D keydowns got past the window to the document."""
    return page.evaluate("window.__talkPast || 0")


def _talk_probe(page):
    page.evaluate(TALK_PROBE)


def _talk_keys(page, n: int = 1) -> list[dict]:
    """The last N D/Space keydowns, as the window left them."""
    return page.evaluate("""(n) => (window.__talkKeys || []).slice(-n).map((e) => ({
      key: e.key, code: e.code, ctrl: e.ctrlKey, shift: e.shiftKey, alt: e.altKey, repeat: e.repeat,
      prevented: e.defaultPrevented }))""", n)


def _last_talk_key(page) -> dict:
    got = _talk_keys(page, 1)
    return got[0] if got else {}


def _hold_keys(page, keys, hold: float = 0.15, ctrl_first: bool = False):
    """Hold `keys` (pressed in order) where focus is now. Returns push-to-talk
    while held and after the first key let go (D, or Ctrl when `ctrl_first`)."""
    _talk_probe(page)
    for k in keys:
        page.keyboard.down(k)
    time.sleep(hold)
    held = page.evaluate("window.__hud.capture.ptt")
    order = list(reversed(keys))
    if ctrl_first and "Control" in order:
        order.remove("Control")
        order.insert(0, "Control")
    page.keyboard.up(order[0])
    time.sleep(0.05)
    after = page.evaluate("window.__hud.capture.ptt")
    for k in order[1:]:
        page.keyboard.up(k)
    time.sleep(0.05)
    return held, after


def _ptt_on_ctrl_d(page, hold: float = 0.15, ctrl_first: bool = False):
    """Hold Ctrl+D where focus is now: (push-to-talk while held, push-to-talk
    once D — or Ctrl, `ctrl_first` — is let go, the keydown as the window left it)."""
    held, after = _hold_keys(page, ["Control", "d"], hold, ctrl_first)
    return held, after, _last_talk_key(page)


def _edit(page, until, pane: int, typed: str):
    """Type at the end of the file open in pane N, in Monaco or its fallback,
    until the pane's Save says the edit landed.

    Under load this was the layout section's flake: it typed as soon as *a*
    `.view-lines` existed in the pane — the editor going away, or the new one
    before the file's text had replaced its buffer — and the edit was lost,
    so four "unsaved edit" checks failed. Now it waits for the editor to show
    the file, types, and types again (at most four times) until Save is
    enabled: an edit that did not land is retried, never assumed."""
    editor = page.locator(_pane(pane, '[data-testid="editor"] .view-lines'))
    fallback = page.locator(_pane(pane, '[data-testid="editor-fallback"]'))
    save = page.locator(_pane(pane, '[data-testid="file-save"]'))
    host = page.locator(_pane(pane, '[data-testid="editor"]'))

    def shows_file() -> bool:
        # One editor (not the old one beside the new), that Monaco has given
        # its model (`data-language` is set only then, ED-1), holding the
        # file's text (not an empty buffer it is about to replace).
        try:
            return (editor.count() == 1 and host.get_attribute("data-language", timeout=1000) is not None
                    and editor.inner_text(timeout=1000).strip() != "")
        except Exception:
            return False

    for attempt in range(4):
        until(lambda: editor.count() > 0 or fallback.count() > 0, timeout=10)
        if editor.count():
            until(shows_file, timeout=5)
            try:
                editor.click(timeout=3000)
            except Exception:
                continue  # replaced under the click: find the new one
            page.keyboard.press("Control+End")
            page.keyboard.type(typed)
        else:
            page.evaluate("([n, t]) => { const h = document.querySelector("
                          "`[data-testid=\"pane-${n}\"] [data-testid=\"editor-fallback\"]`);"
                          " h.value += t; h.dispatchEvent(new Event('input', {bubbles: true})); }", [pane, typed])
        if until(lambda: save.count() > 0 and not save.is_disabled(), timeout=2.0 + attempt):
            return True
    return False


def _approval(mock, req: str, command: str = "ls"):
    mock.emit("approval_requested", {
        "req_id": req, "code": req[-4:].upper(), "tool": "run_command", "args": {"command": command},
        "command": command, "reason": "reviewer declined", "layer": "human", "origin": "task: workspace",
        "allowlistable": True, "timeout_s": 120,
    })


TOGGLES = ("left", "panel", "right")
# ⊞'s tiles, in order (WP-G1, decisions S-1: 6 and 8 in the menu; 5 and 7 by split or close).
MENU_TILES = ("single", "cols2", "rows2", "cols3", "main2", "grid4", "grid6", "grid8")


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
    missing = [sid for sid in ("titlebar-settings", "zoom-control",
                               "layout-customize", "toggle-left", "toggle-panel", "toggle-right")
               if page.locator(f'[data-testid="titlebar"] [data-testid="{sid}"]').count() != 1]
    check("Settings, zoom and the four toggles live in the title bar", not missing,
          str(missing))
    check("the zoom control left the status pane's header",
          page.locator('#right [data-testid="zoom-control"]').count() == 0)
    check("and the global buttons left the centre's header",
          page.locator('#main [data-testid="open-model"]').count() == 0)
    xs = [page.locator(f'[data-testid="{sid}"]').bounding_box()["x"]
          for sid in ("titlebar-settings", "zoom-control", "layout-customize", "toggle-left", "toggle-panel",
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

    # ⬓: the bottom panel, holding the terminals (WP-D). With none open, ⬓
    # opens one (the owner's call, 2026-10-11).
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
    until(lambda: len(mock.world.get("terminals", [])) == 1, timeout=4)
    check("with none open, ⬓ opened exactly one terminal, and the panel shows it",
          len(mock.world.get("terminals", [])) == 1 and not _visible(page, '[data-testid="panel-empty"]'),
          str(len(mock.world.get("terminals", []))))
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
    check("Space on the edge never starts the mic", _ptt_on_space(page) is None)
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
    checked = [p for p in MENU_TILES
               if _attr(page, f'[data-testid="layout-preset-{p}"]', "aria-checked") == "true"]
    check("eight preset tiles (WP-G1: 6 and 8 among them), the current one checked", checked == ["single"]
          and page.locator('[data-testid^="layout-preset-"]').count() == 8, str(checked))
    check("the menu opens with focus on the current preset", _active_testid(page) == "layout-preset-single",
          str(_active_testid(page)))
    page.keyboard.press("ArrowRight")
    check("ArrowRight moves to the next tile", _active_testid(page) == "layout-preset-cols2", str(_active_testid(page)))
    page.keyboard.press("ArrowDown")
    check("ArrowDown to the tile below, four to a row", _active_testid(page) == "layout-preset-grid4",
          str(_active_testid(page)))
    check("and the arrows chose nothing", _attr(page, '[data-testid="workspace"]', "data-preset") == "single")
    check("Space in the menu never starts the mic", _ptt_on_space(page) is None)
    until(lambda: _attr(page, '[data-testid="workspace"]', "data-preset") == "grid4", timeout=2)
    check("it activates the tile under focus instead",
          _attr(page, '[data-testid="workspace"]', "data-preset") == "grid4")
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

    # One Settings gear stays reachable even at narrow zoomed width.
    page.set_viewport_size({"width": 700, "height": 700})
    _set_storage(page, zoom="160", layout="{}")
    _boot(page, mock, None, until, reload=True)
    check("the Settings gear replaces all four text tools and their overflow",
          _visible(page, '[data-testid="titlebar-settings"]')
          and page.locator('[data-testid="titlebar-more"], [data-testid="open-model"]').count() == 0)
    bad = [f"{sid}: {why}" for sid in ("titlebar-settings", "zoom-out", "zoom-in", "layout-customize",
                                      "toggle-left", "toggle-panel", "toggle-right")
           for ok, why in [_reachable(page, f'[data-testid="{sid}"]')] if not ok]
    check("Settings, zoom and layout stay on screen and clickable", not bad, "; ".join(bad))
    check("nothing overflows sideways",
          page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"))
    open_settings(page)
    check("the gear reaches Settings", page.locator('[data-testid="settings-page"]').count() == 1)
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="settings-page"]').count() == 0, timeout=2)
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
    check("two side by side: a second pane, showing the Browser",
          _attr(page, ws, "data-drawn-preset") == "cols2" and _visible(page, _pane(2))
          and _attr(page, _pane(2), "data-view") == "browser")
    b1 = page.locator(_pane(1)).bounding_box()
    b2 = page.locator(_pane(2)).bounding_box()
    check("beside the first, as tall, the centre halved",
          b1["x"] < b2["x"] and abs(b1["y"] - b2["y"]) < 1 and abs(b1["height"] - b2["height"]) < 1
          and abs(b1["width"] - b2["width"]) <= 3, f"{b1} {b2}")
    check("the chat is still one conversation with one box, in pane 1",
          page.locator('[data-testid="input"]').count() == 1 and page.locator(_pane(1, '[data-testid="input"]')).count() == 1)
    page.locator(_pane(2, '[data-testid="tab-browser"]')).click()
    until(lambda: _attr(page, _pane(2), "data-focused") == "true", timeout=2)
    check("a click in a pane focuses it, and only it, with a line under its header",
          _attr(page, _pane(2), "data-focused") == "true" and _attr(page, _pane(1), "data-focused") == "false"
          and "focused" in (_attr(page, _pane(2), "class") or ""))

    # FS3 (§9): edges are named by the tree, split-<axis><path>-<i>: two
    # columns side by side are one x node at the root, so its edge is split-x-1.
    sep = page.locator('[data-testid="split-x-1"]')
    edge = sep.count() == 1   # (absent before FS3: these checks fail, the rest of the section runs)
    check("the edge between them is a vertical separator, split-x-1",
          edge and sep.get_attribute("role") == "separator" and sep.get_attribute("aria-orientation") == "vertical")
    w1 = _width(page, _pane(1))
    if edge:
        _drag(page, '[data-testid="split-x-1"]', 100)
    check("dragging it moves it by the pointer's travel", abs(_width(page, _pane(1)) - (w1 + 100)) <= 3,
          f"{w1} -> {_width(page, _pane(1))}")
    w1 = _width(page, _pane(1))
    if edge:
        sep.focus()
        page.keyboard.press("ArrowLeft")
    check("ArrowLeft on it moves it a step", abs(_width(page, _pane(1)) - (w1 - 16)) <= 2,
          f"{w1} -> {_width(page, _pane(1))}")
    if edge:
        sep.focus()
        check("Space on it never starts the mic", _ptt_on_space(page) is None)
    w1 = _width(page, _pane(1))
    _boot(page, mock, None, until, reload=True)
    check("the preset and the edge survive a reload",
          _attr(page, ws, "data-preset") == "cols2" and abs(_width(page, _pane(1)) - w1) <= 2,
          f"{w1} -> {_width(page, _pane(1))}")
    if edge:
        _drag(page, '[data-testid="split-x-1"]', -2000)
    check("and stops short of the pane minimum", abs(_width(page, _pane(1)) - 360) <= 2, str(_width(page, _pane(1))))
    if edge:
        page.locator('[data-testid="split-x-1"]').dblclick()
        until(lambda: abs(_width(page, _pane(1)) - _width(page, _pane(2))) <= 2, timeout=2)
    check("a double-click makes the panes equal", abs(_width(page, _pane(1)) - _width(page, _pane(2))) <= 2)

    # Several chats (WP-B): choosing chat in pane 2 makes a second chat; it
    # used to swap the two panes' views (WP-A's single chat).
    page.locator(_pane(2, '[data-testid="tab-chat"]')).click()
    until(lambda: _attr(page, _pane(2), "data-view") == "chat", timeout=2)
    check("choosing chat in pane 2 makes a second chat beside the first, each with its own box",
          _attr(page, _pane(2), "data-view") == "chat" and _attr(page, _pane(1), "data-view") == "chat"
          and page.locator(_pane(1, '[data-testid="input"]')).count() == 1
          and page.locator(_pane(2, '[data-testid="input"]')).count() == 1)
    page.locator(_pane(2, '[data-testid="tab-browser"]')).click()
    until(lambda: _attr(page, _pane(2), "data-view") == "browser", timeout=2)

    # Hidden, never unmounted: the same frame after one pane and back.
    _project_root(page, 2)
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
    check("a pane's Browser URL survives a reload, judged again and loaded",
          page.locator(_pane(2, "iframe")).get_attribute("src") == src, str(src))

    # Where a sidebar click goes.
    page.locator(_pane(2, '[data-testid="tab-browser"]')).click()
    until(lambda: _attr(page, _pane(2), "data-focused") == "true", timeout=2)
    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: page.evaluate("window.__hud.state().threadId") == "t1")
    check("a thread click goes to the pane already showing the chat",
          _attr(page, _pane(1), "data-view") == "chat" and _attr(page, _pane(2), "data-view") == "browser"
          and _attr(page, _pane(1), "data-focused") == "true")
    page.locator(_pane(2, '[data-testid="tab-browser"]')).click()
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
    # E-13: the File pane's own Project ▾ (every project, plus Follow chat)
    # replaced the "in: … · follow chat" chip the File pane used to carry.
    folder = page.locator('[data-testid="pane-2-folder"]')
    options = page.evaluate("[...document.querySelectorAll('[data-testid=\"pane-2-folder\"] option')]"
                            ".map(o => o.value)")
    check("E-13: the File pane's own picker names the project it is pinned to, offering every project and "
          "Follow chat", folder.count() == 1 and folder.input_value() == "p1"
          and options == ["", "p1", "p2"], f"{folder.input_value() if folder.count() else None} {options}")
    page.locator('[data-testid="project-chip-select"]').select_option("p2")
    time.sleep(0.3)
    check("with the chat moved to another project the file stays open, and the pane says where it is",
          page.locator(_pane(2, '[data-testid="file-path"]')).inner_text() == "calc.py"
          and folder.input_value() == "p1")
    edited = _edit(page, until, 2, "\n# pinned")
    save = page.locator(_pane(2, '[data-testid="file-save"]'))
    until(lambda: not save.is_disabled(), timeout=3)
    check("setup: the edit landed", edited and not save.is_disabled())
    until(lambda: "Save or reload" in (folder.get_attribute("title") or ""), timeout=2)
    refused = page.locator('[data-testid="pane-2-refused"]')
    for choice, what in (("", "Follow chat"), ("p2", "another project")):
        folder.select_option(choice)
        until(lambda: refused.count() > 0, timeout=2)
        time.sleep(0.15)
        check(f"E-13: with an unsaved edit, {what} is refused with the chip, and the pane stays where it was",
              refused.count() == 1 and folder.input_value() == "p1"
              and _ws(page)["ws"]["panes"][1]["projectId"] == "p1"
              and page.locator(_pane(2, '[data-testid="file-path"]')).inner_text() == "calc.py",
              f"{folder.input_value()} {_ws(page)['ws']['panes'][1]}")
    save.click()
    until(lambda: any(m == "PUT" for m, _p, _b in mock.calls[calls_before:]), timeout=4)
    puts = [p for m, p, _b in mock.calls[calls_before:] if m == "PUT"]
    check("and its save goes to the project it was read from, never the one the chat moved to",
          puts == ["/projects/p1/file"], str(puts))
    until(lambda: save.is_disabled(), timeout=3)
    # The keystroke itself makes the pane dirty for the guard: a pick in the
    # same breath is refused, never a remount that drops the edit (review of
    # PR #43, M1 — the guard read state a frame behind the edit).
    # A clean pane: the keystroke is what makes it dirty.
    page.locator(_pane(2, '[data-testid="editor"] .view-lines')).click()
    page.keyboard.press("Control+End")
    page.keyboard.type("Z")
    folder.select_option("p2")
    time.sleep(0.3)
    check("M1: a project picked in the same breath as a keystroke is refused, and the edit is kept",
          _ws(page)["ws"]["panes"][1]["projectId"] == "p1"
          and "Z" in page.locator(_pane(2, '[data-testid="editor"] .view-lines')).inner_text(),
          str(_ws(page)["ws"]["panes"][1]))
    if not save.is_disabled():  # (on the old code the edit is gone with the remount)
        save.click()
        until(lambda: save.is_disabled(), timeout=3)
    _approval(mock, "fold1")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    time.sleep(0.2)
    check("E-13: under a card the picker is disabled", folder.is_disabled())
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    until(lambda: not folder.is_disabled(), timeout=2)
    folder.select_option("")
    until(lambda: page.locator(_pane(2, '[data-testid="file-path"]')).inner_text() == "no file open", timeout=3)
    check("once saved, Follow chat unpins it: a fresh tree in the chat's project, nothing open",
          _ws(page)["ws"]["panes"][1]["projectId"] is None and folder.input_value() == "")
    others = [q.get("projectId") for i, q in enumerate(_ws(page)["ws"]["panes"]) if i != 1]
    folder.select_option("p1")
    until(lambda: _ws(page)["ws"]["panes"][1]["projectId"] == "p1", timeout=3)
    until(lambda: page.locator(_pane(2, '[data-testid="file-calc.py"]')).count() > 0, timeout=4)
    check("E-13: picking a project re-pins this pane only, to a fresh tree there",
          _ws(page)["ws"]["panes"][1]["projectId"] == "p1" and folder.input_value() == "p1"
          and page.locator(_pane(2, '[data-testid="file-path"]')).inner_text() == "no file open"
          and [q.get("projectId") for i, q in enumerate(_ws(page)["ws"]["panes"]) if i != 1] == others,
          str(_ws(page)["ws"]["panes"]))
    folder.select_option("")
    until(lambda: _ws(page)["ws"]["panes"][1]["projectId"] is None, timeout=3)

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
    check("FS3: stored as version 2, the tree three columns, with the v1 mirror beside it",
          stored.get("version") == 2 and _leaves(stored.get("tree")) == [1, 2, 3]
          and (stored.get("tree") or {}).get("axis") == "x" and stored.get("order") == [1, 2, 3]
          and "splits" not in stored, f'{stored.get("version")} {stored.get("tree")} {sorted(stored)}')

    page.set_viewport_size({"width": 1600, "height": 900})
    _set_storage(page, zoom="100")
    _boot(page, mock, None, until, reload=True)
    _choose(page, until, "rows2")
    r1 = page.locator(_pane(1)).bounding_box()
    r2 = page.locator(_pane(2)).bounding_box()
    check("two stacked", abs(r1["x"] - r2["x"]) < 1 and r1["y"] < r2["y"] and abs(r1["width"] - r2["width"]) < 1,
          f"{r1} {r2}")
    # Two stacked are one y node at the root: split-y-1 (FS3, §9).
    row = page.locator('[data-testid="split-y-1"]')
    check("with a horizontal edge between them, split-y-1",
          row.count() == 1 and row.get_attribute("aria-orientation") == "horizontal")
    h1 = _height(page, _pane(1))
    if row.count():
        _drag_y(page, '[data-testid="split-y-1"]', 60)
    check("dragging it down grows the top pane by the pointer's travel", abs(_height(page, _pane(1)) - (h1 + 60)) <= 3,
          f"{h1} -> {_height(page, _pane(1))}")
    if row.count():
        row.dblclick()

    _choose(page, until, "grid4")
    boxes = [page.locator(_pane(n)).bounding_box() for n in (1, 2, 3, 4)]
    check("2×2: four panes, reading left to right, top to bottom",
          all(boxes) and boxes[0]["x"] < boxes[1]["x"] and boxes[0]["y"] < boxes[2]["y"]
          and abs(boxes[2]["x"] - boxes[0]["x"]) < 1 and abs(boxes[3]["y"] - boxes[2]["y"]) < 1, str(boxes))
    # 2×2 is rows first, y[x[1,2], x[3,4]]: each row its own x node, and their
    # column boundaries coincide, so they are one linked separator (P-5),
    # carrying the first member's id and data-linked="2" — still one edge
    # down and one across, as before FS3.
    col = page.locator('[data-testid="split-x0-1"]')
    check("2×2: one edge down and one across (the column edge one separator through both rows, linked)",
          col.count() == 1 and col.get_attribute("data-linked") == "2"
          and page.locator('[data-testid="split-x1-1"]').count() == 0
          and page.locator('[data-testid="split-y-1"]').count() == 1
          and len(_pane_edges(page)) == 2,
          f'{col.count()} {col.get_attribute("data-linked") if col.count() else None} {_pane_edges(page)}')
    if col.count():
        box = col.bounding_box()
        wsbox = page.locator(ws).bounding_box()
        check("2×2: the linked column edge spans both rows", box is not None and box["height"] >= wsbox["height"] - 4,
              f"{box} {wsbox}")
        w1, w3 = _width(page, _pane(1)), _width(page, _pane(3))
        _drag(page, '[data-testid="split-x0-1"]', 80)
        check("2×2: dragging it moves both rows' column edge together",
              abs(_width(page, _pane(1)) - (w1 + 80)) <= 3 and abs(_width(page, _pane(3)) - (w3 + 80)) <= 3,
              f"{w1},{w3} -> {_width(page, _pane(1))},{_width(page, _pane(3))}")
        col.dblclick()
        until(lambda: abs(_width(page, _pane(1)) - _width(page, _pane(2))) <= 2, timeout=2)
        check("2×2: a double-click makes both rows equal again",
              abs(_width(page, _pane(1)) - _width(page, _pane(2))) <= 2 and abs(_width(page, _pane(3)) - _width(page, _pane(4))) <= 2)
    got = _ws(page)
    check("FS3: the window's projection names the tree and its derived order and preset, and the fit's boxes",
          "tree" in got["ws"] and got["ws"].get("order") == [1, 2, 3, 4] and got["ws"].get("preset") == "grid4"
          and got["fit"].get("drawn") == "grid4" and sorted((got["fit"].get("boxes") or {}).keys()) == ["1", "2", "3", "4"]
          and "splits" not in got["ws"],
          f'{sorted(got["ws"])} {got["fit"].get("drawn")} {sorted((got["fit"].get("boxes") or {}).keys())}')
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
    """A 2×2 grid with Monaco, a preview frame and a terminal (WP-D) under
    the card: the card stays global and on top, and the terminal behind it
    takes no key."""
    from tests.face.hud_v2_mock_terminals import row
    tid = "0a0b0c0d"
    if not any(r["id"] == tid for r in mock.world.setdefault("terminals", [])):
        mock.world["terminals"].append(row(tid, "bash · jarvis"))
    fake = mock.fake_pty
    term = f'[data-testid="terminal-{tid}"]'
    grid = json.dumps({"preset": "grid4", "panes": [{"view": "chat"}, {"view": "file"}, {"view": "browser"},
                                                     {"view": "terminal", "terminalId": tid}]})
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
        _project_root(page, 3)
        until(lambda: page.locator(_pane(3, "iframe")).count() > 0, timeout=4)
        until(lambda: page.locator(term).count() > 0 and _attr(page, term, "data-state") == "attached", timeout=6)
        check(f"at {zoom}% Monaco, a preview frame and a terminal are on screen",
              _visible(page, _pane(2, '[data-testid="editor"]')) and _visible(page, _pane(3, "iframe"))
              and _visible(page, _pane(4, f"{term} .xterm-screen")) and _attr(page, term, "data-state") == "attached")
        page.locator(_pane(4, f"{term} .xterm-screen")).click(position={"x": 20, "y": 8})
        sent = len(fake.sent(tid))
        req = f"grid{zoom}"
        _approval(mock, req, LONG_COMMAND)
        until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
        time.sleep(0.2)
        if zoom == "160":
            ok, why = _authorize_in_reach(page)
            check(f"at {zoom}% over the grid, AUTHORIZE is in reach before any scrolling (AS-12)", ok, why)
        ok, why = _card_on_screen(page)
        check(f"at {zoom}% over the grid, the card is wholly on screen and every button topmost at its point",
              ok, why)
        page.keyboard.type("y")
        page.keyboard.press("Enter")
        page.wait_for_timeout(150)
        check(f"at {zoom}% a y⏎ typed with a terminal in the grid reaches no shell",
              fake.sent(tid)[sent:] == b"", repr(fake.sent(tid)[sent:]))
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
                        "panes": [{"view": "chat"}, {"view": "browser"}, {"view": "file"}, {"view": "task"}]})
    page.set_viewport_size({"width": 1280, "height": 800})
    _set_storage(page, zoom="150", layout="{}")
    _set_ws(page, three)
    _boot(page, mock, None, until, reload=True)
    check("setup: three columns at 150% draw as two, the focused pane 3 with pane 2",
          _attr(page, ws, "data-drawn-preset") == "cols2" and _visible(page, _pane(2)) and _visible(page, _pane(3))
          and not _visible(page, _pane(1)))
    before = [page.locator(_pane(n)).bounding_box() for n in (2, 3)]
    url = page.locator(_pane(2, '[data-testid="browser-url"]'))
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
                       "panes": [{"view": "chat"}, {"view": "browser"}, {"view": "file"}, {"view": "task"}]})
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
                      "panes": [{"view": "chat"}, {"view": "browser"}, {"view": "file"}, {"view": "task"}]})
    page.set_viewport_size({"width": 1600, "height": 900})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, two)
    _boot(page, mock, None, until, reload=True)
    _project_root(page, 2)
    until(lambda: page.locator(_pane(2, "iframe")).count() > 0, timeout=4)
    time.sleep(0.3)
    # A click as a hand makes it: the pointer moves over the frame first (its
    # shield steps aside, R-1), then presses. Playwright's own locator click
    # checks the hit target before it moves, and reads the shield as covering.
    box = page.locator(_pane(2, "iframe")).bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
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


def _monaco_editors(page) -> list:
    return page.evaluate("window.__hud.monacoEditors ? window.__hud.monacoEditors() : []")


def _editor_card_checks(page, mock, check, until):
    """ED-1 loads Monaco's overlays — the context menu, the suggest list —
    which carry z-indexes in the thousands. With one open, a card that
    arrives is still on top at every one of its buttons; the editor behind it
    is unfocused and read-only, Escape denies, and a typed `y` lands nowhere.
    At 100% and 160%."""
    two = json.dumps({"preset": "cols2", "focused": 2,
                      "panes": [{"view": "chat"}, {"view": "file"}, {"view": "browser"}, {"view": "task"}]})
    host = _pane(2, '[data-testid="editor"]')
    lines = page.locator(_pane(2, '[data-testid="editor"] .view-lines'))
    menu = page.locator(_pane(2, ".monaco-menu"))
    suggest = page.locator(_pane(2, ".suggest-widget.visible .monaco-list-row"))
    for zoom in ("100", "160"):
        page.set_viewport_size({"width": 1600, "height": 900})
        _set_storage(page, zoom=zoom, layout="{}")
        _set_ws(page, two)
        _boot(page, mock, None, until, reload=True)
        page.locator(_pane(2, '[data-testid="file-calc.py"]')).click()
        until(lambda: page.locator(host).count() > 0
              and page.locator(host).get_attribute("data-language") is not None, timeout=10)
        if page.locator(host).count() == 0:
            check(f"at {zoom}% Monaco loads in a split pane (fell back to a textarea)", False)
            break
        for n, (what, opened) in enumerate((("its context menu", menu), ("its suggest list", suggest))):
            if what == "its context menu":
                lines.click()
                line = page.locator(_pane(2, '[data-testid="editor"] .view-line')).first.bounding_box()
                at = (line["x"] + 30, line["y"] + line["height"] / 2)
                page.mouse.click(at[0], at[1], button="right")
            else:
                lines.click()
                page.keyboard.press("Control+End")
                page.keyboard.type("\nre")
                page.keyboard.press("Control+Space")
            until(lambda: opened.count() > 0 and opened.first.is_visible(), timeout=4)
            check(f"at {zoom}% setup: Monaco shows {what}", opened.count() > 0 and opened.first.is_visible())
            if what == "its context menu":
                # Monaco places it in viewport pixels and the zoom on #root
                # used to scale that again: at 160% it opened 60% further out,
                # off the window's right edge (lib/zoomfix.ts).
                box = page.evaluate("""() => {
                  const hosts = [...document.querySelectorAll('[data-testid="pane-2"] .shadow-root-host')];
                  const menu = hosts.map(h => h.shadowRoot && h.shadowRoot.querySelector('.monaco-menu'))
                    .find(Boolean);
                  if (!menu) return null;
                  const r = menu.getBoundingClientRect();
                  return {x: r.left, y: r.top, right: r.right, bottom: r.bottom, vw: innerWidth, vh: innerHeight};
                }""")
                check(f"at {zoom}% the context menu opens at the pointer, wholly on screen",
                      bool(box) and abs(box["x"] - at[0]) <= 4 and abs(box["y"] - at[1]) <= 4
                      and box["right"] <= box["vw"] and box["bottom"] <= box["vh"], f"{box} vs click {at}")
                # The menu closes when a card takes focus, so the card check
                # below cannot see where it would have stacked. Lay a layer at
                # #authveil's z-index over the whole window instead: it must be
                # what is under the menu's middle — the menu (z 2575) stacks
                # inside the editor host, never above the veil.
                top = page.evaluate("""() => {
                  const hosts = [...document.querySelectorAll('[data-testid="pane-2"] .shadow-root-host')];
                  const menu = hosts.map(h => h.shadowRoot && h.shadowRoot.querySelector('.monaco-menu'))
                    .find(Boolean);
                  if (!menu) return 'no menu';
                  const r = menu.getBoundingClientRect();
                  const probe = document.createElement('div');
                  probe.style.cssText = 'position:fixed;inset:0;z-index:100';
                  document.getElementById('root').appendChild(probe);
                  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                  probe.remove();
                  return hit === probe ? 'veil' : (hit && hit.className) || String(hit);
                }""")
                check(f"at {zoom}% Monaco's context menu stacks inside the editor, under a layer at the veil's z-index",
                      top == "veil", str(top))
            before = lines.inner_text()
            req = f"edcard{zoom}{n}"
            _approval(mock, req, LONG_COMMAND)
            until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
            time.sleep(0.25)
            ok, why = _card_on_screen(page)
            check(f"at {zoom}% with {what} open, the card arrives on top: every button topmost at its point",
                  ok, why)
            eds = _monaco_editors(page)
            focused = page.evaluate("!!(document.activeElement && document.activeElement.closest('.monaco-editor'))")
            check(f"at {zoom}% the editor behind the card is unfocused and read-only",
                  bool(eds) and all(e["readOnly"] and not e["focused"] for e in eds) and not focused
                  and page.locator(host).get_attribute("data-readonly") == "true", f"{eds} focus in editor {focused}")
            page.keyboard.type("y")
            time.sleep(0.15)
            check(f"at {zoom}% a typed y lands nowhere", lines.inner_text() == before, lines.inner_text()[-40:])
            puts = len(mock.sent("PUT", "/projects/p1/file"))
            page.keyboard.press("Control+s")
            time.sleep(0.2)
            check(f"at {zoom}% and Ctrl+S saves nothing", len(mock.sent("PUT", "/projects/p1/file")) == puts)
            page.keyboard.press("Escape")
            body = until(lambda: mock.sent("POST", f"/approvals/{req}") or None, timeout=4)
            check(f"at {zoom}% and Escape denies", bool(body) and body[-1].get("decision") == "deny",
                  str(body[-1] if body else None))
            until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
            until(lambda: not any(e["readOnly"] for e in _monaco_editors(page)), timeout=2)
            check(f"at {zoom}% once it is answered the editor takes edits again",
                  not any(e["readOnly"] for e in _monaco_editors(page))
                  and page.locator(host).get_attribute("data-readonly") == "false")
            page.keyboard.press("Escape")
            # Throw the setup's edit away (Reload asks first, ED-1), so the
            # reload that follows is not held by beforeunload.
            save = page.locator(_pane(2, '[data-testid="file-save"]'))
            if not save.is_disabled():
                page.locator(_pane(2, '[data-testid="file-reload"]')).click()
                page.locator(_pane(2, '[data-testid="file-discard-yes"]')).click()
                until(lambda: save.is_disabled(), timeout=3)
    _reset_all(page, mock, until)


def expand(page, until, project_id: str, child: str, tries: int = 4) -> bool:
    """Click a project row until the child row is on screen (a project row toggles)."""
    for _ in range(tries):
        if page.locator(f'[data-testid="{child}"]').count() > 0:
            return True
        page.locator(f'[data-testid="project-{project_id}"]').click()
        until(lambda: page.locator(f'[data-testid="{child}"]').count() > 0, timeout=1.0)
    return page.locator(f'[data-testid="{child}"]').count() > 0


def _puts(mock) -> list:
    return [b for m, p, b in mock.calls if m == "PUT" and p == "/projects/p1/file"]


def _open_file(page, until, pane: int, name: str) -> bool:
    """Open `name` from pane N's tree in an editor (Markdown's split, not its
    rendered view); True once Monaco holds it."""
    row = page.locator(_pane(pane, f'[data-testid="file-{name}"]'))
    picker = page.locator(f'[data-testid="pane-{pane}-folder"]')
    path = page.locator(_pane(pane, '[data-testid="file-path"]'))
    # A File pane gets its tabs back when it mounts again (ED-2; WP-G2 before
    # it), and a narrow one hides its tree behind "Files": already open is open.
    if not (path.count() and path.inner_text() == name):
        if not until(lambda: row.count() > 0, timeout=2) and picker.count():
            picker.select_option("p1")  # no chat to follow: the pane picks its project (E-13)
        until(lambda: row.count() > 0, timeout=4)
        toggle = page.locator(_pane(pane, '[data-testid="tree-toggle"]'))
        if row.count() and not row.first.is_visible() and toggle.count():
            toggle.click()
            until(lambda: row.first.is_visible(), timeout=2)
        row.click()
    until(lambda: path.inner_text() == name, timeout=4)
    if name.endswith(".md"):
        until(lambda: page.locator(_pane(pane, '[data-testid="toggle-rendered"]')).count() > 0, timeout=3)
        if page.locator(_pane(pane, '[data-testid="md-split"]')).count() == 0:
            page.locator(_pane(pane, '[data-testid="toggle-rendered"]')).click()
    host = page.locator(_pane(pane, '[data-testid="editor"]'))
    return bool(until(lambda: host.count() == 1 and host.get_attribute("data-language") is not None, timeout=10))


def _save_scope_checks(page, mock, check, until):
    """Ctrl+S saves the editor it is pressed in, and nothing else (review of
    PR #43, H1). In 0.57 `addCommand` is a global keybinding: the last editor
    created took Ctrl+S for every editor and outlived itself, so Ctrl+S in a
    Diff wrote a file blank, and in one pane saved another pane's buffer.

    A 2x2 grid with a chat in pane 1 (the window puts a chat in the focused
    pane at boot when none is drawn): file in 2, diff in 3, task in 4."""
    files = mock.world["files"]
    kept = {k: dict(v) for k, v in files.items()}
    grid = json.dumps({"preset": "grid4", "focused": 2,
                       "panes": [{"view": "chat"}, {"view": "file"}, {"view": "diff"}, {"view": "task"}]})
    page.set_viewport_size({"width": 1600, "height": 900})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, grid)
    _boot(page, mock, None, until, reload=True)

    def restore():
        # Content only, before the files are opened again: a write that went
        # astray (the old code's blank calc.py) must not leak into the next
        # scenario, and the mtime stays what the server last said.
        for k, v in kept.items():
            files[k]["content"] = v["content"]

    try:
        # An editor that went away leaves no Ctrl+S behind.
        check("setup: calc.py open in pane 2's editor", _open_file(page, until, 2, "calc.py"))
        expand(page, until, "p1", "task-k1")
        page.locator('[data-testid="task-k1"]').click()
        until(lambda: page.locator(_pane(3, '[data-testid="diff-calc.py"]')).count() > 0, timeout=4)
        page.locator(_pane(3, '[data-testid="diff-calc.py"]')).click()
        diff = page.locator(_pane(3, '[data-testid="diff-editor"] .editor.modified .view-lines'))
        until(lambda: diff.count() > 0, timeout=10)
        _show(page, until, 2, "browser")
        until(lambda: page.locator(_pane(2, '[data-testid="editor"]')).count() == 0, timeout=3)
        diff.first.click()
        before = len(_puts(mock))
        page.keyboard.press("Control+s")
        time.sleep(0.5)
        check("H1: with pane 2's editor gone, Ctrl+S in a diff saves nothing (no file written blank)",
              len(_puts(mock)) == before and files["calc.py"]["content"] == kept["calc.py"]["content"],
              str(_puts(mock)[before:]))
        restore()

        # Two editors alive: each Ctrl+S is its own pane's.
        _show(page, until, 2, "file")
        _show(page, until, 3, "file")
        opened = _open_file(page, until, 2, "calc.py") and _open_file(page, until, 3, "README.md")
        check("setup: calc.py in pane 2 and README.md in pane 3, both in editors", opened)
        _edit(page, until, 2, "\n#one")
        _edit(page, until, 3, "\n#two")
        page.locator(_pane(2, '[data-testid="editor"] .view-lines')).click()
        before = len(_puts(mock))
        page.keyboard.press("Control+s")
        until(lambda: len(_puts(mock)) > before, timeout=3)
        time.sleep(0.3)
        sent = _puts(mock)[before:]
        check("H1: with two editors alive, Ctrl+S in pane 2 saves pane 2's file, and only it",
              len(sent) == 1 and sent[0].get("path") == "calc.py" and sent[0].get("content", "").endswith("#one")
              and not page.locator(_pane(3, '[data-testid="file-save"]')).is_disabled(), str(sent))
        for n in (2, 3):  # each scenario starts clean, whichever pane the last Ctrl+S wrote
            button = page.locator(_pane(n, '[data-testid="file-save"]'))
            if not button.is_disabled():
                button.click()
                until(lambda: button.is_disabled(), timeout=3)

        # Pane 3 switched away: Ctrl+S in pane 2 is still pane 2's.
        _show(page, until, 3, "browser")
        until(lambda: page.locator(_pane(3, '[data-testid="editor"]')).count() == 0, timeout=3)
        readme = files["README.md"]["content"]
        _edit(page, until, 2, "\n#three")
        page.locator(_pane(2, '[data-testid="editor"] .view-lines')).click()
        before = len(_puts(mock))
        page.keyboard.press("Control+s")
        until(lambda: len(_puts(mock)) > before, timeout=3)
        time.sleep(0.3)
        sent = _puts(mock)[before:]
        check("H1: pane 3 switched away, Ctrl+S in pane 2 saves pane 2, and pane 3's file is byte-identical",
              len(sent) == 1 and sent[0].get("path") == "calc.py" and sent[0].get("content", "").endswith("#three")
              and files["README.md"]["content"] == readme, str(sent))
        restore()

        # An editor that opens while a card is up opens read-only (the first
        # editor of a session, Monaco loading as a card arrives).
        _show(page, until, 2, "browser")
        _show(page, until, 2, "file")
        until(lambda: page.locator(_pane(2, '[data-testid="file-calc.py"]')).count() > 0, timeout=4)
        _approval(mock, "edro1")
        until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
        time.sleep(0.2)
        page.evaluate("document.querySelector('[data-testid=\"pane-2\"] [data-testid=\"file-calc.py\"]').click()")
        host = page.locator(_pane(2, '[data-testid="editor"]'))
        until(lambda: host.count() == 1 and host.get_attribute("data-language") is not None, timeout=10)
        eds = _monaco_editors(page)
        check("an editor that opens while a card is up opens read-only",
              host.count() == 1 and host.get_attribute("data-readonly") == "true"
              and bool(eds) and all(e["readOnly"] for e in eds), str(eds))
        page.keyboard.press("Escape")
        until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
        until(lambda: host.get_attribute("data-readonly") == "false", timeout=2)
        check("and takes edits once the card is answered", host.get_attribute("data-readonly") == "false")
    finally:
        files.clear()
        files.update(kept)
        _reset_all(page, mock, until)


def _late_save_checks(page, mock, check, until):
    """A save's answer lands on the file it was sent for, or nowhere (review
    of PR #43, L4). The old save closed over the file it started with and
    wrote that record back when the answer came: a save still on its way
    when another file was opened turned the new file's record into the old
    one (its path, its mtime, its content), so the pane named the wrong file
    and called the buffer dirty."""
    files = mock.world["files"]
    kept = {k: dict(v) for k, v in files.items()}
    two = json.dumps({"preset": "cols2", "focused": 2,
                      "panes": [{"view": "chat"}, {"view": "file"}, {"view": "browser"}, {"view": "task"}]})
    page.set_viewport_size({"width": 1600, "height": 900})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, two)
    _boot(page, mock, None, until, reload=True)
    save = page.locator(_pane(2, '[data-testid="file-save"]'))
    path = page.locator(_pane(2, '[data-testid="file-path"]'))
    try:
        check("setup: calc.py open in pane 2's editor", _open_file(page, until, 2, "calc.py"))
        check("setup: an edit to save", _edit(page, until, 2, "\n#late"))
        before = len(_puts(mock))
        mock.world["put_delays"] = [1.5]
        save.click()
        until(lambda: len(_puts(mock)) > before, timeout=2)
        row = page.locator(_pane(2, '[data-testid="file-README.md"]'))
        if not row.is_visible():  # a narrow pane folds its tree behind "Files"
            page.locator(_pane(2, '[data-testid="tree-toggle"]')).click()
        row.click()
        discard = page.locator(_pane(2, '[data-testid="file-discard-yes"]'))
        if until(lambda: discard.count() > 0 or path.inner_text() == "README.md", timeout=2) and discard.count():
            discard.click()
        until(lambda: path.inner_text() == "README.md", timeout=3)
        time.sleep(2.0)  # the answer lands
        check("L4: a save answered after another file was opened leaves that file's record alone",
              path.inner_text() == "README.md" and save.is_disabled(),
              f"{path.inner_text()!r} save {'disabled' if save.is_disabled() else 'enabled'}")
        if page.locator(_pane(2, '[data-testid="md-split"]')).count() == 0:
            page.locator(_pane(2, '[data-testid="toggle-rendered"]')).click()
        _edit(page, until, 2, "\n#mine")
        before = len(_puts(mock))
        save.click()
        until(lambda: len(_puts(mock)) > before, timeout=3)
        time.sleep(0.3)
        sent = _puts(mock)[before:]
        check("and the next save is that file's own, sent against its own etag (no conflict)",
              len(sent) == 1 and sent[0].get("path") == "README.md" and files["README.md"]["content"].endswith("#mine")
              and save.is_disabled(), str(sent))
    finally:
        mock.world.pop("put_delays", None)
        files.clear()
        files.update(kept)
        _reset_all(page, mock, until)


def _sidebar_shown(page) -> bool:
    return _visible(page, "#sidebar")


def _chord_checks(page, mock, check, until):
    """The chord tracker waits as Monaco does (review of PR #43, L1): a key
    typed outside any editor neither ends Monaco's chord nor ours, so Ctrl+K
    in the editor, a letter in the chat box, then Ctrl+B in the editor is the
    chord — the anchor, not a fold too."""
    two = json.dumps({"preset": "cols2", "focused": 2,
                      "panes": [{"view": "chat"}, {"view": "file"}, {"view": "browser"}, {"view": "task"}]})
    page.set_viewport_size({"width": 1600, "height": 900})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, two)
    _boot(page, mock, None, until, reload=True)
    _open_file(page, until, 2, "calc.py")
    lines = page.locator(_pane(2, '[data-testid="editor"] .view-lines'))
    lines.click()
    page.keyboard.press("Control+End")
    shown = _sidebar_shown(page)
    page.keyboard.press("Control+k")
    page.locator('[data-testid="input"]').first.click()
    page.keyboard.type("x")
    lines.click()
    page.keyboard.press("Control+b")
    time.sleep(0.3)
    anchors = page.locator(_pane(2, '[data-testid="editor"] .selection-anchor')).count()
    check("L1: Ctrl+K in the editor, a key in the chat box, then Ctrl+B in the editor is Monaco's chord, not a fold",
          shown and _sidebar_shown(page) and anchors > 0, f"sidebar {_sidebar_shown(page)}, anchors {anchors}")
    page.keyboard.press("Control+b")
    until(lambda: not _sidebar_shown(page), timeout=2)
    check("and the next lone Ctrl+B folds the sidebar again", not _sidebar_shown(page))
    page.keyboard.press("Control+b")
    until(lambda: _sidebar_shown(page), timeout=2)
    page.locator('[data-testid="input"]').first.fill("")
    _reset_all(page, mock, until)


def _menu_box(page, pane: int):
    return page.evaluate("""(n) => {
      const hosts = [...document.querySelectorAll(`[data-testid="pane-${n}"] .shadow-root-host`)];
      const menu = hosts.map(h => h.shadowRoot && h.shadowRoot.querySelector('.monaco-menu')).find(Boolean);
      if (!menu) return null;
      const r = menu.getBoundingClientRect();
      return {x: r.left, y: r.top, right: r.right, bottom: r.bottom, vw: innerWidth, vh: innerHeight};
    }""", pane)


def _menu_place_checks(page, mock, check, until):
    """Monaco's context menu where the pointer is, at any zoom and any edge
    (review of PR #43, L2): it flipped by its unzoomed size, and at 70% a
    right-click near the bottom put the menu's bottom 45 px above the
    pointer. And in a window too short for either side, it stays on screen."""
    # The window puts a chat in the focused pane at boot when none is drawn,
    # so the file sits beside one; both side panes folded, so the editor's
    # right edge is near the window's at 160%.
    two = json.dumps({"preset": "cols2", "focused": 2,
                      "panes": [{"view": "chat"}, {"view": "file"}, {"view": "browser"}, {"view": "task"}]})
    folded = json.dumps({"left": 260, "right": 300, "leftCollapsed": True, "rightCollapsed": True})
    cases = (
        ("70", (1600, 900), "bottom", lambda b, at: abs(b["bottom"] - at[1]) <= 4 and abs(b["x"] - at[0]) <= 4),
        ("160", (1600, 900), "right", lambda b, at: abs(b["right"] - at[0]) <= 4 and abs(b["y"] - at[1]) <= 4),
        ("160", (1600, 400), "short", lambda b, at: True),
    )
    for zoom, (w, h), where, at_pointer in cases:
        page.set_viewport_size({"width": w, "height": h})
        _set_storage(page, zoom=zoom, layout=folded)
        _set_ws(page, two)
        _boot(page, mock, None, until, reload=True)
        if not _open_file(page, until, 2, "calc.py"):
            check(f"at {zoom}% Monaco loads ({where})", False)
            continue
        ed = page.locator(_pane(2, '[data-testid="editor"]')).bounding_box()
        # The first line Monaco has drawn (a line can exist before its layout).
        first_line = """() => { const ls = [...document.querySelectorAll('[data-testid="pane-2"] [data-testid="editor"] .view-line')]
            .map(l => l.getBoundingClientRect()).filter(r => r.width > 0 && r.height > 0).sort((a, b) => a.top - b.top);
            return ls.length ? {x: ls[0].left, y: ls[0].top, width: ls[0].width, height: ls[0].height} : null; }"""
        line = until(lambda: page.evaluate(first_line), timeout=5)
        if not ed or not line:
            check(f"at {zoom}% the editor is drawn ({where})", False, f"{ed} {line}")
            continue
        # In the text, never the gutter (a right-click there opens no menu).
        if where == "bottom":
            at = (line["x"] + 30, ed["y"] + ed["height"] - 12)
        elif where == "right":
            at = (ed["x"] + ed["width"] - 30, line["y"] + line["height"] / 2)
        else:
            at = (line["x"] + 30, line["y"] + line["height"] / 2)
        page.locator(_pane(2, '[data-testid="editor"] .view-lines')).click()
        page.mouse.click(at[0], at[1], button="right")
        until(lambda: _menu_box(page, 2) is not None, timeout=3)
        time.sleep(0.2)
        b = _menu_box(page, 2)
        on_screen = bool(b) and b["x"] >= -0.5 and b["y"] >= -0.5 and b["right"] <= b["vw"] + 0.5 \
            and b["bottom"] <= b["vh"] + 0.5
        what = {"bottom": "near the bottom it opens above the pointer, its bottom on it",
                "right": "near the right edge it opens left of the pointer, its right edge on it",
                "short": "in a window too short for either side it stays wholly on screen"}[where]
        check(f"L2: at {zoom}% on {w}x{h} the context menu {what}", on_screen and at_pointer(b, at),
              f"{b} vs click {at}")
        page.keyboard.press("Escape")
    _reset_all(page, mock, until)


def _seen_checks(page, mock, check, until):
    """"Read" is a thread or task shown in **any** drawn pane, not only the
    focused one (review of PR #26: a focused-pane-only mutation passed every
    check before these)."""
    seen = lambda path: len(mock.posted(path))  # noqa: E731
    two = json.dumps({"preset": "cols2", "focused": 1,
                      "panes": [{"view": "browser"}, {"view": "chat"}, {"view": "file"}, {"view": "task"}]})
    page.set_viewport_size({"width": 1600, "height": 900})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, two)
    _boot(page, mock, None, until, reload=True)
    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: page.evaluate("window.__hud.state().threadId") == "t1")
    page.locator(_pane(1, '[data-testid="tab-browser"]')).click()
    until(lambda: _attr(page, _pane(1), "data-focused") == "true", timeout=2)
    check("setup: t1's chat in pane 2, pane 1 focused",
          _attr(page, _pane(2), "data-view") == "chat" and _attr(page, _pane(2), "data-focused") == "false")
    before = seen("/threads/t1/seen")
    mock.activity("thread", "t1", "unread")
    until(lambda: seen("/threads/t1/seen") > before, timeout=4)
    check("a thread finishing in a chat pane that is drawn but not focused is read",
          seen("/threads/t1/seen") > before)

    _choose(page, until, "single")
    check("setup: one pane, showing the Browser; the chat is hidden",
          _attr(page, _pane(1), "data-view") == "browser" and not _visible(page, _pane(2)))
    before = seen("/threads/t1/seen")
    mock.activity("thread", "t1", "unread")
    time.sleep(0.8)
    check("a thread finishing while its chat is hidden is not read", seen("/threads/t1/seen") == before)
    mock.activity("thread", "t1", "idle")

    # The same for a task.
    _choose(page, until, "cols2")
    page.locator(_pane(1, '[data-testid="tab-browser"]')).click()
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

    # The task to pane 2 and the chat back to pane 1 by hand: choosing chat no
    # longer swaps two panes' views (WP-B).
    page.locator(_pane(2, '[data-testid="tab-task"]')).click()
    until(lambda: _attr(page, _pane(2), "data-view") == "task", timeout=2)
    page.locator(_pane(1, '[data-testid="tab-chat"]')).click()
    until(lambda: _attr(page, _pane(1), "data-view") == "chat", timeout=2)
    _choose(page, until, "single")
    check("setup: one pane showing the chat; the task is hidden",
          _attr(page, _pane(2), "data-view") == "task" and not _visible(page, _pane(2)))
    before = seen("/tasks/k1/seen")
    mock.activity("task", "k1", "unread")
    time.sleep(0.8)
    check("a task finishing while no pane shows it is not read", seen("/tasks/k1/seen") == before)
    mock.activity("task", "k1", "idle")
    _reset_all(page, mock, until)


# ---------------------------------------------------------------------------
# six and eight panes (WP-G1, docs/plans/2026-10-10-hud-split-panes-plan.md
# §3.1–§3.3, §3.7, §3.8; decisions S-1)

def _geo(page, ids) -> list:
    """Each pane's [x, y, offsetWidth, offsetHeight, data-slot] (offsets in the HUD's own px)."""
    return page.evaluate("""(ids) => ids.map(n => {
      const el = document.querySelector(`[data-testid="pane-${n}"]`);
      if (!el || el.offsetParent === null) return null;
      const r = el.getBoundingClientRect();
      return [Math.round(r.x), Math.round(r.y), el.offsetWidth, el.offsetHeight, el.getAttribute('data-slot')];
    })""", list(ids))


def _no_hscroll(page) -> tuple[bool, str]:
    got = page.evaluate("""() => {
      const d = document.documentElement, w = document.getElementById('workspace');
      return [d.scrollWidth, d.clientWidth, w.scrollWidth, w.clientWidth];
    }""")
    return got[0] <= got[1] and got[2] <= got[3], str(got)


def _reading_order(geo) -> bool:
    """Left to right, then top to bottom: each pane after the previous one."""
    pts = [(g[1], g[0]) for g in geo]
    return all(pts[i] < pts[i + 1] for i in range(len(pts) - 1))


def _show(page, until, n: int, view: str):
    """Pane N shows `view`: its words, or its `view ▾` menu when the pane is narrow."""
    sel = page.locator(f'[data-testid="pane-{n}-view-select"]')
    if sel.count():
        sel.select_option(view)
    else:
        page.locator(_pane(n, f'[data-testid="tab-{view}"]')).click()
    until(lambda: _attr(page, _pane(n), "data-view") == view, timeout=2)


def _eight_checks(page, mock, check, until):
    """The owner's words: "I'm not seeing the 6 and 8 panel layouts". ⊞ offers
    them; at 1920x1080 every pane is drawn at least the pane minimum with no
    horizontal scroll, and every pane can show chat, file, preview, diff or a
    terminal; smaller windows draw what fits and say so; Ctrl+Alt+N is a slot,
    never an id; 3 over 2's bottom row has edges of its own; and over eight
    panes holding Monaco, a preview frame, a terminal and chats the card is
    still whole and on top."""
    from tests.face.hud_v2_mock_terminals import row
    ws = '[data-testid="workspace"]'
    # A terminal to show in a pane, there before the window lists them.
    tid = "0e0e0e08"
    if not any(r["id"] == tid for r in mock.world.setdefault("terminals", [])):
        mock.world["terminals"].append(row(tid, "bash · eight"))
    _reset_all(page, mock, until, (1920, 1080))
    _open_layout_menu(page, until)
    tiles = [t.get_attribute("data-testid") for t in page.locator('[data-testid^="layout-preset-"]').all()]
    check("⊞ offers eight shapes, four to a row, 3×2 and 4×2 among them",
          tiles == [f"layout-preset-{p}" for p in MENU_TILES], str(tiles))
    boxes = [page.locator(f'[data-testid="layout-preset-{p}"]').bounding_box() for p in ("single", "grid4", "main2")]
    check("the tiles sit four to a row: the fifth starts the second row",
          abs(boxes[0]["x"] - boxes[2]["x"]) < 1 and boxes[2]["y"] > boxes[0]["y"]
          and boxes[1]["y"] > boxes[0]["y"], str(boxes))
    check("its head counts the panes", page.locator('[data-testid="layout-head"]').inner_text().strip().lower()
          == "layout · 1 pane", page.locator('[data-testid="layout-head"]').inner_text())
    page.keyboard.press("Escape")

    for preset, n in (("grid6", 6), ("grid8", 8)):
        _choose(page, until, preset)
        geo = _geo(page, range(1, n + 1))
        check(f"⊞ → {n}: all {n} panes drawn, at 1920x1080",
              _attr(page, ws, "data-drawn-preset") == preset and all(geo)
              and page.locator('[data-testid="layout-badge"]').count() == 0, str(geo))
        check(f"⊞ → {n}: every pane at least the 360px minimum (less the 1px rules between them)",
              all(g and g[2] >= 358 for g in geo), str([g and g[2] for g in geo]))
        check(f"⊞ → {n}: in reading order, each pane carrying its slot",
              all(geo) and _reading_order(geo) and [g[4] for g in geo] == [str(i) for i in range(1, n + 1)],
              str(geo))
        ok, why = _no_hscroll(page)
        check(f"⊞ → {n}: and nothing scrolls sideways", ok, why)
        _open_layout_menu(page, until)
        check(f"⊞ → {n}: the menu checks that tile and counts {n} panes",
              _attr(page, f'[data-testid="layout-preset-{preset}"]', "aria-checked") == "true"
              and page.locator('[data-testid="layout-head"]').inner_text().strip().lower() == f"layout · {n} panes")
        page.keyboard.press("Escape")
        until(lambda: page.locator('[data-testid="layout-menu"]').count() == 0, timeout=2)
    geo12 = _geo(page, range(1, 13))
    geo8 = [g for g in geo12[:8] if g]
    check("4×2: two rows of four, the halves equal (and no id above 8 drawn)",
          len(geo8) == 8 and geo12[8:] == [None] * 4
          and len({g[1] for g in geo8}) == 2 and len({g[0] for g in geo8}) == 4
          and max(g[2] for g in geo8) - min(g[2] for g in geo8) <= 2, str(geo12))

    # Every pane can show what any pane shows: chat, file, preview, diff, a terminal.
    views = {1: "chat", 2: "file", 3: "browser", 4: "diff", 5: "terminal", 6: "chat", 7: "file", 8: "browser"}
    for n, view in views.items():
        _show(page, until, n, view)
    page.locator('[data-testid="task-k1"]').click()
    until(lambda: page.locator(_pane(4, '[data-testid="difflist"]')).count() > 0, timeout=4)
    until(lambda: page.locator(f'[data-testid="pane-5-pick-{tid}"]').count() > 0, timeout=4)
    page.locator(f'[data-testid="pane-5-pick-{tid}"]').click()
    term = f'[data-testid="terminal-{tid}"]'
    until(lambda: page.locator(_pane(5, term)).count() > 0 and _attr(page, term, "data-state") == "attached", timeout=6)
    page.locator(_pane(7, '[data-testid="file-calc.py"]')).click()
    until(lambda: page.locator(_pane(7, '[data-testid="file-path"]')).inner_text() == "calc.py", timeout=4)
    _project_root(page, 8)
    until(lambda: page.locator(_pane(8, "iframe")).count() > 0, timeout=4)
    shows = {
        1: _visible(page, _pane(1, '[data-testid="input"]')),
        2: _visible(page, _pane(2, '[data-testid="filetree"]')) or _visible(page, _pane(2, '[data-testid="tree-toggle"]')),
        3: _visible(page, _pane(3, '[data-testid="browser-url"]')),
        4: page.locator(_pane(4, '[data-testid="difflist"]')).count() > 0,
        5: _visible(page, _pane(5, f"{term} .xterm-screen")),
        6: _visible(page, _pane(6, '[data-testid="input"]')),
        7: page.locator(_pane(7, '[data-testid="file-path"]')).inner_text() == "calc.py",
        8: _visible(page, _pane(8, "iframe")),
    }
    check("in 4×2 every pane shows what it was given: two chats, two files, two Browsers, a diff, a terminal",
          all(shows.values()) and all(_attr(page, _pane(n), "data-view") == v for n, v in views.items()), str(shows))
    check("the chats in panes 1 and 6 are two conversations, each with its own box",
          page.locator(_pane(1, '[data-testid="input"]')).count() == 1
          and page.locator(_pane(6, '[data-testid="input"]')).count() == 1
          and page.locator('[data-testid="compose-row"]').count() == 2)
    page.evaluate("document.querySelector('[data-testid=\"pane-8\"] iframe').__kept = 88")
    _choose(page, until, "single")
    check("⊞ to one pane: panes 2–8 are hidden, not unmounted",
          all(page.locator(_pane(n)).count() == 1 and not _visible(page, _pane(n)) for n in range(2, 9))
          and page.evaluate("document.querySelector('[data-testid=\"pane-8\"] iframe').__kept") == 88)
    _choose(page, until, "grid8")
    check("and back to eight: the very panes, still showing what they showed (pane 8's frame not reloaded)",
          all(_attr(page, _pane(n), "data-view") == v for n, v in views.items())
          and page.locator(_pane(7, '[data-testid="file-path"]')).inner_text() == "calc.py"
          and _visible(page, _pane(8, "iframe")) and _visible(page, _pane(5, f"{term} .xterm-screen"))
          and page.evaluate("document.querySelector('[data-testid=\"pane-8\"] iframe').__kept") == 88)

    # Smaller windows draw what fits, and say so.
    page.set_viewport_size({"width": 1280, "height": 800})
    page.locator(_pane(1, '[data-testid="input"]')).click()
    until(lambda: _attr(page, ws, "data-drawn-preset") == "grid6", timeout=3)
    note = ""
    _open_layout_menu(page, until)
    if page.locator('[data-testid="layout-dropped"]').count():
        note = page.locator('[data-testid="layout-dropped"]').inner_text()
    page.keyboard.press("Escape")
    drawn = [n for n in range(1, 13) if _visible(page, _pane(n))]
    check("1280x800: eight panes draw as six, with a badge and \"Showing 6 of 8\"",
          _attr(page, ws, "data-drawn-preset") == "grid6" and len(drawn) == 6
          and page.locator('[data-testid="layout-badge"]').inner_text().strip() == "2"
          and "Showing 6 of 8 panes" in note, f"{drawn} {note!r}")
    check("1280x800: the focused pane is one of them, and every drawn pane keeps the minimum",
          1 in drawn and all(g and g[2] >= 358 for g in _geo(page, drawn)), str(_geo(page, drawn)))
    _set_storage(page, zoom="150")
    _boot(page, mock, None, until, reload=True)
    drawn = [n for n in range(1, 13) if _visible(page, _pane(n))]
    check("1280x800 at 150%: four of the eight", _attr(page, ws, "data-drawn-preset") == "grid4" and len(drawn) == 4
          and page.locator('[data-testid="layout-badge"]').inner_text().strip() == "4", str(drawn))
    check("setup: pane 7 is not among them", 7 not in drawn, str(drawn))
    _press_on_body(page, "Control+Alt+7")
    until(lambda: _visible(page, _pane(7)), timeout=3)
    check("Ctrl+Alt+7 brings the dropped pane 7 into the drawn set, focused",
          _visible(page, _pane(7)) and _attr(page, _pane(7), "data-focused") == "true"
          and _attr(page, ws, "data-drawn-preset") == "grid4" and _last_key(page).get("prevented") is True)

    # Ctrl+Alt+N is a slot, never an id: a stored order of 1, 5, 2.
    page.set_viewport_size({"width": 1920, "height": 1080})
    _set_storage(page, zoom="100", layout="{}")
    # Pane 2 (slot 3) holds the same terminal as pane 5 (slot 2): one terminal
    # is drawn in one place, the first in reading order, so pane 2 says where.
    # The panel's selected tab is that terminal too (review of PR #37: both
    # "shown in pane N" labels survived an id-for-slot mutant).
    _set_ws(page, json.dumps({"preset": "cols3", "order": [1, 5, 2], "focused": 1,
                              "panes": [{"view": "chat"}, {"view": "terminal", "terminalId": tid}, {"view": "file"},
                                        {"view": "task"}, {"view": "terminal", "terminalId": tid}]}))
    page.evaluate("t => localStorage.setItem('jarvis.hud.terminals', JSON.stringify({active: t, specs: {}}))", tid)
    _boot(page, mock, None, until, reload=True)
    g = _geo(page, (1, 5, 2))
    check("a stored order [1, 5, 2] draws pane 5 in the middle, slot 2",
          all(g) and g[0][0] < g[1][0] < g[2][0] and [x[4] for x in g] == ["1", "2", "3"], str(g))
    _press_on_body(page, "Control+Alt+2")
    until(lambda: _attr(page, _pane(5), "data-focused") == "true", timeout=2)
    check("Ctrl+Alt+2 focuses the pane in slot 2 (id 5), not pane 2",
          _attr(page, _pane(5), "data-focused") == "true" and _attr(page, _pane(2), "data-focused") == "false")
    _press_on_body(page, "Control+Alt+3")
    until(lambda: _attr(page, _pane(2), "data-focused") == "true", timeout=2)
    check("and Ctrl+Alt+3 the pane in slot 3 (id 2)", _attr(page, _pane(2), "data-focused") == "true")
    _press_on_body(page, "Control+Alt+4")
    time.sleep(0.15)
    check("Ctrl+Alt+4 does nothing in three panes and is left to the browser",
          _attr(page, _pane(2), "data-focused") == "true" and _last_key(page).get("prevented") is False)
    check("the panes' labels name their slots", _attr(page, _pane(5), "aria-label").startswith("Pane 2:"),
          str(_attr(page, _pane(5), "aria-label")))
    elsewhere = page.locator('[data-testid="pane-2-terminal-elsewhere"]')
    until(lambda: elsewhere.count() > 0, timeout=3)
    check("pane 2 (slot 3), holding the terminal pane 5 draws, says \"shown in pane 2\" — the slot, not id 5",
          elsewhere.count() == 1 and "shown in pane 2." in elsewhere.inner_text()
          and page.locator(_pane(5, f'[data-testid="terminal-{tid}"]')).count() == 1,
          elsewhere.inner_text() if elsewhere.count() else "no notice")
    page.locator('[data-testid="toggle-panel"]').click()
    tab = page.locator(f'[data-testid="panel-tab-{tid}"]')
    until(lambda: tab.count() > 0 and "in pane" in tab.inner_text(), timeout=3)
    check("the terminal in pane 5 (slot 2): its panel tab says \"in pane 2\"",
          tab.count() == 1 and "in pane 2" in tab.inner_text() and tab.get_attribute("data-place") == "pane-5",
          tab.inner_text() if tab.count() else "no tab")
    there = page.locator('[data-testid="panel-elsewhere"]')
    until(lambda: there.count() > 0, timeout=3)
    check("and the panel, on that terminal's tab, says \"shown in pane 2\" too",
          there.count() == 1 and "shown in pane 2." in there.inner_text(),
          there.inner_text() if there.count() else "no notice")
    page.locator('[data-testid="toggle-panel"]').click()
    page.evaluate("localStorage.removeItem('jarvis.hud.terminals')")

    # A stored 3 over 2 has no tile in ⊞: the menu opens on a tile, never on a
    # toggle row (review of PR #37: Enter on the focused sidebar toggle folded it).
    _set_ws(page, json.dumps({"preset": "grid5"}))
    _boot(page, mock, None, until, reload=True)
    _open_layout_menu(page, until)
    time.sleep(0.1)
    checked = [p for p in MENU_TILES if _attr(page, f'[data-testid="layout-preset-{p}"]', "aria-checked") == "true"]
    check("with 3 over 2 stored, ⊞ checks no tile and opens with focus on the first tile, not a toggle",
          checked == [] and _active_testid(page) == "layout-preset-single", f"{checked} {_active_testid(page)}")
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="layout-menu"]').count() == 0, timeout=2)
    check("and the sidebar is still open", _visible(page, "#sidebar"))

    # 3 over 2: the bottom row's edge is its own. FS3 (§3.3): y[x[1,2,3],
    # x[4,5]], the top row's edges split-x0-1 and split-x0-2, the bottom
    # row's split-x1-1 (none coincide, so none is linked), the row split-y-1.
    _set_ws(page, json.dumps({"preset": "grid5"}))
    _boot(page, mock, None, until, reload=True)
    top0 = [g[2] for g in _geo(page, (1, 2, 3))]
    bot0 = [g[2] for g in _geo(page, (4, 5))]
    ids5 = _pane_edges(page)
    check("3 over 2: three panes over two, the bottom row's edges its own",
          _attr(page, ws, "data-drawn-preset") == "grid5" and page.locator('[data-testid="split-x1-1"]').count() == 1
          and page.locator('[data-testid="split-x0-2"]').count() == 1 and abs(bot0[0] - bot0[1]) <= 2
          and ids5 == ["split-x0-1", "split-x0-2", "split-x1-1", "split-y-1"], f"{top0} {bot0} {ids5}")
    if page.locator('[data-testid="split-x1-1"]').count():
        _drag(page, '[data-testid="split-x1-1"]', 120)
    top1 = [g[2] for g in _geo(page, (1, 2, 3))]
    bot1 = [g[2] for g in _geo(page, (4, 5))]
    check("in grid5, dragging split-x1-1 moves only the bottom row",
          abs(bot1[0] - (bot0[0] + 120)) <= 3 and abs(bot1[1] - (bot0[1] - 120)) <= 3 and top1 == top0,
          f"top {top0} -> {top1}, bottom {bot0} -> {bot1}")
    if page.locator('[data-testid="split-x0-1"]').count():
        _drag(page, '[data-testid="split-x0-1"]', -60)
    top2 = [g[2] for g in _geo(page, (1, 2, 3))]
    bot2 = [g[2] for g in _geo(page, (4, 5))]
    check("and the top row's edge only the top row's", bot2 == bot1 and abs(top2[0] - (top1[0] - 60)) <= 3,
          f"top {top1} -> {top2}, bottom {bot1} -> {bot2}")
    stored = json.loads(page.evaluate("localStorage.getItem('jarvis.hud.workspace')") or "{}")
    rows = (stored.get("tree") or {}).get("children") or [{}, {}]
    sizes = [r.get("sizes", []) if isinstance(r, dict) else [] for r in rows]
    check("both survive in what is stored: the tree's two rows each with sizes of their own (FS3, v2)",
          stored.get("version") == 2 and len(sizes) == 2 and len(sizes[0]) == 3 and len(sizes[1]) == 2
          and abs(sizes[1][0] - 0.5) > 0.02 and abs(sizes[0][0] - 1 / 3) > 0.01, str(stored.get("tree")))

    # Eight panes under a card: Monaco, a preview frame, a terminal and chats.
    grid8 = json.dumps({"preset": "grid8", "focused": 1, "panes": [
        {"view": "chat"}, {"view": "file"}, {"view": "browser"}, {"view": "task"},
        {"view": "terminal", "terminalId": tid}, {"view": "chat"}, {"view": "chat"}, {"view": "file"}]})
    fake = mock.fake_pty
    for zoom in ("160", "70"):
        page.set_viewport_size({"width": 1920, "height": 1080})
        _set_storage(page, zoom=zoom, layout="{}")
        _set_ws(page, grid8)
        _boot(page, mock, None, until, reload=True)
        page.locator(_pane(2, '[data-testid="file-calc.py"]')).click()
        until(lambda: page.locator(_pane(2, '[data-testid="editor"] .view-line')).count() > 0
              or page.locator(_pane(2, '[data-testid="editor-fallback"]')).count() > 0, timeout=10)
        _project_root(page, 3)
        until(lambda: page.locator(_pane(3, "iframe")).count() > 0, timeout=4)
        until(lambda: page.locator(term).count() > 0 and _attr(page, term, "data-state") == "attached", timeout=6)
        drawn = [n for n in range(1, 13) if _visible(page, _pane(n))]
        check(f"at {zoom}% on 1920x1080 eight panes draw ({len(drawn)} of them), Monaco, a frame and a terminal on screen",
              {1, 2, 3, 5, 6}.issubset(drawn) and _visible(page, _pane(2, '[data-testid="editor"]'))
              and _visible(page, _pane(3, "iframe")) and _visible(page, _pane(5, f"{term} .xterm-screen")),
              f"{drawn} {_attr(page, ws, 'data-drawn-preset')}")
        page.locator(_pane(5, f"{term} .xterm-screen")).click(position={"x": 20, "y": 8})
        sent = len(fake.sent(tid))
        req = f"eight{zoom}"
        _approval(mock, req, LONG_COMMAND)
        until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
        time.sleep(0.2)
        if zoom == "160":
            ok, why = _authorize_in_reach(page)
            check(f"at {zoom}% over eight panes, AUTHORIZE is in reach before any scrolling (AS-12)", ok, why)
        ok, why = _card_on_screen(page)
        check(f"at {zoom}% over eight panes, the card is wholly on screen and every button topmost at its point", ok, why)
        got = page.evaluate("""() => {
          const b = [...document.querySelectorAll('[data-testid$="-split-right"], [data-testid$="-split-down"], [data-testid$="-close"]')]
            .filter(e => /^pane-\\d+-(split-right|split-down|close)$/.test(e.getAttribute('data-testid')));
          const panes = [...document.querySelectorAll('section.wpane')];
          const drawn = panes.filter(e => e.offsetParent !== null).length;
          return [panes.length, drawn, b.length, b.filter(e => !e.disabled).map(e => e.getAttribute('data-testid'))];
        }""")
        # Two split buttons in every mounted pane (FS4), a × in every drawn one: all disabled.
        check(f"at {zoom}% under the card every pane's split and × is disabled (WP-G2)",
              got[1] >= 4 and got[2] == 2 * got[0] + got[1] and got[3] == [], str(got))
        page.keyboard.type("y")
        page.keyboard.press("Enter")
        page.wait_for_timeout(150)
        check(f"at {zoom}% a y⏎ typed with a terminal among eight panes reaches no shell",
              fake.sent(tid)[sent:] == b"", repr(fake.sent(tid)[sent:]))
        page.locator('[data-testid="approval-card"] button.deny').click()
        until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    _reset_all(page, mock, until)


# ---------------------------------------------------------------------------
# a render error must not take the window, or the card, with it (review of
# PR #29: three quick clicks on ⊞ unmounted the whole root)

# Three clicks in one task, no await between them: the first update is still
# pending when the second click runs, which is when React deferred the old
# updater to render time and `e.currentTarget` was null there.
TRIPLE_CLICK = """(id) => {
  const b = document.querySelector(`[data-testid="${id}"]`);
  if (!b) return false;
  b.click(); b.click(); b.click();
  return true;
}"""


def _window_whole(page) -> tuple[bool, str]:
    """The window is still drawn: #root has children, the title bar and the
    sidebar are there, and no crash prompt stands in their place."""
    got = page.evaluate("""() => [document.getElementById('root').children.length,
      !!document.querySelector('[data-testid="titlebar"]'), !!document.querySelector('[data-testid="sidebar"]'),
      !!document.querySelector('[data-testid="hud-crashed"]')]""")
    return got[0] > 0 and got[1] and got[2] and not got[3], f"root children, titlebar, sidebar, crashed = {got}"


def _crash(page, until) -> bool:
    """Make a workspace child throw (the test-only probe in components/Boundary.tsx)
    and draw the window again; true once the Reload prompt is up."""
    page.evaluate("window.__hudCrashProbe = 'workspace'")
    # Any store change draws the workspace again, and the probe throws.
    page.evaluate("window.__hud.dispatch({type: 'patch', patch: {error: 'crash probe render'}})")
    return bool(until(lambda: page.locator('[data-testid="hud-crashed"]').count() > 0, timeout=4))


# The Reload prompt can be neither reached nor focused behind a card: inert,
# and a scripted focus() does not take.
RELOAD_HELD = """() => {
  const b = document.querySelector('[data-testid="hud-crashed-reload"]');
  if (!b) return [false, 'no Reload'];
  const inert = !!b.closest('[inert]');
  b.focus();
  const took = document.activeElement === b;
  return [inert && !took, 'inert ' + inert + ', focus took ' + took];
}"""


def _sends(mock) -> int:
    """Every message the window has sent or thread it has opened to send one."""
    return sum(1 for m, p, _ in mock.calls if m == "POST" and (p == "/threads" or p.endswith("/send")))


def _reload_to_window(page, mock, until, act) -> bool:
    """Do `act` (which reloads the page) and wait for the window to boot again."""
    before = mock.sse_connections()
    act()
    try:
        page.wait_for_selector('[data-testid="sidebar"]', state="attached", timeout=5000)
    except Exception:
        return False
    mock.await_reconnect(before)
    time.sleep(0.2)
    _probe(page)
    return True


def _crash_checks(page, mock, check, until):
    errors: list[str] = []
    listen = lambda e: errors.append(str(e))  # noqa: E731
    page.on("pageerror", listen)
    try:
        for size, opener, menu, what in (
            ((1280, 800), "layout-customize", "layout-menu", "⊞ (the layout menu)"),
            ((500, 700), "layout-customize", "layout-menu", "⊞ at narrow width"),
        ):
            _reset_all(page, mock, until, size=size)
            errors.clear()
            clicked = page.evaluate(TRIPLE_CLICK, opener)
            time.sleep(0.4)
            ok, why = _window_whole(page)
            check(f"three quick clicks on {what} leave the window drawn", clicked and ok, why)
            check("and raise no page error", not errors, "; ".join(errors[:2]))
            state = page.evaluate(f"""() => [!!document.querySelector('[data-testid="{menu}"]'),
              document.querySelector('[data-testid="{opener}"]')?.getAttribute('aria-expanded')]""")
            check("and the menu ends open (three toggles), its button saying so", state == [True, "true"], str(state))
            page.keyboard.press("Escape")
            until(lambda: page.locator(f'[data-testid="{menu}"]').count() == 0, timeout=2)

        # The boundary: a workspace child that throws while a card is up. The
        # probe (components/Boundary.tsx) is test-only: it throws only while a
        # page global names it, and nothing but a test sets that.
        _reset_all(page, mock, until)
        errors.clear()
        _approval(mock, "crash1", "rm -rf /home/johnw/projects/scratch")
        until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
        time.sleep(0.2)
        _crash(page, until)
        time.sleep(0.2)
        check("a workspace render error shows the Reload prompt in place of the window",
              page.locator('[data-testid="hud-crashed"]').count() == 1
              and page.locator('[data-testid="titlebar"]').count() == 0
              and page.locator('[data-testid="hud-crashed-reload"]').inner_text().strip() == "Reload")
        check("and warns, in words, that a reload loses what was not sent",
              "Reloading loses anything not yet sent: words typed in a box and files staged for it."
              in page.locator('[data-testid="hud-crashed"]').inner_text())
        check("and is caught: no page error", not errors, "; ".join(errors[:2]))
        cmd = page.locator('[data-testid="approval-command"]')
        check("the approval card is still on screen and readable, its whole command there",
              cmd.count() == 1 and "rm -rf /home/johnw/projects/scratch" in cmd.inner_text())
        ok, why = _card_on_screen(page)
        check("and its buttons are on screen and topmost (DENY clickable)", ok, why)
        ok, why = page.evaluate(RELOAD_HELD)
        check("card first, crash second: the Reload prompt behind the card is inert, not even script-focusable",
              ok, why)
        page.keyboard.press("Tab")
        page.keyboard.press("Tab")
        check("and Tab stays on the card (the Reload behind it is never reached)",
              page.evaluate("!!document.activeElement.closest('[data-testid=\"approval-card\"]')"),
              str(_active_testid(page)))
        page.keyboard.press("Escape")
        body = until(lambda: mock.sent("POST", "/approvals/crash1") or None, timeout=4)
        check("and Escape still denies it", bool(body) and body[-1].get("decision") == "deny",
              str(body[-1] if body else None))
        until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
        check("once the card is answered, the Reload prompt is not inert any more",
              page.evaluate("!document.querySelector('[data-testid=\"hud-crashed\"]').closest('[inert]')"))
        reloaded = _reload_to_window(page, mock, until,
                                     lambda: page.locator('[data-testid="hud-crashed-reload"]').click())
        ok, why = _window_whole(page)
        check("and Reload brings the window back", reloaded and ok, why)
        check("with no page error throughout", not errors, "; ".join(errors[:2]))

        # Crash first, card second: the veil's own pass makes the prompt inert.
        _reset_all(page, mock, until)
        _crash(page, until)
        _approval(mock, "crash2")
        until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
        time.sleep(0.2)
        ok, why = page.evaluate(RELOAD_HELD)
        check("crash first, card second: the Reload prompt is inert behind the card too", ok, why)
        page.keyboard.press("Escape")
        body = until(lambda: mock.sent("POST", "/approvals/crash2") or None, timeout=4)
        check("and Escape denies that card", bool(body) and body[-1].get("decision") == "deny",
              str(body[-1] if body else None))
        until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)

        # The microphone after a crash: nothing heard is sent, to a chat the
        # owner cannot see or anywhere else. AUTO, so a transcript would send.
        page.evaluate("localStorage.setItem('jarvis.dictation', 'auto')")
        _reset_all(page, mock, until)
        errors.clear()
        check("setup: AUTO, and the local wake word listening",
              bool(until(lambda: page.evaluate("window.__hud.mic.wakeRunning()"), timeout=30))
              and page.locator('[data-testid="dictation-auto"]').get_attribute("aria-pressed") == "true")
        _crash(page, until)
        stt0, sends0 = len(mock.sent("POST", "/stt")), _sends(mock)
        page.evaluate("""() => { const m = window.__hud.mic; m.feedMs(1500, 0.001);
          window.__hud.capture.openFollowUp(); m.feedMs(1600, 0.06); m.feedMs(2800, 0.0005);
          window.__hud.mic.wake(); m.feedMs(1600, 0.06); m.feedMs(2800, 0.0005); }""")
        time.sleep(0.8)
        check("after a crash, speech in the follow-up window or after a wake hit uploads nothing",
              len(mock.sent("POST", "/stt")) == stt0 and _sends(mock) == sends0,
              f"{len(mock.sent('POST', '/stt')) - stt0} uploads, {_sends(mock) - sends0} sends")
        check("and the local wake word hears nothing", not page.evaluate("window.__hud.mic.wakeRunning()"))
        page.locator('[data-testid="hud-crashed-reload"]').focus()
        ptt: list[object] = []
        reloaded = _reload_to_window(page, mock, until, lambda: ptt.append(_ptt_on_space(page)))
        check("Space on the focused Reload never starts the mic", ptt == [None], str(ptt))
        ok, why = _window_whole(page)
        check("it presses Reload: the window comes back", reloaded and ok, why)
        check("with no page error", not errors, "; ".join(errors[:2]))

        # A transcript still in flight when the window crashes lands nowhere.
        _reset_all(page, mock, until)
        mock.stt_gate = threading.Event()
        try:
            stt0, sends0 = len(mock.sent("POST", "/stt")), _sends(mock)
            speak_when_listening(page, until)
            until(lambda: len(mock.sent("POST", "/stt")) > stt0, timeout=4)
            check("setup: an utterance is being transcribed", len(mock.sent("POST", "/stt")) > stt0)
            _crash(page, until)
        finally:
            mock.stt_gate.set()
        time.sleep(1.0)
        check("a transcript that lands after the crash is sent nowhere (AUTO)", _sends(mock) == sends0,
              f"{_sends(mock) - sends0} sends")
        mock.stt_gate = None
        page.evaluate("localStorage.removeItem('jarvis.dictation')")

        # A picker or dialog that throws is closed; the window carries on.
        _reset_all(page, mock, until)
        errors.clear()
        open_settings(page, "Model")
        page.locator('[data-testid="model-pin-more"]').click()
        until(lambda: page.locator('[data-testid="catalog"]').count() > 0, timeout=3)
        # The probe and the render that trips it in one evaluate: set apart, a
        # render of the window's own on a slow machine tripped it first, its
        # "dialog hit an error" was then overwritten by this patch's text, and
        # "and says so" below failed (20x CPU throttling: 3 of 3).
        page.evaluate("window.__hudCrashProbe = 'dialog';"
                      " window.__hud.dispatch({type: 'patch', patch: {error: 'dialog probe render'}})")
        until(lambda: page.locator('[data-testid="catalog"]').count() == 0, timeout=3)
        time.sleep(0.2)
        page.locator('[data-testid="settings-close"]').click()
        ok, why = _window_whole(page)
        check("a dialog that throws is closed and the window stays drawn",
              page.locator('[data-testid="catalog"]').count() == 0 and ok, why)
        # The sentence is drawn a render after the dialog goes (the boundary's
        # onCrash patches it in): under load one read 0.2 s later missed it.
        line = lambda: page.evaluate(  # noqa: E731
            "document.querySelector('[data-testid=\"error\"]')?.textContent") or ""
        until(lambda: "dialog hit an error" in line(), timeout=4)
        check("and says so", "dialog hit an error" in line(), repr(line()))
        check("and is caught: no page error", not errors, "; ".join(errors[:2]))
        page.evaluate("delete window.__hudCrashProbe")
        open_settings(page, "Model")
        page.locator('[data-testid="model-pin-more"]').click()
        until(lambda: page.locator('[data-testid="catalog"]').count() > 0, timeout=3)
        check("and the next picker opened is drawn", page.locator('[data-testid="catalog"]').count() > 0)
        page.keyboard.press("Escape")
        until(lambda: page.locator('[data-testid="catalog"]').count() == 0, timeout=2)

        page.locator('[data-testid="settings-close"]').click()
    finally:
        page.remove_listener("pageerror", listen)
        mock.stt_gate = None
    _reset_all(page, mock, until)


# ---------------------------------------------------------------------------
# WP-G2: the one-click split and the × (plan §3.4–§3.6, decisions S-1..S-8)

STYLE_JS = """(sel) => {
  const e = document.querySelector(sel);
  if (!e) return null;
  const c = getComputedStyle(e);
  return [e.offsetWidth, e.offsetHeight, c.color, c.backgroundColor, c.borderTopColor, c.borderRadius];
}"""


def _order(page) -> list:
    return _ws(page)["ws"]["order"]


def _drawn(page) -> list:
    return _ws(page)["fit"]["panes"]


def _close(page, until, n: int):
    """× on pane N, and the window settled (past the double-click guard)."""
    page.locator(f'[data-testid="pane-{n}-close"]').click()
    until(lambda: n not in _order(page), timeout=2)
    time.sleep(0.5)


# FS4: the directional split (docs/plans/2026-10-19-hud-free-split-plan.md, the
# FS4 card; decisions FS-1..FS-9). Two buttons in every pane's strip; each
# halves only its pane. The titles are the plan's §10.
SPLIT_RIGHT_OK = "Split right: a copy of this pane beside it"
SPLIT_DOWN_OK = "Split down: a copy of this pane below it"
ACROSS_TITLE = "Four panes across the window is the most"
DOWN_TITLE = "Three panes down the window is the most"
ROOM_RIGHT = "Not enough room to split right: widen the window, fold a side pane or zoom out (Ctrl+-)"
TWELVE_TITLE = "Twelve panes is the most"
FOLDED = json.dumps({"leftCollapsed": True, "rightCollapsed": True})
# Twelve panes on which FS-3 still allows a split right of 1 and 11 (the
# vitest suite's TWELVE_ROOM): only the id pool says no, so the title is "Twelve".
TWELVE_ROOM = {"axis": "x", "sizes": [0.5, 0.5], "children": [
    {"axis": "y", "sizes": [0.25, 0.25, 0.5], "children": [
        {"axis": "x", "sizes": [0.5, 0.5], "children": [1, 11]},
        {"axis": "x", "sizes": [0.25, 0.25, 0.5], "children": [8, 12, 10]},
        {"axis": "x", "sizes": [0.5, 0.5], "children": [5, 6]}]},
    {"axis": "y", "sizes": [0.5, 0.25, 0.25], "children": [
        2, {"axis": "x", "sizes": [0.5, 0.5], "children": [3, 9]}, {"axis": "x", "sizes": [0.5, 0.5], "children": [4, 7]}]}]}

# Every element's viewport box (null when absent or not drawn).
BOXES_JS = """(sels) => sels.map((s) => {
  const e = document.querySelector(s);
  if (!e || e.offsetParent === null) return null;
  const r = e.getBoundingClientRect();
  return [r.x, r.y, r.width, r.height];
})"""


# Split wherever a split fits: the first drawn pane, in reading order, whose
# split right (else down) the window takes; the new id, or null.
GROW_JS = """() => {
  for (const n of window.__hud.workspace().ws.order) for (const axis of ['x', 'y']) {
    const got = window.__hud.split(n, {axis});
    if (got !== null && got !== undefined) return got;
  }
  return null;
}"""


def _sb(n: int, d: str) -> str:
    """Pane N's split button, d = "right" or "down"."""
    return f'[data-testid="pane-{n}-split-{d}"]'


def _split_click(page, until, n: int, d: str) -> int:
    """Click pane N's split d; the new pane's id once the window has it."""
    before = list(_order(page))
    page.locator(_sb(n, d)).click()
    until(lambda: len(_order(page)) == len(before) + 1, timeout=3)
    return next(x for x in _order(page) if x not in before)


def _same_boxes(a, b, tol: float = 0.5) -> bool:
    return len(a) == len(b) and all(
        (x is None and y is None) or (x is not None and y is not None and all(abs(p - q) <= tol for p, q in zip(x, y)))
        for x, y in zip(a, b))


def _split_buttons_checks(page, mock, check, until):
    """FS-2: two buttons, split right and split down, in every pane's strip —
    the single layout's too — drawn as the title bar's toggles, saying what
    they do; Space presses one and is never push-to-talk; the one-click
    ladder button is gone."""
    _reset_all(page, mock, until, (1920, 1080))
    right1, down1 = page.locator(_sb(1, "right")), page.locator(_sb(1, "down"))
    check("FS4: the single layout's strip has both split buttons, and no ×",
          right1.count() == 1 and down1.count() == 1 and right1.is_visible() and down1.is_visible()
          and page.locator('[data-testid="pane-1-close"]').count() == 0)
    check("FS4: the one-click ladder button is gone", page.locator('[data-testid="pane-1-split"]').count() == 0)
    theirs = page.evaluate(STYLE_JS, '[data-testid="toggle-panel"]')
    for d in ("right", "down"):
        mine = page.evaluate(STYLE_JS, _sb(1, d))
        check(f"FS4: split {d} is the title bar's toggles' button: their computed size and colour", mine == theirs,
              f"{mine} vs {theirs}")
    check("FS4: each says what it does",
          right1.get_attribute("title") == SPLIT_RIGHT_OK and right1.get_attribute("aria-label") == "Split right"
          and down1.get_attribute("title") == SPLIT_DOWN_OK and down1.get_attribute("aria-label") == "Split down",
          f"{right1.get_attribute('title')!r} {down1.get_attribute('title')!r}")
    right1.focus()
    ptt = _ptt_on_space(page)
    until(lambda: len(_order(page)) == 2, timeout=2)
    check("FS4: Space on split right splits right, and is never push-to-talk",
          not ptt and _order(page) == [1, 2] and _attr(page, '[data-testid="workspace"]', "data-preset") == "cols2",
          f"ptt={ptt} {_order(page)}")
    until(lambda: _attr(page, _pane(2), "data-focused") == "true", timeout=2)
    check("FS4: the copy is right after its source (slot 2), focused, and both panes carry both buttons and a ×",
          _attr(page, _pane(2), "data-slot") == "2"
          and all(page.locator(_sb(n, d)).count() == 1 for n in (1, 2) for d in ("right", "down"))
          and all(page.locator(f'[data-testid="pane-{n}-close"]').count() == 1 for n in (1, 2)))
    _reset_all(page, mock, until)


def _split_room_checks(page, mock, check, until):
    """FS-3, FS-4, FS-7 at 1920×1080 with both side panes open (a 1368 px
    centre): split right gives two 684 px columns, and a third column (342 px)
    is disabled with the room title — never borrowing from the side panes;
    down works twice in a column, then that column says three down is the
    most. In a short window down says there is no room."""
    _reset_all(page, mock, until, (1920, 1080))
    sidebar = page.locator("#sidebar").bounding_box()
    _split_click(page, until, 1, "right")
    w1, w2 = _width(page, _pane(1)), _width(page, _pane(2))
    check("FS4: split right halves the one pane: two columns of 684 px", abs(w1 - 684) <= 1.5 and abs(w2 - 684) <= 1.5,
          f"{w1} {w2}")
    for n in (1, 2):
        b = page.locator(_sb(n, "right"))
        until(lambda: b.is_disabled(), timeout=2)
        check(f"FS-7: a third column (342 px) is disabled on pane {n}, saying how to get room",
              b.is_disabled() and b.get_attribute("title") == ROOM_RIGHT, str(b.get_attribute("title")))
    check("FS-7: and the side panes were never folded for it",
          _visible(page, "#sidebar") and page.locator("#sidebar").bounding_box() == sidebar
          and page.locator('[data-testid="rail-right"]').count() == 0)
    a = _split_click(page, until, 1, "down")
    b = _split_click(page, until, a, "down")
    check("FS4: down works twice in the left column: 1, then its two copies, each right after its source",
          _order(page) == [1, a, b, 2], str(_order(page)))
    for n in (1, a, b):
        btn = page.locator(_sb(n, "down"))
        until(lambda: btn.is_disabled(), timeout=2)
        check(f"FS-3: then down on pane {n} is disabled: three panes down is the most",
              btn.is_disabled() and btn.get_attribute("title") == DOWN_TITLE, str(btn.get_attribute("title")))
    check("FS-3: the right column still splits down", not page.locator(_sb(2, "down")).is_disabled())
    page.set_viewport_size({"width": 1920, "height": 700})
    _set_ws(page, None)
    _boot(page, mock, None, until, reload=True)
    _split_click(page, until, 1, "down")
    btn = page.locator(_sb(1, "down"))
    until(lambda: btn.is_disabled(), timeout=2)
    title = btn.get_attribute("title") or ""
    check("FS-4: 1920×700: a 336 px pane cannot split down, and says there is no room",
          btn.is_disabled() and title.startswith("Not enough room to split down") and "zoom out (Ctrl+-)" in title, title)
    _reset_all(page, mock, until)


def _twelve_build(page, mock, check, until, zoom: int):
    """Both side panes folded, the panel open at its minimum: build the 4×3,
    row by row, by clicking. Each split leaves every other pane, both rails
    and the panel where they were, to half a pixel (FS-5) [CSS grid rendering]."""
    size = (1920, 1080) if zoom == 100 else (2880, 1620)       # 1920×1080 of the HUD's own px either way
    page.set_viewport_size({"width": size[0], "height": size[1]})
    _set_storage(page, zoom=str(zoom), layout=FOLDED)
    _set_ws(page, json.dumps({"version": 2, "tree": 1, "kept": [], "focused": 1,
                              "panel": {"open": True, "height": 120}, "panes": [{"view": "chat"}]}))
    _boot(page, mock, None, until, reload=True)
    fixed = ['[data-testid="rail-left"]', '[data-testid="rail-right"]', '[data-testid="panel"]']
    check(f"FS-5 setup at {zoom}%: one pane, both rails, the panel open",
          _order(page) == [1] and all(page.evaluate(BOXES_JS, fixed)), str(page.evaluate(BOXES_JS, fixed)))
    moved = []

    def split(n: int, d: str) -> int:
        others = [x for x in _order(page) if x != n]
        sels = [_pane(x) for x in others] + fixed
        before = page.evaluate(BOXES_JS, sels)
        src = page.evaluate(BOXES_JS, [_pane(n)])[0]
        new = _split_click(page, until, n, d)
        time.sleep(0.15)
        after = page.evaluate(BOXES_JS, sels)
        if not _same_boxes(before, after):
            moved.append(f"{d} on {n}: {[(sels[i], before[i], after[i]) for i in range(len(sels)) if not _same_boxes([before[i]], [after[i]])][:2]}")
        halves = page.evaluate(BOXES_JS, [_pane(n), _pane(new)])
        i = 2 if d == "right" else 3
        if not (src and all(halves) and abs(halves[0][i] - halves[1][i]) <= 1.5
                and abs(halves[0][i] + halves[1][i] - src[i]) <= 3):
            moved.append(f"{d} on {n}: halves {halves} of {src}")
        return new

    a = split(1, "down")
    b = split(1, "down")
    across = {}
    for first in (1, b, a):                                  # top to bottom
        c = split(first, "right")
        split(first, "right")
        split(c, "right")
        across[first] = page.locator(_sb(first, "right")).get_attribute("title")
    check(f"FS-5 at {zoom}%: twelve panes by clicking, a full 4×3",
          len(_order(page)) == 12 and len(_drawn(page)) == 12, str(_order(page)))
    check(f"FS-5 at {zoom}%: every split left every other pane, both rails and the panel where they were (±0.5 px), "
          "and halved its own pane", not moved, "; ".join(moved[:3]))
    check(f"FS-3 at {zoom}%: four across: split right says four is the most",
          all(t == ACROSS_TITLE for t in across.values()), str(across))
    for n in _order(page):
        r, d = page.locator(_sb(n, "right")), page.locator(_sb(n, "down"))
        if not (r.is_disabled() and d.is_disabled() and r.get_attribute("title") == ACROSS_TITLE
                and d.get_attribute("title") == DOWN_TITLE):
            check(f"FS-3 at {zoom}%: in the 4×3 every split is disabled, across and down saying why", False,
                  f"pane {n}: {r.get_attribute('title')!r} {d.get_attribute('title')!r}")
            break
    else:
        check(f"FS-3 at {zoom}%: in the 4×3 every split is disabled, across and down saying why", True)


def _split_twelve_checks(page, mock, check, until):
    """FS-1/FS-3/FS-5: the full 4×3 by clicking at 100% and 150%, nothing
    else moving at any split; and with twelve panes where FS-3 would still
    allow a split, the title says twelve is the most."""
    _twelve_build(page, mock, check, until, 100)
    _twelve_build(page, mock, check, until, 150)
    page.set_viewport_size({"width": 3000, "height": 1700})
    _set_storage(page, zoom="100", layout=FOLDED)
    _set_ws(page, json.dumps({"version": 2, "tree": TWELVE_ROOM, "kept": [], "focused": 1,
                              "panes": [{"view": "chat"} for _ in range(12)]}))
    _boot(page, mock, None, until, reload=True)
    until(lambda: len(_drawn(page)) == 12, timeout=4)
    r1 = page.locator(_sb(1, "right"))
    check("FS-1: twelve panes where FS-3 and FS-4 would allow a split right: disabled, twelve is the most",
          r1.is_disabled() and r1.get_attribute("title") == TWELVE_TITLE, str(r1.get_attribute("title")))
    _reset_all(page, mock, until)


def _split_close_checks(page, mock, check, until):
    """× gives the closed pane's space to its neighbour in the same split
    (the one before it: left or above), and nothing else moves; a
    double-click on × closes one pane, the second click landing on the ×
    that moved under the pointer; under a card no split and no × does
    anything, from the pointer or the keyboard; down to one pane, no ×."""
    ws = '[data-testid="workspace"]'
    _reset_all(page, mock, until, (1920, 1080))
    a = _split_click(page, until, 1, "right")            # x[1, a]
    b = _split_click(page, until, 1, "down")             # x[y[1, b], a]
    c = _split_click(page, until, b, "down")             # x[y[1, b, c], a]
    check("× setup: the left column holds 1, b, c; the right one a", _order(page) == [1, b, c, a], str(_order(page)))
    sels = [_pane(n) for n in (1, b, c, a)]
    before = page.evaluate(BOXES_JS, sels)
    _close(page, until, c)
    after = page.evaluate(BOXES_JS, [_pane(n) for n in (1, b, a)])
    check("× on the lowest pane of a column gives its space to the pane above it; nothing else moves",
          _same_boxes([before[0], before[3]], [after[0], after[2]])
          and abs(after[1][1] - before[1][1]) <= 0.5 and abs(after[1][3] - (before[1][3] + before[2][3] + 1)) <= 1.5,
          f"{before} -> {after}")
    before = page.evaluate(BOXES_JS, [_pane(n) for n in (1, b, a)])
    _close(page, until, a)
    after = page.evaluate(BOXES_JS, [_pane(n) for n in (1, b)])
    check("× on the right column gives its space to the column on its left: both its panes widen, their heights kept",
          all(abs(after[i][0] - before[i][0]) <= 0.5 and abs(after[i][1] - before[i][1]) <= 0.5
              and abs(after[i][3] - before[i][3]) <= 0.5 and abs(after[i][2] - (before[i][2] + before[2][2] + 1)) <= 1.5
              for i in (0, 1)), f"{before} -> {after}")

    # A double-click on the right column's ×: after the first click the middle
    # pane grows right, its × under the pointer. One pane goes, never two.
    _choose(page, until, "cols3")
    order = _order(page)
    x3 = page.locator(f'[data-testid="pane-{order[2]}-close"]').bounding_box()
    page.mouse.dblclick(x3["x"] + x3["width"] / 2, x3["y"] + x3["height"] / 2)
    until(lambda: order[2] not in _order(page), timeout=2)
    time.sleep(0.6)
    check("a double-click on × closes one pane: three columns → two, never one",
          _order(page) == order[:2], str(_order(page)))
    landed = page.locator(f'[data-testid="pane-{order[1]}-close"]').bounding_box()
    check("(the pane that took the space has its × where the double-click landed)",
          abs(landed["x"] - x3["x"]) < 3 and abs(landed["y"] - x3["y"]) < 3, f"{landed} vs {x3}")

    # Under a card: every split and ×, and Enter or Space on a focused one, do nothing.
    page.locator(_sb(order[0], "down")).focus()
    before = _order(page)
    _approval(mock, "split-card")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    time.sleep(0.2)
    got = page.evaluate("""() => [...document.querySelectorAll('button')]
      .filter(b => /^pane-\\d+-(split-right|split-down|close)$/.test(b.getAttribute('data-testid') || ''))
      .map(b => [b.getAttribute('data-testid'), b.disabled])""")
    check("under a card both split buttons and the × of every pane are disabled",
          len(got) >= 3 * len(before) and all(d for _, d in got), str(got))
    check("and the card took focus off the split button", _active_testid(page) != f"pane-{order[0]}-split-down",
          str(_active_testid(page)))
    page.keyboard.press("Enter")
    page.keyboard.press("Space")
    time.sleep(0.3)
    check("Enter and Space under the card split and close nothing", _order(page) == before, str(_order(page)))
    page.locator('[data-testid="approval-card"] button.deny').click()
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)

    while len(_order(page)) > 1:
        _close(page, until, _order(page)[-1])
    check("closed down to one pane: single, and no ×", _attr(page, ws, "data-preset") == "single"
          and page.locator('[data-testid$="-close"]').count() == 0 and page.locator(_sb(_order(page)[0], "right")).is_visible())
    _reset_all(page, mock, until)


def _split_close_one_checks(page, mock, check, until):
    """After pane 1 is closed nothing assumes it is there (plan G-1): the
    header id, Ctrl+Alt+1, the window's chat, a reload and the next split all
    follow the slot."""
    ws = '[data-testid="workspace"]'
    page.set_viewport_size({"width": 1920, "height": 1080})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, json.dumps({"preset": "cols2", "focused": 1, "panes": [{"view": "chat"}, {"view": "chat"}]}))
    _boot(page, mock, None, until, reload=True)
    until(lambda: page.evaluate("window.__hud.state().chats['2'].compose") is not None, timeout=3)
    _close(page, until, 1)
    check("× on pane 1: one pane left, pane 2, and pane 1 is unmounted",
          _order(page) == [2] and _attr(page, ws, "data-preset") == "single" and page.locator(_pane(1)).count() == 0)
    check("the slot-1 header is pane 2's now", page.locator(_pane(2, "#tabs")).count() == 1)
    st = page.evaluate("(() => { const s = window.__hud.state(); return [s.selectedChat, s.home, !!s.compose]; })()")
    check("the window's chat is pane 2's: selected, home, its compose row", st == [2, 2, True], str(st))
    box = page.locator(_pane(2, '[data-testid="input"]'))
    box.click()
    box.fill("still here")
    check("pane 2's box takes typing", page.evaluate("window.__hud.state().input") == "still here")
    _press_on_body(page, "Control+Alt+1")
    time.sleep(0.15)
    check("Ctrl+Alt+1 focuses the pane in slot 1, pane 2", _attr(page, _pane(2), "data-focused") == "true"
          and _last_key(page).get("prevented") is True)
    _boot(page, mock, None, until, reload=True)
    check("a reload keeps the order without pane 1", _order(page) == [2] and page.locator(_pane(1)).count() == 0
          and _visible(page, _pane(2, '[data-testid="input"]')), str(_order(page)))
    st = page.evaluate("(() => { const s = window.__hud.state(); return [s.selectedChat, s.home]; })()")
    check("and after it the window's chat is still pane 2's", st == [2, 2], str(st))
    page.locator('[data-testid="pane-2-split-right"]').click()
    until(lambda: len(_order(page)) == 2, timeout=2)
    check("the next split takes the free id 1, in slot 2 after its source",
          _order(page) == [2, 1] and _attr(page, _pane(1), "data-slot") == "2", str(_order(page)))
    _reset_all(page, mock, until)


# ---------------------------------------------------------------------------
# FS3 (plan §10, P-4): moving panes never remounts, re-parents or reloads one.

# Tag every section, the Browser frame and each of its ancestors up to its
# section, Monaco's host and the terminal's xterm element with an expando.
REMOUNT_TAG = """() => {
  const tags = {};
  const tag = (el, name) => { if (el) { el.__fs3 = name; tags[name] = true; } };
  for (const n of [1, 2, 3, 4]) tag(document.querySelector(`[data-testid="pane-${n}"]`), `pane-${n}`);
  const frame = document.querySelector('[data-testid="pane-3"] iframe[data-browser-frame]');
  tag(frame, 'frame');
  let up = frame && frame.parentElement, i = 0;
  while (up && up.getAttribute('data-testid') !== 'pane-3') { tag(up, `frame-up-${i++}`); up = up.parentElement; }
  tag(document.querySelector('[data-testid="pane-2"] [data-testid="editor"]'), 'monaco');
  tag(document.querySelector('[data-testid="pane-4"] .xterm'), 'xterm');
  return Object.keys(tags);
}"""

# Each tagged element is still the one found at its place, connected; the
# sections are direct children of #workspace, in id order.
REMOUNT_SAME = """(names) => {
  const bad = [];
  const at = (name) => {
    if (name.startsWith('pane-')) return document.querySelector(`[data-testid="${name}"]`);
    if (name === 'monaco') return document.querySelector('[data-testid="pane-2"] [data-testid="editor"]');
    if (name === 'xterm') return document.querySelector('[data-testid="pane-4"] .xterm');
    const frame = document.querySelector('[data-testid="pane-3"] iframe[data-browser-frame]');
    if (name === 'frame') return frame;
    let up = frame && frame.parentElement;
    for (let i = Number(name.slice('frame-up-'.length)); up && i > 0; i--) up = up.parentElement;
    return up;
  };
  for (const name of names) {
    const el = at(name);
    if (!el || !el.isConnected || el.__fs3 !== name) bad.push(name);
  }
  const ws = document.getElementById('workspace');
  const sections = [...document.querySelectorAll('section.wpane')];
  const flat = sections.every((s) => s.parentElement === ws);
  const ids = sections.map((s) => Number(s.getAttribute('data-testid').slice(5)));
  const sorted = ids.every((n, i) => i === 0 || ids[i - 1] < n);
  return { bad, flat, ids, sorted };
}"""


def _no_remount_checks(page, mock, check, until):
    """FS3 (plan §10, P-4, C5): the sections are one flat list in id order,
    each placed from its own box; a split, a close, ⊞, a drag or a resize
    changes only their style. Moving an iframe's ancestor reloads it, so every
    section, the Browser frame and its ancestors, Monaco's host and the
    terminal's xterm element carry a tag, and after ⊞ 2×2 → one → 2×2, a split
    (split down since FS4), a close of another pane, an edge drag and a window resize the very
    same elements are where they were, still in id order, the Browser page
    was never loaded again, no new terminal ticket was fetched, and Monaco
    holds the same models [rendering in slot order, or nested wrappers]."""
    from tests.face.hud_v2_mock_terminals import row
    ws = '[data-testid="workspace"]'
    tid = "0f0f0f03"
    if not any(r["id"] == tid for r in mock.world.setdefault("terminals", [])):
        mock.world["terminals"].append(row(tid, "bash · remount"))
    term = f'[data-testid="terminal-{tid}"]'
    url = f"http://127.0.0.1:{mock.dev_port}/focus-none?who=remount"
    page.set_viewport_size({"width": 1920, "height": 1080})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, json.dumps({"preset": "grid4", "focused": 1, "panes": [
        {"view": "chat"}, {"view": "file"}, {"view": "browser", "browserUrl": url},
        {"view": "terminal", "terminalId": tid}]}))
    _boot(page, mock, None, until, reload=True)
    page.locator(_pane(2, '[data-testid="file-calc.py"]')).click()
    until(lambda: page.locator(_pane(2, '[data-testid="editor"] .view-line')).count() > 0, timeout=10)
    until(lambda: page.locator(_pane(3, "iframe[data-browser-frame]")).count() > 0, timeout=4)
    until(lambda: page.locator(term).count() > 0 and _attr(page, term, "data-state") == "attached", timeout=6)
    until(lambda: any("who=remount" in h for h in mock.dev_hits), timeout=4)
    time.sleep(0.5)
    names = page.evaluate(REMOUNT_TAG)
    check("no-remount setup: 2×2 with a chat, Monaco, a Browser page and a terminal, every one tagged",
          {"pane-1", "pane-2", "pane-3", "pane-4", "frame", "monaco", "xterm"} <= set(names)
          and any(n.startswith("frame-up-") for n in names), str(names))
    hits = lambda: sum(1 for h in mock.dev_hits if "who=remount" in h)  # noqa: E731
    tickets = lambda: len(mock.sent("POST", f"/terminals/{tid}/ticket"))  # noqa: E731
    models = lambda: sorted(page.evaluate("window.__hud.monacoModels()"))  # noqa: E731
    hits0, tickets0, models0 = hits(), tickets(), models()

    def survived(move: str):
        time.sleep(0.4)
        got = page.evaluate(REMOUNT_SAME, names)
        check(f"no-remount, after {move}: every tagged element is the same one, in its place",
              got["bad"] == [], str(got["bad"]))
        check(f"no-remount, after {move}: the sections are flat children of #workspace, in id order (never slot order)",
              got["flat"] and got["sorted"], f'flat={got["flat"]} ids={got["ids"]}')
        check(f"no-remount, after {move}: the Browser page was not loaded again", hits() == hits0, f"{hits0} -> {hits()}")
        check(f"no-remount, after {move}: no new terminal ticket", tickets() == tickets0, f"{tickets0} -> {tickets()}")
        check(f"no-remount, after {move}: Monaco holds the same models", models() == models0, f"{models0} -> {models()}")

    _choose(page, until, "single")
    _choose(page, until, "grid4")
    until(lambda: all(_visible(page, _pane(n)) for n in (1, 2, 3, 4)), timeout=3)
    survived("⊞ 2×2 → one → 2×2")

    page.locator('[data-testid="pane-1-split-down"]').click()
    until(lambda: len(_order(page)) == 5, timeout=3)
    check("no-remount setup: split down halves pane 1, the new pane in slot 2 (a free layout)",
          _order(page) == [1, 5, 2, 3, 4] and _attr(page, ws, "data-drawn-preset") == "free", str(_order(page)))
    survived("a split down (slot order 1, 5, 2, 3, 4)")

    _close(page, until, 5)
    check("no-remount setup: × on the new pane, 2×2 again", _order(page) == [1, 2, 3, 4], str(_order(page)))
    survived("a close of another pane")

    edge = page.locator('[data-testid="split-x0-1"]')
    check("no-remount setup: 2×2 draws its linked column edge, split-x0-1", edge.count() == 1)
    if edge.count():
        w1 = _width(page, _pane(1))
        _drag(page, '[data-testid="split-x0-1"]', 90)
        check("no-remount setup: the drag moved the edge", abs(_width(page, _pane(1)) - (w1 + 90)) <= 3,
              f"{w1} -> {_width(page, _pane(1))}")
        survived("an edge drag")

    page.set_viewport_size({"width": 1600, "height": 900})
    until(lambda: abs(page.locator(ws).bounding_box()["width"] - (1600 - 236 - 316)) <= 2, timeout=3)
    page.set_viewport_size({"width": 1920, "height": 1080})
    until(lambda: abs(page.locator(ws).bounding_box()["width"] - (1920 - 236 - 316)) <= 2, timeout=3)
    survived("a window resize")
    _reset_all(page, mock, until)


# A Ctrl+Alt key as AltGr reports it (the symbol, on the digit's physical
# key), dispatched where the window listens: whether the window took it.
CTRL_ALT_JS = """([key, code]) => {
  const e = new KeyboardEvent('keydown', {key, code, ctrlKey: true, altKey: true, bubbles: true, cancelable: true});
  (document.activeElement || document.body).dispatchEvent(e);
  return e.defaultPrevented;
}"""


def _twelve_ids_checks(page, mock, check, until):
    """FS2 (decisions FS-1, FS-9): twelve pane ids (split right, then down, since FS4). ⊞ 4×2
    then one pane hides 2–8, and the next split takes the free id 9 — in
    slot 2, mounted fresh — never a hidden pane. Ctrl+Alt+N stays slots 1–8:
    Ctrl+Alt+2 is pane 9 when 9 is in slot 2, while Ctrl+Alt+9 and
    Ctrl+Alt+0 focus nothing (panes 9–12 are click-only) and are left to the
    browser, as are the ] and } AltGr types on the 9 and 0 keys."""
    ws = '[data-testid="workspace"]'
    _reset_all(page, mock, until, (1920, 1080))
    _choose(page, until, "grid8")
    _choose(page, until, "single")
    check("FS2 setup: ⊞ 4×2 then one pane: 2–8 hidden, still mounted",
          _order(page) == [1] and _ws(page)["ws"]["kept"] == [2, 3, 4, 5, 6, 7, 8]
          and all(page.locator(_pane(n)).count() == 1 and not _visible(page, _pane(n)) for n in range(2, 9)),
          f'{_order(page)} {_ws(page)["ws"]["kept"]}')
    page.locator('[data-testid="pane-1-split-right"]').click()
    until(lambda: len(_order(page)) == 2, timeout=2)
    check("FS2: the split after ⊞ 8 → 1 takes the free id 9, never a hidden pane",
          _order(page) == [1, 9] and _ws(page)["ws"]["kept"] == [2, 3, 4, 5, 6, 7, 8]
          and _attr(page, ws, "data-preset") == "cols2", f'{_order(page)} {_ws(page)["ws"]["kept"]}')
    nine = _pane(9)
    until(lambda: page.locator(nine).count() == 1 and _attr(page, nine, "data-focused") == "true", timeout=2)
    g = _geo(page, (1, 9))
    check('FS2: pane-9 mounts, drawn in slot 2 (data-slot="2"), focused, a chat with its own box',
          page.locator(nine).count() == 1 and _attr(page, nine, "data-slot") == "2" and all(g) and g[0][0] < g[1][0]
          and _attr(page, nine, "data-focused") == "true" and _attr(page, nine, "data-view") == "chat"
          and _attr(page, nine, "aria-label").startswith("Pane 2:")
          and page.locator(_pane(9, '[data-testid="input"]')).count() == 1, f"{g} {_attr(page, nine, 'aria-label')}")
    check("FS2: pane 9 carries its own split buttons and ×",
          page.locator('[data-testid="pane-9-split-right"]').count() == 1
          and page.locator('[data-testid="pane-9-split-down"]').count() == 1
          and page.locator('[data-testid="pane-9-close"]').count() == 1)
    page.locator('[data-testid="pane-9-split-down"]').click()
    until(lambda: len(_order(page)) == 3, timeout=2)
    check("FS2: and the next split takes 10, right after its source",
          _order(page) == [1, 9, 10] and _attr(page, _pane(10), "data-slot") == "3", str(_order(page)))
    stored = json.loads(page.evaluate("localStorage.getItem('jarvis.hud.workspace')") or "{}")
    check("FS2: ids 9 and 10 are stored, and the hidden panes with them",
          _stored_order(stored) == [1, 9, 10] and stored.get("kept") == [2, 3, 4, 5, 6, 7, 8]
          and len(stored.get("panes", [])) == 12, f'{_stored_order(stored)} {stored.get("kept")} {len(stored.get("panes", []))}')
    _boot(page, mock, None, until, reload=True)
    check("FS2: a reload draws them again, in their slots",
          _order(page) == [1, 9, 10] and _attr(page, _pane(9), "data-slot") == "2"
          and _attr(page, _pane(10), "data-slot") == "3" and _visible(page, _pane(10)), str(_order(page)))

    # Ctrl+Alt+N is a slot, 1–8 only (decision FS-9).
    page.locator(_pane(1, '[data-testid="input"]')).click()
    until(lambda: _attr(page, _pane(1), "data-focused") == "true", timeout=2)
    _press_on_body(page, "Control+Alt+2")
    until(lambda: _attr(page, nine, "data-focused") == "true", timeout=2)
    check("FS2: Ctrl+Alt+2 focuses pane 9, in slot 2", _attr(page, nine, "data-focused") == "true"
          and _last_key(page).get("prevented") is True)
    _press_on_body(page, "Control+Alt+1")
    until(lambda: _attr(page, _pane(1), "data-focused") == "true", timeout=2)
    zoom = _zoom(page)
    for key in ("9", "0"):
        _press_on_body(page, f"Control+Alt+{key}")
        time.sleep(0.15)
        check(f"FS-9: Ctrl+Alt+{key} focuses nothing with panes 9 and 10 drawn, and is left to the browser",
              _attr(page, _pane(1), "data-focused") == "true" and _attr(page, nine, "data-focused") == "false"
              and _attr(page, _pane(10), "data-focused") == "false" and _last_key(page).get("prevented") is False,
              f"{_ws(page)['ws']['focused']} {_last_key(page)}")
    check("FS-9: and Ctrl+Alt+0 is not the zoom reset", _zoom(page) == zoom, str(_zoom(page)))
    # AltGr arrives as Ctrl+Alt with the symbol it types: ] on the 9 key, } on the 0 key.
    page.evaluate("document.activeElement && document.activeElement.blur()")
    took = {sym: page.evaluate(CTRL_ALT_JS, [sym, code]) for sym, code in (("]", "Digit9"), ("}", "Digit0"))}
    time.sleep(0.15)
    check("FS-9: AltGr's ] and } on the 9 and 0 keys match nothing",
          took == {"]": False, "}": False} and _ws(page)["ws"]["focused"] == 1, f"{took} {_ws(page)['ws']['focused']}")
    took2 = page.evaluate(CTRL_ALT_JS, ["3", "Digit3"])
    until(lambda: _ws(page)["ws"]["focused"] == 10, timeout=2)
    check("(the same dispatch with a digit does focus: Ctrl+Alt+3 is pane 10, in slot 3)",
          took2 is True and _ws(page)["ws"]["focused"] == 10, f"{took2} {_ws(page)['ws']['focused']}")
    _reset_all(page, mock, until)


def _file_text(page, n: int) -> str:
    """The text in pane N's editor, every space taken out: Monaco draws them as
    no-break spaces, or not at all, depending on the line."""
    ed = page.locator(_pane(n, '[data-testid="editor"] .view-lines'))
    if ed.count():
        text = ed.first.inner_text()
    else:
        fb = page.locator(_pane(n, '[data-testid="editor-fallback"]'))
        text = fb.first.input_value() if fb.count() else ""
    return "".join(text.split()).replace("\xa0", "")


def _split_file_checks(page, mock, check, until):
    """A File pane's copy holds the same tabs on the same buffers (ED-2, which
    lifted decisions S-8's "save first"): an edit in one shows in the other at
    once, and one save cleans both. × asks only for an edit no other pane
    holds — Save / Don't save / Cancel, as closing the last tab on one does —
    and an id a split takes back mounts fresh, never the old FileTab. (A chat
    in pane 1: the window puts its chat on screen at boot, so a lone File pane
    would boot as chat.) FS4: the copies split down — two columns at 1920×1080
    are 684 px, too narrow to split right."""
    page.set_viewport_size({"width": 1920, "height": 1080})
    _set_storage(page, zoom="100", layout="{}")
    _set_ws(page, json.dumps({"preset": "cols2", "focused": 2, "panes": [{"view": "chat"}, {"view": "file"}]}))
    _boot(page, mock, None, until, reload=True)
    page.locator(_pane(2, '[data-testid="file-calc.py"]')).click()
    until(lambda: page.locator(_pane(2, '[data-testid="file-path"]')).inner_text() == "calc.py", timeout=4)
    page.locator('[data-testid="pane-2-split-down"]').click()
    path3 = page.locator(_pane(3, '[data-testid="file-path"]'))
    until(lambda: path3.count() > 0 and path3.inner_text() == "calc.py", timeout=4)
    tabs3 = (_ws(page)["ws"]["panes"][2].get("editors") or {})
    check("a File pane's copy opens the same tabs, right after it",
          _order(page) == [1, 2, 3] and path3.inner_text() == "calc.py" and tabs3.get("paths") == ["calc.py"],
          f"{_order(page)} {tabs3}")
    until(lambda: len(page.evaluate("window.__hud.buffers()")) == 1
          and len(page.evaluate("window.__hud.buffers()")[0]["refs"]) == 2, timeout=6)
    bufs = page.evaluate("window.__hud.buffers()")
    check("on the same buffer: one model for the file, held by both panes",
          len(bufs) == 1 and sorted(bufs[0]["refs"]) == ["2|calc.py", "3|calc.py"], str(bufs))

    assert _edit(page, until, 2, "\n# edited in two")
    s2 = page.locator('[data-testid="pane-2-split-down"]')
    until(lambda: "editedintwo" in _file_text(page, 3), timeout=3)
    check("an edit in one shows in the copy at once, and a pane holding one still splits (the same buffers)",
          "editedintwo" in _file_text(page, 3) and not s2.is_disabled(), str(s2.get_attribute("title")))
    page.locator(_pane(2, '[data-testid="file-save"]')).click()
    save3 = page.locator(_pane(3, '[data-testid="file-save"]'))
    until(lambda: save3.is_disabled(), timeout=4)
    check("one save cleans both", save3.is_disabled() and page.locator(_pane(2, '[data-testid="file-save"]')).is_disabled())

    # A shared unsaved edit: × on one pane loses nothing, so it does not ask.
    assert _edit(page, until, 3, "\n# shared")
    page.locator('[data-testid="pane-3-close"]').click()
    until(lambda: 3 not in _order(page), timeout=2)
    check("× on a pane whose unsaved edit another pane also holds closes at once, the edit kept there",
          _order(page) == [1, 2] and "shared" in _file_text(page, 2)
          and not page.locator(_pane(2, '[data-testid="file-save"]')).is_disabled())
    page.locator(_pane(2, '[data-testid="file-save"]')).click()
    until(lambda: page.locator(_pane(2, '[data-testid="file-save"]')).is_disabled(), timeout=4)

    # An edit only this pane holds: × asks Save / Don't save / Cancel.
    page.locator('[data-testid="pane-2-split-down"]').click()
    until(lambda: path3.count() > 0 and path3.inner_text() == "calc.py", timeout=4)
    time.sleep(0.5)  # past the one-click-one-close guard
    _open_file(page, until, 3, "README.md")
    assert _edit(page, until, 3, "\n# dirty in three")
    readme = mock.world["files"]["README.md"]["content"]
    page.locator('[data-testid="pane-3-close"]').click()
    ask = page.locator('[data-testid="pane-3-close-ask"]')
    until(lambda: ask.count() > 0, timeout=2)
    check("× on a pane holding an edit no other pane holds asks Save / Don't save / Cancel, inline, focus on Cancel",
          ask.is_visible() and _order(page) == [1, 2, 3] and _active_testid(page) == "pane-3-close-keep"
          and page.locator('[data-testid="pane-3-close-save"]').inner_text() == "Save"
          and page.locator('[data-testid="pane-3-close-discard"]').inner_text() == "Don't save"
          and page.locator('[data-testid="pane-3-close-keep"]').inner_text() == "Cancel"
          and "README.md" in ask.inner_text(), str(_active_testid(page)))
    # A card arriving while it asks: nothing in the question can be pressed.
    _approval(mock, "close-ask-card")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    time.sleep(0.2)
    # Discard is disabled; Keep is only inert behind the veil (disabling it
    # would drop focus to the page before the card could record it).
    check("under a card Discard is disabled, Keep cannot be reached, and the card has the focus",
          page.locator('[data-testid="pane-3-close-discard"]').is_disabled()
          and page.evaluate("!!document.querySelector('[data-testid=\"pane-3-close-keep\"]').closest('[inert]')")
          and page.locator('[data-testid="pane-3-close-save"]').is_disabled()
          and _active_testid(page) == "approval-card", str(_active_testid(page)))
    page.keyboard.press("Enter")
    time.sleep(0.3)
    check("and Enter there discards nothing", _order(page) == [1, 2, 3])
    page.locator('[data-testid="approval-card"] button.deny').click()
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    check("once the card is answered, focus is back on Keep",
          bool(until(lambda: _active_testid(page) == "pane-3-close-keep", timeout=2)), str(_active_testid(page)))
    page.locator('[data-testid="pane-3-close-keep"]').click()
    until(lambda: ask.count() == 0, timeout=2)
    check("Cancel keeps the pane and its edit", _order(page) == [1, 2, 3] and "dirtyinthree" in _file_text(page, 3))
    time.sleep(0.5)
    page.locator('[data-testid="pane-3-close"]').click()
    until(lambda: ask.count() > 0, timeout=2)
    puts = len(_puts(mock))
    page.locator('[data-testid="pane-3-close-save"]').click()
    until(lambda: 3 not in _order(page), timeout=4)
    sent = _puts(mock)[puts:]
    check("Save saves the edit, then closes the pane",
          _order(page) == [1, 2] and len(sent) == 1 and sent[0].get("path") == "README.md"
          and "dirtyinthree" in "".join(mock.world["files"]["README.md"]["content"].split()), str(sent))
    mock.world["files"]["README.md"]["content"] = readme
    # And Don't save: the edit goes, the file untouched.
    page.locator('[data-testid="pane-2-split-down"]').click()
    until(lambda: path3.count() > 0 and path3.inner_text() == "calc.py", timeout=4)
    time.sleep(0.5)
    _open_file(page, until, 3, "README.md")
    assert _edit(page, until, 3, "\n# thrown away")
    page.locator('[data-testid="pane-3-close"]').click()
    until(lambda: ask.count() > 0, timeout=2)
    page.locator('[data-testid="pane-3-close-discard"]').click()
    until(lambda: 3 not in _order(page), timeout=2)
    check("Don't save closes it, the file untouched and no edit left anywhere",
          _order(page) == [1, 2] and page.locator(_pane(3)).count() == 0
          and mock.world["files"]["README.md"]["content"] == readme
          and not any(b["dirty"] for b in page.evaluate("window.__hud.buffers()"))
          and page.evaluate("window.__hud.workspace().ws.preset") == "cols2")
    # Where the mic is (review of PR #47, M1): the selected chat, pane 1 —
    # not the File pane in the slot before, and never the page.
    until(lambda: _active_testid(page) == "pane-1", timeout=2)
    check("and focus goes to the selected chat (pane 1), never the page", _active_testid(page) == "pane-1"
          and page.evaluate("window.__hud.state().selectedChat") == 1, str(_active_testid(page)))

    # Close a File pane on calc.py, then split one on README.md into the same id.
    page.locator('[data-testid="pane-2-split-down"]').click()
    until(lambda: path3.count() > 0 and path3.inner_text() == "calc.py", timeout=4)
    time.sleep(0.5)
    _close(page, until, 3)
    page.locator(_pane(2, '[data-testid="file-README.md"]')).click()
    until(lambda: page.locator(_pane(2, '[data-testid="file-path"]')).inner_text() == "README.md", timeout=4)
    page.locator('[data-testid="pane-2-split-down"]').click()
    until(lambda: _order(page) == [1, 2, 3], timeout=2)
    until(lambda: path3.count() > 0 and path3.inner_text() == "README.md", timeout=4)
    check("an id a close freed and a split reuses shows the new copy (README.md), never the old file",
          path3.inner_text() == "README.md", path3.inner_text())

    # Every id used. ⊞ to 2×2 hides 5–8 (8 the oldest), splits take the free
    # ids above 8 (9–12 since FS2; none before), and ⊞ to one hides the rest:
    # a split then takes back the oldest hidden pane, 8. (FS4: both side panes
    # folded, so 2×2's 924 px panes have room for the four splits, wherever
    # they fit: GROW_JS.)
    _set_storage(page, layout=FOLDED)
    _set_ws(page, json.dumps({"preset": "grid8", "order": [2, 1, 3, 4, 5, 6, 7, 8], "focused": 2, "panes": [
        {"view": "chat"}, {"view": "file"}, {"view": "chat"}, {"view": "chat"},
        {"view": "chat"}, {"view": "chat"}, {"view": "chat"}, {"view": "file"}]}))
    _boot(page, mock, None, until, reload=True)
    ids = len(page.evaluate("window.__hud.state().chats"))
    page.locator(_pane(8, '[data-testid="file-calc.py"]')).click()
    until(lambda: page.locator(_pane(8, '[data-testid="file-path"]')).inner_text() == "calc.py", timeout=4)
    page.locator(_pane(2, '[data-testid="file-README.md"]')).click()
    until(lambda: page.locator(_pane(2, '[data-testid="file-path"]')).inner_text() == "README.md", timeout=4)
    _choose(page, until, "grid4")
    for n in range(5, 5 + ids - 8):
        page.evaluate(GROW_JS)
        until(lambda: len(_order(page)) == n, timeout=2)
    check(f"setup: ⊞ to 2×2 hid 5–8, and splits took the free ids above 8 ({ids} ids)",
          sorted(_order(page)) == [1, 2, 3, 4, *range(9, ids + 1)] and _ws(page)["ws"]["kept"] == [5, 6, 7, 8],
          f'{_order(page)} {_ws(page)["ws"]["kept"]}')
    _choose(page, until, "single")
    check("setup: one pane, every other id hidden by ⊞ (pane 8 still mounted on calc.py, hidden longest)",
          _order(page) == [2] and len(_ws(page)["ws"]["kept"]) == ids - 1 and _ws(page)["ws"]["kept"][-1] == 8
          and page.locator(_pane(8, '[data-testid="file-path"]')).inner_text() == "calc.py", str(_ws(page)["ws"]["kept"]))
    page.locator('[data-testid="pane-2-split-right"]').click()
    until(lambda: _order(page) == [2, 8], timeout=2)
    p8 = page.locator(_pane(8, '[data-testid="file-path"]'))
    until(lambda: p8.count() > 0 and p8.inner_text() == "README.md", timeout=4)
    check("a split that takes back a hidden pane mounts it fresh: the copy (README.md), not its old calc.py",
          p8.inner_text() == "README.md" and 8 not in _ws(page)["ws"]["kept"] and len(_ws(page)["ws"]["kept"]) == ids - 2,
          p8.inner_text())
    _reset_all(page, mock, until)
