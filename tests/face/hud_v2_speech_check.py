"""V6/V7: Jarvis reads agent prose aloud, in a real browser against the mock daemon.

V7 adds the shared queue over every open chat pane (three panes, one hidden), each
chat in its own voice, the voice chip, and the rule that a thread in no pane is silent.

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

from tests.face.hud_v2_layout_check import _choose  # noqa: E402
from tests.face.hud_v2_multichat_check import _fresh, _open, _seed  # noqa: E402

THREE = json.dumps({"preset": "cols3", "focused": 1,
                    "panes": [{"view": "chat"}, {"view": "chat"}, {"view": "chat"}, {"view": "task"}]})

# Counts the audio sources sounding at once, in the page, so "never overlaps" is
# measured on the real Web Audio player and not inferred from the queue.
SOURCES = """
(() => {
  window.__active = 0; window.__maxActive = 0; window.__started = 0;
  const make = BaseAudioContext.prototype.createBufferSource;
  BaseAudioContext.prototype.createBufferSource = function (...args) {
    const node = make.apply(this, args);
    let live = false;
    // The count is dropped by stop() or by the natural end. The 'ended' listener is added
    // here, before the page's own `onended`, so it runs before the next source can start.
    const fin = () => { if (live) { live = false; window.__active -= 1; } };
    node.addEventListener('ended', fin);
    const start = node.start.bind(node);
    node.start = (...a) => {
      live = true; window.__active += 1; window.__started += 1;
      window.__maxActive = Math.max(window.__maxActive, window.__active);
      return start(...a);
    };
    const stop = node.stop.bind(node);
    node.stop = (...a) => { fin(); return stop(...a); };
    return node;
  };
})();
"""
# A hands-free utterance, fed to the fake mic: the follow-up window opens, speech, then quiet.
UTTERANCE = """() => {
  const m = window.__hud.mic;
  m.feedMs(2000, 0.001);
  window.__hud.capture.openFollowUp();
  m.feedMs(1600, 0.06); m.feedMs(2800, 0.0005);
  return true;
}"""
TAGS = {"t1": "Alpha", "t2": "Bravo", "t3": "Charlie", "t9": "Zulu"}


def tag_of(text: str) -> str | None:
    return next((t for t in TAGS.values() if text.startswith(t)), None)


def runs(texts: list[str]) -> list[str]:
    """The thread tags in the order spoken, consecutive repeats folded: A A B B -> A B."""
    out: list[str] = []
    for t in texts:
        tag = tag_of(t)
        if tag and (not out or out[-1] != tag):
            out.append(tag)
    return out

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

            print("\nnested fences")
            nested = "1. Do this first:\n    ```bash\n    cat ~/.config/jarvis/.env\n    export KEY=sk-abcdef\n    ```\nThat was the step."
            mock.emit("text", {"text": nested}, thread_id="t1")
            until(lambda: any("That was the step" in t for t in said(mock)), timeout=5)
            until(lambda: speech(page)["view"]["state"] == "idle", timeout=8)
            check("a fence nested under a list item is never sent to /say",
                  not any(("cat " in t or "sk-abc" in t or "export" in t) for t in said(mock)), str(said(mock)[-3:]))

            print("\ntalking over him")
            mock.ms_per_char = 60
            stream(mock, REPLY)
            mock.emit("text", {"text": REPLY}, thread_id="t1")
            until(lambda: speech(page)["view"]["state"] == "playing", timeout=5)
            page.locator("#orb").dispatch_event("pointerdown")   # the orb is push-to-talk
            check("a talk-key press pauses him", speech(page)["view"]["state"] == "paused")
            page.locator("#orb").dispatch_event("pointerup")   # too short: nothing is sent
            check("and he carries on when nothing was said",
                  bool(until(lambda: speech(page)["view"]["state"] == "playing", timeout=3)))
            page.locator('[data-testid="speech-toggle"]').click()
            page.locator("#orb").dispatch_event("pointerdown"); page.locator("#orb").dispatch_event("pointerup")
            page.wait_for_timeout(300)
            check("paused from the strip first, a release does not resume him", speech(page)["view"]["state"] == "paused")
            page.locator('[data-testid="speech-stop"]').click()
            mock.ms_per_char = 12

            print("\nlong replies become a spoken summary (V8)")
            mock.ms_per_char = 12
            long_prose = " ".join(f"This is sentence number {i} of a long reply, and it says one more thing." for i in range(12))
            long_text = long_prose + "\n" + CODE
            n = len(said(mock))
            stream(mock, long_text)
            mock.emit("text", {"text": long_text}, thread_id="t1")
            until(lambda: len(said(mock)) > n and speech(page)["view"]["state"] == "idle", timeout=10)
            asked = mock.sent("POST", "/speech/summary")
            check("a reply over 600 characters asks for a summary, once", len(asked) == 1, str(asked)[:200])
            check("the summariser got the thread and the prose, never the code",
                  asked[0].get("thread_id") == "t1" and "sentence number 11" in asked[0].get("text", "")
                  and "secret_code" not in asked[0].get("text", ""), str(asked[0])[:200])
            check("only the summary was said: no verbatim lead-in", said(mock)[n:] == [mock.summary_text], str(said(mock)[n:]))
            n = len(said(mock))
            mock.summary_fail = True
            mock.emit("text", {"text": long_text}, thread_id="t1")
            until(lambda: speech(page)["view"]["state"] == "idle" and len(said(mock)) > n + 1, timeout=10)
            tail = said(mock)[n:]
            check("when the summary fails the opening is read, then 'and more on screen'",
                  tail[0].startswith("This is sentence number 0") and tail[-1] == "…and more on screen."
                  and len(" ".join(tail[:-1])) <= 400, str(tail))
            mock.summary_fail = False
            n, m = len(said(mock)), len(mock.sent("POST", "/speech/summary"))
            mock.emit("text", {"text": REPLY}, thread_id="t1")
            until(lambda: len(said(mock)) > n, timeout=5)
            until(lambda: speech(page)["view"]["state"] == "idle", timeout=8)
            check("a short reply is read as written and asks for no summary",
                  len(mock.sent("POST", "/speech/summary")) == m and "I have fixed all seven." in " ".join(said(mock)[n:]))

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
            page.wait_for_timeout(500)    # a request the click started would have landed by now
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

            print("\nthree panes, one hidden: one queue, three voices (V7)")
            ctx.add_init_script(SOURCES)               # takes effect at the reload below
            _fresh(page, mock, until, THREE)
            page.evaluate("() => window.dispatchEvent(new Event('focus'))")
            until(lambda: speech(page)["speaker"], timeout=3)
            for n, tid in ((1, "t1"), (2, "t2"), (3, "t3")):
                _open(page, until, n, tid, "p2" if tid == "t3" else "p1")
            _choose(page, until, "cols2")              # ⊞ folds pane 3 away: its chat is open, not drawn
            until(lambda: not page.locator('[data-testid="pane-3"]').is_visible(), timeout=3)
            until(lambda: all(page.locator(f'[data-testid="pane-{n}-voice"]').count() == 1 for n in (1, 2)), timeout=5)
            check("chats that are merely open have used up no voice",
                  "t2" not in mock.speech_threads and "t3" not in mock.speech_threads, str(mock.speech_threads))
            mock.ms_per_char = 14
            mark = len(mock.sent("POST", "/say"))
            texts = {tid: f"{tag} reply from chat {tid}. {tag} has a second sentence to say here. {tag} has a third one too."
                     for tid, tag in TAGS.items()}
            for tid in TAGS:
                mock.emit("turn_started", {}, thread_id=tid)
            # Deltas of all four chats arrive interleaved, as they would from four live turns.
            for i in range(0, 120, 11):
                for tid in TAGS:
                    mock.emit("text_delta", {"text": texts[tid][i:i + 11]}, thread_id=tid)
            for tid in TAGS:
                mock.emit("text", {"text": texts[tid]}, thread_id=tid)
                mock.emit("turn_finished", {"stop": "end"}, thread_id=tid)
            until(lambda: speech(page)["view"]["state"] == "idle" and len(said(mock)) > mark + 6, timeout=30)
            until(lambda: speech(page)["view"]["state"] == "idle", timeout=30)
            voices = {tid: mock.speech_threads.get(tid, {}).get("voice") for tid in ("t1", "t2", "t3")}
            check("each chat that was read, the hidden one included, was given its own voice by the daemon",
                  all(voices.values()) and len(set(voices.values())) == 3, str(voices))
            check("the chat in no pane was given none", "t9" not in mock.speech_threads)
            check("the voice chips name them", all(
                bool(until(lambda n=n, tid=tid: page.locator(f'[data-testid="pane-{n}-voice"]').get_attribute("data-voice") == voices[tid], timeout=3))
                for n, tid in ((1, "t1"), (2, "t2"))))
            spoken = [b for b in mock.sent("POST", "/say")[mark:]]
            played = [i["text"] for i in speech(page)["past"] if i["state"] == "done"][-len(spoken):]
            order = runs(played)      # the order they SOUNDED in (the /say requests run ahead of it)
            check("the three open chats were read and the chat in no pane was not",
                  sorted(order) == ["Alpha", "Bravo", "Charlie"], str(order))
            check("the hidden pane's chat spoke", "Charlie" in order)
            check("each chat's messages stayed together: no interleaving", len(order) == len(set(order)), str(order))
            by_tag: dict[str, set] = {}
            for b in spoken:
                by_tag.setdefault(tag_of(b["text"]) or "?", set()).add(b.get("voice"))
            want = {TAGS[t]: {v} for t, v in voices.items()}
            check("every chat spoke in its own voice, and only that one", by_tag == want, f"{by_tag} vs {want}")
            check("never more than one sound at a time", page.evaluate("window.__maxActive") == 1,
                  str(page.evaluate("[window.__maxActive, window.__started]")))
            check("and something did sound", page.evaluate("window.__started") >= 3)

            print("\nthe voice chip: skip, mute, voice")
            mock.ms_per_char = 60
            mark = len(mock.sent("POST", "/say"))
            long1 = "Alpha is saying something long. It goes on and on for quite a while. Still more words follow here."
            long2 = "Bravo is waiting behind it. It has a lot to say as well. And then some more words."
            mock.emit("text", {"text": long1}, thread_id="t1")
            mock.emit("text", {"text": long2}, thread_id="t2")
            until(lambda: speech(page)["view"]["state"] == "playing" and speech(page)["view"]["thread"] == "t1", timeout=5)
            check("the chip pulses on the chat being read",
                  page.locator('[data-testid="pane-1-voice"]').get_attribute("data-speaking") == "true"
                  and page.locator('[data-testid="pane-2-voice"]').get_attribute("data-speaking") is None)
            page.locator('[data-testid="pane-1-voice-button"]').click()
            page.locator('[data-testid="pane-1-voice-skip"]').click()
            check("skip this chat moves on to the next chat",
                  bool(until(lambda: speech(page)["view"]["thread"] == "t2" and speech(page)["view"]["state"] == "playing", timeout=5)))
            page.locator('[data-testid="pane-2-voice-button"]').click()
            page.locator('[data-testid="pane-2-voice-mute"]').click()
            check("mute this chat stops it at once", bool(until(lambda: speech(page)["view"]["state"] == "idle", timeout=3)))
            check("and the daemon keeps the mute", mock.speech_threads["t2"]["muted"] is True)
            n = len(said(mock))
            mock.emit("text", {"text": "Bravo again, but muted now."}, thread_id="t2")
            page.wait_for_timeout(600)
            check("a muted chat is not read", len(said(mock)) == n)
            page.locator('[data-testid="pane-2-voice-mute"]').click()
            mock.ms_per_char = 12
            mock.emit("text", {"text": "Bravo is back after the unmute."}, thread_id="t2")
            check("unmuted, it is read again", bool(until(lambda: any("back after the unmute" in t for t in said(mock)), timeout=5)))
            until(lambda: speech(page)["view"]["state"] == "idle", timeout=8)
            print("\nwhile another pane is read, with the self-voice gate off (V6's blanket hold, V9's fallback): hands-free is held off, the talk key is not")
            # V9: with the gate in force hands-free speech is judged frame by frame instead (hud_v2_barge_check.py);
            # the blanket hold is what remains when the owner switches the gate off or it cannot run.
            H.open_settings(page, "Voice")
            page.locator('[data-testid="self-voice-toggle"]').uncheck()
            page.locator('[data-testid="settings-close"]').click()
            sel = page.evaluate("window.__hud.state().selectedChat")
            sel_tid = page.evaluate(f"window.__hud.state().chats[{sel}].threadId")
            reading = "t2" if sel_tid == "t1" else "t1"
            mock.ms_per_char = 60
            mock.emit("text", {"text": "Words from the other pane are being read out to nobody in particular."}, thread_id=reading)
            until(lambda: speech(page)["view"]["state"] == "playing" and speech(page)["view"]["thread"] == reading, timeout=5)
            stt = len(mock.sent("POST", "/stt"))
            page.evaluate(UTTERANCE)
            page.wait_for_timeout(500)
            check("a hands-free utterance in the selected pane is dropped while another pane is read",
                  len(mock.sent("POST", "/stt")) == stt and speech(page)["view"]["state"] == "playing")
            page.locator("#orb").dispatch_event("pointerdown")
            check("the talk key still pauses it", speech(page)["view"]["state"] == "paused")
            page.locator("#orb").dispatch_event("pointerup")
            check("and it carries on when nothing was said",
                  bool(until(lambda: speech(page)["view"]["state"] == "playing", timeout=3)))
            page.locator('[data-testid="speech-stop"]').click()
            page.evaluate("window.__hud.capture.closeFollowUp()")
            H.open_settings(page, "Voice")
            page.locator('[data-testid="self-voice-toggle"]').check()
            page.locator('[data-testid="settings-close"]').click()
            mock.ms_per_char = 12
            pool = [v["name"] for v in mock.world["voices"]["voices"]]
            other = next(v for v in pool if v != voices["t1"])
            if page.locator('[data-testid="pane-1-voice-select"]').count() == 0:    # the chip is still open from skip
                page.locator('[data-testid="pane-1-voice-button"]').click()
            page.locator('[data-testid="pane-1-voice-select"]').select_option(other)
            check("a voice chosen from the chip is the daemon's",
                  bool(until(lambda: mock.speech_threads["t1"]["voice"] == other, timeout=3)))
            mock.emit("text", {"text": "Alpha in a new voice now."}, thread_id="t1")
            until(lambda: any("new voice now" in b["text"] for b in mock.sent("POST", "/say")), timeout=5)
            check("and the next thing it says uses it",
                  [b["voice"] for b in mock.sent("POST", "/say") if "new voice now" in b["text"]] == [other])
            until(lambda: speech(page)["view"]["state"] == "idle", timeout=8)

            print("\nclosing a pane stops its chat")
            mock.ms_per_char = 60
            mock.emit("text", {"text": "Alpha speaks for a long while here. It goes on. And on a bit more."}, thread_id="t1")
            mock.emit("text", {"text": "Bravo is queued behind it and will be dropped. Words words."}, thread_id="t2")
            until(lambda: speech(page)["view"]["state"] == "playing" and speech(page)["view"]["thread"] == "t1", timeout=5)
            page.locator('[data-testid="pane-1-close"]').click()
            check("closing the pane being read silences it at once",
                  bool(until(lambda: speech(page)["view"]["thread"] != "t1" or speech(page)["view"]["state"] != "playing", timeout=3)))
            check("its items were dropped as thread_left",
                  any(i["thread"] == "t1" and i.get("mark") == "thread_left" for i in speech(page)["past"]))
            until(lambda: speech(page)["view"]["state"] == "idle", timeout=15)
            check("still never two at once", page.evaluate("window.__maxActive") == 1)
            mock.ms_per_char = 12

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
