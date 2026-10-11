"""Headless checks for the input bar declutter (PR #29) and its review.

Called from `hud_v2_check.main()` after the multichat section, in a browser
context of its own and against **a `MockDaemon` of its own** on an ephemeral
port (as the layout and multichat sections are: the mock hands SSE frames only
to the newest `/events` connection, and this section's approvals, sends and
archived project must stay out of the world the main suite asserts on). It
also runs on its own: `python tests/face/hud_v2_declutter_check.py`.

PR #29 moved the mic to the orb (the dictation mode, the level meter, and the
selected chat's status as the line under it), made model and effort one
button with a popover, and gave Send, Attach and Stop icons. Its review found
these, and each check below is named after its finding (`review #29 Fn`) and
was shown to fail on 90439bf (the PR as reviewed) or on the merged tree with
that fix reverted:

  F1  the mic's mode buttons were a keyboard lever behind an authorization
      card: focus left on the folded sidebar's cycle button, Enter under the
      card switched OFF to AUTO and restarted the wake recognizer;
  F2  the box's 15-line cap ignored its pane: in a 2×2 grid, two rows, or a
      small window at 160% it pushed Send and the chips out of the pane and
      squeezed the conversation;
  F3  a folded sidebar showed no status at all;
  F4  the model popover could not be used from the keyboard, and Space on an
      option reached push-to-talk;
  F5  the popover stayed where it was opened when its button moved (a zoom, a
      fold, a re-layout), and ran off the window's top when the button was
      high;
  F6  Escape must deny a card even if a popover somehow survived it coming up;
  F7  a short window lost the sidebar's rows to the mic strip;
  F8  the cycle went OFF → AUTO, "LISTENING · SPEAK NOW" and a project-gone
      notice were cut off, under a card the orb said "approval", Steer looked
      like Send, and the popover re-subscribed its listeners every render;
  F9  (in hud_v2_check.py: the effort list's first entry — the effort slider's
      default stop since PR #30 — and AUTO/REVIEW never saying MUTED) and the
      cap and the cycle button pinned here.

PR #30 made the effort a slider: F4's Tab round and arrows and F6's card are
checked against it as well.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.face.hud_v2_layout_check import (  # noqa: E402
    _active_testid, _approval, _attr, _choose, _pane, _press_on_body, _reachable, _visible,
)

SINGLE = json.dumps({"preset": "single", "focused": 1,
                     "panes": [{"view": "chat"}, {"view": "file"}, {"view": "preview"}, {"view": "task"}]})
GRID_CHATS = json.dumps({"preset": "grid4", "focused": 1,
                         "panes": [{"view": "chat"}, {"view": "chat"}, {"view": "chat"}, {"view": "chat"}]})
ROWS_CHATS = json.dumps({"preset": "rows2", "focused": 1,
                         "panes": [{"view": "chat"}, {"view": "chat"}, {"view": "file"}, {"view": "task"}]})
# Two rows, the top one short: its chat's model button is high in the window.
ROWS_TOP = json.dumps({"preset": "rows2", "focused": 1, "splits": {"rows2": {"cols": [], "row": 0.27}},
                       "panes": [{"view": "chat"}, {"view": "chat"}, {"view": "file"}, {"view": "task"}]})

# InputBar's MAX_PANE_SHARE: the box never takes more of its pane than this.
SHARE = 0.35
# …and MIN_LOG_PX / MIN_LOG_SHARE / MIN_LINES: the conversation keeps at least
# max(96 zoomed px, 30% of the pane), unless that would leave the box under
# two lines — then the box stops at two lines and scrolls.
MIN_LOG_PX, MIN_LOG_SHARE, MIN_LINES = 96, 0.3, 2
FORTY_LINES = "\n".join(f"line {i} of a long paste" for i in range(1, 41))
LONG_PROJECT = "Weekend robotics build"


def declutter_checks(browser, mock, base, check, until, guard, init_script):
    print("\nthe input bar declutter (PR #29) and its review")
    _seed(mock)
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, permissions=["microphone"])
    guard(ctx)                      # live ports refused, HTTP and WebSocket alike (hud_v2_check.guard_live)
    ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(5000)
    try:
        before = mock.sse_connections()
        page.goto(base + "/")
        page.wait_for_selector('[data-testid="sidebar"]', state="attached")
        mock.await_reconnect(before)
        for section in (_card_lever_checks, _cap_checks, _min_log_checks, _folded_status_checks,
                        _popover_keyboard_checks,
                        _popover_place_checks, _escape_under_card_checks, _short_window_checks,
                        _orb_line_checks, _steer_look_checks, _listener_checks, _project_gone_checks):
            try:
                section(page, mock, check, until)
            except Exception as e:  # a section that cannot run is a failure, and the rest still run
                check(f"declutter {section.__name__.strip('_')} ran to the end", False,
                      str(e).splitlines()[0][:200])
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        check("no page errors in the declutter section", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()


# ---------------------------------------------------------------------------

def _seed(mock):
    """More threads in jarvis (a sidebar with rows to lose), and a project with
    a long name to archive."""
    w = mock.world
    base = next(t for t in w["threads"] if t["id"] == "t1")
    for i in range(2, 7):
        tid = f"t{i}"
        if not any(t["id"] == tid for t in w["threads"]):
            w["threads"].append({**base, "id": tid, "title": f"chat number {i}", "model": None,
                                 "updated": f"2026-09-1{i}T00:00:00+00:00"})
            w["transcripts"][tid] = [{"role": "user", "text": f"question {i}", "at": "2026-09-14T00:00:00+00:00"}]
    if not any(p["id"] == "p9" for p in w["projects"]):
        p1 = next(p for p in w["projects"] if p["id"] == "p1")
        w["projects"].append({**p1, "id": "p9", "name": LONG_PROJECT, "root": "/home/johnw/jarvis-work/robotics",
                              "inbox": False})
        w["threads"].append({**base, "id": "t9", "project_id": "p9", "title": "robot arm", "model": None,
                             "cwd": "/home/johnw/jarvis-work/robotics"})
        w["transcripts"]["t9"] = [{"role": "user", "text": "arm torque", "at": "2026-09-14T00:00:00+00:00"}]


def _fresh(page, mock, until, ws: str = SINGLE, size=(1280, 800), zoom="100", mode="review"):
    """Reload into a stored workspace, zoom and dictation mode, both side panes open."""
    page.set_viewport_size({"width": size[0], "height": size[1]})
    page.evaluate("""([ws, zoom, mode]) => {
      localStorage.setItem('jarvis.hud.zoom', zoom);
      localStorage.setItem('jarvis.hud.layout', '{}');
      localStorage.setItem('jarvis.hud.workspace', ws);
      localStorage.setItem('jarvis.dictation', mode);
    }""", [ws, zoom, mode])
    before = mock.sse_connections()
    page.reload()
    page.wait_for_selector('[data-testid="sidebar"]', state="attached")
    until(lambda: page.locator('[data-testid="project-p1"]').count() > 0)
    mock.await_reconnect(before)
    time.sleep(0.3)


def _expand(page, until, project_id: str, child: str):
    for _ in range(4):
        if page.locator(f'[data-testid="{child}"]').count() > 0:
            return True
        page.locator(f'[data-testid="project-{project_id}"]').click()
        until(lambda: page.locator(f'[data-testid="{child}"]').count() > 0, timeout=1.0)
    return page.locator(f'[data-testid="{child}"]').count() > 0


def _open(page, until, tid: str, project: str = "p1", pane: int = 1):
    page.locator(_pane(pane, '[data-testid="input"]')).click()
    _expand(page, until, project, f"thread-{tid}")
    page.locator(f'[data-testid="thread-{tid}"]').click()
    # The selected chat's conversation is flattened into state() (WP-B).
    until(lambda: page.evaluate("window.__hud.state().threadId") == tid, timeout=3)


def _mode(page) -> str:
    return page.evaluate("window.__hud.state().dictation")


def _recog(page) -> bool:
    return bool(page.evaluate("!!window.__hudRecog"))


def _fold_left(page, until):
    _press_on_body(page, "Control+b")
    until(lambda: _visible(page, '[data-testid="rail-left"]'), timeout=2)


def _orb_status(page) -> str:
    return page.locator('[data-testid="orb-status"]').inner_text()


def _deny_card(page, mock, until, req: str):
    page.keyboard.press("Escape")
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=3)
    until(lambda: mock.saw("POST", f"/approvals/{req}"), timeout=3)


def _box_parts(page, pane: int) -> dict:
    """Where the box, Send, Attach, the chips and the log are, against the pane."""
    return page.evaluate("""(n) => {
      const p = document.querySelector(`[data-testid="pane-${n}"]`);
      const q = (s) => p.querySelector(s);
      const rect = (el) => { if (!el) return null; const r = el.getBoundingClientRect();
        return {l: r.left, t: r.top, r: r.right, b: r.bottom, h: r.height}; };
      const box = q('[data-testid="input"]');
      return {
        pane: rect(p), box: rect(box), send: rect(q('[data-testid="send"]')),
        attach: rect(q('label.iconbtn.attach')), project: rect(q('[data-testid="project-chip"]')),
        model: rect(q('[data-testid="model-chip-btn"]')) || rect(q('[data-testid="model-chip"]')),
        provider: rect(q('[data-testid="provider-chip-select"]')),
        log: rect(q('[data-testid="log"]')),
        lineH: parseFloat(getComputedStyle(box).lineHeight),
        edges: ['paddingTop', 'paddingBottom', 'borderTopWidth', 'borderBottomWidth']
          .reduce((a, k) => a + parseFloat(getComputedStyle(box)[k]), 0),
        overflow: getComputedStyle(box).overflowY,
        scrolls: box.scrollHeight > box.clientHeight + 1,
        win: {w: innerWidth, h: innerHeight},
      };
    }""", pane)


def _inside(inner, outer, slack=0.5) -> bool:
    return (inner is not None and inner["l"] >= outer["l"] - slack and inner["t"] >= outer["t"] - slack
            and inner["r"] <= outer["r"] + slack and inner["b"] <= outer["b"] + slack)


# ---- F1: the mic's mode buttons under a card ------------------------------

def _card_lever_checks(page, mock, check, until):
    _fresh(page, mock, until, SINGLE, mode="off")
    _fold_left(page, until)
    cycle = page.locator('[data-testid="dictation-cycle"]')
    until(lambda: cycle.count() == 1, timeout=2)
    check("setup: folded, OFF, the wake recognizer stopped", _mode(page) == "off" and not _recog(page))

    # F8 and F9 (M7): one click from OFF is REVIEW, never AUTO; the cycle goes round.
    seen = []
    for _ in range(3):
        was = _mode(page)
        cycle.click()
        until(lambda: _mode(page) != was, timeout=2)
        seen.append(_mode(page))
    check("review #29 F8: the folded cycle goes OFF → REVIEW → AUTO → OFF (one click from muted is never AUTO)",
          seen == ["review", "auto", "off"], str(seen))
    check("review #29 F9: and each click is the mode, on the button and in the window",
          cycle.get_attribute("data-mode") == "off" and "REVIEW" in (cycle.get_attribute("title") or ""),
          f"{cycle.get_attribute('data-mode')} {cycle.get_attribute('title')}")
    until(lambda: not _recog(page), timeout=2)
    check("setup: focus is left on the cycle button, OFF", _active_testid(page) == "dictation-cycle"
          and _mode(page) == "off", str(_active_testid(page)))

    mutes = len(mock.sent("POST", "/mute"))
    _approval(mock, "req-lever-1")
    page.wait_for_selector('[data-testid="approval-card"]')
    time.sleep(0.2)
    check("review #29 F1: under a card the cycle button is disabled", cycle.is_disabled())
    check("review #29 F1: and inert, with the orb", page.evaluate(
        "!!document.getElementById('modecycle').closest('[inert]') && !!document.getElementById('orbdock').closest('[inert]')"))
    check("review #29 F1: and the card took focus off it", _active_testid(page) != "dictation-cycle",
          str(_active_testid(page)))
    page.keyboard.press("Enter")
    page.keyboard.press("Space")
    page.evaluate("document.getElementById('modecycle').focus()")
    page.keyboard.press("Enter")
    page.keyboard.press("Space")
    time.sleep(0.3)
    check("review #29 F1: Enter and Space under the card change nothing: still OFF, no recognizer, no /mute",
          _mode(page) == "off" and not _recog(page) and len(mock.sent("POST", "/mute")) == mutes
          and page.locator('[data-testid="approval-card"]').count() == 1,
          f"{_mode(page)} recog={_recog(page)} mutes={len(mock.sent('POST', '/mute')) - mutes}")
    _deny_card(page, mock, until, "req-lever-1")

    # The same with the full strip: focus on AUTO, mode OFF.
    _press_on_body(page, "Control+b")
    until(lambda: _visible(page, '#orbdock [data-testid="dictation"]'), timeout=2)
    page.evaluate("document.querySelector('[data-testid=\"dictation-auto\"]').focus()")
    _approval(mock, "req-lever-2")
    page.wait_for_selector('[data-testid="approval-card"]')
    time.sleep(0.2)
    check("review #29 F1: under a card the strip's mode buttons are disabled",
          all(page.locator(f'[data-testid="dictation-{m}"]').is_disabled() for m in ("auto", "review", "off")))
    page.evaluate("document.querySelector('[data-testid=\"dictation-auto\"]').focus()")
    page.keyboard.press("Enter")
    page.keyboard.press("Space")
    time.sleep(0.3)
    check("review #29 F1: and Enter or Space on one changes nothing", _mode(page) == "off" and not _recog(page),
          f"{_mode(page)} recog={_recog(page)}")
    _deny_card(page, mock, until, "req-lever-2")
    check("and with the card answered the strip works again",
          not page.locator('[data-testid="dictation-review"]').is_disabled())
    page.locator('[data-testid="dictation-review"]').click()
    until(lambda: _mode(page) == "review", timeout=2)


# ---- F2 (and F9: M4): the box's cap ------------------------------------------

def _type_long(page, pane: int):
    box = page.locator(_pane(pane, '[data-testid="input"]'))
    box.click()
    box.fill(FORTY_LINES)
    time.sleep(0.25)


def _cap_ok(check, where: str, parts: dict):
    pane = parts["pane"]
    for name in ("send", "attach", "project", "model"):
        check(f"review #29 F2: {where}: with 40 lines typed, {name} is wholly inside the pane and the window",
              _inside(parts[name], pane) and _inside(parts[name], {"l": 0, "t": 0, "r": parts["win"]["w"],
                                                                    "b": parts["win"]["h"]}),
              f"{parts[name]} in {pane}")
    log_h = parts["log"]["h"] if parts["log"] else 0
    check(f"review #29 F2: {where}: the box stops at 35% of the pane and scrolls",
          parts["box"]["h"] <= SHARE * pane["h"] + 1 and parts["overflow"] == "auto" and parts["scrolls"],
          f"box {parts['box']['h']:.0f} of pane {pane['h']:.0f}, overflow {parts['overflow']}")
    check(f"review #29 F2: {where}: and the conversation keeps a usable height",
          log_h >= max(60, 0.25 * pane["h"]), f"log {log_h:.0f} of pane {pane['h']:.0f}")


def _cap_checks(page, mock, check, until):
    # Tall enough that 15 lines, not the pane, is the cap.
    _fresh(page, mock, until, SINGLE, size=(1280, 1000))
    box = page.locator(_pane(1, '[data-testid="input"]'))
    box.click()
    box.fill("one\ntwo\nthree")
    time.sleep(0.15)
    p = _box_parts(page, 1)
    check("review #29 F9: three lines grow the box to three lines, without a scrollbar",
          p["box"]["h"] >= 3 * p["lineH"] and p["box"]["h"] < 4 * p["lineH"] + 20 and p["overflow"] == "hidden",
          f"{p['box']['h']:.0f} for line {p['lineH']:.1f}, {p['overflow']}")
    _type_long(page, 1)
    p = _box_parts(page, 1)
    cap = 15 * p["lineH"] + p["edges"]
    check("review #29 F9: forty lines stop at fifteen and scroll (M4)",
          abs(p["box"]["h"] - cap) <= 1.5 and p["overflow"] == "auto" and p["scrolls"],
          f"{p['box']['h']:.0f} vs {cap:.0f}, {p['overflow']}")
    box.fill("")

    # The pane, not fifteen lines, is the limit in a short pane.
    for ws, size, zoom, where in ((GRID_CHATS, (1280, 800), "100", "grid4 at 1280x800"),
                                  (ROWS_CHATS, (1280, 800), "100", "rows2 at 1280x800"),
                                  (SINGLE, (900, 560), "160", "one pane at 900x560 and 160%")):
        _fresh(page, mock, until, ws, size=size, zoom=zoom)
        _type_long(page, 1)
        _cap_ok(check, where, _box_parts(page, 1))
        page.locator(_pane(1, '[data-testid="input"]')).fill("")

    # Re-measured when the pane's height changes, not only its width: forty
    # lines typed in one pane, then the same pane becomes a row of two.
    _fresh(page, mock, until, SINGLE, size=(1280, 1000))
    _type_long(page, 1)
    tall = _box_parts(page, 1)["box"]["h"]
    _choose(page, until, "rows2")
    until(lambda: _attr(page, '[data-testid="workspace"]', "data-drawn-preset") == "rows2", timeout=3)
    time.sleep(0.3)
    p = _box_parts(page, 1)
    check("review #29 F2: a pane that gets shorter (a split into rows) shrinks a full box to its new 35%",
          p["box"]["h"] < tall and p["box"]["h"] <= SHARE * p["pane"]["h"] + 1,
          f"{tall:.0f} → {p['box']['h']:.0f} in a pane of {p['pane']['h']:.0f}")
    page.locator(_pane(1, '[data-testid="input"]')).fill("")


def _min_log_checks(page, mock, check, until):
    """The conversation's minimum (coordinator, after #28's bottom panel): at
    1920x700 and 160% with the panel open the pane is short, and 35% of it
    still left the log 36px. The box now also stops where the pane, less its
    fixed parts, would leave the log under max(96px, 30%) — but never under two
    lines. Killed by dropping the min-log term from InputBar's cap."""
    z = 1.6
    for panel_h, where in ((None, "the bottom panel at its default height (the pane at its 200px minimum)"),
                           (120, "the bottom panel at its smallest")):
        ws = json.loads(SINGLE)
        ws["panel"] = {"open": True, "height": panel_h or 260}
        _fresh(page, mock, until, json.dumps(ws), size=(1920, 700), zoom="160")
        until(lambda: _visible(page, '[data-testid="panel"]'), timeout=3)
        _type_long(page, 1)
        p = _box_parts(page, 1)
        pane, box = p["pane"]["h"], p["box"]["h"]
        log = p["log"]["h"] if p["log"] else 0
        fixed = pane - log - box
        min_log = max(MIN_LOG_PX * z, MIN_LOG_SHARE * pane)
        two = (MIN_LINES * p["lineH"] + p["edges"]) * z  # computed style is in zoomed px, rects on screen
        fits = pane - fixed - min_log >= two
        detail = (f"pane {pane / z:.0f} fixed {fixed / z:.0f} box {box / z:.0f} log {log / z:.0f} "
                  f"(min {min_log / z:.0f}, two lines {two / z:.0f}) zoomed px")
        for name in ("send", "attach", "project", "model"):
            check(f"review #29 F2: 1920x700 at 160%, {where}, 40 lines typed: {name} is wholly inside the pane",
                  _inside(p[name], p["pane"]), f"{p[name]} in {p['pane']}")
        if fits:
            check(f"review #29 F2: 1920x700 at 160%, {where}, 40 lines typed: the log keeps at least its minimum,"
                  " max(96px, 30% of the pane)", log >= min_log - 1 and p["scrolls"], detail)
            check("setup: and there it is the min-log term that stops the box, not 35%",
                  box < SHARE * pane - 2, detail)
        else:
            check(f"review #29 F2: 1920x700 at 160%, {where}, 40 lines typed: the pane cannot fit the log's"
                  " minimum, so the box stops at two lines and scrolls and the log keeps the rest",
                  abs(box - two) <= 1.5 and p["scrolls"] and log >= pane - fixed - two - 1, detail)
        page.locator(_pane(1, '[data-testid="input"]')).fill("")


