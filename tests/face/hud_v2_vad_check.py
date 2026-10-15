"""V5 voice capture through the real HUD: the Silero VAD worker, the energy fallback
and the three timers (plan §3.2, decision V-1).

What it proves, in a real Chromium against the mock daemon:
  - the Silero worker really loads under the HUD's CSP (same-origin worker, WASM and
    model; `wasm-unsafe-eval` only on the worker script's own response, never the page's
    or the model's (owner, 2026-10-15); no `unsafe-eval`, no CSP violation), the model
    served is the pinned file, and Settings -> Voice names the detector in use;
  - synthetic Kokoro speech played through Chrome's fake microphone
    (`--use-file-for-fake-audio-capture`, never a real voice) is detected by Silero:
      a 2.2 s pause continues the message and a 2.8 s pause ends it (the exact 2.4 s / 2.6 s
      edges are scripted in hud/src/lib/captureV5.test.ts: the model's own onset lag of
      about 100 ms puts a real-audio edge test within noise of the 2.5 s timer);
      speech 5 s after a message went out is claimed as its continuation, 7 s is not;
  - a noise-only recording (white + pink, loud) opens no utterance;
  - the worker failing to load (model missing, model altered, runtime blocked,
    Worker construction refused) or running too slowly falls back to the energy
    detector, visibly (Settings -> Voice, the diagnostics snapshot), with dictation
    still working, and "Try again" restores Silero.

Everything is isolated: scripted `/stt` on the mock daemon's ephemeral port, the
fixtures are synthetic (tests/face/fixtures/voice_v5), no external request, no live
daemon port. Run alone, optionally for some sections:
    python tests/face/hud_v2_vad_check.py [section,section]
under a temporary HOME, dummy keys, the worktree PYTHONPATH and a 3G/0swap scope.
Needs hud/dist built; sections: silero_loads, stalls, pauses, continuation, long_dictation, noise, fallbacks.
"""
from __future__ import annotations

import json
import random
import re
import struct
import sys
import tempfile
import wave
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
assert not (REPO / ".env").exists(), "use a credential-free worktree"
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.face.hud_v2_check import FAKE_RECOGNIZER, guard_live, open_settings, watch_csp  # noqa: E402
from tests.face.hud_v2_mock import MockDaemon  # noqa: E402
from tests.face.hud_v2_voice_diagnostics_check import FAKE_MIC  # noqa: E402

FIXTURES = REPO / "tests" / "face" / "fixtures" / "voice_v5"
RATE = 16000
LEAD_S = 12.0     # the file starts when the microphone opens; the page and worker need this long
BASE_ARGS = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
             "--autoplay-policy=no-user-gesture-required"]


# ---------------------------------------------------------------------------
# synthetic audio (all generated or synthetic; nothing recorded)
# ---------------------------------------------------------------------------

def read_wav(path: Path) -> list[int]:
    """The clip trimmed to its first and last audible sample (|x| > 100), so a scenario's
    gap is the silence between the words, not the gap plus the clip's own tails."""
    with wave.open(str(path)) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
        x = list(struct.unpack(f"<{w.getnframes()}h", w.readframes(w.getnframes())))
    loud = [i for i, v in enumerate(x) if abs(v) > 100]
    return x[loud[0]:loud[-1] + 1]


def write_wav(path: Path, samples: list[int]) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(struct.pack(f"<{len(samples)}h", *[max(-32768, min(32767, s)) for s in samples]))
    return path


def silence(seconds: float) -> list[int]:
    return [0] * int(seconds * RATE)


def room(seconds: float, amp: int = 6) -> list[int]:
    """A very quiet room (a few LSBs of noise), so the energy floor is not exactly zero."""
    rnd = random.Random(5)
    return [rnd.randint(-amp, amp) for _ in range(int(seconds * RATE))]


