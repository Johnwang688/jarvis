"""V11 through the real HUD: the local wake word (plan sections 3.6, 3.10, 5; decisions V-6, Q10, Q16).

"Jarvis" is heard by a small keyword model (sherpa-onnx's zipformer2, int8) running in a same-origin
Web Worker on onnxruntime-web; Chrome's cloud speech recognizer is gone. This drives the real page
(Chromium, mock daemon on an ephemeral/own port, no live daemon, no network, no real voice, no paid call)
in four layers, every context under the CSP-violation watch and a refusal of any request that is not the mock:

  mic       synthetic Kokoro speech through Chrome's fake microphone (`--use-file-for-fake-audio-capture`):
              - sentences that only sound like it (Travis, a jar, cancel) wake nothing and upload nothing;
              - "Jarvis." in two voices and "Hey Jarvis, can you hear me?" each wake it, are claimed as source
                `wake`, and uploaded to the (local, mock) /stt; every request the page made went to the mock;
              - the worker and model come from the HUD's own origin, under its CSP, the files served are the
                pinned ones, and no SpeechRecognition is ever constructed;
              - the cost of the keyword spotter in this Chromium, against the same page with the wake word failed.
  own       Jarvis's reply is a synthetic recording of him saying "Hey Jarvis, can you hear me?", played by the
              REAL Web Audio player and fed back to the microphone by the page's acoustic simulator
              (hud_v2_barge_check.py's): the keyword model hears it, and V9's self-voice protections keep it
              from waking, claiming, pausing, or uploading anything. The owner over him still wakes it.
  card      with an authorization card up, "Jarvis." claims nothing and uploads nothing; the keyword set becomes
              the four playback commands (pause, resume, skip, stop) which act on his voice and on nothing else;
              zero approval requests, zero uploads, zero sends; with the owner's switch off they do nothing;
              his own voice saying "Jarvis, stop." is not obeyed.
  fallback  a changed model byte, a short file, a missing file, a tokens file changed, a refused Worker: each
              leaves the wake word visibly unavailable (Settings -> Voice, the engine status), nothing listening,
              dictation by the talk key still working; Try again restores it.

Run (after `cd hud && npm run build`), under a temp HOME, the worktree PYTHONPATH, a 3G scope and a slot:
    python tests/face/hud_v2_wake_check.py [section,section]
Sections: mic_negative, mic_wake, cpu, own_voice, owner_wins, card, fallbacks, settings.
Not claimed: a physical microphone or speakers, Windows Chrome, a real person's voice or room, a TV.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import struct
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
assert not (REPO / ".env").exists(), "use a credential-free worktree"
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.face.hud_v2_barge_check import QUIET, REPLY, SIM, SOURCES, samples_of  # noqa: E402
from tests.face.hud_v2_check import FAKE_RECOGNIZER, guard_live, open_settings, until, watch_csp  # noqa: E402
from tests.face.hud_v2_layout_check import _approval  # noqa: E402
from tests.face.hud_v2_mock import MockDaemon  # noqa: E402
from tests.face.hud_v2_multichat_check import TWO, _box, _dictation, _focus, _fresh, _open, _seed, _sent  # noqa: E402

FIX = REPO / "tests" / "face" / "fixtures" / "voice_v11"
FIX5 = REPO / "tests" / "face" / "fixtures" / "voice_v5"
RATE = 16000
HUD = REPO / "hud"
MIC_ARGS = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"]
SIM_ARGS = ["--autoplay-policy=no-user-gesture-required"]

PINS = {  # lib/wake/pins.ts, hud/vendor/kws-zipformer-gigaspeech-3.3M/manifest.json
    "encoder": ("encoder.int8", 4807159, "1e721676515bcd42a186979733981213c66c80db680e1cc582dfedf3be76e678"),
    "decoder": ("decoder.int8", 277985, "e40ff43297abe815e8898494c17e71bba2152d9d40fa3eb803f75d0f7533329a"),
    "joiner": ("joiner.int8", 163380, "eae9da0c7e1e6c6a3f4cc42d167899c388f6c6701b94cb96320e4f55df79624c"),
    "tokens": ("tokens", 5006, "fd2ded4050a55d2b1578870ba8697d02371980217806b7558bd0a5cc60f3ba53"),
}

# Chrome's SpeechRecognition counted, and the page's own request log.
CHROME_WATCH = FAKE_RECOGNIZER + """
window.__reqs = [];
(() => { const f = window.fetch; window.fetch = function (...a) { try { window.__reqs.push(String(a[0] && a[0].url || a[0])); } catch (e) {} return f.apply(this, a); }; })();
"""


# ---------------------------------------------------------------------------
# synthetic audio (all Kokoro output from tests/face/fixtures; nothing recorded)
# ---------------------------------------------------------------------------

def read_wav(path: Path) -> list[int]:
    with wave.open(str(path)) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
        return list(struct.unpack(f"<{w.getnframes()}h", w.readframes(w.getnframes())))


def write_wav(path: Path, samples: list[int]) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(struct.pack(f"<{len(samples)}h", *[max(-32768, min(32767, s)) for s in samples]))
    return path


def room(seconds: float, amp: int = 6) -> list[int]:
    rnd = random.Random(5)
    return [rnd.randint(-amp, amp) for _ in range(int(seconds * RATE))]


def clip(name: str) -> list[int]:
    return read_wav(FIX / name)


def scenario(tmp: Path, name: str, parts: list) -> Path:
    """("room", seconds) | ("clip", file name): a lead-in the page and worker can load in, then the parts."""
    out: list[int] = []
    for kind, v in parts:
        out += room(v) if kind == "room" else clip(v)
    return write_wav(tmp / f"{name}.wav", out)


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

    port = int(os.environ.get("HUD_V2_CHECK_PORT", "18498"))
    mock = MockDaemon(port).start()
    _seed(mock)
    w = mock.world
    tmp = Path(tempfile.mkdtemp(prefix="wake_v11_"))
    base = f"http://127.0.0.1:{mock.port}"
    served: list[tuple[str, str, int, str]] = []   # (url, content-type, status, CSP) of the wake assets

    def run(section):
        if only and section.__name__ not in only:
            return
        print(f"\n{section.__name__}")
        try:
            section()
        except Exception as e:   # a section that cannot run is a failure, the rest still run
            check(f"{section.__name__} ran to the end", False, str(e).splitlines()[0][:240])

    try:
        with sync_playwright() as pw:

            def new_context(browser, init=""):
                ctx = browser.new_context(viewport={"width": 1600, "height": 900})

                def refuse(r):
                    record(f"request outside isolated mock: {r.request.url}")
                    r.abort()
                ctx.route(re.compile(r"^https?://(?!(127\.0\.0\.1|localhost)[:/])"), refuse)
                guard_live(ctx, record)
                watch_csp(ctx, record)
                ctx.add_init_script(CHROME_WATCH + init)
                ctx.on("response", lambda r: served.append(
                    (r.url, r.headers.get("content-type", ""), r.status, r.headers.get("content-security-policy", "")))
                    if re.search(r"/assets/.*\.(onnx|wasm|txt)$|/assets/worker-.*\.js$", r.url) else None)
                return ctx

            def open_page(ctx, wait_ready=True, hook_driven=False):
                page = ctx.new_page()
                page.set_default_timeout(10000)
                page.on("pageerror", lambda e: record(f"page error {e}"))
                page.goto(base)
                page.wait_for_selector('[data-testid="sidebar"]', state="attached")
                if not hook_driven:
                    page.wait_for_function("() => window.__hud && window.__hud.voiceDiagnostics().mic === 'available'")
                    page.wait_for_function("() => window.__hud.voiceDiagnostics().path !== 'unknown'")
                    if wait_ready:
                        page.wait_for_function("() => window.__hud.mic.wakeEngine().state === 'ready'", timeout=40000)
                    page.locator('[data-testid="tab-chat"]').click()
                    page.locator('[data-testid="thread-t1"]').click()
                    page.locator('[data-testid="dictation-review"]').click()
                return page

            def eng(page):
                return page.evaluate("window.__hud.mic.wakeEngine()")

            def rows(page):
                return page.evaluate("window.__hud.voiceDiagnostics().rows")

            # =============================================================
            # mic: real microphone path, synthetic speech
            # =============================================================
            def mic_negative():
                browser = pw.chromium.launch(headless=True, args=MIC_ARGS + [
                    "--use-file-for-fake-audio-capture=" + str(scenario(tmp, "neg", [
                        ("room", 14), ("clip", "kws_neg_00_am_adam.wav"), ("room", 4), ("clip", "kws_neg_01_am_adam.wav"), ("room", 4),
                        ("clip", "kws_neg_09_am_adam.wav"), ("room", 30)])) + "%noloop"])
                ctx = new_context(browser)
                page = open_page(ctx)
                st = eng(page)
                check("the wake word is listening in this window", st["state"] == "ready" and page.evaluate("window.__hud.mic.wakeRunning()"), str(st))
                stt0 = len(mock.sent("POST", "/stt"))
                # Three sentences that sound a little like it. The file started when the microphone opened.
                heard = until(lambda: sum(1 for x in rows(page) if x["outcome"] == "unclaimed") >= 3, timeout=60)
                check("three sentences with Travis / jar / cancel in them were heard as speech and dropped, unclaimed", bool(heard),
                      str([x["outcome"] for x in rows(page)]))
                time.sleep(1.0)
                st = eng(page)
                check("none of them woke it", st["wakeHits"] == 0, str(st))
                check("and nothing was uploaded", len(mock.sent("POST", "/stt")) == stt0)
                check("Chrome's speech recognizer was never constructed", page.evaluate("window.__srConstructed") == 0)
                ctx.close(); browser.close()

            def mic_wake():
                # Four synthetic takes (the model's recall is ~92% per take and depends on where a word falls against its
                # 320 ms chunks, so one miss in four is within what it measures): at least three must wake it, and each hit
                # must have been claimed and uploaded, no more and no fewer.
                parts = [("room", 14), ("clip", "kws_pos_00_am_adam.wav"), ("room", 11), ("clip", "kws_pos_00_bf_emma.wav"), ("room", 11),
                         ("clip", "kws_pos_11_am_adam.wav"), ("room", 11), ("clip", "kws_pos_07_bf_emma.wav"), ("room", 12)]
                browser = pw.chromium.launch(headless=True, args=MIC_ARGS + [
                    "--use-file-for-fake-audio-capture=" + str(scenario(tmp, "pos", parts)) + "%noloop"])
                ctx = new_context(browser)
                served.clear()
                page = open_page(ctx)
                mock.stt_script = []
                stt0 = len(mock.sent("POST", "/stt"))
                until(lambda: len(rows(page)) >= 4 or eng(page)["wakeHits"] >= 4, timeout=110)
                time.sleep(3.0)
                st = eng(page)
                hits = st["wakeHits"]
                check("at least three of four synthetic 'Jarvis' takes (two voices, one in a sentence, one with a command) woke it", hits >= 3, str(st))
                got = until(lambda: len(mock.sent("POST", "/stt")) - stt0 >= hits, timeout=30)
                check("each hit was claimed as a message and uploaded to the local /stt, and nothing else was",
                      bool(got) and len(mock.sent("POST", "/stt")) - stt0 == hits, f"{len(mock.sent('POST', '/stt')) - stt0} uploads, {hits} hits")
                srcs = [x["source"] for x in rows(page)]
                check("the diagnostics name the source: wake, once per hit", srcs.count("wake") == hits, str(srcs))
                print(f"     ({hits} of 4 woke it; the others were heard as speech and dropped unclaimed)")
                reqs = page.evaluate("window.__reqs")
                check("every request the page made went to the mock daemon, none to a speech service",
                      not [u for u in reqs if not u.startswith(base) and not u.startswith("/")] and not external, str(reqs[:6]) + str(external))
                check("Chrome's speech recognizer was never constructed", page.evaluate("window.__srConstructed") == 0)
                check("and there is no recognizer handle to stream the room", page.evaluate("!window.__hudRecog && !window.__sr"))
                # The model, wasm and worker come from the HUD's origin under its CSP.
                models = {u: (ct, s, p) for u, ct, s, p in list(served) if u.endswith(".onnx") and "silero" not in u}
                check("the keyword model files are served by the HUD's own origin under /assets",
                      len(models) >= 3 and all(u.startswith(base + "/assets/") for u in models), str(list(models)))
                # Grant rule (owner, 2026-10-15; review of the same day): 'wasm-unsafe-eval'
                # only on a worker's own fetch of a HUD script (Sec-Fetch-Dest: worker), the
                # policy that worker runs under; the page's import of the `?worker` wrapper,
                # the model and the .wasm carry the page's. Playwright hides a worker
                # request's headers, so the server's record (`mock.script_fetches`) says
                # which fetches were a worker's.
                from jarvis.v2 import hud_api as H
                page_csp, grant = H.content_security_policy(), H.content_security_policy(wasm=True)
                path = lambda u: u.split("?")[0][len(base):].lstrip("/")
                bad = [(u, p) for u, *_, p in list(served) if p not in (page_csp, grant) or (p == grant and not H.wasm_script(path(u)))]
                bad += [f for f in list(mock.script_fetches) if f[2] != (f[1] == "worker")]
                granted = [u for u, *_, p in list(served) if p == grant]
                check("every wake asset carries the policy the daemon gives it: worker-src 'self', wasm-unsafe-eval "
                      "on a worker's fetch of its script only, never unsafe-eval",
                      bool(granted) and not bad and all("worker-src 'self'" in p and " 'unsafe-eval'" not in p for *_, p in list(served)),
                      f"granted={granted} bad={bad[:2]}")
                sha = lambda u: page.evaluate("""async (u) => { const b = new Uint8Array(await (await fetch(u)).arrayBuffer());
                  const d = await crypto.subtle.digest('SHA-256', b); return Array.from(new Uint8Array(d), x => x.toString(16).padStart(2, '0')).join(''); }""", u)
                tokens_urls = {u for u, *_ in list(served) if u.endswith(".txt")}     # (a snapshot: fetching them adds to `served`)
                digests = {sha(u) for u in models} | {sha(u) for u in tokens_urls}
                pinned = {v[2] for v in PINS.values()}
                check("the files the page received are the pinned ones (SHA-256)", pinned <= digests, str(digests ^ pinned))
                vendored = {k: hashlib.sha256((HUD / "vendor" / "kws-zipformer-gigaspeech-3.3M" / (v[0] + (".onnx" if k != "tokens" else ".txt"))).read_bytes()).hexdigest()
                            for k, v in PINS.items()}
                check("which are the vendored files", all(vendored[k] == v[2] for k, v in PINS.items()))
                open_settings(page, "Voice")
                line = page.locator('[data-testid="wake-status"]').inner_text()
                check("Settings -> Voice says it is on, local, and sends nothing", "on" in line and "nothing sent anywhere" in line, line)
                check("with its cost", re.search(r"about [0-9.]+% of one core", line) is not None, line)
                print(f"     ({line})")
                print(f"     (loaded in {eng(page)['loadMs']:.0f} ms)")
                page.locator('[data-testid="settings-close"]').click()
                ctx.close(); browser.close()

            def cpu():
                """Renderer CPU over 40 s of the same synthetic speech, with the wake word and with it failed."""
                parts = [("room", 14), ("clip", "kws_neg_00_am_adam.wav"), ("room", 3), ("clip", "kws_neg_01_am_adam.wav"), ("room", 3),
                         ("clip", "kws_neg_09_am_adam.wav"), ("room", 3), ("clip", "kws_pos_11_am_adam.wav"), ("room", 60)]
                wav = scenario(tmp, "cpu", parts)

                def measure(block_wake: bool):
                    browser = pw.chromium.launch(headless=True, args=MIC_ARGS + ["--use-file-for-fake-audio-capture=" + str(wav) + "%noloop"])
                    ctx = new_context(browser)
                    if block_wake:
                        ctx.route(re.compile(r".*/assets/encoder\.int8-.*\.onnx"), lambda r: r.fulfill(status=404, body="no"))
                    page = ctx.new_page()
                    page.on("pageerror", lambda e: record(f"page error {e}"))
                    page.goto(base)
                    page.wait_for_selector('[data-testid="sidebar"]', state="attached")
                    page.wait_for_function("() => window.__hud && window.__hud.voiceDiagnostics().mic === 'available'")
                    page.wait_for_function("() => window.__hud.mic.detector().state === 'ready'", timeout=40000)
                    want = "failed" if block_wake else "ready"
                    page.wait_for_function(f"() => window.__hud.mic.wakeEngine().state === {json.dumps(want)}", timeout=40000)
                    cdp = browser.new_browser_cdp_session()

                    def total():
                        info = cdp.send("SystemInfo.getProcessInfo")["processInfo"]
                        return sum(p["cpuTime"] for p in info)
                    time.sleep(2.0)
                    t0, c0 = time.time(), total()
                    time.sleep(40.0)
                    pct = 100 * (total() - c0) / (time.time() - t0)
                    st = eng(page)
                    ctx.close(); browser.close()
                    return pct, st

                with_wake, st1 = measure(False)
                without, st2 = measure(True)
                print(f"     (whole browser, 40 s: {with_wake:.1f}% of a core with the wake word, {without:.1f}% with it failed; "
                      f"the worker's own time {st1['cpuPct']:.1f}%)")
                check("the wake word's worker spends only a few percent of a core on the network, by its own clock", 0 < st1["cpuPct"] < 12, str(st1))
                check("and the whole browser stays within a few points of the same page without it", with_wake - without < 12, f"{with_wake:.1f} vs {without:.1f}")
                check("the failed-wake page really had no wake word", st2["state"] == "failed" and st2["code"] == "fetch", str(st2))

            run(mic_negative)
            run(mic_wake)
            run(cpu)

            # =============================================================
            # the page-driven layers (no microphone: the acoustic simulator pushes frames)
            # =============================================================
            browser = pw.chromium.launch(headless=True, args=SIM_ARGS)
            context = new_context(browser, SOURCES + SIM)
            page = context.new_page()
            page.set_default_timeout(8000)
            page.on("pageerror", lambda e: record(f"page error {e}"))
            before = mock.sse_connections()
            page.goto(base)
            page.wait_for_selector('[data-testid="sidebar"]', state="attached")
            mock.await_reconnect(before)

            wav_own = (FIX / "kws_pos_11_am_adam.wav").read_bytes()        # Jarvis's reply: "Hey Jarvis, can you hear me?"
            # Jarvis's reply opens with him saying "Jarvis, stop." and goes on for another 15 s.
            wav_stop = write_wav(tmp / "stop_long.wav", read_wav(FIX / "kws_pos_10_am_adam.wav") + [0] * 8000 + read_wav(FIX5 / "kokoro_a.wav") * 3).read_bytes()
            a = read_wav(FIX5 / "kokoro_a.wav")                              # a long reply with no keyword in it: six repeats, ~30 s
            wav_long = write_wav(tmp / "long.wav", a * 6).read_bytes()
            LONG = "Here is a long answer with no stopping point in it"
            owner = {n: samples_of(FIX / f"{n}.wav") for n in
                     ("kws_pos_00_bf_emma", "kws_pos_07_bf_emma", "kws_pos_08_bf_emma", "kws_pos_09_bf_emma", "kws_pos_10_bf_emma",
                      "kws_pos_07_am_adam", "kws_pos_08_am_adam", "kws_pos_09_am_adam", "kws_pos_10_am_adam",
                      "kws_neg_00_am_adam", "kws_pos_00_am_adam")}

            def speech():
                return page.evaluate("window.__hud.speech()")

            def state():
                return speech()["view"]["state"]

            def stts():
                return mock.sent("POST", "/stt")

            def interrupts():
                return [c for c in mock.calls if c[0] == "POST" and c[1].endswith("/interrupt")]

            def approvals_posted():
                return [c for c in mock.calls if c[0] == "POST" and (c[1].startswith("/approvals/") or "/approve" in c[1])]

            def waits(pred, timeout=6.0):
                return bool(until(pred, timeout=timeout))

            def started():
                return page.evaluate("window.__started")

            def reset(wav=None, mode="review"):
                mock.stt_script = []
                w.pop("send_status", None)
                mock.say_wav = wav
                mock.ms_per_char = 60
                _fresh(page, mock, until, TWO)
                _open(page, until, 1, "t1", "p1")
                _open(page, until, 2, "t2", "p1")
                _focus(page, until, 1)
                _dictation(page, mode)
                page.evaluate("() => window.dispatchEvent(new Event('focus'))")
                waits(lambda: speech()["speaker"], 3)
                page.evaluate(QUIET)

            def sim_start(delay=120, gain=0.5):
                page.evaluate(f"() => {{ const s = window.__sim; s.delay = {delay}; s.gain = {gain}; s.owner = null; s.run(); }}")
                # the first frame starts the wake word; wait for the model
                if not waits(lambda: eng(page)["state"] == "ready", 40):
                    raise RuntimeError(f"the wake word did not load: {eng(page)}")

            def sim_stop():
                page.evaluate("() => { clearInterval(window.__sim.timer); window.__sim.timer = null; window.__sim.owner = null; }")

            def say_reply(text=REPLY, thread="t1"):
                mock.emit("text", {"text": text}, thread_id=thread)

            def playing():
                return waits(lambda: state() == "playing", 6)

            def stop_speech():
                if page.locator('[data-testid="speech-stop"]').count() and page.locator('[data-testid="speech-stop"]').is_enabled():
                    page.locator('[data-testid="speech-stop"]').click()
                waits(lambda: state() == "idle", 3)

            def learn_room():
                """A reply with only his voice in it, until the gate has learned the room (V9), then it is stopped."""
                say_reply(LONG)
                playing()
                if not waits(lambda: speech()["selfVoice"]["lock"] == "echo", 12):
                    raise RuntimeError(f"the gate did not learn the room: {speech()['selfVoice']}")
                stop_speech()
                time.sleep(1.0)

            def reading(seconds=2.0):
                """A fresh long reply, playing for a while (the gate hears his voice before the owner speaks over it)."""
                say_reply(LONG)
                if not playing():
                    raise RuntimeError("no reply was playing")
                time.sleep(seconds)

            def owner_says(name):
                page.evaluate("(s) => window.__sim.say(s)", owner[name])

            def command(name, done, tries=4, wait=6.0):
                """The owner says a command, again if it was not heard (he would): how many times it took, 0 if never."""
                for n in range(1, tries + 1):
                    owner_says(name)
                    if waits(done, wait):
                        return n
                return 0

            # ============ own: Jarvis's own voice saying "Jarvis" ====================
            def own_voice():
                reset(wav_own)
                sim_start(gain=0.6)
                page.evaluate("window.__hud.capture.openFollowUp()")        # a lure: a window that claims anything that opens
                hits0, st0, s0 = eng(page)["wakeHits"], len(stts()), len(_sent(mock, "t1"))
                page.evaluate("() => { window.__watch.openFrames = 0; window.__watch.states = {}; }")
                say_reply("Hey Jarvis can you hear me")
                check("the reply plays on the real Web Audio player", playing())
                waits(lambda: state() == "idle", 12)
                time.sleep(2.5)       # the spotter reports a word up to a second after it ended
                st = eng(page)
                check("the keyword model really heard his voice say 'Jarvis' (the lure was real)", st["wakeHits"] > hits0, str(st))
                wt = page.evaluate("window.__watch")
                check("and nothing woke: his voice alone opened no utterance", wt["openFrames"] == 0, str(wt))
                check("no claim, no recording", page.evaluate("window.__hud.capture.segClaimed") is False
                      and page.evaluate("window.__hud.capture.recording()") is False)
                check("it was never paused", "paused" not in wt["states"], str(wt["states"]))
                check("nothing was uploaded and nothing sent", len(stts()) == st0 and len(_sent(mock, "t1")) == s0)
                rs = rows(page)
                check("the diagnostics name the ignored hit: self_voice, source wake",
                      any(x["outcome"] == "self_voice" and x["source"] == "wake" for x in rs), str([(x["source"], x["outcome"]) for x in rs]))
                owner_says("kws_neg_00_am_adam")
                time.sleep(5.5)
                check("it is not kept for the owner's next words: speaking right after claims nothing",
                      len(stts()) == st0 and not any(x["source"] == "wake" and x["outcome"] != "self_voice" for x in rows(page)),
                      str([(x["source"], x["outcome"]) for x in rows(page)]))
                sim_stop()

            def owner_wins():
                reset(wav_long)
                sim_start()
                # Learn the room from a reply with only his voice in it, as V9's check does.
                learn_room()
                mock.stt_script = [{"text": "run the tests please"}]
                st0, h0 = len(stts()), eng(page)["wakeHits"]
                owner_says("kws_pos_00_bf_emma")                              # the owner says 'Jarvis.' with no reply playing
                got = waits(lambda: len(stts()) > st0, 12)
                check("the owner saying 'Jarvis' claims a message and uploads it", got and eng(page)["wakeHits"] > h0, f"stt+{len(stts()) - st0} {eng(page)}")
                check("recorded as a wake claim", any(x["source"] == "wake" for x in rows(page)), str([(x['source'], x['outcome']) for x in rows(page)]))
                check("without ever touching Chrome's recognizer", page.evaluate("window.__srConstructed") == 0)
                sim_stop()

            # ============ card ======================================================
            def card():
                reset(wav_long)
                sim_start(gain=0.25)
                learn_room()
                check("by default the owner's switch for commands under a card is on",
                      page.evaluate("localStorage.getItem('jarvis.wakeCardCommands')") in (None, "on"))
                reading()
                a0, st0, s0, i0 = len(approvals_posted()), len(stts()), len(_sent(mock, "t1")), len(interrupts())
                _approval(mock, "v11card1")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=4)
                check("a card up switches the recognizer to the four commands", waits(lambda: eng(page)["set"] == "commands", 5), str(eng(page)))
                mock.stt_script = [{"text": "Jarvis approve"}] * 4
                r0 = page.evaluate("window.__hud.voiceDiagnostics().rows.length")
                # The owner close to the microphone, his voice from the speakers across the room (and partly cancelled):
                # speech over speech at about 0 dB is not recognised by this model (V0: 62% at 10 dB), so this is not that case.
                page.evaluate("() => { window.__sim.ownerGain = 3; }")
                owner_says("kws_pos_00_bf_emma")                                  # 'Jarvis.' alone under a card
                time.sleep(4.0)
                check("'Jarvis' alone under a card claims nothing, uploads nothing, answers nothing",
                      len(stts()) == st0 and len(approvals_posted()) == a0 and page.evaluate("window.__hud.capture.recording()") is False
                      and page.locator('[data-testid="approval-card"]').count() == 1, f"stt+{len(stts()) - st0}")
                check("and does not move his voice", state() == "playing", state())
                reading(0.5)                                                       # a fresh reply: this one is half spent
                tries = command("kws_pos_07_am_adam", lambda: state() == "paused")        # 'Jarvis, pause.'
                check("'Jarvis, pause.' under a card pauses him, by local recognition", tries > 0, f"{state()} {eng(page)}")
                print(f"     (heard on attempt {tries})")
                check("with the card still up and its recognizer a command, not a transcript",
                      page.locator('[data-testid="approval-card"]').count() == 1 and eng(page)["commandHits"] >= 1, str(eng(page)))
                time.sleep(3.0)
                check("(he is paused)", state() == "paused", state())
                tries = command("kws_pos_08_am_adam", lambda: state() == "playing")       # 'Jarvis, resume.'
                check("'Jarvis, resume.' resumes him", tries > 0, state())
                print(f"     (heard on attempt {tries})")
                time.sleep(1.5)
                check("(he is reading again)", state() == "playing", state())
                tries = command("kws_pos_09_am_adam", lambda: state() == "idle")          # 'Jarvis, skip.'
                check("'Jarvis, skip.' skips the rest of what he was saying", tries > 0, state())
                print(f"     (heard on attempt {tries})")
                reading()
                check("(a new reply is playing)", state() == "playing", state())
                tries = command("kws_pos_10_am_adam", lambda: state() == "idle")          # 'Jarvis, stop.'
                check("'Jarvis, stop.' stops him and clears the queue", tries > 0, state())
                print(f"     (heard on attempt {tries})")
                check("through all of it: zero approval requests, zero uploads, nothing sent, nothing interrupted, the card still up",
                      len(approvals_posted()) == a0 and len(stts()) == st0 and len(_sent(mock, "t1")) == s0 and len(interrupts()) == i0
                      and page.locator('[data-testid="approval-card"]').count() == 1,
                      f"approvals+{len(approvals_posted()) - a0} stt+{len(stts()) - st0} interrupts+{len(interrupts()) - i0}")
                check("and nothing was recorded for the capture: the card suppresses it", page.evaluate("window.__hud.capture.recording()") is False)
                _ = r0
                page.keyboard.press("Escape")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
                check("when the card goes the recognizer is the wake word again", waits(lambda: eng(page)["set"] == "wake", 5), str(eng(page)))
                sim_stop()

                # With the owner's switch off, nothing is obeyed under a card.
                reset(wav_long)
                open_settings(page, "Voice")
                page.locator('[data-testid="wake-card-commands"]').uncheck()
                page.locator('[data-testid="settings-close"]').click()
                check("the choice is remembered in this window", page.evaluate("localStorage.getItem('jarvis.wakeCardCommands')") == "off")
                sim_start()
                say_reply(LONG)
                playing()
                time.sleep(1.5)
                _approval(mock, "v11card2")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=4)
                time.sleep(0.8)
                check("with the switch off a card leaves the recognizer on the wake word", eng(page)["set"] == "wake", str(eng(page)))
                owner_says("kws_pos_10_bf_emma")
                time.sleep(5.0)
                check("and 'Jarvis, stop.' is not obeyed", state() == "playing" and eng(page)["commandHits"] == 0, f"{state()} {eng(page)}")
                page.keyboard.press("Escape")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
                stop_speech()
                sim_stop()
                open_settings(page, "Voice")
                page.locator('[data-testid="wake-card-commands"]').check()
                page.locator('[data-testid="settings-close"]').click()

                # His own voice saying the command is not obeyed.
                reset(wav_stop)
                sim_start(gain=0.6)
                c0 = eng(page)["commandHits"]
                say_reply("Jarvis stop and then some more words")
                playing()
                _approval(mock, "v11card3")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=4)
                heard = waits(lambda: eng(page)["commandHits"] > c0, 8)
                time.sleep(2.0)
                check("the keyword model heard his voice say 'Jarvis, stop.' under the card (the lure was real)", heard, str(eng(page)))
                check("and it was not obeyed: he is still reading", state() == "playing", state())
                check("nor recorded as a command", not any(x["outcome"] == "command" for x in rows(page)), str([x["outcome"] for x in rows(page)]))
                page.keyboard.press("Escape")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
                stop_speech()
                sim_stop()

            run(own_voice)
            run(owner_wins)
            run(card)

            # =============================================================
            # settings and the visible fallbacks
            # =============================================================
            def settings():
                reset()
                sim_start()
                open_settings(page, "Voice")
                line = page.locator('[data-testid="wake-status"]').inner_text()
                check("Settings -> Voice shows the wake word on, local, sending nothing", "Local wake word: on" in line and "nothing sent anywhere" in line, line)
                body = page.locator('[data-testid="wake-word"]').inner_text()
                check("it says Chrome's cloud recognizer is retired and avatars' other phrases are not listened for",
                      "retired" in body and "other wake phrases" in body, body[-260:])
                sel = page.locator('[data-testid="wake-sensitivity"]')
                check("sensitivity defaults to Normal", sel.input_value() == "normal" and eng(page)["sensitivity"] == "normal")
                sel.select_option("high")
                check("High reaches the engine and is remembered", waits(lambda: eng(page)["sensitivity"] == "high", 3)
                      and page.evaluate("localStorage.getItem('jarvis.wakeSensitivity')") == "high")
                sel.select_option("low")
                check("Low too", waits(lambda: eng(page)["sensitivity"] == "low", 3))
                sel.select_option("normal")
                check("the commands-under-a-card switch is on by default", page.locator('[data-testid="wake-card-commands"]').is_checked())
                # OFF mutes it
                page.locator('[data-testid="settings-close"]').click()
                _dictation(page, "off")
                time.sleep(0.5)
                check("mic OFF: nothing is listened to or posted", page.evaluate("window.__hud.mic.wakeRunning()") is False)
                open_settings(page, "Voice")
                line = page.locator('[data-testid="wake-status"]').inner_text()
                check("and Settings says so", "off while the microphone is muted" in line, line)
                page.locator('[data-testid="settings-close"]').click()
                _dictation(page, "review")
                check("REVIEW brings it back", waits(lambda: page.evaluate("window.__hud.mic.wakeRunning()") is True, 3))
                sim_stop()

            def fallbacks():
                served_models = re.compile(r".*/assets/(encoder|decoder|joiner)\.int8-[^/]*\.onnx$")
                served_tokens = re.compile(r".*/assets/tokens-[^/]*\.txt$")

                def flipped(r):
                    resp = r.fetch()
                    body = bytearray(resp.body())
                    body[len(body) // 2] ^= 1
                    r.fulfill(response=resp, body=bytes(body))

                def short(r):
                    resp = r.fetch()
                    r.fulfill(response=resp, body=resp.body()[:-1])

                def tokens_changed(r):
                    resp = r.fetch()
                    r.fulfill(response=resp, body=resp.body() + b" ")

                cases = [
                    ("a changed byte in the joiner", served_models, lambda r: flipped(r) if "joiner" in r.request.url else r.fallback(), "hash"),
                    ("a file one byte short", served_models, lambda r: short(r) if "encoder" in r.request.url else r.fallback(), "hash"),
                    ("a changed tokens file", served_tokens, tokens_changed, "hash"),
                    ("a missing decoder", served_models, lambda r: r.fulfill(status=404, body="no") if "decoder" in r.request.url else r.fallback(), "fetch"),
                ]
                for label, pattern, handler, code in cases:
                    page.route(pattern, handler)
                    reset()
                    page.evaluate("() => { window.__sim.run(); }")
                    got = waits(lambda: eng(page)["state"] == "failed", 40)
                    st = eng(page)
                    check(f"{label}: the wake word is unavailable, with the reason {code}", got and st["code"] == code, str(st))
                    check("  and nothing is listening", page.evaluate("window.__hud.mic.wakeRunning()") is False)
                    open_settings(page, "Voice")
                    line = page.locator('[data-testid="wake-status"]').inner_text()
                    check("  Settings -> Voice says so, in words from the closed set, with the way back",
                          "unavailable" in line and ("checksum" in line or "could not be loaded" in line) and "talk key" in line
                          and page.locator('[data-testid="wake-retry"]').count() == 1, line)
                    check("  without a URL, a path or engine text", not re.search(r"https?:|/assets|\.onnx|Error", line), line)
                    page.locator('[data-testid="settings-close"]').click()
                    # The owner still talks to him: the talk key records and uploads.
                    st0 = len(stts())
                    mock.stt_script = [{"text": "typed by voice anyway"}]
                    page.evaluate("window.__hud.capture.press(); window.__hud.mic.feedMs(1200, .06); window.__hud.capture.release()")
                    check("  and the talk key still dictates", waits(lambda: len(stts()) > st0, 8))
                    # Try again, with the fault gone, restores it.
                    page.unroute(pattern, handler)
                    page.evaluate("window.__hud.mic.retryWake()")
                    check("  Try again, the fault gone, restores it", waits(lambda: eng(page)["state"] == "ready", 40), str(eng(page)))
                    sim_stop()

                # A window that cannot construct a Worker.
                ctx2 = new_context(browser, "window.Worker = class { constructor() { throw new Error('refused'); } };")
                p2 = ctx2.new_page()
                p2.on("pageerror", lambda e: record(f"page error {e}"))
                p2.goto(base)
                p2.wait_for_selector('[data-testid="sidebar"]', state="attached")
                p2.evaluate("() => window.__hud.mic.frame(new Float32Array(512))")
                st = until(lambda: p2.evaluate("window.__hud.mic.wakeEngine()") if p2.evaluate("window.__hud.mic.wakeEngine().state === 'failed'") else None, timeout=10)
                check("a window that cannot start a Worker says so", bool(st) and st["code"] == "worker", str(st))
                ctx2.close()

            run(settings)
            run(fallbacks)

            check("no page errors", not [f for f in failures if f.startswith("page error")], "; ".join(failures[:3]))
            check("no CSP violation and no request outside the mock, anywhere in this run", not external, str(external[:3]))
            context.close()
            browser.close()
    finally:
        mock.stop()
    print()
    if failures:
        print(f"{len(failures)} of {count} checks FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"all {count} wake-word checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