# ---- F3: a folded sidebar still says what the selected chat is doing --------

def _folded_status_checks(page, mock, check, until):
    _fresh(page, mock, until, SINGLE)
    line = page.locator(_pane(1, '[data-testid="pane-status"]'))
    check("unfolded, the selected chat's bar draws no status line (the orb carries it)", line.count() == 0)
    _fold_left(page, until)

    def shown(text: str) -> tuple[bool, str]:
        ok = until(lambda: line.count() == 1 and line.inner_text().strip() == text and line.is_visible(), timeout=4)
        if not ok:
            return False, line.inner_text() if line.count() else "no status line"
        whole = page.evaluate("(el) => el.scrollWidth <= el.clientWidth + 1",
                              line.element_handle())
        return bool(whole), f"{line.inner_text()} (cut: {not whole})"

    # LISTENING: the mic opens on speech in the follow-up window.
    page.evaluate("""() => { const m = window.__hud.mic; m.feedMs(2000, 0.001);
        window.__hud.capture.openFollowUp(); m.feedMs(800, 0.06); return true; }""")
    ok, why = shown("LISTENING · SPEAK NOW")
    check("review #29 F3: folded, the bar says LISTENING · SPEAK NOW", ok, why)
    # STT FAILED: the utterance ends and transcription fails.
    page.route("**/stt", lambda r: r.fulfill(status=500, body='{"error": "stt is down"}',
                                               content_type="application/json"))
    try:
        page.evaluate("() => { window.__hud.mic.feedMs(2200, 0.0005); return true; }")
        ok, why = shown("STT FAILED")
        check("review #29 F3: folded, the bar says STT FAILED", ok, why)
    finally:
        page.unroute("**/stt")
    page.evaluate("window.__hud.capture.closeFollowUp()")
    page.evaluate("window.__hud.dispatch({type: 'patch', patch: {status: '', error: ''}})")
    # MIC MUTED: the cycle to OFF (REVIEW → AUTO → OFF).
    cycle = page.locator('[data-testid="dictation-cycle"]')
    for _ in range(3):
        if _mode(page) == "off":
            break
        cycle.click()
        time.sleep(0.1)
    ok, why = shown("MIC MUTED")
    check("review #29 F3: folded, the bar says MIC MUTED", ok, why)
    cycle.click()
    until(lambda: _mode(page) == "review", timeout=2)
    _press_on_body(page, "Control+b")
    until(lambda: not _visible(page, '[data-testid="rail-left"]'), timeout=2)
    check("and unfolded again the line goes back to the orb", bool(until(lambda: line.count() == 0, timeout=2)))


