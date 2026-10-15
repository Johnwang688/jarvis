"""V12: speaker enrollment, verification and Guest voice through the real HUD.

Against the mock daemon (`hud_v2_mock_speaker.py`), on a real Chromium with the
real AudioWorklet fed by an oscillator microphone; no real voice, no live port,
no cloud call. What it proves, end to end:

  - every dictated chunk asks the daemon `verify=1`; before anyone is enrolled the
    daemon skips it and the diagnostics row says so;
  - Settings -> Voice -> Your voice records a take from the microphone, refuses a
    short one without uploading it, sends a good one once as 16-bit 16 kHz WAV,
    proposes a threshold from the daemon's answer, and turns verification on only
    when the owner accepts;
  - a voice the daemon refuses uploads NOTHING to the cloud (the mock counts only
    clips that passed verification), keeps no audio, offers no Retry and says
    NOT YOUR VOICE;
  - Guest voice lets it through, the orb line and the strip say GUEST VOICE at all
    times (also after a reload, and when another window changes it), and the
    diagnostics row records `verification skipped (guest voice)`, not a score;
  - Guest voice changes nothing else: Jarvis's own voice still stops an utterance
    (self-voice gating), a waiting card still stops it, and no approval request is
    ever made from speech.

Run (after `cd hud && npm run build`), under the usual memory scope and slot:
    PYTHONPATH=. HUD_V2_CHECK_PORT=18492 python tests/face/hud_v2_speaker_check.py
Not covered, and not claimed: a physical microphone, real speaker embeddings (the
daemon side is tests/voice_speaker_check.py and tests/v2/speaker_check.py), or the
Windows audio stack.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
assert not (Path(__file__).resolve().parents[2] / ".env").exists(), "use a credential-free worktree"

from playwright.sync_api import sync_playwright  # noqa: E402

from tests.face.hud_v2_check import FAKE_RECOGNIZER, guard_live, open_settings, until, watch_csp  # noqa: E402
from tests.face.hud_v2_chunks_check import CHROME_ARGS, SPEECH_MIC  # noqa: E402
from tests.face.hud_v2_layout_check import _approval  # noqa: E402
from tests.face.hud_v2_mock import MockDaemon  # noqa: E402
from tests.face.hud_v2_multichat_check import _fresh, _open, _seed  # noqa: E402
from tests.face.hud_v2_speech_check import ONE, REPLY, speech, stream  # noqa: E402

PTT = "window.__hud.capture.press(); window.__hud.mic.feedMs(1200, .06); window.__hud.capture.release()"
# A hands-free utterance: the follow-up window opens, speech, then quiet.
UTTERANCE = """() => { const m = window.__hud.mic; m.feedMs(2000, 0.001); window.__hud.capture.openFollowUp();
  m.feedMs(1600, 0.06); m.feedMs(2200, 0.0005); return true; }"""
# About seven seconds of a person-like pattern for the real worklet: 0.9 s on, 0.2 s of breath.
TAKE = [[900, 0.15], [200, 0.0]] * 6 + [[900, 0.15]]


def main() -> int:
    failures: list[str] = []
    count = 0

    def check(name, ok):
        nonlocal count
        count += 1
        print(("ok   " if ok else "FAIL ") + name)
        if not ok:
            failures.append(name)

    mock = MockDaemon(int(os.environ.get("HUD_V2_CHECK_PORT") or 0)).start()
    _seed(mock)
    try:
        base = f"http://127.0.0.1:{mock.port}"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=CHROME_ARGS)
            context = browser.new_context(viewport={"width": 1600, "height": 900})

            def route(r):
                url = urlsplit(r.request.url)
                if url.scheme in ("http", "https") and (url.hostname != "127.0.0.1" or url.port not in {
                        mock.port, mock.workshop_port, mock.api_port, mock.dev_port}):
                    failures.append("request outside isolated mock")
                    r.abort()
                else:
                    r.continue_()
            context.route("**/*", route)
            guard_live(context, failures.append)
            watch_csp(context, failures.append)
            context.add_init_script(FAKE_RECOGNIZER + SPEECH_MIC)
            page = context.new_page()
            page.on("pageerror", lambda e: failures.append(f"page error {e}"))
            page.set_default_timeout(8000)
            before = mock.sse_connections()
            page.goto(base)
            page.wait_for_selector('[data-testid="sidebar"]', state="attached")
            mock.await_reconnect(before)
            page.wait_for_function("() => window.__hud && window.__hud.voiceDiagnostics().mic === 'available' && window.__fm")
            _fresh(page, mock, until, ONE)
            _open(page, until, 1, "t1")
            page.evaluate("() => window.dispatchEvent(new Event('focus'))")
            page.evaluate("window.__hud.mic.feedMs(2000, .0009)")

            def stts():
                return mock.sent("POST", "/stt")

            def box():
                return page.locator('[data-testid="input"]').first.input_value()

            def last_row():
                return page.evaluate("window.__hud.voiceDiagnostics().rows.at(-1)")

            def orb():
                # (the line is upper-cased by CSS, and inner_text sees that)
                return page.locator('[data-testid="orb-status"]').inner_text().upper()

            # --- before enrollment ------------------------------------------------------------
            print("\nnot enrolled")
            mock.world["stt_text"] = "unenrolled words"
            page.evaluate(PTT)
            page.wait_for_function("() => document.querySelector('[data-testid=\"input\"]').value.includes('unenrolled words')")
            check("the chunk asked the daemon to verify it", stts()[-1]["query"].get("verify") == "1")
            check("with nobody enrolled the daemon skipped it and the row says so", last_row().get("verify") == "skipped_unenrolled")
            check("no label while Guest voice is off", "GUEST" not in orb() and page.locator('[data-testid="guest-label"]').count() == 0)
            page.locator('[data-testid="input"]').first.fill("")

            # --- enrollment -------------------------------------------------------------------
            print("\nenrollment")
            open_settings(page, "Voice")
            page.wait_for_selector('[data-testid="speaker-settings"]')
            until(lambda: page.locator('[data-testid="speaker-summary"]').count() > 0)
            check("it says whose voice is accepted", page.locator('[data-testid="speaker-summary"]').inner_text()
                  == "Not enrolled: anyone's voice is accepted.")
            check("the first prompt is shown", "quick brown fox" in page.locator('[data-testid="speaker-prompt"]').inner_text())
            check("the switch reads 'no voice enrolled'", "Guest voice (no voice enrolled)" in page.locator('[data-testid="speaker-settings"]').inner_text())

            page.locator('[data-testid="speaker-record"]').click()
            page.evaluate("window.__fm.speak([[1500, 0.15]])")
            page.wait_for_timeout(1800)
            until(lambda: "s" in page.locator('[data-testid="speaker-meter-text"]').inner_text(), timeout=3)
            page.locator('[data-testid="speaker-stop"]').click()
            until(lambda: page.locator('[data-testid="speaker-problems"]').count() > 0, timeout=3)
            check("a short take says why", "too short" in page.locator('[data-testid="speaker-problems"]').inner_text())
            check("and is not uploaded", len(mock.sent("POST", "/speaker/reading")) == 0)

            page.locator('[data-testid="speaker-record"]').click()
            page.evaluate("t => window.__fm.speak(t)", TAKE)
            page.wait_for_timeout(7600)
            check("the live meter moved", page.locator('[data-testid="speaker-level"]').get_attribute("style") is not None)
            page.locator('[data-testid="speaker-stop"]').click()
            until(lambda: page.locator('[data-testid="speaker-note"]').count() > 0, timeout=6)
            sent = mock.sent("POST", "/speaker/reading")
            check("a good take was sent once, as WAV", len(sent) == 1 and sent[0]["type"] == "audio/wav" and sent[0]["bytes"] > 6 * 16000 * 2)
            check("and kept", page.locator('[data-testid="speaker-note"]').inner_text() == "Reading 1 kept.")
            check("the next prompt is up", bool(until(lambda: "second passage" in page.locator('[data-testid="speaker-prompt"]').inner_text(), timeout=4)))
            check("no analysis is offered with one reading", page.locator('[data-testid="speaker-analyse"]').count() == 0)
            # (two more readings: the daemon side keeps count; this section already proved the recording)
            mock.speaker["readings"] = 3
            page.locator('[data-testid="settings-close"]').click()
            open_settings(page, "Voice")
            until(lambda: page.locator('[data-testid="speaker-analyse"]').count() > 0, timeout=4)
            # First the daemon says the readings do not separate the voice clearly (M2): it must be confirmed.
            mock.speaker["confirm"] = True
            page.locator('[data-testid="speaker-analyse"]').click()
            until(lambda: page.locator('[data-testid="speaker-proposal"]').count() > 0, timeout=4)
            check("a proposal that needs confirming says to record again and offers Accept anyway",
                  "record the readings again" in page.locator('[data-testid="speaker-warning"]').inner_text()
                  and page.locator('[data-testid="speaker-accept"]').inner_text() == "Accept anyway")
            check("nothing is enrolled by looking", mock.speaker["enrolled"] is False)
            mock.speaker["confirm"] = False
            page.locator('[data-testid="speaker-analyse"]').click()
            until(lambda: page.locator('[data-testid="speaker-accept"]').inner_text() == "Accept and turn on", timeout=4)
            check("the proposed threshold and both distributions are shown",
                  page.locator('[data-testid="speaker-proposed"]').inner_text() == "0.47"
                  and page.locator('.spk-hist').count() == 2)
            check("the mock's store is still not enrolled before Accept", mock.speaker["enrolled"] is False)
            stts_before = len(stts())
            page.locator('[data-testid="speaker-accept"]').click()
            until(lambda: mock.speaker["enrolled"], timeout=3)
            until(lambda: "Enrolled" in page.locator('[data-testid="speaker-summary"]').inner_text(), timeout=3)
            check("Accept turns verification on", page.locator('[data-testid="speaker-summary"]').inner_text()
                  == "Enrolled: only your voice is accepted (threshold 0.47).")
            check("a threshold can be tuned", page.locator('[data-testid="speaker-threshold-tune"]').input_value() == "0.47")
            page.screenshot(path=os.environ.get("SPEAKER_SCREENSHOT", "/tmp/voice-v12-enrolled.png"))
            page.locator('[data-testid="settings-close"]').click()

            # --- a refused voice uploads nothing ---------------------------------------------------
            print("\nverification")
            uploads = mock.cloud_uploads
            mock.speaker["reject_next"] = 1
            page.evaluate(PTT)
            until(lambda: len(stts()) > stts_before, timeout=4)
            page.wait_for_function("() => window.__hud.voiceDiagnostics().rows.at(-1).outcome === 'not_owner'")
            check("the chunk was verified", stts()[-1]["query"].get("verify") == "1")
            check("a refusal reached no cloud model", mock.cloud_uploads == uploads)
            check("one attempt, no automatic retry", len(stts()) == stts_before + 1)
            page.wait_for_timeout(2600)
            check("still one attempt after the retry delay", len(stts()) == stts_before + 1)
            check("the orb says NOT YOUR VOICE", "NOT YOUR VOICE" in orb())
            check("no Retry is offered and no audio is kept",
                  page.locator('[data-testid="stt-retry"]').count() == 0 and not last_row().get("kept"))
            check("the row says one part was refused", last_row().get("rejected") == 1)
            check("nothing reached the chat box or the chat", box() == "" and not any(p.endswith("/send") for m, p, b in mock.calls))
            check("the window shows no error line", page.evaluate("window.__hud.state().error") == "")

            mock.world["stt_text"] = "owner words"
            page.evaluate(PTT)
            page.wait_for_function("() => document.querySelector('[data-testid=\"input\"]').value.includes('owner words')")
            check("the owner's voice goes through and the row says matched", last_row().get("verify") == "matched"
                  and mock.cloud_uploads == uploads + 1)
            page.locator('[data-testid="input"]').first.fill("")

            # --- Guest voice -------------------------------------------------------------------
            print("\nGuest voice")
            open_settings(page, "Voice")
            page.locator('[data-testid="guest-switch"]').click()
            until(lambda: mock.speaker["guest"], timeout=3)
            page.locator('[data-testid="settings-close"]').click()
            until(lambda: "GUEST VOICE" in orb(), timeout=3)
            check("the daemon holds the switch", mock.speaker["guest"] is True)
            check("the orb line says GUEST VOICE", "GUEST VOICE" in orb())
            check("so does the strip, with nothing playing", page.locator('[data-testid="guest-label"]').inner_text() == "GUEST VOICE")
            mock.speaker["reject_next"] = 1                  # a refusal that must not happen: Guest skips the check
            uploads = mock.cloud_uploads
            mock.world["stt_text"] = "a guest speaking"
            page.evaluate(PTT)
            page.wait_for_function("() => document.querySelector('[data-testid=\"input\"]').value.includes('a guest speaking')")
            check("the guest's words go through", mock.cloud_uploads == uploads + 1 and mock.speaker["reject_next"] == 1)
            check("the row records the skip, not a score", last_row().get("verify") == "skipped_guest")
            page.locator('[data-testid="input"]').first.fill("")
            open_settings(page, "Voice")
            check("the diagnostics say so in words", "verification skipped (guest voice)" in page.locator('[data-testid="voice-diagnostics"]').inner_text())
            check("Settings says it is on", page.locator('[data-testid="speaker-summary"]').inner_text().startswith("Guest voice is ON"))
            page.locator('[data-testid="settings-close"]').click()

            before = mock.sse_connections()
            page.reload()
            page.wait_for_selector('[data-testid="sidebar"]', state="attached")
            mock.await_reconnect(before)
            until(lambda: page.locator('[data-testid="guest-label"]').count() > 0, timeout=5)
            check("after a reload the label is still there (the daemon holds it)", "GUEST VOICE" in orb()
                  and page.locator('[data-testid="guest-label"]').count() == 1)

            # Another window turns it off: this one learns from the bus.
            mock.speaker["guest"] = False
            mock.emit("speaker_guest", {"guest": False, "enrolled": True, "mode": "on"})
            until(lambda: page.locator('[data-testid="guest-label"]').count() == 0, timeout=3)
            check("another window's change reaches this one at once", "GUEST" not in orb())
            mock.speaker["guest"] = True
            mock.emit("speaker_guest", {"guest": True, "enrolled": True, "mode": "guest"})
            until(lambda: page.locator('[data-testid="guest-label"]').count() == 1, timeout=3)

            # --- a gate change nobody told this window about (M3) -----------------------------------------
            print("\nthe gate changes behind the routes")
            mock.speaker["guest"] = False
            mock.emit("speaker_guest", {"guest": False, "enrolled": True, "mode": "on"})
            until(lambda: page.locator('[data-testid="guest-label"]').count() == 0, timeout=3)
            # The voiceprint is deleted behind everyone's back: the daemon publishes what it sees.
            mock.speaker["enrolled"] = False
            mock.emit("speaker_guest", {"guest": False, "enrolled": False, "mode": "off"})
            until(lambda: "VOICE CHECK OFF" in orb(), timeout=3)
            check("the voice check being off is said on the orb line once a voice had been enrolled", "VOICE CHECK OFF (NOT ENROLLED)" in orb())
            check("and on the strip", page.locator('[data-testid="guest-label"]').inner_text().upper() == "VOICE CHECK OFF (NOT ENROLLED)")
            mock.speaker["enrolled"] = True
            mock.emit("speaker_guest", {"guest": False, "enrolled": True, "mode": "on"})
            until(lambda: page.locator('[data-testid="guest-label"]').count() == 0, timeout=3)
            # guest.json edited by hand: no event, and the next transcript's reply says the check was skipped.
            mock.speaker["guest"] = True
            mock.world["stt_text"] = "unannounced guest"
            page.evaluate(PTT)
            page.wait_for_function("() => document.querySelector('[data-testid=\"input\"]').value.includes('unannounced guest')")
            until(lambda: page.locator('[data-testid="guest-label"]').count() == 1, timeout=4)
            check("a skip the window did not expect puts the label up from the reply alone", "GUEST VOICE" in orb())
            page.locator('[data-testid="input"]').first.fill("")
            mock.speaker["guest"] = True

            # --- Guest voice leaves the other gates alone -------------------------------------------------
            print("\nthe other gates")
            _fresh(page, mock, until, ONE)
            _open(page, until, 1, "t1")
            page.evaluate("() => window.dispatchEvent(new Event('focus'))")
            until(lambda: speech(page)["speaker"], timeout=3)
            check("Guest voice is still on", page.locator('[data-testid="guest-label"]').count() == 1)
            mock.emit("turn_started", {}, thread_id="t1")
            stream(mock, REPLY * 3)
            mock.emit("text", {"text": REPLY * 3}, thread_id="t1")
            until(lambda: speech(page)["view"]["state"] in ("playing", "buffering"), timeout=6)
            check("Jarvis is speaking, and the strip carries both", page.locator('[data-testid="speech-note"]').count() == 1
                  and page.locator('[data-testid="guest-label"]').count() == 1)
            n = len(stts())
            page.evaluate(UTTERANCE)
            until(lambda: page.evaluate("window.__hud.voiceDiagnostics().rows.some(r => r.outcome === 'suppressed:speaking')"), timeout=4)
            check("his own voice still never opens an utterance with Guest voice on",
                  page.evaluate("window.__hud.voiceDiagnostics().rows.some(r => r.outcome === 'suppressed:speaking')") and len(stts()) == n)
            mock.emit("turn_finished", {"stop": "end"}, thread_id="t1")
            until(lambda: speech(page)["view"]["state"] == "idle", timeout=25)

            _approval(mock, "req-speaker-1", "rm -rf build")
            until(lambda: page.locator('[data-testid="approval"], [data-testid="approval-card"]').count() > 0
                  or page.evaluate("window.__hud.state().approvals.length") > 0, timeout=4)
            n = len(stts())
            page.evaluate(UTTERANCE)
            page.evaluate(PTT)
            page.wait_for_timeout(600)
            check("a waiting card still stops dictation with Guest voice on", len(stts()) == n)
            check("and no approval request or decision was made from speech",
                  not any(p.startswith("/approvals") for m, p, b in mock.calls if m == "POST"))

            # --- turning it off from the strip -------------------------------------------------------
            print("\nturning it off")
            mock.emit("approval_resolved", {"req_id": "req-speaker-1", "decision": "deny"})
            until(lambda: page.evaluate("window.__hud.state().approvals.length") == 0, timeout=4)
            page.locator('[data-testid="guest-toggle"]').click()
            until(lambda: mock.speaker["guest"] is False, timeout=3)
            until(lambda: page.locator('[data-testid="guest-label"]').count() == 0, timeout=3)
            check("the strip's button turns it off, the daemon agrees, the label is gone",
                  mock.speaker["guest"] is False and "GUEST" not in orb())

            # --- Delete ---------------------------------------------------------------------------------
            open_settings(page, "Voice")
            page.locator('[data-testid="speaker-delete"]').click()
            page.locator('[data-testid="speaker-delete-confirm"]').click()
            until(lambda: not mock.speaker["enrolled"], timeout=3)
            until(lambda: page.locator('[data-testid="speaker-summary"]').inner_text().startswith("Not enrolled"), timeout=3)
            check("Delete takes it all away and the section reads 'Not enrolled' again",
                  mock.speaker["readings"] == 0 and mock.speaker["guest"] is False)

            browser.close()
    finally:
        mock.stop()
    print(f"\n{count} checks, {len(failures)} failed")
    for f in failures:
        print("  FAILED:", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
