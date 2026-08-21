"""Synthetic checks for the HUD's continuous capture and its detector. Free.

The microphone is open from boot and never closes: audio flows into a ring
buffer and utterances are carved out of it afterwards. That design exists to
answer three complaints, and this suite is those three complaints written down
as assertions:

  1. "I have to wake jarvis each time by saying his name" — a follow-up window
     opens when he stops speaking, so the next utterance is a turn.
  2. "sometimes the recording gets cut" — a quiet mic still registers as
     speech, and a pause mid-sentence does not end the utterance.
  3. "somebody is talking in the background and it just doesn't send" —
     sustained background noise raises the floor instead of wedging the
     detector open until its cap.

Plus the owner's own mute (the MIC row): muted, nothing said opens or uploads
anything, wake hits and the follow-up window are inert, push-to-talk records
nothing (but still interrupts), the room threshold keeps tracking so unmute is
not deaf, the state survives a reload, and audio captured while muted can
never leave the machine — not even as the pre-roll of an utterance begun
after unmuting.

Driving it needs no microphone. A frame of N samples at a constant amplitude
has exactly that amplitude as its RMS, so `window.__hud.mic.feed(rms, n)`
scripts a room — quiet, chatter, speech — through the *real* onCaptureFrame,
ring, segmentation and send path. Sample counts are the clock, so the checks
are deterministic rather than timing-dependent.

Two browser contexts, for two different questions. The first grants a fake
(silent) microphone and asks whether capture actually runs unattended. The
second denies the microphone entirely, so the ring is driven only by the test
and the arithmetic is exact.

Run:  .venv/bin/python tests/face/hud_capture_check.py
"""

from __future__ import annotations

import base64
import json
import struct
import sys
import threading
import time
import wave
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from playwright.sync_api import sync_playwright  # noqa: E402

from jarvis.face.server import STATIC_DIR  # noqa: E402

PORT = 8477
BASE = f"http://127.0.0.1:{PORT}"

POSTS: list[dict] = []  # every /converse payload the window sent


class StubHandler(SimpleHTTPRequestHandler):
    """The real HUD, with /converse recording what it was given."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            return
        if self.path == "/config":
            self._json({"llm": "test/model", "stt": "test/stt", "tts": "test/tts",
                        "voice": "test", "speed": 1.0})
            return
        super().do_GET()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/cancel":
            self._json({"ok": True})
            return
        if self.path != "/converse":
            self.send_error(404)
            return
        try:
            POSTS.append(json.loads(body or b"{}"))
        except Exception:
            POSTS.append({})
        # A canned, audio-free turn: these checks are about what got captured
        # and whether it was sent, never about the reply.
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for line in (
            {"type": "heard", "text": "scripted", "ms": {"stt": 1}},
            {"type": "meta", "heard": "scripted", "reply": "Understood.",
             "ms": {"stt": 1, "agent": 2}, "steps": 1, "cost_usd": 0.0},
            {"type": "done", "tts_ms": 0, "muted": True},
        ):
            self.wfile.write((json.dumps(line) + "\n").encode())
            self.wfile.flush()

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def sent_seconds(post: dict) -> float:
    """Duration of the WAV the window uploaded."""
    raw = base64.b64decode(post["audio_b64"])
    with wave.open(BytesIO(raw)) as w:
        return w.getnframes() / w.getframerate()


def silent_wav(path: Path) -> Path:
    """A second of digital silence for Chromium's fake capture device.

    The default fake device emits a repeating tone, which would drive the
    detector on its own and make every threshold assertion a coin flip.
    """
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<16000h", *([0] * 16000)))
    return path


# --------------------------------------------------------------------------
# 1. capture really is continuous
# --------------------------------------------------------------------------

def check_capture_runs(page) -> None:
    page.wait_for_function("() => window.__hud && window.__hud.mic")
    page.wait_for_function("() => S.booted || S.state === 'error'", timeout=15_000)
    assert page.evaluate("() => S.state !== 'error'"), "fake microphone was refused"
    # Nothing has been pressed and no wake phrase has been said. If the ring is
    # filling anyway, the mic is open the way the design claims.
    first = page.evaluate("() => window.__hud.mic.written()")
    page.wait_for_function("n => window.__hud.mic.written() > n + 4000",
                           arg=first, timeout=8_000)
    assert not POSTS, "capture alone must not send anything"
    print("ok  capture: the ring fills from boot with nothing held and nothing said")


# --------------------------------------------------------------------------
# 2-9. the detector, driven frame by frame
# --------------------------------------------------------------------------

def feed(page, rms: float, ms: int) -> None:
    page.evaluate("([r, m]) => window.__hud.mic.feedMs(r, m)", [rms, ms])


def vad(page) -> dict:
    return page.evaluate("() => window.__hud.mic.vad()")


def wait_posts(page, n: int, what: str, timeout: float = 5.0) -> None:
    """Wait for the window to have uploaded `n` turns.

    Sending is asynchronous — converse() awaits the base64 encode and then the
    fetch — so asserting straight after the last frame was fed is a race the
    test loses about half the time.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(POSTS) >= n:
            # `!S.busy` alone is not the end of the turn — it flips at `meta`,
            # and the `done` line behind it is what opens the follow-up
            # window. Returning early leaves that landing on the *next*
            # check's freshly reset state.
            page.wait_for_function("() => !S.busy && streamDone", timeout=5_000)
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what} (got {len(POSTS)} uploads)")


