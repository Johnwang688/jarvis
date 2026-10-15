"""V14 noise suppression through the real HUD: RNNoise (WebAssembly) in the VAD
worker, behind the Settings -> Voice switch, off by default (plan 3.4, Q16).

What it proves, in a real Chromium against the mock daemon (which serves hud/dist
under the daemon's own CSP):
  - off_by_default: a fresh window has the switch off, the stage reports "off", and
    NOTHING is requested, fetched or compiled for RNNoise, however long it listens;
  - toggle_live: switching it on loads the pinned binary from the HUD's own origin
    (served as application/wasm under the page's CSP, never `unsafe-eval`; the worker
    that compiles it runs under its own script's `wasm-unsafe-eval`, owner 2026-10-15),
    without a reload; Settings and the diagnostics say so; switching it off and on again
    works; the choice survives a reload (and "off" survives one without loading anything);
    Silero keeps scoring the whole time;
  - false_accepts: on a fixture of synthetic Kokoro speech in steady pink noise, the
    VAD calls fewer silent frames speech with noise suppression on than off, and keeps
    nearly all of the speech (this scores frames through a second, separate stage in the
    page, so the same worker and WebAssembly as the microphone path run, with no microphone);
  - recorded_raw: with it on and "ready", what the capture records (the ring) is the raw
    microphone signal (same level as with it off, steady noise included, which RNNoise
    would remove), and an uploaded clip is a byte-for-byte slice of that ring;
  - fallbacks: a missing binary, one changed byte, and a truncated one each turn noise
    suppression off VISIBLY (a sentence from a closed set, no URL or path), with
    Silero still the detector and still scoring, and "Try noise suppression again" brings it
    back once the cause is gone.

Everything is isolated (the mock daemon on an ephemeral or HUD_V2_CHECK_PORT port, scripted `/stt`,
synthetic audio generated here or the Kokoro clips in tests/face/fixtures/voice_v5; no real
voice, no external request, no live daemon port). Run alone, optionally for some sections:
    python tests/face/hud_v2_denoise_check.py [section,section]
under a temporary HOME, dummy keys, the worktree PYTHONPATH and a 3G/0swap scope. Needs hud/dist built;
sections: off_by_default, toggle_live, false_accepts, recorded_raw, fallbacks.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import struct
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
assert not (REPO / ".env").exists(), "use a credential-free worktree"
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.face.hud_v2_check import FAKE_RECOGNIZER, guard_live, open_settings, watch_csp  # noqa: E402
from tests.face.hud_v2_mock import MockDaemon  # noqa: E402
from tests.face.hud_v2_vad_check import (  # noqa: E402
    BASE_ARGS, CLAIM_FIRST, FIXTURES, RATE, read_wav, scenario,
)

# A microphone that is steady, band-limited noise made in the page (no Chrome echo-cancel/noise-suppress
# on this path, unlike the fake capture device, whose processing already removes most of a noise file):
# what reaches the capture is exactly this, so a denoised recording would be visibly quieter.
STEADY_MIC = """
(() => {
  const ctx = new AudioContext(); const dest = ctx.createMediaStreamDestination();
  const n = ctx.sampleRate * 2; const buf = ctx.createBuffer(1, n, ctx.sampleRate); const d = buf.getChannelData(0);
  let a = 12345; for (let i = 0; i < n; i++) { a = (Math.imul(a, 1664525) + 1013904223) | 0; d[i] = ((a >>> 8) / 8388608 - 1) * 0.35; }
  const src = ctx.createBufferSource(); src.buffer = buf; src.loop = true;
  const f1 = ctx.createBiquadFilter(); f1.type = 'lowpass'; f1.frequency.value = 3500;
  const f2 = ctx.createBiquadFilter(); f2.type = 'lowpass'; f2.frequency.value = 3500;
  src.connect(f1); f1.connect(f2); f2.connect(dest); src.start();
  Object.defineProperty(navigator.mediaDevices, 'getUserMedia', { value: async () => dest.stream });
})();
"""
PORT = int(os.environ.get("HUD_V2_CHECK_PORT", "0"))
KEY = "jarvis.hud.voice.noiseSuppression"
WASM_RE = re.compile(r"/assets/rnnoise_0\.2.*\.wasm$")
PINNED_SHA = "4f513a50613de74378331237886138eab52fa2650e8b1a41eb587d932d9b8850"
PINNED_BYTES = 1440118
WASM = REPO / "hud" / "vendor" / "rnnoise" / "rnnoise_0.2.wasm"


def pink(n: int, seed: int) -> list[float]:
    """Voss-McCartney pink noise, values within about [-1, 1]."""
    rnd = random.Random(seed)
    rows, out = [0.0] * 12, []
    for i in range(n):
        k = (i & -i).bit_length() - 1 if i else 0
        if k < len(rows):
            rows[k] = rnd.uniform(-1, 1)
        out.append(sum(rows) / len(rows))
    return out


def speech_in_pink(snr_db: float) -> tuple[list[int], list[bool], list[bool]]:
    """Kokoro speech (a, b, a, b, a, b) with 0.8 s gaps, in steady pink noise `snr_db` below the speech.

    Returns the samples, a per-512-sample-frame mask of speech frames, and the same mask dilated by
    three frames either side. A frame the VAD calls speech outside the dilated mask is a false accept
    (V0's definition: tests/voice_v0, 'frame energy of the clean signal above -50 dBFS, dilated by 3 frames').
    """
    a, b = read_wav(FIXTURES / "kokoro_a.wav"), read_wav(FIXTURES / "kokoro_b.wav")
    gap = [0] * int(0.8 * RATE)
    clean = list(gap)
    for clip in (a, b, a, b, a, b):
        clean += clip + gap
    active = [x for x in clean if abs(x) > 330]
    speech_rms = (sum(x * x for x in active) / len(active)) ** 0.5
    z = pink(len(clean), 3)
    z_rms = (sum(v * v for v in z) / len(z)) ** 0.5
    scale = speech_rms / (10 ** (snr_db / 20)) / z_rms
    mixed = [max(-32768, min(32767, int(c + v * scale))) for c, v in zip(clean, z)]
    frames = len(mixed) // 512
    thresh = 32768 * 10 ** (-50 / 20)
    gt = [(sum(x * x for x in clean[i * 512:(i + 1) * 512]) / 512) ** 0.5 > thresh for i in range(frames)]
    dil = gt[:]
    for k in range(1, 4):
        for i, g in enumerate(gt):
            if g:
                if i - k >= 0:
                    dil[i - k] = True
                if i + k < frames:
                    dil[i + k] = True
    return mixed[:frames * 512], gt, dil


def main(argv=None):
    only = set(filter(None, ((argv if argv is not None else sys.argv[1:]) or [""])[0].split(",")))
    failures: list[str] = []
    external: list[str] = []
    count = 0

    def record(msg):
        external.append(msg)
        failures.append(msg)

    def check(name, ok, detail=""):
        nonlocal count
        count += 1
        print(("ok   " if ok else "FAIL ") + name + ("" if ok or not detail else f"  [{detail}]"))
        if not ok:
            failures.append(name)

    mock = MockDaemon(PORT).start()
    tmp = Path(tempfile.mkdtemp(prefix="denoise_v14_"))
    try:
        base = f"http://127.0.0.1:{mock.port}"
        with sync_playwright() as pw:

            def launch(wav: Path | None = None):
                args = list(BASE_ARGS)
                if wav is not None:
                    args.append(f"--use-file-for-fake-audio-capture={wav}")
                return pw.chromium.launch(headless=True, args=args)

            def new_context(browser, init: str = ""):
                ctx = browser.new_context(viewport={"width": 1400, "height": 900})

                def refuse(r):
                    record(f"request outside isolated mock: {r.request.url}")
                    r.abort()
                ctx.route(re.compile(r"^https?://(?!(127\.0\.0\.1|localhost)[:/])"), refuse)
                guard_live(ctx, record)
                watch_csp(ctx, record)
                ctx.add_init_script(FAKE_RECOGNIZER + init)
                return ctx

            def open_page(ctx, wait_detector="ready"):
                page = ctx.new_page()
                page.set_default_timeout(10000)
                page.on("pageerror", lambda e: record(f"page error {e}"))
                page.goto(base)
                page.wait_for_selector('[data-testid="sidebar"]', state="attached")
                page.wait_for_function("() => window.__hud && window.__hud.voiceDiagnostics().mic === 'available'")
                page.wait_for_function("() => window.__hud.voiceDiagnostics().path !== 'unknown'")
                if wait_detector:
                    page.wait_for_function(
                        f"() => window.__hud.mic.detector().state === {json.dumps(wait_detector)}", timeout=40000)
                page.locator('[data-testid="tab-chat"]').click()
                page.locator('[data-testid="thread-t1"]').click()
                page.locator('[data-testid="dictation-review"]').click()
                return page

            def detector(page):
                return page.evaluate("window.__hud.mic.detector()")

            def noise_state(page):
                return page.evaluate("window.__hud.mic.noise()")

            def wait_noise(page, state, timeout=30000):
                page.wait_for_function(f"() => window.__hud.mic.noise().state === {json.dumps(state)}", timeout=timeout)

            def rnnoise_traffic(ctx):
                """Every request any page or worker of the context makes for the RNNoise binary."""
                seen: list[dict] = []
                ctx.on("request", lambda r: seen.append({"url": r.url}) if WASM_RE.search(r.url) else None)
                ctx.on("response", lambda r: seen.append(
                    {"url": r.url, "status": r.status, "type": r.headers.get("content-type", ""),
                     "csp": r.headers.get("content-security-policy", "")}) if WASM_RE.search(r.url) and r.status else None)
                return seen

            def run(section):
                if only and section.__name__ not in only:
                    return
                print(f"\n{section.__name__}")
                try:
                    section()
                except Exception as e:   # a section that cannot run is a failure, the rest still run
                    check(f"{section.__name__} ran to the end", False, str(e).splitlines()[0][:240])

            quiet = scenario(tmp, "quiet", ("gap", 90))

            # ---------------------------------------------------------------
            # off by default, and nothing loads
            # ---------------------------------------------------------------
            def off_by_default():
                browser = launch(quiet)
                ctx = new_context(browser)
                traffic = rnnoise_traffic(ctx)
                page = open_page(ctx)
                st = noise_state(page)
                check("a fresh window has noise suppression off", st["wanted"] is False and st["state"] == "off", str(st))
                page.wait_for_timeout(3500)
                check("Silero is live and scoring meanwhile", detector(page)["detector"] == "silero" and detector(page)["scored"] > 40)
                check("nothing was requested for RNNoise (no fetch, so nothing compiled either)", traffic == [], str(traffic))
                check("nothing is stored for the switch", page.evaluate(f"localStorage.getItem({json.dumps(KEY)})") is None)
                open_settings(page, "Voice")
                toggle = page.locator('[data-testid="noise-suppression-toggle"]')
                check("Settings -> Voice has the Noise suppression switch, unchecked", toggle.count() == 1 and not toggle.is_checked())
                shown = page.locator('[data-testid="voice-noise-suppression"]').inner_text()
                check("diagnostics say it is off", shown.strip() == "Noise suppression: off", shown)
                check("and Settings says nothing is loaded", "nothing is loaded" in page.locator('[data-testid="noise-suppression-state"]').inner_text())
                check("the diagnostics snapshot agrees", page.evaluate("window.__hud.voiceDiagnostics()").get("noiseState") == "off")
                check("no CSP violation, foreign request or page error", not external, str(external))
                ctx.close(); browser.close()

            # ---------------------------------------------------------------
            # on, off, on without a reload; persistence
            # ---------------------------------------------------------------
            def toggle_live():
                browser = launch(quiet)
                ctx = new_context(browser)
                traffic = rnnoise_traffic(ctx)
                page = open_page(ctx)
                page.evaluate("window.__marker = 'same page'")
                open_settings(page, "Voice")
                toggle = page.locator('[data-testid="noise-suppression-toggle"]')
                toggle.check()
                wait_noise(page, "ready")
                st = noise_state(page)
                check("switching it on loads it without a reload", page.evaluate("window.__marker") == "same page" and st["state"] == "ready", str(st))
                responses = [t for t in traffic if "status" in t]
                check("the binary was fetched once, from the HUD's own origin under /assets",
                      len(responses) == 1 and responses[0]["url"].startswith(base + "/assets/rnnoise_0.2"), str(responses))
                check("as application/wasm with HTTP 200", responses and responses[0]["status"] == 200 and responses[0]["type"].startswith("application/wasm"), str(responses))
                policy = responses[0]["csp"] if responses else ""
                # The binary's own response carries the page's policy; the worker that
                # compiles it runs under its script's, which adds 'wasm-unsafe-eval'
                # (owner, 2026-10-15; hud_api.wasm_script).
                from jarvis.v2 import hud_api as H
                check("under the HUD's CSP: worker-src 'self', never unsafe-eval (the grant is the worker script's)",
                      policy == H.content_security_policy() and " 'unsafe-eval'" not in policy and "worker-src 'self'" in policy, policy)
                fetched = page.evaluate("""async (u) => { const b = new Uint8Array(await (await fetch(u)).arrayBuffer());
                  const d = await crypto.subtle.digest('SHA-256', b);
                  return { n: b.length, h: Array.from(new Uint8Array(d), x => x.toString(16).padStart(2, '0')).join('') }; }""",
                                      responses[0]["url"] if responses else "")
                check("the bytes served are the pinned file (length and SHA-256)",
                      fetched["n"] == PINNED_BYTES and fetched["h"] == PINNED_SHA == hashlib.sha256(WASM.read_bytes()).hexdigest(), str(fetched))
                page.wait_for_timeout(2500)
                st = noise_state(page)
                check("it runs in real time: well inside the 32 ms frame budget", 0 < st["frameMs"] < 8, f"{st['frameMs']:.2f} ms/frame")
                print(f"     (measured here: ready in {st['loadMs']:.0f} ms, {st['frameMs']:.2f} ms per 32 ms frame, headless Chromium on a shared machine)")
                check("and became ready within seconds", 0 < st["loadMs"] < 8000, f"{st['loadMs']:.0f} ms")
                d = detector(page)
                check("Silero is still the detector, scoring", d["detector"] == "silero" and d["state"] == "ready" and d["scored"] > 40, str(d))
                check("the switch is checked and Settings says it is running",
                      toggle.is_checked() and "Running" in page.locator('[data-testid="noise-suppression-state"]').inner_text())
                shown = page.locator('[data-testid="voice-noise-suppression"]').inner_text()
                check("diagnostics name RNNoise, the cost, and that recordings stay raw",
                      "on (RNNoise)" in shown and "ms per 32 ms frame" in shown and "recordings stay raw" in shown, shown)
                check("the choice is remembered for this window", page.evaluate(f"localStorage.getItem({json.dumps(KEY)})") == "1")
                before = detector(page)["scored"]

                toggle.uncheck()
                wait_noise(page, "off")
                page.wait_for_timeout(1500)
                check("switching it off needs no reload, and Silero keeps scoring",
                      page.evaluate("window.__marker") == "same page" and detector(page)["scored"] > before and detector(page)["detector"] == "silero")
                check("diagnostics say off again",
                      page.locator('[data-testid="voice-noise-suppression"]').inner_text().strip() == "Noise suppression: off")
                check("and the choice is stored as off", page.evaluate(f"localStorage.getItem({json.dumps(KEY)})") == "0")
                toggle.check()
                wait_noise(page, "ready")
                check("switching it on again works (second load, still no reload)", page.evaluate("window.__marker") == "same page" and noise_state(page)["state"] == "ready")

                page.reload()
                page.wait_for_selector('[data-testid="sidebar"]', state="attached")
                page.wait_for_function("() => window.__hud && window.__hud.mic.detector().state === 'ready'", timeout=40000)
                wait_noise(page, "ready")
                check("after a reload it comes back on by itself (this window remembers)", noise_state(page)["wanted"] is True)
                open_settings(page, "Voice")
                page.locator('[data-testid="noise-suppression-toggle"]').uncheck()
                wait_noise(page, "off")
                n = len(traffic)
                page.reload()
                page.wait_for_selector('[data-testid="sidebar"]', state="attached")
                page.wait_for_function("() => window.__hud && window.__hud.mic.detector().state === 'ready'", timeout=40000)
                page.wait_for_timeout(2500)
                check("switched off and reloaded: off, and nothing is requested for RNNoise", noise_state(page)["state"] == "off" and len(traffic) == n, f"{noise_state(page)} {traffic[n:]}")
                check("no CSP violation, foreign request or page error", not external, str(external))
                ctx.close(); browser.close()

            # ---------------------------------------------------------------
            # steady-noise false accepts, off against on
            # ---------------------------------------------------------------
            PROBE = """async ({ b64, on }) => {
              const u8 = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
              const pcm = new Int16Array(u8.buffer);
              const frames = Math.floor(pcm.length / 512);
              const ps = [];
              const stage = window.__hud.mic.makeStage({ deliver: (f, p) => ps.push(p), noiseSuppression: on });
              const until = async (fn, ms) => { const t0 = performance.now(); while (!fn()) { if (performance.now() - t0 > ms) throw new Error('timeout'); await new Promise(r => setTimeout(r, 20)); } };
              stage.start();
              await until(() => stage.status().state === 'ready' && (!on || stage.status().noise.state === 'ready'), 40000);
              for (let i = 0; i < frames; i++) {
                const f = new Float32Array(512);
                for (let k = 0; k < 512; k++) f[k] = pcm[i * 512 + k] / 32768;
                stage.push(f);
                while (i + 1 - ps.length > 6) await new Promise(r => setTimeout(r, 1));
              }
              await until(() => ps.length === frames, 20000);
              const st = stage.status();
              stage.stop();
              return { ps, noise: st.noise, detector: st.detector };
            }"""

            def false_accepts():
                snr = 5
                samples, gt, dil = speech_in_pink(snr)
                b64 = base64.b64encode(struct.pack(f"<{len(samples)}h", *samples)).decode()
                browser = launch(quiet)
                ctx = new_context(browser)
                page = open_page(ctx)
                results = {}
                for on in (False, True):
                    results[on] = page.evaluate(PROBE, {"b64": b64, "on": on})

                def fa(ps):
                    return sum(1 for p, d in zip(ps, dil) if p is not None and p >= 0.5 and not d)

                def recall(ps):
                    hits = sum(1 for p, g in zip(ps, gt) if g and p is not None and p >= 0.5)
                    return hits / sum(gt)
                off, on = results[False], results[True]
                check("both runs were judged by Silero, every frame (no energy fallback)",
                      off["detector"] == on["detector"] == "silero" and all(p is not None for p in off["ps"] + on["ps"]) and len(off["ps"]) == len(on["ps"]) == len(gt))
                check("the 'on' run really ran RNNoise, the 'off' run did not",
                      on["noise"]["state"] == "ready" and on["noise"]["frameMs"] > 0 and off["noise"]["state"] == "off", f"{on['noise']} {off['noise']}")
                f_off, f_on = fa(off["ps"]), fa(on["ps"])
                r_off, r_on = recall(off["ps"]), recall(on["ps"])
                print(f"     (speech in pink noise at {snr} dB SNR, {len(gt)} frames, {sum(gt)} speech frames: false-accept frames "
                      f"off {f_off} on {f_on}; recall off {r_off:.3f} on {r_on:.3f})")
                check("steady-noise false accepts are reduced by suppression (at least halved, from a real number)",
                      f_off >= 5 and f_on * 2 <= f_off, f"off {f_off}, on {f_on}")
                check("while nearly all the speech is still heard (recall within 6 points)", r_on >= r_off - 0.06 and r_on > 0.85, f"off {r_off:.3f} on {r_on:.3f}")
                check("no CSP violation, foreign request or page error", not external, str(external))
                ctx.close(); browser.close()

            # ---------------------------------------------------------------
            # the recording stays raw
            # ---------------------------------------------------------------
            RING_RMS = """() => { const r = window.__hud.capture.ring; const x = r.slice(Math.max(0, r.written - 3 * 16000), r.written);
              let s = 0; for (const v of x) s += v * v; return { rms: Math.sqrt(s / x.length), n: x.length, written: r.written }; }"""
            RING_DUMP = """() => { const r = window.__hud.capture.ring; const x = r.slice(0, r.written); const i = new Int16Array(x.length);
              for (let k = 0; k < x.length; k++) { const s = Math.max(-1, Math.min(1, x[k])); i[k] = s < 0 ? s * 0x8000 : s * 0x7fff; }
              const u8 = new Uint8Array(i.buffer); let bin = ''; for (let k = 0; k < u8.length; k += 0x8000) bin += String.fromCharCode.apply(null, u8.subarray(k, k + 0x8000));
              return btoa(bin); }"""

            def recorded_raw():
                levels = {}
                for on in (False, True):
                    browser = launch()
                    ctx = new_context(browser, STEADY_MIC + f"try {{ localStorage.setItem({json.dumps(KEY)}, {json.dumps('1' if on else '0')}); }} catch (e) {{}}")
                    page = open_page(ctx)
                    if on:
                        wait_noise(page, "ready")
                    page.wait_for_function("() => window.__hud.mic.state().written > 16000 * 8", timeout=60000)
                    page.wait_for_timeout(1500)
                    levels[on] = page.evaluate(RING_RMS)
                    levels[on]["state"] = noise_state(page)["state"]
                    ctx.close(); browser.close()
                off_rms, on_rms = levels[False]["rms"], levels[True]["rms"]
                import math
                db = 20 * math.log10(on_rms / off_rms) if on_rms > 0 and off_rms > 0 else -99
                print(f"     (ring level over 3 s of steady noise: off {off_rms:.4f}, on {on_rms:.4f}, {db:+.2f} dB; suppression state '{levels[True]['state']}')")
                check("the noise really is in the capture, loud (so a denoised ring would show it)", off_rms > 0.02, f"{off_rms:.4f}")
                check("suppression was running (ready) while it was recorded", levels[True]["state"] == "ready" and levels[False]["state"] == "off", str(levels))
                check("what is recorded is the raw signal: the same level on or off (RNNoise alone would remove 15 dB+ of it)", abs(db) < 1.0, f"{db:+.2f} dB")

                # An uploaded clip is a verbatim slice of the ring, with suppression on.
                speech = scenario(tmp, "raw_speech", ("a", 0), ("gap", 8))
                browser = launch(speech)
                ctx = new_context(browser, f"try {{ localStorage.setItem({json.dumps(KEY)}, '1'); }} catch (e) {{}}")
                page = open_page(ctx)
                wait_noise(page, "ready")
                bodies: list[bytes] = []

                def handle(route):
                    bodies.append(route.request.post_data_buffer or b"")
                    route.fulfill(content_type="application/json", body=json.dumps({"text": "synthetic words"}))
                page.route("**/stt*", handle)
                page.evaluate(CLAIM_FIRST)
                page.wait_for_function("() => window.__hud.voiceDiagnostics().rows.length >= 1", timeout=70000)
                page.wait_for_timeout(1500)
                check("a clip was recorded and uploaded with suppression on", len(bodies) >= 1 and len(bodies[0]) > 44 + 2 * 16000, str([len(b) for b in bodies]))
                ring = base64.b64decode(page.evaluate(RING_DUMP))
                body = bodies[0][44:]
                key = body[2 * 4000:2 * 4000 + 2 * 3000]
                at = ring.find(key)
                check("the uploaded audio is a byte-for-byte slice of the raw ring (3,000 samples from inside it)", at >= 0 and at % 2 == 0, f"find -> {at}")
                if at >= 0:
                    whole = ring[at - 2 * 4000:at - 2 * 4000 + len(body)]
                    bad = next((i for i in range(0, min(len(whole), len(body)), 2) if whole[i:i + 2] != body[i:i + 2]), -1)
                    tail = sum(1 for i in range(0, len(body), 2) if whole[i:i + 2] != body[i:i + 2])
                    check("and so is the whole clip", whole == body, f"{len(whole)} of {len(body)} bytes compared; first difference at sample {bad // 2}; {tail} samples differ "
                          f"{[(i // 2, struct.unpack('<h', whole[i:i + 2])[0], struct.unpack('<h', body[i:i + 2])[0]) for i in range(0, len(body), 2) if whole[i:i + 2] != body[i:i + 2]][:6]}")
                check("no CSP violation, foreign request or page error", not external, str(external))
                ctx.close(); browser.close()

            # ---------------------------------------------------------------
            # falls back to raw, visibly
            # ---------------------------------------------------------------
            def fallbacks():
                browser = launch(quiet)
                good = WASM.read_bytes()
                variants = [
                    ("the binary is missing (404)", "fetch", "404"),
                    ("the binary has one byte changed", "hash", "flip"),
                    ("the binary is truncated", "hash", "cut"),
                    ("the binary is padded", "hash", "pad"),
                ]
                for name, code, how in variants:
                    ctx = new_context(browser, f"try {{ localStorage.setItem({json.dumps(KEY)}, '1'); }} catch (e) {{}}")
                    state = {"broken": True}

                    def serve(route, request, how=how, state=state):
                        if not state["broken"]:
                            return route.continue_()
                        if how == "404":
                            return route.fulfill(status=404, body="no")
                        bad = bytearray(good)
                        if how == "flip":
                            bad[len(bad) // 2] ^= 1
                        body = bytes(bad[:-1000]) if how == "cut" else bytes(bad) + b"\0" if how == "pad" else bytes(bad)
                        return route.fulfill(status=200, content_type="application/wasm", body=body)
                    ctx.route(WASM_RE, serve)
                    page = open_page(ctx)
                    wait_noise(page, "failed")
                    st = noise_state(page)
                    d = detector(page)
                    check(f"{name}: noise suppression is off with reason '{code}'", st["wanted"] is True and st["code"] == code, str(st))
                    check(f"{name}: the VAD keeps working (Silero still the detector)", d["detector"] == "silero" and d["state"] == "ready", str(d))
                    a = detector(page)["scored"]
                    w0 = page.evaluate("window.__hud.mic.state().written")
                    page.wait_for_timeout(1500)
                    check(f"{name}: and keeps scoring; the microphone keeps flowing",
                          detector(page)["scored"] > a + 20 and page.evaluate("window.__hud.mic.state().written") - w0 > 16000)
                    open_settings(page, "Voice")
                    shown = page.locator('[data-testid="voice-noise-suppression"]').inner_text()
                    check(f"{name}: diagnostics say so in words", "fell back to the raw signal" in shown and len(shown) > 60, shown)
                    check(f"{name}: and Settings says it is not running", "Not running" in page.locator('[data-testid="noise-suppression-state"]').inner_text())
                    snap = json.dumps(page.evaluate("window.__hud.voiceDiagnostics()")) + shown
                    check(f"{name}: with no URL, path or engine text in it", "http" not in snap and ".wasm" not in snap and "rnnoise_" not in snap, snap[:240])
                    check(f"{name}: the switch stays on (the owner's choice), with a retry offered",
                          page.locator('[data-testid="noise-suppression-toggle"]').is_checked() and page.locator('[data-testid="voice-noise-retry"]').count() == 1)
                    state["broken"] = False
                    page.locator('[data-testid="voice-noise-retry"]').click()
                    wait_noise(page, "ready")
                    check(f"{name}: 'Try noise suppression again' restores it once the cause is gone",
                          noise_state(page)["state"] == "ready" and page.locator('[data-testid="voice-noise-retry"]').count() == 0)
                    ctx.close()
                browser.close()
                check("none of this was a CSP violation or a foreign request", not [e for e in external if "CSP" in e or "outside" in e], str(external))

            run(off_by_default)
            run(toggle_live)
            run(false_accepts)
            run(recorded_raw)
            run(fallbacks)
    finally:
        mock.stop()
    print(f"\n{count} checks, {len(failures)} failures")
    for f in failures:
        print("FAILED:", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