def noise(seconds: float, rms: float = 0.04) -> list[int]:
    """Loud white + pink noise (1/f by Voss-McCartney), no speech in it."""
    rnd = random.Random(11)
    n = int(seconds * RATE)
    rows, pink = [0.0] * 12, []
    for i in range(n):
        k = (i & -i).bit_length() - 1 if i else 0
        if k < len(rows):
            rows[k] = rnd.uniform(-1, 1)
        pink.append(sum(rows) / len(rows))
    peak = max(abs(x) for x in pink) or 1.0
    white = [rnd.uniform(-1, 1) for _ in range(n)]
    mix = [0.6 * p / peak + 0.4 * w for p, w in zip(pink, white)]
    scale = rms * 32767 / ((sum(x * x for x in mix) / n) ** 0.5)
    return [int(x * scale) for x in mix]


def scenario(tmp: Path, name: str, *parts) -> Path:
    """lead-in silence, then the parts: ("a"|"b"|"gap"|"noise", seconds)."""
    a, b = read_wav(FIXTURES / "kokoro_a.wav"), read_wav(FIXTURES / "kokoro_b.wav")
    out = room(LEAD_S)
    for kind, secs in parts:
        out += a if kind == "a" else b if kind == "b" else room(secs) if kind == "gap" else noise(secs)
    return write_wav(tmp / f"{name}.wav", out)


class Stt:
    """A scripted `/stt`: records each upload, answers with fixed words."""

    def __init__(self, numbered=False):
        self.reqs: list[dict] = []
        self.numbered = numbered   # answer "part N" (N = the chunk's place in its message)

    def handle(self, route):
        q = {k: v[0] for k, v in parse_qs(urlsplit(route.request.url).query).items()}
        body = route.request.post_data_buffer or b""
        self.reqs.append({"seq": int(q["seq"]), "utt": int(q["utterance_id"]),
                          "ms": max(0, len(body) - 44) / 2 / RATE * 1000})
        text = f"part{int(q['seq']) - int(q['utterance_id']) + 1}" if self.numbered else "synthetic words"
        route.fulfill(content_type="application/json", body=json.dumps({"text": text}))


# Claim the first utterance the detector opens, the way a wake hit does (test hook), so the
# message under test is claimed without depending on a follow-up window's clock.
CLAIM_FIRST = """() => { window.__claimed = 0;
  const t = setInterval(() => { const v = window.__hud.capture.vad;
    if (v.open && !window.__hud.capture.segClaimed && !window.__claimed) { window.__claimed = 1; window.__hud.mic.wake(); clearInterval(t); } }, 15); }"""
PEAK_WATCH = """() => { window.__peak = { opened: 0, ml: 0, frames: 0 };
  setInterval(() => { const v = window.__hud.capture.vad; window.__peak.frames++; if (v.open) window.__peak.opened++; if (v.ml) window.__peak.ml++; }, 20); }"""