def settle(page, ms: int = 350) -> None:
    """Give an upload that must NOT happen time to happen anyway."""
    page.wait_for_timeout(ms)


def run_detector_checks(page) -> None:
    page.wait_for_function("() => window.__hud && window.__hud.mic")
    page.wait_for_function("() => S.booted || S.state === 'error'", timeout=15_000)
    consts = page.evaluate("() => window.__hud.mic.consts()")
    reset = lambda: page.evaluate("() => window.__hud.mic.reset()")  # noqa: E731

    # -- a quiet room, then quiet speech ----------------------------------
    # The old detector compared a raw peak against a hard-coded 0.045, so
    # speech at 0.02 — an ordinary level on a mic set low — never registered
    # at all, and the turn ended on the owner mid-sentence.
    reset()
    feed(page, 0.001, 2000)          # the room
    quiet_floor = vad(page)["floor"]
    assert quiet_floor < 0.01, quiet_floor
    feed(page, 0.02, 300)            # speech, well under the old 0.045
    assert vad(page)["open"], "quiet speech did not register as speech"
    print(f"ok  floor: a quiet room settles to {quiet_floor:.4f}; speech at 0.02 opens "
          f"(the old fixed 0.045 never would)")

    # -- a pause mid-sentence is not the end of the turn -------------------
    feed(page, 0.001, consts["HANGOVER_MS"] - 400)
    assert vad(page)["open"], "a pause shorter than the hangover ended the utterance"
    feed(page, 0.02, 300)            # ...carrying on
    assert vad(page)["open"]
    feed(page, 0.001, consts["HANGOVER_MS"] + 200)
    assert not vad(page)["open"], "the utterance never ended"
    print(f"ok  hangover: {consts['HANGOVER_MS'] - 400}ms of thinking mid-sentence keeps "
          f"the utterance open; {consts['HANGOVER_MS'] + 200}ms closes it")

    # -- sustained background chatter raises the floor instead of wedging --
    # The report was "somebody is talking in the background and it just
    # doesn't send": the level never fell back under the fixed threshold, so
    # the end of the utterance was never detected and the recording ran to its
    # cap. The floor has to climb to meet it.
    reset()
    before = vad(page)["thresh"]
    for _ in range(40):
        feed(page, 0.03, 500)        # 20s of a conversation in the room
    after = vad(page)
    assert after["floor"] > 0.012, after
    assert after["thresh"] > before * 2, (before, after["thresh"])
    assert not after["open"], "background chatter left an utterance wedged open"
    print(f"ok  chatter: 20s of background talk lifts the threshold "
          f"{before:.4f} -> {after['thresh']:.4f} and leaves nothing open")

    # ...and the owner's own voice still clears the raised floor.
    feed(page, 0.12, 300)
    assert vad(page)["open"], "the owner could not be heard over the raised floor"
    feed(page, 0.03, consts["HANGOVER_MS"] + 400)
    print("ok  chatter: the owner still opens an utterance over a noisy room")

    # -- nothing is sent unless it was addressed to him --------------------
    POSTS.clear()
    reset()
    feed(page, 0.001, 1500)
    feed(page, 0.08, 800)
    feed(page, 0.001, consts["HANGOVER_MS"] + 400)
    settle(page)
    assert not POSTS, "an unclaimed utterance was sent"
    print("ok  claim: speech with no wake word and no follow-up window is not sent")

    # -- the wake word claims the utterance already in flight --------------
    # This is the one that used to lose the first word: the recognizer reports
    # a phrase several hundred ms late, and the old code started recording at
    # that point. Now the audio is already in the ring and the wake hit only
    # decides where to cut.
    POSTS.clear()
    reset()
    feed(page, 0.001, 1500)
    feed(page, 0.08, 700)            # "jarvis, what's the..."
    page.evaluate("() => window.__hud.mic.wake()")   # ...recognizer catches up
    assert page.evaluate("() => window.__hud.mic.claimed()"), "wake did not claim it"
    feed(page, 0.08, 700)            # "...weather"
    feed(page, 0.001, consts["HANGOVER_MS"] + 400)
    wait_posts(page, 1, "the wake-claimed utterance")
    spoken = sent_seconds(POSTS[0])
    # 1.4s was actually spoken; the pre-roll and the back-dating for recognizer
    # lag mean strictly more than that reaches the transcriber.
    assert spoken > 1.4, f"the audio sent ({spoken:.2f}s) is shorter than what was said"
    assert POSTS[0]["audio_mime"] == "audio/wav", POSTS[0]["audio_mime"]
    print(f"ok  wake: a late wake hit claims the utterance already in flight and sends "
          f"{spoken:.2f}s for 1.4s spoken — the pre-roll is what used to be lost")

    # -- the follow-up window: no wake word needed -------------------------
    POSTS.clear()
    reset()
    page.evaluate("() => window.__hud.mic.openFollowUp()")
    assert page.evaluate("() => window.__hud.mic.followUp()")
    feed(page, 0.001, 1200)
    feed(page, 0.08, 900)
    feed(page, 0.001, consts["HANGOVER_MS"] + 400)
    wait_posts(page, 1, "the follow-up turn")
    print("ok  follow-up: inside the window, speaking is enough — his name is not needed")

    # ...and the window closes, so the room does not stay hot forever.
    POSTS.clear()
    reset()
    page.evaluate("() => window.__hud.mic.closeFollowUp()")
    feed(page, 0.001, 1200)
    feed(page, 0.08, 900)
    feed(page, 0.001, consts["HANGOVER_MS"] + 400)
    settle(page)
    assert not POSTS, "the follow-up window never closed"
    print("ok  follow-up: once closed, ordinary speech is ignored again")

    # -- he does not answer himself ----------------------------------------
    POSTS.clear()
    reset()
    page.evaluate("() => window.__hud.mic.openFollowUp()")
    page.evaluate("() => setState('speaking')")
    feed(page, 0.12, 1200)           # his own reply, bleeding into the mic
    feed(page, 0.001, consts["HANGOVER_MS"] + 400)
    settle(page)
    assert not POSTS, "his own speech was captured as a turn"
    assert not vad(page)["open"]
    page.evaluate("() => setState('idle')")
    print("ok  suppression: nothing he says while speaking becomes a turn of its own")

    # -- a cough is not a turn ---------------------------------------------
    POSTS.clear()
    reset()
    page.evaluate("() => window.__hud.mic.openFollowUp()")
    feed(page, 0.001, 1200)
    feed(page, 0.09, 90)             # under MIN_UTTER_MS once the hangover trims
    feed(page, 0.001, consts["HANGOVER_MS"] + 400)
    settle(page)
    assert not POSTS, "a 90ms noise was sent as a turn"
    assert page.evaluate("() => window.__hud.mic.followUp()"), \
        "a discarded noise stole the follow-up window"
    print("ok  floor: a 90ms noise is not a turn, and does not consume the follow-up")

    # -- push to talk gets the same pre-roll -------------------------------
    POSTS.clear()
    reset()
    feed(page, 0.001, 1500)          # the owner is already drawing breath
    page.evaluate("() => window.__hud.mic.press()")
    feed(page, 0.08, 900)
    page.evaluate("() => window.__hud.mic.release()")
    wait_posts(page, 1, "the push-to-talk utterance")
    held = sent_seconds(POSTS[0])
    assert held > 0.9 + (consts["PREROLL_MS"] / 1000) * 0.8, \
        f"push-to-talk lost its pre-roll ({held:.2f}s for 0.9s held)"
    print(f"ok  push-to-talk: 0.9s held sends {held:.2f}s — talking the instant you press "
          f"no longer loses the first syllable")

    # -- a tap is still too short ------------------------------------------
    POSTS.clear()
    reset()
    page.evaluate("() => window.__hud.mic.press()")
    feed(page, 0.08, 60)
    page.evaluate("() => window.__hud.mic.release()")
    settle(page)
    assert not POSTS, "a tap on the orb sent a turn"
    print("ok  push-to-talk: a tap is refused rather than transcribed")