# ---- F4: the popover from the keyboard --------------------------------------

def _pop(pane=1):
    return f'[data-testid="model-pop"][data-pane="{pane}"]'


def _btn(pane=1):
    return _pane(pane, '[data-testid="model-chip-btn"]')


def _focused(page) -> dict:
    return page.evaluate("""() => { const a = document.activeElement;
      return { testid: a && a.getAttribute('data-testid'), value: a && a.getAttribute('data-value'),
               selected: a && a.getAttribute('aria-selected'),
               inPop: !!(a && a.closest('[data-testid="model-pop"]')) }; }""")


def _popover_keyboard_checks(page, mock, check, until):
    _fresh(page, mock, until, SINGLE)
    page.locator('[data-testid="new-thread"]').click()
    until(lambda: page.locator(_btn()).count() > 0, timeout=3)
    page.locator(_pane(1, '[data-testid="provider-chip-select"]')).select_option("claude")
    until(lambda: (page.locator(_btn()).inner_text() or "").startswith("default · claude"), timeout=3)
    page.locator(_btn()).click()
    page.wait_for_selector(_pop())
    f = _focused(page)
    check("review #29 F4: the popover opens with focus on the chosen model",
          f["testid"] == "model-opt" and f["selected"] == "true" and f["value"] == "", str(f))
    values = page.locator(_pop() + ' [data-testid="model-opt"]').evaluate_all(
        "els => els.map(e => e.getAttribute('data-value'))")
    page.keyboard.press("ArrowDown")
    f1 = _focused(page)
    page.keyboard.press("ArrowUp")
    f2 = _focused(page)
    page.keyboard.press("End")
    f3 = _focused(page)
    page.keyboard.press("ArrowDown")
    f4 = _focused(page)
    check("review #29 F4: the arrows move within the model list (Down, Up, End, and round)",
          f1["value"] == values[1] and f2["value"] == values[0] and f3["value"] == values[-1]
          and f4["value"] == values[0] and page.locator(_pop()).count() == 1,
          f"{[f1['value'], f2['value'], f3['value'], f4['value']]} of {values}")
    page.keyboard.press("Tab")
    t1 = _focused(page)
    page.keyboard.press("Tab")
    t2 = _focused(page)
    page.keyboard.press("Tab")
    t3 = _focused(page)
    page.keyboard.press("Shift+Tab")
    t4 = _focused(page)
    # The effort is a slider since PR #30: it is the round's second stop.
    check("review #29 F4: Tab goes model list → effort slider → Set default… → round, and never leaves",
          t1["testid"] == "effort-slider" and t2["testid"] == "provider-defaults-open"
          and t3["testid"] == "model-opt" and t4["testid"] == "provider-defaults-open"
          and all(t["inPop"] for t in (t1, t2, t3, t4)),
          str([(t["testid"], t["value"]) for t in (t1, t2, t3, t4)]))
    page.keyboard.press("Shift+Tab")
    slider = page.locator(_pop() + ' [data-testid="effort-slider"]')
    now = lambda: int(slider.get_attribute("aria-valuenow") or -1)
    e0 = (_focused(page), now())
    page.keyboard.press("ArrowRight")
    e1 = (_focused(page), now())
    page.keyboard.press("ArrowLeft")
    e2 = (_focused(page), now())
    check("review #29 F4: and the arrows move the effort slider, focus staying on it",
          all(e[0]["testid"] == "effort-slider" for e in (e0, e1, e2))
          and e1[1] == e0[1] + 1 and e2[1] == e0[1] and page.locator(_pop()).count() == 1,
          str([(e[0]["testid"], e[1]) for e in (e0, e1, e2)]))
    # Space on an option is the option's, never push-to-talk.
    page.keyboard.press("Shift+Tab")
    page.keyboard.down("Space")
    time.sleep(0.15)
    ptt = page.evaluate("window.__hud.capture.ptt")
    page.keyboard.up("Space")
    check("review #29 F4: Space on an option never starts push-to-talk (M5)", ptt is None, str(ptt))
    until(lambda: page.locator(_pop()).count() == 1, timeout=1)
    if page.locator(_pop()).count() == 0:
        page.locator(_btn()).click()
        page.wait_for_selector(_pop())
    page.keyboard.press("Escape")
    check("review #29 F4: Escape closes it and gives focus back to the button",
          bool(until(lambda: page.locator(_pop()).count() == 0, timeout=2)) and _active_testid(page) == "model-chip-btn",
          str(_active_testid(page)))
    page.locator(_btn()).click()
    page.wait_for_selector(_pop())
    page.locator(_pane(1, '[data-testid="input"]')).focus()
    check("review #29 F4: focus leaving it closes it", bool(until(lambda: page.locator(_pop()).count() == 0, timeout=2)))
    # A click inside it on a heading keeps it open (focus stays inside).
    page.locator(_btn()).click()
    page.wait_for_selector(_pop())
    page.locator(_pop() + " .msec").first.click()
    time.sleep(0.2)
    check("and a click on its own heading does not", page.locator(_pop()).count() == 1)
    page.keyboard.press("Escape")
    until(lambda: page.locator(_pop()).count() == 0, timeout=2)
    page.locator(_pane(1, '[data-testid="provider-chip-select"]')).select_option("fast")


