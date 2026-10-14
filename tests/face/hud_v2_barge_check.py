"""V9 through the real HUD: self-voice reference gating, barge-in and spoken commands
(plan sections 3.4, 3.10, 5; decisions V-5, V-6, V-7; owner's answers Q7, Q8, Q10, Q13).

Two layers, in one Chromium against the mock daemon (ephemeral port, no live daemon, no
network, no real voice, no paid call):

  gate     Jarvis's reply is a synthetic Kokoro recording played by the REAL Web Audio player.
           A small acoustic simulator in the page hooks `AudioBufferSourceNode.start/stop`,
           computes what a microphone would hear of it (delayed, scaled, plus a noise floor)
           and mixes in a second synthetic Kokoro voice for "the owner", pushing 512-sample
           frames through the real stage, the real self-voice gate and the real Capture:
             - his voice alone, with a wake hit and a follow-up window as lures, opens nothing,
               claims nothing and is never paused or uploaded;
             - the owner over him, with no wake word, is dropped as unclaimed and does not pause
               him (Q7: before enrollment only "Jarvis" or the talk key pause playback);
             - the same with a wake hit: claimed, he pauses, a message is sent, and the message
               he was in the middle of is skipped.
  barge    scripted frames (the `feedMs` pattern of the steer check) with a gated `/stt`, so each
           barge-in transition of the plan's state machine, each command, and the approval
           isolation can be observed while the transcript is in flight.

Run (after `cd hud && npm run build`), under a temp HOME, the worktree PYTHONPATH, a 3G scope and
a slot:  python tests/face/hud_v2_barge_check.py [section,section]
Sections: gate_self_only, gate_owner_over, gate_wake_over, gate_own_name, gate_wake_command, barge_transitions, commands,
          commands_not_messages, card, fallback.
Not claimed: a physical speaker or microphone, Windows audio, the Silero detector in these runs
(the stage runs on energy here), or a real person's voice.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
assert not (REPO / ".env").exists(), "use a credential-free worktree"
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.face.hud_v2_check import FAKE_RECOGNIZER, guard_live, open_settings, until, watch_csp  # noqa: E402
from tests.face.hud_v2_layout_check import _approval  # noqa: E402
from tests.face.hud_v2_mock import MockDaemon  # noqa: E402
from tests.face.hud_v2_multichat_check import TWO, _box, _chat, _dictation, _focus, _fresh, _open, _seed, _sent  # noqa: E402
from tests.face.hud_v2_vad_check import FIXTURES, read_wav  # noqa: E402

BASE_ARGS = ["--autoplay-policy=no-user-gesture-required"]
CLIP_A = FIXTURES / "kokoro_a.wav"     # Jarvis's reply (af_heart, 4.9 s)
CLIP_B = FIXTURES / "kokoro_b.wav"     # "the owner" (am_adam, 3.4 s)

# Counts sources sounding, started and stopped, in the page, on the real Web Audio player.
SOURCES = """
(() => {
  window.__active = 0; window.__started = 0; window.__stopped = 0;
  const make = BaseAudioContext.prototype.createBufferSource;
  BaseAudioContext.prototype.createBufferSource = function (...args) {
    const node = make.apply(this, args);
    let live = false;
    const fin = () => { if (live) { live = false; window.__active -= 1; } };
    node.addEventListener('ended', fin);
    const start = node.start.bind(node);
    node.start = (...a) => { live = true; window.__active += 1; window.__started += 1; return start(...a); };
    const stop = node.stop.bind(node);
    node.stop = (...a) => { if (live) window.__stopped += 1; fin(); return stop(...a); };
    return node;
  };
})();
"""

# The acoustic simulator: what a microphone in the room would hear of what the page plays, plus an
# owner voice, as 512-sample 16 kHz frames pushed through the HUD's own stage (`__hud.mic.frame`).
SIM = """
(() => {
  const sim = window.__sim = { sources: [], owner: null, gain: 0.3, delay: 120, ownerGain: 1, noise: 0.0004,
    n: 0, t0: 0, timer: null };
  const make = BaseAudioContext.prototype.createBufferSource;
  BaseAudioContext.prototype.createBufferSource = function (...args) {
    const node = make.apply(this, args);
    const start = node.start.bind(node), stop = node.stop.bind(node);
    const entry = () => sim.sources.find(s => s.node === node);
    node.start = (...a) => { sim.sources.push({ node, buf: node.buffer, at: performance.now(), end: null, data: null }); return start(...a); };
    node.stop = (...a) => { const s = entry(); if (s && s.end === null) s.end = performance.now(); return stop(...a); };
    node.addEventListener('ended', () => { const s = entry(); if (s && s.end === null) s.end = performance.now(); });
    return node;
  };
  sim.say = (samples) => { sim.owner = { samples: Float32Array.from(samples), from: performance.now() }; };
  sim.run = () => {
    if (sim.timer) return;
    sim.t0 = performance.now(); sim.n = 0;
    sim.timer = setInterval(() => {
      sim.sources = sim.sources.filter(s => s.end === null || performance.now() - s.end < 3000);
      while (performance.now() >= sim.t0 + (sim.n + 1) * 32) {
        const f = new Float32Array(512);
        for (let i = 0; i < 512; i++) {
          const tm = sim.t0 + (sim.n * 512 + i) / 16;
          let v = sim.noise * (Math.random() * 2 - 1);
          for (const s of sim.sources) {
            const tt = tm - sim.delay;
            if (tt < s.at || (s.end !== null && tt > s.end)) continue;
            const k = Math.floor((tt - s.at) * s.buf.sampleRate / 1000);
            const d = s.data || (s.data = s.buf.getChannelData(0));
            if (k >= 0 && k < d.length) v += sim.gain * d[k];
          }
          if (sim.owner) {
            const k = Math.floor((tm - sim.owner.from) * 16);
            if (k >= 0 && k < sim.owner.samples.length) v += sim.ownerGain * sim.owner.samples[k];
          }
          f[i] = v;
        }
        sim.n++;
        window.__hud.mic.frame(f);
      }
    }, 8);
  };
  window.__watch = { openFrames: 0, states: {}, listens: 0 };
  setInterval(() => {
    if (!window.__hud) return;
    if (window.__hud.capture.vad.open) window.__watch.openFrames++;
    const st = window.__hud.speech().view.state;
    window.__watch.states[st] = (window.__watch.states[st] || 0) + 1;
  }, 10);
})();
"""

REPLY = "Right, here is what I found. The approval gate had seven separate ways past it, and every one was an older copy."
SHORT = "Here is a short reply for the owner to talk over."
# A message said with the wake word, as the steer check says it (the pause that ends it included).
WAKE = ("{const m = window.__hud.mic; m.feedMs(2000, .0009); m.feedMs(400, .06); m.wake();"
        " m.feedMs(1000, .06); m.feedMs(2800, .0005);}")
PTT = "window.__hud.capture.press(); window.__hud.mic.feedMs(1200, .06); window.__hud.capture.release()"
QUIET = "window.__hud.mic.feedMs(1500, .0009)"


def samples_of(path: Path) -> list[float]:
    return [round(v / 32768, 5) for v in read_wav(path)]


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

    mock = MockDaemon(int(os.environ.get("HUD_V2_CHECK_PORT", "18493"))).start()
    _seed(mock)
    w = mock.world
    wav_a = None
    try:
        import io
        import wave
        base = f"http://127.0.0.1:{mock.port}"
        with open(CLIP_A, "rb") as fh:
            wav_a = fh.read()
        clip_b = samples_of(CLIP_B)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=BASE_ARGS)
            context = browser.new_context(viewport={"width": 1600, "height": 900})

            def refuse(r):
                failures.append(f"request outside isolated mock: {r.request.url}")
                r.abort()
            context.route(re.compile(r"^https?://(?!(127\.0\.0\.1|localhost)[:/])"), refuse)
            guard_live(context, failures.append)
            watch_csp(context, failures.append)
            context.add_init_script(FAKE_RECOGNIZER + SOURCES + SIM)
            page = context.new_page()
            page.set_default_timeout(8000)
            page.on("pageerror", lambda e: failures.append(f"page error {e}"))
            before = mock.sse_connections()
            page.goto(base)
            page.wait_for_selector('[data-testid="sidebar"]', state="attached")
            mock.await_reconnect(before)

            # ---- helpers -------------------------------------------------
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

            def rows():
                return page.evaluate("window.__hud.voiceDiagnostics().rows")

            def waits(pred, timeout=6.0):
                return bool(until(pred, timeout=timeout))

            def started():
                return page.evaluate("window.__started")

            def stop_speech():
                if page.locator('[data-testid="speech-stop"]').count() and page.locator('[data-testid="speech-stop"]').is_enabled():
                    page.locator('[data-testid="speech-stop"]').click()
                waits(lambda: state() == "idle", 3)

            def reset(mode="auto", layout=TWO):
                mock.stt_script = []
                w.pop("send_status", None)
                mock.say_wav = None
                mock.ms_per_char = 60
                _fresh(page, mock, until, layout)
                _open(page, until, 1, "t1", "p1")
                _open(page, until, 2, "t2", "p1")
                _focus(page, until, 1)
                _dictation(page, mode)
                page.evaluate("() => window.dispatchEvent(new Event('focus'))")
                waits(lambda: speech()["speaker"], 3)
                page.evaluate(QUIET)

            def say_reply(text=REPLY, thread="t1"):
                mock.emit("text", {"text": text}, thread_id=thread)

            def playing():
                return waits(lambda: state() == "playing", 6)

            def run(section):
                if only and section.__name__ not in only:
                    return
                print(f"\n{section.__name__}")
                try:
                    section()
                except Exception as e:
                    check(f"{section.__name__} ran to the end", False, str(e).splitlines()[0][:240])
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass

            def sim_start(delay=120, gain=0.3):
                page.evaluate(f"() => {{ const s = window.__sim; s.delay = {delay}; s.gain = {gain}; s.owner = null; s.run(); }}")

            def watch_reset():
                page.evaluate("() => { window.__watch.openFrames = 0; window.__watch.states = {}; }")

            def watch():
                return page.evaluate("window.__watch")

            # ============ gate: his voice alone ====================================
            def gate_self_only():
                reset()
                mock.say_wav = wav_a
                sim_start()
                time.sleep(1.0)    # the room's noise, no reply yet
                page.evaluate("window.__hud.capture.openFollowUp()")    # a lure: a window that claims anything that opens
                st0, s0, a0 = len(stts()), len(_sent(mock, "t1")), started()
                watch_reset()
                say_reply(SHORT)
                check("the reply plays on the real Web Audio player", playing())
                time.sleep(1.6)
                check("the follow-up window closed when the reply began to sound",
                      page.evaluate("window.__hud.capture.conversing()") is False)
                r = page.evaluate("window.__hud.mic.wake()")       # the reply 'says Jarvis'; the recognizer hears the speakers
                time.sleep(0.5)
                page.evaluate("window.__hud.mic.wake()")
                check("a wake hit while only his voice is audible claims nothing",
                      page.evaluate("window.__hud.capture.segClaimed") is False and page.evaluate("window.__hud.capture.recording()") is False)
                waits(lambda: state() == "idle", 12)
                wt = watch()
                check("his voice alone opened no utterance at all", wt["openFrames"] == 0, str(wt))
                check("it was never paused", "paused" not in wt["states"], str(wt["states"]))
                check("nothing was uploaded and nothing was sent", len(stts()) == st0 and len(_sent(mock, "t1")) == s0)
                check("it played exactly once", started() == a0 + 1, f"{started() - a0} sources")
                rs = rows()
                check("diagnostics say why: speech-shaped sound held back as self_voice",
                      any(x["outcome"] == "self_voice" for x in rs), str([x["outcome"] for x in rs]))
                check("and the ignored wake hit is named, with its source",
                      any(x["outcome"] == "self_voice" and x["source"] == "wake" for x in rs), str(rs[-4:]))
                g = speech()["selfVoice"]
                check("the gate learned the room: an echo, about 120 ms behind",
                      g["lock"] == "echo" and abs(g["lagMs"] - 120) <= 48, str(g))
                check("and it is the gate that is in force, not the blanket hold", g["runnable"] is True)
                _ = r

            # ============ gate: the owner over him, no wake word ====================
            def gate_owner_over():
                reset()
                mock.say_wav = wav_a
                sim_start()
                time.sleep(0.8)
                # Learn the room first: a reply with only his voice.
                say_reply(SHORT)
                playing()
                waits(lambda: state() == "idle", 12)
                check("the room is learned from the first reply", speech()["selfVoice"]["lock"] == "echo", str(speech()["selfVoice"]))
                st0, s0 = len(stts()), len(_sent(mock, "t1"))
                watch_reset()
                say_reply(SHORT)
                playing()
                time.sleep(2.6)
                page.evaluate("(s) => window.__sim.say(s)", clip_b)       # the owner talks over him, without saying his name
                time.sleep(3.6)
                wt = watch()
                check("the owner's speech over him opened an utterance (the gate let it through)", wt["openFrames"] > 20, str(wt))
                check("but did not pause him: before enrollment only 'Jarvis' or the talk key do (Q7)",
                      "paused" not in wt["states"], str(wt["states"]))
                waits(lambda: state() == "idle", 12)
                dropped = waits(lambda: any(x["outcome"] == "unclaimed" for x in rows()), 8)    # the message pause (2.5 s) ends it
                check("nothing was claimed: nothing uploaded, nothing sent", len(stts()) == st0 and len(_sent(mock, "t1")) == s0,
                      f"stt+{len(stts()) - st0}")
                check("it was dropped as unclaimed", dropped, str([x["outcome"] for x in rows()]))
                page.evaluate("() => { window.__sim.owner = null; }")

            # ============ gate: the owner over him, with his name ===================
            def gate_wake_over():
                reset()
                mock.say_wav = wav_a
                sim_start()
                time.sleep(0.8)
                say_reply(SHORT)
                playing()
                waits(lambda: state() == "idle", 12)
                mock.stt_script = []
                say_reply(REPLY, "t1")
                say_reply("Bravo has a message waiting behind it, which will be spoken afterwards.", "t2")
                playing()
                time.sleep(2.4)
                n0, st0, s0 = started(), len(stts()), len(_sent(mock, "t1"))
                gate_ev = threading.Event()
                mock.stt_script = [{"text": "run the tests please", "wait": gate_ev}]
                page.evaluate("(s) => window.__sim.say(s)", clip_b)
                waits(lambda: page.evaluate("window.__hud.capture.vad.open"), 4)
                time.sleep(0.4)
                page.evaluate("window.__hud.mic.wake()")                   # the recognizer heard the owner say his name
                check("a wake hit over an utterance the owner opened claims it", page.evaluate("window.__hud.capture.segClaimed") is True)
                check("and pauses him", waits(lambda: state() == "paused", 2))
                check("the strip says it is held by a barge-in", speech()["barge"] is True)
                time.sleep(4.2)       # the rest of the owner's words, then the pause that ends the message
                check("the upload is in flight while he stays paused", bool(waits(lambda: not mock.stt_script, 8)) and state() == "paused")
                gate_ev.set()
                sent = until(lambda: _sent(mock, "t1")[s0:] or None, timeout=8)
                check("the message was sent to the chat", bool(sent) and sent[-1].get("text") == "run the tests please", str(sent))
                check("the message he was saying was skipped, not replayed; the rest of the queue goes on",
                      waits(lambda: any("Bravo has a message" in i["text"] for i in speech()["past"]) or "Bravo" in " ".join(i["text"] for i in speech()["live"]), 6))
                waits(lambda: state() == "idle", 20)
                texts = [i for i in speech()["past"] if i["thread"] == "t1" and "approval gate" in i["text"]]
                check("his interrupted reply never came back", all(i["state"] != "done" for i in texts) or not texts, str(texts))
                check("the barge-in is over", speech()["barge"] is False and state() == "idle")
                page.evaluate("() => { window.__sim.owner = null; }")
                _ = (n0, st0)

            # ============ gate: his reply says his name; the room (a TV) is speaking ==
            def gate_own_name():
                reset()
                mock.say_wav = wav_a
                sim_start()
                time.sleep(0.8)
                say_reply(SHORT)                       # learn the room
                playing()
                waits(lambda: state() == "idle", 12)
                st0, s0 = len(stts()), len(_sent(mock, "t1"))
                watch_reset()
                say_reply("Jarvis here: the approval gate was the whole problem.")
                playing()
                time.sleep(2.6)
                page.evaluate("(s) => window.__sim.say(s)", clip_b)       # the TV talks over him and holds the detector open
                waits(lambda: page.evaluate("window.__hud.capture.vad.open"), 4)
                time.sleep(0.3)
                n0 = len(rows())
                page.evaluate("window.__hud.mic.wake()")                  # the recognizer hears the room say 'Jarvis'
                check("a wake hit while his sentence says his name claims nothing, even with an utterance open",
                      page.evaluate("window.__hud.capture.segClaimed") is False and page.evaluate("window.__hud.capture.recording()") is False)
                check("it is reported, with its source", any(x["outcome"] == "self_voice" and x["source"] == "wake" for x in rows()[n0:]),
                      str(rows()[n0:]))
                check("and he was not paused by it", "paused" not in watch()["states"], str(watch()["states"]))
                waits(lambda: state() == "idle", 12)
                time.sleep(3.0)
                check("nothing was uploaded or sent", len(stts()) == st0 and len(_sent(mock, "t1")) == s0)
                page.evaluate("() => { window.__sim.owner = null; }")

            # ============ gate: the owner says "Jarvis stop" over him =================
            def gate_wake_command():
                reset()
                mock.say_wav = wav_a
                sim_start()
                time.sleep(0.8)
                say_reply(SHORT)
                playing()
                waits(lambda: state() == "idle", 12)
                say_reply(REPLY, "t1")
                playing()
                time.sleep(2.4)
                i0, s0 = len(interrupts()), len(_sent(mock, "t1"))
                gate_ev = threading.Event()
                mock.stt_script = [{"text": "Jarvis stop", "wait": gate_ev}]
                page.evaluate("(s) => window.__sim.say(s)", clip_b)
                waits(lambda: page.evaluate("window.__hud.capture.vad.open"), 4)
                time.sleep(0.4)
                page.evaluate("window.__hud.mic.wake()")
                check("the owner's 'Jarvis' over him claims the utterance and pauses him", waits(lambda: state() == "paused", 3))
                waits(lambda: not mock.stt_script, 10)
                gate_ev.set()
                check("'Jarvis stop' is obeyed: speech stops and the queue is empty", waits(lambda: state() == "idle" and speech()["view"]["queued"] == 0, 6))
                time.sleep(0.5)
                check("it sent nothing and interrupted nothing", len(_sent(mock, "t1")) == s0 and len(interrupts()) == i0)
                page.evaluate("() => { window.__sim.owner = null; }")

            run(gate_self_only)
            run(gate_owner_over)
            run(gate_wake_over)
            run(gate_own_name)
            run(gate_wake_command)
            page.evaluate("() => { clearInterval(window.__sim.timer); window.__sim.timer = null; window.__sim.owner = null; }")

            # ============ barge: every transition of the state machine ==============
            def start_barge(stt, gate=True, mode="auto", how=WAKE, second=False, script=None):
                """A reply playing, then an utterance (claimed by `how`) whose transcript is `stt`, held in flight
                (or the scripted `/stt` answers, when `script` is given)."""
                reset(mode)
                say_reply(REPLY, "t1")
                if second:
                    say_reply("Bravo is waiting behind it and has quite a lot to say about that.", "t2")
                if not playing():
                    raise RuntimeError("no reply was playing")
                ev = threading.Event() if gate else None
                mock.stt_script = script if script is not None else [{"text": stt, **({"wait": ev} if ev is not None else {})}]
                page.evaluate(how)
                return ev

            def barge_transitions():
                # --- nothing came of it: an empty transcript --------------------------------
                n0 = None
                ev = start_barge("", how=WAKE)
                n0 = started()
                check("a wake word over him pauses him (Q7)", state() == "paused" and speech()["barge"] is True)
                time.sleep(1.0)
                check("and nothing plays while the owner is being transcribed", started() == n0 and state() == "paused")
                ev.set()
                check("an empty transcript: he resumes", waits(lambda: state() == "playing", 5))
                check("restarting the sentence he was in (Q13)", started() > n0, f"{started() - n0} new sources")
                stop_speech()
                # --- a failed upload -------------------------------------------------------
                ev = start_barge("x", how=WAKE, script=[{"status": 500, "error": "down"}, {"status": 500, "error": "down"}])
                n0 = started()
                check("a failed transcription: he resumes after the retry", waits(lambda: state() == "playing", 14))
                check("restarting the sentence", started() > n0)
                stop_speech()
                # --- REVIEW: the words wait in the box ---------------------------------------
                ev = start_barge("this stays in the box", mode="review", how=WAKE)
                n0, s0 = started(), len(_sent(mock, "t1"))
                ev.set()
                check("REVIEW: the words land in the box and he resumes", waits(lambda: "this stays in the box" in _box(page, 1).input_value() and state() == "playing", 6))
                check("nothing was sent", len(_sent(mock, "t1")) == s0)
                check("restarting the sentence", started() > n0)
                stop_speech()
                # --- a message was sent: the one he was saying is skipped --------------------
                ev = start_barge("please look at the second file", how=WAKE, second=True)
                s0 = len(_sent(mock, "t1"))
                texts_before = [i["text"] for i in speech()["live"]]
                ev.set()
                sent = until(lambda: _sent(mock, "t1")[s0:] or None, timeout=6)
                check("AUTO: the words are sent", bool(sent) and sent[-1].get("text") == "please look at the second file", str(sent))
                check("the message he was saying is skipped; the other chat's message is spoken",
                      waits(lambda: any("Bravo is waiting" in t for t in [i["text"] for i in speech()["live"]] + [i["text"] for i in speech()["past"]]), 6))
                waits(lambda: state() in ("playing", "idle"), 5)
                said = [i["text"] for i in speech()["past"] if i["state"] == "done"]
                check("his first reply was not read to the end", not any("older copy" in t for t in said), str(said[-3:]))
                _ = texts_before
                stop_speech()
                # --- the talk key: too short, then nothing was said ---------------------------
                reset()
                say_reply()
                playing()
                n0 = started()
                page.locator("#orb").dispatch_event("pointerdown")
                check("the talk key pauses him", state() == "paused")
                page.locator("#orb").dispatch_event("pointerup")
                check("a tap that says nothing: he carries on, restarting the sentence",
                      waits(lambda: state() == "playing" and started() > n0, 4))
                stop_speech()
                # --- the owner's own pause is not undone --------------------------------------
                reset()
                say_reply()
                playing()
                page.locator('[data-testid="speech-toggle"]').click()
                page.locator("#orb").dispatch_event("pointerdown")
                page.locator("#orb").dispatch_event("pointerup")
                time.sleep(0.6)
                check("paused from the strip first, a barge-in that came to nothing does not resume him", state() == "paused")
                stop_speech()
                # --- a wake hit with no utterance does not pause him --------------------------
                reset()
                say_reply()
                playing()
                page.evaluate("window.__hud.mic.wake()")
                time.sleep(0.4)
                check("a wake hit with no speech behind it pauses nothing", state() == "playing")
                time.sleep(1.6)       # the hit is kept for 1.2 s in case the speech it names is still coming
                # --- another voice (no wake, no key) does not pause him: Q7 -------------------
                page.evaluate("window.__hud.mic.feedMs(1500, .0009); window.__hud.mic.feedMs(1500, .06); window.__hud.mic.feedMs(2800, .0005)")
                time.sleep(0.5)
                check("speech over him with no wake word and no key does not pause him (Q7)", state() == "playing")
                stop_speech()

            def commands():
                for text, after in (("Jarvis pause", "paused"), ("Jarvis, pause.", "paused")):
                    ev = start_barge(text)
                    n0, s0, i0 = started(), len(_sent(mock, "t1")), len(interrupts())
                    ev.set()
                    time.sleep(1.2)
                    check(f"{text!r}: he stays paused", state() == after and speech()["barge"] is False, state())
                    check("  nothing was sent, nothing interrupted", len(_sent(mock, "t1")) == s0 and len(interrupts()) == i0)
                    check("  recorded as a command, not a message", rows()[-1]["outcome"] == "command", str(rows()[-1]))
                    check("  nothing played meanwhile", started() == n0)
                    stop_speech()
                # resume (from the barge-in's own pause)
                ev = start_barge("Jarvis resume")
                n0 = started()
                ev.set()
                check("'Jarvis resume': he plays again, from the start of the sentence", waits(lambda: state() == "playing" and started() > n0, 5))
                stop_speech()
                # resume after the owner's own pause
                reset()
                say_reply()
                playing()
                page.locator('[data-testid="speech-toggle"]').click()
                n0 = started()
                ev = threading.Event()
                mock.stt_script = [{"text": "Jarvis resume", "wait": ev}]
                page.evaluate(WAKE)
                ev.set()
                check("'Jarvis resume' also ends the owner's own pause", waits(lambda: state() == "playing", 5))
                stop_speech()
                # skip
                ev = start_barge("Jarvis skip", second=True)
                s0 = len(_sent(mock, "t1"))
                ev.set()
                check("'Jarvis skip': the rest of the message goes and the queue goes on",
                      waits(lambda: any("Bravo is waiting" in i["text"] for i in speech()["live"] + speech()["past"]), 6))
                waits(lambda: state() == "idle", 20)
                said = [i["text"] for i in speech()["past"] if i["state"] == "done"]
                check("  the skipped message was not finished, the other was", not any("older copy" in t for t in said) and any("Bravo" in t for t in said), str(said))
                check("  and nothing was sent", len(_sent(mock, "t1")) == s0)
                # stop: clears the queue, never an agent turn
                reset()
                mock.emit("turn_started", {}, thread_id="t1")
                waits(lambda: _chat(page, 1)["busy"], 3)
                say_reply()
                say_reply("Bravo is waiting behind it and has quite a lot to say about that.", "t2")
                playing()
                i0, s0 = len(interrupts()), len(_sent(mock, "t1"))
                ev = threading.Event()
                mock.stt_script = [{"text": "Jarvis stop", "wait": ev}]
                page.evaluate(WAKE)
                ev.set()
                check("'Jarvis stop': speech stops and the queue is cleared",
                      waits(lambda: state() == "idle" and speech()["view"]["queued"] == 0, 5))
                time.sleep(0.5)
                check("  it never interrupted the agent's turn: no interrupt was sent, the turn is still running",
                      len(interrupts()) == i0 and _chat(page, 1)["busy"] is True)
                check("  and it steered nothing", len(_sent(mock, "t1")) == s0)
                mock.emit("turn_finished", {"stop": "end"}, thread_id="t1")
                waits(lambda: not _chat(page, 1)["busy"], 3)
                # the talk key says it too
                reset()
                say_reply()
                playing()
                mock.stt_script = [{"text": "Jarvis stop"}]
                page.evaluate(PTT)
                check("the same by the talk key", waits(lambda: state() == "idle", 5))

            def commands_not_messages():
                for text in ("Pause the build", "Jarvis, stop the build", "Jarvis stop it", "stop"):
                    ev = start_barge(text)
                    s0 = len(_sent(mock, "t1"))
                    n0 = started()
                    ev.set()
                    sent = until(lambda: _sent(mock, "t1")[s0:] or None, timeout=6)
                    check(f"{text!r} is a message, sent as written", bool(sent) and sent[-1].get("text") == text, str(sent))
                    check("  handed to the chat, not run as a command",
                          rows()[-1]["outcome"] == "handed_to_chat", str(rows()[-1]))
                    stop_speech()
                    _ = n0
                stop_speech()

            # ============ the card: no spoken word answers it =======================
            def card():
                reset()
                say_reply()
                playing()
                a0, st0, s0, i0 = len(approvals_posted()), len(stts()), len(_sent(mock, "t1")), len(interrupts())
                _approval(mock, "v9card1")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=4)
                mock.stt_script = [{"text": "Jarvis approve"}] * 4
                n0 = started()
                # Every way of talking: the talk key, a wake hit, the follow-up window, hands-free speech.
                page.keyboard.down("Control"); page.keyboard.down("d")
                page.evaluate("window.__hud.mic.feedMs(1500, .06)")
                page.keyboard.up("d"); page.keyboard.up("Control")
                page.evaluate("window.__hud.capture.openFollowUp(); window.__hud.mic.feedMs(1500, .0009); window.__hud.mic.feedMs(1200, .06); window.__hud.mic.feedMs(2800, .0005)")
                page.evaluate(WAKE)
                time.sleep(0.8)
                check("a card up: 'Jarvis approve' by every means uploads nothing and requests no approval",
                      len(stts()) == st0 and len(approvals_posted()) == a0, f"stt+{len(stts()) - st0} approvals+{len(approvals_posted()) - a0}")
                check("nothing was sent, nothing interrupted, the card is still waiting",
                      len(_sent(mock, "t1")) == s0 and len(interrupts()) == i0 and page.locator('[data-testid="approval-card"]').count() == 1)
                check("voice commands are off under a card: the reply was not paused, skipped or stopped", state() in ("playing", "idle"))
                check("the window says why", any(x["outcome"] == "suppressed:card" for x in rows()), str([x["outcome"] for x in rows()[-4:]]))
                _ = n0
                page.keyboard.press("Escape")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
                stop_speech()

                # A card that arrives while the words are in flight: nothing is obeyed, nothing is answered.
                for phrase in ("Jarvis approve", "Jarvis yes", "approve", "Jarvis stop", "Jarvis pause"):
                    reset()
                    say_reply()
                    playing()
                    a0, s0, i0 = len(approvals_posted()), len(_sent(mock, "t1")), len(interrupts())
                    ev = threading.Event()
                    mock.stt_script = [{"text": phrase, "wait": ev}]
                    page.evaluate(WAKE)
                    waits(lambda: not mock.stt_script, 5)
                    _approval(mock, "v9card2")
                    until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=4)
                    ev.set()
                    time.sleep(1.0)
                    check(f"in flight under a card, {phrase!r}: zero approval requests",
                          len(approvals_posted()) == a0 and page.locator('[data-testid="approval-card"]').count() == 1,
                          f"approvals+{len(approvals_posted()) - a0}")
                    check("  nothing sent, nothing interrupted", len(_sent(mock, "t1")) == s0 and len(interrupts()) == i0)
                    if phrase in ("Jarvis stop", "Jarvis pause"):
                        check("  and the command was not obeyed under the card (still reading, or already finished)",
                              state() != "idle" or any(i["state"] == "done" for i in speech()["past"][-2:]) , state())
                    page.keyboard.press("Escape")
                    until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
                    _box(page, 1).fill("")
                    stop_speech()

                # With no card, 'Jarvis approve' is just a message: still no approval route.
                ev = start_barge("Jarvis approve")
                a0, s0 = len(approvals_posted()), len(_sent(mock, "t1"))
                ev.set()
                sent = until(lambda: _sent(mock, "t1")[s0:] or None, timeout=6)
                check("with no card 'Jarvis approve' is an ordinary message", bool(sent) and sent[-1].get("text") == "Jarvis approve", str(sent))
                check("  and reaches no approval route", len(approvals_posted()) == a0)
                stop_speech()

            # ============ the fallback: the blanket hold when gating is off ===========
            def fallback():
                reset()
                mock.say_wav = None
                say_reply()
                playing()
                g = speech()["selfVoice"]
                check("the gate is in force by default", g["runnable"] is True and g["enabled"] is True, str(g))
                stt0 = len(stts())
                page.evaluate("window.__hud.mic.feedMs(1500, .0009); window.__hud.mic.feedMs(1200, .06); window.__hud.mic.feedMs(2800, .0005)")
                time.sleep(0.4)
                check("gating on: hands-free speech over him is dropped as unclaimed, not as 'speaking'",
                      any(x["outcome"] == "unclaimed" for x in rows()[-3:]) and not any(x["outcome"] == "suppressed:speaking" for x in rows()[-3:]),
                      str([x["outcome"] for x in rows()[-3:]]))
                stop_speech()
                open_settings(page, "Voice")
                check("Settings -> Voice names the gate", "Self-voice gate" in page.locator('[data-testid="self-voice-status"]').inner_text())
                page.locator('[data-testid="self-voice-toggle"]').uncheck()
                page.locator('[data-testid="settings-close"]').click()
                check("switched off: the blanket hold is in force", speech()["selfVoice"]["runnable"] is False and speech()["selfVoice"]["enabled"] is False)
                say_reply()
                playing()
                page.evaluate("window.__hud.capture.openFollowUp(); window.__hud.mic.feedMs(1500, .0009); window.__hud.mic.feedMs(1200, .06); window.__hud.mic.feedMs(2800, .0005)")
                time.sleep(0.4)
                check("the whole of a reply holds the mic (V6): dropped as suppressed:speaking",
                      any(x["outcome"] == "suppressed:speaking" for x in rows()[-4:]), str([x["outcome"] for x in rows()[-4:]]))
                n0 = started()
                page.locator("#orb").dispatch_event("pointerdown")
                check("the talk key still pauses him", state() == "paused")
                page.locator("#orb").dispatch_event("pointerup")
                check("and he carries on", waits(lambda: state() == "playing", 4) and started() > n0)
                # wake hits while the blanket hold applies claim nothing
                mock.stt_script = []
                page.evaluate("window.__hud.mic.feedMs(600, .0009); window.__hud.mic.feedMs(500, .06); window.__hud.mic.wake()")
                check("a wake hit under the blanket hold claims nothing", page.evaluate("window.__hud.capture.recording()") is False)
                stop_speech()
                open_settings(page, "Voice")
                page.locator('[data-testid="self-voice-toggle"]').check()
                page.locator('[data-testid="settings-close"]').click()
                check("switched back on", speech()["selfVoice"]["runnable"] is True)
                check("the choice is remembered in this window",
                      page.evaluate("localStorage.getItem('jarvis.selfVoiceGate')") == "on")

            run(barge_transitions)
            run(commands)
            run(commands_not_messages)
            run(card)
            run(fallback)

            check("no page errors", not [f for f in failures if f.startswith("page error")], "; ".join(failures[:3]))
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
    print(f"all {count} barge-in checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
