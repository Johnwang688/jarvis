"""Settings S1–S8: real browser checks against isolated mock HTTP/SSE/PTYs."""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests.face.hud_v2_check import open_settings, expand
from tests.face.hud_v2_layout_check import _approval, _boot, _reachable, _set_storage, _set_ws, _show
from tests.face.hud_v2_terminal_check import FakePty, _new_terminal, _focus_term, _text as term_text

from tests.face.hud_v2_editor_check import _open, _type, _text as editor_text
from tests.face.hud_v2_browser_check import _setup as browser_setup, _focus_url

PAGE = '[data-testid="settings-page"]'


def settings_page_checks(browser, mock, base, check, until, guard, init_script):
    print("\nSettings: sections, mounted work, nested dialogs and approvals")
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, permissions=["microphone"])
    guard(ctx)
    ctx.add_init_script(init_script)
    fake = FakePty(ctx, mock)
    page = ctx.new_page()
    page.set_default_timeout(6000)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        _boot(page, mock, base, until)
        box = page.locator('[data-testid="input"]').first
        box.fill("unsent selection survives")
        box.evaluate("e => { e.focus(); e.setSelectionRange(7, 16); window.__settingsBox = e; }")
        page.keyboard.press("Control+,")
        page.wait_for_selector(PAGE)
        check("Ctrl+, opens General first", page.locator('[data-testid="settings-general"]').get_attribute("aria-current") == "page")
        check("the covered workspace remains mounted and inert",
              page.evaluate("window.__settingsBox.isConnected && document.querySelector('.workspace-surface').inert")
              and not box.is_visible())
        check("only Schedules remains in the sidebar footer",
              page.locator(".sidebar-foot .tree-row").count() == 1
              and page.locator('[data-testid="open-usage"],[data-testid="open-route"],[data-testid="open-archive"]').count() == 0)
        check("Usage and decisions left the status column",
              page.locator('#right [data-testid="usage"],#right [data-testid="decisions"]').count() == 0)
        for section, interior in [("General", "install-app"), ("Model", "model-scope-note"),
                                  ("Voice", "voice-default"), ("Avatar", "avatar-hoot"),
                                  ("Usage", "usage"), ("Routing", "route-table"), ("Archive", "archive-view")]:
            open_settings(page, section)
            check(f"{section} has its existing content", page.locator(f'[data-testid="{interior}"]').count() == 1)
        page.keyboard.press("Escape")
        page.wait_for_selector(PAGE, state="detached")
        check("close restores the same input, text and selection", bool(until(lambda: page.evaluate("""() =>
          document.activeElement === window.__settingsBox && window.__settingsBox.value === 'unsent selection survives'
          && window.__settingsBox.selectionStart === 7 && window.__settingsBox.selectionEnd === 16"""))))
        open_settings(page, "Model")
        page.locator('[data-testid="model-pin-more"]').click()
        page.wait_for_selector('[data-testid="catalog"]')
        check("catalog is nested above Settings", page.locator(PAGE).evaluate("e => e.inert"))
        page.locator('[data-testid="catalog-close"]').focus()
        page.keyboard.press("Escape")
        page.wait_for_selector('[data-testid="catalog"]', state="detached")
        check("catalog Escape returns to Model and its opener",
              page.locator(PAGE).count() == 1 and page.evaluate("document.activeElement.dataset.testid") == "model-pin-more")
        _approval(mock, "settings-card")
        page.wait_for_selector('[data-testid="approval-card"]')
        check("an approval makes Settings inert and its gear disabled",
              page.locator(PAGE).evaluate("e => e.inert")
              and page.locator('[data-testid="titlebar-settings"]').is_disabled())
        page.keyboard.press("Control+,")
        page.keyboard.press("Enter")
        check("Ctrl+, and Enter under the card do not close or authorize",
              page.locator(PAGE).count() == 1 and not mock.sent("POST", "/approvals/settings-card"))
        page.keyboard.press("Escape")
        page.wait_for_selector('[data-testid="approval-card"]', state="detached")
        check("Escape denies only the approval", page.locator(PAGE).count() == 1
              and mock.sent("POST", "/approvals/settings-card")[-1]["decision"] == "deny")
        page.locator('[data-testid="settings-close"]').click()

        # Fake PTY: output continues, input shortcut is never shell input.
        box.fill("")
        tid = _new_terminal(page, mock, until, fake)
        _focus_term(page, tid)
        inputs = len(fake.sent(tid))
        sockets = len(fake.sockets)
        page.keyboard.press("Control+,")
        page.wait_for_selector(PAGE)
        page.keyboard.type("hidden-terminal-must-not-receive")
        fake.output(tid, b"output while Settings is open\r\n")
        check("terminal output continues while covered",
              bool(until(lambda: "output while Settings is open" in term_text(page, tid))))
        check("Settings shortcut and keys never reach the shell", len(fake.sent(tid)) == inputs)
        page.keyboard.press("Escape")
        page.wait_for_selector(PAGE, state="detached")
        check("closing restores terminal focus without reattaching",
              len(fake.sockets) == sockets and bool(until(lambda: page.evaluate("!!document.activeElement.closest('.termslot')"))))

        # Opening Settings during a real mocked turn keeps its lifecycle.
        expand(page, "p1", "thread-t1")
        page.locator('[data-testid="thread-t1"]').click()
        until(lambda: page.evaluate("window.__hud.state().threadId") == "t1")
        mock.emit("turn_started", {}, thread_id="t1")
        until(lambda: page.evaluate("window.__hud.state().busy"))
        open_settings(page, "General")
        mock.emit("text_delta", {"text": "reply while settings covers work"}, thread_id="t1")
        check("an ongoing turn keeps receiving text behind Settings", bool(until(
            lambda: "reply while settings covers work" in page.locator('[data-testid="draft"]').text_content())))
        mock.emit("text", {"text": "reply while settings covers work"}, thread_id="t1")
        mock.emit("turn_finished", {}, thread_id="t1")
        check("that turn finishes while Settings remains open", bool(until(
            lambda: not page.evaluate("window.__hud.state().busy"))) and page.locator(PAGE).count() == 1)
        page.locator('[data-testid="settings-close"]').click()

        # Model identity, unsaved content and cursor must survive the cover.
        _show(page, until, 1, "file")
        check("editor fixture opens", _open(page, until, 1, "calc.py"))
        check("editor fixture becomes dirty", _type(page, until, 1, "# retained settings edit"))
        page.keyboard.press("Control+Home")
        page.keyboard.press("ArrowRight")
        before_editor = page.evaluate("window.__hud.monacoEditors()")
        before_models = page.evaluate("window.__hud.monacoModels()")
        before_buffers = page.evaluate("window.__hud.buffers()")
        page.keyboard.press("Control+,")
        page.wait_for_selector(PAGE)
        check("covered editor stays mounted and read-only",
              page.evaluate("window.__hud.monacoModels()") == before_models
              and all(e["readOnly"] for e in page.evaluate("window.__hud.monacoEditors()")))
        page.keyboard.press("Control+s")
        page.keyboard.press("Control+b")
        page.keyboard.press("Control+Shift+p")
        check("hidden editor commands do not save or open another dialog",
              page.evaluate("window.__hud.buffers()") == before_buffers
              and page.locator('[data-testid="palette"]').count() == 0)
        page.keyboard.press("Escape")
        page.wait_for_selector(PAGE, state="detached")
        check("dirty editor and exact cursor return", bool(until(lambda:
              page.evaluate("window.__hud.monacoEditors()") == before_editor
              and page.evaluate("window.__hud.buffers()") == before_buffers
              and "# retained settings edit" in editor_text(page, 1))),
              str({"before": before_editor, "after": page.evaluate("window.__hud.monacoEditors()"),
                   "buffers_before": before_buffers, "buffers_after": page.evaluate("window.__hud.buffers()"),
                   "rendered": editor_text(page, 1)}))
        # Save this synthetic file before later reloads; no real file is used.
        page.keyboard.press("Control+s")
        until(lambda: all(not b["dirty"] for b in page.evaluate("window.__hud.buffers()")))

        archived = dict(next(t for t in mock.world["threads"] if t["id"] == "t1"),
                        id="settings-old", title="Archived test chat", archived="2026-10-12T00:00:00Z")
        mock.world["threads"].append(archived)
        open_settings(page, "Archive")
        page.wait_for_selector('[data-testid="archived-thread-settings-old"]')
        page.locator('[data-testid="archive-delete-thread-settings-old"]').click()
        page.wait_for_selector('[data-testid="delete-confirm"]')
        page.locator('[data-testid="delete-confirm-btn"]').focus()
        page.keyboard.press("Enter")
        check("Enter does not delete from Archive", not mock.sent("DELETE", "/threads/settings-old"))
        _approval(mock, "settings-archive")
        page.wait_for_selector('[data-testid="approval-card"]')
        page.keyboard.press("Escape")
        page.wait_for_selector('[data-testid="approval-card"]', state="detached")
        check("approval Escape leaves archive confirmation intact",
              page.locator('[data-testid="delete-confirm"]').count() == 1)
        page.keyboard.press("Escape")
        page.wait_for_selector('[data-testid="delete-confirm"]', state="detached")
        check("confirmation Escape returns to Archive without leaving Settings", page.locator(PAGE).count() == 1)
        page.keyboard.press("Escape")
        page.wait_for_selector(PAGE, state="detached")

        # Quiet frames stay mounted; attempted hidden focus cannot take keys.
        browser_setup(page, mock, until, {"browserUrl": _focus_url(mock, "focus-none")})
        frame = page.locator('[data-testid="browser-frame"]')
        frame.wait_for()
        frame.evaluate("e => window.__settingsFrame = e")
        page.locator('[data-testid="input"]').first.focus()
        open_settings(page, "General")
        check("covered browser stays mounted", page.evaluate("window.__settingsFrame.isConnected"))
        keys_before = len(mock.dev_keys)
        content = next(f for f in page.frames if "/focus-none" in f.url)
        try:
            content.evaluate("window.focus()")
        except Exception as e:
            # The focus guard can remove the attacker before evaluate replies.
            if "Frame was detached" not in str(e) or not page.locator('[data-testid="browser-stolen"]').count():
                raise
        page.keyboard.type("never-to-hidden-frame")
        check("covered frame cannot take Settings keys",
              page.evaluate("document.activeElement.tagName !== 'IFRAME'") and len(mock.dev_keys) == keys_before)
        page.keyboard.press("Escape")
        page.wait_for_selector(PAGE, state="detached")
        # The guard may remove a frame that attempted focus, which is expected.
        check("a blocked frame never changes the workspace selection",
              page.evaluate("window.__hud.workspace().ws.preset") == "cols2")

        # Geometry and authorization stay reachable through both zoom extremes.
        for width, zoom in [(420, "70"), (420, "160"), (1280, "160")]:
            page.set_viewport_size({"width": width, "height": 700})
            _set_storage(page, zoom=zoom)
            _boot(page, mock, None, until, reload=True)
            open_settings(page, "Routing")
            geometry = page.locator(PAGE).evaluate("""e => { const r=e.getBoundingClientRect();
              const t=document.getElementById('titlebar').getBoundingClientRect();
              return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,title:t.bottom,w:innerWidth,h:innerHeight}; }""")
            check(f"Settings fits {width}px at {zoom}%", geometry["left"] >= -0.5
                  and geometry["right"] <= width + 0.5 and abs(geometry["top"] - geometry["title"]) < 1
                  and geometry["bottom"] <= geometry["h"] + 0.5, str(geometry))
            check(f"close remains reachable at {width}px/{zoom}%", _reachable(page, '[data-testid="settings-close"]')[0])
            check(f"sections remain reachable at {width}px/{zoom}%", all(
                _reachable(page, f'[data-testid="settings-{name}"]')[0] for name in
                ("general", "model", "voice", "avatar", "usage", "routing", "archive")))
            _approval(mock, f"settings-{width}-{zoom}")
            page.wait_for_selector('[data-testid="approval-card"]')
            check(f"approval is above Settings at {width}px/{zoom}%", _reachable(page, '[data-testid="approval-deny"]')[0])
            page.keyboard.press("Escape")
            page.wait_for_selector('[data-testid="approval-card"]', state="detached")
            page.locator('[data-testid="settings-close"]').click()
        page.set_viewport_size({"width": 1280, "height": 800})
        _set_storage(page, zoom="100")
        _boot(page, mock, None, until, reload=True)
        page.locator('[data-testid="titlebar-settings"]').click()
        check("last section survives reopen and reload",
              page.locator('[data-testid="settings-routing"]').get_attribute("aria-current") == "page")

        # A section exception must not take down the work or the card.
        _approval(mock, "settings-crash")
        page.wait_for_selector('[data-testid="approval-card"]')
        page.evaluate("window.__hudCrashProbe='settings'; window.__hud.dispatch({type:'patch',patch:{error:'render'}})")
        page.wait_for_selector(PAGE, state="detached")
        check("Settings crash leaves workspace mounted and approval intact",
              page.locator('[data-testid="workspace"]').count() == 1
              and page.locator('[data-testid="approval-card"]').count() == 1
              and page.locator(".workspace-surface").evaluate("e => e.inert")
              and page.locator('[data-testid="hud-crashed"]').count() == 0)
        page.keyboard.press("Escape")
        page.wait_for_selector('[data-testid="approval-card"]', state="detached")
        page.evaluate("delete window.__hudCrashProbe")
        check("work becomes interactive after that card is answered",
              not page.locator(".workspace-surface").evaluate("e => e.inert"))

        page.evaluate("""() => {
          Storage.prototype.getItem = () => { throw Error('storage unavailable'); };
          Storage.prototype.setItem = () => { throw Error('storage unavailable'); };
        }""")
        open_settings(page, "Voice")
        page.keyboard.press("Escape")
        check("storage errors do not prevent section changes or closing", page.locator(PAGE).count() == 0)
        check("Settings raises no uncaught page errors", not errors, str(errors))
    finally:
        ctx.close()


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as common
    from tests.face.hud_v2_mock import MockDaemon
    mock = MockDaemon(int(os.environ.get("HUD_V2_CHECK_PORT", "18731"))).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
            settings_page_checks(browser, mock, f"http://127.0.0.1:{mock.port}", common.check, common.until,
                                 common.guard, common.FAKE_RECOGNIZER)
            browser.close()
    finally:
        mock.stop()
    print(f"{common.CHECKS} Settings checks; {len(common.FAILURES)} failures")
    return bool(common.FAILURES)


if __name__ == "__main__":
    sys.exit(main())