# ---- F5: the popover stays with its button -----------------------------------

def _pop_rect(page, pane=1):
    return page.evaluate("""(n) => { const el = document.querySelector(`[data-testid="model-pop"][data-pane="${n}"]`);
      if (!el) return null; const r = el.getBoundingClientRect();
      return {l: r.left, t: r.top, r: r.right, b: r.bottom, w: innerWidth, h: innerHeight}; }""", pane)


def _btn_rect(page, pane=1):
    return page.evaluate("""(n) => { const el = document.querySelector(`[data-testid="pane-${n}"] [data-testid="model-chip-btn"]`);
      const r = el.getBoundingClientRect(); return {l: r.left, t: r.top, r: r.right, b: r.bottom}; }""", pane)


def _reopen(page):
    if page.locator(_pop()).count() == 0:
        page.locator(_btn()).click()
        page.wait_for_selector(_pop())


def _popover_place_checks(page, mock, check, until):
    _fresh(page, mock, until, SINGLE, zoom="160", size=(1600, 1000))
    _reopen(page)
    r, b = _pop_rect(page), _btn_rect(page)
    check("review #29 F5: at 160% it opens upward from its button, left edges together",
          r["b"] <= b["t"] + 0.5 and abs(r["l"] - b["l"]) <= 1.5, f"{r} {b}")
    # A zoom change (the HUD's own key, focus inside the popover).
    page.keyboard.press("Control+-")
    check("review #29 F5: a zoom change closes it", bool(until(lambda: page.locator(_pop()).count() == 0, timeout=2)))
    _reopen(page)
    page.keyboard.press("Control+b")
    check("review #29 F5: folding the sidebar (its button moves) closes it",
          bool(until(lambda: page.locator(_pop()).count() == 0, timeout=2)))
    _reopen(page)
    page.keyboard.press("Control+b")
    check("review #29 F5: and so does unfolding it", bool(until(lambda: page.locator(_pop()).count() == 0, timeout=2)))
    _reopen(page)
    page.evaluate("document.getElementById('shell').style.setProperty('--left-w', '300px')")
    check("review #29 F5: a re-layout that moves its button closes it",
          bool(until(lambda: page.locator(_pop()).count() == 0, timeout=2)))
    page.evaluate("document.getElementById('shell').style.removeProperty('--left-w')")

    # A button high in the window: two rows, the chat on top, in a short window.
    _fresh(page, mock, until, ROWS_TOP, size=(1280, 800), zoom="100")
    page.locator(_btn()).click()
    page.wait_for_selector(_pop())
    r, b = _pop_rect(page), _btn_rect(page)
    check("review #29 F5: with no room above its button it opens downward",
          r["t"] >= b["b"] - 0.5, f"{r} {b}")
    check("review #29 F5: and never runs off the window", r["t"] >= -0.5 and r["b"] <= r["h"] + 0.5
          and r["l"] >= -0.5 and r["r"] <= r["w"] + 0.5, str(r))
    page.keyboard.press("Escape")
    # Lower pane: room above, so upward — and still inside the window.
    page.locator(_btn(2)).click()
    page.wait_for_selector(_pop(2))
    r, b = _pop_rect(page, 2), _btn_rect(page, 2)
    check("review #29 F5: the lower pane's opens upward, inside the window, and says whose it is",
          r["b"] <= b["t"] + 0.5 and r["t"] >= -0.5 and page.locator('[data-testid="model-pop"]').count() == 1,
          f"{r} {b}")
    page.keyboard.press("Escape")