# --------------------------------------------------------------------------
# 10. the owner's own mute — muted, nothing said can arrive
# --------------------------------------------------------------------------

def run_mic_mute_checks(page) -> None:
    consts = page.evaluate("() => window.__hud.mic.consts()")
    reset = lambda: page.evaluate("() => window.__hud.mic.reset()")  # noqa: E731
    muted = lambda: page.evaluate("() => window.__hud.mic.muted()")  # noqa: E731
    set_mute = lambda on: page.evaluate("(v) => window.__hud.mic.setMute(v)", on)  # noqa: E731

    # -- muted: speech opens nothing and sends nothing ---------------------
    POSTS.clear()
    reset()
    set_mute(True)
    assert muted()
    assert page.evaluate("() => $('mic-state').textContent") == "MUTED"
    assert "MIC MUTED" in page.evaluate("() => $('hint').textContent")
    feed(page, 0.001, 1500)
    feed(page, 0.15, 900)            # loud, clearly-addressed speech
    feed(page, 0.001, consts["HANGOVER_MS"] + 400)
    settle(page)
    assert not POSTS, "a muted mic uploaded audio"
    assert not vad(page)["open"], "a muted mic opened an utterance"
    print("ok  mic mute: speech while muted is never captured as a turn")

    # -- muted: a wake hit is dead, and the follow-up window will not open --
    page.evaluate("() => window.__hud.mic.wake()")
    assert not page.evaluate("() => window.__hud.mic.claimed()"), \
        "a wake hit claimed an utterance while muted"
    page.evaluate("() => window.__hud.mic.openFollowUp()")
    assert not page.evaluate("() => window.__hud.mic.followUp()"), \
        "the follow-up window opened while muted — a promise nothing can keep"
    print("ok  mic mute: wake hits and the follow-up window are inert while muted")

    # -- muted: push-to-talk records nothing -------------------------------
    POSTS.clear()
    page.evaluate("() => window.__hud.mic.press()")
    assert not page.evaluate("() => window.__hud.mic.ptt()"), \
        "push-to-talk started recording while muted"
    assert page.evaluate("() => window.__hud.status()") == "MIC MUTED"
    feed(page, 0.08, 600)
    page.evaluate("() => window.__hud.mic.release()")
    settle(page)
    assert not POSTS, "a muted push-to-talk sent a turn"
    print("ok  mic mute: the orb records nothing while muted, and says why")

    # -- muted: the room is still tracked, so unmute is not deaf or jumpy --
    before = vad(page)["thresh"]
    for _ in range(20):
        feed(page, 0.03, 500)        # 10s of background chatter, all muted
    assert vad(page)["thresh"] > before * 2, (before, vad(page)["thresh"])
    print("ok  mic mute: the threshold keeps tracking the room while muted")

    # -- the mute survives a reload (failing toward muted, like wake) ------
    POSTS.clear()
    page.reload(wait_until="load")
    page.wait_for_function("() => window.__hud && window.__hud.mic")
    page.wait_for_function("() => S.booted || S.state === 'error'", timeout=15_000)
    assert muted(), "a reboot reopened a mic the owner turned off"
    assert page.evaluate("() => $('mic-state').textContent") == "MUTED"
    print("ok  mic mute: persists across a reload — a reboot is not a hot mic")

    # -- unmute restores the ordinary paths --------------------------------
    consts = page.evaluate("() => window.__hud.mic.consts()")
    POSTS.clear()
    reset()
    set_mute(False)
    assert not muted()
    page.evaluate("() => window.__hud.mic.openFollowUp()")
    feed(page, 0.001, 1200)
    feed(page, 0.15, 900)
    feed(page, 0.001, consts["HANGOVER_MS"] + 400)
    wait_posts(page, 1, "the first turn after unmuting")
    print("ok  mic mute: unmuting restores the follow-up path immediately")

    # -- audio captured while muted never leaves, even as pre-roll ---------
    # The wake path back-dates an utterance's start by WAKE_GRACE + PREROLL to
    # cover recognizer lag. Across an unmute that back-dating would reach into
    # audio recorded while the owner believed the mic was off; the unmute mark
    # clamps it.
    POSTS.clear()
    reset()
    set_mute(True)
    feed(page, 0.03, 2000)           # something said while muted ("private")
    set_mute(False)
    feed(page, 0.15, 400)            # "jarvis..." after the mic reopens
    page.evaluate("() => window.__hud.mic.wake()")
    assert page.evaluate("() => window.__hud.mic.claimed()")
    feed(page, 0.15, 400)
    feed(page, 0.001, consts["HANGOVER_MS"] + 400)
    wait_posts(page, 1, "the post-unmute wake turn")
    sent = sent_seconds(POSTS[0])
    grace = (consts["WAKE_GRACE_MS"] + consts["PREROLL_MS"]) / 1000
    assert sent < grace, (
        f"the upload ({sent:.2f}s) reaches back past the unmute — audio from "
        f"the muted period left the machine"
    )
    assert sent > 0.4, f"the clamp ate the utterance itself ({sent:.2f}s)"
    print(f"ok  mic mute: a wake right after unmuting sends {sent:.2f}s — the "
          f"back-dating stops at the unmute, muted audio never leaves")
    set_mute(False)


