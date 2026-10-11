"""Headless checks for the effort slider's review (PR #30).

Called from `hud_v2_check.main()` after the declutter section, in a browser
context of its own and against **a `MockDaemon` of its own** on an ephemeral
port (as the layout, multichat and declutter sections are: this section
archives a thread and opens new ones, which must stay out of the world the
main suite asserts on). It also runs on its own:
`python tests/face/hud_v2_effort_check.py`.

Each check is named after the review finding it pins, and each was shown to
fail on e859140 (the PR as reviewed) or on this tree with its fix reverted:

  R1  a key move still settling was not tied to the conversation it was made
      on: the thread archived from another window re-aimed the pane at a new
      compose row and the move rode that row (and so the new thread's first
      `POST /threads`); a pane switched to another thread had the move PATCHed
      onto that thread, across providers too;
  R2  the sequence guard dropped a successful answer for an opened compose row
      (a first send that failed): a late model change's success, followed by a
      refused effort change, left the chip on the default the server no longer
      held;
  R3a re-swallowing Tab in the slider went unseen: on an OpenRouter thread
      (no "Set default…" footer) Tab from the slider must stay in the popover;
  R3b a model change under a move still settling must send nothing: the stop
      is not one the new model offers.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.face.hud_v2_declutter_check import SINGLE, _fresh, _open  # noqa: E402
from tests.face.hud_v2_layout_check import _pane  # noqa: E402

BTN = _pane(1, '[data-testid="model-chip-btn"]')
POP = '[data-testid="model-pop"][data-pane="1"]'
SLIDER = POP + ' [data-testid="effort-slider"]'
KIMI = "moonshotai/kimi-k3"
LUNA = "openai/gpt-5.6-luna"


def effort_checks(browser, mock, base, check, until, guard, init_script):
    print("\nthe effort slider's review (PR #30)")
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
        for section in (_tab_round_checks, _pane_switch_checks, _model_change_checks,
                        _opened_row_checks, _archived_checks):
            try:
                section(page, mock, check, until)
            except Exception as e:  # a section that cannot run is a failure, and the rest still run
                check(f"effort {section.__name__.strip('_')} ran to the end", False,
                      str(e).splitlines()[0][:200])
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        check("no page errors in the effort section", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()


# ---------------------------------------------------------------------------

def _seed(mock):
    """Threads of our own: OpenRouter ones on the default and on Luna, a
    Claude one, one to archive; and Kimi on the roster, whose ladder stops
    short of max."""
    w = mock.world
    base = next(t for t in w["threads"] if t["id"] == "t1")
    rows = [("eb1", "fast", None, None), ("eb2", "fast", None, None), ("ec1", "claude", None, None),
            ("ed1", "fast", LUNA, "low"), ("ea1", "fast", None, None)]
    for i, (tid, provider, model, effort) in enumerate(rows):
        if not any(t["id"] == tid for t in w["threads"]):
            w["threads"].append({**base, "id": tid, "provider": provider, "model": model, "effort": effort,
                                 "title": f"effort {tid}", "updated": f"2026-09-2{i}T00:00:00+00:00"})
            w["transcripts"][tid] = [{"role": "user", "text": f"hello {tid}", "at": "2026-09-14T00:00:00+00:00"}]
    if not any(r["id"] == KIMI for r in w["models"]["models"]):
        w["models"]["models"].append({"id": KIMI, "name": "Kimi K3", "efforts": ["low", "medium", "high"],
                                      "effort": None})


def _thread(mock, tid):
    return next(t for t in mock.world["threads"] if t["id"] == tid)


def _chat(page, n=1) -> dict:
    return page.evaluate("window.__hud.state().chats")[str(n)]


def _focused(page) -> dict:
    return page.evaluate("""() => { const a = document.activeElement;
      return { testid: a && a.getAttribute('data-testid'),
               inPop: !!(a && a.closest('[data-testid="model-pop"]')) }; }""")


def _show(page, until, tid):
    """Put pane 1 on `tid` as another window or a sidebar click would, without
    a click that would close the popover."""
    page.evaluate(f"window.__hud.dispatch({{type: 'patch', patch: {{threadId: '{tid}', compose: null}}}})")
    until(lambda: _chat(page)["threadId"] == tid, timeout=3)


def _open_pop(page, until):
    if page.locator(POP).count() == 0:
        page.locator(BTN).click()
        page.wait_for_selector(POP)
    until(lambda: page.locator(SLIDER).count() == 1, timeout=3)


def _close_pop(page, until):
    if page.locator(POP).count():
        page.locator(BTN).click()
        until(lambda: page.locator(POP).count() == 0, timeout=2)


def _now(page) -> int:
    return int(page.locator(SLIDER).get_attribute("aria-valuenow") or -1)


def _default_index(page) -> int:
    return int(page.locator(SLIDER).get_attribute("data-default") or -1)


# ---- R3a: Tab stays in a popover with no footer -----------------------------

def _tab_round_checks(page, mock, check, until):
    _fresh(page, mock, until, SINGLE)
    _open(page, until, "eb2")
    _open_pop(page, until)
    check("setup: an OpenRouter thread's popover has no Set default… footer",
          page.locator(POP + " .mfoot").count() == 0)
    page.locator(POP + ' [data-testid="model-opt"][aria-selected="true"]').focus()
    seen = []
    for keys in ("Tab", "Tab", "Shift+Tab", "Shift+Tab", "Shift+Tab"):
        page.keyboard.press(keys)
        seen.append((keys, _focused(page), page.locator(POP).count()))
    want = ["effort-slider", "model-opt", "effort-slider", "model-opt", "effort-slider"]
    check("review #30 R3a: with no footer, Tab and Shift+Tab go model ↔ slider and never leave the popover",
          [s[1]["testid"] for s in seen] == want and all(s[1]["inPop"] and s[2] == 1 for s in seen),
          str([(k, f["testid"], f["inPop"], n) for k, f, n in seen]))
    page.keyboard.press("Escape")
    until(lambda: page.locator(POP).count() == 0, timeout=2)


# ---- R1: a move still settling stays with its conversation -------------------

def _pane_switch_checks(page, mock, check, until):
    _fresh(page, mock, until, SINGLE)
    _open(page, until, "eb1")
    # Same provider and model: the chip's button stays put, so the popover
    # stays open across the switch and the timer is what would commit.
    _open_pop(page, until)
    page.locator(SLIDER).focus()
    n = len(mock.calls)
    page.keyboard.press("ArrowLeft")
    moved = _now(page)
    _show(page, until, "eb2")
    time.sleep(0.9)
    late = [(p, b) for m, p, b in mock.calls[n:] if m == "PATCH" and p.startswith("/threads/")]
    check("review #30 R1: a pane switched to another thread with a move settling sends that thread nothing",
          not [c for c in late if c[0] == "/threads/eb2"] and _thread(mock, "eb2").get("effort") is None, str(late))
    check("and the move is dropped, not sent to the thread it was made on after it was left",
          not [c for c in late if c[0] == "/threads/eb1"], str(late))
    if page.locator(SLIDER).count():
        check("and the slider shows the new thread's own effort, not the move",
              _now(page) == _default_index(page) and moved != _default_index(page),
              f"now {_now(page)}, default {_default_index(page)}, the move was at {moved}")
    _close_pop(page, until)

    # Across providers: the button moves, the popover closes on the next
    # frame, and its unmount used to commit the move onto the Claude thread.
    _show(page, until, "eb1")
    _open_pop(page, until)
    page.locator(SLIDER).focus()
    n = len(mock.calls)
    page.keyboard.press("ArrowLeft")
    _show(page, until, "ec1")
    time.sleep(0.9)
    late = [(p, b) for m, p, b in mock.calls[n:] if m == "PATCH" and p.startswith("/threads/")]
    check("review #30 R1: and switched to a thread on another provider, it sends that thread nothing either",
          not [c for c in late if c[0] == "/threads/ec1"] and _thread(mock, "ec1").get("effort") is None, str(late))
    _close_pop(page, until)


# ---- R3b: a model change under a move settling sends nothing -----------------

def _model_change_checks(page, mock, check, until):
    _fresh(page, mock, until, SINGLE)
    _open(page, until, "ed1")
    _open_pop(page, until)
    page.locator(SLIDER).focus()
    stops = (page.locator(SLIDER).get_attribute("data-stops") or "").split(",")
    n = len(mock.calls)
    page.keyboard.press("End")
    # Another window moves the thread to Kimi, whose ladder has no max.
    t = _thread(mock, "ed1")
    t["model"] = KIMI
    mock.emit("thread_updated", dict(t, thread_id="ed1", changed=["model", "effort"]),
              thread_id="ed1", project_id=t["project_id"])
    until(lambda: page.locator(SLIDER).count() and "max" not in
          (page.locator(SLIDER).get_attribute("data-stops") or "").split(","), timeout=2)
    time.sleep(0.9)
    late = [(p, b) for m, p, b in mock.calls[n:] if m == "PATCH" and p.startswith("/threads/")]
    after = (page.locator(SLIDER).get_attribute("data-stops") or "").split(",") if page.locator(SLIDER).count() else []
    check("setup: the move was to max, and the new model does not offer it",
          stops[-1:] == ["max"] and "max" not in after, f"{stops} → {after}")
    check("review #30 R3b: a model change under a move settling sends nothing — no off-ladder value, no clear",
          not late and _thread(mock, "ed1").get("effort") == "low", str(late))
    _close_pop(page, until)


# ---- R2: an opened compose row keeps a late success --------------------------

def _opened_row_checks(page, mock, check, until):
    _fresh(page, mock, until, SINGLE)
    page.locator('[data-testid="new-thread"]').click()
    until(lambda: (_chat(page)["compose"] or {}) and page.locator(BTN).count() > 0, timeout=3)
    mock.world["fail_send"] = 1
    box = page.locator(_pane(1, '[data-testid="input"]'))
    box.fill("a first try that fails")
    box.press("Enter")
    opened = until(lambda: (_chat(page)["compose"] or {}).get("openedId")
                   if not _chat(page)["busy"] else None, timeout=4)
    known = page.evaluate(f"!!window.__hud.state().threads.find(t => t.id === '{opened}')") if opened else None
    check("setup: a failed first send left an opened row whose record is not loaded", bool(opened) and not known,
          f"{opened} loaded={known}")
    url = re.compile(rf".*/threads/{re.escape(str(opened))}$")
    held = []

    def hold_first(route):
        if route.request.method == "PATCH" and not held:
            held.append(route)
        else:
            route.continue_()

    page.route(url, hold_first)
    try:
        _open_pop(page, until)
        page.locator(POP + f' [data-testid="model-opt"][data-value="{LUNA}"]').click()
        until(lambda: page.wait_for_timeout(1) is None and len(held) == 1, timeout=3)
        # The effort change behind it is refused.
        mock.world["refuse_choice"] = "refused for the test"
        at = _now(page)
        stops = page.locator(POP + ' [data-testid="effort-stop"]')
        box_ = stops.nth(0 if at != 0 else stops.count() - 1).bounding_box()
        page.mouse.click(box_["x"] + box_["width"] / 2, box_["y"] + box_["height"] / 2)
        err = until(lambda: page.locator(_pane(1, '[data-testid="model-error"]')).count() > 0, timeout=3)
        check("setup: the model change is held and the effort change behind it refused",
              len(held) == 1 and bool(err))
        mock.world["refuse_choice"] = None
        held[0].continue_()
        got = until(lambda: page.locator(BTN).get_attribute("data-model") == LUNA, timeout=3)
        row = _chat(page)["compose"] or {}
        check("review #30 R2: the held model change's success still reaches the opened row (the server holds Luna)",
              bool(got) and row.get("model") == LUNA,
              f"chip {page.locator(BTN).get_attribute('data-model')!r}, row {row}")
    finally:
        mock.world["refuse_choice"] = None
        page.unroute(url)
    _close_pop(page, until)


# ---- R1: the thread archived from another window ----------------------------

def _archived_checks(page, mock, check, until):
    _fresh(page, mock, until, SINGLE)
    _open(page, until, "ea1")
    _open_pop(page, until)
    page.locator(SLIDER).focus()
    n = len(mock.calls)
    page.keyboard.press("ArrowLeft")
    t = _thread(mock, "ea1")
    t["archived"] = True
    mock.emit("thread_archived", {"thread_id": "ea1"}, thread_id="ea1", project_id=t["project_id"])
    row = until(lambda: _chat(page)["compose"] if _chat(page)["threadId"] is None else None, timeout=3)
    time.sleep(0.9)
    late = [(p, b) for m, p, b in mock.calls[n:] if m == "PATCH" and p.startswith("/threads/")]
    check("setup: the move was still settling when the archive re-aimed the pane at a new compose row",
          bool(row) and not late, f"row {row}, {late}")
    row = _chat(page)["compose"] or {}
    check("review #30 R1: the archived thread's move does not land on the new compose row",
          row.get("effort") is None and row.get("model") is None, str(row))
    opened = len(mock.posted("/threads"))
    box = page.locator(_pane(1, '[data-testid="input"]'))
    box.fill("after the archive")
    box.press("Enter")
    body = until(lambda: mock.posted("/threads")[opened:] or None, timeout=3)
    brief = (body[-1].get("brief") or {}) if body else {}
    check("and the new thread's first POST /threads carries no effort from it",
          bool(body) and "effort" not in brief, str(body[-1] if body else None))
    tid = until(lambda: _chat(page)["threadId"], timeout=3)
    if tid:
        mock.emit("turn_finished", {"stop": "end"}, thread_id=tid)


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
            effort_checks(browser, mock, f"http://127.0.0.1:{mock.port}", H.check, H.until,
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
    print(f"all {H.CHECKS} effort checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