# ---- F6: Escape is the card's ----------------------------------------------

def _escape_under_card_checks(page, mock, check, until):
    _fresh(page, mock, until, SINGLE)
    # An existing thread: its provider is plain text, so nothing beside the
    # model button changes shape under the card (a compose row's provider
    # select turns to text, the button moves, and that alone closes it).
    _open(page, until, "t2")
    _reopen(page)
    _approval(mock, "req-pop-1")
    page.wait_for_selector('[data-testid="approval-card"]')
    check("review #29 F6: a card coming up closes the popover (M1)",
          bool(until(lambda: page.locator('[data-testid="model-pop"]').count() == 0, timeout=2)))
    page.keyboard.press("Escape")
    denied = until(lambda: [b for b in mock.sent("POST", "/approvals/req-pop-1")] or None, timeout=3)
    check("review #29 F6: and Escape denies the card", bool(denied) and denied[-1].get("decision") == "deny",
          str(denied))
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=3)

    # The same with focus on the effort slider (PR #30), whose keys are its
    # own: the card still closes the popover, the slider's keys reach nothing
    # under it, and Escape is still the card's.
    btn = page.locator(_btn())
    until(lambda: not btn.is_disabled(), timeout=3)
    _reopen(page)
    slider = page.locator(_pop() + ' [data-testid="effort-slider"]')
    slider.focus()
    on_slider = _active_testid(page) == "effort-slider"
    patches = len(mock.sent("PATCH", "/threads/t2"))
    _approval(mock, "req-pop-2")
    page.wait_for_selector('[data-testid="approval-card"]')
    check("PR #30 (F6): a card closes the popover with focus on the effort slider",
          on_slider and bool(until(lambda: page.locator('[data-testid="model-pop"]').count() == 0, timeout=2))
          and btn.is_disabled(), f"on slider: {on_slider}")
    for k in ("ArrowRight", "End", "Home", "Space"):
        page.keyboard.press(k)
    time.sleep(0.6)
    check("PR #30: the slider's keys change nothing under the card",
          len(mock.sent("PATCH", "/threads/t2")) == patches and page.locator('[data-testid="model-pop"]').count() == 0
          and page.locator('[data-testid="approval-card"]').count() == 1,
          str(mock.sent("PATCH", "/threads/t2")[patches:]))
    page.keyboard.press("Escape")
    denied = until(lambda: [b for b in mock.sent("POST", "/approvals/req-pop-2")] or None, timeout=3)
    check("PR #30: and Escape still denies the card", bool(denied) and denied[-1].get("decision") == "deny",
          str(denied))
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=3)