def main() -> int:
    server = ThreadingHTTPServer(
        ("127.0.0.1", PORT), partial(StubHandler, directory=str(STATIC_DIR))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with TemporaryDirectory() as tmp, sync_playwright() as p:
            silence = silent_wav(Path(tmp) / "silence.wav")

            # -- context 1: a granted (silent) microphone ------------------
            browser = p.chromium.launch(
                headless=True,
                args=["--use-fake-ui-for-media-stream",
                      "--use-fake-device-for-media-stream",
                      f"--use-file-for-fake-audio-capture={silence}",
                      "--autoplay-policy=no-user-gesture-required"],
            )
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{BASE}/jarvis.html", wait_until="load")
            check_capture_runs(page)
            assert not errors, f"page errors: {errors}"
            browser.close()

            # -- context 2: no microphone, so the ring is only ours --------
            # Denying it makes the sample arithmetic exact: nothing writes to
            # the ring except this test.
            browser = p.chromium.launch(
                headless=True, args=["--autoplay-policy=no-user-gesture-required"]
            )
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{BASE}/jarvis.html", wait_until="load")
            run_detector_checks(page)
            run_mic_mute_checks(page)
            assert not errors, f"page errors: {errors}"
            browser.close()
    finally:
        server.shutdown()
        server.server_close()

    print("\nall HUD capture checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
