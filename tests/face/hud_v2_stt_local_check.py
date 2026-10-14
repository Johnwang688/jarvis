"""V13 through the real HUD: Settings -> Voice shows where speech-to-text runs.

One focused check: the backend choice and the cloud-fallback switch (read from and
written to the daemon, never kept in the window), the sentence that says what happens
to the next clip, the diagnostics row naming the backend that served each clip, and a
refusal from the daemon (the owner's own switch) shown as a failure that is not retried.
Isolated fake microphone and a scripted mock daemon; no real audio, model, call or
live port.

Run with temporary HOME/config, dummy keys, worktree PYTHONPATH, a 3G/0swap systemd
scope and HUD_V2_CHECK_PORT=18475 (the mock itself takes an ephemeral port).
"""
from __future__ import annotations
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
assert not (REPO / ".env").exists(), "use a credential-free worktree"
from playwright.sync_api import sync_playwright
from tests.face.hud_v2_mock import MockDaemon
from tests.face.hud_v2_check import FAKE_RECOGNIZER, guard_live, watch_csp, open_settings
from tests.face.hud_v2_voice_diagnostics_check import FAKE_MIC

PTT = "window.__hud.capture.press(); window.__hud.mic.feedMs(1200, .06); window.__hud.capture.release()"


def main():
    failures: list[str] = []
    count = 0

    def check(name, ok):
        nonlocal count
        count += 1
        print(("ok   " if ok else "FAIL ") + name)
        if not ok:
            failures.append(name)

    mock = MockDaemon(0).start()
    try:
        base = f"http://127.0.0.1:{mock.port}"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 900})

            def route(r):
                url = urlsplit(r.request.url)
                if url.scheme in ("http", "https") and (url.hostname != "127.0.0.1" or url.port not in {
                        mock.port, mock.workshop_port, mock.api_port, mock.dev_port}):
                    failures.append("request outside isolated mock")
                    r.abort()
                else:
                    r.continue_()
            context.route("**/*", route)
            guard_live(context, failures.append)
            watch_csp(context, failures.append)
            # Count every start of Chrome's wake recognizer (it streams the microphone to Google).
            COUNT_STARTS = """(() => { const P = window.SpeechRecognition.prototype, s = P.start;
              P.start = function () { window.__srStarts = (window.__srStarts || 0) + 1; return s.call(this); }; })();"""
            context.add_init_script(FAKE_RECOGNIZER + COUNT_STARTS + FAKE_MIC)
            page = context.new_page()
            page.on("pageerror", lambda e: failures.append(f"page error {e}"))
            page.goto(base)
            page.wait_for_selector('[data-testid="sidebar"]')
            page.wait_for_function("() => window.__hud && window.__hud.voiceDiagnostics().mic === 'available'")
            page.evaluate("window.__hud.mic.feedMs(2000, .0009)")

            page.wait_for_function("() => !!window.__hudRecog", timeout=10000)
            check("with cloud fallback on the wake recognizer runs (as before V13)",
                  page.evaluate("window.__srStarts || 0") >= 1)

            def stts():
                return mock.sent("POST", "/stt")

            def summary():
                return page.locator('[data-testid="stt-summary"]').inner_text()

            # --- dictation first (Settings suppresses capture): three clips, three backends
            mock.stt_script = [{"text": "heard on cloud", "backend": "cloud", "model": "nvidia/parakeet-tdt-0.6b-v3"},
                               {"text": "heard here", "backend": "local", "model": "parakeet (local)"},
                               {"text": "cloud after a local failure", "backend": "cloud", "model": "x-ai/grok-stt-1.0",
                                "fallback": "local speech-to-text ran out of time"}]
            for i in range(3):
                page.evaluate("window.__hud.mic.feedMs(1500, .0009)")
                page.evaluate(PTT)
                page.wait_for_function("(n) => window.__hud.voiceDiagnostics().rows.filter(r => r.backend).length >= n",
                                       arg=i + 1, timeout=15000)
            rows = page.evaluate("window.__hud.voiceDiagnostics().rows")
            check("each clip's row names the backend that served it",
                  [r.get("backend") for r in rows] == ["cloud", "local", "cloud"])
            check("and whether the cloud answered because local failed", [r.get("fellBack") for r in rows] == [False, False, True])
            check("the daemon's reason text is not kept in the window", "ran out of time" not in str(rows))

            # --- Settings -> Voice
            open_settings(page, "Voice")
            page.wait_for_selector('[data-testid="stt-settings"]')
            texts = page.locator('[data-testid="voice-diagnostic-row"]').all_inner_texts()
            # Newest first.
            check("diagnostics shows cloud (local failed) / local / cloud",
                  len(texts) == 3 and "cloud (local failed)" in texts[0] and " · local" in texts[1]
                  and " · cloud" in texts[2] and "local failed" not in texts[2])
            check("the daemon's settings are shown: Cloud chosen, fallback on",
                  page.locator('[data-testid="stt-backend-cloud"]').is_checked()
                  and page.locator('[data-testid="stt-cloud-fallback"]').is_checked())
            check("and said in one sentence", "sent to the cloud" in summary())
            check("the local engine's state is shown", "installed" in page.locator('[data-testid="stt-local-status"]').inner_text())

            page.locator('[data-testid="stt-backend-local"]').click()
            page.wait_for_function("() => document.querySelector('[data-testid=\"stt-summary\"]').innerText.includes('on this computer')")
            check("choosing Local writes just that to the daemon", mock.sent("POST", "/stt/settings")[-1] == {"backend": "local"}
                  and mock.stt_settings["backend"] == "local")
            check("Local is selected from the daemon's answer", page.locator('[data-testid="stt-backend-local"]').is_checked())
            check("with the fallback on it says the cloud is the safety net", "sent to the cloud" in summary())

            page.locator('[data-testid="stt-cloud-fallback"]').click()
            page.wait_for_function("() => document.querySelector('[data-testid=\"stt-summary\"]').innerText.includes('never leaves this computer')")
            check("switching cloud fallback off writes it", mock.stt_settings["cloud_fallback"] is False)
            check("and says dictation never leaves this computer", "Dictation audio never leaves this computer" in summary()
                  and "sent to the cloud" not in summary())
            page.wait_for_function("() => !window.__hudRecog")
            starts = page.evaluate("window.__srStarts || 0")
            page.wait_for_timeout(800)
            check("fallback OFF stops Chrome's wake recognizer and does not restart it",
                  page.evaluate("window.__srStarts || 0") == starts and page.evaluate("!window.__hudRecog"))
            check("and Settings says plainly that wake by voice is off",
                  page.locator('[data-testid="stt-wake-note"]').inner_text()
                  == "Wake word by voice is off until local wake detection lands; use the talk key.")
            check("the switch is unchecked", not page.locator('[data-testid="stt-cloud-fallback"]').is_checked())

            # A daemon that says the engine is missing: the sentence follows.
            mock.stt_settings["local"].update(installed=False, reason="the speech model is not downloaded")
            page.locator('[data-testid="stt-backend-auto"]').click()
            page.wait_for_function("() => document.querySelector('[data-testid=\"stt-summary\"]').innerText.includes('will fail')")
            check("not installed + fallback off warns that dictation will fail, still uploading nothing",
                  "Nothing is uploaded" in summary() and "not installed" in page.locator('[data-testid="stt-local-status"]').inner_text())

            # A reload shows the daemon's values, not a window-local copy, and with the fallback
            # off the recognizer never starts at all in the new page.
            page.reload()
            page.wait_for_selector('[data-testid="sidebar"]')
            open_settings(page, "Voice")
            page.wait_for_selector('[data-testid="stt-settings"]')
            page.wait_for_function("() => document.querySelector('[data-testid=\"stt-backend-auto\"]').checked")
            page.wait_for_timeout(1000)
            check("after a reload with the fallback off, Chrome's recognizer never starts",
                  page.evaluate("window.__srStarts || 0") == 0 and page.evaluate("!window.__hudRecog"))
            check("after a reload the daemon's values are shown", page.locator('[data-testid="stt-backend-auto"]').is_checked()
                  and not page.locator('[data-testid="stt-cloud-fallback"]').is_checked())

            page.screenshot(path=os.environ.get("STT_LOCAL_SCREENSHOT", "/tmp/voice-v13-stt-local.png"))

            page.locator('[data-testid="stt-cloud-fallback"]').click()
            page.wait_for_function("() => !!window.__hudRecog", timeout=10000)
            check("switching the fallback back on lets the recognizer start again",
                  page.evaluate("window.__srStarts || 0") >= 1
                  and "streams the microphone to Google" in page.locator('[data-testid="stt-wake-note"]').inner_text())
            page.locator('[data-testid="stt-cloud-fallback"]').click()      # off again for what follows
            page.wait_for_function("() => !window.__hudRecog")

            # --- the owner's switch refusing the upload: a visible failure, not retried
            page.locator('[data-testid="settings-close"]').click() if page.locator('[data-testid="settings-close"]').count() else page.keyboard.press("Escape")
            page.wait_for_selector('[data-testid="settings-page"]', state="detached")
            page.wait_for_function("() => window.__hud && window.__hud.voiceDiagnostics().mic === 'available'")
            before = len(stts())
            mock.stt_script = [{"status": 409, "error": "cloud speech-to-text is switched off in Settings > Voice (choose Local, or switch cloud fallback on)"}]
            page.evaluate("window.__hud.mic.feedMs(1500, .0009)")
            page.evaluate(PTT)
            page.wait_for_selector('[data-testid="stt-retry"]', timeout=15000)
            page.wait_for_timeout(2600)       # past the 2 s automatic-retry delay
            check("a 409 is shown and is not retried automatically", len(stts()) == before + 1)
            check("the daemon's sentence reaches the window", "switched off" in page.evaluate("window.__hud.state().error"))
            context.close()
            browser.close()
        print(f"{count - len(failures)}/{count} checks passed; {len(failures)} failures")
        if failures:
            print("\n".join(failures))
        return 1 if failures else 0
    finally:
        mock.stop()


if __name__ == "__main__":
    raise SystemExit(main())
