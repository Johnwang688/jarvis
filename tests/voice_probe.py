"""Probe STT models on OpenRouter with the exact audio format the face sends.

Makes real API calls (pennies). Pipeline:
  1. voice.tts() speaks a known sentence (mp3)
  2. headless Chromium plays it through captureStream -> MediaRecorder,
     producing genuine webm/opus — byte-identical in kind to what the
     hold-to-talk page records from the mic
  3. each candidate model transcribes the webm; scored by similarity to the
     source text, with latency

Run:  .venv/bin/python tests/voice_probe.py
"""

from __future__ import annotations

import base64
import difflib
import re
import sys
import time

from playwright.sync_api import sync_playwright

from jarvis import llm, voice

SOURCE = (
    "Good evening. All systems are online and running within normal parameters. "
    "Shall I begin the diagnostics?"
)

CANDIDATES = [
    "mistralai/voxtral-mini-transcribe",
    "x-ai/grok-stt-1.0",
    "nvidia/parakeet-tdt-0.6b-v3",
    "microsoft/mai-transcribe-1.5",
]

RECORD_JS = """async (b64) => {
    const bin = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
    const ctx = new AudioContext();
    const buf = await ctx.decodeAudioData(bin.buffer.slice(0));
    const dest = ctx.createMediaStreamDestination();
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(dest);

    const rec = new MediaRecorder(dest.stream, { mimeType: "audio/webm;codecs=opus" });
    const chunks = [];
    rec.ondataavailable = (e) => chunks.push(e.data);
    const done = new Promise((res) => { rec.onstop = res; });
    rec.start();
    src.start();
    await new Promise((res) => { src.onended = res; });
    await new Promise((res) => setTimeout(res, 300));
    rec.stop();
    await done;
    await ctx.close();

    const out = new Blob(chunks, { type: "audio/webm" });
    const bytes = new Uint8Array(await out.arrayBuffer());
    let s = "";
    for (const b of bytes) s += String.fromCharCode(b);
    return btoa(s);
}"""


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def main() -> int:
    print("synthesizing source sentence…")
    mp3 = voice.tts(SOURCE, speed=1.2)

    print("re-recording as webm/opus in Chromium…")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--autoplay-policy=no-user-gesture-required"]
        )
        page = browser.new_page()
        webm_b64 = page.evaluate(RECORD_JS, base64.b64encode(mp3).decode())
        browser.close()
    webm = base64.b64decode(webm_b64)
    print(f"webm sample: {len(webm):,} bytes\n")

    results = []
    for model in CANDIDATES:
        started = time.monotonic()
        try:
            text = llm.transcribe(webm, model=model, max_retries=1)
            elapsed = time.monotonic() - started
            score = difflib.SequenceMatcher(None, norm(SOURCE), norm(text)).ratio()
            results.append((model, score, elapsed, text))
            print(f"{model:40} {score:5.1%}  {elapsed:5.2f}s  {text[:70]!r}")
        except Exception as exc:
            results.append((model, 0.0, 0.0, f"FAIL {exc}"))
            print(f"{model:40} FAIL   {str(exc)[:90]}")

    good = [r for r in results if r[1] > 0]
    if good:
        best = max(good, key=lambda r: (r[1], -r[2]))
        print(f"\nbest: {best[0]}  ({best[1]:.1%} match, {best[2]:.2f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
