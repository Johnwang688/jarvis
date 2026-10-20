"""B-DIRECT-CAPTURE (BR-4/M6) over the real HUD bundle and real Chromium.

Chromium's own getDisplayMedia, CropTarget, cropTo and ImageCapture run here
(new headless with `--auto-accept-this-tab-capture`, which accepts the tab
share prompt). **This is not M6 acceptance:** there is no Windows app window,
no owner at a real prompt, and Playwright's display is not the owner's. M6 is
the owner's probe in the installed app (docs/plans/2026-10-16-owner-live-checks.md).

The isolated mock daemon serves the HUD with the real CSP and answers the
probe and the URL-only reference with the real `direct_probe.py` and
`captures.capture_direct`. Never 8402/8403/8405.
"""
from __future__ import annotations
import base64, io, json, sys
from pathlib import Path
REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path: sys.path.insert(0, str(REPO))
from tests.face.hud_v2_layout_check import _boot, _pane, _set_ws, _set_storage, _approval
from tests.face.hud_v2_mock_browser import state

# Count every share request and keep every track, while delegating to Chromium.
WRAP = """(() => {
  const md = navigator.mediaDevices; if (!md || !md.getDisplayMedia) return;
  const real = md.getDisplayMedia.bind(md);
  window.__dc = { calls: 0, tracks: [] };
  md.getDisplayMedia = async (o) => { window.__dc.calls++; const s = await real(o);
    const f = document.querySelector('[data-testid="pane-2"] [data-testid="browser-frame"]');
    window.__dc.rect = f && JSON.parse(JSON.stringify(f.getBoundingClientRect()));
    for (const t of s.getTracks()) window.__dc.tracks.push(t); return s; };
})();"""
ENDED = "window.__dc.tracks.length>0 && window.__dc.tracks.every(t=>t.readyState==='ended')"
STANDALONE = """() => { const real = window.matchMedia.bind(window);
  window.matchMedia = q => q === '(display-mode: standalone)' ? {matches: true, media: q} : real(q); }"""


