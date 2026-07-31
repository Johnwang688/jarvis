"""End-to-end check of the HUD's own conversation JavaScript.

Regression guard for the "Fault: HTTP 200" bug (2026-07-31): the page's error
check matched Content-Type against "json", which also matches the successful
application/x-ndjson stream — so every reply rendered as a fault. The server-
side streaming test could never catch that class of bug; this drives the real
jarvis.html in headless Chromium instead.

Makes real API calls (one TTS + one STT + one agent turn ≈ $0.001).

Run:  .venv/bin/python tests/face/hud_converse_check.py
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from voice_probe import RECORD_JS

from jarvis import voice
from jarvis.face import server

PORT = 8427


def main() -> int:
    mp3 = voice.tts("What time is it?", speed=1.2)
    srv = server.create_server(port=PORT)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--use-fake-ui-for-media-stream",
                    "--use-fake-device-for-media-stream",
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )
            page = browser.new_page()
            webm_b64 = page.evaluate(RECORD_JS, base64.b64encode(mp3).decode())
            page.goto(f"http://localhost:{PORT}/jarvis.html")
            page.wait_for_timeout(1200)
            page.evaluate(
                """(b64) => {
                    const bin = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
                    converse(new Blob([bin], { type: "audio/webm" }));
                }""",
                webm_b64,
            )
            page.wait_for_function(
                "document.querySelectorAll('#transcript .msg').length >= 2", timeout=60_000
            )
            msgs = page.eval_on_selector_all(
                "#transcript .msg", "els => els.map(e => e.textContent)"
            )
            assert not any("Fault" in m for m in msgs), f"HUD rendered a fault: {msgs}"
            page.wait_for_function(
                "S.state === 'speaking' || S.state === 'idle'", timeout=30_000
            )
            print("ok  HUD converse: transcript rendered, audio scheduled, no fault")
            browser.close()
    finally:
        srv.shutdown()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
