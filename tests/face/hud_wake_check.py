"""Checks the HUD's wake-phrase matcher, against the real `jarvis.html`.

Free — no API calls, no microphone. Serves the real page (with /config and
/events stubbed, hud_state_check's pattern) and drives `matchesWake` — the
same function `recog.onresult` calls — so this tests the window's own JS.

What it pins down:
  1. the built-in phrase fires, on its own and mid-sentence.
  2. ordinary speech does NOT fire. A false positive is not cosmetic — a
     wake hit cancels the turn in flight and starts recording, so a pattern
     that matches a substring of normal conversation steals the owner's
     words mid-sentence.
  3. the phrase is anchored: "jarvisson", "harvest" are silence.
  4. **"big yahoo" belongs to the `bibi` avatar now** (2026-08-06), so it is
     silence on the default and fires once that avatar is applied — in the
     spellings Chrome's recognizer actually returns for a phrase it has never
     heard ("big ya hoo", "big yoohoo", "big yahu", ...).
  5. the armed hint names the live phrases — an undiscoverable wake word is a
     wake word nobody says.
  6. **a phrase fires once per thing said**, driven through the real
     `recog.onresult` with a scripted recognizer. Chrome re-delivers the
     transcript being spoken on every revision and once more as final, so the
     level-triggered handler this replaced chimed twice for a bare "jarvis"
     and again for every word spoken after it (reported 2026-08-22).

Run:  .venv/bin/python tests/face/hud_wake_check.py
"""

from __future__ import annotations

import json
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from playwright.sync_api import sync_playwright  # noqa: E402

from jarvis.face.server import STATIC_DIR  # noqa: E402

PORT = 8479
BASE = f"http://127.0.0.1:{PORT}"

# Said out loud, or heard in the middle of a sentence.
WAKE = [
    "jarvis",
    "Jarvis",
    "hey jarvis what time is it",
]

# The phrases that belong to the `bibi` avatar, in every spelling the
# recognizer hands back for a phrase it has never heard. Silence on the
# default; a wake hit once that avatar is applied.
BIBI_WAKE = [
    "big yahu",
    "big yahoo",
    "Big Yahoo!",
    "ok big yahu, mute yourself",
    "big ya hoo",
    "big yoohoo",
    "big yoo-hoo",
    "big yahooo",
    "big yawho",
    "bigyahoo",
    "bibi",
    # the name itself, whole or on its own
    "netanyahu",
    "benjamin netanyahu",
    "hey netanyahu what time is it",
    "netanyahoo",
    "netan yahoo",
    "netan yahu",
    "nathan yahoo",
    "netanyaho",
    "netanya who",
]

# Everything else the recognizer hears while it sits armed in the room.
QUIET = [
    "yahoo finance is down again",
    "that is a big house",
    "we rented a big yacht",
    "javier is on the call",
    "jarvisson called back",
    "big yellow taxi",
    "the big one",
    "yahoo",
    "big y",
    "harvest the logs",
    # near misses for the bibi avatar's phrases — silence for both avatars.
    # "benjamin" alone is deliberately not a wake phrase: it is a common name,
    # and a wake hit takes the owner's words mid-sentence.
    "benjamin",
    "benjamin franklin invented it",
    "nathan is on the call",
    "nathan you should call him",
    "netanya beach",
    "",
]

# What /config would hand the window for the bibi avatar — the same shape the
# server builds from avatars/bibi/avatar.json via Avatar.describe().
BIBI = {
    "slug": "bibi", "name": "Bibi", "banner": "B I B I", "label": "B.I.B.I.",
    "wake": ["BIG YAHU", "NETANYAHU", "BIBI"],
    "wake_regex": [r"\bbig\s*y(?:a|ah|oo)+[\s-]*(?:hoo+|whoo+|who|hu)\b",
                   r"\b(?:benjamin[\s-]*)?n[ae]th?[ae]n[\s-]*y(?:a|ah|oo)+"
                   r"[\s-]*(?:hoo+|whoo+|who|hu|hou|ho)\b",
                   r"\bbig\s*yahu\b", r"\bnetanyahu\b", r"\bbibi\b"],
    "accent": "59,130,246", "description": "Benjamin Netanyahu.", "has_svg": False,
}