# ---- F7: a short window keeps the sidebar's rows -----------------------------

def _rows_visible(page) -> int:
    """Sidebar rows wholly inside its list's visible area and topmost at their centre."""
    return page.evaluate("""() => {
      const list = document.querySelector('[data-testid="sidebar"]');
      const side = document.getElementById('sidebar');
      const lr = list.getBoundingClientRect();
      const sr = side.getBoundingClientRect();
      const pad = parseFloat(getComputedStyle(side).paddingBottom) || 0;
      const bottom = Math.min(lr.bottom, sr.bottom - pad * (sr.height / side.offsetHeight));
      let n = 0;
      for (const row of list.querySelectorAll('.tree-row')) {
        const r = row.getBoundingClientRect();
        if (r.height < 4 || r.top < lr.top - 0.5 || r.bottom > Math.min(lr.bottom, innerHeight) + 0.5) continue;
        const hit = document.elementFromPoint(r.left + Math.min(40, r.width / 2), r.top + r.height / 2);
        if (hit && row.contains(hit)) n++;
      }
      return n;
    }""")


def _short_window_checks(page, mock, check, until):
    _fresh(page, mock, until, SINGLE, size=(1920, 700), zoom="160")
    _expand(page, until, "p1", "thread-t3")
    page.evaluate("document.querySelector('[data-testid=\"sidebar\"]').scrollTop = 0")
    n = _rows_visible(page)
    check("review #29 F7: at 1920x700 and 160% the sidebar still shows at least three rows (a project and two threads)",
          n >= 3, str(n))
    check("review #29 F7: there the strip folds into the one mode button beside the orb, which stays usable",
          not _visible(page, '#orbdock [data-testid="dictation"]') and _reachable(page, '[data-testid="dictation-cycle"]')[0],
          str(_reachable(page, '[data-testid="dictation-cycle"]')))
    _fresh(page, mock, until, SINGLE, size=(1280, 800), zoom="100")
    check("and a window with room keeps the full strip, with no extra button",
          _visible(page, '#orbdock [data-testid="dictation"]') and page.locator('[data-testid="dictation-cycle"]').count() == 0)


