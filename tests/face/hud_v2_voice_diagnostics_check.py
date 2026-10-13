"""V1 diagnostics through real HUD capture and fetch, isolated fake microphone/STT.

Run with temporary HOME/config, dummy keys, worktree PYTHONPATH and a 3G/0swap
systemd scope. No real audio, external calls, credentials or live daemon ports.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
# A fresh worktree contains no credentials; fail instead of importing config if it does.
assert not (REPO / ".env").exists(), "use a credential-free worktree"
from playwright.sync_api import sync_playwright
from tests.face.hud_v2_mock import MockDaemon
from tests.face.hud_v2_check import FAKE_RECOGNIZER, guard_live, watch_csp, until, open_settings

FAKE_MIC = """
window.__gumCalls = [];
Object.defineProperty(navigator.mediaDevices, 'getUserMedia', { value: async constraints => {
  window.__gumCalls.push(constraints);
  if (window.__denyMic) throw new DOMException('private permission message', 'NotAllowedError');
  const context = new AudioContext(); const dest = context.createMediaStreamDestination();
  dest.stream.getAudioTracks()[0].getSettings = () => ({echoCancellation: true,
    noiseSuppression: false, autoGainControl: [true], deviceId: 'private-device'});
  return dest.stream;
} });
"""

def main():
    failures = []; count = 0
    def check(name, ok):
        nonlocal count
        count += 1; print(("ok   " if ok else "FAIL ") + name)
        if not ok: failures.append(name)
    mock = MockDaemon(0).start()
    try:
        base = f"http://127.0.0.1:{mock.port}"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            def route(r):
                url = urlsplit(r.request.url)
                if url.scheme in ("http", "https") and (url.hostname != "127.0.0.1" or url.port not in {mock.port, mock.workshop_port, mock.api_port, mock.dev_port}):
                    failures.append("request outside isolated mock"); r.abort()
                else: r.continue_()
            context.route("**/*", route)
            guard_live(context, failures.append); watch_csp(context, failures.append)
            context.add_init_script(FAKE_RECOGNIZER + FAKE_MIC)
            page = context.new_page(); page.on("pageerror", lambda _: failures.append("page error"))
            page.goto(base); page.wait_for_selector('[data-testid="sidebar"]')
            page.wait_for_function("() => window.__hud && window.__hud.voiceDiagnostics().mic === 'available'")
            check("explicit constraints requested", page.evaluate("JSON.stringify(window.__gumCalls[0])") == json.dumps({"audio": {"echoCancellation": True, "noiseSuppression": True, "autoGainControl": True}}, separators=(",", ":")))
            check("actual readback is allowlisted", page.evaluate("window.__hud.voiceDiagnostics().settings") == {"echoCancellation": True, "noiseSuppression": False, "autoGainControl": "unknown"})
            # One real Capture PTT upload into mocked fetch; REVIEW stays unsent.
            page.evaluate("window.__hud.mic.feedMs(2000, .0009); window.__hud.capture.press(); window.__hud.mic.feedMs(1200, .06); window.__hud.capture.release()")
            page.wait_for_function("() => window.__hud.voiceDiagnostics().rows.some(r => r.outcome === 'review')")
            check("REVIEW transcript placement logged once", page.evaluate("window.__hud.voiceDiagnostics().rows.filter(r => r.outcome === 'review').length") == 1)
            check("review did not send", not any(path.endswith('/send') for method, path, body in mock.calls))
            open_settings(page, "Voice")
            check("Settings shows real diagnostic rows", page.locator('[data-testid="voice-diagnostic-row"]').count() == 1)
            check("readback differs from requested settings", "Noise suppression: off" in page.locator('[data-testid="voice-mic-settings"]').inner_text())
            check("private fields never reach diagnostics", "private" not in page.locator('[data-testid="voice-diagnostics"]').inner_text().lower())
            # While Settings covers capture, a speech segment produces one reason, no upload.
            before = len(mock.sent('POST', '/stt'))
            page.evaluate("window.__hud.mic.feedMs(2000, .0009); window.__hud.mic.feedMs(1200, .06); window.__hud.mic.feedMs(1800, .0009)")
            check("suppressed utterance reports settings", page.evaluate("window.__hud.voiceDiagnostics().rows.at(-1).outcome") == "suppressed:settings")
            check("settings speech never uploaded", len(mock.sent('POST', '/stt')) == before)
            page.screenshot(path=os.environ.get('VOICE_DIAGNOSTICS_SCREENSHOT', '/tmp/voice-v1-diagnostics.png'))
            page.locator('[data-testid="settings-close"]').click()
            page.route('**/stt*', lambda r: r.fulfill(status=502, content_type='application/json', body='{"error":"private server error"}'))
            page.evaluate("window.__hud.capture.press(); window.__hud.mic.feedMs(1200, .06); window.__hud.capture.release()")
            page.wait_for_function("() => window.__hud.voiceDiagnostics().rows.at(-1).outcome === 'stt_failed'")
            check("failure recorded without exception text", "private" not in page.evaluate("JSON.stringify(window.__hud.voiceDiagnostics())"))
            page.unroute('**/stt*')
            # A failed chat send stays a handoff outcome, never falsely "sent".
            page.route('**/threads/*/send', lambda r: r.fulfill(status=503, content_type='application/json', body='{"error":"fake send unavailable"}'))
            page.locator('[data-testid="dictation-auto"]').click()
            page.evaluate("window.__hud.capture.press(); window.__hud.mic.feedMs(1200, .06); window.__hud.capture.release()")
            page.wait_for_function("() => window.__hud.voiceDiagnostics().rows.at(-1).outcome === 'handed_to_chat'")
            check("failed send never reported as sent", not any(r['outcome'] == 'sent' for r in page.evaluate('window.__hud.voiceDiagnostics().rows')))
            page.wait_for_function("() => window.__hud.state().error.includes('fake send unavailable')")
            page.unroute('**/threads/*/send')
            page.locator('[data-testid="dictation-off"]').click()
            before = len(mock.sent('POST', '/stt'))
            page.evaluate("window.__hud.mic.feedMs(2000, .0009); window.__hud.mic.feedMs(1200, .06); window.__hud.mic.feedMs(1800, .0009)")
            check("OFF speech logged as muted", page.evaluate("window.__hud.voiceDiagnostics().rows.at(-1).outcome") == 'muted')
            check("OFF uploaded nothing", len(mock.sent('POST', '/stt')) == before)
            open_settings(page, 'Voice')
            check("muted diagnostics say muted and show level zero", 'MIC MUTED' in page.locator('[data-testid="voice-mic-capability"]').inner_text() and 'Level 0.0000' in page.locator('[data-testid="voice-live-level"]').inner_text())
            page.reload(); page.wait_for_function("() => window.__hud && window.__hud.voiceDiagnostics")
            check("reload clears memory metadata", page.evaluate("window.__hud.voiceDiagnostics().rows.length") == 0)
            context.close()
            # Permission rejection must stay fixed, and never retry access.
            denied = browser.new_context(); denied.route("**/*", route); guard_live(denied, failures.append); watch_csp(denied, failures.append)
            denied.add_init_script("window.__denyMic = true;" + FAKE_RECOGNIZER + FAKE_MIC)
            p = denied.new_page(); p.goto(base); p.wait_for_function("() => window.__hud && window.__hud.voiceDiagnostics().mic === 'unavailable'")
            open_settings(p, 'Voice')
            check("permission failure shown with fixed reason", 'unavailable' in p.locator('[data-testid="voice-mic-capability"]').inner_text() and 'private' not in p.locator('[data-testid="voice-diagnostics"]').inner_text())
            check("permission denial was not retried", p.evaluate('window.__gumCalls.length') == 1)
            denied.close(); browser.close()
        print(f"{count - len(failures)}/{count} checks passed; {len(failures)} failures")
        if failures: print('\n'.join(failures))
        return 1 if failures else 0
    finally: mock.stop()
if __name__ == '__main__': raise SystemExit(main())
