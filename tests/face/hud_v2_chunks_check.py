"""V4 voice capture through the real HUD: the AudioWorklet, the 16 kHz resampler,
the uncapped recorder and chunked STT.

Decisions V-1 and V-11, plan §3.1, §3.5:
  - the microphone reaches the capture through an AudioWorklet (a bundled,
    same-origin module under the daemon's CSP), 16 kHz mono, and the sample
    clock really runs at 16 kHz; without a worklet the same resampler runs behind
    the old ScriptProcessor and the window says so;
  - there is no 30 s cap and no 40 s ring limit: a 45 s push-to-talk and a 36 s
    hands-free dictation are transcribed whole, in order, as one message, the
    overlap of a hard cut said once;
  - chunks are uploaded at most two at a time; a failed chunk is kept and
    retryable while the others are placed (in the box, never sent);
  - the mic OFF and a card upload nothing, a card appearing mid-message stops
    the uploads at once.

Isolated fake microphones (an oscillator through the real worklet for the
real-time sections; scripted frames for the long ones) and a scripted `/stt` on
ephemeral ports: no real audio, external call, credential or live daemon port.
Chromium runs with --autoplay-policy=no-user-gesture-required so the worklet runs
in a headless page. Run alone: `python tests/face/hud_v2_chunks_check.py [section,section]`
with a temporary HOME, dummy keys, the worktree PYTHONPATH and a 3G/0swap scope.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
assert not (REPO / ".env").exists(), "use a credential-free worktree"
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.face.hud_v2_check import FAKE_RECOGNIZER, guard_live, open_settings, until, watch_csp  # noqa: E402
from tests.face.hud_v2_layout_check import _approval  # noqa: E402
from tests.face.hud_v2_mock import MockDaemon  # noqa: E402
from tests.face.hud_v2_multichat_check import _dictation, _sent  # noqa: E402
from tests.face.hud_v2_voice_diagnostics_check import FAKE_MIC  # noqa: E402
try:  # speak only once the capture is listening (the dictation flake)
    from tests.face.hud_voice import speak_when_listening
except ImportError:  # run as a script from tests/face
    from hud_voice import speak_when_listening  # type: ignore[no-redef]

CHROME_ARGS = ["--autoplay-policy=no-user-gesture-required"]

# A microphone the page drives: an oscillator pair behind a gain, through a
# MediaStreamDestination, so the capture hears it through the real worklet.
# `__fm.speak([[ms, gain], ...])` schedules a level pattern from now.
SPEECH_MIC = """
window.__gumCalls = [];
window.__fm = null;
Object.defineProperty(navigator.mediaDevices, 'getUserMedia', { value: async constraints => {
  window.__gumCalls.push(constraints);
  const ctx = new AudioContext(); const dest = ctx.createMediaStreamDestination();
  const gain = ctx.createGain(); gain.gain.value = 0; gain.connect(dest);
  for (const hz of [180, 540]) { const o = ctx.createOscillator(); o.frequency.value = hz; o.connect(gain); o.start(); }
  window.__fm = { ctx, speak(pattern) {
    let t = ctx.currentTime + 0.05;
    for (const [ms, v] of pattern) { gain.gain.setValueAtTime(v, t); t += ms / 1000; }
    gain.gain.setValueAtTime(0, t); return t - ctx.currentTime; } };
  dest.stream.getAudioTracks()[0].getSettings = () => ({ echoCancellation: true, noiseSuppression: false, autoGainControl: false });
  return dest.stream;
} });
"""

# Speech as a person makes it: words with a breath between, so the detector's
# floor does not walk up under a constant level.
WORDS = """(n, pauseEvery, pauseMs) => { const m = window.__hud.mic;
  for (let i = 0; i < n; i++) { m.feedMs(900, .06); m.feedMs(100, .0009);
    if (pauseEvery && (i + 1) % pauseEvery === 0 && i + 1 < n) m.feedMs(pauseMs, .0009); } }"""


class Stt:
    """A scripted `/stt` for one page: text by chunk position, so two uploads in
    flight cannot swap their answers. A tuple ("err", status) fails that chunk."""

    def __init__(self):
        self.reqs: list[dict] = []
        self.script: list = []
        self.default = "default words"

    def reset(self, *script):
        self.reqs.clear()
        self.script = list(script)

    def handle(self, route):
        req = route.request
        q = {k: v[0] for k, v in parse_qs(urlsplit(req.url).query).items()}
        body = req.post_data_buffer or b""
        seq, utt = int(q["seq"]), int(q["utterance_id"])
        self.reqs.append({"seq": seq, "utt": utt, "ms": max(0, len(body) - 44) / 2 / 16000 * 1000,
                          "budget": q.get("budget_ms"), "bytes": len(body)})
        at = seq - utt
        spec = self.script[at] if at < len(self.script) else self.default
        if isinstance(spec, tuple):
            route.fulfill(status=spec[1], content_type="application/json", body=json.dumps({"error": "scripted failure"}))
        else:
            route.fulfill(content_type="application/json", body=json.dumps({"text": spec}))


def main(argv=None):
    only = set(filter(None, ((argv if argv is not None else sys.argv[1:]) or [""])[0].split(",")))
    failures: list[str] = []
    count = 0

    def check(name, ok, detail=""):
        nonlocal count
        count += 1
        print(("ok   " if ok else "FAIL ") + name + ("" if ok or not detail else f"  [{detail}]"))
        if not ok:
            failures.append(name)

    mock = MockDaemon(0).start()
    try:
        base = f"http://127.0.0.1:{mock.port}"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=CHROME_ARGS)

            def new_context(init: str):
                ctx = browser.new_context(viewport={"width": 1400, "height": 900})

                def refuse(r):
                    failures.append(f"request outside isolated mock: {r.request.url}")
                    r.abort()
                ctx.route(re.compile(r"^https?://(?!(127\.0\.0\.1|localhost)[:/])"), refuse)
                guard_live(ctx, failures.append)
                watch_csp(ctx, failures.append)
                # V5: this suite's "speech" is a pair of oscillators, which the Silero VAD
                # (correctly) does not call speech. It exercises the capture, worklet and
                # chunks on the energy detector, which the model failing to load selects,
                # visibly. Silero on real speech is tests/face/hud_v2_vad_check.py.
                ctx.route(re.compile(r"/assets/silero_vad[^/]*\.onnx$"), lambda route: route.fulfill(status=404, body="energy-only suite"))
                ctx.add_init_script(FAKE_RECOGNIZER + init)
                return ctx

            def open_page(ctx, ready_path=True):
                page = ctx.new_page()
                page.set_default_timeout(8000)
                page.on("pageerror", lambda e: failures.append(f"page error {e}"))
                page.goto(base)
                page.wait_for_selector('[data-testid="sidebar"]', state="attached")
                page.wait_for_function("() => window.__hud && window.__hud.voiceDiagnostics().mic === 'available'")
                if ready_path:
                    page.wait_for_function("() => window.__hud.voiceDiagnostics().path !== 'unknown'")
                return page

            def run(section):
                if only and section.__name__ not in only:
                    return
                print(f"\n{section.__name__}")
                try:
                    section()
                except Exception as e:   # a section that cannot run is a failure, the rest still run
                    check(f"{section.__name__} ran to the end", False, str(e).splitlines()[0][:200])

            # ---------------------------------------------------------------
            # The real worklet, in real time
            # ---------------------------------------------------------------
            def worklet_realtime():
                ctx = new_context(SPEECH_MIC)
                page = open_page(ctx)
                stt = Stt()
                page.route("**/stt*", stt.handle)
                check("the microphone reaches the capture through an AudioWorklet",
                      page.evaluate("window.__hud.mic.path()") == "worklet", page.evaluate("window.__hud.voiceDiagnostics().pathNote"))
                a = page.evaluate("window.__hud.mic.state().written")
                page.wait_for_timeout(2000)
                b = page.evaluate("window.__hud.mic.state().written")
                rate = (b - a) / 2.0
                check("the sample clock runs at 16 kHz (the resampler is converting the device rate)",
                      14000 < rate < 18000, f"{rate:.0f} samples/s")
                page.evaluate("() => { window.__frames = []; window.__hud.mic.listen(f => window.__frames.push(f.length)); }")
                page.wait_for_timeout(1000)
                frames = page.evaluate("window.__frames")
                check("the frame seam V5 builds on delivers 512-sample frames, about 31 a second",
                      len(frames) > 20 and set(frames) == {512}, f"{len(frames)} frames of {set(frames)}")
                open_settings(page, "Voice")
                check("Settings -> Voice says which path is live",
                      "AudioWorklet" in page.locator('[data-testid="voice-capture-path"]').inner_text())
                page.locator('[data-testid="settings-close"]').click()
                check("a worklet in use raises no window error", page.evaluate("window.__hud.state().error") == "")

                # ~15 s of speech with one 900 ms breath after 8 s: cut at the breath, into two chunks.
                page.locator('[data-testid="tab-chat"]').click()
                page.locator('[data-testid="thread-t1"]').click()
                page.locator('[data-testid="dictation-review"]').click()
                stt.reset("alpha bravo charlie delta", "echo foxtrot golf hotel")
                pattern = []
                for i in range(9):
                    pattern += [[900, 0.1], [120, 0]]
                pattern += [[900, 0]]
                for i in range(4):
                    pattern += [[900, 0.1], [120, 0]]
                speak_when_listening(page, until, feed_js="window.__hud.capture.openFollowUp()")
                total = page.evaluate("(p) => window.__fm.speak(p)", pattern)
                check("setup: the dictation lasts about 14 s", 13 < total < 16, f"{total:.1f}")
                page.wait_for_function("() => document.querySelector('[data-testid=\"input\"]').value.includes('hotel')", timeout=45000)
                box = page.locator('[data-testid="input"]').first.input_value().strip()
                check("a real-time dictation through the worklet is cut at its breath into two chunks", len(stt.reqs) == 2,
                      str(stt.reqs))
                check("both belong to one utterance, in seq order", len({r["utt"] for r in stt.reqs}) == 1
                      and [r["seq"] for r in stt.reqs] == sorted(r["seq"] for r in stt.reqs))
                check("a cut at a breath repeats nothing", stt.reqs[0]["ms"] > 8000 and stt.reqs[0]["ms"] < 12000, str(stt.reqs))
                check("and the words come back as one message, in order", box == "alpha bravo charlie delta echo foxtrot golf hotel", box)
                check("one diagnostics row for the message",
                      page.evaluate("window.__hud.voiceDiagnostics().rows.length") == 1)
                page.locator('[data-testid="input"]').first.fill("")
                ctx.close()

            # ---------------------------------------------------------------
            # No AudioWorklet: degrade visibly
            # ---------------------------------------------------------------
            def worklet_unavailable():
                variants = {
                    "no AudioWorklet in the window": "Object.defineProperty(BaseAudioContext.prototype, 'audioWorklet', { get() { return undefined; } });",
                    "the module fails to load": "AudioWorklet.prototype.addModule = () => Promise.reject(new DOMException('private.example/x.js', 'AbortError'));",
                }
                for name, patch in variants.items():
                    ctx = new_context(patch + FAKE_MIC)
                    page = open_page(ctx)
                    stt = Stt()
                    page.route("**/stt*", stt.handle)
                    check(f"{name}: the older ScriptProcessor path runs", page.evaluate("window.__hud.mic.path()") == "script_processor")
                    err = page.evaluate("window.__hud.state().error")
                    check(f"{name}: the window says so", "older capture path" in err, err)
                    check(f"{name}: without leaking where it looked", "private.example" not in err
                          and "private.example" not in page.evaluate("JSON.stringify(window.__hud.voiceDiagnostics())"))
                    open_settings(page, "Voice")
                    shown = page.locator('[data-testid="voice-capture-path"]').inner_text()
                    check(f"{name}: Settings -> Voice shows the fallback", "ScriptProcessor" in shown, shown)
                    page.locator('[data-testid="settings-close"]').click()
                    a = page.evaluate("window.__hud.mic.state().written")
                    page.wait_for_timeout(2000)
                    rate = (page.evaluate("window.__hud.mic.state().written") - a) / 2.0
                    check(f"{name}: it still resamples to 16 kHz", 14000 < rate < 18000, f"{rate:.0f} samples/s")
                    stt.reset("fallback words")
                    page.locator('[data-testid="dictation-review"]').click()
                    page.evaluate("window.__hud.mic.feedMs(2000, .0009); window.__hud.capture.press(); window.__hud.mic.feedMs(1500, .06); window.__hud.capture.release()")
                    page.wait_for_function("() => document.querySelector('[data-testid=\"input\"]').value.includes('fallback words')")
                    check(f"{name}: dictation still works", len(stt.reqs) == 1)
                    ctx.close()

            # ---------------------------------------------------------------
            # Long messages, scripted frames
            # ---------------------------------------------------------------
            ctx = new_context(FAKE_MIC)
            page = open_page(ctx)
            stt = Stt()
            page.route("**/stt*", stt.handle)
            page.locator('[data-testid="tab-chat"]').click()
            page.locator('[data-testid="thread-t1"]').click()

            def box():
                return page.locator('[data-testid="input"]').first.input_value().strip()

            def clear_box():
                page.locator('[data-testid="input"]').first.fill("")

            def sends():
                return len(_sent(mock, "t1"))

            def rows():
                return page.evaluate("window.__hud.voiceDiagnostics().rows")

            def waits(pred, timeout=8.0):
                end = time.time() + timeout
                while time.time() < end:
                    if pred():
                        return True
                    page.wait_for_timeout(60)
                return False

            def ptt_long(words, pause_every=0, pause_ms=0):
                page.evaluate("window.__hud.mic.feedMs(2000, .0009)")
                page.evaluate("window.__hud.capture.press()")
                page.evaluate(f"({WORDS})({words}, {pause_every}, {pause_ms})")
                page.evaluate("window.__hud.capture.release()")

            def long_ptt_review():
                page.locator('[data-testid="dictation-review"]').click()
                clear_box()
                stt.reset("one two three four five six seven eight", "seven eight nine ten eleven twelve")
                ptt_long(45)
                check("a 45 s push-to-talk is placed whole, in order, the overlap said once",
                      waits(lambda: "twelve" in box()), box())
                check("... as one message: no 30 s cut, no 40 s ring loss",
                      box() == "one two three four five six seven eight nine ten eleven twelve", box())
                check("it left as two chunks of one utterance, in seq order",
                      len(stt.reqs) == 2 and stt.reqs[0]["utt"] == stt.reqs[1]["utt"] and stt.reqs[1]["seq"] == stt.reqs[0]["seq"] + 1, str(stt.reqs))
                check("no chunk is longer than 25 s, and together they are the whole 45 s (the overlap once)",
                      all(r["ms"] <= 25200 for r in stt.reqs) and 45000 < sum(r["ms"] for r in stt.reqs) - 500 < 47500,
                      str([round(r["ms"]) for r in stt.reqs]))
                check("each chunk carried its own deadline (budget_ms)", all(r["budget"] for r in stt.reqs))
                check("REVIEW placed it unsent", sends() == 0)
                check("one diagnostics row for the message, its audio released",
                      len(rows()) == 1 and rows()[0]["outcome"] == "review" and rows()[0]["kept"] is False, str(rows()))
                clear_box()

            def long_ptt_auto():
                page.evaluate("window.__hud.voiceDiagnostics")  # reachable
                _dictation(page, "auto")
                s0 = sends()
                stt.reset("first half of it", "of it and the second half")
                ptt_long(45)
                check("AUTO sends a 45 s message once, joined", waits(lambda: sends() == s0 + 1), f"{sends() - s0}")
                text = _sent(mock, "t1")[-1].get("text", "") if _sent(mock, "t1") else ""
                check("... with the overlap said once", text.strip() == "first half of it and the second half", text)
                _dictation(page, "review")

            def hands_free_long():
                page.locator('[data-testid="dictation-review"]').click()
                clear_box()
                s0, r0 = sends(), len(rows())
                stt.reset("alpha beta gamma delta", "epsilon zeta eta theta", "iota kappa lambda mu", "nu xi omicron pi")
                speak_when_listening(page, until, feed_js="window.__hud.mic.feedMs(2000, .0009); window.__hud.capture.openFollowUp()")
                page.evaluate(f"({WORDS})(36, 9, 900)")
                page.evaluate("window.__hud.mic.feedMs(2800, .0005)")
                check("a 36 s hands-free dictation is placed whole", waits(lambda: "omicron" in box() or len(stt.reqs) >= 2 and "zeta" in box()), box())
                n = len(stt.reqs)
                check("cut at its breaths into 2+ chunks, none repeating the one before", n >= 2 and len(rows()) == r0 + 1, str(stt.reqs))
                want = " ".join(stt.script[:n])
                check("and the chunks' words are in order", box() == want, f"{box()!r} != {want!r}")
                check("it was one utterance and sent nothing", len({r['utt'] for r in stt.reqs}) == 1 and sends() == s0)
                clear_box()

            def failed_chunk():
                page.locator('[data-testid="dictation-review"]').click()
                clear_box()
                stt.reset("a1 a2 a3", ("err", 400), "c1 c2 c3")
                ptt_long(70)
                check("a chunk that fails is kept for Retry", waits(lambda: page.locator('[data-testid="stt-retry"]').count() == 1))
                check("... and the others are placed in order in the box, with a mark where the missing part belongs", waits(lambda: box() == "a1 a2 a3 […] c1 c2 c3"), box())
                check("... the failed chunk said which part it was",
                      "part 2 of 3" in page.locator('[data-testid="stt-retry"]').inner_text(), page.locator('[data-testid="stt-retry"]').inner_text())
                check("one automatic retry was not made for a refusal", len(stt.reqs) == 3, str(len(stt.reqs)))
                check("the row says STT failed and the audio is kept",
                      rows()[-1]["outcome"] == "stt_failed" and rows()[-1]["kept"] is True, str(rows()[-1]))
                stt.reqs.clear()
                stt.script = ["x", "b1 b2 b3"]            # the retried chunk is the message's second
                page.locator('[data-testid="stt-retry-button"]').click()
                check("Retry lands the missing part, late, after what was placed", waits(lambda: "b1 b2 b3" in box()), box())
                check("the retry bar is gone", waits(lambda: page.locator('[data-testid="stt-retry"]').count() == 0))
                clear_box()

            def failed_chunk_auto_never_sends_half():
                _dictation(page, "auto")
                s0 = sends()
                stt.reset("half one", ("err", 400), "half three")
                ptt_long(70)
                check("AUTO: a message with a failed chunk goes to the box, never to the chat",
                      waits(lambda: box() == "half one […] half three") and sends() == s0, f"{box()!r} sends+{sends() - s0}")
                page.locator('[data-testid="stt-discard-button"]').click()
                check("Discard releases the kept audio", waits(lambda: page.locator('[data-testid="stt-retry"]').count() == 0))
                clear_box()
                _dictation(page, "review")

            def truncated_auto_never_sends():
                """H1: a message cut short (card, mic OFF) is placed late, so AUTO boxes it."""
                _dictation(page, "auto")
                clear_box()
                s0 = sends()
                stt.reset("first half of it", "never said")
                speak_when_listening(page, until, feed_js="window.__hud.mic.feedMs(2000, .0009); window.__hud.capture.openFollowUp()")
                page.evaluate(f"({WORDS})(9, 9, 900)")
                page.evaluate("window.__hud.mic.feedMs(900, .0009)")     # the breath: the first half leaves
                check("setup: the first half has been uploaded", waits(lambda: len(stt.reqs) == 1))
                _approval(mock, "v4cardC")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=4)
                page.evaluate(f"({WORDS})(8)")
                page.evaluate("window.__hud.mic.feedMs(2200, .0005)")
                check("AUTO: a card cut the message, so what left waits in the box and is not sent",
                      waits(lambda: "first half of it" in box()) and sends() == s0, f"{box()!r} sends+{sends() - s0}")
                check("nothing more was uploaded under the card", len(stt.reqs) == 1)
                page.keyboard.press("Escape")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
                check("the orb is not left on LISTENING", page.evaluate("window.__hud.state().orb") != "listening",
                      page.evaluate("window.__hud.state().orb"))
                clear_box()

                # the mic switched OFF in the middle of a push-to-talk
                stt.reset("spoken before off", "never said")
                _dictation(page, "auto")
                s0 = sends()
                page.evaluate("window.__hud.mic.feedMs(2000, .0009); window.__hud.capture.press()")
                page.evaluate(f"({WORDS})(30)")                    # one chunk left (hard cut at 25 s)
                check("setup: a chunk is out", waits(lambda: len(stt.reqs) == 1))
                _dictation(page, "off")
                n = len(stt.reqs)
                page.evaluate(f"({WORDS})(30)")
                page.evaluate("window.__hud.capture.release()")
                page.wait_for_timeout(500)
                check("OFF ends the recording at once: no further chunk, nothing sent", len(stt.reqs) == n and sends() == s0)
                _dictation(page, "review")
                check("what was said before OFF is in the box, unsent", waits(lambda: "spoken before off" in box()) and sends() == s0, box())
                clear_box()

            def two_in_flight():
                page.unroute("**/stt*")
                page.locator('[data-testid="dictation-review"]').click()
                clear_box()
                gate = threading.Event()
                mock.stt_gate = gate
                n0 = len(mock.sent("POST", "/stt"))
                ptt_long(95)
                page.wait_for_timeout(1500)
                arrived = len(mock.sent("POST", "/stt")) - n0
                check("with the answers held, at most two chunk uploads are in flight", arrived == 2, f"{arrived}")
                gate.set()
                mock.stt_gate = None
                check("the rest follow once they answer", waits(lambda: len(mock.sent("POST", "/stt")) - n0 >= 4, 12), f"{len(mock.sent('POST', '/stt')) - n0}")
                check("and the message lands as one", waits(lambda: box() != ""))
                clear_box()
                page.route("**/stt*", stt.handle)

            def off_uploads_nothing():
                _dictation(page, "off")
                n0 = len(stt.reqs)
                pressed = page.evaluate("window.__hud.capture.press()")
                page.evaluate(f"window.__hud.mic.feedMs(2000, .0009); window.__hud.capture.openFollowUp(); ({WORDS})(40, 9, 900); window.__hud.mic.feedMs(2800, .0005)")
                page.wait_for_timeout(500)
                check("mic OFF: the talk key claims nothing", pressed is False)
                check("mic OFF: not one chunk is uploaded, however long it is spoken to", len(stt.reqs) == n0)
                check("mic OFF: the window says it was muted", any(r["outcome"] == "muted" for r in rows()), str([r["outcome"] for r in rows()[-3:]]))
                _dictation(page, "review")

            def card_uploads_nothing():
                n0, a0 = len(stt.reqs), len([c for c in mock.calls if c[0] == "POST" and c[1].startswith("/approvals/")])
                _approval(mock, "v4cardA")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=4)
                page.evaluate(f"window.__hud.mic.feedMs(2000, .0009); window.__hud.capture.openFollowUp(); ({WORDS})(40, 9, 900); window.__hud.mic.feedMs(2800, .0005)")
                page.keyboard.down("Control")
                page.keyboard.down("d")
                page.evaluate("window.__hud.mic.feedMs(30000, .06)")
                page.keyboard.up("d")
                page.keyboard.up("Control")
                page.wait_for_timeout(400)
                check("a card up: no chunk is uploaded, by hands-free or the talk key", len(stt.reqs) == n0)
                check("a card up: the window gives the card as the reason", any(r["outcome"] == "suppressed:card" for r in rows()), str([r["outcome"] for r in rows()[-3:]]))
                check("a card up: no approval request came from speaking", len([c for c in mock.calls if c[0] == "POST" and c[1].startswith("/approvals/")]) == a0)
                page.keyboard.press("Escape")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)

            def card_mid_message():
                clear_box()
                s0 = sends()
                page.locator('[data-testid="dictation-review"]').click()
                stt.reset("before the card", "never said")
                page.evaluate("window.__hud.mic.feedMs(2000, .0009)")
                page.evaluate("window.__hud.capture.press()")
                page.evaluate(f"({WORDS})(30)")                    # one chunk has left (hard cut at 25 s)
                page.wait_for_timeout(300)
                n_before = len(stt.reqs)
                _approval(mock, "v4cardB")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=4)
                page.evaluate(f"({WORDS})(40)")                    # a minute-plus more, under the card
                page.evaluate("window.__hud.capture.release()")
                page.wait_for_timeout(600)
                check("a card appearing mid-message stops the uploads at once", len(stt.reqs) == n_before == 1, f"{n_before} -> {len(stt.reqs)}")
                check("what was said before the card waits in the box, unsent",
                      waits(lambda: "before the card" in box()) and sends() == s0, box())
                check("the part under the card is a suppressed:card drop", any(r["outcome"] == "suppressed:card" for r in rows()),
                      str([r["outcome"] for r in rows()]))
                page.keyboard.press("Escape")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
                clear_box()

            for section in (long_ptt_review, long_ptt_auto, hands_free_long, failed_chunk,
                            failed_chunk_auto_never_sends_half, truncated_auto_never_sends, two_in_flight, off_uploads_nothing,
                            card_uploads_nothing, card_mid_message):
                run(section)
            check("no page errors in the scripted sections", not [f for f in failures if f.startswith("page error")])
            ctx.close()

            run(worklet_realtime)
            run(worklet_unavailable)
            browser.close()
        print(f"\n{count - len(failures)}/{count} checks passed; {len(failures)} failures")
        if failures:
            print("\n".join(failures))
        return 1 if failures else 0
    finally:
        mock.stop()


if __name__ == "__main__":
    raise SystemExit(main())
