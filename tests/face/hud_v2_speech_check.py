"""V6: Jarvis reads agent prose aloud, in a real browser against the mock daemon.

The mock's `/say` answers a quiet tone as long as the text, so the real Web Audio
player, queue, strip and Web Lock run, and nothing makes a sound the owner hears
(headless Chromium has no output device). No live ports, no real TTS, no network.

Run (after `cd hud && npm run build`), under the usual memory scope and slot:
    PYTHONPATH=. HUD_V2_CHECK_PORT=18476 python tests/face/hud_v2_speech_check.py
Not covered, and not claimed: a physical speaker or the Windows audio stack.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.face.hud_v2_multichat_check import _fresh, _open, _seed  # noqa: E402

ONE = json.dumps({"preset": "single", "focused": 1,
                  "panes": [{"view": "chat"}, {"view": "chat"}, {"view": "file"}, {"view": "task"}]})

REPLY = ("Right, here is what I found. The approval gate had seven separate ways past it, and every one "
         "was an older copy of an idea. I have fixed all seven. ")
CODE = "```py\nsecret_code = 1\n```\n"


def speech(page) -> dict:
    return page.evaluate("window.__hud.speech()")


def stream(mock, text: str, thread="t1", size=9):
    for i in range(0, len(text), size):
        mock.emit("text_delta", {"text": text[i:i + size]}, thread_id=thread)


def said(mock) -> list[str]:
    return [b.get("text", "") for b in mock.sent("POST", "/say")]


def main() -> int:
    from playwright.sync_api import sync_playwright

    from tests.face import hud_v2_check as H
    from tests.face.hud_v2_mock import DIST, MockDaemon

    if not (DIST / "index.html").exists():
        print("hud/dist is not built. Run: cd hud && npm ci && npm run build")
        return 2
    check, until = H.check, H.until
    mock = MockDaemon(0).start()
    try:
        _seed(mock)
        base = f"http://127.0.0.1:{mock.port}"
        with sync_playwright() as pw:
            # Autoplay allowed: the player's own gesture unlock is unit-tested.
            browser = pw.chromium.launch(headless=True, args=[
                "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                "--autoplay-policy=no-user-gesture-required"])
            ctx = browser.new_context(viewport={"width": 1600, "height": 900}, permissions=["microphone"])
            H.guard(ctx)
            ctx.add_init_script(H.FAKE_RECOGNIZER)
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.set_default_timeout(5000)
            before = mock.sse_connections()
            page.goto(base + "/")
            page.wait_for_selector('[data-testid="sidebar"]', state="attached")
            mock.await_reconnect(before)
            _fresh(page, mock, until, ONE)
            _open(page, until, 1, "t1")
            page.evaluate("() => window.dispatchEvent(new Event('focus'))")
            until(lambda: speech(page)["speaker"], timeout=3)

            print("\nreading a reply aloud (V6)")
            mock.emit("turn_started", {}, thread_id="t1")
            stream(mock, "Let me check. " + CODE + REPLY)
            mock.emit("text", {"text": "Let me check. " + CODE + REPLY}, thread_id="t1")
            check("the strip appears while Jarvis speaks",
                  bool(until(lambda: page.locator('[data-testid="speech-strip"]').count() == 1, timeout=5)))
            check("the orb shows his voice", bool(until(lambda: page.locator('#orb').get_attribute("data-state") == "speaking", timeout=5)))
            check("the line under the orb says SPEAKING",
                  "SPEAKING" in page.locator('[data-testid="orb-status"]').inner_text())
            until(lambda: len(speech(page)["past"]) >= 2 and speech(page)["view"]["state"] == "idle", timeout=15)
            texts = said(mock)
            check("chunks were asked of /say in order and local only",
                  len(texts) >= 2 and all(b.get("local_only") is True for b in mock.sent("POST", "/say")),
                  str(texts))
            check("code never reached /say", not any("secret_code" in t for t in texts), str(texts))
            check("the reply's words did", "I have fixed all seven." in " ".join(texts))
            check("every chunk was played to the end", all(i["state"] == "done" for i in speech(page)["past"]))
            check("the strip goes away when speech ends", page.locator('[data-testid="speech-strip"]').count() == 0)

            print("\ncontrols")
            mock.ms_per_char = 60
            stream(mock, REPLY)
            mock.emit("text", {"text": REPLY}, thread_id="t1")
            until(lambda: speech(page)["view"]["state"] == "playing", timeout=5)
            page.locator('[data-testid="speech-toggle"]').click()
            check("pause holds the speech", speech(page)["view"]["state"] == "paused")
            check("the line says it is paused", "PAUSED" in page.locator('[data-testid="orb-status"]').inner_text())
            page.locator('[data-testid="speech-toggle"]').click()
            check("resume plays again", bool(until(lambda: speech(page)["view"]["state"] == "playing", timeout=3)))
            page.locator('[data-testid="speech-stop"]').click()
            check("stop clears the queue", speech(page)["view"]["state"] == "idle" and speech(page)["view"]["queued"] == 0)

            print("\nstale speech")
            stream(mock, REPLY)
            mock.emit("text", {"text": REPLY}, thread_id="t1")
            until(lambda: speech(page)["view"]["state"] == "playing", timeout=5)
            mock.emit("user_message", {"via": "hud"}, thread_id="t1")
            check("a message to the thread stops its speech",
                  bool(until(lambda: speech(page)["view"]["state"] == "idle", timeout=3)))

            print("\nwho is read")
            n = len(said(mock))
            mock.emit("text", {"text": "A reply for a thread no pane shows."}, thread_id="t2")
            page.wait_for_timeout(600)
            check("a thread in no pane is silent", len(said(mock)) == n)

            print("\nthe mic is not the voice")
            page.locator('#orbdock [data-testid="dictation-off"]').click()
            check("mic OFF posts no /mute", not mock.sent("POST", "/mute"))
            mock.ms_per_char = 12
            mock.emit("text", {"text": "Spoken with the microphone off."}, thread_id="t1")
            check("he still speaks with the mic OFF",
                  bool(until(lambda: any("microphone off" in t for t in said(mock)), timeout=5)))
            until(lambda: speech(page)["view"]["state"] == "idle", timeout=8)
            page.locator('#orbdock [data-testid="dictation-review"]').click()

            print("\nfailure is skipped and marked")
            mock.say_fail = True
            mock.emit("text", {"text": "This chunk cannot be made."}, thread_id="t1")
            check("a failed chunk is counted in the strip",
                  bool(until(lambda: speech(page)["view"]["failed"] >= 1, timeout=5)))
            check("and the strip says SKIPPED", "SKIPPED" in page.locator('[data-testid="speech-note"]').inner_text())
            page.locator('[data-testid="speech-stop"]').click()
            mock.say_fail = False

            print("\nread-aloud switch")
            H.open_settings(page, "Voice")
            page.locator('[data-testid="read-aloud-toggle"]').uncheck()
            page.locator('[data-testid="settings-close"]').click()
            n = len(said(mock))
            mock.emit("text", {"text": "Not to be spoken."}, thread_id="t1")
            page.wait_for_timeout(600)
            check("with read-aloud off nothing is asked of /say", len(said(mock)) == n)
            H.open_settings(page, "Voice")
            page.locator('[data-testid="read-aloud-toggle"]').check()
            page.locator('[data-testid="settings-close"]').click()

            print("\none speaking window")
            page2 = ctx.new_page()
            page2.goto(base + "/")
            page2.wait_for_selector('[data-testid="sidebar"]', state="attached")
            page2.wait_for_function("() => window.__hud && window.__hud.speech")
            page2.evaluate("() => window.dispatchEvent(new Event('focus'))")
            check("the newly focused window holds the lock",
                  bool(until(lambda: speech(page2)["speaker"] is True, timeout=4)))
            check("and the other no longer does", bool(until(lambda: speech(page)["speaker"] is False, timeout=4)))
            page.evaluate("() => window.dispatchEvent(new Event('focus'))")
            check("focusing it again takes the lock back",
                  bool(until(lambda: speech(page)["speaker"] is True and speech(page2)["speaker"] is False, timeout=4)))
            page2.close()

            check("no page errors", not errors, "; ".join(errors[:3]))
            ctx.close()
            browser.close()
    finally:
        mock.stop()
    print()
    if H.FAILURES:
        print(f"{len(H.FAILURES)} of {H.CHECKS} checks FAILED:")
        for f in H.FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {H.CHECKS} speech checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