# ---- F8: the orb's line ------------------------------------------------------

def _line_whole(page) -> tuple[bool, str]:
    return page.evaluate("""() => { const el = document.querySelector('[data-testid="orb-status"]');
      const ok = el.scrollWidth <= el.clientWidth + 0.5 && el.scrollHeight <= el.clientHeight + 0.5;
      return [ok, `${el.innerText} ${el.scrollWidth}/${el.clientWidth} x ${el.scrollHeight}/${el.clientHeight}`]; }""")


def _orb_line_checks(page, mock, check, until):
    _fresh(page, mock, until, SINGLE)
    page.evaluate("""() => { const m = window.__hud.mic; m.feedMs(2000, 0.001);
        window.__hud.capture.openFollowUp(); m.feedMs(800, 0.06); return true; }""")
    until(lambda: _orb_status(page).strip() == "LISTENING · SPEAK NOW", timeout=4)
    ok, why = _line_whole(page)
    check("review #29 F8: LISTENING · SPEAK NOW fits under the orb, not cut by a pixel", ok, why)
    page.evaluate("() => { window.__hud.mic.feedMs(2200, 0.0005); return true; }")
    until(lambda: page.locator(_pane(1, '[data-testid="input"]')).input_value() != "", timeout=5)
    page.locator(_pane(1, '[data-testid="input"]')).fill("")
    page.evaluate("window.__hud.capture.closeFollowUp()")
    page.evaluate("window.__hud.dispatch({type: 'patch', patch: {status: ''}})")
    _approval(mock, "req-line-1")
    page.wait_for_selector('[data-testid="approval-card"]')
    check("review #29 F8: under a card the orb asks for the answer in words",
          bool(until(lambda: _orb_status(page).strip() == "ANSWER THE AUTHORIZATION", timeout=2)), _orb_status(page))
    _deny_card(page, mock, until, "req-line-1")
    check("and says nothing once it is answered", bool(until(lambda: _orb_status(page).strip() == "", timeout=2)),
          _orb_status(page))


