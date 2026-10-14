"""V2 STT reliability through the real HUD: retained clips, Retry, ordering.

Isolated fake microphone and a scripted mock `/stt`; no real audio, external
call, credential or live daemon port. Deadlines (15 s minimum) are pinned by
the fake-clock vitest suite (hud/src/lib/stt.test.ts), not waited out here.

Run with temporary HOME/config, dummy keys, worktree PYTHONPATH and a 3G/0swap
systemd scope.
"""
from __future__ import annotations
import os
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
assert not (REPO / ".env").exists(), "use a credential-free worktree"
from playwright.sync_api import sync_playwright
from tests.face.hud_v2_mock import MockDaemon
from tests.face.hud_v2_check import FAKE_RECOGNIZER, guard_live, watch_csp, open_settings
from tests.face.hud_v2_voice_diagnostics_check import FAKE_MIC

PTT = "window.__hud.capture.press(); window.__hud.mic.feedMs(1200, .06); window.__hud.capture.release()"


def main():
    failures: list[str] = []
    count = 0

    def check(name, ok):
        nonlocal count
        count += 1
        print(("ok   " if ok else "FAIL ") + name)
        if not ok:
            failures.append(name)

    mock = MockDaemon(0).start()
    # A second thread, so the destination can change while words are in flight.
    t2 = dict(mock.world["threads"][0], id="t2", title="other chat")
    mock.world["threads"].append(t2)
    mock.world["transcripts"]["t2"] = []
    try:
        base = f"http://127.0.0.1:{mock.port}"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 900})

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
            context.add_init_script(FAKE_RECOGNIZER + FAKE_MIC)
            page = context.new_page()
            page.on("pageerror", lambda e: failures.append(f"page error {e}"))
            page.goto(base)
            page.wait_for_selector('[data-testid="sidebar"]')
            page.wait_for_function("() => window.__hud && window.__hud.voiceDiagnostics().mic === 'available'")
            page.evaluate("window.__hud.mic.feedMs(2000, .0009)")

            def stts():
                return mock.sent("POST", "/stt")

            def box():
                return page.locator('[data-testid="input"]').first.input_value()

            # --- failure -> automatic retry once -> visible failure, clip kept
            mock.stt_script = [{"status": 502, "error": "speech-to-text unavailable: a/one (HTTP 503)"},
                               {"status": 502, "error": "speech-to-text unavailable: a/one (HTTP 503)"}]
            page.evaluate(PTT)
            page.wait_for_selector('[data-testid="stt-retry"]', timeout=15000)
            check("one automatic retry, then stop", len(stts()) == 2)
            check("failure is visible in the Status pane", "Couldn't transcribe" in page.locator('[data-testid="stt-retry"]').inner_text())
            check("failure is said under the orb", "STT FAILED" in page.locator('[data-testid="orb-status"]').inner_text())
            first = stts()[0]
            check("the clip is named for the daemon and its deadline sent (utterance_id, seq, budget_ms)",
                  first["query"] == {"utterance_id": "1", "seq": "1", "budget_ms": "15000"})
            check("both attempts sent the same bytes", stts()[1]["bytes"] == first["bytes"])
            check("nothing was sent to a chat", not any(p.endswith("/send") for m, p, b in mock.calls))
            check("diagnostics say the audio is kept", page.evaluate(
                "window.__hud.voiceDiagnostics().rows.at(-1)") .get("kept") is True)

            # --- explicit Retry re-sends the retained clip and places the text
            mock.stt_script = [{"text": "retried words"}]
            page.locator('[data-testid="stt-retry-button"]').click()
            page.wait_for_function("() => document.querySelector('[data-testid=\"input\"]').value.includes('retried words')")
            check("Retry sent the kept clip once", len(stts()) == 3 and stts()[2]["bytes"] == first["bytes"])
            check("REVIEW placed the text unsent", box().strip() == "retried words"
                  and not any(p.endswith("/send") for m, p, b in mock.calls))
            check("the retry bar is gone and the audio released", page.locator('[data-testid="stt-retry"]').count() == 0
                  and page.evaluate("window.__hud.voiceDiagnostics().rows.at(-1).kept") is False)
            check("the window error line cleared", page.evaluate("window.__hud.state().error") == "")
            page.locator('[data-testid="input"]').first.fill("")

            # --- out-of-order replies are placed in capture order
            hold = threading.Event()
            mock.stt_script = [{"text": "alpha", "wait": hold}, {"text": "bravo"}]
            n = len(stts())
            page.evaluate(PTT)
            page.evaluate("window.__hud.mic.feedMs(1500, .0009)")
            page.evaluate(PTT)
            for _ in range(100):
                if len(stts()) >= n + 2:
                    break
                page.wait_for_timeout(50)
            page.wait_for_timeout(400)           # bravo has answered; alpha has not
            check("the later reply waits for the earlier clip", box().strip() == "")
            hold.set()
            page.wait_for_function("() => document.querySelector('[data-testid=\"input\"]').value.includes('bravo')")
            check("placed in the order spoken, not the order answered", box().strip() == "alpha bravo")
            page.locator('[data-testid="input"]').first.fill("")

            # --- AUTO sends only when nothing moved since the words were said
            def sent_to():
                return [p for m, p, b in mock.calls if m == "POST" and p.endswith("/send")]

            def press_held():
                """PTT while /stt is held; returns the Event that releases the answer."""
                gate = threading.Event()
                mock.stt_script = [{"text": "dictated words", "wait": gate}]
                page.evaluate("window.__hud.mic.feedMs(1500, .0009)")
                page.evaluate(PTT)
                for _ in range(100):
                    if not mock.stt_script:
                        break
                    page.wait_for_timeout(50)
                return gate

            def box_has(text):
                try:
                    page.wait_for_function("(t) => document.querySelector('[data-testid=\"input\"]').value.includes(t)", arg=text, timeout=5000)
                    return True
                except Exception:
                    return False

            page.locator('[data-testid="tab-chat"]').click()
            page.locator('[data-testid="thread-t1"]').click()
            page.locator('[data-testid="dictation-auto"]').click()
            sends = len(sent_to())
            gate = press_held()
            gate.set()
            for _ in range(60):
                if len(sent_to()) > sends:
                    break
                page.wait_for_timeout(50)
            check("positive control: AUTO on the same idle chat sends", len(sent_to()) == sends + 1 and sent_to()[-1].endswith("/t1/send"))

            mock.emit("turn_finished", {"stop": "end"}, thread_id="t1")
            page.wait_for_function("() => !window.__hud.state().busy")
            sends = len(sent_to())
            gate = press_held()
            page.locator('[data-testid="thread-t2"]').click()
            page.wait_for_function("() => Object.values(window.__hud.state().chats).some(c => c.threadId === 't2')")
            gate.set()
            check("AUTO: a different chat selected before the transcript landed -> box, unsent", box_has("dictated words"))
            check("and nothing was sent to either chat", len(sent_to()) == sends)
            check("and the window says the words came from another chat",
                  "ANOTHER CHAT" in page.locator('[data-testid="orb-status"]').inner_text().upper())
            page.locator('[data-testid="input"]').first.fill("")

            gate = press_held()
            mock.emit("turn_started", {}, thread_id="t2")
            page.wait_for_function("() => window.__hud.state().busy")
            check("setup: the selected chat is running t2's turn", page.evaluate("window.__hud.state().turnThreadId") == "t2")
            check("a busy chat keeps its own orb while words are transcribed", page.evaluate("window.__hud.state().orb") != "transcribing")
            gate.set()
            check("AUTO: the chat started a turn meanwhile -> box, not a steer", box_has("dictated words"))
            check("and nothing was sent or steered", len(sent_to()) == sends)
            mock.emit("turn_finished", {"stop": "end"}, thread_id="t2")
            page.wait_for_function("() => !window.__hud.state().busy")
            page.locator('[data-testid="input"]').first.fill("")

            mock.stt_script = [{"status": 400, "error": "bad audio"}]
            page.evaluate("window.__hud.mic.feedMs(1500, .0009)")
            page.evaluate(PTT)
            page.wait_for_selector('[data-testid="stt-retry"]', timeout=15000)
            mock.stt_script = [{"text": "after retry"}]
            page.locator('[data-testid="stt-retry-button"]').click()
            check("AUTO: text produced by Retry goes to the box, never sent", box_has("after retry") and len(sent_to()) == sends)
            page.locator('[data-testid="input"]').first.fill("")
            page.locator('[data-testid="dictation-review"]').click()

            # --- Retry is refused while the mic is OFF (nothing uploads)
            mock.stt_script = [{"status": 400, "error": "audio is empty"}]
            page.evaluate("window.__hud.mic.feedMs(1500, .0009)")
            page.evaluate(PTT)
            page.wait_for_selector('[data-testid="stt-retry"]', timeout=15000)
            before = len(stts())
            page.locator('[data-testid="dictation-off"]').click()
            page.locator('[data-testid="stt-retry-button"]').click()
            page.wait_for_timeout(600)
            check("OFF: Retry uploads nothing", len(stts()) == before)
            check("OFF: Retry says why", "NOT RETRIED" in page.locator('[data-testid="orb-status"]').inner_text())
            check("OFF: and on the error line, which a busy chat cannot swallow", "Not retried" in page.evaluate("window.__hud.state().error"))
            page.locator('[data-testid="dictation-review"]').click()

            # --- Settings -> Voice lists the kept clip with Play and Retry
            open_settings(page, "Voice")
            check("diagnostics row offers Play and Retry", page.locator('[data-testid="voice-clip-play"]').count() == 1
                  and page.locator('[data-testid="voice-clip-retry"]').count() == 1)
            mock.stt_script = [{"text": "from diagnostics"}]
            page.locator('[data-testid="voice-clip-retry"]').click()
            page.wait_for_function("() => window.__hud.voiceDiagnostics().rows.some(r => r.outcome === 'review' && r.kept === false)")
            check("Retry from diagnostics placed the text", box_has("from diagnostics"))
            check("no Play/Retry left once placed", page.locator('[data-testid="voice-clip-play"]').count() == 0)
            page.screenshot(path=os.environ.get("STT_SCREENSHOT", "/tmp/voice-v2-stt.png"))
            context.close()
            browser.close()
        print(f"{count - len(failures)}/{count} checks passed; {len(failures)} failures")
        if failures:
            print("\n".join(failures))
        return 1 if failures else 0
    finally:
        mock.stop()


if __name__ == "__main__":
    raise SystemExit(main())