class StaticHandler(SimpleHTTPRequestHandler):
    """The real HUD, with the two routes it calls at boot stubbed out."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            while not SHUTDOWN.wait(0.1):
                pass
            return
        if self.path == "/config":
            body = json.dumps(
                {"llm": "test/model", "stt": "test/stt", "tts": "test/tts",
                 "voice": "test", "speed": 1.0}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


SHUTDOWN = threading.Event()

# A scripted SpeechRecognition, installed before `jarvis.html` runs so that
# `armRecognition` picks it up instead of the real one (which needs a network
# service and a microphone). It records nothing and decides nothing — the test
# hands it exactly the event sequence Chrome produces, and the window's own
# handler is what is under test.
FAKE_RECOGNIZER = """
window.SpeechRecognition = class {
  constructor() { window.__sr = this; }
  start() { if (this.onstart) this.onstart(); }
  stop() { if (this.onend) this.onend(); }
};
window.__chimes = 0;
"""

# One recognizer event. `segments` is [text, ...] oldest-first — the whole
# results list as Chrome would hand it over, with `index` the first entry
# revised by this event.
SAY = """(spec) => {
  window.__sr.onresult({
    resultIndex: spec.index,
    results: spec.segments.map((t) => [{ transcript: t }]),
  });
  return window.__chimes;
}"""


def run_checks(page) -> None:
    match = lambda t: page.evaluate("(t) => window.__hud.matchesWake(t)", t)  # noqa: E731

    # 1. the built-in phrase
    for text in WAKE:
        assert match(text), f"wake phrase not detected: {text!r}"
    print(f"ok  wake: {len(WAKE)} phrasings fire (jarvis)")

    # 2 & 3. ordinary speech is silence, and the phrase is anchored
    for text in QUIET:
        assert not match(text), f"false wake on ordinary speech: {text!r}"
    print(f"ok  wake: {len(QUIET)} non-wake utterances do not fire")

    # 4. "big yahoo" is the bibi avatar's now — silence here...
    for text in BIBI_WAKE:
        assert not match(text), f"default still wakes on a bibi phrase: {text!r}"
    print(f"ok  wake: {len(BIBI_WAKE)} bibi phrasings are silence on the default")

    # 5. the armed hint names the live phrase, and only that one
    page.click("#wake-row")
    hint = page.text_content("#hint")
    assert "JARVIS" in hint and "YAHOO" not in hint.upper(), hint
    print(f"ok  wake: armed hint names the live phrase — {hint!r}")
    page.click("#wake-row")  # disarm

    # ...and fires once that avatar is applied, in every spelling.
    page.evaluate("(a) => window.__hud.applyAvatar(a)", BIBI)
    for text in BIBI_WAKE:
        assert match(text), f"bibi phrase not detected: {text!r}"
    print(f"ok  wake: {len(BIBI_WAKE)} phrasings fire as bibi "
          "(big yahu / netanyahu variants)")
    for text in QUIET + ["jarvis"]:
        assert not match(text), f"false wake as bibi: {text!r}"
    print("ok  wake: bibi is anchored, and does not answer to 'jarvis'")

    page.click("#wake-row")
    hint = page.text_content("#hint")
    assert "BIG YAHU" in hint and "JARVIS" not in hint, hint
    print(f"ok  wake: the hint follows the avatar — {hint!r}")


def recognizer_checks(page) -> None:
    """The handler, driven with the event sequences Chrome actually produces."""
    # Count chimes instead of playing them: the real one needs an AudioContext
    # a headless page has had no gesture to unlock, and the question here is
    # how many times it was *asked* to sound.
    page.evaluate("() => { window.__chimes = 0; window.chime = () => window.__chimes++; }")
    # run_checks leaves the bibi avatar applied and the wake word armed; this
    # section is about the default phrase, from a known state.
    page.evaluate("() => { applyAvatar(DEFAULT_AVATAR); setWake(false); setWake(true); }")
    assert page.evaluate("() => !!window.__sr"), "the recognizer was never constructed"
    say = lambda spec: page.evaluate(SAY, spec)  # noqa: E731

    # 1. A bare "jarvis": one interim revision, then the same words as final.
    #    The old handler chimed on both.
    assert say({"index": 0, "segments": ["jarvis"]}) == 1
    assert say({"index": 0, "segments": ["jarvis"]}) == 1, "the final result chimed again"
    print("ok  onresult: a bare wake phrase fires once, not once per delivery")

    # 2. Talking on: the transcript grows and still begins with his name.
    for text in ("jarvis what's", "jarvis what's the", "jarvis what's the weather"):
        assert say({"index": 0, "segments": [text]}) == 1, f"re-fired on {text!r}"
    print("ok  onresult: carrying on talking does not fire again")

    # 3. A genuinely new phrase, in a later segment, does fire — the gates
    #    must not be able to wedge the wake word off.
    page.wait_for_timeout(1600)  # past WAKE_REFIRE_MS
    assert say({"index": 1, "segments": ["jarvis what's the weather", "jarvis"]}) == 2
    assert say({"index": 1, "segments": ["jarvis what's the weather", "jarvis stop"]}) == 2
    print("ok  onresult: a new phrase in a later segment fires once more")

    # 4. Ordinary speech in a new segment is silence.
    assert say({"index": 2, "segments": ["jarvis", "jarvis stop", "the big one"]}) == 2
    print("ok  onresult: ordinary speech in a new segment stays silent")

    # 5. The recognizer restarts every few seconds (onend -> start), and
    #    indices restart with it. An index *below* the last one fired is a new
    #    session, not an older phrase — getting this wrong would silence the
    #    wake word until as many segments had accumulated again.
    page.wait_for_timeout(1600)
    assert say({"index": 0, "segments": ["jarvis"]}) == 3, "a restarted session was ignored"
    print("ok  onresult: a restarted recognition session fires again")

    page.evaluate("() => setWake(false)")


def main() -> int:
    server = ThreadingHTTPServer(
        ("127.0.0.1", PORT), partial(StaticHandler, directory=str(STATIC_DIR))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--use-fake-ui-for-media-stream",
                      "--use-fake-device-for-media-stream"],
            )
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            # Before the page's own script, so `armRecognition` finds it.
            page.add_init_script(FAKE_RECOGNIZER)
            page.goto(f"{BASE}/jarvis.html", wait_until="load")
            page.wait_for_function("() => window.__hud && window.__hud.matchesWake")
            run_checks(page)
            recognizer_checks(page)
            assert not errors, f"page errors: {errors}"
            browser.close()
    finally:
        SHUTDOWN.set()
        server.shutdown()
        server.server_close()

    print("\nall HUD wake-word checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