def _steer_look_checks(page, mock, check, until):
    _fresh(page, mock, until, SINGLE)
    _open(page, until, "t2")
    send = page.locator(_pane(1, '[data-testid="send"]'))
    look = lambda: page.evaluate("""() => { const b = document.querySelector('[data-testid="pane-1"] [data-testid="send"]');
      const cs = getComputedStyle(b); return [b.className, cs.backgroundColor, b.querySelector('svg').innerHTML,
      b.getAttribute('aria-label')]; }""")  # noqa: E731
    idle = look()
    box = page.locator(_pane(1, '[data-testid="input"]'))
    box.fill("start something")
    box.press("Enter")
    until(lambda: page.locator(_pane(1, '[data-testid="stop"]')).count() == 1, timeout=3)
    run = look()
    check("review #29 F8: while a turn runs, Send becomes Steer and looks it (not the filled arrow)",
          run[3] == "Steer" and "steer" in run[0] and run[1] != idle[1] and run[2] != idle[2]
          and send.get_attribute("data-steer") == "true", f"idle {idle[:2]} running {run[:2]}")
    mock.emit("turn_finished", {"stop": "end"}, thread_id="t2")
    check("and Send again once it ends", bool(until(lambda: look()[3] == "Send" and look()[1] == idle[1], timeout=3)))


def _listener_checks(page, mock, check, until):
    """The popover's listeners are set once, not on every render (the mic level
    re-renders the window many times a second)."""
    _fresh(page, mock, until, SINGLE)
    page.evaluate("""() => {
      window.__regs = 0;
      const add = window.addEventListener.bind(window);
      if (!window.__wrapped) {
        window.addEventListener = (type, fn, opts) => { if (type === 'pointerdown') window.__regs++; return add(type, fn, opts); };
        window.__wrapped = true;
      }
      return true;
    }""")
    _reopen(page)
    time.sleep(0.2)
    page.evaluate("window.__regs = 0")
    # Renders: the level meter moves with every frame of sound.
    page.evaluate("() => { window.__hud.mic.feedMs(1500, 0.03); return true; }")
    for i in range(10):
        page.evaluate(f"window.__hud.dispatch({{type: 'patch', patch: {{level: {0.1 + i * 0.05}}}}})")
        time.sleep(0.03)
    regs = page.evaluate("window.__regs")
    check("review #29 F8: re-renders while it is open re-register none of its listeners", regs == 0, str(regs))
    page.keyboard.press("Escape")
    until(lambda: page.locator(_pop()).count() == 0, timeout=2)


def _project_gone_checks(page, mock, check, until):
    _fresh(page, mock, until, SINGLE)
    _open(page, until, "t9", "p9")
    mock.emit("project_archived", {"project_id": "p9"}, project_id="p9")
    want = f"{LONG_PROJECT} was archived".upper()
    shown = until(lambda: _orb_status(page).strip().replace("\n", " ") == want, timeout=4)
    ok, why = _line_whole(page)
    check("review #29 F8: a project-gone notice is read whole under the orb, not cut in half",
          bool(shown) and ok, why)


# ---------------------------------------------------------------------------

def main():
    from playwright.sync_api import sync_playwright

    from tests.face import hud_v2_check as H
    from tests.face.hud_v2_mock import DIST, MockDaemon

    if not (DIST / "index.html").exists():
        print("hud/dist is not built. Run: cd hud && npm ci && npm run build")
        return 2
    mock = MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--use-fake-ui-for-media-stream",
                                                              "--use-fake-device-for-media-stream"])
            declutter_checks(browser, mock, f"http://127.0.0.1:{mock.port}", H.check, H.until,
                             lambda ctx: H.guard_live(ctx, H.FAILURES.append), H.FAKE_RECOGNIZER)
            browser.close()
    finally:
        mock.stop()
    print()
    if H.FAILURES:
        print(f"{len(H.FAILURES)} of {H.CHECKS} checks FAILED:")
        for f in H.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {H.CHECKS} declutter checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
