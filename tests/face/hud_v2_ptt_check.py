"""Headless checks for push-to-talk's key: Ctrl+D (2026-10-10).

The owner asked: "can we change the push to talk button? change it to Ctrl + D
just like it is in claude", and answered the follow-ups — in a terminal Ctrl+D
stays the shell's (end of input); in Monaco it is push-to-talk and Monaco's own
Ctrl+D ("add selection to next find match") moves to Ctrl+Shift+D; hold to
talk, let go to send; and Space is no longer push-to-talk anywhere.

Called from `hud_v2_check.main()` after the terminal and preview sections, in a browser
context of its own and against **a `MockDaemon` of its own** on an ephemeral
port, as the other sections are. It also runs on its own:
`python tests/face/hud_v2_ptt_check.py`. The terminal half (Ctrl+D reaching the
PTY as `\\x04`, and nothing under a card) is in `hud_v2_terminal_check.py`,
where the PTY is played; W-6 (it goes to the selected chat) in
`hud_v2_multichat_check.py`, ported from Space.

The checks worth keeping, each shown to fail on origin/main (where Ctrl+D is
nothing of the HUD's and Space is push-to-talk):
  - Ctrl+D held in the chat box, the sidebar, a pane header and Monaco starts
    push-to-talk and letting go of D, or of Ctrl, ends it; the window takes it
    from the browser everywhere (`defaultPrevented`: no bookmark dialog), and
    the box, the button or the editor under it does nothing with it;
  - held long enough, letting go sends what was said, and nothing is sent
    while it is held;
  - Ctrl+Shift+D, Ctrl+Alt+D and Meta+D are not it, and auto-repeat never
    presses twice (one interrupt for one hold);
  - under a card it does nothing and Escape still denies; muted it records
    nothing but still stops his turn (as the orb does); after a crash it
    refuses; a window that loses focus mid-hold lets go;
  - Space never starts the mic: in the box, on the page, a pane header, a
    separator, and in Monaco — where it used to start the mic and the space
    never appeared;
  - Monaco's next-match command works on Ctrl+Shift+D, and the Ctrl+K chord on
    Ctrl+K Ctrl+Shift+D.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.face.hud_v2_layout_check import (  # noqa: E402
    TALK_PROBE, _active_testid, _approval, _attr, _crash, _hold_keys, _pane, _ptt_on_ctrl_d, _ptt_on_space,
    _reload_to_window, _talk_keys, _talk_probe, _window_whole,
)

SINGLE = json.dumps({"preset": "single", "focused": 1,
                     "panes": [{"view": "chat"}, {"view": "file"}, {"view": "preview"}, {"view": "task"}]})
# A chat, and a File pane beside it: push-to-talk needs a chat on screen (W-6).
CHAT_FILE = json.dumps({"preset": "cols2", "focused": 1,
                        "panes": [{"view": "chat"}, {"view": "file"}, {"view": "preview"}, {"view": "task"}]})


def ptt_checks(browser, mock, base, check, until, guard, init_script):
    print("\npush-to-talk is Ctrl+D (2026-10-10)")
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, permissions=["microphone"])
    guard(ctx)                      # live ports refused, HTTP and WebSocket alike (hud_v2_check.guard_live)
    ctx.add_init_script(init_script)
    ctx.add_init_script(TALK_PROBE)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    # Monaco's checks leave an edit unsaved, and the window asks before a
    # reload loses one: say yes, as the owner would here.
    page.on("dialog", lambda d: d.accept())
    page.set_default_timeout(5000)
    try:
        before = mock.sse_connections()
        page.goto(base + "/")
        page.wait_for_selector('[data-testid="sidebar"]', state="attached")
        mock.await_reconnect(before)
        # Run alone, `python tests/face/hud_v2_ptt_check.py box_checks,monaco_checks` runs those only.
        only = set(filter(None, (sys.argv[1] if len(sys.argv) > 1 and __name__ == "__main__" else "").split(",")))
        for section in (_box_checks, _places_checks, _modifier_checks, _repeat_checks, _card_checks,
                        _muted_checks, _blur_checks, _space_checks, _monaco_checks, _crash_checks):
            if only and section.__name__.strip("_") not in only:
                continue
            try:
                section(page, mock, check, until)
            except Exception as e:  # a section that cannot run is a failure, and the rest still run
                check(f"ptt {section.__name__.strip('_')} ran to the end", False, str(e).splitlines()[0][:200])
                try:
                    for k in ("d", "Control", "Shift", "Alt", "Space"):
                        page.keyboard.up(k)
                    page.keyboard.press("Escape")
                except Exception:
                    pass
        check("no page errors in the push-to-talk section", not errors, "; ".join(errors[:3]))
    finally:
        ctx.close()


# ---------------------------------------------------------------------------

def _fresh(page, mock, until, ws: str = SINGLE, mode: str = "review", size=(1280, 800)):
    """Reload into a stored workspace at 100%, both side panes open, a dictation mode."""
    page.set_viewport_size({"width": size[0], "height": size[1]})
    page.evaluate("""([ws, mode]) => {
      localStorage.setItem('jarvis.hud.zoom', '100');
      localStorage.setItem('jarvis.hud.layout', '{}');
      localStorage.setItem('jarvis.hud.workspace', ws);
      localStorage.setItem('jarvis.dictation', mode);
    }""", [ws, mode])
    before = mock.sse_connections()
    page.reload()
    page.wait_for_selector('[data-testid="sidebar"]', state="attached")
    until(lambda: page.locator('[data-testid="project-p1"]').count() > 0)
    mock.await_reconnect(before)
    until(lambda: page.locator(_pane(1, '[data-testid="input"]')).count() > 0)
    time.sleep(0.3)
    _talk_probe(page)


def _box(page):
    return page.locator(_pane(1, '[data-testid="input"]'))


def _blur(page):
    page.evaluate("document.activeElement && document.activeElement.blur()")


def _chat1(page) -> dict:
    return page.evaluate("window.__hud.state().chats")["1"]


def _open_t1(page, until):
    """Thread t1 in pane 1."""
    for _ in range(4):
        if page.locator('[data-testid="thread-t1"]').count():
            break
        page.locator('[data-testid="project-p1"]').click()
        until(lambda: page.locator('[data-testid="thread-t1"]').count() > 0, timeout=1.0)
    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: _chat1(page)["threadId"] == "t1", timeout=3)


def _busy_t1(page, mock, until, text: str):
    """Start a turn in t1 (the mock keeps it running until turn_finished)."""
    _open_t1(page, until)
    n = len(mock.sent("POST", "/threads/t1/send"))
    _box(page).fill(text)
    _box(page).press("Enter")
    until(lambda: len(mock.sent("POST", "/threads/t1/send")) > n, timeout=3)
    return bool(until(lambda: _chat1(page)["busy"], timeout=3))


def _end_t1(page, mock, until):
    mock.emit("turn_finished", {"stop": "end"}, thread_id="t1")
    until(lambda: not _chat1(page)["busy"], timeout=3)


def _interrupts(mock) -> int:
    return len(mock.sent("POST", "/threads/t1/interrupt"))


def _deny(page, mock, until, req: str) -> bool:
    page.keyboard.press("Escape")
    body = until(lambda: mock.sent("POST", f"/approvals/{req}") or None, timeout=4)
    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
    return bool(body) and body[-1].get("decision") == "deny"


# ---- the chat box -----------------------------------------------------------

def _box_checks(page, mock, check, until):
    _fresh(page, mock, until)
    box = _box(page)
    box.click()
    box.fill("draft words")
    held, after, key = _ptt_on_ctrl_d(page)
    check("Ctrl+D held in the chat box starts push-to-talk", held is not None, str(held))
    check("and letting go of D ends it", after is None, str(after))
    check("the window takes it from the browser there: its default is prevented (no bookmark dialog)",
          key.get("prevented") is True and key.get("ctrl") is True, str(key))
    check("and the box does nothing with it: same words, focus still in it",
          box.input_value() == "draft words" and _active_testid(page) == "input",
          f"{box.input_value()!r} {_active_testid(page)}")
    held, after, _ = _ptt_on_ctrl_d(page, ctrl_first=True)
    check("letting go of Ctrl first ends it too", held is not None and after is None, f"{held} {after}")
    box.press("End")
    box.type(" d")
    check("a plain d typed in the box is a letter, never push-to-talk",
          box.input_value() == "draft words d" and page.evaluate("window.__hud.capture.ptt") is None,
          box.input_value())

    # Hold to talk, let go to send: nothing leaves while it is held.
    box.fill("")
    stt = len(mock.sent("POST", "/stt"))
    page.keyboard.down("Control")
    page.keyboard.down("d")
    page.evaluate("() => { window.__hud.mic.feedMs(1500, 0.06); return true; }")
    time.sleep(0.3)
    while_held = len(mock.sent("POST", "/stt")) - stt
    page.keyboard.up("d")
    page.keyboard.up("Control")
    sent = until(lambda: len(mock.sent("POST", "/stt")) > stt, timeout=4)
    time.sleep(0.3)
    check("held, nothing is sent; let go, what was said goes once for transcription",
          while_held == 0 and bool(sent) and len(mock.sent("POST", "/stt")) == stt + 1,
          f"{while_held} while held, {len(mock.sent('POST', '/stt')) - stt} after")
    # REVIEW: the transcript lands in the selected chat's box.
    got = until(lambda: box.input_value() == mock.world["stt_text"], timeout=3)
    check("and in REVIEW it lands in the selected chat's box", bool(got), box.input_value())
    box.fill("")


# ---- the sidebar and a pane header -------------------------------------------

def _places_checks(page, mock, check, until):
    _fresh(page, mock, until)
    compose = _chat1(page)["compose"]
    page.locator('[data-testid="new-thread"]').focus()
    held, after, key = _ptt_on_ctrl_d(page)
    check("Ctrl+D on a focused sidebar button starts and stops push-to-talk",
          held is not None and after is None, f"{held} {after}")
    check("and is taken from the browser, pressing nothing there",
          key.get("prevented") is True and _active_testid(page) == "new-thread" and _chat1(page)["compose"] == compose,
          f"{key} {_active_testid(page)}")
    page.locator('[data-testid="project-p1"]').focus()
    held, after, key = _ptt_on_ctrl_d(page)
    check("on a focused project row too", held is not None and after is None and key.get("prevented") is True,
          f"{held} {after} {key}")
    tab = page.locator(_pane(1, '[data-testid="tab-chat"]'))
    tab.focus()
    held, after, key = _ptt_on_ctrl_d(page)
    check("on a pane header (its view tab) too, and the pane keeps its view",
          held is not None and after is None and key.get("prevented") is True
          and _attr(page, _pane(1), "data-view") == "chat", f"{held} {after} {key}")
    _blur(page)
    held, after, key = _ptt_on_ctrl_d(page)
    check("and with nothing focused at all", held is not None and after is None and key.get("prevented") is True,
          f"{held} {after} {key}")


# ---- modifiers and auto-repeat -----------------------------------------------

def _modifier_checks(page, mock, check, until):
    _fresh(page, mock, until)
    _blur(page)
    for name, keys in (("Ctrl+Shift+D", ["Control", "Shift", "d"]), ("Ctrl+Alt+D (AltGr)", ["Control", "Alt", "d"]),
                       ("Meta+D", ["Meta", "d"]), ("Ctrl+Meta+D", ["Control", "Meta", "d"]), ("D", ["d"])):
        held, _ = _hold_keys(page, keys)
        check(f"{name} is not push-to-talk", held is None, str(held))


def _repeat_checks(page, mock, check, until):
    _fresh(page, mock, until)
    ok = _busy_t1(page, mock, until, "a long job")
    check("setup: a turn is running in the selected chat", ok)
    i0 = _interrupts(mock)
    _blur(page)
    page.keyboard.down("Control")
    page.keyboard.down("d")
    for _ in range(4):                # auto-repeat: the key is already down
        time.sleep(0.04)
        page.keyboard.down("d")
    time.sleep(0.1)
    held = page.evaluate("window.__hud.capture.ptt")
    page.keyboard.up("d")
    page.keyboard.up("Control")
    keys = _talk_keys(page, 5)
    check("setup: the keydowns after the first are auto-repeat",
          [k.get("repeat") for k in keys] == [False, True, True, True, True], str(keys))
    check("every one of them is taken from the browser", all(k.get("prevented") for k in keys), str(keys))
    until(lambda: _interrupts(mock) > i0, timeout=3)
    time.sleep(0.4)
    check("auto-repeat never presses twice: one hold stops his turn once",
          held is not None and _interrupts(mock) == i0 + 1, f"held={held} interrupts={_interrupts(mock) - i0}")
    _end_t1(page, mock, until)


# ---- a card, the mic muted, a lost focus -------------------------------------

def _card_checks(page, mock, check, until):
    _fresh(page, mock, until)
    _box(page).click()
    _approval(mock, "pttcard1")
    until(lambda: page.locator('[data-testid="approval-card"]').count() > 0, timeout=4)
    time.sleep(0.2)
    held, after, key = _ptt_on_ctrl_d(page)
    check("under a card Ctrl+D does nothing: no push-to-talk", held is None and after is None, f"{held} {after}")
    check("and it is still taken from the browser", key.get("prevented") is True, str(key))
    page.evaluate("document.querySelector('[data-testid=\"input\"]').focus()")
    held, _, key = _ptt_on_ctrl_d(page)
    check("nor with the box focused behind the card", held is None and key.get("prevented") is True, f"{held} {key}")
    check("and the card is still up", page.locator('[data-testid="approval-card"]').count() == 1)
    check("Escape still denies it", _deny(page, mock, until, "pttcard1"))
    held, after, _ = _ptt_on_ctrl_d(page)
    check("with the card answered Ctrl+D works again", held is not None and after is None, f"{held} {after}")


def _muted_checks(page, mock, check, until):
    _fresh(page, mock, until, mode="off")
    _box(page).click()
    held, after, key = _ptt_on_ctrl_d(page)
    check("muted (OFF), Ctrl+D records nothing", held is None and after is None, f"{held} {after}")
    check("says the mic is muted, and is taken from the browser",
          (_chat1(page).get("status") or "") == "MIC MUTED" and key.get("prevented") is True,
          f"{_chat1(page).get('status')!r} {key}")
    ok = _busy_t1(page, mock, until, "work while muted")
    check("setup: a turn is running, the mic muted", ok)
    i0 = _interrupts(mock)
    _blur(page)
    held, _, _ = _ptt_on_ctrl_d(page)
    until(lambda: _interrupts(mock) > i0, timeout=3)
    check("muted, it still stops his turn (the orb's interrupt half) and records nothing",
          held is None and _interrupts(mock) == i0 + 1, f"held={held} interrupts={_interrupts(mock) - i0}")
    _end_t1(page, mock, until)


def _blur_checks(page, mock, check, until):
    _fresh(page, mock, until)
    _blur(page)
    page.keyboard.down("Control")
    page.keyboard.down("d")
    time.sleep(0.1)
    held = page.evaluate("window.__hud.capture.ptt")
    page.evaluate("window.dispatchEvent(new Event('blur'))")
    after = page.evaluate("window.__hud.capture.ptt")
    page.keyboard.up("d")
    page.keyboard.up("Control")
    check("a window that loses focus mid-hold lets go (its keyup never comes)",
          held is not None and after is None, f"{held} {after}")


# ---- Space is nothing of the mic's --------------------------------------------

def _space_checks(page, mock, check, until):
    _fresh(page, mock, until)
    box = _box(page)
    box.click()
    box.fill("a")
    page.keyboard.down("Space")
    time.sleep(0.15)
    ptt = page.evaluate("window.__hud.capture.ptt")
    page.keyboard.up("Space")
    check("Space held in the box types a space and never starts the mic",
          ptt is None and box.input_value() == "a ", f"{ptt} {box.input_value()!r}")
    box.fill("")
    _blur(page)
    check("Space held with nothing focused never starts the mic", _ptt_on_space(page) is None)
    page.locator(_pane(1, '[data-testid="tab-chat"]')).focus()
    check("nor on a pane header", _ptt_on_space(page) is None)
    page.locator('[data-testid="split-left"]').focus()
    check("nor on a separator", _ptt_on_space(page) is None)
    page.locator('[data-testid="project-p1"]').focus()
    check("nor on a sidebar row", _ptt_on_space(page) is None)


# ---- Monaco --------------------------------------------------------------------

EDITOR = 2  # the File pane beside the chat


def _editor_text(page) -> str:
    return page.locator(_pane(EDITOR, '[data-testid="editor"] .view-lines')).inner_text().replace("\u00a0", " ")


def _cursors(page) -> int:
    return page.locator(_pane(EDITOR, '[data-testid="editor"] .cursors-layer .cursor')).count()


def _select_first(page, chars: int):
    page.keyboard.press("Escape")             # one cursor
    page.keyboard.press("Control+Home")
    for _ in range(chars):
        page.keyboard.press("Shift+ArrowRight")


def _monaco_checks(page, mock, check, until):
    _fresh(page, mock, until, CHAT_FILE, size=(1600, 900))
    _open_t1(page, until)
    check("setup: a chat beside the File pane, the selected one",
          page.evaluate("window.__hud.state().selectedChat") == 1)
    until(lambda: page.locator(_pane(EDITOR, '[data-testid="file-calc.py"]')).count() > 0, timeout=4)
    page.locator(_pane(EDITOR, '[data-testid="file-calc.py"]')).click()
    lines = page.locator(_pane(EDITOR, '[data-testid="editor"] .view-lines'))
    until(lambda: lines.count() == 1 and lines.inner_text().strip() != "", timeout=10)
    check("setup: the file is open in Monaco", lines.count() == 1)
    lines.click()
    page.keyboard.press("Control+a")
    page.keyboard.type("foo bar foo bar foo")
    until(lambda: _editor_text(page).strip() == "foo bar foo bar foo", timeout=3)
    check("setup: the editor holds foo bar foo bar foo", _editor_text(page).strip() == "foo bar foo bar foo",
          repr(_editor_text(page)))

    page.keyboard.press("End")
    page.keyboard.down("Space")
    time.sleep(0.15)
    ptt = page.evaluate("window.__hud.capture.ptt")
    page.keyboard.up("Space")
    until(lambda: _editor_text(page).rstrip("\n") == "foo bar foo bar foo ", timeout=2)
    check("Space in Monaco types a space and never starts the mic (it used to do both wrong)",
          ptt is None and _editor_text(page).rstrip("\n") == "foo bar foo bar foo ", f"{ptt} {_editor_text(page)!r}")

    _select_first(page, 3)
    check("setup: one cursor, the first foo selected", _cursors(page) == 1, str(_cursors(page)))
    held, after, key = _ptt_on_ctrl_d(page)
    check("Ctrl+D held in Monaco starts and stops push-to-talk", held is not None and after is None,
          f"{held} {after}")
    check("and Monaco never sees it: no second selection, its default prevented",
          _cursors(page) == 1 and key.get("prevented") is True and _editor_text(page).startswith("foo bar"),
          f"cursors={_cursors(page)} {key}")

    page.keyboard.press("Control+Shift+D")
    until(lambda: _cursors(page) == 2, timeout=2)
    check("Monaco's add-selection-to-next-match is Ctrl+Shift+D now: a second cursor on the next foo",
          _cursors(page) == 2 and page.evaluate("window.__hud.capture.ptt") is None, str(_cursors(page)))
    page.keyboard.type("X")
    until(lambda: _editor_text(page).startswith("X bar X"), timeout=2)
    check("and typing replaces both foos", _editor_text(page).rstrip("\n") == "X bar X bar foo ",
          repr(_editor_text(page)))

    _select_first(page, 1)
    page.keyboard.press("Control+k")
    page.keyboard.press("Control+Shift+D")
    page.keyboard.type("Y")
    until(lambda: "Y" in _editor_text(page), timeout=2)
    check("and its Ctrl+K chord is Ctrl+K Ctrl+Shift+D: the selection moves to the next X",
          _editor_text(page).rstrip("\n") == "X bar Y bar foo ", repr(_editor_text(page)))


# ---- after a crash -----------------------------------------------------------------

def _crash_checks(page, mock, check, until):
    _fresh(page, mock, until)
    check("setup: the window crashes into its Reload prompt", _crash(page, until))
    _blur(page)
    held, after, key = _ptt_on_ctrl_d(page)
    check("after a crash Ctrl+D refuses: nothing on screen to talk to", held is None and after is None,
          f"{held} {after}")
    check("and it is still taken from the browser", key.get("prevented") is True, str(key))
    page.locator('[data-testid="hud-crashed-reload"]').focus()
    held, _, _ = _ptt_on_ctrl_d(page)
    check("nor with Reload focused", held is None, str(held))
    reloaded = _reload_to_window(page, mock, until, lambda: page.locator('[data-testid="hud-crashed-reload"]').click())
    ok, why = _window_whole(page)
    check("Reload brings the window back", reloaded and ok, why)
    _talk_probe(page)
    _blur(page)
    held, after, _ = _ptt_on_ctrl_d(page)
    check("and Ctrl+D talks again", held is not None and after is None, f"{held} {after}")


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
            ptt_checks(browser, mock, f"http://127.0.0.1:{mock.port}", H.check, H.until,
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
    print(f"all {H.CHECKS} push-to-talk checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
