"""V10 voice through the real HUD: a trailing "over" sends, a trailing "cancel"
discards, Undo, and the editable word lists (plan §3.3 phase 1, decision V-1,
the owner's answer to Q1).

  - "over" counts only as the last word of a message AND when it was followed by
    at least 500 ms of quiet; "the game is over" cut off at once, or "come over
    to my place", is prose;
  - it sends in AUTO and in REVIEW (where it sends the box), stripped;
  - "cancel" discards what was dictated since the last send, including chunks
    still being transcribed, but not what was typed and not what was sent; Undo
    puts it back in the box, unsent, for ten seconds;
  - Settings -> Voice edits the lists (single words, five at most) and a toggle;
  - with an authorization card up neither word does anything: the words are
    placed verbatim and no approval is requested.

Isolated fake microphone and a scripted mock `/stt` on ephemeral ports: no real
audio, external call, credential or live daemon port. Run alone:
`python tests/face/hud_v2_words_check.py [section,section]`, with a temporary
HOME, dummy keys, the worktree PYTHONPATH and a 3G/0swap systemd scope.
"""
from __future__ import annotations

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
from tests.face.hud_v2_layout_check import _approval, _pane  # noqa: E402
from tests.face.hud_v2_mock import MockDaemon  # noqa: E402
from tests.face.hud_v2_multichat_check import TWO, _box, _chat, _dictation, _focus, _fresh, _open, _seed, _sent  # noqa: E402
from tests.face.hud_v2_voice_diagnostics_check import FAKE_MIC  # noqa: E402