def direct_capture_checks(pw, mock, base, check, until, guard, init_script, dsf=1):
    """`dsf` 1.5 is the review's case (PR #276 MED-1): Chromium then delivers
    CSS-pixel frames, which the size rule must accept as a uniform scale."""
    from PIL import Image
    tag = "DC" if dsf == 1 else f"DC@{dsf}x"
    def check(name, ok, detail="", _check=check): _check(name.replace("DC", tag, 1), ok, detail)
    print(f"\nB-DIRECT-CAPTURE Direct capture at device scale {dsf}: owner switch, probe and one cropped frame (real Chromium, not M6)")
    try:
        browser = pw.chromium.launch(headless=True, channel="chromium", args=["--auto-accept-this-tab-capture"])
    except Exception as exc:
        check("DC full Chromium launches for real tab capture", False, str(exc)[:300]); return
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000}, device_scale_factor=dsf)
    guard(ctx); ctx.add_init_script(init_script); ctx.add_init_script(WRAP)
    page = ctx.new_page(); page.set_default_timeout(6000); errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    state(mock)
    import tempfile
    report = Path(tempfile.mkdtemp(prefix="dc-probe-")) / "direct-capture-probe.json"
    mock.browser_direct_probe.attach(report)
    url = f"http://127.0.0.1:{mock.dev_port}/"
    raw = {"preset": "cols2", "focused": 2, "panes": [{"view": "chat"}, {"view": "browser",
           "browser": {"active": "pageaa", "tabs": [{"id": "pageaa", "url": url}]}}, {"view": "file"}, {"view": "task"}]}
    active = _pane(2, '[data-browser-page][aria-hidden="false"]')
    frame = page.locator(active + ' [data-testid="browser-frame"]')
    annotation = page.locator(active + ' [data-testid="browser-annotate"]')
    actions = page.locator(_pane(2, '[data-testid="browser-capture-actions"]'))
    calls = lambda: page.evaluate("window.__dc.calls")
    def capture(label):
        button = page.locator(_pane(2, '[data-testid="browser-options-toggle"]'))
        if button.get_attribute("aria-expanded") != "true": button.click()
        actions.get_by_role("button", name=label, exact=True).click()
    def boot():
        _set_storage(page, zoom="100", layout="{}"); _set_ws(page, json.dumps(raw)); _boot(page, mock, None, until, reload=True)
        until(lambda: frame.count() == 1, timeout=5)
        until(lambda: any(p == "/browser/front" for _, p, _ in mock.calls), timeout=3)
        page.locator(_pane(1, '[data-testid="input"]')).click()
    def settings():
        page.keyboard.press("Control+,"); until(lambda: page.locator('[data-testid="settings-page"]').count() == 1, timeout=3)
        page.get_by_role("button", name="Browser", exact=True).click()
    def close_settings():
        page.locator('[data-testid="settings-close"]').click()
        until(lambda: page.locator('[data-testid="settings-page"]').count() == 0, timeout=3)
    try:
        _boot(page, mock, base, until)
        check("DC Chromium exposes the real capture APIs", page.evaluate(
            "!!(navigator.mediaDevices.getDisplayMedia && window.CropTarget && window.ImageCapture)"))
        boot()
        capture("Annotate")
        until(lambda: "Open this in Remote to annotate." in page.locator(active).inner_text(), timeout=4)
        check("DC off by default: the documented fallback, no share prompt", calls() == 0 and annotation.count() == 0)
        settings()
        box = page.locator('[data-testid="browser-direct-capture"]'); probe = page.locator('[data-testid="browser-direct-probe"]')
        until(lambda: box.count() == 1, timeout=3)
        check("DC switch is off and locked before a passing probe", not box.is_checked() and box.is_disabled()
              and "Not tested since Jarvis started" in page.locator('[data-testid="direct-probe-status"]').inner_text())
        probe.evaluate("e=>e.click()")
        page.wait_for_timeout(200)
        check("DC a scripted probe press does nothing", calls() == 0 and page.locator('[data-testid="direct-probe-note"]').count() == 0)
        probe.click()
        until(lambda: page.locator('[data-testid="direct-probe-note"]').count() == 1, timeout=3)
        check("DC the probe refuses outside the installed app, with no prompt", calls() == 0
              and "installed Jarvis app" in page.locator('[data-testid="direct-probe-note"]').inner_text()
              and mock.browser_direct_probe.status() is None)
        page.evaluate(STANDALONE)
        probe.click()
        until(lambda: mock.browser_direct_probe.status() is not None, timeout=8)
        until(lambda: "Passed" in page.locator('[data-testid="direct-probe-note"]').inner_text()
              or "Failed" in page.locator('[data-testid="direct-probe-note"]').inner_text(), timeout=4)
        status = mock.browser_direct_probe.status() or {}
        note = page.locator('[data-testid="direct-probe-setting"], [data-testid="direct-capture-setting"]').inner_text()
        pattern_ok = report.exists() and json.loads(report.read_text())["facts"]["rect"]["width"] > 320
        check("DC the probe's pattern is as wide as Settings allows", pattern_ok)
        check("DC the real Chromium probe of the HUD's test pattern passes the daemon's own verdict", status.get("passed") is True,
              f"{status} calls={calls()} facts={json.dumps(json.loads(report.read_text())['facts']) if report.exists() else None}".replace("\n", " "))
        check("DC the probe asked once and stopped every track", calls() == 1 and page.evaluate(ENDED))
        check("DC the probe's pattern frame is gone afterwards", page.locator('[data-testid="direct-probe-pattern"]').count() == 0)
        until(lambda: box.is_enabled(), timeout=3)
        box.evaluate("e=>e.click()"); page.wait_for_timeout(200)
        check("DC a scripted switch click changes nothing", not box.is_checked() and not mock.browser_settings.value("direct_capture"))
        box.click(); until(lambda: box.is_checked(), timeout=3)
        check("DC the owner's click turns Direct capture on", mock.browser_settings.value("direct_capture") is True)
        close_settings()

        # One real cropped frame of the Direct iframe, frozen for annotation.
        before = calls(); focus_before = page.evaluate("document.activeElement && document.activeElement.tagName")
        capture("Annotate")
        until(lambda: annotation.count() == 1, timeout=8)
        rect = page.evaluate("window.__dc.rect")
        check("DC a real cropped Direct frame is frozen for annotation", annotation.count() == 1,
              (page.locator(active).inner_text()[:300] + f" rect={rect}").replace("\n", " "))
        check("DC Annotate on Direct asks Chrome once and stops every track at once", calls() == before + 1 and page.evaluate(ENDED))
        check("DC the reference was asked for only after the frame", getattr(mock, "direct_capture_requests", 0) == 1)
        check("DC no frame took the keyboard", page.locator(active + ' [data-testid="browser-stolen"]').count() == 0
              and page.evaluate("document.activeElement && document.activeElement.tagName") != "IFRAME", str(focus_before))
        annotation.locator('[data-testid="capture-send"]').click(); until(lambda: annotation.count() == 0, timeout=4)
        files = page.evaluate("window.__hud.state().chats[1].files")
        check("DC Send stages the canonical pair", [f["name"] for f in files] == ["page.png", "page text"], str([f["name"] for f in files]))
        image = Image.open(io.BytesIO(base64.b64decode(files[0]["data_b64"])))
        scale = image.width / rect["width"]
        check("DC the staged picture is the frame's own shape at a scale from 1 to the DPR",
              1 - 0.01 <= scale <= dsf + 0.01 and abs(image.height - rect["height"] * scale) <= 2, f"{image.size} vs {rect}")
        colours = {image.getpixel((x, y)) for x in range(0, image.width, 37) for y in range(0, image.height, 23)}
        check("DC the staged picture is not blank", len(colours) > 1)
        check("DC staging sent no chat turn", not any(p.endswith("/send") for _, p, _ in mock.calls))
        page.evaluate("window.__hud.dispatch({type:'chat',pane:1,patch:{files:[]}})")

        # Something painted over the frame: refused before any prompt.
        page.evaluate("""() => { const d = document.createElement('div'); d.id = 'dc-overlay'; d.setAttribute('role','tooltip');
          const r = document.querySelector('[data-testid="pane-2"] [data-testid="browser-frame"]').getBoundingClientRect();
          Object.assign(d.style, {position:'fixed', left: (r.left+20)+'px', top: (r.top+20)+'px', width:'80px', height:'30px',
            background:'#fff', pointerEvents:'none', zIndex: 99}); document.body.append(d); }""")
        before = calls(); capture("Annotate")
        until(lambda: "Something covers this page" in page.locator(active).inner_text(), timeout=4)
        check("DC an overlay over the frame refuses with no prompt and offers Remote", calls() == before
              and page.locator(active).get_by_role("button", name="Open in Remote", exact=True).count() == 1)
        page.evaluate("document.getElementById('dc-overlay').remove()")

        # An approval card: the controls are inert and nothing prompts.
        _approval(mock, "direct-card"); until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=3)
        before = calls()
        check("DC a pending card disables Direct capture", page.evaluate("""() => [...document.querySelectorAll(
          '[data-testid="pane-2"] [data-testid="browser-capture-actions"] button')].every(b => b.disabled || b.closest('[inert]'))""")
              and page.locator(_pane(2, '[data-testid="browser-options-toggle"]')).is_disabled())
        check("DC a pending card prompts nothing", calls() == before)
        page.keyboard.press("Escape"); until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=3)

        # Off again: the fallback returns, with no prompt.
        settings(); box.click(); until(lambda: not box.is_checked(), timeout=3); close_settings()
        before = calls(); capture("Annotate")
        until(lambda: "Open this in Remote to annotate." in page.locator(active).inner_text(), timeout=4)
        check("DC turning the switch off restores the fallback", calls() == before and not mock.browser_settings.value("direct_capture"))
        check("DC no page errors", not errors, str(errors[:3]))
        check("DC no live control listener request", not mock.api_hits, str(mock.api_hits[:3]))
    except Exception:
        try: print("Direct capture failure state:", page.locator(_pane(2)).inner_text()[:1200])
        except Exception: pass
        raise
    finally:
        ctx.close(); browser.close()


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    from tests.face.hud_v2_mock import MockDaemon
    with sync_playwright() as pw:
        for dsf in (1, 1.5):
            mock = MockDaemon(0).start()
            try: direct_capture_checks(pw, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until, v2.guard, v2.FAKE_RECOGNIZER, dsf)
            except Exception as exc: v2.check(f"DC@{dsf}x scenario completes", False, str(exc)[:500])
            finally: mock.stop()
    print(f"{v2.CHECKS} direct capture checks, {len(v2.FAILURES)} failures")
    return bool(v2.FAILURES)


if __name__ == "__main__": sys.exit(main())