def main(argv=None):
    only = set(filter(None, ((argv if argv is not None else sys.argv[1:]) or [""])[0].split(",")))
    failures: list[str] = []
    external: list[str] = []   # what the guards recorded (CSP violations, requests outside the mock, page errors)
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

    mock = MockDaemon(0).start()
    tmp = Path(tempfile.mkdtemp(prefix="vad_v5_"))
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

            def open_page(ctx, wait_detector=None):
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

            def run(section):
                if only and section.__name__ not in only:
                    return
                print(f"\n{section.__name__}")
                try:
                    section()
                except Exception as e:   # a section that cannot run is a failure, the rest still run
                    check(f"{section.__name__} ran to the end", False, str(e).splitlines()[0][:240])

            # ---------------------------------------------------------------
            # Silero loads, under the CSP, from the pinned model
            # ---------------------------------------------------------------
            def silero_loads():
                browser = launch(scenario(tmp, "quiet", ("gap", 20)))
                ctx = new_context(browser)
                before = len(external)
                seen: list[tuple[str, str, int, str]] = []
                ctx.on("response", lambda r: seen.append(
                    (r.url, r.headers.get("content-type", ""), r.status, r.headers.get("content-security-policy", "")))
                    if re.search(r"/assets/.*\.(onnx|wasm)$|/assets/worker-.*\.js$", r.url) else None)
                page = open_page(ctx, wait_detector="ready")
                st = detector(page)
                check("Silero is the live detector once the worker is ready", st["detector"] == "silero" and st["state"] == "ready", str(st))
                models = [u for u, *_ in seen if u.endswith(".onnx") and "/assets/silero_vad" in u]   # (V11's wake-word model files load beside it)
                wasms = [(u, ct) for u, ct, *_ in seen if u.endswith(".wasm") and "ort-wasm" in u]
                check("the model is served by the HUD's own origin under /assets", len(models) >= 1
                      and all(u.startswith(base + "/assets/silero_vad") for u in models), str(models))
                check("so is the onnxruntime WebAssembly, as application/wasm", len(wasms) >= 1
                      and all(u.startswith(base + "/assets/") and ct.startswith("application/wasm") for u, ct in wasms), str(wasms))
                # The worker's own script response carries 'wasm-unsafe-eval' (the policy a
                # worker runs under); the model and the .wasm carry the page's, which never
                # does (owner, 2026-10-15; hud_api.wasm_script).
                from jarvis.v2 import hud_api as H
                path = lambda u: u.split("?")[0][len(base):].lstrip("/")
                bad = [(u, p) for u, _, _, p in seen if p != H.content_security_policy(wasm=H.wasm_script(path(u)))]
                scripts = [u for u, *_ in seen if H.wasm_script(path(u))]
                check("each carries the policy the daemon gives it: wasm-unsafe-eval on the worker's script only, never unsafe-eval",
                      bool(scripts) and not bad and all(" 'unsafe-eval'" not in p and "worker-src 'self'" in p for *_, p in seen),
                      f"scripts={scripts} bad={bad[:2]}")
                served = (REPO / "hud" / "vendor" / "silero-vad" / "silero_vad_v5.1.2.onnx").read_bytes()
                fetched = page.evaluate("""async (u) => { if (!u) return ''; const b = new Uint8Array(await (await fetch(u)).arrayBuffer());
                  const d = await crypto.subtle.digest('SHA-256', b); return Array.from(new Uint8Array(d), x => x.toString(16).padStart(2, '0')).join(''); }""", models[0] if models else "")
                import hashlib
                check("the model the page received is the pinned one (SHA-256 of the vendored file)",
                      fetched == hashlib.sha256(served).hexdigest() == "2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f", fetched)
                page.wait_for_timeout(2500)
                st2 = detector(page)
                check("it scores frames in real time, well inside the 32 ms budget", st2["scored"] > 40 and 0 < st2["frameMs"] < 16,
                      f"scored {st2['scored']}, {st2['frameMs']:.2f} ms/frame")
                print(f"     (measured here: ready in {st2['loadMs']:.0f} ms, {st2['frameMs']:.2f} ms per frame, headless Chromium on a shared machine)")
                check("and was ready within seconds of the page opening", 0 < st2["loadMs"] < 8000, f"{st2['loadMs']:.0f} ms")
                check("the capture hears probabilities (Vad.ml)", page.evaluate("window.__hud.capture.vad.ml") is True)
                open_settings(page, "Voice")
                shown = page.locator('[data-testid="voice-detector"]').inner_text()
                check("Settings -> Voice names the detector", "Silero VAD (ML)" in shown and "ms per 32 ms frame" in shown, shown)
                check("and the timers", "2.5 s" in page.locator('[data-testid="voice-end-pause"]').inner_text()
                      and "0.7 s" in page.locator('[data-testid="voice-end-pause"]').inner_text())
                check("the pause setting defaults to 2.5 s", page.locator('[data-testid="end-pause-select"]').input_value() == "2500")
                page.locator('[data-testid="end-pause-select"]').select_option("3000")
                check("and changing it reaches the capture", page.evaluate("window.__hud.mic.endPause()") == 3000)
                page.locator('[data-testid="end-pause-select"]').select_option("2500")
                page.locator('[data-testid="settings-close"]').click()
                check("no CSP violation, foreign request or page error while it loaded", len(external) == before, str(external[before:]))
                ctx.close(); browser.close()

            # ---------------------------------------------------------------
            # a busy main thread is not a slow worker
            # ---------------------------------------------------------------
            def stalls():
                browser = launch(scenario(tmp, "quiet2", ("gap", 40)))
                ctx = new_context(browser)
                page = open_page(ctx, wait_detector="ready")
                for ms in (700, 1500, 700):
                    before = detector(page)["scored"]
                    frames_before = page.evaluate("window.__hud.mic.state().written")
                    page.evaluate("(ms) => { const t = performance.now(); while (performance.now() - t < ms); }", ms)
                    page.wait_for_timeout(2500)
                    st = detector(page)
                    check(f"a {ms} ms busy main thread: Silero is still the detector", st["detector"] == "silero" and st["state"] == "ready", str(st))
                    check(f"a {ms} ms busy main thread: it keeps scoring afterwards", st["scored"] > before + 40, f"{before} -> {st['scored']}")
                    check(f"a {ms} ms busy main thread: no audio was lost",
                          page.evaluate("window.__hud.mic.state().written") - frames_before > 16000 * (2.5 + ms / 1000) * 0.9)
                check("the stalls cost the worker nothing: still about a millisecond a frame", detector(page)["frameMs"] < 16, str(detector(page)))
                ctx.close(); browser.close()

            # ---------------------------------------------------------------
            # 2.4 s continues, 2.6 s ends
            # ---------------------------------------------------------------
            def run_speech(name, parts, wait_rows, settle_s=8):
                browser = launch(scenario(tmp, name, *parts))
                ctx = new_context(browser)
                page = open_page(ctx, wait_detector="ready")
                stt = Stt()
                page.route("**/stt*", stt.handle)
                page.evaluate(CLAIM_FIRST)
                page.evaluate(PEAK_WATCH)
                try:
                    page.wait_for_function(f"() => window.__hud.voiceDiagnostics().rows.length >= {wait_rows}", timeout=int((settle_s + 45) * 1000))
                except Exception:
                    pass
                page.wait_for_timeout(int(settle_s * 1000))
                rows = page.evaluate("window.__hud.voiceDiagnostics().rows")
                det = detector(page)
                return ctx, browser, page, stt, rows, det

            def pauses():
                for gap, ends in ((2.2, False), (2.8, True)):
                    ctx, browser, page, stt, rows, det = run_speech(f"pause{int(gap * 10)}", [("b", 0), ("gap", gap), ("b", 0), ("gap", 6)], 1 if not ends else 2)
                    tag = f"a {gap:.1f} s pause"
                    check(f"{tag}: Silero was judging the whole time", det["detector"] == "silero" and det["scored"] > 500, str(det))
                    utts = {r["utt"] for r in stt.reqs}
                    if not ends:
                        check(f"{tag} continues: one message (one utterance id, one diagnostics row below)", len(utts) == 1, str(stt.reqs))
                        check(f"{tag} continues: the opening is cut at its first 700 ms gap and transcribes while the rest is said (2 chunks)",
                              len(stt.reqs) == 2 and stt.reqs[0]["ms"] < 6000, str(stt.reqs))
                        check(f"{tag} continues: between them they hold both sentences and the pause",
                              9500 < sum(r["ms"] for r in stt.reqs) < 13000, str(stt.reqs))
                        check(f"{tag} continues: one diagnostics row", len(rows) == 1 and rows[0]["source"] == "wake" and rows[0]["detector"] == "silero", str(rows))
                    else:
                        check(f"{tag} ends the message: two messages, the second a continuation", len(utts) == 2
                              and [r["source"] for r in rows] == ["wake", "continuation"], f"{stt.reqs} {rows}")
                        check(f"{tag} ends the message: the first holds only the first sentence",
                              len(stt.reqs) >= 1 and 4300 < stt.reqs[0]["ms"] < 7500, str(stt.reqs))
                    check(f"{tag}: rows say which detector judged them", all(r["detector"] == "silero" for r in rows), str(rows))
                    ctx.close(); browser.close()

            # ---------------------------------------------------------------
            # continuation window: claimed 5 s after the message ended, not 7 s
            # ---------------------------------------------------------------
            def continuation():
                # The message ends 2.5 s after the first sentence; the window runs 6 s from there.
                for after_end, claimed in ((5.0, True), (7.0, False)):
                    ctx, browser, page, stt, rows, det = run_speech(
                        f"cont{int(after_end)}", [("a", 0), ("gap", 2.5 + after_end), ("b", 0), ("gap", 6)], 2)
                    sources = [r["source"] for r in rows]
                    tag = f"speech {after_end:.0f} s after the message ended"
                    if claimed:
                        check(f"{tag} is claimed as its continuation, no wake word", sources == ["wake", "continuation"], str(rows))
                        check(f"{tag}: both were uploaded", len({r['utt'] for r in stt.reqs}) == 2, str(stt.reqs))
                    else:
                        check(f"{tag} is not claimed", sources[:1] == ["wake"] and "continuation" not in sources, str(rows))
                        check(f"{tag}: it is dropped as unclaimed, nothing uploaded for it",
                              any(r["outcome"] == "unclaimed" for r in rows) and len({r['utt'] for r in stt.reqs}) == 1, f"{rows} {stt.reqs}")
                    ctx.close(); browser.close()

            # ---------------------------------------------------------------
            # a long dictation through Silero, chunked
            # ---------------------------------------------------------------
            def long_dictation():
                parts = [("a", 0), ("gap", 0.9), ("b", 0), ("gap", 0.9), ("a", 0), ("gap", 0.9), ("b", 0), ("gap", 0.9), ("a", 0), ("gap", 0.9), ("b", 0), ("gap", 6)]
                browser = launch(scenario(tmp, "long", *parts))
                ctx = new_context(browser)
                page = open_page(ctx, wait_detector="ready")
                stt = Stt(numbered=True)
                page.route("**/stt*", stt.handle)
                page.evaluate(CLAIM_FIRST)
                try:
                    page.wait_for_function("() => document.querySelector('[data-testid=\"input\"]').value.includes('part3')", timeout=75000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)
                box = page.locator('[data-testid="input"]').first.input_value().strip()
                rows = page.evaluate("window.__hud.voiceDiagnostics().rows")
                total = sum(r["ms"] for r in stt.reqs)
                check("a 30 s dictation with 0.9 s breaths is one message in several chunks", len({r["utt"] for r in stt.reqs}) == 1 and len(stt.reqs) >= 3, str(stt.reqs))
                check("the first chunk left early (under 8 s: only the first gap rule cuts there), the others at 8 s or more",
                      stt.reqs and stt.reqs[0]["ms"] < 8000 and all(r["ms"] > 8000 for r in stt.reqs[1:-1]), str(stt.reqs))
                check("their audio adds up to the dictation, nothing repeated at pause cuts", 28000 < total < 36000, f"{total:.0f} ms")
                expect = " ".join(f"part{i + 1}" for i in range(len(stt.reqs)))
                check("the words come back as one message, in order, in the box", box == expect, f"{box!r} vs {expect!r}")
                check("one row, judged by Silero", len(rows) == 1 and rows[0]["detector"] == "silero" and rows[0]["source"] == "wake", str(rows))
                ctx.close(); browser.close()

            # ---------------------------------------------------------------
            # noise only
            # ---------------------------------------------------------------
            def noise_only():
                browser = launch(scenario(tmp, "noise", ("noise", 16), ("gap", 3)))
                ctx = new_context(browser)
                page = open_page(ctx, wait_detector="ready")
                stt = Stt()
                page.route("**/stt*", stt.handle)
                page.evaluate("() => { window.__hud.capture.openFollowUp(); setInterval(() => window.__hud.capture.openFollowUp(), 500); }")
                page.evaluate(PEAK_WATCH)
                page.evaluate("() => { window.__maxRms = 0; setInterval(() => { window.__maxRms = Math.max(window.__maxRms, window.__hud.capture.vad.rms); }, 20); }")
                page.wait_for_timeout(int((LEAD_S + 22) * 1000))
                peak = page.evaluate("window.__peak")
                rms = page.evaluate("window.__maxRms")
                check("the noise really reached the capture (loud: well above the quiet room)", rms > 0.02, f"max frame rms {rms:.4f}")
                check("loud white + pink noise opens no utterance, even with a follow-up window held open",
                      peak["opened"] == 0 and peak["ml"] > 100, str(peak))
                check("nothing was claimed, uploaded or listed", not stt.reqs and page.evaluate("window.__hud.voiceDiagnostics().rows.length") == 0)
                check("Silero judged it throughout", detector(page)["detector"] == "silero")
                ctx.close(); browser.close()

            # ---------------------------------------------------------------
            # fallbacks, visibly
            # ---------------------------------------------------------------
            def fallbacks():
                browser = launch()
                variants = [
                    ("the model is missing", "fetch", lambda ctx: ctx.route(re.compile(r"/assets/silero_vad.*\.onnx$"), lambda route: route.fulfill(status=404, body="no")), ""),
                    ("the model has one byte changed", "hash", None, "flip"),
                    ("the model is truncated", "hash", None, "cut"),
                    ("the WebAssembly runtime is blocked", "runtime", lambda ctx: ctx.route(re.compile(r"/assets/ort-wasm.*\.wasm$"), lambda route: route.fulfill(status=500, body="no")), ""),
                    ("the browser refuses to start the worker", "worker",
                     None, "window.Worker = class { constructor() { throw new Error('blocked at http://private.example/w.js'); } };"),
                    ("the worker answers far too slowly", None,
                     None, """(() => { const W = window.Worker;
                        window.__slowVad = 1500;
                        window.Worker = class extends W { postMessage(m, t) {
                          if (m && m.type === 'frame' && window.__slowVad) setTimeout(() => super.postMessage(m, t), window.__slowVad); else super.postMessage(m, t); } }; })();"""),
                ]
                good = (REPO / "hud" / "vendor" / "silero-vad" / "silero_vad_v5.1.2.onnx").read_bytes()
                for name, code, router, extra in variants:
                    init = FAKE_MIC + (extra if extra not in ("flip", "cut", "") else "")
                    ctx = new_context(browser, init)
                    if router:
                        router(ctx)
                    if extra == "flip":
                        bad = bytearray(good); bad[len(bad) // 2] ^= 1
                        def serve_bad(route, request, body=bytes(bad)):
                            route.fulfill(status=200, content_type="application/octet-stream", body=body)
                        ctx.route(re.compile(r"/assets/silero_vad.*\.onnx$"), serve_bad)
                    elif extra == "cut":
                        def serve_cut(route, request):
                            route.fulfill(status=200, content_type="application/octet-stream", body=good[:-1000])
                        ctx.route(re.compile(r"/assets/silero_vad.*\.onnx$"), serve_cut)
                    page = open_page(ctx)
                    stt = Stt()
                    page.route("**/stt*", stt.handle)
                    page.wait_for_function("() => window.__hud.mic.detector().state === 'failed'", timeout=30000)
                    st = detector(page)
                    if code:
                        check(f"{name}: falls back to energy with reason '{code}'", st["detector"] == "energy" and st["code"] == code, str(st))
                    else:
                        # Frames reach the worker (listen is the stage's neighbour); give the deadline time to pass.
                        check(f"{name}: falls back to energy ('late')", st["detector"] == "energy" and st["code"] == "late", str(st))
                    open_settings(page, "Voice")
                    shown = page.locator('[data-testid="voice-detector"]').inner_text()
                    check(f"{name}: Settings -> Voice says so, in words", "energy fallback" in shown and len(shown) > 40 and "Silero VAD (ML)" not in shown, shown)
                    check(f"{name}: and offers to try again", page.locator('[data-testid="voice-detector-retry"]').count() == 1)
                    log = page.locator('[data-testid="voice-detector-log"]').text_content() if page.locator('[data-testid="voice-detector-log"]').count() else ""
                    check(f"{name}: and the switch is in the detector log", "failed" in log.lower() or "energy" in log, log[:200])
                    snap = json.dumps(page.evaluate("window.__hud.voiceDiagnostics()")) + shown
                    check(f"{name}: with no URL, path or engine text in it", "private.example" not in snap and "silero_vad" not in snap and ".wasm" not in snap and "http" not in snap, snap[:200])
                    page.locator('[data-testid="settings-close"]').click()
                    a = page.evaluate("window.__hud.mic.state().written")
                    page.wait_for_timeout(1200)
                    check(f"{name}: the microphone keeps flowing", page.evaluate("window.__hud.mic.state().written") - a > 12000)
                    # Dictation on the energy detector still works.
                    stt.reqs.clear()
                    page.evaluate("window.__hud.mic.feedMs(2000, .0009); window.__hud.capture.press(); window.__hud.mic.feedMs(1500, .06); window.__hud.capture.release()")
                    page.wait_for_function("() => document.querySelector('[data-testid=\"input\"]').value.includes('synthetic words')")
                    check(f"{name}: dictation still works on the energy detector", len(stt.reqs) == 1)
                    rows = page.evaluate("window.__hud.voiceDiagnostics().rows")
                    check(f"{name}: and its row says the energy detector judged it", rows and rows[-1]["detector"] == "energy", str(rows[-1:]))
                    page.locator('[data-testid="input"]').first.fill("")
                    if code is None:
                        # The page coming back to the foreground re-arms a detector that failed for timing.
                        page.evaluate("window.__slowVad = 0; document.dispatchEvent(new Event('visibilitychange'))")
                        page.wait_for_function("() => window.__hud.mic.detector().state === 'ready'", timeout=30000)
                        check(f"{name}: the page returning to the foreground tries again at once, and Silero is back", detector(page)["detector"] == "silero")
                    ctx.close()

                # Try again restores Silero once the cause is gone.
                ctx = new_context(browser, FAKE_MIC)
                blocked = {"on": True}
                ctx.route(re.compile(r"/assets/silero_vad.*\.onnx$"),
                          lambda route: route.fulfill(status=404, body="no") if blocked["on"] else route.continue_())
                page = open_page(ctx)
                page.wait_for_function("() => window.__hud.mic.detector().state === 'failed'", timeout=30000)
                blocked["on"] = False
                open_settings(page, "Voice")
                page.locator('[data-testid="voice-detector-retry"]').click()
                page.wait_for_function("() => window.__hud.mic.detector().state === 'ready'", timeout=30000)
                check("'Try the Silero VAD again' restores Silero once the cause is gone",
                      detector(page)["detector"] == "silero" and page.locator('[data-testid="voice-detector-retry"]').count() == 0)
                ctx.close()
                browser.close()

            run(silero_loads)
            run(stalls)
            run(pauses)
            run(continuation)
            run(long_dictation)
            run(noise_only)
            run(fallbacks)
    finally:
        mock.stop()
    print(f"\n{count} checks, {len(failures)} failures")
    for f in failures:
        print("FAILED:", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