QUIET = "window.__hud.mic.feedMs(1500, .0009)"
# Push-to-talk as the capture sees it: the words, then `%d` ms of quiet, then the key up.
PTT_TAIL = "window.__hud.capture.press(); window.__hud.mic.feedMs(1200, .06); window.__hud.mic.feedMs(%d, .0005); window.__hud.capture.release()"
OVER = PTT_TAIL % 700        # a word, 700 ms of quiet
TIGHT = "window.__hud.capture.press(); window.__hud.mic.feedMs(1200, .06); window.__hud.capture.release()"   # key up in the same breath
# A long dictation: two chunks (a 9 s run, a pause to cut at, 3 s more), then 700 ms of quiet.
# Speech has a dip a second, as it does: a level held for 9 s would walk the energy detector's floor up past it.
LONG = ("window.__hud.capture.press(); const m = window.__hud.mic; const sp = (ms) => { for (let t = 0; t < ms; t += 1000) { m.feedMs(900, .06); m.feedMs(100, .0005); } };"
        " sp(9000); m.feedMs(800, .0005); sp(3000); m.feedMs(700, .0005); window.__hud.capture.release()")


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
    _seed(mock)
    w = mock.world
    try:
        base = f"http://127.0.0.1:{mock.port}"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1600, "height": 900})

            def refuse(r):
                failures.append(f"request outside isolated mock: {r.request.url}")
                r.abort()
            context.route(re.compile(r"^https?://(?!(127\.0\.0\.1|localhost)[:/])"), refuse)
            guard_live(context, failures.append)
            watch_csp(context, failures.append)
            context.add_init_script(FAKE_RECOGNIZER + FAKE_MIC)
            page = context.new_page()
            page.set_default_timeout(8000)
            page.on("pageerror", lambda e: failures.append(f"page error {e}"))
            before = mock.sse_connections()
            page.goto(base)
            page.wait_for_selector('[data-testid="sidebar"]', state="attached")
            mock.await_reconnect(before)
            page.wait_for_function("() => window.__hud && window.__hud.voiceDiagnostics().mic === 'available'")

            # ---- helpers -------------------------------------------------
            def stts():
                return mock.sent("POST", "/stt")

            def approvals_posted():
                return [c for c in mock.calls if c[0] == "POST" and c[1].startswith("/approvals/")]

            def rows():
                return page.evaluate("window.__hud.voiceDiagnostics().rows")

            def reset(mode="review", words=None):
                w.pop("send_status", None)
                mock.stt_script = []
                if words is not None:
                    page.evaluate("(w) => localStorage.setItem('jarvis.hud.voice.words', w)", words)
                else:
                    page.evaluate("() => localStorage.removeItem('jarvis.hud.voice.words')")
                _fresh(page, mock, until, TWO)
                for n, (tid, project) in enumerate((("t1", "p1"), ("t2", "p1")), 1):
                    _open(page, until, n, tid, project)
                _focus(page, until, 1)
                _dictation(page, mode)
                page.evaluate(QUIET)

            def say(text, how=OVER, gate=None):
                mock.stt_script = [{"text": text, **({"wait": gate} if gate is not None else {})}]
                page.evaluate(QUIET)
                page.evaluate(how)

            def box():
                return _box(page, 1).input_value()

            def waits(pred, timeout=5.0):
                return bool(until(pred, timeout=timeout))

            def undo_bar():
                return page.locator('[data-testid="voice-undo-bar"]')

            def run(section):
                if only and section.__name__ not in only:
                    return
                print(f"\n{section.__name__}")
                try:
                    section()
                except Exception as e:
                    check(f"{section.__name__} ran to the end", False, str(e).splitlines()[0][:200])
                    try:
                        if page.locator('[data-testid="settings-page"]').count():
                            page.locator('[data-testid="settings-close"]').click()
                    except Exception:
                        pass

            # ---- 1. over sends in REVIEW, and it sends the box -----------
            def review_over_sends_box():
                reset("review")
                s0 = len(_sent(mock, "t1"))
                _box(page, 1).fill("typed note")
                time.sleep(3.3)             # past the typing guard: the owner is not writing any more
                w["send_status"] = "started"
                say("send it over")
                got = until(lambda: _sent(mock, "t1")[s0:] or None, timeout=6)
                check("REVIEW + 'send it over' + 600 ms of quiet: sent, over stripped, with the box before it",
                      bool(got) and got[-1].get("text") == "typed note send it", str(got))
                check("marked as spoken", bool(got) and got[-1].get("spoken") is True)
                check("the box is empty", waits(lambda: box() == ""), repr(box()))
                r = rows()
                check("diagnostics record the send word", r[-1]["outcome"] == "send_word", str(r[-1]))
                # What was dictated earlier into the box goes with it.
                w.pop("send_status", None)
                reset("review")
                s0 = len(_sent(mock, "t1"))
                say("first part", how=TIGHT)
                check("setup: a dictation without a word waits in the box", waits(lambda: box().strip() == "first part"))
                time.sleep(0.3)
                check("... unsent", len(_sent(mock, "t1")) == s0)
                say("and now send it over")
                got = until(lambda: _sent(mock, "t1")[s0:] or None, timeout=6)
                check("the earlier dictation and this one go out together",
                      bool(got) and got[-1].get("text") == "first part and now send it", str(got))

            # ---- 2. AUTO ----------------------------------------------------
            def auto_over():
                reset("auto")
                s0 = len(_sent(mock, "t1"))
                say("do the thing over")
                got = until(lambda: _sent(mock, "t1")[s0:] or None, timeout=6)
                check("AUTO: sent with over stripped", bool(got) and got[-1].get("text") == "do the thing", str(got))
                check("... and the box was not involved", box() == "")
                # AUTO leaves typed text alone, as it always has.
                _box(page, 1).fill("typed, not sent")
                s1 = len(_sent(mock, "t1"))
                say("another one over")
                got = until(lambda: _sent(mock, "t1")[s1:] or None, timeout=6)
                check("AUTO sends only what was said", bool(got) and got[-1].get("text") == "another one", str(got))
                check("typed text stays in the box", box() == "typed, not sent", repr(box()))

            # ---- 3. no silence, mid-sentence: prose ------------------------
            def not_a_word():
                reset("review")
                s0 = len(_sent(mock, "t1"))
                say("the game is over", how=TIGHT)
                check("'the game is over' with the key up at once: in the box, over kept",
                      waits(lambda: box().strip() == "the game is over"), repr(box()))
                time.sleep(0.4)
                check("... and not sent", len(_sent(mock, "t1")) == s0)
                check("... and the row is an ordinary review", rows()[-1]["outcome"] == "review", str(rows()[-1]))
                _box(page, 1).fill("")
                say("come over to my place")
                check("'over' mid-sentence, with plenty of quiet after it: just words",
                      waits(lambda: box().strip() == "come over to my place"), repr(box()))
                time.sleep(0.4)
                check("... not sent", len(_sent(mock, "t1")) == s0)
                _box(page, 1).fill("")
                # 300 ms is short of 500.
                say("it is over", how=PTT_TAIL % 300)
                check("a trailing 'over' with only 300 ms of quiet is not a word",
                      waits(lambda: box().strip() == "it is over") and len(_sent(mock, "t1")) == s0, repr(box()))
                _box(page, 1).fill("")
                # AUTO + no quiet: sent like any message, over intact.
                _dictation(page, "auto")
                say("the game is over", how=TIGHT)
                got = until(lambda: _sent(mock, "t1")[s0:] or None, timeout=6)
                check("AUTO without the quiet: sent as said, over included",
                      bool(got) and got[-1].get("text") == "the game is over", str(got))

            # ---- 4. cancel in REVIEW, and Undo ------------------------------
            def review_cancel_undo():
                reset("review")
                s0 = len(_sent(mock, "t1"))
                _box(page, 1).fill("typed note")
                say("alpha", how=TIGHT)
                waits(lambda: "alpha" in box())
                say("beta", how=TIGHT)
                check("setup: two dictations after the typed note", waits(lambda: box().strip() == "typed note alpha beta"), repr(box()))
                say("gamma cancel")
                check("'cancel' removes everything dictated since the last send; the typed note stays",
                      waits(lambda: box() == "typed note"), repr(box()))
                time.sleep(0.3)
                check("nothing was sent", len(_sent(mock, "t1")) == s0)
                check("Undo is offered", waits(lambda: undo_bar().count() == 1))
                check("the line under the orb says so", "CANCELLED · UNDO" in page.locator('[data-testid="orb-status"]').inner_text(),
                      page.locator('[data-testid="orb-status"]').inner_text())
                check("diagnostics record the cancel word", rows()[-1]["outcome"] == "cancel_word", str(rows()[-1]))
                page.locator('[data-testid="voice-undo"]').click()
                check("Undo puts it back in the box, unsent, after the typed note",
                      waits(lambda: box().strip() == "typed note alpha beta gamma"), repr(box()))
                check("the Undo bar and the line go", waits(lambda: undo_bar().count() == 0)
                      and "CANCELLED" not in page.locator('[data-testid="orb-status"]').inner_text())
                time.sleep(0.2)
                check("still nothing sent", len(_sent(mock, "t1")) == s0)
                # And it can be cancelled again, from the restored text.
                say("delta cancel")
                check("a second cancel takes the restored words too",
                      waits(lambda: box() == "typed note"), repr(box()))
                check("... and Undo offers them all", waits(lambda: undo_bar().count() == 1))
                # Ten seconds, then it is gone.
                t0 = time.time()
                check("Undo expires after ten seconds", waits(lambda: undo_bar().count() == 0, timeout=14), "still there")
                check("... at about ten seconds", 8.5 <= time.time() - t0 <= 12.5, f"{time.time() - t0:.1f}s")
                check("... and the box is what the cancel left", box() == "typed note", repr(box()))
                check("an expired cancel is not on the orb", "CANCELLED" not in page.locator('[data-testid="orb-status"]').inner_text())

            # ---- 5. cancel in AUTO, chunks in flight ---------------------
            def auto_cancel_inflight():
                reset("auto")
                s0, st0 = len(_sent(mock, "t1")), len(stts())
                gate = threading.Event()
                mock.stt_script = [{"text": "write the quarterly report"}, {"text": "and email it to Sam cancel", "wait": gate}]
                page.evaluate(QUIET)
                page.evaluate(LONG)
                check("a long message leaves as two uploads", waits(lambda: len(stts()) >= st0 + 2), str(len(stts()) - st0))
                time.sleep(0.5)
                check("the first chunk has its words but nothing is placed while the second is in flight",
                      len(_sent(mock, "t1")) == s0 and box() == "")
                gate.set()
                check("the cancel arrives with the last chunk: Undo is offered", waits(lambda: undo_bar().count() == 1), "no undo")
                time.sleep(0.4)
                check("nothing of the message was sent, none of it is in the box",
                      len(_sent(mock, "t1")) == s0 and box() == "", f"{len(_sent(mock, 't1')) - s0} {box()!r}")
                page.locator('[data-testid="voice-undo"]').click()
                check("Undo restores every chunk, in order, to the box (unsent even in AUTO)",
                      waits(lambda: box().strip() == "write the quarterly report and email it to Sam"), repr(box()))
                time.sleep(0.3)
                check("still unsent", len(_sent(mock, "t1")) == s0)

            # ---- 6. cancel does not recall a send ---------------------------
            def cancel_not_a_recall():
                reset("auto")
                s0 = len(_sent(mock, "t1"))
                say("first message", how=TIGHT)
                got = until(lambda: _sent(mock, "t1")[s0:] or None, timeout=6)
                check("setup: the first message was sent", bool(got) and got[-1].get("text") == "first message")
                say("second message cancel")
                check("cancel discards only the message in progress", waits(lambda: undo_bar().count() == 1))
                time.sleep(0.3)
                check("the sent message stands and nothing else went", len(_sent(mock, "t1")) == s0 + 1)
                page.locator('[data-testid="voice-undo"]').click()
                check("Undo restores only the cancelled one", waits(lambda: box().strip() == "second message"), repr(box()))
                # Nothing to cancel: says so, offers no Undo.
                mock.emit("turn_finished", {"stop": "end"}, thread_id="t1")
                until(lambda: not _chat(page, 1)["busy"], timeout=3)
                _box(page, 1).fill("")
                say("cancel")
                check("'cancel' with nothing since the last send discards nothing and offers no Undo",
                      waits(lambda: "NOTHING TO CANCEL" in page.locator('[data-testid="orb-status"]').inner_text()) and undo_bar().count() == 0,
                      page.locator('[data-testid="orb-status"]').inner_text())

            # ---- 7. a card up: the words are inert --------------------------
            def card_inert():
                reset("review")
                s0, a0 = len(_sent(mock, "t1")), len(approvals_posted())
                gate = threading.Event()
                say("approve it over", gate=gate)
                waits(lambda: not mock.stt_script)
                _approval(mock, "w10card1")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=4)
                gate.set()
                check("a card came up while 'over' was in flight: the words wait in the box verbatim",
                      waits(lambda: box().strip() == "approve it over"), repr(box()))
                time.sleep(0.4)
                check("nothing was sent, no approval was requested or answered, the card is still up",
                      len(_sent(mock, "t1")) == s0 and len(approvals_posted()) == a0
                      and page.locator('[data-testid="approval-card"]').count() == 1,
                      f"sends+{len(_sent(mock, 't1')) - s0} approvals+{len(approvals_posted()) - a0}")
                page.keyboard.press("Escape")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
                _box(page, 1).fill("")
                a0 = len(approvals_posted())
                gate = threading.Event()
                say("deny it cancel", gate=gate)
                waits(lambda: not mock.stt_script)
                _approval(mock, "w10card2")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=4)
                gate.set()
                check("'cancel' under a card discards nothing and answers nothing: the words are placed verbatim",
                      waits(lambda: box().strip() == "deny it cancel") and undo_bar().count() == 0, repr(box()))
                time.sleep(0.4)
                check("... and no approval was requested", len(approvals_posted()) == a0
                      and page.locator('[data-testid="approval-card"]').count() == 1)
                page.keyboard.press("Escape")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
                # Nothing is claimed under a card at all.
                a0 = len(approvals_posted())
                _approval(mock, "w10card3")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=4)
                st0 = len(stts())
                mock.stt_script = [{"text": "over"}]
                page.evaluate(OVER)
                time.sleep(0.6)
                check("with the card up the mic uploads nothing, so a spoken 'over' never reaches anything",
                      len(stts()) == st0 and len(approvals_posted()) == a0 and len(_sent(mock, "t1")) == s0)
                page.keyboard.press("Escape")
                until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
                mock.stt_script = []

            # ---- 8. a running turn: over steers -----------------------------
            def busy_over():
                reset("review")
                mock.emit("turn_started", {}, thread_id="t1")
                until(lambda: _chat(page, 1)["busy"], timeout=3)
                w["send_status"] = "steered"
                s0 = len(_sent(mock, "t1"))
                say("also check the logs over")
                got = until(lambda: _sent(mock, "t1")[s0:] or None, timeout=6)
                check("REVIEW + over on a busy chat steers its own turn",
                      bool(got) and got[-1].get("text") == "also check the logs", str(got))
                w.pop("send_status", None)
                mock.emit("turn_finished", {"stop": "end"}, thread_id="t1")
                until(lambda: not _chat(page, 1)["busy"], timeout=3)

            # ---- 8b. a box still being typed in is not sent ------------------
            def typing_guard():
                reset("review")
                s0 = len(_sent(mock, "t1"))
                w["send_status"] = "started"
                _box(page, 1).fill("half written")
                say("send it over")
                check("REVIEW + over while the owner typed a moment ago: the words go into the box beside theirs",
                      waits(lambda: box().strip() == "half written send it"), repr(box()))
                time.sleep(0.4)
                check("... and nothing was sent", len(_sent(mock, "t1")) == s0)
                check("the line says why", waits(lambda: "STILL TYPING" in page.locator('[data-testid="orb-status"]').inner_text()),
                      page.locator('[data-testid="orb-status"]').inner_text())
                check("the row is an ordinary review, not a send word", rows()[-1]["outcome"] == "review", str(rows()[-1]))
                time.sleep(3.2)
                say("and now over")
                got = until(lambda: _sent(mock, "t1")[s0:] or None, timeout=6)
                check("three seconds after the last keystroke the same word sends the box",
                      bool(got) and got[-1].get("text") == "half written send it and now", str(got))
                # AUTO never looked at the box: no guard there.
                reset("auto")
                s1 = len(_sent(mock, "t1"))
                _box(page, 1).fill("typed just now")
                say("send it over")
                got = until(lambda: _sent(mock, "t1")[s1:] or None, timeout=6)
                check("AUTO sends what was said at once, typing or not (it never sends the box)",
                      bool(got) and got[-1].get("text") == "send it" and box() == "typed just now", str(got))

            # ---- 8c. a cancel never deletes what was typed -------------------
            def cancel_keeps_typing():
                reset("review")
                s0 = len(_sent(mock, "t1"))
                say("ok", how=TIGHT)
                check("setup: 'ok' dictated", waits(lambda: box().strip() == "ok"))
                _box(page, 1).fill(box() + " book")
                say("cancel")
                check("'cancel' removes the dictated 'ok' and leaves the typed 'book' whole",
                      waits(lambda: box() == "book"), repr(box()))
                page.locator('[data-testid="voice-undo"]').click()
                check("Undo puts 'ok' back without touching 'book'", waits(lambda: box().strip() == "book ok"), repr(box()))
                _box(page, 1).fill("")
                say("go", how=TIGHT)
                waits(lambda: box().strip() == "go")
                _box(page, 1).fill("gone ahead")                  # the owner replaced it with their own words
                say("cancel")
                check("a dictated word that is no longer in the box deletes nothing: 'gone ahead' is intact",
                      waits(lambda: "NOTHING TO CANCEL" in page.locator('[data-testid="orb-status"]').inner_text() or undo_bar().count() == 1)
                      and box() == "gone ahead", repr(box()))
                check("... and the window says part of it stayed", "matches words you typed" in page.evaluate("window.__hud.state().error"),
                      page.evaluate("window.__hud.state().error"))
                check("nothing was sent", len(_sent(mock, "t1")) == s0)

            # ---- 8d. a stale Undo line does not sit on another pane ---------
            def undo_line_follows_the_hold():
                reset("review")
                say("first chat words cancel")
                check("setup: pane 1 offers Undo", waits(lambda: "CANCELLED" in (_chat(page, 1).get("status") or "")))
                _focus(page, until, 2)
                say("second chat words cancel")
                check("a cancel in pane 2 takes the Undo hold: pane 1's line is gone",
                      waits(lambda: "CANCELLED" not in (_chat(page, 1).get("status") or "")), str(_chat(page, 1).get("status")))
                check("... and pane 2 has it", "CANCELLED" in (_chat(page, 2).get("status") or ""), str(_chat(page, 2).get("status")))

            # ---- 9. the lists in Settings -> Voice --------------------------
            def settings_lists():
                reset("review")
                open_settings(page, "Voice")
                sec = page.locator('[data-testid="voice-words"]')
                check("Settings -> Voice has the word lists", sec.count() == 1)
                chips = lambda which: page.locator(f'[data-testid="voice-words-{which}-list"] [data-testid="voice-words-chip"]').all_inner_texts()
                check("the defaults are over and cancel", [c.strip("× \n") for c in chips("send")] == ["over"]
                      and [c.strip("× \n") for c in chips("cancel")] == ["cancel"], str(chips("send")))
                err = lambda: page.locator('[data-testid="voice-words-error"]').inner_text()
                add = lambda which, text: (page.locator(f'[data-testid="voice-words-{which}-input"]').fill(text),
                                           page.locator(f'[data-testid="voice-words-{which}-add"]').click())
                add("send", "two words")
                check("two words are refused with a reason", "One word" in err() and len(chips("send")) == 1, err())
                add("send", "cancel")
                check("a word already in the other list is refused", "other list" in err(), err())
                add("send", "x")
                check("a one-letter word is refused", "single word" in err().lower() or "2 to" in err(), err())
                add("send", "done")
                check("a valid word is added", [c.strip("× \n") for c in chips("send")] == ["over", "done"] and err() == "", str(chips("send")))
                for extra in ("go", "fire", "ship"):
                    add("send", extra)
                check("a list is bounded at five: the box and Add are switched off at the bound",
                      len(chips("send")) == 5 and page.locator('[data-testid="voice-words-send-input"]').is_disabled()
                      and page.locator('[data-testid="voice-words-send-add"]').is_disabled())
                page.locator('[data-testid="voice-words-send"] [data-testid="voice-words-remove"]').nth(2).click()
                check("a word can be removed", len(chips("send")) == 4)
                # Storage that refuses writes must not undo the edit on screen.
                page.evaluate("() => { Storage.prototype.setItem = function () { throw new Error('blocked'); }; }")
                add("send", "zeta")
                check("with localStorage blocked the edit still shows (and is simply forgotten at reload)",
                      "zeta" in [c.strip("× \n") for c in chips("send")], str(chips("send")))
                page.reload()                                  # the patch goes with the page
                page.wait_for_selector('[data-testid="sidebar"]', state="attached")
                page.wait_for_function("() => window.__hud && window.__hud.voiceDiagnostics().mic === 'available'")
                stored = page.evaluate("localStorage.getItem('jarvis.hud.voice.words')")
                check("the lists are stored (window-local)", stored is not None and '"done"' in stored, str(stored))
                page.reload()
                page.wait_for_selector('[data-testid="sidebar"]', state="attached")
                page.wait_for_function("() => window.__hud && window.__hud.voiceDiagnostics().mic === 'available'")
                check("and survive a reload", "done" in page.evaluate("window.__hud.voiceWords().send"))
                check("the read-only hook exposes the lists, not any text", set(page.evaluate("Object.keys(window.__hud.voiceWords())")) == {"enabled", "send", "cancel", "undo"})
                page.locator('[data-testid="settings-close"]').click() if page.locator('[data-testid="settings-page"]').count() else None

            def edited_lists_are_used():
                words = '{"enabled":true,"send":["done","go"],"cancel":["scratch"]}'
                reset("review", words)
                s0 = len(_sent(mock, "t1"))
                w["send_status"] = "started"
                say("ship the build done")
                got = until(lambda: _sent(mock, "t1")[s0:] or None, timeout=6)
                check("an added send word sends", bool(got) and got[-1].get("text") == "ship the build", str(got))
                say("send it over")
                check("a word that was taken out no longer does: it is prose",
                      waits(lambda: box().strip() == "send it over") and len(_sent(mock, "t1")) == s0 + 1, repr(box()))
                _box(page, 1).fill("")
                say("never mind scratch")
                check("an added cancel word discards", waits(lambda: undo_bar().count() == 1), "no undo")
                page.locator('[data-testid="voice-undo"]').click()
                _box(page, 1).fill("")
                # The toggle.
                reset("review", '{"enabled":false,"send":["over"],"cancel":["cancel"]}')
                s0 = len(_sent(mock, "t1"))
                say("send it over")
                check("with the feature off, over is just a word", waits(lambda: box().strip() == "send it over") and len(_sent(mock, "t1")) == s0, repr(box()))
                _box(page, 1).fill("")
                say("never mind cancel")
                check("... and so is cancel", waits(lambda: box().strip() == "never mind cancel") and undo_bar().count() == 0, repr(box()))
                _box(page, 1).fill("")
                # A list that is empty is that function off.
                reset("review", '{"enabled":true,"send":[],"cancel":["cancel"]}')
                s0 = len(_sent(mock, "t1"))
                say("send it over")
                check("an empty send list turns sending off", waits(lambda: box().strip() == "send it over") and len(_sent(mock, "t1")) == s0)
                _box(page, 1).fill("")
                # Hostile storage cannot widen anything.
                reset("review", '{"enabled":true,"send":["two words","<b>x</b>","over"],"cancel":[7,null,"cancel"]}')
                check("invalid stored entries are dropped", page.evaluate("window.__hud.voiceWords().send") == ["over"]
                      and page.evaluate("window.__hud.voiceWords().cancel") == ["cancel"])

            for section in (review_over_sends_box, auto_over, not_a_word, review_cancel_undo, auto_cancel_inflight,
                            cancel_not_a_recall, card_inert, busy_over, typing_guard, cancel_keeps_typing,
                            undo_line_follows_the_hold, settings_lists, edited_lists_are_used):
                run(section)
            context.close()
            browser.close()
        print(f"\n{count - len(failures)}/{count} checks passed; {len(failures)} failures")
        if failures:
            print("\n".join(failures))
        return 1 if failures else 0
    finally:
        mock.stop()


if __name__ == "__main__":
    raise SystemExit(main())
